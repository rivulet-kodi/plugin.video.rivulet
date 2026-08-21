"""Tests for lib.ui.detailwindow: `_episode_rows()`/`_group_by_season()`
and `DetailWindow` (including its season-selector bar, id `SEASON_BAR`/
30007), Rivulet's custom replacement for the classical `meta()`/
`videos()` directories, exercised against the shared fake xbmc/xbmcgui
stubs in tests/kodistubs (no real Kodi runtime, no network).

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
WindowXML.doModal() is a no-op counter, and getControl()/setFocusId()
are plain in-memory fakes. DetailWindow.xml's actual skin rendering is
Kodi-skin-engine-only and is NOT, and cannot be, exercised by this suite.
"""
import contextlib
import types

import pytest

from lib.stremio.api import ApiError
from tests.kodistubs import install_kodi_stubs

_RELOAD_MODULE_NAMES = (
    'lib.ui.compat', 'lib.ui.router', 'lib.ui.uicommon', 'lib.ui.dialogs',
    'lib.ui.views', 'lib.ui.streamswindow', 'lib.ui.dependencies', 'lib.ui.infowindow',
    'lib.ui.detailwindow',
)


@pytest.fixture
def load_detailwindow():
    """Factory fixture: `load_detailwindow(addon_info=None)` installs fresh
    stubs (via tests.kodistubs.install_kodi_stubs) reloading lib.ui.compat/
    lib.ui.router/lib.ui.uicommon/lib.ui.views/lib.ui.streamswindow/
    lib.ui.dependencies/lib.ui.infowindow/lib.ui.detailwindow, and returns a
    namespace with `.detailwindow`, `.compat`, `.views`, `.streamswindow`,
    `.dependencies`, `.infowindow`, and `.env` - the last two for
    `_open_credits()`'s lazy `get_store`/`get_client`/
    `open_credits_picker` reach (see its own test section below). Every
    call is torn down automatically, in reverse order, at test end.
    """
    with contextlib.ExitStack() as stack:
        def _load(addon_info=None):
            return stack.enter_context(install_kodi_stubs(
                reload=_RELOAD_MODULE_NAMES,
                addon_info=addon_info,
            ))

        yield _load


def _make_window(detailwindow_mod):
    return detailwindow_mod.DetailWindow('DetailWindow.xml', '/addon/path', 'Default', '1080i')


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
    ({'id': 'v1', 'season': 1, 'episode': 3, 'title': 'The Title'}, 'S01E03 \u00b7 The Title'),
    ({'id': 'v2', 'title': 'No Season Info'}, 'S00E00 \u00b7 No Season Info'),
    ({'id': 'v3', 'season': 2, 'episode': 5, 'name': 'Fallback Name'}, 'S02E05 \u00b7 Fallback Name'),
    ({'id': 'v4', 'season': 1, 'episode': 1}, 'S01E01 \u00b7 v4'),
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

    assert rows == [('v1', 'S01E02 \u00b7 Has Id')]


@pytest.mark.parametrize('videos', [[], None], ids=['empty-list', 'none'])
def test_episode_rows_empty_or_none_input_returns_empty_list(load_detailwindow, videos):
    ctx = load_detailwindow()

    assert ctx.detailwindow._episode_rows(videos) == []


# ---------------------------------------------------------------------------
# _group_by_season() - pure per-season bucketing for the season bar (30007)
# ---------------------------------------------------------------------------


def test_group_by_season_orders_seasons_specials_last_and_labels_them(load_detailwindow):
    ctx = load_detailwindow()
    videos = [
        {'id': 'v-2x01', 'season': 2, 'episode': 1},
        {'id': 'v-1x01', 'season': 1, 'episode': 1},
        {'id': 'v-special', 'season': 0, 'episode': 1},
        {'id': 'v-1x02', 'season': 1, 'episode': 2},
    ]

    groups = ctx.detailwindow._group_by_season(videos)

    assert [label for _season, label, _videos in groups] == ['Season 1', 'Season 2', 'STR30189']
    assert [video['id'] for video in groups[0][2]] == ['v-1x01', 'v-1x02']
    assert [video['id'] for video in groups[1][2]] == ['v-2x01']
    assert [video['id'] for video in groups[2][2]] == ['v-special']


def test_group_by_season_single_season_yields_one_group(load_detailwindow):
    ctx = load_detailwindow()
    videos = [
        {'id': 'v1', 'season': 1, 'episode': 1},
        {'id': 'v2', 'season': 1, 'episode': 2},
    ]

    groups = ctx.detailwindow._group_by_season(videos)

    assert [(season, label) for season, label, _videos in groups] == [(1, 'Season 1')]


# ---------------------------------------------------------------------------
# _season_count() - left column's 'N SEASONS' segment
# ---------------------------------------------------------------------------


def test_season_count_excludes_specials(load_detailwindow):
    ctx = load_detailwindow()
    groups = ctx.detailwindow._group_by_season([
        {'id': 'v1', 'season': 1, 'episode': 1},
        {'id': 'v2', 'season': 2, 'episode': 1},
        {'id': 'v3', 'season': 0, 'episode': 1},
    ])

    assert ctx.detailwindow._season_count(groups) == 2


def test_season_count_zero_for_specials_only(load_detailwindow):
    ctx = load_detailwindow()
    groups = ctx.detailwindow._group_by_season([{'id': 'v1', 'season': 0, 'episode': 1}])

    assert ctx.detailwindow._season_count(groups) == 0


# ---------------------------------------------------------------------------
# _metadata_line()/_genres_line() - the left column's series-level metadata,
# skipping any segment (year/season-count/rating, genres) with no source
# value rather than leaving a dangling separator behind.
# ---------------------------------------------------------------------------


def test_metadata_line_joins_every_present_segment(load_detailwindow):
    ctx = load_detailwindow()
    meta = {'releaseInfo': '2020-2024', 'imdbRating': '7.6'}

    line = ctx.detailwindow._metadata_line(meta, 2)

    assert line == '2020-2024 \u00b7 2 SEASONS \u00b7 [COLOR FF38BDF8]\u2605 7.6[/COLOR]'


def test_metadata_line_closes_a_running_series_range_with_now(load_detailwindow):
    """A still-running series arrives open-ended (`2025-`). The dash
    used to be stripped, which silently lost the fact that the show has
    not ended; it is now closed with the localized "now" - the same
    range the coverflow hero and StreamsWindow print.

    The kodistubs fake returns a 'STR<id>' marker for any string id, so
    this also pins that the word is localized rather than hardcoded."""
    ctx = load_detailwindow()
    now = 'STR%d' % ctx.detailwindow._NOW_STRING_ID

    line = ctx.detailwindow._metadata_line({'releaseInfo': '2025-'}, 0)

    assert line == '2025-%s' % now


