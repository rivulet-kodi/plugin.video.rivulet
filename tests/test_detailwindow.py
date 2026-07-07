"""Tests for lib.ui.detailwindow: `_episode_rows()` and `DetailWindow`,
Rivulet's custom replacement for the classical `meta()`/`videos()`
directories, exercised against the shared fake xbmc/xbmcgui stubs in
tests/kodistubs (no real Kodi runtime, no network).

lib.ui.detailwindow imports xbmcgui and lib.ui.uicommon at module scope;
`DetailWindow.onClick()` lazily `from lib.ui.streamswindow import
open_streams` and `open_detail()` lazily `from lib.ui.views import
_fetch_meta` at call time - so load_detailwindow reloads lib.ui.compat/
lib.ui.router/lib.ui.uicommon/lib.ui.views/lib.ui.streamswindow/
lib.ui.detailwindow fresh together, the same way tests/test_catalogpicker.py
reloads lib.ui.views/lib.ui.infowindow to get handles this file
monkeypatches `_fetch_meta`/`open_streams` on directly.

DetailWindow.onInit()/onClick()/onAction()/start() are called directly
here, never through a real modal event loop, exactly like
tests/test_catalogpicker.py drives CatalogPickerWindow: the fake
WindowXMLDialog.doModal() is a no-op counter, and getControl()/setFocusId()
are plain in-memory fakes. DetailWindow.xml's actual skin rendering is
Kodi-skin-engine-only and is NOT, and cannot be, exercised by this suite.
"""
import contextlib

import pytest

from tests.kodistubs import install_kodi_stubs

_RELOAD_MODULE_NAMES = (
    'lib.ui.compat', 'lib.ui.router', 'lib.ui.uicommon',
    'lib.ui.views', 'lib.ui.streamswindow', 'lib.ui.detailwindow',
)


@pytest.fixture
def load_detailwindow():
    """Factory fixture: `load_detailwindow(addon_info=None)` installs fresh
    stubs (via tests.kodistubs.install_kodi_stubs) reloading lib.ui.compat/
    lib.ui.router/lib.ui.uicommon/lib.ui.views/lib.ui.streamswindow/
    lib.ui.detailwindow, and returns a namespace with `.detailwindow`,
    `.compat`, `.views`, `.streamswindow`, and `.env`. Every call is torn
    down automatically, in reverse order, at test end.
    """
    with contextlib.ExitStack() as stack:
        def _load(addon_info=None):
            return stack.enter_context(install_kodi_stubs(
                reload=_RELOAD_MODULE_NAMES,
                addon_info=addon_info,
            ))

        yield _load


def _make_window(detailwindow_mod):
    return detailwindow_mod.DetailWindow('DetailWindow.xml', '/addon/path', 'Default', '720p')


def _window_with_focused_row(detailwindow_mod, meta, stype, row_id):
    import xbmcgui
    win = _make_window(detailwindow_mod)
    win.meta = meta
    win.stype = stype
    item = xbmcgui.ListItem('row')
    item.setProperty('row_id', row_id)
    win.getControl(detailwindow_mod.LIST).addItems([item])
    return win


# ---------------------------------------------------------------------------
# _episode_rows() - pure flatten/sort/label logic
# ---------------------------------------------------------------------------


def test_episode_rows_orders_specials_last_despite_lowest_season_number(load_detailwindow):
    ctx = load_detailwindow()
    videos = [
        {'id': 'v-special', 'season': 0, 'episode': 1, 'title': 'A Special'},
        {'id': 'v-1x02', 'season': 1, 'episode': 2, 'title': 'Ep Two'},
        {'id': 'v-1x01', 'season': 1, 'episode': 1, 'title': 'Ep One'},
        {'id': 'v-2x01', 'season': 2, 'episode': 1, 'title': 'S2 Ep One'},
    ]

    rows = ctx.detailwindow._episode_rows(videos)

    assert [row_id for row_id, _label in rows] == ['v-1x01', 'v-1x02', 'v-2x01', 'v-special']


