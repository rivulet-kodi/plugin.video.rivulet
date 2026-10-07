# SPDX-FileCopyrightText: 2026 M0Rf30
# SPDX-License-Identifier: MIT

"""Tests for lib.ui.streamswindow's playback round trip: StreamsWindow.
onClick() (resolving the focused pair and dispatching to play_direct(),
including the on_ready teardown hook for GH-2), _wait_for_playback_end()
(the injectable poll loop deciding when it is safe to reopen the picker),
open_streams()'s post-playback reopen over the same fetched pairs, and
the binge-watching auto-play chain (_try_binge_watch() wired through
lib.ui.binge). See test_streamswindow.py for onInit()/rendering coverage
and test_streamswindow_fetch.py for the addon-fetch/failure-aggregation
side of open_streams(); fixtures/fakes are duplicated from that file.
"""
import contextlib
import threading
import time

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


def test_onclick_ignores_a_pick_replayed_while_the_first_is_still_preparing(load_streamswindow, monkeypatch):
    """Kodi delivers queued click callbacks on the script thread whenever it
    calls into Kodi, including from inside a pick still preparing its
    stream. kodi.log showed each queued click starting its own "Preparing
    stream" for another source, nested in the first; only the first counts."""
    ctx = load_streamswindow()
    win = _make_window(ctx.streamswindow)
    stream_a = {'url': 'https://a.example/a.mp4'}
    stream_b = {'url': 'https://b.example/b.mp4'}
    win.pairs = [({'raw': 'A'}, stream_a), ({'raw': 'B'}, stream_b)]
    win.onInit()
    played = []

    def fake_play_direct(stream, stype, sid, item_meta=None, on_ready=None, video_id=None):
        played.append(stream)
        win.getControl(ctx.streamswindow.LIST).selected_index = 1
        win.onClick(ctx.streamswindow.LIST)  # the queued click, replayed mid-preparation
        return False  # e.g. the user cancelled the first one

    monkeypatch.setattr(ctx.player, 'play_direct', fake_play_direct)

    win.onClick(ctx.streamswindow.LIST)

    assert played == [stream_a]
    assert any('ignoring a pick' in msg for msg, _level in ctx.env.log_calls)


def test_onclick_after_a_cancelled_pick_plays_again(load_streamswindow, monkeypatch):
    ctx = load_streamswindow()
    win = _make_window(ctx.streamswindow)
    win.pairs = [({'raw': 'A'}, {'url': 'https://a.example/a.mp4'})]
    win.onInit()
    results = [False, True]
    calls = []
    monkeypatch.setattr(
        ctx.player, 'play_direct', lambda stream, *a, **k: calls.append(stream) or results.pop(0),
    )

    win.onClick(ctx.streamswindow.LIST)
    win.onClick(ctx.streamswindow.LIST)

    assert len(calls) == 2
    assert win.played is True


def test_onclick_ignores_a_pick_delivered_after_the_window_was_handed_to_the_player(
    load_streamswindow, monkeypatch,
):
    ctx = load_streamswindow()
    win = _make_window(ctx.streamswindow)
    win.pairs = [({'raw': 'A'}, {'url': 'https://a.example/a.mp4'})]
    win.onInit()
    calls = []

    def fake_play_direct(stream, stype, sid, item_meta=None, on_ready=None, video_id=None):
        calls.append(stream)
        on_ready()
        return True

    monkeypatch.setattr(ctx.player, 'play_direct', fake_play_direct)

    win.onClick(ctx.streamswindow.LIST)
    win.onClick(ctx.streamswindow.LIST)  # queued before the handoff, delivered after it

    assert len(calls) == 1


def test_onclick_works_again_once_a_force_closed_picker_is_reopened(load_streamswindow, monkeypatch):
    """ModalStackWindow.doModal() reopens a screen close_windows_for_playback()
    force-closed without going through start(); onInit() must make it
    pickable again."""
    ctx = load_streamswindow()
    win = _make_window(ctx.streamswindow)
    win.pairs = [({'raw': 'A'}, {'url': 'https://a.example/a.mp4'})]
    win.onInit()
    win.close()
    win.onInit()  # reopened
    calls = []
    monkeypatch.setattr(ctx.player, 'play_direct', lambda stream, *a, **k: calls.append(stream) or False)

    win.onClick(ctx.streamswindow.LIST)

    assert len(calls) == 1


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

