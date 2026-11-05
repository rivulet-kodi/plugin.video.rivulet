# SPDX-FileCopyrightText: 2026 M0Rf30
# SPDX-License-Identifier: MIT

"""Shared data helpers and RunPlugin script actions for plugin.video.rivulet.

The addon's real UI is the custom WindowXML dialog stack (HomeWindow,
SearchWindow, infowindow.ShowcaseWindow, DetailWindow,
StreamsWindow, AddonsWindow, AddonCatalogWindow) - this module no
longer builds any of their directory listings. What remains here is:

- Shared data-fetch/sync helpers (`_fetch_meta`, `_fetch_catalog`,
  `_sync_addons_if_logged_in`, `_refresh_addon_manifests`) those custom
  windows import lazily, so the addon-fetch/caching/sync logic lives in
  one place instead of being duplicated per window.
- The RunPlugin one-shot script actions wired directly into
  resources/settings.xml (`login`, `logout`, `sync_addons_now`,
  `open_settings`) - side effects, not listings, so they finish with
  _finish_action() instead of xbmcplugin.endOfDirectory() on their own.
- `home()`, the minimal recovery directory default.py falls back to when
  constructing or opening HomeWindow fails.
"""
import queue
import threading
import time
from functools import wraps

import xbmc
import xbmcgui
import xbmcplugin

from lib.stremio import addons as addons_lib
from lib.stremio.addons import (
    AddonError,
    addon_error_detail,
    safe_url_for_log,
)
from lib.stremio.api import ApiError, StremioAPI
from lib.stremio.contentrating import filter_metas, is_adult_catalog
from lib.ui import addonsync, compat, dialogs, router, urlutil
from lib.ui.compat import L, log, notify
from lib.ui.dependencies import get_client, get_store
from lib.ui.uicommon import CANCELLED, run_cancellable

#: Cap on concurrent addon HTTP calls per fan-out (`_fetch_meta()` and
#: `_refresh_addon_manifests()`) - bounded so a user with dozens of
#: installed addons doesn't spawn dozens of threads at once. Each
#: AddonClient call still carries its own 15s timeout; this only lets
#: that timeout run concurrently across addons instead of serializing N
#: of them one after another.
_MAX_ADDON_WORKERS = 8

#: Ceiling on `skip` pages `iter_catalog_pages()` will walk for one
#: catalog. Pages are fetched serially (each `skip` depends on the
#: previous page's size), so this bounds both the wait behind the busy
#: dialog and how much a pathological catalog can pour into the
#: coverflow. At the 100 metas Cinemeta serves per page that is 2000
#: titles; at the 20 an addon like AIOLists serves, 400 - past what a
#: coverflow is browsable at either way.
_MAX_CATALOG_PAGES = 20


def _url_for(action, **params):
    """Local convenience wrapper binding urlutil.url_for() to the router's
    current BASE_URL, so call sites below don't repeat it."""
    return urlutil.url_for(router.BASE_URL, action, **params)


#: Soft deadline (seconds) for `_map_addons()`'s fan-out: on a low-power
#: ARM box (see serverbin.py's device notes) one dead/slow addon must
#: never blank the whole Home screen while every other addon already
#: answered - so results are rendered as soon as this deadline passes,
#: with `None` standing in for whichever addons are still in flight.
#: Abandoned stragglers keep running to completion or their own 15s
#: AddonClient timeout in a background daemon thread nobody joins - see
#: `_fetch_meta()`'s docstring for why raw daemon Threads are used here
#: instead of `concurrent.futures.ThreadPoolExecutor` (its atexit join
#: hook blocks interpreter shutdown on an in-flight worker regardless of
#: the daemon flag; Kodi itself still lingers up to 5 s for any thread).
_SOFT_DEADLINE_S = 4.0


def _fan_out(fn, items, max_workers, client=None):
    """Call `fn(item)` once per item, fanned out over up to `max_workers`
    raw daemon `threading.Thread` workers pulling from a `queue.Queue` -
    NOT `concurrent.futures.ThreadPoolExecutor` (see `_SOFT_DEADLINE_S`'s
    docstring for why). Returns a `queue.Queue` of `(index, result)`
    pairs, one per item, in completion order (not input order) - callers
    read from it directly so they can apply their own winner/deadline
    semantics on top of the same daemon-thread fan-out.

    `client`, when given, has its per-thread requests session closed as
    each worker exits (`AddonClient._release_worker_session`). Callers
    that do not pass it still get a private session per worker via
    `AddonClient._active_session()`; that session is closed when the
    thread drops it.
    """
    work = queue.Queue()
    for index, item in enumerate(items):
        work.put((index, item))
    results = queue.Queue()

    def _worker():
        try:
            while True:
                try:
                    index, item = work.get_nowait()
                except queue.Empty:
                    return
                results.put((index, fn(item)))
        finally:
            release = getattr(client, '_release_worker_session', None)
            if callable(release):
                release()

    for _ in range(min(len(items), max_workers)):
        threading.Thread(target=_worker, daemon=True).start()
    return results