@pytest.mark.parametrize('video,expected_label', [
    ({'id': 'v1', 'season': 1, 'episode': 3, 'title': 'The Title'}, '1x03. The Title'),
    ({'id': 'v2', 'title': 'No Season Info'}, '0x00. No Season Info'),
    ({'id': 'v3', 'season': 2, 'episode': 5, 'name': 'Fallback Name'}, '2x05. Fallback Name'),
    ({'id': 'v4', 'season': 1, 'episode': 1}, '1x01. v4'),
], ids=['title', 'missing-season-and-episode-default-to-zero', 'title-missing-falls-back-to-name',
        'title-and-name-missing-falls-back-to-id'])
def test_episode_rows_label_format_and_title_fallback_chain(load_detailwindow, video, expected_label):
    ctx = load_detailwindow()

    rows = ctx.detailwindow._episode_rows([video])

    assert rows == [(video['id'], expected_label)]


def test_episode_rows_filters_out_videos_without_an_id(load_detailwindow):
    ctx = load_detailwindow()
    videos = [
        {'season': 1, 'episode': 1, 'title': 'No Id'},
        {'id': 'v1', 'season': 1, 'episode': 2, 'title': 'Has Id'},
    ]

    rows = ctx.detailwindow._episode_rows(videos)

    assert rows == [('v1', '1x02. Has Id')]


@pytest.mark.parametrize('videos', [[], None], ids=['empty-list', 'none'])
def test_episode_rows_empty_or_none_input_returns_empty_list(load_detailwindow, videos):
    ctx = load_detailwindow()

    assert ctx.detailwindow._episode_rows(videos) == []


# ---------------------------------------------------------------------------
# DetailWindow.onInit() - background fallback + row building
# ---------------------------------------------------------------------------


@pytest.mark.parametrize('meta,expected_key', [
    ({'background': 'https://x/bg.jpg', 'logo': 'https://x/logo.jpg', 'poster': 'https://x/poster.jpg'},
     'background'),
    ({'logo': 'https://x/logo.jpg', 'poster': 'https://x/poster.jpg'}, 'logo'),
    ({'poster': 'https://x/poster.jpg'}, 'poster'),
    ({}, None),
], ids=['background-wins-over-logo-and-poster', 'logo-wins-over-poster', 'poster-only', 'falls-back-to-addon-fanart'])
def test_oninit_background_fallback_chain(load_detailwindow, meta, expected_key):
    ctx = load_detailwindow()
    win = _make_window(ctx.detailwindow)
    win.meta = meta
    win.rows = [('v1', '1x01. Ep One')]

    win.onInit()

    expected = meta[expected_key] if expected_key else ctx.compat.addon_fanart()
    assert win.getControl(ctx.detailwindow.BACKGROUND).image == expected


def test_oninit_builds_one_item_per_row_with_row_id_property_for_a_series(load_detailwindow):
    ctx = load_detailwindow()
    picker = ctx.detailwindow
    win = _make_window(picker)
    win.meta = {}
    win.rows = [('v1', '1x01. Pilot'), ('v2', '1x02. Second')]

    win.onInit()

    items = win.getControl(picker.LIST).items
    assert [item.getLabel() for item in items] == ['1x01. Pilot', '1x02. Second']
    assert [item.getProperty('row_id') for item in items] == ['v1', 'v2']
    assert win.getFocusId() == picker.LIST


# ---------------------------------------------------------------------------
# DetailWindow.onAction()
# ---------------------------------------------------------------------------


@pytest.mark.parametrize('action_id', [9, 10, 92], ids=['nav-back', 'previous-menu', 'backspace'])
def test_onaction_back_actions_close_the_window(load_detailwindow, action_id):
    ctx = load_detailwindow()
    import xbmcgui
    win = _make_window(ctx.detailwindow)

    win.onAction(xbmcgui.Action(action_id))

    assert win.closed is True


