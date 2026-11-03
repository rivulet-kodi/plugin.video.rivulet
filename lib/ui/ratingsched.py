# SPDX-FileCopyrightText: 2026 M0Rf30
# SPDX-License-Identifier: MIT

"""Fill the focused hero's ratings without blocking the remote.

The catalog text is drawn immediately. When an OMDb or MDBList key is
set and the 24-hour cache has this IMDb id, that text is rebuilt with
the cached scores.

A miss does not fetch at once. Holding Right across a row used to start
one request per title it passed, and OMDb's free key allows 1,000 a
day. The miss becomes the plan: the focused title, then the next few on
its row. One worker waits until the focus has rested ``_DWELL_SECONDS``
on that plan, then fetches it in order. A new focus replaces the plan
(latest wins), so titles skimmed past are never requested. The worker
does not touch Kodi; after each fetch it posts Action(noop), and the
window's existing onAction paints the hero again, which now hits the
cache.

The key is read on the GUI thread and passed into the worker. It is
never written to the log. A failure is remembered for a minute, keyed
by a hash of the key, so fixing a typo retries at once and a dead
network does not refetch on every arrow press.
"""

import hashlib
import json
import os
import re
import threading
import time

from lib.stremio.ratings import (
    RatingsCache,
    RatingsError,
    imdb_id_of,
    merge_scores,
)

_MAX_CACHE_ENTRIES = 2000
_FAIL_SECONDS = 60
#: Rejected/quota-limited keys pause across titles for five minutes. A
#: corrected key has a different token and is usable immediately.
_PROVIDER_COOLDOWN_SECONDS = 5 * 60
_PARTIAL_TTL_SECONDS = 10 * 60
_MAX_QUOTA_COOLDOWN_SECONDS = 6 * 60 * 60
_provider_state = {}  # type: dict
_prefetch_until = {}  # type: dict
_generation = 0
_context = threading.local()
_focused_imdb = ''
_session = {'active': False, 'complete': 0, 'partial': 0, 'missing': 0, 'hits': 0}
_session_seen = {'missing': set(), 'hits': set()}  # type: dict
_provider_until = {}  # type: dict
#: How long the focus must rest before a miss is fetched.
_DWELL_SECONDS = 0.6
#: Titles after the focused one that ride along once it has been fetched.
PREFETCH = 2
_lock = threading.Lock()
_inflight = set()  # type: set
_failed_until = {}  # type: dict
_cache = None
_cache_path = ''
_cache_keys = ('', '')
#: Latest-wins plan: (imdb id, omdb key, mdblist key, cache) in fetch order,
#: and the monotonic time it was set.
_plan = {'items': [], 'at': 0.0, 'running': False}
_wake = threading.Event()
_stop = threading.Event()


def _token(key):
    text = (key or '').strip()
    if not text:
        return ''
    return hashlib.sha256(text.encode('utf-8')).hexdigest()[:12]


def _fail_id(imdb_id, omdb_key, mdblist_key):
    return '%s\n%s\n%s' % (imdb_id, _token(omdb_key), _token(mdblist_key))


def _keys():
    from lib.ui.compat import ADDON

    omdb = (ADDON.getSetting('ratings_omdb_key') or '').strip()
    mdblist = (ADDON.getSetting('ratings_mdblist_key') or '').strip()
    return omdb, mdblist


def _get_cache(omdb_key='', mdblist_key=''):
    global _cache, _cache_path, _cache_keys
    from lib.ui.compat import addon_profile_dir

    path = os.path.join(addon_profile_dir(), 'ratings.json')
    key_ids = (_token(omdb_key), _token(mdblist_key))
    with _lock:
        if _cache is None or path != _cache_path or key_ids != _cache_keys:
            if _cache is not None:
                _cache.close()
            _cache = RatingsCache(path, keys=(omdb_key, mdblist_key))
            _cache_path = path
            _cache_keys = key_ids
        return _cache


def _type_of(meta):
    """The Stremio type a meta (or its panel facts) carries, or ''."""
    if not isinstance(meta, dict):
        return ''
    value = meta.get('type')
    return value.strip() if isinstance(value, str) else ''


