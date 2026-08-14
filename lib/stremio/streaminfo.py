"""Stream label/metadata parsing for the streams view (pure Python, no
``xbmc*`` imports).

Stremio addons put wildly inconsistent, emoji-laden text into
``Stream.name``/``Stream.title``/``Stream.description`` (e.g. AIOStreams:
``name='[AIOStreams Stable] 4K (p2p)'``, ``title='\U0001F4C0 The Batman
(2022)\n\U0001F3A5 HEVC \U0001F39E 10bit \u00b7 DV \u00b7 HDR10+\n...'``).
Kodi's default skin font can't render most of those glyphs (tofu boxes),
and the raw multi-line text wraps into unreadable multi-row list entries.

This module turns that mess into one clean, single-line, colour-coded
label plus a structured info dict, following the label-formatting /
colour-tag recipe recovered from Stream4Me's ``platformcode/unify.py``
(``title_format()``/``set_color()``/``remove_format()``): strip Kodi/host
junk first, then rebuild a single ``[COLOR ..]..[/COLOR]`` flavoured
label from scratch rather than trying to salvage the original markup.

``parse_stream()`` also re-derives audio/channels/languages/bitrate/
group/tracker/service/cached/release as plain text (never emoji -- Kodi's
skin font can't render those either) for ``format_details()``'s second
row line and ``format_plot()``'s info panel. Sources, all verified
against the addons' own code: Torrentio ``addon/moch/moch.js`` (debrid
bracket markers, short codes), Comet ``comet/api/endpoints/stream.py``
(Kodi ``[RD C]``/``[RD U]`` markers and ``cometKodiMetaV1`` structured
metadata), StremThru ``internal/stremio/transformer/
stream_template_default.go`` (emoji cache/tracker/group markers), and
AIOStreams ``packages/core/src/{parser/regex.ts,utils/
formatter-definitions.ts}`` (audio-tag/channel/release-group/language
vocabularies and the built-in cache-state templates). Several of those
markers (U+26A1, U+274C, U+1F39F, U+1F3AB, all flag-emoji pairs) are
inside ``clean_text()``'s strip ranges or above the BMP, so anything
derived from one is parsed from the pre-``clean_text()`` text, not
``raw``.
"""
import re

# ---------------------------------------------------------------------------
# clean_text
# ---------------------------------------------------------------------------

# Astral-plane emoji (\U0001F300-\U0001FAFF etc.), regional-indicator flag
# pairs and the like are all above the BMP -- stripped wholesale below by
# the `\U00010000-\U0010FFFF` class in `_JUNK_RE`. What's left is BMP junk
# Kodi's font still can't render: Misc Symbols + Dingbats (weather/emoji
# glyphs like the U+26A1 "high voltage" bolt in AIOStreams' "3.3 Mbps"
# line), stray zero-width joiners/spaces some addons use to defeat
# text-truncation, and the variation-selector-16 that forces emoji
# presentation on an otherwise printable codepoint (e.g. U+2764 U+FE0F).
_JUNK_RANGES = (
    (0x2600, 0x27BF),  # Misc Symbols + Dingbats
    (0x200B, 0x200D),  # zero-width space / ZWNJ / ZWJ
)
_JUNK_SINGLES = frozenset([0xFE0F])  # variation selector-16 (emoji style)


def _build_junk_re():
    """One compiled character class covering `_JUNK_RANGES`, `_JUNK_SINGLES`
    and everything above the BMP, so `clean_text()` strips junk in a single
    C-level `re.sub()` pass instead of a per-codepoint Python loop.
    """
    spans = [(chr(lo), chr(hi)) for lo, hi in _JUNK_RANGES]
    spans.append((chr(0x10000), chr(0x10FFFF)))  # everything outside the BMP
    parts = ['%s-%s' % (re.escape(lo), re.escape(hi)) for lo, hi in spans]
    parts.extend(re.escape(chr(cp)) for cp in sorted(_JUNK_SINGLES))
    return re.compile('[%s]' % ''.join(parts))


_JUNK_RE = _build_junk_re()

_WHITESPACE_RE = re.compile(r'\s+')

# Hard cap on input length, applied BEFORE any of the regex/iteration work
# below runs. A Stremio addon is semi-trusted, arbitrary user-installed
# code -- a malicious or simply broken one can hand us a title/description
# of unbounded size, and every extra character costs a linear scan plus a
# regex substitution for no benefit: Kodi's ListItem label has a practical
# on-screen display limit far below this cap, so nothing past it would
# ever usefully render anyway. Comfortably above any real stream title or
# multi-line AIOStreams-style description, well below "adversarial input".
_MAX_TEXT_LEN = 4000


def clean_text(s):
    """Strip emoji/symbol junk Kodi can't render and collapse whitespace.

    Keeps Latin/Latin-1 letters (accents included), digits, common
    punctuation and CJK/Cyrillic/etc. text untouched -- only the specific
    junk ranges above and anything outside the BMP are removed. Any run
    of whitespace, including embedded newlines, collapses to one space.

    Input longer than `_MAX_TEXT_LEN` is truncated up front, before any
    regex pass runs, so a hostile/broken addon can't burn CPU cleaning a
    huge string. Truncation is on code points (never mid-surrogate), so
    it can't land inside a multi-codepoint emoji sequence in a way that
    raises -- worst case a partial glyph gets filtered out below anyway.
    """
    if not s:
        return ''
    truncated = str(s)[:_MAX_TEXT_LEN]
    filtered = _JUNK_RE.sub('', truncated)
    return _WHITESPACE_RE.sub(' ', filtered).strip()


# ---------------------------------------------------------------------------
# parse_stream
# ---------------------------------------------------------------------------

_RESOLUTION_PATTERNS = (
    ('2160p', re.compile(r'\b(2160p|4k|uhd)\b', re.I)),
    ('1080p', re.compile(r'\b1080p\b', re.I)),
    ('720p', re.compile(r'\b720p\b', re.I)),
    ('480p', re.compile(r'\b480p\b', re.I)),
)

