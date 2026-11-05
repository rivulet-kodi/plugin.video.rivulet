# SPDX-FileCopyrightText: 2026 M0Rf30
# SPDX-License-Identifier: MIT

"""Direct tests of lib.service_runner.ServiceMonitor / LoopState /
Supervisor with every Kodi dependency injected (no xbmc stubs)."""
import pytest

import lib.libserver as libserver
import lib.serverbin as serverbin
import lib.service_runner as sr


class FakeAddon:
    def __init__(self, settings=None):
        self.settings = dict(settings or {})

    def getSetting(self, key):
        value = self.settings.get(key, "")
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)


class FakeSettings:
    @staticmethod
    def setting_bool(addon, key, default=False):
        raw = addon.settings.get(key)
        return default if raw is None else bool(raw)

    @staticmethod
    def setting_int(addon, key, default=0):
        raw = addon.settings.get(key)
        return default if raw is None else int(raw)


class Proc:
    def __init__(self, polls=(), uptime=0, stop_error=None):
        self.polls = list(polls)
        self._uptime = uptime
        self.stop_error = stop_error
        self.stops = 0
        self.rotations = 0

    def poll(self):
        return self.polls.pop(0) if self.polls else None

    def uptime(self):
        return self._uptime

    def maybe_rotate_log(self):
        self.rotations += 1

    def stop(self):
        self.stops += 1
        if self.stop_error:
            raise self.stop_error


class Monitor(sr.ServiceMonitor):
    def __init__(self, addon, abort_after=1):
        super().__init__(addon, FakeSettings)
        self.waits = []
        self._abort_after = abort_after

    def abortRequested(self):
        return False

    def waitForAbort(self, timeout=None):
        self.waits.append(timeout)
        return len(self.waits) >= self._abort_after


class Autoload:
    def __init__(self, interval=None, error=None):
        self.fired = False
        self.interval = interval
        self.error = error

    def poll(self, now):
        if self.error:
            raise self.error
        return self.interval


class Player:
    def __init__(self, error=None, sampling=False):
        self.error = error
        self.samples = 0
        self.sampling = sampling

    def is_sampling(self):
        return self.sampling

    def sample_if_playing(self):
        self.samples += 1
        if self.error:
            raise self.error


def make(tmp_path, settings=None, abort_after=1, autoload=None, player=None, clock=None, sync_iptv_fn=None):
    logs, notes, events = [], [], []
    monitor = Monitor(FakeAddon(settings or {"server_enable": True}), abort_after)
    sup = sr.Supervisor(
        monitor, str(tmp_path),
        lambda level, msg: logs.append((level, msg)),
        sr.LogLevels("I", "W", "E"),
        lambda sid, error=False: notes.append((sid, error)),
        player or Player(),
        lambda: events.append("sync"),
        lambda: events.append("bridge-shutdown"),
        autoload or Autoload(),
        clock=clock or (lambda: 100.0),
        sync_iptv_fn=sync_iptv_fn,
    )
    sup.logs, sup.notes, sup.events = logs, notes, events
    return sup


@pytest.fixture(autouse=True)
def _unsliced_sleep(monkeypatch):
    """These tests script `waitForAbort()` by call count, one call per
    supervision interval; slicing (see `test_supervisor.py`'s
    `test_sleep_*`) would turn each interval into several calls."""
    monkeypatch.setattr(sr, "SLEEP_SLICE_SECONDS", float("inf"))


@pytest.fixture(autouse=True)
def _no_probe(monkeypatch):
    monkeypatch.setattr(sr, "probe_listening", lambda *a, **k: False)
    monkeypatch.setattr(libserver, "LIBRARY_SUPPORTED", False)


def test_monitor_flags_restart_only_on_real_change():
    addon = FakeAddon({"server_enable": True})
    monitor = sr.ServiceMonitor(addon, FakeSettings)
    monitor.onSettingsChanged()
    assert monitor.restart_requested is False
    addon.settings["server_url"] = "http://127.0.0.1:9"
    monitor.onSettingsChanged()
    assert monitor.restart_requested is True
    assert monitor.server_url == "http://127.0.0.1:9"


