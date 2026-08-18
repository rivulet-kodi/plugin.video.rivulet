"""Tests for lib.ui.streamswindow: StreamsWindow and open_streams(),
Rivulet's custom replacement for the classical `streams()` directory,
exercised against the shared fake xbmc/xbmcgui stubs in tests/kodistubs (no
real Kodi runtime, no network).

Unlike lib.ui.detailwindow/lib.ui.catalogpicker, lib.ui.streamswindow
imports `get_store`/`get_client` (from lib.ui.dependencies) at MODULE
scope, plus `lib.stremio.addons.AddonError`/`addon_supports` - so,
mirroring tests/test_searchwindow.py's `_wire_data_layer` pattern for
lib.ui.searchwindow, the data layer is faked by assigning directly to
`streamswindow.get_store`/`streamswindow.get_client` rather than via
monkeypatching `lib.store`/`lib.stremio.addons` or the removed
`streamswindow.Store`/`streamswindow.AddonClient` names. `addon_supports`
and `streaminfo.sort_streams` are exercised for real (both are pure, no
xbmc dependency).

`StreamsWindow.onClick()` lazily `from lib.ui.player import play_direct` at
call time, so load_streamswindow reloads lib.ui.compat/lib.ui.uicommon/
lib.ui.player/lib.ui.streamswindow fresh together to get a handle
(`ctx.player`) this file monkeypatches `play_direct` on directly.
`play_direct()` also now takes `item_meta=`/`on_ready=` (the picker's own
heading/art/meta, and a hook it fires right before `xbmc.Player().play()`
to force-close every other live screen via
`lib.ui.uicommon.close_windows_for_playback()` - see that module's
docstring for why: every Rivulet screen is a real `WindowXMLDialog`, so
playing over a live one leaves play/pause and the OSD dead until the
user backs all the way out) - the onClick() tests below cover both,
including invoking the captured `on_ready` hook against a fake
`_MODAL_WINDOW_STACK` entry to prove the teardown actually happens and
never touches `self`.

StreamsWindow.onInit()/onClick()/onAction()/start() are called directly
here, never through a real modal event loop, exactly like
tests/test_catalogpicker.py drives CatalogPickerWindow: the fake
WindowXML.doModal() is a no-op counter, and getControl()/setFocusId()
are plain in-memory fakes. StreamsWindow.xml's actual skin rendering is
Kodi-skin-engine-only and is NOT, and cannot be, exercised by this suite.

`StreamsWindow`/`open_streams()` also take optional `heading`/`art`
context kwargs (empty/`None` by default, so every pre-existing call
site keeps working unchanged) - see this file's onInit()/start()/
open_streams() tests below for the heading-fallback, background/
poster-panel art precedence, and kwarg-forwarding coverage. The
addonerror tests near the end also cover open_streams()'s log-noise
fix: a single failing addon logs one DEBUG line (never ERROR), and at
most one aggregate WARNING summarizes the whole fetch.

`open_streams()` no longer returns True after a played pick - it reopens
a fresh StreamsWindow over the SAME already-fetched pairs once playback
ends (see `_wait_for_playback_end()`) and keeps looping, only ever
returning False. The tests near the end cover both sides of that:
`_wait_for_playback_end()` itself directly, via its `player=`/`monitor=`
injection points (tiny local `_ScriptedPlayer`/`_ScriptedMonitor` fakes,
independent of tests/kodistubs), and `open_streams()`'s end-to-end
reopen behavior through the real installed `xbmc.Player()`/
`xbmc.Monitor()` fakes, scripted via the `ctx.env.player_is_playing`
knob (tests/kodistubs/modules.py's `Player.isPlaying()` - same
plain-bool-or-1-based-callable convention as `ctx.env.cancel`/
`ctx.env.monitor_abort`).
"""
import contextlib
import threading
import types

import pytest

from lib.stremio import streaminfo
from lib.stremio.addons import AddonError
from tests.kodistubs import install_kodi_stubs

_RELOAD_MODULE_NAMES = (
    'lib.ui.compat', 'lib.ui.dependencies', 'lib.ui.uicommon', 'lib.ui.dialogs', 'lib.ui.player',
    'lib.ui.streamswindow',
)


class _FakeStore:
    """Fake `lib.store.Store`: only `get_enabled_addons()` matters to open_streams()."""

    def __init__(self, addons=None):
        self._addons = addons or []

    def get_addons(self):
        return self._addons

    def get_enabled_addons(self):
        return [a for a in self._addons if not (a.get('flags') or {}).get('disabled')]


class _FakeAddonClient:
    """Fake `lib.stremio.addons.AddonClient`. `stream_results` maps
    transport_url -> a list of Stream objects, or an Exception instance to
    raise instead (standing in for an addon-request failure). `.calls`
    records every `streams(transport, stype, sid)` invocation."""

    def __init__(self, stream_results):
        self._stream_results = stream_results
        self.calls = []

    def streams(self, transport, stype, sid):
        self.calls.append((transport, stype, sid))
        result = self._stream_results[transport]
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture
def load_streamswindow():
    """Factory fixture: `load_streamswindow(addon_info=None)` installs fresh
    stubs (via tests.kodistubs.install_kodi_stubs) reloading lib.ui.compat/
    lib.ui.uicommon/lib.ui.player/lib.ui.streamswindow, and returns a
    namespace with `.streamswindow`, `.compat`, `.player`, and `.env`. Every
    call is torn down automatically, in reverse order, at test end.
    """
    with contextlib.ExitStack() as stack:
        def _load(addon_info=None):
            return stack.enter_context(install_kodi_stubs(
                reload=_RELOAD_MODULE_NAMES,
                addon_info=addon_info,
            ))

        yield _load


def _wire_data_layer(streamswindow_mod, store, client):
    streamswindow_mod.get_store = lambda: store
    streamswindow_mod.get_client = lambda: client


def _make_window(streamswindow_mod):
    return streamswindow_mod.StreamsWindow('StreamsWindow.xml', '/addon/path', 'Default', '1080i')


# ---------------------------------------------------------------------------
# StreamsWindow.onInit() - label building + background fallback
# ---------------------------------------------------------------------------


def test_oninit_multi_provider_row_shows_gray_addon_on_line1_and_details_on_line2(load_streamswindow):
    ctx = load_streamswindow()
    win = _make_window(ctx.streamswindow)
    info = {
        'resolution': '1080p', 'source': 'WEB-DL', 'codec': 'x265', 'hdr': ['HDR10'],
        'size_text': '2.1 GB', 'seeders': 42, 'addon': 'AddonA',
        'audio': ['TrueHD', 'Atmos'], 'channels': '7.1', 'languages': ['EN', 'FR'],
        'bitrate': '25.5 Mbps', 'release': ['Hybrid', 'P8'], 'group': 'FraMeSToR', 'tracker': '1337x',
    }
    # A second pair from a different addon keeps this a multi-provider
    # case, so format_label() renders the gray addon segment on line 1
    # instead of the single-provider 'via <addon>' info-panel dedupe
    # (see below) masking what this test is actually about.
    win.pairs = [(info, {'url': 'https://a.example/a.mp4'}), ({'addon': 'AddonB'}, {})]

    win.onInit()

    item = win.getControl(ctx.streamswindow.LIST).items[0]
    assert item.getLabel() == (
        '[COLOR lime]1080p[/COLOR] [B]WEB-DL[/B] x265 HDR10 \u00b7 2.1 GB \u00b7 \u25b242 \u00b7 [COLOR gray]AddonA[/COLOR]'
    )
    assert item.label2 == (
        'TrueHD Atmos 7.1 \u00b7 EN / FR \u00b7 25.5 Mbps \u00b7 Hybrid P8 \u00b7 FraMeSToR \u00b7 1337x'
    )


def test_oninit_falls_back_to_raw_text_stripping_cr_and_lf_when_format_label_is_empty(load_streamswindow):
    ctx = load_streamswindow()
    win = _make_window(ctx.streamswindow)
    # No resolution/source/codec/hdr/size_text/seeders/addon -> format_label()
    # returns '' and onInit() must fall back to 'raw', with embedded CR/LF
    # (as a raw multi-line release description might contain) replaced by
    # spaces so the single-line list row never wraps oddly.
    info = {'raw': 'Some Raw Title\r\nLine2'}
    win.pairs = [(info, {'url': 'https://a.example/a.mp4'})]

    win.onInit()

    item = win.getControl(ctx.streamswindow.LIST).items[0]
    assert item.getLabel() == 'Some Raw Title  Line2'
    assert item.label2 == ''


def test_oninit_falls_back_to_question_mark_when_no_label_material_is_available(load_streamswindow):
    ctx = load_streamswindow()
    win = _make_window(ctx.streamswindow)
    win.pairs = [({}, {'url': 'https://a.example/a.mp4'})]

    win.onInit()

    item = win.getControl(ctx.streamswindow.LIST).items[0]
    assert item.getLabel() == '?'
    assert item.label2 == ''


def test_oninit_addon_only_info_renders_the_gray_addon_segment_on_line1_with_empty_details_on_line2(
    load_streamswindow,
):
    ctx = load_streamswindow()
    win = _make_window(ctx.streamswindow)
    # No resolution/source/codec/hdr/size_text/seeders -> format_label()'s
    # head is empty, so with two distinct addons (include_addon=True) its
    # only tail segment - the gray addon name - IS the whole line 1; line
    # 2 has nothing to derive from an addon-only info dict.
    win.pairs = [({'addon': 'AddonA'}, {'url': 'https://a.example/a.mp4'}), ({'addon': 'AddonB'}, {})]

    win.onInit()

    item = win.getControl(ctx.streamswindow.LIST).items[0]
    assert item.getLabel() == '[COLOR gray]AddonA[/COLOR]'
    assert item.label2 == ''


def test_oninit_scrubs_cr_lf_from_the_details_line(load_streamswindow, monkeypatch):
    ctx = load_streamswindow()
    win = _make_window(ctx.streamswindow)
    # format_details() itself never emits CR/LF (see its own docstring/
    # tests), but onInit() must scrub it defensively just like it already
    # does for line 1 - stub it out to prove that independently.
    monkeypatch.setattr(ctx.streamswindow.streaminfo, 'format_details', lambda info: 'TrueHD\r\nAtmos')
    win.pairs = [({'raw': 'A'}, {})]

    win.onInit()

    item = win.getControl(ctx.streamswindow.LIST).items[0]
    assert item.label2 == 'TrueHD  Atmos'


def test_oninit_sets_position_property_in_pair_order_and_focuses_the_list(load_streamswindow):
    ctx = load_streamswindow()
    win = _make_window(ctx.streamswindow)
    win.pairs = [({'raw': 'A'}, {}), ({'raw': 'B'}, {})]

    win.onInit()

    items = win.getControl(ctx.streamswindow.LIST).items
    assert [item.getProperty('position') for item in items] == ['0', '1']
    assert win.getFocusId() == ctx.streamswindow.LIST


def test_oninit_sets_discrete_stream_fields_properties_on_each_row(load_streamswindow):
    ctx = load_streamswindow()
    win = _make_window(ctx.streamswindow)
    info = {
        'resolution': '2160p', 'source': 'Remux', 'codec': 'HEVC', 'hdr': ['DV', 'HDR10'],
        'audio': ['Atmos'], 'size_text': '55.46 GB', 'seeders': 72, 'service': 'RD',
        'cached': True, 'addon': 'AIOStreams Stable',
    }
    win.pairs = [(info, {})]

    win.onInit()

    item = win.getControl(ctx.streamswindow.LIST).items[0]
    assert item.getProperty('quality') == '2160p'
    assert item.getProperty('quality_color') == 'FFFFD700'
    assert item.getProperty('release') == 'Remux'
    assert item.getProperty('flags') == 'HEVC \u00b7 DV \u00b7 HDR10 \u00b7 Atmos'
    assert item.getProperty('provider') == 'AIOStreams Stable'
    assert item.getProperty('size') == '55.46 GB'
    assert item.getProperty('seeders') == '72'
    assert item.getProperty('cache_state') == 'CACHED'
    assert item.getProperty('cache_color') == 'FF4ADE80'


def test_oninit_missing_discrete_fields_are_empty_strings_not_omitted(load_streamswindow):
    ctx = load_streamswindow()
    win = _make_window(ctx.streamswindow)
    win.pairs = [({'raw': 'A'}, {})]

    win.onInit()

    item = win.getControl(ctx.streamswindow.LIST).items[0]
    for key in ('quality', 'release', 'flags', 'provider', 'size', 'seeders'):
        assert item.getProperty(key) == ''
    # cache_state is the one exception: a stream with no cache verdict
    # renders a dim em-dash rather than a blank cell (streaminfo.stream_fields()).
    assert item.getProperty('cache_state') == '\u2014'


def test_oninit_renders_sources_addons_and_cached_summary_counts(load_streamswindow):
    ctx = load_streamswindow()
    win = _make_window(ctx.streamswindow)
    win.pairs = [
        ({'addon': 'AddonA', 'cached': True}, {}),
        ({'addon': 'AddonA', 'cached': False}, {}),
        ({'addon': 'AddonB', 'cached': True}, {}),
    ]

    win.onInit()

    assert win.getControl(ctx.streamswindow.SOURCES_COUNT).label == '3 SOURCES'
    assert win.getControl(ctx.streamswindow.ADDONS_COUNT).label == '2 ADDONS'
    assert win.getControl(ctx.streamswindow.CACHED_COUNT).label == '2 CACHED'


def test_oninit_summary_counts_are_zero_when_there_are_no_pairs(load_streamswindow):
    ctx = load_streamswindow()
    win = _make_window(ctx.streamswindow)
    win.pairs = []

    win.onInit()

    assert win.getControl(ctx.streamswindow.SOURCES_COUNT).label == '0 SOURCES'
    assert win.getControl(ctx.streamswindow.ADDONS_COUNT).label == '0 ADDONS'
    assert win.getControl(ctx.streamswindow.CACHED_COUNT).label == '0 CACHED'


@pytest.mark.parametrize('pair_count,expected', [(1, '1 SOURCE'), (2, '2 SOURCES')], ids=['n1-singular', 'n2-plural'])
def test_oninit_sources_count_label_singular_vs_plural(load_streamswindow, pair_count, expected):
    ctx = load_streamswindow()
    win = _make_window(ctx.streamswindow)
    win.pairs = [({'addon': 'AddonA', 'cached': False}, {})] * pair_count

    win.onInit()

    assert win.getControl(ctx.streamswindow.SOURCES_COUNT).label == expected


