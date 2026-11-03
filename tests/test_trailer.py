"""lib/ui/trailer.py: which YouTube id a meta's trailer is, and where it plays from."""
import pytest

from tests.kodistubs import install_kodi_stubs


@pytest.fixture
def trailer():
    # The spinner must use this test's Kodi/dialog state, rather than a
    # canceled dialog class retained from a previous isolated session.
    with install_kodi_stubs(reload=(
        'lib.ui.compat', 'lib.ui.uicommon', 'lib.ui.dialogs', 'lib.ui.trailer',
    )) as ctx:
        import lib.ui.trailer as module
        ctx.trailer = module
        yield ctx


def test_trailer_streams_win_over_the_older_field(trailer):
    meta = {
        'trailerStreams': [{'title': 'Official', 'ytId': 'abcdefghijk'}],
        'trailers': [{'source': 'zzzzzzzzzzz', 'type': 'Trailer'}],
    }
    assert trailer.trailer.trailer_id(meta) == 'abcdefghijk'


def test_a_trailer_is_preferred_over_a_clip(trailer):
    meta = {'trailers': [
        {'source': 'clip0000000', 'type': 'Clip'},
        {'source': 'trailer0000', 'type': 'Trailer'},
    ]}
    assert trailer.trailer.trailer_id(meta) == 'trailer0000'


@pytest.mark.parametrize('meta', [None, {}, {'trailers': [{'source': 'not an id!'}]}, {'trailerStreams': ['x']}])
def test_no_usable_trailer(trailer, meta):
    assert trailer.trailer.trailer_id(meta) == ''


@pytest.mark.parametrize(('stream', 'expected'), [
    ({'ytId': 'K5MGifjkW8w'}, 'K5MGifjkW8w'),
    ({'ytId': ' K5MGifjkW8w '}, 'K5MGifjkW8w'),
    ({'ytId': 'K5MGifjkW8w', 'url': 'https://x/v.mp4'}, ''),
    ({'ytId': 'K5MGifjkW8w', 'infoHash': 'a' * 40}, ''),
    ({'ytId': 'K5MGifjkW8w', 'externalUrl': 'https://x'}, ''),
    ({'ytId': 'not/an id'}, ''),
    ({'url': 'https://x/v.mp4'}, ''),
    (None, ''),
])
def test_only_a_stream_that_is_just_a_youtube_id_is_a_trailer(trailer, stream, expected):
    assert trailer.trailer.stream_trailer_id(stream) == expected


def test_a_server_stream_that_answers_goes_first_then_the_youtube_addon(trailer, monkeypatch):
    monkeypatch.setattr(trailer.trailer, '_youtube_addon_url', lambda yt: 'plugin://plugin.video.youtube/play/?video_id=%s' % yt)
    monkeypatch.setattr(trailer.trailer, '_server_url', lambda yt: 'http://127.0.0.1:11470/yt/%s' % yt)
    assert list(trailer.trailer.trailer_urls('abcdefghijk')) == [
        'http://127.0.0.1:11470/yt/abcdefghijk',
        'plugin://plugin.video.youtube/play/?video_id=abcdefghijk',
    ]
    # A probe already made is not made again.
    assert list(trailer.trailer.trailer_urls('abcdefghijk', '')) == [
        'plugin://plugin.video.youtube/play/?video_id=abcdefghijk',
    ]


def test_a_server_url_that_does_not_play_is_not_used(trailer, monkeypatch):
    import lib.ui.player as player

    class _Server:
        def is_available(self):
            return True

        def resolve_stream(self, stream):
            return 'http://127.0.0.1:11470/yt/%s' % stream['ytId']

    monkeypatch.setattr(player, '_server_client', lambda: _Server())
    monkeypatch.setattr(trailer.trailer, '_plays', lambda url: False)
    assert trailer.trailer._server_url('abcdefghijk') == ''
    monkeypatch.setattr(trailer.trailer, '_plays', lambda url: True)
    assert trailer.trailer._server_url('abcdefghijk').endswith('/yt/abcdefghijk')


