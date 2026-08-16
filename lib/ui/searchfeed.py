"""Cinemeta's `feed.json` as a ranking oracle for search results.

`lib.ui.searchwindow._rank_by_title()` can order results by how the
returned title relates to the query, but within a tier it has nothing to
say: "Alien" (1979), "Alien: Earth" and "Alien Xmas" all merely start
with the query, and the addons' own order decides which the user sees
first.

The obvious fix - rank on the `imdbRating`/`popularity` the metas
themselves carry - does not work. Search previews are trimmed, and
measured against a real install those two fields were present on only 6
of Cinemeta's 19 results for "alien", and on the OBSCURE titles rather
than the famous ones: ranking on them directly puts "Alien Warfare"
(rating 2.6) above "Alien" (1979), which carries neither field. The
fields are absent, not zero, and their absence correlates with fame.

`feed.json` - a single ~3.7MB document served by Cinemeta's catalog
host, holding ~20k records of `id`/`name`/`type`/`poster`/`releaseInfo`/
`imdbRating`/`popularity` - carries both fields on ~100% of its records.
It is what `stremio-core` indexes for its own search autocompletion (see
that project's `models/local_search.rs`). Here it is used only as an
ORACLE: the live addon fan-out still decides WHICH titles come back, and
the feed only helps order them.

The scoring mirrors `local_search.rs`:

    imdb_boost = exp(rating     / max_rating * 0.5)
    pop_boost  = exp(popularity / max_pop    * 0.5)

Both weights are 0.5 and both terms exponential there, so a title that
is both well rated and widely watched is lifted multiplicatively above
one that is merely well rated. Normalising against the maxima across the
whole record set keeps this relative to the feed's own population rather
than to absolute rating/popularity scales, which the feed does not
document and which are free to change.

The feed is a popularity-ranked HEAD, not the whole Cinemeta corpus, so
most long-tail results are simply absent from it. Those score a neutral
1.0 and therefore keep their tier's existing order - never dropped,
never pushed below titles that ARE in the feed but match the query
worse, because the match tier always outranks the boost.
"""
import json
import math
import os
import tempfile
import time

import xbmc

from lib.ui.compat import log

#: Cinemeta's catalog host - a different host from the v3 API that
#: serves `catalog`/`meta` (`https://v3-cinemeta.strem.io`), which does
#: not serve this document at all. Same constant pair `stremio-core`
#: uses (`CINEMETA_CATALOGS_URL` + `CINEMETA_FEED_CATALOG_ID`).
FEED_URL = 'https://cinemeta-catalogs.strem.io/feed.json'

#: Refresh interval. The feed tracks what is popular now, which moves on
#: the order of days, not minutes - and it is ~3.7MB, far too big to
#: re-fetch on the cadence `lib.ui.metacache` uses for single metas.
TTL_SECONDS = 24 * 60 * 60

#: `stremio-core`'s `INDEX_OPTIONS`, unchanged.
IMDB_RATING_WEIGHT = 0.5
POPULARITY_WEIGHT = 0.5

#: Guard against a malformed/hostile response being written to disk. The
#: real document is ~3.7MB; this leaves generous headroom while still
#: bounding what a compromised or confused host can make the addon
#: store.
MAX_FEED_BYTES = 32 * 1024 * 1024

_FILENAME = 'search-feed.json'


def _path(data_dir):
    return os.path.join(data_dir, _FILENAME)


def _atomic_write(path, data):
    """Write `data` as JSON via a temp file in the same directory, then
    `os.replace()`. Mirrors `lib.ui.metacache._atomic_write()`: a torn
    write here would poison every later search until the TTL expired."""
    directory = os.path.dirname(path) or '.'
    fd, tmp_path = tempfile.mkstemp(prefix='.tmp-', dir=directory)
    try:
        with os.fdopen(fd, 'w') as handle:
            json.dump(data, handle, separators=(',', ':'))
        os.replace(tmp_path, path)
    except OSError:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


def _read_cached(data_dir):
    """The cached `{'ts': ..., 'records': [...]}`, or None when missing,
    unreadable, malformed or expired."""
    try:
        with open(_path(data_dir)) as handle:
            cached = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(cached, dict):
        return None
    records = cached.get('records')
    if not isinstance(records, list):
        return None
    if time.time() - (cached.get('ts') or 0) > TTL_SECONDS:
        return None
    return records


def _fetch(session, timeout):
    """GET the feed. Returns the record list, or None on any failure -
    ranking is an enhancement, so a feed that will not load must leave
    search working exactly as it did without it."""
    try:
        response = session.get(FEED_URL, timeout=timeout)
        response.raise_for_status()
    except Exception as exc:  # noqa: BLE001 - never let ranking break search
        log('searchfeed: fetch failed: %s' % type(exc).__name__, xbmc.LOGWARNING)
        return None
    content_length = len(response.content or b'')
    if content_length > MAX_FEED_BYTES:
        log('searchfeed: feed too large (%d bytes), ignoring' % content_length, xbmc.LOGWARNING)
        return None
    try:
        records = response.json()
    except ValueError:
        log('searchfeed: feed returned invalid JSON', xbmc.LOGWARNING)
        return None
    if not isinstance(records, list):
        log('searchfeed: feed was not a list, ignoring', xbmc.LOGWARNING)
        return None
    return records


def load_records(data_dir, session, timeout=30):
    """The feed's records - from the on-disk cache while it is fresh,
    otherwise re-fetched and cached. Returns [] when the feed is
    unavailable, which callers treat as "rank without it"."""
    cached = _read_cached(data_dir)
    if cached is not None:
        return cached
    records = _fetch(session, timeout)
    if records is None:
        return []
    log('searchfeed: fetched %d feed records' % len(records), xbmc.LOGINFO)
    _atomic_write(_path(data_dir), {'ts': time.time(), 'records': records})
    return records


def build_index(records):
    """`((type, id) -> record, max_rating, max_popularity)` for
    `boost()`.

    The maxima are floored at a tiny positive number rather than at 0:
    they are denominators, and an empty or field-less feed would
    otherwise divide by zero. With a floor, such a feed yields a boost
    of 1.0 everywhere, which is exactly "rank as if there were no feed".
    """
    index = {}
    max_rating = 0.0
    max_popularity = 0.0
    for record in records:
        if not isinstance(record, dict):
            continue
        record_id = record.get('id')
        if not record_id:
            continue
        index[(record.get('type'), record_id)] = record
        rating = _number(record.get('imdbRating'))
        popularity = _number(record.get('popularity'))
        max_rating = max(max_rating, rating)
        max_popularity = max(max_popularity, popularity)
    return index, max(max_rating, 1e-9), max(max_popularity, 1e-9)


def _number(value):
    """`value` as a float, or 0.0 if it is missing or not numeric - the
    feed types these fields loosely (`imdbRating` arrives as a string in
    some records)."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def boost(meta, index, max_rating, max_popularity):
    """`stremio-core`'s multiplicative boost for one meta, or 1.0 when
    the title is not in the feed (or carries neither field)."""
    record = index.get((meta.get('type'), meta.get('id')))
    if record is None:
        return 1.0
    rating = _number(record.get('imdbRating'))
    popularity = _number(record.get('popularity'))
    imdb_boost = math.exp(rating / max_rating * IMDB_RATING_WEIGHT) if rating else 1.0
    popularity_boost = math.exp(popularity / max_popularity * POPULARITY_WEIGHT) if popularity else 1.0
    return imdb_boost * popularity_boost
