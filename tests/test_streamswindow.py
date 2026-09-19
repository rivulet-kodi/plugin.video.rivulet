"""Tests for lib.ui.streamswindow: StreamsWindow's onInit()/onAction()/
start() rendering contract - label/background/info-panel building in
onInit(), the playback_* stream-filter view applied at render time
(_stream_filter_view()/_read_stream_filters()/_rebuild_list()), the
append-only fast path vs full-rebuild fallback in _apply_pending(), and
add_pairs()'s thread-safety (GUI controls only ever touched on drain).
Exercised against the shared fake xbmc/xbmcgui stubs in tests/kodistubs
(no real Kodi runtime, no network) - see test_streamswindow_playback.py
for onClick()/binge/reopen coverage and test_streamswindow_fetch.py for
open_streams()'s addon-fetch/failure-aggregation coverage; all three
files share the fixtures/fakes duplicated here (`load_streamswindow`,
`_FakeStore`, `_FakeAddonClient`, `_wire_data_layer`, `_make_window`).
"""
import contextlib
import threading

import pytest

from lib.stremio import streaminfo
from tests.conftest import make_window
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


class _SpoofedCategoryError(Exception):
    """A non-`AddonError` exception that happens to carry a `category`
    attribute of its own - standing in for some future/third-party
    exception type outside `AddonError`'s safe-by-construction contract
    (see `_safe_failure_reason()`'s docstring in lib/ui/streamswindow.py).
    Used below to prove that contract is never extended to an arbitrary
    exception via `hasattr(exc, 'category')` duck-typing."""

    def __init__(self, message, category):
        super().__init__(message)
        self.category = category


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
    return make_window(streamswindow_mod.StreamsWindow)


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


def test_oninit_escapes_addon_supplied_markup_in_provider_property_via_line_and_heading(load_streamswindow):
    """Regression: an addon's own manifest/catalog-controlled name or
    title reaching a rendered label/property unescaped lets it inject
    Kodi skin markup (`[COLOR ...]`) or evaluate a live info label
    (`$INFO[...]`) inside Rivulet's own windows - see
    `lib.ui.uicommon.escape_label()`. Exercises three sinks at once:
    the single-provider dedupe's discrete `provider` property, its
    INFO_PANEL 'via <addon>' line, and the window HEADING label."""
    ctx = load_streamswindow()
    win = _make_window(ctx.streamswindow)
    malicious_addon = '[COLOR red]evil[/COLOR]$INFO[System.ProfileName]'
    win.pairs = [({'addon': malicious_addon}, {})]
    win.heading = malicious_addon

    win.onInit()

    item = win.getControl(ctx.streamswindow.LIST).items[0]
    provider = item.getProperty('provider')
    info_panel = win.getControl(ctx.streamswindow.INFO_PANEL).text
    heading = win.getControl(ctx.streamswindow.HEADING).label
    for rendered in (provider, info_panel, heading):
        assert '[COLOR' not in rendered
        assert '$INFO[' not in rendered
    assert provider == 'evilINFO[System.ProfileName]'
    assert info_panel == 'via evilINFO[System.ProfileName]'


# ---------------------------------------------------------------------------
# StreamsWindow: 'playback_*' stream filtering (_stream_filter_view()/
# _rebuild_list()) - filtering is applied at RENDER time against
# `self.pairs`, never mutating it, so `_apply_pending()`'s focus-by-
# identity restore and position-property lookups keep working unchanged
# (see `_rebuild_list()`'s own docstring).
# ---------------------------------------------------------------------------


def test_rebuild_list_hides_stream_excluded_by_max_size_and_shrinks_counts(load_streamswindow):
    ctx = load_streamswindow()
    win = _make_window(ctx.streamswindow)
    ctx.env.addon.settings['playback_max_size_gb'] = '1'  # 1 GiB cap
    # Single addon throughout so line1's single-provider dedupe stays
    # stable (include_addon=False) and each row falls back to its plain
    # 'raw' text - see _rebuild_list()'s own single_provider comment.
    kept = {'addon': 'AddonA', 'raw': 'Kept', 'size_bytes': 500 * 1024 * 1024, 'cached': True}
    big = {'addon': 'AddonA', 'raw': 'Big', 'size_bytes': 5 * 1024 ** 3, 'cached': True}
    win.pairs = [(kept, {}), (big, {})]

    win.onInit()

    items = win.getControl(ctx.streamswindow.LIST).items
    assert [item.getLabel() for item in items] == ['Kept']
    # position stays a self.pairs index, not a display-list index, so
    # _apply_pending()'s focus-by-identity restore keeps working.
    assert items[0].getProperty('position') == '0'
    assert win.getControl(ctx.streamswindow.SOURCES_COUNT).label == '1 SOURCE'
    assert win.getControl(ctx.streamswindow.ADDONS_COUNT).label == '1 ADDON'
    assert win.getControl(ctx.streamswindow.INFO_PANEL).text == 'via AddonA\n1 source hidden by filters'


