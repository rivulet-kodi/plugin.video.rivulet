"""Tests for lib.ui.infowindow: the fullscreen coverflow overlay
(ShowcaseWindow) opened via `open_showcase()` over one catalog page,
exercised against the shared fake xbmc/xbmcgui stubs in tests/kodistubs
(no real Kodi runtime, no network).

lib.ui.infowindow imports xbmcgui at module scope (`class ShowcaseWindow
(xbmcgui.WindowXML)`), so even `_item_properties()` - a pure
function that touches no xbmc API itself - needs the module imported
fresh against the fake xbmcgui (via `load_infowindow`) before it is
reachable at all. `ShowcaseWindow.onClick()` also lazily `from
lib.ui.streamswindow import open_streams` for its movie shortcut (a
movie has nothing left to pick once you're already looking at its
poster - see that module's docstring), so `load_infowindow` reloads
`lib.ui.streamswindow` alongside it and the tests below that exercise
that path monkeypatch `ctx.streamswindow.open_streams` directly.

ShowcaseWindow's onInit()/onClick()/onAction() are called directly here,
never through a real modal event loop: tests/kodistubs's fake
WindowXML.doModal() is a no-op counter, and getControl()/
setFocusId()/getFocusId() are plain in-memory fakes (see
tests/kodistubs/modules.py's make_xbmcgui). This exercises 100% of the
controller *logic* (item building, focus-driven background swaps, back
actions, the info-key no-op, the movie shortcut, selection-by-position,
the empty-metas short-circuit) with none of the *visual* rendering.

ShowcaseWindow.xml's actual skin rendering - the coverflow's fixedlist/
focusedlayout geometry, the fanart crossfade, the WindowOpen/WindowClose
slide+fade animations - is Kodi-skin-engine-only and is NOT, and cannot
be, exercised by this suite. Confirming it renders/scrolls/animates
correctly requires manually opening the overlay on a real Kodi install.
"""
import contextlib

import pytest

from tests.kodistubs import install_kodi_stubs

_RELOAD_MODULE_NAMES = (
    # lib.ui.views is reloaded alongside the rest because ShowcaseWindow's
    # meta enrichment lazily `from lib.ui.views import _fetch_meta` inside
    # its worker - the enrichment tests monkeypatch `ctx.views._fetch_meta`
    # so no test ever reaches the network. lib.ui.dependencies/
    # lib.ui.searchwindow/lib.ui.detailwindow are reloaded for the same
    # reason `_open_credits()`/`open_credits_picker()` lazily reach into
    # them (get_store/get_client, run_query, open_detail) - the credits
    # picker tests below monkeypatch `ctx.dependencies.get_store`/
    # `get_client`, `ctx.searchwindow.run_query`, and
    # `ctx.detailwindow.open_detail` directly.
    'lib.ui.compat', 'lib.ui.uicommon', 'lib.ui.streamswindow', 'lib.ui.views', 'lib.ui.dialogs',
    'lib.ui.dependencies', 'lib.ui.searchwindow', 'lib.ui.detailwindow', 'lib.ui.infowindow',
)


@pytest.fixture
def load_infowindow(monkeypatch):
    """Factory fixture: `load_infowindow(addon_info=None)` installs fresh
    stubs (via tests.kodistubs.install_kodi_stubs) reloading
    lib.ui.compat/lib.ui.infowindow, and returns a namespace with
    `.infowindow`, `.compat`, and `.env`. Every call is torn down
    automatically, in reverse order, at test end.

    `ShowcaseWindow._spawn_enrich` is neutered on the freshly loaded class:
    otherwise every test that drives `onInit()` with a description-less
    meta would spawn a real daemon worker that outlives it, racing that
    test's own `metas`/`_enrich_pending` and reaching `import xbmc` after
    these stubs are torn down. The enrichment tests below either record
    `_spawn_enrich` themselves (see `_spawned`) or call `_enrich_worker`/
    `_enrich_fetch` directly, so nothing needs a real thread; the one test
    that pins the spawn itself takes the pristine method back off
    `ctx.real_spawn_enrich` and joins the thread it starts.
    """
    with contextlib.ExitStack() as stack:
        def _load(addon_info=None):
            ctx = stack.enter_context(install_kodi_stubs(
                reload=_RELOAD_MODULE_NAMES,
                addon_info=addon_info,
            ))
            ctx.real_spawn_enrich = ctx.infowindow.ShowcaseWindow._spawn_enrich
            monkeypatch.setattr(
                ctx.infowindow.ShowcaseWindow, '_spawn_enrich',
                lambda self, index, meta: None,
            )
            return ctx

        yield _load


def _make_meta(mid, name, mtype='movie', **extra):
    meta = {'id': mid, 'name': name, 'type': mtype}
    meta.update(extra)
    return meta


# ---------------------------------------------------------------------------
# _item_properties() - pure mapping, no window involved
# ---------------------------------------------------------------------------


def test_item_properties_full_meta_maps_every_field(load_infowindow):
    ctx = load_infowindow()
    meta = {
        'poster': 'https://x/poster.jpg',
        'logo': 'https://x/logo.png',
        'background': 'https://x/bg.jpg',
        'genres': ['Action', 'Sci-Fi'],
        'imdbRating': '8.4',
        'description': 'A plot.',
        'releaseInfo': '2019',
        'released': '2019-05-01T00:00:00.000Z',
        'runtime': '132 min',
    }
    assert ctx.infowindow._item_properties(meta) == {
        'thumbnail': 'https://x/poster.jpg',
        'fanart': 'https://x/bg.jpg',
        'genre': 'Action, Sci-Fi',
        'rating': '8.4',
        'plot': 'A plot.',
        'year': '2019',
        'runtime': '132 min',
    }


def test_item_properties_thumbnail_falls_back_to_logo_without_poster(load_infowindow):
    ctx = load_infowindow()
    props = ctx.infowindow._item_properties({'logo': 'https://x/logo.png'})
    assert props['thumbnail'] == 'https://x/logo.png'


def test_item_properties_fanart_falls_back_through_logo_then_poster(load_infowindow):
    ctx = load_infowindow()
    _item_properties = ctx.infowindow._item_properties
    assert _item_properties({'logo': 'https://x/logo.png'})['fanart'] == 'https://x/logo.png'
    assert _item_properties({'poster': 'https://x/poster.jpg'})['fanart'] == 'https://x/poster.jpg'
    # background always wins over both when present
    full = {'background': 'https://x/bg.jpg', 'logo': 'https://x/logo.png', 'poster': 'https://x/poster.jpg'}
    assert _item_properties(full)['fanart'] == 'https://x/bg.jpg'


def test_item_properties_year_prefers_release_info_over_released_date(load_infowindow):
    ctx = load_infowindow()
    props = ctx.infowindow._item_properties({'releaseInfo': '2014-2020', 'released': '2019-05-01T00:00:00.000Z'})
    assert props['year'] == '2014-2020'


def test_item_properties_year_falls_back_to_date_only_released(load_infowindow):
    ctx = load_infowindow()
    props = ctx.infowindow._item_properties({'released': '2021-07-04T00:00:00.000Z'})
    assert props['year'] == '2021-07-04'


def test_item_properties_genres_join_with_comma_space(load_infowindow):
    ctx = load_infowindow()
    assert ctx.infowindow._item_properties({'genres': ['Drama']})['genre'] == 'Drama'
    assert ctx.infowindow._item_properties({'genres': []})['genre'] == ''


