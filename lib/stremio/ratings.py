# SPDX-FileCopyrightText: 2026 M0Rf30
# SPDX-License-Identifier: MIT

"""Optional Rotten Tomatoes / Metacritic / IMDb scores for the hero panel.

Cinemeta's catalog meta usually carries `imdbRating` and nothing else.
When the user has put an OMDb key, an MDBList key, or both in settings,
the hero asks that service for the focused title's IMDb id and fills
whatever comes back. An empty key means this module is not called.

The key travels only as a request parameter. Nothing in this module
logs, and `RatingsError` carries a short category (`timeout`,
`rejected`, `not found`) rather than `str(exc)`, because a `requests`
error repeats the full URL, query string included.

No xbmc import. The 24-hour cache is a JSON file the caller points at.
"""

import hashlib
import json
import math
import os
import re
import tempfile
import threading
import time

TTL_SECONDS = 24 * 60 * 60

OMDB_URL = 'https://www.omdbapi.com/'
MDBLIST_URL = 'https://api.mdblist.com/'

_TIMEOUT = 8

_IMDB_RE = re.compile(r'tt\d{7,8}')

_EMPTY = frozenset(('n/a', 'na', 'none', 'null'))

_OMDB_RT = frozenset(('rotten tomatoes',))
_OMDB_META = frozenset(('metacritic',))
_OMDB_IMDB = frozenset(('internet movie database',))

#: Exact source names. `tomatoesaudience` is the audience score, not
#: the critic Tomatometer the panel is asking for, so a substring match
#: would file it under Rotten Tomatoes.
_MDB_RT = frozenset(('tomatoes', 'rotten tomatoes', 'rottentomatoes'))
_MDB_AUDIENCE = frozenset(('popcorn', 'tomatoesaudience', 'audience'))
_MDB_META = frozenset(('metacritic', 'metascore'))
_MDB_IMDB = frozenset(('imdb', 'internet movie database'))

_SCORE_KEYS = ('rottenTomatoes', 'rtAudience', 'metascore', 'imdbRating')

_FACT_KEYS = (
    'id', 'type', 'imdb_id', 'imdbId', 'imdbID',
    'imdbRating', 'year', 'releaseInfo', 'released', 'runtime',
    # `playbackmeta.in_cinema()` reads `type`, `released` and this one;
    # once a score arrives the panel text is rebuilt from these facts
    # alone, and the IN CINEMA tag must survive that.
    'dvdRelease',
    'rottenTomatoes', 'rtRating', 'tomatoMeter', 'tomatoes',
    'rtAudience', 'audienceScore', 'popcornmeter',
    'metascore', 'metacritic', 'metaScore',
)


class RatingsError(Exception):
    """A fetch that produced no scores. `empty` is a definitive miss
    (unknown id, 404) and is safe to cache. Anything else should be
    retried later, and the message is a category, never a URL."""

    def __init__(self, category, empty=False, retry_after=0, remaining=None, reset_after=0,
                 providers=None):
        super().__init__(category)
        self.category = category
        self.empty = empty
        hints = [detail[2] for detail in (providers or {}).values()]
        self.retry_after = max([retry_after] + [hint.get('retry_after', 0) for hint in hints])
        self.remaining = remaining
        self.reset_after = max([reset_after] + [hint.get('reset_after', 0) for hint in hints])
        self.providers = providers or {}


class Scores(dict):
    """Keep the dict contract, with provider completeness for cache policy.

    Only the score dictionary is persisted. Provider status and numeric rate
    hints live on this response, never the request URL or configured keys.
    """

    def __init__(self, values, providers=None, partial=False):
        super().__init__(values)
        self.providers = providers or {}
        self.partial = partial or any(
            not empty and category != 'ok'
            for category, empty, _hints in self.providers.values()
        )


