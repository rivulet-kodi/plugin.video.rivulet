"""CatalogPickerWindow: a vertical list of every installed addon's
catalogs - Rivulet's custom replacement for the classical `discover()`
directory. Picking a row opens the coverflow (`lib.ui.infowindow`) over
that catalog's items.

Picking a TITLE from the coverflow has no custom detail screen yet, so
it falls back to the classical `meta` directory
(`lib.ui.uicommon.fallback_to_classical`) and signals its own caller to
close too, so the classical directory - which lands on Kodi's Home
container, behind our modal stack - actually becomes visible. This is a
temporary bridge: once a `DetailWindow` exists, `_open_catalog` should
call it directly instead of falling back.
"""
import xbmcgui

from lib.stremio.addons import AddonError
from lib.ui.uicommon import BACK_ACTIONS, fallback_to_classical, open_window

LIST = 30002


class CatalogPickerWindow(xbmcgui.WindowXMLDialog):
    """See module docstring. Built/run via `open_catalog_picker()`."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.catalogs = []
        self.should_close_caller = False

    def start(self, catalogs):
        """doModal() with `catalogs` (a list of `(transport_url, manifest,
        catalog)` tuples, as `lib.stremio.addons.iter_catalogs` yields)
        loaded as the picker's rows. Returns True if a classical-fallback
        navigation happened and the caller should also close."""
        self.catalogs = list(catalogs or [])
        self.should_close_caller = False
        if not self.catalogs:
            return False
        self.doModal()
        return self.should_close_caller

    def _make_item(self, index, manifest, catalog):
        addon_name = manifest.get('name', '?')
        catalog_name = catalog.get('name') or catalog.get('id')
        label = '%s: %s (%s)' % (addon_name, catalog_name, catalog.get('type'))
        item = xbmcgui.ListItem(label)
        item.setProperty('position', str(index))
        return item

    def onInit(self):
        items = [
            self._make_item(index, manifest, catalog)
            for index, (_transport_url, manifest, catalog) in enumerate(self.catalogs)
        ]
        self.getControl(LIST).addItems(items)
        self.setFocusId(LIST)

    def onAction(self, action):
        if action.getId() in BACK_ACTIONS:
            self.close()

    def onClick(self, control_id):
        if control_id != LIST:
            return
        focused = self.getControl(LIST).getSelectedItem()
        if focused is None:
            return
        transport_url, _manifest, catalog = self.catalogs[int(focused.getProperty('position'))]
        self._open_catalog(transport_url, catalog)

    def _open_catalog(self, transport_url, catalog):
        from lib.ui.compat import log
        from lib.ui.views import _fetch_catalog

        ctype = catalog.get('type')
        try:
            metas = _fetch_catalog(transport_url, ctype, catalog.get('id'))
        except AddonError as exc:
            log('catalogpicker: %s failed: %r' % (transport_url, exc))
            return
        if not metas:
            return

        from lib.ui.infowindow import open_showcase
        selected = open_showcase(metas)
        if not selected:
            return

        fallback_to_classical('meta', type=selected.get('type') or ctype, id=selected.get('id'))
        self.should_close_caller = True
        self.close()


def open_catalog_picker():
    """List every installed addon's catalogs and open the coverflow for
    the one picked. Returns True if the caller should also close (see
    the module docstring)."""
    from lib.store import Store
    from lib.stremio.addons import iter_catalogs
    from lib.ui.compat import L, addon_profile_dir, notify

    catalogs = list(iter_catalogs(Store(addon_profile_dir()).get_addons()))
    if not catalogs:
        notify(L(30030))
        return False

    win = open_window(CatalogPickerWindow, 'CatalogPickerWindow.xml')
    return win.start(catalogs)