def test_no_trailer_tells_the_viewer_and_plays_nothing(trailer, monkeypatch):
    monkeypatch.setattr(trailer.trailer, '_full_meta', lambda meta, stype, sid: meta)
    assert trailer.trailer.play_trailer({'name': 'X'}) is False
    assert trailer.env.notifications[-1][1] == 'STR30497'


def test_nowhere_to_play_it_offers_the_youtube_addon(trailer, monkeypatch):
    monkeypatch.setattr(trailer.trailer, '_full_meta', lambda meta, stype, sid: meta)
    monkeypatch.setattr(trailer.trailer, '_server_url', lambda yt: '')
    monkeypatch.setattr(trailer.trailer, '_youtube_addon_url', lambda yt: '')
    import lib.ui.dialogs as dialogs
    asked = []
    monkeypatch.setattr(dialogs, 'confirm', lambda heading, body, yes, no: asked.append(body) or True)
    assert trailer.trailer.play_trailer({'trailers': [{'source': 'abcdefghijk'}]}) is False
    assert asked == ['STR30504']
    assert 'InstallAddon(plugin.video.youtube)' in trailer.env.executed_builtins


def _routes(trailer, monkeypatch, outcomes):
    monkeypatch.setattr(trailer.trailer, '_full_meta', lambda meta, stype, sid: meta)
    monkeypatch.setattr(trailer.trailer, '_server_url', lambda yt: 'http://127.0.0.1:11470/yt/%s' % yt)
    monkeypatch.setattr(trailer.trailer, '_youtube_addon_url', lambda yt: 'plugin://plugin.video.youtube/play/?video_id=%s' % yt)
    tried = []
    watched = []
    queue = list(outcomes)
    monkeypatch.setattr(trailer.trailer, '_start', lambda url, item, heading: tried.append(url) or queue.pop(0))
    monkeypatch.setattr(trailer.trailer, '_watch', lambda: watched.append(True))
    return tried, watched


_META = {'name': 'Dune', 'trailers': [{'source': 'abcdefghijk'}]}


def test_a_stalled_route_is_dropped_for_the_next(trailer, monkeypatch):
    tried, watched = _routes(trailer, monkeypatch, ['stalled', 'rolling'])
    assert trailer.trailer.play_trailer(_META) is True
    assert [url.split(':')[0] for url in tried] == ['http', 'plugin']
    assert watched == [True]


def test_no_route_starting_says_so_and_keeps_the_screens(trailer, monkeypatch):
    tried, watched = _routes(trailer, monkeypatch, ['stalled', 'stalled'])
    assert trailer.trailer.play_trailer(_META) is False
    assert len(tried) == 2
    assert watched == []
    assert trailer.env.notifications[-1][1] == 'STR30510'


def test_back_on_the_spinner_stops_trying(trailer, monkeypatch):
    tried, watched = _routes(trailer, monkeypatch, ['cancelled', 'rolling'])
    assert trailer.trailer.play_trailer(_META) is False
    assert len(tried) == 1
    assert watched == []


class _Player:
    def __init__(self, clock):
        self._clock = list(clock)
        self.stopped = False
        self.playing = True

    def isPlayingVideo(self):
        return bool(self._clock)

    def isPlaying(self):
        return self.playing and not self.stopped

    def getTime(self):
        return self._clock.pop(0) if len(self._clock) > 1 else self._clock[0]

    def stop(self):
        self.stopped = True


class _Monitor:
    def waitForAbort(self, seconds):
        return False

    def abortRequested(self):
        return False


def test_rolling_means_the_clock_moved(trailer):
    player = _Player([0.0, 0.0, 0.1, 0.6])
    assert trailer.trailer._wait_rolling(player, _Monitor(), timeout=5, tick=0) == 'rolling'


def test_a_clock_stuck_at_zero_is_a_stall(trailer):
    player = _Player([0.0])
    assert trailer.trailer._wait_rolling(player, _Monitor(), timeout=0.05, tick=0) == 'stalled'


def test_the_start_wait_runs_on_the_monotonic_clock(trailer, monkeypatch):
    """A wall clock that jumps back (NTP, a time zone change) must not
    hold the wait open: the deadline is measured on time.monotonic()."""
    import time

    ticks = iter(range(100))
    monkeypatch.setattr(time, 'monotonic', lambda: float(next(ticks)))
    monkeypatch.setattr(time, 'time', lambda: 0.0)  # frozen wall clock
    player = _Player([0.0])
    assert trailer.trailer._wait_rolling(player, _Monitor(), timeout=5, tick=0) == 'stalled'