def _misses(cache, metas):
    """`(imdb_id, type)` for the `metas` the cache does not hold, in order."""
    found = []
    seen = set()
    for meta in metas or ():
        try:
            imdb_id = imdb_id_of(meta)
        except Exception:
            imdb_id = None
        if imdb_id and imdb_id not in seen and cache.lookup(imdb_id) is None:
            seen.add(imdb_id)
            found.append((imdb_id, _type_of(meta)))
    return found


_meta_ids = {}  # type: dict


def _facts_with_imdb(meta):
    """Resolve only from already-held previews or the existing disk meta cache."""
    if imdb_id_of(meta) or not isinstance(meta, dict) or not meta.get('id'):
        return meta
    from lib.ui import metacache, previews
    from lib.ui.compat import addon_profile_dir
    from lib.ui.dependencies import get_store

    sid = meta['id']
    candidate = previews.lookup(sid)
    imdb_id = imdb_id_of(candidate)
    key = (_type_of(meta), sid)
    if not imdb_id:
        # Hits are remembered as well as misses: a card whose scores have
        # not arrived is asked about on every focus change and worker
        # wake-up, and each ask would re-parse up to 256 KB from disk.
        with _lock:
            known = key in _meta_ids
            imdb_id = _meta_ids.get(key)
        if not known:
            folder = getattr(get_store(), 'data_dir', None) or addon_profile_dir()
            imdb_id = imdb_id_of(metacache.load_cached_meta(folder, _type_of(meta), sid))
            with _lock:
                _meta_ids[key] = imdb_id or None
                _cap(_meta_ids)
    return dict(meta, imdb_id=imdb_id) if imdb_id else meta


def needs_refresh(facts_raw):
    """Home's completed-card guard: retry short-lived or expired entries.

    This only reads settings and existing caches. The ordinary overlay path
    still owns scheduling and the 0.6s dwell; the GUI never fetches here.
    """
    try:
        _count()
        keys = _keys()
        if not any(keys):
            return False
        facts = _facts_with_imdb(json.loads(facts_raw))
        imdb_id = imdb_id_of(facts)
        if not imdb_id:
            return False
        scores = _get_cache(*keys).lookup(imdb_id)
        if scores is not None:
            _count('hits', imdb_id)
        return scores is None or getattr(scores, 'partial', False)
    except Exception:
        return False


def _count(kind=None, identity=None, generation=None):
    with _lock:
        if generation is not None and (generation != _generation or _stop.is_set()):
            return
        if not _session['active']:
            _session.update(active=True, complete=0, partial=0, missing=0, hits=0)
            for seen in _session_seen.values():
                seen.clear()
        if kind is None:
            return
        if identity is not None:
            if identity in _session_seen[kind]:
                return
            _session_seen[kind].add(identity)
        _session[kind] += 1


def overlay_text(meta, fallback, upcoming=None):
    """`fallback` (the catalog panel) plus cached scores when a key is set.

    A miss plans a fetch and returns `fallback` unchanged, so the first
    paint still shows the catalog's IMDb rating. `upcoming` is the next
    few metas on the row; their misses ride along after this one. Never
    raises.
    """
    global _focused_imdb
    try:
        _count()
        omdb_key, mdblist_key = _keys()
        if not omdb_key and not mdblist_key:
            return fallback
        if not imdb_id_of(meta) and isinstance(meta, dict) and meta.get('id'):
            _count('missing', str(meta['id']))
            meta = _facts_with_imdb(meta)
        imdb_id = imdb_id_of(meta)
        if not imdb_id:
            return fallback
        _focused_imdb = imdb_id
        stype = _type_of(meta)
        cache = _get_cache(omdb_key, mdblist_key)
        scores = cache.lookup(imdb_id)
        later = [other for other in _misses(cache, upcoming) if other[0] != imdb_id]
        if scores is None:
            _schedule(imdb_id, omdb_key, mdblist_key, cache, later[:PREFETCH], stype=stype)
            return fallback
        _count('hits', imdb_id)
        if later:
            _schedule(later[0][0], omdb_key, mdblist_key, cache, later[1:PREFETCH], stype=later[0][1])
        if not scores:
            return fallback
        from lib.ui.home import _hero_meta_text
        from lib.ui.uicommon import escape_label

        return escape_label(_hero_meta_text(merge_scores(meta, scores)))
    except Exception:
        return fallback


