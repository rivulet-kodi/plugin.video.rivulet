"""Tests for the playback-progress half of lib.service_runner: the
_RivuletPlayer subclass built by build_progress_player() -- resume seeks
(onAVStarted), local progress caching, and remote library sync (push) --
plus its pure should_push_now() and is_context_stale() helpers.
"""
import contextlib
import datetime
import sys

import pytest

import lib.service_runner as service_runner
from tests.kodistubs import install_kodi_stubs
from tests.test_service_runner import _main_env

# ===========================================================================
# should_push_now
# ===========================================================================


def test_should_push_now_true_when_final_regardless_of_timing():
    now = datetime.datetime(2020, 1, 1, 0, 0, 0)
    assert service_runner.should_push_now(now, now, final=True) is True


def test_should_push_now_true_when_never_pushed_before():
    now = datetime.datetime(2020, 1, 1, 0, 0, 0)
    assert service_runner.should_push_now(None, now, final=False) is True


def test_should_push_now_false_before_interval_elapses():
    last = datetime.datetime(2020, 1, 1, 0, 0, 0)
    now = last + datetime.timedelta(seconds=service_runner.LIBRARY_PUSH_INTERVAL_SECONDS - 1)
    assert service_runner.should_push_now(last, now, final=False) is False


def test_should_push_now_true_once_interval_elapses():
    last = datetime.datetime(2020, 1, 1, 0, 0, 0)
    now = last + datetime.timedelta(seconds=service_runner.LIBRARY_PUSH_INTERVAL_SECONDS)
    assert service_runner.should_push_now(last, now, final=False) is True


def test_should_push_now_honors_custom_interval():
    last = datetime.datetime(2020, 1, 1, 0, 0, 0)
    now = last + datetime.timedelta(seconds=10)
    assert service_runner.should_push_now(last, now, final=False, interval=10) is True
    assert service_runner.should_push_now(last, now, final=False, interval=11) is False


# ===========================================================================
# build_progress_player: the xbmc.Player subclass tracking Rivulet playback
# ===========================================================================


class _FakeProgressStore:
    """Fake `lib.store.Store` surface `build_progress_player` needs --
    controllable and inspectable without touching a real filesystem."""

    def __init__(self, now_playing=None, auth=None, resume_offset_ms=None, set_progress_error=None):
        self._now_playing = now_playing
        self._auth = auth
        self._resume_offset_ms = resume_offset_ms
        self._set_progress_error = set_progress_error
        self.progress_calls = []       # [(type, id, video_id, position_ms, duration_ms, now), ...]
        self.now_playing_sets = []     # every set_now_playing() call, in order (incl. None to clear)

    def get_now_playing(self):
        return self._now_playing

    def set_now_playing(self, context):
        self.now_playing_sets.append(context)
        self._now_playing = context

    def get_resume_offset_ms(self):
        return self._resume_offset_ms

    def set_resume_offset_ms(self, offset_ms):
        self._resume_offset_ms = offset_ms

    def get_auth(self):
        return self._auth

    def set_progress(self, content_type, content_id, video_id, position_ms, duration_ms, now):
        if self._set_progress_error is not None:
            raise self._set_progress_error
        self.progress_calls.append((content_type, content_id, video_id, position_ms, duration_ms, now))


class _FakeProgressAPI:
    """Fake `lib.stremio.api.StremioAPI` surface the progress player's
    push path needs."""

    def __init__(self, datastore_get_result=None, datastore_get_error=None, datastore_put_error=None):
        self.datastore_get_calls = []
        self.datastore_put_calls = []
        self._datastore_get_result = [] if datastore_get_result is None else datastore_get_result
        self._datastore_get_error = datastore_get_error
        self._datastore_put_error = datastore_put_error

    def datastore_get(self, auth_key, collection='libraryItem', ids=None, all=True):
        self.datastore_get_calls.append((auth_key, collection, ids, all))
        if self._datastore_get_error is not None:
            raise self._datastore_get_error
        return self._datastore_get_result

    def datastore_put(self, auth_key, changes, collection='libraryItem'):
        self.datastore_put_calls.append((auth_key, collection, list(changes)))
        if self._datastore_put_error is not None:
            raise self._datastore_put_error