def test_rebuild_list_hidden_count_is_plural_for_more_than_one(load_streamswindow):
    ctx = load_streamswindow()
    win = _make_window(ctx.streamswindow)
    ctx.env.addon.settings['playback_max_size_gb'] = '1'
    kept = {'addon': 'AddonA', 'raw': 'Kept', 'size_bytes': 500 * 1024 * 1024}
    big_a = {'addon': 'AddonA', 'raw': 'BigA', 'size_bytes': 5 * 1024 ** 3}
    big_b = {'addon': 'AddonA', 'raw': 'BigB', 'size_bytes': 5 * 1024 ** 3}
    win.pairs = [(kept, {}), (big_a, {}), (big_b, {})]

    win.onInit()

    assert win.getControl(ctx.streamswindow.INFO_PANEL).text == 'via AddonA\n2 sources hidden by filters'


def test_rebuild_list_falls_back_to_showing_everything_when_filters_match_nothing(load_streamswindow):
    # A misconfigured filter (here: minimum quality above anything an
    # addon actually found) must never look identical to open_streams()'s
    # own "no sources found" empty-result path - see _stream_filter_view()'s
    # docstring. Both entries share one addon and both carry a real
    # 'resolution' (needed to trigger the min-quality filter), so
    # format_label() renders them identically - the assertions below
    # check row COUNT/position, not label text, to stay independent of
    # that.
    ctx = load_streamswindow()
    win = _make_window(ctx.streamswindow)
    ctx.env.addon.settings['playback_min_quality'] = '2160p'
    pairs = [
        ({'addon': 'AddonA', 'raw': 'A', 'resolution': '1080p'}, {}),
        ({'addon': 'AddonA', 'raw': 'B', 'resolution': '1080p'}, {}),
    ]
    win.pairs = list(pairs)

    win.onInit()

    items = win.getControl(ctx.streamswindow.LIST).items
    assert [item.getProperty('position') for item in items] == ['0', '1']
    assert win.getControl(ctx.streamswindow.SOURCES_COUNT).label == '2 SOURCES'
    notice = 'via AddonA\nSTR%d' % ctx.streamswindow._FILTERS_MATCHED_NOTHING_STRING_ID
    assert win.getControl(ctx.streamswindow.INFO_PANEL).text == notice


def test_rebuild_list_no_filters_configured_hides_nothing(load_streamswindow):
    ctx = load_streamswindow()
    win = _make_window(ctx.streamswindow)
    win.pairs = [({'addon': 'AddonA', 'raw': 'A'}, {}), ({'addon': 'AddonA', 'raw': 'B'}, {})]

    win.onInit()

    items = win.getControl(ctx.streamswindow.LIST).items
    assert [item.getLabel() for item in items] == ['A', 'B']
    assert win.getControl(ctx.streamswindow.INFO_PANEL).text == 'via AddonA'


def test_rebuild_list_skips_a_filtered_entry_without_disturbing_surrounding_order(load_streamswindow):
    # filter_streams() drops the excluded pair in place - the two
    # survivors either side of it must keep their RELATIVE order, exactly
    # as if the excluded pair had never been in self.pairs at all.
    ctx = load_streamswindow()
    win = _make_window(ctx.streamswindow)
    ctx.env.addon.settings['playback_max_size_gb'] = '1'
    first = {'addon': 'AddonA', 'raw': 'First', 'size_bytes': 500 * 1024 * 1024}
    excluded = {'addon': 'AddonA', 'raw': 'Excluded', 'size_bytes': 5 * 1024 ** 3}
    last = {'addon': 'AddonA', 'raw': 'Last', 'size_bytes': 500 * 1024 * 1024}
    win.pairs = [(first, {}), (excluded, {}), (last, {})]

    win.onInit()

    items = win.getControl(ctx.streamswindow.LIST).items
    assert [item.getLabel() for item in items] == ['First', 'Last']
    # positions are still real self.pairs indices (0 and 2) - the skipped
    # middle index is never renumbered away.
    assert [item.getProperty('position') for item in items] == ['0', '2']


