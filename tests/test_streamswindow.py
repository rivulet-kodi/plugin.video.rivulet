"""Tests for lib.ui.streamswindow: StreamsWindow and open_streams(),
Rivulet's custom replacement for the classical `streams()` directory,
exercised against the shared fake xbmc/xbmcgui stubs in tests/kodistubs (no
real Kodi runtime, no network).

Unlike lib.ui.detailwindow/lib.ui.catalogpicker, lib.ui.streamswindow imports
its data layer (lib.store.Store, lib.stremio.addons.AddonClient/AddonError/
addon_supports) at MODULE scope, so - mirroring tests/test_searchwindow.py's
`_wire_data_layer` pattern for lib.ui.searchwindow - the data layer is faked
by assigning directly to the names lib.ui.streamswindow itself imported
(`streamswindow.Store`, `streamswindow.AddonClient`) rather than via
monkeypatching lib.store/lib.stremio.addons. `addon_supports` and
`streaminfo.sort_streams` are exercised for real (both are pure, no xbmc
dependency).

`StreamsWindow.onClick()` lazily `from lib.ui.player import play_direct` at
call time, so load_streamswindow reloads lib.ui.compat/lib.ui.uicommon/
lib.ui.player/lib.ui.streamswindow fresh together to get a handle
(`ctx.player`) this file monkeypatches `play_direct` on directly.

StreamsWindow.onInit()/onClick()/onAction()/start() are called directly
here, never through a real modal event loop, exactly like
tests/test_catalogpicker.py drives CatalogPickerWindow: the fake
WindowXMLDialog.doModal() is a no-op counter, and getControl()/setFocusId()
are plain in-memory fakes. StreamsWindow.xml's actual skin rendering is
Kodi-skin-engine-only and is NOT, and cannot be, exercised by this suite.
"""
import contextlib

import pytest

from lib.stremio import streaminfo
from lib.stremio.addons import AddonError
from tests.kodistubs import install_kodi_stubs

_RELOAD_MODULE_NAMES = ('lib.ui.compat', 'lib.ui.uicommon', 'lib.ui.player', 'lib.ui.streamswindow')


class _FakeStore:
    """Fake `lib.store.Store`: only `get_addons()` matters to open_streams()."""

    def __init__(self, addons=None):
        self._addons = addons or []

    def get_addons(self):
        return self._addons


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
    streamswindow_mod.Store = lambda *a, **k: store
    streamswindow_mod.AddonClient = lambda *a, **k: client


def _make_window(streamswindow_mod):
    return streamswindow_mod.StreamsWindow('StreamsWindow.xml', '/addon/path', 'Default', '720p')


# ---------------------------------------------------------------------------
# StreamsWindow.onInit() - label building + background fallback
# ---------------------------------------------------------------------------