@pytest.mark.parametrize(
    'addon_names,expected',
    [(['AddonA'], '1 ADDON'), (['AddonA', 'AddonB'], '2 ADDONS')],
    ids=['n1-singular', 'n2-plural'],
)
def test_oninit_addons_count_label_singular_vs_plural(load_streamswindow, addon_names, expected):
    ctx = load_streamswindow()
    win = _make_window(ctx.streamswindow)
    win.pairs = [({'addon': name, 'cached': False}, {}) for name in addon_names]

    win.onInit()

    assert win.getControl(ctx.streamswindow.ADDONS_COUNT).label == expected


@pytest.mark.parametrize('poster,expect_fanart', [
    ('https://x/poster.jpg', False),
    (None, True),
], ids=['poster-set', 'no-poster-falls-back-to-addon-fanart'])
def test_oninit_background_uses_poster_or_falls_back_to_addon_fanart(load_streamswindow, poster, expect_fanart):
    ctx = load_streamswindow()
    win = _make_window(ctx.streamswindow)
    win.poster = poster
    win.pairs = []

    win.onInit()

    expected = ctx.compat.addon_fanart() if expect_fanart else poster
    assert win.getControl(ctx.streamswindow.BACKGROUND).image == expected


def test_oninit_heading_defaults_to_generic_streams_title_uppercased_when_omitted(load_streamswindow):
    ctx = load_streamswindow()
    win = _make_window(ctx.streamswindow)
    win.pairs = []

    win.onInit()

    # L(30041) isn't configured in the fake localized-string map, so it
    # resolves to the deterministic 'STR30041' marker (see FakeAddon) -
    # already all-uppercase, so .upper() is a no-op here, but this still
    # exercises the exact code path a real 'Streams' string would.
    assert win.getControl(ctx.streamswindow.HEADING).label == 'STR30041'


def test_oninit_heading_uses_the_supplied_title_uppercased(load_streamswindow):
    ctx = load_streamswindow()
    win = _make_window(ctx.streamswindow)
    win.pairs = []
    win.heading = 'Breaking Bad \u2013 S01E01 Pilot'

    win.onInit()

    assert win.getControl(ctx.streamswindow.HEADING).label == 'BREAKING BAD \u2013 S01E01 PILOT'


def test_oninit_art_fanart_drives_background_and_art_poster_drives_the_side_panel(load_streamswindow):
    ctx = load_streamswindow()
    win = _make_window(ctx.streamswindow)
    win.pairs = []
    win.poster = 'https://x/legacy-poster.jpg'
    win.art = {'poster': 'https://x/art-poster.jpg', 'fanart': 'https://x/art-fanart.jpg'}

    win.onInit()

    assert win.getControl(ctx.streamswindow.BACKGROUND).image == 'https://x/art-fanart.jpg'
    assert win.getControl(ctx.streamswindow.POSTER).image == 'https://x/art-poster.jpg'


def test_oninit_art_poster_drives_background_when_no_fanart_is_supplied(load_streamswindow):
    ctx = load_streamswindow()
    win = _make_window(ctx.streamswindow)
    win.pairs = []
    win.art = {'poster': 'https://x/art-poster.jpg'}

    win.onInit()

    assert win.getControl(ctx.streamswindow.BACKGROUND).image == 'https://x/art-poster.jpg'
    assert win.getControl(ctx.streamswindow.POSTER).image == 'https://x/art-poster.jpg'


def test_oninit_poster_panel_is_cleared_when_neither_art_nor_legacy_poster_is_supplied(load_streamswindow):
    ctx = load_streamswindow()
    win = _make_window(ctx.streamswindow)
    win.pairs = []

    win.onInit()

    assert win.getControl(ctx.streamswindow.POSTER).image == ''


# ---------------------------------------------------------------------------
# StreamsWindow.onInit() - info panel (INFO_PANEL/30008): year/runtime/
# rating/genres built from `self.meta`, plus the single-provider dedupe
# that drops the addon segment from every row's line 1 (format_label's
# include_addon=False) and appends a trailing 'via <addon>' line once
# every pair came from the same addon. label2 is always
# streaminfo.format_details(info), independent of that dedupe.
# ---------------------------------------------------------------------------


def test_oninit_meta_renders_year_runtime_rating_and_genres_into_the_info_panel(load_streamswindow):
    ctx = load_streamswindow()
    win = _make_window(ctx.streamswindow)
    win.meta = {
        'releaseInfo': '2015-2019', 'runtime': '48 min', 'imdbRating': '8.7',
        'genres': ['Drama', 'Crime', 'Thriller', 'Extra'],
    }
    # Two distinct addons -> no single-provider dedupe, isolating this to
    # the meta-driven lines alone.
    win.pairs = [({'addon': 'AddonA'}, {}), ({'addon': 'AddonB'}, {})]

    win.onInit()

    assert win.getControl(ctx.streamswindow.INFO_PANEL).text == (
        '2015-2019 \u00b7 48 min\n\u2605 8.7\nDrama / Crime / Thriller'
    )


def test_oninit_closes_a_running_series_range_with_now(load_streamswindow):
    """A still-running series' open-ended range gains the localized
    "now" rather than losing its dash silently - the same range
    DetailWindow and the coverflow hero print. Cinemeta sends the EN
    DASH the old `.rstrip('-')` never matched.

    The kodistubs fake returns a 'STR<id>' marker for any string id, so
    this also pins that the word is localized rather than hardcoded."""
    ctx = load_streamswindow()
    win = _make_window(ctx.streamswindow)
    win.meta = {'releaseInfo': '2022\u2013', 'runtime': '60 min'}
    win.pairs = [({'addon': 'AddonA'}, {}), ({'addon': 'AddonB'}, {})]

    win.onInit()

    now = 'STR%d' % ctx.streamswindow._NOW_STRING_ID
    assert win.getControl(ctx.streamswindow.INFO_PANEL).text == (
        '2022\u2013%s \u00b7 60 min' % now
    )


def test_oninit_single_provider_drops_addon_from_line1_and_appends_via_line(load_streamswindow):
    ctx = load_streamswindow()
    win = _make_window(ctx.streamswindow)
    win.meta = None
    win.pairs = [
        ({'addon': 'AddonA', 'raw': 'A'}, {}),
        ({'addon': 'AddonA', 'raw': 'B'}, {}),
    ]

    win.onInit()

    items = win.getControl(ctx.streamswindow.LIST).items
    # format_label(..., include_addon=False) has nothing else to render
    # here, so line 1 falls back to 'raw' with no addon segment at all -
    # the single-provider dedupe now lives in include_addon, not label2.
    assert [item.getLabel() for item in items] == ['A', 'B']
    assert [item.label2 for item in items] == ['', '']
    assert win.getControl(ctx.streamswindow.INFO_PANEL).text == 'via AddonA'


def test_oninit_single_provider_still_shows_line2_details_when_known(load_streamswindow):
    ctx = load_streamswindow()
    win = _make_window(ctx.streamswindow)
    win.meta = None
    win.pairs = [({'addon': 'AddonA', 'raw': 'A', 'audio': ['DTS'], 'channels': '5.1'}, {})]

    win.onInit()

    item = win.getControl(ctx.streamswindow.LIST).items[0]
    # The single-provider dedupe only ever touches the addon segment - it
    # never blanks label2, which is always the re-derived details line.
    assert item.label2 == 'DTS 5.1'
    assert win.getControl(ctx.streamswindow.INFO_PANEL).text == 'via AddonA'


def test_oninit_multiple_providers_show_details_on_line2_and_skip_the_via_line(load_streamswindow):
    ctx = load_streamswindow()
    win = _make_window(ctx.streamswindow)
    win.meta = {'runtime': '90 min'}
    win.pairs = [
        ({'addon': 'AddonA', 'raw': 'A', 'audio': ['DTS'], 'channels': '5.1'}, {}),
        ({'addon': 'AddonB', 'raw': 'B', 'languages': ['EN']}, {}),
    ]

    win.onInit()

    items = win.getControl(ctx.streamswindow.LIST).items
    assert [item.label2 for item in items] == ['DTS 5.1', 'EN']
    assert win.getControl(ctx.streamswindow.INFO_PANEL).text == '90 min'


def test_oninit_no_meta_and_multiple_providers_leaves_the_info_panel_empty(load_streamswindow):
    ctx = load_streamswindow()
    win = _make_window(ctx.streamswindow)
    win.meta = None
    win.pairs = [
        ({'addon': 'AddonA', 'raw': 'A'}, {}),
        ({'addon': 'AddonB', 'raw': 'B'}, {}),
    ]

    win.onInit()

    assert win.getControl(ctx.streamswindow.INFO_PANEL).text == ''
    items = win.getControl(ctx.streamswindow.LIST).items
    assert [item.label2 for item in items] == ['', '']

# ---------------------------------------------------------------------------
# StreamsWindow.onAction()
# ---------------------------------------------------------------------------


@pytest.mark.parametrize('action_id', [9, 10, 92], ids=['nav-back', 'previous-menu', 'backspace'])
def test_onaction_back_actions_close_the_window(load_streamswindow, action_id):
    ctx = load_streamswindow()
    import xbmcgui
    win = _make_window(ctx.streamswindow)

    win.onAction(xbmcgui.Action(action_id))

    assert win.closed is True


def test_onaction_non_back_action_does_not_close(load_streamswindow):
    ctx = load_streamswindow()
    import xbmcgui
    win = _make_window(ctx.streamswindow)

    win.onAction(xbmcgui.Action(1))

    assert win.closed is False


# ---------------------------------------------------------------------------
# StreamsWindow.onClick() - resolves the focused pair, dispatches to play_direct
# ---------------------------------------------------------------------------


def test_onclick_ignores_control_ids_other_than_list(load_streamswindow, monkeypatch):
    ctx = load_streamswindow()
    win = _make_window(ctx.streamswindow)
    calls = []
    monkeypatch.setattr(ctx.player, 'play_direct', lambda *a: calls.append(a) or False)

    win.onClick(9999)

    assert calls == []


def test_onclick_list_with_no_focused_item_does_not_crash(load_streamswindow, monkeypatch):
    ctx = load_streamswindow()
    win = _make_window(ctx.streamswindow)
    calls = []
    monkeypatch.setattr(ctx.player, 'play_direct', lambda *a: calls.append(a) or False)

    win.onClick(ctx.streamswindow.LIST)

    assert calls == []


def test_onclick_dispatches_the_focused_pairs_own_stream_to_play_direct(load_streamswindow, monkeypatch):
    ctx = load_streamswindow()
    win = _make_window(ctx.streamswindow)
    stream_a = {'url': 'https://a.example/a.mp4'}
    stream_b = {'url': 'https://b.example/b.mp4'}
    win.pairs = [({'raw': 'A'}, stream_a), ({'raw': 'B'}, stream_b)]
    win.stype = 'movie'
    win.sid = 'tt1'
    win.onInit()
    win.getControl(ctx.streamswindow.LIST).selected_index = 1  # simulate scrolling to the 2nd row
    captured = {}

    def fake_play_direct(stream, stype, sid, item_meta=None, on_ready=None, video_id=None):
        captured['args'] = (stream, stype, sid)
        return True

    monkeypatch.setattr(ctx.player, 'play_direct', fake_play_direct)

    win.onClick(ctx.streamswindow.LIST)

    assert captured['args'] == (stream_b, 'movie', 'tt1')
    assert win.played is True
    assert win.closed is True


def test_onclick_forwards_the_windows_video_id_to_play_direct(load_streamswindow, monkeypatch):
    ctx = load_streamswindow()
    win = _make_window(ctx.streamswindow)
    win.pairs = [({'raw': 'A'}, {'url': 'https://a.example/a.mp4'})]
    win.stype = 'series'
    win.sid = 'tt1'
    win.video_id = 'tt1:1:2'
    win.onInit()
    captured = {}

    def fake_play_direct(stream, stype, sid, item_meta=None, on_ready=None, video_id=None):
        captured['video_id'] = video_id
        return True

    monkeypatch.setattr(ctx.player, 'play_direct', fake_play_direct)

    win.onClick(ctx.streamswindow.LIST)

    assert captured['video_id'] == 'tt1:1:2'


def test_onclick_forwards_none_video_id_for_a_movie_or_context_free_window(load_streamswindow, monkeypatch):
    ctx = load_streamswindow()
    win = _make_window(ctx.streamswindow)
    win.pairs = [({'raw': 'A'}, {'url': 'https://a.example/a.mp4'})]
    win.onInit()
    captured = {}

    def fake_play_direct(stream, stype, sid, item_meta=None, on_ready=None, video_id=None):
        captured['video_id'] = video_id
        return True

    monkeypatch.setattr(ctx.player, 'play_direct', fake_play_direct)

    win.onClick(ctx.streamswindow.LIST)

    assert captured['video_id'] is None


def test_onclick_records_played_pair_when_play_direct_succeeds(load_streamswindow, monkeypatch):
    ctx = load_streamswindow()
    win = _make_window(ctx.streamswindow)
    info_a, stream_a = {'raw': 'A'}, {'url': 'https://a.example/a.mp4'}
    info_b, stream_b = {'raw': 'B'}, {'url': 'https://b.example/b.mp4'}
    win.pairs = [(info_a, stream_a), (info_b, stream_b)]
    win.onInit()
    win.getControl(ctx.streamswindow.LIST).selected_index = 1
    monkeypatch.setattr(
        ctx.player, 'play_direct',
        lambda stream, stype, sid, item_meta=None, on_ready=None, video_id=None: True,
    )

    win.onClick(ctx.streamswindow.LIST)

    assert win.played_pair == (info_b, stream_b)


def test_onclick_leaves_played_pair_none_when_play_direct_returns_false(load_streamswindow, monkeypatch):
    ctx = load_streamswindow()
    win = _make_window(ctx.streamswindow)
    win.pairs = [({'raw': 'A'}, {'url': 'https://a.example/a.mp4'})]
    win.onInit()
    monkeypatch.setattr(
        ctx.player, 'play_direct',
        lambda stream, stype, sid, item_meta=None, on_ready=None, video_id=None: False,
    )

    win.onClick(ctx.streamswindow.LIST)

    assert win.played_pair is None


