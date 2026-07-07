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
"""
import re

# ---------------------------------------------------------------------------
# clean_text
# ---------------------------------------------------------------------------

# Astral-plane emoji (\U0001F300-\U0001FAFF etc.), regional-indicator flag
# pairs and the like are all above the BMP -- stripped wholesale below by
# the `cp > 0xFFFF` check. What's left is BMP junk Kodi's font still can't
# render: Misc Symbols + Dingbats (weather/emoji glyphs like the U+26A1
# "high voltage" bolt in AIOStreams' "3.3 Mbps" line), stray zero-width
# joiners/spaces some addons use to defeat text-truncation, and the
# variation-selector-16 that forces emoji presentation on an otherwise
# printable codepoint (e.g. U+2764 U+FE0F).
_JUNK_RANGES = (
    (0x2600, 0x27BF),  # Misc Symbols + Dingbats
    (0x200B, 0x200D),  # zero-width space / ZWNJ / ZWJ
)
_JUNK_SINGLES = frozenset([0xFE0F])  # variation selector-16 (emoji style)

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


def _is_junk_codepoint(cp):
    if cp > 0xFFFF:
        return True
    if cp in _JUNK_SINGLES:
        return True
    for lo, hi in _JUNK_RANGES:
        if lo <= cp <= hi:
            return True
    return False


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
    filtered = ''.join(ch for ch in truncated if not _is_junk_codepoint(ord(ch)))
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

# Labeled seeder counts, tried first (most reliable): 'Seeds: 50',
# 'Seeders 50', 'Seed-50', 'S:12'.
_SEEDS_LABELED_PATTERNS = (
    re.compile(r'\bseed(?:ers|s)?\s*[:\-]?\s*(\d+)', re.I),
    re.compile(r'\b(?:se|sd)\s*[:\-]\s*(\d+)\b', re.I),
    re.compile(r'\bs\s*:\s*(\d+)\b', re.I),
)
# Fallback: many addons (AIOStreams included) put the peer/seed count right
# after the size/speed group, originally introduced by a person emoji that
# clean_text() has already stripped down to bare whitespace, e.g.
# "... 4.39 GB \u00b7 3.3 Mbps \u00b7 50 Il Corsaro Viola". Take the LAST
# such "<unit> ... <int>" match in the text, since seeders is conventionally
# the trailing group after size/speed.
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


def _parse_hdr(text):
    found = []
    consumed_spans = []
    for tag, pattern in _HDR_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        span = match.span()
        if any(lo <= span[0] and span[1] <= hi for lo, hi in consumed_spans):
            continue
        found.append((span[0], tag))
        consumed_spans.append(span)
    found.sort(key=lambda item: item[0])
    return [tag for _, tag in found]


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


def _parse_seeders(text):
    for pattern in _SEEDS_LABELED_PATTERNS:
        match = pattern.search(text)
        if match:
            seeders = int(match.group(1))
            return seeders if seeders <= _MAX_PLAUSIBLE_SEEDERS else None
    matches = list(_SEEDS_AFTER_UNIT_RE.finditer(text))
    if matches:
        seeders = int(matches[-1].group(1))
        return seeders if seeders <= _MAX_PLAUSIBLE_SEEDERS else None
    return None


def parse_stream(stream, addon_name=''):
    """Extract structured metadata from a Stream protocol dict.

    Reads ``stream['name']``/``['title']``/['description']`` plus
    ``behaviorHints.videoSize``/``.filename``, all of which are free-form
    text addons stuff arbitrary marketing/quality info into (see module
    docstring for the AIOStreams shape this is built against).
    """
    stream = stream or {}
    name = stream.get('name') or ''
    title = stream.get('title') or ''
    description = stream.get('description') or ''
    behavior_hints = stream.get('behaviorHints') or {}
    filename = behavior_hints.get('filename') or ''

    raw = clean_text(' '.join(p for p in (name, title, description, filename) if p))

    size_bytes, size_text = _parse_size(behavior_hints, raw)

    display_title = _first_nonempty_line(title) or _first_nonempty_line(name) or clean_text(addon_name)

    return {
        'addon': clean_text(addon_name),
        'title': display_title,
        'resolution': _match_first(_RESOLUTION_PATTERNS, raw),
        'source': _match_first(_SOURCE_PATTERNS, raw),
        'codec': _match_first(_CODEC_PATTERNS, raw),
        'hdr': _parse_hdr(raw),
        'size_bytes': size_bytes,
        'size_text': size_text,
        'seeders': _parse_seeders(raw),
        'is_torrent': bool(stream.get('infoHash')),
        'filename': clean_text(filename),
        'raw': raw,
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


def format_label(info, include_addon=True):
    """Build the single-line BBcode label shown in the streams view.

    ``[COLOR <c>]<res>[/COLOR] [B]<source>[/B] <codec/hdr> \u00b7 <size> \u00b7
    S<seeders>[ \u00b7 [COLOR gray]<addon>[/COLOR]]`` -- any empty part (and
    its separator) is dropped so e.g. a stream with no detected source or
    HDR tags doesn't leave dangling '· ·' gaps. Every input already passed
    through clean_text() via parse_stream(), so the result never contains
    a newline.

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
        tail_bits.append('S%s' % seeders)
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
