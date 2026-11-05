# SPDX-FileCopyrightText: 2026 M0Rf30
# SPDX-License-Identifier: MIT

"""First catalog page through the slim stale-while-revalidate cache.

The window's own in-memory pages still win for the session
(``HomeWindow._catalog_pages``, a hub row's ``posters``). This module
is the disk layer underneath the first fetch: the next launch, and a
hub opened after Home has warmed, skips the GET while the page is fresh.

Home can fill a filtered anime view from later pages; those raw pages
share a bounded session LRU below. Stock first-page callers are unchanged.
"""

import contextlib
import os
import threading
import time
from collections import OrderedDict

from lib.stremio.addons import AddonError
from lib.stremio.pagecache import cache_key

_CATALOG_PAGES_FILENAME = 'catalog_pages.json'
_caches = {}  # type: dict
_caches_lock = threading.Lock()
_revalidate_inflight = {}  # type: dict
_revalidate_lock = threading.Lock()
_revalidate_queue = OrderedDict()  # type: OrderedDict
_revalidate_failed_until = {}  # type: dict
_revalidate_worker = None
_revalidate_stop = threading.Event()
_revalidate_wake = threading.Event()
_revalidate_generation = 0
_MAX_REVALIDATE_QUEUE = 32
_REVALIDATE_BACKOFF_SECONDS = 300

#: `deferred_saves()` nesting depth, and whether a store happened inside.
#: Not thread-local on purpose: Home and the hubs fetch a first screen on
#: several workers inside one `deferred_saves()` block.
_defer = {'depth': 0, 'dirty': False}
_defer_lock = threading.Lock()


@contextlib.contextmanager
def deferred_saves():
    """Inside, pages are stored as they arrive and the file is written
    once, on the way out. Home used to rewrite catalog_pages.json once
    per row it opened (#13)."""
    with _defer_lock:
        _defer['depth'] += 1
    try:
        yield
    finally:
        with _defer_lock:
            _defer['depth'] -= 1
            flush = _defer['depth'] == 0 and _defer['dirty']
            if flush:
                _defer['dirty'] = False
        if flush:
            cache = get_page_cache()
            if cache is not None:
                try:
                    cache.save()
                except OSError as exc:
                    _log_save_error(exc)


def _save(cache):
    """Write now, or once at the end of the enclosing `deferred_saves()`."""
    with _defer_lock:
        if _defer['depth']:
            _defer['dirty'] = True
            return
    cache.save()


def _log_save_error(exc):
    from lib.stremio.addons import addon_error_detail
    from lib.ui.compat import log

    log('catalogpages: cache save failed: %s' % addon_error_detail(exc))


def _store_page(cache, key, metas):
    """A loaded row survives a read-only/full cache directory."""
    try:
        cache.store(key, metas)
        _save(cache)
    except OSError as exc:
        _log_save_error(exc)


def get_page_cache():
    """The process cache for the current profile, or None when the store
    has no data directory (fakes, and any caller that cannot persist).
    """
    from lib.ui.dependencies import get_store

    store = get_store()
    data_dir = getattr(store, 'data_dir', None)
    if not data_dir:
        return None
    path = os.path.join(data_dir, _CATALOG_PAGES_FILENAME)
    with _caches_lock:
        cache = _caches.get(path)
        if cache is None:
            from lib.stremio.pagecache import PageCache

            cache = PageCache(path)
            _caches[path] = cache
        return cache


def _hide_adult():
    from lib.ui.views import _adult_filtering_enabled

    return _adult_filtering_enabled()


def _blocked(manifest, catalog):
    """True for an adult catalog while the setting hides them. Such a
    catalog is neither requested nor cached: an empty page stored now
    would keep it empty after the setting is turned off."""
    if not _hide_adult():
        return False
    from lib.stremio.contentrating import is_adult_catalog

    return bool(is_adult_catalog(catalog if catalog is not None else {}, manifest))


def _visible(metas, manifest, catalog):
    """`metas` as the CURRENT hide-adult setting allows them: none for an
    adult catalog, and adult items dropped from any other. Pages are
    stored unfiltered, so this runs on every read, cached or fresh, and
    turning the setting on hides a page cached while it was off."""
    if metas is None or not _hide_adult():
        return metas
    from lib.stremio.contentrating import filter_metas, is_adult_catalog

    if is_adult_catalog(catalog if catalog is not None else {}, manifest):
        return []
    return filter_metas(metas)


