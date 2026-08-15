"""Tests for lib.ui.dialogs: RivuletProgress/RivuletBusy/RivuletCountdown
(non-blocking, driven from a caller's own loop - see the module's own
docstring for why) and confirm()/choose() (blocking doModal() request/
response, mirroring xbmcgui.Dialog().yesno()/.select()).

Follows tests/test_streamswindow.py's `install_kodi_stubs`/factory-fixture
pattern: every test gets a fresh reload of lib.ui.compat/lib.ui.uicommon/
lib.ui.dialogs against fake xbmc*/xbmcgui modules, so no test can leak
state into another. `doModal()`-driven windows (confirm()/choose()) have
no real event loop in the fake, so a test simulates "the user clicked/
pressed a key" by monkeypatching the window class's own `doModal()` to
fire the click/action before returning - `_domodal_click`/`_domodal_action`
below are the two shapes every such test needs.
"""
import contextlib

import pytest

from tests.kodistubs import install_kodi_stubs

_RELOAD_MODULE_NAMES = ('lib.ui.compat', 'lib.ui.uicommon', 'lib.ui.dialogs')


@pytest.fixture
def load_dialogs():
    """Factory fixture: `load_dialogs()` installs fresh stubs reloading
    lib.ui.compat/lib.ui.uicommon/lib.ui.dialogs, returning a namespace
    with `.dialogs`, `.compat`, `.uicommon`, and `.env`. Every call is
    torn down automatically, in reverse order, at test end."""
    with contextlib.ExitStack() as stack:
        def _load(**kwargs):
            return stack.enter_context(install_kodi_stubs(reload=_RELOAD_MODULE_NAMES, **kwargs))

        yield _load


class _FakeAction:
    """Minimal `xbmcgui.Action`-shaped stand-in: only `getId()` is used
    by `onAction()`. Built directly rather than importing the fake
    `xbmcgui` module, matching test_streamswindow.py's own convention."""

    def __init__(self, action_id):
        self._id = action_id

    def getId(self):
        return self._id


def _domodal_click(control_id):
    """A `doModal()` replacement simulating a single click on
    `control_id` before the (fake, non-blocking) call returns.

    `onInit()` is fired first because that is what Kodi does - it creates
    the window's controls, then calls `onInit()`, then runs the modal
    loop. Skipping it here is precisely how a "Non-Existent Control"
    bug reached a real device: the dialogs used to populate their
    controls from the caller BEFORE `doModal()`, which cannot work, and
    a fake that never called `onInit()` could not tell the difference."""
    def _domodal(self):
        self.onInit()
        self.onClick(control_id)
    return _domodal


def _domodal_action(action_id):
    """A `doModal()` replacement simulating a single `onAction()` (e.g. a
    BACK_ACTIONS key) before the (fake, non-blocking) call returns.
    Fires `onInit()` first, for the reason `_domodal_click` explains."""
    def _domodal(self):
        self.onInit()
        self.onAction(_FakeAction(action_id))
    return _domodal


# ---------------------------------------------------------------------------
# RivuletProgress
# ---------------------------------------------------------------------------


def test_progress_update_with_only_percent_and_message(load_dialogs):
    ctx = load_dialogs()
    d = ctx.dialogs
    progress = d.RivuletProgress()
    progress.create('Preparing stream', 'Obsession')
    progress.update(28, 'Fetching torrent metadata\u2026')
    window = progress._window
    assert window.getControl(d.PROGRESS_HEADING).label == 'Preparing stream'
    assert window.getControl(d.PROGRESS_SUBHEADING).label == 'Obsession'
    assert window.getControl(d.PROGRESS_STATUS).label == 'Fetching torrent metadata\u2026'
    assert window.getControl(d.PROGRESS_PERCENT).label == '28%'
    assert window.getControl(d.PROGRESS_BAR).percent == 28
    # attempt/stats default to '' - not "None" - and stay cleared.
    assert window.getControl(d.PROGRESS_ATTEMPT).label == ''
    assert window.getControl(d.PROGRESS_STATS).label == ''


def test_progress_update_with_attempt_and_stats(load_dialogs):
    ctx = load_dialogs()
    d = ctx.dialogs
    progress = d.RivuletProgress()
    progress.create('Preparing stream', 'Obsession')
    progress.update(28, 'Fetching torrent metadata\u2026', attempt='Attempt 3 of 60', stats='348 KB/s \u2014 12 peers')
    window = progress._window
    assert window.getControl(d.PROGRESS_ATTEMPT).label == 'Attempt 3 of 60'
    assert window.getControl(d.PROGRESS_STATS).label == '348 KB/s \u2014 12 peers'