def test_a_stalled_start_stops_the_player(trailer, monkeypatch):
    import xbmc

    player = _Player([0.0])
    monkeypatch.setattr(xbmc, 'Player', lambda: player, raising=False)
    played = []
    player.play = lambda url, item: played.append(url)
    monkeypatch.setattr(trailer.trailer, '_wait_rolling', lambda player, monitor: 'stalled')
    assert trailer.trailer._start('http://x/yt/abc', object(), 'Trailer') == 'stalled'
    assert played == ['http://x/yt/abc']
    assert player.stopped is True


def test_watching_closes_the_screens_and_goes_full_screen(trailer, monkeypatch):
    import lib.ui.streamswindow as streamswindow
    import lib.ui.uicommon as uicommon

    calls = []
    monkeypatch.setattr(uicommon, 'close_windows_for_playback', lambda: calls.append('close'))
    monkeypatch.setattr(uicommon, 'end_player_phase', lambda: calls.append('end'))
    monkeypatch.setattr(streamswindow, '_wait_for_playback_end', lambda start_timeout: calls.append('wait'))
    trailer.trailer._watch()
    assert calls == ['close', 'wait', 'end']
    assert 'ActivateWindow(fullscreenvideo)' in trailer.env.executed_builtins


@pytest.fixture
def menu():
    with install_kodi_stubs(reload=('lib.ui.compat', 'lib.ui.dialogs', 'lib.ui.libraryaction', 'lib.ui.trailer')) as ctx:
        import lib.ui.libraryaction as libraryaction
        ctx.libraryaction = libraryaction
        yield ctx


class _Store:
    def __init__(self, auth):
        self._auth = auth

    def get_auth(self):
        return self._auth


def _wire(ctx, monkeypatch, auth, pick, record=None):
    import lib.ui.dependencies as dependencies
    import lib.ui.dialogs as dialogs
    import lib.ui.trailer as trailer
    shown = []
    played = []
    monkeypatch.setattr(dependencies, 'get_store', lambda: _Store(auth))
    monkeypatch.setattr(dialogs, 'choose', lambda heading, rows: shown.append(rows) or pick)
    monkeypatch.setattr(trailer, 'play_trailer', lambda meta, stype='', sid='': played.append((stype, sid)))
    monkeypatch.setattr(ctx.libraryaction, '_lookup', lambda auth, sid: (record, True))
    return shown, played


def test_poster_menu_signed_out_still_offers_the_trailer(menu, monkeypatch):
    shown, played = _wire(menu, monkeypatch, None, 0)
    assert menu.libraryaction.poster_menu({'id': 'tt1', 'type': 'movie', 'name': 'Dune'}) is None
    assert shown == [['STR30499']]
    assert played == [('movie', 'tt1')]


def test_poster_menu_signed_in_puts_the_library_row_first(menu, monkeypatch):
    shown, played = _wire(menu, monkeypatch, {'authKey': 'k'}, 2)
    menu.libraryaction.poster_menu({'id': 'tt1', 'type': 'movie', 'name': 'Dune'})
    assert shown == [['STR30293', 'STR30295', 'STR30499']]
    assert played == [('movie', 'tt1')]


def test_poster_menu_library_row_writes(menu, monkeypatch):
    _wire(menu, monkeypatch, {'authKey': 'k'}, 0)
    writes = []
    monkeypatch.setattr(menu.libraryaction, '_write', lambda auth, meta, in_library: writes.append(in_library) or 'added')
    assert menu.libraryaction.poster_menu({'id': 'tt1', 'type': 'movie', 'name': 'Dune'}) == 'added'
    assert writes == [False]


def test_poster_menu_backed_out_does_nothing(menu, monkeypatch):
    shown, played = _wire(menu, monkeypatch, {'authKey': 'k'}, -1)
    assert menu.libraryaction.poster_menu({'id': 'tt1', 'name': 'Dune'}) is None
    assert played == []