def test_metadata_line_handles_the_en_dash_cinemeta_sends(load_detailwindow):
    """Cinemeta terminates every range with an EN DASH, not the ASCII
    hyphen the old `.rstrip('-')` looked for - so that strip never fired
    on a real series at all."""
    ctx = load_detailwindow()
    now = 'STR%d' % ctx.detailwindow._NOW_STRING_ID

    assert ctx.detailwindow._metadata_line({'releaseInfo': '2022\u2013'}, 0) == '2022\u2013%s' % now
    # a closed range is untouched
    assert ctx.detailwindow._metadata_line({'releaseInfo': '2011\u20132019'}, 0) == '2011\u20132019'


def test_metadata_line_singular_season(load_detailwindow):
    ctx = load_detailwindow()

    assert ctx.detailwindow._metadata_line({}, 1) == '1 SEASON'


def test_metadata_line_falls_back_from_release_info_to_year(load_detailwindow):
    ctx = load_detailwindow()

    assert ctx.detailwindow._metadata_line({'year': '2019'}, 0) == '2019'


def test_metadata_line_missing_year_rating_and_season_is_empty_string(load_detailwindow):
    ctx = load_detailwindow()

    assert ctx.detailwindow._metadata_line({}, 0) == ''
    assert ctx.detailwindow._metadata_line(None, 0) == ''


def test_metadata_line_skips_missing_segments_without_dangling_separators(load_detailwindow):
    ctx = load_detailwindow()

    line = ctx.detailwindow._metadata_line({'imdbRating': '8.1'}, 0)

    assert line == '[COLOR FF38BDF8]\u2605 8.1[/COLOR]'


def test_genres_line_joins_and_caps_at_three(load_detailwindow):
    ctx = load_detailwindow()

    line = ctx.detailwindow._genres_line({'genres': ['Comedy', 'Mystery', 'Crime', 'Drama']})

    assert line == 'Comedy \u00b7 Mystery \u00b7 Crime'


def test_genres_line_empty_when_meta_has_no_genres(load_detailwindow):
    ctx = load_detailwindow()

    assert ctx.detailwindow._genres_line({}) == ''
    assert ctx.detailwindow._genres_line(None) == ''


# ---------------------------------------------------------------------------
# _episode_code()/_episode_title() - the split halves of _episode_label(),
# each now its own ListItem Property (see _episode_properties()).
# ---------------------------------------------------------------------------


def test_episode_code_zero_pads_season_and_episode(load_detailwindow):
    ctx = load_detailwindow()

    assert ctx.detailwindow._episode_code({'season': 1, 'episode': 3}) == 'S01E03'
    assert ctx.detailwindow._episode_code({}) == 'S00E00'


def test_episode_title_falls_back_title_then_name_then_id(load_detailwindow):
    ctx = load_detailwindow()

    assert ctx.detailwindow._episode_title({'title': 'A Title', 'name': 'A Name', 'id': 'vid'}) == 'A Title'
    assert ctx.detailwindow._episode_title({'name': 'A Name', 'id': 'vid'}) == 'A Name'
    assert ctx.detailwindow._episode_title({'id': 'vid'}) == 'vid'
    assert ctx.detailwindow._episode_title({}) == ''


def test_episode_properties_include_split_code_and_title(load_detailwindow):
    ctx = load_detailwindow()
    video = {'id': 'v1', 'season': 1, 'episode': 2, 'title': 'Second'}

    properties = ctx.detailwindow._episode_properties(video)

    assert properties['code'] == 'S01E02'
    assert properties['title'] == 'Second'


# ---------------------------------------------------------------------------
# _watched_percent() - 0-100 int from a Store.get_progress() payload, or
# None (never 0-as-"no data") when nothing usable was recorded.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize('progress,expected', [
    ({'position_ms': 21000, 'duration_ms': 50000}, 42),
    ({'position_ms': 50000, 'duration_ms': 50000}, 100),
    ({'position_ms': 60000, 'duration_ms': 50000}, 100),
    (None, None),
    ({}, None),
    ({'position_ms': 0, 'duration_ms': 50000}, None),
    ({'position_ms': 21000, 'duration_ms': 0}, None),
], ids=['42-percent', 'exactly-done', 'clamped-at-100', 'no-progress', 'empty-dict',
        'zero-position', 'zero-duration'])
def test_watched_percent(load_detailwindow, progress, expected):
    ctx = load_detailwindow()

    assert ctx.detailwindow._watched_percent(progress) == expected


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

    win.onInit()

    expected = meta[expected_key] if expected_key else ctx.compat.addon_fanart()
    assert win.getControl(ctx.detailwindow.BACKGROUND).image == expected


def test_oninit_builds_one_item_per_row_with_row_id_property_for_a_series(load_detailwindow):
    ctx = load_detailwindow()
    picker = ctx.detailwindow
    win = _make_window(picker)
    win.start({
        'id': 'tt1',
        'videos': [
            {'id': 'v1', 'season': 1, 'episode': 1, 'title': 'Pilot'},
            {'id': 'v2', 'season': 1, 'episode': 2, 'title': 'Second'},
        ],
    }, 'series')

    win.onInit()

    items = win.getControl(picker.LIST).items
    assert [item.getLabel() for item in items] == ['S01E01 \u00b7 Pilot', 'S01E02 \u00b7 Second']
    assert [item.getProperty('row_id') for item in items] == ['v1', 'v2']
    assert win.getFocusId() == picker.LIST
    # A single season is exactly the pre-season-bar flat-list case: 30007
    # stays hidden, every episode is one row - unchanged, byte-for-byte.
    assert win.getControl(picker.SEASON_BAR).visible is False


def test_oninit_builds_season_bar_and_defaults_to_the_first_non_special_season(load_detailwindow):
    ctx = load_detailwindow()
    picker = ctx.detailwindow
    win = _make_window(picker)
    win.start({
        'id': 'tt1',
        'videos': [
            {'id': 'v-2x01', 'season': 2, 'episode': 1, 'title': 'S2E1'},
            {'id': 'v-1x01', 'season': 1, 'episode': 1, 'title': 'S1E1'},
            {'id': 'v-1x02', 'season': 1, 'episode': 2, 'title': 'S1E2'},
            {'id': 'v-special', 'season': 0, 'episode': 1, 'title': 'Special'},
        ],
    }, 'series')

    win.onInit()

    bar = win.getControl(picker.SEASON_BAR)
    assert bar.visible is True
    assert [item.getLabel() for item in bar.items] == ['Season 1', 'Season 2', 'STR30189']
    assert [item.getProperty('season') for item in bar.items] == ['1', '2', '0']
    assert win.season_index == 0
    list_row_ids = [item.getProperty('row_id') for item in win.getControl(picker.LIST).items]
    assert list_row_ids == ['v-1x01', 'v-1x02']