def test_item_properties_runtime_is_copied_verbatim_when_present(load_infowindow):
    ctx = load_infowindow()
    assert ctx.infowindow._item_properties({'runtime': '132 min'})['runtime'] == '132 min'


def test_item_properties_runtime_is_empty_string_when_absent(load_infowindow):
    ctx = load_infowindow()
    assert ctx.infowindow._item_properties({})['runtime'] == ''


def test_item_properties_missing_fields_are_empty_strings(load_infowindow):
    ctx = load_infowindow()
    assert ctx.infowindow._item_properties({}) == {
        'thumbnail': '', 'fanart': '', 'genre': '', 'rating': '', 'plot': '', 'year': '', 'runtime': '',
    }


def test_item_properties_none_meta_is_treated_as_empty(load_infowindow):
    ctx = load_infowindow()
    assert ctx.infowindow._item_properties(None) == ctx.infowindow._item_properties({})


# ---------------------------------------------------------------------------
# _header_label() - the header wordmark's breadcrumb markup
# ---------------------------------------------------------------------------


def test_header_label_with_no_title_is_bare_rivulet(load_infowindow):
    ctx = load_infowindow()
    assert ctx.infowindow._header_label() == '[COLOR 57EEF3F6]RIVULET[/COLOR]'
    assert ctx.infowindow._header_label(None) == '[COLOR 57EEF3F6]RIVULET[/COLOR]'
    assert ctx.infowindow._header_label('') == '[COLOR 57EEF3F6]RIVULET[/COLOR]'


def test_header_label_with_a_title_renders_uppercased_breadcrumb(load_infowindow):
    ctx = load_infowindow()
    assert ctx.infowindow._header_label('Cinemeta \u00b7 Popular Movies') == (
        '[COLOR 57EEF3F6]RIVULET[/COLOR] [COLOR 2EEEF3F6]/[/COLOR] '
        '[COLOR 9EEEF3F6]CINEMETA \u00b7 POPULAR MOVIES[/COLOR]'
    )


# ---------------------------------------------------------------------------
# ShowcaseWindow.onInit() - item building, background/loading/focus setup
# ---------------------------------------------------------------------------


def test_oninit_builds_items_sets_background_hides_loading_and_focuses_select(load_infowindow):
    ctx = load_infowindow()
    infowindow = ctx.infowindow
    metas = [
        _make_meta('tt1', 'One', background='https://x/bg1.jpg'),
        _make_meta('tt2', 'Two', background='https://x/bg2.jpg'),
    ]
    win = infowindow.ShowcaseWindow('ShowcaseWindow.xml', '/addon/path', 'Default', '1080i')
    win.metas = list(metas)

    win.onInit()

    select = win.getControl(infowindow.SELECT)
    assert len(select.items) == 2
    assert [item.getProperty('position') for item in select.items] == ['0', '1']
    assert select.items[0].getProperty('fanart') == 'https://x/bg1.jpg'
    assert select.items[0].getLabel() == 'One'
    assert win.getControl(infowindow.BACKGROUND).image == 'https://x/bg1.jpg'
    assert win.getControl(infowindow.LOADING).visible is False
    assert win.getFocusId() == infowindow.SELECT
    assert win.getControl(infowindow.HEADER).label == '[COLOR 57EEF3F6]RIVULET[/COLOR]'


def test_oninit_sets_breadcrumb_header_when_catalog_title_is_set(load_infowindow):
    ctx = load_infowindow()
    infowindow = ctx.infowindow
    win = infowindow.ShowcaseWindow('ShowcaseWindow.xml', '/addon/path', 'Default', '1080i')
    win.metas = [_make_meta('tt1', 'One')]
    win.catalog_title = 'Cinemeta \u00b7 Popular Movies'

    win.onInit()

    assert win.getControl(infowindow.HEADER).label == (
        '[COLOR 57EEF3F6]RIVULET[/COLOR] [COLOR 2EEEF3F6]/[/COLOR] '
        '[COLOR 9EEEF3F6]CINEMETA \u00b7 POPULAR MOVIES[/COLOR]'
    )


def test_oninit_item_label_falls_back_to_id_then_placeholder(load_infowindow):
    ctx = load_infowindow()
    infowindow = ctx.infowindow
    win = infowindow.ShowcaseWindow('ShowcaseWindow.xml', '/addon/path', 'Default', '1080i')
    win.metas = [{'id': 'tt9'}, {}]

    win.onInit()

    items = win.getControl(infowindow.SELECT).items
    assert items[0].getLabel() == 'tt9'
    assert items[1].getLabel() == '?'


def test_oninit_with_no_metas_is_a_no_op(load_infowindow):
    ctx = load_infowindow()
    infowindow = ctx.infowindow
    win = infowindow.ShowcaseWindow('ShowcaseWindow.xml', '/addon/path', 'Default', '1080i')
    win.metas = []

    win.onInit()  # must not raise (e.g. IndexError on metas[0])

    assert win.getControl(infowindow.SELECT).items == []
    assert win.getFocusId() is None


# ---------------------------------------------------------------------------
# ShowcaseWindow.onAction() - focus-driven background swap + back actions
# ---------------------------------------------------------------------------


def test_onaction_updates_background_to_focused_items_fanart_when_select_is_focused(load_infowindow):
    ctx = load_infowindow()
    infowindow = ctx.infowindow
    import xbmcgui
    win = infowindow.ShowcaseWindow('ShowcaseWindow.xml', '/addon/path', 'Default', '1080i')
    win.metas = [
        _make_meta('tt1', 'One', background='https://x/bg1.jpg'),
        _make_meta('tt2', 'Two', background='https://x/bg2.jpg'),
    ]
    win.onInit()
    win.getControl(infowindow.SELECT).selected_index = 1  # simulate scrolling to item 2

    win.onAction(xbmcgui.Action(0))  # a non-back nav action (e.g. Right)

    assert win.getControl(infowindow.BACKGROUND).image == 'https://x/bg2.jpg'


def test_onaction_does_not_touch_background_when_select_not_focused(load_infowindow):
    ctx = load_infowindow()
    infowindow = ctx.infowindow
    import xbmcgui
    win = infowindow.ShowcaseWindow('ShowcaseWindow.xml', '/addon/path', 'Default', '1080i')
    win.metas = [_make_meta('tt1', 'One', background='https://x/bg1.jpg')]
    win.onInit()
    win.setFocusId(infowindow.CLOSE)  # focus moved off the coverflow
    win.getControl(infowindow.BACKGROUND).image = 'unchanged'

    win.onAction(xbmcgui.Action(0))

    assert win.getControl(infowindow.BACKGROUND).image == 'unchanged'


def test_onaction_with_select_focused_but_no_items_does_not_crash(load_infowindow):
    ctx = load_infowindow()
    infowindow = ctx.infowindow
    import xbmcgui
    win = infowindow.ShowcaseWindow('ShowcaseWindow.xml', '/addon/path', 'Default', '1080i')
    win.metas = [_make_meta('tt1', 'One')]
    # Focus SELECT directly without ever populating it via onInit(),
    # simulating a focused-but-empty coverflow control.
    win.setFocusId(infowindow.SELECT)

    win.onAction(xbmcgui.Action(0))  # must not raise on getSelectedItem() -> None

    assert win.getControl(infowindow.BACKGROUND).image is None