def test_onclick_stays_open_when_play_direct_returns_false(load_streamswindow, monkeypatch):
    ctx = load_streamswindow()
    win = _make_window(ctx.streamswindow)
    win.pairs = [({'raw': 'A'}, {'url': 'https://a.example/a.mp4'})]
    win.onInit()
    monkeypatch.setattr(
        ctx.player, 'play_direct',
        lambda stream, stype, sid, item_meta=None, on_ready=None, video_id=None: False,
    )

    win.onClick(ctx.streamswindow.LIST)

    assert win.played is False
    assert win.closed is False


class _FakeStackWindow:
    """Minimal stand-in for another live `ModalStackWindow`-mixed screen
    sitting on `lib.ui.uicommon._MODAL_WINDOW_STACK` underneath this
    StreamsWindow - only `.close()`/`._closed_for_playback` matter to
    `close_windows_for_playback()`, exactly like tests/test_uicommon.py's
    own `_StackWindow`. `name`/`order`, if given, additionally record
    each `close()` call into the shared `order` list so a test can
    assert relative closing order (e.g. against a fake Player.play
    sink)."""

    def __init__(self, name=None, order=None):
        self._closed_for_playback = False
        self.closed = False
        self.name = name
        self.order = order

    def close(self):
        self.closed = True
        if self.order is not None:
            self.order.append(self.name)


def test_onclick_passes_item_meta_with_heading_art_and_meta_to_play_direct(load_streamswindow, monkeypatch):
    ctx = load_streamswindow()
    win = _make_window(ctx.streamswindow)
    win.pairs = [({'raw': 'A'}, {'url': 'https://a.example/a.mp4'})]
    win.heading = 'Dune'
    win.art = {'poster': 'https://x/p.jpg', 'fanart': 'https://x/f.jpg'}
    win.meta = {'name': 'Dune', 'runtime': '155 min'}
    win.onInit()
    captured = {}

    def fake_play_direct(stream, stype, sid, item_meta=None, on_ready=None, video_id=None):
        captured['item_meta'] = item_meta
        captured['on_ready'] = on_ready
        return True

    monkeypatch.setattr(ctx.player, 'play_direct', fake_play_direct)

    win.onClick(ctx.streamswindow.LIST)

    assert captured['item_meta'] == {
        'label': 'Dune',
        'art': {'poster': 'https://x/p.jpg', 'fanart': 'https://x/f.jpg'},
        'meta': {'name': 'Dune', 'runtime': '155 min'},
    }
    assert callable(captured['on_ready'])


def test_onclick_item_meta_falls_back_to_meta_name_and_bare_poster_when_no_heading_or_art(
    load_streamswindow, monkeypatch,
):
    ctx = load_streamswindow()
    win = _make_window(ctx.streamswindow)
    win.pairs = [({'raw': 'A'}, {'url': 'https://a.example/a.mp4'})]
    win.poster = 'https://x/poster.jpg'
    win.meta = {'name': 'Some Movie'}
    win.onInit()
    captured = {}

    def fake_play_direct(stream, stype, sid, item_meta=None, on_ready=None, video_id=None):
        captured['item_meta'] = item_meta
        return True

    monkeypatch.setattr(ctx.player, 'play_direct', fake_play_direct)

    win.onClick(ctx.streamswindow.LIST)

    assert captured['item_meta'] == {
        'label': 'Some Movie',
        'art': {'poster': 'https://x/poster.jpg'},
        'meta': {'name': 'Some Movie'},
    }


def test_onclick_item_meta_is_empty_when_nothing_is_known(load_streamswindow, monkeypatch):
    ctx = load_streamswindow()
    win = _make_window(ctx.streamswindow)
    win.pairs = [({'raw': 'A'}, {'url': 'https://a.example/a.mp4'})]
    win.onInit()
    captured = {}

    def fake_play_direct(stream, stype, sid, item_meta=None, on_ready=None, video_id=None):
        captured['item_meta'] = item_meta
        return True

    monkeypatch.setattr(ctx.player, 'play_direct', fake_play_direct)

    win.onClick(ctx.streamswindow.LIST)

    assert captured['item_meta'] == {}


def test_onclick_on_ready_hook_tears_down_every_other_live_window_and_closes_self_without_the_reopen_flag(
    load_streamswindow, monkeypatch,
):
    ctx = load_streamswindow()
    win = _make_window(ctx.streamswindow)
    win.pairs = [({'raw': 'A'}, {'url': 'https://a.example/a.mp4'})]
    win.onInit()
    other = _FakeStackWindow()
    ctx.uicommon._MODAL_WINDOW_STACK.append(other)
    captured = {}

    def fake_play_direct(stream, stype, sid, item_meta=None, on_ready=None, video_id=None):
        captured['on_ready'] = on_ready
        return True

    monkeypatch.setattr(ctx.player, 'play_direct', fake_play_direct)

    win.onClick(ctx.streamswindow.LIST)
    captured['on_ready']()

    assert other.closed is True
    assert other._closed_for_playback is True
    assert win.closed is True  # the hook now closes the picker itself too
    assert win._closed_for_playback is False  # but never marks it for the reopen loop


def test_onclick_closes_every_rivulet_modal_including_the_picker_before_player_play_runs(
    load_streamswindow, monkeypatch,
):
    """Order-sensitive regression test for GH-2: at the exact instant
    play_direct() is about to hand off to xbmc.Player().play(), every
    live Rivulet modal - ancestors AND the picker itself - must already
    be closed. Ancestors are marked `_closed_for_playback` for
    open_streams()'s own restoration; the picker is not, so
    ModalStackWindow.doModal() never reopens it immediately - only
    open_streams()'s post-playback reopen loop does that."""
    ctx = load_streamswindow()
    win = _make_window(ctx.streamswindow)
    info, stream = {'raw': 'A'}, {'url': 'https://a.example/a.mp4'}
    win.pairs = [(info, stream)]
    win.onInit()

    order = []
    outer = _FakeStackWindow(name='outer', order=order)
    inner = _FakeStackWindow(name='inner', order=order)
    ctx.uicommon._MODAL_WINDOW_STACK.extend([outer, inner, win])
    original_close = win.close

    def tracking_close():
        if 'picker' not in order:
            order.append('picker')
        original_close()

    win.close = tracking_close

    def fake_play_direct(stream_, stype, sid, item_meta=None, on_ready=None, video_id=None):
        on_ready()
        order.append('player.play')
        return True

    monkeypatch.setattr(ctx.player, 'play_direct', fake_play_direct)

    win.onClick(ctx.streamswindow.LIST)

    assert order == ['inner', 'outer', 'picker', 'player.play']  # every modal gone before play()
    assert outer._closed_for_playback is True
    assert inner._closed_for_playback is True
    assert win._closed_for_playback is False  # picker excluded from the reopen-flag marking
    assert win.closed is True
    assert win.played is True
    assert win.played_pair == (info, stream)


# ---------------------------------------------------------------------------
# StreamsWindow.start() - the doModal()/empty-pairs contract
# ---------------------------------------------------------------------------


def test_start_with_empty_pairs_returns_false_without_domodal(load_streamswindow):
    ctx = load_streamswindow()
    win = _make_window(ctx.streamswindow)

    result = win.start([], 'movie', 'tt1')

    assert result is False
    assert win.modal_calls == 0


def test_start_resets_played_state_on_each_call(load_streamswindow):
    ctx = load_streamswindow()
    win = _make_window(ctx.streamswindow)
    win.played = True  # leftover from a previous run

    result = win.start([], 'movie', 'tt1')

    assert result is False
    assert win.played is False


def test_start_with_pairs_calls_domodal_and_returns_played(load_streamswindow, monkeypatch):
    ctx = load_streamswindow()
    win = _make_window(ctx.streamswindow)
    pairs = [({'raw': 'A'}, {'url': 'https://a.example/a.mp4'})]
    monkeypatch.setattr(
        ctx.player, 'play_direct',
        lambda stream, stype, sid, item_meta=None, on_ready=None, video_id=None: True,
    )

    # The fake doModal() is a no-op counter; simulate what a real modal event
    # loop would drive around it (onInit(), the user picking the only row).
    real_domodal = win.doModal

    def fake_domodal():
        real_domodal()
        win.onInit()
        win.getControl(ctx.streamswindow.LIST).selected_index = 0
        win.onClick(ctx.streamswindow.LIST)

    win.doModal = fake_domodal

    result = win.start(pairs, 'movie', 'tt1', poster='https://x/poster.jpg')

    assert result is True
    assert win.modal_calls == 1


def test_start_forwards_heading_art_and_meta_onto_the_window(load_streamswindow):
    ctx = load_streamswindow()
    win = _make_window(ctx.streamswindow)
    pairs = [({'raw': 'A'}, {'url': 'https://a.example/a.mp4'})]
    meta = {'name': 'A Movie', 'runtime': '90 min'}

    win.start(pairs, 'movie', 'tt1', heading='My Title', art={'poster': 'P', 'fanart': 'F'}, meta=meta)

    assert win.heading == 'My Title'
    assert win.art == {'poster': 'P', 'fanart': 'F'}
    assert win.meta == meta


def test_start_defaults_heading_art_and_meta_when_omitted(load_streamswindow):
    ctx = load_streamswindow()
    win = _make_window(ctx.streamswindow)
    pairs = [({'raw': 'A'}, {'url': 'https://a.example/a.mp4'})]

    win.start(pairs, 'movie', 'tt1')

    assert win.heading == ''
    assert win.art is None
    assert win.meta is None


def test_start_forwards_video_id_onto_the_window(load_streamswindow):
    ctx = load_streamswindow()
    win = _make_window(ctx.streamswindow)
    pairs = [({'raw': 'A'}, {'url': 'https://a.example/a.mp4'})]

    win.start(pairs, 'series', 'tt1:1:2', video_id='tt1:1:2')

    assert win.video_id == 'tt1:1:2'


def test_start_defaults_video_id_to_none_when_omitted(load_streamswindow):
    ctx = load_streamswindow()
    win = _make_window(ctx.streamswindow)
    pairs = [({'raw': 'A'}, {'url': 'https://a.example/a.mp4'})]

    win.start(pairs, 'movie', 'tt1')

    assert win.video_id is None


def test_start_resets_played_pair_on_each_call(load_streamswindow):
    ctx = load_streamswindow()
    win = _make_window(ctx.streamswindow)
    win.played_pair = ({'raw': 'stale'}, {'url': 'stale'})  # leftover from a previous run

    win.start([], 'movie', 'tt1')

    assert win.played_pair is None


# ---------------------------------------------------------------------------
# open_streams()
# ---------------------------------------------------------------------------


def test_open_streams_filters_unsupported_addons_and_forwards_aggregate_to_the_window(
    load_streamswindow, monkeypatch,
):
    ctx = load_streamswindow()
    sw = ctx.streamswindow
    supported = {
        'transportUrl': 't-supported',
        'manifest': {'name': 'Supported', 'resources': ['stream'], 'types': ['movie']},
    }
    unsupported = {
        'transportUrl': 't-unsupported',
        # declares no 'stream' resource at all -> addon_supports() excludes
        # it before any HTTP request is made.
        'manifest': {'name': 'Unsupported', 'resources': ['catalog'], 'types': ['movie']},
    }
    stream = {'url': 'https://a.example/a.mp4'}
    client = _FakeAddonClient({'t-supported': [stream]})
    _wire_data_layer(sw, _FakeStore(addons=[supported, unsupported]), client)
    captured = {}

    class RecordingWindow(sw.StreamsWindow):
        def start(self, pairs, stype, sid, poster=None, heading='', art=None, meta=None, video_id=None):
            captured['args'] = (pairs, stype, sid, poster)
            return True

    monkeypatch.setattr(sw, 'StreamsWindow', RecordingWindow)
    # open_streams() now round-trips after a played start() - stub the wait
    # helper to "no reopen" (as if the user backed out immediately) so this
    # stays a single-iteration test of aggregate forwarding.
    monkeypatch.setattr(sw, '_wait_for_playback_end', lambda *a, **k: (False, False))

    result = sw.open_streams('movie', 'tt1', poster='https://x/poster.jpg')

    assert [call[0] for call in client.calls] == ['t-supported']
    pairs, stype, sid, poster = captured['args']
    assert (stype, sid, poster) == ('movie', 'tt1', 'https://x/poster.jpg')
    assert [s for _info, s in pairs] == [stream]
    assert result is False


def test_supported_stream_addons_skips_disabled_addon(load_streamswindow):
    """A disabled addon stays installed but must never be dispatched for
    streams - `_supported_stream_addons()` fans out over
    `get_enabled_addons()`, not every installed descriptor."""
    ctx = load_streamswindow()
    sw = ctx.streamswindow
    enabled = {
        'transportUrl': 't-enabled',
        'manifest': {'name': 'Enabled', 'resources': ['stream'], 'types': ['movie']},
    }
    disabled = {
        'transportUrl': 't-disabled',
        'manifest': {'name': 'Disabled', 'resources': ['stream'], 'types': ['movie']},
        'flags': {'disabled': True},
    }
    _wire_data_layer(sw, _FakeStore(addons=[enabled, disabled]), _FakeAddonClient({}))

    addons = sw._supported_stream_addons('movie', 'tt1')

    assert [descriptor['transportUrl'] for descriptor, _manifest in addons] == ['t-enabled']



def test_open_streams_forwards_heading_art_and_meta_to_the_window(load_streamswindow, monkeypatch):
    ctx = load_streamswindow()
    sw = ctx.streamswindow
    supported = {
        'transportUrl': 't-supported',
        'manifest': {'name': 'Supported', 'resources': ['stream'], 'types': ['movie']},
    }
    stream = {'url': 'https://a.example/a.mp4'}
    client = _FakeAddonClient({'t-supported': [stream]})
    _wire_data_layer(sw, _FakeStore(addons=[supported]), client)
    captured = {}

    class RecordingWindow(sw.StreamsWindow):
        def start(self, pairs, stype, sid, poster=None, heading='', art=None, meta=None, video_id=None):
            captured['heading'] = heading
            captured['art'] = art
            captured['meta'] = meta
            return True

    monkeypatch.setattr(sw, 'StreamsWindow', RecordingWindow)
    # See test_open_streams_filters_unsupported_addons_and_forwards_aggregate_to_the_window
    # - stub the round-trip wait so a played start() ends the call here.
    monkeypatch.setattr(sw, '_wait_for_playback_end', lambda *a, **k: (False, False))

    result = sw.open_streams(
        'movie', 'tt1', heading='Some Movie',
        art={'poster': 'https://x/p.jpg', 'fanart': 'https://x/f.jpg'},
        meta={'name': 'Some Movie', 'runtime': '90 min'},
    )

    assert result is False
    assert captured['heading'] == 'Some Movie'
    assert captured['art'] == {'poster': 'https://x/p.jpg', 'fanart': 'https://x/f.jpg'}
    assert captured['meta'] == {'name': 'Some Movie', 'runtime': '90 min'}


