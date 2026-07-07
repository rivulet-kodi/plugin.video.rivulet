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


def test_oninit_builds_two_line_row_stripping_addon_from_line_one_into_label2(load_streamswindow):
    ctx = load_streamswindow()
    win = _make_window(ctx.streamswindow)
    info = {
        'resolution': '1080p', 'source': 'WEB-DL', 'codec': 'x265', 'hdr': ['HDR10'],
        'size_text': '2.1 GB', 'seeders': 42, 'addon': 'AddonA',
    }
    win.pairs = [(info, {'url': 'https://a.example/a.mp4'})]

    win.onInit()

    item = win.getControl(ctx.streamswindow.LIST).items[0]
    assert item.getLabel() == (
        '[COLOR lime]1080p[/COLOR] [B]WEB-DL[/B] x265 HDR10 \u00b7 2.1 GB \u00b7 S42'
    )
    assert item.label2 == 'AddonA'


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


def test_oninit_addon_only_info_falls_back_to_question_mark_on_line1_but_keeps_addon_on_line2(load_streamswindow):
    ctx = load_streamswindow()
    win = _make_window(ctx.streamswindow)
    # No resolution/source/codec/hdr/size_text/seeders -> format_label(...,
    # include_addon=False) returns '' regardless of 'addon' being set, so
    # line 1 falls back to '?' -- but the addon name still surfaces, on
    # line 2, where the two-line row now dedicates it.
    win.pairs = [({'addon': 'AddonA'}, {'url': 'https://a.example/a.mp4'})]

    win.onInit()

    item = win.getControl(ctx.streamswindow.LIST).items[0]
    assert item.getLabel() == '?'
    assert item.label2 == 'AddonA'


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


def test_start_forwards_heading_and_art_onto_the_window(load_streamswindow):
    ctx = load_streamswindow()
    win = _make_window(ctx.streamswindow)
    pairs = [({'raw': 'A'}, {'url': 'https://a.example/a.mp4'})]

    win.start(pairs, 'movie', 'tt1', heading='My Title', art={'poster': 'P', 'fanart': 'F'})

    assert win.heading == 'My Title'
    assert win.art == {'poster': 'P', 'fanart': 'F'}


def test_start_defaults_heading_and_art_when_omitted(load_streamswindow):
    ctx = load_streamswindow()
    win = _make_window(ctx.streamswindow)
    pairs = [({'raw': 'A'}, {'url': 'https://a.example/a.mp4'})]

    win.start(pairs, 'movie', 'tt1')

    assert win.heading == ''
    assert win.art is None


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
        def start(self, pairs, stype, sid, poster=None, heading='', art=None):
            captured['args'] = (pairs, stype, sid, poster)
            return True

    monkeypatch.setattr(sw, 'StreamsWindow', RecordingWindow)
    # open_streams() now round-trips after a played start() - stub the wait
    # helper to "no reopen" (as if the user backed out immediately) so this
    # stays a single-iteration test of aggregate forwarding.
    monkeypatch.setattr(sw, '_wait_for_playback_end', lambda *a, **k: False)

    result = sw.open_streams('movie', 'tt1', poster='https://x/poster.jpg')

    assert [call[0] for call in client.calls] == ['t-supported']
    pairs, stype, sid, poster = captured['args']
    assert (stype, sid, poster) == ('movie', 'tt1', 'https://x/poster.jpg')
    assert [s for _info, s in pairs] == [stream]
    assert result is False