# --- the probe ----------------------------------------------------------------


class _Probe:
    def __init__(self, status, kind, close_fails=False):
        self.status_code = status
        self.headers = {'Content-Type': kind}
        self.closed = False
        self._close_fails = close_fails

    def close(self):
        if self._close_fails:
            raise OSError('gone')
        self.closed = True


@pytest.mark.parametrize('status, kind, plays', [
    (200, 'video/mp4', True),
    (206, 'application/octet-stream', True),
    (206, '', True),
    (200, 'text/html; charset=utf-8', False),
    (200, 'application/json', False),
    (404, 'video/mp4', False),
    (502, '', False),
])
def test_the_probe_wants_video_not_a_page(trailer, monkeypatch, status, kind, plays):
    import requests

    sent = []
    probe = _Probe(status, kind)

    def _get(url, **kwargs):
        sent.append(kwargs)
        return probe

    monkeypatch.setattr(requests, 'get', _get)
    assert trailer.trailer._plays('http://127.0.0.1:11470/yt/abcdefghijk') is plays
    # Only the first bytes are asked for, on a bounded wait, and the
    # connection is always let go.
    assert sent[0]['headers'] == {'Range': 'bytes=0-1023'}
    assert sent[0]['timeout'] == trailer.trailer.PROBE_SECONDS
    assert probe.closed is True


def test_a_probe_that_cannot_connect_does_not_play(trailer, monkeypatch):
    import requests

    def _refused(url, **kwargs):
        raise requests.exceptions.ConnectionError('refused')

    monkeypatch.setattr(requests, 'get', _refused)
    assert trailer.trailer._plays('http://127.0.0.1:11470/yt/abcdefghijk') is False


def test_a_close_that_fails_does_not_spoil_the_answer(trailer, monkeypatch):
    import requests

    monkeypatch.setattr(requests, 'get', lambda url, **kwargs: _Probe(206, 'video/webm', close_fails=True))
    assert trailer.trailer._plays('http://x/yt/abcdefghijk') is True


def test_no_server_or_a_server_that_errors_gives_no_url(trailer, monkeypatch):
    import lib.ui.player as player

    class _Down:
        def is_available(self):
            return False

        def resolve_stream(self, stream):
            raise AssertionError('a server that is down is not asked')

    class _Broken:
        def is_available(self):
            return True

        def resolve_stream(self, stream):
            raise RuntimeError('boom')

    class _Empty:
        def is_available(self):
            return True

        def resolve_stream(self, stream):
            return None

    probed = []
    monkeypatch.setattr(trailer.trailer, '_plays', lambda url: probed.append(url) or True)
    for server in (_Down(), _Broken(), _Empty()):
        monkeypatch.setattr(player, '_server_client', lambda server=server: server)
        assert trailer.trailer._server_url('abcdefghijk') == ''
    assert probed == []


def test_the_youtube_addon_is_a_route_only_when_installed(trailer, monkeypatch):
    import xbmc

    asked = []
    monkeypatch.setattr(xbmc, 'getCondVisibility', lambda cond: asked.append(cond) or False)
    assert trailer.trailer._youtube_addon_url('abcdefghijk') == ''
    monkeypatch.setattr(xbmc, 'getCondVisibility', lambda cond: asked.append(cond) or True)
    assert trailer.trailer._youtube_addon_url('abcdefghijk') == 'plugin://plugin.video.youtube/play/?video_id=abcdefghijk'
    assert asked == ['System.HasAddon(plugin.video.youtube)'] * 2


def test_no_route_at_all_gives_an_empty_url(trailer, monkeypatch):
    monkeypatch.setattr(trailer.trailer, '_server_url', lambda yt: '')
    monkeypatch.setattr(trailer.trailer, '_youtube_addon_url', lambda yt: '')
    assert list(trailer.trailer.trailer_urls('abcdefghijk')) == []


# --- the full meta ------------------------------------------------------------


def _fetching(trailer, monkeypatch, answer):
    import lib.ui.uicommon as uicommon
    import lib.ui.views as views

    fetched = []
    monkeypatch.setattr(views, '_fetch_meta', lambda stype, sid: fetched.append((stype, sid)) or answer)
    monkeypatch.setattr(uicommon, 'run_cancellable', lambda func, heading, message='': func())
    return fetched