def test_open_streams_window_is_closed_exactly_once_when_start_raises(load_streamswindow, monkeypatch):
    ctx = load_streamswindow()
    sw = ctx.streamswindow
    supported = {
        'transportUrl': 't-supported',
        'manifest': {'name': 'Supported', 'resources': ['stream'], 'types': ['movie']},
    }
    stream = {'url': 'https://a.example/a.mp4'}
    client = _FakeAddonClient({'t-supported': [stream]})
    _wire_data_layer(sw, _FakeStore(addons=[supported]), client)
    captured = {}

    class ExplodingWindow(sw.StreamsWindow):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.close_calls = 0
            captured['window'] = self

        def close(self):
            self.close_calls += 1
            super().close()

        def start(self, pairs, stype, sid, poster=None, heading='', art=None, meta=None, video_id=None):
            # Stands in for a crash inside onInit()/onAction() while the
            # modal loop is running - self.close() (the window's own,
            # normal-path close) never gets a chance to run.
            raise RuntimeError('onInit blew up')

    monkeypatch.setattr(sw, 'StreamsWindow', ExplodingWindow)

    result = sw.open_streams('movie', 'tt1')

    assert result is False
    win = captured['window']
    assert win.close_calls == 1
    assert win.closed is True
    assert ctx.env.notifications == [('Rivulet', 'STR30032', 'info', 4000)]


def test_open_streams_addonerror_is_logged_and_skipped_not_fatal(load_streamswindow, monkeypatch):
    ctx = load_streamswindow()
    import xbmc
    sw = ctx.streamswindow
    failing_transport = 'https://fail.example/manifest.json'
    ok_transport = 'https://ok.example/manifest.json'
    failing = {
        'transportUrl': failing_transport,
        'manifest': {'name': 'Failing', 'resources': ['stream'], 'types': ['movie']},
    }
    working = {
        'transportUrl': ok_transport,
        'manifest': {'name': 'Working', 'resources': ['stream'], 'types': ['movie']},
    }
    ok_stream = {'url': 'https://a.example/a.mp4'}
    client = _FakeAddonClient({failing_transport: AddonError('upstream down'), ok_transport: [ok_stream]})
    _wire_data_layer(sw, _FakeStore(addons=[failing, working]), client)
    captured = {}

    class RecordingWindow(sw.StreamsWindow):
        def start(self, pairs, stype, sid, poster=None, heading='', art=None, meta=None, video_id=None):
            captured['pairs'] = pairs
            return True

    monkeypatch.setattr(sw, 'StreamsWindow', RecordingWindow)
    # Not testing the round-trip here - stub it away (see
    # test_open_streams_filters_unsupported_addons_and_forwards_aggregate_to_the_window).
    monkeypatch.setattr(sw, '_wait_for_playback_end', lambda *a, **k: (False, False))

    result = sw.open_streams('movie', 'tt1')

    assert result is False
    assert [call[0] for call in client.calls] == [failing_transport, ok_transport]
    assert [s for _info, s in captured['pairs']] == [ok_stream]
    # The failing addon must never hit ERROR (that was the noisy old
    # behavior) - one DEBUG line naming its safe scheme+host, plus exactly
    # one aggregate WARNING summarizing the fetch, and nothing else at
    # WARNING/ERROR. The exception's own text ('upstream down') and the
    # transport's path (manifest.json) are never logged - only
    # safe_url_for_log()'s scheme+host and the exception's class name.
    assert not [lvl for _msg, lvl in ctx.env.log_calls if lvl == xbmc.LOGERROR]
    debug_msgs = [msg for msg, lvl in ctx.env.log_calls if lvl == xbmc.LOGDEBUG]
    assert any('fail.example' in msg and 'AddonError' in msg for msg in debug_msgs)
    assert not any('upstream down' in msg or 'manifest.json' in msg for msg in debug_msgs)
    warnings = [msg for msg, lvl in ctx.env.log_calls if lvl == xbmc.LOGWARNING]
    assert len(warnings) == 1
    assert 'streamswindow: 1 addon(s) failed' in warnings[0]


def test_open_streams_multiple_addon_failures_still_log_a_single_aggregate_warning(
    load_streamswindow, monkeypatch,
):
    ctx = load_streamswindow()
    import xbmc
    sw = ctx.streamswindow
    fail_a_transport = 'https://fail-a.example/manifest.json'
    fail_b_transport = 'https://fail-b.example/manifest.json'
    ok_transport = 'https://ok.example/manifest.json'
    fail_a = {
        'transportUrl': fail_a_transport,
        'manifest': {'name': 'FailA', 'resources': ['stream'], 'types': ['movie']},
    }
    fail_b = {
        'transportUrl': fail_b_transport,
        'manifest': {'name': 'FailB', 'resources': ['stream'], 'types': ['movie']},
    }
    working = {
        'transportUrl': ok_transport,
        'manifest': {'name': 'Working', 'resources': ['stream'], 'types': ['movie']},
    }
    ok_stream = {'url': 'https://a.example/a.mp4'}
    client = _FakeAddonClient({
        fail_a_transport: AddonError('boom a'), fail_b_transport: AddonError('boom b'), ok_transport: [ok_stream],
    })
    _wire_data_layer(sw, _FakeStore(addons=[fail_a, fail_b, working]), client)

    class RecordingWindow(sw.StreamsWindow):
        def start(self, pairs, stype, sid, poster=None, heading='', art=None, meta=None, video_id=None):
            return True

    monkeypatch.setattr(sw, 'StreamsWindow', RecordingWindow)
    monkeypatch.setattr(sw, '_wait_for_playback_end', lambda *a, **k: (False, False))

    result = sw.open_streams('movie', 'tt1')

    assert result is False
    debug_msgs = [msg for msg, lvl in ctx.env.log_calls if lvl == xbmc.LOGDEBUG]
    assert sum(1 for msg in debug_msgs if 'fail-a.example' in msg) == 1
    assert sum(1 for msg in debug_msgs if 'fail-b.example' in msg) == 1
    assert not any('boom a' in msg or 'boom b' in msg for msg in debug_msgs)
    warnings = [msg for msg, lvl in ctx.env.log_calls if lvl == xbmc.LOGWARNING]
    assert len(warnings) == 1
    assert 'streamswindow: 2 addon(s) failed' in warnings[0]
    assert not [lvl for _msg, lvl in ctx.env.log_calls if lvl == xbmc.LOGERROR]


def test_open_streams_addon_failure_log_never_leaks_credentials_path_or_query(load_streamswindow):
    ctx = load_streamswindow()
    import xbmc
    sw = ctx.streamswindow
    secret_transport = 'https://user:hunter2@evil.example:8443/private/path/manifest.json?token=abc123'
    failing = {
        'transportUrl': secret_transport,
        'manifest': {'name': 'Failing', 'resources': ['stream'], 'types': ['movie']},
    }
    client = _FakeAddonClient({
        secret_transport: AddonError('GET %s failed: bad request' % secret_transport),
    })
    _wire_data_layer(sw, _FakeStore(addons=[failing]), client)

    result = sw.open_streams('movie', 'tt1')

    assert result is False  # the only addon failed -> no streams at all
    all_messages = ' '.join(msg for msg, _level in ctx.env.log_calls)
    assert 'hunter2' not in all_messages
    assert 'token=abc123' not in all_messages
    assert '/private/path' not in all_messages
    debug_msgs = [msg for msg, lvl in ctx.env.log_calls if lvl == xbmc.LOGDEBUG]
    assert any('evil.example:8443' in msg for msg in debug_msgs)
    assert all('\n' not in msg and '\r' not in msg for msg in debug_msgs)


def test_open_streams_no_results_notifies_and_returns_false_without_building_a_window(
    load_streamswindow, monkeypatch,
):
    ctx = load_streamswindow()
    sw = ctx.streamswindow
    descriptor = {
        'transportUrl': 't1',
        'manifest': {'resources': ['stream'], 'types': ['movie']},
    }
    _wire_data_layer(sw, _FakeStore(addons=[descriptor]), _FakeAddonClient({'t1': []}))

    def _unexpected(*a, **k):
        raise AssertionError('StreamsWindow must never be constructed on an empty aggregate')

    monkeypatch.setattr(sw, 'StreamsWindow', _unexpected)

    result = sw.open_streams('movie', 'tt1')

    assert result is False
    assert ctx.env.notifications == [('Rivulet', 'STR30030', 'info', 4000)]


def test_open_streams_reads_stream_sort_setting_and_applies_it_to_final_order(load_streamswindow, monkeypatch):
    ctx = load_streamswindow()
    sw = ctx.streamswindow
    hi_res_low_seeds = {'id': 'hi-res'}
    lo_res_hi_seeds = {'id': 'lo-res'}
    descriptor = {
        'transportUrl': 't1',
        'manifest': {'resources': ['stream'], 'types': ['movie']},
    }
    _wire_data_layer(
        sw, _FakeStore(addons=[descriptor]),
        _FakeAddonClient({'t1': [hi_res_low_seeds, lo_res_hi_seeds]}),
    )

    def fake_parse_stream(stream, addon_name=''):
        if stream is hi_res_low_seeds:
            return {'resolution': '2160p', 'seeders': 1, 'size_bytes': 100}
        return {'resolution': '480p', 'seeders': 999, 'size_bytes': 100}

    monkeypatch.setattr(streaminfo, 'parse_stream', fake_parse_stream)
    captured = {}

    class RecordingWindow(sw.StreamsWindow):
        def start(self, pairs, stype, sid, poster=None, heading='', art=None, meta=None, video_id=None):
            captured['pairs'] = pairs
            return True

    monkeypatch.setattr(sw, 'StreamsWindow', RecordingWindow)
    # A played start() would otherwise round-trip forever (RecordingWindow
    # always returns True) - stub it away; this test only cares about sort
    # order, not the round-trip loop.
    monkeypatch.setattr(sw, '_wait_for_playback_end', lambda *a, **k: (False, False))

    # Default setting ('' -> 'quality'): resolution tier wins over seeders.
    sw.open_streams('movie', 'tt1')
    assert [s for _info, s in captured['pairs']] == [hi_res_low_seeds, lo_res_hi_seeds]

    # An explicit 'seeders' setting must flip the order for the SAME inputs.
    ctx.env.addon.settings['stream_sort'] = 'seeders'
    sw.open_streams('movie', 'tt1')
    assert [s for _info, s in captured['pairs']] == [lo_res_hi_seeds, hi_res_low_seeds]


# ---------------------------------------------------------------------------
# open_streams() - busy_dialog progress reporting/cancellation
# ---------------------------------------------------------------------------


def _cancel_after(n):
    """Builds a zero-arg closure for a scripted `iscanceled()` check that
    reports cancelled (True) starting from its (n+1)th call onward -
    same no-arg call convention `RivuletBusy.iscanceled()` itself uses
    (unlike `Monitor.waitForAbort()`'s 1-based-count-arg convention)."""
    state = {'calls': 0}

    def _check():
        state['calls'] += 1
        return state['calls'] > n
    return _check


def _record_busy_calls(monkeypatch, dialogs_mod):
    """Monkeypatches `lib.ui.dialogs.RivuletBusy`'s create()/update()/
    close() to record calls in the same (heading, message)/(percent,
    message)/count shape the old `xbmcgui.DialogProgress` fake exposed
    as `env.dialog_created`/`dialog_updates`/`dialog_closed_count`,
    while still delegating to the real implementation so the fetch
    loop's dialog is genuinely created/updated/closed against the fake
    window/controls too. Mirrors test_router.py's `_record_progress_calls`."""
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


def test_open_streams_busy_dialog_reports_progress_and_skips_unsupported_addons(
    load_streamswindow, monkeypatch,
):
    ctx = load_streamswindow()
    sw = ctx.streamswindow
    alpha = {
        'transportUrl': 't-alpha',
        'manifest': {'name': 'Alpha', 'resources': ['stream'], 'types': ['movie']},
    }
    unsupported = {
        'transportUrl': 't-unsupported',
        # no 'stream' resource -> excluded before total_addons is even computed.
        'manifest': {'name': 'Unsupported', 'resources': ['catalog'], 'types': ['movie']},
    }
    alpha_stream = {'url': 'https://a.example/a.mp4'}
    client = _FakeAddonClient({'t-alpha': [alpha_stream]})
    _wire_data_layer(sw, _FakeStore(addons=[alpha, unsupported]), client)
    captured = {}

    class RecordingWindow(sw.StreamsWindow):
        def start(self, pairs, stype, sid, poster=None, heading='', art=None, meta=None, video_id=None):
            captured['pairs'] = pairs
            return True

    monkeypatch.setattr(sw, 'StreamsWindow', RecordingWindow)
    monkeypatch.setattr(sw, '_wait_for_playback_end', lambda *a, **k: (False, False))
    busy = _record_busy_calls(monkeypatch, ctx.dialogs)

    result = sw.open_streams('movie', 'tt1')

    assert result is False
    assert [call[0] for call in client.calls] == ['t-alpha']  # the unsupported addon is never even queried
    assert [s for _info, s in captured['pairs']] == [alpha_stream]
    assert busy.created == [('STR30033', '')]
    # total_addons is 1 (the unsupported addon never counts toward the
    # denominator) - one 'Checking Alpha...' update at 100%, on top of
    # busy_dialog's own initial update(0, message) on entry.
    assert busy.updated == [
        (0, ''),
        (100, 'Checking Alpha...'),
    ]
    assert busy.closed == 1