def test_oninit_defaults_to_specials_when_that_is_the_only_season(load_detailwindow):
    ctx = load_detailwindow()
    picker = ctx.detailwindow
    win = _make_window(picker)
    win.start({
        'id': 'tt1',
        'videos': [
            {'id': 'v-special-1', 'season': 0, 'episode': 1, 'title': 'Special One'},
            {'id': 'v-special-2', 'season': 0, 'episode': 2, 'title': 'Special Two'},
        ],
    }, 'series')

    win.onInit()

    assert win.getControl(picker.SEASON_BAR).visible is False
    assert win.season_index == 0
    list_row_ids = [item.getProperty('row_id') for item in win.getControl(picker.LIST).items]
    assert list_row_ids == ['v-special-1', 'v-special-2']


def test_oninit_hides_season_bar_when_there_are_no_episodes(load_detailwindow):
    ctx = load_detailwindow()
    picker = ctx.detailwindow
    win = _make_window(picker)
    win.start({'id': 'tt1'}, 'movie')

    win.onInit()

    assert win.getControl(picker.SEASON_BAR).visible is False
    assert win.getControl(picker.LIST).items == []


def test_oninit_sets_heading_bold_and_left_column_metadata(load_detailwindow):
    ctx = load_detailwindow()
    picker = ctx.detailwindow
    win = _make_window(picker)
    win.start({
        'id': 'tt1', 'name': 'The Sheep Detectives', 'releaseInfo': '2023-2025',
        'imdbRating': '7.6', 'genres': ['Comedy', 'Mystery', 'Crime'],
        'videos': [
            {'id': 'v-1x01', 'season': 1, 'episode': 1, 'title': 'S1E1'},
            {'id': 'v-2x01', 'season': 2, 'episode': 1, 'title': 'S2E1'},
        ],
    }, 'series')

    win.onInit()

    assert win.getControl(picker.HEADING).label == '[B]THE SHEEP DETECTIVES[/B]'
    assert win.getControl(picker.METADATA_LINE).label == (
        '2023-2025 \u00b7 2 SEASONS \u00b7 [COLOR FF38BDF8]\u2605 7.6[/COLOR]'
    )
    assert win.getControl(picker.GENRES_LINE).label == 'Comedy \u00b7 Mystery \u00b7 Crime'


def test_oninit_left_column_metadata_shows_only_season_count_when_year_rating_and_genres_are_missing(
    load_detailwindow,
):
    ctx = load_detailwindow()
    picker = ctx.detailwindow
    win = _make_window(picker)
    win.start({'id': 'tt1', 'videos': [{'id': 'v1', 'season': 1, 'episode': 1, 'title': 'Ep'}]}, 'series')

    win.onInit()

    # One real season and nothing else: no dangling ' \u00b7 ' separators.
    assert win.getControl(picker.METADATA_LINE).label == '1 SEASON'
    assert win.getControl(picker.GENRES_LINE).label == ''


def test_oninit_left_column_metadata_empty_when_there_is_nothing_to_show_at_all(load_detailwindow):
    ctx = load_detailwindow()
    picker = ctx.detailwindow
    win = _make_window(picker)
    win.start({'id': 'tt1'}, 'movie')

    win.onInit()

    assert win.getControl(picker.METADATA_LINE).label == ''
    assert win.getControl(picker.GENRES_LINE).label == ''


def test_oninit_sets_season_count_for_the_default_season(load_detailwindow):
    ctx = load_detailwindow()
    picker = ctx.detailwindow
    win = _make_window(picker)
    win.start({
        'id': 'tt1',
        'videos': [
            {'id': 'v-1x01', 'season': 1, 'episode': 1, 'title': 'A'},
            {'id': 'v-1x02', 'season': 1, 'episode': 2, 'title': 'B'},
            {'id': 'v-2x01', 'season': 2, 'episode': 1, 'title': 'C'},
        ],
    }, 'series')

    win.onInit()

    assert win.getControl(picker.SEASON_COUNT).label == '2 EPISODES'


def test_season_count_updates_when_the_selected_season_changes(load_detailwindow):
    ctx = load_detailwindow()
    picker = ctx.detailwindow
    import xbmcgui
    win = _make_window(picker)
    win.start({
        'id': 'tt1',
        'videos': [
            {'id': 'v-1x01', 'season': 1, 'episode': 1, 'title': 'A'},
            {'id': 'v-2x01', 'season': 2, 'episode': 1, 'title': 'B'},
            {'id': 'v-2x02', 'season': 2, 'episode': 2, 'title': 'C'},
        ],
    }, 'series')
    win.onInit()
    assert win.getControl(picker.SEASON_COUNT).label == '1 EPISODE'
    win.setFocusId(picker.SEASON_BAR)
    win.getControl(picker.SEASON_BAR).selected_index = 1

    win.onAction(xbmcgui.Action(2))  # ACTION_MOVE_RIGHT

    assert win.getControl(picker.SEASON_COUNT).label == '2 EPISODES'


def test_season_count_empty_when_there_are_no_episodes(load_detailwindow):
    ctx = load_detailwindow()
    picker = ctx.detailwindow
    win = _make_window(picker)
    win.start({'id': 'tt1'}, 'movie')

    win.onInit()

    assert win.getControl(picker.SEASON_COUNT).label == ''


def test_episode_row_code_and_title_are_separate_properties_and_label_stays_combined(load_detailwindow):
    ctx = load_detailwindow()
    picker = ctx.detailwindow
    win = _make_window(picker)
    win.start({'id': 'tt1', 'videos': [{'id': 'v1', 'season': 1, 'episode': 3, 'title': 'The Title'}]}, 'series')

    win.onInit()

    item = win.getControl(picker.LIST).items[0]
    assert item.getProperty('code') == 'S01E03'
    assert item.getProperty('title') == 'The Title'
    assert item.getLabel() == 'S01E03 \u00b7 The Title'


# ---------------------------------------------------------------------------
# DetailWindow._build_episode_items() - watched_percent Property (the
# localized '%d%% WATCHED' text, WATCHED_STRING_ID/30212) + legacy
# resumetime/totaltime video info, from the local Store.get_progress()
# cache (mirrors lib.ui.player._maybe_resume_offset_ms()'s own shape/guard).
# A test that never wires a fake store (most of this file) exercises the
# real lazy get_store() and gets back nothing useful in this sandbox (no
# real Kodi profile directory) - _episode_progress()'s own broad
# except/None catches that, so those tests are unaffected either way.
# ---------------------------------------------------------------------------