def _fetch_first_page(transport, manifest, catalog, extra):
    """Read one raw page. Only _visible applies the current adult setting."""
    from lib.ui.views import _dedupe_page, _fetch_catalog

    if _blocked(manifest, catalog):
        return []
    ctype = catalog.get('type') if isinstance(catalog, dict) else None
    cid = catalog.get('id') if isinstance(catalog, dict) else None
    page = _fetch_catalog(transport, ctype, cid, extra=list(extra or ()) or None)
    metas = [meta for meta in (page or []) if isinstance(meta, dict)]
    return _dedupe_page(metas, set(), False)[1]


def _revalidate(transport, manifest, catalog, extra, key, generation=None):
    try:
        metas = _fetch_first_page(transport, manifest, catalog, extra)
    except Exception:
        with _revalidate_lock:
            if generation is None or generation == _revalidate_generation:
                _revalidate_failed_until[key] = time.monotonic() + _REVALIDATE_BACKOFF_SECONDS
                while len(_revalidate_failed_until) > 2000:
                    del _revalidate_failed_until[next(iter(_revalidate_failed_until))]
        return
    if _blocked(manifest, catalog):
        return
    with _revalidate_lock:
        if generation is not None and (generation != _revalidate_generation or _revalidate_stop.is_set()):
            return
        cache = get_page_cache()
        if cache is not None:
            _store_page(cache, key, metas)
        _revalidate_failed_until.pop(key, None)


def _revalidate_loop():
    global _revalidate_worker
    from lib.ui.background import monitor

    try:
        while True:
            with _revalidate_lock:
                if _revalidate_stop.is_set() or monitor().abortRequested():
                    _revalidate_worker = None
                    return
                if _revalidate_queue:
                    key, args = _revalidate_queue.popitem(last=True)
                    generation = _revalidate_generation
                else:
                    key = None
                    _revalidate_wake.clear()
            if key is None:
                _revalidate_wake.wait(1.0)
                continue
            try:
                _revalidate(*args, generation)
            finally:
                with _revalidate_lock:
                    if _revalidate_inflight.get(key) == generation:
                        _revalidate_inflight.pop(key, None)
    finally:
        with _revalidate_lock:
            if _revalidate_worker is threading.current_thread():
                _revalidate_worker = None


def _schedule_revalidate(transport, manifest, catalog, extra, key):
    global _revalidate_worker
    with _revalidate_lock:
        if key in _revalidate_inflight or time.monotonic() < _revalidate_failed_until.get(key, 0):
            return
        _revalidate_stop.clear()
        _revalidate_inflight[key] = _revalidate_generation
        _revalidate_queue[key] = (transport, manifest, catalog, extra, key)
        while len(_revalidate_queue) > _MAX_REVALIDATE_QUEUE:
            old, _args = _revalidate_queue.popitem(last=False)
            _revalidate_inflight.pop(old, None)
        if _revalidate_worker is None:
            _revalidate_worker = threading.Thread(target=_revalidate_loop,
                                                  name='rivulet-page-revalidate', daemon=True)
            _revalidate_worker.start()
        _revalidate_wake.set()


def stop():
    """Close Home or refresh: drop queued work and reject late HTTP results."""
    global _revalidate_generation
    with _revalidate_lock:
        _revalidate_generation += 1
        _revalidate_stop.set()
        _revalidate_queue.clear()
        _revalidate_inflight.clear()
        _revalidate_wake.set()


def _stamped(extra, manifest):
    """`extra` plus the manifest's playlist stamp, for the cache KEY only.

    The engine's IPTV catalogs are numbered by position (`iptv0`) at one
    transport, so changing the playlist in Settings leaves (transport,
    type, id) untouched and the disk cache (fresh 15 min, servable 24 h)
    would keep answering with the old channels. `lib.iptv` stamps the
    published manifest with a hash of the playlists; folding it into the
    key makes the new playlist a miss. A manifest without a stamp (every
    other add-on) leaves the key, and so its cached pages, as it was. The
    pair never reaches the add-on: the fetch uses the original `extra`.
    """
    from lib.iptv import manifest_stamp

    stamp = manifest_stamp(manifest)
    return list(extra or ()) + [('_stamp', stamp)] if stamp else extra


