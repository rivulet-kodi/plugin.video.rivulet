"""AddonsWindow: a vertical list of every installed addon - Rivulet's
add-on manager. Row 0 installs a new addon from a manifest URL; every
other row opens a yes/no removal prompt for that addon, refused for
protected/official addons. Built/run via `open_addons()`.
"""
import xbmcgui

from lib.ui.dependencies import get_client, get_store
from lib.ui.uicommon import BaseWindow, open_window

LIST = 30002

#: strings.po id for the removal refusal shown for protected/official
#: addons.
_PROTECTED_MESSAGE_STRING_ID = 30191


def _clean_description(text):
    """Collapse a manifest description to one line - CR/LF and repeated
    whitespace folded to single spaces - truncated to ~120 chars, for a
    row's `Label2`."""
    text = ' '.join((text or '').split())
    if len(text) > 120:
        text = text[:117].rstrip() + '...'
    return text


class AddonsWindow(BaseWindow):
    """See module docstring. Built/run via `open_addons()`."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.addons = []
        self.store = None

    def onInit(self):
        self._reload()

    def _reload(self):
        self.store = get_store()
        self.addons = self.store.get_addons()

        control = self.getControl(LIST)
        control.reset()
        control.addItems(self._build_items())
        self.setFocusId(LIST)

    def _build_items(self):
        from lib.ui.compat import L

        install_item = xbmcgui.ListItem(
            label=L(30010), label2=L(30192),
        )
        install_item.setProperty('position', 'install')
        items = [install_item]
        for index, descriptor in enumerate(self.addons):
            manifest = descriptor.get('manifest') or {}
            label = '%s  \u00b7  v%s' % (manifest.get('name', '?'), manifest.get('version', '?'))
            item = xbmcgui.ListItem(label=label, label2=_clean_description(manifest.get('description', '')))
            item.setProperty('position', str(index))
            items.append(item)
        return items

    def onClick(self, control_id):
        if control_id != LIST:
            return
        focused = self.getControl(LIST).getSelectedItem()
        if focused is None:
            return
        position = focused.getProperty('position')
        if position == 'install':
            self._install()
            return
        self._remove(self.addons[int(position)])

    def _install(self):
        import xbmc

        from lib.stremio.addons import (
            AddonError,
            safe_url_for_log,
            validate_transport_url,
        )
        from lib.ui.compat import L, log, notify

        url = xbmcgui.Dialog().input(L(30010))
        if not url:
            return

        try:
            transport_url = validate_transport_url(url)
        except AddonError as exc:
            log('addonswindow: invalid transport url %s: %s' % (safe_url_for_log(url), exc), xbmc.LOGERROR)
            notify(L(30014))
            return

        try:
            manifest = get_client().manifest(transport_url)
        except AddonError as exc:
            log('addonswindow: manifest fetch failed for %s: %s' % (safe_url_for_log(transport_url), type(exc).__name__), xbmc.LOGERROR)
            notify(L(30014))
            return

        if not manifest or not manifest.get('id'):
            notify(L(30014))
            return

        from lib.ui.views import _sync_addons_if_logged_in

        self.store.install_addon(transport_url, manifest)
        _sync_addons_if_logged_in(self.store)
        notify(L(30012))
        self._reload()

    def _remove(self, descriptor):
        import xbmc

        from lib.ui import dialogs
        from lib.ui.compat import L, notify

        manifest = descriptor.get('manifest') or {}
        flags = descriptor.get('flags') or {}
        if flags.get('protected'):
            notify(L(_PROTECTED_MESSAGE_STRING_ID))
            return

        if not dialogs.confirm(L(30011), manifest.get('name', '?'), xbmc.getLocalizedString(107), xbmc.getLocalizedString(106)):
            return

        from lib.ui.views import _sync_addons_if_logged_in

        try:
            self.store.remove_addon(descriptor.get('transportUrl'))
        except ValueError:
            notify(L(_PROTECTED_MESSAGE_STRING_ID))
            return

        _sync_addons_if_logged_in(self.store)
        notify(L(30013))
        self._reload()


def open_addons():
    """List every installed addon with install/remove actions. Mirrors
    `catalogpicker.open_catalog_picker`'s error-handling shape; unlike
    that picker there is no should-close-caller outcome to report, so
    this always returns None."""
    import xbmc

    from lib.ui.compat import L, log, notify

    count = len(get_store().get_addons())
    log('addonswindow: opening AddonsWindow (%d addons)' % count, xbmc.LOGINFO)
    win = None
    try:
        win = open_window(AddonsWindow, 'AddonsWindow.xml')
        win.doModal()
    except Exception as exc:  # a skin/UI failure must surface, not vanish
        log('addonswindow: window failed to open: %r' % (exc,), xbmc.LOGERROR)
        notify(L(30032))
    finally:
        # A normal return means AddonsWindow already closed itself (its
        # own onAction calls self.close()) before doModal() returned -
        # but an exception raised from WITHIN doModal() (onInit(), or a
        # callback mid-modal) skips that self-close entirely. Close
        # unconditionally here so no exit path leaves a zombie modal
        # window behind; closing an already-closed window is a safe no-op.
        if win is not None:
            try:
                win.close()
            except Exception:
                pass
