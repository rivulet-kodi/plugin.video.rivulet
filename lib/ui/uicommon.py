# SPDX-FileCopyrightText: 2026 M0Rf30
# SPDX-License-Identifier: MIT

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
  `lib.ui.dialogs.RivuletBusy` spinner explicitly for the fetch's
  duration and close it before opening any further window.
- `run_cancellable(func, heading)` / `wait_cancellable(func, dialog)` /
  `deliver_queued_input()`: the blocking call runs on a worker thread
  while the GUI thread polls the spinner's `iscanceled()` and, every
  tick, calls into `xbmc.Monitor.waitForAbort()` - the ONLY thing that
  makes Kodi deliver the Back press it has queued for this script
  (`MakePendingCalls()`); a plain `Queue.get(timeout)`/`Event.wait()`
  poll never sees it. Back returns `CANCELLED` and the caller stays on
  its own screen. Kodi also swallows every action while the topmost
  modal plays a WindowClose animation (`CGUIWindowManager::HandleAction`),
  which is why no skin window carries one (tests/test_skin_xml.py).
- `open_window(window_cls, xml_name, *args, **kwargs)`: build one of our
  windows against the addon's own skin directory
  (`resources/skins/Default/1080i/<xml_name>`), matching
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
- UI phases (`begin_player_phase()`/`end_player_phase()`/
  `input_suppressed()`/`UiHeartbeat`/`is_duplicate_launch()`): while the
  player owns the screen no Rivulet screen reacts to input and a Kodi
  re-run of the plugin root opens nothing - see the "UI phases" notes
  above `ModalStackWindow`.

