"""Shared helpers for Rivulet's custom `WindowXML` screens.

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
- `busy_dialog(heading, message='')`: unlike that classical GetDirectory
  spinner above, Kodi has no busy indicator of its own for a fetch made
  from INSIDE an already-open custom window (search aggregation, a
  catalog/meta/streams fetch) - so screens open this context-managed
  `xbmcgui.DialogProgress` explicitly for the fetch's duration and close
  it before opening any further window.
- `open_window(window_cls, xml_name, *args, **kwargs)`: build one of our
  windows against the addon's own skin directory
  (`resources/skins/Default/720p/<xml_name>`), matching
  `infowindow.open_showcase`'s resolution so every screen is constructed
  identically.
- `ModalStackWindow`/`close_windows_for_playback()`: every one of these
  screens IS an `xbmcgui.WindowXMLDialog` - confirmed on a real device,
  Kodi routes ALL input (play/pause, the OSD) to whichever dialog is
  topmost, never to `fullscreenvideo`, so starting playback while even
  one ancestor screen is still open underneath leaves the video looking
  entirely unresponsive until the user backs all the way out to it.
  Every screen mixes in `ModalStackWindow`, which tracks it on a
  module-level stack for the duration of its `doModal()` call;
  `close_windows_for_playback(exclude=<the picker about to play>)`,
  called right before `xbmc.Player().play()`, force-closes every OTHER
  live screen so Kodi's player ends up the only modal thing on screen,
  then reopens each one - exactly where the user left it - once
  playback ends and control naturally unwinds back to it.

Navigation model: each screen is a blocking `doModal()` call. "Forward"
navigation is a screen's onClick calling another screen's `open_*()`
helper (which blocks until that screen closes); "back" is simply that
inner call returning, so nested doModal() calls form a navigation stack
for free - no separate router/state machine needed. A picker's
force-close of every ancestor is safe to fire from deep inside that
stack (several `open_*()` calls below the screen the user actually
started at): `close()` only dismisses a screen's underlying C++ window
immediately - the Python `doModal()` call that opened it is still
blocked several stack frames further down and does not actually return
until every nested call between here and there unwinds naturally, which
is exactly when `ModalStackWindow.doModal()` gets to notice the
force-close and reopen.
"""
import contextlib

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


@contextlib.contextmanager
def busy_dialog(heading, message=''):
    """An indeterminate `xbmcgui.DialogProgress` spinner for a blocking
    network fetch made from inside an already-open custom window - which,
    unlike a classical GetDirectory call, has no Kodi-provided busy
    indicator of its own once the window is open (see the module
    docstring's `busy_dialog` bullet). Mirrors the exact DialogProgress
    idiom `lib.ui.player._prebuffer_torrent` and
    `lib.ui.router._download_server_binary` already use.

    Yields the `xbmcgui.DialogProgress` instance so callers can
    `.update(percent, message)` for real progress feedback (e.g.
    per-addon in a fetch loop) or check `.iscanceled()` to support early
    cancellation; both are optional - a caller that does neither still
    gets a visible spinner for the duration of the `with` block. Always
    closed on the way out, even on an exception, so it can never overlap
    a subsequently-opened window.
    """
    dialog = xbmcgui.DialogProgress()
    dialog.create(heading, message)
    dialog.update(0, message)
    try:
        yield dialog
    finally:
        dialog.close()


def addon_skin_path():
    """Return the addon's own install path, the `cwd` a `WindowXML`
    resolves its `resources/skins/<skin>/<res>/<xml>` from."""
    from lib.ui.compat import ADDON
    return ADDON.getAddonInfo('path')


def open_window(window_cls, xml_name, *args, **kwargs):
    """Build `window_cls(xml_name, addon_skin_path(), 'Default', '720p')`
    and return it (unconstructed screens are useless - callers still call
    `.start(...)` themselves, since each screen's `start()` signature
    differs)."""
    return window_cls(xml_name, addon_skin_path(), 'Default', '720p', *args, **kwargs)