def test_open_streams_forwards_heading_and_art_to_the_window(load_streamswindow, monkeypatch):
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
        def start(self, pairs, stype, sid, poster=None, heading='', art=None):
            captured['heading'] = heading
            captured['art'] = art
            return True

    monkeypatch.setattr(sw, 'StreamsWindow', RecordingWindow)
    # See test_open_streams_filters_unsupported_addons_and_forwards_aggregate_to_the_window
    # - stub the round-trip wait so a played start() ends the call here.
    monkeypatch.setattr(sw, '_wait_for_playback_end', lambda *a, **k: False)

    result = sw.open_streams(
        'movie', 'tt1', heading='Some Movie',
        art={'poster': 'https://x/p.jpg', 'fanart': 'https://x/f.jpg'},
    )

    assert result is False
    assert captured['heading'] == 'Some Movie'
    assert captured['art'] == {'poster': 'https://x/p.jpg', 'fanart': 'https://x/f.jpg'}


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

        def start(self, pairs, stype, sid, poster=None, heading='', art=None):
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
        def start(self, pairs, stype, sid, poster=None, heading='', art=None):
            captured['pairs'] = pairs
            return True

    monkeypatch.setattr(sw, 'StreamsWindow', RecordingWindow)
    # Not testing the round-trip here - stub it away (see
    # test_open_streams_filters_unsupported_addons_and_forwards_aggregate_to_the_window).
    monkeypatch.setattr(sw, '_wait_for_playback_end', lambda *a, **k: False)

    result = sw.open_streams('movie', 'tt1')

    assert result is False
    assert [call[0] for call in client.calls] == ['t-fail', 't-ok']
    assert [s for _info, s in captured['pairs']] == [ok_stream]
    # The failing addon must never hit ERROR (that was the noisy old
    # behavior) - one DEBUG line naming it, plus exactly one aggregate
    # WARNING summarizing the fetch, and nothing else at WARNING/ERROR.
    assert not [lvl for _msg, lvl in ctx.env.log_calls if lvl == xbmc.LOGERROR]
    debug_msgs = [msg for msg, lvl in ctx.env.log_calls if lvl == xbmc.LOGDEBUG]
    assert any('t-fail' in msg and 'upstream down' in msg for msg in debug_msgs)
    warnings = [msg for msg, lvl in ctx.env.log_calls if lvl == xbmc.LOGWARNING]
    assert len(warnings) == 1
    assert 'streamswindow: 1 addon(s) failed' in warnings[0]


def test_open_streams_multiple_addon_failures_still_log_a_single_aggregate_warning(
    load_streamswindow, monkeypatch,
):
    ctx = load_streamswindow()
    import xbmc
    sw = ctx.streamswindow
    fail_a = {
        'transportUrl': 't-fail-a',
        'manifest': {'name': 'FailA', 'resources': ['stream'], 'types': ['movie']},
    }
    fail_b = {
        'transportUrl': 't-fail-b',
        'manifest': {'name': 'FailB', 'resources': ['stream'], 'types': ['movie']},
    }
    working = {
        'transportUrl': 't-ok',
        'manifest': {'name': 'Working', 'resources': ['stream'], 'types': ['movie']},
    }
    ok_stream = {'url': 'https://a.example/a.mp4'}
    client = _FakeAddonClient({
        't-fail-a': AddonError('boom a'), 't-fail-b': AddonError('boom b'), 't-ok': [ok_stream],
    })
    _wire_data_layer(sw, _FakeStore(addons=[fail_a, fail_b, working]), client)

    class RecordingWindow(sw.StreamsWindow):
        def start(self, pairs, stype, sid, poster=None, heading='', art=None):
            return True

    monkeypatch.setattr(sw, 'StreamsWindow', RecordingWindow)
    monkeypatch.setattr(sw, '_wait_for_playback_end', lambda *a, **k: False)

    result = sw.open_streams('movie', 'tt1')

    assert result is False
    debug_msgs = [msg for msg, lvl in ctx.env.log_calls if lvl == xbmc.LOGDEBUG]
    assert sum(1 for msg in debug_msgs if 't-fail-a' in msg) == 1
    assert sum(1 for msg in debug_msgs if 't-fail-b' in msg) == 1
    warnings = [msg for msg, lvl in ctx.env.log_calls if lvl == xbmc.LOGWARNING]
    assert len(warnings) == 1
    assert 'streamswindow: 2 addon(s) failed' in warnings[0]
    assert not [lvl for _msg, lvl in ctx.env.log_calls if lvl == xbmc.LOGERROR]