def test_open_streams_cancelled_while_still_waiting_for_a_non_empty_result_falls_back_to_no_results(
    load_streamswindow, monkeypatch,
):
    """Every addon queried concurrently now fires its own HTTP call
    immediately regardless of cancellation (unlike the old serial loop,
    which could skip an addon it never reached) - so cancellation can no
    longer stop an addon from being QUERIED, only stop open_streams()
    from continuing to WAIT for a non-empty result. Two addons that both
    answer empty force the wait loop to actually check
    `dialog.iscanceled()` more than once before giving up."""
    ctx = load_streamswindow()
    sw = ctx.streamswindow
    empty_a = {
        'transportUrl': 't-empty-a',
        'manifest': {'name': 'A', 'resources': ['stream'], 'types': ['movie']},
    }
    empty_b = {
        'transportUrl': 't-empty-b',
        'manifest': {'name': 'B', 'resources': ['stream'], 'types': ['movie']},
    }
    client = _FakeAddonClient({'t-empty-a': [], 't-empty-b': []})
    _wire_data_layer(sw, _FakeStore(addons=[empty_a, empty_b]), client)
    # RivuletBusy.iscanceled() is checked by the REAL wait loop inside
    # _fetch_stream_pairs()/open_streams() (not mocked here) - it has no
    # BACK_ACTIONS onAction() to drive since the dialog is created
    # internally, so the scripted check is patched onto the class
    # itself instead, same as test_router.py's mid-download cancel test.
    _scripted_cancel = _cancel_after(1)
    monkeypatch.setattr(ctx.dialogs.RivuletBusy, 'iscanceled', lambda self: _scripted_cancel())
    busy = _record_busy_calls(monkeypatch, ctx.dialogs)

    def _unexpected(*a, **k):
        raise AssertionError('StreamsWindow must never be constructed when the wait is cancelled with nothing found')

    monkeypatch.setattr(sw, 'StreamsWindow', _unexpected)

    result = sw.open_streams('movie', 'tt1')

    assert result is False
    assert ctx.env.notifications == [('Rivulet', 'STR30030', 'info', 4000)]
    assert busy.closed == 1


def test_open_streams_cancelled_before_first_addon_falls_back_to_no_results(
    load_streamswindow, monkeypatch,
):
    ctx = load_streamswindow()
    sw = ctx.streamswindow
    descriptor = {
        'transportUrl': 't1',
        'manifest': {'name': 'Alpha', 'resources': ['stream'], 'types': ['movie']},
    }
    client = _FakeAddonClient({'t1': [{'url': 'https://a.example/a.mp4'}]})
    _wire_data_layer(sw, _FakeStore(addons=[descriptor]), client)
    # Already cancelled before the wait ever starts - see the comment on
    # the scripted RivuletBusy.iscanceled() patch above.
    monkeypatch.setattr(ctx.dialogs.RivuletBusy, 'iscanceled', lambda self: True)
    busy = _record_busy_calls(monkeypatch, ctx.dialogs)

    def _unexpected(*a, **k):
        raise AssertionError('StreamsWindow must never be constructed on an empty aggregate')

    monkeypatch.setattr(sw, 'StreamsWindow', _unexpected)

    result = sw.open_streams('movie', 'tt1')

    # Every addon is now submitted to the fan-out CONCURRENTLY, before
    # open_streams() ever checks cancellation - unlike the old serial
    # loop, which gated each addon's own HTTP call behind that same
    # check, cancelling before the first addon can no longer prevent it
    # from being queried. What it DOES still guarantee is the same
    # user-visible outcome: no window, and the "no results" notification.
    assert result is False
    assert ctx.env.notifications == [('Rivulet', 'STR30030', 'info', 4000)]
    assert busy.closed == 1


# ---------------------------------------------------------------------------
# Progressive picker: open_streams() opens on the first addon's own
# results without waiting for a slower one, and StreamsWindow.add_pairs()/
# set_loading() feed the rest in live - see the module docstring's
# concurrency/progressive-opening paragraph.
# ---------------------------------------------------------------------------


def test_open_streams_opens_on_the_first_addon_without_waiting_for_a_slower_one(
    load_streamswindow, monkeypatch,
):
    ctx = load_streamswindow()
    sw = ctx.streamswindow
    fast = {
        'transportUrl': 't-fast',
        'manifest': {'name': 'Fast', 'resources': ['stream'], 'types': ['movie']},
    }
    slow = {
        'transportUrl': 't-slow',
        'manifest': {'name': 'Slow', 'resources': ['stream'], 'types': ['movie']},
    }
    fast_stream = {'url': 'https://fast.example/a.mp4'}
    slow_stream = {'url': 'https://slow.example/a.mp4'}
    release_slow = threading.Event()
    slow_answered = threading.Event()

    class _OrderedClient:
        """Blocks the slow addon's own answer until the test releases it
        - guarantees the fast addon is always the one open_streams() sees
        first, deterministically, regardless of real thread scheduling."""

        def __init__(self):
            self.calls = []

        def streams(self, transport, stype, sid):
            self.calls.append(transport)
            if transport == 't-slow':
                release_slow.wait(2)
                slow_answered.set()
                return [slow_stream]
            return [fast_stream]

    client = _OrderedClient()
    _wire_data_layer(sw, _FakeStore(addons=[fast, slow]), client)
    captured = {}

    class RecordingWindow(sw.StreamsWindow):
        def set_loading(self, loading):
            captured.setdefault('loading_calls', []).append(loading)
            super().set_loading(loading)

        def start(self, pairs, stype, sid, poster=None, heading='', art=None, meta=None, video_id=None):
            captured['pairs'] = list(pairs)
            captured['slow_answered_before_open'] = slow_answered.is_set()
            release_slow.set()  # only let the slow addon finish once the picker has already decided to open
            return False

    monkeypatch.setattr(sw, 'StreamsWindow', RecordingWindow)

    result = sw.open_streams('movie', 'tt1')

    assert result is False
    assert not captured['slow_answered_before_open']
    assert [s for _info, s in captured['pairs']] == [fast_stream]
    assert captured['loading_calls'] == [True]  # the slow addon was still outstanding when this window opened


def test_streamswindow_add_pairs_from_a_worker_thread_never_touches_controls_until_the_gui_drain_runs(
    load_streamswindow,
):
    ctx = load_streamswindow()
    sw = ctx.streamswindow
    win = _make_window(sw)
    info = {'addon': 'A', 'raw': 'Row A'}
    stream = {'url': 'https://a.example/a.mp4'}
    win.start([(info, stream)], 'movie', 'tt1')
    win.onInit()

    new_info = {'addon': 'B', 'raw': 'Row B'}
    new_stream = {'url': 'https://b.example/b.mp4'}

    # add_pairs() runs on a real background thread here - proving it is
    # actually thread-safe, not merely "called synchronously and happens
    # not to touch anything".
    worker = threading.Thread(target=win.add_pairs, args=([(new_info, new_stream)],))
    worker.start()
    worker.join(2)

    assert win.pairs == [(info, stream)]  # not merged yet - only queued
    assert len(win.getControl(sw.LIST).items) == 1  # onInit()'s original single row, untouched

    win.onAction(_FakeBackAction(-1))

    assert [s for _i, s in win.pairs] == [stream, new_stream]
    assert len(win.getControl(sw.LIST).items) == 2


class _FakeBackAction:
    """Minimal `xbmcgui.Action`-shaped stand-in for a non-back keypress -
    `getId()` alone is what `BaseWindow.onAction()`/`StreamsWindow.onAction()`
    read."""

    def __init__(self, action_id):
        self._id = action_id

    def getId(self):
        return self._id


def test_streamswindow_add_pairs_drain_resorts_and_preserves_focus_by_identity_not_equality(
    load_streamswindow, monkeypatch,
):
    ctx = load_streamswindow()
    sw = ctx.streamswindow
    win = _make_window(sw)
    low_info = {'addon': 'A', 'raw': 'Low'}
    low_stream_1 = {'url': 'https://a.example/low.mp4'}  # focused row - must stay selected after the re-sort
    low_stream_2 = dict(low_stream_1)  # a DIFFERENT object, but an EQUAL dict - the identity trap
    win.start([(low_info, low_stream_1)], 'movie', 'tt1')
    win.onInit()
    win.getControl(sw.LIST).selected_index = 0

    def fake_sort_streams(pairs, key='quality'):
        # Put whatever is NOT the originally-focused pair first, so a
        # naive re-select-by-index (rather than by identity) would land
        # on the wrong row.
        return sorted(pairs, key=lambda pair: pair[1] is low_stream_1)

    monkeypatch.setattr(streaminfo, 'sort_streams', fake_sort_streams)

    high_info = {'addon': 'B', 'raw': 'High'}
    win.add_pairs([(high_info, low_stream_2)])
    win.onAction(_FakeBackAction(-1))

    assert [s for _i, s in win.pairs] == [low_stream_2, low_stream_1]  # low_stream_1 sorted to the END
    focused = win.getControl(sw.LIST).getSelectedItem()
    focused_pair = win.pairs[int(focused.getProperty('position'))]
    assert focused_pair[1] is low_stream_1  # NOT low_stream_2, despite comparing equal as a dict
    assert low_stream_1 == low_stream_2  # the equality trap this test guards against


# ---------------------------------------------------------------------------
# StreamsWindow._apply_pending()/_rebuild_list() - the append-only fast
# path: when a live add_pairs() merge's re-sort leaves every already-
# rendered row exactly where it was, only the new suffix gets built and
# control.addItems()-ed (no reset(), no O(N) identity search - see
# _append_prefix_length()'s own docstring). Otherwise falls back to the
# full reset()+rebuild the tests above already cover.
# ---------------------------------------------------------------------------


def _spy_control_calls(control):
    """Wraps `control.reset`/`control.addItems` with recording spies
    (instance-level monkeypatch - `FakeWindowControl` is a plain object,
    no call-tracking of its own) and returns `(reset_calls, added_batches)`,
    each a list appended to on every real call, still forwarding through
    to the original behaviour."""
    reset_calls = []
    original_reset = control.reset

    def spy_reset():
        reset_calls.append(True)
        original_reset()

    control.reset = spy_reset

    added_batches = []
    original_add_items = control.addItems

    def spy_add_items(items):
        added_batches.append(list(items))
        original_add_items(items)

    control.addItems = spy_add_items
    return reset_calls, added_batches


def test_apply_pending_batch_sorting_strictly_after_existing_rows_appends_without_reset(
    load_streamswindow,
):
    ctx = load_streamswindow()
    sw = ctx.streamswindow
    win = _make_window(sw)
    info_a = {'addon': 'A', 'raw': 'Row A', 'resolution': '1080p'}
    stream_a = {'url': 'https://a.example/a.mp4'}
    win.start([(info_a, stream_a)], 'movie', 'tt1')
    win.onInit()

    control = win.getControl(sw.LIST)
    reset_calls, added_batches = _spy_control_calls(control)

    # Lower resolution tier than info_a - sort_streams' default 'quality'
    # key sorts it strictly AFTER the already-rendered row.
    info_b = {'addon': 'A', 'raw': 'Row B', 'resolution': '720p'}
    stream_b = {'url': 'https://a.example/b.mp4'}
    win.add_pairs([(info_b, stream_b)])
    win.onAction(_FakeBackAction(-1))

    assert reset_calls == []  # append-only fast path never resets
    assert len(added_batches) == 1
    assert [item.getProperty('position') for item in added_batches[0]] == ['1']  # ONLY the new row was built
    assert [s for _i, s in win.pairs] == [stream_a, stream_b]
    assert len(control.items) == 2


def test_apply_pending_batch_interleaving_existing_rows_falls_back_to_full_rebuild(
    load_streamswindow,
):
    ctx = load_streamswindow()
    sw = ctx.streamswindow
    win = _make_window(sw)
    info_a = {'addon': 'A', 'raw': 'Row A', 'resolution': '720p'}
    stream_a = {'url': 'https://a.example/a.mp4'}
    win.start([(info_a, stream_a)], 'movie', 'tt1')
    win.onInit()
    win.getControl(sw.LIST).selected_index = 0  # focus the only row before the merge

    control = win.getControl(sw.LIST)
    reset_calls, added_batches = _spy_control_calls(control)

    # Higher resolution tier than info_a - sort_streams' default
    # 'quality' key sorts it BEFORE the already-rendered row, so the old
    # prefix is no longer a prefix of the re-sorted list.
    info_b = {'addon': 'B', 'raw': 'Row B', 'resolution': '1080p'}
    stream_b = {'url': 'https://b.example/b.mp4'}
    win.add_pairs([(info_b, stream_b)])
    win.onAction(_FakeBackAction(-1))

    assert reset_calls == [True]  # fallback still does a full reset()+rebuild
    assert len(added_batches) == 1 and len(added_batches[0]) == 2  # every row rebuilt, not just the new one
    assert [s for _i, s in win.pairs] == [stream_b, stream_a]  # new row sorted BEFORE the existing one
    focused = win.getControl(sw.LIST).getSelectedItem()
    focused_pair = win.pairs[int(focused.getProperty('position'))]
    assert focused_pair[1] is stream_a  # focus followed the original row to its new position


def test_apply_pending_fast_path_and_full_rebuild_produce_identical_final_list_contents(
    load_streamswindow,
):
    """Same two pairs, reached two different ways - `fast_win` renders
    `info_a` alone, then merges `info_b` in via the append-only fast
    path; `full_win` renders both at once through the ordinary
    reset()+rebuild `onInit()` always takes. Both must produce the same
    rows: the fast path is an optimization, never an alternate render."""
    ctx = load_streamswindow()
    sw = ctx.streamswindow

    info_a = {'addon': 'A', 'raw': 'Row A', 'resolution': '1080p'}
    stream_a = {'url': 'https://a.example/a.mp4'}
    info_b = {'addon': 'A', 'raw': 'Row B', 'resolution': '720p'}
    stream_b = {'url': 'https://a.example/b.mp4'}

    fast_win = _make_window(sw)
    fast_win.start([(info_a, stream_a)], 'movie', 'tt1')
    fast_win.onInit()
    fast_win.add_pairs([(info_b, stream_b)])
    fast_win.onAction(_FakeBackAction(-1))

    full_win = _make_window(sw)
    full_win.start([(info_a, stream_a), (info_b, stream_b)], 'movie', 'tt1')
    full_win.onInit()

    def snapshot(win):
        items = win.getControl(sw.LIST).items
        return [
            (item.getLabel(), item.label2, item.getProperty('position'), item.getProperty('quality'))
            for item in items
        ]

    assert snapshot(fast_win) == snapshot(full_win)
    assert [s for _i, s in fast_win.pairs] == [s for _i, s in full_win.pairs] == [stream_a, stream_b]


