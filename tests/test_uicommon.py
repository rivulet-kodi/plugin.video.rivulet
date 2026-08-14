"""Tests for lib.ui.uicommon: the shared helpers every custom
`WindowXML` screen (`HomeWindow`, `CatalogPickerWindow`, the coverflow,
...) builds on - `BACK_ACTIONS`, `dismiss_busy_dialog()`, `addon_skin_path()`,
`open_window()`, `BaseWindow`, `ModalStackWindow`, and
`close_windows_for_playback()` - exercised against the shared fake
xbmc/xbmcgui stubs in tests/kodistubs (no real Kodi runtime, no network).

lib.ui.uicommon imports xbmc/xbmcgui at module scope (`class BaseWindow
(xbmcgui.WindowXML)`, `xbmc.executebuiltin(...)` inside
dismiss_busy_dialog()), so load_uicommon reloads it fresh each call
alongside lib.ui.compat (addon_skin_path()'s `ADDON`).

BaseWindow is also the shared base for HomeWindow/CatalogPickerWindow/
StreamsWindow/AddonsWindow/SearchWindow's back-navigation onAction() -
see each module's own docstring - but it is exercised directly here too,
the same way tests/test_infowindow.py drives ShowcaseWindow.onInit()/
onClick()/onAction() without a real modal loop.

`ModalStackWindow`/`close_windows_for_playback()` are the fix for a real
device bug: every Rivulet screen is an `xbmcgui.WindowXMLDialog`, and
Kodi routes ALL input to whichever dialog is topmost rather than to
`fullscreenvideo`, so playing a stream while any ancestor screen is
still open underneath left play/pause and the OSD dead until the user
backed all the way out. `close_windows_for_playback()` is exercised
directly against `_MODAL_WINDOW_STACK` with tiny local `_StackWindow`
fakes (only `.close()`/`._closed_for_playback` matter to it);
`ModalStackWindow.doModal()`'s push/pop-registry and reopen-after-
force-close loop is exercised through `BaseWindow` (the mixin's real
host) with the fake `xbmcgui.WindowXMLDialog.doModal()` monkeypatched
per test to script what a nested doModal()/onClick() callback did
during that call - new `xbmc.Monitor().abortRequested()` support in
tests/kodistubs backs the "Kodi is shutting down" case.
"""
import contextlib

import pytest

from tests.kodistubs import install_kodi_stubs

_RELOAD_MODULE_NAMES = ('lib.ui.compat', 'lib.ui.uicommon')


@pytest.fixture
def load_uicommon():
    """Factory fixture: `load_uicommon(addon_info=None)` installs fresh
    stubs (via tests.kodistubs.install_kodi_stubs) reloading lib.ui.compat/
    lib.ui.uicommon, and returns a namespace with `.uicommon`, `.compat`,
    and `.env`. Every call is torn down automatically, in reverse order,
    at test end.
    """
    with contextlib.ExitStack() as stack:
        def _load(addon_info=None):
            return stack.enter_context(install_kodi_stubs(
                reload=_RELOAD_MODULE_NAMES,
                addon_info=addon_info,
            ))

        yield _load


# ---------------------------------------------------------------------------
# setting_bool() / setting_int() -- thin ADDON-bound wrappers delegating to
# the pure lib.settings helpers also used by ServiceMonitor._refresh() in
# lib.service_runner (see tests/test_service_runner.py for the shared
# parsing behavior's own edge-case coverage); these prove the delegation
# itself reproduces the same missing/malformed/mixed-case/zero/negative/
# minimum-clamped results through compat's real ADDON instance.
# ---------------------------------------------------------------------------


def test_setting_bool_missing_key_returns_default(load_uicommon):
    ctx = load_uicommon()
    assert ctx.compat.setting_bool('missing', True) is True
    assert ctx.compat.setting_bool('missing', False) is False


def test_setting_bool_malformed_value_returns_default(load_uicommon):
    ctx = load_uicommon()
    ctx.compat.ADDON.settings['flag'] = 'not-a-bool'
    assert ctx.compat.setting_bool('flag', True) is True