# Checked most-specific-first: a "BluRay REMUX" release should report as
# Remux, and "WEB-DL" must win over the more generic "WEB"/"WEBRip".
_SOURCE_PATTERNS = (
    ('Remux', re.compile(r'\bremux\b', re.I)),
    ('BluRay', re.compile(r'\b(bluray|blu-ray|bdrip|brrip)\b', re.I)),
    ('WEB-DL', re.compile(r'\bweb[-.]?dl\b', re.I)),
    ('WEB', re.compile(r'\bweb\s*rip\b|\bweb\b', re.I)),
    ('HDTV', re.compile(r'\bhdtv\b', re.I)),
    ('CAM', re.compile(r'\b(hdcam|hdts|cam|ts)\b', re.I)),
)

_CODEC_PATTERNS = (
    ('HEVC', re.compile(r'\b(hevc|h\.?265|x265)\b', re.I)),
    ('AV1', re.compile(r'\bav1\b', re.I)),
    ('x264', re.compile(r'\b(x264|h\.?264|avc)\b', re.I)),
)

# Order here only seeds the containment check below; the returned list is
# re-ordered by position of first appearance in the source text.
_HDR_PATTERNS = (
    ('DV', re.compile(r'\b(dv|dolby\s*vision)\b', re.I)),
    ('HDR10+', re.compile(r'\bhdr10\s*\+|\bhdr10plus\b', re.I)),
    ('HDR10', re.compile(r'\bhdr10\b', re.I)),
    ('HLG', re.compile(r'\bhlg\b', re.I)),
    ('HDR', re.compile(r'\bhdr\b', re.I)),
)

_SIZE_RE = re.compile(r'(\d+(?:\.\d+)?)\s*(GB|MB|KB|TB)\b', re.I)
_UNIT_MULTIPLIERS = {'KB': 1024, 'MB': 1024 ** 2, 'GB': 1024 ** 3, 'TB': 1024 ** 4}

# Marker-scoped seeder count, tried FIRST and against the PRE-clean text:
# Torrentio renders "\U0001F464 109 \U0001F4BE 54.33 GB \u2699\uFE0F TorrentGalaxy"
# (its own `toStreamInfo`, seeders before size), and the marker is astral so
# `clean_text()` deletes it, leaving a bare "109 54.33 GB" the heuristics
# below cannot tell from any other number. Verified against 100 live
# Torrentio streams: every one carries the marker, and matching it takes
# seeder coverage from 5% to 100% while removing four wrong values the
# `_SEEDS_AFTER_UNIT_RE` fallback had guessed from unrelated digits.
_SEEDS_MARKER_RE = re.compile('\U0001F464\uFE0F?\\s*(\\d+)')

# Labeled seeder counts, tried after the marker above: 'Seeds: 50',
# 'Seeders 50', 'Seed-50', 'S:12'.
_SEEDS_LABELED_PATTERNS = (
    re.compile(r'\bseed(?:ers|s)?\s*[:\-]?\s*(\d+)', re.I),
    re.compile(r'\b(?:se|sd)\s*[:\-]\s*(\d+)\b', re.I),
    re.compile(r'\bs\s*:\s*(\d+)\b', re.I),
)
# Last-resort fallback: some addons (AIOStreams included) put the peer/seed
# count right after the size/speed group, introduced by a person emoji that
# clean_text() has already stripped down to bare whitespace, e.g.
# "... 4.39 GB \u00b7 3.3 Mbps \u00b7 50 Il Corsaro Viola". Take the LAST
# such "<unit> ... <int>" match in the text, since seeders is conventionally
# the trailing group after size/speed. Deliberately last: it is the loosest
# rule here and will happily read an unrelated trailing number (a codec's
# digits, an episode count) as a swarm size, so anything that names its
# seeders explicitly must win before it is consulted.
_SEEDS_AFTER_UNIT_RE = re.compile(
    r'(?:gb|mb|kb|tb|mbps|kbps|kb/s|mb/s|gbps)\b\D{0,8}?(\d+)\b', re.I
)

# Sanity ceiling for a parsed seeder count. Python ints are arbitrary
# precision, so a crafted string like "GB 99999999999999999999 seeders"
# won't crash -- it'll just produce a number that sorts/displays as
# nonsense. No real torrent swarm gets anywhere near this many seeders,
# so treat anything above it as unparseable rather than a literal value.
_MAX_PLAUSIBLE_SEEDERS = 1_000_000


def _first_nonempty_line(text):
    """First line of `text` that survives clean_text(), or ''."""
    if not text:
        return ''
    for line in str(text).split('\n'):
        cleaned = clean_text(line)
        if cleaned:
            return cleaned
    return ''


def _match_first(patterns, text):
    for tag, pattern in patterns:
        if pattern.search(text):
            return tag
    return ''


def _dedupe_tags(patterns, text, limit=None):
    """Match each ``(tag, pattern)`` against `text`, keep only the first
    non-contained match per pattern, then return tags ordered by position
    of first appearance.

    A later pattern's match that falls entirely inside an already-
    consumed span is dropped -- e.g. checking 'DTS-HD MA' before
    'DTS-HD' before bare 'DTS' means "DTS-HD MA" wins over the "DTS-HD"
    and "DTS" it also contains, with no lookahead/lookbehind gymnastics
    needed for the narrower patterns.
    """
    found = []
    consumed_spans = []
    for tag, pattern in patterns:
        match = pattern.search(text)
        if not match:
            continue
        span = match.span()
        if any(lo <= span[0] and span[1] <= hi for lo, hi in consumed_spans):
            continue
        found.append((span[0], tag))
        consumed_spans.append(span)
    found.sort(key=lambda item: item[0])
    tags = [tag for _, tag in found]
    return tags[:limit] if limit else tags


def _parse_hdr(text):
    return _dedupe_tags(_HDR_PATTERNS, text)