class _FakeProgressStore:
    def __init__(self, progress_by_video_id):
        self._progress_by_video_id = progress_by_video_id
        self.calls = []

    def get_progress(self, stype, sid, video_id):
        self.calls.append((stype, sid, video_id))
        return self._progress_by_video_id.get(video_id)


def test_oninit_sets_watched_percent_and_resume_info_from_the_local_progress_cache(
    load_detailwindow, monkeypatch,
):
    ctx = load_detailwindow()
    picker = ctx.detailwindow
    store = _FakeProgressStore({'v1': {'position_ms': 21000, 'duration_ms': 50000}})
    monkeypatch.setattr(ctx.dependencies, 'get_store', lambda: store)
    win = _make_window(picker)
    win.start({'id': 'tt1', 'videos': [{'id': 'v1', 'season': 1, 'episode': 1, 'title': 'Ep'}]}, 'series')

    win.onInit()

    item = win.getControl(picker.LIST).items[0]
    label = item.getProperty('watched_percent')
    assert label == '42% WATCHED'
    assert label.count('%') == 1
    assert item.legacy_info == {'resumetime': 21.0, 'totaltime': 50.0}
    assert store.calls == [('series', 'tt1', 'v1')]


def test_oninit_watched_percent_empty_when_no_progress_recorded(load_detailwindow, monkeypatch):
    ctx = load_detailwindow()
    picker = ctx.detailwindow
    store = _FakeProgressStore({})
    monkeypatch.setattr(ctx.dependencies, 'get_store', lambda: store)
    win = _make_window(picker)
    win.start({'id': 'tt1', 'videos': [{'id': 'v1', 'season': 1, 'episode': 1, 'title': 'Ep'}]}, 'series')

    win.onInit()

    item = win.getControl(picker.LIST).items[0]
    assert item.getProperty('watched_percent') == ''
    assert item.legacy_info == {}


def test_oninit_watched_percent_empty_when_store_raises(load_detailwindow, monkeypatch):
    ctx = load_detailwindow()
    picker = ctx.detailwindow

    class _RaisingStore:
        def get_progress(self, stype, sid, video_id):
            raise OSError('corrupt cache')

    monkeypatch.setattr(ctx.dependencies, 'get_store', lambda: _RaisingStore())
    win = _make_window(picker)
    win.start({'id': 'tt1', 'videos': [{'id': 'v1', 'season': 1, 'episode': 1, 'title': 'Ep'}]}, 'series')

    win.onInit()

    item = win.getControl(picker.LIST).items[0]
    assert item.getProperty('watched_percent') == ''


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


def test_onaction_season_move_repopulates_episode_list_and_resets_selection(load_detailwindow):
    ctx = load_detailwindow()
    picker = ctx.detailwindow
    import xbmcgui
    win = _make_window(picker)
    win.start({
        'id': 'tt1',
        'videos': [
            {'id': 'v-1x01', 'season': 1, 'episode': 1, 'title': 'S1E1'},
            {'id': 'v-2x01', 'season': 2, 'episode': 1, 'title': 'S2E1'},
            {'id': 'v-2x02', 'season': 2, 'episode': 2, 'title': 'S2E2'},
        ],
    }, 'series')
    win.onInit()
    assert win.season_index == 0  # defaults to Season 1

    list_control = win.getControl(picker.LIST)
    list_control.selected_index = 1  # scrolled to episode 2 before switching away
    win.setFocusId(picker.SEASON_BAR)
    win.getControl(picker.SEASON_BAR).selected_index = 1  # Kodi already moved the bar right

    win.onAction(xbmcgui.Action(2))  # ACTION_MOVE_RIGHT

    assert win.season_index == 1
    assert [item.getProperty('row_id') for item in list_control.items] == ['v-2x01', 'v-2x02']
    assert list_control.selected_index == 0


def test_onaction_season_nav_without_focus_on_the_bar_does_not_repopulate(load_detailwindow):
    ctx = load_detailwindow()
    picker = ctx.detailwindow
    import xbmcgui
    win = _make_window(picker)
    win.start({
        'id': 'tt1',
        'videos': [
            {'id': 'v-1x01', 'season': 1, 'episode': 1, 'title': 'S1E1'},
            {'id': 'v-2x01', 'season': 2, 'episode': 1, 'title': 'S2E1'},
        ],
    }, 'series')
    win.onInit()
    win.setFocusId(picker.LIST)  # focus stayed on the episode list itself
    win.getControl(picker.SEASON_BAR).selected_index = 1

    win.onAction(xbmcgui.Action(2))  # ACTION_MOVE_RIGHT

    assert win.season_index == 0


# ---------------------------------------------------------------------------
# DetailWindow._open_context_menu() - ACTION_CONTEXT_MENU (117): one
# dialogs.choose() actions menu combining the pre-existing 'Cast & Crew'
# flow (_open_credits() - unchanged, still passes self.meta straight
# through with no re-fetch, unlike ShowcaseWindow's own version of that
# affordance which fetches on demand - see test_infowindow.py's much
# larger section for that) with this feature's library write rows
# (Add/Remove library, Mark (un)watched).
# ---------------------------------------------------------------------------


class _FakeAuthStore:
    def __init__(self, auth=None):
        self._auth = auth

    def get_auth(self):
        return self._auth


class _FakeLibraryApi:
    """Records every get_library_item()/put_library_item() call - the
    two calls `_current_library_state()`/`_push_library_item()` make
    through `lib.ui.dependencies.get_api()`."""

    def __init__(self, item=None, get_error=None, put_error=None):
        self.item = item
        self.get_error = get_error
        self.put_error = put_error
        self.get_calls = []
        self.put_calls = []

    def get_library_item(self, auth_key, item_id):
        self.get_calls.append((auth_key, item_id))
        if self.get_error is not None:
            raise self.get_error
        return self.item

    def put_library_item(self, auth_key, item):
        self.put_calls.append((auth_key, item))
        if self.put_error is not None:
            raise self.put_error


def _stub_choose(monkeypatch, ctx, answers, capture=None):
    """Patches `lib.ui.dialogs.choose` directly (already exhaustively
    covered by tests/test_dialogs.py) rather than driving a real
    `doModal()` - mirrors tests/test_catalogpicker.py's own helper of
    the same name. `answers` is either a single constant answer (every
    call returns it - the shape every pre-existing caller of this
    helper used) or a list consumed in call order, needed now that one
    onAction() can drive TWO choose() calls (the top-level actions menu,
    then _open_credits()'s own nested picker when 'Cast & Crew' is
    picked)."""
    remaining = list(answers) if isinstance(answers, (list, tuple)) else None

    def _choose(heading, rows):
        if capture is not None:
            capture.append((heading, list(rows)))
        return remaining.pop(0) if remaining is not None else answers

    monkeypatch.setattr(ctx.dialogs, 'choose', _choose)