@pytest.mark.parametrize('raw,expected', [
    ('TRUE', True), ('YES', True), ('On', True),
    ('FALSE', False), ('No', False), ('OFF', False),
])
def test_setting_bool_parses_mixed_case(load_uicommon, raw, expected):
    ctx = load_uicommon()
    ctx.compat.ADDON.settings['flag'] = raw
    assert ctx.compat.setting_bool('flag', not expected) is expected


def test_setting_int_malformed_value_returns_default(load_uicommon):
    ctx = load_uicommon()
    ctx.compat.ADDON.settings['n'] = 'nope'
    assert ctx.compat.setting_int('n', 7) == 7


def test_setting_int_zero_is_not_treated_as_missing(load_uicommon):
    ctx = load_uicommon()
    ctx.compat.ADDON.settings['n'] = '0'
    assert ctx.compat.setting_int('n', 99) == 0


def test_setting_int_negative_value_clamped_up_to_minimum(load_uicommon):
    ctx = load_uicommon()
    ctx.compat.ADDON.settings['n'] = '-5'
    assert ctx.compat.setting_int('n', 0, minimum=1) == 1


# ---------------------------------------------------------------------------
# dismiss_busy_dialog()
# ---------------------------------------------------------------------------


def test_dismiss_busy_dialog_closes_every_dialog(load_uicommon):
    ctx = load_uicommon()

    ctx.uicommon.dismiss_busy_dialog()

    assert ctx.env.executed_builtins == ['Dialog.Close(all, true)']


# ---------------------------------------------------------------------------
# busy_dialog()
# ---------------------------------------------------------------------------


def test_busy_dialog_creates_and_updates_on_enter_then_closes_on_normal_exit(load_uicommon):
    ctx = load_uicommon()

    with ctx.uicommon.busy_dialog('My Heading', 'my message'):
        assert ctx.env.dialog_created == [('My Heading', 'my message')]
        assert ctx.env.dialog_updates[0] == (0, 'my message')
        assert ctx.env.dialog_closed_count == 0

    assert ctx.env.dialog_closed_count == 1


def test_busy_dialog_defaults_message_to_empty_string(load_uicommon):
    ctx = load_uicommon()

    with ctx.uicommon.busy_dialog('My Heading'):
        pass

    assert ctx.env.dialog_created == [('My Heading', '')]


def test_busy_dialog_yields_the_dialog_for_progress_updates_and_cancellation(load_uicommon):
    ctx = load_uicommon()
    ctx.env.cancel = True

    with ctx.uicommon.busy_dialog('My Heading', 'my message') as dialog:
        dialog.update(42, 'progress')
        assert dialog.iscanceled() is True

    assert ctx.env.dialog_updates == [(0, 'my message'), (42, 'progress')]


def test_busy_dialog_closes_the_dialog_even_when_the_body_raises(load_uicommon):
    ctx = load_uicommon()

    class _MarkerError(Exception):
        pass

    with pytest.raises(_MarkerError):
        with ctx.uicommon.busy_dialog('My Heading', 'my message'):
            raise _MarkerError('boom')

    assert ctx.env.dialog_closed_count == 1


# ---------------------------------------------------------------------------
# addon_skin_path()
# ---------------------------------------------------------------------------


def test_addon_skin_path_returns_the_addons_own_install_path(load_uicommon):
    ctx = load_uicommon(addon_info={'path': 'special://home/addons/plugin.video.rivulet'})

    assert ctx.uicommon.addon_skin_path() == 'special://home/addons/plugin.video.rivulet'


# ---------------------------------------------------------------------------
# open_window()
# ---------------------------------------------------------------------------


def test_open_window_builds_against_the_skin_quadruple_and_forwards_extra_args(load_uicommon):
    ctx = load_uicommon(addon_info={'path': '/addon/path'})
    captured = {}

    class DummyWindow:
        def __init__(self, *args, **kwargs):
            captured['args'] = args
            captured['kwargs'] = kwargs

    result = ctx.uicommon.open_window(DummyWindow, 'Some.xml', 'extra-positional', flag=True)

    assert captured['args'] == ('Some.xml', '/addon/path', 'Default', '720p', 'extra-positional')
    assert captured['kwargs'] == {'flag': True}
    assert isinstance(result, DummyWindow)