def _map_addons(fn, items, client=None):
    """Call `fn(item)` once per item in `items`, fanned out across a small
    bounded thread pool, and return the results in the same order as
    `items` - a drop-in replacement for `[fn(item) for item in items]`
    that keeps N addons' worth of blocking HTTP calls (each with its own
    15s timeout) from serializing behind each other. `fn` is expected to
    catch its own `AddonError` (log it, return a falsy sentinel) so one
    addon's failure can never abort the others - that per-addon
    try/except still runs, just inside whichever worker thread executes
    it.

    Collection is bounded by `_SOFT_DEADLINE_S`, not "wait for every
    item": Home's render used to call this (via `_refresh_addon_manifests`
    and friends) and block on `ThreadPoolExecutor.map()`, which waits for
    the slowest addon before yielding even the first result - one addon
    stuck anywhere inside its own 15s `AddonClient` timeout blanked the
    entire Home screen for up to 15s even though every other addon had
    already answered. Past the deadline, whichever addons have not yet
    answered are abandoned (left running in their own daemon thread, not
    joined) and their slot in the returned list is `None` - every caller
    of `_map_addons()` must treat a `None` entry as "this addon didn't
    answer in time", exactly like it already treats a `None` from `fn`
    itself failing outright.
    """
    if not items:
        return []
    if len(items) == 1:
        return [fn(items[0])]
    results = _fan_out(fn, items, _MAX_ADDON_WORKERS, client=client)
    out = [None] * len(items)
    remaining = len(items)
    deadline = time.monotonic() + _SOFT_DEADLINE_S
    while remaining:
        budget = deadline - time.monotonic()
        if budget <= 0:
            break
        try:
            index, value = results.get(timeout=budget)
        except queue.Empty:
            break
        out[index] = value
        remaining -= 1
    return out


def _safe_listing(view):
    """Guard a directory-listing view: on any uncaught error, notify and
    end the directory as failed instead of leaving Kodi hanging."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        try:
            return view(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - last-resort guard for a Kodi directory
            log('views.%s failed: %r' % (view.__name__, exc), xbmc.LOGERROR)
            notify(str(exc) or view.__name__)
            xbmcplugin.endOfDirectory(router.ADDON_HANDLE, succeeded=False)
    return wrapper


def _finish_action(handle, refresh=True):
    """End a RunPlugin-style script action (login/logout/sync_addons_now)."""
    xbmcplugin.endOfDirectory(handle, succeeded=True, updateListing=False, cacheToDisc=False)
    if refresh:
        xbmc.executebuiltin('Container.Refresh')


def _action_item(label, url, icon=None):
    """A RunPlugin-style action row (home()'s Settings row): gets art like
    any other row but keeps isFolder=False, so Kodi runs it in place
    instead of pushing it onto the navigation stack."""
    li = xbmcgui.ListItem(label=label)
    art = {'fanart': compat.addon_fanart()}
    if icon:
        art.update({'icon': icon, 'thumb': icon})
    li.setArt(art)
    return (url, li, False)


def _usable_meta(meta):
    """A meta the detail screen can open: a dict with a non-empty string id.

    `None`, `{}`, and a dict that has no id used to count as a hit -
    `_fetch_meta` returned on the first non-None, and anything truthy was
    cached for `metacache.TTL_SECONDS`. An addon that answered HTTP 200
    with an empty object then beat a slower addon that had the title, or
    poisoned the cache for half an hour. An id is what every later screen
    keys off; without one there is nothing to open.
    """
    if not isinstance(meta, dict):
        return False
    meta_id = meta.get('id')
    return isinstance(meta_id, str) and bool(meta_id.strip())


#: First try plus two silent retries of a retryable `AddonError`
#: (`_meta_error_is_retryable()`). A usable meta
#: returns immediately; a successful body with no id is not retried
#: (the addon answered, it just had nothing we can open).
_META_ATTEMPTS = 3
_META_RETRY_BACKOFF_SECONDS = (0.4, 0.8)

#: A retry only follows a failure that came back within this many
#: seconds of the addon's first request. A slow failure (a connection
#: dropped after a long wait) is not asked again, so one addon costs at
#: most about this budget, one backoff and one `AddonClient` timeout.
_META_RETRY_WITHIN_SECONDS = 2.0


def _meta_error_is_retryable(exc):
    """Whether a failed meta request is worth asking again: a dropped
    connection, HTTP 408/429/5xx, or an addon's own error placeholder (a
    throttled aggregator). A timeout is not: the addon already had the
    whole `AddonClient` timeout (15 s), and two more would keep the user
    and a fan-out slot waiting about 46 s. A definite answer (any other
    HTTP 4xx, invalid JSON, a response too large) would only fail the
    same way, and each retry delays the type and IMDb fallbacks."""
    category = str(getattr(exc, 'category', '') or '')
    if category in ('invalid JSON', 'response too large'):
        return False
    if 'timeout' in category.lower():
        # ReadTimeout, ConnectTimeout, Timeout, TimeoutError, socket's
        # `timeout`: the category is the exception's type name.
        return False
    if category.startswith('HTTP '):
        status = category[5:]
        if not status.isdigit():
            return True
        code = int(status)
        return code in (408, 429) or code >= 500
    return True


def _pause_before_meta_retry(seconds):
    """Wait `seconds` before retrying a failed meta request. Returns True
    when Kodi is aborting and the worker should stop."""
    if seconds <= 0:
        return False
    return xbmc.Monitor().waitForAbort(seconds)


def _addon_name(descriptor):
    """An addon's manifest name for the log (never its transport URL,
    which can carry a user's token)."""
    manifest = descriptor.get('manifest') if isinstance(descriptor, dict) else None
    return str((manifest or {}).get('name') or (manifest or {}).get('id') or '?')


def _meta_summary(meta):
    """``id=..., videos=N`` for the log, or why `meta` is not usable."""
    if meta is None:
        return 'no meta'
    if not _usable_meta(meta):
        return 'a meta without an id'
    videos = meta.get('videos')
    return 'id=%s, type=%s, videos=%d' % (
        meta.get('id'), meta.get('type'), len(videos) if isinstance(videos, list) else 0)