@pytest.mark.parametrize('attempt,stats', [('', ''), (None, None)], ids=['empty-strings', 'none'])
def test_progress_update_empty_attempt_and_stats_clears_rather_than_stringifies(load_dialogs, attempt, stats):
    ctx = load_dialogs()
    d = ctx.dialogs
    progress = d.RivuletProgress()
    progress.create('Preparing stream', 'Obsession')
    progress.update(28, 'Fetching torrent metadata\u2026', attempt='Attempt 1 of 60', stats='1 KB/s')
    progress.update(30, 'Fetching torrent metadata\u2026', attempt=attempt, stats=stats)
    window = progress._window
    assert window.getControl(d.PROGRESS_ATTEMPT).label == ''
    assert window.getControl(d.PROGRESS_STATS).label == ''


def test_progress_iscanceled_flips_true_after_a_back_action_and_stays_false_otherwise(load_dialogs):
    ctx = load_dialogs()
    d = ctx.dialogs
    progress = d.RivuletProgress()
    progress.create('Preparing stream')
    assert progress.iscanceled() is False
    progress._window.onAction(_FakeAction(1))  # not a back action
    assert progress.iscanceled() is False
    progress._window.onAction(_FakeAction(10))  # ACTION_PREVIOUS_MENU
    assert progress.iscanceled() is True


def test_progress_close_is_idempotent_and_safe_without_create(load_dialogs):
    ctx = load_dialogs()
    d = ctx.dialogs
    progress = d.RivuletProgress()
    progress.close()  # never created - must not raise
    progress.create('Preparing stream')
    window = progress._window
    progress.close()
    assert window.closed is True
    assert progress._window is None
    progress.close()  # already closed - must not raise or double-close
    assert progress._window is None


def test_progress_create_shows_without_blocking(load_dialogs):
    ctx = load_dialogs()
    d = ctx.dialogs
    progress = d.RivuletProgress()
    progress.create('Preparing stream')
    assert progress._window.shown is True
    assert progress._window.modal_calls == 0


# ---------------------------------------------------------------------------
# RivuletBusy
# ---------------------------------------------------------------------------


def test_busy_reveals_progress_bar_only_once_a_nonzero_percent_arrives(load_dialogs):
    ctx = load_dialogs()
    d = ctx.dialogs
    busy = d.RivuletBusy()
    busy.create('Loading', 'Fetching catalogs\u2026')
    bar = busy._window.getControl(d.BUSY_BAR)
    assert bar.visible is False  # indeterminate (marching squares) by default

    busy.update(0, 'Still fetching catalogs\u2026')
    assert bar.visible is False
    assert bar.percent is None

    busy.update(42, 'Almost there\u2026')
    assert bar.visible is True
    assert bar.percent == 42

    # Once revealed, stays revealed and keeps tracking percent.
    busy.update(70, 'Nearly done\u2026')
    assert bar.visible is True
    assert bar.percent == 70


def test_busy_update_with_only_percent_and_message(load_dialogs):
    ctx = load_dialogs()
    d = ctx.dialogs
    busy = d.RivuletBusy()
    busy.create('Loading')
    busy.update(0, 'Fetching catalogs\u2026')
    assert busy._window.getControl(d.BUSY_MESSAGE).label == 'Fetching catalogs\u2026'


def test_busy_iscanceled_and_close(load_dialogs):
    ctx = load_dialogs()
    d = ctx.dialogs
    busy = d.RivuletBusy()
    busy.close()  # never created
    busy.create('Loading')
    assert busy.iscanceled() is False
    busy._window.onAction(_FakeAction(92))  # ACTION_NAV_BACK
    assert busy.iscanceled() is True
    window = busy._window
    busy.close()
    busy.close()
    assert window.closed is True
    assert busy._window is None


# ---------------------------------------------------------------------------
# RivuletCountdown
# ---------------------------------------------------------------------------


class _ScriptedMonitor:
    """`xbmc.Monitor`-shaped fake: `waitForAbort()` returns `aborts[i]`
    (extended with False once exhausted) for the i-th tick."""

    def __init__(self, aborts=()):
        self._aborts = list(aborts)
        self.calls = 0

    def waitForAbort(self, timeout=None):
        result = self._aborts[self.calls] if self.calls < len(self._aborts) else False
        self.calls += 1
        return result