def _wire_library_deps(monkeypatch, ctx, auth=None, item=None, get_error=None, put_error=None):
    """Common wiring for `_current_library_state()`/`_push_library_item()`:
    a fake `Store` (just `get_auth()`) via `get_store()` and a fake
    `StremioAPI` via `get_api()` - both resolved through
    `lib.ui.dependencies` exactly like `get_client()` already is in this
    file's other sections, so the real network/profile-dir code paths
    are never reached."""
    store = _FakeAuthStore(auth)
    api = _FakeLibraryApi(item=item, get_error=get_error, put_error=put_error)
    monkeypatch.setattr(ctx.dependencies, 'get_store', lambda: store)
    monkeypatch.setattr(ctx.dependencies, 'get_api', lambda: api)
    return store, api


def test_context_menu_cast_and_crew_uses_self_meta_directly_with_no_extra_fetch(load_detailwindow, monkeypatch):
    ctx = load_detailwindow()
    import xbmcgui
    win = _make_window(ctx.detailwindow)
    win.meta = {
        'id': 'tt1', 'name': 'Breaking Bad',
        'links': [{'name': 'Bryan Cranston', 'category': 'Cast', 'url': 'stremio:///search?search=Bryan%20Cranston'}],
    }
    win.stype = 'series'
    fetch_calls = []
    monkeypatch.setattr(ctx.views, '_fetch_meta', lambda stype, sid: fetch_calls.append((stype, sid)) or {})
    monkeypatch.setattr(ctx.dependencies, 'get_client', lambda: object())
    _wire_library_deps(monkeypatch, ctx, auth=None)
    captured = []
    # picked=0 (Cast & Crew) on the top-level menu, then -1 (cancel) on
    # open_credits_picker()'s own nested person list.
    _stub_choose(monkeypatch, ctx, [0, -1], capture=captured)

    win.onAction(xbmcgui.Action(ctx.detailwindow._CONTEXT_MENU_ACTION))

    assert fetch_calls == []  # self.meta was already full - no re-fetch
    assert captured[-1] == ('STR30196', [('Bryan Cranston', 'Cast')])


def test_context_menu_rows_offer_add_and_mark_watched_when_not_in_library(load_detailwindow, monkeypatch):
    ctx = load_detailwindow()
    win = _make_window(ctx.detailwindow)
    win.meta = {'id': 'tt1', 'name': 'Breaking Bad'}
    _wire_library_deps(monkeypatch, ctx, auth={'authKey': 'tok'}, item=None)
    captured = []
    _stub_choose(monkeypatch, ctx, -1, capture=captured)

    win._open_context_menu()

    assert captured == [('Breaking Bad', ['STR30196', 'STR30293', 'STR30295'])]


def test_context_menu_rows_offer_remove_and_mark_unwatched_when_already_in_library_and_watched(
    load_detailwindow, monkeypatch,
):
    ctx = load_detailwindow()
    win = _make_window(ctx.detailwindow)
    win.meta = {'id': 'tt1', 'name': 'Breaking Bad'}
    item = {'_id': 'tt1', 'removed': False, 'state': {'flaggedWatched': 1}}
    _wire_library_deps(monkeypatch, ctx, auth={'authKey': 'tok'}, item=item)
    captured = []
    _stub_choose(monkeypatch, ctx, -1, capture=captured)

    win._open_context_menu()

    assert captured == [('Breaking Bad', ['STR30196', 'STR30294', 'STR30296'])]


def test_context_menu_removed_record_still_offers_add_not_remove(load_detailwindow, monkeypatch):
    """A `removed=True` record (the implicit "recently watched, never
    added" shape `build_library_item()` produces) must still read as
    "not in library" - never offer "Remove" for something the account
    doesn't actually consider a library entry."""
    ctx = load_detailwindow()
    win = _make_window(ctx.detailwindow)
    win.meta = {'id': 'tt1', 'name': 'Breaking Bad'}
    item = {'_id': 'tt1', 'removed': True, 'temp': True, 'state': {'flaggedWatched': 0}}
    _wire_library_deps(monkeypatch, ctx, auth={'authKey': 'tok'}, item=item)
    captured = []
    _stub_choose(monkeypatch, ctx, -1, capture=captured)

    win._open_context_menu()

    assert captured == [('Breaking Bad', ['STR30196', 'STR30293', 'STR30295'])]


def test_context_menu_cancelled_menu_does_nothing(load_detailwindow, monkeypatch):
    ctx = load_detailwindow()
    win = _make_window(ctx.detailwindow)
    win.meta = {'id': 'tt1', 'name': 'Breaking Bad'}
    store, api = _wire_library_deps(monkeypatch, ctx, auth={'authKey': 'tok'}, item=None)
    _stub_choose(monkeypatch, ctx, -1)

    win._open_context_menu()

    assert api.put_calls == []
    assert ctx.env.notifications == []


def test_context_menu_add_to_library_logged_out_notifies_and_performs_no_write(load_detailwindow, monkeypatch):
    ctx = load_detailwindow()
    win = _make_window(ctx.detailwindow)
    win.meta = {'id': 'tt1', 'name': 'Breaking Bad'}
    store, api = _wire_library_deps(monkeypatch, ctx, auth=None)
    _stub_choose(monkeypatch, ctx, 1)  # row 1: "Add to library" (not in library, logged out)

    win._open_context_menu()

    assert api.get_calls == []  # logged out: no lookup either
    assert api.put_calls == []
    assert ctx.env.notifications == [('Rivulet', 'STR30190', 'info', 4000)]


def test_context_menu_mark_watched_logged_out_notifies_and_performs_no_write(load_detailwindow, monkeypatch):
    ctx = load_detailwindow()
    win = _make_window(ctx.detailwindow)
    win.meta = {'id': 'tt1', 'name': 'Breaking Bad'}
    _wire_library_deps(monkeypatch, ctx, auth=None)
    _stub_choose(monkeypatch, ctx, 2)  # row 2: "Mark as watched"

    win._open_context_menu()

    assert ctx.env.notifications == [('Rivulet', 'STR30190', 'info', 4000)]


def test_context_menu_add_to_library_pushes_explicit_payload_and_notifies_success(load_detailwindow, monkeypatch):
    ctx = load_detailwindow()
    win = _make_window(ctx.detailwindow)
    win.meta = {'id': 'tt1', 'name': 'Breaking Bad', 'type': 'series'}
    store, api = _wire_library_deps(monkeypatch, ctx, auth={'authKey': 'tok-123'}, item=None)
    _stub_choose(monkeypatch, ctx, 1)

    win._open_context_menu()

    assert len(api.put_calls) == 1
    auth_key, payload = api.put_calls[0]
    assert auth_key == 'tok-123'
    assert payload['_id'] == 'tt1'
    assert payload['removed'] is False
    assert payload['temp'] is False
    assert ctx.env.notifications == [('Rivulet', 'STR30297', 'info', 4000)]


