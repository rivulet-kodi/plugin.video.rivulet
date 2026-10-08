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
    def __init__(self, error=None):
        self.error = error
        self.samples = 0

    def sample_if_playing(self):
        self.samples += 1
        if self.error:
            raise self.error


def make(tmp_path, settings=None, abort_after=1, autoload=None, player=None, clock=None):
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
    )
    sup.logs, sup.notes, sup.events = logs, notes, events
    return sup


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
    monkeypatch.setattr(sr, "resolve_binary", lambda *a: "/x/stremio-server")
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
