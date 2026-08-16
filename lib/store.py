"""Local JSON persistence for installed Stremio addons and account auth.

Pure Python -- no ``xbmc*`` imports, unit-testable in isolation. Two flat
JSON files live under ``data_dir``:

* ``addons.json`` -- list of addon descriptors, each shaped like the
  Stremio addon-collection API: ``{"transportUrl": ..., "manifest": ...,
  "flags": {...}}``.
* ``auth.json`` -- ``{"authKey": ..., "user": {...}}`` when logged in to a
  Stremio account, or absent/``None`` when logged out.
* ``search_history.json`` -- list of past search query strings, most
  recent first, capped at :data:`MAX_SEARCH_HISTORY` entries.
* ``now_playing.json`` -- the single active "now playing" context dict
  (see the cross-slice Contract: ``{'type', 'id', 'video_id', 'name',
  'poster', 'started_at'}``) while Rivulet-originated playback is
  ongoing, or absent when nothing is playing.
* ``resume_offset.json`` -- a one-shot pending resume-seek offset
  (milliseconds) queued by ``lib.ui.player`` for the NEXT playback
  session's ``onAVStarted``, consumed and cleared exactly once by
  ``lib.service_runner``. Deliberately a SEPARATE file from
  ``now_playing.json`` rather than an extra key on that dict, so the
  Contract-pinned shape above stays exactly those five keys.
* ``progress.json`` -- a local cache of playback position, keyed by
  ``(type, id, video_id)`` (see :func:`Store.get_progress`/
  :func:`Store.set_progress`), so resume and progress display work
  fully offline and while logged out -- this file is never read from or
  written to the Stremio account API (that is
  ``lib.stremio.api.StremioAPI.datastore_get``/``datastore_put``'s job,
  driven by ``lib.service_runner`` only when logged in AND the
  "sync_progress" setting is on).

Writes are atomic (write to a temp file, then ``os.replace``) so a crash or
power loss never leaves a half-written JSON file behind. A corrupt file on
read is tolerated and treated the same as a missing one.

Kodi can run multiple ``default.py`` OS processes concurrently (e.g. the
user keeps navigating while a slow addon-install action is still
mid-flight), so a naive read-modify-write on addons.json can silently
lose whichever process's write happens first. :meth:`Store.update_addons`
(used by :meth:`Store.install_addon`/:meth:`Store.remove_addon`) guards
against this with a portable optimistic-concurrency/compare-and-swap
retry -- no ``fcntl``/``msvcrt`` OS-specific locking, since this addon
also runs on Windows and Android. ``auth.json`` has no such
read-modify-write pattern (every write either replaces it wholesale or
clears it), so it does not need this.

``now_playing.json``/``resume_offset.json`` follow the same wholesale-
replace-or-clear pattern as ``auth.json`` above (no read-modify-write, so
no compare-and-swap needed). ``progress.json`` is a plain read-modify-
write like ``search_history.json`` -- a lost update under two concurrent
writers (e.g. ``default.py`` and the background service both sampling
around the same moment) at worst drops one sample, which the next
periodic sample corrects within seconds; writes are still atomic, so
this never corrupts the file itself.
"""

import copy
import datetime
import heapq
import json
import os
import tempfile
import time

ADDONS_FILENAME = "addons.json"
AUTH_FILENAME = "auth.json"
SEARCH_HISTORY_FILENAME = "search_history.json"
NOW_PLAYING_FILENAME = "now_playing.json"
RESUME_OFFSET_FILENAME = "resume_offset.json"
PROGRESS_FILENAME = "progress.json"
LAST_VERSION_FILENAME = "last_version.json"

#: Most-recent-first cap for the persisted search history list.
MAX_SEARCH_HISTORY = 15

#: Hard cap on the number of entries kept in ``progress.json``. Once a
#: :meth:`Store.set_progress` write would exceed this, the oldest
#: entries (by ``updated_at``) are evicted first -- see
#: :func:`_prune_progress`.
MAX_PROGRESS_ENTRIES = 500

#: Entries in ``progress.json`` older than this (by ``updated_at``) are
#: dropped the next time a :meth:`Store.set_progress` write runs a full
#: age sweep -- immediately once the entry count exceeds
#: :data:`MAX_PROGRESS_ENTRIES`, otherwise at most
#: :data:`PROGRESS_SWEEP_INTERVAL_SECONDS` late -- see
#: :func:`_prune_progress`.
MAX_PROGRESS_AGE_DAYS = 180

#: Wall-clock interval between full age-based sweeps of
#: ``progress.json`` in :meth:`Store.set_progress` (see
#: ``Store._last_progress_sweep_monotonic``). Measured: parsing every
#: entry's ``updated_at`` with ``datetime.strptime`` on every
#: ~15-second ``set_progress`` call during playback cost 1.535ms
#: @500 entries -- 62% of the whole call, ~3.1us/entry, dominated by
#: ``_strptime`` overhead -- to enforce a 180-day cutoff that is only
#: ever crossed rarely. Sweeping once a day instead of on every call
#: still catches stale entries promptly relative to that 180-day
#: cutoff, for a fraction of the cost.
PROGRESS_SWEEP_INTERVAL_SECONDS = 86400