def test_context_menu_remove_from_library_pushes_tombstone_and_notifies_success(load_detailwindow, monkeypatch):
    ctx = load_detailwindow()
    win = _make_window(ctx.detailwindow)
    win.meta = {'id': 'tt1', 'name': 'Breaking Bad'}
    item = {'_id': 'tt1', 'removed': False, 'temp': False, 'name': 'Breaking Bad', 'state': {'flaggedWatched': 0}}
    store, api = _wire_library_deps(monkeypatch, ctx, auth={'authKey': 'tok-123'}, item=item)
    _stub_choose(monkeypatch, ctx, 1)  # row 1 reads "Remove from library" for an in-library item

    win._open_context_menu()

    assert len(api.put_calls) == 1
    auth_key, payload = api.put_calls[0]
    assert auth_key == 'tok-123'
    assert payload['removed'] is True
    assert payload['state'] == item['state']  # unrelated fields preserved
    assert ctx.env.notifications == [('Rivulet', 'STR30298', 'info', 4000)]


def test_context_menu_mark_watched_with_no_prior_record_builds_fresh_item(load_detailwindow, monkeypatch):
    ctx = load_detailwindow()
    win = _make_window(ctx.detailwindow)
    win.meta = {'id': 'tt1', 'name': 'Breaking Bad', 'type': 'series'}
    store, api = _wire_library_deps(monkeypatch, ctx, auth={'authKey': 'tok-123'}, item=None)
    _stub_choose(monkeypatch, ctx, 2)  # row 2 reads "Mark as watched" with no prior record

    win._open_context_menu()

    assert len(api.put_calls) == 1
    _auth_key, payload = api.put_calls[0]
    assert payload['_id'] == 'tt1'
    assert payload['state']['flaggedWatched'] == 1
    assert payload['state']['timesWatched'] == 1
    assert ctx.env.notifications == [('Rivulet', 'STR30299', 'info', 4000)]


def test_context_menu_mark_unwatched_clears_flag_on_existing_record(load_detailwindow, monkeypatch):
    ctx = load_detailwindow()
    win = _make_window(ctx.detailwindow)
    win.meta = {'id': 'tt1', 'name': 'Breaking Bad'}
    item = {'_id': 'tt1', 'removed': False, 'state': {'flaggedWatched': 1, 'timesWatched': 3}}
    store, api = _wire_library_deps(monkeypatch, ctx, auth={'authKey': 'tok-123'}, item=item)
    _stub_choose(monkeypatch, ctx, 2)  # row 2 reads "Mark as unwatched" - item is already watched

    win._open_context_menu()

    _auth_key, payload = api.put_calls[0]
    assert payload['state']['flaggedWatched'] == 0
    assert payload['state']['timesWatched'] == 3  # history untouched by un-marking
    assert ctx.env.notifications == [('Rivulet', 'STR30300', 'info', 4000)]


def test_context_menu_failed_write_notifies_failure_not_success(load_detailwindow, monkeypatch):
    ctx = load_detailwindow()
    win = _make_window(ctx.detailwindow)
    win.meta = {'id': 'tt1', 'name': 'Breaking Bad'}
    store, api = _wire_library_deps(
        monkeypatch, ctx, auth={'authKey': 'tok-123'}, item=None, put_error=ApiError('boom'),
    )
    _stub_choose(monkeypatch, ctx, 1)

    win._open_context_menu()

    assert ctx.env.notifications == [('Rivulet', 'STR30301', 'info', 4000)]


def test_context_menu_lookup_failure_still_offers_default_wording_without_crashing(load_detailwindow, monkeypatch):
    """A failed `get_library_item()` still has to render SOME wording for
    rows 1/2 - `_open_context_menu()` falls back to the same "nothing
    exists yet" default a genuine empty record would produce (Add /
    Mark watched). The row wording is cosmetic; what actually matters
    is that neither row's write goes through on a bad guess - see the
    two tests below, which pick these same rows and assert on the
    write's absence instead of just cancelling."""
    ctx = load_detailwindow()
    win = _make_window(ctx.detailwindow)
    win.meta = {'id': 'tt1', 'name': 'Breaking Bad'}
    _wire_library_deps(monkeypatch, ctx, auth={'authKey': 'tok-123'}, get_error=ApiError('boom'))
    captured = []
    _stub_choose(monkeypatch, ctx, -1, capture=captured)

    win._open_context_menu()

    assert captured == [('Breaking Bad', ['STR30196', 'STR30293', 'STR30295'])]


def test_context_menu_add_after_lookup_failure_notifies_and_performs_no_write(load_detailwindow, monkeypatch):
    """The actual Finding-A bug: a FAILED lookup must never be treated
    as "no record" - picking "Add" here must not let `_toggle_library()`
    build a fresh payload and overwrite whatever the server actually
    has for this title."""
    ctx = load_detailwindow()
    win = _make_window(ctx.detailwindow)
    win.meta = {'id': 'tt1', 'name': 'Breaking Bad'}
    store, api = _wire_library_deps(monkeypatch, ctx, auth={'authKey': 'tok-123'}, get_error=ApiError('boom'))
    _stub_choose(monkeypatch, ctx, 1)  # row 1: "Add to library" wording, but the lookup never actually succeeded

    win._open_context_menu()

    assert api.put_calls == []
    assert ctx.env.notifications == [('Rivulet', 'STR30302', 'info', 4000)]


def test_context_menu_mark_watched_after_lookup_failure_notifies_and_performs_no_write(load_detailwindow, monkeypatch):
    """Same guard for the Mark-watched row: `_toggle_watched()`'s
    no-prior-record fallback (`build_library_item()`) is only correct
    when the account truly has no record, which a failed lookup never
    establishes."""
    ctx = load_detailwindow()
    win = _make_window(ctx.detailwindow)
    win.meta = {'id': 'tt1', 'name': 'Breaking Bad'}
    store, api = _wire_library_deps(monkeypatch, ctx, auth={'authKey': 'tok-123'}, get_error=ApiError('boom'))
    _stub_choose(monkeypatch, ctx, 2)  # row 2: "Mark as watched"

    win._open_context_menu()

    assert api.put_calls == []
    assert ctx.env.notifications == [('Rivulet', 'STR30302', 'info', 4000)]