def _human_size(num_bytes):
    if num_bytes is None:
        return ''
    size = float(num_bytes)
    for unit in ('B', 'KB', 'MB', 'GB'):
        if size < 1024.0:
            return '%d %s' % (size, unit) if unit == 'B' else '%.2f %s' % (size, unit)
        size /= 1024.0
    return '%.2f TB' % size


def _parse_size(behavior_hints, text):
    video_size = (behavior_hints or {}).get('videoSize')
    try:
        video_size = int(video_size)
    except (TypeError, ValueError):
        video_size = None
    if video_size and video_size > 0:
        return video_size, _human_size(video_size)

    match = _SIZE_RE.search(text)
    if not match:
        return None, ''
    value, unit = match.group(1), match.group(2).upper()
    size_bytes = int(round(float(value) * _UNIT_MULTIPLIERS[unit]))
    return size_bytes, '%s %s' % (value, unit)


def _bounded_seeders(value):
    """`value` as a seeder count, or None when it exceeds the plausible
    ceiling (see `_MAX_PLAUSIBLE_SEEDERS`)."""
    seeders = int(value)
    return seeders if seeders <= _MAX_PLAUSIBLE_SEEDERS else None


def _parse_seeders(text, pre_clean=''):
    """Seeder count from `text` (cleaned), preferring the marker in
    `pre_clean` when the addon emitted one.

    Most specific first: a marker names the number as seeders, a label
    spells it out, and only then does the loose positional fallback get
    to guess. `pre_clean` is required for the marker because
    `clean_text()` strips the emoji that carries it.
    """
    match = _SEEDS_MARKER_RE.search(pre_clean)
    if match:
        return _bounded_seeders(match.group(1))
    for pattern in _SEEDS_LABELED_PATTERNS:
        match = pattern.search(text)
        if match:
            return _bounded_seeders(match.group(1))
    matches = list(_SEEDS_AFTER_UNIT_RE.finditer(text))
    if matches:
        return _bounded_seeders(matches[-1].group(1))
    return None


# ---------------------------------------------------------------------------
# audio / channels / bitrate
# ---------------------------------------------------------------------------

# Verbatim from AIOStreams packages/core/src/parser/regex.ts `audioTags`,
# most-specific-first so `_dedupe_tags()`'s containment check lets e.g.
# "DTS-HD MA" win over the "DTS-HD"/"DTS" it also contains, and
# "e-ac-3"/"dolby digital plus" win over the "ac-3" they also contain.
_AUDIO_PATTERNS = (
    ('Atmos', re.compile(r'atmos|ddpa\d?', re.I)),
    ('DD+', re.compile(r'dolby\s*digital\s*plus|e[\-\s]?ac[\-\s]?3', re.I)),
    ('DD', re.compile(r'dolby\s*digital|ac[\-\s]?3', re.I)),
    ('DTS:X', re.compile(r'dts[ .\-:_]?x', re.I)),
    ('DTS-HD MA', re.compile(r'dts[ .\-_]?hd[ .\-_]?ma', re.I)),
    ('DTS-HD', re.compile(r'dts[ .\-_]?hd(?![ .\-_]?ma)', re.I)),
    ('DTS-ES', re.compile(r'dts[ .\-_]?es', re.I)),
    ('DTS', re.compile(r'\bdts\b', re.I)),
    ('TrueHD', re.compile(r'true[ .\-_]?hd', re.I)),
    ('OPUS', re.compile(r'\bopus\b', re.I)),
    ('FLAC', re.compile(r'\bflac\b', re.I)),
    ('AAC', re.compile(r'\bq?aac\b', re.I)),
)

# Verbatim from AIOStreams regex.ts `audioChannels`: only these four
# configurations are recognised. The separator is mandatory (unlike the
# optional one in the upstream source) so a bare digit pair from
# somewhere else entirely -- "S:20" seeders, a "2021" year -- can't
# false-positive as a channel layout.
_CHANNELS_RE = re.compile(r'\b([2567])[ .\-_]([01])(?:ch)?\b', re.I)
_VALID_CHANNELS = {('2', '0'): '2.0', ('5', '1'): '5.1', ('6', '1'): '6.1', ('7', '1'): '7.1'}

# AIOStreams/StremThru render bitrate as e.g. "25.5 Mbps"/"130 Mbps" --
# normalize unit casing/spelling so "kb/s"-style variants collapse to the
# same "Kbps"/"Mbps"/"Gbps" those addons show.
_BITRATE_RE = re.compile(r'(\d+(?:\.\d+)?)\s*(kbps|mbps|gbps|kb/s|mb/s|gb/s)', re.I)
_BITRATE_UNIT_NORMALIZE = {
    'kbps': 'Kbps', 'kb/s': 'Kbps',
    'mbps': 'Mbps', 'mb/s': 'Mbps',
    'gbps': 'Gbps', 'gb/s': 'Gbps',
}


def _parse_audio_tags(text):
    return _dedupe_tags(_AUDIO_PATTERNS, text)


def _match_channels(text):
    for match in _CHANNELS_RE.finditer(text):
        value = _VALID_CHANNELS.get((match.group(1), match.group(2)))
        if value:
            return value
    return ''


def _parse_bitrate(text):
    match = _BITRATE_RE.search(text)
    if not match:
        return ''
    return '%s %s' % (match.group(1), _BITRATE_UNIT_NORMALIZE[match.group(2).lower()])


# ---------------------------------------------------------------------------
# release markers (DV profile / edition / provenance)
# ---------------------------------------------------------------------------

