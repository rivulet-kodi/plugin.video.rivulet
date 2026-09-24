"""Tests for lib.service_runner's main() supervision loop: the
xbmc.Monitor-driven loop that spawns/probes/restarts the embedded
stremio-server-go child, the AutoloadTrigger that opens Rivulet's UI once
per Kodi session, and http_port_from_url(), a small pure helper main() uses
to derive the probe port from the configured server URL.

See test_service_runner_server.py for the pure process-management core
(resolve_binary, probe_listening, ServerProcess, ...) and
test_service_runner_player.py for the playback-progress player built by
build_progress_player(). The shared tests/kodistubs fake xbmc modules were
built for lib.ui.* and don't define xbmc.Monitor.abortRequested() (lib.ui.
player only calls waitForAbort()) or xbmcgui.NOTIFICATION_ERROR (lib.ui
never raises an error notification) -- both of which main() needs. Rather
than hand-rolling a parallel set of xbmc fakes, `_main_env` below installs
the real shared stubs via install_kodi_stubs() and patches only those two
gaps directly onto the fresh, per-call fake module objects it returns;
nothing here touches tests/kodistubs itself, and every mutation is
discarded when install_kodi_stubs()'s own `finally` restores sys.modules
at the end of the `with` block.
"""
import contextlib
import os
import sys

import pytest

import lib.libserver as libserver
import lib.serverbin as serverbin
import lib.service_runner as service_runner
from tests.kodistubs import install_kodi_stubs
from tests.test_service_runner_server import fake_popen  # noqa: F401 -- pytest fixture

# ===========================================================================
# http_port_from_url
# ===========================================================================


@pytest.mark.parametrize('url,expected', [
    ('http://127.0.0.1:11470', 11470),
    ('http://127.0.0.1:11470/settings', 11470),
    ('https://example.com:8443/x', 8443),
])
def test_http_port_from_url_extracts_explicit_port(url, expected):
    assert service_runner.http_port_from_url(url) == expected


def test_http_port_from_url_falls_back_to_default_when_port_missing():
    assert service_runner.http_port_from_url('http://127.0.0.1') == service_runner.DEFAULT_HTTP_PORT


def test_http_port_from_url_honors_caller_supplied_default():
    assert service_runner.http_port_from_url('http://127.0.0.1', default=9999) == 9999


def test_http_port_from_url_falls_back_to_default_on_malformed_ipv6_url():
    """Exercises the `ValueError` arm of the except clause: an unclosed
    IPv6 literal makes `urlparse(...).port` raise instead of returning."""
    assert service_runner.http_port_from_url('http://[::1') == service_runner.DEFAULT_HTTP_PORT


def test_http_port_from_url_falls_back_to_default_on_non_string_input():
    """Exercises the `AttributeError` arm of the except clause: urlparse
    chokes on a non-string/bytes `server_url`."""
    assert service_runner.http_port_from_url(12345) == service_runner.DEFAULT_HTTP_PORT


def test_http_port_from_url_honors_explicit_port_zero():
    """An explicit ``:0`` port is syntactically valid and is now honored
    verbatim. Previously ``return port or default`` coerced it to the default
    because ``0`` is falsy; fixed to ``port if port is not None else default``.
    """
    assert service_runner.http_port_from_url('http://127.0.0.1:0') == 0



# ===========================================================================
# main(): the xbmc.Monitor-driven supervision loop
# ===========================================================================
#
# The shared `tests/kodistubs` fake xbmc modules were built for `lib.ui.*`
# and don't define `xbmc.Monitor.abortRequested()` (lib.ui.player only
# calls waitForAbort()) or `xbmcgui.NOTIFICATION_ERROR` (lib.ui never
# raises an error notification) -- both of which `main()` needs. Rather
# than hand-rolling a parallel set of xbmc fakes, `_main_env` below installs
# the real shared stubs via `install_kodi_stubs()` and patches only those
# two gaps directly onto the fresh, per-call fake module objects it
# returns; nothing here touches `tests/kodistubs` itself, and every mutation
# is discarded when `install_kodi_stubs()`'s own `finally` restores
# `sys.modules` at the end of the `with` block.


# Real Kodi defaults (per the shared settings contract) for every one of the
# 30 EXTRA_ENV_SETTINGS keys. Tests that seed `env_box['env'].addon.settings`
# with these before flipping ONE key can trust that a resave changing
# nothing among the 30 really means nothing changed -- FakeAddon otherwise
# defaults an absent key to ''/False/0, which disagrees with several of
# these real defaults (e.g. `disable_webtorrent`/`local_imdb` default True,
# `https_port` defaults to 12470, not 0).
_EXTRA_ENV_DEFAULTS = {
    'bt_listen_port': 0,
    'peers_per_torrent': 0,
    'torrent_idle_timeout': 300,
    'bt_encryption': 'prefer',
    'bt_anonymous': False,
    'disable_trackers': False,
    'bt_proxy': '',
    'disable_webtorrent': True,
    'trackers_max': 5,
    'trackers_url': '',
    'dht_bootstrap': '',
    'memory_cache_size_mb': 0,
    'mem_limit_mb': 0,
    'proxy_prebuffer': 3,
    'proxy_seg_cache_ttl': 300,
    'proxy_password': '',
    'proxy_ip_acl': '',
    'proxy_public_url': '',
    'proxy_upstream': '',
    'proxy_secret': '',
    'enable_dlna': False,
    'local_imdb': True,
    'metadata_url': '',
    'bitmagnet_url': '',
    'torznab_url': '',
    'torznab_apikey': '',
    'web_ui_location': '',
    'https_port': 12470,
    'pprof_addr': '',
    'cert_authkey': '',
}


class ScriptedProcess:
    """Stand-in for the `ServerProcess` class itself (not for
    `subprocess.Popen`) used only by the `main()` orchestration tests
    below: records constructor args and start()/stop() call counts, and
    returns pre-scripted poll()/uptime() results instead of touching a
    real subprocess.
    """

    def __init__(
        self, binary, server_url, app_path, log_path,
        poll_sequence=None, uptime_value=None, extra_env=None,
        start_exceptions=None, stop_exceptions=None,
    ):
        self.binary = binary
        self.server_url = server_url
        self.app_path = app_path
        self.log_path = log_path
        self.extra_env = extra_env or {}
        self.start_calls = 0
        self.stop_calls = 0
        self.rotate_check_calls = 0
        self._poll_sequence = list(poll_sequence or [])
        self._uptime_value = uptime_value
        self._start_exceptions = list(start_exceptions or [])  # queue: None entries succeed
        self._stop_exceptions = list(stop_exceptions or [])

    def start(self):
        self.start_calls += 1
        if self._start_exceptions:
            exc = self._start_exceptions.pop(0)
            if exc is not None:
                raise exc

    def poll(self):
        return self._poll_sequence.pop(0) if self._poll_sequence else None

    def uptime(self):
        return self._uptime_value

    def maybe_rotate_log(self):
        """No real file I/O -- just counts calls so a test can assert
        main()'s HEALTHY branch invokes this once per healthy tick (the
        actual stat()-gating cadence is unit-tested against the real
        ServerProcess.maybe_rotate_log() instead)."""
        self.rotate_check_calls += 1

    def stop(self, grace=5.0):
        self.stop_calls += 1
        if self._stop_exceptions:
            exc = self._stop_exceptions.pop(0)
            if exc is not None:
                raise exc


def _make_process_factory(specs):
    """Returns `(factory, spawned)`. `factory` is a drop-in replacement
    for the `ServerProcess` class, called positionally exactly like
    `ServerProcess(binary, server_url, app_path, log_path)`; each call
    consumes the next `specs` entry (a dict of `ScriptedProcess` kwargs)
    to build one instance. `spawned` collects every instance made, in
    construction order, for assertions.
    """
    queue = list(specs)
    spawned = []

    def factory(binary, server_url, app_path, log_path, extra_env=None):
        kwargs = queue.pop(0) if queue else {}
        proc = ScriptedProcess(binary, server_url, app_path, log_path, extra_env=extra_env, **kwargs)
        spawned.append(proc)
        return proc

    return factory, spawned


class ScriptedLibraryServer:
    """Stand-in for `lib.libserver.LibraryServer` itself, used only by the
    library-mode `main()` orchestration tests below -- mirrors
    `ScriptedProcess`'s recording/scripting surface exactly, since
    `_start_library_server()` treats the two interchangeably."""

    def __init__(
        self, library_path, server_url, app_path, log_path,
        poll_sequence=None, uptime_value=None, extra_env=None, log_fn=None,
        start_exceptions=None, stop_exceptions=None,
    ):
        self.library_path = library_path
        self.server_url = server_url
        self.app_path = app_path
        self.log_path = log_path
        self.extra_env = extra_env or {}
        self.log_fn = log_fn
        self.start_calls = 0
        self.stop_calls = 0
        self.rotate_check_calls = 0
        self._poll_sequence = list(poll_sequence or [])
        self._uptime_value = uptime_value
        self._start_exceptions = list(start_exceptions or [])
        self._stop_exceptions = list(stop_exceptions or [])

    def start(self):
        self.start_calls += 1
        if self._start_exceptions:
            exc = self._start_exceptions.pop(0)
            if exc is not None:
                raise exc

    def poll(self):
        return self._poll_sequence.pop(0) if self._poll_sequence else None

    def uptime(self):
        return self._uptime_value

    def maybe_rotate_log(self):
        self.rotate_check_calls += 1

    def stop(self, grace=5.0):
        self.stop_calls += 1
        if self._stop_exceptions:
            exc = self._stop_exceptions.pop(0)
            if exc is not None:
                raise exc


def _make_library_process_factory(specs):
    """Same contract as `_make_process_factory()`, for
    `lib.libserver.LibraryServer` instead of `ServerProcess`."""
    queue = list(specs)
    spawned = []

    def factory(library_path, server_url, app_path, log_path, extra_env=None, log_fn=None):
        kwargs = queue.pop(0) if queue else {}
        proc = ScriptedLibraryServer(
            library_path, server_url, app_path, log_path,
            extra_env=extra_env, log_fn=log_fn, **kwargs)
        spawned.append(proc)
        return proc

    return factory, spawned


def _scripted_wait(intervals, steps):
    """Builds a `Monitor.waitForAbort(self, timeout)` replacement.

    Records every `timeout` argument into `intervals` (so a test can
    assert exactly what interval each loop iteration computed), runs the
    aligned `steps[i](monitor)` callback -- if any -- *before* deciding
    whether to abort (mirroring Kodi invoking a Monitor hook, e.g.
    `onSettingsChanged()`, asynchronously during the wait), and returns
    True (abort the loop) on and after the `len(steps)`'th call so a test
    drives an exact, deterministic number of iterations.
    """

    def waitForAbort(self, timeout=None):
        intervals.append(timeout)
        idx = len(intervals) - 1
        step = steps[idx] if idx < len(steps) else None
        if step is not None:
            step(self)
        return idx >= len(steps) - 1

    return waitForAbort


@contextlib.contextmanager
def _main_env(tmp_path, waitforabort, settings=None, cond_visibility=True):
    """Installs the shared kodistubs for one `main()` run, patching the
    two Monitor/xbmcgui gaps described above and redirecting
    `xbmcvfs.translatePath` to a real pytest `tmp_path` so `main()`'s
    `os.makedirs(profile_dir, exist_ok=True)` writes somewhere hermetic
    instead of the shared fake's literal `/fake-kodi-home/...` path.

    `xbmc.getCondVisibility` is a third such gap, needed only by
    `main()`'s startup-autoload GUI-ready probe: pass a bool (every
    query answers it) or a callable taking the condition string.
    """
    with install_kodi_stubs(reload=(), settings=settings) as ctx:
        xbmc_mod = sys.modules['xbmc']
        xbmcgui_mod = sys.modules['xbmcgui']
        xbmcvfs_mod = sys.modules['xbmcvfs']

        xbmcgui_mod.NOTIFICATION_ERROR = 'error'
        xbmcvfs_mod.translatePath = lambda path: str(tmp_path)
        xbmc_mod.Monitor.abortRequested = lambda self: False
        xbmc_mod.Monitor.waitForAbort = waitforabort
        xbmc_mod.getCondVisibility = (
            cond_visibility if callable(cond_visibility) else (lambda cond: cond_visibility)
        )

        ctx.xbmc = xbmc_mod
        ctx.xbmcgui = xbmcgui_mod
        yield ctx


# --- (a) external server already listening: no spawn ------------------------


def test_main_external_server_already_listening_skips_spawn(monkeypatch, tmp_path):
    probe_calls = []

    def fake_probe(url, **kwargs):
        probe_calls.append(url)
        return True

    def resolve_binary_must_not_run(*args, **kwargs):
        pytest.fail('resolve_binary must not run while an external server answers')

    def install_binary_must_not_run(*args, **kwargs):
        pytest.fail('install_binary must not run while an external server answers')

    monkeypatch.setattr(service_runner, 'probe_listening', fake_probe)
    monkeypatch.setattr(service_runner, 'resolve_binary', resolve_binary_must_not_run)
    monkeypatch.setattr(serverbin, 'install_binary', install_binary_must_not_run)
    factory, spawned = _make_process_factory([])
    monkeypatch.setattr(service_runner, 'ServerProcess', factory)

    intervals = []
    wait = _scripted_wait(intervals, [None, None])
    with _main_env(tmp_path, wait, settings={'server_enable': True}) as ctx:
        service_runner.main()

    assert probe_calls == [service_runner.DEFAULT_SERVER_URL] * 2
    assert spawned == []
    assert intervals == [service_runner.EXTERNAL_RECHECK_INTERVAL] * 2
    assert not any('shutting down' in msg for msg, _level in ctx.env.log_calls)



# --- s4me bridge hook: main() syncs BridgeSupervisor every tick -------------