def imdb_id_of(value):
    """`tt` plus 7 or 8 digits, or '' when `value` has no IMDb id.

    Episode ids (`tt0944947:1:3`) keep the series id. A dict is checked
    at `imdb_id` / `imdbId` / `imdbID` before `id`, because a Stremio
    id is sometimes `tmdb:…` while the IMDb id sits beside it.
    """
    if isinstance(value, dict):
        for key in ('imdb_id', 'imdbId', 'imdbID', 'id'):
            found = imdb_id_of(value.get(key))
            if found:
                return found
        return ''
    text = str(value or '').strip()
    if not text:
        return ''
    head = text.split(':', 1)[0]
    match = _IMDB_RE.search(head) or _IMDB_RE.search(text)
    return match.group(0) if match else ''


def panel_facts(meta):
    """The slice of a catalog meta the hero panel can draw, and no more.

    Cast, description and the API keys are not in here. Stored on the
    list item so a later cache hit can rebuild the panel without the
    original meta object.
    """
    if not isinstance(meta, dict):
        return {}
    facts = {}
    for key in _FACT_KEYS:
        value = meta.get(key)
        if isinstance(value, bool) or value in (None, ''):
            continue
        if isinstance(value, (int, float)) and value <= 0:
            continue
        facts[key] = value
    genres = meta.get('genres')
    names = []
    if isinstance(genres, list):
        for genre in genres:
            if isinstance(genre, str) and genre.strip():
                names.append(genre.strip())
            if len(names) == 3:
                break
    if names:
        facts['genres'] = names
    return facts


def merge_scores(meta, scores):
    """Copy `meta` with non-empty fetched scores written over the top.

    A score the service did not send is left as the catalog had it, so
    a Cinemeta IMDb rating survives an OMDb reply that only has
    Rotten Tomatoes.
    """
    if not isinstance(meta, dict):
        meta = {}
    merged = dict(meta)
    if not isinstance(scores, dict):
        return merged
    for key in _SCORE_KEYS:
        value = scores.get(key)
        if value:
            merged[key] = value
    return merged


def _complete(scores):
    return all(scores.get(key) for key in _SCORE_KEYS)


