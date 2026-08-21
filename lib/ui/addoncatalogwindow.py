"""AddonCatalogWindow: browse and install addons Rivulet's OWN installed addons
publish through the `addon_catalog` protocol resource, instead of
requiring a manifest URL typed with a remote control (`AddonsWindow`'s
"Install addon from URL" row, still the only path for an addon that
publishes no catalog of its own). One flat list, aggregated across every
installed addon that declares `manifest['addonCatalogs']` (see
`lib.stremio.addoncatalogs` - Cinemeta ships one seeded by default, but
nothing here is Cinemeta-specific). Built/run via `open_addon_catalog()`.

Each row is badged with `lib.stremio.addoncatalogs.descriptor_state()`:
already-installed rows are informational only (clicking one just says
so - `Store.install_addon()` is deliberately never called for them), an
entry needing configuration opens a paste-URL flow instead of installing
it broken (see `_configure()`), and everything else installs directly
after a confirmation, mirroring `AddonsWindow._install()`'s own
`validate_transport_url()` + manifest-shape checks.

Rendering an addon's `/configure` HTML setup page is impossible here -
Kodi's `WindowXMLDialog` has no embedded browser - so `_configure()`
only ever shows that URL as plain text for the user to open elsewhere,
never fetches or renders it. Nobody should later "fix" this by trying to
draw the page in-app.

Incremental rendering. Cinemeta's own community catalog alone runs ~95
entries, ~79 of them carrying a `logo`. `_render()` therefore builds and
adds only the FIRST `_PAGE_SIZE` rows synchronously - artwork included -
and hands the rest to `_spawn_paging()`, which appends further pages on
a daemon thread, `_PAGE_PACE_SECS` apart, onto the still-open list (see
`_apply_pending_pages()`). `setArt()` is only ever called for a row that
has actually been built into a page - a row nobody has scrolled to yet
never gets Kodi's own async texture loader queued against it. Without
this, `addItems()` handing the list ~79 logo URLs in one call is what
stalled the screen on the ARM/SD-card box this was measured on, before
a single row had even painted.

In-memory search. ~100 rows is unusable one-row-at-a-time on a remote,
so the "Search addon catalogs" row (`_search_row()`) opens a plain
`xbmcgui.Dialog().input()` (mirrors `searchwindow.py`) and filters
`self.entries` - already fully fetched by `_reload()` - by a
case-insensitive substring match against the manifest's name AND
description. This NEVER triggers a new fetch under any circumstance;
see `_visible_indices()`. A "Clear search" row appears once a filter is
active to drop back to the full list.
"""
import threading

import xbmcgui

from lib.ui.dependencies import get_client, get_store
from lib.ui.uicommon import BACK_ACTIONS, BaseWindow, busy_dialog, open_window

LIST = 30360

#: catalog entry state (lib.stremio.addoncatalogs.descriptor_state) ->
#: strings.po id for the suffix appended to its row label. A plain
#: "installable" entry gets no suffix, matching AddonsWindow's own
#: enabled-addon rows.
_STATE_SUFFIX_STRING_IDS = {
    'installed': 30335,
    'update-available': 30336,
    'needs-configuration': 30337,
}

#: Rows built (and, for a logo, `setArt()`-ed) per page. ~102 entries at
#: the community catalog's default size / _PAGE_SIZE == 6 pages: small
#: enough that the first page - the only one built before the list is on
#: screen - never queues more than a fraction of the ~79-logo worst case
#: that stalled the screen before paging existed, large enough that a
#: typical single-source catalog (a handful of entries) still renders in
#: one page, same as before this change.
_PAGE_SIZE = 20

#: Gap between successive page appends once the first page is already on
#: screen. Kodi's own async texture loader starts fetching a row's
#: `setArt()` URL the moment `addItems()` hands it over - this spaces
#: those bursts out over real wall-clock time instead of one Python loop
#: firing every page at once, without blocking the GUI thread the way a
#: sleep inside `onInit()` itself would. Same order of magnitude as
#: `infowindow.py`'s `_ENRICH_SETTLE_SECS`; each pacing site owns its own
#: constant rather than sharing one (see AGENTS.md's fan-out convention).
_PAGE_PACE_SECS = 0.2