# Issue #4: confirmed NOT derived by any of the four addons above -- these
# exist only inside release names/filenames (scene-naming convention),
# never a vendor-emitted field. The DV-profile group is gated behind
# `_DV_HINT_RE`: bare "P8"-style tokens are common enough elsewhere
# (episode/part numbers, resolutions) that matching them unconditionally
# would false-positive.
_DV_HINT_RE = re.compile(r'\b(?:dv|dolby\s*vision)\b', re.I)
_DV_PROFILE_PATTERNS = (
    ('FEL', re.compile(r'\bfel\b|bl\+el\+rpu', re.I)),
    ('MEL', re.compile(r'\bmel\b', re.I)),
    ('BL+RPU', re.compile(r'bl\+rpu', re.I)),
    ('P5', re.compile(r'\bp5\b', re.I)),
    ('P7', re.compile(r'\bp7\b|profile\s*7', re.I)),
    ('P8', re.compile(r'\bp8\b', re.I)),
    ('Hybrid', re.compile(r'\bhybrid\b', re.I)),
)
_RELEASE_ALWAYS_PATTERNS = (
    ('Extended', re.compile(r'\bextended\b', re.I)),
    ("Director's Cut", re.compile(r"director'?s\s*cut", re.I)),
    ('Theatrical', re.compile(r'\btheatrical\b', re.I)),
    ('Remastered', re.compile(r'\bremaster(?:ed)?\b', re.I)),
    ('IMAX', re.compile(r'\bimax\b', re.I)),
    ('Open Matte', re.compile(r'\bopen\s*matte\b', re.I)),
    ('Criterion', re.compile(r'\bcriterion\b', re.I)),
    ('REPACK', re.compile(r'\brepack\b', re.I)),
    ('PROPER', re.compile(r'\bproper\b', re.I)),
    ('10bit', re.compile(r'\b10\s*bit\b', re.I)),  # AIOStreams VISUAL_TAGS spelling
)
_MAX_RELEASE = 6


def _parse_release(text):
    patterns = _RELEASE_ALWAYS_PATTERNS
    if _DV_HINT_RE.search(text):
        patterns = _DV_PROFILE_PATTERNS + patterns
    return _dedupe_tags(patterns, text, limit=_MAX_RELEASE)


# ---------------------------------------------------------------------------
# languages
# ---------------------------------------------------------------------------

# Audio-track descriptors, not languages: no real title contains them, so
# they are matched anywhere in the text (Torrentio's own language line is
# unlabelled, e.g. "Dual Audio / \U0001F1EC\U0001F1E7 / \U0001F1EE\U0001F1F3").
_LANGUAGE_TAG_NAMES = (
    ('Multi', 'MULTI'),
    ('Dual Audio', 'DUAL'),
    ('Dubbed', 'DUB'),
)

# Full-name vocabulary, verbatim from AIOStreams regex.ts `languages`.
# Matched as whole words only -- never the 3-letter ISO abbreviations
# ("per"/"pan"/"mar"/"lat" false-positive against ordinary release-name
# words) and only inside a marked language segment (see
# `_LANGUAGE_SEGMENT_RES`), since the names themselves are ordinary
# English words that occur in real titles: "The Italian Job", "The
# French Connection", "My Big Fat Greek Wedding".
_LANGUAGE_NAMES = (
    ('Portuguese (Brazil)', 'PT-BR'),
    ('English', 'EN'), ('Japanese', 'JA'), ('Chinese', 'ZH'), ('Russian', 'RU'),
    ('Arabic', 'AR'), ('Portuguese', 'PT'), ('Spanish', 'ES'), ('French', 'FR'),
    ('German', 'DE'), ('Italian', 'IT'), ('Korean', 'KO'), ('Hindi', 'HI'),
    ('Bengali', 'BN'), ('Punjabi', 'PA'), ('Marathi', 'MR'), ('Gujarati', 'GU'),
    ('Tamil', 'TA'), ('Telugu', 'TE'), ('Kannada', 'KN'), ('Malayalam', 'ML'),
    ('Thai', 'TH'), ('Vietnamese', 'VI'), ('Indonesian', 'ID'), ('Turkish', 'TR'),
    ('Hebrew', 'HE'), ('Persian', 'FA'), ('Ukrainian', 'UK'), ('Greek', 'EL'),
    ('Lithuanian', 'LT'), ('Latvian', 'LV'), ('Estonian', 'ET'), ('Polish', 'PL'),
    ('Czech', 'CS'), ('Slovak', 'SK'), ('Hungarian', 'HU'), ('Romanian', 'RO'),
    ('Bulgarian', 'BG'), ('Serbian', 'SR'), ('Croatian', 'HR'), ('Slovenian', 'SL'),
    ('Dutch', 'NL'), ('Danish', 'DA'), ('Finnish', 'FI'), ('Swedish', 'SV'),
    ('Norwegian', 'NB'), ('Malay', 'MS'), ('Latino', 'ES-LA'),
)


def _build_language_patterns(names):
    patterns = []
    for name, code in names:
        if name == 'Portuguese':
            # Don't also count "Portuguese (Brazil)" as bare Portuguese.
            pattern = re.compile(r'(?<!\w)Portuguese(?!\w)(?!\s*\(Brazil\))', re.I)
        else:
            pattern = re.compile(r'(?<!\w)%s(?!\w)' % re.escape(name), re.I)
        patterns.append((code, pattern))
    return patterns


# Compiled lazily on first use so a navigation that never shows a stream
# never pays to compile ~50 language-name patterns (see `_scan_languages()`).
_LANGUAGE_PATTERNS_CACHE = None
_LANGUAGE_TAG_PATTERNS_CACHE = None
_LANGUAGE_NAME_TO_CODE = {
    name.lower(): code for name, code in _LANGUAGE_TAG_NAMES + _LANGUAGE_NAMES
}


def _language_patterns():
    global _LANGUAGE_PATTERNS_CACHE
    if _LANGUAGE_PATTERNS_CACHE is None:
        _LANGUAGE_PATTERNS_CACHE = _build_language_patterns(_LANGUAGE_NAMES)
    return _LANGUAGE_PATTERNS_CACHE


def _language_tag_patterns():
    global _LANGUAGE_TAG_PATTERNS_CACHE
    if _LANGUAGE_TAG_PATTERNS_CACHE is None:
        _LANGUAGE_TAG_PATTERNS_CACHE = _build_language_patterns(_LANGUAGE_TAG_NAMES)
    return _LANGUAGE_TAG_PATTERNS_CACHE