def test_loopstate_reset_download():
    state = sr.LoopState()
    state.download_backoff_idx = 2
    state.next_download_at = 5
    state.download_attempt_notified = state.download_failure_notified = True
    state.reset_download()
    assert (state.download_backoff_idx, state.next_download_at) == (0, None)
    assert not state.download_attempt_notified and not state.download_failure_notified


def test_disabled_stops_running_process(tmp_path):
    sup = make(tmp_path, {"server_enable": False})
    proc = Proc()
    sup.state.proc = proc
    assert sup.dispatch(sup.state) == (sr.IDLE_POLL_INTERVAL, False)
    assert proc.stops == 1 and sup.state.proc is None
    assert sup.state.server_mode == "disabled"


def test_failed_stop_keeps_process(tmp_path):
    sup = make(tmp_path, {"server_enable": False})
    sup.state.proc = Proc(stop_error=OSError("wedged"))
    sup.dispatch(sup.state)
    assert sup.state.proc is not None
    assert any(level == "E" for level, _ in sup.logs)


def test_healthy_process_rotates_log(tmp_path):
    sup = make(tmp_path)
    sup.state.proc = Proc()
    assert sup.dispatch(sup.state) == (sr.HEALTHY_POLL_INTERVAL, False)
    assert sup.state.proc.rotations == 1 and sup.state.server_mode == "embedded"


def test_crashed_process_backs_off_and_resets_when_stable(tmp_path):
    sup = make(tmp_path)
    sup.state.proc = Proc(polls=[1])
    assert sup.dispatch(sup.state) == (sr.RESTART_BACKOFF[0], False)
    assert sup.state.backoff_idx == 1 and sup.state.proc is None
    sup.state.proc = Proc(polls=[1], uptime=sr.MIN_STABLE_UPTIME)
    assert sup.dispatch(sup.state) == (sr.RESTART_BACKOFF[0], False)


def test_external_server_is_left_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(sr, "probe_listening", lambda *a, **k: True)
    sup = make(tmp_path)
    assert sup.dispatch(sup.state) == (sr.EXTERNAL_RECHECK_INTERVAL, False)
    assert sup.state.server_mode == "external"


def test_spawns_found_binary_with_backoff_on_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(sr, "resolve_binary", lambda *a: "/x/rivulet-engine")
    monkeypatch.setattr(sr, "is_bundled_binary", lambda *a: False)
    started = []

    class Boom:
        def __init__(self, *a, **k):
            pass

        def start(self):
            started.append(1)
            raise OSError("nope")

    monkeypatch.setattr(sr, "ServerProcess", Boom)
    sup = make(tmp_path)
    assert sup.dispatch(sup.state) == (sr.RESTART_BACKOFF[0], False)
    assert sup.state.proc is None and sup.state.backoff_idx == 1


def test_missing_binary_download_success_then_failure_backoff(tmp_path, monkeypatch):
    monkeypatch.setattr(sr, "resolve_binary", lambda *a: None)
    results = [None, RuntimeError("offline")]

    def install(*a, **k):
        r = results.pop(0)
        if r:
            raise r

    monkeypatch.setattr(serverbin, "install_binary", install)
    now = [100.0]
    sup = make(tmp_path, clock=lambda: now[0])
    assert sup.dispatch(sup.state) == (sr.POST_DOWNLOAD_RECHECK_INTERVAL, False)
    assert sup.dispatch(sup.state) == (sr.MISSING_BINARY_RECHECK_INTERVAL, False)
    assert sup.state.next_download_at == 100.0 + sr.DOWNLOAD_RETRY_BACKOFF[0]
    assert (30063, True) in sup.notes
    assert sup.dispatch(sup.state) == (sr.MISSING_BINARY_RECHECK_INTERVAL, False)  # cooling down
    assert results == []


def test_download_abort_requests_break(tmp_path, monkeypatch):
    monkeypatch.setattr(sr, "resolve_binary", lambda *a: None)

    def install(*a, **k):
        raise sr._AbortRequested()

    monkeypatch.setattr(serverbin, "install_binary", install)
    sup = make(tmp_path)
    assert sup.dispatch(sup.state)[1] is True


