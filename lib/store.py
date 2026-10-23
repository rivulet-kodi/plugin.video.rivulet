# SPDX-FileCopyrightText: 2026 M0Rf30
# SPDX-License-Identifier: MIT

"""Local JSON persistence for installed Stremio addons and account auth.

Pure Python -- no ``xbmc*`` imports, unit-testable in isolation. Two flat
JSON files live under ``data_dir``:

* ``addons.json`` -- list of addon descriptors, each shaped like the
  Stremio addon-collection API: ``{"transportUrl": ..., "manifest": ...,
  "flags": {...}}``. Rivulet adds one flag of its own on top of that
  shape: ``flags.disabled`` (present and ``True``, or absent -- never
  ``False``), set by :meth:`Store.set_addon_disabled` to hide an addon
  from catalog/meta/stream/subtitle fan-out (:meth:`Store.get_enabled_addons`)
  while leaving it installed. This is purely local presentation state,
  not part of the Stremio addon-collection schema, so it is never pushed
  by the account sync.
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
  fully offline and while logged out. This module never talks to the
  Stremio account API itself: ``lib.service_runner`` pushes samples with
  ``datastore_put`` and pulls the account's watch state back in through
  :func:`Store.merge_remote_progress`, both only when logged in AND the
  "sync_progress" setting is on.
* ``progress_dismissed.json`` -- titles removed from Continue, with the
  time, so the library pull does not bring them back (see
  :meth:`Store.remove_progress_title`). Read fresh, not cached.
* ``last_version.json`` -- the addon version the user was last told
  about (see :meth:`Store.get_last_seen_version`).
* ``home_layout.json`` -- the user's Home arrangement
  (:meth:`Store.get_home_layout`), read fresh rather than through the
  read cache.

Writes are atomic (write to a temp file, fsync it, then ``os.replace``) so
a crash or power loss never leaves a half-written JSON file behind.

Reads tell three situations apart, because treating them alike once let a
transient error wipe a user's data: a file that is *missing* is seeded or
treated as empty; one that is *unreadable* right now (Windows sharing
violation, flaky Android storage -- an ``OSError`` other than "not found")
is never written over, since its content is probably fine; one that is
*corrupt* (not valid UTF-8/JSON, or the wrong shape) is copied to
``<name>.corrupt`` before the fresh default replaces it, so the user's
bytes stay recoverable.

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
import shutil
import tempfile
import time
import zlib

ADDONS_FILENAME = "addons.json"
AUTH_FILENAME = "auth.json"
SEARCH_HISTORY_FILENAME = "search_history.json"
NOW_PLAYING_FILENAME = "now_playing.json"
RESUME_OFFSET_FILENAME = "resume_offset.json"
PROGRESS_FILENAME = "progress.json"
LAST_VERSION_FILENAME = "last_version.json"
HOME_LAYOUT_FILENAME = "home_layout.json"
#: ``{"<type>\x1f<id>": "<iso8601 dismissed at>"}`` -- titles removed from
#: Continue. See :meth:`Store.remove_progress_title`.
PROGRESS_DISMISSED_FILENAME = "progress_dismissed.json"

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


#: ``os.replace`` attempts before :func:`_atomic_write` gives up on a
#: ``PermissionError``. On Windows the swap fails for as long as another
#: process (``default.py`` reading the file, an antivirus scan) holds the
#: target open, which is momentary; the service's playback flush calls this
#: from a ``Player`` callback, where raising would drop the sample.
_REPLACE_ATTEMPTS = 5
_REPLACE_RETRY_DELAY_SECONDS = 0.05

#: A ``.tmp-*`` file older than this is the leftover of a write that never
#: reached ``os.replace`` (Kodi killed mid-write), not one in flight.
_STALE_TMP_SECONDS = 60

#: Outcomes of :func:`_read_text`.
_MISSING = "missing"
_UNREADABLE = "unreadable"
_UNDECODABLE = "undecodable"
_CORRUPT = "corrupt"
_OK = "ok"


def _replace_with_retry(tmp_path, path):
    """``os.replace`` that rides out a transient Windows sharing violation."""
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(tmp_path, path)
            return
        except PermissionError:
            if attempt == _REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(_REPLACE_RETRY_DELAY_SECONDS)


def _atomic_write(path, data, compact=False):
    """Write ``data`` as JSON to ``path`` via a tmp-file + rename.

    The temp file is fsynced before the rename: without it ext4 and the
    SD/eMMC boxes this addon runs on can persist the rename but not the
    data, leaving a zero-length file after power loss -- which reads as
    corrupt and would reseed ``addons.json`` or log the user out.

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
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
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
            fh.flush()
            os.fsync(fh.fileno())
        _replace_with_retry(tmp_path, path)
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def _sweep_stale_tmp(directory):
    """Delete ``.tmp-*`` leftovers older than :data:`_STALE_TMP_SECONDS`.

    Progress is written every ~15s during playback, so a Kodi kill between
    ``mkstemp`` and ``os.replace`` is likely to strand one; nothing else
    ever removes it. The age guard keeps a write in flight in another
    process safe.
    """
    cutoff = time.time() - _STALE_TMP_SECONDS
    try:
        names = os.listdir(directory)
    except OSError:
        return
    for name in names:
        if not name.startswith(".tmp-"):
            continue
        tmp_path = os.path.join(directory, name)
        try:
            if os.path.getmtime(tmp_path) < cutoff:
                os.remove(tmp_path)
        except OSError:
            pass