def test_onaction_non_back_action_does_not_close(load_detailwindow):
    ctx = load_detailwindow()
    import xbmcgui
    win = _make_window(ctx.detailwindow)

    win.onAction(xbmcgui.Action(1))

    assert win.closed is False


# ---------------------------------------------------------------------------
# DetailWindow.onClick() - dispatch to lib.ui.streamswindow.open_streams()
# ---------------------------------------------------------------------------


def test_onclick_ignores_control_ids_other_than_list(load_detailwindow, monkeypatch):
    ctx = load_detailwindow()
    win = _make_window(ctx.detailwindow)
    calls = []
    monkeypatch.setattr(ctx.streamswindow, 'open_streams', lambda *a, **k: calls.append(a) or False)

    win.onClick(9999)

    assert calls == []


def test_onclick_list_with_no_focused_item_does_not_crash(load_detailwindow, monkeypatch):
    ctx = load_detailwindow()
    win = _make_window(ctx.detailwindow)
    calls = []
    monkeypatch.setattr(ctx.streamswindow, 'open_streams', lambda *a, **k: calls.append(a) or False)

    win.onClick(ctx.detailwindow.LIST)

    assert calls == []


def test_onclick_episode_row_uses_the_episodes_own_id_as_sid_not_the_titles(load_detailwindow, monkeypatch):
    ctx = load_detailwindow()
    picker = ctx.detailwindow
    meta = {'id': 'tt1'}  # the title's id must NOT be used for an episode row
    win = _window_with_focused_row(picker, meta, 'series', 'tt1:1:2')
    captured = {}

    def fake_open_streams(stype, sid, poster=None):
        captured['args'] = (stype, sid)
        return True

    monkeypatch.setattr(ctx.streamswindow, 'open_streams', fake_open_streams)

    win.onClick(picker.LIST)

    assert captured['args'] == ('series', 'tt1:1:2')
    assert win.should_close_caller is True
    assert win.closed is True


def test_onclick_stays_open_when_open_streams_returns_false(load_detailwindow, monkeypatch):
    ctx = load_detailwindow()
    picker = ctx.detailwindow
    win = _window_with_focused_row(picker, {'id': 'tt1'}, 'series', 'v1')
    monkeypatch.setattr(ctx.streamswindow, 'open_streams', lambda stype, sid, poster=None: False)

    win.onClick(picker.LIST)

    assert win.should_close_caller is False
    assert win.closed is False


# ---------------------------------------------------------------------------
# DetailWindow.start() - row derivation + the always-doModal() contract
# ---------------------------------------------------------------------------


def test_start_produces_no_rows_for_a_meta_with_no_videos(load_detailwindow):
    ctx = load_detailwindow()
    win = _make_window(ctx.detailwindow)

    result = win.start({'id': 'tt1', 'name': 'A Movie'}, 'movie')

    assert win.rows == []
    assert win.modal_calls == 1
    assert result is False


def test_start_flattens_videos_into_episode_rows_for_a_series(load_detailwindow):
    ctx = load_detailwindow()
    win = _make_window(ctx.detailwindow)
    meta = {
        'id': 'tt1',
        'videos': [
            {'id': 'v2', 'season': 1, 'episode': 2, 'title': 'Ep Two'},
            {'id': 'v1', 'season': 1, 'episode': 1, 'title': 'Ep One'},
        ],
    }

    win.start(meta, 'series')

    assert win.rows == [('v1', '1x01. Ep One'), ('v2', '1x02. Ep Two')]


def test_start_resets_should_close_caller_on_each_call(load_detailwindow):
    ctx = load_detailwindow()
    win = _make_window(ctx.detailwindow)
    win.should_close_caller = True  # leftover from a previous run

    result = win.start({}, 'movie')

    assert result is False
    assert win.should_close_caller is False