@pytest.mark.parametrize('action_id', [9, 10, 92], ids=['nav-back', 'previous-menu', 'backspace'])
def test_onaction_back_actions_close_the_window(load_infowindow, action_id):
    ctx = load_infowindow()
    infowindow = ctx.infowindow
    import xbmcgui
    win = infowindow.ShowcaseWindow('ShowcaseWindow.xml', '/addon/path', 'Default', '1080i')
    win.metas = [_make_meta('tt1', 'One')]
    win.onInit()

    win.onAction(xbmcgui.Action(action_id))

    assert win.closed is True
    assert win.selected is None


def test_onaction_non_back_action_does_not_close(load_infowindow):
    ctx = load_infowindow()
    infowindow = ctx.infowindow
    import xbmcgui
    win = infowindow.ShowcaseWindow('ShowcaseWindow.xml', '/addon/path', 'Default', '1080i')
    win.metas = [_make_meta('tt1', 'One')]
    win.onInit()

    win.onAction(xbmcgui.Action(1))  # ACTION_MOVE_LEFT-ish, not a back action

    assert win.closed is False


def test_onaction_info_key_is_a_noop_and_does_not_close_the_window(load_infowindow):
    ctx = load_infowindow()
    infowindow = ctx.infowindow
    import xbmcgui
    win = infowindow.ShowcaseWindow('ShowcaseWindow.xml', '/addon/path', 'Default', '1080i')
    win.metas = [_make_meta('tt1', 'One')]
    win.onInit()

    win.onAction(xbmcgui.Action(11))  # ACTION_SHOW_INFO

    assert win.closed is False
    assert win.selected is None


# ---------------------------------------------------------------------------
# ShowcaseWindow.onClick() - selection-by-position / close button
# ---------------------------------------------------------------------------


def test_onclick_select_records_focused_meta_by_position_and_closes(load_infowindow):
    ctx = load_infowindow()
    infowindow = ctx.infowindow
    metas = [
        _make_meta('tt1', 'One', mtype='series'), _make_meta('tt2', 'Two', mtype='series'),
        _make_meta('tt3', 'Three', mtype='series'),
    ]
    win = infowindow.ShowcaseWindow('ShowcaseWindow.xml', '/addon/path', 'Default', '1080i')
    win.metas = list(metas)
    win.onInit()
    win.getControl(infowindow.SELECT).selected_index = 2

    win.onClick(infowindow.SELECT)

    assert win.selected == metas[2]
    assert win.closed is True


def test_onclick_close_button_closes_without_selecting(load_infowindow):
    ctx = load_infowindow()
    infowindow = ctx.infowindow
    win = infowindow.ShowcaseWindow('ShowcaseWindow.xml', '/addon/path', 'Default', '1080i')
    win.metas = [_make_meta('tt1', 'One')]
    win.onInit()

    win.onClick(infowindow.CLOSE)

    assert win.selected is None
    assert win.closed is True


def test_onclick_unknown_control_id_is_ignored(load_infowindow):
    ctx = load_infowindow()
    infowindow = ctx.infowindow
    win = infowindow.ShowcaseWindow('ShowcaseWindow.xml', '/addon/path', 'Default', '1080i')
    win.metas = [_make_meta('tt1', 'One')]
    win.onInit()

    win.onClick(99999)

    assert win.selected is None
    assert win.closed is False


def test_onclick_select_with_movie_opens_streams_directly_with_heading_and_art(
    load_infowindow, monkeypatch,
):
    ctx = load_infowindow()
    infowindow = ctx.infowindow
    meta = _make_meta(
        'tt1', 'A Movie', mtype='movie',
        poster='https://x/poster.jpg', background='https://x/fanart.jpg',
    )
    win = infowindow.ShowcaseWindow('ShowcaseWindow.xml', '/addon/path', 'Default', '1080i')
    win.metas = [meta]
    win.onInit()
    captured = {}

    def fake_open_streams(stype, sid, poster=None, heading='', art=None, meta=None):
        captured['args'] = (stype, sid)
        captured['poster'] = poster
        captured['heading'] = heading
        captured['art'] = art
        return True

    monkeypatch.setattr(ctx.streamswindow, 'open_streams', fake_open_streams)

    win.onClick(infowindow.SELECT)

    assert captured['args'] == ('movie', 'tt1')
    assert captured['poster'] == 'https://x/poster.jpg'
    assert captured['heading'] == 'A Movie'
    assert captured['art'] == {'poster': 'https://x/poster.jpg', 'fanart': 'https://x/fanart.jpg'}
    # Fully handled internally - nothing left for the caller (open_showcase())
    # to act on, same as closing the overlay without picking anything.
    assert win.selected is None
    assert win.closed is True


def test_onclick_select_with_movie_falls_back_to_logo_then_poster_for_fanart(
    load_infowindow, monkeypatch,
):
    ctx = load_infowindow()
    infowindow = ctx.infowindow
    meta = _make_meta('tt1', 'A Movie', mtype='movie', poster='https://x/poster.jpg', logo='https://x/logo.png')
    win = infowindow.ShowcaseWindow('ShowcaseWindow.xml', '/addon/path', 'Default', '1080i')
    win.metas = [meta]
    win.onInit()
    captured = {}
    monkeypatch.setattr(
        ctx.streamswindow, 'open_streams',
        lambda stype, sid, poster=None, heading='', art=None, meta=None: captured.update(art=art) or True,
    )

    win.onClick(infowindow.SELECT)

    assert captured['art'] == {'poster': 'https://x/poster.jpg', 'fanart': 'https://x/logo.png'}


def test_onclick_select_with_non_movie_type_does_not_open_streams(load_infowindow, monkeypatch):
    ctx = load_infowindow()
    infowindow = ctx.infowindow
    meta = _make_meta('tt1', 'A Show', mtype='series')
    win = infowindow.ShowcaseWindow('ShowcaseWindow.xml', '/addon/path', 'Default', '1080i')
    win.metas = [meta]
    win.onInit()

    def _unexpected(*args, **kwargs):
        raise AssertionError('a series must not take the movie shortcut')

    monkeypatch.setattr(ctx.streamswindow, 'open_streams', _unexpected)

    win.onClick(infowindow.SELECT)

    assert win.selected == meta
    assert win.closed is True


# ---------------------------------------------------------------------------
# ShowcaseWindow.start() - the doModal()/empty-metas contract
# ---------------------------------------------------------------------------


def test_start_with_empty_metas_returns_none_without_domodal(load_infowindow):
    ctx = load_infowindow()
    infowindow = ctx.infowindow
    win = infowindow.ShowcaseWindow('ShowcaseWindow.xml', '/addon/path', 'Default', '1080i')

    result = win.start([])

    assert result is None
    assert win.modal_calls == 0


def test_start_with_none_metas_returns_none_without_domodal(load_infowindow):
    ctx = load_infowindow()
    infowindow = ctx.infowindow
    win = infowindow.ShowcaseWindow('ShowcaseWindow.xml', '/addon/path', 'Default', '1080i')

    result = win.start(None)

    assert result is None
    assert win.modal_calls == 0


def test_start_with_metas_calls_domodal_and_returns_the_selected_meta(load_infowindow):
    ctx = load_infowindow()
    infowindow = ctx.infowindow
    metas = [_make_meta('tt1', 'One', mtype='series'), _make_meta('tt2', 'Two', mtype='series')]
    win = infowindow.ShowcaseWindow('ShowcaseWindow.xml', '/addon/path', 'Default', '1080i')

    # The fake doModal() is a no-op counter; simulate what a real modal
    # event loop would drive (onInit(), the user scrolling + clicking)
    # around it, exactly as Kodi calls back into the window.
    real_domodal = win.doModal

    def fake_domodal():
        real_domodal()
        win.onInit()
        win.getControl(infowindow.SELECT).selected_index = 1
        win.onClick(infowindow.SELECT)

    win.doModal = fake_domodal

    result = win.start(metas)

    assert result == metas[1]
    assert win.modal_calls == 1