def test_open_streams_late_addon_answer_delivered_right_at_window_close_survives_the_reopen(
    load_streamswindow, monkeypatch,
):
    """Regression: `consume_next()` used to append straight onto the
    `pairs` name that the live->snapshot transition then rebinds to
    `win.pairs` (see `open_streams()`'s own comment on that transition) -
    an addon answering on the background drain thread at that exact
    moment raced the rebind and its pairs vanished from the reopened
    window instead of surviving into it. Blocks the second addon's own
    answer until the picked window's `start()` is already past the
    point where the real bug's race would have mattered, then asserts
    the reopened window still gets it - deterministic, not timing-luck."""
    ctx = load_streamswindow()
    sw = ctx.streamswindow
    fast = {
        'transportUrl': 't-fast',
        'manifest': {'name': 'Fast', 'resources': ['stream'], 'types': ['movie']},
    }
    late = {
        'transportUrl': 't-late',
        'manifest': {'name': 'Late', 'resources': ['stream'], 'types': ['movie']},
    }
    fast_stream = {'url': 'https://fast.example/a.mp4'}
    late_stream = {'url': 'https://late.example/a.mp4'}
    release_late = threading.Event()
    late_delivered = threading.Event()

    class _OrderedClient:
        def streams(self, transport, stype, sid):
            if transport == 't-late':
                release_late.wait(2)
                streams = [late_stream]
                late_delivered.set()
                return streams
            return [fast_stream]

    _wire_data_layer(sw, _FakeStore(addons=[fast, late]), _OrderedClient())
    start_calls = []

    class RecordingWindow(sw.StreamsWindow):
        def start(self, pairs, stype, sid, poster=None, heading='', art=None, meta=None, video_id=None):
            self.pairs = list(pairs)
            start_calls.append(list(pairs))
            if len(start_calls) == 1:
                # Let the slow addon answer now, and wait for the fan-out
                # thread to have actually folded it into `late_pairs`
                # before this call returns and open_streams() runs the
                # live->snapshot transition - the exact instant the old
                # code's rebind race could drop it.
                release_late.set()
                assert late_delivered.wait(2)
                time.sleep(0.1)  # let the drain thread finish folding it into late_pairs
                return True  # "played" - triggers the reopen round trip
            return False  # back out of the reopened window

    monkeypatch.setattr(sw, 'StreamsWindow', RecordingWindow)
    ctx.env.player_is_playing = lambda n: False

    result = sw.open_streams('movie', 'tt1')

    assert result is False
    assert len(start_calls) == 2
    assert [s for _info, s in start_calls[0]] == [fast_stream]  # first window only ever saw the fast addon
    reopened_streams = {s['url'] for _info, s in start_calls[1]}
    assert reopened_streams == {fast_stream['url'], late_stream['url']}


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
    # default 45s/0.5s start-wait budget
    # (streamswindow._PLAYBACK_START_TIMEOUT_SECONDS).

    result = sw.open_streams('movie', 'tt1')

    assert result is False
    assert len(start_calls) == 2  # reopened despite playback never starting
    assert ctx.env.player_is_playing_calls == 90  # int(45.0 / 0.5) start-wait attempts
    assert ctx.env.monitor_abort_calls == 91  # 90 start-wait ticks + the settle pause


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


# ---------------------------------------------------------------------------
# The three phases and the transitions between them
#
#   1 addon   Rivulet screens own the input (before playback)
#   2 player  every screen closed, Kodi's VideoPlayer/OSD owns the input
#   3 addon   the player is truly gone: ONE coherent stack comes back
#
# Real Kodi flips `isPlaying()` to False at the START of CVideoPlayer::
# CloseFile() but announces onPlayBackStopped/Ended/Error only after it
# returned (kodi.log 01:07:53.96 CloseFile ... 01:07:59.98 finished, service
# callback 01:08:00.137 - the cache thread cannot cancel a blocked HTTP read).
# `_SlowClosingKodi` reproduces exactly that ordering; the fake Player's own
# immediate dispatch would hide it.
# ---------------------------------------------------------------------------


