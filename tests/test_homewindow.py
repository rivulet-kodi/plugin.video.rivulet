"""Tests for lib.ui.homewindow: HomeWindow, Rivulet's custom entry-point
screen replacing the classical root plugin directory, exercised against the
shared fake xbmc/xbmcgui stubs in tests/kodistubs (no real Kodi runtime, no
network).

lib.ui.homewindow imports xbmcgui and lib.ui.uicommon at module scope, so
load_homewindow reloads lib.ui.compat/lib.ui.uicommon/lib.ui.router/
lib.ui.homewindow fresh together. HomeWindow.onClick()'s 'discover'/'search'
handlers lazily `from lib.ui.catalogpicker import open_catalog_picker` /
`from lib.ui.searchwindow import open_search` at call time, so
lib.ui.catalogpicker/lib.ui.searchwindow are reloaded too (same reason
tests/test_views.py reloads lib.ui.infowindow: to get a handle - `ctx.
catalogpicker`/`ctx.searchwindow` - whose functions this file monkeypatches,
and to have install_kodi_stubs clean their sys.modules entries back up at
teardown so no later test file observes them bound to a dead test's fakes).

HomeWindow.onInit()/onClick()/onAction() are called directly here, never
through a real modal event loop, exactly like tests/test_infowindow.py drives
ShowcaseWindow: the fake WindowXMLDialog.doModal() is a no-op counter, and
getControl()/setFocusId() are plain in-memory fakes.

HomeWindow.xml's actual skin rendering is Kodi-skin-engine-only and is NOT,
and cannot be, exercised by this suite.
"""
import contextlib

import pytest

import lib.store as store_module
from tests.kodistubs import install_kodi_stubs

_RELOAD_MODULE_NAMES = (
    'lib.ui.compat', 'lib.ui.uicommon', 'lib.ui.router',
    'lib.ui.homewindow', 'lib.ui.catalogpicker', 'lib.ui.searchwindow',
)


class _FakeStore:
    """Fake `lib.store.Store`: only `get_auth()` matters to HomeWindow.onInit()
    (its truthiness decides whether the Library row is shown)."""

    def __init__(self, auth=None):
        self._auth = auth

    def get_auth(self):
        return self._auth


@pytest.fixture
def load_homewindow():
    """Factory fixture: `load_homewindow(addon_info=None, localized=None)`
    installs fresh stubs (via tests.kodistubs.install_kodi_stubs) reloading
    lib.ui.compat/lib.ui.uicommon/lib.ui.router/lib.ui.homewindow/
    lib.ui.catalogpicker/lib.ui.searchwindow, and returns a namespace with
    `.homewindow`, `.compat`, `.router`, `.catalogpicker`, `.searchwindow`,
    and `.env`. `localized` overrides FakeAddon's default 'STR<id>' string
    marker - needed for string id 30022 ("Logged in as %s"), which
    HomeWindow's status label formats with `%`, exactly like
    `lib.ui.views.addons()` does for its logout-row label.
    Every call is torn down automatically, in reverse order, at test end.
    """
    with contextlib.ExitStack() as stack:
        def _load(addon_info=None, localized=None):
            return stack.enter_context(install_kodi_stubs(
                reload=_RELOAD_MODULE_NAMES,
                addon_info=addon_info,
                localized=localized,
            ))

        yield _load


def _window_with_focused_action(homewindow_mod, action):
    """Build a fresh HomeWindow whose LIST control has one focused row
    carrying `action` as its 'action' Property - the shape onClick() reads,
    without needing a real onInit()/Store round-trip."""
    import xbmcgui
    win = homewindow_mod.HomeWindow('HomeWindow.xml', '/addon/path', 'Default', '720p')
    item = xbmcgui.ListItem('label')
    item.setProperty('action', action)
    win.getControl(homewindow_mod.LIST).addItems([item])
    return win


# ---------------------------------------------------------------------------
# _menu_items()
# ---------------------------------------------------------------------------


def test_menu_items_includes_library_when_show_library_true(load_homewindow):
    ctx = load_homewindow()

    items = ctx.homewindow._menu_items(True)

    assert [item.getProperty('action') for item in items] == [
        'discover', 'search', 'library', 'addons', 'settings',
    ]
    assert [item.getLabel() for item in items] == [
        'STR30000', 'STR30001', 'STR30002', 'STR30003', 'STR30004',
    ]
    assert [item.art['icon'] for item in items] == [
        ctx.compat.addon_media_path('discover.png'),
        ctx.compat.addon_media_path('search.png'),
        ctx.compat.addon_media_path('library.png'),
        ctx.compat.addon_media_path('addons.png'),
        ctx.compat.addon_media_path('settings.png'),
    ]
    assert [item.getProperty('subtitle') for item in items] == [
        'Browse catalogs from your installed addons',
        'Search across every installed addon',
        'Your saved titles',
        'Manage installed Stremio addons',
        'Configure Rivulet',
    ]


def test_menu_items_omits_library_when_show_library_false(load_homewindow):
    ctx = load_homewindow()

    items = ctx.homewindow._menu_items(False)

    assert [item.getProperty('action') for item in items] == ['discover', 'search', 'addons', 'settings']