class _FakeBridgeSupervisor:
    """Stand-in for lib.s4me.BridgeSupervisor: records every apply() call's
    (enabled, port) plus whether has_addon_fn()/launch_fn were passed
    through unchanged, without touching a real store or RunScript."""

    instances = []

    def __init__(self, addon_path):
        self.addon_path = addon_path
        self.apply_calls = []
        _FakeBridgeSupervisor.instances.append(self)

    def apply(self, enabled, port, has_addon_fn, launch_fn, store):
        self.apply_calls.append((enabled, port, has_addon_fn(), launch_fn, store))


def test_main_syncs_s4me_bridge_every_tick_with_current_settings(monkeypatch, tmp_path):
    _FakeBridgeSupervisor.instances = []
    monkeypatch.setattr(service_runner.s4me, "BridgeSupervisor", _FakeBridgeSupervisor)
    monkeypatch.setattr(service_runner, "probe_listening", lambda *a, **kw: True)

    cond_calls = []

    def cond_visibility(cond):
        cond_calls.append(cond)
        return True

    intervals = []
    wait = _scripted_wait(intervals, [None, None])
    settings = {'server_enable': True, 's4me_enable': True, 's4me_port': 11499}
    with _main_env(tmp_path, wait, settings=settings, cond_visibility=cond_visibility) as ctx:
        service_runner.main()
        expected_launch_fn = ctx.xbmc.executebuiltin

    assert len(_FakeBridgeSupervisor.instances) == 1
    supervisor = _FakeBridgeSupervisor.instances[0]
    assert isinstance(supervisor.addon_path, str)  # xbmcaddon FakeAddon.getAddonInfo("path")
    assert len(supervisor.apply_calls) == 2  # once per loop tick
    enabled, port, has_addon, launch_fn, store = supervisor.apply_calls[0]
    assert enabled is True
    assert port == 11499
    assert has_addon is True
    assert launch_fn == expected_launch_fn
    assert any('plugin.video.s4me' in c for c in cond_calls)


def test_main_s4me_bridge_disabled_by_default(monkeypatch, tmp_path):
    """s4me_enable defaults False -- an untouched install must never sync
    an active bridge."""
    _FakeBridgeSupervisor.instances = []
    monkeypatch.setattr(service_runner.s4me, "BridgeSupervisor", _FakeBridgeSupervisor)
    monkeypatch.setattr(service_runner, "probe_listening", lambda *a, **kw: True)

    intervals = []
    wait = _scripted_wait(intervals, [None])
    with _main_env(tmp_path, wait, settings={'server_enable': True}):
        service_runner.main()

    supervisor = _FakeBridgeSupervisor.instances[0]
    enabled, port, has_addon, launch_fn, store = supervisor.apply_calls[0]
    assert enabled is False
    assert port == service_runner.s4me.DEFAULT_PORT


def test_main_s4me_bridge_sync_failure_is_swallowed(monkeypatch, tmp_path):
    """A raising BridgeSupervisor.apply() must never crash the supervision
    loop -- mirrors every other defensively-wrapped per-tick call in
    main()."""
    class _RaisingSupervisor:
        def __init__(self, addon_path):
            pass

        def apply(self, *a, **kw):
            raise RuntimeError("boom")

    monkeypatch.setattr(service_runner.s4me, "BridgeSupervisor", _RaisingSupervisor)
    monkeypatch.setattr(service_runner, "probe_listening", lambda *a, **kw: True)

    intervals = []
    wait = _scripted_wait(intervals, [None])
    with _main_env(tmp_path, wait, settings={'server_enable': True}):
        service_runner.main()  # must not raise


# --- (b) embedded enabled + binary found: spawn, then healthy poll ----------


def test_main_embedded_enabled_binary_found_spawns_and_polls_healthy(monkeypatch, tmp_path):
    monkeypatch.setattr(service_runner, 'probe_listening', lambda *a, **kw: False)
    monkeypatch.setattr(service_runner, 'resolve_binary', lambda *a, **kw: '/opt/bin/stremio-server')
    factory, spawned = _make_process_factory([{}])
    monkeypatch.setattr(service_runner, 'ServerProcess', factory)

    intervals = []
    wait = _scripted_wait(intervals, [None, None, None])
    with _main_env(tmp_path, wait, settings={'server_enable': True}) as ctx:
        service_runner.main()

    assert len(spawned) == 1
    proc = spawned[0]
    assert proc.binary == '/opt/bin/stremio-server'
    assert proc.server_url == service_runner.DEFAULT_SERVER_URL
    assert proc.app_path == os.path.join(str(tmp_path), 'server')
    assert proc.log_path == os.path.join(str(tmp_path), service_runner.LOG_FILENAME)
    assert proc.start_calls == 1
    # HEALTHY-branch wiring: main() calls maybe_rotate_log() every tick
    # where poll() reports the child still alive (iterations 2 and 3 here
    # -- iteration 1 is the initial spawn, not a HEALTHY-branch poll).
    assert proc.rotate_check_calls == 2
    assert intervals == [service_runner.HEALTHY_POLL_INTERVAL] * 3
    assert any('starting embedded server' in msg for msg, _level in ctx.env.log_calls)

    # main() returned with the child still alive -> the post-loop shutdown
    # path (scenario g) must stop it exactly once.
    assert proc.stop_calls == 1
    assert any('shutting down embedded server' in msg for msg, _level in ctx.env.log_calls)


def test_main_healthy_loop_rotates_log_on_a_coarse_cadence_not_every_tick(monkeypatch, fake_popen, tmp_path):  # noqa: F811
    """Finding 8 integration coverage, against the REAL ServerProcess (not
    the ScriptedProcess test double), wired through main(): the HEALTHY
    branch calls maybe_rotate_log() every tick, but its internal gate must
    keep the actual stat() to once per LOG_ROTATE_CHECK_INTERVAL -- ticks
    that land inside that window touch the filesystem zero times (the
    idle no-disk-touch property extended to the healthy branch), and the
    tick that crosses the interval boundary stats exactly once and rotates
    a by-then-oversized log.
    """
    monkeypatch.setattr(service_runner, 'probe_listening', lambda *a, **kw: False)
    monkeypatch.setattr(service_runner, 'resolve_binary', lambda *a, **kw: '/opt/bin/stremio-server')
    # service_runner.ServerProcess is deliberately left un-patched here --
    # only subprocess.Popen is faked (fake_popen) -- so the real
    # maybe_rotate_log()/_rotate_log() gating logic under test actually runs.

    log_path = tmp_path / service_runner.LOG_FILENAME
    log_path.write_bytes(b'hello')  # small, pre-existing log: start()'s own rotation is a no-op

    monotonic_values = iter([
        0.0,                                          # AutoloadTrigger construction (autoload stays disabled)
        0.0,                                          # iteration 1: ServerProcess.start()
        100.0,                                         # iteration 2: gated tick, well inside the window
        service_runner.LOG_ROTATE_CHECK_INTERVAL,      # iteration 3: crosses the interval boundary
    ])
    monkeypatch.setattr(service_runner.time, 'monotonic', lambda: next(monotonic_values))

    getsize_calls = []
    real_getsize = os.path.getsize

    def spy_getsize(path):
        getsize_calls.append(path)
        return real_getsize(path)

    monkeypatch.setattr(service_runner.os.path, 'getsize', spy_getsize)

    getsize_counts_at_tick = []

    def _snapshot(_monitor):
        getsize_counts_at_tick.append(len(getsize_calls))

    def _grow_log_then_snapshot(_monitor):
        # Simulates the still-running child appending past the threshold
        # between iteration 2's (gated, no-op) check and iteration 3's.
        log_path.write_bytes(log_path.read_bytes() + b'x' * (service_runner.LOG_ROTATE_BYTES + 1))
        getsize_counts_at_tick.append(len(getsize_calls))

    intervals = []
    wait = _scripted_wait(intervals, [_snapshot, _grow_log_then_snapshot, _snapshot])
    with _main_env(tmp_path, wait, settings={'server_enable': True}):
        service_runner.main()

    assert intervals == [service_runner.HEALTHY_POLL_INTERVAL] * 3

    # Iteration 1 (start()) did exactly one stat -- its own one-time,
    # pre-existing rotation check, unrelated to the new periodic gate.
    assert getsize_counts_at_tick[0] == 1
    # Iteration 2's periodic check is gated (100s < 300s since start()):
    # no additional stat, no matter that the log is about to grow.
    assert getsize_counts_at_tick[1] == 1
    # Iteration 3 crosses the interval boundary: the gate fires, stats
    # exactly once, and rotates the now-oversized log.
    assert getsize_counts_at_tick[2] == 2

    backup = tmp_path / (service_runner.LOG_FILENAME + '.1')
    assert backup.exists()
    assert backup.stat().st_size == len(b'hello') + service_runner.LOG_ROTATE_BYTES + 1
    assert not log_path.exists()  # renamed away; a fresh one appears only on the next start()


# --- (c) embedded enabled + binary missing: auto-download once -------------


def test_main_embedded_enabled_binary_missing_auto_downloads_then_starts(monkeypatch, tmp_path):
    """The happy path: nothing is running and no binary is resolvable, so
    the very first "missing" iteration downloads one via
    `serverbin.install_binary` (into `<profile>/bin`, matching
    `resolve_binary`'s bundled-bin lookup) instead of just notifying and
    waiting for a human to intervene. Once `resolve_binary` reports the
    freshly-installed binary on the next iteration, the server starts
    normally.
    """
    monkeypatch.setattr(service_runner, 'probe_listening', lambda *a, **kw: False)

    resolve_calls = []

    def fake_resolve_binary(explicit_path, addon_data_dir):
        resolve_calls.append(addon_data_dir)
        # Nothing installed yet on the first call; the "installed" binary
        # is found starting the very next iteration.
        return None if len(resolve_calls) == 1 else '/opt/bin/stremio-server'

    monkeypatch.setattr(service_runner, 'resolve_binary', fake_resolve_binary)

    install_calls = []

    def fake_install_binary(dest_dir, progress_cb=None):
        install_calls.append(dest_dir)
        return os.path.join(dest_dir, service_runner.BINARY_NAME)

    monkeypatch.setattr(serverbin, 'install_binary', fake_install_binary)
    factory, spawned = _make_process_factory([{}])
    monkeypatch.setattr(service_runner, 'ServerProcess', factory)

    intervals = []
    wait = _scripted_wait(intervals, [None, None, None])
    with _main_env(tmp_path, wait, settings={'server_enable': True}) as ctx:
        service_runner.main()

    # install_binary runs exactly once, straight into <profile>/bin --
    # exactly where resolve_binary looks for a bundled binary.
    assert install_calls == [os.path.join(str(tmp_path), 'bin')]

    setup_notifications = [n for n in ctx.env.notifications if n[1] == 'STR30069']
    assert len(setup_notifications) == 1
    heading, _message, icon, _time = setup_notifications[0]
    assert heading == 'Rivulet'
    assert icon is None  # informational notification, not the error icon

    # First iteration: download succeeds, so the loop rechecks almost
    # immediately instead of waiting out a full missing-binary cycle.
    assert intervals[0] == service_runner.POST_DOWNLOAD_RECHECK_INTERVAL
    # Second iteration: resolve_binary now finds it, the server starts.
    assert len(spawned) == 1
    assert spawned[0].binary == '/opt/bin/stremio-server'
    assert spawned[0].start_calls == 1

    info_logs = [msg for msg, level in ctx.env.log_calls if level == ctx.xbmc.LOGINFO]
    assert any('auto-downloading stremio-server binary' in msg for msg in info_logs)
    assert any('download complete' in msg for msg in info_logs)


def test_main_transient_download_failure_retries_after_backoff_deadline_and_notifies_once(
    monkeypatch, tmp_path
):
    """A transient DownloadError (network hiccup, GitHub outage, no
    release asset published yet, ...) is not a one-shot: main() retries
    automatically once DOWNLOAD_RETRY_BACKOFF[n] has actually elapsed
    (gated on a monotonic deadline, not merely the loop's own sleep), but
    only shows the failure notification once per cooldown cycle so a
    prolonged outage does not spam the user. Success resets the schedule.
    """
    clock = {'t': 1000.0}
    monkeypatch.setattr(service_runner.time, 'monotonic', lambda: clock['t'])
    monkeypatch.setattr(service_runner, 'probe_listening', lambda *a, **kw: False)

    install_calls = []

    def fake_install_binary(dest_dir, progress_cb=None):
        install_calls.append(dest_dir)
        if len(install_calls) < 3:
            raise serverbin.DownloadError('no network')
        return os.path.join(dest_dir, service_runner.BINARY_NAME)

    def fake_resolve_binary(explicit_path, addon_data_dir):
        return None if len(install_calls) < 3 else '/opt/bin/stremio-server'

    monkeypatch.setattr(serverbin, 'install_binary', fake_install_binary)
    monkeypatch.setattr(service_runner, 'resolve_binary', fake_resolve_binary)
    factory, spawned = _make_process_factory([{}])
    monkeypatch.setattr(service_runner, 'ServerProcess', factory)

    def advance(seconds):
        def _step(monitor):
            clock['t'] += seconds
        return _step

    # iter0: 1st attempt fails, arms a DOWNLOAD_RETRY_BACKOFF[0]s cooldown.
    # iter1: cooldown still active (no time advanced) -> skipped entirely,
    # no install_binary() call, no repeated notification. iter2: clock
    # advanced past the deadline during iter1's wait -> 2nd attempt fails
    # too, arms a DOWNLOAD_RETRY_BACKOFF[1]s cooldown. iter3: still
    # cooling down -> skipped. iter4: clock advanced again -> 3rd attempt
    # succeeds. iter5: resolve_binary now finds it -> spawns.
    intervals = []
    wait = _scripted_wait(intervals, [
        None,
        advance(service_runner.DOWNLOAD_RETRY_BACKOFF[0]),
        None,
        advance(service_runner.DOWNLOAD_RETRY_BACKOFF[1]),
        None,
        None,
    ])
    with _main_env(tmp_path, wait, settings={'server_enable': True}) as ctx:
        service_runner.main()

    assert install_calls == [os.path.join(str(tmp_path), 'bin')] * 3
    assert intervals == [
        service_runner.MISSING_BINARY_RECHECK_INTERVAL,
        service_runner.MISSING_BINARY_RECHECK_INTERVAL,  # skipped: still cooling down
        service_runner.MISSING_BINARY_RECHECK_INTERVAL,
        service_runner.MISSING_BINARY_RECHECK_INTERVAL,  # skipped: still cooling down
        service_runner.POST_DOWNLOAD_RECHECK_INTERVAL,
        service_runner.HEALTHY_POLL_INTERVAL,
    ]

    setup_notifications = [n for n in ctx.env.notifications if n[1] == 'STR30069']
    failed_notifications = [n for n in ctx.env.notifications if n[1] == 'STR30063']
    assert len(setup_notifications) == 1  # rate-limited across retries
    assert len(failed_notifications) == 1  # rate-limited across retries

    error_logs = [msg for msg, level in ctx.env.log_calls if level == ctx.xbmc.LOGERROR]
    assert sum('download failed' in msg for msg in error_logs) == 2  # only the real attempts
    assert any(f'retrying in {service_runner.DOWNLOAD_RETRY_BACKOFF[0]}s' in msg for msg in error_logs)
    assert any(f'retrying in {service_runner.DOWNLOAD_RETRY_BACKOFF[1]}s' in msg for msg in error_logs)

    assert len(spawned) == 1
    assert spawned[0].binary == '/opt/bin/stremio-server'
    assert spawned[0].start_calls == 1


