"""Rivulet-styled replacements for xbmcgui's native progress/busy/yesno/
select dialogs (see "Turn 2 — dialogs for 1a" of the UI design).

`RivuletProgress`/`RivuletBusy` are drop-in replacements for
`xbmcgui.DialogProgress`: same `create()`/`update()`/`iscanceled()`/
`close()` shape, so a caller's diff is one line. Like the real
`DialogProgress`, both are NON-BLOCKING - they `show()` a `WindowXMLDialog`
and return immediately, so the caller's own loop can keep polling
`update()`/`iscanceled()` while it does its own work (a network read, a
download chunk) on the SAME thread. `WindowXMLDialog.doModal()` BLOCKS
that thread until the window closes, which would freeze the very loop
that needs to keep ticking the dialog - see uicommon's module docstring
for the same reasoning applied to every OTHER Rivulet screen (which
choose `doModal()` deliberately, because they have nothing else to do
concurrently).

For the same reason, neither is a `ModalStackWindow`: they are
transient/self-closing (constructed, driven, closed by the SAME caller
that owns them - `lib.ui.player._resolve_playable_item`,
`lib.ui.router._download_server_binary` - never left open across a
`close_windows_for_playback()` boundary the way a real screen is). The
progress dialog is deliberately still open at the moment playback
starts (closed by its owner's `finally` once `xbmc.Player().play()` has
been handed the URL); registering it would make
`close_windows_for_playback()` force-close it too, or reopen it after
playback the way a real screen expects - neither is wanted here.

`RivuletCountdown` is different: its only caller
(`streamswindow._binge_countdown`) does nothing concurrent besides its
own tick sleep, so `RivuletCountdown.run()` owns the whole loop and
blocks for the countdown's duration, returning the same True/None/False
tri-state `_binge_countdown` already returns.

`confirm()`/`choose()` mirror `xbmcgui.Dialog().yesno()`/`.select()`:
request/response, so they use `doModal()` like every other Rivulet
screen, and their windows DO mix in `uicommon.BaseWindow` (back-action
handling + the modal stack) - they are not transient in the sense above.
"""
import contextlib

import xbmc
import xbmcgui

from lib.ui.compat import log
from lib.ui.uicommon import BACK_ACTIONS, BaseWindow, open_window

# ProgressDialog.xml
PROGRESS_HEADING = 30300
PROGRESS_SUBHEADING = 30301
PROGRESS_STATUS = 30302
PROGRESS_PERCENT = 30303
PROGRESS_BAR = 30304
PROGRESS_ATTEMPT = 30305
PROGRESS_STATS = 30306

# CountdownDialog.xml
COUNTDOWN_HEADING = 30310
COUNTDOWN_MESSAGE = 30311
COUNTDOWN_BAR = 30312
COUNTDOWN_REMAINING = 30313

# BusyDialog.xml
BUSY_HEADING = 30320
BUSY_MESSAGE = 30321
BUSY_BAR = 30322

# ConfirmDialog.xml
CONFIRM_HEADING = 30330
CONFIRM_BODY = 30331
CONFIRM_NO = 30332
CONFIRM_YES = 30333

# OptionListDialog.xml
OPTIONLIST_HEADING = 30340
OPTIONLIST_LIST = 30341
OPTIONLIST_SCROLLBAR = 30342

#: Kodi's OK/Select action id - what `onAction` receives for an OK/Enter
#: press that no focused control already consumed. The countdown dialog
#: has no button of its own (just the "BACK cancels · OK plays now"
#: hint), so this is the only way to observe "the user pressed OK".
_SELECT_ACTIONS = frozenset({7})


def _get_control(window, control_id):
    """`window.getControl(control_id)`, or None on a broken/partial skin
    (a missing id raises in real Kodi) - every call site below must
    tolerate this rather than crash whatever caller (often the playback
    resolve path) is driving the dialog."""
    try:
        return window.getControl(control_id)
    except Exception as exc:  # noqa: BLE001 - a broken/partial skin must never crash playback
        log('dialogs: getControl(%d) failed: %r' % (control_id, exc), xbmc.LOGWARNING)
        return None