def _fetch_meta(stype, sid, store=True, on_miss=None):  # noqa: C901 - legacy; ratchet
    """Aggregate meta across every installed addon supporting it for
    (stype, sid); Stremio addons commonly disagree on coverage, so the
    first addon to return a usable object wins.

    Every eligible addon is queried concurrently rather than one at a
    time - each is a blocking HTTP call with its own 15s timeout, so a
    sequential loop over N addons could stall the UI for up to N x 15s.
    We return the instant a usable result is ready instead of waiting
    for every addon to answer.

    Preference order: the old sequential loop always returned the first
    addon (in store.get_enabled_addons() order) with a usable result, since it
    never even called later addons once one hit. With real concurrency
    every eligible addon is called up front, so that exact guarantee is
    no longer possible in general - but we still prefer the earliest
    addon among whichever have *already* answered by the time we check
    (a cheap, non-blocking snapshot), so if the winning addon is at
    least as fast as the others, the result is identical to before. If
    the earliest-preference addon happens to be the slow one, a faster
    later addon wins instead of blocking on it - strict list-order
    preference is sacrificed on purpose in that case, since waiting on
    the slowest addon ahead of one that already answered is exactly the
    freeze this function exists to avoid. Addons still in flight when we
    return are abandoned, not joined - they keep running to completion or
    their own 15s timeout in a background daemon thread we no longer
    wait on.

    Fan-out uses raw `threading.Thread(daemon=True)` workers pulling from
    a `queue.Queue`, NOT `concurrent.futures.ThreadPoolExecutor`:
    `concurrent.futures.thread` registers an atexit hook that JOINS every
    worker at interpreter shutdown regardless of its daemon flag.
    Measured directly (see lib/ui/streamswindow.py's
    `_start_stream_fetch_workers` docstring for the same finding in the
    streams fan-out): a single addon still inside its own 15s timeout
    blocked plugin-process exit for a full 6.0s, because
    `pool.shutdown(wait=False)` only stops the pool itself from waiting,
    not the interpreter's own atexit join. A raw daemon thread has no
    such hook: CPython does not wait for it at exit. Kodi's own
    CPythonInvoker still waits for every interpreter thread, daemon or
    not, up to PYTHON_SCRIPT_TIMEOUT (5 s) and then injects SystemExit,
    so a straggler can linger at most that long.

    A short-TTL disk cache (`lib.ui.metacache`) sits in front of the
    fan-out: DetailWindow, infowindow's enrichment, and any other custom
    window that re-opens the same title within one browsing session
    would otherwise re-fetch this exact same object from every addon
    each time. `store.data_dir` doubles as the cache's on/off switch: it
    is only set on the real `Store` (test fakes omit it), so tests never
    touch the filesystem.

    `store=False` lets a caller take over the on-disk write itself - e.g.
    `continuewatching.open_continue_watching()` fans this out over up to
    `_MAX_ITEMS` candidates and batches all of their results into one
    `metacache.store_cached_metas()` call instead of triggering one
    full-file cache rewrite per candidate (see that module's docstring).
    The cache READ above is unaffected by `store` - a cache hit is always
    served regardless of whether this call's own miss would be stored.

    `on_miss`, if given, is invoked with no arguments exactly when this
    call bypasses the cache above and reaches for a fresh addon answer -
    never on a cache hit. `continuewatching.open_continue_watching()`
    uses it to tell a genuine cache-miss fetch apart from a value merely
    served from disk, so its batched `metacache.store_cached_metas()`
    call only re-stamps entries that were actually just fetched:
    restamping `ts` for every warm candidate on every reopen would
    perpetually re-arm each entry's TTL and defeat `metacache.TTL_SECONDS`
    from ever actually expiring one (Finding 7's adversarial-review fix).
    """
    data_store = get_store()
    client = get_client()

    cache_dir = getattr(data_store, 'data_dir', None)
    if cache_dir is not None:
        from lib.ui.metacache import load_cached_meta
        cached = load_cached_meta(cache_dir, stype, sid)
        if _usable_meta(cached):
            log('views._fetch_meta: %s/%s from the meta cache (%s)' % (stype, sid, _meta_summary(cached)),
                xbmc.LOGINFO)
            return cached

    if on_miss is not None:
        on_miss()

    targets = [
        descriptor for descriptor in data_store.get_enabled_addons()
        if addons_lib.addon_supports(descriptor.get('manifest') or {}, 'meta', stype, sid)
    ]
    log('views._fetch_meta: asking %d addon(s) for %s/%s: %s' % (
        len(targets), stype, sid, ', '.join(_addon_name(d) for d in targets) or '-'), xbmc.LOGINFO)
    if not targets:
        return None

    def _fetch_one(descriptor):
        transport_url = descriptor.get('transportUrl')
        started = time.monotonic()
        for attempt in range(_META_ATTEMPTS):
            try:
                meta = client.meta(transport_url, stype, sid)
            except AddonError as exc:
                log('views._fetch_meta: %s failed: %s' % (safe_url_for_log(transport_url), addon_error_detail(exc)), xbmc.LOGWARNING)
                if (
                    not _meta_error_is_retryable(exc)
                    or attempt + 1 >= _META_ATTEMPTS
                    or time.monotonic() - started > _META_RETRY_WITHIN_SECONDS
                    or _pause_before_meta_retry(_META_RETRY_BACKOFF_SECONDS[attempt])
                ):
                    return None
                continue
            except Exception as exc:  # noqa: BLE001 - a dead worker would hang the result queue
                log('views._fetch_meta: %s raised %s' % (safe_url_for_log(transport_url), type(exc).__name__), xbmc.LOGWARNING)
                return None
            log('views._fetch_meta: %s answered %s/%s with %s' % (
                _addon_name(descriptor), stype, sid, _meta_summary(meta)), xbmc.LOGINFO)
            # A body that arrived is the addon's answer. Retrying `{}`
            # would only delay the next addon, and it must not win.
            return meta if _usable_meta(meta) else None
        return None

    if len(targets) == 1:
        result = _fetch_one(targets[0])
    else:
        work = queue.Queue()
        for index, descriptor in enumerate(targets):
            work.put((index, descriptor))
        results = queue.Queue()

        def _worker():
            try:
                while True:
                    try:
                        index, descriptor = work.get_nowait()
                    except queue.Empty:
                        return
                    results.put((index, _fetch_one(descriptor)))
            finally:
                release = getattr(client, '_release_worker_session', None)
                if callable(release):
                    release()

        for _ in range(min(len(targets), _MAX_ADDON_WORKERS)):
            threading.Thread(target=_worker, daemon=True).start()

        finished = {}
        result = None
        while len(finished) < len(targets):
            index, one = results.get()
            finished[index] = one
            if _usable_meta(one):
                # Only consults results already in hand - never blocks on a
                # still-running addon - then picks the earliest-index winner
                # among those, preserving list-order preference whenever the
                # preferred addon is at least as fast as the others. An
                # id-less body is not a winner: the loop keeps waiting for
                # an addon that actually has this title.
                winning_index = min(i for i, r in finished.items() if _usable_meta(r))
                result = finished[winning_index]
                break

    log('views._fetch_meta: %s/%s resolved to %s' % (stype, sid, _meta_summary(result)), xbmc.LOGINFO)
    if store and cache_dir is not None and _usable_meta(result):
        from lib.ui.metacache import store_cached_meta
        store_cached_meta(cache_dir, stype, sid, result)
    return result