def test_streamswindow_add_pairs_after_close_is_a_silent_noop(load_streamswindow):
    ctx = load_streamswindow()
    sw = ctx.streamswindow
    win = _make_window(sw)
    info = {'addon': 'A', 'raw': 'Row A'}
    stream = {'url': 'https://a.example/a.mp4'}
    win.start([(info, stream)], 'movie', 'tt1')
    win.onInit()
    win.close()

    win.add_pairs([({'addon': 'B'}, {'url': 'https://b.example/b.mp4'})])
    win.set_loading(True)

    assert win.pairs == [(info, stream)]  # nothing merged
    assert ctx.env.executed_builtins == []  # never even woke the GUI thread




def test_fetch_stream_pairs_aggregates_every_addon_and_logs_a_single_warning_on_failure(
    load_streamswindow,
):
    ctx = load_streamswindow()
    import xbmc
    sw = ctx.streamswindow
    failing_transport = 'https://fail.example/manifest.json'
    ok_transport = 'https://ok.example/manifest.json'
    failing = {
        'transportUrl': failing_transport,
        'manifest': {'name': 'Failing', 'resources': ['stream'], 'types': ['movie']},
    }
    working = {
        'transportUrl': ok_transport,
        'manifest': {'name': 'Working', 'resources': ['stream'], 'types': ['movie']},
    }
    ok_stream = {'url': 'https://a.example/a.mp4'}
    client = _FakeAddonClient({failing_transport: AddonError('upstream down'), ok_transport: [ok_stream]})
    _wire_data_layer(sw, _FakeStore(addons=[failing, working]), client)

    pairs = sw._fetch_stream_pairs('movie', 'tt1')

    assert [s for _info, s in pairs] == [ok_stream]
    assert sorted(call[0] for call in client.calls) == [failing_transport, ok_transport]
    warnings = [msg for msg, lvl in ctx.env.log_calls if lvl == xbmc.LOGWARNING]
    assert len(warnings) == 1 and 'streamswindow: 1 addon(s) failed' in warnings[0]


# ---------------------------------------------------------------------------
# _start_stream_fetch_workers() - the bounded raw-daemon-thread fan-out
# both _fetch_stream_pairs() and open_streams() feed from. Regression
# coverage for the defect this whole helper replaces
# ThreadPoolExecutor to fix: concurrent.futures.thread's atexit hook
# JOINS every worker at interpreter shutdown regardless of daemon flag,
# so a still-running addon fetch blocked process exit for 6.0s on both
# Python 3.8 and 3.13 even with pool.shutdown(wait=False). Raw daemon
# threads have no such hook - which only holds if every thread this
# helper starts is ACTUALLY daemon, and there are never more of them
# than _MAX_STREAM_ADDON_WORKERS regardless of how many addons are fed
# in - both asserted directly here rather than through a real interpreter
# exit (which this suite has no way to observe).
# ---------------------------------------------------------------------------


def _spy_on_threads(monkeypatch, streamswindow_mod):
    """Wraps `streamswindow_mod.threading.Thread` so every instance it
    constructs (not just `.start()`ed ones) is recorded, and returns the
    list those instances land in - the deterministic "threads the helper
    creates" this file's daemon-thread/bounded-pool tests inspect,
    instead of the process-wide (and thus test-order-sensitive)
    `threading.enumerate()`."""
    created = []
    real_thread = streamswindow_mod.threading.Thread

    def _make_thread(*args, **kwargs):
        thread = real_thread(*args, **kwargs)
        created.append(thread)
        return thread

    monkeypatch.setattr(streamswindow_mod.threading, 'Thread', _make_thread)
    return created


def test_start_stream_fetch_workers_starts_only_daemon_threads(load_streamswindow, monkeypatch):
    ctx = load_streamswindow()
    sw = ctx.streamswindow
    addons = [
        ({'transportUrl': 't0'}, {'name': 'A0'}),
        ({'transportUrl': 't1'}, {'name': 'A1'}),
        ({'transportUrl': 't2'}, {'name': 'A2'}),
    ]
    client = _FakeAddonClient({'t0': [], 't1': [], 't2': []})
    sw.get_client = lambda: client
    created = _spy_on_threads(monkeypatch, sw)

    results = sw._start_stream_fetch_workers('movie', 'tt1', addons)

    for _ in addons:
        results.get(timeout=2)  # drain every answer so no worker outlives the test
    for thread in created:
        thread.join(2)

    assert len(created) == len(addons)
    assert all(thread.daemon for thread in created)


def test_start_stream_fetch_workers_never_starts_more_threads_than_the_worker_cap(
    load_streamswindow, monkeypatch,
):
    ctx = load_streamswindow()
    sw = ctx.streamswindow
    addon_count = sw._MAX_STREAM_ADDON_WORKERS + 5
    stream_results = {'t%d' % i: [] for i in range(addon_count)}
    addons = [({'transportUrl': 't%d' % i}, {'name': 'A%d' % i}) for i in range(addon_count)]
    client = _FakeAddonClient(stream_results)
    sw.get_client = lambda: client
    created = _spy_on_threads(monkeypatch, sw)

    results = sw._start_stream_fetch_workers('movie', 'tt1', addons)

    for _ in addons:
        results.get(timeout=2)  # drain every answer so no worker outlives the test
    for thread in created:
        thread.join(2)

    assert len(created) == sw._MAX_STREAM_ADDON_WORKERS
    assert all(thread.daemon for thread in created)
    assert len(client.calls) == addon_count  # every addon still got queried, just via a bounded pool



# ---------------------------------------------------------------------------
# _wait_for_playback_end() - the injectable poll-loop helper open_streams()
# uses to decide when it's safe to reopen the picker after a played pick.
# Exercised directly here via its player=/monitor= injection points; the
# open_streams() round-trip section further below re-exercises the SAME
# helper through the real installed xbmc.Player()/xbmc.Monitor() fakes to
# prove the production wiring - not just the helper in isolation - reopens
# correctly.
# ---------------------------------------------------------------------------


class _ScriptedPlayer:
    """Minimal `xbmc.Player`-shaped fake for direct `_wait_for_playback_end()`
    tests: `is_playing` is a plain bool, or a callable taking the 1-based
    call count (mirrors tests/kodistubs' `env.monitor_abort` convention).
    `.calls` records every `isPlaying()` poll. `ended_naturally` mirrors
    the real `_PlaybackEndWatcher`'s own attribute, letting a test drive
    either a natural-end or a stopped outcome directly."""

    def __init__(self, is_playing, ended_naturally=False):
        self._is_playing = is_playing
        self.calls = 0
        self.ended_naturally = ended_naturally

    def isPlaying(self):
        self.calls += 1
        playing = self._is_playing
        return bool(playing(self.calls)) if callable(playing) else bool(playing)


class _ScriptedMonitor:
    """Minimal `xbmc.Monitor`-shaped fake: `abort` is a plain bool, or a
    callable taking the 1-based call count. `.calls` records every
    `waitForAbort()` poll."""

    def __init__(self, abort=False):
        self._abort = abort
        self.calls = 0

    def waitForAbort(self, timeout=None):
        self.calls += 1
        abort = self._abort
        return bool(abort(self.calls)) if callable(abort) else bool(abort)


def test_wait_for_playback_end_polls_until_playing_starts_then_until_it_stops(load_streamswindow):
    ctx = load_streamswindow()
    sw = ctx.streamswindow
    # call 1: not yet playing; calls 2-3: playing; call 4: stopped.
    player = _ScriptedPlayer(is_playing=lambda n: n in (2, 3), ended_naturally=True)
    monitor = _ScriptedMonitor(abort=False)

    result = sw._wait_for_playback_end(player=player, monitor=monitor, start_timeout=5.0, tick=0.1)

    assert result == (True, True)
    assert player.calls == 4
    assert monitor.calls == 2  # one abort check per tick that didn't already end the wait


def test_wait_for_playback_end_reports_ended_naturally_false_when_the_player_was_stopped(load_streamswindow):
    ctx = load_streamswindow()
    sw = ctx.streamswindow
    # Same poll shape as the natural-end test above, but the injected
    # player reports it was stopped, not ended - _wait_for_playback_end()
    # must forward that through unchanged.
    player = _ScriptedPlayer(is_playing=lambda n: n in (2, 3), ended_naturally=False)
    monitor = _ScriptedMonitor(abort=False)

    result = sw._wait_for_playback_end(player=player, monitor=monitor, start_timeout=5.0, tick=0.1)

    assert result == (True, False)


def test_wait_for_playback_end_returns_true_when_playback_never_starts_within_timeout(load_streamswindow):
    ctx = load_streamswindow()
    sw = ctx.streamswindow
    player = _ScriptedPlayer(is_playing=False, ended_naturally=True)  # never starts
    monitor = _ScriptedMonitor(abort=False)

    result = sw._wait_for_playback_end(player=player, monitor=monitor, start_timeout=1.0, tick=0.5)

    # nothing left to wait out - safe to reopen anyway, but nothing played
    # so ended_naturally is always False here regardless of the player's
    # own attribute.
    assert result == (True, False)
    assert player.calls == 2  # int(1.0 / 0.5) start-wait attempts
    assert monitor.calls == 2


def test_wait_for_playback_end_returns_false_immediately_on_monitor_abort_before_playing(load_streamswindow):
    ctx = load_streamswindow()
    sw = ctx.streamswindow
    player = _ScriptedPlayer(is_playing=False)
    monitor = _ScriptedMonitor(abort=True)  # aborts on the very first poll

    result = sw._wait_for_playback_end(player=player, monitor=monitor, start_timeout=20.0, tick=0.5)

    assert result == (False, False)
    assert player.calls == 1
    assert monitor.calls == 1  # stopped on the very first abort check, not the full budget


def test_wait_for_playback_end_returns_false_immediately_on_monitor_abort_while_playing(load_streamswindow):
    ctx = load_streamswindow()
    sw = ctx.streamswindow
    player = _ScriptedPlayer(is_playing=True)  # already playing on the very first check
    monitor = _ScriptedMonitor(abort=True)

    result = sw._wait_for_playback_end(player=player, monitor=monitor, start_timeout=20.0, tick=0.5)

    assert result == (False, False)
    assert player.calls == 2  # loop1's break, then loop2's own isPlaying() check
    assert monitor.calls == 1  # loop2's first abort check ends the wait immediately


def test_wait_for_playback_end_swallows_an_exception_and_returns_false(load_streamswindow):
    ctx = load_streamswindow()
    import xbmc
    sw = ctx.streamswindow

    class _ExplodingPlayer:
        def isPlaying(self):
            raise RuntimeError('boom')

    result = sw._wait_for_playback_end(
        player=_ExplodingPlayer(), monitor=_ScriptedMonitor(), start_timeout=1.0, tick=0.5,
    )

    assert result == (False, False)
    warnings = [(msg, lvl) for msg, lvl in ctx.env.log_calls if lvl == xbmc.LOGWARNING]
    assert len(warnings) == 1
    assert 'boom' in warnings[0][0]


# ---------------------------------------------------------------------------
# open_streams() - the post-playback reopen round trip. Uses the SAME
# xbmc.Player()/xbmc.Monitor() fakes every other test in this file gets from
# tests/kodistubs, scripted via ctx.env.player_is_playing (mirrors
# ctx.env.cancel/ctx.env.monitor_abort's plain-bool-or-1-based-callable
# convention - see tests/kodistubs/modules.py's Player.isPlaying()), to
# prove the PRODUCTION wiring - not just the _wait_for_playback_end() unit
# above - actually reopens (or doesn't) at the right moments. Exact
# player_is_playing_calls/monitor_abort_calls counts below were verified
# against the real implementation, not hand-derived.
# ---------------------------------------------------------------------------


def _wire_single_supported_addon(sw, stream=None):
    """Wires exactly one supported addon returning one `stream` (default a
    generic movie url) - the minimal aggregate the round-trip tests below
    need; they exercise the reopen mechanics, not aggregation, so every
    detail here is deliberately arbitrary/interchangeable. Returns
    `(client, stream)` so a test can assert on `client.calls`."""
    stream = stream or {'url': 'https://a.example/a.mp4'}
    supported = {
        'transportUrl': 't-supported',
        'manifest': {'name': 'Supported', 'resources': ['stream'], 'types': ['movie']},
    }
    client = _FakeAddonClient({'t-supported': [stream]})
    _wire_data_layer(sw, _FakeStore(addons=[supported]), client)
    return client, stream


def test_open_streams_reopens_with_the_same_pairs_after_a_played_round_trip_then_returns_false(
    load_streamswindow, monkeypatch,
):
    ctx = load_streamswindow()
    sw = ctx.streamswindow
    client, stream = _wire_single_supported_addon(sw)
    start_calls = []

    class RecordingWindow(sw.StreamsWindow):
        def start(self, pairs, stype, sid, poster=None, heading='', art=None, meta=None, video_id=None):
            # open_streams() now reads win.pairs back after this returns
            # (the live streaming fan-out may have accumulated more than
            # what was passed in) - mirror that part of the real
            # start()'s contract so the reopen below sees the same rows.
            self.pairs = list(pairs)
            start_calls.append((pairs, stype, sid, poster, heading, art, meta))
            return len(start_calls) == 1  # plays the first time, backs out of the reopened window

    monkeypatch.setattr(sw, 'StreamsWindow', RecordingWindow)
    ctx.env.player_is_playing = lambda n: n <= 2  # "playing" for 2 polls, then stopped
    meta = {'name': 'Some Movie', 'runtime': '90 min'}

    result = sw.open_streams('movie', 'tt1', heading='Some Movie', art={'poster': 'https://x/p.jpg'}, meta=meta)

    assert result is False
    assert len(start_calls) == 2
    # Same rows/heading/art/meta on reopen - but no longer the identical
    # `pairs` OBJECT: open_streams() now re-reads win.pairs after the
    # first window closes (a single-addon fetch has nothing left to
    # stream in here, but the accumulation point is real - see the
    # module docstring), which is a fresh list StreamsWindow.start()
    # itself copies `pairs` into, not the one open_streams() fetched.
    assert start_calls[0][1:] == start_calls[1][1:]
    assert start_calls[0][0] == start_calls[1][0]
    assert start_calls[0][6] is meta is start_calls[1][6]  # meta threaded through unchanged, same object
    assert len(client.calls) == 1  # addon streams were fetched only once, never re-fetched
    assert [s for _info, s in start_calls[0][0]] == [stream]