# Official addon descriptors seeded on first run. Manifests are copied
# verbatim from the live addons (https://v3-cinemeta.strem.io/manifest.json
# and https://opensubtitles-v3.strem.io/manifest.json), matching the shape
# stremio-core's OFFICIAL_ADDONS (src/constants.rs) loads at startup.
DEFAULT_ADDONS = [{'transportUrl': 'https://v3-cinemeta.strem.io/manifest.json',
  'manifest': {'id': 'com.linvo.cinemeta',
               'version': '3.0.14',
               'description': 'The official addon for movie and series catalogs',
               'name': 'Cinemeta',
               'resources': ['catalog', 'meta', 'addon_catalog'],
               'types': ['movie', 'series'],
               'idPrefixes': ['tt'],
               'addonCatalogs': [{'type': 'all', 'id': 'official', 'name': 'Official'},
                                 {'type': 'movie',
                                  'id': 'official',
                                  'name': 'Official'},
                                 {'type': 'series',
                                  'id': 'official',
                                  'name': 'Official'},
                                 {'type': 'channel',
                                  'id': 'official',
                                  'name': 'Official'},
                                 {'type': 'all',
                                  'id': 'community',
                                  'name': 'Community'},
                                 {'type': 'movie',
                                  'id': 'community',
                                  'name': 'Community'},
                                 {'type': 'series',
                                  'id': 'community',
                                  'name': 'Community'},
                                 {'type': 'channel',
                                  'id': 'community',
                                  'name': 'Community'},
                                 {'type': 'tv', 'id': 'community', 'name': 'Community'},
                                 {'type': 'Podcasts',
                                  'id': 'community',
                                  'name': 'Community'},
                                 {'type': 'other',
                                  'id': 'community',
                                  'name': 'Community'}],
               'catalogs': [{'type': 'movie',
                             'id': 'top',
                             'genres': ['Action',
                                        'Adventure',
                                        'Animation',
                                        'Biography',
                                        'Comedy',
                                        'Crime',
                                        'Documentary',
                                        'Drama',
                                        'Family',
                                        'Fantasy',
                                        'History',
                                        'Horror',
                                        'Mystery',
                                        'Romance',
                                        'Sci-Fi',
                                        'Sport',
                                        'Thriller',
                                        'War',
                                        'Western'],
                             'extra': [{'name': 'genre',
                                        'options': ['Action',
                                                    'Adventure',
                                                    'Animation',
                                                    'Biography',
                                                    'Comedy',
                                                    'Crime',
                                                    'Documentary',
                                                    'Drama',
                                                    'Family',
                                                    'Fantasy',
                                                    'History',
                                                    'Horror',
                                                    'Mystery',
                                                    'Romance',
                                                    'Sci-Fi',
                                                    'Sport',
                                                    'Thriller',
                                                    'War',
                                                    'Western']},
                                       {'name': 'search'},
                                       {'name': 'skip'}],
                             'extraSupported': ['search', 'genre', 'skip'],
                             'name': 'Popular'},
                            {'type': 'series',
                             'id': 'top',
                             'genres': ['Action',
                                        'Adventure',
                                        'Animation',
                                        'Biography',
                                        'Comedy',
                                        'Crime',
                                        'Documentary',
                                        'Drama',
                                        'Family',
                                        'Fantasy',
                                        'History',
                                        'Horror',
                                        'Mystery',
                                        'Romance',
                                        'Sci-Fi',
                                        'Sport',
                                        'Thriller',
                                        'War',
                                        'Western',
                                        'Reality-TV',
                                        'Talk-Show',
                                        'Game-Show'],
                             'extra': [{'name': 'genre',
                                        'options': ['Action',
                                                    'Adventure',
                                                    'Animation',
                                                    'Biography',
                                                    'Comedy',
                                                    'Crime',
                                                    'Documentary',
                                                    'Drama',
                                                    'Family',
                                                    'Fantasy',
                                                    'History',
                                                    'Horror',
                                                    'Mystery',
                                                    'Romance',
                                                    'Sci-Fi',
                                                    'Sport',
                                                    'Thriller',
                                                    'War',
                                                    'Western',
                                                    'Reality-TV',
                                                    'Talk-Show',
                                                    'Game-Show']},
                                       {'name': 'search'},
                                       {'name': 'skip'}],
                             'extraSupported': ['search', 'genre', 'skip'],
                             'name': 'Popular'},
                            {'type': 'movie',
                             'id': 'year',
                             'genres': ['2026',
                                        '2025',
                                        '2024',
                                        '2023',
                                        '2022',
                                        '2021',
                                        '2020',
                                        '2019',
                                        '2018',
                                        '2017',
                                        '2016',
                                        '2015',
                                        '2014',
                                        '2013',
                                        '2012',
                                        '2011',
                                        '2010',
                                        '2009',
                                        '2008',
                                        '2007',
                                        '2006',
                                        '2005',
                                        '2004',
                                        '2003',
                                        '2002',
                                        '2001',
                                        '2000',
                                        '1999',
                                        '1998',
                                        '1997',
                                        '1996',
                                        '1995',
                                        '1994',
                                        '1993',
                                        '1992',
                                        '1991',
                                        '1990',
                                        '1989',
                                        '1988',
                                        '1987',
                                        '1986',
                                        '1985',
                                        '1984',
                                        '1983',
                                        '1982',
                                        '1981',
                                        '1980',
                                        '1979',
                                        '1978',
                                        '1977',
                                        '1976',
                                        '1975',
                                        '1974',
                                        '1973',
                                        '1972',
                                        '1971',
                                        '1970',
                                        '1969',
                                        '1968',
                                        '1967',
                                        '1966',
                                        '1965',
                                        '1964',
                                        '1963',
                                        '1962',
                                        '1961',
                                        '1960',
                                        '1959',
                                        '1958',
                                        '1957',
                                        '1956',
                                        '1955',
                                        '1954',
                                        '1953',
                                        '1952',
                                        '1951',
                                        '1950',
                                        '1949',
                                        '1948',
                                        '1947',
                                        '1946',
                                        '1945',
                                        '1944',
                                        '1943',
                                        '1942',
                                        '1941',
                                        '1940',
                                        '1939',
                                        '1938',
                                        '1937',
                                        '1936',
                                        '1935',
                                        '1934',
                                        '1933',
                                        '1932',
                                        '1931',
                                        '1930',
                                        '1929',
                                        '1928',
                                        '1927',
                                        '1926',
                                        '1925',
                                        '1924',
                                        '1923',
                                        '1922',
                                        '1921',
                                        '1920'],
                             'extra': [{'name': 'genre',
                                        'options': ['2026',
                                                    '2025',
                                                    '2024',
                                                    '2023',
                                                    '2022',
                                                    '2021',
                                                    '2020',
                                                    '2019',
                                                    '2018',
                                                    '2017',
                                                    '2016',
                                                    '2015',
                                                    '2014',
                                                    '2013',
                                                    '2012',
                                                    '2011',
                                                    '2010',
                                                    '2009',
                                                    '2008',
                                                    '2007',
                                                    '2006',
                                                    '2005',
                                                    '2004',
                                                    '2003',
                                                    '2002',
                                                    '2001',
                                                    '2000',
                                                    '1999',
                                                    '1998',
                                                    '1997',
                                                    '1996',
                                                    '1995',
                                                    '1994',
                                                    '1993',
                                                    '1992',
                                                    '1991',
                                                    '1990',
                                                    '1989',
                                                    '1988',
                                                    '1987',
                                                    '1986',
                                                    '1985',
                                                    '1984',
                                                    '1983',
                                                    '1982',
                                                    '1981',
                                                    '1980',
                                                    '1979',
                                                    '1978',
                                                    '1977',
                                                    '1976',
                                                    '1975',
                                                    '1974',
                                                    '1973',
                                                    '1972',
                                                    '1971',
                                                    '1970',
                                                    '1969',
                                                    '1968',
                                                    '1967',
                                                    '1966',
                                                    '1965',
                                                    '1964',
                                                    '1963',
                                                    '1962',
                                                    '1961',
                                                    '1960',
                                                    '1959',
                                                    '1958',
                                                    '1957',
                                                    '1956',
                                                    '1955',
                                                    '1954',
                                                    '1953',
                                                    '1952',
                                                    '1951',
                                                    '1950',
                                                    '1949',
                                                    '1948',
                                                    '1947',
                                                    '1946',
                                                    '1945',
                                                    '1944',
                                                    '1943',
                                                    '1942',
                                                    '1941',
                                                    '1940',
                                                    '1939',
                                                    '1938',
                                                    '1937',
                                                    '1936',
                                                    '1935',
                                                    '1934',
                                                    '1933',
                                                    '1932',
                                                    '1931',
                                                    '1930',
                                                    '1929',
                                                    '1928',
                                                    '1927',
                                                    '1926',
                                                    '1925',
                                                    '1924',
                                                    '1923',
                                                    '1922',
                                                    '1921',
                                                    '1920'],
                                        'isRequired': True},
                                       {'name': 'skip'}],
                             'extraSupported': ['genre', 'skip'],
                             'extraRequired': ['genre'],
                             'name': 'New'},
                            {'type': 'series',
                             'id': 'year',
                             'genres': ['2026',
                                        '2025',
                                        '2024',
                                        '2023',
                                        '2022',
                                        '2021',
                                        '2020',
                                        '2019',
                                        '2018',
                                        '2017',
                                        '2016',
                                        '2015',
                                        '2014',
                                        '2013',
                                        '2012',
                                        '2011',
                                        '2010',
                                        '2009',
                                        '2008',
                                        '2007',
                                        '2006',
                                        '2005',
                                        '2004',
                                        '2003',
                                        '2002',
                                        '2001',
                                        '2000',
                                        '1999',
                                        '1998',
                                        '1997',
                                        '1996',
                                        '1995',
                                        '1994',
                                        '1993',
                                        '1992',
                                        '1991',
                                        '1990',
                                        '1989',
                                        '1988',
                                        '1987',
                                        '1986',
                                        '1985',
                                        '1984',
                                        '1983',
                                        '1982',
                                        '1981',
                                        '1980',
                                        '1979',
                                        '1978',
                                        '1977',
                                        '1976',
                                        '1975',
                                        '1974',
                                        '1973',
                                        '1972',
                                        '1971',
                                        '1970',
                                        '1969',
                                        '1968',
                                        '1967',
                                        '1966',
                                        '1965',
                                        '1964',
                                        '1963',
                                        '1962',
                                        '1961',
                                        '1960'],
                             'extra': [{'name': 'genre',
                                        'options': ['2026',
                                                    '2025',
                                                    '2024',
                                                    '2023',
                                                    '2022',
                                                    '2021',
                                                    '2020',
                                                    '2019',
                                                    '2018',
                                                    '2017',
                                                    '2016',
                                                    '2015',
                                                    '2014',
                                                    '2013',
                                                    '2012',
                                                    '2011',
                                                    '2010',
                                                    '2009',
                                                    '2008',
                                                    '2007',
                                                    '2006',
                                                    '2005',
                                                    '2004',
                                                    '2003',
                                                    '2002',
                                                    '2001',
                                                    '2000',
                                                    '1999',
                                                    '1998',
                                                    '1997',
                                                    '1996',
                                                    '1995',
                                                    '1994',
                                                    '1993',
                                                    '1992',
                                                    '1991',
                                                    '1990',
                                                    '1989',
                                                    '1988',
                                                    '1987',
                                                    '1986',
                                                    '1985',
                                                    '1984',
                                                    '1983',
                                                    '1982',
                                                    '1981',
                                                    '1980',
                                                    '1979',
                                                    '1978',
                                                    '1977',
                                                    '1976',
                                                    '1975',
                                                    '1974',
                                                    '1973',
                                                    '1972',
                                                    '1971',
                                                    '1970',
                                                    '1969',
                                                    '1968',
                                                    '1967',
                                                    '1966',
                                                    '1965',
                                                    '1964',
                                                    '1963',
                                                    '1962',
                                                    '1961',
                                                    '1960'],
                                        'isRequired': True},
                                       {'name': 'skip'}],
                             'extraSupported': ['genre', 'skip'],
                             'extraRequired': ['genre'],
                             'name': 'New'},
                            {'type': 'movie',
                             'id': 'imdbRating',
                             'genres': ['Action',
                                        'Adventure',
                                        'Animation',
                                        'Biography',
                                        'Comedy',
                                        'Crime',
                                        'Documentary',
                                        'Drama',
                                        'Family',
                                        'Fantasy',
                                        'History',
                                        'Horror',
                                        'Mystery',
                                        'Romance',
                                        'Sci-Fi',
                                        'Sport',
                                        'Thriller',
                                        'War',
                                        'Western'],
                             'extra': [{'name': 'genre',
                                        'options': ['Action',
                                                    'Adventure',
                                                    'Animation',
                                                    'Biography',
                                                    'Comedy',
                                                    'Crime',
                                                    'Documentary',
                                                    'Drama',
                                                    'Family',
                                                    'Fantasy',
                                                    'History',
                                                    'Horror',
                                                    'Mystery',
                                                    'Romance',
                                                    'Sci-Fi',
                                                    'Sport',
                                                    'Thriller',
                                                    'War',
                                                    'Western']},
                                       {'name': 'skip'}],
                             'extraSupported': ['genre', 'skip'],
                             'name': 'Featured'},
                            {'type': 'series',
                             'id': 'imdbRating',
                             'genres': ['Action',
                                        'Adventure',
                                        'Animation',
                                        'Biography',
                                        'Comedy',
                                        'Crime',
                                        'Documentary',
                                        'Drama',
                                        'Family',
                                        'Fantasy',
                                        'History',
                                        'Horror',
                                        'Mystery',
                                        'Romance',
                                        'Sci-Fi',
                                        'Sport',
                                        'Thriller',
                                        'War',
                                        'Western',
                                        'Reality-TV',
                                        'Talk-Show',
                                        'Game-Show'],
                             'extra': [{'name': 'genre',
                                        'options': ['Action',
                                                    'Adventure',
                                                    'Animation',
                                                    'Biography',
                                                    'Comedy',
                                                    'Crime',
                                                    'Documentary',
                                                    'Drama',
                                                    'Family',
                                                    'Fantasy',
                                                    'History',
                                                    'Horror',
                                                    'Mystery',
                                                    'Romance',
                                                    'Sci-Fi',
                                                    'Sport',
                                                    'Thriller',
                                                    'War',
                                                    'Western',
                                                    'Reality-TV',
                                                    'Talk-Show',
                                                    'Game-Show']},
                                       {'name': 'skip'}],
                             'extraSupported': ['genre', 'skip'],
                             'name': 'Featured'},
                            {'type': 'series',
                             'id': 'last-videos',
                             'extra': [{'name': 'lastVideosIds',
                                        'isRequired': True,
                                        'optionsLimit': 100}],
                             'extraSupported': ['lastVideosIds'],
                             'extraRequired': ['lastVideosIds'],
                             'name': 'Last videos'},
                            {'type': 'series',
                             'id': 'calendar-videos',
                             'extra': [{'name': 'calendarVideosIds',
                                        'isRequired': True,
                                        'optionsLimit': 100}],
                             'extraSupported': ['calendarVideosIds'],
                             'extraRequired': ['calendarVideosIds'],
                             'name': 'Calendar videos'}],
               'behaviorHints': {'newEpisodeNotifications': True}},
  'flags': {'official': True, 'protected': True}},
 {'transportUrl': 'https://opensubtitles-v3.strem.io/manifest.json',
  'manifest': {'id': 'org.stremio.opensubtitlesv3',
               'version': '1.0.0',
               'name': 'OpenSubtitles v3',
               'description': 'OpenSubtitles v3 Addon for Stremio',
               'catalogs': [],
               'resources': ['subtitles'],
               'types': ['movie', 'series'],
               'idPrefixes': ['tt'],
               'logo': 'http://www.strem.io/images/addons/opensubtitles-logo.png'},
  'flags': {'official': True, 'protected': True}}]