def test_start_resets_selected_and_metas_on_each_call(load_infowindow):
    ctx = load_infowindow()
    infowindow = ctx.infowindow
    win = infowindow.ShowcaseWindow('ShowcaseWindow.xml', '/addon/path', 'Default', '1080i')
    win.selected = _make_meta('stale', 'Stale leftover from a previous run')

    result = win.start([])

    assert result is None
    assert win.selected is None
    assert win.metas == []


# ---------------------------------------------------------------------------
# open_showcase() - builds and runs the ShowcaseWindow modal
# ---------------------------------------------------------------------------


def test_open_showcase_resolves_addon_path_and_delegates_to_start(load_infowindow, monkeypatch):
    ctx = load_infowindow(addon_info={'path': 'special://home/addons/plugin.video.rivulet'})
    infowindow = ctx.infowindow
    metas = [_make_meta('tt1', 'One')]
    captured = {}

    class RecordingWindow(infowindow.ShowcaseWindow):
        def __init__(self, *args, **kwargs):
            captured['init_args'] = args
            super().__init__(*args, **kwargs)

        def start(self, passed_metas, catalog_title=None):
            captured['start_metas'] = passed_metas
            captured['start_catalog_title'] = catalog_title
            return passed_metas[0]

    monkeypatch.setattr(infowindow, 'ShowcaseWindow', RecordingWindow)

    result = infowindow.open_showcase(metas)

    assert captured['init_args'] == (
        'ShowcaseWindow.xml', 'special://home/addons/plugin.video.rivulet', 'Default', '1080i'
    )
    assert captured['start_metas'] == metas
    assert captured['start_catalog_title'] is None
    assert result == metas[0]


def test_open_showcase_passes_catalog_title_through_to_start(load_infowindow, monkeypatch):
    ctx = load_infowindow(addon_info={'path': '/addon/path'})
    infowindow = ctx.infowindow
    metas = [_make_meta('tt1', 'One')]
    captured = {}

    class RecordingWindow(infowindow.ShowcaseWindow):
        def start(self, passed_metas, catalog_title=None):
            captured['catalog_title'] = catalog_title
            return None

    monkeypatch.setattr(infowindow, 'ShowcaseWindow', RecordingWindow)

    infowindow.open_showcase(metas, catalog_title='Cinemeta \u00b7 Popular Movies')

    assert captured['catalog_title'] == 'Cinemeta \u00b7 Popular Movies'


def test_open_showcase_closes_the_window_exactly_once_and_reraises_when_start_raises(
    load_infowindow, monkeypatch,
):
    ctx = load_infowindow(addon_info={'path': '/addon/path'})
    infowindow = ctx.infowindow
    metas = [_make_meta('tt1', 'One')]
    captured = {}

    class ExplodingWindow(infowindow.ShowcaseWindow):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.close_calls = 0
            captured['window'] = self

        def close(self):
            self.close_calls += 1
            super().close()

        def start(self, passed_metas, catalog_title=None):
            # Stands in for a crash inside onInit()/onAction() while the
            # modal loop is running - self.close() (the window's own,
            # normal-path close) never gets a chance to run. Every caller
            # (catalogpicker._open_catalog, searchwindow._run_search,
            # librarywindow.open_library) already wraps open_showcase() in
            # its own try/except, so the exception must keep propagating here.
            raise RuntimeError('coverflow blew up')

    monkeypatch.setattr(infowindow, 'ShowcaseWindow', ExplodingWindow)

    with pytest.raises(RuntimeError, match='coverflow blew up'):
        infowindow.open_showcase(metas)

    win = captured['window']
    assert win.close_calls == 1
    assert win.closed is True


def test_open_showcase_with_empty_metas_returns_none(load_infowindow):
    ctx = load_infowindow(addon_info={'path': '/addon/path'})
    assert ctx.infowindow.open_showcase([]) is None


# ---------------------------------------------------------------------------
# _enrich_focused()/_enrich_worker()/_apply_enriched() - lazy meta enrichment
#
# Stremio's `catalog` resource returns meta *previews* (no description/genres),
# so the focused poster's full meta is fetched lazily. The worker never touches
# a ListItem: it merges into self.metas[index] and queues props for the UI
# thread to apply, which is what keeps a fetch landing across a reopen from
# writing to a detached item. These drive the worker synchronously (by calling
# _enrich_worker directly, or via a Thread that is joined) with _fetch_meta
# monkeypatched - no real threading races, no network.
# ---------------------------------------------------------------------------


def _sparse(mid='tt1', name='Sparse', **extra):
    """A catalog preview: id/name/type only, no description or genres."""
    return _make_meta(mid, name, **extra)


def _spawned(monkeypatch, ctx, win):
    """Record (index, meta) each _spawn_enrich call would hand to a thread,
    instead of actually spawning one."""
    calls = []
    monkeypatch.setattr(
        type(win), '_spawn_enrich',
        lambda self, index, meta: calls.append((index, meta)),
    )
    return calls


def test_enrich_fires_for_item_zero_from_oninit(load_infowindow, monkeypatch):
    ctx = load_infowindow()
    win = ctx.infowindow.ShowcaseWindow()
    win.metas = [_sparse()]
    win._reset_enrich_state()
    calls = _spawned(monkeypatch, ctx, win)

    win.onInit()

    # onAction() never fires for the item the window opens focused, so
    # onInit() must enrich item 0 itself - and without the settle delay,
    # since that item is already settled.
    assert [index for index, _ in calls] == [0]


def test_enrich_fires_on_focus_change_from_onaction(load_infowindow, monkeypatch):
    ctx = load_infowindow()
    win = ctx.infowindow.ShowcaseWindow()
    win.metas = [_sparse('tt1', 'One'), _sparse('tt2', 'Two')]
    win._reset_enrich_state()
    win.onInit()
    calls = _spawned(monkeypatch, ctx, win)
    control = win.getControl(ctx.infowindow.SELECT)
    control.selected_index = 1
    win.setFocusId(ctx.infowindow.SELECT)

    win.onAction(ctx.infowindow.xbmcgui.Action(0))

    # A focus change arms the settle timer rather than fetching inline, so
    # scrolling past an item does not fetch it. Nothing is marked until a
    # worker actually has a slot to fetch with. Run the timer's callback to
    # stand in for the 200ms elapsing.
    assert win._enriched == set()
    timer = win._enrich_timer
    assert timer is not None
    timer.cancel()
    timer.function(*timer.args)

    assert [index for index, _ in calls] == [1]


def test_enrich_settle_timer_is_superseded_by_the_next_focus_change(load_infowindow, monkeypatch):
    """Scrolling past items must not leave a fetch armed for each one."""
    ctx = load_infowindow()
    win = ctx.infowindow.ShowcaseWindow()
    win.metas = [_sparse('tt1', 'One'), _sparse('tt2', 'Two'), _sparse('tt3', 'Three')]
    win._reset_enrich_state()
    win.onInit()
    calls = _spawned(monkeypatch, ctx, win)
    control = win.getControl(ctx.infowindow.SELECT)
    win.setFocusId(ctx.infowindow.SELECT)

    control.selected_index = 1
    win.onAction(ctx.infowindow.xbmcgui.Action(0))
    first_timer = win._enrich_timer
    control.selected_index = 2
    win.onAction(ctx.infowindow.xbmcgui.Action(0))

    # The timer armed for item 1 was cancelled when focus moved on. (cancel()
    # only stops the callback firing - the thread itself lingers until its
    # wait unblocks - so assert on the effect, not on is_alive().)
    assert first_timer is not win._enrich_timer
    first_timer.join(timeout=1)
    assert calls == []
    # ...so only the item focus settled on actually fetches.
    timer = win._enrich_timer
    timer.cancel()
    timer.function(*timer.args)
    assert [index for index, _ in calls] == [2]