# ---------------------------------------------------------------------------
# _status_text()
# ---------------------------------------------------------------------------


def test_status_text_reports_email_when_authenticated_with_email(load_homewindow):
    ctx = load_homewindow(localized={30022: 'Logged in as %s'})

    text = ctx.homewindow._status_text({'authKey': 'x', 'user': {'email': 'me@example.com', 'name': 'Me'}})

    assert text == 'Logged in as me@example.com'


def test_status_text_falls_back_to_name_when_email_is_absent(load_homewindow):
    ctx = load_homewindow(localized={30022: 'Logged in as %s'})

    text = ctx.homewindow._status_text({'authKey': 'x', 'user': {'name': 'Me'}})

    assert text == 'Logged in as Me'


def test_status_text_reports_not_logged_in_when_auth_is_none(load_homewindow):
    ctx = load_homewindow()

    text = ctx.homewindow._status_text(None)

    assert text == 'Not logged in'


# ---------------------------------------------------------------------------
# HomeWindow.onInit()
# ---------------------------------------------------------------------------


def test_oninit_shows_library_row_when_authenticated(load_homewindow, monkeypatch):
    ctx = load_homewindow(localized={30022: 'Logged in as %s'})
    monkeypatch.setattr(store_module, 'Store', lambda *a, **k: _FakeStore(auth={'authKey': 'x'}))
    win = ctx.homewindow.HomeWindow('HomeWindow.xml', '/addon/path', 'Default', '720p')

    win.onInit()

    actions = [item.getProperty('action') for item in win.getControl(ctx.homewindow.LIST).items]
    assert actions == ['discover', 'search', 'library', 'addons', 'settings']
    assert win.getControl(ctx.homewindow.BACKGROUND).image == ctx.compat.ADDON_FANART
    assert win.getControl(ctx.homewindow.STATUS_LABEL).label == 'Logged in as ?'
    assert win.getFocusId() == ctx.homewindow.LIST