def test_oninit_builds_label_from_format_label_for_full_metadata(load_streamswindow):
    ctx = load_streamswindow()
    win = _make_window(ctx.streamswindow)
    info = {
        'resolution': '1080p', 'source': 'WEB-DL', 'codec': 'x265', 'hdr': ['HDR10'],
        'size_text': '2.1 GB', 'seeders': 42, 'addon': 'AddonA',
    }
    win.pairs = [(info, {'url': 'https://a.example/a.mp4'})]

    win.onInit()

    items = win.getControl(ctx.streamswindow.LIST).items
    assert items[0].getLabel() == (
        '[COLOR lime]1080p[/COLOR] [B]WEB-DL[/B] x265 HDR10 \u00b7 2.1 GB \u00b7 S42 \u00b7 '
        '[COLOR gray]AddonA[/COLOR]'
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

    assert win.getControl(ctx.streamswindow.LIST).items[0].getLabel() == 'Some Raw Title  Line2'


def test_oninit_falls_back_to_question_mark_when_no_label_material_is_available(load_streamswindow):
    ctx = load_streamswindow()
    win = _make_window(ctx.streamswindow)
    win.pairs = [({}, {'url': 'https://a.example/a.mp4'})]

    win.onInit()

    assert win.getControl(ctx.streamswindow.LIST).items[0].getLabel() == '?'


def test_oninit_sets_position_property_in_pair_order_and_focuses_the_list(load_streamswindow):
    ctx = load_streamswindow()
    win = _make_window(ctx.streamswindow)
    win.pairs = [({'raw': 'A'}, {}), ({'raw': 'B'}, {})]

    win.onInit()

    items = win.getControl(ctx.streamswindow.LIST).items
    assert [item.getProperty('position') for item in items] == ['0', '1']
    assert win.getFocusId() == ctx.streamswindow.LIST


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

    def fake_play_direct(stream, stype, sid):
        captured['args'] = (stream, stype, sid)
        return True

    monkeypatch.setattr(ctx.player, 'play_direct', fake_play_direct)

    win.onClick(ctx.streamswindow.LIST)

    assert captured['args'] == (stream_b, 'movie', 'tt1')
    assert win.played is True
    assert win.closed is True


def test_onclick_stays_open_when_play_direct_returns_false(load_streamswindow, monkeypatch):
    ctx = load_streamswindow()
    win = _make_window(ctx.streamswindow)
    win.pairs = [({'raw': 'A'}, {'url': 'https://a.example/a.mp4'})]
    win.onInit()
    monkeypatch.setattr(ctx.player, 'play_direct', lambda stream, stype, sid: False)

    win.onClick(ctx.streamswindow.LIST)

    assert win.played is False
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
    monkeypatch.setattr(ctx.player, 'play_direct', lambda stream, stype, sid: True)

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
        def start(self, pairs, stype, sid, poster=None):
            captured['args'] = (pairs, stype, sid, poster)
            return True

    monkeypatch.setattr(sw, 'StreamsWindow', RecordingWindow)

    result = sw.open_streams('movie', 'tt1', poster='https://x/poster.jpg')

    assert [call[0] for call in client.calls] == ['t-supported']
    pairs, stype, sid, poster = captured['args']
    assert (stype, sid, poster) == ('movie', 'tt1', 'https://x/poster.jpg')
    assert [s for _info, s in pairs] == [stream]
    assert result is True


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

        def start(self, pairs, stype, sid, poster=None):
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
    sw = ctx.streamswindow
    failing = {
        'transportUrl': 't-fail',
        'manifest': {'name': 'Failing', 'resources': ['stream'], 'types': ['movie']},
    }
    working = {
        'transportUrl': 't-ok',
        'manifest': {'name': 'Working', 'resources': ['stream'], 'types': ['movie']},
    }
    ok_stream = {'url': 'https://a.example/a.mp4'}
    client = _FakeAddonClient({'t-fail': AddonError('upstream down'), 't-ok': [ok_stream]})
    _wire_data_layer(sw, _FakeStore(addons=[failing, working]), client)
    captured = {}

    class RecordingWindow(sw.StreamsWindow):
        def start(self, pairs, stype, sid, poster=None):
            captured['pairs'] = pairs
            return True

    monkeypatch.setattr(sw, 'StreamsWindow', RecordingWindow)

    result = sw.open_streams('movie', 'tt1')

    assert result is True
    assert [call[0] for call in client.calls] == ['t-fail', 't-ok']
    assert [s for _info, s in captured['pairs']] == [ok_stream]


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
        def start(self, pairs, stype, sid, poster=None):
            captured['pairs'] = pairs
            return True

    monkeypatch.setattr(sw, 'StreamsWindow', RecordingWindow)

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
    """Builds a zero-arg closure for `ctx.env.cancel` that reports
    cancelled (True) starting from its (n+1)th call onward. Mirrors
    DialogProgress.iscanceled()'s no-arg call convention (unlike
    Monitor.waitForAbort()'s 1-based-count-arg convention)."""
    state = {'calls': 0}

    def _check():
        state['calls'] += 1
        return state['calls'] > n
    return _check


def test_open_streams_busy_dialog_reports_progress_and_skips_unsupported_addons(
    load_streamswindow, monkeypatch,
):
    ctx = load_streamswindow()
    sw = ctx.streamswindow
    alpha = {
        'transportUrl': 't-alpha',
        'manifest': {'name': 'Alpha', 'resources': ['stream'], 'types': ['movie']},
    }
    beta = {
        'transportUrl': 't-beta',
        'manifest': {'name': 'Beta', 'resources': ['stream'], 'types': ['movie']},
    }
    unsupported = {
        'transportUrl': 't-unsupported',
        # no 'stream' resource -> excluded before total_addons is even computed.
        'manifest': {'name': 'Unsupported', 'resources': ['catalog'], 'types': ['movie']},
    }
    alpha_stream = {'url': 'https://a.example/a.mp4'}
    beta_stream = {'url': 'https://b.example/b.mp4'}
    client = _FakeAddonClient({'t-alpha': [alpha_stream], 't-beta': [beta_stream]})
    _wire_data_layer(sw, _FakeStore(addons=[alpha, beta, unsupported]), client)
    captured = {}

    class RecordingWindow(sw.StreamsWindow):
        def start(self, pairs, stype, sid, poster=None):
            captured['pairs'] = pairs
            return True

    monkeypatch.setattr(sw, 'StreamsWindow', RecordingWindow)

    result = sw.open_streams('movie', 'tt1')

    assert result is True
    assert [call[0] for call in client.calls] == ['t-alpha', 't-beta']
    assert [s for _info, s in captured['pairs']] == [alpha_stream, beta_stream]
    assert ctx.env.dialog_created == [('STR30033', '')]
    # total_addons is 2 (the unsupported addon never counts toward the
    # denominator), so percent is index * 100 / 2 -> 0, then 50; the
    # unsupported addon produces no 'Checking Unsupported...' entry at all.
    assert ctx.env.dialog_updates == [
        (0, ''),  # busy_dialog's own initial update(0, message) on entry
        (0, 'Checking Alpha...'),
        (50, 'Checking Beta...'),
    ]
    assert ctx.env.dialog_closed_count == 1


def test_open_streams_cancelled_mid_loop_keeps_partial_results_and_closes_dialog(
    load_streamswindow, monkeypatch,
):
    ctx = load_streamswindow()
    sw = ctx.streamswindow
    alpha = {
        'transportUrl': 't-alpha',
        'manifest': {'name': 'Alpha', 'resources': ['stream'], 'types': ['movie']},
    }
    beta = {
        'transportUrl': 't-beta',
        'manifest': {'name': 'Beta', 'resources': ['stream'], 'types': ['movie']},
    }
    alpha_stream = {'url': 'https://a.example/a.mp4'}
    beta_stream = {'url': 'https://b.example/b.mp4'}
    client = _FakeAddonClient({'t-alpha': [alpha_stream], 't-beta': [beta_stream]})
    _wire_data_layer(sw, _FakeStore(addons=[alpha, beta]), client)
    ctx.env.cancel = _cancel_after(1)  # index 0 -> not cancelled; index 1 -> cancelled, breaks
    captured = {}

    class RecordingWindow(sw.StreamsWindow):
        def start(self, pairs, stype, sid, poster=None):
            captured['pairs'] = pairs
            return True

    monkeypatch.setattr(sw, 'StreamsWindow', RecordingWindow)

    result = sw.open_streams('movie', 'tt1')

    assert result is True
    assert [call[0] for call in client.calls] == ['t-alpha']  # beta never queried
    assert [s for _info, s in captured['pairs']] == [alpha_stream]  # partial results kept
    assert ctx.env.dialog_closed_count == 1


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
    ctx.env.cancel = True  # already cancelled before the loop ever starts

    def _unexpected(*a, **k):
        raise AssertionError('StreamsWindow must never be constructed on an empty aggregate')

    monkeypatch.setattr(sw, 'StreamsWindow', _unexpected)

    result = sw.open_streams('movie', 'tt1')

    assert client.calls == []  # cancelled before the first addon was ever queried
    assert result is False
    assert ctx.env.notifications == [('Rivulet', 'STR30030', 'info', 4000)]
    assert ctx.env.dialog_closed_count == 1