def test_read_stream_filters_maps_quality_setting_to_height_and_gb_to_bytes(load_streamswindow):
    ctx = load_streamswindow()
    ctx.env.addon.settings['playback_min_quality'] = '720p'
    ctx.env.addon.settings['playback_max_size_gb'] = '2'
    ctx.env.addon.settings['playback_exclude_cam'] = 'true'
    ctx.env.addon.settings['playback_cached_only'] = 'true'

    filters = ctx.streamswindow._read_stream_filters()

    assert filters == {
        'min_height': 720,
        'max_size_bytes': 2 * 1024 ** 3,
        'exclude_cam': True,
        'cached_only': True,
    }


def test_read_stream_filters_defaults_to_no_filtering(load_streamswindow):
    ctx = load_streamswindow()

    filters = ctx.streamswindow._read_stream_filters()

    assert filters == {
        'min_height': 0,
        'max_size_bytes': 0,
        'exclude_cam': False,
        'cached_only': False,
    }


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


def test_apply_pending_batch_flipping_matched_nothing_to_false_drops_stale_fallback_row(
    load_streamswindow,
):
    """Regression: `_stream_filter_view()`'s "filters matched nothing"
    fallback shows every pair, UNFILTERED, when the active filter would
    hide everything (see that method's own docstring). If a later batch
    then gives the filter something real to keep, `single_provider`
    stays put and the old raw prefix is still an identity prefix of the
    re-sorted `self.pairs` - the two checks `_append_prefix_length()`
    used to run - but the fallback row the OLD prefix was showing is
    now something the SAME active filter must hide. The append-only
    fast path must not leave it on screen just because nothing "moved"."""
    ctx = load_streamswindow()
    sw = ctx.streamswindow
    win = _make_window(sw)
    ctx.env.addon.settings['playback_exclude_cam'] = 'true'
    cam_info = {'addon': 'A', 'raw': 'Movie.2020.CAMRip.x264'}
    cam_stream = {'url': 'https://a.example/cam.mp4'}
    win.start([(cam_info, cam_stream)], 'movie', 'tt1')
    win.onInit()

    # Fallback kicked in: the lone pair is a CAM release the active
    # filter would hide, so _stream_filter_view() shows it anyway.
    assert [i.getLabel() for i in win.getControl(sw.LIST).items] == ['Movie.2020.CAMRip.x264']

    clean_info = {'addon': 'A', 'raw': 'Movie.2020.WEB-DL.x264'}
    clean_stream = {'url': 'https://a.example/clean.mp4'}
    win.add_pairs([(clean_info, clean_stream)])
    win.onAction(_FakeBackAction(-1))

    # self.pairs itself is untouched by filtering - both raw pairs stay.
    assert [s for _i, s in win.pairs] == [cam_stream, clean_stream]
    # LIST must show exactly the real filtered subset now - the stale
    # CAM fallback row must be gone, not merely appended past.
    items = win.getControl(sw.LIST).items
    assert [i.getLabel() for i in items] == ['Movie.2020.WEB-DL.x264']


def test_apply_pending_full_rebuild_selects_visible_index_not_raw_index_when_a_filtered_pair_precedes_focus(
    load_streamswindow, monkeypatch,
):
    """Regression: `_apply_pending()`'s full-rebuild fallback used to
    re-find the focused pair's RAW `self.pairs` index and hand it
    straight to `selectItem()`, even though LIST only ever holds
    `display_pairs` (the FILTERED view - see `_stream_filter_view()`).
    A newly-arrived cam release sorted ahead of the already-focused row
    is filtered out, so the focused row's raw index (1) and its actual
    on-screen row (0, the only visible item) diverge - selectItem() must
    get the latter."""
    ctx = load_streamswindow()
    sw = ctx.streamswindow
    win = _make_window(sw)
    target_info = {'addon': 'A', 'raw': 'Target.WEB-DL.x264'}
    target_stream = {'url': 'https://a.example/target.mp4'}
    win.start([(target_info, target_stream)], 'movie', 'tt1')
    win.onInit()
    win.getControl(sw.LIST).selected_index = 0  # focus the only (visible) row

    ctx.env.addon.settings['playback_exclude_cam'] = 'true'
    cam_info = {'addon': 'A', 'raw': 'Movie.2020.CAMRip.x264'}
    cam_stream = {'url': 'https://a.example/cam.mp4'}

    def fake_sort_streams(pairs, key='quality'):
        # Sorts the newly-arrived (soon-to-be-filtered) cam pair AHEAD
        # of the already-focused row, so the old rendered prefix is no
        # longer a prefix of the re-sorted self.pairs - forcing the
        # full-rebuild fallback with a FILTERED pair now preceding the
        # focused one at the raw self.pairs level.
        return sorted(pairs, key=lambda pair: pair[1] is target_stream)

    monkeypatch.setattr(streaminfo, 'sort_streams', fake_sort_streams)
    win.add_pairs([(cam_info, cam_stream)])
    win.onAction(_FakeBackAction(-1))

    assert [s for _i, s in win.pairs] == [cam_stream, target_stream]  # cam is raw index 0, target is raw index 1
    control = win.getControl(sw.LIST)
    assert [item.getLabel() for item in control.items] == ['Target.WEB-DL.x264']  # cam row hidden by the filter
    assert control.selected_index == 0  # the pair's VISIBLE index, not its raw index (1)
    focused = control.getSelectedItem()
    assert win.pairs[int(focused.getProperty('position'))][1] is target_stream