def test_start_calls_domodal_and_returns_should_close_caller(load_detailwindow, monkeypatch):
    ctx = load_detailwindow()
    picker = ctx.detailwindow
    win = _make_window(picker)
    meta = {'id': 'tt1', 'videos': [{'id': 'v1', 'season': 1, 'episode': 1, 'title': 'Ep One'}]}
    monkeypatch.setattr(ctx.streamswindow, 'open_streams', lambda stype, sid, poster=None: True)

    # The fake doModal() is a no-op counter; simulate what a real modal event
    # loop would drive around it (onInit(), the user picking the only row),
    # exactly as Kodi calls back into the window.
    real_domodal = win.doModal

    def fake_domodal():
        real_domodal()
        win.onInit()
        win.getControl(picker.LIST).selected_index = 0
        win.onClick(picker.LIST)

    win.doModal = fake_domodal

    result = win.start(meta, 'series')

    assert result is True
    assert win.modal_calls == 1


# ---------------------------------------------------------------------------
# open_detail()
# ---------------------------------------------------------------------------


def test_open_detail_not_found_notifies_and_returns_false_without_building_a_window(
    load_detailwindow, monkeypatch,
):
    ctx = load_detailwindow()
    monkeypatch.setattr(ctx.views, '_fetch_meta', lambda stype, sid: None)

    def _unexpected(*a, **k):
        raise AssertionError('DetailWindow must never be constructed when meta fetch fails')

    monkeypatch.setattr(ctx.detailwindow, 'DetailWindow', _unexpected)

    result = ctx.detailwindow.open_detail('movie', 'tt404')

    assert result is False
    assert ctx.env.notifications == [('Rivulet', 'STR30030', 'info', 4000)]


def test_open_detail_movie_skips_detailwindow_and_opens_streams_directly(
    load_detailwindow, monkeypatch,
):
    ctx = load_detailwindow()
    meta = {'id': 'tt1', 'name': 'A Movie', 'poster': 'https://x/poster.jpg', 'videos': []}
    monkeypatch.setattr(ctx.views, '_fetch_meta', lambda stype, sid: meta)
    captured = {}

    def _unexpected(*a, **k):
        raise AssertionError('DetailWindow must never be constructed for a title with no videos')

    monkeypatch.setattr(ctx.detailwindow, 'DetailWindow', _unexpected)

    def fake_open_streams(stype, sid, poster=None):
        captured['args'] = (stype, sid, poster)
        return True

    monkeypatch.setattr(ctx.streamswindow, 'open_streams', fake_open_streams)

    result = ctx.detailwindow.open_detail('movie', 'tt1')

    assert result is True
    assert captured['args'] == ('movie', 'tt1', 'https://x/poster.jpg')


def test_open_detail_series_builds_window_against_skin_path_and_starts_with_the_fetched_meta(
    load_detailwindow, monkeypatch,
):
    ctx = load_detailwindow(addon_info={'path': '/addon/path'})
    meta = {'id': 'tt1', 'name': 'One', 'videos': [{'id': 'v1', 'season': 1, 'episode': 1}]}
    monkeypatch.setattr(ctx.views, '_fetch_meta', lambda stype, sid: meta)
    captured = {}

    class RecordingWindow(ctx.detailwindow.DetailWindow):
        def __init__(self, *args, **kwargs):
            captured['init_args'] = args
            super().__init__(*args, **kwargs)

        def start(self, meta_obj, stype):
            captured['start_args'] = (meta_obj, stype)
            return True

    monkeypatch.setattr(ctx.detailwindow, 'DetailWindow', RecordingWindow)

    result = ctx.detailwindow.open_detail('series', 'tt1')

    assert result is True
    assert captured['init_args'] == ('DetailWindow.xml', '/addon/path', 'Default', '720p')
    assert captured['start_args'] == (meta, 'series')


