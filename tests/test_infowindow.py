"""Tests for lib.ui.infowindow: the fullscreen coverflow overlay
(ShowcaseWindow) lib.ui.views.showcase() opens over one catalog page,
exercised against the shared fake xbmc/xbmcgui stubs in tests/kodistubs
(no real Kodi runtime, no network).

lib.ui.infowindow imports xbmcgui at module scope (`class ShowcaseWindow
(xbmcgui.WindowXMLDialog)`), so even `_item_properties()` - a pure
function that touches no xbmc API itself - needs the module imported
fresh against the fake xbmcgui (via `load_infowindow`) before it is
reachable at all.

ShowcaseWindow's onInit()/onClick()/onAction() are called directly here,
never through a real modal event loop: tests/kodistubs's fake
WindowXMLDialog.doModal() is a no-op counter, and getControl()/
setFocusId()/getFocusId() are plain in-memory fakes (see
tests/kodistubs/modules.py's make_xbmcgui). This exercises 100% of the
controller *logic* (item building, focus-driven background swaps, back
actions, selection-by-position, the empty-metas short-circuit) with
none of the *visual* rendering.

ShowcaseWindow.xml's actual skin rendering - the coverflow's fixedlist/
focusedlayout geometry, the fanart crossfade, the WindowOpen/WindowClose
slide+fade animations - is Kodi-skin-engine-only and is NOT, and cannot
be, exercised by this suite. Confirming it renders/scrolls/animates
correctly requires manually opening the overlay on a real Kodi install.
"""
import contextlib

import pytest

from tests.kodistubs import install_kodi_stubs

_RELOAD_MODULE_NAMES = ('lib.ui.compat', 'lib.ui.infowindow')