def test_unsupported_platform_latches_and_notifies_once(tmp_path, monkeypatch):
    monkeypatch.setattr(sr, "resolve_binary", lambda *a: None)

    def install(*a, **k):
        raise serverbin.UnsupportedPlatformError("selinux")

    monkeypatch.setattr(serverbin, "install_binary", install)
    sup = make(tmp_path)
    sup.dispatch(sup.state)
    assert sup.state.unsupported_platform is True
    assert (30091, True) in sup.notes
    assert sup.dispatch(sup.state) == (sr.UNSUPPORTED_PLATFORM_POLL_INTERVAL, False)
    sup.dispatch(sup.state)
    assert sup.notes.count((30031, True)) == 1


def test_restart_request_resets_state(tmp_path):
    sup = make(tmp_path)
    proc = Proc()
    sup.state.proc = proc
    sup.state.unsupported_platform = True
    sup.state.backoff_idx = 2
    sup.monitor.restart_requested = True
    sup.tick_progress_and_restart(sup.state)
    assert proc.stops == 1 and sup.state.proc is None
    assert sup.state.backoff_idx == 0 and not sup.state.unsupported_platform
    assert sup.monitor.restart_requested is False


def test_progress_sampling_failure_is_logged_not_raised(tmp_path):
    sup = make(tmp_path, player=Player(error=RuntimeError("x")))
    sup.tick_progress_and_restart(sup.state)
    assert any("progress sampling failed" in msg for _, msg in sup.logs)


def test_run_loop_syncs_bridge_autoload_shortens_and_shuts_down(tmp_path):
    sup = make(tmp_path, {"server_enable": False}, autoload=Autoload(interval=0.5))
    sup.run()
    assert sup.monitor.waits == [0.5]
    assert sup.events == ["sync", "bridge-shutdown"]


def test_autoload_failure_latches_off(tmp_path):
    autoload = Autoload(error=RuntimeError("boom"))
    sup = make(tmp_path, {"server_enable": False}, autoload=autoload)
    sup.run()
    assert autoload.fired is True
    assert sup.monitor.waits == [sr.IDLE_POLL_INTERVAL]


def test_shutdown_survives_bridge_failure_and_stops_proc(tmp_path):
    sup = make(tmp_path)

    def bad():
        raise OSError("gone")

    sup._shutdown_bridge = bad
    proc = Proc()
    sup.state.proc = proc
    sup.shutdown()
    assert proc.stops == 1
    assert any("bridge shutdown failed" in msg for _, msg in sup.logs)


# --- IPTV add-on registration hook ----------------------------------------------


def _iptv_calls(tmp_path, settings, setup=None):
    """Run one `_sync_iptv_addon()` and return what the hook was called with."""
    calls = []
    sup = make(tmp_path, settings, sync_iptv_fn=lambda *args: calls.append(args))
    if setup:
        setup(sup.state)
    sup._sync_iptv_addon(sup.state)
    return calls


def test_iptv_hook_gets_setting_url_and_serving_for_embedded_engine(tmp_path):
    settings = {"server_enable": True, "iptv_playlists": "http://x/a.m3u", "server_url": "http://127.0.0.1:9"}

    def embedded(state):
        state.proc = Proc()
        state.server_mode = "embedded"

    assert _iptv_calls(tmp_path, settings, embedded) == [("http://x/a.m3u", "http://127.0.0.1:9", True, "")]


def test_iptv_hook_serving_while_engine_is_restarting(tmp_path):
    """Nothing running yet and nothing external adopted (the tick after a
    settings-changed restart): still ours, so the entry goes offline rather
    than being removed."""
    calls = _iptv_calls(tmp_path, {"server_enable": True, "iptv_playlists": "http://x/a.m3u"})
    assert calls[0][2] is True


def test_iptv_hook_not_serving_when_embedded_server_disabled(tmp_path):
    calls = _iptv_calls(tmp_path, {"server_enable": False, "iptv_playlists": "http://x/a.m3u"})
    assert calls[0][2] is False


def test_iptv_hook_not_serving_for_external_server(tmp_path):
    def external(state):
        state.server_mode = "external"

    calls = _iptv_calls(tmp_path, {"server_enable": True, "iptv_playlists": "http://x/a.m3u"}, external)
    assert calls[0][2] is False


