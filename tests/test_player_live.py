# SPDX-FileCopyrightText: 2026 M0Rf30
# SPDX-License-Identifier: MIT

# ruff: noqa: F811
"""Playing a live channel through lib.ui.player: no resume prompt, a
`live` now-playing context, and the now/next programme lines on the
playing ListItem (stremio-core's player gives a live item no timeline,
src/models/player.rs).
"""
import time

import pytest

from lib.ui import playbackmeta
from tests.test_player_buffer import _resolved_one, _ServerScript, kodi_stubs  # noqa: F401 - fixtures
from tests.test_player_callbacks import _FakeProgressStore, _install_progress_store

NOW = 1_800_000_000  # 2027-01-15 08:00:00 UTC


@pytest.fixture
def utc(monkeypatch):
    if not hasattr(time, 'tzset'):
        pytest.skip('needs time.tzset')
    monkeypatch.setenv('TZ', 'UTC')
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def _iso(epoch):
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(epoch))


def _videos():
    return [
        {'id': 'p1', 'title': 'News', 'startTime': _iso(NOW - 600), 'endTime': _iso(NOW + 1800)},
        {'id': 'p2', 'title': 'Film', 'startTime': _iso(NOW + 1800), 'endTime': _iso(NOW + 7200)},
    ]


def _channel_meta(**extra):
    meta = {'id': 'iptv:ch1', 'type': 'tv', 'name': 'Channel 1', 'posterShape': 'square',
            'behaviorHints': {'isLive': True}, 'videos': _videos()}
    meta.update(extra)
    return meta


# --- pure formatter ---------------------------------------------------------


def test_live_programme_lines_format_now_with_span_and_next_with_start(utc):
    now_next = {
        'current': {'title': 'News', 'start': NOW - 600, 'end': NOW + 1800, 'overview': '', 'progress': 25},
        'next': {'title': 'Film', 'start': NOW + 1800, 'end': NOW + 7200},
    }
    lines = playbackmeta.live_programme_lines(now_next, lambda t: 'Now: ' + t, lambda t: 'Next: ' + t)
    assert lines == ['Now: 07:50 - 08:30 News', 'Next: 08:30 Film']


def test_live_programme_lines_follow_a_12_hour_region_without_leading_zero(utc):
    now_next = {'current': None, 'next': {'title': 'Film', 'start': NOW + 1800, 'end': NOW + 7200}}
    lines = playbackmeta.live_programme_lines(now_next, str, str, '%I:%M:%S %p')
    assert lines == ['8:30 AM Film']


def test_live_programme_lines_handle_missing_parts(utc):
    assert playbackmeta.live_programme_lines(None, str, str) == []
    assert playbackmeta.live_programme_lines({'current': None, 'next': None}, str, str) == []
    only_now = {'current': {'title': '', 'start': NOW, 'end': NOW + 60}, 'next': None}
    assert playbackmeta.live_programme_lines(only_now, str, str) == ['08:00 - 08:01']


# --- playback ------------------------------------------------------------------


def test_live_item_records_a_live_context_and_never_asks_to_resume(kodi_stubs, monkeypatch):
    store = _FakeProgressStore(progress={'position_ms': 50000, 'duration_ms': 100000})
    _install_progress_store(monkeypatch, kodi_stubs.player, store)
    _ServerScript(resolve_url='http://server/x/0').install(monkeypatch, kodi_stubs.player)

    kodi_stubs.player.play_direct(
        {'url': 'http://server/x/0'}, 'tv', 'iptv:ch1',
        item_meta={'label': 'Channel 1', 'meta': _channel_meta()},
    )

    assert store.now_playing['live'] is True
    assert store.now_playing['id'] == 'iptv:ch1' and store.now_playing['name'] == 'Channel 1'
    assert store.now_playing['posterShape'] == 'square'
    assert store.get_progress_calls == []  # local progress is never even looked at
    assert kodi_stubs.env.dialog_yesno_prompts == []
    assert store.resume_offset_ms is None


def test_hinted_live_item_of_another_type_is_live_too(kodi_stubs, monkeypatch):
    store = _FakeProgressStore()
    _install_progress_store(monkeypatch, kodi_stubs.player, store)
    _ServerScript(resolve_url='http://server/x/0').install(monkeypatch, kodi_stubs.player)
    kodi_stubs.player.play_direct(
        {'url': 'http://server/x/0'}, 'movie', 'iptv:ch1',
        item_meta={'meta': _channel_meta(type='movie')},
    )
    assert store.now_playing['live'] is True


def test_a_movie_context_has_no_live_flag(kodi_stubs, monkeypatch):
    store = _FakeProgressStore()
    _install_progress_store(monkeypatch, kodi_stubs.player, store)
    _ServerScript(resolve_url='http://server/x/0').install(monkeypatch, kodi_stubs.player)
    kodi_stubs.player.play_direct({'url': 'http://server/x/0'}, 'movie', 'tt1', item_meta={'meta': {'name': 'M'}})
    assert 'live' not in store.now_playing and 'posterShape' not in store.now_playing


def test_live_item_shows_the_channel_title_and_now_next_lines(kodi_stubs, monkeypatch, utc):
    env = kodi_stubs.env
    env.addon.settings['buffer_enable'] = False
    _ServerScript(resolve_url='http://server/x/0').install(monkeypatch, kodi_stubs.player)
    monkeypatch.setattr(kodi_stubs.player.time, 'time', lambda: NOW)

    kodi_stubs.player.play(81, {'url': 'http://server/x/0'}, 'tv', 'iptv:ch1',
                           item_meta={'label': 'Channel 1', 'meta': _channel_meta(description='About')})

    _handle, _succeeded, list_item = _resolved_one(env)
    info = list_item.legacy_info
    assert list_item.getLabel() == 'Channel 1' and info['title'] == 'Channel 1'
    lines = info['plot'].split('\n')
    assert len(lines) == 2 and 'News' in lines[0] and '07:50' in lines[0] and 'Film' in lines[1]


def test_live_item_without_a_schedule_keeps_the_usual_plot(kodi_stubs, monkeypatch):
    env = kodi_stubs.env
    env.addon.settings['buffer_enable'] = False
    _ServerScript(resolve_url='http://server/x/0').install(monkeypatch, kodi_stubs.player)
    kodi_stubs.player.play(82, {'url': 'http://server/x/0'}, 'tv', 'iptv:ch1',
                           item_meta={'label': 'Channel 1', 'meta': _channel_meta(videos=[], description='About')})
    _handle, _succeeded, list_item = _resolved_one(env)
    assert list_item.legacy_info['plot'] == 'About'