_HOME_META_RETRY_CONTEXT = threading.local()
_STOCK_META_RETRY_PAUSE = _pause_before_meta_retry
_STOCK_META_CLIENT = get_client


class _HomeMetaClient:
    """Observe definitive replies without changing upstream's lookup text.

    The proxy is local to one lookup and is captured by its fan-out
    workers. A timeout from any provider prevents a negative session
    entry; a later Home load must be able to try that provider again.
    """

    def __init__(self, client):
        self.client = client
        self.answered = False
        self.failed = False
        self.lock = threading.Lock()

    def __getattr__(self, name):
        return getattr(self.client, name)

    def meta(self, *args, **kwargs):
        try:
            result = self.client.meta(*args, **kwargs)
        except Exception as exc:
            with self.lock:
                if isinstance(exc, AddonError) and exc.category == 'HTTP 404':
                    self.answered = True
                else:
                    self.failed = True
            raise
        with self.lock:
            self.answered = True
        return result


def _meta_client_with_home_context():
    client = getattr(_HOME_META_RETRY_CONTEXT, 'client', None)
    return client if client is not None else _STOCK_META_CLIENT()


get_client = _meta_client_with_home_context


def _fetch_meta_for_home(stype, sid, should_stop, stop_event=None, on_not_found=None, **kw):
    """Home owns the result, while the upstream lookup stays unchanged.

    Already-running HTTP/fan-out finishes under upstream's limits. Home's
    next focus request is never started after close, and a late result is
    never handed back to its window. Retry pauses on this caller's thread
    observe Home close without changing concurrent stock callers' pauses.
    """
    if should_stop():
        return None
    previous = getattr(_HOME_META_RETRY_CONTEXT, 'should_stop', None)
    previous_event = getattr(_HOME_META_RETRY_CONTEXT, 'stop_event', None)
    previous_client = getattr(_HOME_META_RETRY_CONTEXT, 'client', None)
    client = _HomeMetaClient(get_client()) if on_not_found is not None else previous_client
    _HOME_META_RETRY_CONTEXT.should_stop = should_stop
    _HOME_META_RETRY_CONTEXT.stop_event = stop_event
    _HOME_META_RETRY_CONTEXT.client = client
    try:
        result = _fetch_meta(stype, sid, **kw)
    finally:
        _HOME_META_RETRY_CONTEXT.should_stop = previous
        _HOME_META_RETRY_CONTEXT.stop_event = previous_event
        _HOME_META_RETRY_CONTEXT.client = previous_client
    if should_stop():
        return None
    if result is None and on_not_found is not None and client.answered and not client.failed:
        on_not_found()
    return result


def _pause_home_meta_retry(seconds, stopped, stop_event=None):
    """One interruptible backoff; the existing maximum pause is below 1 s."""
    from lib.ui.background import monitor

    if stopped() or monitor().abortRequested():
        return True
    return bool((stop_event or threading.Event()).wait(seconds) or stopped() or monitor().abortRequested())


def _pause_meta_retry_with_home_context(seconds):
    stopped = getattr(_HOME_META_RETRY_CONTEXT, 'should_stop', None)
    if stopped is None:
        return _STOCK_META_RETRY_PAUSE(seconds)
    return _pause_home_meta_retry(seconds, stopped, getattr(_HOME_META_RETRY_CONTEXT, 'stop_event', None))