def test_main_embedded_enabled_binary_missing_unsupported_platform_notifies_once_and_stops_retrying(
    monkeypatch, tmp_path
):
    """When install_binary() raises UnsupportedPlatformError (e.g. Android's
    W^X ban on exec()-ing anything inside app storage), main() must not
    crash, must notify the dedicated 30091 message exactly once, and must
    never call install_binary() again on later polls -- unlike a plain
    DownloadError (which retries automatically forever behind a bounded
    backoff), so latching `unsupported_platform` and warning once per
    session is exactly right. Once latched, later iterations must still
    call BOTH probe_listening() and resolve_binary() every tick -- the
    exception cannot tell Android's permanent exec() ban apart from a
    transient noexec/EACCES mount condition (see
    UNSUPPORTED_PLATFORM_POLL_INTERVAL's comment) -- just at the coarse
    UNSUPPORTED_PLATFORM_POLL_INTERVAL cadence instead of
    MISSING_BINARY_RECHECK_INTERVAL, and without ever re-attempting
    install_binary() itself."""
    probe_calls = []
    resolve_calls = []
    monkeypatch.setattr(service_runner, 'probe_listening', lambda *a, **kw: probe_calls.append(1) or False)
    monkeypatch.setattr(service_runner, 'resolve_binary', lambda *a, **kw: resolve_calls.append(1) or None)

    install_calls = []

    def fake_install_binary(dest_dir, progress_cb=None):
        install_calls.append(dest_dir)
        raise serverbin.UnsupportedPlatformError('exec() is forbidden on Android 10+')

    monkeypatch.setattr(serverbin, 'install_binary', fake_install_binary)
    factory, spawned = _make_process_factory([])
    monkeypatch.setattr(service_runner, 'ServerProcess', factory)

    intervals = []
    wait = _scripted_wait(intervals, [None, None, None])
    with _main_env(tmp_path, wait, settings={'server_enable': True}) as ctx:
        service_runner.main()

    assert spawned == []
    assert intervals == [
        service_runner.MISSING_BINARY_RECHECK_INTERVAL,
        service_runner.UNSUPPORTED_PLATFORM_POLL_INTERVAL,
        service_runner.UNSUPPORTED_PLATFORM_POLL_INTERVAL,
    ]

    # Attempted exactly once across all 3 iterations, never retried.
    assert install_calls == [os.path.join(str(tmp_path), 'bin')]
    # probe_listening() and resolve_binary() both run on every iteration
    # regardless of the latch -- resolve_binary() keeps returning None
    # throughout this test, so the latch never clears and install_binary()
    # is never retried.
    assert len(probe_calls) == 3
    assert len(resolve_calls) == 3

    setup_notifications = [n for n in ctx.env.notifications if n[1] == 'STR30069']
    unsupported_notifications = [n for n in ctx.env.notifications if n[1] == 'STR30091']
    missing_notifications = [n for n in ctx.env.notifications if n[1] == 'STR30031']
    assert len(setup_notifications) == 1
    assert len(unsupported_notifications) == 1
    assert unsupported_notifications[0][2] == 'error'
    # After the one unsupported-platform attempt, the loop falls back to
    # the original notify-once "binary not found" behavior for the
    # remaining iterations.
    assert len(missing_notifications) == 1
    assert missing_notifications[0][2] == 'error'

    warning_logs = [msg for msg, level in ctx.env.log_calls if level == ctx.xbmc.LOGWARNING]
    assert any('cannot run on this device' in msg for msg in warning_logs)
    assert f'[{service_runner.ADDON_ID}] stremio-server binary not found' in [
        msg for msg, level in ctx.env.log_calls if level == ctx.xbmc.LOGERROR
    ]


def test_main_settings_changed_resets_unsupported_platform_latch_for_retry(monkeypatch, tmp_path):
    """`UnsupportedPlatformError` latches `unsupported_platform` (no
    further install attempts at all, unlike a transient DownloadError
    which keeps retrying on its own) -- but only until the user changes a
    setting. A settings change resets the latch alongside the download
    notification flags, giving install_binary() a fresh attempt without
    restarting the whole service.
    """
    monkeypatch.setattr(service_runner, 'probe_listening', lambda *a, **kw: False)

    install_calls = []

    def fake_install_binary(dest_dir, progress_cb=None):
        install_calls.append(dest_dir)
        if len(install_calls) == 1:
            raise serverbin.UnsupportedPlatformError('exec() forbidden')
        return os.path.join(dest_dir, service_runner.BINARY_NAME)

    def fake_resolve_binary(explicit_path, addon_data_dir):
        return None if len(install_calls) < 2 else '/opt/bin/stremio-server'

    monkeypatch.setattr(serverbin, 'install_binary', fake_install_binary)
    monkeypatch.setattr(service_runner, 'resolve_binary', fake_resolve_binary)
    factory, spawned = _make_process_factory([{}])
    monkeypatch.setattr(service_runner, 'ServerProcess', factory)

    env_box = {}

    def trigger_settings_change(monitor):
        env_box['env'].addon.settings['server_url'] = 'http://127.0.0.1:9999'
        monitor.onSettingsChanged()

    # iter1: unsupported, latches (no cooldown involved). iter2: still
    # latched -> no retry; the settings change fires during this wait.
    # iter3: latch reset -> retries and succeeds. iter4: binary now
    # resolvable -> spawns.
    intervals = []
    wait = _scripted_wait(intervals, [None, trigger_settings_change, None, None])
    with _main_env(tmp_path, wait, settings={'server_enable': True}) as ctx:
        env_box['env'] = ctx.env
        service_runner.main()

    assert install_calls == [os.path.join(str(tmp_path), 'bin')] * 2
    assert len(spawned) == 1
    assert spawned[0].binary == '/opt/bin/stremio-server'

    unsupported_notifications = [n for n in ctx.env.notifications if n[1] == 'STR30091']
    missing_notifications = [n for n in ctx.env.notifications if n[1] == 'STR30031']
    assert len(unsupported_notifications) == 1
    # notified_missing fired once while latched, then was reset by the
    # settings change alongside unsupported_platform.
    assert len(missing_notifications) == 1


def test_main_latched_unsupported_platform_rechecks_resolve_binary_at_coarse_cadence(
    monkeypatch, tmp_path
):
    """Once install_binary() latches `unsupported_platform`, later loop
    iterations must still call probe_listening() every tick -- an
    external/manually-started server appearing at server_url is exactly
    what UnsupportedPlatformError's own docstring points users at as "the
    only remedy" -- and must ALSO keep calling resolve_binary() every
    tick: the exception that latches this flag cannot tell Android's
    permanent exec() ban apart from a transient noexec/EACCES mount
    condition (see UNSUPPORTED_PLATFORM_POLL_INTERVAL's comment), so a
    binary becoming available while latched must still be found. What the
    latch actually does is coarsen the cadence from
    MISSING_BINARY_RECHECK_INTERVAL to UNSUPPORTED_PLATFORM_POLL_INTERVAL
    and skip re-attempting install_binary() itself -- as long as
    resolve_binary() keeps returning None here, the latch never clears
    and no further installs happen."""
    probe_calls = []
    resolve_calls = []
    monkeypatch.setattr(service_runner, 'probe_listening', lambda *a, **kw: probe_calls.append(1) or False)
    monkeypatch.setattr(service_runner, 'resolve_binary', lambda *a, **kw: resolve_calls.append(1) or None)

    install_calls = []

    def fake_install_binary(dest_dir, progress_cb=None):
        install_calls.append(dest_dir)
        raise serverbin.UnsupportedPlatformError('exec() forbidden')

    monkeypatch.setattr(serverbin, 'install_binary', fake_install_binary)
    factory, spawned = _make_process_factory([])
    monkeypatch.setattr(service_runner, 'ServerProcess', factory)

    # iter1: fresh attempt fails -> latches. iter2, iter3, iter4: latched,
    # but resolve_binary keeps returning None -- probe and resolve both
    # keep running every iteration, just at the coarse cadence and
    # without ever calling install_binary() again.
    intervals = []
    wait = _scripted_wait(intervals, [None, None, None, None])
    with _main_env(tmp_path, wait, settings={'server_enable': True}) as ctx:
        service_runner.main()

    assert spawned == []
    assert install_calls == [os.path.join(str(tmp_path), 'bin')]  # exactly once, never retried
    assert len(probe_calls) == 4  # every iteration, latched or not
    assert len(resolve_calls) == 4  # every iteration too -- the latch never skips detection
    assert intervals == [
        service_runner.MISSING_BINARY_RECHECK_INTERVAL,
        service_runner.UNSUPPORTED_PLATFORM_POLL_INTERVAL,
        service_runner.UNSUPPORTED_PLATFORM_POLL_INTERVAL,
        service_runner.UNSUPPORTED_PLATFORM_POLL_INTERVAL,
    ]

    setup_notifications = [n for n in ctx.env.notifications if n[1] == 'STR30069']
    unsupported_notifications = [n for n in ctx.env.notifications if n[1] == 'STR30091']
    missing_notifications = [n for n in ctx.env.notifications if n[1] == 'STR30031']
    assert len(setup_notifications) == 1
    assert len(unsupported_notifications) == 1
    assert len(missing_notifications) == 1  # fires once while latched, never re-notified per tick


def test_main_latched_unsupported_platform_self_heals_when_resolve_binary_finds_binary(
    monkeypatch, tmp_path
):
    """The transient-cause half of the latch (see
    UNSUPPORTED_PLATFORM_POLL_INTERVAL's comment): verify_executable()
    raises the same UnsupportedPlatformError for a noexec/EACCES mount
    condition that can clear on its own, with install_binary() having
    already placed a chmod'd binary at the exact path resolve_binary()
    checks. So once resolve_binary() finds a runnable binary while
    latched -- with no settings change at all -- main() must clear the
    latch immediately and start supervising it normally, instead of
    staying broken until onSettingsChanged() fires."""
    probe_calls = []
    resolve_calls = []

    def fake_resolve_binary(explicit_path, addon_data_dir):
        resolve_calls.append(1)
        # Nothing usable for the first two calls (pre-latch + one latched
        # recheck); the noexec/mount condition "clears" starting the third.
        return None if len(resolve_calls) < 3 else '/opt/bin/stremio-server'

    monkeypatch.setattr(service_runner, 'probe_listening', lambda *a, **kw: probe_calls.append(1) or False)
    monkeypatch.setattr(service_runner, 'resolve_binary', fake_resolve_binary)

    def fake_install_binary(dest_dir, progress_cb=None):
        raise serverbin.UnsupportedPlatformError('exec() forbidden')

    monkeypatch.setattr(serverbin, 'install_binary', fake_install_binary)
    factory, spawned = _make_process_factory([{}])
    monkeypatch.setattr(service_runner, 'ServerProcess', factory)

    # iter1: fresh attempt fails -> latches (resolve call #1, None).
    # iter2: latched, resolve call #2 still None -> stays latched.
    # iter3: latched, resolve call #3 now finds a binary -> unlatches and
    # spawns. iter4: proc is running -> ordinary healthy supervision, no
    # more probe/resolve calls at all.
    intervals = []
    wait = _scripted_wait(intervals, [None, None, None, None])
    with _main_env(tmp_path, wait, settings={'server_enable': True}) as ctx:
        service_runner.main()

    assert len(spawned) == 1
    assert spawned[0].binary == '/opt/bin/stremio-server'
    assert spawned[0].start_calls == 1
    assert len(resolve_calls) == 3
    assert len(probe_calls) == 3  # probe/resolve only run while proc is None
    assert intervals == [
        service_runner.MISSING_BINARY_RECHECK_INTERVAL,
        service_runner.UNSUPPORTED_PLATFORM_POLL_INTERVAL,
        service_runner.HEALTHY_POLL_INTERVAL,
        service_runner.HEALTHY_POLL_INTERVAL,
    ]

    unsupported_notifications = [n for n in ctx.env.notifications if n[1] == 'STR30091']
    missing_notifications = [n for n in ctx.env.notifications if n[1] == 'STR30031']
    assert len(unsupported_notifications) == 1
    assert len(missing_notifications) == 1  # fired once during iter2, before the self-heal