Jumping into a plugin directory from one of these dialogs (e.g. a
RunPlugin-only action) must use `ActivateWindow(Videos, url)`, NOT
`Container.Update(...)`: these custom windows are modal dialogs
overlaying whatever screen was active before the addon launched (often
not a video directory at all), so there is no existing compatible
container for Container.Update to target - it fails outright
("GetDirectory - Error getting ..."/"CGUIMediaWindow::GetDirectory(...)
failed", confirmed against a real device's kodi.log). ActivateWindow
(Videos, url) instead explicitly opens a fresh Videos window at `url`,
the standard way to jump into a plugin directory from a non-container
context (a dialog, a script, anywhere).

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
import threading

import xbmc
import xbmcgui

from lib.stremio.streaminfo import escape_label  # noqa: F401 (re-exported)

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
    """A `lib.ui.dialogs.RivuletBusy` spinner for a blocking network
    fetch made from inside an already-open custom window - which,
    unlike a classical GetDirectory call, has no Kodi-provided busy
    indicator of its own once the window is open (see the module
    docstring's `busy_dialog` bullet).

    Yields the `RivuletBusy` instance so callers can `.update(percent,
    message)` for real progress feedback (e.g. per-addon in a fetch
    loop) or check `.iscanceled()` to support early cancellation; both
    are optional - a caller that does neither still gets a visible
    spinner for the duration of the `with` block. Always closed on the
    way out, even on an exception, so it can never overlap a
    subsequently-opened window.
    """
    # Function-local: lib.ui.dialogs imports open_window/BACK_ACTIONS
    # from this module at module scope, so importing it back here at
    # module scope would form a cycle - same idiom this file already
    # uses for lib.ui.compat (see addon_skin_path()/
    # close_windows_for_playback()).
    from lib.ui.dialogs import RivuletBusy

    dialog = RivuletBusy()
    dialog.create(heading, message)
    dialog.update(0, message)
    try:
        yield dialog
    finally:
        dialog.close()


#: How long `run_cancellable()` waits on its worker between Back checks.
#: The wait itself wakes the instant the worker finishes, so this only
#: bounds how stale a Back press can get while a fetch is still running;
#: with `_PUMP_SECONDS` below, a cancel is honoured well inside the 0.5 s
#: a user perceives as "immediate".
_CANCEL_POLL_SECONDS = 0.1

#: Shortest `xbmc.Monitor.waitForAbort()` that still runs its delivery
#: step. NEVER pass 0 here: Kodi's `waitForAbort()` treats a timeout of
#: `<= 0` as INFINITE (`timeoutMS <= 0` -> `endTime.SetInfinite()`,
#: xbmc/interfaces/legacy/Monitor.cpp), which would hang the caller until
#: Kodi quits.
_PUMP_SECONDS = 0.001


class _Cancelled:
    """Type of the `CANCELLED` sentinel; falsy so a stray truthiness check
    reads it as "nothing", but callers MUST test `is CANCELLED` - an empty
    list/None are legitimate results too."""

    def __bool__(self):
        return False

    def __repr__(self):
        return 'CANCELLED'


#: What `run_cancellable()` returns when the user backed out (or Kodi is
#: shutting down) before the fetch finished.
CANCELLED = _Cancelled()


def deliver_queued_input(monitor=None):
    """Let Kodi hand this script thread the window callbacks
    (`onAction`/`onClick`) it has queued for it; returns True if Kodi is
    shutting down.

    Kodi does NOT run a script's window callbacks on arrival: it queues
    them and delivers them only from `LanguageHook::MakePendingCalls()`,
    which `xbmc.Monitor.waitForAbort()` (and `xbmc.sleep()`) call every
    100 ms slice (xbmc/interfaces/legacy/Monitor.cpp). A thread parked in
    `queue.Queue.get(timeout=...)`, `Event.wait()` or a socket read never
    reaches it, so a Back press on a spinner stayed undelivered - and its
    `iscanceled()` False - until something else happened to call into
    Kodi. Every loop that polls `iscanceled()` MUST call this (or
    `waitForAbort()` itself) once per tick, or the poll is blind.
    """
    return (monitor or xbmc.Monitor()).waitForAbort(_PUMP_SECONDS)


def wait_cancellable(func, dialog, monitor=None):
    """Run the blocking, argument-less callable `func` on a daemon worker
    thread and wait for it in `_CANCEL_POLL_SECONDS` slices, polling
    `dialog.iscanceled()` (any object with that method - a `RivuletBusy`
    or the `RivuletProgress` a stream resolve already owns) and letting
    Kodi deliver queued Back presses every slice
    (`deliver_queued_input()`). Returns `func()`'s value, re-raises
    whatever `func()` raised (`AddonError` etc. reach the caller exactly
    as they did when the call ran inline), or returns `CANCELLED` if the
    user pressed Back / Kodi is quitting first.

    On `CANCELLED` the worker is NOT interrupted - a blocking socket read
    cannot be, and it is bounded by the request's own timeout - it is
    simply orphaned (daemon, so it never delays shutdown) and its result
    or exception is discarded: the caller must not use anything `func`
    was producing, and so can never open a screen for it later.

    After a Back cancel the queue is drained (`drain_queued_input()`)
    while the caller's dialog is still registered, so a second stale Back
    is swallowed instead of closing the screen once the dialog is gone.

    One worker per call (not a pool): the call sites that fan out do so
    inside `func` under their own per-call-site bound.
    """
    from lib.ui.compat import log

    done = threading.Event()
    outcome = {}

    def _worker():
        try:
            outcome['value'] = func()
        except BaseException as exc:  # handed to the caller's thread below
            outcome['error'] = exc
        finally:
            done.set()

    threading.Thread(target=_worker, name='RivuletCancellableFetch', daemon=True).start()
    monitor = monitor or xbmc.Monitor()
    while not done.wait(_CANCEL_POLL_SECONDS):
        aborted = deliver_queued_input(monitor)
        if aborted or dialog.iscanceled():
            log('uicommon: cancellable wait ended early by %s' % ('shutdown' if aborted else 'Back'), xbmc.LOGINFO)
            if not aborted:
                drain_queued_input(monitor)
            return CANCELLED
    if dialog.iscanceled():
        # Back landed in the same instant the call finished: the user's
        # explicit "no" outranks a result they never saw.
        drain_queued_input(monitor)
        return CANCELLED
    if 'error' in outcome:
        raise outcome['error']
    return outcome['value']


#: Slices / length of `drain_queued_input()`. Short and bounded: it only has
#: to outlast Kodi's hand-off of what is already queued.
_DRAIN_PUMPS = 3
_DRAIN_SLICE_SECONDS = 0.05


def drain_queued_input(monitor=None):
    """Pump `deliver_queued_input()` a few times so every callback Kodi has
    queued for this script is delivered NOW. Call it right after a cancel
    while the transient dialog is still registered: a further stale Back
    then hits `redirect_back_to_transient()` (harmless - the dialog is
    already cancelled) instead of closing the screen underneath once the
    dialog is gone. Returns True if Kodi is shutting down."""
    monitor = monitor or xbmc.Monitor()
    for _ in range(_DRAIN_PUMPS):
        if monitor.waitForAbort(_DRAIN_SLICE_SECONDS):
            return True
    return False


def run_cancellable(func, heading, message=''):
    """`wait_cancellable(func, <busy spinner>)`: run `func` on a worker
    under a `busy_dialog(heading, message)` so Back on the spinner is
    honoured within ~0.1-0.2 s instead of after the network call's own
    timeout (a first-page catalog fetch ran 45 s in kodi.log with the
    spinner ignoring every Back press). Result/exception/`CANCELLED`
    contract: see `wait_cancellable()`.

    Queued input is delivered once as soon as the spinner is up, so a Back
    pressed just before it appeared cancels it (and `func` is never even
    started) rather than closing the screen underneath later.

    The spinner is closed before this returns on every path, so the
    caller's own screen is topmost again and receives the next Back.
    """
    with busy_dialog(heading, message) as dialog:
        aborted = deliver_queued_input()
        if aborted or dialog.iscanceled():
            if not aborted:
                drain_queued_input()
            return CANCELLED
        return wait_cancellable(func, dialog)


def addon_skin_path():
    """Return the addon's own install path, the `cwd` a `WindowXML`
    resolves its `resources/skins/<skin>/<res>/<xml>` from."""
    from lib.ui.compat import ADDON
    return ADDON.getAddonInfo('path')


def open_window(window_cls, xml_name, *args, **kwargs):
    """Build `window_cls(xml_name, addon_skin_path(), 'Default', '1080i')`
    and return it (unconstructed screens are useless - callers still call
    `.start(...)` themselves, since each screen's `start()` signature
    differs)."""
    return window_cls(xml_name, addon_skin_path(), 'Default', '1080i', *args, **kwargs)


# --- UI phases ---------------------------------------------------------------
#
# A Rivulet session is three phases, and only the first and the last are ours:
#
#   addon   Rivulet screens own the input. (before playback, and again once
#           the player is gone)
#   player  every Rivulet screen is closed and Kodi's VideoPlayer/OSD owns
#           the input - from `close_windows_for_playback()` right before
#           `Player.play()` until `streamswindow` has seen the player truly
#           close (its stop/end/error callback, which Kodi only fires AFTER
#           CVideoPlayer::CloseFile() returned) and drained what was queued.
#
# While the phase is `player`, nothing of Rivulet's may react to input or
# open anything. Two things used to break that, both visible in kodi.log:
#
# 1. Kodi queues a script window's callbacks and delivers them whenever the
#    script thread next calls into Kodi - for the closed ancestors that was
#    the polling `waitForAbort()` of the playback wait, so a Back/click made
#    before the handoff was replayed against screens that were no longer
#    there. `input_suppressed()` gates every `ModalStackWindow` callback
#    (see `ModalStackWindow.__init_subclass__`).
# 2. When playback stops Kodi re-activates the Videos window the addon was
#    launched from; CGUIMediaWindow::OnInitWindow() re-fetches a plugin
#    path ~200ms later (PLUGIN_REFRESH_DELAY), which runs `default.py` with
#    the bare root again - a SECOND interpreter that opened a second
#    HomeWindow over the restored StreamsWindow (kodi.log 01:07:59.987
#    "Control 55 in window 10025 ..." then 01:08:00.189 "opening
#    HomeWindow") and ran `Dialog.Close(all)`, closing the picker that had
#    just come back. `default.py` asks `is_duplicate_launch()` first.
#
# The in-process flag (`_ui_phase`) is what screens consult - a plain
# attribute read, no Kodi call, safe to hit on every mouse-move action. The
# same phase plus a heartbeat are also published on Window(10000), the one
# place two script interpreters can see each other, for `default.py`.

PHASE_ADDON = 'addon'
PHASE_PLAYER = 'player'

#: Window(10000) property names owned by the interpreter running Home.
PHASE_PROPERTY = 'rivulet.ui.phase'
HEARTBEAT_PROPERTY = 'rivulet.ui.heartbeat'

#: How often the heartbeat thread refreshes its stamp, and how old a stamp may
#: be before the owner counts as dead. A killed/crashed interpreter never
#: clears its property; the age limit is what keeps that from locking the user
#: out of Rivulet (a fresh launch just opens Home normally once the stamp is
#: stale). Five beats fit in one limit so a busy GIL or a slow GUI-lock
#: acquisition never reads as death.
UI_HEARTBEAT_INTERVAL_SECONDS = 2.0
UI_HEARTBEAT_MAX_AGE_SECONDS = 10.0

#: Kodi's Home window - always exists, the conventional shared-state window.
_HOME_WINDOW_ID = 10000

_ui_phase = PHASE_ADDON


def _home_property(name, value=None):
    """Read (`value is None`) or write Window(10000)'s `name`. Never raises:
    this is bookkeeping, and a Kodi that cannot answer must degrade to "no
    marker" (a normal launch), not break a screen or a launch."""
    try:
        window = xbmcgui.Window(_HOME_WINDOW_ID)
        if value is None:
            return window.getProperty(name)
        if value == '':
            window.clearProperty(name)
        else:
            window.setProperty(name, value)
    except Exception as exc:  # noqa: BLE001 - see docstring
        xbmc.log('[plugin.video.rivulet] uicommon: Window(10000) property %s failed: %r' % (name, exc), xbmc.LOGDEBUG)
    return ''


def input_suppressed():
    """True while Kodi's player owns the input (phase `player`): screens must
    ignore every action and click, and nothing may open. A pure in-process
    flag read - cheap enough for `onAction()`, which Kodi fires for every
    mouse move."""
    return _ui_phase == PHASE_PLAYER


def player_phase_active():
    """Alias of `input_suppressed()` for callers that mean the phase, not the
    input policy."""
    return _ui_phase == PHASE_PLAYER


def begin_player_phase():
    """Phase 1 -> 2 handoff: from now on Rivulet is silent until
    `end_player_phase()`. Called by `close_windows_for_playback()`, i.e. right
    before `Player.play()`, so it also covers every callback still queued from
    before the handoff."""
    global _ui_phase
    _ui_phase = PHASE_PLAYER
    _home_property(PHASE_PROPERTY, PHASE_PLAYER)


def end_player_phase():
    """Phase 2 -> 3: the player is gone and the queue drained - Rivulet screens
    may open and take input again. Idempotent."""
    global _ui_phase
    if _ui_phase == PHASE_ADDON:
        return
    _ui_phase = PHASE_ADDON
    _home_property(PHASE_PROPERTY, PHASE_ADDON)


class UiHeartbeat:
    """Context manager marking "a Rivulet UI interpreter is alive" on
    Window(10000): a timestamp refreshed every `interval` seconds by a daemon
    thread, cleared (and the thread joined) on exit. `open_home()` wraps the
    whole Home session in one.

    The value is `<token>|<unix time>`; the token makes `stop()` clear only its
    OWN stamp, never one a newer interpreter already wrote.
    """

    def __init__(self, interval=UI_HEARTBEAT_INTERVAL_SECONDS, clock=None):
        import time

        self._interval = interval
        self._clock = clock or time.time
        self._token = '%x%x' % (int(self._clock() * 1000) & 0xFFFFFFFF, id(self) & 0xFFFFFF)
        self._stop = threading.Event()
        self._thread = None

    def _beat(self):
        _home_property(HEARTBEAT_PROPERTY, '%s|%.3f' % (self._token, self._clock()))

    def _run(self):
        while not self._stop.wait(self._interval):
            self._beat()

    def start(self):
        global _ui_phase
        _ui_phase = PHASE_ADDON  # a fresh session never inherits a stuck phase
        _home_property(PHASE_PROPERTY, PHASE_ADDON)
        self._beat()
        self._thread = threading.Thread(target=self._run, name='rivulet-ui-heartbeat', daemon=True)
        self._thread.start()

    def stop(self):
        global _ui_phase
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        _ui_phase = PHASE_ADDON
        if (_home_property(HEARTBEAT_PROPERTY) or '').startswith(self._token + '|'):
            _home_property(HEARTBEAT_PROPERTY, '')
            _home_property(PHASE_PROPERTY, '')

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.stop()
        return False


def ui_owner_alive(max_age=UI_HEARTBEAT_MAX_AGE_SECONDS, clock=None):
    """True if some interpreter's `UiHeartbeat` stamp is fresh (within
    `max_age` seconds either way - a clock that jumped backwards must not read
    as an immortal owner)."""
    import time

    raw = _home_property(HEARTBEAT_PROPERTY) or ''
    try:
        stamp = float(raw.rsplit('|', 1)[1])
    except (IndexError, ValueError):
        return False
    age = (clock or time.time)() - stamp
    return -max_age <= age <= max_age


def is_duplicate_launch():
    """True when a bare plugin-root run (`default.py` home action) must NOT open
    another HomeWindow: another interpreter's UI is alive AND either its
    phase is `player`, or Kodi is not playing anything.

    - alive + `player`: the player owns the screen; this is Kodi refreshing
      the Videos container the addon was started from. Whatever it plays or
      closes now, a second UI is wrong.
    - alive + not playing (+ phase `addon`): the same refresh landing just
      after the old UI came back (it ends the phase before the refresh's
      interpreter starts), or any stray re-run while a UI is on screen.
    - alive + playing + `addon`: a video is running in the background and the
      user opened Rivulet from Kodi's menus while an earlier session is still
      up - a deliberate launch, honoured.
    - not alive (never started, exited, killed - stamp stale): a genuine
      launch, always honoured, so a crashed interpreter can never lock the
      user out.
    """
    if not ui_owner_alive():
        return False
    if _home_property(PHASE_PROPERTY) == PHASE_PLAYER:
        return True
    try:
        playing = xbmc.Player().isPlaying()
    except Exception:  # noqa: BLE001 - cannot tell: treat as "not playing"
        playing = False
    return not playing


#: `ModalStackWindow` callbacks gated by `input_suppressed()`: the ones that
#: act on input. onInit and friends stay live.
_GATED_CALLBACKS = ('onAction', 'onClick', 'onDoubleClick', 'onControl')


def _gate_input(callback, name=''):
    """Wrap one window callback so it is a no-op while `input_suppressed()`,
    and (for `onAction`) so a Back press is redirected to the active
    transient dialog instead of the screen - see `redirect_back_to_transient()`."""
    import functools

    @functools.wraps(callback)
    def gated(self, *args, **kwargs):
        if _ui_phase == PHASE_PLAYER:
            return None
        if name == 'onAction' and args and redirect_back_to_transient(args[0]):
            return None
        return callback(self, *args, **kwargs)

    gated._rivulet_input_gated = True
    return gated


# --- transient dialogs -------------------------------------------------------
#
# A spinner (`busy_dialog`/`run_cancellable`) or a `RivuletProgress` is
# topmost while a blocking call runs, so a Back made DURING it reaches the
# dialog. But Kodi delivers a script's callbacks late (see
# `deliver_queued_input`): a Back pressed just BEFORE the spinner appeared
# is still queued for the screen underneath and arrived after the spinner
# was up, closing that screen while the fetch carried on. While any
# transient dialog is registered here, a Back reaching a `ModalStackWindow`
# screen is therefore taken as "cancel the spinner" (the user's intent) and
# never closes the screen.

#: Live transient dialogs, innermost last. Objects with a `cancel()` method
#: (`RivuletBusy`, `RivuletProgress`), registered in their `create()` and
#: removed in their `close()`.
_TRANSIENT_DIALOGS: list = []


def register_transient_dialog(dialog):
    """Mark `dialog` as on screen (idempotent)."""
    if not any(entry is dialog for entry in _TRANSIENT_DIALOGS):
        _TRANSIENT_DIALOGS.append(dialog)


def unregister_transient_dialog(dialog):
    """Remove `dialog` by identity; unknown dialogs are ignored."""
    for index in range(len(_TRANSIENT_DIALOGS) - 1, -1, -1):
        if _TRANSIENT_DIALOGS[index] is dialog:
            del _TRANSIENT_DIALOGS[index]
            return


def transient_dialog_active():
    """True while a spinner/progress dialog is on screen."""
    return bool(_TRANSIENT_DIALOGS)


def redirect_back_to_transient(action):
    """If a transient dialog is active and `action` is a Back, cancel that
    dialog and return True (the caller must then drop the action); else
    False. Never raises: a malformed action just is not a Back."""
    if not _TRANSIENT_DIALOGS:
        return False
    try:
        is_back = action.getId() in BACK_ACTIONS
    except Exception:  # noqa: BLE001 - not an action object
        return False
    if not is_back:
        return False
    try:
        _TRANSIENT_DIALOGS[-1].cancel()
    except Exception as exc:  # noqa: BLE001 - a broken dialog must not close the screen
        xbmc.log('[plugin.video.rivulet] uicommon: transient cancel failed: %r' % (exc,), xbmc.LOGDEBUG)
    return True


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

    def __init_subclass__(cls, **kwargs):
        """Gate every input callback a screen defines behind
        `input_suppressed()` in ONE place, instead of an `if` at the top of
        a dozen windows' `onClick()`/`onAction()` (see the phase notes
        above). A subclass that calls `super().onAction()` hits the already
        gated base method too - harmless."""
        super().__init_subclass__(**kwargs)
        for name in _GATED_CALLBACKS:
            callback = cls.__dict__.get(name)
            if callable(callback) and not getattr(callback, '_rivulet_input_gated', False):
                setattr(cls, name, _gate_input(callback, name))

    def doModal(self):
        # A Rivulet screen opening IS phase 3: it cannot be on screen while
        # the player owns the input. Ending the phase here is the safety
        # valve that means a missed `end_player_phase()` (an exception on the
        # handoff path) can never leave every screen deaf.
        end_player_phase()
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

    Marks the phase `player` first (`begin_player_phase()`): every callback
    still queued for a screen is ignored from here on, and a Videos-window
    refresh of the plugin root is a no-op, until `streamswindow` ends it.

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

    begin_player_phase()

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


def fetch_and_validate_addon(client, url):
    """Validate + fetch a Stremio addon manifest for `url`, the shared
    body of `AddonsWindow._install()`, `AddonCatalogWindow._configure()`
    and `_install_from_catalog()`, and `CatalogPickerWindow` - all three
    used to hand-roll the same `validate_transport_url()` ->
    `client.manifest()` -> `manifest.get('id')` sequence with their own
    copy-pasted `AddonError` handling and logging, drifting slightly
    between call sites.

    Returns `(manifest, transport_url, None)` on success - `transport_url`
    is the already-normalized URL `validate_transport_url()` produced, so
    callers that need it (to hand to `store.install_addon()`) don't have
    to re-run validation just to recover it, as `AddonsWindow._install()`/
    `AddonCatalogWindow._configure()` both used to. On failure, returns
    `(None, None, string_id)` where `string_id` is the strings.po id
    (30014, "Invalid addon manifest.") every call site already showed via
    `notify(L(string_id))` for an invalid URL, a fetch failure, or a
    manifest missing its required `id` field. Callers keep their own
    `notify()`/`log()` framing for the happy path; this only centralizes
    the validation and its logging. The manifest fetch runs under
    `run_cancellable()` (it had no spinner at all, so a dead host froze
    the screen for the request timeout with Back dropped); if the user
    backs out of it the result is `(None, None, CANCELLED)` - callers
    MUST check `error_id is CANCELLED` BEFORE treating a truthy id as a
    message to notify.
    """
    from lib.stremio.addons import (
        AddonError,
        addon_error_detail,
        safe_url_for_log,
        validate_transport_url,
    )
    from lib.ui.compat import L, log

    try:
        transport_url = validate_transport_url(url)
    except AddonError as exc:
        log('uicommon: invalid transport url %s: %s' % (safe_url_for_log(url or ''), exc), xbmc.LOGERROR)
        return None, None, 30014

    try:
        manifest = run_cancellable(lambda: client.manifest(transport_url), L(30033))
    except AddonError as exc:
        log('uicommon: manifest fetch failed for %s: %s' % (
            safe_url_for_log(transport_url), addon_error_detail(exc),
        ), xbmc.LOGERROR)
        return None, None, 30014

    if manifest is CANCELLED:
        return None, None, CANCELLED

    if not manifest or not manifest.get('id'):
        return None, None, 30014

    return manifest, transport_url, None