# Keep the upstream function text intact. Only the caller thread that
# entered Home's wrapper gets its cancellable pause; upstream fan-out and
# stock callers keep their normal bounded backoff. No per-call global swap.
_pause_before_meta_retry = _pause_meta_retry_with_home_context


def _fetch_catalog(transport, ctype, cid, extra=None):
    """Fetch one catalog's metas via the shared AddonClient - imported
    lazily by infowindow's discover-link picker to build its overlay. Raises AddonError on failure; each
    caller decides how to surface it."""
    client = get_client()
    return client.catalog(transport, ctype, cid, extra=extra)


def _adult_filtering_enabled():
    """Whether resources/settings.xml's home_hide_adult toggle (default
    True - the only defensible default for a living-room client, whose
    audience skews younger than the household member who configures
    addons) should suppress adult-flagged catalogs and metas. Read at
    call time, never cached, so a mid-session settings change takes
    effect on the very next fetch."""
    return compat.setting_bool('home_hide_adult', True)


def iter_catalog_pages(transport, ctype, cid, extra=None, catalog=None, manifest=None):
    """Yield a catalog's pages, following the protocol's `skip` paging
    until the addon runs out of items.

    `_fetch_catalog()` returns exactly one page, which is all the addon
    chooses to serve per request - Cinemeta serves 100 metas, but an
    addon is free to serve fewer, and AIOLists serves 20. Browsing such a
    catalog through a single call therefore showed only its first page
    (a 300-title list opened as 20 titles) with no way to reach the rest,
    since the coverflow has no "next page" affordance to hang further
    requests off.

    A generator rather than one combined list because the pages are
    fetched SERIALLY - each `skip` depends on how much the addon has
    already served - and on a real device one page of AIOLists takes
    several seconds. Collecting all 20 pages before showing anything
    would trade "20 titles now" for "400 titles a minute from now",
    which is a worse screen: the user sees a handful of posters before
    they scroll. The coverflow instead opens on the first page and
    appends the rest as they land (see
    `lib.ui.infowindow.ShowcaseWindow.start`).

    The first page is always yielded, and an `AddonError` fetching it
    propagates - the caller has nothing to show and already surfaces the
    failure. Paging past it is attempted only when `catalog` declares the
    `skip` extra (`catalog_supports_extra`); without that declaration -
    or with no `catalog` to inspect at all, as the discover-link path
    has - the generator stops after that one page, exactly as the
    unpaged behaviour was. `extra` (a list of `(name, value)` pairs, e.g.
    a chosen genre) is preserved on every page, with `skip` appended; a
    caller that already supplies its own `skip` is left alone and fetched
    once.

    Each request's `skip` is the number of metas the addon has actually
    served so far, not a multiple of the first page's length - an addon
    whose pages vary in size then still gets asked for the item straight
    after the last one it gave us.

    Stops at the first page that is empty, shorter than the first page
    (the last page, by the protocol's own convention), contains no meta
    id unseen so far (an addon that ignores `skip` and re-serves page
    one - otherwise an infinite loop), or when `_MAX_CATALOG_PAGES` have
    been fetched. Duplicate ids across pages are dropped, so an addon
    with a shifting window can't feed the coverflow the same title
    twice; a meta with no `id` at all is always kept, since it cannot be
    compared - a cosmetic duplicate beats losing a title the addon
    served. An `AddonError` on a LATER page ends the walk quietly: the
    pages already yielded are a browsable screenful, which beats
    replacing them with an error.

    Consumers may stop early (the coverflow closing mid-walk does), in
    which case no further request is made.

    When resources/settings.xml's home_hide_adult setting is on (the
    default): a whole `catalog` that itself looks adult
    (`lib.stremio.contentrating.is_adult_catalog()`) is never even
    fetched - not one request is made, and the generator yields a
    single empty page instead, which existing callers already treat
    as an ordinary empty result (catalogpicker._fetch_and_show()'s
    "no results" notify), never as a load failure. `manifest`, if
    given, is passed straight through to `is_adult_catalog()` so an
    adult-only addon (`manifest['types']` naming an adult type) is
    caught even when it publishes a neutrally-named catalog - a signal
    unavailable from `catalog` alone, which is why callers that HAVE
    the manifest on hand (catalogpicker._fetch_and_show()) must pass
    it. Otherwise, each page's metas are run through `filter_metas()`
    before being yielded - but the PRE-filter page is still what the
    skip/short-page bookkeeping above counts, so adult content
    vanishing from a page can never desync `skip` from what the addon
    actually served.
    """
    from lib.stremio.addons import catalog_supports_extra

    hide_adult = _adult_filtering_enabled()
    if hide_adult and is_adult_catalog(catalog if catalog is not None else {'type': ctype}, manifest):
        yield []
        return

    base_extra = list(extra or [])
    first = _fetch_catalog(transport, ctype, cid, extra=base_extra or None)

    seen = set()
    _, filtered_first = _dedupe_page(first, seen, hide_adult)
    yield filtered_first

    if not first or not catalog_supports_extra(catalog, 'skip'):
        return
    if any(name == 'skip' for name, _value in base_extra):
        return
    yield from _follow_pages(transport, ctype, cid, base_extra, len(first), len(first), seen, hide_adult)