def _schedule(imdb_id, omdb_key, mdblist_key, cache, prefetch=(), stype=''):
    """Replace the plan with `imdb_id` then `prefetch`. Latest wins.

    `stype` is `imdb_id`'s Stremio type, and each `prefetch` entry is an
    `(imdb_id, type)` pair (a bare id is taken as type unknown): MDBList
    looks movies and shows up on different paths. Nothing is fetched
    until the focus has rested `_DWELL_SECONDS` on this plan. One worker
    serves every window; it is started here when it is not already
    running.
    """
    items = []
    for entry in [(imdb_id, stype)] + list(prefetch or ()):
        wanted, wanted_type = entry if isinstance(entry, tuple) else (entry, '')
        if wanted and wanted not in [item[0] for item in items]:
            items.append((wanted, omdb_key, mdblist_key, cache, wanted_type or ''))
    if not items:
        return
    with _lock:
        _stop.clear()
        _plan['items'] = items
        _plan['focus'] = _focused_imdb or imdb_id
        _plan['at'] = time.monotonic()
        generation = _generation
        start = not _plan['running']
        if start:
            _plan['running'] = True
    _wake.set()
    if start:
        thread = threading.Thread(target=_loop, name='rivulet-ratings', daemon=True)
        thread._ratings_generation = generation
        thread.start()


def stop(log_session=True):
    """Discard Home's plan; old workers cannot rejoin a later session."""
    global _generation, _focused_imdb
    summary = None
    with _lock:
        _generation += 1
        _stop.set()
        _plan['items'] = []
        _plan['running'] = False
        _focused_imdb = ''
        _inflight.clear()
        _wake.set()
        if log_session and _session['active']:
            summary = tuple(_session[key] for key in ('complete', 'partial', 'missing', 'hits'))
            _session['active'] = False
    if summary is not None:
        _info('ratings: session fetched %d complete, %d partial, %d missing-imdb-id, %d cache hits' % summary)


def reset():
    """Refresh retires cache objects and every old worker's generation."""
    global _cache, _generation, _focused_imdb
    with _lock:
        _generation += 1
        close = getattr(_cache, 'close', None)
        if close is not None:
            close()
            # Settings may have deleted the file in another interpreter
            # while Home's old writer was finishing. Remove that copy too.
            path = getattr(_cache, 'path', None)
            if path:
                try:
                    os.remove(path)
                except OSError:
                    pass
        _cache = None
        _meta_ids.clear()
        _provider_until.clear()
        _provider_state.clear()
        _prefetch_until.clear()
        _failed_until.clear()
        _inflight.clear()
        _plan['items'] = []
        _focused_imdb = ''
    stop(log_session=False)
    with _lock:
        _stop.clear()


def _next_due(generation=None):
    """The plan's head once its dwell has passed: `(item, None)`, or
    `(None, seconds)` to wait, or `(None, None)` when the plan is empty
    (the worker then stops)."""
    from lib.ui.background import monitor

    with _lock:
        if generation is not None and generation != _generation:
            return None, None
        if _stop.is_set() or monitor().abortRequested() or not _plan['items']:
            _plan['running'] = False
            return None, None
        left = _plan['at'] + _DWELL_SECONDS - time.monotonic()
        if left > 0:
            return None, left
        item = _plan['items'].pop(0)
        imdb_id, omdb_key, mdblist_key, _cache_obj, _stype = item
        fail_id = _fail_id(imdb_id, omdb_key, mdblist_key)
        if imdb_id in _inflight or _failed_until.get(fail_id, 0) > time.time():
            return (), None
        _inflight.add(imdb_id)
        return item + (fail_id,), None