# Regional-indicator flag pairs (U+1F1E6-U+1F1FF, one per letter A-Z)
# decode arithmetically to a 2-letter country code; AIOStreams'
# `languageEmojis` renders these instead of full names for some formats.
# `clean_text()` drops them wholesale (they're above the BMP, covered by
# `_JUNK_RE`'s astral range), so they only
# survive in pre-clean text. Override table for the country codes that
# don't match their language's own code; anything else falls back to the
# country code itself, per AIOStreams' own emoji choices.
_FLAG_PAIR_RE = re.compile('[\U0001F1E6-\U0001F1FF]{2}')
_FLAG_LANGUAGE_OVERRIDES = {
    'GB': 'EN', 'US': 'EN', 'BR': 'PT-BR', 'CN': 'ZH', 'JP': 'JA', 'KR': 'KO',
    'SA': 'AR', 'IN': 'HI', 'BD': 'BN', 'PK': 'PA', 'IL': 'HE', 'IR': 'FA',
    'UA': 'UK', 'GR': 'EL', 'CZ': 'CS', 'MY': 'MS', 'VN': 'VI', 'MX': 'ES',
    'DK': 'DA', 'SE': 'SV', 'NO': 'NB', 'EE': 'ET', 'SI': 'SL', 'RS': 'SR',
}

# Language-segment markers: every verified emitter labels the segment it
# puts languages in -- AIOStreams gdrive/minimalisticgdrive U+1F30E,
# lightgdrive + StremThru + Comet U+1F310, prism U+1F5E3, StremThru's
# with-subtitles variant U+1F399, torbox the literal "Languages:".
# U+1F30E doubles as AIOStreams' `multi` language emoji, so a globe whose
# segment carries no letters (e.g. `\U0001F30E / \U0001F1EC\U0001F1E7`,
# an emoji-only list) means "Multi" rather than "names follow".
_MULTI_GLOBE_MARKER = re.compile('\U0001F30E')
_LANGUAGE_SEGMENT_RES = (
    _MULTI_GLOBE_MARKER,
    re.compile('\U0001F310'),
    re.compile('\U0001F5E3\uFE0F?'),
    re.compile('\U0001F399\uFE0F?'),
    re.compile(r'Languages:', re.I),
)
_LETTER_RE = re.compile('[A-Za-z]')
_MAX_LANGUAGES = 6


def _decode_flag_pair(pair):
    letters = (chr(0x41 + (ord(ch) - 0x1F1E6)) for ch in pair)
    country = ''.join(letters)
    return _FLAG_LANGUAGE_OVERRIDES.get(country, country)


def _scan_languages(pre_clean):
    matches = []
    for marker in _LANGUAGE_SEGMENT_RES:
        offset, segment = _segment_after(pre_clean, marker)
        if not segment:
            continue
        for code, pattern in _language_patterns():
            match = pattern.search(segment)
            if match:
                matches.append((offset + match.start(), code))
        if marker is _MULTI_GLOBE_MARKER and not _LETTER_RE.search(segment):
            matches.append((offset, 'MULTI'))
    # Flag pairs and audio-track tags need no marker: neither a
    # regional-indicator pair nor "Dual Audio" belongs to a real title,
    # so wherever they appear they are a language claim.
    for match in _FLAG_PAIR_RE.finditer(pre_clean):
        matches.append((match.start(), _decode_flag_pair(match.group(0))))
    for code, pattern in _language_tag_patterns():
        match = pattern.search(pre_clean)
        if match:
            matches.append((match.start(), code))
    matches.sort(key=lambda item: item[0])
    seen = set()
    result = []
    for _, code in matches:
        if code in seen:
            continue
        seen.add(code)
        result.append(code)
        if len(result) >= _MAX_LANGUAGES:
            break
    return result


# ---------------------------------------------------------------------------
# tracker / release-group markers
# ---------------------------------------------------------------------------

# Field-marker emoji, from the verified templates (semantics differ per
# addon -- see module docstring). All are stripped by `clean_text()`
# (Misc Symbols/Dingbats, or astral), so captured only from pre-clean text.
_TRACKER_MARKER_PATTERNS = (
    re.compile(r'\U0001F50D\uFE0F?'),  # AIOStreams gdrive/lightgdrive, StremThru
    re.compile(r'\U0001F50E\uFE0F?'),  # Comet
    re.compile(r'\U0001F517\uFE0F?'),  # StremThru site fallback
    re.compile(r'\U0001F4E1\uFE0F?'),  # AIOStreams network
)
_GROUP_MARKER_RE = re.compile(r'\U0001F3F7\uFE0F?')  # AIOStreams gdrive/lightgdrive, Comet
# Torrentio's own title and AIOStreams' torrentio format use this as the
# tracker; StremThru uses it as the group. Deterministic per the shared
# precedence rule: whichever field hasn't already been filled wins it.
_GEAR_MARKER_RE = re.compile(r'\u2699\uFE0F?')

# A marker's value ends at the next newline or the next marker-ish
# codepoint on the same line (covers every emoji used as a field marker
# above, plus the misc-symbol/arrow ranges some addons use as
# separators) -- e.g. Torrentio's "... \U0001f464 16 \U0001f4be 22.42 GB
# \u2699\ufe0f 1337x" puts several marker/value pairs on one physical line.
_MARKER_STOP_RE = re.compile(r'[\U00010000-\U0010FFFF\u2300-\u27BF\u2B00-\u2BFF]')
# Capped so a malformed/adversarial payload can't push a paragraph of
# text into what's meant to be a one- or two-word tracker/group name.
_MAX_MARKER_LEN = 24


def _sanitize_marker(value):
    return clean_text(value)[:_MAX_MARKER_LEN].strip()