def test_iptv_hook_serving_for_fresh_spawn_while_mode_still_external(tmp_path):
    """`server_mode` lags a tick behind a spawn that replaced an external
    server: our process counts, or the entry would be removed for a tick."""
    def spawned(state):
        state.server_mode = "external"
        state.proc = Proc()

    calls = _iptv_calls(tmp_path, {"server_enable": True, "iptv_playlists": "http://x/a.m3u"}, spawned)
    assert calls[0][2] is True


def test_iptv_hook_failure_is_logged_not_raised(tmp_path):
    def boom(*_args):
        raise RuntimeError("store locked")

    sup = make(tmp_path, {"server_enable": True}, sync_iptv_fn=boom)
    sup._sync_iptv_addon(sup.state)
    assert any("IPTV add-on sync failed" in msg for _, msg in sup.logs)


def test_run_loop_syncs_iptv_after_dispatch_each_tick(tmp_path):
    calls = []
    sup = make(
        tmp_path, {"server_enable": False, "iptv_playlists": "http://x/a.m3u"}, abort_after=2,
        sync_iptv_fn=lambda *args: calls.append(args),
    )
    sup.run()
    assert len(calls) == 2 and calls[0][2] is False


def test_monitor_restarts_when_iptv_playlists_change():
    addon = FakeAddon({"server_enable": True})
    monitor = sr.ServiceMonitor(addon, FakeSettings)
    addon.settings["iptv_playlists"] = "http://x/a.m3u"
    monitor.onSettingsChanged()
    assert monitor.restart_requested is True
    assert monitor.extra_env["RIVULET_IPTV_PLAYLISTS"] == "http://x/a.m3u"
    assert monitor.extra_settings["iptv_playlists"] == "http://x/a.m3u"


def test_monitor_restarts_when_iptv_epg_changes():
    addon = FakeAddon({"server_enable": True, "iptv_playlists": "http://x/a.m3u"})
    monitor = sr.ServiceMonitor(addon, FakeSettings)
    monitor.restart_requested = False
    addon.settings["iptv_epg"] = "http://x/guide.xml"
    monitor.onSettingsChanged()
    assert monitor.restart_requested is True
    assert monitor.extra_env["RIVULET_IPTV_EPG"] == "http://x/guide.xml"


def test_iptv_hook_gets_the_guide_the_engine_was_started_with(tmp_path):
    def embedded(state):
        state.proc = Proc()
        state.server_mode = "embedded"
        state.proc.extra_env = {"RIVULET_IPTV_PLAYLISTS": "http://x/a.m3u", "RIVULET_IPTV_EPG": "http://x/old.xml"}

    settings = {"server_enable": True, "iptv_playlists": "http://x/a.m3u", "iptv_epg": "http://x/new.xml"}
    assert _iptv_calls(tmp_path, settings, embedded)[0][3] == "http://x/old.xml"


def test_iptv_hook_gets_the_guide_setting_when_no_engine_of_ours_runs(tmp_path):
    settings = {"server_enable": True, "iptv_playlists": "http://x/a.m3u", "iptv_epg": "http://x/new.xml"}
    assert _iptv_calls(tmp_path, settings)[0][3] == "http://x/new.xml"


# --- sliced sleep, playback sampling cadence, shutdown order -----------------


class SliceMonitor(Monitor):
    def __init__(self, addon, on_wait=None):
        super().__init__(addon, abort_after=10 ** 9)
        self.on_wait = on_wait

    def waitForAbort(self, timeout=None):
        self.waits.append(timeout)
        if self.on_wait:
            self.on_wait(self, len(self.waits))
        return False


def make_sliced(tmp_path, on_wait=None):
    sup = make(tmp_path)
    sup.monitor = SliceMonitor(FakeAddon({"server_enable": True}), on_wait)
    return sup


def test_sleep_runs_in_slices_totalling_the_interval(monkeypatch, tmp_path):
    monkeypatch.setattr(sr, "SLEEP_SLICE_SECONDS", 1.0)
    sup = make_sliced(tmp_path)
    assert sup._sleep(2.5) is False
    assert sup.monitor.waits == [1.0, 1.0, 0.5]