def test_a_preview_without_trailers_is_filled_from_the_full_meta(trailer, monkeypatch):
    full = {'id': 'tt1', 'name': 'Dune', 'trailers': [{'source': 'abcdefghijk'}]}
    fetched = _fetching(trailer, monkeypatch, full)
    assert trailer.trailer._full_meta({'id': 'tt1'}, 'movie', 'tt1') is full
    assert fetched == [('movie', 'tt1')]


def test_a_meta_that_has_its_trailer_is_not_fetched_again(trailer, monkeypatch):
    fetched = _fetching(trailer, monkeypatch, {})
    meta = {'trailers': [{'source': 'abcdefghijk'}]}
    assert trailer.trailer._full_meta(meta, 'movie', 'tt1') is meta
    # Without a type or an id there is nothing to fetch either.
    assert trailer.trailer._full_meta({}, '', 'tt1') == {}
    assert trailer.trailer._full_meta({}, 'movie', '') == {}
    assert fetched == []


@pytest.mark.parametrize('answer', [None, ['not', 'a', 'meta']])
def test_a_cancelled_or_failed_fetch_keeps_the_preview(trailer, monkeypatch, answer):
    import lib.ui.uicommon as uicommon

    if answer == 'cancelled':
        answer = uicommon.CANCELLED
    _fetching(trailer, monkeypatch, answer)
    preview = {'id': 'tt1', 'name': 'Dune'}
    assert trailer.trailer._full_meta(preview, 'movie', 'tt1') is preview


def test_back_on_the_probe_spinner_plays_nothing(trailer, monkeypatch):
    import lib.ui.uicommon as uicommon

    monkeypatch.setattr(trailer.trailer, '_full_meta', lambda meta, stype, sid: meta)
    monkeypatch.setattr(uicommon, 'run_cancellable', lambda func, heading, message='': uicommon.CANCELLED)
    started = []
    monkeypatch.setattr(trailer.trailer, '_start', lambda url, item, heading: started.append(url) or 'rolling')
    assert trailer.trailer.play_trailer(_META) is False
    assert started == []
    assert trailer.env.notifications == []


def test_kodi_quitting_mid_start_stops_trying(trailer, monkeypatch):
    tried, watched = _routes(trailer, monkeypatch, ['abort', 'rolling'])
    assert trailer.trailer.play_trailer(_META) is False
    assert len(tried) == 1
    assert watched == []


def test_the_spinner_heading_names_the_title(trailer, monkeypatch):
    headings = []
    _routes(trailer, monkeypatch, [])
    monkeypatch.setattr(trailer.trailer, '_start', lambda url, item, heading: headings.append(heading) or 'rolling')
    assert trailer.trailer.play_trailer(_META) is True
    assert headings == ['Dune – STR30499']
    headings[:] = []
    trailer.trailer.play_trailer({'trailers': [{'source': 'abcdefghijk'}]})
    assert headings == ['STR30499']


def test_a_meta_that_is_not_a_dict_has_no_trailer(trailer, monkeypatch):
    monkeypatch.setattr(trailer.trailer, '_full_meta', lambda meta, stype, sid: meta)
    assert trailer.trailer.play_trailer(['junk']) is False
    assert trailer.env.notifications[-1][1] == 'STR30497'


# --- starting a route -----------------------------------------------------------


class _Busy:
    def __init__(self):
        self.entered = 0

    def __enter__(self):
        self.entered += 1
        return self

    def __exit__(self, *exc):
        return False


def _starting(trailer, monkeypatch, result, abort=False, play_fails=False):
    import xbmc

    import lib.ui.uicommon as uicommon

    player = _Player([0.0])
    played = []

    def _play(url, item):
        if play_fails:
            raise RuntimeError('no such file')
        played.append(url)

    player.play = _play

    class _Mon(_Monitor):
        def abortRequested(self):
            return abort

        def waitForAbort(self, seconds):
            return abort

    busy = _Busy()
    monkeypatch.setattr(xbmc, 'Player', lambda: player, raising=False)
    monkeypatch.setattr(xbmc, 'Monitor', _Mon, raising=False)
    monkeypatch.setattr(uicommon, 'busy_dialog', lambda heading, message='': busy)
    outcome = uicommon.CANCELLED if result == 'cancelled' else result
    monkeypatch.setattr(uicommon, 'wait_cancellable', lambda func, dialog, monitor=None: outcome)
    return player, played, busy