def test_main_binary_download_aborts_mid_chunk_without_error_notification(monkeypatch, tmp_path):
    """install_binary()'s progress_cb is polled once per chunk (see
    serverbin._download_to_file); the moment monitor.abortRequested()
    flips True mid-download, main() must unwind immediately instead of
    letting the download run to completion -- and because an abort isn't a
    download failure, no error notification (30063) may fire.
    """
    monkeypatch.setattr(service_runner, 'probe_listening', lambda *a, **kw: False)
    monkeypatch.setattr(service_runner, 'resolve_binary', lambda *a, **kw: None)

    abort_box = {'requested': False}
    install_calls = []

    def fake_install_binary(dest_dir, progress_cb=None):
        install_calls.append(dest_dir)
        progress_cb(1000, 10000)  # first chunk: abort not requested yet
        abort_box['requested'] = True  # shutdown arrives mid-download
        progress_cb(2000, 10000)  # second chunk: must raise and unwind now
        pytest.fail('install_binary kept running after abortRequested() flipped True')

    monkeypatch.setattr(serverbin, 'install_binary', fake_install_binary)
    factory, spawned = _make_process_factory([])
    monkeypatch.setattr(service_runner, 'ServerProcess', factory)

    intervals = []
    wait = _scripted_wait(intervals, [None, None])
    with _main_env(tmp_path, wait, settings={'server_enable': True}) as ctx:
        ctx.xbmc.Monitor.abortRequested = lambda self: abort_box['requested']
        service_runner.main()

    assert install_calls == [os.path.join(str(tmp_path), 'bin')]
    assert spawned == []

    error_notifications = [n for n in ctx.env.notifications if n[1] == 'STR30063']
    assert error_notifications == []

    info_logs = [msg for msg, level in ctx.env.log_calls if level == ctx.xbmc.LOGINFO]
    assert any('aborted' in msg for msg in info_logs)

    # Unwinds the instant the callback raises -- never falls through to the
    # waitForAbort() at the bottom of the loop for the aborted iteration.
    assert intervals == []


# --- (d) crash-restart backoff progression + stable-uptime reset -----------


def test_main_crash_restart_backoff_progression_and_stable_uptime_reset(monkeypatch, tmp_path):
    """A repeatedly-crashing child restarts on the 5s/10s/30s(capped)
    schedule (any exit code, not just a nonzero one, counts as a crash to
    restart from); a run lasting >= MIN_STABLE_UPTIME resets the backoff
    index back to RESTART_BACKOFF[0] instead of staying capped, so a
    server that crashes only occasionally isn't throttled like a genuine
    crash loop.
    """
    monkeypatch.setattr(service_runner, 'probe_listening', lambda *a, **kw: False)
    monkeypatch.setattr(service_runner, 'resolve_binary', lambda *a, **kw: '/opt/bin/stremio-server')
    specs = [
        {'poll_sequence': [0], 'uptime_value': 5.0},   # clean exit(0) still restarts
        {'poll_sequence': [1], 'uptime_value': 3.0},
        {'poll_sequence': [1], 'uptime_value': 1.0},
        {'poll_sequence': [1], 'uptime_value': service_runner.MIN_STABLE_UPTIME + 1.0},
    ]
    factory, spawned = _make_process_factory(specs)
    monkeypatch.setattr(service_runner, 'ServerProcess', factory)

    intervals = []
    # 8 iterations: spawn, crash, spawn, crash, spawn, crash, spawn, crash.
    wait = _scripted_wait(intervals, [None] * 8)
    with _main_env(tmp_path, wait, settings={'server_enable': True}):
        service_runner.main()

    assert len(spawned) == 4
    assert [p.start_calls for p in spawned] == [1, 1, 1, 1]
    assert [p.stop_calls for p in spawned] == [1, 1, 1, 1]

    # Spawn iterations (0, 2, 4, 6) always poll at HEALTHY_POLL_INTERVAL.
    assert [intervals[i] for i in (0, 2, 4, 6)] == [service_runner.HEALTHY_POLL_INTERVAL] * 4

    # Crash iterations (1, 3, 5) climb the backoff schedule in order.
    assert [intervals[1], intervals[3], intervals[5]] == list(service_runner.RESTART_BACKOFF)

    # The 4th crash (iteration 7) followed a run >= MIN_STABLE_UPTIME:
    # backoff resets to RESTART_BACKOFF[0] instead of staying capped at
    # RESTART_BACKOFF[-1].
    assert intervals[7] == service_runner.RESTART_BACKOFF[0]


def test_main_crash_restart_backoff_cadence_matches_elapsed_fake_clock_time(monkeypatch, tmp_path):
    """Companion to the test above, but proving the cadence against
    elapsed TIME rather than the raw interval sequence: the shared
    fake `xbmc.Monitor.waitForAbort()` (tests/kodistubs/fakes.py's
    `Env.clock`/`wait_calls` - previously a no-op returning immediately
    regardless of `timeout`, so no test could ever prove the supervisor
    actually paces itself) now advances `env.clock` by each call's
    `timeout` and records it into `env.wait_calls`. Runs against the
    REAL (un-overridden) fake `Monitor.waitForAbort`, not the local
    `_scripted_wait` helper other tests use, and drives loop
    termination the same way real Kodi would: `env.monitor_abort`
    flipping true after the 8th call.
    """
    monkeypatch.setattr(service_runner, 'probe_listening', lambda *a, **kw: False)
    monkeypatch.setattr(service_runner, 'resolve_binary', lambda *a, **kw: '/opt/bin/stremio-server')
    specs = [
        {'poll_sequence': [0], 'uptime_value': 5.0},
        {'poll_sequence': [1], 'uptime_value': 3.0},
        {'poll_sequence': [1], 'uptime_value': 1.0},
        {'poll_sequence': [1], 'uptime_value': service_runner.MIN_STABLE_UPTIME + 1.0},
    ]
    factory, spawned = _make_process_factory(specs)
    monkeypatch.setattr(service_runner, 'ServerProcess', factory)

    with install_kodi_stubs(
        reload=(), settings={'server_enable': True}, monitor_abort=lambda count: count >= 8,
    ) as ctx:
        xbmc_mod = sys.modules['xbmc']
        xbmcgui_mod = sys.modules['xbmcgui']
        xbmcvfs_mod = sys.modules['xbmcvfs']
        xbmcgui_mod.NOTIFICATION_ERROR = 'error'
        xbmcvfs_mod.translatePath = lambda path: str(tmp_path)
        xbmc_mod.getCondVisibility = lambda cond: True
        service_runner.main()

    env = ctx.env
    assert len(spawned) == 4
    # Spawn iterations (0, 2, 4, 6) poll at HEALTHY_POLL_INTERVAL; crash
    # iterations (1, 3, 5) climb 5s/10s/30s, and the 4th crash (7) --
    # following a >= MIN_STABLE_UPTIME run -- resets to RESTART_BACKOFF[0].
    expected = [
        service_runner.HEALTHY_POLL_INTERVAL, service_runner.RESTART_BACKOFF[0],
        service_runner.HEALTHY_POLL_INTERVAL, service_runner.RESTART_BACKOFF[1],
        service_runner.HEALTHY_POLL_INTERVAL, service_runner.RESTART_BACKOFF[2],
        service_runner.HEALTHY_POLL_INTERVAL, service_runner.RESTART_BACKOFF[0],
    ]
    assert env.wait_calls == expected
    # The fake clock only advances via waitForAbort() -- total elapsed
    # time is exactly the sum of the documented cadence, proving the
    # supervisor paces itself rather than just emitting the right
    # numbers without ever "spending" them.
    assert env.clock.now() == pytest.approx(sum(expected))

def test_main_crash_path_closes_the_log_file_handle(fake_popen, monkeypatch, tmp_path):  # noqa: F811
    """The crash branch (~main()'s `elif proc is not None:` / `else:` arm)
    must call `proc.stop()` itself instead of dropping the `ServerProcess`
    reference and leaving `_log_fh` open for GC to eventually close.
    Exercised with the *real* `ServerProcess` (wrapped, not `ScriptedProcess`)
    so `log_fh.closed` genuinely proves `stop()` ran.
    """
    monkeypatch.setattr(service_runner, 'probe_listening', lambda *a, **kw: False)
    monkeypatch.setattr(service_runner, 'resolve_binary', lambda *a, **kw: '/opt/bin/stremio-server')

    real_server_process = service_runner.ServerProcess
    created = []

    def factory(*args, **kwargs):
        sp = real_server_process(*args, **kwargs)
        created.append(sp)
        return sp

    monkeypatch.setattr(service_runner, 'ServerProcess', factory)

    log_fh_box = {}

    def crash_it(monitor):
        # Runs during the waitForAbort() call ending the spawn iteration,
        # once the log file is open: grab the handle now, then flip the
        # fake child's exit code so the *next* iteration's poll() sees a
        # crash.
        log_fh_box['fh'] = created[0]._log_fh
        fake_popen[0]['proc'].poll_result = 1

    intervals = []
    wait = _scripted_wait(intervals, [crash_it, None])
    with _main_env(tmp_path, wait, settings={'server_enable': True}):
        service_runner.main()

    assert len(created) == 1
    log_fh = log_fh_box['fh']
    assert log_fh is not None
    assert log_fh.closed is True


# --- supervisor containment: process/download failures never crash main() -


def test_main_survives_failed_spawn_and_retries_with_backoff(monkeypatch, tmp_path):
    """A ServerProcess.start() failure (e.g. exec() denied, ENOENT after a
    TOCTOU binary removal) must not crash main() or spawn a duplicate: the
    failed instance is discarded, a bounded restart backoff applies
    (not a tight loop), and the very next spawn attempt gets a fresh
    ServerProcess."""
    monkeypatch.setattr(service_runner, 'probe_listening', lambda *a, **kw: False)
    monkeypatch.setattr(service_runner, 'resolve_binary', lambda *a, **kw: '/opt/bin/stremio-server')
    specs = [
        {'start_exceptions': [OSError('exec failed')]},
        {},
    ]
    factory, spawned = _make_process_factory(specs)
    monkeypatch.setattr(service_runner, 'ServerProcess', factory)

    intervals = []
    wait = _scripted_wait(intervals, [None, None])
    with _main_env(tmp_path, wait, settings={'server_enable': True}) as ctx:
        service_runner.main()

    assert len(spawned) == 2  # the failed instance is discarded, not retried
    assert spawned[0].start_calls == 1
    assert spawned[1].start_calls == 1
    assert intervals[0] == service_runner.RESTART_BACKOFF[0]
    assert intervals[1] == service_runner.HEALTHY_POLL_INTERVAL

    error_logs = [msg for msg, level in ctx.env.log_calls if level == ctx.xbmc.LOGERROR]
    assert any('failed to start embedded server' in msg for msg in error_logs)
    # main() kept running -- the second spawn succeeded and gets shut
    # down cleanly at the end.
    assert spawned[1].stop_calls == 1


def test_main_survives_failed_stop_and_defers_respawn_until_confirmed_stopped(monkeypatch, tmp_path):
    """A stop() failure (e.g. an unkillable/wedged child) during
    crash-cleanup must not be swallowed into discarding the ServerProcess:
    main() keeps polling the SAME instance next iteration instead of
    spawning a duplicate next to a possibly-still-alive process, and only
    respawns once stop() finally succeeds."""
    monkeypatch.setattr(service_runner, 'probe_listening', lambda *a, **kw: False)
    monkeypatch.setattr(service_runner, 'resolve_binary', lambda *a, **kw: '/opt/bin/stremio-server')
    specs = [
        {'poll_sequence': [1, 1], 'stop_exceptions': [OSError('kill failed'), None]},
        {},
    ]
    factory, spawned = _make_process_factory(specs)
    monkeypatch.setattr(service_runner, 'ServerProcess', factory)

    intervals = []
    wait = _scripted_wait(intervals, [None, None, None, None])
    with _main_env(tmp_path, wait, settings={'server_enable': True}) as ctx:
        service_runner.main()

    assert len(spawned) == 2  # the wedged instance was never duplicated
    wedged, replacement = spawned
    assert wedged.stop_calls == 2  # retried stop() until it finally succeeded
    assert replacement.start_calls == 1

    error_logs = [msg for msg, level in ctx.env.log_calls if level == ctx.xbmc.LOGERROR]
    assert any('failed to stop embedded server' in msg for msg in error_logs)


# --- (e) settings-changed restart -------------------------------------------


def test_main_settings_changed_restarts_the_running_server(monkeypatch, tmp_path):
    monkeypatch.setattr(service_runner, 'probe_listening', lambda *a, **kw: False)
    monkeypatch.setattr(service_runner, 'resolve_binary', lambda *a, **kw: '/opt/bin/stremio-server')
    factory, spawned = _make_process_factory([{}, {}])
    monkeypatch.setattr(service_runner, 'ServerProcess', factory)

    env_box = {}

    def change_server_url_and_signal(monitor):
        # Simulates Kodi invoking the Monitor hook asynchronously once
        # settings.xml is saved with a new server_url.
        env_box['env'].addon.settings['server_url'] = 'http://127.0.0.1:9999'
        monitor.onSettingsChanged()

    intervals = []
    wait = _scripted_wait(intervals, [None, change_server_url_and_signal, None, None])
    with _main_env(tmp_path, wait, settings={'server_enable': True}) as ctx:
        env_box['env'] = ctx.env
        service_runner.main()

    assert len(spawned) == 2
    old_proc, new_proc = spawned
    assert old_proc.stop_calls == 1  # stopped by the restart, not by shutdown
    assert new_proc.server_url == 'http://127.0.0.1:9999'
    assert new_proc.stop_calls == 1  # then stopped again by the final shutdown path

    restart_logs = [msg for msg, _level in ctx.env.log_calls if 'settings changed, restarting' in msg]
    assert len(restart_logs) == 1


# --- (f) embedded disabled: stop --------------------------------------------


