# SPDX-FileCopyrightText: 2026 M0Rf30
# SPDX-License-Identifier: MIT

"""A live channel through the service's progress player: stremio-core's
player gives a live item no timeline (src/models/player.rs), so it never
writes local progress, never flags watched, and records `lastWatched`
(account) or a local recent-channels entry (no sync).
"""
import lib.service_runner as service_runner
from tests.test_service_runner_player import (
    _FakeProgressAPI,
    _FakeProgressStore,
    _progress_player_env,
)


class _LiveStore(_FakeProgressStore):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.recent = []

    def note_recent_channel(self, meta, when_iso=None):
        self.recent.append(dict(meta))
        return True


def _context(**extra):
    context = {
        'type': 'tv', 'id': 'iptv:ch1', 'video_id': None, 'name': 'Channel 1',
        'poster': 'http://x/logo.png', 'posterShape': 'square', 'live': True,
        'started_at': service_runner.library.iso8601_utc(),
    }
    context.update(extra)
    return context


def _play(player, env, total=0.0):
    player.onAVStarted()
    env.player_is_playing = True
    env.player_get_time = 3600.0
    env.player_get_total_time = total
    player.sample_if_playing()


def test_live_playback_pushes_last_watched_even_without_a_duration():
    store = _LiveStore(now_playing=_context(), auth={'authKey': 'tok'})
    api = _FakeProgressAPI()
    with _progress_player_env(store, api, sync_enabled=True) as (env, player, _logs):
        _play(player, env)
    assert len(api.datastore_put_calls) == 1
    record = api.datastore_put_calls[0][2][0]
    state = record['state']
    assert record['_id'] == 'iptv:ch1' and record['behaviorHints']['isLive'] is True
    assert record['posterShape'] == 'square'
    assert state['video_id'] == 'iptv:ch1' and state['timeOffset'] == 0
    assert state['lastWatched'] and state['flaggedWatched'] == 0 and state['timesWatched'] == 0
    assert state['timeWatched'] == 0 and state['duration'] == 0
    assert store.progress_calls == [] and store.recent == []


def test_live_playback_never_writes_local_progress_even_with_a_reported_duration():
    store = _LiveStore(now_playing=_context(), auth=None)
    with _progress_player_env(store, _FakeProgressAPI(), sync_enabled=False) as (env, player, _logs):
        _play(player, env, total=7200.0)
        player.onPlayBackStopped()
    assert store.progress_calls == []


def test_live_playback_without_sync_records_a_recent_channel_locally():
    store = _LiveStore(now_playing=_context(), auth={'authKey': 'tok'})
    api = _FakeProgressAPI()
    with _progress_player_env(store, api, sync_enabled=False) as (env, player, _logs):
        _play(player, env)
    assert api.datastore_put_calls == []
    assert [r['id'] for r in store.recent] == ['iptv:ch1']
    assert store.recent[0]['posterShape'] == 'square'


def test_live_playback_signed_out_records_a_recent_channel_locally():
    store = _LiveStore(now_playing=_context(), auth=None)
    with _progress_player_env(store, _FakeProgressAPI(), sync_enabled=True) as (env, player, _logs):
        _play(player, env)
    assert [r['id'] for r in store.recent] == ['iptv:ch1']


def test_live_stop_flushes_a_final_push_without_any_sample():
    store = _LiveStore(now_playing=_context(), auth={'authKey': 'tok'})
    api = _FakeProgressAPI()
    with _progress_player_env(store, api, sync_enabled=True) as (env, player, _logs):
        _play(player, env)
        player.onPlayBackStopped()
    # first sample + the final flush (within the interval, but `final` always pushes)
    assert len(api.datastore_put_calls) == 2


def test_second_live_playback_pushes_at_once_not_a_push_interval_later():
    store = _LiveStore(now_playing=_context(), auth={'authKey': 'tok'})
    api = _FakeProgressAPI()
    with _progress_player_env(store, api, sync_enabled=True) as (env, player, _logs):
        _play(player, env)
        env.player_is_playing = False
        player._terminate()
        store.set_now_playing(_context(id='iptv:ch2'))
        before = len(api.datastore_put_calls)
        _play(player, env)
    assert len(api.datastore_put_calls) == before + 1
    assert api.datastore_put_calls[-1][2][0]['_id'] == 'iptv:ch2'


def test_non_live_playback_is_unchanged_by_the_live_path():
    context = {'type': 'movie', 'id': 'tt1', 'video_id': None, 'name': 'M', 'poster': None,
               'started_at': service_runner.library.iso8601_utc()}
    store = _LiveStore(now_playing=context, auth=None)
    with _progress_player_env(store, _FakeProgressAPI(), sync_enabled=False) as (env, player, _logs):
        _play(player, env, total=100.0)
    assert len(store.progress_calls) == 1 and store.recent == []