def iter_catalog_pages_after(transport, ctype, cid, served, seen_ids, extra=None,
                             catalog=None, manifest=None, page_limit=_MAX_CATALOG_PAGES, fetch_page=None,
                             stop_short=True, raise_on_error=False, on_end=None):
    """The pages after a first page the caller already has.

    Home's rows hold page one from the disk cache. Resuming used to
    build `iter_catalog_pages()` and throw its first page away, which
    fetched page one again before page two. `served` is how many metas
    page one held and is where `skip` starts; `seen_ids` are the ids
    already on screen, so a title cannot appear twice. Same stopping
    rules as `iter_catalog_pages()`.
    """
    from lib.stremio.addons import catalog_supports_extra

    hide_adult = _adult_filtering_enabled()
    if hide_adult and is_adult_catalog(catalog if catalog is not None else {'type': ctype}, manifest):
        return
    base_extra = list(extra or [])
    if not served or not catalog_supports_extra(catalog, 'skip'):
        if on_end:
            on_end('no skip' if served else 'empty page')
        return
    if any(name == 'skip' for name, _value in base_extra):
        if on_end:
            on_end('fixed skip')
        return
    seen = set(key for key in (seen_ids or ()) if key is not None)
    yield from _follow_pages(transport, ctype, cid, base_extra, served, served, seen, hide_adult,
                             page_limit, fetch_page, stop_short, raise_on_error, on_end)


def _dedupe_page(page, seen, hide_adult):
    """This page's not-yet-seen metas, in order (`fresh`), and that
    same list with adult-flagged metas additionally dropped when
    `hide_adult` is on (`filtered` - what callers actually see).
    Kept separate so a page that IS new but happens to be entirely
    adult content is not mistaken for the addon re-serving an
    already-seen window (see `_follow_pages`'s `if not fresh` check,
    which must see the PRE-filter freshness). `seen` is updated."""
    fresh = []
    for meta_obj in page:
        key = meta_obj.get('id') if isinstance(meta_obj, dict) else None
        if key is not None:
            if key in seen:
                continue
            seen.add(key)
        fresh.append(meta_obj)
    filtered = filter_metas(fresh) if hide_adult else fresh
    return fresh, filtered


def _follow_pages(transport, ctype, cid, base_extra, served, page_size, seen, hide_adult,
                  page_limit=_MAX_CATALOG_PAGES, fetch_page=None, stop_short=True,
                  raise_on_error=False, on_end=None):
    """Pages two onward. `served` is the metas the addon has actually
    served so far, duplicates and all - NOT the deduped count. This is
    what the next `skip` is counted from, so a page that comes back
    longer or shorter than the first can't make the following request
    skip past (or back over) items the addon never served."""
    page_index = 0
    fetch = fetch_page or _fetch_catalog
    while page_limit is None or page_index + 1 < page_limit:
        page_index += 1
        try:
            page = fetch(
                transport, ctype, cid,
                extra=base_extra + [('skip', served)],
            )
        except AddonError as exc:
            log('views.iter_catalog_pages: %s page %d failed: %s - keeping what landed'
                % (safe_url_for_log(transport), page_index, addon_error_detail(exc)),
                xbmc.LOGWARNING)
            if raise_on_error:
                raise
            return
        if not page:
            if on_end:
                on_end('empty page')
            return
        served += len(page)
        fresh, filtered = _dedupe_page(page, seen, hide_adult)
        # A page with nothing new means the addon is ignoring `skip` and
        # re-serving the same window - stop rather than loop to the cap.
        # Judged on `fresh` (pre-filter): a page that IS new but entirely
        # adult content must keep paging, not look like a stuck addon.
        if not fresh:
            log('views.iter_catalog_pages: %s page %d added nothing new - stopping'
                % (safe_url_for_log(transport), page_index), xbmc.LOGINFO)
            if on_end:
                on_end('added nothing new')
            return
        yield filtered
        if stop_short and len(page) < page_size:
            return

    log('views.iter_catalog_pages: %s hit the %d-page cap'
        % (safe_url_for_log(transport), _MAX_CATALOG_PAGES), xbmc.LOGINFO)
    # Surface the truncation instead of only logging it: the user would
    # otherwise assume the catalog simply ends here.
    message = L(30411)
    try:
        message = message % _MAX_CATALOG_PAGES
    except TypeError:
        pass  # a catalog without the %d placeholder: show it verbatim
    notify(message)