_CONTEXT = {
    'type': 'movie', 'id': 'tt1', 'video_id': None,
    'name': 'A Movie', 'poster': None, 'started_at': service_runner.library.iso8601_utc(),
}


@contextlib.contextmanager
def _progress_player_env(store, api, sync_enabled=True):
    """Builds one `build_progress_player()` instance against the shared
    fake `xbmc` module, with a plain list-based log recorder (`logs`)
    instead of a real `xbmc.log()` -- no full `main()` loop involved."""
    with install_kodi_stubs(reload=()) as ctx:
        xbmc_mod = sys.modules['xbmc']
        logs = []

        def log_fn(level, message):
            logs.append((level, message))

        player = service_runner.build_progress_player(
            xbmc_mod, store, api, log_fn, lambda: sync_enabled,
        )
        yield ctx.env, player, logs


def test_main_wires_sync_progress_through_pure_setting_bool(monkeypatch, tmp_path):
    """main()'s `sync_enabled_fn` passed to `build_progress_player()` must
    go through `lib.settings.setting_bool()` -- proven by mutating the raw
    `sync_progress` setting string to malformed/mixed-case values after
    main() wires the closure and checking each one matches what
    `lib.settings.setting_bool()` itself documents (never
    `addon.getSettingBool()`, which would coerce a malformed string to
    `False` instead of falling back to the True default)."""
    captured = {}

    def fake_build_progress_player(xbmc_module, store, api, log_fn, sync_enabled_fn):
        captured['sync_enabled_fn'] = sync_enabled_fn
        return object()

    monkeypatch.setattr(service_runner, 'build_progress_player', fake_build_progress_player)

    with _main_env(tmp_path, waitforabort=None, settings={'server_enable': True, 'sync_progress': True}) as ctx:
        ctx.xbmc.Monitor.abortRequested = lambda self: True
        service_runner.main()  # returns immediately; wires build_progress_player first

        sync_enabled_fn = captured['sync_enabled_fn']
        addon = ctx.env.addon

        addon.settings['sync_progress'] = 'not-a-bool'
        assert sync_enabled_fn() is True  # malformed falls back to the documented True default

        addon.settings['sync_progress'] = 'FALSE'
        assert sync_enabled_fn() is False  # mixed-case synonym still parses

        addon.settings['sync_progress'] = 'On'
        assert sync_enabled_fn() is True



# --- is_context_stale: pure staleness check ---------------------------------


@pytest.mark.parametrize('started_at', [None, '', 'not-a-timestamp', 12345])
def test_is_context_stale_true_when_started_at_missing_or_malformed(started_at):
    now = datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)
    assert service_runner.is_context_stale(started_at, now, max_age_seconds=60) is True


def test_is_context_stale_true_once_older_than_max_age():
    started_at = '2020-01-01T00:00:00Z'
    now = datetime.datetime(2020, 1, 1, 0, 1, 1, tzinfo=datetime.timezone.utc)  # 61s later
    assert service_runner.is_context_stale(started_at, now, max_age_seconds=60) is True


def test_is_context_stale_false_at_exactly_max_age_boundary():
    started_at = '2020-01-01T00:00:00Z'
    now = datetime.datetime(2020, 1, 1, 0, 1, 0, tzinfo=datetime.timezone.utc)  # exactly 60s later
    assert service_runner.is_context_stale(started_at, now, max_age_seconds=60) is False


def test_is_context_stale_false_when_within_max_age():
    started_at = '2020-01-01T00:00:00Z'
    now = datetime.datetime(2020, 1, 1, 0, 0, 30, tzinfo=datetime.timezone.utc)
    assert service_runner.is_context_stale(started_at, now, max_age_seconds=60) is False