def test_oninit_hides_library_row_when_not_authenticated(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    monkeypatch.setattr(store_module, 'Store', lambda *a, **k: _FakeStore(auth=None))
    win = ctx.homewindow.HomeWindow('HomeWindow.xml', '/addon/path', 'Default', '720p')

    win.onInit()

    actions = [item.getProperty('action') for item in win.getControl(ctx.homewindow.LIST).items]
    assert 'library' not in actions
    assert win.getControl(ctx.homewindow.STATUS_LABEL).label == 'Not logged in'


def test_oninit_sets_status_label_to_email_when_authenticated_with_email(load_homewindow, monkeypatch):
    ctx = load_homewindow(localized={30022: 'Logged in as %s'})
    auth = {'authKey': 'x', 'user': {'email': 'me@example.com', 'name': 'Me'}}
    monkeypatch.setattr(store_module, 'Store', lambda *a, **k: _FakeStore(auth=auth))
    win = ctx.homewindow.HomeWindow('HomeWindow.xml', '/addon/path', 'Default', '720p')

    win.onInit()

    assert win.getControl(ctx.homewindow.STATUS_LABEL).label == 'Logged in as me@example.com'


def test_oninit_sets_status_label_to_name_when_email_is_absent(load_homewindow, monkeypatch):
    ctx = load_homewindow(localized={30022: 'Logged in as %s'})
    auth = {'authKey': 'x', 'user': {'name': 'Me'}}
    monkeypatch.setattr(store_module, 'Store', lambda *a, **k: _FakeStore(auth=auth))
    win = ctx.homewindow.HomeWindow('HomeWindow.xml', '/addon/path', 'Default', '720p')

    win.onInit()

    assert win.getControl(ctx.homewindow.STATUS_LABEL).label == 'Logged in as Me'


# ---------------------------------------------------------------------------
# HomeWindow.onAction()
# ---------------------------------------------------------------------------


@pytest.mark.parametrize('action_id', [9, 10, 92], ids=['nav-back', 'previous-menu', 'backspace'])
def test_onaction_back_actions_close_the_window(load_homewindow, action_id):
    ctx = load_homewindow()
    import xbmcgui
    win = ctx.homewindow.HomeWindow('HomeWindow.xml', '/addon/path', 'Default', '720p')

    win.onAction(xbmcgui.Action(action_id))

    assert win.closed is True


def test_onaction_non_back_action_does_not_close(load_homewindow):
    ctx = load_homewindow()
    import xbmcgui
    win = ctx.homewindow.HomeWindow('HomeWindow.xml', '/addon/path', 'Default', '720p')

    win.onAction(xbmcgui.Action(1))

    assert win.closed is False


# ---------------------------------------------------------------------------
# HomeWindow.onClick() - dispatch to one of the module-level _open_*()
# ---------------------------------------------------------------------------


def test_onclick_ignores_control_ids_other_than_list(load_homewindow):
    ctx = load_homewindow()
    win = ctx.homewindow.HomeWindow('HomeWindow.xml', '/addon/path', 'Default', '720p')

    win.onClick(9999)

    assert win.closed is False


def test_onclick_list_with_no_focused_item_does_not_crash(load_homewindow):
    ctx = load_homewindow()
    win = ctx.homewindow.HomeWindow('HomeWindow.xml', '/addon/path', 'Default', '720p')

    win.onClick(ctx.homewindow.LIST)

    assert win.closed is False


def test_onclick_focused_item_with_unrecognized_action_does_not_crash_or_close(load_homewindow):
    ctx = load_homewindow()
    win = _window_with_focused_action(ctx.homewindow, 'not-a-real-action')

    win.onClick(ctx.homewindow.LIST)

    assert win.closed is False


def test_onclick_discover_closes_when_catalog_picker_returns_true(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    calls = []
    monkeypatch.setattr(ctx.catalogpicker, 'open_catalog_picker', lambda: (calls.append(1), True)[1])
    win = _window_with_focused_action(ctx.homewindow, 'discover')

    win.onClick(ctx.homewindow.LIST)

    assert calls == [1]
    assert win.closed is True


def test_onclick_discover_stays_open_when_catalog_picker_returns_false(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    monkeypatch.setattr(ctx.catalogpicker, 'open_catalog_picker', lambda: False)
    win = _window_with_focused_action(ctx.homewindow, 'discover')

    win.onClick(ctx.homewindow.LIST)

    assert win.closed is False


def test_onclick_search_closes_when_open_search_returns_true(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    monkeypatch.setattr(ctx.searchwindow, 'open_search', lambda: True)
    win = _window_with_focused_action(ctx.homewindow, 'search')

    win.onClick(ctx.homewindow.LIST)

    assert win.closed is True


def test_onclick_search_stays_open_when_open_search_returns_false(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    monkeypatch.setattr(ctx.searchwindow, 'open_search', lambda: False)
    win = _window_with_focused_action(ctx.homewindow, 'search')

    win.onClick(ctx.homewindow.LIST)

    assert win.closed is False


def test_onclick_library_always_closes_and_falls_back_to_classical_directory(load_homewindow):
    ctx = load_homewindow()
    ctx.router.BASE_URL = 'plugin://plugin.video.rivulet/'
    win = _window_with_focused_action(ctx.homewindow, 'library')

    win.onClick(ctx.homewindow.LIST)

    assert win.closed is True
    assert ctx.env.executed_builtins == [
        'ActivateWindow(Videos,plugin://plugin.video.rivulet/?action=library)'
    ]


def test_onclick_addons_always_closes_and_falls_back_to_classical_directory(load_homewindow):
    ctx = load_homewindow()
    ctx.router.BASE_URL = 'plugin://plugin.video.rivulet/'
    win = _window_with_focused_action(ctx.homewindow, 'addons')

    win.onClick(ctx.homewindow.LIST)

    assert win.closed is True
    assert ctx.env.executed_builtins == [
        'ActivateWindow(Videos,plugin://plugin.video.rivulet/?action=addons)'
    ]


def test_onclick_settings_opens_native_settings_without_closing(load_homewindow):
    ctx = load_homewindow()
    win = _window_with_focused_action(ctx.homewindow, 'settings')

    win.onClick(ctx.homewindow.LIST)

    assert ctx.env.opened_settings is True
    assert win.closed is False


# ---------------------------------------------------------------------------
# open_home()
# ---------------------------------------------------------------------------


def test_open_home_builds_the_window_against_the_skin_path_and_blocks_on_domodal(
    load_homewindow, monkeypatch,
):
    ctx = load_homewindow(addon_info={'path': '/addon/path'})
    captured = {}

    class RecordingWindow(ctx.homewindow.HomeWindow):
        def __init__(self, *args, **kwargs):
            captured['init_args'] = args
            super().__init__(*args, **kwargs)
            captured['instance'] = self

    monkeypatch.setattr(ctx.homewindow, 'HomeWindow', RecordingWindow)

    ctx.homewindow.open_home()

    assert captured['init_args'] == ('HomeWindow.xml', '/addon/path', 'Default', '720p')
    assert captured['instance'].modal_calls == 1


def test_open_home_closes_the_window_exactly_once_and_reraises_when_domodal_raises(
    load_homewindow, monkeypatch,
):
    ctx = load_homewindow(addon_info={'path': '/addon/path'})
    captured = {}

    class ExplodingWindow(ctx.homewindow.HomeWindow):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.close_calls = 0
            captured['window'] = self

        def close(self):
            self.close_calls += 1
            super().close()

        def doModal(self):
            # Stands in for a crash inside onInit() while the modal loop is
            # running - self.close() (the window's own, normal-path close)
            # never gets a chance to run.
            raise RuntimeError('onInit blew up')

    monkeypatch.setattr(ctx.homewindow, 'HomeWindow', ExplodingWindow)

    # default.py wraps open_home() itself and falls back to the classical
    # home directory on ANY exception - that contract requires the
    # exception to keep propagating unchanged, not be swallowed here.
    with pytest.raises(RuntimeError, match='onInit blew up'):
        ctx.homewindow.open_home()

    win = captured['window']
    assert win.close_calls == 1
    assert win.closed is True