class _SlowClosingKodi:
    """Patches the fake `xbmc.Player`/`Monitor` so the stop callback arrives
    `close_ticks` `waitForAbort()` ticks AFTER `isPlaying()` went False, like
    real Kodi, and runs `on_tick(tick_number, kodi)` on every tick (the
    script thread calling into Kodi - which is when Kodi delivers queued
    window callbacks). `.playing_polls` is how many polls report True."""

    def __init__(self, ctx, monkeypatch, playing_polls=2, close_ticks=3, reason='stopped', on_tick=None):
        import sys

        self.ctx = ctx
        self.players = []
        self.on_tick = on_tick
        self.close_ticks = close_ticks
        self.reason = reason
        self.closing_ticks = None  # None = not closing; else ticks since isPlaying() went False
        self.callback_at_tick = None
        self.ticks = 0
        xbmc = sys.modules['xbmc']
        kodi = self
        real_init = xbmc.Player.__init__

        def init(player):
            real_init(player)
            kodi.players.append(player)

        def is_playing(player):
            env = ctx.env
            env.player_is_playing_calls += 1
            result = env.player_is_playing_calls <= playing_polls
            if player._was_playing and not result and kodi.closing_ticks is None:
                kodi.closing_ticks = 0  # CloseFile() started
            player._was_playing = result
            return result

        monkeypatch.setattr(xbmc.Player, '__init__', init)
        monkeypatch.setattr(xbmc.Player, 'isPlaying', is_playing)
        ctx.env.monitor_abort = self._tick

    def _tick(self, _call_number):
        self.ticks += 1
        if self.closing_ticks is not None:
            self.closing_ticks += 1
            if self.closing_ticks >= self.close_ticks:
                self.closing_ticks = None
                self.callback_at_tick = self.ticks
                for player in self.players:
                    getattr(player, 'onPlayBackStopped' if self.reason == 'stopped' else 'onPlayBackEnded')()
        if self.on_tick is not None:
            self.on_tick(self.ticks, self)
        return False


def test_wait_for_playback_end_waits_for_the_closed_callback_not_just_isplaying_false(
    load_streamswindow, monkeypatch,
):
    ctx = load_streamswindow()
    sw = ctx.streamswindow
    kodi = _SlowClosingKodi(ctx, monkeypatch, playing_polls=2, close_ticks=4, reason='stopped')

    result = sw._wait_for_playback_end(start_timeout=5.0, tick=0.5)

    assert result == (True, False)
    assert kodi.callback_at_tick is not None
    assert kodi.ticks == kodi.callback_at_tick  # returned the very tick the callback landed, not before
    assert kodi.closing_ticks is None


def test_wait_for_playback_end_reports_a_natural_end_only_after_its_callback(load_streamswindow, monkeypatch):
    ctx = load_streamswindow()
    sw = ctx.streamswindow
    _SlowClosingKodi(ctx, monkeypatch, playing_polls=1, close_ticks=2, reason='ended')

    assert sw._wait_for_playback_end(start_timeout=5.0, tick=0.5) == (True, True)


def test_wait_for_playback_end_gives_up_waiting_for_a_callback_that_never_comes(load_streamswindow, monkeypatch):
    ctx = load_streamswindow()
    sw = ctx.streamswindow
    _SlowClosingKodi(ctx, monkeypatch, playing_polls=1, close_ticks=10 ** 9)  # CloseFile never finishes

    result = sw._wait_for_playback_end(start_timeout=5.0, tick=0.5)

    assert result == (True, False)  # reopen anyway rather than strand the user on an empty screen
    assert any('no player-closed callback' in msg for msg, _lvl in ctx.env.log_calls)
    assert sum(ctx.env.wait_calls) == pytest.approx(sw._PLAYER_CLOSE_TIMEOUT_SECONDS)


def test_wait_for_playback_end_aborts_while_the_player_is_still_closing(load_streamswindow, monkeypatch):
    ctx = load_streamswindow()
    sw = ctx.streamswindow
    kodi = _SlowClosingKodi(ctx, monkeypatch, playing_polls=1, close_ticks=10 ** 9)
    real_tick = kodi._tick
    ctx.env.monitor_abort = lambda n: real_tick(n) or kodi.ticks >= 3  # Kodi quits mid-teardown

    assert sw._wait_for_playback_end(start_timeout=5.0, tick=0.5) == (False, False)