@pytest.fixture
def load_infowindow():
    """Factory fixture: `load_infowindow(addon_info=None)` installs fresh
    stubs (via tests.kodistubs.install_kodi_stubs) reloading
    lib.ui.compat/lib.ui.infowindow, and returns a namespace with
    `.infowindow`, `.compat`, and `.env`. Every call is torn down
    automatically, in reverse order, at test end.
    """
    with contextlib.ExitStack() as stack:
        def _load(addon_info=None):
            return stack.enter_context(install_kodi_stubs(
                reload=_RELOAD_MODULE_NAMES,
                addon_info=addon_info,
            ))

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
    }
    assert ctx.infowindow._item_properties(meta) == {
        'thumbnail': 'https://x/poster.jpg',
        'fanart': 'https://x/bg.jpg',
        'genre': 'Action, Sci-Fi',
        'rating': '8.4',
        'plot': 'A plot.',
        'year': '2019',
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


def test_item_properties_missing_fields_are_empty_strings(load_infowindow):
    ctx = load_infowindow()
    assert ctx.infowindow._item_properties({}) == {
        'thumbnail': '', 'fanart': '', 'genre': '', 'rating': '', 'plot': '', 'year': '',
    }


def test_item_properties_none_meta_is_treated_as_empty(load_infowindow):
    ctx = load_infowindow()
    assert ctx.infowindow._item_properties(None) == ctx.infowindow._item_properties({})


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
    win = infowindow.ShowcaseWindow('ShowcaseWindow.xml', '/addon/path', 'Default', '720p')
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


def test_oninit_item_label_falls_back_to_id_then_placeholder(load_infowindow):
    ctx = load_infowindow()
    infowindow = ctx.infowindow
    win = infowindow.ShowcaseWindow('ShowcaseWindow.xml', '/addon/path', 'Default', '720p')
    win.metas = [{'id': 'tt9'}, {}]

    win.onInit()

    items = win.getControl(infowindow.SELECT).items
    assert items[0].getLabel() == 'tt9'
    assert items[1].getLabel() == '?'


def test_oninit_with_no_metas_is_a_no_op(load_infowindow):
    ctx = load_infowindow()
    infowindow = ctx.infowindow
    win = infowindow.ShowcaseWindow('ShowcaseWindow.xml', '/addon/path', 'Default', '720p')
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
    win = infowindow.ShowcaseWindow('ShowcaseWindow.xml', '/addon/path', 'Default', '720p')
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
    win = infowindow.ShowcaseWindow('ShowcaseWindow.xml', '/addon/path', 'Default', '720p')
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
    win = infowindow.ShowcaseWindow('ShowcaseWindow.xml', '/addon/path', 'Default', '720p')
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
    win = infowindow.ShowcaseWindow('ShowcaseWindow.xml', '/addon/path', 'Default', '720p')
    win.metas = [_make_meta('tt1', 'One')]
    win.onInit()

    win.onAction(xbmcgui.Action(action_id))

    assert win.closed is True
    assert win.selected is None


def test_onaction_non_back_action_does_not_close(load_infowindow):
    ctx = load_infowindow()
    infowindow = ctx.infowindow
    import xbmcgui
    win = infowindow.ShowcaseWindow('ShowcaseWindow.xml', '/addon/path', 'Default', '720p')
    win.metas = [_make_meta('tt1', 'One')]
    win.onInit()

    win.onAction(xbmcgui.Action(1))  # ACTION_MOVE_LEFT-ish, not a back action

    assert win.closed is False


# ---------------------------------------------------------------------------
# ShowcaseWindow.onClick() - selection-by-position / close button
# ---------------------------------------------------------------------------


def test_onclick_select_records_focused_meta_by_position_and_closes(load_infowindow):
    ctx = load_infowindow()
    infowindow = ctx.infowindow
    metas = [_make_meta('tt1', 'One'), _make_meta('tt2', 'Two'), _make_meta('tt3', 'Three')]
    win = infowindow.ShowcaseWindow('ShowcaseWindow.xml', '/addon/path', 'Default', '720p')
    win.metas = list(metas)
    win.onInit()
    win.getControl(infowindow.SELECT).selected_index = 2

    win.onClick(infowindow.SELECT)

    assert win.selected == metas[2]
    assert win.closed is True


def test_onclick_close_button_closes_without_selecting(load_infowindow):
    ctx = load_infowindow()
    infowindow = ctx.infowindow
    win = infowindow.ShowcaseWindow('ShowcaseWindow.xml', '/addon/path', 'Default', '720p')
    win.metas = [_make_meta('tt1', 'One')]
    win.onInit()

    win.onClick(infowindow.CLOSE)

    assert win.selected is None
    assert win.closed is True


def test_onclick_unknown_control_id_is_ignored(load_infowindow):
    ctx = load_infowindow()
    infowindow = ctx.infowindow
    win = infowindow.ShowcaseWindow('ShowcaseWindow.xml', '/addon/path', 'Default', '720p')
    win.metas = [_make_meta('tt1', 'One')]
    win.onInit()

    win.onClick(99999)

    assert win.selected is None
    assert win.closed is False


# ---------------------------------------------------------------------------
# ShowcaseWindow.start() - the doModal()/empty-metas contract
# ---------------------------------------------------------------------------


def test_start_with_empty_metas_returns_none_without_domodal(load_infowindow):
    ctx = load_infowindow()
    infowindow = ctx.infowindow
    win = infowindow.ShowcaseWindow('ShowcaseWindow.xml', '/addon/path', 'Default', '720p')

    result = win.start([])

    assert result is None
    assert win.modal_calls == 0


def test_start_with_none_metas_returns_none_without_domodal(load_infowindow):
    ctx = load_infowindow()
    infowindow = ctx.infowindow
    win = infowindow.ShowcaseWindow('ShowcaseWindow.xml', '/addon/path', 'Default', '720p')

    result = win.start(None)

    assert result is None
    assert win.modal_calls == 0


def test_start_with_metas_calls_domodal_and_returns_the_selected_meta(load_infowindow):
    ctx = load_infowindow()
    infowindow = ctx.infowindow
    metas = [_make_meta('tt1', 'One'), _make_meta('tt2', 'Two')]
    win = infowindow.ShowcaseWindow('ShowcaseWindow.xml', '/addon/path', 'Default', '720p')

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
    win = infowindow.ShowcaseWindow('ShowcaseWindow.xml', '/addon/path', 'Default', '720p')
    win.selected = _make_meta('stale', 'Stale leftover from a previous run')

    result = win.start([])

    assert result is None
    assert win.selected is None
    assert win.metas == []


# ---------------------------------------------------------------------------
# open_showcase() - the factory lib.ui.views.showcase() calls
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

        def start(self, passed_metas):
            captured['start_metas'] = passed_metas
            return passed_metas[0]

    monkeypatch.setattr(infowindow, 'ShowcaseWindow', RecordingWindow)

    result = infowindow.open_showcase(metas)

    assert captured['init_args'] == (
        'ShowcaseWindow.xml', 'special://home/addons/plugin.video.rivulet', 'Default', '720p'
    )
    assert captured['start_metas'] == metas
    assert result == metas[0]


def test_open_showcase_with_empty_metas_returns_none(load_infowindow):
    ctx = load_infowindow(addon_info={'path': '/addon/path'})
    assert ctx.infowindow.open_showcase([]) is None