def _loop():
    """The one worker. Waits out the dwell, then fetches in plan order."""
    generation = getattr(threading.current_thread(), '_ratings_generation', _generation)
    _context.generation = generation
    while True:
        item, wait = _next_due(generation)
        if item is None and wait is None:
            return
        if item is None:
            _wake.clear()
            _wake.wait(min(wait, 1.0))
            continue
        if not item:
            continue
        imdb_id, omdb_key, mdblist_key, cache, stype, fail_id = item
        try:
            cached = cache.lookup(imdb_id) is not None
        except Exception:
            cached = False
        if cached:
            with _lock:
                if generation == _generation:
                    _inflight.discard(imdb_id)
            continue
        _worker(imdb_id, omdb_key, mdblist_key, cache, fail_id, stype)


def _cap(cache):
    """Oldest failures go first; remote scrolling is unbounded."""
    while len(cache) > _MAX_CACHE_ENTRIES:
        del cache[next(iter(cache))]


def _provider_key(provider, key):
    return '%s\n%s' % (provider, _token(key))


def _safe_category(category):
    # Only known categories and exception class names enter diagnostics.
    text = str(category)
    if text.startswith('http ') and text[5:].isdigit():
        return 'status-' + text[5:]
    if text in ('ok', 'quota', 'rejected', 'json', 'not found', 'unavailable', 'cooldown', 'prefetch', 'Timeout'):
        return text
    if re.fullmatch(r'[A-Za-z]{1,60}(?:Error|Timeout|Exception)', text):
        return text
    return 'error'


def _info(message):
    try:
        import xbmc

        from lib.ui.compat import log
        log(message, xbmc.LOGINFO)
    except Exception:
        pass


def _provider_result(provider, key, category, empty, hints, generation):
    token = _provider_key(provider, key)
    category = _safe_category(category)
    state = 'ok' if empty or category == 'ok' else category
    cooldown = 0
    remaining = hints.get('remaining')
    if category in ('quota', 'rejected'):
        cooldown = _PROVIDER_COOLDOWN_SECONDS
        if category == 'quota':
            cooldown = min(_MAX_QUOTA_COOLDOWN_SECONDS,
                           max(cooldown, hints.get('retry_after') or 0))
    with _lock:
        if generation != _generation or _stop.is_set():
            return
        now = time.monotonic()
        entering = bool(cooldown) and _provider_until.get(token, 0) <= now
        if cooldown:
            _provider_until[token] = now + cooldown
            _cap(_provider_until)
        elif state == 'ok':
            _provider_until.pop(token, None)
        if remaining is not None and remaining <= 25:
            _prefetch_until[token] = now + (hints.get('reset_after') or _PROVIDER_COOLDOWN_SECONDS)
            _cap(_prefetch_until)
        elif remaining is not None:
            _prefetch_until.pop(token, None)
        changed = entering or _provider_state.get(token) != state
        _provider_state[token] = state
        _cap(_provider_state)
    if changed:
        label = state if state in ('ok', 'quota', 'rejected') else 'unavailable:' + state
        _info('ratings: %s %s - cooldown %d s (remaining %s)' % (
            provider, label, cooldown, int(remaining) if remaining is not None else '?'))


def _record_providers(providers, rejected, keys, generation):
    for provider in rejected - set(providers):
        _provider_result(provider, keys[provider], 'rejected', False, {}, generation)
    for provider, (category, empty, hints) in providers.items():
        _provider_result(provider, keys[provider], category, empty, hints, generation)


def _worker_keys(keys, imdb_id):
    available, paused = {}, []
    with _lock:
        prefetch = bool(_plan.get('focus')) and _plan['focus'] != imdb_id
        for provider, key in keys.items():
            token = _provider_key(provider, key)
            quota_prefetch = prefetch and _prefetch_until.get(token, 0) > time.monotonic()
            cooling = _provider_until.get(token, 0) > time.monotonic()
            available[provider] = '' if cooling or quota_prefetch else key
            if key and not available[provider]:
                paused.append((provider, 'prefetch' if quota_prefetch else _provider_state.get(token, 'cooldown')))
    return available, paused


