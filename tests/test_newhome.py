# SPDX-FileCopyrightText: 2026 M0Rf30
# SPDX-License-Identifier: MIT

"""Drive the single Home's callbacks against isolated Kodi stubs.

The fake validates control ids against NewHomeWindow.xml. Child-window,
store and shared-helper modules reload together so no later test sees
modules bound to a previous test's fakes. Actual Kodi rendering remains
a device check; these tests verify rows, focus, properties and handoffs.
"""
import contextlib
import os
import threading
import weakref

import pytest

from tests.homehooks import join_library_meta, join_shelf_extends
from tests.kodistubs import install_kodi_stubs

#: The real addon directory, for the few assertions that must check what
#: actually ships rather than what the stubbed path helper returns.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_RELOAD_MODULE_NAMES = (
    'lib.ui.compat', 'lib.ui.uicommon', 'lib.ui.router', 'lib.ui.homewindow',
    'lib.ui.catalogpicker', 'lib.ui.searchwindow', 'lib.ui.newsearch', 'lib.ui.mystuff', 'lib.ui.addonswindow',
    'lib.ui.detailwindow', 'lib.ui.newlook', 'lib.ui.newhome',
    'lib.ui.dialogs', 'lib.ui.dependencies', 'lib.ui.libraryaction', 'lib.ui.homelists',
    'lib.ui.player', 'lib.ui.streamswindow', 'lib.ui.views',
    'lib.ui.catalogpages', 'lib.ui.newsearch',
)


#: Catalog descriptors covering every type row, for _menu_items()'s
#: auto-hide check (a row whose types nothing publishes is left out).
_ALL_TYPE_ADDONS = [{'transportUrl': 'https://a/manifest.json', 'manifest': {'catalogs': [
    {'type': 'movie', 'id': 'm'}, {'type': 'series', 'id': 's'}, {'type': 'anime', 'id': 'a'},
]}}]