def test_open_window_with_no_extra_args_passes_only_the_skin_quadruple(load_uicommon):
    ctx = load_uicommon(addon_info={'path': '/addon/path'})
    captured = {}

    class DummyWindow:
        def __init__(self, *args, **kwargs):
            captured['args'] = args
            captured['kwargs'] = kwargs

    ctx.uicommon.open_window(DummyWindow, 'Some.xml')

    assert captured['args'] == ('Some.xml', '/addon/path', 'Default', '720p')
    assert captured['kwargs'] == {}


# ---------------------------------------------------------------------------
# close_windows_for_playback() - force-closes every OTHER live screen so
# Kodi's player is the only modal thing left standing (see the module
# docstring). Exercised directly against `_MODAL_WINDOW_STACK` with tiny
# local `_StackWindow` fakes rather than real `BaseWindow` instances -
# only `.close()`/`._closed_for_playback` matter to this function.
# ---------------------------------------------------------------------------


class _StackWindow:
    """Minimal stand-in for a live `ModalStackWindow`-mixed screen:
    records `close()` calls into a shared `order` list (so a test can
    assert the closing order) and optionally raises from `close()`, to
    prove one broken screen can't block the rest of the teardown."""

    def __init__(self, name, order, raise_on_close=False):
        self.name = name
        self.order = order
        self.raise_on_close = raise_on_close
        self._closed_for_playback = False
        self.close_calls = 0

    def close(self):
        self.close_calls += 1
        self.order.append(self.name)
        if self.raise_on_close:
            raise RuntimeError('close failed: %s' % self.name)


def test_close_windows_for_playback_closes_every_window_but_excluded_innermost_first(load_uicommon):
    ctx = load_uicommon()
    order = []
    outer = _StackWindow('outer', order)
    middle = _StackWindow('middle', order)
    inner = _StackWindow('inner', order)
    ctx.uicommon._MODAL_WINDOW_STACK.extend([outer, middle, inner])

    ctx.uicommon.close_windows_for_playback(exclude=middle)

    assert order == ['inner', 'outer']  # innermost first, middle skipped entirely
    assert middle.close_calls == 0
    assert middle._closed_for_playback is False
    assert outer._closed_for_playback is True
    assert inner._closed_for_playback is True


def test_close_windows_for_playback_with_no_exclude_closes_the_whole_stack(load_uicommon):
    ctx = load_uicommon()
    order = []
    outer = _StackWindow('outer', order)
    inner = _StackWindow('inner', order)
    ctx.uicommon._MODAL_WINDOW_STACK.extend([outer, inner])

    ctx.uicommon.close_windows_for_playback()

    assert order == ['inner', 'outer']


def test_close_windows_for_playback_a_broken_close_does_not_block_the_rest(load_uicommon):
    ctx = load_uicommon()
    import xbmc
    order = []
    outer = _StackWindow('outer', order)
    middle = _StackWindow('middle', order, raise_on_close=True)
    ctx.uicommon._MODAL_WINDOW_STACK.extend([outer, middle])

    ctx.uicommon.close_windows_for_playback()

    assert order == ['middle', 'outer']  # middle's close() raised but outer still got closed
    assert outer.close_calls == 1
    assert middle._closed_for_playback is True  # marked even though close() blew up
    warnings = [msg for msg, lvl in ctx.env.log_calls if lvl == xbmc.LOGWARNING]
    assert len(warnings) == 1
    assert 'close failed: middle' in warnings[0]


# ---------------------------------------------------------------------------
# ModalStackWindow.doModal() - the registry push/pop and the "was I
# force-closed for playback" reopen loop. `BaseWindow` mixes this in, so
# it's exercised through `ctx.uicommon.BaseWindow` directly; the fake
# `xbmcgui.WindowXMLDialog.doModal()` (a no-op call counter) is
# monkeypatched per-test to script what happened during that call, the
# same way a real nested doModal()/onClick() callback would mutate
# `_closed_for_playback` out from under this frame.
# ---------------------------------------------------------------------------