#: Live Rivulet screens, in the order their `doModal()` calls are
#: currently blocked - outermost (first opened) first, innermost (most
#: recently opened, currently topmost) last. See `ModalStackWindow`/
#: `close_windows_for_playback()` in the module docstring.
_MODAL_WINDOW_STACK = []


class ModalStackWindow:
    """Mixin registering a screen on `_MODAL_WINDOW_STACK` for the
    duration of its `doModal()` call, and reopening it - exactly where
    the user left it - if `close_windows_for_playback()` force-closed it
    to make room for the player rather than the user genuinely backing
    out of it.

    Mixed into `BaseWindow` (so `HomeWindow`/`SearchWindow`/
    `StreamsWindow`/every other screen built on it gets this for free)
    and directly onto `DetailWindow`/`ShowcaseWindow`, which subclass
    `xbmcgui.WindowXMLDialog` themselves with no shared base to route it
    through. MUST be listed FIRST in a class's bases
    (`class Foo(ModalStackWindow, xbmcgui.WindowXMLDialog)`) so
    `super().doModal()` below resolves to Kodi's real implementation,
    not back to this mixin.
    """

    #: Set True by `close_windows_for_playback()` immediately before it
    #: calls `close()` on this window; cleared at the top of every
    #: `doModal()` call. Class-level default so a window that has never
    #: entered `doModal()` yet still reads False instead of raising.
    _closed_for_playback = False

    def doModal(self):
        _MODAL_WINDOW_STACK.append(self)
        self._closed_for_playback = False
        try:
            super().doModal()
            while self._closed_for_playback and not xbmc.Monitor().abortRequested():
                # Force-closed to hand the screen to the player, not a
                # genuine user "back" - reopen exactly where they left
                # off. abortRequested() guards a Kodi shutdown landing
                # mid-playback: nothing should pop a fresh modal window
                # up in front of a Kodi that is already on its way down.
                self._closed_for_playback = False
                super().doModal()
        finally:
            _pop_modal_window(self)


def _pop_modal_window(window):
    """Remove `window` from `_MODAL_WINDOW_STACK` by identity, scanning
    from the top down - never `list.remove()`, which matches by `==`
    and would remove the first EQUAL entry rather than specifically
    `window` (a screen that ever defined its own `__eq__` could make
    that the wrong one), and silently returns rather than raising if
    `window` is not present.
    """
    for index in range(len(_MODAL_WINDOW_STACK) - 1, -1, -1):
        if _MODAL_WINDOW_STACK[index] is window:
            del _MODAL_WINDOW_STACK[index]
            return


def close_windows_for_playback(exclude=None):
    """Force-close every live Rivulet screen except `exclude` (the
    screen whose own `onClick()` is calling this, immediately before
    `xbmc.Player().play()`) so Kodi's player ends up the only modal
    thing on screen - see the module docstring for why every screen
    being a real `WindowXMLDialog` otherwise leaves playback controls
    unresponsive.

    Walks a snapshot of `_MODAL_WINDOW_STACK` innermost-first (the
    reversed live order), marking each survivor `_closed_for_playback =
    True` and then calling `close()` on it - wrapped so one screen's
    broken `close()` can never stop the rest of the stack from tearing
    down (logged at LOGWARNING, not raised).

    `close()` only dismisses that screen's underlying C++ window right
    away; the Python `doModal()` call that opened it is normally still
    several stack frames further down (blocked inside whatever chain of
    nested `open_*()` calls eventually reached the screen calling this)
    and will not actually return until every frame between here and
    there unwinds on its own - this is exactly what makes it safe to
    call from deep inside a nested `onClick()`. Once each ancestor's
    `doModal()` call does return, `ModalStackWindow.doModal()` is what
    notices the force-close and reopens it.
    """
    from lib.ui.compat import log

    for window in reversed(list(_MODAL_WINDOW_STACK)):
        if window is exclude:
            continue
        window._closed_for_playback = True
        try:
            window.close()
        except Exception as exc:  # one ancestor's broken close() must never block the rest
            log('uicommon: close_windows_for_playback failed to close %r: %r' % (window, exc), xbmc.LOGWARNING)


class BaseWindow(ModalStackWindow, xbmcgui.WindowXMLDialog):
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
