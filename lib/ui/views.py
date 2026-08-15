"""Shared data helpers and RunPlugin script actions for plugin.video.rivulet.

The addon's real UI is the custom WindowXML dialog stack (HomeWindow,
CatalogPickerWindow, infowindow.ShowcaseWindow, DetailWindow,
StreamsWindow, SearchWindow, AddonsWindow, LibraryWindow) - this module no
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
  opening the custom HomeWindow itself raises.
"""
from functools import wraps

import xbmc
import xbmcgui
import xbmcplugin

from lib.store import ConcurrentUpdateError
from lib.stremio import addons as addons_lib
from lib.stremio.addons import AddonError, safe_url_for_log, validate_transport_url
from lib.stremio.api import ApiError, StremioAPI
from lib.ui import compat, dialogs, router, urlutil
from lib.ui.compat import L, log, notify
from lib.ui.dependencies import get_client, get_store

#: Cap on concurrent addon HTTP calls per fan-out (`_fetch_meta()` and
#: `_refresh_addon_manifests()`) - bounded so a user with dozens of
#: installed addons doesn't spawn dozens of threads at once. Each
#: AddonClient call still carries its own 15s timeout; this only lets
#: that timeout run concurrently across addons instead of serializing N
#: of them one after another.
_MAX_ADDON_WORKERS = 8

#: Ceiling on `skip` pages `fetch_catalog_pages()` will walk for one
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


def _map_addons(fn, items):
    """Call `fn(item)` once per item in `items`, fanned out across a small
    bounded thread pool instead of one call at a time, and return the
    results in the same order as `items` - a drop-in replacement for
    `[fn(item) for item in items]` that keeps N addons' worth of blocking
    HTTP calls (each with its own 15s timeout) from serializing behind
    each other. `fn` is expected to catch its own `AddonError` (log it,
    return a falsy sentinel) so one addon's failure can never abort the
    others - that per-addon try/except still runs, just inside whichever
    worker thread executes it. Used by `_refresh_addon_manifests()`'s
    per-addon manifest fan-out.
    """
    if not items:
        return []
    if len(items) == 1:
        return [fn(items[0])]
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(len(items), _MAX_ADDON_WORKERS)) as pool:
        return list(pool.map(fn, items))


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


def _fetch_meta(stype, sid):
    """Aggregate meta across every installed addon supporting it for
    (stype, sid); Stremio addons commonly disagree on coverage, so the
    first addon to return a usable object wins.

    Every eligible addon is queried concurrently rather than one at a
    time - each is a blocking HTTP call with its own 15s timeout, so a
    sequential loop over N addons could stall the UI for up to N x 15s.
    We return the instant a usable result is ready instead of waiting
    for every addon to answer.

    Preference order: the old sequential loop always returned the first
    addon (in store.get_addons() order) with a usable result, since it
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
    return are abandoned, not cancelled (Future.cancel() only works
    before a thread starts running) - they keep running to completion or
    their own 15s timeout in a background thread we no longer wait on.

    A short-TTL disk cache (`lib.ui.metacache`) sits in front of the
    fan-out: DetailWindow, infowindow's enrichment, and any other custom
    window that re-opens the same title within one browsing session
    would otherwise re-fetch this exact same object from every addon
    each time. `store.data_dir` doubles as the cache's on/off switch: it
    is only set on the real `Store` (test fakes omit it), so tests never
    touch the filesystem.
    """
    store = get_store()
    client = get_client()

    cache_dir = getattr(store, 'data_dir', None)
    if cache_dir is not None:
        from lib.ui.metacache import load_cached_meta
        cached = load_cached_meta(cache_dir, stype, sid)
        if cached is not None:
            return cached

    targets = [
        descriptor for descriptor in store.get_addons()
        if addons_lib.addon_supports(descriptor.get('manifest') or {}, 'meta', stype, sid)
    ]
    if not targets:
        return None

    def _fetch_one(descriptor):
        transport_url = descriptor.get('transportUrl')
        try:
            return client.meta(transport_url, stype, sid)
        except AddonError as exc:
            log('views._fetch_meta: %s failed: %s' % (safe_url_for_log(transport_url), type(exc).__name__), xbmc.LOGWARNING)
            return None

    if len(targets) == 1:
        result = _fetch_one(targets[0])
    else:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        pool = ThreadPoolExecutor(max_workers=min(len(targets), _MAX_ADDON_WORKERS))
        futures = []
        result = None
        try:
            futures = [pool.submit(_fetch_one, descriptor) for descriptor in targets]
            index_of = {future: index for index, future in enumerate(futures)}
            for future in as_completed(futures):
                if future.result() is None:
                    continue
                winner = future
                # Only promotes to a future that has *already* finished (a
                # non-blocking .done() check) - never waits on one still running.
                for other in futures:
                    if (index_of[other] < index_of[winner] and other.done()
                            and other.result() is not None):
                        winner = other
                result = winner.result()
                break
        finally:
            # Drop any addon call that never got a worker thread (only
            # possible when len(targets) > _MAX_ADDON_WORKERS); already-running
            # calls are left to finish in the background. wait=False so this
            # cleanup never blocks the caller on a straggler.
            for future in futures:
                future.cancel()
            pool.shutdown(wait=False)

    if cache_dir is not None and result:
        from lib.ui.metacache import store_cached_meta
        store_cached_meta(cache_dir, stype, sid, result)
    return result


