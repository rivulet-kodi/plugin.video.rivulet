"""Shared helpers for Rivulet's custom `WindowXMLDialog` screens.

Rivulet's UI is moving from Kodi directory listings to a small stack of
fullscreen custom windows (`HomeWindow`, `ShowcaseWindow`/coverflow,
`DetailWindow`, `StreamsWindow`, ...), following the pattern already
proven by `lib.ui.infowindow.ShowcaseWindow`. This module centralizes the
bits every one of those screens needs so they stay consistent:

- `BACK_ACTIONS`: the action ids that close a window without a selection.
- `dismiss_busy_dialog()`: Kodi shows a "working" spinner while a plugin's
  GetDirectory call is in flight; a custom window opened from inside that
  call must close it first or the window can appear uninteractive/behind
  it (mirrors the reference addon's `prevent_busy()`).
- `open_window(window_cls, xml_name, *args, **kwargs)`: build one of our
  windows against the addon's own skin directory
  (`resources/skins/Default/720p/<xml_name>`), matching
  `infowindow.open_showcase`'s resolution so every screen is constructed
  identically.

Navigation model: each screen is a blocking `doModal()` call. "Forward"
navigation is a screen's onClick calling another screen's `open_*()`
helper (which blocks until that screen closes); "back" is simply that
inner call returning, so nested doModal() calls form a navigation stack
for free - no separate router/state machine needed.
"""
import xbmc
import xbmcgui

#: Back/Nav-Back, PreviousMenu/Esc, Backspace - closes a window without a
#: selection. Shared by every custom screen (mirrors infowindow's
#: `_BACK_ACTIONS`, which keeps its own copy so this module can be added
#: without touching that already-tested one).
BACK_ACTIONS = frozenset({9, 10, 92})


def dismiss_busy_dialog():
    """Close Kodi's GetDirectory "working" spinner so a modal opened from
    inside a directory callback is immediately interactive."""
    xbmc.executebuiltin('Dialog.Close(all, true)')


def addon_skin_path():
    """Return the addon's own install path, the `cwd` a `WindowXMLDialog`
    resolves its `resources/skins/<skin>/<res>/<xml>` from."""
    from lib.ui.compat import ADDON
    return ADDON.getAddonInfo('path')


def open_window(window_cls, xml_name, *args, **kwargs):
    """Build `window_cls(xml_name, addon_skin_path(), 'Default', '720p')`
    and return it (unconstructed screens are useless - callers still call
    `.start(...)` themselves, since each screen's `start()` signature
    differs)."""
    return window_cls(xml_name, addon_skin_path(), 'Default', '720p', *args, **kwargs)


class BaseWindow(xbmcgui.WindowXMLDialog):
    """Common `onAction` back-handling for a simple (non-coverflow) modal
    screen: any of `BACK_ACTIONS` closes the window. Screens with extra
    per-focus behaviour (e.g. the coverflow's background swap) should
    override `onAction` and still check `BACK_ACTIONS` themselves rather
    than subclass this - see `infowindow.ShowcaseWindow`."""

    def onAction(self, action):
        if action.getId() in BACK_ACTIONS:
            self.close()


def fallback_to_classical(action, **params):
    """Temporary bridge for screens with no custom-window replacement yet:
    open the classical plugin directory for `action` (see
    `lib.ui.router.url_for`) in Kodi's Videos window. Callers should
    close every custom window in their call chain afterwards
    (conventionally: return True from an `open_*()` function and have
    its caller close too).

    Uses `ActivateWindow(Videos, ...)`, NOT `Container.Update(...)`:
    our custom windows are modal dialogs overlaying whatever screen was
    active before the addon launched (often not a video directory at
    all), so there is no existing compatible container for
    Container.Update to target - it fails outright
    ("GetDirectory - Error getting ..."/"CGUIMediaWindow::GetDirectory(...)
    failed", confirmed against a real device's kodi.log).
    ActivateWindow(Videos, url) instead explicitly opens a fresh Videos
    window at `url`, the standard way to jump into a plugin directory
    from a non-container context (a dialog, a script, anywhere).
    """
    from lib.ui import router
    xbmc.executebuiltin('ActivateWindow(Videos,%s)' % router.url_for(action, **params))