class _FakeStore:
    """Fake `lib.store.Store` for HomeWindow.onInit(): `get_enabled_addons()`
    supplies the catalogs the type rows auto-hide against (defaulting to
    one of every type), and `get_auth()`/`get_progress_entries()`
    (defaulting to logged out with nothing cached) feed the personal
    rows. Tests supply the library read explicitly when signed in;
    `library_cache` stands in for the service's `library.json`."""

    def __init__(self, auth=None, addons=None, progress_entries=None, library_cache=None):
        self._auth = auth
        self._addons = _ALL_TYPE_ADDONS if addons is None else addons
        self._progress_entries = [] if progress_entries is None else progress_entries
        self._library_cache = [] if library_cache is None else library_cache

    def get_library_cache(self):
        return self._library_cache

    def get_home_layout(self):
        from lib.homelayout import default_layout
        return default_layout()

    def get_auth(self):
        return self._auth

    def get_addons(self):
        return self._addons

    def get_enabled_addons(self):
        return [a for a in self._addons if not (a.get('flags') or {}).get('disabled')]

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
    lib.ui.catalogpicker/lib.ui.newsearch, and returns a namespace with
    `.homewindow`, `.compat`, `.router`, `.catalogpicker`, `.newsearch`,
    and `.env`. `localized` overrides FakeAddon's default 'STR<id>' string
    marker - needed for string id 30022 ("Logged in as %s"), which
    HomeWindow's status label formats with `%`, the same shared string
    `lib.ui.views.login()` shows as a post-login notification.
    Every call is torn down automatically, in reverse order, at test end.
    """
    with contextlib.ExitStack() as stack:
        def _load(addon_info=None, localized=None, settings=None):
            ctx = stack.enter_context(install_kodi_stubs(
                reload=_RELOAD_MODULE_NAMES,
                addon_info=addon_info,
                localized=localized,
                settings=settings,
            ))
            ctx.homewindow = ctx.newhome
            windows = weakref.WeakSet()
            home_stop = ctx.newhome._home_stop

            def track(window):
                windows.add(window)
                return home_stop(window)

            ctx.newhome._home_stop = track

            def close_sessions():
                for window in list(windows):
                    window.close()
                    thread = getattr(window, '_library_meta_thread', None)
                    if thread is not None:
                        thread.join(1)
                    join_shelf_extends(window)

            stack.callback(close_sessions)
            return ctx

        yield _load


def _window_with_focused_action(homewindow_mod, action, control_id=None):
    """Build a fresh HomeWindow whose browse list has one focused row
    carrying `action` as its 'action' Property - the shape onClick() reads,
    without needing a real onInit()/Store round-trip."""
    import xbmcgui
    win = homewindow_mod.NewHomeWindow('NewHomeWindow.xml', '/addon/path', 'Default', '1080i')
    item = xbmcgui.ListItem('label')
    item.setProperty('action', action)
    control_id = homewindow_mod.BROWSE_LIST if control_id is None else control_id
    win.getControl(control_id).addItems([item])
    return win


def _patch_library(monkeypatch, ctx, entries=None):
    """Home's signed-in paint reads the account library. The real call
    opens a socket the suite forbids; tests pass the records they care
    about and never touch the network."""
    monkeypatch.setattr(
        ctx.mystuff, '_read_library_entries', lambda store, _entries=list(entries or []): _entries,
    )
    # A real fetch opens the addon client. These tests assert the row,
    # not Cinemeta. The one that does is explicit about the answer.
    monkeypatch.setattr(ctx.homewindow, '_fetch_library_meta', lambda stype, sid, stop=None: None)


# ---------------------------------------------------------------------------
# _menu_items()
# ---------------------------------------------------------------------------
























# ---------------------------------------------------------------------------
# _status_text()
# ---------------------------------------------------------------------------


def test_status_text_says_signed_in_without_the_address(load_homewindow):
    """#19: Home is a shared TV. The address is not on the screen; it is
    in Settings > Account."""
    ctx = load_homewindow(localized={30488: 'Signed in'})

    text = ctx.homewindow._status_text({'authKey': 'x', 'user': {'email': 'me@example.com', 'name': 'Me'}})

    assert text == '[COLOR FFD6D6D6]\u25cf[/COLOR] Signed in'
    assert 'me@example.com' not in text
    assert 'Me' not in text.replace('[COLOR', '')




def test_status_text_reports_not_logged_in_when_auth_is_none(load_homewindow):
    ctx = load_homewindow()

    text = ctx.homewindow._status_text(None)

    assert text == '[COLOR 57EEF3F6]\u25cf[/COLOR] STR30190'
    assert 'STR30190' in text


# ---------------------------------------------------------------------------
# HomeWindow.onInit()
# ---------------------------------------------------------------------------




def test_library_focus_fetches_meta_and_paints_the_backdrop(load_homewindow, monkeypatch):
    """The library record is a poster. On focus, Cinemeta (the meta
    addon) supplies the backdrop and the description the other rows
    already show. The fetch does not block the window opening."""
    import time

    ctx = load_homewindow()
    import xbmcgui
    _patch_library(monkeypatch, ctx, [{
        '_id': 'tt9', 'name': 'Saved', 'type': 'movie', 'poster': 's', 'removed': False,
    }])
    started = threading.Event()
    release = threading.Event()

    def _meta(stype, sid, stop=None):
        assert (stype, sid) == ('movie', 'tt9')
        started.set()
        release.wait(2)
        return {
            'id': 'tt9', 'type': 'movie', 'name': 'Saved',
            'poster': 's', 'background': 'wide.jpg',
            'description': 'A desert planet.', 'year': '2024',
        }

    monkeypatch.setattr(ctx.homewindow, '_fetch_library_meta', _meta)
    monkeypatch.setattr(ctx.homewindow, 'get_store', lambda: _FakeStore(auth={'authKey': 'x'}))
    win = ctx.homewindow.NewHomeWindow('NewHomeWindow.xml', '/addon/path', 'Default', '1080i')

    opened = time.time()
    win.onInit()
    ctx.homewindow._switch_tab(win, 'mystuff')
    ctx.homewindow._focus_first(win)
    ctx.homewindow.paint_hero(win)
    assert time.time() - opened < 0.5
    assert started.wait(0.5)
    # Still the poster, covering the frame, while the request is in flight.
    assert win.getProperty('HeroFill') == '1'
    assert win.getControl(ctx.homewindow.BACKGROUND).image == 's'
    release.set()
    join_library_meta(win)
    win.onAction(xbmcgui.Action(999))

    assert win.getControl(ctx.homewindow.BACKGROUND).image == 'wide.jpg'
    assert win.getProperty('HeroFill') == ''
    assert win.getControl(ctx.homewindow.HERO_FILL).image == ''
    assert win.getControl(ctx.homewindow.HERO_TITLE).label == 'Saved'
    assert 'desert planet' in win.getControl(ctx.homewindow.HERO_PLOT).text
    assert win.getControl(30026).visible is True
    item = win.getControl(ctx.homewindow.LIBRARY_SHELF).items[0]
    assert item.getProperty('needs_meta') == ''
    assert item.getProperty('fanart') == 'wide.jpg'


def test_library_focus_keeps_a_full_bleed_poster_when_meta_has_no_backdrop(load_homewindow, monkeypatch):
    """No backdrop in the answer: the poster still covers the hero, and
    the description and the ratings show on the bar."""
    ctx = load_homewindow()
    import xbmcgui
    _patch_library(monkeypatch, ctx, [{
        '_id': 'tt9', 'name': 'Saved', 'type': 'movie', 'poster': 's', 'removed': False,
    }])

    def _meta(stype, sid, stop=None):
        return {
            'id': 'tt9', 'type': 'movie', 'name': 'Saved', 'poster': 's',
            'description': 'Only a poster.', 'year': '2021',
        }

    calls = []

    def _counting(stype, sid, stop=None):
        calls.append(sid)
        return _meta(stype, sid)

    monkeypatch.setattr(ctx.homewindow, '_fetch_library_meta', _counting)
    monkeypatch.setattr(ctx.homewindow, 'get_store', lambda: _FakeStore(auth={'authKey': 'x'}))
    win = ctx.homewindow.NewHomeWindow('NewHomeWindow.xml', '/addon/path', 'Default', '1080i')
    win.onInit()
    ctx.homewindow._switch_tab(win, 'mystuff')
    ctx.homewindow._focus_first(win)
    ctx.homewindow.paint_hero(win)
    join_library_meta(win)
    win.onAction(xbmcgui.Action(999))
    win.onAction(xbmcgui.Action(999))

    assert calls == ['tt9']
    assert win.getProperty('HeroFill') == '1'
    assert win.getControl(ctx.homewindow.HERO_FILL).image == 's'
    assert 'Only a poster' in win.getControl(ctx.homewindow.HERO_PLOT).text


def test_oninit_hides_library_row_when_not_authenticated(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    monkeypatch.setattr(ctx.homewindow, 'get_store', lambda: _FakeStore(auth=None))
    win = ctx.homewindow.NewHomeWindow('NewHomeWindow.xml', '/addon/path', 'Default', '1080i')

    win.onInit()

    assert win.getProperty('HomeSignedOut') == '1'
    actions = [item.getProperty('action') for item in win.getControl(ctx.homewindow.BROWSE_LIST).items]
    assert 'library' not in actions
    assert win.getControl(ctx.homewindow.STATUS_LABEL).label == '[COLOR 57EEF3F6]\u25cf[/COLOR] STR30190'


def test_oninit_status_label_says_signed_in(load_homewindow, monkeypatch):
    ctx = load_homewindow(localized={30488: 'Signed in'})
    auth = {'authKey': 'x', 'user': {'email': 'me@example.com', 'name': 'Me'}}
    _patch_library(monkeypatch, ctx)
    monkeypatch.setattr(ctx.homewindow, 'get_store', lambda: _FakeStore(auth=auth))
    win = ctx.homewindow.NewHomeWindow('NewHomeWindow.xml', '/addon/path', 'Default', '1080i')

    win.onInit()

    assert win.getControl(ctx.homewindow.STATUS_LABEL).label == '[COLOR FFD6D6D6]\u25cf[/COLOR] Signed in'


def test_oninit_status_label_says_signed_in_with_only_a_name(load_homewindow, monkeypatch):
    ctx = load_homewindow(localized={30488: 'Signed in'})
    auth = {'authKey': 'x', 'user': {'name': 'Me'}}
    _patch_library(monkeypatch, ctx)
    monkeypatch.setattr(ctx.homewindow, 'get_store', lambda: _FakeStore(auth=auth))
    win = ctx.homewindow.NewHomeWindow('NewHomeWindow.xml', '/addon/path', 'Default', '1080i')

    win.onInit()

    assert win.getControl(ctx.homewindow.STATUS_LABEL).label == '[COLOR FFD6D6D6]\u25cf[/COLOR] Signed in'


def test_oninit_omits_type_row_supplied_only_by_a_disabled_addon(load_homewindow, monkeypatch):
    """A catalog type published only by a disabled add-on must not get a
    row: onInit() must fan out through get_enabled_addons(), not
    get_addons() (which would include it)."""
    ctx = load_homewindow()
    addons = [
        {'transportUrl': 'https://a/manifest.json',
         'manifest': {'catalogs': [{'type': 'movie', 'id': 'm'}]}},
        {'transportUrl': 'https://b/manifest.json', 'flags': {'disabled': True},
         'manifest': {'catalogs': [{'type': 'anime', 'id': 'a'}]}},
    ]
    monkeypatch.setattr(ctx.homewindow, 'get_store', lambda: _FakeStore(addons=addons))
    win = ctx.homewindow.NewHomeWindow('NewHomeWindow.xml', '/addon/path', 'Default', '1080i')

    win.onInit()

    actions = [
        item.getProperty('action')
        for item in win.getControl(ctx.homewindow.BROWSE_LIST).items
    ]
    assert 'anime' not in actions
    assert 'movies' in actions


# ---------------------------------------------------------------------------
# HomeWindow.onAction()
# ---------------------------------------------------------------------------




def test_onaction_non_back_action_does_not_close(load_homewindow):
    ctx = load_homewindow()
    import xbmcgui
    win = ctx.homewindow.NewHomeWindow('NewHomeWindow.xml', '/addon/path', 'Default', '1080i')

    win.onAction(xbmcgui.Action(1))

    assert win.closed is False


# ---------------------------------------------------------------------------
# HomeWindow.onClick() - dispatch to one of the module-level _open_*()
# ---------------------------------------------------------------------------


def test_onclick_ignores_control_ids_other_than_list(load_homewindow):
    ctx = load_homewindow()
    win = ctx.homewindow.NewHomeWindow('NewHomeWindow.xml', '/addon/path', 'Default', '1080i')

    win.onClick(9999)

    assert win.closed is False


def test_onclick_list_with_no_focused_item_does_not_crash(load_homewindow):
    ctx = load_homewindow()
    win = ctx.homewindow.NewHomeWindow('NewHomeWindow.xml', '/addon/path', 'Default', '1080i')

    win.onClick(ctx.homewindow.BROWSE_LIST)

    assert win.closed is False






















def test_onclick_search_button_opens_search(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    monkeypatch.setattr(ctx.newsearch, 'open_search', lambda: True)
    win = ctx.homewindow.NewHomeWindow('NewHomeWindow.xml', '/addon/path', 'Default', '1080i')

    win.onClick(ctx.homewindow.BTN_SEARCH)

    assert win.closed is True




# ---------------------------------------------------------------------------
# _notify_if_updated()
# ---------------------------------------------------------------------------










# ---------------------------------------------------------------------------
# open_home()
# ---------------------------------------------------------------------------
















# ---------------------------------------------------------------------------
# _menu_items() - "Continue watching" row
# ---------------------------------------------------------------------------








def test_continue_row_includes_in_progress_library_state(load_homewindow, monkeypatch):
    """A title watched in the Stremio app has no local progress.json row.
    Its libraryItem state is still in progress, so it belongs on Continue.
    A removed-and-not-temp tombstone does not."""
    ctx = load_homewindow()
    library = [
        {
            '_id': 'tt3', 'name': 'App', 'type': 'movie', 'poster': 'a',
            'removed': True, 'temp': True,
            'state': {'timeOffset': 40000, 'duration': 100000},
        },
        {
            '_id': 'tt4', 'name': 'Gone', 'type': 'movie', 'poster': 'g',
            'removed': True, 'temp': False,
            'state': {'timeOffset': 40000, 'duration': 100000},
        },
    ]
    _patch_library(monkeypatch, ctx, library)
    monkeypatch.setattr(
        ctx.homewindow, 'get_store', lambda: _FakeStore(auth={'authKey': 'k'}),
    )
    win = ctx.homewindow.NewHomeWindow('NewHomeWindow.xml', '/addon/path', 'Default', '1080i')

    win.onInit()

    continue_ids = [item.getProperty('sid') for item in win.getControl(ctx.homewindow.CONTINUE_SHELF).items]
    library_ids = [item.getProperty('sid') for item in win.getControl(ctx.homewindow.LIBRARY_SHELF).items]
    assert continue_ids == ['tt3']
    assert 'tt4' not in continue_ids
    assert 'tt3' not in library_ids
    assert 'tt4' not in library_ids


def test_onclick_continue_poster_opens_detail(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    opened = []
    monkeypatch.setattr(
        ctx.detailwindow, 'open_detail',
        lambda ctype, sid: opened.append((ctype, sid)) or True,
    )
    import xbmcgui
    win = ctx.homewindow.NewHomeWindow('NewHomeWindow.xml', '/addon/path', 'Default', '1080i')
    item = xbmcgui.ListItem('Dune')
    item.setProperty('sid', 'tt1')
    item.setProperty('stype', 'movie')
    win.getControl(ctx.homewindow.CONTINUE_SHELF).addItems([item])

    win.onClick(ctx.homewindow.CONTINUE_SHELF)

    assert opened == [('movie', 'tt1')]
    assert win.closed is True




# ---------------------------------------------------------------------------
# home_show_continue -> home_show_mystuff migration
# ---------------------------------------------------------------------------










def test_onclick_library_signin_poster_opens_login(load_homewindow, monkeypatch):
    """Rivulet's auth.json is empty on a fresh profile even when the
    Stremio app is signed in. The Library row's Sign in cell opens the
    existing login dialog."""
    ctx = load_homewindow()
    calls = []
    monkeypatch.setattr(ctx.homewindow, 'get_store', lambda: _FakeStore(auth=None))

    import lib.ui.views as views
    monkeypatch.setattr(views, 'sign_in', lambda: calls.append('login'))
    # Home never ends a plugin handle (#22): login() would.
    monkeypatch.setattr(views, 'login', lambda: calls.append('handle'))
    import xbmcgui
    win = ctx.homewindow.NewHomeWindow('NewHomeWindow.xml', '/addon/path', 'Default', '1080i')
    item = xbmcgui.ListItem('Sign in')
    item.setProperty('action', 'signin')
    win.getControl(ctx.homewindow.LIBRARY_SHELF).addItems([item])

    win.onClick(ctx.homewindow.LIBRARY_SHELF)

    assert calls == ['login']
    assert win.closed is False
    assert ctx.env.opened_settings is False




def test_onaction_paints_the_hero_from_the_focused_poster(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    monkeypatch.setattr(ctx.homewindow, 'get_store', lambda: _FakeStore(auth=None))
    import xbmcgui
    win = ctx.homewindow.NewHomeWindow('NewHomeWindow.xml', '/addon/path', 'Default', '1080i')
    item = xbmcgui.ListItem('Dune')
    item.setProperties({
        'hero_title': 'Dune',
        'hero_meta': '2021',
        'hero_plot': 'A boy and a desert.',
        'fanart': 'https://img/dune.jpg',
        'sid': 'tt1',
        'stype': 'movie',
    })
    win.getControl(ctx.homewindow.CONTINUE_SHELF).addItems([item])
    win.setFocusId(ctx.homewindow.CONTINUE_SHELF)

    win.onAction(xbmcgui.Action(1))

    assert win.closed is False
    assert win.getControl(ctx.homewindow.HERO_TITLE).label == 'Dune'
    assert win.getControl(ctx.homewindow.HERO_META).text == '2021'
    assert win.getControl(ctx.homewindow.HERO_PLOT).text == 'A boy and a desert.'
    assert win.getControl(ctx.homewindow.BACKGROUND).image == 'https://img/dune.jpg'


def _hero_with_facts(ctx, facts, meta='2021'):
    import json

    import xbmcgui
    win = ctx.homewindow.NewHomeWindow('NewHomeWindow.xml', '/addon/path', 'Default', '1080i')
    item = xbmcgui.ListItem('Shawshank')
    item.setProperties({
        'hero_title': 'Shawshank',
        'hero_meta': meta,
        'hero_plot': 'A man and a wall.',
        'hero_facts': json.dumps(facts),
        'fanart': 'https://img/shawshank.jpg',
        'sid': 'tt0111161',
        'stype': 'movie',
    })
    win.getControl(ctx.homewindow.CONTINUE_SHELF).addItems([item])
    win.setFocusId(ctx.homewindow.CONTINUE_SHELF)
    return win


def test_cached_ratings_replace_the_hero_panel(load_homewindow, monkeypatch, tmp_path):
    """A key plus a fresh cache rebuilds the right-quarter panel. The
    key itself never lands in the label."""
    ctx = load_homewindow()
    monkeypatch.setattr(ctx.homewindow, 'get_store', lambda: _FakeStore(auth=None))
    import lib.ui.ratingsched as ratingsched
    from lib.stremio.ratings import RatingsCache

    cache = RatingsCache(str(tmp_path / 'ratings.json'))
    cache.store('tt0111161', {
        'rottenTomatoes': '91%',
        'metascore': '95',
        'imdbRating': '8.8',
    })
    scheduled = []
    monkeypatch.setattr(ratingsched, '_keys', lambda: ('secret-omdb', ''))
    monkeypatch.setattr(ratingsched, '_get_cache', lambda *args: cache)
    monkeypatch.setattr(ratingsched, '_schedule', lambda *args: scheduled.append(args))
    win = _hero_with_facts(ctx, {'id': 'tt0111161', 'year': 2021})

    import xbmcgui
    win.onAction(xbmcgui.Action(1))

    text = win.getControl(ctx.homewindow.HERO_META).text
    assert text == 'Rotten Tomatoes 91%\nMetascore 95\nIMDb 8.8\n2021'
    assert 'secret-omdb' not in text
    assert scheduled == []


def test_an_empty_ratings_key_keeps_the_catalog_line(load_homewindow, monkeypatch):
    """No key means no fetch. The catalog's IMDb line stays, and the
    missing Rotten Tomatoes and Metascore lines stay hidden."""
    ctx = load_homewindow()
    monkeypatch.setattr(ctx.homewindow, 'get_store', lambda: _FakeStore(auth=None))
    import lib.ui.ratingsched as ratingsched

    def _forbidden(*_args, **_kwargs):
        raise AssertionError('a fetch was scheduled with no key')

    monkeypatch.setattr(ratingsched, '_schedule', _forbidden)
    win = _hero_with_facts(ctx, {'id': 'tt0111161', 'year': 2021, 'imdbRating': '9.3'})

    import xbmcgui
    win.onAction(xbmcgui.Action(1))

    assert win.getControl(ctx.homewindow.HERO_META).text == '2021'
    logged = ' '.join(msg for msg, _level in ctx.env.log_calls)
    assert 'secret-omdb' not in logged


def test_a_ratings_miss_keeps_the_catalog_line_and_schedules(load_homewindow, monkeypatch, tmp_path):
    ctx = load_homewindow()
    monkeypatch.setattr(ctx.homewindow, 'get_store', lambda: _FakeStore(auth=None))
    import lib.ui.ratingsched as ratingsched
    from lib.stremio.ratings import RatingsCache

    cache = RatingsCache(str(tmp_path / 'ratings.json'))
    scheduled = []
    monkeypatch.setattr(ratingsched, '_keys', lambda: ('secret-omdb', ''))
    monkeypatch.setattr(ratingsched, '_get_cache', lambda *args: cache)
    monkeypatch.setattr(ratingsched, '_schedule', lambda *args, **kwargs: scheduled.append((args[0], kwargs.get('stype'))))
    win = _hero_with_facts(ctx, {'id': 'tt0111161', 'type': 'series', 'year': 2021})

    import xbmcgui
    win.onAction(xbmcgui.Action(1))

    assert win.getControl(ctx.homewindow.HERO_META).text == '2021'
    assert scheduled == [('tt0111161', 'series')]
    logged = ' '.join(msg for msg, _level in ctx.env.log_calls)
    assert 'secret-omdb' not in logged


def test_a_blank_focused_item_does_not_clear_the_hero(load_homewindow, monkeypatch):
    """Scrolling past the posters the row actually holds used to hand
    paint_hero a blank item. That cleared the title and the plot, and
    the row looked dead. A blank item keeps the hero that was there."""
    ctx = load_homewindow()
    monkeypatch.setattr(ctx.homewindow, 'get_store', lambda: _FakeStore(auth=None))
    import xbmcgui
    win = ctx.homewindow.NewHomeWindow('NewHomeWindow.xml', '/addon/path', 'Default', '1080i')
    item = xbmcgui.ListItem('Dune')
    item.setProperties({
        'hero_title': 'Dune',
        'hero_meta': '2021',
        'hero_plot': 'A boy and a desert.',
        'fanart': 'https://img/dune.jpg',
        'sid': 'tt1',
    })
    win.getControl(ctx.homewindow.CONTINUE_SHELF).addItems([item])
    win.setFocusId(ctx.homewindow.CONTINUE_SHELF)
    win.onAction(xbmcgui.Action(1))
    assert win.getControl(ctx.homewindow.HERO_TITLE).label == 'Dune'

    win.getControl(ctx.homewindow.BROWSE_LIST).addItems([xbmcgui.ListItem()])
    win.setFocusId(ctx.homewindow.BROWSE_LIST)
    win.onAction(xbmcgui.Action(1))

    assert win.getControl(ctx.homewindow.HERO_TITLE).label == 'Dune'
    assert win.getControl(ctx.homewindow.HERO_PLOT).text == 'A boy and a desert.'












class _LibraryApi:
    def __init__(self, item=None):
        self.item = item
        self.put_calls = []

    def get_library_item(self, auth_key, item_id):
        return self.item

    def put_library_item(self, auth_key, item):
        self.put_calls.append((auth_key, item))




def _resume_poster(label, **props):
    import xbmcgui
    item = xbmcgui.ListItem(label)
    for key, value in props.items():
        item.setProperty(key, str(value))
    return item


def _click_continue(ctx, choice=None, stream=None, started=True, store_error=False):
    """Click one Continue poster. `choice` is what the dialog returns."""
    captured = {'choose': None, 'play': None, 'streams': None, 'detail': [], 'order': []}

    def fake_choose(heading, rows):
        captured['choose'] = (heading, list(rows))
        return -1 if choice is None else choice

    def fake_play(stream_obj, stype, sid, **kwargs):
        captured['order'].append('play')
        captured['play'] = (stream_obj, stype, sid, kwargs)
        return started

    def fake_wait(*_a, **_k):
        captured['order'].append('wait')
        return True, False

    def fake_end():
        captured['order'].append('end')

    def fake_streams(*args, **kwargs):
        captured['order'].append('streams')
        captured['streams'] = (args, kwargs)
        return started

    def fake_detail(stype, sid):
        # The stock title page: no episode to land on yet.
        captured['detail'].append((stype, sid))
        return True

    class _Store:
        def get_last_stream(self, content_type, content_id, video_id=None):
            if store_error:
                raise OSError('unreadable')
            captured['lookup'] = (content_type, content_id, video_id)
            return stream

    monkeypatch = captured  # placeholder so the name exists; caller patches
    del monkeypatch
    return captured, fake_choose, fake_play, fake_wait, fake_end, fake_streams, fake_detail, _Store


def test_continue_series_back_closes_the_dialog_and_opens_nothing(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    captured = _arm_continue(ctx, monkeypatch, choice=None)
    win = ctx.homewindow.NewHomeWindow('NewHomeWindow.xml', '/addon/path', 'Default', '1080i')
    win.getControl(ctx.homewindow.CONTINUE_SHELF).addItems([_series_resume_item()])

    win.onClick(ctx.homewindow.CONTINUE_SHELF)

    assert captured['choose'] == ('Show', ['Resume S1E3 from 12:04', 'Episode list'])
    assert captured['detail'] == []
    assert captured['play'] is None
    assert captured['streams'] is None
    assert win.closed is False


def _arm_continue(ctx, monkeypatch, choice=0, stream=None, started=True, store_error=False):
    captured, fake_choose, fake_play, fake_wait, fake_end, fake_streams, fake_detail, store_cls = (
        _click_continue(ctx, choice=choice, stream=stream, started=started, store_error=store_error)
    )
    monkeypatch.setattr(ctx.dialogs, 'choose', fake_choose)
    monkeypatch.setattr(ctx.player, 'play_direct', fake_play)
    monkeypatch.setattr(ctx.streamswindow, '_wait_for_playback_end', fake_wait)
    monkeypatch.setattr(ctx.uicommon, 'end_player_phase', fake_end)
    monkeypatch.setattr(ctx.streamswindow, 'open_streams', fake_streams)
    monkeypatch.setattr(ctx.detailwindow, 'open_detail', fake_detail)
    monkeypatch.setattr(ctx.homewindow, 'get_store', lambda: store_cls())

    def fake_fetch(stype, sid, **_kwargs):
        captured['fetch'] = (stype, sid)
        return {
            'id': sid, 'name': 'Show', 'type': stype,
            'videos': [
                {'id': 'tt1:1:3', 'season': 1, 'episode': 3},
                {'id': 'tt1:1:4', 'season': 1, 'episode': 4},
            ],
        }

    monkeypatch.setattr(ctx.views, '_fetch_meta', fake_fetch)
    return captured


def _series_resume_item():
    return _resume_poster(
        'Show', sid='tt1', stype='series', band='resume', video_id='tt1:1:3',
        position_ms='724000', duration_ms='3600000', hero_title='Show',
        thumbnail='http://poster', fanart='http://fanart',
    )


def test_continue_series_episode_list_opens_detail_and_does_not_play(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    captured = _arm_continue(ctx, monkeypatch, choice=1)
    win = ctx.homewindow.NewHomeWindow('NewHomeWindow.xml', '/addon/path', 'Default', '1080i')
    win.getControl(ctx.homewindow.CONTINUE_SHELF).addItems([_series_resume_item()])

    win.onClick(ctx.homewindow.CONTINUE_SHELF)

    assert captured['detail'] == [('series', 'tt1')]
    assert captured['play'] is None
    assert captured['streams'] is None
    assert win.closed is True


def test_continue_resume_opens_the_stream_picker_for_the_episode(load_homewindow, monkeypatch):
    """Resume opens the stock stream picker for the episode the card is
    on, with the show's fetched meta; the player then offers the saved
    position itself. Playback starting closes Home behind it."""
    ctx = load_homewindow()
    captured = _arm_continue(ctx, monkeypatch, choice=0, started=True)
    win = ctx.homewindow.NewHomeWindow('NewHomeWindow.xml', '/addon/path', 'Default', '1080i')
    win.getControl(ctx.homewindow.CONTINUE_SHELF).addItems([_series_resume_item()])

    win.onClick(ctx.homewindow.CONTINUE_SHELF)

    args, kwargs = captured['streams']
    assert args == ('series', 'tt1:1:3')
    assert kwargs['video_id'] == 'tt1:1:3'
    assert kwargs['poster'] == 'http://poster'
    assert kwargs['heading'] == 'Show'
    assert kwargs['meta']['id'] == 'tt1'
    assert kwargs['meta']['videos'][1]['id'] == 'tt1:1:4'
    assert captured['fetch'] == ('series', 'tt1')
    assert captured['play'] is None
    assert captured['detail'] == []
    assert win.closed is True


def test_continue_resume_backed_out_of_the_picker_keeps_home(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    captured = _arm_continue(ctx, monkeypatch, choice=0, started=False)
    monkeypatch.setattr(ctx.homewindow, '_after_child', lambda window, played=False: None)
    win = ctx.homewindow.NewHomeWindow('NewHomeWindow.xml', '/addon/path', 'Default', '1080i')
    win.getControl(ctx.homewindow.CONTINUE_SHELF).addItems([_series_resume_item()])

    win.onClick(ctx.homewindow.CONTINUE_SHELF)

    assert captured['streams'] is not None
    assert win.closed is False


def test_continue_resume_stops_when_the_title_fetch_is_cancelled(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    captured = _arm_continue(ctx, monkeypatch, choice=0, stream={'url': 'https://cdn.example/ep.mp4'})
    monkeypatch.setattr(ctx.uicommon, 'run_cancellable', lambda *_a, **_k: ctx.uicommon.CANCELLED)
    win = ctx.homewindow.NewHomeWindow('NewHomeWindow.xml', '/addon/path', 'Default', '1080i')
    win.getControl(ctx.homewindow.CONTINUE_SHELF).addItems([_series_resume_item()])

    win.onClick(ctx.homewindow.CONTINUE_SHELF)

    assert captured['play'] is None
    assert captured['streams'] is None
    assert captured['detail'] == []
    assert win.closed is False




def test_continue_movie_offers_resume_or_details(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    captured = _arm_continue(ctx, monkeypatch, choice=1)
    win = ctx.homewindow.NewHomeWindow('NewHomeWindow.xml', '/addon/path', 'Default', '1080i')
    item = _resume_poster(
        'Dune', sid='tt1', stype='movie', band='resume',
        position_ms='50000', duration_ms='100000', hero_title='Dune',
    )
    win.getControl(ctx.homewindow.CONTINUE_SHELF).addItems([item])

    win.onClick(ctx.homewindow.CONTINUE_SHELF)

    assert captured['choose'] == ('Dune', ['Resume from 0:50', 'Details'])
    assert captured['detail'] == [('movie', 'tt1')]
    assert captured['play'] is None


def test_continue_next_up_still_opens_the_episode_list(load_homewindow, monkeypatch):
    """Finished episodes sit on the same row. They are not a resume."""
    ctx = load_homewindow()
    captured = _arm_continue(ctx, monkeypatch, choice=0)
    win = ctx.homewindow.NewHomeWindow('NewHomeWindow.xml', '/addon/path', 'Default', '1080i')
    item = _resume_poster(
        'Show', sid='tt1', stype='series', band='next_up', video_id='tt1:1:3',
        position_ms='3500000', duration_ms='3600000',
    )
    win.getControl(ctx.homewindow.CONTINUE_SHELF).addItems([item])

    win.onClick(ctx.homewindow.CONTINUE_SHELF)

    assert captured['choose'] is None
    assert captured['detail'] == [('series', 'tt1')]
    assert captured['play'] is None


def test_library_poster_does_not_ask_to_resume(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    captured = _arm_continue(ctx, monkeypatch, choice=0)
    win = ctx.homewindow.NewHomeWindow('NewHomeWindow.xml', '/addon/path', 'Default', '1080i')
    item = _resume_poster(
        'Dune', sid='tt1', stype='movie', band='resume',
        position_ms='50000', duration_ms='100000',
    )
    win.getControl(ctx.homewindow.LIBRARY_SHELF).addItems([item])

    win.onClick(ctx.homewindow.LIBRARY_SHELF)

    assert captured['choose'] is None
    assert captured['detail'] == [('movie', 'tt1')]


def test_continue_row_carries_the_episode_and_the_offset(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    progress = [{
        'type': 'series', 'id': 'tt1', 'video_id': 'tt1:1:3', 'name': 'Show',
        'poster': 'p', 'position_ms': 724000, 'duration_ms': 3600000,
        'updated_at': '2020-01-02T00:00:00Z',
    }]
    monkeypatch.setattr(
        ctx.homewindow, 'get_store', lambda: _FakeStore(progress_entries=progress),
    )
    # Local progress carries no name; the row's meta fetch supplies it.
    monkeypatch.setattr(
        ctx.mystuff, '_enrich', lambda items, **kw: [item.update(name='Show', poster='p') for item in items],
    )
    win = ctx.homewindow.NewHomeWindow('NewHomeWindow.xml', '/addon/path', 'Default', '1080i')

    win.onInit()

    item = win.getControl(ctx.homewindow.CONTINUE_SHELF).items[0]
    assert item.getProperty('sid') == 'tt1'
    assert item.getProperty('video_id') == 'tt1:1:3'
    assert item.getProperty('band') == 'resume'
    assert item.getProperty('position_ms') == '724000'
    assert item.getProperty('duration_ms') == '3600000'


# ---------------------------------------------------------------------------
# Coming back to Home: refresh what changed, keep the remote's place
# ---------------------------------------------------------------------------


def _built_home(ctx, monkeypatch, library, auth=None, progress=None):
    store = _FakeStore(auth=auth or {'authKey': 'k', 'user': {'email': 'a@b.c'}},
                       progress_entries=progress or [])
    monkeypatch.setattr(ctx.homewindow, 'get_store', lambda: store)
    monkeypatch.setattr(ctx.homewindow, '_one_catalog_page', lambda *args: [])
    _patch_library(monkeypatch, ctx, library)
    win = ctx.homewindow.NewHomeWindow('NewHomeWindow.xml', '/addon/path', 'Default', '1080i')
    win.onInit()
    ctx.homewindow._switch_tab(win, 'mystuff')
    ctx.homewindow._focus_first(win)
    return win, store


_THREE = [
    {'_id': 'tt%d' % index, 'name': 'T%d' % index, 'type': 'movie', 'poster': 'p', 'removed': False}
    for index in (1, 2, 3)
]


def test_reopen_after_playback_keeps_the_poster_the_remote_was_on(load_homewindow, monkeypatch):
    """The reopen used to rebuild every row and land on Continue's first poster."""
    ctx = load_homewindow()
    win, _store = _built_home(ctx, monkeypatch, _THREE)
    library = win.getControl(ctx.homewindow.LIBRARY_SHELF)
    library.selectItem(2)
    win.setFocusId(ctx.homewindow.LIBRARY_SHELF)
    picked = library.getSelectedItem().getProperty('sid')

    win.onInit()

    assert win.getFocusId() == ctx.homewindow.LIBRARY_SHELF
    assert library.getSelectedItem().getProperty('sid') == picked