def _chunk_pages(indices):
    """Split `indices` into `_PAGE_SIZE`-sized chunks: `(first_page,
    remaining_pages)`. `first_page` is rendered synchronously by
    `_render()`; `remaining_pages` - a list of further chunks, possibly
    empty - is what `_spawn_paging()` walks. `([], [])` for no indices."""
    if not indices:
        return [], []
    pages = [indices[i:i + _PAGE_SIZE] for i in range(0, len(indices), _PAGE_SIZE)]
    return pages[0], pages[1:]


class AddonCatalogWindow(BaseWindow):
    """See module docstring. Built/run via `open_addon_catalog()`."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.store = None
        self.entries = []
        self.states = []
        #: current in-memory search text, '' when unfiltered. Persists
        #: across `_reload()` (e.g. after installing an entry) so acting
        #: on a filtered row does not silently drop the search.
        self.filter_query = ''
        self._reset_paging_state()

    def _reset_paging_state(self):
        #: pages a background walker has queued but the UI thread has
        #: not appended yet. Written under `_paging_lock`, drained by
        #: `_apply_pending_pages()` on the UI thread - a ListItem must
        #: not be touched off the GUI thread, same reason
        #: `infowindow.ShowcaseWindow` keeps this split.
        self._pending_pages = []
        self._paging_lock = threading.Lock()
        #: set by `_stop_paging()`/`close()` to tell a running walker to
        #: stop between pages - polled rather than relied on to already
        #: have stopped, since the worker may be mid-`waitForAbort()`.
        self._paging_stopped = threading.Event()

    def onInit(self):
        self._reload()

    def _reload(self):
        from lib.ui.compat import L

        self.store = get_store()
        installed = self.store.get_addons()
        # Network happens once per DECLARING addon (typically just
        # Cinemeta), not per listed entry - see `_fetch_entries()` - but
        # even one slow/dead source is enough to stall a remote-control
        # screen with no feedback, hence the spinner every other
        # fetch-driven Rivulet screen already opens for this
        # (lib.ui.uicommon.busy_dialog's own docstring).
        with busy_dialog(L(30033)):
            self.entries = self._fetch_entries(installed)

        from lib.stremio.addoncatalogs import descriptor_state

        self.states = [descriptor_state(entry, installed) for entry in self.entries]
        self._render()

    def _fetch_entries(self, installed):
        """Best-effort aggregate of every `addon_catalog` resource
        declared by an installed addon: one GET per declaring addon per
        unique catalog id, not per declared `(type, id)` pair and not per
        listed entry.

        The pair-vs-id distinction is the difference between a usable
        window and a stalled one. Cinemeta alone declares ELEVEN
        `addonCatalogs` pairs, and fetching all of them costs 11 requests
        returning 297 rows for just 102 unique addons - `all/community`
        already contains everything its per-type variants repeat. So this
        goes through `iter_unique_addon_catalogs()` (one source per id,
        preferring the broadest declared type) and `fetch_addon_catalogs()`,
        which fans the remaining sources out concurrently under its own
        bounded pool and serves repeats from a short TTL cache - so
        backing out of this window and reopening it inside one Kodi
        process costs zero requests.

        Deliberately no per-entry reachability probe: a real community
        catalog runs ~100 entries and about 16% of their transportUrls
        are dead, but probing each one before showing the list would
        stall a Raspberry Pi for minutes. A dead entry instead falls
        through the app's existing `AddonError` paths the first time it
        is actually used, exactly like any other addon that goes offline
        after install.

        One source addon being unreachable never hides catalogs from the
        others - `fetch_addon_catalogs()` isolates each source and reports
        the failures, mirroring `lib.ui.views._refresh_addon_manifests()`'s
        per-addon isolation. Entries are de-duplicated by transportUrl,
        keeping the first one seen, so an addon listed by two different
        catalog sources shows once.
        """
        import xbmc

        from lib.stremio.addoncatalogs import (
            fetch_addon_catalogs,
            iter_unique_addon_catalogs,
        )
        from lib.stremio.addons import addon_error_detail, safe_url_for_log
        from lib.ui.compat import L, log, notify

        sources = []
        names = {}
        for transport_url, manifest, addon_catalog in iter_unique_addon_catalogs(installed):
            source = (transport_url, addon_catalog.get('type'), addon_catalog.get('id'))
            sources.append(source)
            names.setdefault(transport_url, manifest.get('name', '?'))

        fetched, failures = fetch_addon_catalogs(get_client(), sources)

        for transport_url, _type, _id, exc in failures:
            log('addoncatalogwindow: addon_catalog fetch failed for %s: %s' % (
                safe_url_for_log(transport_url or ''), addon_error_detail(exc),
            ), xbmc.LOGWARNING)
            notify(L(30340) % names.get(transport_url, '?'))

        seen = {}
        for entry in fetched:
            url = entry.get('transportUrl')
            if url and url not in seen:
                seen[url] = entry
        return list(seen.values())

    def _visible_indices(self):
        """Indices into `self.entries`/`self.states` matching
        `self.filter_query`, case-insensitively against the manifest's
        name AND description. Purely in-memory - `self.entries` is
        exactly what `_reload()` already fetched, so no filter action
        ever triggers a GET. Every index, in order, when unfiltered."""
        if not self.filter_query:
            return list(range(len(self.entries)))
        needle = self.filter_query.lower()
        indices = []
        for index, descriptor in enumerate(self.entries):
            manifest = descriptor.get('manifest') or {}
            haystack = '%s %s' % (manifest.get('name', ''), manifest.get('description', ''))
            if needle in haystack.lower():
                indices.append(index)
        return indices

    def _search_row(self, visible_count):
        """The "Search addon catalogs" row, always first: opens
        `_open_search()` on click. `label2` surfaces the active filter
        text (if any) plus the "Showing %d of %d" (#30348) count, so the
        state of the list is visible without scrolling to the end."""
        from lib.ui.compat import L

        counts = L(30348) % (visible_count, len(self.entries))
        label2 = '"%s"  \u00b7  %s' % (self.filter_query, counts) if self.filter_query else counts
        item = xbmcgui.ListItem(label=L(30346), label2=label2)
        item.setProperty('position', 'search')
        return item

    def _page_items(self, indices):
        """Build ListItems for exactly `indices` into `self.entries`.
        Artwork is set ONLY on the rows built here: a row not yet paged
        in never gets a `setArt()` call, so Kodi's texture loader never
        queues a fetch for it - see the module docstring."""
        from lib.ui.addonswindow import _clean_description
        from lib.ui.compat import L

        items = []
        for index in indices:
            descriptor = self.entries[index]
            manifest = descriptor.get('manifest') or {}
            label = '%s  \u00b7  v%s' % (manifest.get('name', '?'), manifest.get('version', '?'))
            state = self.states[index]
            if state in _STATE_SUFFIX_STRING_IDS:
                label += '  \u00b7  ' + L(_STATE_SUFFIX_STRING_IDS[state])
            item = xbmcgui.ListItem(label=label, label2=_clean_description(manifest.get('description', '')))
            item.setProperty('position', str(index))
            logo = (manifest.get('logo') or '').strip()
            if logo:
                item.setArt({'icon': logo})
            items.append(item)
        return items

    def _render(self):
        """Rebuild the LIST from `self.entries`/`self.states` under the
        current `self.filter_query`. Always in-memory - never a fetch.
        See the module docstring for why only the first page is built
        here, and `_spawn_paging()` for the rest."""
        from lib.ui.compat import L

        self._stop_paging()
        control = self.getControl(LIST)
        control.reset()

        if not self.entries:
            item = xbmcgui.ListItem(label=L(30339))
            item.setProperty('position', '')
            control.addItems([item])
            self.setFocusId(LIST)
            return

        visible = self._visible_indices()
        items = [self._search_row(len(visible))]
        if self.filter_query:
            clear_item = xbmcgui.ListItem(label=L(30347))
            clear_item.setProperty('position', 'clear')
            items.append(clear_item)

        if not visible:
            # A filter that matches nothing: the search/clear rows above
            # stay so the user can change or drop it, but the "no
            # catalogs at all" placeholder (#30339) would misleadingly
            # read as a load failure here - #30349 says what actually
            # happened.
            placeholder = xbmcgui.ListItem(label=L(30349))
            placeholder.setProperty('position', '')
            items.append(placeholder)
            control.addItems(items)
            self.setFocusId(LIST)
            return

        first_page, remaining_pages = _chunk_pages(visible)
        items.extend(self._page_items(first_page))
        control.addItems(items)
        self.setFocusId(LIST)
        if remaining_pages:
            self._spawn_paging(remaining_pages)

    def _stop_paging(self):
        """Stop any walker from a previous `_render()` and clear
        whatever it had queued - a stale page from before a re-filter
        must never land on the new list."""
        self._paging_stopped.set()
        with self._paging_lock:
            self._pending_pages = []
        self._paging_stopped = threading.Event()

    def _spawn_paging(self, pages):
        """Walk `pages` (a list of index chunks past the first) on a
        daemon thread, queueing each for the UI thread - see
        `_reset_paging_state()`."""
        thread = threading.Thread(target=self._paging_worker, args=(pages,), daemon=True)
        thread.start()

    def _paging_worker(self, pages):
        """Append `pages` to `_pending_pages` one at a time,
        `_PAGE_PACE_SECS` apart, waking the UI thread after each so
        `_apply_pending_pages()` drains it - see the module docstring
        for why the pacing lives here rather than in `_render()`."""
        import xbmc

        monitor = xbmc.Monitor()
        try:
            for page in pages:
                if self._paging_stopped.is_set():
                    return
                if monitor.waitForAbort(_PAGE_PACE_SECS):
                    return  # Kodi shutting down mid-walk
                if self._paging_stopped.is_set():
                    return
                if not page:
                    continue
                with self._paging_lock:
                    self._pending_pages.append(list(page))
                xbmc.executebuiltin('Action(noop)')
        except Exception:
            # Daemon thread with nobody to catch for it; a failed walk
            # must never take the window down.
            try:
                from lib.ui.compat import log

                log('addoncatalogwindow: catalog paging worker failed', xbmc.LOGDEBUG)
            except Exception:
                pass

    def _apply_pending_pages(self):
        """Append whatever the paging worker has queued onto the live
        list. Always call this from the UI thread.

        Appending (rather than rebuilding) leaves every already-rendered
        row untouched. `addItems()` resets a ControlList's selection to
        0 though, so the focused position is saved and restored around
        the call - same guard `infowindow.ShowcaseWindow._apply_pending_pages()`
        applies for the same reason."""
        with self._paging_lock:
            if not self._pending_pages:
                return
            pages, self._pending_pages = self._pending_pages, []
        items = []
        for page in pages:
            items.extend(self._page_items(page))
        if not items:
            return
        control = self.getControl(LIST)
        focus_index = control.getSelectedPosition()
        control.addItems(items)
        if focus_index > 0:
            control.selectItem(focus_index)

    def _open_search(self):
        """The "Search addon catalogs" row: prompts for a query the same
        way `searchwindow.py` does, then filters in place - see
        `_visible_indices()`. Backing out (empty input) leaves whatever
        filter was already active untouched."""
        from lib.ui.compat import L

        query = xbmcgui.Dialog().input(L(30346))
        if not query:
            return
        self.filter_query = query
        self._render()

    def _clear_filter(self):
        """The "Clear search" row: drops back to the full list."""
        self.filter_query = ''
        self._render()

    def onAction(self, action):
        # Drain first: a queued page appends rows, and doing that before
        # any back-action handling means a page landing right as the
        # user backs out still lands rather than being dropped.
        self._apply_pending_pages()
        if action.getId() in BACK_ACTIONS:
            self.close()

    def close(self):
        # Any exit path must not leave a paging walker running for a
        # window nobody is looking at.
        self._paging_stopped.set()
        super().close()

    def onClick(self, control_id):
        if control_id != LIST:
            return
        focused = self.getControl(LIST).getSelectedItem()
        if focused is None:
            return
        position = focused.getProperty('position')
        if position == 'search':
            self._open_search()
            return
        if position == 'clear':
            self._clear_filter()
            return
        if not position.isdigit():
            return

        from lib.stremio.addoncatalogs import STATE_INSTALLED, STATE_NEEDS_CONFIGURATION

        index = int(position)
        descriptor = self.entries[index]
        state = self.states[index]
        if state == STATE_NEEDS_CONFIGURATION:
            self._configure(descriptor)
        elif state == STATE_INSTALLED:
            from lib.ui.compat import L, notify

            manifest = descriptor.get('manifest') or {}
            notify(L(30338) % manifest.get('name', '?'))
        else:
            self._install_from_catalog(descriptor)

    def _guard_mutation(self, mutate):
        """Run a store mutation through the CAS `update_addons()` path.
        Identical to `AddonsWindow._guard_mutation()` - duplicated
        rather than shared across the two windows; see that method's
        docstring for the concurrent-`default.py`-process race it
        guards against."""
        import xbmc

        from lib.store import ConcurrentUpdateError
        from lib.ui.compat import L, log, notify

        try:
            mutate()
        except ConcurrentUpdateError as exc:
            log('addoncatalogwindow: concurrent update: %s' % exc, xbmc.LOGWARNING)
            notify(L(30032))
            self._reload()
            return False
        return True

    def _install_from_catalog(self, descriptor):
        """Install (or update) `descriptor` - one `addon_catalog` entry,
        already carrying a full manifest (see the module the entry came
        from, `lib.stremio.addoncatalogs`) - after a confirmation.
        Mirrors `AddonsWindow._install()`'s own validation and error
        handling exactly: `validate_transport_url()` first, a manifest
        `id` sanity check, then the same CAS-guarded
        `Store.install_addon()` + best-effort account sync."""
        import xbmc

        from lib.stremio.addons import AddonError, safe_url_for_log, validate_transport_url
        from lib.ui import dialogs
        from lib.ui.compat import L, log, notify

        manifest = descriptor.get('manifest') or {}
        raw_url = descriptor.get('transportUrl')
        try:
            transport_url = validate_transport_url(raw_url)
        except AddonError as exc:
            log('addoncatalogwindow: invalid transport url %s: %s' % (safe_url_for_log(raw_url or ''), exc), xbmc.LOGERROR)
            notify(L(30014))
            return
        if not manifest.get('id'):
            notify(L(30014))
            return

        if not dialogs.confirm(L(30342), manifest.get('name', '?'), xbmc.getLocalizedString(107), xbmc.getLocalizedString(106)):
            return

        from lib.ui.views import _sync_addons_if_logged_in

        if not self._guard_mutation(lambda: self.store.install_addon(transport_url, manifest)):
            return
        _sync_addons_if_logged_in(self.store)
        notify(L(30012))
        self._reload()

    def _configure(self, descriptor):
        """An addon needing configuration cannot be installed as-is (see
        the module docstring) - show its `/configure` URL and fall back
        to `AddonsWindow`'s own paste-a-manifest-URL flow so the user can
        configure it in a browser elsewhere, then hand the resulting
        (configured) manifest URL back to Rivulet. Rendering that HTML
        page in-app is impossible - `WindowXMLDialog` has no browser -
        so this never attempts to fetch or display it, only its URL."""
        import xbmc

        from lib.stremio.addons import (
            AddonError,
            addon_error_detail,
            safe_url_for_log,
            validate_transport_url,
        )
        from lib.ui.compat import L, log, notify

        manifest = descriptor.get('manifest') or {}
        transport_url = descriptor.get('transportUrl') or ''
        heading = L(30341) % (manifest.get('name', '?'), _configure_url(transport_url))
        pasted_url = xbmcgui.Dialog().input(heading)
        if not pasted_url:
            return

        try:
            pasted_transport_url = validate_transport_url(pasted_url)
        except AddonError as exc:
            log('addoncatalogwindow: invalid pasted url %s: %s' % (safe_url_for_log(pasted_url), exc), xbmc.LOGERROR)
            notify(L(30014))
            return

        try:
            configured_manifest = get_client().manifest(pasted_transport_url)
        except AddonError as exc:
            log('addoncatalogwindow: manifest fetch failed for %s: %s' % (
                safe_url_for_log(pasted_transport_url), addon_error_detail(exc),
            ), xbmc.LOGERROR)
            notify(L(30014))
            return

        if not configured_manifest or not configured_manifest.get('id'):
            notify(L(30014))
            return

        from lib.ui.views import _sync_addons_if_logged_in

        if not self._guard_mutation(lambda: self.store.install_addon(pasted_transport_url, configured_manifest)):
            return
        _sync_addons_if_logged_in(self.store)
        notify(L(30012))
        self._reload()


def _configure_url(transport_url):
    """The addon's `/configure` HTML setup-page URL, derived by swapping
    `manifest.json` for `configure` on its transport URL's *path* - the
    same convention stremio-web itself uses to link a configurable
    addon's setup page. Operating on the parsed path (not the raw
    string) matters because `validate_transport_url()` explicitly
    allows - and preserves - a `?query` component on transport URLs;
    swapping the suffix on the whole string would then land "/configure"
    after the query instead of the path, e.g. turning
    "https://host/manifest.json?token=x" into the unopenable
    "https://host/manifest.json?token=x/configure" instead of
    "https://host/configure?token=x". Only ever shown as plain text (see
    `_configure()`); never fetched or rendered - Kodi has no embedded
    browser."""
    from urllib.parse import urlsplit, urlunsplit

    from lib.stremio.addons import MANIFEST_SUFFIX

    try:
        scheme, netloc, path, query, fragment = urlsplit(transport_url)
    except ValueError:
        # Unparsable (e.g. a malformed IPv6 host) - a third-party addon's
        # transportUrl is untrusted input; fall back to the old
        # whole-string behaviour rather than raising out of a UI callback.
        if transport_url.endswith(MANIFEST_SUFFIX):
            return transport_url[:-len(MANIFEST_SUFFIX)] + '/configure'
        return transport_url.rstrip('/') + '/configure'

    if path.endswith(MANIFEST_SUFFIX):
        path = path[:-len(MANIFEST_SUFFIX)] + '/configure'
    else:
        path = path.rstrip('/') + '/configure'
    return urlunsplit((scheme, netloc, path, query, fragment))


def open_addon_catalog():
    """Browse and install addons from every installed addon's own
    `addon_catalog`. Mirrors `lib.ui.addonswindow.open_addons()`'s own
    open/doModal/close-once-on-exception shape exactly - see that
    function's docstring for why the `finally: win.close()` is
    unconditional."""
    import xbmc

    from lib.ui.compat import L, log, notify

    log('addoncatalogwindow: opening AddonCatalogWindow', xbmc.LOGINFO)
    win = None
    try:
        win = open_window(AddonCatalogWindow, 'AddonCatalogWindow.xml')
        win.doModal()
    except Exception as exc:  # a skin/UI failure must surface, not vanish
        log('addoncatalogwindow: window failed to open: %r' % (exc,), xbmc.LOGERROR)
        notify(L(30032))
    finally:
        if win is not None:
            try:
                win.close()
            except Exception:
                pass