def test_main_embedded_disabled_stops_the_running_server(monkeypatch, tmp_path):
    monkeypatch.setattr(service_runner, 'probe_listening', lambda *a, **kw: False)
    monkeypatch.setattr(service_runner, 'resolve_binary', lambda *a, **kw: '/opt/bin/stremio-server')
    factory, spawned = _make_process_factory([{}])
    monkeypatch.setattr(service_runner, 'ServerProcess', factory)

    intervals = []
    # Flip `enabled` directly on the live monitor instance (bypassing
    # onSettingsChanged()/restart_requested entirely) to isolate the
    # "disabled -> stop" branch from the "settings changed -> restart"
    # branch exercised by the test above.
    wait = _scripted_wait(intervals, [None, lambda m: setattr(m, 'enabled', False), None, None])
    with _main_env(tmp_path, wait, settings={'server_enable': True}) as ctx:
        service_runner.main()

    assert len(spawned) == 1
    assert spawned[0].stop_calls == 1
    # Once disabled, the interval falls back to the idle default (it is
    # never reassigned in the "not enabled" branch) for every subsequent
    # iteration, and neither probe_listening/resolve_binary/ServerProcess
    # run again while disabled.
    assert intervals[2] == service_runner.IDLE_POLL_INTERVAL
    assert intervals[3] == service_runner.IDLE_POLL_INTERVAL

    disable_logs = [msg for msg, _level in ctx.env.log_calls if 'embedded server disabled, stopping' in msg]
    assert len(disable_logs) == 1
    # proc is already None by the time the loop exits -> no second,
    # shutdown-path stop() call.
    assert not any('shutting down embedded server' in msg for msg, _level in ctx.env.log_calls)


# --- edge cases: no-op resave, immediate abort, restart with no proc -------


def test_main_onsettingschanged_with_no_actual_change_does_not_restart(monkeypatch, tmp_path):
    """Kodi fires `Monitor.onSettingsChanged()` for ANY settings.xml save
    of this addon, even one that only touched an unrelated key (e.g.
    subs_language) -- a resave that leaves (enabled, binary, url)
    unchanged must not restart an already-healthy server."""
    monkeypatch.setattr(service_runner, 'probe_listening', lambda *a, **kw: False)
    monkeypatch.setattr(service_runner, 'resolve_binary', lambda *a, **kw: '/opt/bin/stremio-server')
    factory, spawned = _make_process_factory([{}])
    monkeypatch.setattr(service_runner, 'ServerProcess', factory)

    def resave_without_changes(monitor):
        monitor.onSettingsChanged()

    intervals = []
    wait = _scripted_wait(intervals, [None, resave_without_changes, None])
    with _main_env(tmp_path, wait, settings={'server_enable': True}) as ctx:
        service_runner.main()

    assert len(spawned) == 1  # never restarted -> never respawned
    assert spawned[0].stop_calls == 1  # only the final shutdown-path stop
    assert not any('settings changed, restarting' in msg for msg, _level in ctx.env.log_calls)


def test_main_aborts_immediately_before_the_loop_body_ever_runs(monkeypatch, tmp_path):
    """`abortRequested()` is the `while` condition itself: when it is
    already true on entry, the loop body -- and therefore
    probe_listening/resolve_binary/ServerProcess -- must never run, and
    main() must still return cleanly (no proc to shut down)."""
    monkeypatch.setattr(
        service_runner, 'probe_listening',
        lambda *a, **kw: pytest.fail('probe_listening must not run'),
    )
    monkeypatch.setattr(
        service_runner, 'resolve_binary',
        lambda *a, **kw: pytest.fail('resolve_binary must not run'),
    )
    factory, spawned = _make_process_factory([])
    monkeypatch.setattr(service_runner, 'ServerProcess', factory)

    with _main_env(tmp_path, waitforabort=None, settings={'server_enable': True}) as ctx:
        ctx.xbmc.Monitor.abortRequested = lambda self: True
        service_runner.main()  # must return cleanly without calling waitForAbort at all

    assert spawned == []


def test_main_settings_changed_with_no_running_server_resets_state_without_crashing(monkeypatch, tmp_path):
    """The restart_requested handling's `if proc is not None` guard must
    actually gate the stop()/log call -- and a settings change while
    nothing is running (binary still missing, download still failing)
    must reset backoff_idx/notified_missing/download retry state
    (including the monotonic cooldown deadline) without touching a None
    proc, giving install_binary() an immediate fresh attempt instead of
    waiting out the remaining backoff."""
    monkeypatch.setattr(service_runner, 'probe_listening', lambda *a, **kw: False)
    monkeypatch.setattr(service_runner, 'resolve_binary', lambda *a, **kw: None)  # binary missing throughout

    install_calls = []

    def fake_install_binary(dest_dir, progress_cb=None):
        install_calls.append(dest_dir)
        raise serverbin.DownloadError('still no network')

    monkeypatch.setattr(serverbin, 'install_binary', fake_install_binary)
    factory, spawned = _make_process_factory([])
    monkeypatch.setattr(service_runner, 'ServerProcess', factory)

    env_box = {}

    def change_binary_setting_and_signal(monitor):
        env_box['env'].addon.settings['server_binary'] = '/new/path'
        monitor.onSettingsChanged()

    # iter1: binary missing -> auto-download attempted and fails, arming a
    # cooldown far longer than this test's real wall-clock runtime. iter2:
    # still cooling down (negligible real time elapsed) so no second
    # attempt yet -- must not crash trying to stop() a None proc. The
    # settings change fires during this wait. iter3: the reset cooldown
    # deadline lets install_binary() run again immediately (and it fails
    # again too).
    intervals = []
    wait = _scripted_wait(intervals, [None, change_binary_setting_and_signal, None])
    with _main_env(tmp_path, wait, settings={'server_enable': True}) as ctx:
        env_box['env'] = ctx.env
        service_runner.main()  # must not crash trying to stop() a None proc

    assert spawned == []
    assert install_calls == [os.path.join(str(tmp_path), 'bin')] * 2
    setup_notifications = [n for n in ctx.env.notifications if n[1] == 'STR30069']
    failed_notifications = [n for n in ctx.env.notifications if n[1] == 'STR30063']
    # Two separate download attempts (one per settings "generation"), each
    # failing -- proving the cooldown deadline really was reset, not just
    # skipped by coincidence.
    assert len(setup_notifications) == 2
    assert len(failed_notifications) == 2
    assert not any('settings changed, restarting' in msg for msg, _level in ctx.env.log_calls)


# --- (i) extra-env settings changes also trigger a restart -----------------


def test_main_onsettingschanged_extra_env_setting_change_triggers_restart(monkeypatch, tmp_path):
    """Changing exactly one of the 30 new env-var-forwarding settings
    (`disable_trackers`) must trigger a restart just like a `server_url`
    change already does, and the respawned process must carry the new
    value through `extra_env`."""
    monkeypatch.setattr(service_runner, 'probe_listening', lambda *a, **kw: False)
    monkeypatch.setattr(service_runner, 'resolve_binary', lambda *a, **kw: '/opt/bin/stremio-server')
    factory, spawned = _make_process_factory([{}, {}])
    monkeypatch.setattr(service_runner, 'ServerProcess', factory)

    env_box = {}
    settings = {'server_enable': True}
    settings.update(_EXTRA_ENV_DEFAULTS)

    def flip_disable_trackers_and_signal(monitor):
        env_box['env'].addon.settings['disable_trackers'] = True
        monitor.onSettingsChanged()

    intervals = []
    wait = _scripted_wait(intervals, [None, flip_disable_trackers_and_signal, None, None])
    with _main_env(tmp_path, wait, settings=settings) as ctx:
        env_box['env'] = ctx.env
        service_runner.main()

    assert len(spawned) == 2
    old_proc, new_proc = spawned
    assert old_proc.stop_calls == 1  # stopped by the restart, not by shutdown
    assert new_proc.extra_env.get('STREMIO_DISABLE_TRACKERS') == 'true'
    assert new_proc.stop_calls == 1  # then stopped again by the final shutdown path

    restart_logs = [msg for msg, _level in ctx.env.log_calls if 'settings changed, restarting' in msg]
    assert len(restart_logs) == 1


def test_main_onsettingschanged_extra_env_resave_without_change_does_not_restart(monkeypatch, tmp_path):
    """A resave that leaves every one of the 30 extra-env settings (seeded
    at their real Kodi defaults) unchanged must not restart an
    already-healthy server, exactly like the plain
    `test_main_onsettingschanged_with_no_actual_change_does_not_restart`
    case above for (enabled, binary, url)."""
    monkeypatch.setattr(service_runner, 'probe_listening', lambda *a, **kw: False)
    monkeypatch.setattr(service_runner, 'resolve_binary', lambda *a, **kw: '/opt/bin/stremio-server')
    factory, spawned = _make_process_factory([{}])
    monkeypatch.setattr(service_runner, 'ServerProcess', factory)

    settings = {'server_enable': True}
    settings.update(_EXTRA_ENV_DEFAULTS)

    def resave_without_changes(monitor):
        monitor.onSettingsChanged()

    intervals = []
    wait = _scripted_wait(intervals, [None, resave_without_changes, None])
    with _main_env(tmp_path, wait, settings=settings) as ctx:
        service_runner.main()

    assert len(spawned) == 1  # never restarted -> never respawned
    assert spawned[0].stop_calls == 1  # only the final shutdown-path stop
    assert not any('settings changed, restarting' in msg for msg, _level in ctx.env.log_calls)


# ===========================================================================
# AutoloadTrigger: open Rivulet's UI once per Kodi session (pure half)
# ===========================================================================


def _trigger(gui_ready=True, started_at=0.0, **kwargs):
    """An `AutoloadTrigger` with recording collaborators. Returns
    `(trigger, launches)`; `gui_ready` is a bool or a zero-arg callable."""
    launches = []
    ready = gui_ready if callable(gui_ready) else (lambda: gui_ready)
    trigger = service_runner.AutoloadTrigger(
        gui_ready_fn=ready, launch_fn=lambda: launches.append(True),
        started_at=started_at, **kwargs
    )
    return trigger, launches


def test_autoload_disabled_never_launches_and_asks_for_no_wakeups():
    """The default (setting off): `poll()` must be a pure no-op, never
    shortening `main()`'s supervision interval."""
    trigger, launches = _trigger(disabled=True)

    assert [trigger.poll(t) for t in (0.0, 100.0, 10000.0)] == [None, None, None]
    assert launches == []


def test_autoload_waits_for_the_gui_before_arming_the_settle_delay():
    """A service that starts before the skin is up must not fire into a
    still-loading GUI -- it polls until `Window.IsVisible(home)`."""
    ready = {'value': False}
    trigger, launches = _trigger(gui_ready=lambda: ready['value'])

    assert trigger.poll(0.0) == service_runner.AUTOLOAD_READY_POLL_INTERVAL
    assert trigger.poll(30.0) == service_runner.AUTOLOAD_READY_POLL_INTERVAL
    assert launches == []

    ready['value'] = True
    trigger.poll(30.0)
    assert launches == []  # GUI is up, but the settle delay has not elapsed yet


def test_autoload_launches_once_after_the_settle_delay():
    trigger, launches = _trigger(settle_delay=5.0)

    assert trigger.poll(0.0) is not None      # GUI ready -> settle delay armed
    assert launches == []
    assert trigger.poll(4.9) is not None      # still settling
    assert launches == []
    assert trigger.poll(5.0) is None          # deadline reached -> fire
    assert launches == [True]
    assert trigger.fired is True


def test_autoload_latches_after_firing_and_never_launches_twice():
    """One launch per Kodi session: `main()` polls this every loop
    iteration for the rest of the session."""
    trigger, launches = _trigger(settle_delay=0.0)

    for t in (0.0, 1.0, 2.0, 600.0):
        trigger.poll(t)

    assert launches == [True]


def test_autoload_launches_anyway_once_the_ready_timeout_expires():
    """An unusual skin that never reports `Window.IsVisible(home)` must
    not silently disable the feature for the whole session."""
    trigger, launches = _trigger(
        gui_ready=False, ready_timeout=60.0, settle_delay=5.0,
    )

    assert trigger.poll(59.0) == service_runner.AUTOLOAD_READY_POLL_INTERVAL
    assert launches == []
    trigger.poll(60.0)          # timeout expired -> arm the settle delay anyway
    assert launches == []
    trigger.poll(65.0)
    assert launches == [True]


def test_autoload_never_asks_main_to_sleep_past_its_own_launch_deadline():
    """The returned interval is what `main()` caps its sleep at, so it
    must never overshoot the remaining settle time."""
    trigger, _launches = _trigger(settle_delay=10.0, poll_interval=1.0)

    trigger.poll(0.0)
    assert trigger.poll(9.5) == pytest.approx(0.5)


# ===========================================================================
# startup autoload wired into main()
# ===========================================================================


def _autoload_main_env(tmp_path, monkeypatch, wait, settings, cond_visibility=True, tick=1.0):
    """`main()` with an external server already answering, so the
    supervision half is inert and only the autoload behaviour varies.

    `main()`'s autoload clock is driven off `time.monotonic()`, which
    barely advances across a scripted loop that runs in microseconds --
    so it is replaced here by a fake advancing `tick` seconds per read,
    making the settle delay elapse deterministically instead of
    depending on how fast the test host happens to be.
    """
    monkeypatch.setattr(service_runner, 'probe_listening', lambda url, **kw: True)
    factory, _spawned = _make_process_factory([])
    monkeypatch.setattr(service_runner, 'ServerProcess', factory)

    clock = {'now': 0.0}

    def fake_monotonic():
        now = clock['now']
        clock['now'] += tick
        return now

    monkeypatch.setattr(service_runner.time, 'monotonic', fake_monotonic)
    return _main_env(tmp_path, wait, settings=settings, cond_visibility=cond_visibility)