def _segment_after(text, pattern):
    """``(offset, text)`` of the run following `pattern`'s first match,
    ending at the next newline or field-marker codepoint; ``(0, '')``
    when the marker is absent."""
    match = pattern.search(text)
    if not match:
        return 0, ''
    rest = text[match.end():]
    stop = rest.find('\n')
    if stop == -1:
        stop = len(rest)
    boundary = _MARKER_STOP_RE.search(rest, 0, stop)
    if boundary:
        stop = boundary.start()
    return match.end(), rest[:stop]


def _marker_value(text, pattern):
    return _sanitize_marker(_segment_after(text, pattern)[1])


# Ported from AIOStreams packages/core/src/parser/regex.ts `releaseGroup`,
# used as the `group` fallback when no marker captured one -- e.g.
# Comet's Kodi `name` field is plain pipe-delimited text with no marker
# emoji at all.
_RELEASE_GROUP_RE = re.compile(
    r'-[. ]?(?!\d+$|S\d+|\d+x|ep?\d+|[^\[]+\]$)'
    r'([^\-. \[]+[^\-. \[)\]\d][^\-. \[)\]]*)'
    r'(?:\[[\w.-]+\])?'
    r'(?=\)|[.-]+\w{2,4}$|$)',
    re.I,
)


def _parse_group_from_filename(filename):
    if not filename:
        return ''
    match = _RELEASE_GROUP_RE.search(filename)
    return _sanitize_marker(match.group(1)) if match else ''


def _parse_tracker_group(pre_clean, filename, addon):
    # AIOStreams' prism format puts the ADDON name behind U+1F50D
    # ("\U0001F50D{addon.name}") and its indexer behind U+1F4E1, while
    # gdrive/lightgdrive/StremThru put the indexer behind U+1F50D. A
    # capture that just repeats the addon name is never the tracker
    # (the row already shows the addon), so skip it and keep looking.
    tracker = ''
    for pattern in _TRACKER_MARKER_PATTERNS:
        value = _marker_value(pre_clean, pattern)
        if value and value.lower() != addon.lower():
            tracker = value
            break

    gear_value = _marker_value(pre_clean, _GEAR_MARKER_RE)
    gear_is_tracker = False
    if not tracker and gear_value:
        tracker = gear_value
        gear_is_tracker = True

    group = _marker_value(pre_clean, _GROUP_MARKER_RE)
    if not group:
        if gear_value and not gear_is_tracker:
            group = gear_value
        else:
            group = _parse_group_from_filename(filename)

    return tracker, group


# ---------------------------------------------------------------------------
# debrid service / cache state
# ---------------------------------------------------------------------------

# Union of every short code the four addons are verified to emit (see
# module docstring for sources). A bracketed/parenthesised token is only
# ever treated as a service if it's in this list -- real payloads contain
# plenty of bracketed non-services ("[SEV]", "[P2P]", "[AIOStreams
# Stable]") that must yield `service == ''`, not a false match.
_SERVICE_CODES = (
    'RD', 'PM', 'AD', 'DL', 'ED', 'OC', 'TB', 'Putio', 'ST', 'DB', 'PP',
    'TORRENT', 'DR', 'TI', 'SN', 'ND', 'AIO', 'AM', 'P.IO', 'EN', 'PKP', 'SDR',
)
_SERVICE_ALT = '|'.join(re.escape(code) for code in sorted(_SERVICE_CODES, key=len, reverse=True))

# Plain-text markers (verbatim casing per addon template), checked first
# against the cleaned `raw` text -- text patterns win over emoji markers
# per the precedence rule. Every pattern here names its service AND its
# state, so it is safe to match anywhere in the text.
_CACHE_TEXT_PATTERNS = (
    (re.compile(r'\[(%s)\+\]' % _SERVICE_ALT), True),                      # Torrentio / AIOStreams torrentio format
    (re.compile(r'\[(%s)\s+download\]' % _SERVICE_ALT), False),            # Torrentio / AIOStreams torrentio format
    (re.compile(r'\[(%s)\s+C\]' % _SERVICE_ALT), True),                    # Comet Kodi mode
    (re.compile(r'\[(%s)\s+U\]' % _SERVICE_ALT), False),                   # Comet Kodi mode
    (re.compile(r'\(Instant\s+(%s)\)' % _SERVICE_ALT), True),              # AIOStreams torbox format
    (re.compile(r'\bNot\s+Ready\b[^()]*\((%s)\)' % _SERVICE_ALT), False),  # AIOStreams prism format
    (re.compile(r'\bReady\b[^()]*\((%s)\)' % _SERVICE_ALT), True),         # AIOStreams prism format
)
# Emoji-only markers, checked against the pre-clean text when no text
# pattern matched -- U+26A1/U+23F3/U+2B07 are what StremThru/AIOStreams
# gdrive/lightgdrive/Comet non-Kodi mode use instead of words.
_CACHE_EMOJI_PATTERNS = (
    (re.compile(r'\u26a1\ufe0f?\s*\[(%s)\]' % _SERVICE_ALT), True),              # StremThru cached
    (re.compile(r'\[(%s)\s*\u26a1\ufe0f?\]' % _SERVICE_ALT), True),              # AIOStreams gdrive/lightgdrive cached
    (re.compile(r'\[(%s)\s*(?:\u23f3|\u2b07\ufe0f?)\]' % _SERVICE_ALT), False),  # gdrive/lightgdrive/Comet non-Kodi uncached
)
# Last resort: a bare code with no state marker at all means "uncached"
# in exactly two templates - AIOStreams' torbox format, whose cached form
# is the `(Instant RD)` above, and StremThru's, whose cached form carries
# the bolt above. Both put it in `Stream.name`, and only there is it
# unambiguous: a bare `[EN]`/`[ST]`/`(RD)` elsewhere in a title or
# description is far more likely a language/release tag than a debrid
# verdict, and claiming "not cached" on that guess is worse than saying
# nothing.
_CACHE_BARE_PATTERNS = (
    (re.compile(r'\((%s)\)' % _SERVICE_ALT), False),  # AIOStreams torbox format
    (re.compile(r'\[(%s)\]' % _SERVICE_ALT), False),  # StremThru
)


