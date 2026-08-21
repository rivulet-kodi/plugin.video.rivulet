"""MarketWindow: browse and install addons Rivulet's OWN installed addons
publish through the `addon_catalog` protocol resource, instead of
requiring a manifest URL typed with a remote control (`AddonsWindow`'s
"Install addon from URL" row, still the only path for an addon that
publishes no catalog of its own). One flat list, aggregated across every
installed addon that declares `manifest['addonCatalogs']` (see
`lib.stremio.addoncatalogs` - Cinemeta ships one seeded by default, but
nothing here is Cinemeta-specific). Built/run via `open_market()`.

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
"""
import xbmcgui

from lib.ui.dependencies import get_client, get_store
from lib.ui.uicommon import BaseWindow, busy_dialog, open_window

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


class MarketWindow(BaseWindow):
    """See module docstring. Built/run via `open_market()`."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.store = None
        self.entries = []
        self.states = []

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

        control = self.getControl(LIST)
        control.reset()
        control.addItems(self._build_items())
        self.setFocusId(LIST)

    def _fetch_entries(self, installed):
        """Best-effort aggregate of every `addon_catalog` resource
        declared by an installed addon: one GET per DECLARING addon, not
        per listed entry - deliberately no per-entry reachability probe.
        A real community catalog runs ~100 entries; probing every one's
        transportUrl before showing the list would stall a Raspberry Pi
        for minutes, and a dead one simply falls through the app's own
        existing `AddonError` paths the first time it is actually used,
        exactly like any other addon that goes offline after install.

        One source addon being unreachable never hides catalogs from the
        others - mirrors `lib.ui.views._refresh_addon_manifests()`'s
        per-addon isolation. Entries are de-duplicated by transportUrl,
        keeping the first one seen, so an addon listed by two different
        catalog sources shows once."""
        import xbmc

        from lib.stremio.addoncatalogs import fetch_addon_catalog, iter_addon_catalogs
        from lib.stremio.addons import AddonError, addon_error_detail, safe_url_for_log
        from lib.ui.compat import L, log, notify

        seen = {}
        for transport_url, manifest, addon_catalog in iter_addon_catalogs(installed):
            try:
                fetched = fetch_addon_catalog(
                    get_client(), transport_url, addon_catalog.get('type'), addon_catalog.get('id'),
                )
            except AddonError as exc:
                log('marketwindow: addon_catalog fetch failed for %s: %s' % (
                    safe_url_for_log(transport_url or ''), addon_error_detail(exc),
                ), xbmc.LOGWARNING)
                notify(L(30340) % manifest.get('name', '?'))
                continue
            for entry in fetched:
                url = entry.get('transportUrl')
                if url and url not in seen:
                    seen[url] = entry
        return list(seen.values())

    def _build_items(self):
        from lib.ui.addonswindow import _clean_description
        from lib.ui.compat import L

        if not self.entries:
            item = xbmcgui.ListItem(label=L(30339))
            item.setProperty('position', '')
            return [item]

        items = []
        for index, descriptor in enumerate(self.entries):
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

    def onClick(self, control_id):
        if control_id != LIST:
            return
        focused = self.getControl(LIST).getSelectedItem()
        if focused is None:
            return
        position = focused.getProperty('position')
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
            self._install_from_market(descriptor)

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
            log('marketwindow: concurrent update: %s' % exc, xbmc.LOGWARNING)
            notify(L(30032))
            self._reload()
            return False
        return True

    def _install_from_market(self, descriptor):
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
            log('marketwindow: invalid transport url %s: %s' % (safe_url_for_log(raw_url or ''), exc), xbmc.LOGERROR)
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
            log('marketwindow: invalid pasted url %s: %s' % (safe_url_for_log(pasted_url), exc), xbmc.LOGERROR)
            notify(L(30014))
            return

        try:
            configured_manifest = get_client().manifest(pasted_transport_url)
        except AddonError as exc:
            log('marketwindow: manifest fetch failed for %s: %s' % (
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


def open_market():
    """Browse and install addons from every installed addon's own
    `addon_catalog`. Mirrors `lib.ui.addonswindow.open_addons()`'s own
    open/doModal/close-once-on-exception shape exactly - see that
    function's docstring for why the `finally: win.close()` is
    unconditional."""
    import xbmc

    from lib.ui.compat import L, log, notify

    log('marketwindow: opening MarketWindow', xbmc.LOGINFO)
    win = None
    try:
        win = open_window(MarketWindow, 'MarketWindow.xml')
        win.doModal()
    except Exception as exc:  # a skin/UI failure must surface, not vanish
        log('marketwindow: window failed to open: %r' % (exc,), xbmc.LOGERROR)
        notify(L(30032))
    finally:
        if win is not None:
            try:
                win.close()
            except Exception:
                pass