def _fetch_catalog(transport, ctype, cid, extra=None):
    """Fetch one catalog's metas via the shared AddonClient - imported
    lazily by CatalogPickerWindow and infowindow's discover-link picker
    to build their listing/overlay. Raises AddonError on failure; each
    caller decides how to surface it."""
    client = get_client()
    return client.catalog(transport, ctype, cid, extra=extra)


def iter_catalog_pages(transport, ctype, cid, extra=None, catalog=None):
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
    `lib.ui.infowindow.ShowcaseWindow.start`); `fetch_catalog_pages()`
    below is the eager wrapper for callers that do want the whole list.

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
    """
    from lib.stremio.addons import catalog_supports_extra

    base_extra = list(extra or [])
    first = _fetch_catalog(transport, ctype, cid, extra=base_extra or None)

    seen = set()

    def _dedupe(page):
        """This page's not-yet-seen metas, in order."""
        fresh = []
        for meta_obj in page:
            key = meta_obj.get('id') if isinstance(meta_obj, dict) else None
            if key is not None:
                if key in seen:
                    continue
                seen.add(key)
            fresh.append(meta_obj)
        return fresh

    yield _dedupe(first)

    if not first or not catalog_supports_extra(catalog, 'skip'):
        return
    if any(name == 'skip' for name, _value in base_extra):
        return

    page_size = len(first)
    #: Metas the addon has actually served so far, duplicates and all -
    #: NOT the deduped count. This is what the next `skip` is counted
    #: from, so a page that comes back longer or shorter than the first
    #: can't make the following request skip past (or back over) items
    #: the addon never served.
    served = len(first)

    for page_index in range(1, _MAX_CATALOG_PAGES):
        try:
            page = _fetch_catalog(
                transport, ctype, cid,
                extra=base_extra + [('skip', served)],
            )
        except AddonError as exc:
            log('views.iter_catalog_pages: %s page %d failed: %s - keeping what landed'
                % (safe_url_for_log(transport), page_index, type(exc).__name__),
                xbmc.LOGWARNING)
            return
        if not page:
            return
        served += len(page)
        fresh = _dedupe(page)
        # A page with nothing new means the addon is ignoring `skip` and
        # re-serving the same window - stop rather than loop to the cap.
        if not fresh:
            log('views.iter_catalog_pages: %s page %d added nothing new - stopping'
                % (safe_url_for_log(transport), page_index), xbmc.LOGINFO)
            return
        yield fresh
        if len(page) < page_size:
            return

    log('views.iter_catalog_pages: %s hit the %d-page cap'
        % (safe_url_for_log(transport), _MAX_CATALOG_PAGES), xbmc.LOGINFO)