def _parse_cache_state(raw, pre_clean, name):
    for pattern, cached in _CACHE_TEXT_PATTERNS:
        match = pattern.search(raw)
        if match:
            return match.group(1), cached
    for pattern, cached in _CACHE_EMOJI_PATTERNS:
        match = pattern.search(pre_clean)
        if match:
            return match.group(1), cached
    name_clean = clean_text(name)
    for pattern, cached in _CACHE_BARE_PATTERNS:
        match = pattern.search(name_clean)
        if match:
            return match.group(1), cached
    return '', None


# ---------------------------------------------------------------------------
# behaviorHints.cometKodiMetaV1
# ---------------------------------------------------------------------------

# Comet's structured Kodi metadata (comet/api/endpoints/stream.py:43-70)
# is authoritative for audio/channels/languages when present -- prefer it
# over guessing from free-form text. A Stremio addon is semi-trusted,
# arbitrary user-installed code, so every field is type-checked before use.
def _comet_meta(behavior_hints):
    meta = (behavior_hints or {}).get('cometKodiMetaV1')
    return meta if isinstance(meta, dict) else {}


def _parse_audio(behavior_hints, raw):
    comet_audio = _comet_meta(behavior_hints).get('audio')
    if isinstance(comet_audio, str) and comet_audio.strip():
        tags = _parse_audio_tags(comet_audio)
        if tags:
            return tags
    return _parse_audio_tags(raw)


def _parse_channels(behavior_hints, raw):
    comet_channels = _comet_meta(behavior_hints).get('channels')
    if isinstance(comet_channels, str):
        value = _match_channels(comet_channels)
        if value:
            return value
    return _match_channels(raw)


def _parse_languages(behavior_hints, pre_clean):
    comet_languages = _comet_meta(behavior_hints).get('languages')
    if isinstance(comet_languages, list):
        codes = []
        for item in comet_languages:
            if not isinstance(item, str):
                continue
            code = _LANGUAGE_NAME_TO_CODE.get(item.strip().lower())
            if code and code not in codes:
                codes.append(code)
            if len(codes) >= _MAX_LANGUAGES:
                break
        if codes:
            return codes
    return _scan_languages(pre_clean)


def parse_stream(stream, addon_name=''):
    """Extract structured metadata from a Stream protocol dict.

    Reads ``stream['name']``/``['title']``/['description']`` plus
    ``behaviorHints.videoSize``/``.filename``, all of which are free-form
    text addons stuff arbitrary marketing/quality info into (see module
    docstring for the AIOStreams shape this is built against). Also lifts
    ``behaviorHints.bingeGroup`` through verbatim (never through
    ``clean_text()`` - it is an opaque addon-chosen id, not display text)
    as ``binge_group``: `stremio-core`'s `types::resource::stream::Stream`
    (``src/types/resource/stream.rs`` lines 953-958) declares the same
    field `Option<String>`, and its ``is_binge_match()`` (lines 140-149)
    is exactly `lib.ui.binge.pick_binge_stream()`'s matching rule -
    absent here, like there, means "cannot binge-match", not "".

    ``audio``/``channels``/``languages``/``bitrate``/``group``/
    ``tracker``/``service``/``cached``/``release`` are re-derived per the
    marker matrix in the module docstring. Emoji-derived pieces (flag
    languages, cache-state emoji, tracker/group marker captures) are
    parsed from the pre-`clean_text()` text since `clean_text()` deletes
    most of the markers that carry them; everything else uses the
    cleaned `raw` below, built from that same capped text.
    """
    stream = stream or {}
    name = stream.get('name') or ''
    title = stream.get('title') or ''
    description = stream.get('description') or ''
    behavior_hints = stream.get('behaviorHints')
    # A Stremio addon is arbitrary user-installed code: behaviorHints
    # arriving as a string/list must not take the streams list down with
    # an AttributeError on the first .get() below.
    if not isinstance(behavior_hints, dict):
        behavior_hints = {}
    filename = behavior_hints.get('filename') or ''

    pre_clean = '\n'.join(p for p in (name, title, description, filename) if p)[:_MAX_TEXT_LEN]
    raw = clean_text(pre_clean)

    size_bytes, size_text = _parse_size(behavior_hints, raw)

    addon = clean_text(addon_name)
    display_title = _first_nonempty_line(title) or _first_nonempty_line(name) or addon

    tracker, group = _parse_tracker_group(pre_clean, filename, addon)
    service, cached = _parse_cache_state(raw, pre_clean, name)

    return {
        'addon': addon,
        'title': display_title,
        'resolution': _match_first(_RESOLUTION_PATTERNS, raw),
        'source': _match_first(_SOURCE_PATTERNS, raw),
        'codec': _match_first(_CODEC_PATTERNS, raw),
        'hdr': _parse_hdr(raw),
        'size_bytes': size_bytes,
        'size_text': size_text,
        'seeders': _parse_seeders(raw, pre_clean),
        'is_torrent': bool(stream.get('infoHash')),
        'filename': clean_text(filename),
        'binge_group': behavior_hints.get('bingeGroup') or None,
        'raw': raw,
        'audio': _parse_audio(behavior_hints, raw),
        'channels': _parse_channels(behavior_hints, raw),
        'languages': _parse_languages(behavior_hints, pre_clean),
        'bitrate': _parse_bitrate(raw),
        'group': group,
        'tracker': tracker,
        'service': service,
        'cached': cached,
        'release': _parse_release(raw),
    }


# ---------------------------------------------------------------------------
# format_label / format_plot
# ---------------------------------------------------------------------------

_RESOLUTION_COLORS = {
    '2160p': 'gold',
    '1080p': 'lime',
    '720p': 'cyan',
    '480p': 'white',
    '': 'white',
}


