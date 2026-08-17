"""Tests for lib.ui.homewindow: HomeWindow, Rivulet's custom entry-point
screen, exercised against the
shared fake xbmc/xbmcgui stubs in tests/kodistubs (no real Kodi runtime, no
network).

lib.ui.homewindow imports xbmcgui and lib.ui.uicommon at module scope, so
load_homewindow reloads lib.ui.compat/lib.ui.uicommon/lib.ui.router/
lib.ui.homewindow fresh together. HomeWindow.onClick()'s 'discover'/
'search'/'mystuff'/'addons' handlers lazily `from lib.ui.catalogpicker
import open_catalog_picker` / `from lib.ui.searchwindow import
open_search` / `from lib.ui.mystuff import open_my_stuff` / `from
lib.ui.addonswindow import open_addons` at call time, so
lib.ui.catalogpicker/lib.ui.searchwindow/lib.ui.mystuff/
lib.ui.addonswindow are reloaded too (same reason tests/test_views.py
reloads lib.ui.infowindow: to get a handle - `ctx.catalogpicker`/`ctx.
searchwindow`/`ctx.mystuff`/`ctx.addonswindow` - whose functions
this file monkeypatches, and to have install_kodi_stubs clean their
sys.modules entries back up at teardown so no later test file observes
them bound to a dead test's fakes).

HomeWindow.onInit()/onClick()/onAction() are called directly here, never
through a real modal event loop, exactly like tests/test_infowindow.py drives
ShowcaseWindow: the fake WindowXML.doModal() is a no-op counter, and
getControl()/setFocusId() are plain in-memory fakes.

HomeWindow.xml's actual skin rendering is Kodi-skin-engine-only and is NOT,
and cannot be, exercised by this suite.
"""
import contextlib
import os

import pytest

from tests.kodistubs import install_kodi_stubs

#: The real addon directory, for the few assertions that must check what
#: actually ships rather than what the stubbed path helper returns.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_RELOAD_MODULE_NAMES = (
    'lib.ui.compat', 'lib.ui.uicommon', 'lib.ui.router', 'lib.ui.homewindow',
    'lib.ui.catalogpicker', 'lib.ui.searchwindow', 'lib.ui.mystuff', 'lib.ui.addonswindow',
)


#: Catalog descriptors covering every type row, for _menu_items()'s
#: auto-hide check (a row whose types nothing publishes is left out).
_ALL_TYPE_ADDONS = [{'transportUrl': 'https://a/manifest.json', 'manifest': {'catalogs': [
    {'type': 'movie', 'id': 'm'}, {'type': 'series', 'id': 's'}, {'type': 'anime', 'id': 'a'},
]}}]


class _FakeStore:
    """Fake `lib.store.Store` for HomeWindow.onInit(): `get_addons()`
    supplies the catalogs the type rows auto-hide against (defaulting to
    one of every type), and `get_auth()`/`get_progress_entries()`
    (defaulting to logged out with nothing cached) are what the real
    `lib.ui.mystuff.has_content()` reads - tests that care about the My
    Stuff row's gating monkeypatch `has_content()` itself instead of
    populating these."""

    def __init__(self, auth=None, addons=None, progress_entries=None):
        self._auth = auth
        self._addons = _ALL_TYPE_ADDONS if addons is None else addons
        self._progress_entries = [] if progress_entries is None else progress_entries

    def get_auth(self):
        return self._auth

    def get_addons(self):
        return self._addons

    def get_progress_entries(self):
        return self._progress_entries


class _FakeVersionStore:
    """Fake `lib.store.Store`: only `get_last_seen_version()`/
    `set_last_seen_version()` matter to `_notify_if_updated()`.
    `raise_on_get` stands in for a Store I/O failure (e.g. a corrupt or
    unwritable `last_version.json`), which `_notify_if_updated()` must
    swallow rather than let take down the home screen."""

    def __init__(self, last_seen=None, raise_on_get=False):
        self._last_seen = last_seen
        self._raise_on_get = raise_on_get
        self.set_calls = []

    def get_last_seen_version(self):
        if self._raise_on_get:
            raise RuntimeError('store unavailable')
        return self._last_seen

    def set_last_seen_version(self, version):
        self.set_calls.append(version)
        self._last_seen = version


