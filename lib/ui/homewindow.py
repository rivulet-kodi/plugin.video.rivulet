"""HomeWindow: Rivulet's custom entry-point screen, replacing the
classical root plugin directory (`lib.ui.views.home`). A vertical menu
over the addon's fanart; picking a row opens the next screen as a
nested modal (see `lib.ui.uicommon`'s module docstring for the
navigation model this and every other custom screen shares).

Library/Add-ons management have no custom screen yet, so they fall back
to the classical Kodi directory via `lib.ui.uicommon.fallback_to_classical`
(and close HomeWindow first, since there is no nested window to draw over
it in that case - see `_open_library`/`_open_addons`). Settings opens
Kodi's own native settings dialog, which is not worth replacing.
"""
import xbmcgui

from lib.ui.uicommon import BACK_ACTIONS, fallback_to_classical, open_window

BACKGROUND = 30000
LIST = 30002

#: (localized-string id, action) - mirrors lib.ui.views.home()'s item set.
_MENU = (
    (30000, 'discover'),
    (30001, 'search'),
    (30002, 'library'),
    (30003, 'addons'),
    (30004, 'settings'),
)


def _menu_items(show_library):
    from lib.ui.compat import L

    items = []
    for string_id, action in _MENU:
        if action == 'library' and not show_library:
            continue
        item = xbmcgui.ListItem(L(string_id))
        item.setProperty('action', action)
        items.append(item)
    return items


class HomeWindow(xbmcgui.WindowXMLDialog):
    """See module docstring. Built/run via `open_home()`."""

    def onInit(self):
        from lib.store import Store
        from lib.ui.compat import addon_fanart, addon_profile_dir

        show_library = bool(Store(addon_profile_dir()).get_auth())
        self.getControl(BACKGROUND).setImage(addon_fanart())
        self.getControl(LIST).addItems(_menu_items(show_library))
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
        handler = _ACTIONS.get(focused.getProperty('action'))
        if handler:
            handler(self)


def _open_discover(window):
    # Nested modal: Discover draws over Home, so Home stays open - backing
    # all the way out returns here rather than exiting the addon.
    from lib.ui.catalogpicker import open_catalog_picker
    if open_catalog_picker():
        window.close()


def _open_search(window):
    from lib.ui.searchwindow import open_search
    if open_search():
        window.close()


def _open_library(window):
    # No custom screen yet: close Home (nothing custom left to show over
    # it) and drop back to the classical directory.
    window.close()
    fallback_to_classical('library')


def _open_addons(window):
    window.close()
    fallback_to_classical('addons')


def _open_settings(window):
    from lib.ui.compat import ADDON
    ADDON.openSettings()


_ACTIONS = {
    'discover': _open_discover,
    'search': _open_search,
    'library': _open_library,
    'addons': _open_addons,
    'settings': _open_settings,
}


def open_home():
    """Build and run the HomeWindow modal; blocks until the user exits."""
    win = open_window(HomeWindow, 'HomeWindow.xml')
    win.doModal()
