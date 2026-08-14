"""Pure, Kodi-independent metadata/formatting helpers for playback resolution.

No xbmc*/network/buffering code here - just string/number transforms over
resolved-URL, `/create` stats, and Stremio `meta`/stream shapes that
lib.ui.player (and, for tests, this module directly) can call without any
Kodi stub. Split out of lib.ui.player to keep that module's Kodi-facing
responsibility (actually starting playback) separate from this one
(deriving the metadata it displays).
"""
import os
import re

#: Extension -> MIME type for the video containers Stremio streams commonly
#: use. Keyed by `os.path.splitext()` output (lowercased, leading dot kept).
_MIME_TYPES = {
    '.mkv': 'video/x-matroska',
    '.mp4': 'video/mp4',
    '.m4v': 'video/mp4',
    '.avi': 'video/x-msvideo',
    '.mov': 'video/quicktime',
    '.ts': 'video/mp2t',
    '.m2ts': 'video/mp2t',
    '.webm': 'video/webm',
    '.flv': 'video/x-flv',
    '.wmv': 'video/x-ms-wmv',
    '.mpg': 'video/mpeg',
    '.mpeg': 'video/mpeg',
}


def mime_for(filename):
    """Best-effort MIME type for `filename`'s extension, or None.

    Unknown/absent extensions return None so the caller skips
    `setMimeType` entirely rather than hinting a wrong/generic type.
    """
    if not filename:
        return None
    ext = os.path.splitext(filename)[1].lower()
    return _MIME_TYPES.get(ext)


def filename_from_url(url):
    """Last path segment of a resolved playback `url`, with any baked
    `|urlencoded-headers` suffix (see the header-baking in player.py's
    `play()`) and query string stripped first.
    """
    base = url.split('|', 1)[0].split('?', 1)[0]
    return base.rsplit('/', 1)[-1]


def extract_file_name(stats, file_idx):
    """Best-effort filename for `file_idx` out of a `/create` stats
    dict's `files` array (`[{'name', 'path', 'length', 'offset'}, ...]` -
    see `guess_file_idx()`'s docstring in lib/stremio/server.py for the
    full response shape), or None when `stats`/`files`/the entry at
    `file_idx` is missing or an unexpected shape.

    A torrent's resolved playback URL (`http://host/<infoHash>/<fileIdx>`)
    carries no filename or extension of its own, so this is the only way
    a torrent stream ever gets a real filename for `_apply_item_metadata`
    (title/originaltitle fallback) or a correct MIME type from
    `mime_for`. Callers thread back a `/create` stats dict they already
    fetched for another reason (engine warm, or the metadata-wait loop) -
    this never issues a request of its own. Never raises: a malformed
    `/create` response must never break playback.
    """
    try:
        files = stats.get('files')
    except AttributeError:
        return None
    if not isinstance(files, list) or not (0 <= file_idx < len(files)):
        return None
    entry = files[file_idx]
    name = entry.get('name') if isinstance(entry, dict) else None
    return name if isinstance(name, str) and name else None


def format_hms(seconds):
    """'H:MM:SS' for the resume-prompt message (e.g. 5410 -> '1:30:10')."""
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return '%d:%02d:%02d' % (hours, minutes, secs)


def human_size(num_bytes):
    """Format a byte count as e.g. '12.3 MB' (B/KB/MB/GB, 1 decimal)."""
    value = float(num_bytes or 0)
    for unit in ('B', 'KB', 'MB'):
        if value < 1024.0:
            return '%.1f %s' % (value, unit)
        value /= 1024.0
    return '%.1f GB' % value


#: Regex for the first 4-digit year group in a Stremio `releaseInfo`
#: value - that field's shape is an open-ended range ('2019-',
#: '2019-2023'), not a bare year, so the year itself must be pulled out
#: rather than the whole field parsed as one.
_YEAR_RE = re.compile(r'\d{4}')

#: Regex for the first run of digits in a Stremio `runtime` string
#: (e.g. '132 min') - every Stremio addon's `runtime` field is minutes,
#: never seconds, hence `_SECONDS_PER_MINUTE` below.
_RUNTIME_MINUTES_RE = re.compile(r'\d+')
_SECONDS_PER_MINUTE = 60


def sanitize_title(text):
    """Strip CR/LF from `text` and trim surrounding whitespace.

    Addon-supplied `title`/`name`/`label` fields routinely bake in
    newlines (the same hazard `lib.ui.streamswindow.onInit` already
    sanitizes stream-picker rows against) - left in, they visibly break
    Kodi's single-line fullscreen OSD title.
    """
    if not text:
        return ''
    return text.replace('\r', ' ').replace('\n', ' ').strip()


def parse_year(value):
    """Best-effort 4-digit release year out of a Stremio `releaseInfo`
    (or plain `year`) value, tolerating the open-ended-range shapes
    Stremio metadata actually uses ('2019', '2019-', '2019-2023') by
    taking the FIRST 4-digit group rather than requiring the whole value
    to be a bare year. Returns None for anything else - never raises, so
    a malformed field only skips this one piece of metadata.
    """
    if value is None:
        return None
    match = _YEAR_RE.search(str(value))
    return int(match.group()) if match else None


def parse_rating(value):
    """Best-effort float rating out of a Stremio `imdbRating` value
    (e.g. '7.8'), or None for an unparseable value like 'n/a' - never
    raises.
    """
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_duration_seconds(value):
    """Best-effort duration in SECONDS out of a Stremio `runtime` string
    such as '132 min' - the first run of digits found is taken as
    MINUTES (the unit every Stremio addon's `runtime` field actually
    uses) and converted to the seconds
    `InfoTagVideo.setDuration()`/legacy `setInfo('video', {'duration':
    ...})` both expect. Returns None for an unparseable value like '?' -
    never raises.
    """
    if value is None:
        return None
    match = _RUNTIME_MINUTES_RE.search(str(value))
    if not match:
        return None
    return int(match.group()) * _SECONDS_PER_MINUTE


def resolve_art(art, meta):
    """Best-effort `ListItem.setArt()` payload from `item_meta['art']`
    (any subset of poster/fanart/thumb), falling back field-by-field to
    the raw Stremio `meta` dict's own poster/background/logo when `art`
    doesn't supply one. A poster with no explicit thumb also becomes the
    thumb and icon - the same poster->thumb/icon convention every other
    Rivulet ListItem builder already uses (see e.g. lib/ui/views.py).
    Missing/falsy values are skipped entirely: `ListItem.setArt()`
    treats an empty string or None as "clear this art type", never
    "leave it alone".
    """
    art = art or {}
    meta = meta or {}
    poster = art.get('poster') or meta.get('poster')
    thumb = art.get('thumb')
    fanart = art.get('fanart') or meta.get('background') or meta.get('logo')

    result = {}
    if poster:
        result['poster'] = poster
        result['icon'] = poster
        result['thumb'] = thumb or poster
    elif thumb:
        result['thumb'] = thumb
    if fanart:
        result['fanart'] = fanart
    return result