@pytest.fixture
def load_homewindow():
    """Factory fixture: `load_homewindow(addon_info=None, localized=None)`
    installs fresh stubs (via tests.kodistubs.install_kodi_stubs) reloading
    lib.ui.compat/lib.ui.uicommon/lib.ui.router/lib.ui.homewindow/
    lib.ui.catalogpicker/lib.ui.searchwindow, and returns a namespace with
    `.homewindow`, `.compat`, `.router`, `.catalogpicker`, `.searchwindow`,
    and `.env`. `localized` overrides FakeAddon's default 'STR<id>' string
    marker - needed for string id 30022 ("Logged in as %s"), which
    HomeWindow's status label formats with `%`, the same shared string
    `lib.ui.views.login()` shows as a post-login notification.
    Every call is torn down automatically, in reverse order, at test end.
    """
    with contextlib.ExitStack() as stack:
        def _load(addon_info=None, localized=None, settings=None):
            return stack.enter_context(install_kodi_stubs(
                reload=_RELOAD_MODULE_NAMES,
                addon_info=addon_info,
                localized=localized,
                settings=settings,
            ))

        yield _load


def _window_with_focused_action(homewindow_mod, action):
    """Build a fresh HomeWindow whose LIST control has one focused row
    carrying `action` as its 'action' Property - the shape onClick() reads,
    without needing a real onInit()/Store round-trip."""
    import xbmcgui
    win = homewindow_mod.HomeWindow('HomeWindow.xml', '/addon/path', 'Default', '1080i')
    item = xbmcgui.ListItem('label')
    item.setProperty('action', action)
    win.getControl(homewindow_mod.LIST).addItems([item])
    return win


# ---------------------------------------------------------------------------
# _menu_items()
# ---------------------------------------------------------------------------


def test_menu_items_lists_every_row_with_label_and_icon(load_homewindow):
    ctx = load_homewindow()

    items = ctx.homewindow._menu_items(True, _ALL_TYPE_ADDONS)

    assert [item.getProperty('action') for item in items] == [
        'mystuff', 'movies', 'series', 'anime', 'search', 'addons', 'settings',
    ]
    assert [item.getLabel() for item in items] == [
        'STR30241', 'STR30213', 'STR30214', 'STR30215', 'STR30001', 'STR30003', 'STR30004',
    ]
    assert [item.art['icon'] for item in items] == [
        ctx.compat.addon_media_path('mystuff.png'),
        ctx.compat.addon_media_path('movies.png'),
        ctx.compat.addon_media_path('series.png'),
        ctx.compat.addon_media_path('anime.png'),
        ctx.compat.addon_media_path('search.png'),
        ctx.compat.addon_media_path('addons.png'),
        ctx.compat.addon_media_path('settings.png'),
    ]
    assert [item.getProperty('subtitle') for item in items] == [
        'STR30232', 'STR30216', 'STR30217', 'STR30218', 'STR30149', 'STR30151', 'STR30152',
    ]


def test_menu_items_omits_library_when_show_library_false(load_homewindow):
    ctx = load_homewindow()

    items = ctx.homewindow._menu_items(False, _ALL_TYPE_ADDONS)

    assert [item.getProperty('action') for item in items] == [
        'movies', 'series', 'anime', 'search', 'addons', 'settings',
    ]


def test_menu_items_hides_a_type_row_its_setting_turns_off(load_homewindow):
    ctx = load_homewindow(settings={'home_show_series': 'false'})

    items = ctx.homewindow._menu_items(False, _ALL_TYPE_ADDONS)

    actions = [item.getProperty('action') for item in items]
    assert 'series' not in actions
    assert 'movies' in actions and 'anime' in actions