def test_enrich_skips_a_meta_that_already_has_a_description(load_infowindow, monkeypatch):
    ctx = load_infowindow()
    win = ctx.infowindow.ShowcaseWindow()
    win.metas = [_sparse(description='Already complete.')]
    win._reset_enrich_state()
    calls = _spawned(monkeypatch, ctx, win)

    win.onInit()

    # Discover's catalogs usually carry a description already: no fetch, but
    # still marked so a later focus does not reconsider it.
    assert calls == []
    assert win._enriched == {0}


def test_enrich_skips_an_index_it_has_already_handled(load_infowindow, monkeypatch):
    ctx = load_infowindow()
    win = ctx.infowindow.ShowcaseWindow()
    win.metas = [_sparse()]
    win._reset_enrich_state()
    win._enriched = {0}
    calls = _spawned(monkeypatch, ctx, win)

    win.onInit()

    assert calls == []


def test_enrich_ignores_an_item_whose_position_is_unparseable(load_infowindow, monkeypatch):
    ctx = load_infowindow()
    win = ctx.infowindow.ShowcaseWindow()
    win.metas = [_sparse()]
    win._reset_enrich_state()
    calls = _spawned(monkeypatch, ctx, win)
    item = ctx.infowindow.xbmcgui.ListItem('No position')
    item.setProperty('position', 'not-a-number')

    win._enrich_focused(item)

    assert calls == []
    assert win._enriched == set()


def test_enrich_ignores_a_position_outside_the_meta_list(load_infowindow, monkeypatch):
    ctx = load_infowindow()
    win = ctx.infowindow.ShowcaseWindow()
    win.metas = [_sparse()]
    win._reset_enrich_state()
    calls = _spawned(monkeypatch, ctx, win)
    item = ctx.infowindow.xbmcgui.ListItem('Out of range')
    item.setProperty('position', '7')

    win._enrich_focused(item)

    assert calls == []
    # An out-of-range position must not poison the index either.
    assert win._enriched == set()


def test_enrich_ignores_a_meta_without_an_id(load_infowindow, monkeypatch):
    ctx = load_infowindow()
    win = ctx.infowindow.ShowcaseWindow()
    win.metas = [{'name': 'No id', 'type': 'movie'}]
    win._reset_enrich_state()
    calls = _spawned(monkeypatch, ctx, win)

    win.onInit()

    assert calls == []


def test_enrich_worker_merges_into_metas_and_queues_props(load_infowindow, monkeypatch):
    ctx = load_infowindow()
    win = ctx.infowindow.ShowcaseWindow()
    win.metas = [_sparse()]
    win._reset_enrich_state()
    monkeypatch.setattr(
        ctx.views, '_fetch_meta',
        lambda stype, sid: {
            'id': 'tt1',
            'description': 'A full plot.',
            'genres': ['Drama', 'Thriller'],
            'imdbRating': '7.9',
            'releaseInfo': '2026',
        },
    )

    win._enrich_worker(0, win.metas[0])

    # Merged onto the meta, so a rebuild re-derives it...
    assert win.metas[0]['description'] == 'A full plot.'
    assert win.metas[0]['genres'] == ['Drama', 'Thriller']
    # ...and queued for the UI thread rather than written to a ListItem.
    assert win._enrich_pending == {
        0: {'genre': 'Drama, Thriller', 'rating': '7.9', 'plot': 'A full plot.', 'year': '2026'},
    }


def test_enrich_worker_leaves_fields_the_preview_already_had(load_infowindow, monkeypatch):
    ctx = load_infowindow()
    win = ctx.infowindow.ShowcaseWindow()
    win.metas = [_sparse(releaseInfo='1999', genres=['Comedy'])]
    win._reset_enrich_state()
    monkeypatch.setattr(
        ctx.views, '_fetch_meta',
        lambda stype, sid: {'description': 'Plot.', 'genres': ['Drama'], 'releaseInfo': '2026'},
    )

    win._enrich_worker(0, win.metas[0])

    assert win.metas[0]['releaseInfo'] == '1999'
    assert win.metas[0]['genres'] == ['Comedy']


def test_enrich_worker_coerces_a_string_genres_field(load_infowindow, monkeypatch):
    ctx = load_infowindow()
    win = ctx.infowindow.ShowcaseWindow()
    win.metas = [_sparse()]
    win._reset_enrich_state()
    # Unvalidated third-party JSON: a str is iterable, so joining it
    # directly would render "D, r, a, m, a".
    monkeypatch.setattr(
        ctx.views, '_fetch_meta',
        lambda stype, sid: {'description': 'Plot.', 'genres': 'Drama'},
    )

    win._enrich_worker(0, win.metas[0])

    assert 'genres' not in win.metas[0]
    assert win._enrich_pending[0]['genre'] == ''


def test_enrich_worker_stringifies_a_numeric_rating(load_infowindow, monkeypatch):
    ctx = load_infowindow()
    win = ctx.infowindow.ShowcaseWindow()
    win.metas = [_sparse()]
    win._reset_enrich_state()
    monkeypatch.setattr(
        ctx.views, '_fetch_meta',
        lambda stype, sid: {'description': 'Plot.', 'imdbRating': 8.0, 'releaseInfo': 2026},
    )

    win._enrich_worker(0, win.metas[0])

    assert win._enrich_pending[0]['rating'] == '8.0'
    assert win._enrich_pending[0]['year'] == '2026'


def test_enrich_worker_survives_a_fetch_that_raises(load_infowindow, monkeypatch):
    ctx = load_infowindow()
    win = ctx.infowindow.ShowcaseWindow()
    win.metas = [_sparse()]
    win._reset_enrich_state()

    def _boom(stype, sid):
        raise RuntimeError('addon exploded')

    monkeypatch.setattr(ctx.views, '_fetch_meta', _boom)

    win._enrich_worker(0, win.metas[0])  # must not raise

    assert win._enrich_pending == {}


def test_enrich_worker_ignores_an_empty_fetch_result(load_infowindow, monkeypatch):
    ctx = load_infowindow()
    win = ctx.infowindow.ShowcaseWindow()
    win.metas = [_sparse()]
    win._reset_enrich_state()
    monkeypatch.setattr(ctx.views, '_fetch_meta', lambda stype, sid: None)

    win._enrich_worker(0, win.metas[0])

    assert win._enrich_pending == {}


def test_apply_enriched_writes_queued_props_onto_the_live_listitem(load_infowindow, monkeypatch):
    ctx = load_infowindow()
    win = ctx.infowindow.ShowcaseWindow()
    win.metas = [_sparse()]
    win._reset_enrich_state()
    _spawned(monkeypatch, ctx, win)  # onInit() must not spawn a real worker
    win.onInit()
    win._enrich_pending = {0: {'genre': 'Drama', 'rating': '7.9', 'plot': 'Plot.', 'year': '2026'}}

    win._apply_enriched()

    item = win.getControl(ctx.infowindow.SELECT).getListItem(0)
    assert item.getProperty('plot') == 'Plot.'
    assert item.getProperty('genre') == 'Drama'
    assert win._enrich_pending == {}