def test_is_context_stale_uses_module_default_max_age_when_omitted():
    started_at = '2020-01-01T00:00:00Z'
    now = (
        datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)
        + datetime.timedelta(seconds=service_runner.MAX_STARTUP_AGE_SECONDS + 1)
    )
    assert service_runner.is_context_stale(started_at, now) is True


# --- onAVStarted: one-shot resume seek --------------------------------------


def test_onavstarted_seeks_when_resume_offset_queued_for_active_context():
    store = _FakeProgressStore(now_playing=_CONTEXT, resume_offset_ms=45000)
    with _progress_player_env(store, _FakeProgressAPI(), sync_enabled=False) as (env, player, logs):
        player.onAVStarted()
    assert env.player_seek_calls == [45.0]
    assert store.get_resume_offset_ms() is None  # consumed exactly once




def test_onavstarted_clears_resume_offset_so_it_never_reseeks_twice():
    store = _FakeProgressStore(now_playing=_CONTEXT, resume_offset_ms=45000)
    with _progress_player_env(store, _FakeProgressAPI(), sync_enabled=False) as (env, player, logs):
        player.onAVStarted()
        player.onAVStarted()
    assert env.player_seek_calls == [45.0]  # only once


def test_onavstarted_noop_when_no_rivulet_context_active():
    """Kodi fires onAVStarted for ANY playback, not just Rivulet's --
    a queued resume offset must not leak into unrelated playback."""
    store = _FakeProgressStore(now_playing=None, resume_offset_ms=45000)
    with _progress_player_env(store, _FakeProgressAPI(), sync_enabled=False) as (env, player, logs):
        player.onAVStarted()
    assert env.player_seek_calls == []
    assert store.get_resume_offset_ms() == 45000  # left untouched


def test_onavstarted_noop_when_no_resume_offset_queued():
    store = _FakeProgressStore(now_playing=_CONTEXT, resume_offset_ms=None)
    with _progress_player_env(store, _FakeProgressAPI(), sync_enabled=False) as (env, player, logs):
        player.onAVStarted()
    assert env.player_seek_calls == []


@pytest.mark.parametrize('started_at', [
    '2000-01-01T00:00:00Z',  # far older than MAX_STARTUP_AGE_SECONDS
    'not-a-timestamp',       # malformed
    None,                    # missing
])
def test_onavstarted_clears_stale_or_malformed_context_instead_of_seeking(started_at):
    stale_context = dict(_CONTEXT, started_at=started_at)
    store = _FakeProgressStore(now_playing=stale_context, resume_offset_ms=45000)
    with _progress_player_env(store, _FakeProgressAPI(), sync_enabled=False) as (env, player, logs):
        player.onAVStarted()
    assert env.player_seek_calls == []  # never accepted, so never seeks
    assert store.get_now_playing() is None
    assert store.get_resume_offset_ms() is None


# --- sample_if_playing / local progress cache (ms conversion) --------------


def test_sample_if_playing_writes_local_progress_cache_with_ms_conversion():
    store = _FakeProgressStore(now_playing=_CONTEXT)
    with _progress_player_env(store, _FakeProgressAPI(), sync_enabled=False) as (env, player, logs):
        player.onAVStarted()  # accept the context so sample_if_playing() actually flushes
        env.player_is_playing = True
        env.player_get_time = 12.5
        env.player_get_total_time = 100.0
        player.sample_if_playing()
    assert store.progress_calls == [('movie', 'tt1', None, 12500, 100000, store.progress_calls[0][5])]


def test_sample_if_playing_noop_when_not_playing_video():
    store = _FakeProgressStore(now_playing=_CONTEXT)
    with _progress_player_env(store, _FakeProgressAPI(), sync_enabled=False) as (env, player, logs):
        env.player_is_playing = False
        player.sample_if_playing()
    assert store.progress_calls == []


