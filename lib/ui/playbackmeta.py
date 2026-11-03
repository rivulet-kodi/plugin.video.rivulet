# SPDX-FileCopyrightText: 2026 M0Rf30
# SPDX-License-Identifier: MIT

"""Pure, Kodi-independent metadata/formatting helpers for playback resolution.

No xbmc*/network/buffering code here - just string/number transforms over
resolved-URL, `/create` stats, and Stremio `meta`/stream shapes that
lib.ui.player (and, for tests, this module directly) can call without any
Kodi stub. Split out of lib.ui.player to keep that module's Kodi-facing
responsibility (actually starting playback) separate from this one
(deriving the metadata it displays).
"""
import datetime
import os
import re
from urllib.parse import parse_qsl

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


def split_embedded_headers(url):
    """`(clean_url, headers)` splitting off a resolved stream url's own
    `|urlencoded-headers` suffix (the Kodi player-path convention some
    Stremio addons/resolvers bake straight into the `url` they return,
    same shape player.py's own `play()` bakes for the non-ISA path
    below) - `{}` when `url` carries none.

    Exists so the ISA path can merge these into
    `behaviorHints.proxyHeaders.request` instead of leaving them stuck
    in a path suffix inputstream.adaptive never reads (it only reads
    its own Properties, set from `_apply_isa_properties()`) - a manifest
    fetch missing an addon-required header (referer/cookie/auth) then
    fails outright rather than merely losing that header when Kodi's
    ordinary player would have sent it.
    """
    if not url or '|' not in url:
        return url, {}
    base, _, raw = url.partition('|')
    if not raw:
        return base, {}
    return base, dict(parse_qsl(raw, keep_blank_values=True))



#: `behaviorHints.rivuletManifestType` values the Stream4Me bridge sets
#: (resources/s4me_bridge/bridge_helpers.py's `is_adaptive_entry()`)
#: when it already determined Stream4Me itself would hand a resolved
#: url to inputstream.adaptive rather than play it as a plain file -
#: see `is_adaptive_stream()` below for how player.py acts on it.
_ADAPTIVE_HINT_TYPES = ('hls', 'mpd')


def is_adaptive_stream(behavior_hints, url):
    """Whether `url` should be routed through inputstream.adaptive, and
    which manifest type ('hls' or 'mpd') - or None for an ordinary file.

    Prefers the bridge's own `behaviorHints.rivuletManifestType` hint:
    the bridge already ran Stream4Me's own `is_adaptive_entry()` check
    against the SOURCE `video_urls` entry, before the resolved url even
    existed, so it knows things the url's own shape cannot show (a
    label/entry-length-driven decision, not just an extension). Falling
    back to the resolved url's own extension (`.m3u8` HLS, `.mpd` DASH,
    via `filename_from_url()` - which already strips any baked
    `|headers`/query suffix) covers any OTHER addon that resolves
    straight to a manifest url without carrying that hint.
    """
    hint = (behavior_hints or {}).get('rivuletManifestType')
    if hint in _ADAPTIVE_HINT_TYPES:
        return hint
    if not url:
        return None
    name = filename_from_url(url).lower()
    if name.endswith('.mpd'):
        return 'mpd'
    if name.endswith('.m3u8'):
        return 'hls'
    return None


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


def format_resume_clock(seconds):
    """'m:ss', or 'h:mm:ss' once an hour is in (724 -> '12:04').

    Continue's row says "from mm:ss". `format_hms()` always prints the
    hour, so the same offset would read '0:12:04'.
    """
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return '%d:%02d:%02d' % (hours, minutes, secs)
    return '%d:%02d' % (minutes, secs)


def episode_label_from_id(video_id):
    """'S1E3' when `video_id` ends in ':season:episode', else ''.

    Stremio episode ids are ``tt1234567:1:3`` (the show id, then the two
    numbers). Unpadded, the way the Continue card reads.
    `episode_code()` stays zero-padded for the episode list.
    """
    if not isinstance(video_id, str):
        return ''
    show, season, episode = (video_id.rsplit(':', 2) + ['', ''])[:3]
    if not show or not season.isdigit() or not episode.isdigit():
        return ''
    return 'S%dE%d' % (int(season), int(episode))


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
#: (e.g. '132 min') - a bare number is minutes, never seconds, hence
#: `_SECONDS_PER_MINUTE` below.
_RUNTIME_MINUTES_RE = re.compile(r'\d+')
#: Explicit unit forms some addons send instead ('2h 15min', '1 h', '45m').
_RUNTIME_HOURS_RE = re.compile(r'(\d+)\s*h', re.IGNORECASE)
_RUNTIME_UNIT_MINUTES_RE = re.compile(r'(\d+)\s*m', re.IGNORECASE)
_SECONDS_PER_MINUTE = 60
_SECONDS_PER_HOUR = 3600


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