def test_open_streams_addon_failure_debug_message_is_a_single_line(load_streamswindow):
    ctx = load_streamswindow()
    import xbmc
    sw = ctx.streamswindow
    failing = {
        'transportUrl': 't-fail',
        'manifest': {'name': 'Failing', 'resources': ['stream'], 'types': ['movie']},
    }
    client = _FakeAddonClient({'t-fail': AddonError('line one\r\nline two')})
    _wire_data_layer(sw, _FakeStore(addons=[failing]), client)

    result = sw.open_streams('movie', 'tt1')

    assert result is False  # the only addon failed -> no streams at all
    debug_msgs = [msg for msg, lvl in ctx.env.log_calls if lvl == xbmc.LOGDEBUG]
    assert any('line one  line two' in msg for msg in debug_msgs)
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
        def start(self, pairs, stype, sid, poster=None, heading='', art=None):
            captured['pairs'] = pairs
            return True

    monkeypatch.setattr(sw, 'StreamsWindow', RecordingWindow)
    # A played start() would otherwise round-trip forever (RecordingWindow
    # always returns True) - stub it away; this test only cares about sort
    # order, not the round-trip loop.
    monkeypatch.setattr(sw, '_wait_for_playback_end', lambda *a, **k: False)

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
        def start(self, pairs, stype, sid, poster=None, heading='', art=None):
            captured['pairs'] = pairs
            return True

    monkeypatch.setattr(sw, 'StreamsWindow', RecordingWindow)
    monkeypatch.setattr(sw, '_wait_for_playback_end', lambda *a, **k: False)

    result = sw.open_streams('movie', 'tt1')

    assert result is False
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
        def start(self, pairs, stype, sid, poster=None, heading='', art=None):
            captured['pairs'] = pairs
            return True

    monkeypatch.setattr(sw, 'StreamsWindow', RecordingWindow)
    monkeypatch.setattr(sw, '_wait_for_playback_end', lambda *a, **k: False)

    result = sw.open_streams('movie', 'tt1')

    assert result is False
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
    `.calls` records every `isPlaying()` poll."""

    def __init__(self, is_playing):
        self._is_playing = is_playing
        self.calls = 0

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
    player = _ScriptedPlayer(is_playing=lambda n: n in (2, 3))
    monitor = _ScriptedMonitor(abort=False)

    result = sw._wait_for_playback_end(player=player, monitor=monitor, start_timeout=5.0, tick=0.1)

    assert result is True
    assert player.calls == 4
    assert monitor.calls == 2  # one abort check per tick that didn't already end the wait


def test_wait_for_playback_end_returns_true_when_playback_never_starts_within_timeout(load_streamswindow):
    ctx = load_streamswindow()
    sw = ctx.streamswindow
    player = _ScriptedPlayer(is_playing=False)  # never starts
    monitor = _ScriptedMonitor(abort=False)

    result = sw._wait_for_playback_end(player=player, monitor=monitor, start_timeout=1.0, tick=0.5)

    assert result is True  # nothing left to wait out - safe to reopen anyway
    assert player.calls == 2  # int(1.0 / 0.5) start-wait attempts
    assert monitor.calls == 2


def test_wait_for_playback_end_returns_false_immediately_on_monitor_abort_before_playing(load_streamswindow):
    ctx = load_streamswindow()
    sw = ctx.streamswindow
    player = _ScriptedPlayer(is_playing=False)
    monitor = _ScriptedMonitor(abort=True)  # aborts on the very first poll

    result = sw._wait_for_playback_end(player=player, monitor=monitor, start_timeout=20.0, tick=0.5)

    assert result is False
    assert player.calls == 1
    assert monitor.calls == 1  # stopped on the very first abort check, not the full budget


def test_wait_for_playback_end_returns_false_immediately_on_monitor_abort_while_playing(load_streamswindow):
    ctx = load_streamswindow()
    sw = ctx.streamswindow
    player = _ScriptedPlayer(is_playing=True)  # already playing on the very first check
    monitor = _ScriptedMonitor(abort=True)

    result = sw._wait_for_playback_end(player=player, monitor=monitor, start_timeout=20.0, tick=0.5)

    assert result is False
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

    assert result is False
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
        def start(self, pairs, stype, sid, poster=None, heading='', art=None):
            start_calls.append((pairs, stype, sid, poster, heading, art))
            return len(start_calls) == 1  # plays the first time, backs out of the reopened window

    monkeypatch.setattr(sw, 'StreamsWindow', RecordingWindow)
    ctx.env.player_is_playing = lambda n: n <= 2  # "playing" for 2 polls, then stopped

    result = sw.open_streams('movie', 'tt1', heading='Some Movie', art={'poster': 'https://x/p.jpg'})

    assert result is False
    assert len(start_calls) == 2
    assert start_calls[0] == start_calls[1]  # reopened with the SAME pairs/heading/art/poster
    assert start_calls[0][0] is start_calls[1][0]  # not just equal - the identical pairs list
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
        def start(self, pairs, stype, sid, poster=None, heading='', art=None):
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
        def start(self, pairs, stype, sid, poster=None, heading='', art=None):
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
        def start(self, pairs, stype, sid, poster=None, heading='', art=None):
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
        def start(self, pairs, stype, sid, poster=None, heading='', art=None):
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
        def start(self, pairs, stype, sid, poster=None, heading='', art=None):
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