def _sync_addons_if_logged_in(store, notify_success=False, cancellable=False, removed_url=None):
    """Best-effort push of the local addon collection back to Stremio's
    remote sync API when the user is logged in. A failed push is
    notified (not just logged) - previously silent, which made a real
    failure indistinguishable from "nothing to sync"/"working fine".
    Never blocks or fails the local install/remove/login that triggered
    it. Returns True on a successful push (or when there is nothing to
    do because the user isn't logged in and `notify_success` is False),
    False on failure.

    A failure whose `ApiError.is_auth_error` is true (401/403 - the
    authKey itself was invalidated server-side, not a transient blip)
    additionally clears the stored auth via `store.set_auth(None)`, since
    retrying the same dead key can never succeed; the next user-facing
    screen (HomeWindow, AddonsWindow, Settings > Account) then
    correctly shows "not logged in" instead of repeating this failure
    forever.

    `flags.disabled` is stripped from each descriptor before the push -
    it is Rivulet's own local-only presentation bit (see the `flags`
    docstring at the top of lib/store.py), never part of the Stremio
    addon-collection schema. Pushing it as-is would leak a purely local
    "hidden in this install" toggle into the account, so every other
    Stremio client syncing that account would see the addon vanish too.

    `cancellable=True` (the addon windows, which are on screen) runs the
    push under a "Syncing addons…" spinner so Back honours within ~0.2 s
    instead of freezing the screen for the request timeout. A cancelled
    push is neither a success nor a failure: nothing is notified, the
    auth is untouched, and the return value is False - the local change
    already happened and the next sync pushes the whole collection
    anyway (it is a full-collection set, not a delta).

    `removed_url` names an addon just uninstalled locally, so the
    pull-before-push does not union it straight back from the account."""
    from lib.store import ConcurrentUpdateError

    auth = store.get_auth()
    if not auth:
        if notify_success:
            notify(L(30020))
        return False
    try:
        auth_key = auth.get('authKey')

        def push():
            # addonCollectionSet REPLACES the account collection, so a
            # stale local list would delete an addon added from another
            # Stremio client. Pull + union first; a failed pull raises
            # ApiError before anything is pushed.
            api = StremioAPI()
            addonsync.pull_account_addons(store, api, auth_key, removed_url)
            payload = []
            for descriptor in store.get_addons():
                flags = descriptor.get('flags')
                if flags and 'disabled' in flags:
                    descriptor = dict(descriptor)
                    flags = dict(flags)
                    del flags['disabled']
                    descriptor['flags'] = flags
                payload.append(descriptor)
            api.addon_collection_set(auth_key, payload)

        if cancellable:
            # Only the network push runs on the worker; every notify/
            # set_auth below stays on this thread AFTER the outcome is
            # known, so a push the user backed out of can never report
            # success or failure (or clear the auth) behind their back.
            if run_cancellable(push, L(30410)) is CANCELLED:
                log('views._sync_addons_if_logged_in: cancelled by user', xbmc.LOGINFO)
                return False
        else:
            push()
    except ApiError as exc:
        log('views._sync_addons_if_logged_in: %r' % (exc,), xbmc.LOGERROR)
        if exc.is_auth_error:
            # Clear the dead authKey so the next user-facing screen (Library,
            # Addons, Settings > Account) shows "not logged in" instead of
            # retrying the same bad token forever. Reuse the existing
            # generic failure notification below rather than a dedicated
            # re-login prompt: this also runs from background
            # install/remove/login paths, where a "session expired" popup
            # would be out of context.
            store.set_auth(None)
        notify(L(30035))
        return False
    except ConcurrentUpdateError as exc:
        # The pre-push pull could not be persisted: do NOT push a list
        # that may lack remote-only addons.
        log('views._sync_addons_if_logged_in: pull merge failed: %r' % (exc,), xbmc.LOGERROR)
        notify(L(30035))
        return False
    if notify_success:
        notify(L(30034))
    return True


def _refresh_addon_manifests(store, client):
    """Best-effort refresh of every installed addon's cached manifest from
    its own transportUrl, so catalog/resource/logo/version changes the
    remote addon makes after install time eventually reach the local
    cache instead of staying stale forever - previously the only fix was
    to manually remove and reinstall the addon. Mirrors
    `_sync_addons_if_logged_in`'s best-effort philosophy: one addon being
    briefly unreachable (`AddonError`) or returning a manifest too
    malformed to use (no `id`) never aborts refreshing the others and
    never disturbs that addon's last-known-good cached manifest.
    Persisted via `Store.update_addons` (never a raw `get_addons()` +
    `set_addons()` pair) so the write stays safe against a concurrent
    `default.py` process changing addons.json at the same time.
    """
    descriptors = store.get_addons()
    if not descriptors:
        return

    def _fetch(descriptor):
        transport_url = descriptor.get('transportUrl')
        if not transport_url:
            return None
        try:
            return client.manifest(transport_url)
        except AddonError as exc:
            log('views._refresh_addon_manifests: %s failed: %s' % (safe_url_for_log(transport_url), addon_error_detail(exc)), xbmc.LOGWARNING)
            return None

    fetched = _map_addons(_fetch, descriptors, client=client)
    refreshed = {}
    for descriptor, manifest in zip(descriptors, fetched):
        if manifest and manifest.get('id') and manifest != descriptor.get('manifest'):
            refreshed[descriptor.get('transportUrl')] = manifest

    if not refreshed:
        return

    def _apply(addons):
        return [
            dict(addon, manifest=refreshed[addon.get('transportUrl')])
            if addon.get('transportUrl') in refreshed else addon
            for addon in addons
        ]

    from lib.store import ConcurrentUpdateError

    try:
        store.update_addons(_apply)
    except ConcurrentUpdateError as exc:
        log('views._refresh_addon_manifests: update_addons failed: %r' % (exc,), xbmc.LOGWARNING)


# --------------------------------------------------------------------------
# Router actions
# --------------------------------------------------------------------------

@_safe_listing
def home():
    """Recovery directory - NOT Rivulet's home screen any more (that is
    now the custom `HomeWindow`). This is what `default.py` falls back to
    when constructing or opening `HomeWindow` fails. Data/row failures
    are handled within Home and leave the affected row empty.

    It deliberately offers only Settings: every other screen is now a
    custom WindowXML dialog that depends on those same skin resources,
    so retrying any of them here would just fail the exact same way. The
    first row is a plain notice explaining that something went wrong;
    the second is the one recovery action that can actually fix it
    (change the skin, fix a server URL, reinstall, ...).
    """
    handle = router.ADDON_HANDLE
    notice = xbmcgui.ListItem(label=L(30032))
    notice.setArt({'fanart': compat.addon_fanart()})
    items = [
        # The notice is a FOLDER pointing back at this same action, not an
        # _action_item: an isFolder=False row is a playable item to Kodi, so
        # selecting it would have it try to play `?action=home`, which
        # answers with endOfDirectory() and fails playback - a broken row in
        # the one screen that exists to survive breakage. As a folder it
        # reopens the Home window (lib/ui/entry.py intercepts `action=home`), i.e.
        # a harmless retry that lands back here if Home still fails.
        (_url_for('home'), notice, True),
        _action_item(L(30004), _url_for('settings'), compat.addon_media_path('settings.png')),
    ]
    xbmcplugin.addDirectoryItems(handle, items, len(items))
    xbmcplugin.setContent(handle, 'files')
    xbmcplugin.setPluginCategory(handle, compat.ADDON_NAME)
    xbmcplugin.endOfDirectory(handle)