def _store_result(cache, imdb_id, scores, paused, generation):
    partial = bool(paused) or getattr(scores, 'partial', False)
    if partial and not scores:
        raise RatingsError('provider cooldown')
    with _lock:
        if generation != _generation or _stop.is_set():
            return
    # The write runs outside `_lock`: the GUI thread takes it on every
    # paint, and the cache's own lock already stops a retired cache
    # (Refresh) from being written to.
    if partial:
        cache.store(imdb_id, scores, ttl=_PARTIAL_TTL_SECONDS)
    else:
        cache.store(imdb_id, scores)
    _count('partial' if partial else 'complete', generation=generation)
    _log_fetch(imdb_id, partial)
    if partial:
        _partial_log(imdb_id, getattr(scores, 'providers', {}), paused, _PARTIAL_TTL_SECONDS)


def _partial_log(imdb_id, providers, paused, ttl):
    failures = [(p, _safe_category(c)) for p, (c, empty, _h) in providers.items()
                if c != 'ok' and not empty]
    reason = ', '.join('%s %s' % pair for pair in failures + paused) or 'unavailable'
    _info('ratings: %s partial (%s) - cached %d s' % (imdb_id, reason, ttl))


def _get_for_generation(generation):
    """Keep HTTP timeouts, but do not start another provider after close."""
    def get(url, params=None, timeout=None):
        with _lock:
            if generation != _generation or _stop.is_set():
                raise RatingsError('stopped')
        import requests  # type: ignore[import-untyped]
        return requests.get(url, params=params, timeout=timeout)
    return get


def _worker(imdb_id, omdb_key, mdblist_key, cache, fail_id, stype=''):
    from lib.stremio.ratings import fetch_scores

    generation = getattr(_context, 'generation', _generation)
    with _lock:
        if generation != _generation or _stop.is_set():
            return
    keys = {'omdb': omdb_key, 'mdblist': mdblist_key}
    available, paused = _worker_keys(keys, imdb_id)
    if not any(available.values()):
        with _lock:
            if generation == _generation:
                _inflight.discard(imdb_id)
        return
    rejected = set()
    failed = False
    try:
        scores = fetch_scores(imdb_id, available['omdb'], available['mdblist'], stype=stype,
                              on_rejected=rejected.add, get=_get_for_generation(generation))
        _record_providers(getattr(scores, 'providers', {}), rejected, keys, generation)
        _store_result(cache, imdb_id, scores, paused, generation)
    except RatingsError as exc:
        _record_providers(exc.providers, rejected, keys, generation)
        failed = not exc.empty
        if exc.empty:
            try:
                _store_result(cache, imdb_id, {}, paused, generation)
            except Exception:
                failed = True
        elif generation == _generation and not _stop.is_set():
            _count('partial', generation=generation)
            _partial_log(imdb_id, exc.providers, paused, 0)
        _log_failure(imdb_id, exc)
    except Exception as exc:
        failed = True
        _log_failure(imdb_id, exc)
    with _lock:
        if generation != _generation or _stop.is_set():
            return
        if failed:
            _failed_until[fail_id] = time.time() + _FAIL_SECONDS
            _cap(_failed_until)
        _inflight.discard(imdb_id)
    try:
        import xbmc
        xbmc.executebuiltin('Action(noop)')
    except Exception:
        pass


def _log_fetch(imdb_id, partial):
    try:
        import xbmc

        from lib.ui.compat import log
        log('ratings: fetched %s %s' % (imdb_id, 'partial' if partial else 'complete'), xbmc.LOGDEBUG)
    except Exception:
        pass


def _log_failure(imdb_id, exc):
    """The class name only. `str(exc)` on a requests error is the URL."""
    try:
        import xbmc

        from lib.ui.compat import log

        log('ratings: fetch failed for %s: %s' % (imdb_id, type(exc).__name__), xbmc.LOGDEBUG)
    except Exception:
        pass