def test_apply_enriched_drops_an_index_no_longer_in_the_list(load_infowindow, monkeypatch):
    ctx = load_infowindow()
    win = ctx.infowindow.ShowcaseWindow()
    win.metas = [_sparse()]
    win._reset_enrich_state()
    _spawned(monkeypatch, ctx, win)  # onInit() must not spawn a real worker
    win.onInit()
    win._enrich_pending = {5: {'plot': 'Stale.'}}

    win._apply_enriched()  # must not raise

    assert win._enrich_pending == {}


def test_enrichment_survives_a_reopen_for_playback(load_infowindow, monkeypatch):
    """A fetch landing across a reopen must reach the item on screen.

    onInit() rebuilds every ListItem (reset() + addItems), so a worker that
    closed over the handle it was spawned with would write to a detached
    object while the live one stayed blank - and never retry, because the
    index is already in _enriched.
    """
    ctx = load_infowindow()
    win = ctx.infowindow.ShowcaseWindow()
    win.metas = [_sparse()]
    win._reset_enrich_state()
    monkeypatch.setattr(
        ctx.views, '_fetch_meta',
        lambda stype, sid: {'description': 'Landed late.', 'genres': ['Drama']},
    )

    win.onInit()
    win._enrich_worker(0, win.metas[0])   # fetch completes...
    win.onInit()                          # ...and THEN the window reopens

    item = win.getControl(ctx.infowindow.SELECT).getListItem(0)
    # Rebuilt from self.metas, which the worker merged into.
    assert item.getProperty('plot') == 'Landed late.'


def test_enrich_worker_gives_up_its_index_when_no_slot_is_free(load_infowindow, monkeypatch):
    ctx = load_infowindow()
    win = ctx.infowindow.ShowcaseWindow()
    win.metas = [_sparse()]
    win._reset_enrich_state()
    fetched = []
    monkeypatch.setattr(
        ctx.views, '_fetch_meta',
        lambda stype, sid: fetched.append(sid),
    )
    # Exhaust the in-flight ceiling.
    for _ in range(ctx.infowindow._ENRICH_MAX_INFLIGHT):
        win._enrich_slots.acquire()

    win._enrich_worker(0, win.metas[0])

    assert fetched == []
    # Never entered the cache, so a later focus can retry it.
    assert win._enriched == set()


def test_start_clears_enrich_state_between_runs(load_infowindow):
    ctx = load_infowindow()
    win = ctx.infowindow.ShowcaseWindow()
    win.metas = [_sparse()]
    win._reset_enrich_state()
    win._enriched = {0, 1}
    win._enrich_pending = {0: {'plot': 'Stale.'}}

    win.start([_sparse('tt9', 'Fresh')])

    assert win._enriched == set()
    assert win._enrich_pending == {}


def test_enrich_worker_swallows_an_exception_from_its_own_imports(load_infowindow, monkeypatch):
    """The worker runs on a daemon thread nobody joins, so nothing can
    surface an exception it lets escape - pytest reports one as an
    unhandled thread exception against whichever test happens to be running
    when it fires. A worker still in flight while the interpreter (or, under
    test, the injected xbmc stubs) is torn down fails on `import xbmc`
    before it reaches any of its own error handling.
    """
    ctx = load_infowindow()
    win = ctx.infowindow.ShowcaseWindow()
    win.metas = [_sparse()]
    win._reset_enrich_state()

    def _torn_down(index, meta):
        raise ModuleNotFoundError("No module named 'xbmc'")

    monkeypatch.setattr(type(win), '_enrich_fetch', _torn_down)

    win._enrich_worker(0, win.metas[0])  # must not raise

    # ...and the slot it took is handed back, so enrichment still works after.
    assert win._enrich_slots.acquire(blocking=False) is True
    win._enrich_slots.release()


def test_enrich_worker_releases_its_slot_after_a_successful_fetch(load_infowindow, monkeypatch):
    ctx = load_infowindow()
    win = ctx.infowindow.ShowcaseWindow()
    win.metas = [_sparse()]
    win._reset_enrich_state()
    monkeypatch.setattr(ctx.views, '_fetch_meta', lambda stype, sid: {'description': 'Plot.'})

    for _ in range(ctx.infowindow._ENRICH_MAX_INFLIGHT + 1):
        win._enrich_worker(0, win.metas[0])

    assert win._enrich_pending[0]['plot'] == 'Plot.'


def test_enrich_retries_an_item_that_was_scrolled_past(load_infowindow, monkeypatch):
    """A cancelled settle timer must leave its item eligible again.

    Scrolling through a sparse catalog cancels each item's pending fetch as
    focus moves on. If arming had marked the index handled, the item you
    land on after a fast scroll would be skipped by the cache check for the
    life of the window - blank plot, no retry.
    """
    ctx = load_infowindow()
    win = ctx.infowindow.ShowcaseWindow()
    win.metas = [_sparse('tt1', 'One'), _sparse('tt2', 'Two'), _sparse('tt3', 'Three')]
    win._reset_enrich_state()
    win.onInit()
    calls = _spawned(monkeypatch, ctx, win)
    control = win.getControl(ctx.infowindow.SELECT)
    win.setFocusId(ctx.infowindow.SELECT)

    control.selected_index = 1
    win.onAction(ctx.infowindow.xbmcgui.Action(0))  # arms a fetch for item 1...
    control.selected_index = 2
    win.onAction(ctx.infowindow.xbmcgui.Action(0))  # ...which this cancels
    control.selected_index = 1
    win.onAction(ctx.infowindow.xbmcgui.Action(0))  # back to the skipped item

    timer = win._enrich_timer
    timer.cancel()
    timer.function(*timer.args)

    assert [index for index, _ in calls] == [1]


def test_enrich_worker_wakes_the_ui_thread_after_queueing(load_infowindow, monkeypatch):
    """Only onInit()/onAction() drain the queue, and Kodi calls neither
    without input - so a landed fetch would not reach the screen until the
    user's next keypress, by which time focus has moved off the item that
    was fetched. Action(noop) is posted to Kodi's application messenger and
    dispatched to this dialog's onAction() on the GUI thread.
    """
    ctx = load_infowindow()
    win = ctx.infowindow.ShowcaseWindow()
    win.metas = [_sparse()]
    win._reset_enrich_state()
    monkeypatch.setattr(ctx.views, '_fetch_meta', lambda stype, sid: {'description': 'Plot.'})

    win._enrich_worker(0, win.metas[0])

    assert win._enrich_pending[0]['plot'] == 'Plot.'
    assert ctx.env.executed_builtins == ['Action(noop)']


def test_enrich_worker_does_not_wake_the_ui_thread_with_nothing_to_apply(load_infowindow, monkeypatch):
    ctx = load_infowindow()
    win = ctx.infowindow.ShowcaseWindow()
    win.metas = [_sparse()]
    win._reset_enrich_state()
    monkeypatch.setattr(ctx.views, '_fetch_meta', lambda stype, sid: None)

    win._enrich_worker(0, win.metas[0])

    assert ctx.env.executed_builtins == []