def test_apply_pending_full_rebuild_clamps_selection_when_the_focused_pair_itself_is_filtered_out(
    load_streamswindow, monkeypatch,
):
    """Regression: when the row the user is ON gets hidden by a filter
    change (e.g. the user opened Settings and flipped 'exclude CAM'
    while a background batch was still landing), the focused pair is
    simply gone from the new visible list - there is no "correct" row
    to land on. `_apply_pending()` falls back to the pair's old NUMERIC
    row position, clamped to the NEW (shorter) VISIBLE list, rather
    than the old buggy clamp against `len(self.pairs)` - which counts
    every hidden row LIST never had, so it does not shrink at all here
    (a second, also-filtered cam pair grows `self.pairs` right alongside
    the visible list shrinking) and would select a row past the end of
    the actual on-screen list."""
    ctx = load_streamswindow()
    sw = ctx.streamswindow
    win = _make_window(sw)
    other1_info = {'addon': 'A', 'raw': 'Other1.WEB-DL.x264'}
    other1_stream = {'url': 'https://a.example/other1.mp4'}
    other2_info = {'addon': 'A', 'raw': 'Other2.WEB-DL.x264'}
    other2_stream = {'url': 'https://a.example/other2.mp4'}
    cam_info = {'addon': 'A', 'raw': 'Movie.2020.CAMRip.x264'}
    cam_stream = {'url': 'https://a.example/cam.mp4'}
    win.start(
        [(other1_info, other1_stream), (other2_info, other2_stream), (cam_info, cam_stream)], 'movie', 'tt1',
    )
    win.onInit()
    assert [item.getLabel() for item in win.getControl(sw.LIST).items] == [
        'Other1.WEB-DL.x264', 'Other2.WEB-DL.x264', 'Movie.2020.CAMRip.x264',
    ]
    win.getControl(sw.LIST).selected_index = 2  # focus the cam row (last) - no filter active yet

    # Simulate the user opening Settings and enabling exclude-CAM while
    # this picker stays open, then a second cam batch landing.
    ctx.env.addon.settings['playback_exclude_cam'] = 'true'
    new_cam_info = {'addon': 'A', 'raw': 'Movie.2021.CAMRip.x264'}
    new_cam_stream = {'url': 'https://a.example/new_cam.mp4'}

    def fake_sort_streams(pairs, key='quality'):
        return pairs  # keep insertion order - the new pair lands strictly at the end

    monkeypatch.setattr(streaminfo, 'sort_streams', fake_sort_streams)
    win.add_pairs([(new_cam_info, new_cam_stream)])
    win.onAction(_FakeBackAction(-1))

    assert [s for _i, s in win.pairs] == [
        other1_stream, other2_stream, cam_stream, new_cam_stream,
    ]  # 4 raw pairs - self.pairs kept growing
    control = win.getControl(sw.LIST)
    # Both cam rows (the previously-focused one included) are now hidden -
    # the visible list SHRANK to 2 while self.pairs grew to 4.
    assert [item.getLabel() for item in control.items] == ['Other1.WEB-DL.x264', 'Other2.WEB-DL.x264']
    # Clamped to the last valid visible row (old numeric position 2,
    # new visible length 2), not left at the old raw-length clamp of 2.
    assert control.selected_index == 1
    assert control.getSelectedItem() is not None  # would IndexError against the old len(self.pairs) clamp


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