def _set_label(window, control_id, text):
    """`getControl(control_id).setLabel(text)`, tolerating both a missing
    control and a `None` text (Kodi's setLabel('') is how the skin hides
    an optional row - see ProgressDialog.xml's `visible` bindings on
    30303/30305/30306)."""
    ctrl = _get_control(window, control_id)
    if ctrl is None:
        return
    try:
        ctrl.setLabel(text or '')
    except Exception as exc:  # noqa: BLE001 - a broken/partial skin must never crash playback
        log('dialogs: setLabel(%d) failed: %r' % (control_id, exc), xbmc.LOGWARNING)


def _set_progress(window, control_id, percent):
    """`getControl(control_id).setPercent(percent)`, tolerating a missing
    `type="progress"` control."""
    ctrl = _get_control(window, control_id)
    if ctrl is None:
        return
    try:
        ctrl.setPercent(percent)
    except Exception as exc:  # noqa: BLE001 - a broken/partial skin must never crash playback
        log('dialogs: setPercent(%d) failed: %r' % (control_id, exc), xbmc.LOGWARNING)

class _Panel:
    """Resolved controls for one dialog window, plus the last value
    written to each.

    `RivuletProgress.update()` is a hot path: `player._prebuffer_torrent`
    calls it once per HTTP chunk out of `server.iter_front()`, so a
    20 MB pre-buffer drives it hundreds to thousands of times. Doing
    `getControl()` per field per call meant five id lookups and five
    fresh Python wrapper objects per chunk, and five setter crossings
    into Kodi whether or not anything had actually changed.

    So controls are resolved once at `create()`, and every write is
    compared against the last one that succeeded. Most chunks change
    nothing a human can see: the integer percent moves ~60 times across
    the whole buffer, and the stats line is re-polled only once per
    attempt, so the common case is now zero calls into Kodi rather than
    five. `_last` is only updated on success, so a control that raised
    is retried on the next tick rather than being written off.
    """

    __slots__ = ('_controls', '_last')

    def __init__(self, window, control_ids):
        self._controls = {}
        self._last = {}
        for control_id in control_ids:
            control = _get_control(window, control_id)
            if control is not None:
                self._controls[control_id] = control

    def label(self, control_id, text):
        text = text or ''
        if self._last.get(control_id) == text:
            return
        control = self._controls.get(control_id)
        if control is None:
            return
        try:
            control.setLabel(text)
        except Exception as exc:  # noqa: BLE001 - a broken/partial skin must never crash playback
            log('dialogs: setLabel(%d) failed: %r' % (control_id, exc), xbmc.LOGWARNING)
        else:
            self._last[control_id] = text

    def percent(self, control_id, value):
        value = int(value or 0)
        if self._last.get(control_id) == value:
            return
        control = self._controls.get(control_id)
        if control is None:
            return
        try:
            control.setPercent(value)
        except Exception as exc:  # noqa: BLE001 - a broken/partial skin must never crash playback
            log('dialogs: setPercent(%d) failed: %r' % (control_id, exc), xbmc.LOGWARNING)
        else:
            self._last[control_id] = value

    def visible(self, control_id, flag):
        key = (control_id, 'visible')
        if self._last.get(key) is flag:
            return
        control = self._controls.get(control_id)
        if control is None:
            return
        try:
            control.setVisible(flag)
        except Exception as exc:  # noqa: BLE001 - a broken/partial skin must never crash playback
            log('dialogs: setVisible(%d) failed: %r' % (control_id, exc), xbmc.LOGWARNING)
        else:
            self._last[key] = flag