def test_countdown_runs_to_completion_and_closes(load_dialogs):
    ctx = load_dialogs()
    d = ctx.dialogs
    countdown = d.RivuletCountdown()
    monitor = _ScriptedMonitor()
    result = countdown.run('Next episode', 'Playing The Pilot', 3, monitor=monitor)
    assert result is True
    assert countdown._window is None  # closed itself on the way out


def test_countdown_message_may_be_a_per_tick_formatter(load_dialogs):
    """The design's copy embeds the countdown in the sentence ("Playing
    <title> in 8 s"), and a migration briefly lost it by passing the bare
    episode label instead - so pin that a callable is re-evaluated every
    tick and its result reaches COUNTDOWN_MESSAGE."""
    ctx = load_dialogs()
    d = ctx.dialogs
    seen = []

    def _message(remaining):
        seen.append(remaining)
        return 'Playing The Pilot in %d s' % remaining

    countdown = d.RivuletCountdown()
    window = {}
    real_open_window = d.open_window

    def _capturing_open_window(window_cls, xml_name, *args, **kwargs):
        window['w'] = real_open_window(window_cls, xml_name, *args, **kwargs)
        return window['w']

    d.open_window = _capturing_open_window
    try:
        assert countdown.run('Next episode', _message, 3, monitor=_ScriptedMonitor()) is True
    finally:
        d.open_window = real_open_window

    assert seen == [3, 2, 1]
    assert window['w'].getControl(d.COUNTDOWN_MESSAGE).label == 'Playing The Pilot in 1 s'
    assert window['w'].getControl(d.COUNTDOWN_REMAINING).label == '1 s'


def test_countdown_cancelled_via_back_action_returns_none(load_dialogs, monkeypatch):
    ctx = load_dialogs()
    d = ctx.dialogs
    real_open_window = d.open_window

    def _capturing_open_window(window_cls, xml_name, *args, **kwargs):
        window = real_open_window(window_cls, xml_name, *args, **kwargs)
        window._canceled = True  # simulate a back action before the first tick
        return window

    monkeypatch.setattr(d, 'open_window', _capturing_open_window)
    countdown = d.RivuletCountdown()
    result = countdown.run('Next episode', 'Playing The Pilot', 5, monitor=_ScriptedMonitor())
    assert result is None
    assert countdown._window is None  # closed itself on the way out


def test_countdown_skipped_via_select_action_returns_true_immediately(load_dialogs, monkeypatch):
    ctx = load_dialogs()
    d = ctx.dialogs

    real_open_window = d.open_window

    def _capturing_open_window(window_cls, xml_name, *args, **kwargs):
        window = real_open_window(window_cls, xml_name, *args, **kwargs)
        window._skipped = True  # simulate pressing OK before the first tick
        return window

    monkeypatch.setattr(d, 'open_window', _capturing_open_window)
    countdown = d.RivuletCountdown()
    result = countdown.run('Next episode', 'Playing The Pilot', 5, monitor=_ScriptedMonitor())
    assert result is True


def test_countdown_monitor_abort_returns_false(load_dialogs):
    ctx = load_dialogs()
    d = ctx.dialogs
    countdown = d.RivuletCountdown()
    monitor = _ScriptedMonitor(aborts=[True])
    result = countdown.run('Next episode', 'Playing The Pilot', 5, monitor=monitor)
    assert result is False


# ---------------------------------------------------------------------------
# confirm()
# ---------------------------------------------------------------------------


def test_confirm_returns_true_when_the_primary_button_is_clicked(load_dialogs, monkeypatch):
    ctx = load_dialogs()
    d = ctx.dialogs
    monkeypatch.setattr(d._ConfirmWindow, 'doModal', _domodal_click(d.CONFIRM_YES))
    result = d.confirm('Resume playback?', 'Resume from 00:12:34?', 'Resume', 'Start from beginning')
    assert result is True


def test_confirm_returns_false_when_the_secondary_button_is_clicked(load_dialogs, monkeypatch):
    ctx = load_dialogs()
    d = ctx.dialogs
    monkeypatch.setattr(d._ConfirmWindow, 'doModal', _domodal_click(d.CONFIRM_NO))
    result = d.confirm('Resume playback?', 'Resume from 00:12:34?', 'Resume', 'Start from beginning')
    assert result is False