def test_open_streams_user_cancel_on_first_window_returns_false_without_waiting_or_reopening(
    load_streamswindow, monkeypatch,
):
    ctx = load_streamswindow()
    sw = ctx.streamswindow
    _wire_single_supported_addon(sw)
    start_calls = []

    class RecordingWindow(sw.StreamsWindow):
        def start(self, pairs, stype, sid, poster=None, heading='', art=None, meta=None, video_id=None):
            start_calls.append(1)
            return False  # user backed out without picking anything

    monkeypatch.setattr(sw, 'StreamsWindow', RecordingWindow)

    result = sw.open_streams('movie', 'tt1')

    assert result is False
    assert len(start_calls) == 1  # no reopen
    assert ctx.env.player_is_playing_calls == 0  # the wait helper was never even entered
    assert ctx.env.monitor_abort_calls == 0


def test_open_streams_reopens_even_when_playback_never_starts_within_the_timeout(
    load_streamswindow, monkeypatch,
):
    ctx = load_streamswindow()
    sw = ctx.streamswindow
    _wire_single_supported_addon(sw)
    start_calls = []

    class RecordingWindow(sw.StreamsWindow):
        def start(self, pairs, stype, sid, poster=None, heading='', art=None, meta=None, video_id=None):
            start_calls.append(1)
            return len(start_calls) == 1  # "played" once, then the reopened window backs out

    monkeypatch.setattr(sw, 'StreamsWindow', RecordingWindow)
    # ctx.env.player_is_playing defaults to False forever - Kodi's player
    # never actually reports playing, exhausting _wait_for_playback_end()'s
    # default 20s/0.5s start-wait budget.

    result = sw.open_streams('movie', 'tt1')

    assert result is False
    assert len(start_calls) == 2  # reopened despite playback never starting
    assert ctx.env.player_is_playing_calls == 40  # int(20.0 / 0.5) start-wait attempts
    assert ctx.env.monitor_abort_calls == 41  # 40 start-wait ticks + the settle pause


def test_open_streams_monitor_abort_before_playing_returns_false_without_reopening(
    load_streamswindow, monkeypatch,
):
    ctx = load_streamswindow()
    sw = ctx.streamswindow
    _wire_single_supported_addon(sw)
    start_calls = []

    class RecordingWindow(sw.StreamsWindow):
        def start(self, pairs, stype, sid, poster=None, heading='', art=None, meta=None, video_id=None):
            start_calls.append(1)
            return True  # must never be reached a second time

    monkeypatch.setattr(sw, 'StreamsWindow', RecordingWindow)
    ctx.env.monitor_abort = True  # Kodi shutting down - aborts the very first poll

    result = sw.open_streams('movie', 'tt1')

    assert result is False
    assert len(start_calls) == 1  # no reopen
    assert ctx.env.monitor_abort_calls == 1


def test_open_streams_monitor_abort_while_playing_returns_false_without_reopening(
    load_streamswindow, monkeypatch,
):
    ctx = load_streamswindow()
    sw = ctx.streamswindow
    _wire_single_supported_addon(sw)
    start_calls = []

    class RecordingWindow(sw.StreamsWindow):
        def start(self, pairs, stype, sid, poster=None, heading='', art=None, meta=None, video_id=None):
            start_calls.append(1)
            return True

    monkeypatch.setattr(sw, 'StreamsWindow', RecordingWindow)
    ctx.env.player_is_playing = True  # already playing from the very first check
    ctx.env.monitor_abort = True

    result = sw.open_streams('movie', 'tt1')

    assert result is False
    assert len(start_calls) == 1  # no reopen
    assert ctx.env.monitor_abort_calls == 1


def test_open_streams_monitor_abort_during_settle_pause_returns_false_without_reopening(
    load_streamswindow, monkeypatch,
):
    ctx = load_streamswindow()
    sw = ctx.streamswindow
    _wire_single_supported_addon(sw)
    start_calls = []

    class RecordingWindow(sw.StreamsWindow):
        def start(self, pairs, stype, sid, poster=None, heading='', art=None, meta=None, video_id=None):
            start_calls.append(1)
            return True  # must never be reached a second time

    monkeypatch.setattr(sw, 'StreamsWindow', RecordingWindow)
    # Playing only on the very first poll, then stopped - _wait_for_playback_end()
    # itself never touches the monitor at all (loop1 breaks immediately, loop2's
    # own isPlaying() check is already False) - so the ONE waitForAbort() call
    # below is unambiguously the post-wait settle pause's own.
    ctx.env.player_is_playing = lambda n: n == 1
    ctx.env.monitor_abort = True

    result = sw.open_streams('movie', 'tt1')

    assert result is False
    assert len(start_calls) == 1  # no reopen - shutdown safety on the settle pause too
    assert ctx.env.monitor_abort_calls == 1


# ---------------------------------------------------------------------------
# open_streams() - the binge-watching round trip (lib.ui.binge.next_video()/
# pick_binge_stream() wired into _try_binge_watch(), which runs right after
# the SAME _wait_for_playback_end()/settle-pause the plain reopen round trip
# above already exercises). `video_id`/`meta` drive it entirely; see
# lib/ui/binge.py's own module docstring and tests/test_binge.py for the
# pure "what's next"/"which stream" logic these tests wire end to end.
# ---------------------------------------------------------------------------


class _PerEpisodeAddonClient:
    """Fake `lib.stremio.addons.AddonClient` keyed by `(transport, sid)`,
    not just `transport` like `_FakeAddonClient` above - the
    binge-watching round trip fetches TWO different episode ids from the
    SAME addon (the one just played, then its next one[s]) and each must
    answer with its own stream list. `.calls` mirrors `_FakeAddonClient`'s
    own `(transport, stype, sid)` recorder."""

    def __init__(self, stream_results):
        self._stream_results = stream_results
        self.calls = []

    def streams(self, transport, stype, sid):
        self.calls.append((transport, stype, sid))
        result = self._stream_results.get((transport, sid), [])
        if isinstance(result, Exception):
            raise result
        return result


_TWO_EPISODE_SERIES_META = {
    'id': 'tt1', 'name': 'Show',
    'videos': [
        {'id': 's1e1', 'season': 1, 'episode': 1, 'title': 'One'},
        {'id': 's1e2', 'season': 1, 'episode': 2, 'title': 'Two'},
    ],
}

_THREE_EPISODE_SERIES_META = {
    'id': 'tt1', 'name': 'Show',
    'videos': [
        {'id': 's1e1', 'season': 1, 'episode': 1, 'title': 'One'},
        {'id': 's1e2', 'season': 1, 'episode': 2, 'title': 'Two'},
        {'id': 's1e3', 'season': 1, 'episode': 3, 'title': 'Three'},
    ],
}


def _wire_series_addon(sw, results_by_sid, types=('series',)):
    """`_wire_data_layer()` with one supported addon (declaring `types`,
    'series' by default) whose `streams()` answers per-sid via
    `_PerEpisodeAddonClient`. Returns the client so a test can assert on
    `.calls`."""
    supported = {
        'transportUrl': 't1',
        'manifest': {'name': 'Addon', 'resources': ['stream'], 'types': list(types)},
    }
    client = _PerEpisodeAddonClient({('t1', sid): results for sid, results in results_by_sid.items()})
    _wire_data_layer(sw, _FakeStore(addons=[supported]), client)
    return client


class _OnceThenBackOutWindow:
    """Builds a `StreamsWindow` subclass whose `.start()` simulates "the
    user picked `played_pair` the first time, then backed out with no
    pick on every reopen" - the exact shape the binge round trip needs a
    picker double for, without a real onClick()/doModal() event loop."""

    @staticmethod
    def build(streamswindow_mod, played_pair, start_calls):
        class RecordingWindow(streamswindow_mod.StreamsWindow):
            def start(self, pairs, stype, sid, poster=None, heading='', art=None, meta=None, video_id=None):
                start_calls.append(sid)
                if len(start_calls) == 1:
                    self.played_pair = played_pair
                    return True
                return False
        return RecordingWindow


def test_open_streams_stopping_a_played_episode_does_not_auto_play_the_next_one(
    load_streamswindow, monkeypatch,
):
    """The bug this whole tuple-return refactor fixes: the user pressing
    stop on a played episode must reopen the picker, exactly like any
    other non-played-through end, and must NEVER auto-play the next
    episode - contrast with the natural-end test right below."""
    ctx = load_streamswindow()
    sw = ctx.streamswindow
    ctx.env.addon.settings['binge_countdown'] = 1
    played_stream = {'name': 'Episode One', 'behaviorHints': {}, 'url': 'https://a.example/e1.mp4'}
    next_stream = {'name': 'Episode Two', 'behaviorHints': {}, 'url': 'https://a.example/e2.mp4'}
    client = _wire_series_addon(sw, {'s1e1': [played_stream], 's1e2': [next_stream]})
    start_calls = []
    played_info = streaminfo.parse_stream(played_stream, addon_name='Addon')
    monkeypatch.setattr(
        sw, 'StreamsWindow', _OnceThenBackOutWindow.build(sw, (played_info, played_stream), start_calls),
    )
    play_direct_calls = []
    monkeypatch.setattr(
        ctx.player, 'play_direct',
        lambda stream, stype, sid, item_meta=None, on_ready=None, video_id=None: (
            play_direct_calls.append((stream, sid)) or True
        ),
    )
    # The user pressed stop rather than letting the episode play through -
    # the fake Player dispatches onPlayBackStopped() instead of
    # onPlayBackEnded() at the isPlaying() transition (call 2).
    ctx.env.player_end_reason = 'stopped'
    ctx.env.player_is_playing = lambda n: n == 1

    result = sw.open_streams('series', 's1e1', meta=_TWO_EPISODE_SERIES_META, video_id='s1e1')

    assert result is False
    assert start_calls == ['s1e1', 's1e1']  # reopened the SAME original picker, no auto-play
    assert play_direct_calls == []  # the next episode was never auto-played
    assert [call[2] for call in client.calls] == ['s1e1']  # next episode's streams never even fetched


def test_open_streams_binge_watch_auto_plays_the_next_episode_then_reopens_the_original_picker(
    load_streamswindow, monkeypatch,
):
    ctx = load_streamswindow()
    sw = ctx.streamswindow
    ctx.env.addon.settings['binge_countdown'] = 1  # keep the test fast: a single tick
    played_stream = {'name': 'Episode One', 'behaviorHints': {}, 'url': 'https://a.example/e1.mp4'}
    next_stream = {'name': 'Episode Two', 'behaviorHints': {}, 'url': 'https://a.example/e2.mp4'}
    client = _wire_series_addon(sw, {'s1e1': [played_stream], 's1e2': [next_stream]})
    start_calls = []
    played_info = streaminfo.parse_stream(played_stream, addon_name='Addon')
    monkeypatch.setattr(
        sw, 'StreamsWindow', _OnceThenBackOutWindow.build(sw, (played_info, played_stream), start_calls),
    )
    play_direct_calls = []
    monkeypatch.setattr(
        ctx.player, 'play_direct',
        lambda stream, stype, sid, item_meta=None, on_ready=None, video_id=None: (
            play_direct_calls.append((stream, stype, sid, item_meta, on_ready, video_id)) or True
        ),
    )
    # Reports "playing" once per _wait_for_playback_end() call (calls 1 and
    # 3 - the first check of each of the two invocations this flow makes),
    # then "stopped" right after - see the module's own such comments above.
    # `player_end_reason` stays at its default 'ended' - the episode plays
    # through to its natural end, which is exactly what must trigger the
    # auto-play-next below; contrast with the stopped test right above.
    ctx.env.player_end_reason = 'ended'
    ctx.env.player_is_playing = lambda n: n in (1, 3)

    result = sw.open_streams('series', 's1e1', meta=_TWO_EPISODE_SERIES_META, video_id='s1e1')

    assert result is False
    assert start_calls == ['s1e1', 's1e1']  # opened once (played), reopened once (chain fell back)
    assert len(play_direct_calls) == 1  # only the auto-played next episode goes through play_direct here
    auto_stream, auto_stype, auto_sid, auto_item_meta, auto_on_ready, auto_video_id = play_direct_calls[0]
    assert (auto_stream, auto_stype, auto_sid) == (next_stream, 'series', 's1e2')
    assert auto_item_meta['label'] == 'Show \u2013 S01E02 \u00b7 Two'
    assert auto_on_ready is sw.close_windows_for_playback
    assert auto_video_id == 's1e2'  # the next episode's own id, not the just-played one
    assert [call[2] for call in client.calls] == ['s1e1', 's1e2']  # fetched the played sid, then the next one


def test_open_streams_binge_watch_prefers_the_stream_matching_the_played_binge_group(
    load_streamswindow, monkeypatch,
):
    ctx = load_streamswindow()
    sw = ctx.streamswindow
    ctx.env.addon.settings['binge_countdown'] = 1
    played_stream = {'name': 'Episode One', 'behaviorHints': {'bingeGroup': 'grp'}, 'url': 'https://a.example/e1.mp4'}
    # The non-matching stream is listed FIRST - pick_binge_stream() must
    # still prefer the matching one, not just "whatever sorts/lists first".
    other_group_stream = {
        'name': 'Episode Two Other', 'behaviorHints': {'bingeGroup': 'other'}, 'url': 'https://a.example/e2-other.mp4',
    }
    matching_stream = {
        'name': 'Episode Two Match', 'behaviorHints': {'bingeGroup': 'grp'}, 'url': 'https://a.example/e2-match.mp4',
    }
    client = _wire_series_addon(sw, {'s1e1': [played_stream], 's1e2': [other_group_stream, matching_stream]})
    start_calls = []
    played_info = streaminfo.parse_stream(played_stream, addon_name='Addon')
    monkeypatch.setattr(
        sw, 'StreamsWindow', _OnceThenBackOutWindow.build(sw, (played_info, played_stream), start_calls),
    )
    play_direct_calls = []
    monkeypatch.setattr(
        ctx.player, 'play_direct',
        lambda stream, stype, sid, item_meta=None, on_ready=None, video_id=None: play_direct_calls.append(stream) or True,
    )
    ctx.env.player_is_playing = lambda n: n in (1, 3)

    result = sw.open_streams('series', 's1e1', meta=_TWO_EPISODE_SERIES_META, video_id='s1e1')

    assert result is False
    assert play_direct_calls == [matching_stream]
    assert client.calls  # sanity: the next episode really was fetched