def test_menu_items_hides_a_type_row_nothing_publishes(load_homewindow):
    # Only movie catalogs installed - Series and Anime would open empty.
    addons = [{'transportUrl': 'https://a/manifest.json',
               'manifest': {'catalogs': [{'type': 'movie', 'id': 'm'}]}}]
    ctx = load_homewindow()

    items = ctx.homewindow._menu_items(False, addons)

    assert [item.getProperty('action') for item in items] == [
        'movies', 'search', 'addons', 'settings',
    ]


def test_menu_items_groups_tv_catalogs_under_the_series_row(load_homewindow):
    # An addon publishing only "tv" catalogs still gets a Series row -
    # linear/live channels are television, not a row of their own.
    addons = [{'transportUrl': 'https://a/manifest.json',
               'manifest': {'catalogs': [{'type': 'tv', 'id': 'ch'}]}}]
    ctx = load_homewindow()

    items = ctx.homewindow._menu_items(False, addons)

    assert [item.getProperty('action') for item in items] == [
        'series', 'search', 'addons', 'settings',
    ]


def test_menu_items_with_no_addons_shows_no_type_rows(load_homewindow):
    ctx = load_homewindow()

    items = ctx.homewindow._menu_items(False, [])

    assert [item.getProperty('action') for item in items] == ['search', 'addons', 'settings']


def test_menu_items_classifies_dotted_anime_subtypes_into_anime_row(load_homewindow):
    # Stremio's dotted-subtype convention: 'anime.movie'/'anime.series'
    # specialize 'anime' and must still join the Anime row, not go unrouted.
    addons = [{'transportUrl': 'https://a/manifest.json',
               'manifest': {'catalogs': [{'type': 'anime.movie', 'id': 'm'},
                                          {'type': 'anime.series', 'id': 's'}]}}]
    ctx = load_homewindow()

    items = ctx.homewindow._menu_items(False, addons)

    assert [item.getProperty('action') for item in items] == [
        'anime', 'search', 'addons', 'settings',
    ]


def test_menu_items_type_matching_is_case_insensitive(load_homewindow):
    # A capitalized 'TV' must join Series exactly like lowercase 'tv' does.
    addons = [{'transportUrl': 'https://a/manifest.json',
               'manifest': {'catalogs': [{'type': 'TV', 'id': 'ch'}]}}]
    ctx = load_homewindow()

    items = ctx.homewindow._menu_items(False, addons)

    assert [item.getProperty('action') for item in items] == [
        'series', 'search', 'addons', 'settings',
    ]


def test_menu_items_shows_other_row_for_a_type_no_curated_row_claims(load_homewindow):
    # The real-world case: an addon publishing an arbitrary type like
    # "Porn" has no curated row and must land in the catch-all instead.
    addons = [{'transportUrl': 'https://a/manifest.json',
               'manifest': {'catalogs': [{'type': 'Porn', 'id': 'p'}]}}]
    ctx = load_homewindow()

    items = ctx.homewindow._menu_items(False, addons)

    assert [item.getProperty('action') for item in items] == [
        'other', 'search', 'addons', 'settings',
    ]
    other = items[0]
    assert other.getLabel() == 'STR30227'
    assert other.getProperty('subtitle') == 'STR30228'


def test_menu_items_omits_other_row_when_only_curated_types_are_installed(load_homewindow):
    # Cinemeta-only (movie/series/anime): the common case must not change -
    # no remainder exists, so 'other' stays out exactly like today.
    ctx = load_homewindow()

    items = ctx.homewindow._menu_items(True, _ALL_TYPE_ADDONS)

    assert [item.getProperty('action') for item in items] == [
        'mystuff', 'movies', 'series', 'anime', 'search', 'addons', 'settings',
    ]


def test_menu_items_hides_other_row_its_setting_turns_off(load_homewindow):
    addons = [{'transportUrl': 'https://a/manifest.json',
               'manifest': {'catalogs': [{'type': 'Porn', 'id': 'p'}]}}]
    ctx = load_homewindow(settings={'home_show_other': 'false'})

    items = ctx.homewindow._menu_items(False, addons)

    assert [item.getProperty('action') for item in items] == [
        'search', 'addons', 'settings',
    ]