def test_main_does_not_autoload_when_the_setting_is_off(monkeypatch, tmp_path):
    """Default-off: a stock install must never pop the UI open by itself."""
    intervals = []
    wait = _scripted_wait(intervals, [None] * 3)
    settings = {'server_enable': True, 'startup_autoload': False}
    with _autoload_main_env(tmp_path, monkeypatch, wait, settings) as ctx:
        service_runner.main()

    assert ctx.env.executed_builtins == []
    # An inert trigger must not shorten the external-server recheck either.
    assert intervals == [service_runner.EXTERNAL_RECHECK_INTERVAL] * 3


def test_main_autoloads_the_addon_ui_when_the_setting_is_on(monkeypatch, tmp_path):
    intervals = []
    wait = _scripted_wait(intervals, [None] * 20)
    settings = {'server_enable': True, 'startup_autoload': True}
    with _autoload_main_env(tmp_path, monkeypatch, wait, settings) as ctx:
        service_runner.main()

    assert ctx.env.executed_builtins == [service_runner.AUTOLOAD_BUILTIN]
    assert service_runner.AUTOLOAD_BUILTIN == 'RunAddon(plugin.video.rivulet)'
    assert any('startup autoload' in msg for msg, _level in ctx.env.log_calls)


def test_main_autoload_shortens_the_supervision_interval_while_it_waits(monkeypatch, tmp_path):
    """The external-server branch would otherwise sleep 10s per
    iteration, delaying the launch well past the settle delay."""
    intervals = []
    wait = _scripted_wait(intervals, [None] * 3)
    settings = {'server_enable': True, 'startup_autoload': True}
    with _autoload_main_env(tmp_path, monkeypatch, wait, settings):
        service_runner.main()

    assert intervals[0] < service_runner.EXTERNAL_RECHECK_INTERVAL
    assert intervals[0] <= service_runner.AUTOLOAD_READY_POLL_INTERVAL


def test_main_autoload_launch_failure_never_crashes_the_service(monkeypatch, tmp_path):
    """`executebuiltin` raising must be contained: the supervision loop
    is the whole point of the service, the autoload is a convenience."""
    intervals = []
    wait = _scripted_wait(intervals, [None] * 20)
    settings = {'server_enable': True, 'startup_autoload': True}
    with _autoload_main_env(tmp_path, monkeypatch, wait, settings) as ctx:
        def boom(function, wait=False):
            raise RuntimeError('no GUI')

        sys.modules['xbmc'].executebuiltin = boom
        service_runner.main()

    assert len(intervals) == 20  # the loop ran to completion regardless
    assert any('startup autoload failed' in msg for msg, _level in ctx.env.log_calls)


def test_main_autoload_poll_failure_is_contained_and_latched_off(monkeypatch, tmp_path):
    """The outer guard around `AutoloadTrigger.poll()` itself (as
    opposed to the launch): a trigger that raises is latched off rather
    than raising once per loop iteration for the rest of the session."""
    class Exploding:
        def __init__(self):
            self.polls = 0
            self.fired = False

        def poll(self, now):
            self.polls += 1
            raise RuntimeError('boom')

    exploding = Exploding()
    monkeypatch.setattr(service_runner, 'AutoloadTrigger', lambda **kw: exploding)

    intervals = []
    wait = _scripted_wait(intervals, [None] * 4)
    settings = {'server_enable': True, 'startup_autoload': True}
    with _autoload_main_env(tmp_path, monkeypatch, wait, settings) as ctx:
        service_runner.main()

    assert exploding.polls == 1  # latched off after the first failure
    assert len(intervals) == 4   # ... and the supervision loop carried on
    assert any('startup autoload failed' in msg for msg, _level in ctx.env.log_calls)


def test_main_autoload_does_not_fire_while_the_gui_is_still_loading(monkeypatch, tmp_path):
    intervals = []
    wait = _scripted_wait(intervals, [None] * 3)
    settings = {'server_enable': True, 'startup_autoload': True}
    with _autoload_main_env(
        tmp_path, monkeypatch, wait, settings, cond_visibility=lambda cond: False,
    ) as ctx:
        service_runner.main()

    assert ctx.env.executed_builtins == []


def test_main_autoload_probes_kodis_home_window_for_gui_readiness(monkeypatch, tmp_path):
    conditions = []

    def record(cond):
        conditions.append(cond)
        return True

    intervals = []
    wait = _scripted_wait(intervals, [None] * 20)
    settings = {'server_enable': True, 'startup_autoload': True}
    with _autoload_main_env(tmp_path, monkeypatch, wait, settings, cond_visibility=record):
        service_runner.main()

    assert conditions and all(c == 'Window.IsVisible(home)' for c in conditions)



# --- SERVER_TAG upgrade of the bundled binary -------------------------------


def _bundled(tmp_path):
    return os.path.join(str(tmp_path), 'bin', service_runner.BINARY_NAME)


def test_main_upgrades_a_bundled_binary_installed_under_an_older_server_tag(monkeypatch, tmp_path):
    """install_binary() is otherwise only reachable when resolve_binary()
    finds nothing, so a SERVER_TAG bump in a new addon release would never
    reach anyone who already has a binary. Before spawning, a bundled
    binary whose stamp disagrees with SERVER_TAG is reinstalled, and the
    freshly-returned path -- not the stale one -- is what gets spawned."""
    monkeypatch.setattr(service_runner, 'probe_listening', lambda *a, **kw: False)
    monkeypatch.setattr(service_runner, 'resolve_binary', lambda *a, **kw: _bundled(tmp_path))
    monkeypatch.setattr(serverbin, 'installed_tag', lambda dest_dir: 'v0.1.0')

    install_calls = []
    fresh = os.path.join(str(tmp_path), 'bin', 'freshly-installed')

    def fake_install_binary(dest_dir, progress_cb=None):
        install_calls.append(dest_dir)
        return fresh

    monkeypatch.setattr(serverbin, 'install_binary', fake_install_binary)
    factory, spawned = _make_process_factory([])
    monkeypatch.setattr(service_runner, 'ServerProcess', factory)

    with _main_env(tmp_path, _scripted_wait([], [None]), settings={'server_enable': True}) as ctx:
        service_runner.main()

    assert install_calls == [os.path.join(str(tmp_path), 'bin')]
    assert [p.binary for p in spawned] == [fresh]
    assert [n for n in ctx.env.notifications if n[1] == 'STR30069']
    assert any(
        'upgrading stremio-server binary' in msg and serverbin.SERVER_TAG in msg
        for msg, level in ctx.env.log_calls if level == ctx.xbmc.LOGINFO
    )


def test_main_does_not_reinstall_a_bundled_binary_already_at_the_current_tag(monkeypatch, tmp_path):
    monkeypatch.setattr(service_runner, 'probe_listening', lambda *a, **kw: False)
    monkeypatch.setattr(service_runner, 'resolve_binary', lambda *a, **kw: _bundled(tmp_path))
    monkeypatch.setattr(serverbin, 'installed_tag', lambda dest_dir: serverbin.SERVER_TAG)

    def install_must_not_run(*args, **kwargs):
        pytest.fail('install_binary must not run for an up-to-date binary')

    monkeypatch.setattr(serverbin, 'install_binary', install_must_not_run)
    factory, spawned = _make_process_factory([])
    monkeypatch.setattr(service_runner, 'ServerProcess', factory)

    with _main_env(tmp_path, _scripted_wait([], [None]), settings={'server_enable': True}) as ctx:
        service_runner.main()

    assert [p.binary for p in spawned] == [_bundled(tmp_path)]
    assert [n for n in ctx.env.notifications if n[1] == 'STR30069'] == []


def test_main_never_replaces_a_binary_it_did_not_install(monkeypatch, tmp_path):
    """An explicit `server_binary` setting or a PATH hit is the user's own
    build: unstamped, so `installed_tag()` would report None and look
    stale. It must not even be consulted, let alone overwritten."""
    monkeypatch.setattr(service_runner, 'probe_listening', lambda *a, **kw: False)
    monkeypatch.setattr(service_runner, 'resolve_binary', lambda *a, **kw: '/usr/bin/stremio-server')

    def installed_tag_must_not_run(*args, **kwargs):
        pytest.fail('installed_tag must not be consulted for a non-bundled binary')

    def install_must_not_run(*args, **kwargs):
        pytest.fail('install_binary must not replace a user-supplied binary')

    monkeypatch.setattr(serverbin, 'installed_tag', installed_tag_must_not_run)
    monkeypatch.setattr(serverbin, 'install_binary', install_must_not_run)
    factory, spawned = _make_process_factory([])
    monkeypatch.setattr(service_runner, 'ServerProcess', factory)

    with _main_env(tmp_path, _scripted_wait([], [None]), settings={'server_enable': True}):
        service_runner.main()

    assert [p.binary for p in spawned] == ['/usr/bin/stremio-server']


def test_main_never_replaces_a_binary_the_server_binary_setting_points_at(monkeypatch, tmp_path):
    """`server_binary` may name the bundled path itself -- someone who
    dropped a hand-built binary exactly where we install ours.
    resolve_binary() returns it from its explicit branch, so the path
    alone cannot tell it apart from one we installed; the setting decides."""
    monkeypatch.setattr(service_runner, 'probe_listening', lambda *a, **kw: False)
    monkeypatch.setattr(service_runner, 'resolve_binary', lambda *a, **kw: _bundled(tmp_path))

    def installed_tag_must_not_run(*args, **kwargs):
        pytest.fail('installed_tag must not be consulted for a user-named binary')

    def install_must_not_run(*args, **kwargs):
        pytest.fail('install_binary must not replace a user-named binary')

    monkeypatch.setattr(serverbin, 'installed_tag', installed_tag_must_not_run)
    monkeypatch.setattr(serverbin, 'install_binary', install_must_not_run)
    factory, spawned = _make_process_factory([])
    monkeypatch.setattr(service_runner, 'ServerProcess', factory)

    settings = {'server_enable': True, 'server_binary': _bundled(tmp_path)}
    with _main_env(tmp_path, _scripted_wait([], [None]), settings=settings):
        service_runner.main()

    assert [p.binary for p in spawned] == [_bundled(tmp_path)]


def test_main_failed_upgrade_still_starts_the_installed_binary(monkeypatch, tmp_path):
    """An offline user with a stale binary must still get a server. A
    failed upgrade falls back to the binary already on disk instead of
    entering the missing-binary download backoff."""
    monkeypatch.setattr(service_runner, 'probe_listening', lambda *a, **kw: False)
    monkeypatch.setattr(service_runner, 'resolve_binary', lambda *a, **kw: _bundled(tmp_path))
    monkeypatch.setattr(serverbin, 'installed_tag', lambda dest_dir: 'v0.1.0')

    def fake_install_binary(dest_dir, progress_cb=None):
        raise serverbin.DownloadError('github unreachable')

    monkeypatch.setattr(serverbin, 'install_binary', fake_install_binary)
    factory, spawned = _make_process_factory([])
    monkeypatch.setattr(service_runner, 'ServerProcess', factory)

    with _main_env(tmp_path, _scripted_wait([], [None]), settings={'server_enable': True}) as ctx:
        service_runner.main()

    assert [p.binary for p in spawned] == [_bundled(tmp_path)]
    assert any(
        'upgrade' in msg and 'keeping the installed one' in msg
        for msg, level in ctx.env.log_calls if level == ctx.xbmc.LOGWARNING
    )


def test_main_attempts_the_upgrade_at_most_once_per_session(monkeypatch, tmp_path):
    """The spawn path runs again on every failed start (5s apart), so
    without the one-shot latch a GitHub outage would re-download the
    archive on every retry."""
    monkeypatch.setattr(service_runner, 'probe_listening', lambda *a, **kw: False)
    monkeypatch.setattr(service_runner, 'resolve_binary', lambda *a, **kw: _bundled(tmp_path))
    monkeypatch.setattr(serverbin, 'installed_tag', lambda dest_dir: 'v0.1.0')

    install_calls = []

    def fake_install_binary(dest_dir, progress_cb=None):
        install_calls.append(dest_dir)
        raise serverbin.DownloadError('github unreachable')

    monkeypatch.setattr(serverbin, 'install_binary', fake_install_binary)
    # Both spawns fail, so `proc` stays None and the binary is re-resolved
    # on the next iteration -- the exact shape the latch has to survive.
    factory, spawned = _make_process_factory([
        {'start_exceptions': [OSError('boom')]},
        {'start_exceptions': [OSError('boom')]},
    ])
    monkeypatch.setattr(service_runner, 'ServerProcess', factory)

    with _main_env(tmp_path, _scripted_wait([], [None, None]), settings={'server_enable': True}):
        service_runner.main()

    assert len(spawned) == 2
    assert len(install_calls) == 1


def test_main_upgrade_aborted_by_shutdown_unwinds_without_spawning(monkeypatch, tmp_path):
    """The upgrade download shares main()'s abort-aware progress callback:
    a Kodi shutdown mid-transfer must unwind the loop rather than spawn a
    server we are about to tear down."""
    monkeypatch.setattr(service_runner, 'probe_listening', lambda *a, **kw: False)
    monkeypatch.setattr(service_runner, 'resolve_binary', lambda *a, **kw: _bundled(tmp_path))
    monkeypatch.setattr(serverbin, 'installed_tag', lambda dest_dir: 'v0.1.0')

    progress_calls = []

    def fake_install_binary(dest_dir, progress_cb=None):
        """Abort the way the real download does: install_binary() itself
        never raises _AbortRequested -- it is main()'s own progress
        callback that does, once per chunk, when Kodi asks to shut down
        mid-transfer. Raising directly here would keep passing even if the
        upgrade call stopped handing over a progress_cb at all."""
        progress_calls.append((dest_dir, progress_cb))
        progress_cb(0, 1024)  # before the shutdown request: must not raise
        sys.modules['xbmc'].Monitor.abortRequested = lambda self: True
        progress_cb(512, 1024)
        pytest.fail('progress_cb must raise _AbortRequested once abort is requested')

    monkeypatch.setattr(serverbin, 'install_binary', fake_install_binary)
    factory, spawned = _make_process_factory([])
    monkeypatch.setattr(service_runner, 'ServerProcess', factory)

    with _main_env(tmp_path, _scripted_wait([], [None]), settings={'server_enable': True}) as ctx:
        service_runner.main()

    assert spawned == []
    assert len(progress_calls) == 1
    assert progress_calls[0][1] is not None
    assert any(
        'upgrade aborted, shutting down' in msg
        for msg, level in ctx.env.log_calls if level == ctx.xbmc.LOGINFO
    )