def test_full_play_stop_cycle_phase1_to_phase2_to_phase3(load_streamswindow, monkeypatch):
    """Pick -> player -> stop -> picker back, with everything that used to
    go wrong along the way injected at the moment it went wrong."""
    import time

    ctx = load_streamswindow()
    import xbmc
    import xbmcgui
    sw = ctx.streamswindow
    uic = ctx.uicommon
    _wire_single_supported_addon(sw)
    # Another interpreter's Home heartbeat, as in the live log (Home runs
    # in the interpreter that owns this stack).
    ctx.env.window_properties.setdefault(10000, {})['rivulet.ui.heartbeat'] = 'tok|%.3f' % time.time()
    ctx.env.window_properties[10000]['rivulet.ui.phase'] = 'addon'

    class Ancestor(uic.BaseWindow):
        """Stands for Detail/Showcase/Home sitting under the picker."""

        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.clicks = 0
            self.backs = 0

        def onClick(self, control_id):
            self.clicks += 1  # in real life: opens another screen / another pick

        def onAction(self, action):
            if action.getId() in uic.BACK_ACTIONS:
                self.backs += 1
            super().onAction(action)

    ancestor = Ancestor('Detail.xml', '/addon/path', 'Default', '1080i')
    uic._MODAL_WINDOW_STACK.append(ancestor)
    log = []
    start_calls = []

    def on_tick(tick, kodi):
        # Phase 2: Kodi delivers a Back and a click queued before the
        # handoff, and refreshes the Videos container (re-running default.py).
        if kodi.callback_at_tick is None:
            log.append(('tick', tick, uic.input_suppressed()))
            ancestor.onAction(xbmcgui.Action(92))
            ancestor.onClick(30002)
            assert uic.is_duplicate_launch() is True

    kodi = _SlowClosingKodi(ctx, monkeypatch, playing_polls=3, close_ticks=3, on_tick=on_tick)

    class RecordingWindow(sw.StreamsWindow):
        def start(self, pairs, stype, sid, poster=None, heading='', art=None, meta=None, video_id=None):
            self.pairs = list(pairs)
            start_calls.append(1)
            if len(start_calls) == 1:
                # Phase 1 -> 2: exactly what StreamsWindow.onClick() does.
                log.append(('phase1', uic.input_suppressed()))
                sw._close_for_player_handoff(self)
                xbmc.Player().play('http://127.0.0.1:11470/x/0')
                log.append(('handoff', uic.input_suppressed(), ancestor._closed_for_playback, self.closed))
                return True
            # Phase 3: the picker is back.
            log.append(('phase3', uic.input_suppressed(), kodi.callback_at_tick is not None))
            return False

    monkeypatch.setattr(sw, 'StreamsWindow', RecordingWindow)

    result = sw.open_streams('movie', 'tt1', heading='Some Movie')

    assert result is False
    assert len(start_calls) == 2  # exactly ONE picker reopened - no duplicate UI
    assert log[0] == ('phase1', False)
    assert log[1] == ('handoff', True, True, True)  # silent, ancestor flagged for lazy restore, picker closed
    ticks = [entry for entry in log if entry[0] == 'tick']
    assert ticks and all(entry[2] for entry in ticks)  # input suppressed on every phase-2 tick
    assert ancestor.clicks == 0 and ancestor.backs == 0  # nothing replayed into a closed screen
    assert ancestor._closed_for_playback is True  # still flagged: restored lazily when the stack unwinds
    # 2 -> 3: reopened only once Kodi announced the player closed, after the drain.
    assert log[-1] == ('phase3', False, True)
    assert ctx.env.window_properties[10000]['rivulet.ui.phase'] == 'addon'
    assert 'Dialog.Close(all, true)' not in ctx.env.executed_builtins
    # ...and input is live again for the next Back/click.
    ancestor.onClick(30002)
    ancestor.onAction(xbmcgui.Action(92))
    assert (ancestor.clicks, ancestor.backs) == (1, 1)


def test_a_play_that_fails_after_the_handoff_never_leaves_the_player_phase_stuck(load_streamswindow, monkeypatch):
    ctx = load_streamswindow()
    sw = ctx.streamswindow
    _wire_single_supported_addon(sw)

    class RecordingWindow(sw.StreamsWindow):
        def start(self, pairs, stype, sid, poster=None, heading='', art=None, meta=None, video_id=None):
            self.pairs = list(pairs)
            ctx.uicommon.close_windows_for_playback(exclude=self)
            return False  # Player.play() raised: onClick unwound without `played`

    monkeypatch.setattr(sw, 'StreamsWindow', RecordingWindow)

    assert sw.open_streams('movie', 'tt1') is False

    assert ctx.uicommon.input_suppressed() is False


def test_aborting_during_the_wait_ends_the_player_phase(load_streamswindow, monkeypatch):
    ctx = load_streamswindow()
    sw = ctx.streamswindow
    _wire_single_supported_addon(sw)

    class RecordingWindow(sw.StreamsWindow):
        def start(self, pairs, stype, sid, poster=None, heading='', art=None, meta=None, video_id=None):
            self.pairs = list(pairs)
            ctx.uicommon.close_windows_for_playback(exclude=self)
            return True

    monkeypatch.setattr(sw, 'StreamsWindow', RecordingWindow)
    ctx.env.monitor_abort = True

    assert sw.open_streams('movie', 'tt1') is False

    assert ctx.uicommon.input_suppressed() is False