def test_a_route_that_rolls_is_left_playing(trailer, monkeypatch):
    player, played, busy = _starting(trailer, monkeypatch, 'rolling')
    assert trailer.trailer._start('http://x/yt/abc', object(), 'Dune') == 'rolling'
    assert played == ['http://x/yt/abc']
    assert busy.entered == 1
    assert player.stopped is False


def test_back_on_the_start_spinner_stops_the_player(trailer, monkeypatch):
    player, _, _ = _starting(trailer, monkeypatch, 'cancelled')
    assert trailer.trailer._start('http://x/yt/abc', object(), 'Dune') == 'cancelled'
    assert player.stopped is True


def test_kodi_quitting_on_the_start_spinner_is_an_abort(trailer, monkeypatch):
    player, _, _ = _starting(trailer, monkeypatch, 'cancelled', abort=True)
    assert trailer.trailer._start('http://x/yt/abc', object(), 'Dune') == 'abort'
    player2, _, _ = _starting(trailer, monkeypatch, 'abort', abort=True)
    assert trailer.trailer._start('http://x/yt/abc', object(), 'Dune') == 'abort'


def test_a_player_that_refuses_the_url_is_a_stall(trailer, monkeypatch):
    _, played, busy = _starting(trailer, monkeypatch, 'rolling', play_fails=True)
    assert trailer.trailer._start('http://x/yt/abc', object(), 'Dune') == 'stalled'
    assert busy.entered == 0


def test_the_start_wait_ends_on_kodi_quitting(trailer):
    class _Quitting(_Monitor):
        def waitForAbort(self, seconds):
            return True

    assert trailer.trailer._wait_rolling(_Player([0.0, 1.0]), _Quitting(), timeout=5, tick=0) == 'abort'


def test_the_start_wait_rides_out_a_player_that_errors(trailer, monkeypatch):
    import time

    ticks = iter(range(100))
    monkeypatch.setattr(time, 'monotonic', lambda: float(next(ticks)))

    class _Flaky(_Player):
        def isPlayingVideo(self):
            raise RuntimeError('not yet')

    assert trailer.trailer._wait_rolling(_Flaky([0.0]), _Monitor(), timeout=5, tick=0) == 'stalled'


def test_the_start_wait_ignores_a_player_not_yet_on_video(trailer, monkeypatch):
    import time

    ticks = iter(range(100))
    monkeypatch.setattr(time, 'monotonic', lambda: float(next(ticks)))
    assert trailer.trailer._wait_rolling(_Player([]), _Monitor(), timeout=5, tick=0) == 'stalled'


# --- stopping -------------------------------------------------------------------


def test_stop_waits_for_the_player_to_close(trailer):
    class _Slow(_Player):
        def __init__(self):
            super().__init__([0.0])
            self.polls = 0

        def isPlaying(self):
            self.polls += 1
            # Still up for two polls after stop().
            return self.polls <= 3

    player = _Slow()
    trailer.trailer._stop(player, _Monitor(), wait=5.0)
    assert player.stopped is True
    assert player.polls == 4


def test_stop_gives_up_after_its_wait(trailer):
    class _Stuck(_Player):
        def isPlaying(self):
            return True

    waits = []

    class _Mon(_Monitor):
        def waitForAbort(self, seconds):
            waits.append(seconds)
            return False

    player = _Stuck([0.0])
    trailer.trailer._stop(player, _Mon(), wait=1.0)
    assert player.stopped is True
    assert waits == [0.25] * 4


def test_stop_ends_on_kodi_quitting(trailer):
    class _Stuck(_Player):
        def isPlaying(self):
            return True

    class _Quitting(_Monitor):
        def waitForAbort(self, seconds):
            return True

    player = _Stuck([0.0])
    trailer.trailer._stop(player, _Quitting(), wait=5.0)
    assert player.stopped is True


