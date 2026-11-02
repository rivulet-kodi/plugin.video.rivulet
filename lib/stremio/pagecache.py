# SPDX-FileCopyrightText: 2026 M0Rf30
# SPDX-License-Identifier: MIT

"""Slim stale-while-revalidate cache for one catalog page.

This is not ``metacache.py``. That file refuses catalog bodies on
purpose; a shelf page lives here, in its own JSON file, with its own
byte budget. Only the fields a poster row, the hero panel and a decade sort
need are kept. Cast, trailers, links and awards are dropped before
the write.

Fresh for 15 minutes. After that the page is still served, and the
caller revalidates, for one hour plus a further hour on revalidation
failure. Past that the lookup is a miss unless explicitly allowed to
serve an expired page. Production clients do not expose envelope hints.

One ``save`` writes the whole file. Callers store a page when they
fetch it, not on every remote click.
"""

import json
import os
import threading
import time

from lib.stremio import jsonfile

#: About half a megabyte. Oldest pages go first. A single page that is
#: itself over the budget is kept: evicting it would make the cache
#: refuse the only page the screen needs.
BUDGET_BYTES = 512 * 1024

DEFAULT_MAX_AGE = 15 * 60
DEFAULT_STALE_REVALIDATE = 3600
DEFAULT_STALE_ERROR = 3600

#: How old a page may be and still paint a first screen, when the caller
#: asks for it (``allow_expired``) and revalidates at once. Past the
#: stale windows above (about two hours) the first open of an evening was
#: a cold fetch for every row (#13). A day covers that and no more.
EXPIRED_SERVE_SECONDS = 24 * 3600

_DESCRIPTION_LIMIT = 280

_SLIM_FIELDS = (
    'id', 'type', 'name', 'poster', 'background', 'logo',
    'year', 'releaseInfo', 'released', 'runtime', 'imdbRating', 'popularity',
    'genres',
    # The IN CINEMA tag (`playbackmeta.in_cinema()`) is "recent `released`
    # and no `dvdRelease`". Dropping the second half would tag every
    # recent title that already has a home release as soon as its page
    # came back from this cache.
    'dvdRelease',
    # The hide-adult check runs on every read of a cached page
    # (lib/ui/catalogpages.py), so the signals it reads are kept.
    'adult', 'genre',
    # The region checklist reads these. Dropping them made a cached row
    # look unlabeled, so a hidden country came back on the next launch.
    'country', 'language', 'originalLanguage', 'original_language',
    'rottenTomatoes', 'rtRating', 'tomatoMeter', 'tomatoes',
    'metascore', 'metacritic', 'metaScore',
)

#: Words that identify a score the hero panel can print. Anything else
#: in a `ratings` blob (Trakt, awards) is dropped with the rest of the
#: meta. "rt" is matched whole so it does not keep "runtime".
_RATING_NEEDLES = ('imdb', 'internet movie', 'rotten', 'tomato', 'metacritic', 'metascore')


def _keep_rating(source):
    low = str(source or '').lower().strip()
    if low == 'rt':
        return True
    return any(needle in low for needle in _RATING_NEEDLES)


def _slim_ratings(ratings):
    """The scores the hero panel prints, or None when there are none."""
    if isinstance(ratings, dict):
        kept = {}
        for key, value in ratings.items():
            if _keep_rating(key):
                kept[key] = value
        return kept or None
    if isinstance(ratings, list):
        kept = []
        for entry in ratings:
            if not isinstance(entry, dict):
                continue
            source = entry.get('source') or entry.get('name') or ''
            if not _keep_rating(source):
                continue
            value = entry.get('value') if 'value' in entry else entry.get('rating')
            kept.append({'source': source, 'value': value})
            if len(kept) == 6:
                break
        return kept or None
    return None


