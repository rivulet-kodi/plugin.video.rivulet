"""Subtitle discovery for playback (pure Python, no ``xbmc*`` imports).

Queries every installed Stremio addon that declares subtitle support for
the content being played, merges the results, drops duplicates/unusable
entries, and narrows what is left to the user's preferred language so the
player can hand Kodi a list it can safely auto-select from.

OpenSubtitles v3 (bundled in the Store's default addon list) accepts the
usual stream ``extra`` props -- ``filename`` and ``videoSize`` -- to
improve its match. ``videoHash`` is intentionally not computed here: it
requires reading bytes off the resolved media file, which this layer
doesn't have access to.

Upstream's subtitle resource also carries an optional ``fonts`` field
(custom font URLs for styled subtitles, stremio-core commit b1bd9b3f);
it is intentionally dropped here because Kodi's libass renderer cannot
fetch remote fonts, so carrying it through would be dead weight.
"""
from lib.stremio.addons import addon_supports

# ISO 639-1 (2-letter) -> ISO 639-2/B (3-letter) codes for the languages
# subtitle addons commonly report. Only used for the loose alias matching
# in filter_subtitles(); an unrecognized code simply won't gain an alias.
_ISO639_1_TO_3 = {
    'af': 'afr', 'am': 'amh', 'ar': 'ara', 'az': 'aze', 'be': 'bel',
    'bg': 'bul', 'bn': 'ben', 'bs': 'bos', 'ca': 'cat', 'cs': 'cze',
    'cy': 'wel', 'da': 'dan', 'de': 'ger', 'el': 'gre', 'en': 'eng',
    'eo': 'epo', 'es': 'spa', 'et': 'est', 'eu': 'baq', 'fa': 'per',
    'fi': 'fin', 'fil': 'fil', 'fr': 'fre', 'ga': 'gle', 'gl': 'glg',
    'gu': 'guj', 'he': 'heb', 'hi': 'hin', 'hr': 'hrv', 'hu': 'hun',
    'hy': 'arm', 'id': 'ind', 'is': 'ice', 'it': 'ita', 'ja': 'jpn',
    'ka': 'geo', 'kk': 'kaz', 'km': 'khm', 'kn': 'kan', 'ko': 'kor',
    'ku': 'kur', 'ky': 'kir', 'lo': 'lao', 'lt': 'lit', 'lv': 'lav',
    'mk': 'mac', 'ml': 'mal', 'mn': 'mon', 'mr': 'mar', 'ms': 'may',
    'mt': 'mlt', 'my': 'bur', 'ne': 'nep', 'nl': 'dut', 'no': 'nor',
    'pa': 'pan', 'pl': 'pol', 'pt': 'por', 'ro': 'rum', 'ru': 'rus',
    'si': 'sin', 'sk': 'slo', 'sl': 'slv', 'sq': 'alb', 'sr': 'srp',
    'sv': 'swe', 'sw': 'swa', 'ta': 'tam', 'te': 'tel', 'th': 'tha',
    'tl': 'tgl', 'tr': 'tur', 'uk': 'ukr', 'ur': 'urd', 'uz': 'uzb',
    'vi': 'vie', 'zh': 'chi',
}

# A handful of languages have distinct ISO 639-2/T (terminological) codes
# that also show up in the wild alongside the /B form above.
_ISO639_1_TO_3_ALT = {
    'de': 'deu', 'es': 'spa', 'fr': 'fra', 'hy': 'hye', 'is': 'isl',
    'ka': 'kat', 'mk': 'mkd', 'ms': 'msa', 'my': 'mya', 'nl': 'nld',
    'ro': 'ron', 'sk': 'slk', 'sq': 'sqi', 'zh': 'zho',
}

_ISO639_3_TO_1 = {three: two for two, three in _ISO639_1_TO_3.items()}
_ISO639_3_TO_1.update({three: two for two, three in _ISO639_1_TO_3_ALT.items()})


def _lang_aliases(code):
    """Return the set of language codes considered equivalent to `code`
    (itself, plus its ISO 639-1<->639-2 counterpart when known), lowercased."""
    code = (code or '').strip().lower()
    if not code:
        return set()
    aliases = {code}
    three = _ISO639_1_TO_3.get(code)
    if three:
        aliases.add(three)
    alt = _ISO639_1_TO_3_ALT.get(code)
    if alt:
        aliases.add(alt)
    two = _ISO639_3_TO_1.get(code)
    if two:
        aliases.add(two)
    return aliases


def _sanitize_label(label):
    """Return `label` (the addon's human-readable subtitle name, e.g.
    "English (SDH)" or "Forced") stripped of embedded CR/LF and leading
    /trailing whitespace, or `None` if it doesn't resolve to a non-empty
    string.

    `label` is optional per the upstream resource schema
    (stremio-core commit b1bd9b3f, src/types/resource/subtitles.rs), so
    unlike `lang` (which always defaults to `''`) an absent/blank label
    means the key is omitted from the result entirely rather than kept
    as an empty string.
    """
    if not label:
        return None
    text = str(label).replace('\r', '').replace('\n', '').strip()
    return text or None


def collect_subtitles(client, addons, rtype, rid, extra=None):
    """Query every addon in `addons` (Store descriptors, as returned by
    `Store.get_addons()`) that declares subtitle support for `rtype`/`rid`,
    merge their results, and return a flat list of `{id, lang, url}` dicts,
    each with an optional `label` key (see `_sanitize_label`) when the
    addon supplied a usable one.

    A per-addon request failure (network error, malformed JSON, whatever)
    is swallowed so one broken addon never hides subtitles from the rest.
    Entries without a usable `url` are dropped; duplicate URLs (across or
    within addons) keep only the first occurrence.
    """
    seen_urls = set()
    subs = []
    for descriptor in addons or []:
        manifest = descriptor.get('manifest') or {}
        if not addon_supports(manifest, 'subtitles', rtype, rid):
            continue
        transport_url = descriptor.get('transportUrl')
        if not transport_url:
            continue
        try:
            raw = client.subtitles(transport_url, rtype, rid, extra=extra)
        except Exception:  # noqa: BLE001 - one addon's failure must not sink the rest
            continue
        for item in raw or []:
            if not isinstance(item, dict):
                continue
            url = item.get('url')
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            entry = {
                'id': item.get('id') or url,
                'lang': str(item.get('lang') or ''),
                'url': url,
            }
            label = _sanitize_label(item.get('label'))
            if label is not None:
                entry['label'] = label
            subs.append(entry)
    return subs


def filter_subtitles(subs, preferred_lang):
    """Return only the `subs` entries whose language matches
    `preferred_lang` (case-insensitive, treating 2-letter/3-letter ISO
    639 codes for the same language as equal), preserving order; an
    unusable/empty `preferred_lang` passes everything through unchanged.

    Ordering alone is not enough for the player: `ListItem.setSubtitles()`
    takes bare paths, and Kodi derives an external subtitle's language
    from the filename of that path (`CUtil::GetExternalStreamDetails
    FromFilename` -> `URIUtils::GetFileName`, query string stripped).
    Addon subtitle URLs end in opaque numeric ids, so every attached
    track reaches Kodi with an empty language and Kodi's auto-selection
    picks an arbitrary one. Handing it a single-language list makes that
    arbitrary pick harmless.

    Returns [] when nothing matches - attaching a wrong-language track is
    worse than attaching none, since the file's own embedded tracks do
    carry proper language metadata for Kodi to choose from.
    """
    preferred = _lang_aliases(preferred_lang)
    if not preferred:
        return list(subs or [])
    return [sub for sub in subs or [] if _lang_aliases(sub.get('lang')) & preferred]
