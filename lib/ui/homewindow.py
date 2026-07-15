"""HomeWindow: Rivulet's custom entry-point screen, replacing the
classical root plugin directory (`lib.ui.views.home`). A vertical menu
over the addon's fanart; picking a row opens the next screen as a
nested modal (see `lib.ui.uicommon`'s module docstring for the
navigation model this and every other custom screen shares).

Discover/Search/Library each draw a nested modal over Home and only
close it once their own selection chain reaches playback (see
`_open_discover`/`_open_search`/`_open_library`); Add-ons has no
playback path of its own, so Home always stays open behind it (see
`_open_addons`). Settings opens Kodi's own native settings dialog,
which is not worth replacing.
"""
import xbmcgui

from lib.ui.uicommon import BaseWindow, open_window

BACKGROUND = 30000
LIST = 30002
STATUS_LABEL = 30005  # plain text label; set at runtime via setLabel(), not a skin <label>

#: (localized-string id, action) - mirrors lib.ui.views.home()'s item set.
_MENU = (
    (30000, 'discover'),
    (30001, 'search'),
    (30002, 'library'),
    (30003, 'addons'),
    (30004, 'settings'),
)


#: (action -> localized-string id) for HomeWindow.xml's dimmer second
#: label per row - localized via L(), not plain literals, so it follows
#: Kodi's language setting the same as every other row's main label.
_SUBTITLES = {
    'discover': 30148,
    'search': 30149,
    'library': 30150,
    'addons': 30151,
    'settings': 30152,
}


def _menu_items(show_library):
    from lib.ui.compat import L, addon_media_path

    items = []
    for string_id, action in _MENU:
        if action == 'library' and not show_library:
            continue
        item = xbmcgui.ListItem(L(string_id))
        item.setProperty('action', action)
        item.setArt({'icon': addon_media_path('%s.png' % action)})
        item.setProperty('subtitle', L(_SUBTITLES[action]))
        items.append(item)
    return items


def _status_text(auth):
    """Render HomeWindow's top status line from the same `get_auth()`
    result onInit() already fetched for `show_library`: mirrors
    `lib.ui.views.addons()`'s exact "Logged in as <email/name/?>" wording
    and string id (30022) so the two screens read identically; there is
    no matching string id for the logged-out case, so it stays a plain
    literal."""
    from lib.ui.compat import L

    if not auth:
        return 'Not logged in'
    user = auth.get('user') or {}
    return L(30022) % (user.get('email') or user.get('name') or '?')


class HomeWindow(BaseWindow):
    """See module docstring. Built/run via `open_home()`."""

    def onInit(self):
        from lib.store import Store
        from lib.ui.compat import addon_fanart, addon_profile_dir

        auth = Store(addon_profile_dir()).get_auth()
        self.getControl(BACKGROUND).setImage(addon_fanart())
        self.getControl(LIST).addItems(_menu_items(bool(auth)))
        self.getControl(STATUS_LABEL).setLabel(_status_text(auth))
        self.setFocusId(LIST)

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
    from lib.ui.librarywindow import open_library
    if open_library():
        window.close()


def _open_addons(window):
    from lib.ui.addonswindow import open_addons
    open_addons()


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
    """Build and run the HomeWindow modal; blocks until the user exits.

    default.py wraps this call in its own try/except and falls back to the
    classical home directory on ANY exception, so an exception raised here
    must keep propagating unchanged - this only logs it for diagnostics and
    guarantees the window is closed (it may not have had a chance to
    self-close, e.g. if onInit() or doModal() itself raised) before
    re-raising."""
    import xbmc

    from lib.ui.compat import log

    log('homewindow: opening HomeWindow', xbmc.LOGINFO)
    win = open_window(HomeWindow, 'HomeWindow.xml')
    try:
        win.doModal()
    except Exception as exc:  # default.py's caller falls back to classical home
        log('homewindow: HomeWindow failed: %r' % (exc,), xbmc.LOGERROR)
        raise
    finally:
        # A normal return means HomeWindow already closed itself; close()
        # again here is a safe no-op. Only a raised exception makes this
        # the window's one chance to close.
        try:
            win.close()
        except Exception:
            pass
    log('homewindow: HomeWindow closed', xbmc.LOGINFO)