def test_sample_if_playing_noop_when_no_rivulet_context_active():
    store = _FakeProgressStore(now_playing=None)
    with _progress_player_env(store, _FakeProgressAPI(), sync_enabled=False) as (env, player, logs):
        env.player_is_playing = True
        env.player_get_time = 10.0
        env.player_get_total_time = 100.0
        player.sample_if_playing()
    assert store.progress_calls == []


def test_sample_if_playing_skips_zero_duration_sample():
    store = _FakeProgressStore(now_playing=_CONTEXT)
    with _progress_player_env(store, _FakeProgressAPI(), sync_enabled=False) as (env, player, logs):
        player.onAVStarted()  # accept the context so sample_if_playing() actually flushes
        env.player_is_playing = True
        env.player_get_time = 0.0
        env.player_get_total_time = 0.0
        player.sample_if_playing()
    assert store.progress_calls == []


def test_sample_if_playing_rejects_stale_unaccepted_persisted_context():
    """A context this Player instance never accepted via onAVStarted --
    e.g. a crashed previous session's leftover now_playing.json -- must
    not be sampled once its started_at is stale, and must be cleared so
    it can never leak into a later unrelated video."""
    stale_context = dict(_CONTEXT, started_at='2000-01-01T00:00:00Z')
    store = _FakeProgressStore(now_playing=stale_context, resume_offset_ms=45000)
    with _progress_player_env(store, _FakeProgressAPI(), sync_enabled=False) as (env, player, logs):
        env.player_is_playing = True
        env.player_get_time = 10.0
        env.player_get_total_time = 100.0
        player.sample_if_playing()
    assert store.progress_calls == []
    assert store.get_now_playing() is None
    assert store.get_resume_offset_ms() is None


def test_sample_if_playing_preserves_accepted_context_regardless_of_started_at_age(monkeypatch):
    """Once onAVStarted() has accepted a context, sample_if_playing() must
    keep sampling it for the rest of a long playback no matter how old
    started_at looks by wall-clock time -- proven by making the
    staleness check itself always report "stale" and showing the
    already-accepted context is still sampled regardless."""
    store = _FakeProgressStore(now_playing=_CONTEXT)
    with _progress_player_env(store, _FakeProgressAPI(), sync_enabled=False) as (env, player, logs):
        player.onAVStarted()  # accepts the fresh context
        monkeypatch.setattr(service_runner, 'is_context_stale', lambda *a, **kw: True)
        env.player_is_playing = True
        env.player_get_time = 500.0
        env.player_get_total_time = 1000.0
        player.sample_if_playing()
    assert store.progress_calls == [('movie', 'tt1', None, 500000, 1000000, store.progress_calls[0][5])]
    assert store.get_now_playing() is not None



def test_sample_if_playing_waits_for_onavstarted_on_fresh_unaccepted_context():
    """A freshly-written context that onAVStarted has not yet accepted --
    e.g. Kodi is still opening the resolved URL -- must be left
    completely alone: no local flush, no remote push, and no premature
    clearing, until onAVStarted actually accepts it or it goes stale."""
    store = _FakeProgressStore(now_playing=_CONTEXT, auth={'authKey': 'tok'})
    api = _FakeProgressAPI()
    with _progress_player_env(store, api, sync_enabled=True) as (env, player, logs):
        env.player_is_playing = True
        env.player_get_time = 10.0
        env.player_get_total_time = 100.0
        player.sample_if_playing()
    assert store.progress_calls == []
    assert api.datastore_put_calls == []
    assert store.get_now_playing() is not None  # not cleared -- still fresh, just not yet accepted


# --- onPlayBackStopped/onPlayBackEnded: final flush + context clear --------


def test_onplaybackstopped_flushes_local_cache_and_clears_context():
    store = _FakeProgressStore(now_playing=_CONTEXT)
    with _progress_player_env(store, _FakeProgressAPI(), sync_enabled=False) as (env, player, logs):
        player.onAVStarted()  # accept the context so the stop below actually flushes
        env.player_get_time = 50.0
        env.player_get_total_time = 100.0
        player.onPlayBackStopped()
    assert store.progress_calls == [('movie', 'tt1', None, 50000, 100000, store.progress_calls[0][5])]
    assert store.get_now_playing() is None