#: Trailing characters marking an open-ended `releaseInfo` range - a
#: series that has started and is still running. Cinemeta terminates
#: EVERY range with an EN DASH, not the ASCII hyphen ('2011–2019',
#: '2022–' - confirmed against v3-cinemeta.strem.io), so code matching
#: only '-' misses every series it sees. The em dash costs nothing to
#: include and the field is third-party text.
_YEAR_RANGE_TRAILERS = '-–—'


def year_range(value, now_word):
    """A Stremio `releaseInfo` value rendered for display, with
    `now_word` closing an open-ended range.

    `releaseInfo` is a RANGE for a series ('2011–2019'), not just a
    year, and a series that is still running arrives open-ended, as a
    start year with a dangling separator ('2022–'). Printing that as-is
    looks like truncated text, but simply stripping the dash loses the
    very thing it says: that the show has NOT ended. So it is closed
    with a word instead - '2022–now' - which carries the same
    information in a way that reads as deliberate.

    The separator already in the data is reused rather than normalised,
    so a range keeps the same dash on both sides.

    A closed range ('2011–2019'), a bare year ('1999') and the full ISO
    date callers fall back to ('2021-07-04' - whose hyphens are
    separators, not an open range) are all returned unchanged.

    `now_word` is passed in rather than localized here so this module
    stays Kodi-independent; callers resolve it through `compat.L()`.
    """
    text = str(value or '').strip()
    stripped = text.rstrip(_YEAR_RANGE_TRAILERS).rstrip()
    if stripped == text or not stripped:
        # Not an open-ended range (or nothing but dashes) - leave it be.
        return text
    return '%s%s%s' % (stripped, text[len(stripped):].strip(), now_word)


#: How long after its `released` date a movie with no home release yet is
#: still tagged IN CINEMA. See `in_cinema()`.
IN_CINEMA_WINDOW_DAYS = 120