def test_a_settings_visit_that_changes_nothing_leaves_the_rows_alone(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    win, _store = _built_home(ctx, monkeypatch, _THREE)
    library = win.getControl(ctx.homewindow.LIBRARY_SHELF)
    library.selectItem(1)
    win.setFocusId(ctx.homewindow.LIBRARY_SHELF)
    loads = []
    monkeypatch.setattr(ctx.homewindow, '_load_rows', lambda *a, **k: loads.append(1))

    ctx.homewindow._open_settings(win)

    assert ctx.env.opened_settings is True
    assert loads == []
    assert library.getSelectedPosition() == 1
    assert win.getFocusId() == ctx.homewindow.LIBRARY_SHELF




def test_a_sign_in_rebuilds_the_rows_for_the_new_account(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    win, store = _built_home(ctx, monkeypatch, _THREE)
    builds = []
    real_build = win._build
    monkeypatch.setattr(win, '_build', lambda place=None: (builds.append(place), real_build(place=place)))
    import lib.ui.views as views

    def fake_sign_in():
        store._auth = {'authKey': 'other', 'user': {'email': 'x@y.z'}}
        return store._auth

    monkeypatch.setattr(views, 'sign_in', fake_sign_in)

    ctx.homewindow._open_signin(win)

    assert len(builds) == 1 and builds[0] is not None


def test_tmdb_original_art_is_asked_for_at_the_drawn_size(load_homewindow):
    """#18: a 200px card and the 1920px hero do not need TMDB `original`."""
    ctx = load_homewindow()
    item = ctx.homewindow._poster_item({
        'id': 'tt1', 'type': 'movie', 'name': 'Dune',
        'poster': 'https://image.tmdb.org/t/p/original/p.jpg',
        'background': 'https://image.tmdb.org/t/p/original/b.jpg',
    })
    assert item.getProperty('thumbnail') == 'https://image.tmdb.org/t/p/w342/p.jpg'
    assert item.getProperty('fanart') == 'https://image.tmdb.org/t/p/w1280/b.jpg'
    # No backdrop: the poster stands in for the hero and is scaled to the
    # screen, so it is fetched larger than the card.
    bare = ctx.homewindow._poster_item({
        'id': 'tt2', 'type': 'movie', 'name': 'Arrival',
        'poster': 'https://image.tmdb.org/t/p/original/a.jpg',
    })
    assert bare.getProperty('thumbnail') == 'https://image.tmdb.org/t/p/w342/a.jpg'
    assert bare.getProperty('fanart') == 'https://image.tmdb.org/t/p/w780/a.jpg'









def _hero_with_logo(ctx, logo, next_logo=''):
    import xbmcgui
    win = ctx.homewindow.NewHomeWindow('NewHomeWindow.xml', '/addon/path', 'Default', '1080i')
    items = []
    for name, sid, art in (('Dune', 'tt1', logo), ('Arrival', 'tt2', next_logo)):
        item = xbmcgui.ListItem(name)
        item.setProperties({
            'hero_title': name, 'hero_plot': 'Plot.', 'hero_logo': art,
            'fanart': 'https://img/%s.jpg' % sid, 'sid': sid, 'stype': 'movie',
        })
        items.append(item)
    win.getControl(ctx.homewindow.LIBRARY_SHELF).addItems(items)
    win.setFocusId(ctx.homewindow.LIBRARY_SHELF)
    return win


def test_a_logo_kodi_has_not_loaded_keeps_the_text_title(load_homewindow, monkeypatch):
    """#10: the skin hides the title whenever a logo is set, so a logo URL
    that 404s left the title area blank. A logo replaces the title only
    once Kodi's texture cache holds it; until then it is warmed."""
    ctx = load_homewindow()
    import xbmcgui
    ctx.env.cached_textures = set()
    win = _hero_with_logo(ctx, 'https://img/logo1.png', 'https://img/logo2.png')

    win.onAction(xbmcgui.Action(1))

    assert win.getProperty('HeroLogo') == ''
    assert win.getControl(ctx.homewindow.HERO_TITLE).label == 'Dune'
    assert win.getControl(ctx.homewindow.HERO_LOGO).image == ''
    # The focused logo and the next one load in the two 1x1 slots.
    assert win.getControl(30098).image == 'https://img/logo1.png'
    assert win.getControl(30099).image == 'https://img/logo2.png'

    # Once Kodi has it (and the short re-check window has passed), it shows.
    ctx.env.cached_textures = {'https://img/logo1.png'}
    from lib.ui import logocheck
    logocheck._LOGO_STATE.clear()
    win.onAction(xbmcgui.Action(1))

    assert win.getProperty('HeroLogo') == 'https://img/logo1.png'
    assert win.getControl(ctx.homewindow.HERO_LOGO).image == 'https://img/logo1.png'


def test_a_cached_logo_shows_at_once_and_is_not_asked_for_again(load_homewindow):
    ctx = load_homewindow()
    import xbmcgui
    win = _hero_with_logo(ctx, 'https://img/logo1.png')

    win.onAction(xbmcgui.Action(1))
    win.onAction(xbmcgui.Action(1))

    assert win.getProperty('HeroLogo') == 'https://img/logo1.png'
    lookups = [call for call in ctx.env.jsonrpc_calls if 'logo1.png' in call]
    assert len(lookups) == 1


def test_a_card_without_art_shows_its_title_on_the_plate():
    """#10: a title with no poster drew an empty plate (and, on a hub, a
    focus ring around nothing). The plate now carries the title, which is
    also how the More card and the Sign in card read since the captions
    under the posters went away (#7)."""
    import os
    import xml.etree.ElementTree as ET

    skin = os.path.join(_REPO_ROOT, 'resources', 'skins', 'Default', '1080i')
    home = ET.parse(os.path.join(skin, 'NewHomeWindow.xml')).getroot()
    for panel_id in ('30040', '30041', '30042', '30043', '30044', '30045'):
        panel = home.find(".//control[@id='%s']" % panel_id)
        for layout in ('itemlayout', 'focusedlayout'):
            labels = [
                el for el in panel.find(layout).findall('control')
                if el.get('type') == 'label' and 'ListItem.Label' in (el.findtext('label') or '')
                and any('String.IsEmpty(ListItem.Property(thumbnail))' in (v.text or '') for v in el.findall('visible'))
            ]
            assert len(labels) == 1, (panel_id, layout)


def test_continue_cards_are_wide_art_with_the_show_and_episode(load_homewindow):
    """#9: a Continue card is 16:9. The poster scaled into it was a slice
    through the middle. It now uses the episode still (Cinemeta ids) or
    the backdrop, and writes the show and S:1 E:2 bottom-left."""
    ctx = load_homewindow()
    episode = ctx.homewindow._continue_item({
        'id': 'tt0944947', 'type': 'series', 'name': 'Game of Thrones',
        'poster': 'p.jpg', 'background': 'b.jpg', 'video_id': 'tt0944947:2:5',
    })
    assert episode.getProperty('land') == 'https://episodes.metahub.space/tt0944947/2/5/w780.jpg'
    assert episode.getProperty('card_show') == 'Game of Thrones'
    assert episode.getProperty('card_code') == 'S:2 E:5'
    assert episode.getProperty('video_id') == 'tt0944947:2:5'

    movie = ctx.homewindow._continue_item({
        'id': 'tt1', 'type': 'movie', 'name': 'Dune', 'poster': 'p.jpg',
        'background': 'https://image.tmdb.org/t/p/original/b.jpg',
    })
    assert movie.getProperty('land') == 'https://image.tmdb.org/t/p/w1280/b.jpg'
    assert movie.getProperty('card_code') == ''
    assert movie.getProperty('card_show') == 'Dune'

    bare = ctx.homewindow._continue_item({
        'id': 'kitsu:1', 'type': 'series', 'name': 'Show', 'poster': 'p.jpg', 'video_id': 'kitsu:1:4',
    })
    # No Cinemeta still for a non-imdb id and no backdrop: the skin shows
    # the poster whole on the plate.
    assert bare.getProperty('land') == ''


def test_continue_card_skin_layers_the_poster_under_the_wide_art():
    import os
    import xml.etree.ElementTree as ET

    path = os.path.join(_REPO_ROOT, 'resources', 'skins', 'Default', '1080i', 'NewHomeWindow.xml')
    panel = ET.parse(path).getroot().find(".//control[@id='30040']")
    for layout in ('itemlayout', 'focusedlayout'):
        controls = panel.find(layout).findall('control')
        textures = [(el.findtext('texture') or '').strip() for el in controls]
        poster = textures.index('$INFO[ListItem.Property(thumbnail)]')
        land = textures.index('$INFO[ListItem.Property(land)]')
        assert poster < land
        assert (controls[poster].findtext('aspectratio') or '').strip() == 'keep'
        assert (controls[land].findtext('aspectratio') or '').strip() == 'scale'
        labels = [el.findtext('label') or '' for el in controls if el.get('type') == 'label']
        assert any('card_show' in text for text in labels)
        assert any('card_code' in text for text in labels)
        # Nothing hangs under the card any more.
        assert not [el for el in controls if el.get('type') == 'label' and (el.findtext('top') or '') == '232']


def test_any_poster_only_title_fills_the_hero(load_homewindow):
    """#21: only library records used to get the full-bleed poster; a
    catalog title without a backdrop sat as a pillarbox."""
    ctx = load_homewindow()
    import xbmcgui
    item = ctx.homewindow._poster_item({'id': 'tt5', 'type': 'movie', 'name': 'Five', 'poster': 'p5.jpg'})
    assert item.getProperty('hero_fill') == '1'
    win = ctx.homewindow.NewHomeWindow('NewHomeWindow.xml', '/addon/path', 'Default', '1080i')
    win.getControl(30042).addItems([item])
    win.setFocusId(30042)
    win.onAction(xbmcgui.Action(1))
    assert win.getProperty('HeroFill') == '1'
    assert win.getControl(ctx.homewindow.HERO_FILL).image == 'p5.jpg'





def test_an_unchanged_hero_is_not_repainted_and_applied_ratings_stay_on_the_card(load_homewindow, monkeypatch):
    """#17: every action repaints the hero (worker noops, a key that did not
    move). An unchanged hero now costs nothing, and scores merged from the
    ratings cache are kept on the card instead of being merged again."""
    ctx = load_homewindow()
    import xbmcgui

    import lib.ui.newlook as newlook
    import lib.ui.ratingsched as ratingsched

    placed = []
    real_paint = newlook.paint_hero
    monkeypatch.setattr(newlook, 'paint_hero', lambda *a, **k: placed.append('home') or real_paint(*a, **k))
    overlays = []

    def _overlay(facts, fallback, upcoming=None):
        overlays.append(fallback)
        return 'RT 94%\n' + fallback

    monkeypatch.setattr(ratingsched, 'overlay_text', _overlay)
    win = _hero_with_facts(ctx, {'imdb_id': 'tt0111161'})

    win.onAction(xbmcgui.Action(1))
    win.onAction(xbmcgui.Action(0))
    win.onAction(xbmcgui.Action(0))

    assert placed == ['home']
    assert overlays == ['2021']
    card = win.getControl(ctx.homewindow.CONTINUE_SHELF).getSelectedItem()
    assert card.getProperty('hero_meta') == 'RT 94%\n2021'
    assert win.getControl(ctx.homewindow.HERO_META).text == 'RT 94%\n2021'


def test_the_library_row_reads_newest_added_first(load_homewindow):
    ctx = load_homewindow()
    rows = [
        {'_id': 'old', '_ctime': '2020-01-01T00:00:00Z'},
        {'_id': 'new', '_ctime': '2024-06-01T00:00:00Z'},
        {'_id': 'mid', '_mtime': '2022-01-01T00:00:00Z'},
        'not a row',
    ]
    assert [row['_id'] for row in ctx.homewindow._newest_first(rows)] == ['new', 'mid', 'old']






def test_library_meta_worker_keeps_only_the_latest_focus(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    hw = ctx.homewindow

    win = hw.NewHomeWindow('NewHomeWindow.xml', '/addon/path', 'Default', '1080i')
    items = [hw._poster_item({'id': 'tt%d' % i, 'type': 'movie', 'name': str(i)},
                             sparse=True) for i in range(40)]
    shelf = win.getControl(hw.LIBRARY_SHELF)
    shelf.addItems(items)
    win.setFocusId(hw.LIBRARY_SHELF)
    started, release = threading.Event(), threading.Event()
    fetched = []

    def meta(stype, sid, stop=None):
        fetched.append(sid)
        started.set()
        assert release.wait(2)
        return {'id': sid, 'type': stype, 'description': sid}

    monkeypatch.setattr(hw, '_fetch_library_meta', meta)
    hw._schedule_library_meta(win, items[0])
    assert started.wait(2)
    worker = win._library_meta_thread
    for index, item in enumerate(items[1:], 1):
        shelf.selectItem(index)
        hw._schedule_library_meta(win, item)
        assert win._library_meta_thread is worker
    release.set()
    join_library_meta(win)
    hw._apply_library_meta(win)
    assert fetched == ['tt0', 'tt39']
    assert all(not item.getProperty('hero_plot') for item in items[:-1])
    assert items[-1].getProperty('hero_plot') == 'tt39'
    assert not hasattr(win, '_library_meta_threads')
    assert worker.daemon
    # Already published results are also stale once Kodi changes selection.
    items[-1].setProperty('hero_plot', '')
    win._library_meta_ready = [('movie', 'tt39', {'description': 'stale'})]
    shelf.selectItem(0)
    hw._apply_library_meta(win)
    assert items[-1].getProperty('hero_plot') == ''






def test_catalog_errors_never_log_credentials(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    from lib.ui import catalogpages

    def fail(*args):
        raise OSError('https://user:secret@server.test/private?token=hidden')

    monkeypatch.setattr(catalogpages, 'cached_first_page', fail)
    with pytest.raises(ctx.homewindow.CatalogUnavailable):
        ctx.homewindow._one_catalog_page('t', {}, {'id': 'top'})

    def pages():
        fail()
        yield []

    monkeypatch.setattr(ctx.homewindow, '_shelf_page_gen', lambda state: pages())
    state = {'transport': 't', 'catalog': {'id': 'top'}}
    assert ctx.homewindow._pull_more(state, 10) == []
    messages = [msg for msg, _ in ctx.env.log_calls if 'home:' in msg and 'failed' in msg]
    assert len(messages) == 2
    assert all('OSError' in msg and 'secret' not in msg and 'hidden' not in msg for msg in messages)


def test_library_worker_retires_when_idle_without_retaining_the_window(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    hw = ctx.homewindow
    win = hw.NewHomeWindow('NewHomeWindow.xml', '/addon/path', 'Default', '1080i')
    monkeypatch.setattr(hw, '_fetch_library_meta', lambda stype, sid, stop=None: {'id': sid, 'description': sid})
    workers = []
    for i in range(30):
        item = hw._poster_item({'id': 'tt%d' % i, 'type': 'movie'}, sparse=True)
        hw._schedule_library_meta(win, item)
        join_library_meta(win)
        assert win._library_meta_idle.is_set()
        workers.append(win._library_meta_thread)
        win._library_meta_thread.join(1)
        assert not win._library_meta_thread.is_alive()
    assert not any(worker.is_alive() for worker in workers)
    # The idle worker must not retain a discarded window.
    import gc
    import weakref

    ref = weakref.ref(win)
    worker = workers[0]
    del win
    gc.collect()
    worker.join(2)
    assert ref() is None and not worker.is_alive()


def test_a_stale_library_result_does_not_leave_its_poster_loading(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    hw = ctx.homewindow
    item = hw._poster_item({'id': 'tt1', 'type': 'movie'}, sparse=True)
    win = hw.NewHomeWindow('NewHomeWindow.xml', '/addon/path', 'Default', '1080i')
    win.getControl(hw.LIBRARY_SHELF).addItems([item])
    monkeypatch.setattr(hw, '_fetch_library_meta', lambda *args: {'description': 'loaded'})
    hw._schedule_library_meta(win, item)
    join_library_meta(win)
    win.setFocusId(hw.BTN_SETTINGS)
    hw._apply_library_meta(win)
    assert item.getProperty('meta_state') == ''
    assert item.getProperty('hero_plot') == ''
    win.setFocusId(hw.LIBRARY_SHELF)
    hw._schedule_library_meta(win, item)
    join_library_meta(win)
    hw._apply_library_meta(win)
    assert item.getProperty('hero_plot') == 'loaded'


def test_signin_button_opens_sign_in_without_settings_or_a_plugin_handle(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    import lib.ui.views as views
    calls = []
    monkeypatch.setattr(ctx.homewindow, 'get_store', lambda: _FakeStore(auth=None))
    monkeypatch.setattr(views, 'sign_in', lambda: calls.append('sign_in'))
    monkeypatch.setattr(views, 'login', lambda: calls.append('plugin_handle'))
    win = ctx.homewindow.NewHomeWindow('NewHomeWindow.xml', '/addon/path', 'Default', '1080i')
    win.setProperty('HomeSignedOut', '1')
    win.onClick(ctx.homewindow.BTN_SIGNIN)
    assert calls == ['sign_in']
    assert ctx.env.opened_settings is False
    assert ctx.env.end_of_directory == []
    assert not win.closed


# --- the watched check and the IN CINEMA strip (issues #85, #84) ---------------


def _recent_release(days=10):
    import datetime

    today = datetime.datetime.now(datetime.timezone.utc).date()
    return (today - datetime.timedelta(days=days)).isoformat() + 'T00:00:00.000Z'


def test_a_watched_movie_card_wears_the_check(load_homewindow):
    from lib.ui import watchmarks

    ctx = load_homewindow()
    watchmarks.refresh(_FakeStore(progress_entries=[{
        'id': 'tt1', 'type': 'movie', 'video_id': None, 'position_ms': 80000,
        'duration_ms': 100000, 'updated_at': '2026-05-02T00:00:00Z',
    }]))
    item = ctx.homewindow._poster_item({'id': 'tt1', 'type': 'movie', 'name': 'Dune'})
    assert item.getProperty('watched') == '1'
    assert item.legacy_info == {'playcount': 1, 'overlay': 5}
    other = ctx.homewindow._poster_item({'id': 'tt2', 'type': 'movie', 'name': 'Arrival'})
    assert other.getProperty('watched') == ''
    assert other.legacy_info == {}
    # A series wears it too: Stremio's `timesWatched > 0` rule is type-blind.
    show = ctx.homewindow._poster_item({'id': 'tt1', 'type': 'series', 'name': 'Dune'})
    assert show.getProperty('watched') == '1'
    other_show = ctx.homewindow._poster_item({'id': 'tt2', 'type': 'series', 'name': 'Severance'})
    assert other_show.getProperty('watched') == ''


def test_a_card_built_with_no_watched_set_loaded_is_unmarked(load_homewindow):
    ctx = load_homewindow()
    item = ctx.homewindow._poster_item({'id': 'tt1', 'type': 'movie'}, sparse=True)
    assert item.getProperty('watched') == ''
    assert item.getProperty('in_cinema') == ''


def test_a_recent_movie_without_a_home_release_gets_the_cinema_strip(load_homewindow):
    ctx = load_homewindow()
    meta = {'id': 'tt1', 'type': 'movie', 'name': 'Dune', 'released': _recent_release()}
    assert ctx.homewindow._poster_item(meta).getProperty('in_cinema') == '1'
    meta['dvdRelease'] = _recent_release(2)
    assert ctx.homewindow._poster_item(meta).getProperty('in_cinema') == ''
    meta.pop('dvdRelease')
    meta['type'] = 'series'
    assert ctx.homewindow._poster_item(meta).getProperty('in_cinema') == ''


def test_the_hero_line_names_the_cinema_in_the_year_row(load_homewindow):
    ctx = load_homewindow()
    meta = {'id': 'tt1', 'type': 'movie', 'name': 'Dune', 'released': _recent_release(), 'releaseInfo': '2026'}
    item = ctx.homewindow._poster_item(meta)
    assert '2026 \u00b7 STR30548' in item.getProperty('hero_meta').splitlines()


def test_the_fetched_meta_decides_the_tag_on_a_library_card(load_homewindow):
    """A library record has no `released`; once the focus fetch lands the
    card on the row picks the tag up."""
    ctx = load_homewindow()
    item = ctx.homewindow._poster_item({'id': 'tt1', 'type': 'movie', 'name': 'Dune'}, sparse=True)
    assert item.getProperty('in_cinema') == ''
    ctx.homewindow._write_meta_onto(item, {'id': 'tt1', 'type': 'movie', 'released': _recent_release()})
    assert item.getProperty('in_cinema') == '1'


def test_repaint_watched_flips_cards_in_place_and_leaves_the_rest(load_homewindow):
    from lib.ui import watchmarks

    ctx = load_homewindow()
    win = ctx.homewindow.NewHomeWindow('NewHomeWindow.xml', '/addon/path', 'Default', '1080i')
    watchmarks.refresh(_FakeStore())
    cards = [ctx.homewindow._poster_item({'id': 'tt%d' % n, 'type': 'movie'}) for n in (1, 2)]
    show = ctx.homewindow._poster_item({'id': 'tt3', 'type': 'series'})
    watched_show = ctx.homewindow._poster_item({'id': 'tt4', 'type': 'series'})
    win._rows = {ctx.homewindow.CONTINUE_SHELF: {'items': cards + [show, watched_show]}}
    assert [c.getProperty('watched') for c in cards] == ['', '']

    watchmarks.refresh(_FakeStore(auth={'authKey': 'k'}), [
        {'_id': 'tt1', 'type': 'movie', '_mtime': '2026-05-01T00:00:00Z', 'state': {'timesWatched': 1}},
        {'_id': 'tt4', 'type': 'series', '_mtime': '2026-05-01T00:00:00Z', 'state': {'timesWatched': 2}},
    ])
    ctx.homewindow.repaint_watched(win)
    assert [c.getProperty('watched') for c in cards] == ['1', '']
    assert cards[0].legacy_info == {'playcount': 1, 'overlay': 5}
    assert show.getProperty('watched') == ''
    assert watched_show.getProperty('watched') == '1'

    # Unmarked again: the check and the playcount both clear.
    watchmarks.refresh(_FakeStore(auth={'authKey': 'k'}), [
        {'_id': 'tt1', 'type': 'movie', '_mtime': '2026-05-03T00:00:00Z', 'state': {'timesWatched': 0}},
    ])
    ctx.homewindow.repaint_watched(win)
    assert cards[0].getProperty('watched') == ''
    assert cards[0].legacy_info == {'playcount': 0, 'overlay': 4}
