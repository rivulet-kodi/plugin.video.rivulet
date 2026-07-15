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
"""

import copy
import json
import os
import tempfile

ADDONS_FILENAME = "addons.json"
AUTH_FILENAME = "auth.json"
SEARCH_HISTORY_FILENAME = "search_history.json"

#: Most-recent-first cap for the persisted search history list.
MAX_SEARCH_HISTORY = 15

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


def _atomic_write(path, data):
    """Write ``data`` as JSON to ``path`` via a tmp-file + rename."""
    directory = os.path.dirname(path) or "."
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp-", dir=directory)
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=2, sort_keys=False)
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

    # -- addons ----------------------------------------------------------

    def get_addons(self):
        """Return the list of installed addon descriptors.

        Seeds and persists :data:`DEFAULT_ADDONS` the first time this is
        called (including recovery from a missing/corrupt addons.json).
        """
        addons = _read_json(self._addons_path, None)
        if not isinstance(addons, list):
            addons = copy.deepcopy(DEFAULT_ADDONS)
            self.set_addons(addons)
        return addons

    def set_addons(self, addons):
        _atomic_write(self._addons_path, list(addons))

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
                current = [dict(addon) for addon in DEFAULT_ADDONS]
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
        """Return ``{"authKey": ..., "user": {...}}`` or ``None``."""
        auth = _read_json(self._auth_path, None)
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
            return
        _atomic_write(self._auth_path, auth)

    # -- search history ------------------------------------------------------

    def get_search_history(self):
        """Return past search queries, most recent first."""
        history = _read_json(self._search_history_path, None)
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

    def clear_search_history(self):
        """Delete all persisted search history."""
        try:
            os.remove(self._search_history_path)
        except OSError:
            pass
