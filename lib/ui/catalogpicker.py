"""CatalogPickerWindow: a vertical list of every installed addon's
catalogs - Rivulet's custom replacement for the classical `discover()`
directory. Picking a row opens the coverflow (`lib.ui.infowindow`) over
that catalog's items; picking a TITLE from the coverflow opens
`lib.ui.detailwindow` for it.
"""
import xbmcgui

from lib.stremio.addons import AddonError
from lib.ui.uicommon import BaseWindow, busy_dialog, open_window

LIST = 30002


class CatalogPickerWindow(BaseWindow):
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
        catalog_type = catalog.get('type')
        item = xbmcgui.ListItem(label=catalog_name, label2='%s \u00b7 %s' % (addon_name, catalog_type))
        item.setProperty('position', str(index))
        return item

    def onInit(self):
        items = [
            self._make_item(index, manifest, catalog)
            for index, (_transport_url, manifest, catalog) in enumerate(self.catalogs)
        ]
        self.getControl(LIST).addItems(items)
        self.setFocusId(LIST)

    def onClick(self, control_id):
        if control_id != LIST:
            return
        focused = self.getControl(LIST).getSelectedItem()
        if focused is None:
            return
        transport_url, _manifest, catalog = self.catalogs[int(focused.getProperty('position'))]
        self._open_catalog(transport_url, catalog)

    def _open_catalog(self, transport_url, catalog):
        from lib.ui.compat import L, log
        from lib.ui.views import _fetch_catalog

        ctype = catalog.get('type')
        try:
            with busy_dialog(L(30033)):
                metas = _fetch_catalog(transport_url, ctype, catalog.get('id'))
        except AddonError as exc:
            log('catalogpicker: %s failed: %r' % (transport_url, exc))
            return
        if not metas:
            return

        import xbmc

        from lib.ui.compat import notify

        log('catalogpicker: opening coverflow (%d results)' % len(metas), xbmc.LOGINFO)
        try:
            from lib.ui.infowindow import open_showcase
            selected = open_showcase(metas)
        except Exception as exc:  # a skin/UI failure must surface, not vanish
            log('catalogpicker: coverflow failed to open: %r' % (exc,), xbmc.LOGERROR)
            notify(L(30032))
            return
        if not selected:
            return

        from lib.ui.detailwindow import open_detail
        if open_detail(selected.get('type') or ctype, selected.get('id')):
            self.should_close_caller = True
            self.close()


def open_catalog_picker():
    """List every installed addon's catalogs and open the coverflow for
    the one picked. Returns True if the caller should also close (see
    the module docstring)."""
    import xbmc

    from lib.store import Store
    from lib.stremio.addons import iter_catalogs
    from lib.ui.compat import L, addon_profile_dir, log, notify

    catalogs = list(iter_catalogs(Store(addon_profile_dir()).get_addons()))
    if not catalogs:
        notify(L(30030))
        return False

    log('catalogpicker: opening CatalogPickerWindow (%d catalogs)' % len(catalogs), xbmc.LOGINFO)
    win = None
    try:
        win = open_window(CatalogPickerWindow, 'CatalogPickerWindow.xml')
        return win.start(catalogs)
    except Exception as exc:  # a skin/UI failure must surface, not vanish
        log('catalogpicker: window failed to open: %r' % (exc,), xbmc.LOGERROR)
        notify(L(30032))
        return False
    finally:
        # A normal return means CatalogPickerWindow already closed itself
        # (its own onAction/onClick calls self.close()) before .start()
        # returned - but an exception raised from WITHIN .start() (onInit(),
        # or a callback mid-doModal()) skips that self-close entirely.
        # Close unconditionally here so no exit path leaves a zombie modal
        # window behind; closing an already-closed window is a safe no-op.
        if win is not None:
            try:
                win.close()
            except Exception:
                pass