def test_onplaybackended_flushes_local_cache_and_clears_context():
    store = _FakeProgressStore(now_playing=_CONTEXT)
    with _progress_player_env(store, _FakeProgressAPI(), sync_enabled=False) as (env, player, logs):
        player.onAVStarted()  # accept the context so the end below actually flushes
        env.player_get_time = 99.0
        env.player_get_total_time = 100.0
        player.onPlayBackEnded()
    assert store.progress_calls == [('movie', 'tt1', None, 99000, 100000, store.progress_calls[0][5])]
    assert store.get_now_playing() is None


def test_onplaybackstopped_noop_when_no_rivulet_context_active():
    store = _FakeProgressStore(now_playing=None)
    with _progress_player_env(store, _FakeProgressAPI(), sync_enabled=False) as (env, player, logs):
        player.onPlayBackStopped()
    assert store.progress_calls == []


def test_onplaybackerror_before_av_started_clears_context_and_resume_offset():
    """A resolved stream that fails before Kodi ever reaches AV start
    (dead/expired link, unsupported codec) fires ONLY onPlayBackError,
    never onAVStarted/onPlayBackStopped/onPlayBackEnded -- the queued
    resume offset and now-playing context must still be cleared, and the
    never-accepted context must never be locally flushed or pushed to
    the remote library, so a later unrelated video can't inherit
    either."""
    store = _FakeProgressStore(now_playing=_CONTEXT, resume_offset_ms=45000, auth={'authKey': 'tok'})
    api = _FakeProgressAPI()
    with _progress_player_env(store, api, sync_enabled=True) as (env, player, logs):
        env.player_get_time = 50.0
        env.player_get_total_time = 100.0
        player.onPlayBackError()
    assert store.get_now_playing() is None
    assert store.get_resume_offset_ms() is None
    assert store.progress_calls == []  # never accepted -> no local flush
    assert api.datastore_put_calls == []  # never accepted -> no remote push


def test_onplaybackerror_flush_failure_still_clears_context_and_resume_offset():
    """The final flush is best-effort: a sampling failure (e.g. a broken
    local progress-cache write) must never prevent the unconditional
    now-playing/resume-offset cleanup below it."""
    store = _FakeProgressStore(
        now_playing=_CONTEXT, resume_offset_ms=45000, set_progress_error=OSError('disk full'),
    )
    with _progress_player_env(store, _FakeProgressAPI(), sync_enabled=False) as (env, player, logs):
        player.onAVStarted()  # accept the context so the error path below actually attempts a flush
        env.player_get_time = 50.0
        env.player_get_total_time = 100.0
        player.onPlayBackError()  # must not raise
    assert store.get_now_playing() is None
    assert store.get_resume_offset_ms() is None
    assert any('final flush failed' in msg for _level, msg in logs)


# --- push to the Stremio API: gating, merge, failure handling --------------


def test_push_skipped_when_sync_setting_disabled_zero_api_calls():
    store = _FakeProgressStore(now_playing=_CONTEXT, auth={'authKey': 'tok'})
    api = _FakeProgressAPI()
    with _progress_player_env(store, api, sync_enabled=False) as (env, player, logs):
        player.onAVStarted()
        env.player_is_playing = True
        env.player_get_time = 50.0
        env.player_get_total_time = 100.0
        player.sample_if_playing()
    assert api.datastore_get_calls == []
    assert api.datastore_put_calls == []
    assert store.progress_calls  # local cache still written regardless


def test_push_skipped_when_logged_out_zero_api_calls():
    """A logged-out user gets local progress/resume with ZERO API calls,
    even with sync_progress enabled."""
    store = _FakeProgressStore(now_playing=_CONTEXT, auth=None)
    api = _FakeProgressAPI()
    with _progress_player_env(store, api, sync_enabled=True) as (env, player, logs):
        player.onAVStarted()
        env.player_is_playing = True
        env.player_get_time = 50.0
        env.player_get_total_time = 100.0
        player.sample_if_playing()
    assert api.datastore_get_calls == []
    assert api.datastore_put_calls == []
    assert store.progress_calls  # local cache still written regardless




