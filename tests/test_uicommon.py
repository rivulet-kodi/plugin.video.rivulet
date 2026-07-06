"""Tests for lib.ui.uicommon: the shared helpers every custom
`WindowXMLDialog` screen (`HomeWindow`, `CatalogPickerWindow`, the coverflow,
...) builds on - `BACK_ACTIONS`, `dismiss_busy_dialog()`, `addon_skin_path()`,
`open_window()`, `BaseWindow`, and `fallback_to_classical()` - exercised
against the shared fake xbmc/xbmcgui stubs in tests/kodistubs (no real Kodi
runtime, no network).

lib.ui.uicommon imports xbmc/xbmcgui at module scope (`class BaseWindow
(xbmcgui.WindowXMLDialog)`, `xbmc.executebuiltin(...)` inside
dismiss_busy_dialog()/fallback_to_classical()), so load_uicommon reloads it
fresh each call alongside lib.ui.compat (addon_skin_path()'s `ADDON`) and
lib.ui.router (fallback_to_classical()'s lazy `from lib.ui import router`) -
the same trio tests/test_router.py itself relies on for router's own
BASE_URL-dependent behavior. Setting `ctx.router.BASE_URL` before calling
fallback_to_classical() mirrors tests/test_router.py's url_for() tests.

BaseWindow currently has no caller among the other three new custom-window
modules (each defines its own onAction() back-handling instead of
subclassing it - see the module docstring), so it is instantiated and
driven directly here, the same way tests/test_infowindow.py drives
ShowcaseWindow.onInit()/onClick()/onAction() without a real modal loop.
"""
import contextlib

import pytest

from tests.kodistubs import install_kodi_stubs

_RELOAD_MODULE_NAMES = ('lib.ui.compat', 'lib.ui.uicommon', 'lib.ui.router')


@pytest.fixture
def load_uicommon():
    """Factory fixture: `load_uicommon(addon_info=None)` installs fresh
    stubs (via tests.kodistubs.install_kodi_stubs) reloading lib.ui.compat/
    lib.ui.uicommon/lib.ui.router, and returns a namespace with
    `.uicommon`, `.compat`, `.router`, and `.env`. Every call is torn down
    automatically, in reverse order, at test end.
    """
    with contextlib.ExitStack() as stack:
        def _load(addon_info=None):
            return stack.enter_context(install_kodi_stubs(
                reload=_RELOAD_MODULE_NAMES,
                addon_info=addon_info,
            ))

        yield _load


# ---------------------------------------------------------------------------
# dismiss_busy_dialog()
# ---------------------------------------------------------------------------


def test_dismiss_busy_dialog_closes_every_dialog(load_uicommon):
    ctx = load_uicommon()

    ctx.uicommon.dismiss_busy_dialog()

    assert ctx.env.executed_builtins == ['Dialog.Close(all, true)']


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


# ---------------------------------------------------------------------------
# fallback_to_classical()
# ---------------------------------------------------------------------------


def test_fallback_to_classical_updates_the_container_with_an_action_only_url(load_uicommon):
    ctx = load_uicommon()
    ctx.router.BASE_URL = 'plugin://plugin.video.rivulet/'

    ctx.uicommon.fallback_to_classical('library')

    assert ctx.env.executed_builtins == [
        'Container.Update(plugin://plugin.video.rivulet/?action=library)'
    ]


def test_fallback_to_classical_forwards_params_into_the_url(load_uicommon):
    ctx = load_uicommon()
    ctx.router.BASE_URL = 'plugin://plugin.video.rivulet/'

    ctx.uicommon.fallback_to_classical('meta', type='movie', id='tt123')

    assert ctx.env.executed_builtins == [
        'Container.Update(plugin://plugin.video.rivulet/?action=meta&type=movie&id=tt123)'
    ]