def cached_first_page(transport, manifest, catalog, extra=None):
    """The first page, from the cache when it is fresh or still servable.

    A stale hit is returned immediately and revalidated on a daemon. So
    is an expired one up to a day old (#13): the first open of an evening
    paints the rows it had instead of fetching every one cold, and the
    revalidation has the next open current. A miss fetches, stores, and
    writes the file once (once per `deferred_saves()` block).

    The page is stored as the addon sent it; the hide-adult setting is
    applied to what is returned (`_visible`), so it always reflects the
    setting as it is now. An adult catalog the setting hides is answered
    empty before the cache is read or written, so turning the setting
    off shows it again at once.
    """
    if _blocked(manifest, catalog):
        return []
    cache = get_page_cache()
    key = None
    if cache is not None and isinstance(catalog, dict):
        key = cache_key(transport, catalog.get('type'), catalog.get('id'), _stamped(extra, manifest))
        state, metas = cache.lookup(key, allow_expired=True)
        if metas:
            # Pages cached before types were defaulted on the way in.
            from lib.stremio.addons import default_meta_types

            metas = default_meta_types(metas, catalog.get('type'))
        if state == 'fresh' and metas is not None:
            return _visible(metas, manifest, catalog)
        if state in ('stale', 'expired') and metas:
            _schedule_revalidate(transport, manifest, catalog, extra, key)
            return _visible(metas, manifest, catalog)
    metas = _fetch_first_page(transport, manifest, catalog, extra)
    if cache is not None and key is not None:
        _store_page(cache, key, metas)
    return _visible(metas, manifest, catalog)


# Later pages live only for this interpreter. A bounded LRU shares a plain
# anime catalog's raw pages between its two filtered Home views.
_later_pages = OrderedDict()  # type: OrderedDict
_later_failures = OrderedDict()  # type: OrderedDict
_later_lock = threading.Lock()
_MAX_LATER_PAGES = 200


def clear_later_pages():
    with _revalidate_lock:
        _revalidate_failed_until.clear()
    with _later_lock:
        _later_pages.clear()
        _later_failures.clear()


def _later_key(transport, ctype, cid, extra, stamp=''):
    pairs = list(extra or ())
    return (transport, ctype, cid, tuple((k, v) for k, v in pairs if k != 'skip'),
            next((v for k, v in pairs if k == 'skip'), 0), stamp)


def later_page_failure(transport, ctype, cid, extra=None, cooldown=30.0, stamp=''):
    """`stamp`: the manifest's `lib.iptv.manifest_stamp()`, see `_stamped`."""
    with _later_lock:
        failed = _later_failures.get(_later_key(transport, ctype, cid, extra, stamp))
        return dict(failed) if failed and time.monotonic() - failed['at'] < cooldown else None


class LaterPageFailure(AddonError):
    """A session retry deadline, distinct from a catalog's real end."""

    def __init__(self, failed, remembered=False):
        super().__init__('later page temporarily unavailable', category=failed['category'])
        self.failed = {'at': failed['at'], 'count': failed['count']}
        self.remembered = remembered


def _remember_later_failure(key, exc):
    category = str(getattr(exc, 'category', '') or '')
    status = category[5:] if category.startswith('HTTP ') else ''
    retryable = ((status.isdigit() and int(status) >= 500)
                 or any(word in category.lower() for word in ('timeout', 'connection')))
    if not retryable:
        return None
    with _later_lock:
        failed = {'at': time.monotonic(), 'count': _later_failures.get(key, {}).get('count', 0) + 1,
                  'category': category}
        _later_failures[key] = failed
        _later_failures.move_to_end(key)
        while len(_later_failures) > _MAX_LATER_PAGES:
            _later_failures.popitem(last=False)
        return failed


def cached_later_page(transport, ctype, cid, extra=None, stamp=''):
    """A later page, shared for this interpreter. `stamp` keeps a
    republished catalog of the same id apart (see `_stamped`)."""
    from lib.ui.views import _fetch_catalog

    key = _later_key(transport, ctype, cid, extra, stamp)
    failed = later_page_failure(transport, ctype, cid, extra, stamp=stamp)
    if failed:
        raise LaterPageFailure(failed, remembered=True)
    with _later_lock:
        if key in _later_pages:
            _later_pages.move_to_end(key)
            return list(_later_pages[key])
    try:
        page = _fetch_catalog(transport, ctype, cid, extra=extra)
    except AddonError as exc:
        failed = _remember_later_failure(key, exc)
        if failed:
            raise LaterPageFailure(failed) from exc
        raise
    with _later_lock:
        _later_failures.pop(key, None)
        _later_pages[key] = list(page)
        _later_pages.move_to_end(key)
        while len(_later_pages) > _MAX_LATER_PAGES:
            _later_pages.popitem(last=False)
    return page