# ---------------------------------------------------------------------------
# _status_text()
# ---------------------------------------------------------------------------


def test_status_text_reports_email_when_authenticated_with_email(load_homewindow):
    ctx = load_homewindow(localized={30022: 'Logged in as %s'})

    text = ctx.homewindow._status_text({'authKey': 'x', 'user': {'email': 'me@example.com', 'name': 'Me'}})

    assert text == '[COLOR FF38BDF8]\u25cf[/COLOR] Logged in as me@example.com'
    assert 'Logged in as me@example.com' in text


def test_status_text_falls_back_to_name_when_email_is_absent(load_homewindow):
    ctx = load_homewindow(localized={30022: 'Logged in as %s'})

    text = ctx.homewindow._status_text({'authKey': 'x', 'user': {'name': 'Me'}})

    assert text == '[COLOR FF38BDF8]\u25cf[/COLOR] Logged in as Me'


def test_status_text_reports_not_logged_in_when_auth_is_none(load_homewindow):
    ctx = load_homewindow()

    text = ctx.homewindow._status_text(None)

    assert text == '[COLOR 57EEF3F6]\u25cf[/COLOR] STR30190'
    assert 'STR30190' in text


# ---------------------------------------------------------------------------
# HomeWindow.onInit()
# ---------------------------------------------------------------------------


def test_oninit_shows_mystuff_row_when_authenticated(load_homewindow, monkeypatch):
    """Logging in is enough for the merged row on its own: the user may
    have a library worth opening even with an empty local progress cache
    (`lib.ui.mystuff.has_content()`)."""
    ctx = load_homewindow(localized={30022: 'Logged in as %s'})
    monkeypatch.setattr(ctx.homewindow, 'get_store', lambda: _FakeStore(auth={'authKey': 'x'}))
    win = ctx.homewindow.HomeWindow('HomeWindow.xml', '/addon/path', 'Default', '1080i')

    win.onInit()

    actions = [item.getProperty('action') for item in win.getControl(ctx.homewindow.LIST).items]
    assert actions == ['mystuff', 'movies', 'series', 'anime', 'search', 'addons', 'settings']
    assert win.getControl(ctx.homewindow.BACKGROUND).image == ctx.compat.ADDON_FANART
    assert win.getControl(ctx.homewindow.STATUS_LABEL).label == '[COLOR FF38BDF8]\u25cf[/COLOR] Logged in as ?'
    assert win.getFocusId() == ctx.homewindow.LIST