# ===========================================================================
# main(): c-shared library mode (lib.libserver.LibraryServer) selection
# ===========================================================================


def test_main_force_library_prefers_library_mode_when_companion_library_present(monkeypatch, tmp_path):
    """`server_force_library=True` skips resolve_binary()/install_binary()
    entirely and goes straight to c-shared library mode whenever a
    companion libstremio-server.so is already on disk."""

    def resolve_binary_must_not_run(*args, **kwargs):
        pytest.fail('resolve_binary must not run when server_force_library is set '
                    'and a companion library is available')

    def install_binary_must_not_run(*args, **kwargs):
        pytest.fail('install_binary must not run when server_force_library is set '
                    'and a companion library is available')

    monkeypatch.setattr(service_runner, 'probe_listening', lambda *a, **kw: False)
    monkeypatch.setattr(service_runner, 'resolve_binary', resolve_binary_must_not_run)
    monkeypatch.setattr(serverbin, 'install_binary', install_binary_must_not_run)
    monkeypatch.setattr(serverbin, 'resolve_library', lambda dest_dir: '/opt/lib/libstremio-server.so')

    factory, spawned = _make_library_process_factory([{}])
    monkeypatch.setattr(libserver, 'LibraryServer', factory)

    intervals = []
    wait = _scripted_wait(intervals, [None, None])
    settings = {'server_enable': True, 'server_force_library': True}
    with _main_env(tmp_path, wait, settings=settings) as ctx:
        service_runner.main()

    assert len(spawned) == 1
    proc = spawned[0]
    assert proc.library_path == '/opt/lib/libstremio-server.so'
    assert proc.server_url == service_runner.DEFAULT_SERVER_URL
    assert proc.start_calls == 1
    assert intervals == [service_runner.HEALTHY_POLL_INTERVAL] * 2
    assert len([n for n in ctx.env.notifications if n[1] == 'STR30364']) == 1
    # main() returned with the library server still alive -> the
    # post-loop shutdown path stops it exactly once, same as ServerProcess.
    assert proc.stop_calls == 1
    assert any('starting library-mode server' in msg for msg, _level in ctx.env.log_calls)


def test_main_force_library_falls_back_to_binary_flow_when_no_companion_library(monkeypatch, tmp_path):
    """`server_force_library=True` with nothing on disk to load must fall
    straight through to the ordinary resolve_binary()/install_binary()
    flow instead of getting stuck."""
    monkeypatch.setattr(service_runner, 'probe_listening', lambda *a, **kw: False)
    monkeypatch.setattr(service_runner, 'resolve_binary', lambda *a, **kw: '/opt/bin/stremio-server')
    monkeypatch.setattr(serverbin, 'resolve_library', lambda dest_dir: None)

    def library_factory_must_not_run(*args, **kwargs):
        pytest.fail('LibraryServer must not be constructed with no companion library available')

    monkeypatch.setattr(libserver, 'LibraryServer', library_factory_must_not_run)
    factory, spawned = _make_process_factory([{}])
    monkeypatch.setattr(service_runner, 'ServerProcess', factory)

    intervals = []
    wait = _scripted_wait(intervals, [None, None])
    settings = {'server_enable': True, 'server_force_library': True}
    with _main_env(tmp_path, wait, settings=settings):
        service_runner.main()

    assert len(spawned) == 1
    assert spawned[0].binary == '/opt/bin/stremio-server'


def test_main_force_library_falls_back_to_binary_flow_when_ctypes_unsupported(monkeypatch, tmp_path):
    """Even with a companion library on disk and the setting enabled,
    ctypes being unavailable in this Python build must fall back to the
    ordinary exec()-based flow -- `_resolve_library_candidate()` must
    short-circuit on `LIBRARY_SUPPORTED` before even asking
    `serverbin.resolve_library()`."""
    monkeypatch.setattr(service_runner, 'probe_listening', lambda *a, **kw: False)
    monkeypatch.setattr(service_runner, 'resolve_binary', lambda *a, **kw: '/opt/bin/stremio-server')
    monkeypatch.setattr(libserver, 'LIBRARY_SUPPORTED', False)

    def resolve_library_must_not_run(*args, **kwargs):
        pytest.fail('resolve_library must not be consulted when ctypes is unsupported')

    monkeypatch.setattr(serverbin, 'resolve_library', resolve_library_must_not_run)
    factory, spawned = _make_process_factory([{}])
    monkeypatch.setattr(service_runner, 'ServerProcess', factory)

    intervals = []
    wait = _scripted_wait(intervals, [None, None])
    settings = {'server_enable': True, 'server_force_library': True}
    with _main_env(tmp_path, wait, settings=settings):
        service_runner.main()

    assert len(spawned) == 1
    assert spawned[0].binary == '/opt/bin/stremio-server'


def test_main_unsupported_platform_falls_back_to_library_mode_immediately(monkeypatch, tmp_path):
    """install_binary() raising UnsupportedPlatformError must prefer an
    already-extracted companion library over latching
    `unsupported_platform` -- it extracts the .so regardless of whether
    the executable itself passed verify_executable() (see
    serverbin.install_binary()'s docstring)."""
    monkeypatch.setattr(service_runner, 'probe_listening', lambda *a, **kw: False)
    monkeypatch.setattr(service_runner, 'resolve_binary', lambda *a, **kw: None)
    monkeypatch.setattr(serverbin, 'resolve_library', lambda dest_dir: '/opt/lib/libstremio-server.so')

    def fake_install_binary(dest_dir, progress_cb=None):
        raise serverbin.UnsupportedPlatformError('exec() forbidden')

    monkeypatch.setattr(serverbin, 'install_binary', fake_install_binary)
    factory, spawned = _make_library_process_factory([{}])
    monkeypatch.setattr(libserver, 'LibraryServer', factory)

    intervals = []
    wait = _scripted_wait(intervals, [None, None])
    with _main_env(tmp_path, wait, settings={'server_enable': True}) as ctx:
        service_runner.main()

    assert len(spawned) == 1
    assert spawned[0].start_calls == 1
    assert intervals == [service_runner.HEALTHY_POLL_INTERVAL] * 2
    assert [n for n in ctx.env.notifications if n[1] == 'STR30091'] == []  # never latched/notified unsupported
    assert any('falling back to library mode' in msg for msg, _level in ctx.env.log_calls)


def test_main_unsupported_platform_self_heals_into_library_mode_when_library_appears_later(
        monkeypatch, tmp_path):
    """The coarse-cadence latch self-heal check must prefer library mode
    over the exec()-based resolve_binary() self-heal once a companion
    library shows up on disk, exactly like the immediate-fallback case
    above but discovered one poll later."""
    probe_calls = []
    resolve_library_calls = []

    def fake_resolve_library(dest_dir):
        resolve_library_calls.append(1)
        return None if len(resolve_library_calls) < 2 else '/opt/lib/libstremio-server.so'

    monkeypatch.setattr(service_runner, 'probe_listening', lambda *a, **kw: probe_calls.append(1) or False)
    monkeypatch.setattr(serverbin, 'resolve_library', fake_resolve_library)

    resolve_binary_calls = []

    def fake_resolve_binary(*args, **kwargs):
        # Only ever called by the "else" (not-yet-latched) branch's
        # unconditional first lookup, and by the latched self-heal
        # sub-branch when NO library is available -- must never be
        # reached again once a companion library is found while latched.
        resolve_binary_calls.append(1)
        return None

    monkeypatch.setattr(service_runner, 'resolve_binary', fake_resolve_binary)

    def fake_install_binary(dest_dir, progress_cb=None):
        raise serverbin.UnsupportedPlatformError('exec() forbidden')

    monkeypatch.setattr(serverbin, 'install_binary', fake_install_binary)
    factory, spawned = _make_library_process_factory([{}])
    monkeypatch.setattr(libserver, 'LibraryServer', factory)

    # iter1: install_binary() fails, resolve_library() (call #1) still
    # None -> latches unsupported_platform. iter2: latched, resolve_library()
    # (call #2) now finds the library -> unlatches and starts it. iter3:
    # library server running -> ordinary healthy supervision.
    intervals = []
    wait = _scripted_wait(intervals, [None, None, None])
    with _main_env(tmp_path, wait, settings={'server_enable': True}) as ctx:
        service_runner.main()

    assert len(spawned) == 1
    assert spawned[0].start_calls == 1
    assert len(resolve_library_calls) == 2
    # Called once for the initial (pre-latch) "nothing resolvable" probe;
    # never again once the latched self-heal check finds a library first.
    assert len(resolve_binary_calls) == 1
    assert intervals == [
        service_runner.MISSING_BINARY_RECHECK_INTERVAL,
        # The self-heal finds and starts the library server within the
        # SAME (latched) tick, so the interval reflects that immediately
        # -- no need to wait out one more UNSUPPORTED_PLATFORM_POLL_INTERVAL
        # tick now that something is actually running.
        service_runner.HEALTHY_POLL_INTERVAL,
        service_runner.HEALTHY_POLL_INTERVAL,
    ]
    assert len([n for n in ctx.env.notifications if n[1] == 'STR30091']) == 1


def test_main_unsupported_platform_still_latches_when_no_library_available(monkeypatch, tmp_path):
    """Regression guard: with no companion library ever available (the
    ordinary non-Android/pre-library-mode case), the original latch +
    30091 notification behavior must be unchanged."""
    monkeypatch.setattr(service_runner, 'probe_listening', lambda *a, **kw: False)
    monkeypatch.setattr(serverbin, 'resolve_library', lambda dest_dir: None)
    monkeypatch.setattr(service_runner, 'resolve_binary', lambda *a, **kw: None)

    def fake_install_binary(dest_dir, progress_cb=None):
        raise serverbin.UnsupportedPlatformError('exec() forbidden')

    monkeypatch.setattr(serverbin, 'install_binary', fake_install_binary)

    def library_factory_must_not_run(*args, **kwargs):
        pytest.fail('LibraryServer must not be constructed with no companion library available')

    monkeypatch.setattr(libserver, 'LibraryServer', library_factory_must_not_run)

    intervals = []
    wait = _scripted_wait(intervals, [None, None])
    with _main_env(tmp_path, wait, settings={'server_enable': True}) as ctx:
        service_runner.main()

    assert len([n for n in ctx.env.notifications if n[1] == 'STR30091']) == 1
    assert intervals == [
        service_runner.MISSING_BINARY_RECHECK_INTERVAL,
        service_runner.UNSUPPORTED_PLATFORM_POLL_INTERVAL,
    ]


def test_main_library_mode_never_consults_the_stale_upgrade_helper(monkeypatch, tmp_path):
    """Deferred-upgrade contract: `lib.libserver._load_library()` refuses
    a different path in the same process, so any SERVER_TAG upgrade of
    the on-disk .so must wait for the next Kodi start (a fresh process)
    -- library mode must never call the exec-mode upgrade-if-stale
    helper (`serverbin.installed_tag`/a second `install_binary()`) at
    all."""
    monkeypatch.setattr(service_runner, 'probe_listening', lambda *a, **kw: False)
    monkeypatch.setattr(serverbin, 'resolve_library', lambda dest_dir: '/opt/lib/libstremio-server.so')

    def installed_tag_must_not_run(*args, **kwargs):
        pytest.fail('installed_tag must not be consulted in library mode')

    def install_binary_must_not_run(*args, **kwargs):
        pytest.fail('install_binary must not run in library mode with a companion library present')

    monkeypatch.setattr(serverbin, 'installed_tag', installed_tag_must_not_run)
    monkeypatch.setattr(serverbin, 'install_binary', install_binary_must_not_run)
    factory, spawned = _make_library_process_factory([{}])
    monkeypatch.setattr(libserver, 'LibraryServer', factory)

    wait = _scripted_wait([], [None])
    settings = {'server_enable': True, 'server_force_library': True}
    with _main_env(tmp_path, wait, settings=settings):
        service_runner.main()

    assert len(spawned) == 1


def test_main_library_mode_restart_backoff_on_failed_spawn(monkeypatch, tmp_path):
    """A failed `LibraryServer.start()` must back off exactly like a
    failed `ServerProcess.start()` -- never crash the supervision loop,
    and advance `state.backoff_idx` through RESTART_BACKOFF."""
    monkeypatch.setattr(service_runner, 'probe_listening', lambda *a, **kw: False)
    monkeypatch.setattr(serverbin, 'resolve_library', lambda dest_dir: '/opt/lib/libstremio-server.so')

    factory, spawned = _make_library_process_factory([
        {'start_exceptions': [OSError('cannot dlopen')]},
        {},
    ])
    monkeypatch.setattr(libserver, 'LibraryServer', factory)

    intervals = []
    wait = _scripted_wait(intervals, [None, None])
    settings = {'server_enable': True, 'server_force_library': True}
    with _main_env(tmp_path, wait, settings=settings):
        service_runner.main()

    assert len(spawned) == 2
    assert spawned[0].start_calls == 1
    assert spawned[1].start_calls == 1
    assert intervals == [service_runner.RESTART_BACKOFF[0], service_runner.HEALTHY_POLL_INTERVAL]