def open_settings():
    compat.ADDON.openSettings()
    xbmcplugin.endOfDirectory(router.ADDON_HANDLE, succeeded=False, updateListing=False, cacheToDisc=False)


def sync_addons_now():
    """RunPlugin action (Settings > Account > Sync addons now): force a
    push of the local addon collection, with explicit feedback either
    way - unlike the automatic post-install/remove/login push, a
    manually-triggered sync must confirm success too, not just surface
    failures. Also refreshes every installed addon's cached manifest
    from its own transportUrl first (see `_refresh_addon_manifests`), so
    a freshly-updated local manifest set - not a stale install-time
    snapshot - is what gets pushed to the account."""
    handle = router.ADDON_HANDLE
    store = get_store()
    _refresh_addon_manifests(store, get_client())
    _sync_addons_if_logged_in(store, notify_success=True)
    _finish_action(handle, refresh=False)


def _typed_login(api):
    """Email and password on the on-screen keyboard. The result, or None."""
    dialog = xbmcgui.Dialog()
    email = dialog.input(L(30024))
    if not email:
        return None
    password = dialog.input(L(30025), option=xbmcgui.ALPHANUM_HIDE_INPUT)
    if not password:
        return None
    try:
        # On a worker under the spinner: a slow server must not freeze the
        # screen with no way to press Back.
        result = run_cancellable(lambda: api.login(email, password), L(30033))
    except ApiError as exc:
        log('views.login failed: %r' % (exc,), xbmc.LOGERROR)
        notify(L(30023))
        return None
    return None if result is CANCELLED else result


def login():
    """The Settings "Sign in" action: `sign_in()`, then end the plugin call.

    Only a RunPlugin call owns a directory handle to end. Home and the
    hubs are windows and call `sign_in()` directly (#22): ending the
    long-spent handle from there logged an error and refreshed whatever
    container sat under the window.
    """
    handle = router.ADDON_HANDLE
    result = sign_in()
    _finish_action(handle, refresh=bool(result))


def sign_in():
    """Sign in: approve a code on a phone, or type email and password.

    The phone way is Stremio's own code sign-in; nothing is typed on the
    TV and no password crosses the network here. The keyboard stays for
    anyone who prefers it (phone remote apps can type into it).

    The auth result once the account is stored, else None. Touches no
    plugin handle.
    """
    api = StremioAPI()
    picked = dialogs.choose(L(30427), [L(30480), L(30481)])
    if picked == 0:
        from lib.ui.pairwindow import link_sign_in

        result = link_sign_in(api=api)
    elif picked == 1:
        result = _typed_login(api)
    else:
        result = None
    if not result:
        return None

    store = get_store()
    store.set_auth(result)

    try:
        remote_addons = run_cancellable(lambda: api.addon_collection_get(result.get('authKey')), L(30033))
    except ApiError as exc:
        log('views.login: addon_collection_get failed: %r' % (exc,), xbmc.LOGERROR)
        remote_addons = None
    if remote_addons is CANCELLED:
        remote_addons = None

    if remote_addons is not None:
        def _merge_with_remote(local_addons):
            # Union, not filter: EVERY local addon (protected or not) must
            # survive login. The previous version kept only protected ones,
            # silently dropping any community addon installed while logged
            # out - the store's local state must never regress on login.
            # Re-run against a freshly-read `local_addons` on every retry
            # (see Store.update_addons), so a concurrent install/remove
            # racing this login is merged rather than clobbered.
            # `union_remote` is the same union every pre-push pull uses,
            # including its discarding of remote descriptors whose
            # `transportUrl` fails validate_transport_url().
            return addonsync.union_remote(local_addons, remote_addons)

        from lib.store import ConcurrentUpdateError

        try:
            store.update_addons(_merge_with_remote)
        except ConcurrentUpdateError as exc:
            log('views.login: addon merge failed: %r' % (exc,), xbmc.LOGERROR)
        else:
            # Push the merged list right back up: closes the gap where an
            # addon installed before ever logging in would otherwise never
            # reach the account until its next unrelated install/remove.
            _sync_addons_if_logged_in(store, cancellable=True)

    user = result.get('user') or {}
    notify(L(30022) % (user.get('email') or user.get('name') or ''))
    return result


def logout():
    handle = router.ADDON_HANDLE
    store = get_store()
    auth = store.get_auth()
    if not auth:
        _finish_action(handle, refresh=False)
        return
    if not dialogs.confirm(L(30021), L(30021), xbmc.getLocalizedString(107), xbmc.getLocalizedString(106)):
        _finish_action(handle, refresh=False)
        return

    try:
        StremioAPI().logout(auth.get('authKey'))
    except ApiError as exc:
        log('views.logout: %r' % (exc,), xbmc.LOGERROR)

    store.set_auth(None)
    _finish_action(handle)