class _TransientDialog(xbmcgui.WindowXMLDialog):
    """Shared base for `RivuletProgress`/`RivuletBusy`'s window: plain
    `WindowXMLDialog` (deliberately NOT a `ModalStackWindow` - see the
    module docstring), tracking a back action as "cancelled" for
    `iscanceled()` the same way `uicommon.BaseWindow` does for a real
    `doModal()` screen."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._canceled = False

    def onAction(self, action):
        if action.getId() in BACK_ACTIONS:
            self._canceled = True

    def iscanceled(self):
        return self._canceled


class RivuletProgress:
    """Drop-in replacement for `xbmcgui.DialogProgress` - see the module
    docstring for why it is non-blocking. `create()`/`update()` clear an
    empty `attempt`/`stats` to `''` rather than the caller's own text: a
    `None` or `''` value hides that row (ProgressDialog.xml gates 30303/
    30305/30306's visibility on their own label being empty), never
    prints the literal string "None"."""

    _CONTROLS = (
        PROGRESS_HEADING, PROGRESS_SUBHEADING, PROGRESS_STATUS,
        PROGRESS_PERCENT, PROGRESS_BAR, PROGRESS_ATTEMPT, PROGRESS_STATS,
    )

    def __init__(self):
        self._window = None
        self._panel = None
        self._percent = None

    def create(self, heading, message=''):
        self._window = open_window(_TransientDialog, 'ProgressDialog.xml')
        self._window.show()
        self._panel = _Panel(self._window, self._CONTROLS)
        self._percent = None
        self._panel.label(PROGRESS_HEADING, heading)
        self._panel.label(PROGRESS_SUBHEADING, message)
        self._panel.label(PROGRESS_STATUS, '')
        self._panel.label(PROGRESS_PERCENT, '')
        self._panel.label(PROGRESS_ATTEMPT, '')
        self._panel.label(PROGRESS_STATS, '')
        self._panel.percent(PROGRESS_BAR, 0)

    def update(self, percent, message='', attempt='', stats=''):
        if self._panel is None:
            return
        # The percent readout is the one field that needs formatting, so
        # it is also the one worth not formatting when it has not moved -
        # see _Panel for why this runs per downloaded chunk.
        percent = int(percent or 0)
        if percent != self._percent:
            self._percent = percent
            self._panel.label(PROGRESS_PERCENT, '%d%%' % percent)
            self._panel.percent(PROGRESS_BAR, percent)
        self._panel.label(PROGRESS_STATUS, message)
        self._panel.label(PROGRESS_ATTEMPT, attempt)
        self._panel.label(PROGRESS_STATS, stats)

    def iscanceled(self):
        return self._window.iscanceled() if self._window is not None else False

    def close(self):
        if self._window is None:
            return
        with contextlib.suppress(Exception):
            self._window.close()
        self._window = None
        self._panel = None


class RivuletBusy:
    """Drop-in replacement for `xbmcgui.DialogProgress` used as an
    indeterminate spinner - see the module docstring for why it is
    non-blocking. `BusyDialog.xml`'s marching-squares activity indicator
    is gated (in the skin) on 30322's own visibility, so revealing the
    real progress bar is the only thing `update()` needs to do once a
    genuine percent arrives; it never goes back to indeterminate once
    revealed."""

    _CONTROLS = (BUSY_HEADING, BUSY_MESSAGE, BUSY_BAR)

    def __init__(self):
        self._window = None
        self._panel = None

    def create(self, heading, message=''):
        self._window = open_window(_TransientDialog, 'BusyDialog.xml')
        self._window.show()
        self._panel = _Panel(self._window, self._CONTROLS)
        self._panel.label(BUSY_HEADING, heading)
        self._panel.label(BUSY_MESSAGE, message)
        # BusyDialog.xml deliberately carries no static <visible> tag on
        # the bar - a hardcoded false would outrank setVisible() on real
        # Kodi - so the indeterminate default is established here.
        self._panel.visible(BUSY_BAR, False)

    def update(self, percent, message='', attempt='', stats=''):
        # attempt/stats accepted only for drop-in parity with
        # RivuletProgress/DialogProgress - BusyDialog.xml has no controls
        # for them.
        if self._panel is None:
            return
        self._panel.label(BUSY_MESSAGE, message)
        if percent and percent > 0:
            self._panel.visible(BUSY_BAR, True)
            self._panel.percent(BUSY_BAR, percent)

    def iscanceled(self):
        return self._window.iscanceled() if self._window is not None else False

    def close(self):
        if self._window is None:
            return
        with contextlib.suppress(Exception):
            self._window.close()
        self._window = None
        self._panel = None


class _CountdownWindow(xbmcgui.WindowXMLDialog):
    """`RivuletCountdown`'s window: like `_TransientDialog`, plain
    `WindowXMLDialog` with no modal-stack registration, plus a second
    flag for the OK/Select "play now" affordance the countdown design
    adds (`_SELECT_ACTIONS`) - there is no button control to hang an
    `onClick()` off, so this is caught in `onAction` instead."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._canceled = False
        self._skipped = False

    def onAction(self, action):
        action_id = action.getId()
        if action_id in BACK_ACTIONS:
            self._canceled = True
        elif action_id in _SELECT_ACTIONS:
            self._skipped = True

    def iscanceled(self):
        return self._canceled

    def isskipped(self):
        return self._skipped


class RivuletCountdown:
    """Cancellable/skippable "next episode in N s" countdown for
    `streamswindow._binge_countdown()`. Unlike `RivuletProgress`/
    `RivuletBusy`, its only caller has nothing else to interleave beyond
    its own per-second sleep, so `run()` owns that whole loop and BLOCKS
    for the countdown's duration - see the module docstring."""

    _CONTROLS = (COUNTDOWN_HEADING, COUNTDOWN_MESSAGE, COUNTDOWN_BAR, COUNTDOWN_REMAINING)

    def __init__(self):
        self._window = None
        self._panel = None

    def run(self, heading, message, seconds, monitor=None):
        """Ticks `seconds` down to 0, one second at a time, setting
        `heading`/`message` and a `remaining` Mono26 readout
        (`'<N> s'`) each tick. Returns True once the countdown runs out
        OR the user presses OK/Select to skip it (CountdownDialog.xml's
        "OK plays now" hint), None if the user backs out instead (a
        "not now", matching `_binge_countdown`'s existing contract), or
        False the instant `monitor.waitForAbort()` fires (Kodi shutting
        down - same convention as every other abort check in
        streamswindow.py). `monitor` defaults to a fresh `xbmc.Monitor()`
        for real callers; tests inject their own.

        `message` is either a plain string, or a callable taking the
        remaining seconds and returning the line to show. The callable
        form exists because the design's own copy embeds the countdown
        in the sentence ("Playing <title> in 8 s"), which no static
        string can express - and keeping the formatting in the caller
        keeps this module free of any particular strings.po id."""
        monitor = monitor or xbmc.Monitor()
        formatter = message if callable(message) else (lambda remaining: message)
        self._window = open_window(_CountdownWindow, 'CountdownDialog.xml')
        self._window.show()
        self._panel = _Panel(self._window, self._CONTROLS)
        self._panel.label(COUNTDOWN_HEADING, heading)
        try:
            for remaining in range(seconds, 0, -1):
                if self._window.iscanceled():
                    return None
                if self._window.isskipped():
                    return True
                # _Panel skips a write whose value has not changed, so a
                # static message costs one setLabel for the whole run.
                self._panel.label(COUNTDOWN_MESSAGE, formatter(remaining))
                self._panel.percent(COUNTDOWN_BAR, int((seconds - remaining) * 100 / seconds))
                self._panel.label(COUNTDOWN_REMAINING, '%d s' % remaining)
                if monitor.waitForAbort(1.0):
                    return False
            return True
        finally:
            self.close()

    def close(self):
        if self._window is None:
            return
        with contextlib.suppress(Exception):
            self._window.close()
        self._window = None
        self._panel = None


class _ConfirmWindow(BaseWindow):
    """`confirm()`'s window: a real `doModal()` screen (see the module
    docstring), so it gets `uicommon.BaseWindow`'s back-action handling
    and modal-stack registration like every other Rivulet screen.
    `self.result` defaults False so backing out behaves exactly like
    `xbmcgui.Dialog().yesno()`'s own cancel-is-declined contract."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.result = False
        self.heading = ''
        self.body = ''
        self.yeslabel = ''
        self.nolabel = ''

    def onInit(self):
        # Kodi does not create a window's controls until it loads the
        # window, which happens inside doModal() - so touching them from
        # the caller beforehand raises "Non-Existent Control" and, thanks
        # to the defensive getControl() wrapper, silently renders an
        # empty dialog. Observed on a real device. Populate here, the
        # same way every other Rivulet screen does.
        _set_label(self, CONFIRM_HEADING, self.heading)
        _set_label(self, CONFIRM_BODY, self.body)
        _set_label(self, CONFIRM_NO, self.nolabel)
        _set_label(self, CONFIRM_YES, '[B]%s[/B]' % self.yeslabel)
        with contextlib.suppress(Exception):
            self.setFocusId(CONFIRM_YES)

    def onClick(self, control_id):
        if control_id == CONFIRM_YES:
            self.result = True
            self.close()
        elif control_id == CONFIRM_NO:
            self.result = False
            self.close()


def confirm(heading, body, yeslabel, nolabel):
    """Mirrors `xbmcgui.Dialog().yesno(heading, message, yeslabel=,
    nolabel=)`: True if the primary (right/focused) button was picked,
    False for the secondary button OR backing out.

    The primary button's `[B]...[/B]` wrap is deliberate, not
    decorative: ConfirmDialog.xml's button controls can't make their
    label bold only while focused (no conditional-on-focus font
    weight), so the "focused button reads bold" look the design draws
    has to come from the label text itself - see OtherSkins' skin
    handoff. The secondary button stays plain.
    """
    window = open_window(_ConfirmWindow, 'ConfirmDialog.xml')
    window.heading = heading
    window.body = body
    window.yeslabel = yeslabel
    window.nolabel = nolabel
    window.doModal()
    return window.result


class _OptionListWindow(BaseWindow):
    """`choose()`'s window - see `_ConfirmWindow` for why this is a
    `doModal()`/`BaseWindow` screen rather than a transient one.
    `self.result` defaults -1 so backing out matches
    `xbmcgui.Dialog().select()`'s own cancelled-is-negative-one
    contract."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.result = -1
        self.heading = ''
        self.rows = ()

    def onInit(self):
        # Populated here, not from choose(), for the same reason
        # _ConfirmWindow is: a window's controls do not exist until Kodi
        # loads it inside doModal(). Doing it early raised
        # "Non-Existent Control 30340/30341" on a real device and left
        # the list silently empty.
        _set_label(self, OPTIONLIST_HEADING, self.heading)
        items = []
        for row in self.rows:
            if isinstance(row, str):
                items.append(xbmcgui.ListItem(label=row))
            else:
                label, sublabel = row
                items.append(xbmcgui.ListItem(label=label, label2=sublabel or ''))
        ctrl = _get_control(self, OPTIONLIST_LIST)
        if ctrl is None:
            return
        with contextlib.suppress(Exception):
            ctrl.reset()
        with contextlib.suppress(Exception):
            ctrl.addItems(items)
        with contextlib.suppress(Exception):
            self.setFocusId(OPTIONLIST_LIST)

    def onClick(self, control_id):
        if control_id != OPTIONLIST_LIST:
            return
        ctrl = _get_control(self, OPTIONLIST_LIST)
        if ctrl is None:
            return
        try:
            position = ctrl.getSelectedPosition()
        except Exception as exc:  # noqa: BLE001 - a broken/partial skin must never crash playback
            log('dialogs: OPTIONLIST_LIST getSelectedPosition failed: %r' % (exc,), xbmc.LOGWARNING)
            return
        if position is not None and position >= 0:
            self.result = position
            self.close()


def choose(heading, rows):
    """Mirrors `xbmcgui.Dialog().select(heading, rows)`: returns the
    picked row's index, or -1 if dismissed. Each of `rows` is either a
    plain label string, or a `(label, sublabel)` pair for the two-line
    row OptionListDialog.xml draws (see the design's "Cast & crew"
    frame)."""
    window = open_window(_OptionListWindow, 'OptionListDialog.xml')
    window.heading = heading
    window.rows = list(rows)
    window.doModal()
    return window.result