def test_context_menu_cast_and_crew_still_works_after_lookup_failure(load_detailwindow, monkeypatch):
    """A failed library lookup must not block the non-mutating Cast &
    Crew row - it never touches `get_api()`/`get_store()` at all."""
    ctx = load_detailwindow()
    import xbmcgui
    win = _make_window(ctx.detailwindow)
    win.meta = {
        'id': 'tt1', 'name': 'Breaking Bad',
        'links': [{'name': 'Bryan Cranston', 'category': 'Cast', 'url': 'stremio:///search?search=Bryan%20Cranston'}],
    }
    win.stype = 'series'
    fetch_calls = []
    monkeypatch.setattr(ctx.views, '_fetch_meta', lambda stype, sid: fetch_calls.append((stype, sid)) or {})
    monkeypatch.setattr(ctx.dependencies, 'get_client', lambda: object())
    _wire_library_deps(monkeypatch, ctx, auth={'authKey': 'tok-123'}, get_error=ApiError('boom'))
    captured = []
    # picked=0 (Cast & Crew) on the top-level menu, then -1 (cancel) on
    # open_credits_picker()'s own nested person list.
    _stub_choose(monkeypatch, ctx, [0, -1], capture=captured)

    win.onAction(xbmcgui.Action(ctx.detailwindow._CONTEXT_MENU_ACTION))

    assert fetch_calls == []  # self.meta was already full - no re-fetch
    assert captured[-1] == ('STR30196', [('Bryan Cranston', 'Cast')])


def test_current_library_state_wraps_the_lookup_in_a_busy_dialog(load_detailwindow, monkeypatch):
    ctx = load_detailwindow()
    win = _make_window(ctx.detailwindow)
    win.meta = {'id': 'tt1'}
    _wire_library_deps(monkeypatch, ctx, auth={'authKey': 'tok'}, item=None)
    busy = _record_busy_calls(monkeypatch, ctx.dialogs)

    win._current_library_state()

    assert busy.created == [('STR30033', '')]
    assert busy.closed == 1


def test_push_library_item_wraps_the_write_in_a_busy_dialog(load_detailwindow, monkeypatch):
    ctx = load_detailwindow()
    win = _make_window(ctx.detailwindow)
    _, api = _wire_library_deps(monkeypatch, ctx, auth={'authKey': 'tok'})
    busy = _record_busy_calls(monkeypatch, ctx.dialogs)

    result = win._push_library_item({'authKey': 'tok'}, {'_id': 'tt1'})

    assert result is True
    assert busy.created == [('STR30033', '')]
    assert busy.closed == 1


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

    def fake_open_streams(stype, sid, poster=None, heading='', art=None, meta=None, video_id=None):
        captured['args'] = (stype, sid)
        return True

    monkeypatch.setattr(ctx.streamswindow, 'open_streams', fake_open_streams)

    win.onClick(picker.LIST)

    assert captured['args'] == ('series', 'tt1:1:2')
    assert win.should_close_caller is True
    assert win.closed is True


def test_onclick_passes_the_episodes_own_id_as_video_id(load_detailwindow, monkeypatch):
    ctx = load_detailwindow()
    picker = ctx.detailwindow
    meta = {'id': 'tt1'}
    win = _window_with_focused_row(picker, meta, 'series', 'tt1:1:2')
    captured = {}

    def fake_open_streams(stype, sid, poster=None, heading='', art=None, meta=None, video_id=None):
        captured['video_id'] = video_id
        return True

    monkeypatch.setattr(ctx.streamswindow, 'open_streams', fake_open_streams)

    win.onClick(picker.LIST)

    assert captured['video_id'] == 'tt1:1:2'


def test_onclick_passes_episode_heading_and_show_art_to_open_streams(load_detailwindow, monkeypatch):
    ctx = load_detailwindow()
    picker = ctx.detailwindow
    win = _make_window(picker)
    meta = {
        'id': 'tt1', 'name': 'Some Show',
        'poster': 'https://x/poster.jpg', 'background': 'https://x/fanart.jpg',
    }
    win.start(
        {**meta, 'videos': [{'id': 'v1', 'season': 1, 'episode': 2, 'title': 'The Title'}]},
        'series',
    )
    win.getControl(picker.LIST).selected_index = 0
    captured = {}

    def fake_open_streams(stype, sid, poster=None, heading='', art=None, meta=None, video_id=None):
        captured['heading'] = heading
        captured['art'] = art
        return False

    monkeypatch.setattr(ctx.streamswindow, 'open_streams', fake_open_streams)

    win.onInit()
    win.onClick(picker.LIST)

    assert captured['heading'] == 'Some Show \u2013 S01E02 The Title'
    assert captured['art'] == {'poster': 'https://x/poster.jpg', 'fanart': 'https://x/fanart.jpg'}


def test_onclick_stays_open_when_open_streams_returns_false(load_detailwindow, monkeypatch):
    ctx = load_detailwindow()
    picker = ctx.detailwindow
    win = _window_with_focused_row(picker, {'id': 'tt1'}, 'series', 'v1')
    monkeypatch.setattr(
        ctx.streamswindow, 'open_streams',
        lambda stype, sid, poster=None, heading='', art=None, meta=None, video_id=None: False,
    )

    win.onClick(picker.LIST)

    assert win.should_close_caller is False
    assert win.closed is False


def test_onclick_season_bar_switches_season_and_moves_focus_to_episode_list(load_detailwindow):
    ctx = load_detailwindow()
    picker = ctx.detailwindow
    win = _make_window(picker)
    win.start({
        'id': 'tt1',
        'videos': [
            {'id': 'v-1x01', 'season': 1, 'episode': 1, 'title': 'S1E1'},
            {'id': 'v-2x01', 'season': 2, 'episode': 1, 'title': 'S2E1'},
        ],
    }, 'series')
    win.onInit()
    win.getControl(picker.SEASON_BAR).selected_index = 1

    win.onClick(picker.SEASON_BAR)

    assert win.season_index == 1
    assert [item.getProperty('row_id') for item in win.getControl(picker.LIST).items] == ['v-2x01']
    assert win.getFocusId() == picker.LIST