def test_sleep_wakes_early_on_a_pending_restart(monkeypatch, tmp_path):
    """A 300 s latched-platform sleep must not hold a settings change."""
    monkeypatch.setattr(sr, "SLEEP_SLICE_SECONDS", 1.0)
    sup = make_sliced(tmp_path, lambda m, n: setattr(m, "restart_requested", True) if n == 2 else None)
    assert sup._sleep(300.0) is False
    assert sup.monitor.waits == [1.0, 1.0]
    assert sup.monitor.restart_requested is True  # consumed by the next tick, not here


def test_sleep_wakes_early_on_any_settings_change_and_clears_the_flag(monkeypatch, tmp_path):
    monkeypatch.setattr(sr, "SLEEP_SLICE_SECONDS", 1.0)
    sup = make_sliced(tmp_path, lambda m, n: setattr(m, "wake_requested", True) if n == 1 else None)
    assert sup._sleep(300.0) is False
    assert sup.monitor.waits == [1.0]
    assert sup.monitor.wake_requested is False


def test_sleep_reports_abort_immediately(monkeypatch, tmp_path):
    monkeypatch.setattr(sr, "SLEEP_SLICE_SECONDS", 1.0)
    sup = make_sliced(tmp_path)
    sup.monitor.waitForAbort = lambda timeout=None: True
    assert sup._sleep(300.0) is True


def test_monitor_flags_wake_on_every_settings_change():
    monitor = sr.ServiceMonitor(FakeAddon({"server_enable": True}), FakeSettings)
    monitor.onSettingsChanged()
    assert monitor.wake_requested is True
    assert monitor.restart_requested is False


def test_run_caps_the_interval_while_a_playback_is_sampled(tmp_path):
    sup = make(tmp_path, player=Player(sampling=True))
    sup.run()
    assert sup.monitor.waits and max(sup.monitor.waits) <= sr.PLAYBACK_SAMPLE_INTERVAL


def test_shutdown_drains_the_final_push_before_the_blocking_engine_stop(tmp_path):
    sup = make(tmp_path)
    order = []
    sup.progress_player.drain_pushes = lambda timeout: order.append("drain") or True
    sup.state.proc = Proc()
    sup.state.proc.stop = lambda: order.append("stop")
    sup.shutdown()
    assert order == ["drain", "stop"]


def test_monitor_translates_special_paths_for_binary_and_cache_dir():
    addon = FakeAddon({"server_enable": True, "server_binary": "special://home/bin/engine",
                       "cache_dir": "special://temp/c", "server_url": "http://x:1"})
    monitor = sr.ServiceMonitor(addon, FakeSettings, translate_path=lambda p: p.replace("special://", "/k/"))
    assert monitor.binary_setting == "/k/home/bin/engine"
    assert monitor.extra_env["RIVULET_CACHE_ROOT"] == "/k/temp/c"
    addon.settings["cache_dir"] = "/plain/dir"
    monitor.onSettingsChanged()
    assert monitor.extra_env["RIVULET_CACHE_ROOT"] == "/plain/dir"


def test_iptv_hook_skips_a_tick_with_a_restart_pending(tmp_path):
    """Settings changed after this tick's restart check: the engine still
    answering serves the old playlists, which must not be published under
    the new playlists' stamp."""
    calls = []
    sup = make(tmp_path, {"server_enable": True, "iptv_playlists": "http://x/new.m3u"},
               sync_iptv_fn=lambda *args: calls.append(args))
    sup.state.proc = Proc()
    sup.monitor.restart_requested = True
    sup._sync_iptv_addon(sup.state)
    assert calls == []


def test_iptv_hook_reports_what_our_engine_was_started_with(tmp_path):
    def embedded(state):
        state.proc = Proc()
        state.proc.extra_env = {"RIVULET_IPTV_PLAYLISTS": "http://x/old.m3u"}

    calls = _iptv_calls(tmp_path, {"server_enable": True, "iptv_playlists": "http://x/new.m3u"}, embedded)
    assert calls[0][0] == "http://x/old.m3u"
