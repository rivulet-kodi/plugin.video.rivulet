"""Tests for lib.ui.infowindow: the fullscreen coverflow overlay
(ShowcaseWindow) lib.ui.views.showcase() opens over one catalog page,
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
    # so no test ever reaches the network.
    'lib.ui.compat', 'lib.ui.uicommon', 'lib.ui.streamswindow', 'lib.ui.views', 'lib.ui.infowindow',
)


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


def test_onaction_info_key_is_a_noop_and_does_not_close_the_window(load_infowindow):
    ctx = load_infowindow()
    infowindow = ctx.infowindow
    import xbmcgui
    win = infowindow.ShowcaseWindow('ShowcaseWindow.xml', '/addon/path', 'Default', '720p')
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


def test_onclick_select_with_movie_opens_streams_directly_with_heading_and_art(
    load_infowindow, monkeypatch,
):
    ctx = load_infowindow()
    infowindow = ctx.infowindow
    meta = _make_meta(
        'tt1', 'A Movie', mtype='movie',
        poster='https://x/poster.jpg', background='https://x/fanart.jpg',
    )
    win = infowindow.ShowcaseWindow('ShowcaseWindow.xml', '/addon/path', 'Default', '720p')
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
    win = infowindow.ShowcaseWindow('ShowcaseWindow.xml', '/addon/path', 'Default', '720p')
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
    win = infowindow.ShowcaseWindow('ShowcaseWindow.xml', '/addon/path', 'Default', '720p')
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
    metas = [_make_meta('tt1', 'One', mtype='series'), _make_meta('tt2', 'Two', mtype='series')]
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

        def start(self, passed_metas):
            # Stands in for a crash inside onInit()/onAction() while the
            # modal loop is running - self.close() (the window's own,
            # normal-path close) never gets a chance to run. Every caller
            # (catalogpicker._open_catalog, searchwindow.open_search,
            # views.showcase/search) already wraps open_showcase() in its
            # own try/except, so the exception must keep propagating here.
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
    # scrolling past an item does not fetch it. Run the timer's callback to
    # stand in for the 200ms elapsing.
    assert win._enriched == {0, 1}
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


def test_apply_enriched_writes_queued_props_onto_the_live_listitem(load_infowindow):
    ctx = load_infowindow()
    win = ctx.infowindow.ShowcaseWindow()
    win.metas = [_sparse()]
    win._reset_enrich_state()
    win.onInit()
    win._enrich_pending = {0: {'genre': 'Drama', 'rating': '7.9', 'plot': 'Plot.', 'year': '2026'}}

    win._apply_enriched()

    item = win.getControl(ctx.infowindow.SELECT).getListItem(0)
    assert item.getProperty('plot') == 'Plot.'
    assert item.getProperty('genre') == 'Drama'
    assert win._enrich_pending == {}


def test_apply_enriched_drops_an_index_no_longer_in_the_list(load_infowindow):
    ctx = load_infowindow()
    win = ctx.infowindow.ShowcaseWindow()
    win.metas = [_sparse()]
    win._reset_enrich_state()
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
    win._enriched = {0}
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
    # Dropped from the cache so a later focus can retry it.
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