def test_main_library_mode_settings_change_restarts_and_can_revert_to_binary_mode(monkeypatch, tmp_path):
    """Flipping `server_force_library` off mid-session must tear down the
    running library server (via the normal restart_requested path, since
    `force_library` is part of `ServiceMonitor._snapshot()`) and fall
    back to the ordinary binary flow on the very next tick."""
    monkeypatch.setattr(service_runner, 'probe_listening', lambda *a, **kw: False)
    monkeypatch.setattr(serverbin, 'resolve_library', lambda dest_dir: '/opt/lib/libstremio-server.so')
    monkeypatch.setattr(service_runner, 'resolve_binary', lambda *a, **kw: '/opt/bin/stremio-server')

    lib_factory, lib_spawned = _make_library_process_factory([{}])
    monkeypatch.setattr(libserver, 'LibraryServer', lib_factory)
    bin_factory, bin_spawned = _make_process_factory([{}])
    monkeypatch.setattr(service_runner, 'ServerProcess', bin_factory)

    env_box = {}

    def disable_force_library(monitor):
        env_box['env'].addon.settings['server_force_library'] = False
        monitor.onSettingsChanged()

    intervals = []
    wait = _scripted_wait(intervals, [None, disable_force_library, None])
    settings = {'server_enable': True, 'server_force_library': True}
    with _main_env(tmp_path, wait, settings=settings) as ctx:
        env_box['env'] = ctx.env
        service_runner.main()

    assert len(lib_spawned) == 1
    assert lib_spawned[0].stop_calls == 1  # torn down by the settings-changed restart
    assert len(bin_spawned) == 1
    assert bin_spawned[0].binary == '/opt/bin/stremio-server'


# --- regression: library-mode notification fires once per session ----------


def test_main_library_mode_notification_fires_once_per_session_across_crash_restarts(monkeypatch, tmp_path):
    """Notification 30364 must fire once per session, not once per
    successful `_start_library_server()` call: a crashing library-mode
    server that keeps getting restarted (ServerStart erroring out right
    after start(), for example) would otherwise re-show it on every
    RESTART_BACKOFF-spaced restart for the rest of the session."""
    monkeypatch.setattr(service_runner, 'probe_listening', lambda *a, **kw: False)
    monkeypatch.setattr(serverbin, 'resolve_library', lambda dest_dir: '/opt/lib/libstremio-server.so')

    factory, spawned = _make_library_process_factory([
        {'poll_sequence': [1]},  # exits right away -> triggers a crash restart
        {},
    ])
    monkeypatch.setattr(libserver, 'LibraryServer', factory)

    intervals = []
    wait = _scripted_wait(intervals, [None, None, None, None])
    settings = {'server_enable': True, 'server_force_library': True}
    with _main_env(tmp_path, wait, settings=settings) as ctx:
        service_runner.main()

    assert len(spawned) == 2  # crash-restarted once
    library_notifications = [n for n in ctx.env.notifications if n[1] == 'STR30364']
    assert len(library_notifications) == 1


# --- regression: exec-spawn failure of an on-disk bundled binary falls ------
# --- back to library mode instead of retrying the doomed spawn forever -----


def test_main_binary_spawn_failure_falls_back_to_library_mode_when_bundled_and_library_present(
    monkeypatch, tmp_path
):
    """install_binary() deliberately promotes an unverified, chmod'd
    executable to final_path even when verify_executable() failed (see
    its own docstring). On a LATER session, resolve_binary() finds that
    leftover executable via a plain os.access(X_OK) check that an
    SELinux-enforcing W^X policy still passes, but ServerProcess.start()
    keeps failing (EACCES/PermissionError). Library mode must be tried
    immediately once a companion .so is already on disk, instead of
    retrying the same doomed exec() at RESTART_BACKOFF cadence for the
    rest of the session with library mode never selected."""
    monkeypatch.setattr(service_runner, 'probe_listening', lambda *a, **kw: False)
    monkeypatch.setattr(service_runner, 'resolve_binary', lambda *a, **kw: _bundled(tmp_path))
    # Already at the current tag -- _upgrade_bundled_if_stale() must be a
    # no-op here so the only thing under test is the post-spawn-failure
    # fallback, not a reinstall.
    monkeypatch.setattr(serverbin, 'installed_tag', lambda dest_dir: serverbin.SERVER_TAG)
    monkeypatch.setattr(serverbin, 'resolve_library', lambda dest_dir: '/opt/lib/libstremio-server.so')

    factory, spawned = _make_process_factory([
        {'start_exceptions': [PermissionError('exec() denied')]},
    ])
    monkeypatch.setattr(service_runner, 'ServerProcess', factory)

    lib_factory, lib_spawned = _make_library_process_factory([{}])
    monkeypatch.setattr(libserver, 'LibraryServer', lib_factory)

    intervals = []
    wait = _scripted_wait(intervals, [None, None])
    with _main_env(tmp_path, wait, settings={'server_enable': True}) as ctx:
        service_runner.main()

    assert len(spawned) == 1
    assert spawned[0].start_calls == 1
    assert len(lib_spawned) == 1
    assert lib_spawned[0].start_calls == 1
    assert intervals[0] == service_runner.HEALTHY_POLL_INTERVAL
    assert any('falling back to library mode' in msg for msg, _level in ctx.env.log_calls)


def test_main_binary_spawn_failure_stays_in_binary_mode_when_no_library_available(monkeypatch, tmp_path):
    """Regression guard: with no companion library ever available, a
    failed exec() of a bundled binary must keep retrying the ordinary
    embedded-server backoff -- unchanged from before this fallback was
    added -- and must never construct a LibraryServer."""
    monkeypatch.setattr(service_runner, 'probe_listening', lambda *a, **kw: False)
    monkeypatch.setattr(service_runner, 'resolve_binary', lambda *a, **kw: _bundled(tmp_path))
    monkeypatch.setattr(serverbin, 'installed_tag', lambda dest_dir: serverbin.SERVER_TAG)
    monkeypatch.setattr(serverbin, 'resolve_library', lambda dest_dir: None)

    factory, spawned = _make_process_factory([
        {'start_exceptions': [PermissionError('exec() denied')]},
        {},
    ])
    monkeypatch.setattr(service_runner, 'ServerProcess', factory)

    def library_factory_must_not_run(*args, **kwargs):
        pytest.fail('LibraryServer must not be constructed with no companion library available')

    monkeypatch.setattr(libserver, 'LibraryServer', library_factory_must_not_run)

    intervals = []
    wait = _scripted_wait(intervals, [None, None])
    with _main_env(tmp_path, wait, settings={'server_enable': True}):
        service_runner.main()

    assert len(spawned) == 2
    assert intervals == [service_runner.RESTART_BACKOFF[0], service_runner.HEALTHY_POLL_INTERVAL]


# --- regression: unsupported_platform latch survives a failed library start


def test_main_unsupported_platform_latch_survives_failed_library_start_in_both_branches(
    monkeypatch, tmp_path
):
    """A failed `LibraryServer.start()` (dlopen() failure, missing
    export, ...) must NOT clear `state.unsupported_platform` in either
    fallback site: doing so would fall through to the un-latched branch
    on the very next tick, which re-attempts `install_binary()` (and
    therefore a fresh archive download) every poll for as long as the
    library keeps failing to load. Exercises both call sites: iteration
    1 fails inside the `UnsupportedPlatformError` handler's fallback,
    iteration 2 fails inside the already-latched self-heal branch, and
    iteration 3 finally succeeds there and unlatches.
    """
    monkeypatch.setattr(service_runner, 'probe_listening', lambda *a, **kw: False)
    monkeypatch.setattr(service_runner, 'resolve_binary', lambda *a, **kw: None)
    monkeypatch.setattr(serverbin, 'resolve_library', lambda dest_dir: '/opt/lib/libstremio-server.so')

    install_calls = []

    def fake_install_binary(dest_dir, progress_cb=None):
        install_calls.append(dest_dir)
        raise serverbin.UnsupportedPlatformError('exec() forbidden')

    monkeypatch.setattr(serverbin, 'install_binary', fake_install_binary)

    factory, spawned = _make_library_process_factory([
        {'start_exceptions': [OSError('cannot dlopen')]},  # iter1: UnsupportedPlatformError branch
        {'start_exceptions': [OSError('cannot dlopen')]},  # iter2: latched self-heal branch
        {},                                                 # iter3: latched self-heal branch, succeeds
    ])
    monkeypatch.setattr(libserver, 'LibraryServer', factory)

    intervals = []
    wait = _scripted_wait(intervals, [None, None, None])
    with _main_env(tmp_path, wait, settings={'server_enable': True}):
        service_runner.main()

    # install_binary() is only reachable from the un-latched branch --
    # exactly one call means the latch held through both failed library
    # starts instead of re-entering the download path on iter2/iter3.
    assert len(install_calls) == 1
    assert len(spawned) == 3
    assert [p.start_calls for p in spawned] == [1, 1, 1]
    assert intervals == [
        service_runner.RESTART_BACKOFF[0],
        service_runner.RESTART_BACKOFF[1],
        service_runner.HEALTHY_POLL_INTERVAL,
    ]


# --- regression: a failed library start also tries a now-available exec ----


def test_main_latched_self_heal_falls_back_to_executable_when_library_start_fails(monkeypatch, tmp_path):
    """Once latched, retrying ONLY the companion library forever (while it
    remains on disk but keeps failing to dlopen()) can never notice a
    binary that becomes runnable in the meantime -- that self-heal
    previously ran only when NO library was found at all. A failed
    `_start_library_server()` must also try `resolve_binary()` /
    `_start_embedded_server()` on the SAME tick instead of waiting out a
    full `UNSUPPORTED_PLATFORM_POLL_INTERVAL` cycle (or a settings
    change/restart) to notice it."""
    monkeypatch.setattr(service_runner, 'probe_listening', lambda *a, **kw: False)

    resolve_library_calls = []

    def fake_resolve_library(dest_dir):
        resolve_library_calls.append(1)
        return None if len(resolve_library_calls) < 2 else '/opt/lib/libstremio-server.so'

    monkeypatch.setattr(serverbin, 'resolve_library', fake_resolve_library)

    resolve_binary_calls = []

    def fake_resolve_binary(*args, **kwargs):
        resolve_binary_calls.append(1)
        # None on the initial pre-latch probe (iter1) and for as long as
        # the library is still missing; becomes available only once the
        # latched self-heal branch's library attempt has failed (iter2).
        return None if len(resolve_binary_calls) < 2 else '/opt/bin/stremio-server'

    monkeypatch.setattr(service_runner, 'resolve_binary', fake_resolve_binary)

    def fake_install_binary(dest_dir, progress_cb=None):
        raise serverbin.UnsupportedPlatformError('exec() forbidden')

    monkeypatch.setattr(serverbin, 'install_binary', fake_install_binary)

    lib_factory, lib_spawned = _make_library_process_factory([
        {'start_exceptions': [OSError('cannot dlopen')]},
    ])
    monkeypatch.setattr(libserver, 'LibraryServer', lib_factory)

    bin_factory, bin_spawned = _make_process_factory([{}])
    monkeypatch.setattr(service_runner, 'ServerProcess', bin_factory)

    intervals = []
    wait = _scripted_wait(intervals, [None, None])
    with _main_env(tmp_path, wait, settings={'server_enable': True}):
        service_runner.main()

    # iter1: install_binary() raises, no library yet -> latches, no exec
    # attempted (nothing resolvable either). iter2: latched, library now
    # found but LibraryServer.start() fails -> falls back to the now-
    # available executable on the SAME tick and unlatches.
    assert resolve_library_calls == [1, 1]
    assert resolve_binary_calls == [1, 1]
    assert len(lib_spawned) == 1
    assert lib_spawned[0].start_calls == 1
    assert len(bin_spawned) == 1
    assert bin_spawned[0].start_calls == 1
    assert intervals == [
        service_runner.MISSING_BINARY_RECHECK_INTERVAL,
        service_runner.HEALTHY_POLL_INTERVAL,
    ]


def test_main_unsupported_platform_error_branch_falls_back_to_executable_when_library_start_fails(
    monkeypatch, tmp_path
):
    """Same fallback, exercised at the OTHER call site: the fresh
    `UnsupportedPlatformError` handler's own library attempt (not yet
    latched) also tries a resolvable executable on the same tick when
    the library fails to start, instead of latching unconditionally."""
    monkeypatch.setattr(service_runner, 'probe_listening', lambda *a, **kw: False)
    monkeypatch.setattr(serverbin, 'resolve_library', lambda dest_dir: '/opt/lib/libstremio-server.so')
    resolve_binary_calls = []

    def fake_resolve_binary(*args, **kwargs):
        resolve_binary_calls.append(1)
        # None for the outer "nothing resolvable yet" probe that leads
        # into install_binary(); becomes available only once the library
        # fallback's own attempt has failed.
        return None if len(resolve_binary_calls) < 2 else '/opt/bin/stremio-server'

    monkeypatch.setattr(service_runner, 'resolve_binary', fake_resolve_binary)

    def fake_install_binary(dest_dir, progress_cb=None):
        raise serverbin.UnsupportedPlatformError('exec() forbidden')

    monkeypatch.setattr(serverbin, 'install_binary', fake_install_binary)

    lib_factory, lib_spawned = _make_library_process_factory([
        {'start_exceptions': [OSError('cannot dlopen')]},
    ])
    monkeypatch.setattr(libserver, 'LibraryServer', lib_factory)

    bin_factory, bin_spawned = _make_process_factory([{}])
    monkeypatch.setattr(service_runner, 'ServerProcess', bin_factory)

    intervals = []
    wait = _scripted_wait(intervals, [None])
    with _main_env(tmp_path, wait, settings={'server_enable': True}):
        service_runner.main()

    assert resolve_binary_calls == [1, 1]
    assert len(lib_spawned) == 1
    assert lib_spawned[0].start_calls == 1
    assert len(bin_spawned) == 1
    assert bin_spawned[0].start_calls == 1
    assert intervals == [service_runner.HEALTHY_POLL_INTERVAL]