def _number(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _score_text(value, percent=False):
    """A display score, or '' for N/A and blanks.

    Rotten Tomatoes is a percent (`91%`). IMDb keeps one decimal
    (`8.8`). Metacritic is a whole number (`82`). A `8.8/10` or
    `82/100` value keeps the part before the slash.
    """
    text = str(value or '').strip()
    if not text or text.lower() in _EMPTY:
        return ''
    if '/' in text:
        text = text.split('/', 1)[0].strip()
    if not text or text.lower() in _EMPTY:
        return ''
    if percent and text.endswith('%'):
        return text
    number = _number(text)
    if number is None:
        return text
    if percent:
        return '%d%%' % int(round(number))
    if number > 10:
        return '%d' % int(round(number))
    return '%.1f' % number


def _take(scores, key, value, percent=False):
    if scores.get(key):
        return
    text = _score_text(value, percent=percent)
    if text:
        scores[key] = text


def parse_omdb(payload):
    """Scores from one OMDb title object. `Response: False` with a
    key or limit error raises. A missing title is an empty dict."""
    if not isinstance(payload, dict):
        return {}
    if str(payload.get('Response') or '').lower() == 'false':
        error = str(payload.get('Error') or '').lower()
        if 'key' in error or 'limit' in error:
            raise RatingsError('rejected')
        return {}
    scores = {}
    _take(scores, 'imdbRating', payload.get('imdbRating'))
    _take(scores, 'metascore', payload.get('Metascore'))
    for entry in payload.get('Ratings') or []:
        if not isinstance(entry, dict):
            continue
        source = str(entry.get('Source') or '').strip().lower()
        value = entry.get('Value')
        if source in _OMDB_RT:
            _take(scores, 'rottenTomatoes', value, percent=True)
        elif source in _OMDB_META:
            _take(scores, 'metascore', value)
        elif source in _OMDB_IMDB:
            _take(scores, 'imdbRating', value)
    return scores


def parse_mdblist(payload):
    """Scores from one MDBList title object or a one-element list."""
    if isinstance(payload, list):
        payload = payload[0] if payload and isinstance(payload[0], dict) else None
    if not isinstance(payload, dict):
        return {}
    scores = {}
    for entry in payload.get('ratings') or []:
        if not isinstance(entry, dict):
            continue
        source = str(entry.get('source') or '').strip().lower()
        value = entry.get('value')
        if source in _MDB_RT:
            _take(scores, 'rottenTomatoes', value, percent=True)
        elif source in _MDB_AUDIENCE:
            _take(scores, 'rtAudience', value, percent=True)
        elif source in _MDB_META:
            _take(scores, 'metascore', value)
        elif source in _MDB_IMDB:
            _take(scores, 'imdbRating', value)
    return scores


def _positive_number(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) and number >= 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def _rate_hints(response):
    headers = {str(key).lower(): value for key, value in
               (getattr(response, 'headers', None) or {}).items()}
    retry = _positive_number(headers.get('retry-after')) or 0
    reset = _positive_number(headers.get('x-ratelimit-reset'))
    reset_after = max(0, reset - time.time()) if reset is not None else 0
    return {'retry_after': max(retry, reset_after), 'reset_after': reset_after,
            'remaining': _positive_number(headers.get('x-ratelimit-remaining'))}


def _get_json(get, url, params, timeout, on_headers=None):
    try:
        response = get(url, params=params, timeout=timeout)
    except Exception as exc:
        raise RatingsError(type(exc).__name__)
    try:
        hints = _rate_hints(response)
        if on_headers is not None:
            on_headers(hints)
        status = getattr(response, 'status_code', 0)
        if status == 404:
            raise RatingsError('not found', empty=True, **hints)
        if status in (401, 403):
            raise RatingsError('rejected', **hints)
        if status == 429:
            raise RatingsError('quota', **hints)
        if status == 503:
            try:
                error = response.json()
            except Exception:
                error = None
            if isinstance(error, dict) and 'rate limiter' in str(error.get('error') or '').lower():
                raise RatingsError('quota', **hints)
        if status != 200:
            raise RatingsError('http %s' % status, **hints)
        try:
            payload = response.json()
        except Exception:
            raise RatingsError('json', **hints)
    finally:
        close = getattr(response, 'close', None)
        if close is not None:
            try:
                close()
            except Exception:
                pass
    return payload


def _ask(get, url, params, timeout, parse, on_rejected=None, on_result=None):
    """One service: scores, definitive miss, failure; safe status separately."""
    hints = {}
    try:
        found = parse(_get_json(get, url, params, timeout, hints.update))
    except RatingsError as exc:
        if exc.category in ('rejected', 'quota') and on_rejected is not None:
            on_rejected()
        if on_result is not None:
            on_result(exc.category, exc.empty, hints)
        return {}, exc.empty, not exc.empty
    if on_result is not None:
        on_result('ok', not bool(found), hints)
    if found:
        return found, False, False
    return {}, True, False


#: MDBList looks a title up under its media type: ``imdb/movie/<id>`` or
#: ``imdb/show/<id>``. A series asked for on the movie path is not found.
_MDBLIST_MOVIE = 'movie'
_MDBLIST_SHOW = 'show'
_SHOW_TYPES = frozenset(('series', 'tv', 'show', 'anime'))


def mdblist_media_type(stype):
    """``'movie'`` or ``'show'`` for a Stremio type.

    Series, tv and anime are shows; a dotted subtype is read by its last
    part, so ``anime.movie`` is a movie and ``anime.series`` a show.
    Anything else, an empty type included, is looked up as a movie.
    """
    parts = [part for part in str(stype or '').strip().lower().split('.') if part]
    if not parts:
        return _MDBLIST_MOVIE
    if parts[-1] == 'movie':
        return _MDBLIST_MOVIE
    if parts[-1] in _SHOW_TYPES or parts[0] in _SHOW_TYPES:
        return _MDBLIST_SHOW
    return _MDBLIST_MOVIE


def mdblist_url(imdb_id, stype=''):
    """The MDBList lookup URL for `imdb_id` as a title of type `stype`."""
    return '%simdb/%s/%s' % (MDBLIST_URL, mdblist_media_type(stype), imdb_id)


def fetch_scores(imdb_id, omdb_key='', mdblist_key='', get=None, timeout=_TIMEOUT, stype='',
                 on_rejected=None):
    """Scores for one IMDb id, or {} when the title is unknown.

    `stype` is the title's Stremio type; it picks MDBList's movie or
    show path (`mdblist_url`).

    OMDb is asked first. MDBList is asked only when a key is set and
    OMDb left a score out. A transport failure raises `RatingsError`
    when no scores survive. A dict-compatible Scores response marks partial
    answers when another provider failed. A definitive miss
    returns {} and the caller may cache that.

    `get` is `requests.get`-shaped (`url`, `params`, `timeout`) so
    tests never open a socket.
    """
    imdb_id = imdb_id_of(imdb_id)
    omdb_key = (omdb_key or '').strip()
    mdblist_key = (mdblist_key or '').strip()
    if not imdb_id or not (omdb_key or mdblist_key):
        return {}
    if get is None:
        import requests
        get = requests.get
    scores = {}
    providers = {}
    def result(provider):
        return lambda category, empty, hints: providers.update({provider: (category, empty, hints)})
    saw_error = False
    if omdb_key:
        found, _empty, error = _ask(get, OMDB_URL, {
            'i': imdb_id, 'apikey': omdb_key, 'tomatoes': 'true',
        }, timeout, parse_omdb,
           (lambda: on_rejected('omdb')) if on_rejected is not None else None, result('omdb'))
        scores.update(found)
        saw_error = error
    if mdblist_key and not _complete(scores):
        found, _empty, error = _ask(get, mdblist_url(imdb_id, stype), {
            'apikey': mdblist_key,
        }, timeout, parse_mdblist,
           (lambda: on_rejected('mdblist')) if on_rejected is not None else None, result('mdblist'))
        for key, value in found.items():
            if value and not scores.get(key):
                scores[key] = value
        saw_error = saw_error or error
    if saw_error and not scores:
        raise RatingsError('unavailable', providers=providers)
    return Scores(scores, providers)


def _atomic_write(path, payload):
    directory = os.path.dirname(path) or '.'
    fd, tmp_path = tempfile.mkstemp(prefix='.tmp-ratings-', dir=directory)
    try:
        with os.fdopen(fd, 'w') as handle:
            handle.write(json.dumps(payload, separators=(',', ':')))
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def _valid_entry(entry):
    """Reject malformed disk entries before they reach age/eviction math."""
    if not isinstance(entry, dict):
        return False
    stamp = entry.get('at')
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float)):
        return False
    try:
        if not math.isfinite(stamp):
            return False
    except OverflowError:
        return False
    ttl = entry.get('ttl', TTL_SECONDS)
    if isinstance(ttl, bool) or not isinstance(ttl, (int, float)):
        return False
    if _positive_number(ttl) is None or ttl <= 0:
        return False
    scores = entry.get('scores')
    if not isinstance(scores, dict):
        return False
    return all(key in _SCORE_KEYS and isinstance(value, (str, int, float))
               and not isinstance(value, bool) for key, value in scores.items())