def _atomic_write(path, data, compact=False):
    """Write ``data`` as JSON to ``path`` via a tmp-file + rename.

    ``compact`` writes without indentation (``separators=(',', ':')``)
    for frequently-rewritten, machine-only files (progress/now_playing/
    resume_offset) -- smaller writes and reads, less flash wear on every
    15-second playback-progress sample. ``addons.json``/``auth.json``
    stay pretty-printed (the default): they are user-inspectable and
    rarely written, so paying the indent cost once is the safer trade.
    """
    directory = os.path.dirname(path) or "."
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp-", dir=directory)
    try:
        with os.fdopen(fd, "w") as fh:
            # `fh.write(json.dumps(...))` rather than `json.dump(data,
            # fh, ...)` -- `json.dump` always drives the pure-Python
            # `iterencode` (no C-accelerated one-shot path), while
            # `json.dumps` takes CPython's C `_one_shot` encoder.
            # Measured: 4.35x faster (0.669ms -> 0.154ms @500 entries),
            # byte-identical output.
            if compact:
                fh.write(json.dumps(data, separators=(',', ':')))
            else:
                fh.write(json.dumps(data, indent=2, sort_keys=False))
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def _read_raw(path):
    """Return the exact on-disk text of ``path``, or ``None`` if it is
    missing or unreadable.

    Reading the raw text once and parsing that exact string (rather than
    reading the file twice) guarantees a parsed value and its before/after
    fingerprint always describe identical bytes -- which
    ``Store.update_addons`` relies on to tell a concurrent writer's change
    apart from its own read.
    """
    try:
        with open(path) as fh:
            return fh.read()
    except (OSError, UnicodeDecodeError):
        return None