def test_push_merges_existing_remote_item_preserving_watched_bitfield():
    existing = {
        '_id': 'tt1', 'name': 'A Movie', 'type': 'movie', 'poster': None,
        'posterShape': 'poster', 'removed': False, 'temp': False,
        '_ctime': '2019-01-01T00:00:00Z', '_mtime': '2019-01-01T00:00:00Z',
        'state': {
            'lastWatched': '2019-01-01T00:00:00Z', 'timeWatched': 0, 'timeOffset': 0,
            'overallTimeWatched': 0, 'timesWatched': 0, 'flaggedWatched': 0,
            'duration': 0, 'video_id': None, 'watched': 'REAL-BITFIELD', 'noNotif': False,
        },
        'behaviorHints': {'defaultVideoId': None, 'featuredVideoId': None, 'hasScheduledVideos': False},
    }
    store = _FakeProgressStore(now_playing=_CONTEXT, auth={'authKey': 'tok'})
    api = _FakeProgressAPI(datastore_get_result=[existing])
    with _progress_player_env(store, api, sync_enabled=True) as (env, player, logs):
        player.onAVStarted()
        env.player_is_playing = True
        env.player_get_time = 50.0
        env.player_get_total_time = 100.0
        player.sample_if_playing()
    assert api.datastore_get_calls == [('tok', 'libraryItem', ['tt1'], False)]
    assert len(api.datastore_put_calls) == 1
    auth_key, collection, changes = api.datastore_put_calls[0]
    assert (auth_key, collection) == ('tok', 'libraryItem')
    assert changes[0]['state']['watched'] == 'REAL-BITFIELD'  # carried over untouched
    assert changes[0]['state']['timeOffset'] == 50000
    assert changes[0]['state']['duration'] == 100000


def test_push_builds_fresh_item_when_no_existing_remote_item():
    store = _FakeProgressStore(now_playing=_CONTEXT, auth={'authKey': 'tok'})
    api = _FakeProgressAPI(datastore_get_result=[])
    with _progress_player_env(store, api, sync_enabled=True) as (env, player, logs):
        player.onAVStarted()
        env.player_is_playing = True
        env.player_get_time = 50.0
        env.player_get_total_time = 100.0
        player.sample_if_playing()
    changes = api.datastore_put_calls[0][2]
    assert changes[0]['_id'] == 'tt1'
    assert changes[0]['state']['watched'] is None


def test_push_failure_is_logged_and_local_cache_still_written():
    store = _FakeProgressStore(now_playing=_CONTEXT, auth={'authKey': 'tok'})
    api = _FakeProgressAPI(datastore_get_error=RuntimeError('network down'))
    with _progress_player_env(store, api, sync_enabled=True) as (env, player, logs):
        player.onAVStarted()
        env.player_is_playing = True
        env.player_get_time = 50.0
        env.player_get_total_time = 100.0
        player.sample_if_playing()  # must not raise
    assert store.progress_calls  # local cache written before the push attempt
    assert api.datastore_put_calls == []
    assert any('library push failed' in msg for _level, msg in logs)


def test_push_throttled_between_consecutive_samples():
    store = _FakeProgressStore(now_playing=_CONTEXT, auth={'authKey': 'tok'})
    api = _FakeProgressAPI()
    with _progress_player_env(store, api, sync_enabled=True) as (env, player, logs):
        player.onAVStarted()
        env.player_is_playing = True
        env.player_get_time = 10.0
        env.player_get_total_time = 100.0
        player.sample_if_playing()
        player.sample_if_playing()
    assert len(store.progress_calls) == 1  # local write also throttled on the second sample
    assert len(api.datastore_put_calls) == 1  # push throttled on the second sample