def test_close_cancels_an_armed_settle_timer(load_infowindow, monkeypatch):
    """Selecting a poster, backing out, or a force-close for playback all
    reach close() - none of them should leave a timer armed to fetch for a
    window that is no longer on screen."""
    ctx = load_infowindow()
    win = ctx.infowindow.ShowcaseWindow()
    win.metas = [_sparse('tt1', 'One'), _sparse('tt2', 'Two')]
    win._reset_enrich_state()
    win.onInit()
    calls = _spawned(monkeypatch, ctx, win)
    control = win.getControl(ctx.infowindow.SELECT)
    control.selected_index = 1
    win.setFocusId(ctx.infowindow.SELECT)
    win.onAction(ctx.infowindow.xbmcgui.Action(0))
    timer = win._enrich_timer
    assert timer is not None

    win.onClick(ctx.infowindow.CLOSE)

    assert win._enrich_timer is None
    timer.join(timeout=1)
    assert calls == []


def test_enrich_worker_merges_released_when_the_preview_has_no_year(load_infowindow, monkeypatch):
    """_item_properties() derives `year` from releaseInfo *or* released, so
    a full meta that only dates itself with `released` must still fill the
    label."""
    ctx = load_infowindow()
    win = ctx.infowindow.ShowcaseWindow()
    win.metas = [_sparse()]
    win._reset_enrich_state()
    monkeypatch.setattr(
        ctx.views, '_fetch_meta',
        lambda stype, sid: {'description': 'Plot.', 'released': '1999-03-31T00:00:00.000Z'},
    )

    win._enrich_worker(0, win.metas[0])

    assert win._enrich_pending[0]['year'] == '1999-03-31'


def test_spawn_enrich_runs_the_worker_on_a_daemon_thread(load_infowindow, monkeypatch):
    """The fixture neuters _spawn_enrich for every other test, so this is
    the one place the real thread hand-off is exercised - joined, so it
    cannot outlive the test."""
    ctx = load_infowindow()
    win = ctx.infowindow.ShowcaseWindow()
    win.metas = [_sparse()]
    win._reset_enrich_state()
    monkeypatch.setattr(ctx.views, '_fetch_meta', lambda stype, sid: {'description': 'Plot.'})
    created = []
    real_thread = ctx.infowindow.threading.Thread

    def recording_thread(*args, **kwargs):
        thread = real_thread(*args, **kwargs)
        created.append(thread)
        return thread

    monkeypatch.setattr(ctx.infowindow.threading, 'Thread', recording_thread)

    ctx.real_spawn_enrich(win, 0, win.metas[0])

    assert len(created) == 1
    assert created[0].daemon  # Kodi must never wait on an enrich fetch to exit
    created[0].join(timeout=5)
    assert win._enrich_pending[0]['plot'] == 'Plot.'


def test_enrich_worker_survives_a_guard_that_cannot_even_log(load_infowindow, monkeypatch):
    """The last-resort guard reports what it swallowed, but reporting is
    itself allowed to fail - a torn-down interpreter is exactly when both
    happen."""
    ctx = load_infowindow()
    win = ctx.infowindow.ShowcaseWindow()
    win.metas = [_sparse()]
    win._reset_enrich_state()

    def _boom(*args, **kwargs):
        raise RuntimeError('nothing works any more')

    monkeypatch.setattr(type(win), '_enrich_fetch', _boom)
    monkeypatch.setattr(ctx.compat, 'log', _boom)

    win._enrich_worker(0, win.metas[0])  # must not raise

    assert win._enrich_slots.acquire(blocking=False) is True
    win._enrich_slots.release()


# ---------------------------------------------------------------------------
# ShowcaseWindow._open_credits() / open_credits_picker() - ACTION_CONTEXT_MENU
# (117), the cast & crew affordance. ShowcaseWindow is the only place a
# movie's cast/crew can be reached in the custom-window path - DetailWindow
# only ever shows for a series and reuses this exact shared function from
# self.meta with no fetch (see test_detailwindow.py's own, much smaller,
# section for that wiring).
# ---------------------------------------------------------------------------


def _link(name, category, url):
    return {'name': name, 'category': category, 'url': url}


class _Store:
    def __init__(self, addons):
        self._addons = addons

    def get_addons(self):
        return self._addons


def _stub_choose(monkeypatch, ctx, answers=None, capture=None):
    """Patches `lib.ui.dialogs.choose` directly (already exhaustively
    covered by tests/test_dialogs.py) to pop successive `answers`
    (default: a single -1, i.e. cancelled), recording each
    `(heading, rows)` call into `capture` if given."""
    queue = list(answers) if answers is not None else [-1]

    def _choose(heading, rows):
        if capture is not None:
            capture.append((heading, list(rows)))
        return queue.pop(0) if queue else -1

    monkeypatch.setattr(ctx.dialogs, 'choose', _choose)


def _showcase_window(ctx, metas, focus_index=0):
    win = ctx.infowindow.ShowcaseWindow('ShowcaseWindow.xml', '/addon/path', 'Default', '1080i')
    win.metas = list(metas)
    win.onInit()
    win.getControl(ctx.infowindow.SELECT).selected_index = focus_index
    return win


def _fire_context_menu(win, ctx):
    import xbmcgui
    win.onAction(xbmcgui.Action(ctx.infowindow._CONTEXT_MENU_ACTION))


def test_context_menu_fetches_full_meta_and_opens_select_with_category_name_labels(
    load_infowindow, monkeypatch,
):
    ctx = load_infowindow()
    full_meta = {
        'id': 'tt1', 'name': 'The Godfather', 'type': 'movie',
        'links': [
            _link('Francis Ford Coppola', 'Directors', 'stremio:///search?search=Francis'),
            _link('Marlon Brando', 'Cast', 'stremio:///search?search=Brando'),
        ],
    }
    fetched = []
    monkeypatch.setattr(ctx.views, '_fetch_meta', lambda stype, sid: fetched.append((stype, sid)) or full_meta)
    monkeypatch.setattr(ctx.dependencies, 'get_store', lambda: object())
    monkeypatch.setattr(ctx.dependencies, 'get_client', lambda: object())
    captured = []
    _stub_choose(monkeypatch, ctx, capture=captured)
    win = _showcase_window(ctx, [_make_meta('tt1', 'The Godfather', mtype='movie')])

    _fire_context_menu(win, ctx)

    assert fetched == [('movie', 'tt1')]
    assert captured == [('STR30196', [('Francis Ford Coppola', 'Directors'), ('Marlon Brando', 'Cast')])]


def test_context_menu_person_entry_runs_run_query_and_opens_results(load_infowindow, monkeypatch):
    ctx = load_infowindow()
    full_meta = {'id': 'tt1', 'type': 'movie', 'links': [_link('Marlon Brando', 'Cast', 'stremio:///search?search=Brando')]}
    monkeypatch.setattr(ctx.views, '_fetch_meta', lambda stype, sid: full_meta)
    store, client = object(), object()
    monkeypatch.setattr(ctx.dependencies, 'get_store', lambda: store)
    monkeypatch.setattr(ctx.dependencies, 'get_client', lambda: client)
    _stub_choose(monkeypatch, ctx, answers=[0])
    run_query_calls = []
    person_metas = [{'id': 'tt2', 'name': 'One-Eyed Jacks', 'type': 'movie'}]

    def _run_query(passed_store, passed_client, query):
        run_query_calls.append((passed_store, passed_client, query))
        return person_metas

    monkeypatch.setattr(ctx.searchwindow, 'run_query', _run_query)
    opened = []
    monkeypatch.setattr(ctx.infowindow, 'open_showcase', lambda metas: opened.append(metas) or None)
    win = _showcase_window(ctx, [_make_meta('tt1', 'The Godfather', mtype='movie')])

    _fire_context_menu(win, ctx)

    assert run_query_calls == [(store, client, 'Brando')]
    assert opened == [person_metas]