def test_stop_leaves_an_idle_player_alone(trailer):
    player = _Player([0.0])
    player.playing = False
    trailer.trailer._stop(player, _Monitor())
    assert player.stopped is False


def test_stop_survives_a_player_that_errors(trailer):
    class _Gone(_Player):
        def isPlaying(self):
            raise RuntimeError('player gone')

    class _GoneLater(_Player):
        def __init__(self):
            super().__init__([0.0])
            self.calls = 0

        def isPlaying(self):
            self.calls += 1
            if self.calls > 1:
                raise RuntimeError('player gone')
            return True

    trailer.trailer._stop(_Gone([0.0]), _Monitor())
    later = _GoneLater()
    trailer.trailer._stop(later, _Monitor())
    assert later.stopped is True


def test_watching_when_already_full_screen_does_not_switch_again(trailer, monkeypatch):
    import xbmc

    import lib.ui.streamswindow as streamswindow
    import lib.ui.uicommon as uicommon

    calls = []
    monkeypatch.setattr(uicommon, 'close_windows_for_playback', lambda: calls.append('close'))
    monkeypatch.setattr(uicommon, 'end_player_phase', lambda: calls.append('end'))
    monkeypatch.setattr(xbmc, 'getCondVisibility', lambda cond: cond == 'Window.IsActive(fullscreenvideo)')

    def _wait(start_timeout):
        raise RuntimeError('player died')

    monkeypatch.setattr(streamswindow, '_wait_for_playback_end', _wait)
    with pytest.raises(RuntimeError):
        trailer.trailer._watch()
    # The player phase still ends when the wait blows up.
    assert calls == ['close', 'end']
    assert 'ActivateWindow(fullscreenvideo)' not in trailer.env.executed_builtins


# --- the YouTube add-on offer ---------------------------------------------------


def _offer(trailer, monkeypatch, installed, confirm):
    import xbmc

    import lib.ui.dialogs as dialogs

    told = []
    monkeypatch.setattr(xbmc, 'getCondVisibility', lambda cond: installed)
    monkeypatch.setattr(dialogs, 'confirm', confirm)
    trailer.trailer._offer_youtube_addon(lambda sid: 'STR%d' % sid, told.append)
    return told


def test_an_installed_addon_that_still_fails_is_reported(trailer, monkeypatch):
    def _never(*args):
        raise AssertionError('not asked')

    told = _offer(trailer, monkeypatch, True, _never)
    assert told == ['STR30498']
    assert not any('InstallAddon' in b for b in trailer.env.executed_builtins)


def test_declining_the_addon_installs_nothing(trailer, monkeypatch):
    told = _offer(trailer, monkeypatch, False, lambda *args: False)
    assert told == []
    assert not any('InstallAddon' in b for b in trailer.env.executed_builtins)


def test_a_dialog_that_fails_counts_as_no(trailer, monkeypatch):
    def _broken(*args):
        raise RuntimeError('skin missing')

    _offer(trailer, monkeypatch, False, _broken)
    assert not any('InstallAddon' in b for b in trailer.env.executed_builtins)


def test_back_on_the_meta_spinner_is_silent(trailer, monkeypatch):
    from lib.ui import uicommon

    monkeypatch.setattr(uicommon, 'run_cancellable', lambda *args: uicommon.CANCELLED)
    assert trailer.trailer.play_trailer({'id': 'tt1'}, 'movie', 'tt1') is False
    assert trailer.env.notifications == []


def test_trailer_route_logs_redact_server_credentials(trailer, monkeypatch):
    url = 'https://user:secret@server.test/yt/abcdefghijk?token=private'
    monkeypatch.setattr(trailer.trailer, '_server_url', lambda yt: url)
    monkeypatch.setattr(trailer.trailer, '_youtube_addon_url', lambda yt: '')
    monkeypatch.setattr(trailer.trailer, '_start', lambda *args: 'stalled')
    assert trailer.trailer.play_trailer(_META) is False
    messages = [msg for msg, _ in trailer.env.log_calls if 'trailer:' in msg]
    assert len(messages) == 2
    assert all('server.test' in msg and 'user' not in msg and 'secret' not in msg
               and 'private' not in msg for msg in messages)
