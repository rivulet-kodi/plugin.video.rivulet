"""AddonsWindow: a vertical list of every installed addon - Rivulet's
add-on manager. Row 0 installs a new addon from a manifest URL; every
other row opens an action menu (disable/enable, remove, move up/down)
for that addon - removal is refused for protected/official addons,
disabling is not (it is reversible local state, never pushed to
Stremio), and moving reorders the list every catalog/stream fan-out
call site walks in order. Built/run via `open_addons()`.
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

    def _reload(self, focus_transport_url=None):
        self.store = get_store()
        self.addons = self.store.get_addons()

        control = self.getControl(LIST)
        control.reset()
        control.addItems(self._build_items())
        self.setFocusId(LIST)
        if focus_transport_url is None:
            return
        # Control position 0 is the "install" row ahead of every addon
        # row, so an addon's list position is its addons-list index + 1.
        # A move that loses the user's place is unusable on a remote
        # control - see the move-up/down entries in _open_actions().
        index = next(
            (i for i, a in enumerate(self.addons) if a.get('transportUrl') == focus_transport_url),
            None,
        )
        if index is not None:
            control.selectItem(index + 1)

    def _build_items(self):
        from lib.ui.compat import L

        install_item = xbmcgui.ListItem(
            label=L(30010), label2=L(30192),
        )
        install_item.setProperty('position', 'install')
        items = [install_item]
        for index, descriptor in enumerate(self.addons):
            manifest = descriptor.get('manifest') or {}
            flags = descriptor.get('flags') or {}
            label = '%s  \u00b7  v%s' % (manifest.get('name', '?'), manifest.get('version', '?'))
            if flags.get('disabled'):
                label += '  \u00b7  ' + L(30251)
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
        index = int(position)
        self._open_actions(self.addons[index], index)

    def _guard_mutation(self, mutate):
        """Run a store mutation that goes through the CAS `update_addons()`
        path and turn `lib.store.ConcurrentUpdateError` into user
        feedback instead of an unhandled exception escaping the Kodi
        click handler.

        Kodi can run `default.py` as separate concurrent OS processes, so
        two clicks can race to update the same addons file; when the
        retried read-modify-write in `update_addons()` gives up, this
        notifies `L(30032)`, reloads the list to reflect whatever ended
        up persisted, and returns `False` so the caller skips its own
        success notification. Any other exception (e.g. `_remove`'s
        protected-addon `ValueError`) is left to propagate untouched.
        """
        import xbmc

        from lib.store import ConcurrentUpdateError
        from lib.ui.compat import L, log, notify

        try:
            mutate()
        except ConcurrentUpdateError as exc:
            log('addonswindow: concurrent update: %s' % exc, xbmc.LOGWARNING)
            notify(L(30032))
            self._reload()
            return False
        return True

    def _install(self):
        import xbmc

        from lib.stremio.addons import (
            AddonError,
            addon_error_detail,
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
            log('addonswindow: manifest fetch failed for %s: %s' % (safe_url_for_log(transport_url), addon_error_detail(exc)), xbmc.LOGERROR)
            notify(L(30014))
            return

        if not manifest or not manifest.get('id'):
            notify(L(30014))
            return

        from lib.ui.views import _sync_addons_if_logged_in

        if not self._guard_mutation(lambda: self.store.install_addon(transport_url, manifest)):
            return
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
            if not self._guard_mutation(lambda: self.store.remove_addon(descriptor.get('transportUrl'))):
                return
        except ValueError:
            notify(L(_PROTECTED_MESSAGE_STRING_ID))
            return

        _sync_addons_if_logged_in(self.store)
        notify(L(30013))
        self._reload()

    def _open_actions(self, descriptor, index):
        from lib.ui import dialogs
        from lib.ui.compat import L

        manifest = descriptor.get('manifest') or {}
        flags = descriptor.get('flags') or {}
        disabled = bool(flags.get('disabled'))
        rows = [L(30249) if disabled else L(30248), L(30250)]
        actions = [self._toggle, self._remove]
        # Omit rather than show a move entry that would be a no-op at the
        # boundary it points toward - an inert "Move up" on the first
        # addon is just noise on a remote-control menu.
        if index > 0:
            rows.append(L(30270))
            actions.append(lambda d: self._move(d, -1))
        if index < len(self.addons) - 1:
            rows.append(L(30271))
            actions.append(lambda d: self._move(d, 1))
        picked = dialogs.choose(manifest.get('name', '?'), rows)
        if not 0 <= picked < len(actions):
            return
        actions[picked](descriptor)

    def _toggle(self, descriptor):
        from lib.ui.compat import L, notify

        flags = descriptor.get('flags') or {}
        disabled = bool(flags.get('disabled'))
        if not self._guard_mutation(lambda: self.store.set_addon_disabled(descriptor.get('transportUrl'), not disabled)):
            return
        # Local presentation state only, deliberately not part of Stremio's
        # addon-collection schema, so no `_sync_addons_if_logged_in()` push
        # here - toggling must never cost a network call.
        notify(L(30252) if disabled else L(30251))
        self._reload()

    def _move(self, descriptor, delta):
        """Reorder ``descriptor`` by ``delta`` positions via the CAS
        `Store.move_addon()` and reload the list keeping focus on it -
        a reorder tool that loses your place on a remote control is
        unusable. Pushed to the Stremio account like install/remove
        (unlike `_toggle`'s local-only `disabled` flag): list order is
        part of the synced addon-collection shape, not presentation
        state.
        """
        from lib.ui.views import _sync_addons_if_logged_in

        transport_url = descriptor.get('transportUrl')
        if not self._guard_mutation(lambda: self.store.move_addon(transport_url, delta)):
            return
        _sync_addons_if_logged_in(self.store)
        self._reload(focus_transport_url=transport_url)


def open_addons():
    """List every installed addon with install/enable-disable/remove
    actions. Mirrors
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