def slim_meta(meta):
    """The poster-row slice of one meta, or None when ``meta`` is not a dict."""
    if not isinstance(meta, dict):
        return None
    slim = {}
    for field in _SLIM_FIELDS:
        value = meta.get(field)
        if value is not None:
            slim[field] = value
    ratings = _slim_ratings(meta.get('ratings'))
    if ratings is not None:
        slim['ratings'] = ratings
    description = meta.get('description')
    if isinstance(description, str) and description:
        slim['description'] = description[:_DESCRIPTION_LIMIT]
    popularities = meta.get('popularities')
    if isinstance(popularities, dict) and 'moviedb' in popularities:
        slim['popularities'] = {'moviedb': popularities.get('moviedb')}
    return slim




def cache_key(transport, ctype, cid, extra=None):
    """Stable key: transport, type, catalog id, extra pairs in name order."""
    pairs = []
    for item in extra or []:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            pairs.append((str(item[0]), str(item[1])))
    pairs.sort()
    encoded = '|'.join('%s=%s' % pair for pair in pairs)
    return '%s\n%s\n%s\n%s' % (transport or '', ctype or '', cid or '', encoded)


def _atomic_write(path, payload):
    jsonfile.write_json(path, payload, '.tmp-pages-')


def _valid_entry(entry):
    """Reject malformed disk entries before they reach age/eviction math."""
    if not isinstance(entry, dict) or not jsonfile.finite_number(entry.get('stored_at')):
        return False
    metas = entry.get('metas')
    return isinstance(metas, list) and all(isinstance(meta, dict) for meta in metas)


class PageCache:
    """In-memory pages plus one JSON file. ``clock`` is injectable."""

    def __init__(self, path, clock=None):
        self.path = path
        self._clock = clock or time.time
        self._lock = threading.Lock()
        self._entries = {}
        self._load()

    def _now(self):
        return self._clock()

    def _load(self):
        payload = jsonfile.read_json(self.path)
        entries = payload.get('entries') if isinstance(payload, dict) else None
        if isinstance(entries, dict):
            self._entries = {key: entry for key, entry in entries.items() if _valid_entry(entry)}

    def lookup(self, key, now=None, allow_expired=False):
        """``('fresh'|'stale'|'expired'|'miss', metas or None)``.

        ``expired`` is only answered with ``allow_expired``: a page past
        its stale windows but younger than `EXPIRED_SERVE_SECONDS`, for a
        caller that paints it and revalidates straight away.
        """
        moment = self._now() if now is None else now
        with self._lock:
            entry = self._entries.get(key)
            if not isinstance(entry, dict):
                return 'miss', None
            stored = entry.get('stored_at')
            metas = entry.get('metas')
            if not isinstance(stored, (int, float)) or not isinstance(metas, list):
                return 'miss', None
            age = moment - stored
            fresh_for = DEFAULT_MAX_AGE
            revalidate_for = DEFAULT_STALE_REVALIDATE
            error_for = DEFAULT_STALE_ERROR
            if age < fresh_for:
                return 'fresh', metas
            if age < fresh_for + revalidate_for + error_for:
                return 'stale', metas
            if allow_expired and age < EXPIRED_SERVE_SECONDS:
                return 'expired', metas
            return 'miss', None

    def store(self, key, metas, now=None):
        """Remember a slim copy of ``metas``. Does not write the file."""
        moment = self._now() if now is None else now
        slim = []
        for meta in metas or []:
            item = slim_meta(meta)
            if item is not None:
                slim.append(item)
        entry = {
            'stored_at': moment,
            'metas': slim,
        }
        with self._lock:
            self._entries[key] = entry
            self._evict()

    def _encoded_size(self):
        return len(json.dumps({'entries': self._entries}, separators=(',', ':')).encode('utf-8'))

    def _evict(self):
        """Drop the oldest entry until the payload fits, keeping at least one."""
        self._entries = {key: entry for key, entry in self._entries.items() if _valid_entry(entry)}
        while len(self._entries) > 1 and self._encoded_size() > BUDGET_BYTES:
            oldest = min(self._entries, key=lambda key: self._entries[key].get('stored_at') or 0)
            del self._entries[oldest]

    def save(self):
        """One rewrite of the whole file."""
        # A copy: json.dumps runs outside the lock, and a revalidation
        # storing a page meanwhile must not resize the dict it walks.
        with self._lock:
            payload = {'entries': dict(self._entries)}
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        _atomic_write(self.path, payload)