#: Seeder-count marker in `format_label`'s row. U+25B2 BLACK UP-POINTING
#: TRIANGLE is the conventional "seeds" glyph in torrent UIs and reads at
#: a glance, unlike the old bare "S50" which looked like part of the
#: release name. Deliberately from Geometric Shapes (U+25A0-U+25FF), NOT
#: the Misc Symbols/Dingbats block `clean_text()` strips as unrenderable
#: junk (see `_JUNK_RANGES`): Kodi's default font covers this one.
SEEDERS_SYMBOL = '\u25b2'


def format_details(info):
    """Build the plain-text second-line row detail for the streams view.

    ``<audio+channels> \u00b7 <languages> \u00b7 <bitrate> \u00b7
    <release> \u00b7 <group> \u00b7 <tracker>`` -- same empty-segment-
    dropping rule as `format_label()` below, and never BBcode or a
    newline since every input already came out of `parse_stream()` as
    plain text.
    """
    info = info or {}

    audio_bits = list(info.get('audio') or [])
    channels = info.get('channels') or ''
    if channels:
        audio_bits.append(channels)
    audio_part = ' '.join(audio_bits)

    languages_part = ' / '.join(info.get('languages') or [])
    bitrate_part = info.get('bitrate') or ''
    release_part = ' '.join(info.get('release') or [])
    group_part = info.get('group') or ''
    tracker_part = info.get('tracker') or ''

    return ' \u00b7 '.join(
        part for part in (audio_part, languages_part, bitrate_part, release_part, group_part, tracker_part)
        if part
    )


def format_label(info, include_addon=True):
    """Build the single-line BBcode label shown in the streams view.

    ``[COLOR <c>]<res>[/COLOR] [B]<source>[/B] <codec/hdr> \u00b7 <size> \u00b7
    \u25b2<seeders> \u00b7 [<cache colour><service>[/COLOR]][ \u00b7
    [COLOR gray]<addon>[/COLOR]]`` -- any empty part (and its separator)
    is dropped so e.g. a stream with no detected source or HDR tags
    doesn't leave dangling '· ·' gaps. Every input already passed
    through clean_text() via parse_stream(), so the result never contains
    a newline.

    The cache-state segment (when `service` is known) is
    ``[COLOR lime]<service>[/COLOR]`` when cached, ``[COLOR
    orange]<service> DL[/COLOR]`` when known not cached, or bare
    `<service>` when cache state is unknown.

    `include_addon=False` drops the trailing addon segment entirely -
    used by `lib.ui.streamswindow.StreamsWindow`'s two-line row, which
    renders the addon/provider name on its own second line instead.
    """
    info = info or {}

    resolution = info.get('resolution') or ''
    resolution_part = (
        '[COLOR %s]%s[/COLOR]' % (_RESOLUTION_COLORS.get(resolution, 'white'), resolution)
        if resolution else ''
    )
    source_part = '[B]%s[/B]' % info['source'] if info.get('source') else ''
    codec_hdr_bits = ([info['codec']] if info.get('codec') else []) + list(info.get('hdr') or [])
    codec_hdr_part = ' '.join(codec_hdr_bits)
    head = ' '.join(part for part in (resolution_part, source_part, codec_hdr_part) if part)

    tail_bits = []
    if info.get('size_text'):
        tail_bits.append(info['size_text'])
    seeders = info.get('seeders')
    if seeders is not None:
        tail_bits.append('%s%s' % (SEEDERS_SYMBOL, seeders))
    service = info.get('service') or ''
    if service:
        cached = info.get('cached')
        if cached is True:
            tail_bits.append('[COLOR lime]%s[/COLOR]' % service)
        elif cached is False:
            tail_bits.append('[COLOR orange]%s DL[/COLOR]' % service)
        else:
            tail_bits.append(service)
    if include_addon and info.get('addon'):
        tail_bits.append('[COLOR gray]%s[/COLOR]' % info['addon'])
    tail = ' \u00b7 '.join(tail_bits)

    return ' \u00b7 '.join(part for part in (head, tail) if part)


def format_plot(info):
    """Multi-line plot text for the streams view's info panel."""
    info = info or {}
    lines = []

    heading = info.get('filename') or info.get('title') or ''
    if heading:
        lines.append(heading)

    size_seed_bits = []
    if info.get('size_text'):
        size_seed_bits.append(info['size_text'])
    seeders = info.get('seeders')
    if seeders is not None:
        size_seed_bits.append('%s seeders' % seeders)
    if size_seed_bits:
        lines.append(' \u00b7 '.join(size_seed_bits))

    details = format_details(info)
    if details:
        lines.append(details)

    service = info.get('service') or ''
    if service:
        cached = info.get('cached')
        if cached is True:
            lines.append('Cached (%s)' % service)
        elif cached is False:
            lines.append('Not cached (%s)' % service)
        else:
            lines.append(service)

    if info.get('addon'):
        lines.append(info['addon'])

    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# sort_streams
# ---------------------------------------------------------------------------

_RESOLUTION_TIER = {'2160p': 4, '1080p': 3, '720p': 2, '480p': 1, '': 0}


def _desc_none_last(value):
    """Sort key fragment: None sorts after every number, numbers descend."""
    return (value is None, -(value or 0))


def sort_streams(pairs, key='quality'):
    """Return a new, stably-sorted list of ``(info, stream)`` pairs.

    - ``'quality'`` (default): resolution tier desc, then seeders desc
      (streams with unknown seeders sort last), then size desc.
    - ``'size'``: size desc (unknown size last).
    - ``'seeders'``: seeders desc (unknown seeders last).
    """
    pairs = list(pairs or [])

    if key == 'size':
        return sorted(pairs, key=lambda pair: _desc_none_last(pair[0].get('size_bytes')))
    if key == 'seeders':
        return sorted(pairs, key=lambda pair: _desc_none_last(pair[0].get('seeders')))

    def quality_key(pair):
        info = pair[0]
        tier = _RESOLUTION_TIER.get(info.get('resolution') or '', 0)
        return (
            -tier,
            _desc_none_last(info.get('seeders')),
            _desc_none_last(info.get('size_bytes')),
        )

    return sorted(pairs, key=quality_key)