def _make_stack_window(uicommon_mod):
    return uicommon_mod.BaseWindow('Some.xml', '/addon/path', 'Default', '720p')


def test_domodal_pushes_itself_then_pops_on_a_normal_return(load_uicommon, monkeypatch):
    ctx = load_uicommon()
    win = _make_stack_window(ctx.uicommon)
    stack_during_call = []
    monkeypatch.setattr(
        ctx.uicommon.xbmcgui.WindowXMLDialog, 'doModal',
        lambda self: stack_during_call.append(list(ctx.uicommon._MODAL_WINDOW_STACK)),
    )

    win.doModal()

    assert stack_during_call == [[win]]
    assert ctx.uicommon._MODAL_WINDOW_STACK == []


def test_domodal_pops_itself_even_when_the_call_raises(load_uicommon, monkeypatch):
    ctx = load_uicommon()
    win = _make_stack_window(ctx.uicommon)

    class _MarkerError(Exception):
        pass

    def fake_super_domodal(self):
        raise _MarkerError('boom')

    monkeypatch.setattr(ctx.uicommon.xbmcgui.WindowXMLDialog, 'doModal', fake_super_domodal)

    with pytest.raises(_MarkerError):
        win.doModal()

    assert ctx.uicommon._MODAL_WINDOW_STACK == []


def test_domodal_reopens_exactly_once_after_being_force_closed_then_stops(load_uicommon, monkeypatch):
    ctx = load_uicommon()
    win = _make_stack_window(ctx.uicommon)
    calls = []

    def fake_super_domodal(self):
        calls.append(1)
        if len(calls) == 1:
            self._closed_for_playback = True  # simulates close_windows_for_playback() mid-call

    monkeypatch.setattr(ctx.uicommon.xbmcgui.WindowXMLDialog, 'doModal', fake_super_domodal)

    win.doModal()

    assert len(calls) == 2  # the original call, plus exactly one reopen - never a third
    assert win._closed_for_playback is False
    assert ctx.uicommon._MODAL_WINDOW_STACK == []


def test_domodal_does_not_reopen_a_window_the_user_closed_normally(load_uicommon, monkeypatch):
    ctx = load_uicommon()
    win = _make_stack_window(ctx.uicommon)
    calls = []
    monkeypatch.setattr(ctx.uicommon.xbmcgui.WindowXMLDialog, 'doModal', lambda self: calls.append(1))

    win.doModal()

    assert calls == [1]  # never reopened - _closed_for_playback was never set


def test_domodal_does_not_reopen_when_kodi_is_shutting_down(load_uicommon, monkeypatch):
    ctx = load_uicommon()
    ctx.env.monitor_abort_requested = True
    win = _make_stack_window(ctx.uicommon)
    calls = []

    def fake_super_domodal(self):
        calls.append(1)
        self._closed_for_playback = True  # force-closed every time, but Kodi is exiting

    monkeypatch.setattr(ctx.uicommon.xbmcgui.WindowXMLDialog, 'doModal', fake_super_domodal)

    win.doModal()

    assert calls == [1]  # abortRequested() stopped the loop before a 2nd attempt
    assert win._closed_for_playback is True  # left set - nothing left to reopen into
    assert ctx.uicommon._MODAL_WINDOW_STACK == []

# ---------------------------------------------------------------------------
# BaseWindow.onAction() - the shared back-navigation contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize('action_id', [9, 10, 92], ids=['nav-back', 'previous-menu', 'backspace'])
def test_basewindow_onaction_back_actions_close_the_window(load_uicommon, action_id):
    ctx = load_uicommon()
    import xbmcgui
    win = ctx.uicommon.BaseWindow('Some.xml', '/addon/path', 'Default', '720p')

    win.onAction(xbmcgui.Action(action_id))

    assert win.closed is True


def test_basewindow_onaction_non_back_action_does_not_close(load_uicommon):
    ctx = load_uicommon()
    import xbmcgui
    win = ctx.uicommon.BaseWindow('Some.xml', '/addon/path', 'Default', '720p')

    win.onAction(xbmcgui.Action(1))  # ACTION_MOVE_LEFT-ish, not a back action

    assert win.closed is False