def test_context_menu_person_entry_with_no_results_notifies(load_infowindow, monkeypatch):
    ctx = load_infowindow()
    full_meta = {'id': 'tt1', 'type': 'movie', 'links': [_link('Marlon Brando', 'Cast', 'stremio:///search?search=Brando')]}
    monkeypatch.setattr(ctx.views, '_fetch_meta', lambda stype, sid: full_meta)
    monkeypatch.setattr(ctx.dependencies, 'get_store', lambda: object())
    monkeypatch.setattr(ctx.dependencies, 'get_client', lambda: object())
    _stub_choose(monkeypatch, ctx, answers=[0])
    monkeypatch.setattr(ctx.searchwindow, 'run_query', lambda store, client, query: [])
    opened = []
    monkeypatch.setattr(ctx.infowindow, 'open_showcase', lambda metas: opened.append(metas))
    win = _showcase_window(ctx, [_make_meta('tt1', 'The Godfather', mtype='movie')])

    _fire_context_menu(win, ctx)

    assert opened == []
    assert ctx.env.notifications == [('Rivulet', 'STR30030', 'info', 4000)]


def test_context_menu_genre_entry_with_installed_transport_fetches_catalog_and_opens_results(
    load_infowindow, monkeypatch,
):
    from urllib.parse import quote

    ctx = load_infowindow()
    transport = 'https://a.example/manifest.json'
    url = 'stremio:///discover/%s/movie/top?genre=Drama' % quote(transport, safe='')
    full_meta = {'id': 'tt1', 'type': 'movie', 'links': [_link('Drama', 'Genres', url)]}
    monkeypatch.setattr(ctx.views, '_fetch_meta', lambda stype, sid: full_meta)
    monkeypatch.setattr(ctx.dependencies, 'get_store', lambda: _Store([{'transportUrl': transport}]))
    monkeypatch.setattr(ctx.dependencies, 'get_client', lambda: object())
    _stub_choose(monkeypatch, ctx, answers=[0])
    fetch_calls = []
    genre_metas = [{'id': 'tt9', 'name': 'Some Drama', 'type': 'movie'}]

    def _fetch_catalog(transport_url, ctype, cid, extra=None):
        fetch_calls.append((transport_url, ctype, cid, extra))
        return genre_metas

    monkeypatch.setattr(ctx.views, '_fetch_catalog', _fetch_catalog)
    opened = []
    monkeypatch.setattr(ctx.infowindow, 'open_showcase', lambda metas: opened.append(metas) or None)
    win = _showcase_window(ctx, [_make_meta('tt1', 'The Godfather', mtype='movie')])

    _fire_context_menu(win, ctx)

    assert fetch_calls == [(transport, 'movie', 'top', [('genre', 'Drama')])]
    assert opened == [genre_metas]


def test_context_menu_genre_entry_with_uninstalled_transport_is_skipped_and_logged(
    load_infowindow, monkeypatch,
):
    from urllib.parse import quote

    ctx = load_infowindow()
    transport = 'https://a.example/manifest.json'
    url = 'stremio:///discover/%s/movie/top?genre=Drama' % quote(transport, safe='')
    full_meta = {'id': 'tt1', 'type': 'movie', 'links': [_link('Drama', 'Genres', url)]}
    monkeypatch.setattr(ctx.views, '_fetch_meta', lambda stype, sid: full_meta)
    monkeypatch.setattr(ctx.dependencies, 'get_store', lambda: _Store([]))  # nothing installed
    monkeypatch.setattr(ctx.dependencies, 'get_client', lambda: object())
    _stub_choose(monkeypatch, ctx, answers=[0])
    fetch_calls = []
    monkeypatch.setattr(ctx.views, '_fetch_catalog', lambda *a, **k: fetch_calls.append((a, k)) or [])
    win = _showcase_window(ctx, [_make_meta('tt1', 'The Godfather', mtype='movie')])

    _fire_context_menu(win, ctx)

    assert fetch_calls == []
    assert any('not installed' in msg for msg, _level in ctx.env.log_calls)


def test_context_menu_with_no_usable_links_notifies_and_shows_no_picker(load_infowindow, monkeypatch):
    ctx = load_infowindow()
    monkeypatch.setattr(ctx.views, '_fetch_meta', lambda stype, sid: {'id': 'tt1', 'type': 'movie'})  # no links
    monkeypatch.setattr(ctx.dependencies, 'get_store', lambda: object())
    monkeypatch.setattr(ctx.dependencies, 'get_client', lambda: object())
    captured = []
    _stub_choose(monkeypatch, ctx, capture=captured)
    win = _showcase_window(ctx, [_make_meta('tt1', 'The Godfather', mtype='movie')])

    _fire_context_menu(win, ctx)

    assert captured == []
    assert ctx.env.notifications == [('Rivulet', 'STR30197', 'info', 4000)]


def test_context_menu_cancelled_select_does_nothing(load_infowindow, monkeypatch):
    ctx = load_infowindow()
    full_meta = {'id': 'tt1', 'type': 'movie', 'links': [_link('Marlon Brando', 'Cast', 'stremio:///search?search=Brando')]}
    monkeypatch.setattr(ctx.views, '_fetch_meta', lambda stype, sid: full_meta)
    monkeypatch.setattr(ctx.dependencies, 'get_store', lambda: object())
    monkeypatch.setattr(ctx.dependencies, 'get_client', lambda: object())
    _stub_choose(monkeypatch, ctx)  # default answers=[-1]
    run_query_calls = []
    monkeypatch.setattr(ctx.searchwindow, 'run_query', lambda *a: run_query_calls.append(a))
    win = _showcase_window(ctx, [_make_meta('tt1', 'The Godfather', mtype='movie')])

    _fire_context_menu(win, ctx)

    assert run_query_calls == []
    assert ctx.env.notifications == []


def test_context_menu_detail_entry_opens_detailwindow_directly(load_infowindow, monkeypatch):
    ctx = load_infowindow()
    full_meta = {
        'id': 'tt1', 'type': 'movie',
        'links': [_link('The Godfather Part II', 'Related', 'stremio:///detail/movie/tt0071562')],
    }
    monkeypatch.setattr(ctx.views, '_fetch_meta', lambda stype, sid: full_meta)
    monkeypatch.setattr(ctx.dependencies, 'get_store', lambda: object())
    monkeypatch.setattr(ctx.dependencies, 'get_client', lambda: object())
    _stub_choose(monkeypatch, ctx, answers=[0])
    captured = []
    monkeypatch.setattr(ctx.detailwindow, 'open_detail', lambda stype, sid: captured.append((stype, sid)))
    win = _showcase_window(ctx, [_make_meta('tt1', 'The Godfather', mtype='movie')])

    _fire_context_menu(win, ctx)

    assert captured == [('movie', 'tt0071562')]


def test_context_menu_with_select_not_focused_does_nothing(load_infowindow, monkeypatch):
    """`_open_credits()` reuses onAction()'s own SELECT-focus gate - the
    same one guarding the background swap above it - rather than a second
    "what's focused" mechanism."""
    ctx = load_infowindow()
    fetched = []
    monkeypatch.setattr(ctx.views, '_fetch_meta', lambda stype, sid: fetched.append((stype, sid)) or {})
    win = _showcase_window(ctx, [_make_meta('tt1', 'The Godfather', mtype='movie')])
    win.setFocusId(ctx.infowindow.CLOSE)

    _fire_context_menu(win, ctx)

    assert fetched == []