def _read_text(path):
    """Return ``(status, raw)`` for ``path``, telling apart a file that is
    absent from one that exists but cannot be used.

    ``_MISSING`` -- no such file. ``_UNREADABLE`` -- an I/O error other
    than "not found" (sharing violation, flaky storage): the content may
    be perfectly good, so callers must not write over it.
    ``_UNDECODABLE`` -- the bytes are not UTF-8, so no JSON this module
    wrote. ``_OK`` -- ``raw`` is the exact on-disk text.

    Reading the raw text once and parsing that exact string (rather than
    reading the file twice) guarantees a parsed value and its before/after
    fingerprint always describe identical bytes -- which
    ``Store.update_addons`` relies on to tell a concurrent writer's change
    apart from its own read.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            return _OK, fh.read()
    except FileNotFoundError:
        return _MISSING, None
    except UnicodeDecodeError:
        return _UNDECODABLE, None
    except OSError:
        return _UNREADABLE, None


def _read_raw(path):
    """Return the on-disk text of ``path``, or ``None`` if it is missing
    or unusable. For read-only accessors that only need a best-effort
    value; anything that may write back must use :func:`_load_json`."""
    return _read_text(path)[1]


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


def _classify_json(text_result, expected_type):
    """Turn a :func:`_read_text` result into ``(status, value)``.

    ``status`` is :data:`_OK` only when the text parsed to an instance of
    ``expected_type``; text that parses to something else counts as
    :data:`_CORRUPT` like malformed JSON does, and so does an undecodable
    file. :data:`_MISSING` and :data:`_UNREADABLE` pass through. ``value``
    is ``None`` unless the status is :data:`_OK`.
    """
    status, raw = text_result
    if status == _UNDECODABLE:
        return _CORRUPT, None
    if status != _OK:
        return status, None
    try:
        value = json.loads(raw)
    except ValueError:
        return _CORRUPT, None
    if not isinstance(value, expected_type):
        return _CORRUPT, None
    return _OK, value


def _load_json(path, expected_type):
    """Read ``path`` for a read-modify-write: ``(status, value)`` -- see
    :func:`_classify_json`."""
    return _classify_json(_read_text(path), expected_type)


def _dict_entries(addons):
    """``addons`` without non-dict entries (a hand-edited or partly
    corrupt ``addons.json``), which every ``a.get(...)`` consumer would
    otherwise crash on. Returns ``addons`` itself when it is already clean
    so the read cache's list identity is kept."""
    if all(isinstance(a, dict) for a in addons):
        return addons
    return [a for a in addons if isinstance(a, dict)]