def test_confirm_returns_false_on_back_action(load_dialogs, monkeypatch):
    ctx = load_dialogs()
    d = ctx.dialogs
    monkeypatch.setattr(d._ConfirmWindow, 'doModal', _domodal_action(9))
    result = d.confirm('Resume playback?', 'Resume from 00:12:34?', 'Resume', 'Start from beginning')
    assert result is False


def test_confirm_bolds_the_primary_label_only(load_dialogs, monkeypatch):
    ctx = load_dialogs()
    d = ctx.dialogs
    captured = {}

    def _domodal(self):
        self.onInit()  # Kodi creates controls, then calls onInit(), then loops
        captured['yes'] = self.getControl(d.CONFIRM_YES).label
        captured['no'] = self.getControl(d.CONFIRM_NO).label

    monkeypatch.setattr(d._ConfirmWindow, 'doModal', _domodal)
    d.confirm('Resume playback?', 'Resume from 00:12:34?', 'Resume', 'Start from beginning')
    assert captured['yes'] == '[B]Resume[/B]'
    assert captured['no'] == 'Start from beginning'


# ---------------------------------------------------------------------------
# choose()
# ---------------------------------------------------------------------------


def test_choose_returns_the_picked_index(load_dialogs, monkeypatch):
    ctx = load_dialogs()
    d = ctx.dialogs

    def _domodal(self):
        self.onInit()  # Kodi creates controls, then calls onInit(), then loops
        self.getControl(d.OPTIONLIST_LIST).selected_index = 1
        self.onClick(d.OPTIONLIST_LIST)

    monkeypatch.setattr(d._OptionListWindow, 'doModal', _domodal)
    rows = ['Vincent Okafor', ('Idris Farrow', 'Constable Mabel Thorne (voice)')]
    result = d.choose('Cast & crew', rows)
    assert result == 1


def test_choose_returns_negative_one_when_dismissed(load_dialogs, monkeypatch):
    ctx = load_dialogs()
    d = ctx.dialogs
    monkeypatch.setattr(d._OptionListWindow, 'doModal', _domodal_action(10))
    result = d.choose('Cast & crew', ['Row A', 'Row B'])
    assert result == -1


def test_choose_populates_two_line_rows_and_plain_string_rows(load_dialogs, monkeypatch):
    ctx = load_dialogs()
    d = ctx.dialogs
    captured = {}

    def _domodal(self):
        self.onInit()  # Kodi creates controls, then calls onInit(), then loops
        captured['items'] = list(self.getControl(d.OPTIONLIST_LIST).items)

    monkeypatch.setattr(d._OptionListWindow, 'doModal', _domodal)
    d.choose('Cast & crew', ['Plain row', ('Idris Farrow', 'Constable Mabel Thorne (voice)')])
    labels = [(item.getLabel(), item.label2) for item in captured['items']]
    assert labels == [('Plain row', ''), ('Idris Farrow', 'Constable Mabel Thorne (voice)')]


# ---------------------------------------------------------------------------
# WindowXML fake's getControl() - Non-Existent Control contract
# ---------------------------------------------------------------------------


def test_getcontrol_resolves_a_control_id_declared_by_the_window_xml(load_dialogs):
    """A control id ConfirmDialog.xml actually declares (see the
    <control id="..."> elements there) resolves, exactly like real
    Kodi - and memoises the same FakeWindowControl on repeat calls (see
    tests/kodistubs/modules.py's WindowXML.getControl())."""
    ctx = load_dialogs()
    d = ctx.dialogs
    import xbmcgui
    window = xbmcgui.WindowXML('ConfirmDialog.xml', '/addon/path', 'Default', '1080i')

    control = window.getControl(d.CONFIRM_YES)

    assert window.getControl(d.CONFIRM_YES) is control


def test_getcontrol_raises_non_existent_control_for_an_undeclared_id(load_dialogs):
    """A control id ConfirmDialog.xml does NOT declare raises RuntimeError
    the same way a real device does - the check that would have caught
    `RuntimeError: Non-Existent Control 30340/30341` before it shipped
    (see _ConfirmWindow/_OptionListWindow's onInit() docstrings above):
    ConfirmDialog/OptionListDialog wrote to controls before doModal() had
    loaded the window, and a defensive try/except silently swallowed it."""
    load_dialogs()  # installs the fresh xbmcgui stubs this asserts against
    import xbmcgui
    window = xbmcgui.WindowXML('ConfirmDialog.xml', '/addon/path', 'Default', '1080i')

    with pytest.raises(RuntimeError, match='Non-Existent Control'):
        window.getControl(99999)