def fetch_catalog_pages(transport, ctype, cid, extra=None, catalog=None):
    """Every page of a catalog as one combined list - the eager form of
    `iter_catalog_pages()`, for callers that cannot show partial results
    and would rather wait (the discover-link path, which passes no
    `catalog` and so never pages anyway).

    The coverflow deliberately does NOT use this: see the generator's
    docstring for why a long catalog must open on its first page rather
    than behind a minute-long spinner.
    """
    metas = []
    for page in iter_catalog_pages(transport, ctype, cid, extra=extra, catalog=catalog):
        metas.extend(page)
    return metas


def _sync_addons_if_logged_in(store, notify_success=False):
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
    screen (LibraryWindow, AddonsWindow, Settings > Account) then
    correctly shows "not logged in" instead of repeating this failure
    forever."""
    auth = store.get_auth()
    if not auth:
        if notify_success:
            notify(L(30020))
        return False
    try:
        StremioAPI().addon_collection_set(auth.get('authKey'), store.get_addons())
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
            log('views._refresh_addon_manifests: %s failed: %s' % (safe_url_for_log(transport_url), type(exc).__name__), xbmc.LOGWARNING)
            return None

    fetched = _map_addons(_fetch, descriptors)
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

    store.update_addons(_apply)


# --------------------------------------------------------------------------
# Router actions
# --------------------------------------------------------------------------

@_safe_listing
def home():
    """Recovery directory - NOT Rivulet's home screen any more (that is
    now the custom `HomeWindow`). This is what `default.py` falls back to
    when opening `HomeWindow` itself raises, e.g. a broken skin missing
    the InfoWindow/DetailWindow XML the custom UI depends on.

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
        # simply redraws this directory, i.e. a harmless retry.
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


def login():
    handle = router.ADDON_HANDLE
    dialog = xbmcgui.Dialog()
    email = dialog.input(L(30024))
    if not email:
        _finish_action(handle, refresh=False)
        return
    password = dialog.input(L(30025), option=xbmcgui.ALPHANUM_HIDE_INPUT)
    if not password:
        _finish_action(handle, refresh=False)
        return

    api = StremioAPI()
    try:
        result = api.login(email, password)
    except ApiError as exc:
        log('views.login failed: %r' % (exc,), xbmc.LOGERROR)
        notify(L(30023))
        _finish_action(handle, refresh=False)
        return

    store = get_store()
    store.set_auth(result)

    try:
        remote_addons = api.addon_collection_get(result.get('authKey'))
    except ApiError as exc:
        log('views.login: addon_collection_get failed: %r' % (exc,), xbmc.LOGERROR)
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
            #
            # Remote descriptors are untrusted (server-side account data,
            # or another client's tampered sync push): a `transportUrl`
            # that fails validate_transport_url() (credentials, plaintext
            # public host, non-HTTP(S), ...) is discarded here rather than
            # persisted/installed - only its safe identity/origin is
            # logged, never the raw descriptor.
            seen = {a.get('transportUrl') for a in local_addons}
            merged = list(local_addons)
            for descriptor in remote_addons:
                transport_url = descriptor.get('transportUrl')
                if not transport_url:
                    continue
                try:
                    normalized_url = validate_transport_url(transport_url)
                except AddonError:
                    manifest_id = (descriptor.get('manifest') or {}).get('id') or '?'
                    log('views.login: discarding unsafe synced addon %s (%s)' % (
                        manifest_id, safe_url_for_log(transport_url)), xbmc.LOGWARNING)
                    continue
                if normalized_url not in seen:
                    merged.append(dict(descriptor, transportUrl=normalized_url))
                    seen.add(normalized_url)
            return merged

        try:
            store.update_addons(_merge_with_remote)
        except ConcurrentUpdateError as exc:
            log('views.login: addon merge failed: %r' % (exc,), xbmc.LOGERROR)
        else:
            # Push the merged list right back up: closes the gap where an
            # addon installed before ever logging in would otherwise never
            # reach the account until its next unrelated install/remove.
            _sync_addons_if_logged_in(store)

    user = result.get('user') or {}
    notify(L(30022) % (user.get('email') or user.get('name') or ''))
    _finish_action(handle)


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