def test_oninit_hides_library_row_when_not_authenticated(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    monkeypatch.setattr(ctx.homewindow, 'get_store', lambda: _FakeStore(auth=None))
    win = ctx.homewindow.HomeWindow('HomeWindow.xml', '/addon/path', 'Default', '1080i')

    win.onInit()

    actions = [item.getProperty('action') for item in win.getControl(ctx.homewindow.LIST).items]
    assert 'library' not in actions
    assert win.getControl(ctx.homewindow.STATUS_LABEL).label == '[COLOR 57EEF3F6]\u25cf[/COLOR] STR30190'


def test_oninit_sets_status_label_to_email_when_authenticated_with_email(load_homewindow, monkeypatch):
    ctx = load_homewindow(localized={30022: 'Logged in as %s'})
    auth = {'authKey': 'x', 'user': {'email': 'me@example.com', 'name': 'Me'}}
    monkeypatch.setattr(ctx.homewindow, 'get_store', lambda: _FakeStore(auth=auth))
    win = ctx.homewindow.HomeWindow('HomeWindow.xml', '/addon/path', 'Default', '1080i')

    win.onInit()

    assert win.getControl(ctx.homewindow.STATUS_LABEL).label == '[COLOR FF38BDF8]\u25cf[/COLOR] Logged in as me@example.com'


def test_oninit_sets_status_label_to_name_when_email_is_absent(load_homewindow, monkeypatch):
    ctx = load_homewindow(localized={30022: 'Logged in as %s'})
    auth = {'authKey': 'x', 'user': {'name': 'Me'}}
    monkeypatch.setattr(ctx.homewindow, 'get_store', lambda: _FakeStore(auth=auth))
    win = ctx.homewindow.HomeWindow('HomeWindow.xml', '/addon/path', 'Default', '1080i')

    win.onInit()

    assert win.getControl(ctx.homewindow.STATUS_LABEL).label == '[COLOR FF38BDF8]\u25cf[/COLOR] Logged in as Me'


# ---------------------------------------------------------------------------
# HomeWindow.onAction()
# ---------------------------------------------------------------------------


@pytest.mark.parametrize('action_id', [9, 10, 92], ids=['nav-back', 'previous-menu', 'backspace'])
def test_onaction_back_actions_close_the_window(load_homewindow, action_id):
    ctx = load_homewindow()
    import xbmcgui
    win = ctx.homewindow.HomeWindow('HomeWindow.xml', '/addon/path', 'Default', '1080i')

    win.onAction(xbmcgui.Action(action_id))

    assert win.closed is True


def test_onaction_non_back_action_does_not_close(load_homewindow):
    ctx = load_homewindow()
    import xbmcgui
    win = ctx.homewindow.HomeWindow('HomeWindow.xml', '/addon/path', 'Default', '1080i')

    win.onAction(xbmcgui.Action(1))

    assert win.closed is False


# ---------------------------------------------------------------------------
# HomeWindow.onClick() - dispatch to one of the module-level _open_*()
# ---------------------------------------------------------------------------


def test_onclick_ignores_control_ids_other_than_list(load_homewindow):
    ctx = load_homewindow()
    win = ctx.homewindow.HomeWindow('HomeWindow.xml', '/addon/path', 'Default', '1080i')

    win.onClick(9999)

    assert win.closed is False


def test_onclick_list_with_no_focused_item_does_not_crash(load_homewindow):
    ctx = load_homewindow()
    win = ctx.homewindow.HomeWindow('HomeWindow.xml', '/addon/path', 'Default', '1080i')

    win.onClick(ctx.homewindow.LIST)

    assert win.closed is False


def test_onclick_focused_item_with_unrecognized_action_does_not_crash_or_close(load_homewindow):
    ctx = load_homewindow()
    win = _window_with_focused_action(ctx.homewindow, 'not-a-real-action')

    win.onClick(ctx.homewindow.LIST)

    assert win.closed is False


def test_onclick_type_row_closes_when_catalog_picker_returns_true(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    calls = []
    monkeypatch.setattr(
        ctx.catalogpicker, 'open_catalog_picker',
        lambda types=None, heading='': (calls.append((types, heading)), True)[1],
    )
    win = _window_with_focused_action(ctx.homewindow, 'movies')

    win.onClick(ctx.homewindow.LIST)

    # The picker is filtered to the row's own types and headed by its label.
    assert calls == [(ctx.homewindow.TYPE_ROW_TYPES['movies'], 'STR30213')]
    assert win.closed is True


def test_onclick_type_row_stays_open_when_catalog_picker_returns_false(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    monkeypatch.setattr(ctx.catalogpicker, 'open_catalog_picker', lambda types=None, heading='': False)
    win = _window_with_focused_action(ctx.homewindow, 'movies')

    win.onClick(ctx.homewindow.LIST)

    assert win.closed is False


def test_onclick_series_row_passes_its_own_types(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    calls = []
    monkeypatch.setattr(
        ctx.catalogpicker, 'open_catalog_picker',
        lambda types=None, heading='': (calls.append(types), False)[1],
    )
    win = _window_with_focused_action(ctx.homewindow, 'series')

    win.onClick(ctx.homewindow.LIST)

    assert calls == [ctx.homewindow.TYPE_ROW_TYPES['series']]
    assert 'tv' in calls[0]


def test_onclick_other_row_passes_only_the_unclaimed_remainder(load_homewindow, monkeypatch):
    # 'other' has no fixed type set - it must be computed live from what's
    # installed, and must NOT also carry movie/series/anime along with it.
    ctx = load_homewindow()
    addons = [{'transportUrl': 'https://a/manifest.json', 'manifest': {'catalogs': [
        {'type': 'movie', 'id': 'm'}, {'type': 'Porn', 'id': 'p'},
    ]}}]
    monkeypatch.setattr(ctx.homewindow, 'get_store', lambda: _FakeStore(addons=addons))
    calls = []
    monkeypatch.setattr(
        ctx.catalogpicker, 'open_catalog_picker',
        lambda types=None, heading='': (calls.append(types), False)[1],
    )
    win = _window_with_focused_action(ctx.homewindow, 'other')

    win.onClick(ctx.homewindow.LIST)

    assert calls == [{'porn'}]


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


def test_onclick_addons_never_closes_home(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    calls = []
    monkeypatch.setattr(ctx.addonswindow, 'open_addons', lambda: calls.append(1))
    win = _window_with_focused_action(ctx.homewindow, 'addons')

    win.onClick(ctx.homewindow.LIST)

    assert calls == [1]
    assert win.closed is False


def test_onclick_settings_opens_native_settings_without_closing(load_homewindow):
    ctx = load_homewindow()
    win = _window_with_focused_action(ctx.homewindow, 'settings')

    win.onClick(ctx.homewindow.LIST)

    assert ctx.env.opened_settings is True
    assert win.closed is False


# ---------------------------------------------------------------------------
# _notify_if_updated()
# ---------------------------------------------------------------------------


def test_notify_if_updated_first_run_records_version_and_notifies_nothing(load_homewindow):
    ctx = load_homewindow(addon_info={'version': '1.0.0'})
    store = _FakeVersionStore(last_seen=None)

    ctx.homewindow._notify_if_updated(store)

    assert store.set_calls == ['1.0.0']
    assert ctx.env.notifications == []


def test_notify_if_updated_changed_version_notifies_once_and_records(load_homewindow):
    ctx = load_homewindow(addon_info={'version': '1.1.0'}, localized={30184: 'Updated to version %s'})
    store = _FakeVersionStore(last_seen='1.0.0')

    ctx.homewindow._notify_if_updated(store)

    assert store.set_calls == ['1.1.0']
    assert len(ctx.env.notifications) == 1
    heading, message, icon, time_ms = ctx.env.notifications[0]
    assert message == 'Updated to version 1.1.0'


def test_notify_if_updated_unchanged_version_notifies_nothing(load_homewindow):
    ctx = load_homewindow(addon_info={'version': '1.0.0'})
    store = _FakeVersionStore(last_seen='1.0.0')

    ctx.homewindow._notify_if_updated(store)

    assert store.set_calls == []
    assert ctx.env.notifications == []


def test_notify_if_updated_swallows_store_exception(load_homewindow):
    ctx = load_homewindow(addon_info={'version': '1.0.0'})
    store = _FakeVersionStore(last_seen=None, raise_on_get=True)

    ctx.homewindow._notify_if_updated(store)  # must not raise

    assert ctx.env.notifications == []


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

    assert captured['init_args'] == ('HomeWindow.xml', '/addon/path', 'Default', '1080i')
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

    # default.py wraps open_home() itself and falls back to the recovery
    # home directory on ANY exception - that contract requires the
    # exception to keep propagating unchanged, not be swallowed here.
    with pytest.raises(RuntimeError, match='onInit blew up'):
        ctx.homewindow.open_home()

    win = captured['window']
    assert win.close_calls == 1
    assert win.closed is True


def test_open_home_calls_notify_if_updated_with_the_store(load_homewindow, monkeypatch):
    ctx = load_homewindow(addon_info={'path': '/addon/path'})
    store = _FakeVersionStore(last_seen='1.0.0')
    monkeypatch.setattr(ctx.homewindow, 'get_store', lambda: store)
    calls = []
    monkeypatch.setattr(ctx.homewindow, '_notify_if_updated', calls.append)

    ctx.homewindow.open_home()

    assert calls == [store]


def test_open_home_still_opens_the_window_when_the_store_raises(load_homewindow, monkeypatch):
    ctx = load_homewindow(addon_info={'path': '/addon/path'})
    monkeypatch.setattr(ctx.homewindow, 'get_store', lambda: _FakeVersionStore(raise_on_get=True))
    captured = {}

    class RecordingWindow(ctx.homewindow.HomeWindow):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            captured['instance'] = self

    monkeypatch.setattr(ctx.homewindow, 'HomeWindow', RecordingWindow)

    ctx.homewindow.open_home()

    assert captured['instance'].modal_calls == 1
    assert ctx.env.notifications == []


def test_every_menu_row_has_an_icon_shipped_for_it(load_homewindow):
    """`_menu_items()` derives each row's icon from its action name
    (`addon_media_path('%s.png' % action)`), so a row whose file is
    missing renders with an empty icon slot - silently, since Kodi just
    draws nothing. The `other` row shipped exactly that way: it was added
    without an `other.png`, and the row it replaced (`discover`) left its
    icon behind unreferenced.

    Kodi resolves these at draw time against the real addon directory,
    so this walks the repo rather than the stubbed path helper.
    """
    media = os.path.join(_REPO_ROOT, 'resources', 'media')
    actions = [action for _string_id, action in load_homewindow().homewindow._MENU]

    missing = [a for a in actions if not os.path.isfile(os.path.join(media, '%s.png' % a))]
    assert not missing, 'Home rows with no resources/media/<action>.png: %s' % missing

    unused = sorted(
        name for name in os.listdir(media)
        if name.endswith('.png') and name[:-len('.png')] not in actions
    )
    assert not unused, 'icons in resources/media nothing on Home draws: %s' % unused


# ---------------------------------------------------------------------------
# _menu_items() - "Continue watching" row
# ---------------------------------------------------------------------------


def test_menu_items_shows_mystuff_row_first_when_show_mystuff_true(load_homewindow):
    ctx = load_homewindow()

    items = ctx.homewindow._menu_items(True, _ALL_TYPE_ADDONS)

    assert [item.getProperty('action') for item in items] == [
        'mystuff', 'movies', 'series', 'anime', 'search', 'addons', 'settings',
    ]
    assert items[0].getLabel() == 'STR30241'
    assert items[0].art['icon'] == ctx.compat.addon_media_path('mystuff.png')
    assert items[0].getProperty('subtitle') == 'STR30232'


def test_menu_items_omits_mystuff_row_when_gate_false(load_homewindow):
    """The merged row replaced the separate Continue-watching and Library
    rows, so neither survives in the menu whatever the gate says."""
    ctx = load_homewindow()

    items = ctx.homewindow._menu_items(False, _ALL_TYPE_ADDONS)

    actions = [item.getProperty('action') for item in items]
    assert actions == ['movies', 'series', 'anime', 'search', 'addons', 'settings']
    assert 'continue' not in actions
    assert 'library' not in actions


# ---------------------------------------------------------------------------
# HomeWindow.onInit() - "My Stuff" row gating
# ---------------------------------------------------------------------------


def test_oninit_shows_mystuff_row_when_setting_on_and_has_content(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    monkeypatch.setattr(ctx.homewindow, 'get_store', lambda: _FakeStore())
    monkeypatch.setattr(ctx.mystuff, 'has_content', lambda store: True)
    win = ctx.homewindow.HomeWindow('HomeWindow.xml', '/addon/path', 'Default', '1080i')

    win.onInit()

    actions = [item.getProperty('action') for item in win.getControl(ctx.homewindow.LIST).items]
    assert actions[0] == 'mystuff'


def test_oninit_hides_mystuff_row_when_setting_off(load_homewindow, monkeypatch):
    ctx = load_homewindow(settings={'home_show_mystuff': 'false'})
    monkeypatch.setattr(ctx.homewindow, 'get_store', lambda: _FakeStore())
    monkeypatch.setattr(ctx.mystuff, 'has_content', lambda store: True)
    win = ctx.homewindow.HomeWindow('HomeWindow.xml', '/addon/path', 'Default', '1080i')

    win.onInit()

    actions = [item.getProperty('action') for item in win.getControl(ctx.homewindow.LIST).items]
    assert 'mystuff' not in actions


def test_oninit_hides_mystuff_row_when_no_content(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    monkeypatch.setattr(ctx.homewindow, 'get_store', lambda: _FakeStore())
    monkeypatch.setattr(ctx.mystuff, 'has_content', lambda store: False)
    win = ctx.homewindow.HomeWindow('HomeWindow.xml', '/addon/path', 'Default', '1080i')

    win.onInit()

    actions = [item.getProperty('action') for item in win.getControl(ctx.homewindow.LIST).items]
    assert 'mystuff' not in actions


# ---------------------------------------------------------------------------
# HomeWindow.onClick() - "My Stuff" row
# ---------------------------------------------------------------------------


def test_onclick_mystuff_closes_when_open_my_stuff_returns_true(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    monkeypatch.setattr(ctx.mystuff, 'open_my_stuff', lambda: True)
    win = _window_with_focused_action(ctx.homewindow, 'mystuff')

    win.onClick(ctx.homewindow.LIST)

    assert win.closed is True


def test_onclick_mystuff_stays_open_when_open_my_stuff_returns_false(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    monkeypatch.setattr(ctx.mystuff, 'open_my_stuff', lambda: False)
    win = _window_with_focused_action(ctx.homewindow, 'mystuff')

    win.onClick(ctx.homewindow.LIST)

    assert win.closed is False


# ---------------------------------------------------------------------------
# home_show_continue -> home_show_mystuff migration
# ---------------------------------------------------------------------------


def test_a_disabled_continue_row_stays_disabled_after_the_rename(load_homewindow):
    """The merged row took a new setting id, so without this anyone who had
    deliberately turned the old row OFF would silently get the new one back
    on."""
    ctx = load_homewindow(settings={'home_show_continue': 'false'})

    ctx.homewindow._migrate_mystuff_setting()

    assert ctx.compat.ADDON.getSetting('home_show_mystuff') == 'false'
    assert ctx.compat.ADDON.getSetting('home_show_continue') == ''


def test_an_enabled_continue_row_needs_no_migration(load_homewindow):
    """The new setting already defaults to on, so a true (or unset) old
    value is already correct and must not be written over."""
    ctx = load_homewindow(settings={'home_show_continue': 'true'})

    ctx.homewindow._migrate_mystuff_setting()

    assert ctx.compat.ADDON.getSetting('home_show_mystuff') != 'false'
    assert ctx.compat.ADDON.getSetting('home_show_continue') == ''


def test_migration_is_idempotent(load_homewindow):
    """Clearing the old key is what makes a second launch a no-op - it must
    not re-apply an old value over a choice the user has since changed."""
    ctx = load_homewindow(settings={'home_show_continue': 'false'})

    ctx.homewindow._migrate_mystuff_setting()
    ctx.compat.ADDON.setSetting('home_show_mystuff', 'true')   # user turns it back on
    ctx.homewindow._migrate_mystuff_setting()

    assert ctx.compat.ADDON.getSetting('home_show_mystuff') == 'true'


def test_migration_survives_a_broken_settings_write(load_homewindow, monkeypatch):
    """A cosmetic migration must never stop Home from opening."""
    ctx = load_homewindow(settings={'home_show_continue': 'false'})

    def _boom(*_args, **_kwargs):
        raise RuntimeError('settings unwritable')

    monkeypatch.setattr(ctx.compat.ADDON, 'setSetting', _boom)

    ctx.homewindow._migrate_mystuff_setting()   # must not raise