def _release_date(value):
    """The `datetime.date` of a Cinemeta-style ISO `released` value
    (``2026-09-12T00:00:00.000Z``), or None when it is missing, not a
    string, or does not start with a full ``YYYY-MM-DD`` (a bare year
    says nothing about whether the film is still in theatres)."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.datetime.strptime(value.strip()[:10], '%Y-%m-%d').date()
    except ValueError:
        return None


def in_cinema(meta, now=None):
    """Whether a catalog `meta` is a movie that is probably still in
    theatres, the state official Stremio apps show as an "In cinema" tag.

    INFERRED RULE, not upstream-verified. The tag is not computed in
    stremio-core or stremio-web (both were read for it) and the mobile
    apps are closed source, so this is reconstructed from what the data
    allows: Cinemeta's movie metas carry `released` (the theatrical date)
    and add `dvdRelease` only once a digital/home release exists (on its
    top catalog 13 of 49 titles had one). A movie therefore reads as in
    cinemas when

    * `type` is ``movie``,
    * `released` parses to a date that is not in the future and is at most
      `IN_CINEMA_WINDOW_DAYS` days old, and
    * there is no `dvdRelease`.

    The 120-day ceiling is a judgement call that also guards against a
    catalog that never fills `dvdRelease` in: without it such a title
    would be tagged forever. An addon that omits `released`, or sends a
    bare year, is never tagged.

    `now` is a `datetime` (naive is read as UTC) or a `date`; it defaults
    to the current UTC time. Pure: no xbmc, no network.
    """
    if not isinstance(meta, dict) or meta.get('type') != 'movie':
        return False
    if str(meta.get('dvdRelease') or '').strip():
        return False
    released = _release_date(meta.get('released'))
    if released is None:
        return False
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)
    if isinstance(now, datetime.datetime):
        if now.tzinfo is not None:
            now = now.astimezone(datetime.timezone.utc)
        today = now.date()
    else:
        today = now
    return 0 <= (today - released).days <= IN_CINEMA_WINDOW_DAYS


#: Brand names on the hero's scores line. Left untranslated, the
#: same way the hero already prints "IMDb": they name a scale, and a
#: translation would make the number harder to recognise.
_IMDB_LABEL = 'IMDb'
_AUDIENCE_LABEL = 'Rotten Tomatoes Audience'
_ROTTEN_LABEL = 'Rotten Tomatoes'
_METASCORE_LABEL = 'Metascore'

#: Direct meta keys, then the words that identify the same score inside
#: a `ratings` dict or a list of `{source, value}`. Short needles match
#: the whole source only, so "rt" does not hit "runtime".
_PANEL_RATINGS = (
    (_AUDIENCE_LABEL,
     ('rtAudience', 'audienceScore', 'popcornmeter'),
     ('popcorn', 'audience')),
    (_ROTTEN_LABEL,
     ('rottenTomatoes', 'rtRating', 'tomatoMeter', 'tomatoes'),
     ('rotten', 'tomato', 'rt')),
    (_METASCORE_LABEL,
     ('metascore', 'metacritic', 'metaScore'),
     ('metacritic', 'metascore')),
    (_IMDB_LABEL,
     ('imdbRating',),
     ('imdb', 'internet movie')),
)

_EMPTY_FACTS = frozenset(('n/a', 'na', 'none', 'null'))


def _matches_source(source, needles):
    low = str(source or '').lower().strip()
    for needle in needles:
        if len(needle) <= 3:
            if low == needle:
                return True
        elif needle in low:
            return True
    return False


def _fact(value, nested=True):
    """One displayable fact, or '' when the meta has nothing usable.

    A one-level dict (`{'value': '91%'}`) is unwrapped. Booleans are
    not numbers. 'n/a' is treated as missing so the panel never prints
    a label with nothing after it.
    """
    if isinstance(value, dict):
        if not nested:
            return ''
        inner = value.get('value') if 'value' in value else value.get('rating')
        return _fact(inner, nested=False)
    if value is None or isinstance(value, bool):
        return ''
    if isinstance(value, float):
        text = '%s' % value
    elif isinstance(value, int):
        text = '%s' % value
    elif isinstance(value, str):
        text = value.strip()
    else:
        return ''
    if text.lower() in _EMPTY_FACTS:
        return ''
    return text


def _rating_value(meta, keys, needles):
    for key in keys:
        text = _fact(meta.get(key))
        if text:
            return text
    ratings = meta.get('ratings')
    if isinstance(ratings, dict):
        pairs = ratings.items()
    elif isinstance(ratings, list):
        pairs = []
        for entry in ratings:
            if isinstance(entry, dict):
                source = entry.get('source') or entry.get('name') or ''
                value = entry.get('value') if 'value' in entry else entry.get('rating')
                pairs.append((source, value))
    else:
        return ''
    for source, value in pairs:
        if 'rottenTomatoes' in keys and _matches_source(source, ('popcorn', 'audience')):
            continue  # Audience ratings must never also become critics' RT.
        if _matches_source(source, needles):
            text = _fact(value)
            if text:
                return text
    return ''


def _panel_year(meta, now_word):
    """`releaseInfo`, else the date part of `released`, else `year`."""
    release = meta.get('releaseInfo')
    if release not in (None, ''):
        source = release
    else:
        released = meta.get('released')
        if isinstance(released, str) and released:
            source = released.split('T', 1)[0]
        else:
            source = meta.get('year') or ''
    return year_range(source, now_word or '')


def hero_panel_text(meta, runtime_text='', now_word='', cinema_word=''):
    """The hero's facts: one per line, missing ones omitted.

    Order is the one the panel is read in: Rotten Tomatoes, Metascore,
    IMDb, year, runtime, then up to three genres. A catalog that only
    has a year still gets that year, and a catalog with none of these
    gets an empty string rather than a row of blank labels.

    `runtime_text` is the already-localized runtime (`"98 min"`). When
    it is empty the raw `runtime` field is used, which is how a string
    like `"155 min"` arrives. `now_word` closes an open series range
    (`"2022–now"`). Both are passed in so this module stays free of
    xbmc.

    `cinema_word` (the localized IN CINEMA tag, passed in for the same
    reason) is appended to the year line when `in_cinema(meta)` holds, so
    the tag sits where the release year is read.
    """
    if not isinstance(meta, dict):
        return ''
    lines = []
    for label, keys, needles in _PANEL_RATINGS:
        value = _rating_value(meta, keys, needles)
        if value:
            lines.append('%s %s' % (label, value))
    year = _panel_year(meta, now_word)
    if cinema_word and in_cinema(meta):
        year = '%s \u00b7 %s' % (year, cinema_word) if year else cinema_word
    if year:
        lines.append(year)
    runtime = _fact(runtime_text) or _fact(meta.get('runtime'))
    if runtime:
        lines.append(runtime)
    genres = meta.get('genres')
    names = []
    if isinstance(genres, list):
        for genre in genres:
            name = _fact(genre)
            if name:
                names.append(name)
            if len(names) == 3:
                break
    if names:
        lines.append(', '.join(names))
    return '\n'.join(lines)


#: The hero prints the panel's scores on one line, short names
#: first.
_LOOK_SCORE_NAMES = (
    (_IMDB_LABEL, 'IMDb'),
    (_AUDIENCE_LABEL, 'RT Audience'),
    (_ROTTEN_LABEL, 'RT'),
    (_METASCORE_LABEL, 'MC'),
)


def look_hero_parts(panel_text):
    """``(stars, facts, scores)`` from the hero panel text, for the Home hero.

    `panel_text` is what `hero_panel_text()` (or the ratings overlay)
    wrote: one fact per line, scores first. `stars` is the IMDb score as
    half stars, 0-10, or None when there is no IMDb line. `facts` are
    the other lines (year, runtime, genres) in order. `scores` are
    ``(short name, value)`` pairs: IMDb, audience, critics' RT, MC. Lines
    are matched on the label the panel writes; anything else is a fact,
    so an unknown line is shown rather than dropped.
    """
    stars = None
    found = {}
    facts = []
    for raw in str(panel_text or '').split('\n'):
        line = raw.strip()
        if not line:
            continue
        for label, short in _LOOK_SCORE_NAMES:
            if line.startswith(label + ' '):
                found[short] = line[len(label) + 1:].strip()
                break
        else:
            facts.append(line)
    scores = [(short, found[short]) for _label, short in _LOOK_SCORE_NAMES if found.get(short)]
    imdb = parse_rating(found.get('IMDb', '').split('/')[0]) if found.get('IMDb') else None
    if imdb is not None and 0 <= imdb <= 10:
        stars = int(imdb + 0.5)
    return stars, facts, scores


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
    """Best-effort duration in SECONDS out of a Stremio `runtime` string,
    in the seconds `InfoTagVideo.setDuration()`/legacy `setInfo('video',
    {'duration': ...})` both expect. Handles '132 min' and a bare '132'
    (minutes - the unit the `runtime` field normally uses) as well as the
    explicit-unit '2h 15min' / '1h' shapes some addons send; reading the
    first digit run of the latter as minutes made a two-hour film 2
    minutes long. Returns None for an unparseable value like '?' - never
    raises.
    """
    if value is None:
        return None
    text = str(value)
    hours = _RUNTIME_HOURS_RE.search(text)
    minutes = _RUNTIME_UNIT_MINUTES_RE.search(text)
    if hours or minutes:
        return (
            (int(hours.group(1)) * _SECONDS_PER_HOUR if hours else 0)
            + (int(minutes.group(1)) * _SECONDS_PER_MINUTE if minutes else 0)
        )
    match = _RUNTIME_MINUTES_RE.search(text)
    if not match:
        return None
    return int(match.group()) * _SECONDS_PER_MINUTE


def coerce_int(value):
    """`int(value)`, or 0 for anything that is not an integer (missing,
    None, 'x'). Stremio addons send `season`/`episode` as strings (or omit
    them), which `%02d` formatting and int/str ordering both choke on."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def episode_code(season, episode):
    """'S01E03' - zero-padded season/episode (Specials as S00Exx). Both
    inputs go through `coerce_int()`."""
    return 'S%02dE%02d' % (coerce_int(season), coerce_int(episode))


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