def test_open_detail_series_window_is_closed_exactly_once_when_start_raises(
    load_detailwindow, monkeypatch,
):
    ctx = load_detailwindow(addon_info={'path': '/addon/path'})
    meta = {'id': 'tt1', 'name': 'One', 'videos': [{'id': 'v1', 'season': 1, 'episode': 1}]}
    monkeypatch.setattr(ctx.views, '_fetch_meta', lambda stype, sid: meta)
    captured = {}

    class ExplodingWindow(ctx.detailwindow.DetailWindow):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.close_calls = 0
            captured['window'] = self

        def close(self):
            self.close_calls += 1
            super().close()

        def start(self, meta_obj, stype):
            # Stands in for a crash inside onInit()/onAction() while the
            # modal loop is running - self.close() (the window's own,
            # normal-path close) never gets a chance to run.
            raise RuntimeError('onInit blew up')

    monkeypatch.setattr(ctx.detailwindow, 'DetailWindow', ExplodingWindow)

    result = ctx.detailwindow.open_detail('series', 'tt1')

    assert result is False
    win = captured['window']
    assert win.close_calls == 1
    assert win.closed is True
    assert ctx.env.notifications == [('Rivulet', 'STR30032', 'info', 4000)]


def test_open_detail_movie_success_wraps_the_fetch_in_a_busy_dialog(
    load_detailwindow, monkeypatch,
):
    ctx = load_detailwindow()
    meta = {'id': 'tt1', 'name': 'A Movie', 'poster': 'https://x/poster.jpg', 'videos': []}
    monkeypatch.setattr(ctx.views, '_fetch_meta', lambda stype, sid: meta)
    monkeypatch.setattr(ctx.streamswindow, 'open_streams', lambda stype, sid, poster=None: True)

    result = ctx.detailwindow.open_detail('movie', 'tt1')

    assert result is True
    assert ctx.env.dialog_created == [('STR30033', '')]
    assert ctx.env.dialog_updates == [(0, '')]
    assert ctx.env.dialog_closed_count == 1


def test_open_detail_movie_closes_the_busy_dialog_before_opening_streams(
    load_detailwindow, monkeypatch,
):
    ctx = load_detailwindow()
    meta = {'id': 'tt1', 'name': 'A Movie', 'poster': 'https://x/poster.jpg', 'videos': []}
    monkeypatch.setattr(ctx.views, '_fetch_meta', lambda stype, sid: meta)
    captured = {}

    def fake_open_streams(stype, sid, poster=None):
        captured['dialog_closed_count'] = ctx.env.dialog_closed_count
        return True

    monkeypatch.setattr(ctx.streamswindow, 'open_streams', fake_open_streams)

    result = ctx.detailwindow.open_detail('movie', 'tt1')

    assert result is True
    assert captured['dialog_closed_count'] == 1


def test_open_detail_series_closes_the_busy_dialog_before_building_the_window(
    load_detailwindow, monkeypatch,
):
    ctx = load_detailwindow(addon_info={'path': '/addon/path'})
    meta = {'id': 'tt1', 'name': 'One', 'videos': [{'id': 'v1', 'season': 1, 'episode': 1}]}
    monkeypatch.setattr(ctx.views, '_fetch_meta', lambda stype, sid: meta)
    captured = {}

    class RecordingWindow(ctx.detailwindow.DetailWindow):
        def __init__(self, *args, **kwargs):
            captured['dialog_closed_count'] = ctx.env.dialog_closed_count
            super().__init__(*args, **kwargs)

        def start(self, meta_obj, stype):
            return True

    monkeypatch.setattr(ctx.detailwindow, 'DetailWindow', RecordingWindow)

    result = ctx.detailwindow.open_detail('series', 'tt1')

    assert result is True
    assert captured['dialog_closed_count'] == 1


def test_open_detail_not_found_still_closes_the_busy_dialog_around_the_fetch(
    load_detailwindow, monkeypatch,
):
    ctx = load_detailwindow()
    monkeypatch.setattr(ctx.views, '_fetch_meta', lambda stype, sid: None)

    result = ctx.detailwindow.open_detail('movie', 'tt404')

    assert result is False
    assert ctx.env.dialog_created == [('STR30033', '')]
    assert ctx.env.dialog_closed_count == 1