def test_final_flush_bypasses_the_push_throttle():
    store = _FakeProgressStore(now_playing=_CONTEXT, auth={'authKey': 'tok'})
    api = _FakeProgressAPI()
    with _progress_player_env(store, api, sync_enabled=True) as (env, player, logs):
        player.onAVStarted()
        env.player_is_playing = True
        env.player_get_time = 10.0
        env.player_get_total_time = 100.0
        player.sample_if_playing()
        player.onPlayBackStopped()
    assert len(api.datastore_put_calls) == 2  # the final flush always pushes


# --- local progress-cache write cadence (bounded write cost) ---------------


def test_local_progress_write_throttled_between_consecutive_samples():
    store = _FakeProgressStore(now_playing=_CONTEXT)
    with _progress_player_env(store, _FakeProgressAPI(), sync_enabled=False) as (env, player, logs):
        player.onAVStarted()
        env.player_is_playing = True
        env.player_get_time = 10.0
        env.player_get_total_time = 100.0
        player.sample_if_playing()
        player.sample_if_playing()
    assert len(store.progress_calls) == 1  # second sample arrives well within the write interval


def test_final_flush_bypasses_local_progress_write_throttle():
    store = _FakeProgressStore(now_playing=_CONTEXT)
    with _progress_player_env(store, _FakeProgressAPI(), sync_enabled=False) as (env, player, logs):
        player.onAVStarted()
        env.player_is_playing = True
        env.player_get_time = 10.0
        env.player_get_total_time = 100.0
        player.sample_if_playing()   # first local write
        player.onPlayBackStopped()   # final flush must persist regardless of cadence
    assert len(store.progress_calls) == 2


def test_onavstarted_accepting_new_context_resets_local_write_cadence():
    """Kodi fires `onAVStarted` for every new video, not only after this
    Player instance's previous `onPlayBackStopped`/`onPlayBackEnded` ran
    -- so accepting a brand-new context must reset the local-write
    cadence, otherwise its first sample could be silently suppressed by
    cadence state left over from the PREVIOUS session."""
    store = _FakeProgressStore(now_playing=_CONTEXT)
    with _progress_player_env(store, _FakeProgressAPI(), sync_enabled=False) as (env, player, logs):
        player.onAVStarted()  # accepts context A
        env.player_is_playing = True
        env.player_get_time = 10.0
        env.player_get_total_time = 100.0
        player.sample_if_playing()  # writes A's first sample

        other_context = dict(_CONTEXT, id='tt2', started_at=service_runner.library.iso8601_utc())
        store.set_now_playing(other_context)
        player.onAVStarted()  # accepts context B without an intervening terminate

        env.player_get_time = 20.0
        env.player_get_total_time = 200.0
        player.sample_if_playing()  # B's first sample must not be throttled
    assert len(store.progress_calls) == 2
    assert store.progress_calls[-1][:2] == ('movie', 'tt2')


def test_terminate_resets_local_write_cadence_for_next_session():
    store = _FakeProgressStore(now_playing=_CONTEXT)
    with _progress_player_env(store, _FakeProgressAPI(), sync_enabled=False) as (env, player, logs):
        player.onAVStarted()  # accepts context A
        env.player_is_playing = True
        env.player_get_time = 10.0
        env.player_get_total_time = 100.0
        player.sample_if_playing()   # A's first (and only) local write
        player.onPlayBackStopped()   # terminate: final flush + cadence reset

        other_context = dict(_CONTEXT, id='tt2', started_at=service_runner.library.iso8601_utc())
        store.set_now_playing(other_context)
        player.onAVStarted()  # accepts context B
        env.player_get_time = 20.0
        env.player_get_total_time = 200.0
        player.sample_if_playing()   # B's first sample must not be throttled
    # A: 1 sample write + 1 final-flush write; B: 1 more write.
    assert len(store.progress_calls) == 3
    assert store.progress_calls[-1][:2] == ('movie', 'tt2')