def _parse_json(raw, default):
    """Parse ``raw`` JSON text, tolerating ``None`` or corrupt content."""
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except ValueError:
        return default


def _read_json(path, default):
    """Read JSON from ``path``, tolerating a missing or corrupt file."""
    return _parse_json(_read_raw(path), default)


def _parse_progress_timestamp(value):
    """Parse a ``progress.json`` ``updated_at`` value -- the seconds-
    precision ISO 8601 UTC string ``library.iso8601_utc()`` produces --
    tolerating anything else (missing, wrong type, malformed string) by
    returning ``None``. Callers then treat that entry as unparseable,
    the same as an expired one."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return None


def _prune_progress(progress, now, keep_key, sweep_age):
    """Bound ``progress.json`` after a write: drop malformed entries
    (not a dict, or an unparseable ``updated_at``) and entries older
    than :data:`MAX_PROGRESS_AGE_DAYS`, then -- if still over
    :data:`MAX_PROGRESS_ENTRIES` -- evict the oldest remaining entries
    by ``updated_at`` until the cap holds.

    ``keep_key`` (the entry :meth:`Store.set_progress` just wrote) is
    always retained, regardless of its own age or how it sorts --
    otherwise two sessions racing the same offline/stale clock could
    prune the very sample that was just persisted.

    ``now`` is the ISO 8601 UTC string of the entry just written, reused
    as the "current time" reference so this stays a pure function of
    its arguments; if it fails to parse, age-based pruning is skipped
    for this call (malformed-entry and count pruning still apply).

    ``sweep_age`` -- decided by the caller, see
    ``Store._last_progress_sweep_monotonic`` -- gates the per-entry
    ``updated_at`` parse below. When ``False`` this skips straight to
    the cheap ``isinstance`` malformed-entry filter with no
    :func:`_parse_progress_timestamp` calls at all: measured, the full
    sweep's ``datetime.strptime`` calls cost 1.535ms @500 entries (62%
    of a whole ``set_progress`` round-trip), to enforce a 180-day cutoff
    that is only ever crossed rarely. The caller always passes ``True``
    when the entry count exceeds :data:`MAX_PROGRESS_ENTRIES`, so the
    cap-eviction path below (which needs entries' parsed timestamps to
    rank them) still runs whenever it is actually needed.
    """
    if not sweep_age:
        return {k: v for k, v in progress.items() if k == keep_key or isinstance(v, dict)}
    now_parsed = _parse_progress_timestamp(now)
    max_age_seconds = MAX_PROGRESS_AGE_DAYS * 86400
    kept = {}
    for key, entry in progress.items():
        if key == keep_key:
            kept[key] = entry
            continue
        if not isinstance(entry, dict):
            continue
        updated = _parse_progress_timestamp(entry.get("updated_at"))
        if updated is None:
            continue
        if now_parsed is not None and (now_parsed - updated).total_seconds() > max_age_seconds:
            continue
        kept[key] = entry
    if len(kept) <= MAX_PROGRESS_ENTRIES:
        return kept

    def _sort_key(item):
        key, entry = item
        if key == keep_key:
            return datetime.datetime.max
        return _parse_progress_timestamp(entry.get("updated_at")) or datetime.datetime.min

    # Bounded top-N selection (keep the newest MAX_PROGRESS_ENTRIES) --
    # `heapq.nlargest` is O(n log k) versus a full `sorted()`'s
    # O(n log n), and is documented as equivalent to
    # `sorted(iterable, key=key, reverse=True)[:n]`, so order/contents
    # are unchanged. The common case (map already under the cap) never
    # reaches here at all -- see the early return above.
    ordered = heapq.nlargest(MAX_PROGRESS_ENTRIES, kept.items(), key=_sort_key)
    return dict(ordered)


class ConcurrentUpdateError(RuntimeError):
    """Raised when a JSON store file keeps changing underneath a retried
    read-modify-write update.

    Kodi can invoke ``default.py`` as separate concurrent OS processes
    (e.g. the user navigates again while an addon-install ``RunPlugin``
    action is still mid-flight); if two such processes race to update the
    same file, :meth:`Store.update_addons` raises this after exhausting
    its retries instead of silently discarding whichever write happened
    first.
    """


class Store:
    """Filesystem-backed store for addons and auth state."""

    def __init__(self, data_dir):
        self.data_dir = data_dir
        if not os.path.isdir(self.data_dir):
            os.makedirs(self.data_dir)
        self._addons_path = os.path.join(self.data_dir, ADDONS_FILENAME)
        self._auth_path = os.path.join(self.data_dir, AUTH_FILENAME)
        self._search_history_path = os.path.join(self.data_dir, SEARCH_HISTORY_FILENAME)
        self._now_playing_path = os.path.join(self.data_dir, NOW_PLAYING_FILENAME)
        self._resume_offset_path = os.path.join(self.data_dir, RESUME_OFFSET_FILENAME)
        self._progress_path = os.path.join(self.data_dir, PROGRESS_FILENAME)
        self._last_version_path = os.path.join(self.data_dir, LAST_VERSION_FILENAME)
        #: Per-instance memoisation for `_cached_read`: `path -> ((mtime_ns,
        #: size), parsed_value)`. A single process/screen-render often
        #: calls e.g. `get_addons()` many times; a fresh eMMC/SD `open()`
        #: + `json.loads()` per call is far pricier than one `os.stat()`.
        #: NEVER consulted by `update_addons`'s compare-and-swap, which
        #: must always observe the current on-disk bytes -- see
        #: `_cached_read`'s docstring.
        self._read_cache = {}
        #: `time.monotonic()` of this instance's last full age-based
        #: `progress.json` sweep in `set_progress` (see
        #: `PROGRESS_SWEEP_INTERVAL_SECONDS`). `None` means "never swept
        #: in this process", which forces one on the very first
        #: `set_progress` call. Deliberately in-memory only, not
        #: persisted: `set_progress` runs in the long-lived service
        #: process, so this amortises the sweep cost across its ~15s
        #: write cadence fine, and a sweep skipped by a process restart
        #: is harmless -- the next restart's first call always sweeps,
        #: and cap eviction (which needs no daily marker) still runs
        #: every time the entry count actually exceeds
        #: `MAX_PROGRESS_ENTRIES`, sweep or not.
        self._last_progress_sweep_monotonic = None

    def _cached_read(self, path, default):
        """Memoised ``_read_json(path, default)`` for read-only accessors.

        Revalidates against a single ``os.stat()`` (comparing
        ``st_mtime_ns`` and ``st_size``, not just mtime -- coarse mtime
        granularity on some filesystems could otherwise miss a same-tick
        rewrite) instead of paying a fresh ``open()``/``read()``/
        ``json.loads()`` on every call.

        MUST NOT be used anywhere that needs to observe the CURRENT
        on-disk bytes on every attempt -- :meth:`update_addons`'s
        compare-and-swap (and anything else detecting a concurrent
        writer) calls ``_read_raw``/``_read_json`` directly instead.
        Every mutating method calls :meth:`_invalidate_cache` right
        after it writes/removes its file, so a ``get_*`` right after a
        ``set_*`` on the SAME instance never sees a stale value.
        """
        try:
            stat = os.stat(path)
        except OSError:
            self._read_cache.pop(path, None)
            return _read_json(path, default)
        fingerprint = (stat.st_mtime_ns, stat.st_size)
        cached = self._read_cache.get(path)
        if cached is not None and cached[0] == fingerprint:
            return cached[1]
        value = _read_json(path, default)
        self._read_cache[path] = (fingerprint, value)
        return value

    def _invalidate_cache(self, path):
        """Drop any memoised value for ``path``. Called by every
        mutating method immediately after it writes or removes the file
        it owns, so the cache never outlives the bytes it describes."""
        self._read_cache.pop(path, None)

    # -- addons ----------------------------------------------------------

    def get_addons(self):
        """Return the list of installed addon descriptors.

        Seeds and persists :data:`DEFAULT_ADDONS` the first time this is
        called (including recovery from a missing/corrupt addons.json).
        Served from the per-instance read cache -- see `_cached_read`.
        """
        addons = self._cached_read(self._addons_path, None)
        if not isinstance(addons, list):
            addons = copy.deepcopy(DEFAULT_ADDONS)
            self.set_addons(addons)
        return addons

    def set_addons(self, addons):
        addons = list(addons)
        _atomic_write(self._addons_path, addons)
        self._invalidate_cache(self._addons_path)

    def update_addons(self, transform, max_attempts=3):
        """Safely read-modify-write the addon list against concurrent writers.

        Kodi can run ``default.py`` as separate concurrent OS processes, so
        a plain ``get_addons()`` + mutate + ``set_addons()`` sequence can
        silently lose whichever write happens first if two processes
        interleave (last-writer-wins, no detection). This instead:

        1. Reads addons.json fresh (seeding :data:`DEFAULT_ADDONS` if it is
           missing/corrupt, same as :meth:`get_addons`).
        2. Calls ``transform(current_addons)``, which must return the new
           list to persist -- or ``current_addons`` itself/an equal list to
           mean "no change needed", in which case nothing is written.
        3. Immediately before the atomic replace, re-reads the raw file and
           compares it byte-for-byte to what was read in step 1. If it
           still matches, writes; if it changed -- another process won the
           race -- retries the whole cycle from step 1 against the new
           content it left behind, up to ``max_attempts`` times.

        ``transform`` must not mutate ``current_addons`` in place -- return
        a new list (or the exact same object, completely unchanged) so the
        "did anything change" comparison above stays meaningful.

        This is optimistic-concurrency/compare-and-swap, not an OS lock: it
        needs no platform-specific API (``fcntl``/``msvcrt``), which matters
        because this addon runs on Linux, Windows, Android and macOS. The
        trade-off is a small residual race between the final compare and
        the rename, but that shrinks the original window -- the entire
        read..transform..write cycle, which for a caller like
        :meth:`install_addon` includes a network fetch -- down to a few
        in-process microseconds around an already-atomic ``os.replace``.

        Raises :class:`ConcurrentUpdateError` if every attempt collides.
        Any exception raised by ``transform`` itself (e.g. the
        protected-addon refusal in :meth:`remove_addon`) propagates
        immediately, without retrying.
        """
        attempt = 0
        while True:
            attempt += 1
            baseline_raw = _read_raw(self._addons_path)
            current = _parse_json(baseline_raw, None)
            if not isinstance(current, list):
                current = copy.deepcopy(DEFAULT_ADDONS)
            new_value = transform(current)
            if new_value == current and baseline_raw is not None:
                return current
            if _read_raw(self._addons_path) == baseline_raw:
                self.set_addons(new_value)
                return new_value
            if attempt >= max_attempts:
                raise ConcurrentUpdateError(
                    "could not update %s after %d attempts: another "
                    "process kept writing it concurrently"
                    % (ADDONS_FILENAME, max_attempts)
                )
            # Another process changed addons.json since our read above;
            # loop around and retry the whole read+transform against the
            # fresh content it left behind.

    def install_addon(self, transport_url, manifest):
        """Add or replace the addon descriptor for ``transport_url``.

        Safe against a concurrent ``default.py`` process modifying
        addons.json at the same time -- see :meth:`update_addons`.
        """
        def _install(addons):
            filtered = [
                addon
                for addon in addons
                if addon.get("transportUrl") != transport_url
            ]
            filtered.append(
                {"transportUrl": transport_url, "manifest": manifest, "flags": {}}
            )
            return filtered

        self.update_addons(_install)

    def remove_addon(self, transport_url):
        """Remove the addon descriptor for ``transport_url``.

        Raises :class:`ValueError` if the addon is flagged ``protected``
        (the built-in official addons); no-ops if it is not installed.

        Safe against a concurrent ``default.py`` process modifying
        addons.json at the same time -- see :meth:`update_addons`.
        """
        def _remove(addons):
            target = next(
                (a for a in addons if a.get("transportUrl") == transport_url), None
            )
            if target is None:
                return addons
            if target.get("flags", {}).get("protected"):
                raise ValueError(
                    "cannot remove protected addon: %s" % transport_url
                )
            return [a for a in addons if a.get("transportUrl") != transport_url]

        self.update_addons(_remove)

    # -- auth --------------------------------------------------------------

    def get_auth(self):
        """Return ``{"authKey": ..., "user": {...}}`` or ``None``.
        Served from the per-instance read cache -- see `_cached_read`."""
        auth = self._cached_read(self._auth_path, None)
        return auth if isinstance(auth, dict) else None

    def set_auth(self, auth):
        """Persist the auth state, or clear it when ``auth`` is ``None``.

        Unlike the addons list, auth.json is never read-modify-written --
        every caller either replaces it wholesale with a fresh login result
        or clears it on logout -- so there is no lost-update race to guard
        against here and no need for :meth:`Store.update_addons`-style
        compare-and-swap retries.
        """
        if auth is None:
            try:
                os.remove(self._auth_path)
            except OSError:
                pass
            self._invalidate_cache(self._auth_path)
            return
        _atomic_write(self._auth_path, auth)
        self._invalidate_cache(self._auth_path)

    # -- search history ------------------------------------------------------

    def get_search_history(self):
        """Return past search queries, most recent first. Served from
        the per-instance read cache -- see `_cached_read`."""
        history = self._cached_read(self._search_history_path, None)
        return history if isinstance(history, list) else []

    def add_search_query(self, query):
        """Record ``query`` at the front of the search history, deduping
        case-insensitively (an existing entry is moved to the front rather
        than duplicated) and capping the list at :data:`MAX_SEARCH_HISTORY`.
        A blank/whitespace-only query is a no-op.

        Like ``auth.json``, this is a plain read-modify-write with no
        :meth:`update_addons`-style compare-and-swap: a search query is
        low-stakes, so a lost update under concurrent ``default.py``
        processes at worst drops or duplicates one history entry, never
        corrupts the file.
        """
        query = (query or "").strip()
        if not query:
            return
        history = [q for q in self.get_search_history() if q.lower() != query.lower()]
        history.insert(0, query)
        _atomic_write(self._search_history_path, history[:MAX_SEARCH_HISTORY])
        self._invalidate_cache(self._search_history_path)

    def clear_search_history(self):
        """Delete all persisted search history."""
        try:
            os.remove(self._search_history_path)
        except OSError:
            pass
        self._invalidate_cache(self._search_history_path)

    # -- now playing / resume / progress (LibrarySync) ----------------------

    def get_now_playing(self):
        """Return the current Rivulet "now playing" context dict (the
        cross-slice Contract shape: ``{'type', 'id', 'video_id', 'name',
        'poster', 'started_at'}``), or ``None`` while nothing Rivulet-
        originated is playing. Served from the per-instance read cache --
        see `_cached_read`."""
        context = self._cached_read(self._now_playing_path, None)
        return context if isinstance(context, dict) else None

    def set_now_playing(self, context):
        """Persist/replace the "now playing" context, or clear it when
        ``context`` is ``None`` (playback ended). Wholesale replace-or-
        clear like :meth:`set_auth` -- see the module docstring. Written
        compactly (see `_atomic_write`): this file is rewritten on every
        playback start/stop and never hand-inspected."""
        if context is None:
            try:
                os.remove(self._now_playing_path)
            except OSError:
                pass
            self._invalidate_cache(self._now_playing_path)
            return
        _atomic_write(self._now_playing_path, dict(context), compact=True)
        self._invalidate_cache(self._now_playing_path)

    def get_resume_offset_ms(self):
        """Return the pending resume-seek offset (milliseconds) queued by
        ``lib.ui.player`` for the NEXT playback session's ``onAVStarted``,
        or ``None`` if there is nothing queued. Served from the
        per-instance read cache -- see `_cached_read`."""
        payload = self._cached_read(self._resume_offset_path, None)
        return payload.get("offset_ms") if isinstance(payload, dict) else None

    def set_resume_offset_ms(self, offset_ms):
        """Queue a pending resume-seek offset, or clear it when
        ``offset_ms`` is ``None``. ``lib.service_runner``'s playback-
        progress tracker consumes and clears this exactly once, on the
        next ``onAVStarted``, so a later session (e.g. auto-playing the
        next episode) never re-seeks using a stale value. Written
        compactly (see `_atomic_write`): machine-only, one small int."""
        if offset_ms is None:
            try:
                os.remove(self._resume_offset_path)
            except OSError:
                pass
            self._invalidate_cache(self._resume_offset_path)
            return
        _atomic_write(self._resume_offset_path, {"offset_ms": int(offset_ms)}, compact=True)
        self._invalidate_cache(self._resume_offset_path)

    def get_last_seen_version(self):
        """Return the addon version last recorded by
        `lib.ui.homewindow._notify_if_updated`, or ``None`` if nothing has
        been recorded yet (including a missing/corrupt file). Served from
        the per-instance read cache -- see `_cached_read`."""
        payload = self._cached_read(self._last_version_path, None)
        return payload.get("version") if isinstance(payload, dict) else None

    def set_last_seen_version(self, version):
        """Record `version` as the addon version the user has now been
        told about (or silently seeded on first run), so the next launch's
        `_notify_if_updated` comparison doesn't re-fire for the same
        version. Written compactly (see `_atomic_write`): machine-only,
        one small string."""
        _atomic_write(self._last_version_path, {"version": str(version)}, compact=True)
        self._invalidate_cache(self._last_version_path)

    @staticmethod
    def _progress_key(content_type, content_id, video_id=None):
        """Flatten ``(content_type, content_id, video_id)`` into the
        single string key ``progress.json`` indexes by (JSON object keys
        must be strings). Joined with ``"\\x1f"`` (ASCII unit separator)
        rather than ``":"`` -- Stremio ids are themselves colon-delimited
        (e.g. an episode id like ``"tt1234567:1:2"``), so reusing ``":"``
        here could make two different tuples collide; ``"\\x1f"`` never
        appears in a Stremio id."""
        return "\x1f".join((content_type or "", content_id or "", video_id or ""))

    def get_progress(self, content_type, content_id, video_id=None):
        """Return the cached local playback-progress dict for
        ``(content_type, content_id, video_id)`` --
        ``{'position_ms', 'duration_ms', 'updated_at'}`` -- or ``None`` if
        nothing has been recorded yet. Never talks to the Stremio API
        (that is ``lib.stremio.api.StremioAPI``'s job): this is the sole
        source of truth for ``lib.ui.player``'s resume prompt, so it
        works fully offline and while logged out. Served from the
        per-instance read cache -- see `_cached_read`."""
        progress = self._cached_read(self._progress_path, None)
        if not isinstance(progress, dict):
            return None
        entry = progress.get(self._progress_key(content_type, content_id, video_id))
        return entry if isinstance(entry, dict) else None

    def set_progress(self, content_type, content_id, video_id, position_ms, duration_ms, now):
        """Record/replace the local progress-cache entry for
        ``(content_type, content_id, video_id)``. ``now`` is a caller-
        supplied timestamp string (see ``lib.library.iso8601_utc``) --
        this module stays purely mechanical persistence and never
        formats timestamps itself.

        Plain read-modify-write, no :meth:`update_addons`-style compare-
        and-swap: like :meth:`add_search_query`, a lost update under two
        concurrent writers (``default.py`` and the background service
        sampling at the same moment) at worst drops one sample, which the
        next periodic sample corrects within seconds -- never file
        corruption (writes are still atomic).

        Also bounds ``progress.json`` -- see :func:`_prune_progress` --
        so a long-lived install's cache cannot grow without bound. The
        expensive per-entry age sweep only runs when the entry count
        exceeds :data:`MAX_PROGRESS_ENTRIES` (cap eviction genuinely
        needed) or :data:`PROGRESS_SWEEP_INTERVAL_SECONDS` has elapsed
        since this instance's last sweep -- see
        ``_last_progress_sweep_monotonic`` and :func:`_prune_progress`.
        """
        progress = _read_json(self._progress_path, None)
        if not isinstance(progress, dict):
            progress = {}
        key = self._progress_key(content_type, content_id, video_id)
        progress[key] = {
            "position_ms": int(position_ms),
            "duration_ms": int(duration_ms),
            "updated_at": now,
        }
        last_sweep = self._last_progress_sweep_monotonic
        monotonic_now = time.monotonic()
        sweep_age = (
            len(progress) > MAX_PROGRESS_ENTRIES
            or last_sweep is None
            or (monotonic_now - last_sweep) >= PROGRESS_SWEEP_INTERVAL_SECONDS
        )
        progress = _prune_progress(progress, now, keep_key=key, sweep_age=sweep_age)
        if sweep_age:
            self._last_progress_sweep_monotonic = monotonic_now
        # Compact (see `_atomic_write`): rewritten every ~15s during
        # playback, machine-only, never hand-inspected.
        _atomic_write(self._progress_path, progress, compact=True)
        self._invalidate_cache(self._progress_path)

    def get_progress_entries(self):
        """Return every cached local playback-progress entry, flattened
        to ``{'type', 'id', 'video_id', 'position_ms', 'duration_ms',
        'updated_at'}`` dicts -- ``video_id`` is ``None`` when the
        stored key part is ``''`` (a movie, see :meth:`_progress_key`).
        Feeds ``lib.ui.continuewatching``'s Home "Continue watching"
        row from the same on-disk cache :meth:`get_progress` reads, so
        it works fully offline and while logged out too.

        An entry is skipped -- never raised -- if its key doesn't split
        into exactly the three ``"\\x1f"``-joined parts
        :meth:`_progress_key` writes, or its value isn't a dict with
        numeric ``position_ms``/``duration_ms``, the same tolerance
        :meth:`get_progress` applies to a missing/corrupt file. Served
        from the per-instance read cache -- see `_cached_read`.
        """
        progress = self._cached_read(self._progress_path, None)
        if not isinstance(progress, dict):
            return []
        entries = []
        for key, value in progress.items():
            if not isinstance(key, str):
                continue
            parts = key.split("\x1f")
            if len(parts) != 3:
                continue
            if not isinstance(value, dict):
                continue
            position_ms = value.get("position_ms")
            duration_ms = value.get("duration_ms")
            if not isinstance(position_ms, (int, float)) or isinstance(position_ms, bool):
                continue
            if not isinstance(duration_ms, (int, float)) or isinstance(duration_ms, bool):
                continue
            content_type, content_id, video_id = parts
            entries.append({
                "type": content_type,
                "id": content_id,
                "video_id": video_id or None,
                "position_ms": position_ms,
                "duration_ms": duration_ms,
                "updated_at": value.get("updated_at"),
            })
        return entries