def test_open_streams_binge_watch_chain_continues_through_a_third_episode(
    load_streamswindow, monkeypatch,
):
    ctx = load_streamswindow()
    sw = ctx.streamswindow
    ctx.env.addon.settings['binge_countdown'] = 1
    stream_e1 = {'name': 'E1', 'behaviorHints': {}, 'url': 'https://a.example/e1.mp4'}
    stream_e2 = {'name': 'E2', 'behaviorHints': {}, 'url': 'https://a.example/e2.mp4'}
    stream_e3 = {'name': 'E3', 'behaviorHints': {}, 'url': 'https://a.example/e3.mp4'}
    client = _wire_series_addon(sw, {'s1e1': [stream_e1], 's1e2': [stream_e2], 's1e3': [stream_e3]})
    start_calls = []
    played_info = streaminfo.parse_stream(stream_e1, addon_name='Addon')
    monkeypatch.setattr(
        sw, 'StreamsWindow', _OnceThenBackOutWindow.build(sw, (played_info, stream_e1), start_calls),
    )
    play_direct_calls = []
    monkeypatch.setattr(
        ctx.player, 'play_direct',
        lambda stream, stype, sid, item_meta=None, on_ready=None, video_id=None: play_direct_calls.append((sid, stream)) or True,
    )
    # "playing" once at the START of EVERY _wait_for_playback_end() call
    # this makes (episode 1, then auto-played 2, then auto-played 3) -
    # i.e. every odd-numbered isPlaying() call in sequence.
    ctx.env.player_is_playing = lambda n: n % 2 == 1

    result = sw.open_streams('series', 's1e1', meta=_THREE_EPISODE_SERIES_META, video_id='s1e1')

    assert result is False
    assert [sid for sid, _stream in play_direct_calls] == ['s1e2', 's1e3']
    assert start_calls == ['s1e1', 's1e1']  # the original picker only ever reopens once the WHOLE chain ends
    assert [call[2] for call in client.calls] == ['s1e1', 's1e2', 's1e3']


def test_open_streams_binge_watch_stopping_the_auto_played_episode_ends_the_chain(
    load_streamswindow, monkeypatch,
):
    """Stopping episode 2 (auto-played by the binge chain) must end the
    chain right there - never continue on into episode 3 - and fall back
    to reopening the ORIGINAL picker, same as any other "nothing left to
    binge into" case."""
    ctx = load_streamswindow()
    sw = ctx.streamswindow
    ctx.env.addon.settings['binge_countdown'] = 1
    stream_e1 = {'name': 'E1', 'behaviorHints': {}, 'url': 'https://a.example/e1.mp4'}
    stream_e2 = {'name': 'E2', 'behaviorHints': {}, 'url': 'https://a.example/e2.mp4'}
    stream_e3 = {'name': 'E3', 'behaviorHints': {}, 'url': 'https://a.example/e3.mp4'}
    client = _wire_series_addon(sw, {'s1e1': [stream_e1], 's1e2': [stream_e2], 's1e3': [stream_e3]})
    start_calls = []
    played_info = streaminfo.parse_stream(stream_e1, addon_name='Addon')
    monkeypatch.setattr(
        sw, 'StreamsWindow', _OnceThenBackOutWindow.build(sw, (played_info, stream_e1), start_calls),
    )
    play_direct_calls = []
    monkeypatch.setattr(
        ctx.player, 'play_direct',
        lambda stream, stype, sid, item_meta=None, on_ready=None, video_id=None: play_direct_calls.append((sid, stream)) or True,
    )

    def is_playing(n):
        if n == 3:
            # Episode 1 already ended naturally (the transition at call 2
            # read the default 'ended' reason, triggering the chain into
            # episode 2). Flip the reason here, BEFORE episode 2's own
            # isPlaying() transition at call 4, to simulate the user
            # stopping episode 2 instead of letting it end.
            ctx.env.player_end_reason = 'stopped'
        return n % 2 == 1

    ctx.env.player_is_playing = is_playing

    result = sw.open_streams('series', 's1e1', meta=_THREE_EPISODE_SERIES_META, video_id='s1e1')

    assert result is False
    assert [sid for sid, _stream in play_direct_calls] == ['s1e2']  # chain stopped after episode 2
    assert start_calls == ['s1e1', 's1e1']  # falls back to reopening the ORIGINAL picker
    assert [call[2] for call in client.calls] == ['s1e1', 's1e2']  # episode 3 was never even fetched


def test_open_streams_binge_watch_cancelling_the_countdown_reopens_the_original_picker(
    load_streamswindow, monkeypatch,
):
    ctx = load_streamswindow()
    sw = ctx.streamswindow
    ctx.env.addon.settings['binge_countdown'] = 5
    played_stream = {'name': 'E1', 'behaviorHints': {}, 'url': 'https://a.example/e1.mp4'}
    next_stream = {'name': 'E2', 'behaviorHints': {}, 'url': 'https://a.example/e2.mp4'}
    client = _wire_series_addon(sw, {'s1e1': [played_stream], 's1e2': [next_stream]})
    start_calls = []
    played_info = streaminfo.parse_stream(played_stream, addon_name='Addon')
    monkeypatch.setattr(
        sw, 'StreamsWindow', _OnceThenBackOutWindow.build(sw, (played_info, played_stream), start_calls),
    )
    play_direct_calls = []
    monkeypatch.setattr(
        ctx.player, 'play_direct',
        lambda stream, stype, sid, item_meta=None, on_ready=None, video_id=None: play_direct_calls.append(stream) or True,
    )
    ctx.env.player_is_playing = lambda n: n == 1
    # RivuletBusy.iscanceled() (the played/next episode fetches) and
    # RivuletCountdown's own window are unrelated dialogs now, so a
    # single shared counter can no longer drive both - only the
    # countdown's own window is marked cancelled here, and only once
    # RivuletCountdown.run() actually opens it (before its first tick),
    # matching test_dialogs.py's own "back action before the first tick"
    # convention for a dialog created internally.
    real_open_window = ctx.dialogs.open_window

    def _cancel_countdown_window(window_cls, xml_name, *args, **kwargs):
        window = real_open_window(window_cls, xml_name, *args, **kwargs)
        if window_cls is ctx.dialogs._CountdownWindow:
            window._canceled = True
        return window

    monkeypatch.setattr(ctx.dialogs, 'open_window', _cancel_countdown_window)

    result = sw.open_streams('series', 's1e1', meta=_TWO_EPISODE_SERIES_META, video_id='s1e1')

    assert result is False
    assert play_direct_calls == []  # never auto-played
    assert start_calls == ['s1e1', 's1e1']  # falls back to reopening the SAME original picker
    assert client.calls  # the next episode's streams were fetched before the countdown ran


def test_open_streams_binge_watch_monitor_abort_during_the_countdown_returns_false_without_reopening(
    load_streamswindow, monkeypatch,
):
    ctx = load_streamswindow()
    sw = ctx.streamswindow
    ctx.env.addon.settings['binge_countdown'] = 5
    played_stream = {'name': 'E1', 'behaviorHints': {}, 'url': 'https://a.example/e1.mp4'}
    next_stream = {'name': 'E2', 'behaviorHints': {}, 'url': 'https://a.example/e2.mp4'}
    client = _wire_series_addon(sw, {'s1e1': [played_stream], 's1e2': [next_stream]})
    start_calls = []
    played_info = streaminfo.parse_stream(played_stream, addon_name='Addon')
    monkeypatch.setattr(
        sw, 'StreamsWindow', _OnceThenBackOutWindow.build(sw, (played_info, played_stream), start_calls),
    )
    play_direct_calls = []
    monkeypatch.setattr(
        ctx.player, 'play_direct',
        lambda stream, stype, sid, item_meta=None, on_ready=None, video_id=None: play_direct_calls.append(stream) or True,
    )
    ctx.env.player_is_playing = lambda n: n == 1
    # Call #1 is the settle pause right after episode 1's own
    # _wait_for_playback_end() (which never touches the monitor itself -
    # see the plain reopen round trip's own such comments above); call #2
    # is unambiguously the countdown's own first tick.
    ctx.env.monitor_abort = lambda n: n == 2

    result = sw.open_streams('series', 's1e1', meta=_TWO_EPISODE_SERIES_META, video_id='s1e1')

    assert result is False
    assert play_direct_calls == []  # never auto-played
    assert start_calls == ['s1e1']  # no reopen at all - a shutdown, not a "not now"
    assert ctx.env.monitor_abort_calls == 2
    assert client.calls  # the next episode's streams really were fetched before the abort


def test_open_streams_binge_watch_no_next_episode_reopens_the_original_picker_without_fetching_anything_else(
    load_streamswindow, monkeypatch,
):
    ctx = load_streamswindow()
    sw = ctx.streamswindow
    played_stream = {'name': 'E1', 'behaviorHints': {}, 'url': 'https://a.example/e1.mp4'}
    only_episode_meta = {'id': 'tt1', 'name': 'Show', 'videos': [{'id': 's1e1', 'season': 1, 'episode': 1}]}
    client = _wire_series_addon(sw, {'s1e1': [played_stream]})
    start_calls = []
    played_info = streaminfo.parse_stream(played_stream, addon_name='Addon')
    monkeypatch.setattr(
        sw, 'StreamsWindow', _OnceThenBackOutWindow.build(sw, (played_info, played_stream), start_calls),
    )
    monkeypatch.setattr(
        ctx.player, 'play_direct',
        lambda stream, stype, sid, item_meta=None, on_ready=None, video_id=None: True,
    )
    ctx.env.player_is_playing = lambda n: n == 1

    result = sw.open_streams('series', 's1e1', meta=only_episode_meta, video_id='s1e1')

    assert result is False
    assert start_calls == ['s1e1', 's1e1']
    assert [call[2] for call in client.calls] == ['s1e1']  # never fetched anything beyond the played sid


def test_open_streams_binge_watch_disabled_setting_reopens_the_original_picker_without_fetching_anything_else(
    load_streamswindow, monkeypatch,
):
    ctx = load_streamswindow()
    sw = ctx.streamswindow
    ctx.env.addon.settings['binge_enable'] = 'false'
    played_stream = {'name': 'E1', 'behaviorHints': {}, 'url': 'https://a.example/e1.mp4'}
    client = _wire_series_addon(sw, {'s1e1': [played_stream], 's1e2': [{'url': 'https://a.example/e2.mp4'}]})
    start_calls = []
    played_info = streaminfo.parse_stream(played_stream, addon_name='Addon')
    monkeypatch.setattr(
        sw, 'StreamsWindow', _OnceThenBackOutWindow.build(sw, (played_info, played_stream), start_calls),
    )
    monkeypatch.setattr(
        ctx.player, 'play_direct',
        lambda stream, stype, sid, item_meta=None, on_ready=None, video_id=None: True,
    )
    ctx.env.player_is_playing = lambda n: n == 1

    result = sw.open_streams('series', 's1e1', meta=_TWO_EPISODE_SERIES_META, video_id='s1e1')

    assert result is False
    assert start_calls == ['s1e1', 's1e1']
    assert [call[2] for call in client.calls] == ['s1e1']  # the setting gate never even checked for a next episode


def test_open_streams_binge_watch_no_fetchable_stream_for_the_next_episode_reopens_the_original_picker(
    load_streamswindow, monkeypatch,
):
    ctx = load_streamswindow()
    sw = ctx.streamswindow
    played_stream = {'name': 'E1', 'behaviorHints': {}, 'url': 'https://a.example/e1.mp4'}
    client = _wire_series_addon(sw, {'s1e1': [played_stream], 's1e2': []})  # next episode: nothing to play
    start_calls = []
    played_info = streaminfo.parse_stream(played_stream, addon_name='Addon')
    monkeypatch.setattr(
        sw, 'StreamsWindow', _OnceThenBackOutWindow.build(sw, (played_info, played_stream), start_calls),
    )
    monkeypatch.setattr(
        ctx.player, 'play_direct',
        lambda stream, stype, sid, item_meta=None, on_ready=None, video_id=None: True,
    )
    ctx.env.player_is_playing = lambda n: n == 1

    result = sw.open_streams('series', 's1e1', meta=_TWO_EPISODE_SERIES_META, video_id='s1e1')

    assert result is False
    assert start_calls == ['s1e1', 's1e1']
    assert [call[2] for call in client.calls] == ['s1e1', 's1e2']  # the next episode WAS looked up, just empty


def test_open_streams_binge_watch_a_movie_without_video_id_never_triggers_it(load_streamswindow, monkeypatch):
    ctx = load_streamswindow()
    sw = ctx.streamswindow
    played_stream = {'name': 'Movie', 'behaviorHints': {}, 'url': 'https://a.example/movie.mp4'}
    client = _wire_series_addon(sw, {'tt-movie': [played_stream]}, types=('movie',))
    start_calls = []
    played_info = streaminfo.parse_stream(played_stream, addon_name='Addon')
    monkeypatch.setattr(
        sw, 'StreamsWindow', _OnceThenBackOutWindow.build(sw, (played_info, played_stream), start_calls),
    )
    monkeypatch.setattr(
        ctx.player, 'play_direct',
        lambda stream, stype, sid, item_meta=None, on_ready=None, video_id=None: True,
    )
    ctx.env.player_is_playing = lambda n: n == 1

    # No video_id kwarg at all - a movie/context-free call, exactly like every
    # pre-existing open_streams() call site keeps making.
    result = sw.open_streams('movie', 'tt-movie')

    assert result is False
    assert start_calls == ['tt-movie', 'tt-movie']
    assert [call[2] for call in client.calls] == ['tt-movie']  # no second fetch was ever attempted