def _keep_corrupt_copy(path):
    """Copy a corrupt ``path`` to ``<path>.corrupt`` before it is replaced
    by a fresh default, so the user's bytes stay recoverable. Best-effort:
    failing to keep the copy must not block recovery."""
    try:
        shutil.copyfile(path, path + ".corrupt")
    except OSError:
        pass


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
        # `exist_ok`: the service and a `default.py` process can both create
        # the directory on a fresh install; the loser must not crash.
        os.makedirs(self.data_dir, exist_ok=True)
        _sweep_stale_tmp(self.data_dir)
        self._addons_path = os.path.join(self.data_dir, ADDONS_FILENAME)
        self._auth_path = os.path.join(self.data_dir, AUTH_FILENAME)
        self._search_history_path = os.path.join(self.data_dir, SEARCH_HISTORY_FILENAME)
        self._now_playing_path = os.path.join(self.data_dir, NOW_PLAYING_FILENAME)
        self._resume_offset_path = os.path.join(self.data_dir, RESUME_OFFSET_FILENAME)
        self._progress_path = os.path.join(self.data_dir, PROGRESS_FILENAME)
        self._dismissed_path = os.path.join(self.data_dir, PROGRESS_DISMISSED_FILENAME)
        self._last_version_path = os.path.join(self.data_dir, LAST_VERSION_FILENAME)
        #: Per-instance memoisation for `_cached_read`: `path -> ((mtime_ns,
        #: size), parsed_value)`. A single process/screen-render often
        #: calls e.g. `get_addons()` many times; a fresh eMMC/SD `open()`
        #: + `json.loads()` per call is far pricier than one `os.stat()`.
        #: NEVER consulted by `update_addons`'s compare-and-swap, which
        #: must always observe the current on-disk bytes -- see
        #: `_cached_read`'s docstring. Its key set is closed on purpose --
        #: exactly seven fixed paths (addons, auth, search_history,
        #: now_playing, resume_offset, last_version, progress), which is
        #: why it needs no size cap; `home_layout.json` is deliberately not
        #: one of them. Adding a path is a decision to revisit that bound.
        self._read_cache = {}
        self._home_layout_path = os.path.join(self.data_dir, HOME_LAYOUT_FILENAME)
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

    def _checked_home_layout(self, value):
        """``normalize(value)``, or ``None`` after logging why it was refused."""
        from lib.homelayout import LayoutError, normalize
        from lib.stremio.addons import addon_error_detail

        try:
            return normalize(value)
        except LayoutError as exc:
            import logging

            logging.getLogger(__name__).warning('Home layout reset: %s', addon_error_detail(exc))
            return None

    def _validated_home_layout(self, value):
        from lib.homelayout import default_layout

        layout = self._checked_home_layout(value)
        return default_layout() if layout is None else layout

    def get_home_layout(self):
        """Read fresh: Settings edits this file in a separate interpreter.

        Avoid adding a data-controlled key to _cached_read's closed set,
        and observe an atomic same-size rewrite even on coarse filesystems.

        A saved layout this build cannot use (corrupt, or from another
        layout version) is moved aside to ``home_layout.json.bak`` and the
        defaults are served. Moving it aside rather than just ignoring it
        does two things: the user's arrangement stays recoverable instead
        of being overwritten by the next Settings edit, and the file stops
        being re-read, re-rejected and re-logged on every render. A file
        that is merely unreadable at this instant is left untouched.
        """
        from lib.homelayout import default_layout

        status, raw = _read_text(self._home_layout_path)
        if status in (_MISSING, _UNREADABLE):
            return default_layout()
        layout = self._checked_home_layout(_parse_json(raw, None))
        if layout is not None:
            return layout
        try:
            os.replace(self._home_layout_path, self._home_layout_path + ".bak")
        except OSError:
            pass
        return default_layout()

    def set_home_layout(self, layout):
        """Validate and replace the arrangement atomically, never API data."""
        value = self._validated_home_layout(layout)
        _atomic_write(self._home_layout_path, value)
        return value

    def _cached_read(self, path, default):
        """Memoised ``_read_json(path, default)`` for read-only accessors.

        Revalidates against ``os.stat()`` (``st_mtime_ns``, ``st_size``)
        PLUS a ``zlib.crc32`` of the file's bytes, not just mtime/size --
        coarse mtime granularity on some filesystems (FAT/exFAT, common
        on the Android storage this addon targets) can report the SAME
        ``st_mtime_ns`` for two writes within the same tick, and a
        same-tick rewrite that happens to keep the byte count identical
        would also alias on ``st_size`` alone, so ``(mtime_ns, size)``
        by itself can miss a concurrent writer's change entirely. The
        files here are at most a few hundred KB, so paying one
        ``read()`` + crc32 per call to safely skip ``json.loads`` is
        cheap.

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
        raw = _read_raw(path)
        if raw is None:
            self._read_cache.pop(path, None)
            return default
        fingerprint = (stat.st_mtime_ns, stat.st_size, zlib.crc32(raw.encode("utf-8")))
        cached = self._read_cache.get(path)
        if cached is not None and cached[0] == fingerprint:
            return cached[1]
        value = _parse_json(raw, default)
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
        called, and again after a corrupt addons.json (kept as
        ``addons.json.corrupt``). A file that is merely unreadable right
        now -- a Windows sharing violation, a flaky Android mount -- is
        NOT reseeded: the defaults are returned for this call only, since
        persisting them would destroy every installed addon over a
        transient error. Served from the per-instance read cache -- see
        `_cached_read`.
        """
        addons = self._cached_read(self._addons_path, None)
        if not isinstance(addons, list):
            status = _load_json(self._addons_path, list)[0]
            addons = copy.deepcopy(DEFAULT_ADDONS)
            if status == _UNREADABLE:
                return addons
            if status == _CORRUPT:
                _keep_corrupt_copy(self._addons_path)
            self.set_addons(addons)
        return _dict_entries(addons)

    def get_enabled_addons(self):
        """Return installed addon descriptors minus the disabled and
        offline ones.

        This is the read path every fan-out call site uses to decide which
        addons to query for catalogs, metas, streams and subtitles.
        :meth:`get_addons` itself remains the full installed list -- the
        one the addon manager UI displays and the Stremio account sync
        pushes -- since ``disabled`` is local presentation state, not part
        of the addon-collection schema being synced. ``offline`` is the
        same kind of local, non-synced state: it marks a builtin entry
        (see :meth:`set_builtin_addon_offline`) whose backing process is
        transiently unavailable, without removing/disabling it.
        """
        return [
            a for a in self.get_addons()
            if not (a.get("flags") or {}).get("disabled") and not (a.get("flags") or {}).get("offline")
        ]

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
           missing or corrupt -- a corrupt file is first kept as
           ``addons.json.corrupt`` -- same as :meth:`get_addons`). A file
           that exists but cannot be read right now counts as a failed
           attempt instead: writing defaults-plus-the-change over it would
           erase every installed addon over a transient error.
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

        Raises :class:`ConcurrentUpdateError` if every attempt collides
        or the file stays unreadable.
        Any exception raised by ``transform`` itself (e.g. the
        protected-addon refusal in :meth:`remove_addon`) propagates
        immediately, without retrying.
        """
        attempt = 0
        while True:
            attempt += 1
            # One read feeds both the parsed value and the compare-and-swap
            # baseline, so they always describe identical bytes.
            baseline = _read_text(self._addons_path)
            status, current = _classify_json(baseline, list)
            if status != _UNREADABLE:
                if status == _OK:
                    current = _dict_entries(current)
                else:
                    current = copy.deepcopy(DEFAULT_ADDONS)
                new_value = transform(current)
                if new_value == current and status == _OK:
                    return current
                if _read_text(self._addons_path) == baseline:
                    if status == _CORRUPT:
                        _keep_corrupt_copy(self._addons_path)
                    self.set_addons(new_value)
                    return new_value
            if attempt >= max_attempts:
                raise ConcurrentUpdateError(
                    "could not update %s after %d attempts: another "
                    "process kept writing it concurrently, or it could "
                    "not be read" % (ADDONS_FILENAME, max_attempts)
                )
            # Another process changed addons.json since our read above (or
            # it was momentarily unreadable); loop around and retry the
            # whole read+transform against what is there now.

    def install_addon(self, transport_url, manifest):
        """Add or update the addon descriptor for ``transport_url``.

        An existing descriptor is updated in place, keeping its position
        and ``flags`` -- only ``manifest`` is replaced. A naive
        filter-then-append (drop the old entry, append a fresh one with
        ``flags: {}``) silently reset flags on every reinstall/update:
        updating a disabled addon re-enabled it, and updating a
        protected one made it removable. A brand-new transport URL still
        gets appended with ``flags: {}``.

        Safe against a concurrent ``default.py`` process modifying
        addons.json at the same time -- see :meth:`update_addons`.
        """
        def _install(addons):
            updated = []
            found = False
            for addon in addons:
                if addon.get("transportUrl") == transport_url:
                    updated.append(dict(addon, manifest=manifest))
                    found = True
                else:
                    updated.append(addon)
            if not found:
                updated.append(
                    {"transportUrl": transport_url, "manifest": manifest, "flags": {}}
                )
            return updated

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
            if (target.get("flags") or {}).get("protected"):
                raise ValueError(
                    "cannot remove protected addon: %s" % transport_url
                )
            return [a for a in addons if a.get("transportUrl") != transport_url]

        self.update_addons(_remove)

    def set_addon_disabled(self, transport_url, disabled):
        """Toggle ``flags.disabled`` for the addon at ``transport_url``.

        Unlike :meth:`remove_addon`, this is reversible -- flipping a bit
        the user can flip back -- so protected (built-in) addons may be
        disabled too; there is no refusal branch here.

        Enabling (``disabled=False``) deletes the ``disabled`` key rather
        than writing ``False``, so addons.json stays clean and an
        already-enabled addon compares equal to itself in
        :meth:`update_addons`'s no-op check. No-ops if ``transport_url``
        is not installed.

        Safe against a concurrent ``default.py`` process modifying
        addons.json at the same time -- see :meth:`update_addons`.
        """
        def _set_disabled(addons):
            target = next(
                (a for a in addons if a.get("transportUrl") == transport_url), None
            )
            if target is None:
                return addons
            new_flags = dict(target.get("flags") or {})
            if disabled:
                new_flags["disabled"] = True
            else:
                new_flags.pop("disabled", None)
            new_target = dict(target)
            new_target["flags"] = new_flags
            return [
                new_target if a.get("transportUrl") == transport_url else a
                for a in addons
            ]

        self.update_addons(_set_disabled)

    def move_addon(self, transport_url, delta):
        """Move the addon at ``transport_url`` by ``delta`` positions
        within the installed list.

        List order is not cosmetic: `lib.ui.views` fans catalog/meta/
        stream requests out across `get_enabled_addons()` in list order,
        so moving an addon earlier moves it earlier in every such
        fan-out too. ``delta`` is directional -- negative moves the
        addon toward index 0, positive moves it toward the end -- and
        the target index is clamped to ``[0, len(addons) - 1]``, so a
        move already pointed past a boundary (e.g. moving the first
        addon up) resolves to the same index it started at. That
        resolves-to-same-index case returns ``addons`` unchanged (the
        very same list object), which :meth:`update_addons` recognises
        as "nothing to write" -- a boundary move is a genuine no-op, not
        a silently-discarded intent that still rewrites the file.

        Raises :class:`ValueError` if ``transport_url`` is not
        installed, the same shape as :meth:`remove_addon`'s
        protected-addon refusal -- unlike removal/disable there is no
        sensible "not installed" position to move to, so this does not
        silently no-op.

        Safe against a concurrent ``default.py`` process modifying
        addons.json at the same time -- see :meth:`update_addons`.
        """
        def _move(addons):
            index = next(
                (i for i, a in enumerate(addons) if a.get("transportUrl") == transport_url),
                None,
            )
            if index is None:
                raise ValueError("addon not installed: %s" % transport_url)
            target = max(0, min(len(addons) - 1, index + delta))
            if target == index:
                return addons
            new_addons = list(addons)
            new_addons.insert(target, new_addons.pop(index))
            return new_addons

        self.update_addons(_move)

    def set_builtin_addon(self, builtin_id, transport_url, manifest):
        """Ensure exactly one addon descriptor flagged `flags.builtin ==
        builtin_id` exists, at `transport_url` with `manifest`, protected
        and unofficial.

        Matched by `flags.builtin`, NOT `transportUrl` -- so a caller
        whose `transport_url` changes (e.g. a bridge addon whose local
        port setting changed) still updates the SAME entry in place
        rather than leaving a stale duplicate behind. If more than one
        entry is already flagged with this `builtin_id` (a duplicate
        persisted before this method de-duplicated, or from external
        tampering with ``addons.json``), only the first is kept and
        updated in place -- every later match is dropped, so a sync
        always repairs the list back down to exactly one. Idempotent:
        calling this every session with an unchanged
        `transport_url`/`manifest`/disabled-state is a no-op write (see
        :meth:`update_addons`'s equality check).

        Unlike :meth:`install_addon`, this always pins
        `flags.official`/`flags.protected`/`flags.builtin` rather than
        preserving whatever flags an existing entry had -- EXCEPT
        `flags.disabled`, which is carried over from the existing entry
        (defaulting to not-disabled for a brand new entry). That is the
        one piece of independent local state a builtin entry can carry:
        a user who disabled this addon via :meth:`set_addon_disabled`
        must stay disabled across every later resync, not be silently
        re-enabled the next time this method runs.

        Safe against a concurrent ``default.py`` process modifying
        addons.json at the same time -- see :meth:`update_addons`.
        """
        def _descriptor(disabled):
            flags = {"official": False, "protected": True, "builtin": builtin_id}
            if disabled:
                flags["disabled"] = True
            return {"transportUrl": transport_url, "manifest": manifest, "flags": flags}

        def _sync(addons):
            updated = []
            found = False
            for addon in addons:
                if (addon.get("flags") or {}).get("builtin") == builtin_id:
                    if found:
                        # Already replaced the first match above -- drop
                        # this extra duplicate instead of keeping it.
                        continue
                    found = True
                    updated.append(_descriptor((addon.get("flags") or {}).get("disabled")))
                else:
                    updated.append(addon)
            if not found:
                updated.append(_descriptor(False))
            return updated

        self.update_addons(_sync)

    def set_builtin_addon_offline(self, builtin_id):
        """Mark the existing entry flagged `flags.builtin == builtin_id`
        as transiently unavailable (`flags.offline = True`), in place --
        preserving its list position and `flags.disabled` -- WITHOUT
        touching `transportUrl`/`manifest` or removing it. No-op if no
        such entry exists yet (nothing published, nothing to mark).

        For `lib.s4me.BridgeSupervisor.apply()`'s TRANSIENT unavailable
        cases (not yet confirmed ready, or a launch/port-change in
        flight): unlike :meth:`remove_builtin_addon` -- for the
        PERMANENT case, the bridge setting is off or Stream4Me itself is
        gone -- this keeps the user's `flags.disabled` choice and the
        entry's position in `addons.json` intact across a blip, instead
        of losing both to a remove-then-re-append. `get_enabled_addons()`
        excludes an offline entry from every fan-out exactly like a
        disabled one. The next successful :meth:`set_builtin_addon` call
        rebuilds `flags` from scratch and so clears `offline` again.

        Safe against a concurrent ``default.py`` process modifying
        addons.json at the same time -- see :meth:`update_addons`.
        """
        def _mark_offline(addons):
            updated = []
            changed = False
            for addon in addons:
                flags = addon.get("flags") or {}
                if flags.get("builtin") == builtin_id and not flags.get("offline"):
                    new_addon = dict(addon)
                    new_addon["flags"] = dict(flags, offline=True)
                    updated.append(new_addon)
                    changed = True
                else:
                    updated.append(addon)
            return updated if changed else addons

        self.update_addons(_mark_offline)

    def remove_builtin_addon(self, builtin_id):
        """Remove any addon descriptor flagged `flags.builtin ==
        builtin_id`. No-ops if none is installed.

        Deliberately bypasses :meth:`remove_addon`'s protected-addon
        refusal: that guard exists to stop a *user* removing a protected
        addon through the UI, not to stop the very code that maintains a
        builtin entry from retracting it once its feature is disabled or
        its prerequisite (e.g. Stream4Me itself) goes missing.

        Safe against a concurrent ``default.py`` process modifying
        addons.json at the same time -- see :meth:`update_addons`.
        """
        def _remove(addons):
            return [a for a in addons if (a.get("flags") or {}).get("builtin") != builtin_id]

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
        if not isinstance(history, list):
            return []
        # A hand-edited or partly corrupt file can hold numbers or nulls;
        # `add_search_query` calls `.lower()` on every entry.
        return [q for q in history if isinstance(q, str)]

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

    def _load_progress_for_update(self):
        """The ``progress.json`` dict for a read-modify-write, or ``None``
        when the file exists but cannot be read right now.

        ``None`` means "do not write": the content is probably intact, and
        writing a one-entry map over it would wipe the whole watch history
        over a transient error (Windows sharing violation, flaky storage).
        Progress is resampled every ~15s, so a dropped write costs nothing.
        A missing file starts empty; a corrupt one is kept as
        ``progress.json.corrupt`` and then starts empty.
        """
        status, progress = _load_json(self._progress_path, dict)
        if status == _UNREADABLE:
            return None
        if status == _CORRUPT:
            _keep_corrupt_copy(self._progress_path)
        return progress if status == _OK else {}

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
        progress = self._load_progress_for_update()
        if progress is None:
            return
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

    def merge_remote_progress(self, entries):
        """Fold progress entries pulled from the Stremio account (see
        ``lib.library.progress_from_library_item``) into ``progress.json``
        in one read-modify-write, and return how many were written.

        Last write wins on ``updated_at``: an entry replaces the local one
        only when it is strictly newer, so a sample this device recorded
        after the account's copy is never rolled back. Both sides use the
        seconds-precision ``lib.library.iso8601_utc`` form, so a plain
        string comparison orders them. Pruned exactly like
        :meth:`set_progress` (full age sweep, then the
        :data:`MAX_PROGRESS_ENTRIES` cap) -- an account with years of
        history must not bloat the local cache past its bound.

        The return value counts only entries that survive that pruning, and
        an entry already older than the age cutoff is never inserted. A pull
        that adds nothing durable therefore returns 0 and leaves the file
        alone, instead of rewriting it (and reporting an "update") on every
        library pull for an account that holds old entries.
        """
        progress = self._load_progress_for_update()
        if progress is None:
            return 0
        now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        now_parsed = _parse_progress_timestamp(now)
        max_age_seconds = MAX_PROGRESS_AGE_DAYS * 86400
        merged = []
        dismissed = self._load_dismissed()
        for entry in entries:
            key = self._progress_key(entry["type"], entry["id"], entry.get("video_id"))
            updated_at = entry["updated_at"]
            updated = _parse_progress_timestamp(updated_at)
            if updated is None or (now_parsed - updated).total_seconds() > max_age_seconds:
                continue
            local = progress.get(key)
            if dismissed.get(self._progress_key(entry["type"], entry["id"], ""), "") >= updated_at:
                # Removed from Continue after the account last saw this
                # title played: the pull must not bring it back. A newer
                # play (on any device) is later than the dismissal and wins.
                continue
            if isinstance(local, dict) and str(local.get("updated_at") or "") >= updated_at:
                continue
            progress[key] = {
                "position_ms": int(entry["position_ms"]),
                "duration_ms": int(entry["duration_ms"]),
                "updated_at": updated_at,
            }
            merged.append(key)
        if not merged:
            return 0
        progress = _prune_progress(progress, now, keep_key=None, sweep_age=True)
        written = sum(1 for key in set(merged) if key in progress)
        if not written:
            return 0
        self._last_progress_sweep_monotonic = time.monotonic()
        _atomic_write(self._progress_path, progress, compact=True)
        self._invalidate_cache(self._progress_path)
        return written

    def remove_progress_title(self, content_type, content_id):
        """Drop every local progress entry for ``(content_type,
        content_id)`` -- the movie's own entry, or every episode of a
        series -- and return how many were removed.

        Backs "Remove from Continue" on Home (issue #65): a show abandoned
        part-way, or episodes stopped during the credits short of
        `RESUME_MAX_PERCENT`, otherwise stay pinned in the resume/next-up
        bands for the 180-day sweep.

        Deleting the local entries is not enough on its own: the service's
        library pull (:meth:`merge_remote_progress`) re-creates them from
        the account's library state at the next Kodi start, and the title
        came straight back. The removal is therefore also recorded in
        ``progress_dismissed.json`` with its time, and the pull skips any
        account entry not newer than that. Plain read-modify-write, same
        reasoning as :meth:`set_progress`.
        """
        prefix = self._progress_key(content_type, content_id, "")
        now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        status, dismissed = _load_json(self._dismissed_path, dict)
        if status == _UNREADABLE:
            raise OSError("progress_dismissed.json is unreadable")
        dismissed = dismissed if status == _OK else {}
        dismissed[prefix] = now
        now_parsed = _parse_progress_timestamp(now)
        dismissed = {
            key: value for key, value in dismissed.items()
            if isinstance(key, str) and isinstance(value, str)
            and _parse_progress_timestamp(value) is not None
            and (now_parsed - _parse_progress_timestamp(value)).total_seconds()
            <= MAX_PROGRESS_AGE_DAYS * 86400
        }
        _atomic_write(self._dismissed_path, dismissed, compact=True)
        status, progress = _load_json(self._progress_path, dict)
        if status != _OK:
            return 0
        # `prefix` ends in the "\x1f" before the (empty) video id, so it
        # never matches a different id that merely starts the same way.
        kept = {key: value for key, value in progress.items()
                if not (isinstance(key, str) and key.startswith(prefix))}
        removed = len(progress) - len(kept)
        if removed:
            _atomic_write(self._progress_path, kept, compact=True)
            self._invalidate_cache(self._progress_path)
        return removed

    def _load_dismissed(self):
        """``{title_prefix: dismissed_at}``; empty when absent or bad."""
        status, dismissed = _load_json(self._dismissed_path, dict)
        return dismissed if status == _OK else {}

    def get_continue_dismissals(self):
        """``{(type, id): dismissed_at}`` for titles removed from Continue.

        Home's Continue row also lists in-progress titles straight from
        the account library (watched in the Stremio app, never on this
        device), which no local progress entry backs. Those need the same
        dismissal check the library pull applies."""
        result = {}
        for key, value in self._load_dismissed().items():
            parts = key.split("\x1f") if isinstance(key, str) else []
            if len(parts) == 3 and isinstance(value, str):
                result[(parts[0], parts[1])] = value
        return result

    def get_progress_entries(self):
        """Return every cached local playback-progress entry, flattened
        to ``{'type', 'id', 'video_id', 'position_ms', 'duration_ms',
        'updated_at'}`` dicts -- ``video_id`` is ``None`` when the
        stored key part is ``''`` (a movie, see :meth:`_progress_key`).
        Feeds ``lib.ui.mystuff``'s merged Home "My Stuff" screen
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