def test_onclick_episode_in_non_default_season_resolves_via_video_by_id_across_seasons(
    load_detailwindow, monkeypatch,
):
    ctx = load_detailwindow()
    picker = ctx.detailwindow
    win = _make_window(picker)
    meta = {
        'id': 'tt1', 'name': 'Some Show',
        'poster': 'https://x/poster.jpg', 'background': 'https://x/fanart.jpg',
        'videos': [
            {'id': 'v-1x01', 'season': 1, 'episode': 1, 'title': 'S1 Ep'},
            {'id': 'v-2x01', 'season': 2, 'episode': 1, 'title': 'S2 Ep'},
        ],
    }
    win.start(meta, 'series')
    win.onInit()
    win.getControl(picker.SEASON_BAR).selected_index = 1
    win.onClick(picker.SEASON_BAR)  # switch to Season 2, focus moves to the episode list
    win.getControl(picker.LIST).selected_index = 0
    captured = {}

    def fake_open_streams(stype, sid, poster=None, heading='', art=None, meta=None, video_id=None):
        captured['sid'] = sid
        captured['heading'] = heading
        captured['art'] = art
        return False

    monkeypatch.setattr(ctx.streamswindow, 'open_streams', fake_open_streams)

    win.onClick(picker.LIST)

    assert captured['sid'] == 'v-2x01'
    assert captured['heading'] == 'Some Show \u2013 S02E01 S2 Ep'
    assert captured['art'] == {'poster': 'https://x/poster.jpg', 'fanart': 'https://x/fanart.jpg'}


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

    assert win.rows == [('v1', 'S01E01 \u00b7 Ep One'), ('v2', 'S01E02 \u00b7 Ep Two')]


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
    monkeypatch.setattr(ctx.streamswindow, 'open_streams', lambda stype, sid, poster=None, heading='', art=None, meta=None, video_id=None: True)

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

    def fake_open_streams(stype, sid, poster=None, heading='', art=None, meta=None, video_id=None):
        captured['args'] = (stype, sid, poster)
        captured['heading'] = heading
        captured['art'] = art
        captured['video_id'] = video_id
        return True

    monkeypatch.setattr(ctx.streamswindow, 'open_streams', fake_open_streams)

    result = ctx.detailwindow.open_detail('movie', 'tt1')

    assert result is True
    assert captured['args'] == ('movie', 'tt1', 'https://x/poster.jpg')
    assert captured['heading'] == 'A Movie'
    assert captured['art'] == {'poster': 'https://x/poster.jpg', 'fanart': 'https://x/poster.jpg'}
    assert captured['video_id'] is None  # a movie has no episode to identify


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
    assert captured['init_args'] == ('DetailWindow.xml', '/addon/path', 'Default', '1080i')
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


def _record_busy_calls(monkeypatch, dialogs_mod):
    """Monkeypatches `lib.ui.dialogs.RivuletBusy`'s create()/update()/
    close() to record calls in the same (heading, message)/(percent,
    message)/count shape the old `xbmcgui.DialogProgress` fake exposed
    as `env.dialog_created`/`dialog_updates`/`dialog_closed_count`,
    while still delegating to the real implementation so the fetch's
    dialog is genuinely created/updated/closed against the fake
    window/controls too. Mirrors test_streamswindow.py's own helper of
    the same name."""
    calls = types.SimpleNamespace(created=[], updated=[], closed=0)
    orig_create = dialogs_mod.RivuletBusy.create
    orig_update = dialogs_mod.RivuletBusy.update
    orig_close = dialogs_mod.RivuletBusy.close

    def create(self, heading, message=''):
        calls.created.append((heading, message))
        return orig_create(self, heading, message)

    def update(self, percent, message='', attempt='', stats=''):
        calls.updated.append((percent, message))
        return orig_update(self, percent, message, attempt, stats)

    def close(self):
        calls.closed += 1
        return orig_close(self)

    monkeypatch.setattr(dialogs_mod.RivuletBusy, 'create', create)
    monkeypatch.setattr(dialogs_mod.RivuletBusy, 'update', update)
    monkeypatch.setattr(dialogs_mod.RivuletBusy, 'close', close)
    return calls



def test_open_detail_movie_success_wraps_the_fetch_in_a_busy_dialog(
    load_detailwindow, monkeypatch,
):
    ctx = load_detailwindow()
    meta = {'id': 'tt1', 'name': 'A Movie', 'poster': 'https://x/poster.jpg', 'videos': []}
    monkeypatch.setattr(ctx.views, '_fetch_meta', lambda stype, sid: meta)
    monkeypatch.setattr(ctx.streamswindow, 'open_streams', lambda stype, sid, poster=None, heading='', art=None, meta=None, video_id=None: True)
    busy = _record_busy_calls(monkeypatch, ctx.dialogs)

    result = ctx.detailwindow.open_detail('movie', 'tt1')

    assert result is True
    assert busy.created == [('STR30033', '')]
    assert busy.updated == [(0, '')]
    assert busy.closed == 1


def test_open_detail_movie_closes_the_busy_dialog_before_opening_streams(
    load_detailwindow, monkeypatch,
):
    ctx = load_detailwindow()
    meta = {'id': 'tt1', 'name': 'A Movie', 'poster': 'https://x/poster.jpg', 'videos': []}
    monkeypatch.setattr(ctx.views, '_fetch_meta', lambda stype, sid: meta)
    busy = _record_busy_calls(monkeypatch, ctx.dialogs)
    captured = {}

    def fake_open_streams(stype, sid, poster=None, heading='', art=None, meta=None, video_id=None):
        captured['dialog_closed_count'] = busy.closed
        return True

    monkeypatch.setattr(ctx.streamswindow, 'open_streams', fake_open_streams)

    result = ctx.detailwindow.open_detail('movie', 'tt1')

    assert result is True
    assert captured['dialog_closed_count'] == 1  # closed BEFORE open_streams ran, not just eventually


def test_open_detail_series_closes_the_busy_dialog_before_building_the_window(
    load_detailwindow, monkeypatch,
):
    ctx = load_detailwindow(addon_info={'path': '/addon/path'})
    meta = {'id': 'tt1', 'name': 'One', 'videos': [{'id': 'v1', 'season': 1, 'episode': 1}]}
    monkeypatch.setattr(ctx.views, '_fetch_meta', lambda stype, sid: meta)
    busy = _record_busy_calls(monkeypatch, ctx.dialogs)
    captured = {}

    class RecordingWindow(ctx.detailwindow.DetailWindow):
        def __init__(self, *args, **kwargs):
            captured['dialog_closed_count'] = busy.closed
            super().__init__(*args, **kwargs)

        def start(self, meta_obj, stype):
            return True

    monkeypatch.setattr(ctx.detailwindow, 'DetailWindow', RecordingWindow)

    result = ctx.detailwindow.open_detail('series', 'tt1')

    assert result is True
    assert captured['dialog_closed_count'] == 1  # closed BEFORE the window was even constructed


def test_open_detail_not_found_still_closes_the_busy_dialog_around_the_fetch(
    load_detailwindow, monkeypatch,
):
    ctx = load_detailwindow()
    monkeypatch.setattr(ctx.views, '_fetch_meta', lambda stype, sid: None)
    busy = _record_busy_calls(monkeypatch, ctx.dialogs)

    result = ctx.detailwindow.open_detail('movie', 'tt404')

    assert result is False
    assert busy.created == [('STR30033', '')]
    assert busy.closed == 1