class RatingsCache:
    """IMDb id to scores, fresh for `TTL_SECONDS`. `clock` is injectable.

    The file holds scores only. A failed lookup is not written: the
    caller decides that, and a bad key must not stick for a day.

    Two locks, always taken `_write_lock` then `_lock`. `_lock` guards the
    in-memory entries and is only ever held for dictionary work, because
    `lookup` runs on the GUI thread on every focus change. `_write_lock`
    serializes the whole-file write, which can take long on slow flash.
    """

    def __init__(self, path, clock=None, keys=('', '')):
        self.path = path
        # v3 invalidates partial answers previously cached for a full day.
        # Entries with older tokens remain
        # readable for eviction, and raw keys never enter the JSON file.
        configured = tuple((key or '').strip() for key in keys)
        self._key_token = hashlib.sha256(json.dumps(('v3',) + configured).encode('utf-8')).hexdigest()
        self._clock = clock or time.time
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._entries = {}
        self._closed = False
        self._load()

    def _now(self):
        return self._clock()

    def _load(self):
        try:
            with open(self.path) as handle:
                payload = json.load(handle)
        except (OSError, ValueError):
            return
        if not isinstance(payload, dict):
            return
        self._entries = {key: entry for key, entry in payload.items() if _valid_entry(entry)}

    def _entry_key(self, imdb_id):
        return '%s\n%s' % (imdb_id, self._key_token) if self._key_token else imdb_id

    def lookup(self, imdb_id, now=None):
        """The cached scores, {} for a cached miss, or None when stale."""
        imdb_id = imdb_id_of(imdb_id)
        if not imdb_id:
            return None
        now = self._now() if now is None else now
        with self._lock:
            entry = self._entries.get(self._entry_key(imdb_id))
            if not isinstance(entry, dict):
                return None
            at = entry.get('at')
            scores = entry.get('scores')
            if isinstance(at, bool) or not isinstance(at, (int, float)):
                return None
            if not isinstance(scores, dict):
                return None
            if now - at > entry.get('ttl', TTL_SECONDS):
                return None
            return Scores(scores, partial=entry.get('ttl', TTL_SECONDS) < TTL_SECONDS)

    def close(self):
        """Retire this cache before Refresh discards its file and workers.

        Taking the write lock ensures a write already in progress finishes
        before Refresh removes the file. Later stores cannot resurrect it.
        """
        with self._write_lock, self._lock:
            self._closed = True

    def store(self, imdb_id, scores, now=None, ttl=None):
        imdb_id = imdb_id_of(imdb_id)
        if not imdb_id:
            return
        if not isinstance(scores, dict):
            scores = {}
        kept = {}
        for key in _SCORE_KEYS:
            value = scores.get(key)
            if value:
                kept[key] = value
        now = self._now() if now is None else now
        if ttl is not None and (isinstance(ttl, bool) or not isinstance(ttl, (int, float))
                                or _positive_number(ttl) is None or ttl <= 0):
            raise ValueError('invalid ratings ttl')
        with self._lock:
            if self._closed:
                return
            self._entries[self._entry_key(imdb_id)] = {'at': now, 'scores': kept}
            if ttl is not None:
                self._entries[self._entry_key(imdb_id)]['ttl'] = ttl
            self._trim()
        self._flush()

    def _flush(self):
        """Write the newest entries to disk without blocking `lookup`.

        The snapshot is taken after `_write_lock` is held, so writers that
        queue up each persist the latest state and an older snapshot can
        never overwrite a newer one.
        """
        with self._write_lock:
            with self._lock:
                if self._closed:
                    return
                snapshot = dict(self._entries)
            directory = os.path.dirname(self.path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            _atomic_write(self.path, snapshot)

    def _trim(self):
        """Bound the file. Oldest stamps go first. Called with the lock."""
        self._entries = {key: entry for key, entry in self._entries.items() if _valid_entry(entry)}
        if len(self._entries) <= 400:
            return
        ordered = sorted(
            self._entries.items(),
            key=lambda item: item[1].get('at', 0) if isinstance(item[1], dict) else 0,
        )
        for key, _entry in ordered[:len(self._entries) - 400]:
            self._entries.pop(key, None)
