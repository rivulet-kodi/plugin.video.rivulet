"""Tests for lib.service_runner: the background service that supervises a
local stremio-server-go child process.

The module is split in two halves (see its own docstring):
  - A pure process-management core (`http_port_from_url`, `resolve_binary`,
    `probe_listening`, `ServerProcess`, `extra_env_from_settings`) with no
    `xbmc*` imports anywhere at module scope -- importable and testable
    with plain python3, exercised below with NO Kodi stubs at all (real
    filesystem via `tmp_path`, mocked
    `subprocess.Popen`/`urllib.request.urlopen`, never a real socket or
    child process).
  - `main()`, which does all `xbmc*` imports locally and drives an
    `xbmc.Monitor` supervision loop on top of that pure core -- exercised
    below against the shared fake xbmc modules in `tests/kodistubs`, with
    two small local patches for the two gaps that package's `lib.ui.*`
    consumers never needed: `xbmc.Monitor.abortRequested()` and
    `xbmcgui.NOTIFICATION_ERROR` (see `_main_env` below).
"""
import contextlib
import os
import subprocess
import sys
import urllib.error

import pytest

import lib.serverbin as serverbin
import lib.service_runner as service_runner
from tests.kodistubs import install_kodi_stubs

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
# resolve_binary
# ===========================================================================


def _make_executable(path):
    path.write_text('#!/bin/sh\necho fake\n')
    path.chmod(0o755)


def test_resolve_binary_prefers_explicit_path_when_present_and_executable(tmp_path):
    explicit = tmp_path / 'custom-server'
    _make_executable(explicit)
    addon_data = tmp_path / 'addon_data'
    assert service_runner.resolve_binary(str(explicit), str(addon_data)) == str(explicit)


def test_resolve_binary_ignores_explicit_path_when_not_executable(tmp_path):
    explicit = tmp_path / 'custom-server'
    explicit.write_text('not executable')  # no chmod +x
    addon_data = tmp_path / 'addon_data'
    bin_dir = addon_data / 'bin'
    bin_dir.mkdir(parents=True)
    bundled = bin_dir / service_runner.BINARY_NAME
    _make_executable(bundled)
    assert service_runner.resolve_binary(str(explicit), str(addon_data)) == str(bundled)


def test_resolve_binary_falls_back_to_bundled_bin_dir_when_explicit_missing(tmp_path):
    addon_data = tmp_path / 'addon_data'
    bin_dir = addon_data / 'bin'
    bin_dir.mkdir(parents=True)
    bundled = bin_dir / service_runner.BINARY_NAME
    _make_executable(bundled)
    missing_explicit = str(tmp_path / 'does-not-exist')
    assert service_runner.resolve_binary(missing_explicit, str(addon_data)) == str(bundled)


def test_resolve_binary_falls_back_to_bundled_exe_variant(tmp_path):
    """Windows-style layout: only the `.exe` variant is present."""
    addon_data = tmp_path / 'addon_data'
    bin_dir = addon_data / 'bin'
    bin_dir.mkdir(parents=True)
    bundled_exe = bin_dir / (service_runner.BINARY_NAME + '.exe')
    _make_executable(bundled_exe)
    assert service_runner.resolve_binary('', str(addon_data)) == str(bundled_exe)


def test_resolve_binary_falls_back_to_path_lookup(monkeypatch, tmp_path):
    addon_data = tmp_path / 'addon_data'  # no bin/ dir at all
    monkeypatch.setattr(service_runner.shutil, 'which', lambda name: '/usr/bin/' + name)
    assert service_runner.resolve_binary('', str(addon_data)) == '/usr/bin/' + service_runner.BINARY_NAME


def test_resolve_binary_returns_none_when_nothing_found(monkeypatch, tmp_path):
    addon_data = tmp_path / 'addon_data'
    monkeypatch.setattr(service_runner.shutil, 'which', lambda name: None)
    assert service_runner.resolve_binary('', str(addon_data)) is None


# ===========================================================================
# probe_listening
# ===========================================================================


def test_probe_listening_true_on_first_probe_path_success(monkeypatch):
    calls = []

    def fake_urlopen(url, timeout=None):
        calls.append(url)
        return object()

    monkeypatch.setattr(service_runner.urllib.request, 'urlopen', fake_urlopen)
    assert service_runner.probe_listening('http://host:1234') is True
    assert calls == ['http://host:1234' + service_runner.PROBE_PATHS[0]]


@pytest.mark.parametrize('responding_path', service_runner.PROBE_PATHS)
def test_probe_listening_true_when_any_probe_path_responds(monkeypatch, responding_path):
    """Every entry in PROBE_PATHS must be tried, in order, until one
    completes -- not just the first."""
    calls = []

    def fake_urlopen(url, timeout=None):
        calls.append(url)
        if url.endswith(responding_path):
            return object()
        raise urllib.error.URLError('connection refused')

    monkeypatch.setattr(service_runner.urllib.request, 'urlopen', fake_urlopen)
    assert service_runner.probe_listening('http://host:1234') is True
    assert calls[-1] == 'http://host:1234' + responding_path


def test_probe_listening_true_on_http_error_status(monkeypatch):
    """An HTTP-level error status still proves *something* is bound to
    the port -- only connection-level failures mean "nothing listening"."""

    def fake_urlopen(url, timeout=None):
        raise urllib.error.HTTPError(url, 404, 'Not Found', {}, None)

    monkeypatch.setattr(service_runner.urllib.request, 'urlopen', fake_urlopen)
    assert service_runner.probe_listening('http://host:1234') is True


def test_probe_listening_false_when_every_probe_path_is_refused(monkeypatch):
    calls = []

    def fake_urlopen(url, timeout=None):
        calls.append(url)
        raise urllib.error.URLError('connection refused')

    monkeypatch.setattr(service_runner.urllib.request, 'urlopen', fake_urlopen)
    assert service_runner.probe_listening('http://host:1234') is False
    assert calls == ['http://host:1234' + p for p in service_runner.PROBE_PATHS]


def test_probe_listening_strips_trailing_slash_from_base_url(monkeypatch):
    calls = []

    def fake_urlopen(url, timeout=None):
        calls.append(url)
        raise urllib.error.URLError('connection refused')

    monkeypatch.setattr(service_runner.urllib.request, 'urlopen', fake_urlopen)
    service_runner.probe_listening('http://host:1234/')
    assert calls[0] == 'http://host:1234' + service_runner.PROBE_PATHS[0]


def test_probe_listening_forwards_caller_supplied_timeout(monkeypatch):
    seen = []

    def fake_urlopen(url, timeout=None):
        seen.append(timeout)
        return object()

    monkeypatch.setattr(service_runner.urllib.request, 'urlopen', fake_urlopen)
    service_runner.probe_listening('http://host', timeout=7.5)
    assert seen == [7.5]


# ===========================================================================
# extra_env_from_settings / EXTRA_ENV_SETTINGS
# ===========================================================================


def test_extra_env_from_settings_forwards_truthy_string_value():
    env = service_runner.extra_env_from_settings({'bt_proxy': 'socks5://127.0.0.1:9050'})
    assert env == {'STREMIO_BT_PROXY': 'socks5://127.0.0.1:9050'}


def test_extra_env_from_settings_omits_falsy_string_value():
    assert service_runner.extra_env_from_settings({'bt_proxy': ''}) == {}


def test_extra_env_from_settings_skips_missing_key_without_raising():
    """A caller supplying only a subset of EXTRA_ENV_SETTINGS keys must not
    KeyError on the rows it did not supply -- `bt_proxy` here is entirely
    absent from `values`, not merely falsy."""
    env = service_runner.extra_env_from_settings({'bt_listen_port': 6900})
    assert env == {'BT_LISTEN_PORT': '6900'}


@pytest.mark.parametrize('port', [0, 6900])
def test_extra_env_from_settings_int_always_forwarded_including_zero(port):
    env = service_runner.extra_env_from_settings({'bt_listen_port': port})
    assert env == {'BT_LISTEN_PORT': str(port)}


@pytest.mark.parametrize('mb,expected_bytes', [(0, 0), (256, 256 * 1024 * 1024)])
def test_extra_env_from_settings_mb_to_bytes_multiplies_correctly(mb, expected_bytes):
    env = service_runner.extra_env_from_settings({'memory_cache_size_mb': mb})
    assert env == {'STREMIO_MEMORY_CACHE_SIZE': str(expected_bytes)}


@pytest.mark.parametrize('value,expected', [(True, 'true'), (False, 'false')])
def test_extra_env_from_settings_bool_always_forwarded_as_true_false_string(value, expected):
    env = service_runner.extra_env_from_settings({'bt_anonymous': value})
    assert env == {'STREMIO_BT_ANONYMOUS': expected}


def test_extra_env_from_settings_combines_multiple_kinds_and_ignores_absent_rows():
    """Exercises several kinds in one call; every EXTRA_ENV_SETTINGS row
    not present in `values` (i.e. every one of the 30 besides these four)
    contributes nothing."""
    values = {
        'bt_listen_port': 6900,
        'disable_trackers': True,
        'memory_cache_size_mb': 512,
        'bt_proxy': '',  # present but falsy -> omitted, not skipped
    }
    env = service_runner.extra_env_from_settings(values)
    assert env == {
        'BT_LISTEN_PORT': '6900',
        'STREMIO_DISABLE_TRACKERS': 'true',
        'STREMIO_MEMORY_CACHE_SIZE': str(512 * 1024 * 1024),
    }


def test_extra_env_from_settings_empty_values_dict_returns_empty_env():
    assert service_runner.extra_env_from_settings({}) == {}

# ===========================================================================
# ServerProcess
# ===========================================================================


class FakePopenProcess:
    """Stand-in for the object `subprocess.Popen(...)` returns, letting
    `ServerProcess` tests script poll()/wait() behavior without a real
    child process."""

    def __init__(self, argv):
        self.argv = argv
        self.pid = 4242
        self.poll_result = None
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_calls = []
        self._wait_results = []  # queue of None (succeed) or an exception to raise

    def poll(self):
        return self.poll_result

    def terminate(self):
        self.terminate_calls += 1

    def kill(self):
        self.kill_calls += 1

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        if self._wait_results:
            result = self._wait_results.pop(0)
            if isinstance(result, Exception):
                raise result
        return self.poll_result


@pytest.fixture
def fake_popen(monkeypatch):
    """Patches subprocess.Popen; returns a list every FakePopenProcess it
    creates is appended to, in construction order."""
    created = []

    def factory(argv, **kwargs):
        created.append({'argv': argv, 'kwargs': kwargs})
        proc = FakePopenProcess(argv)
        created[-1]['proc'] = proc
        return proc

    monkeypatch.setattr(service_runner.subprocess, 'Popen', factory)
    return created


def _server_process(tmp_path, server_url='http://127.0.0.1:9090', extra_env=None):
    return service_runner.ServerProcess(
        '/opt/bin/stremio-server', server_url,
        str(tmp_path / 'server'), str(tmp_path / 'server.log'), extra_env=extra_env,
    )


# --- start(): argv/env/log/started_at --------------------------------------


def test_start_spawns_popen_with_argv_env_and_opens_log_for_append(fake_popen, tmp_path):
    sp = _server_process(tmp_path, server_url='http://127.0.0.1:9090')
    sp.start()

    assert len(fake_popen) == 1
    call = fake_popen[0]
    assert call['argv'] == ['/opt/bin/stremio-server']
    assert call['kwargs']['env']['APP_PATH'] == str(tmp_path / 'server')
    assert call['kwargs']['env']['HTTP_PORT'] == '9090'
    assert call['kwargs']['stderr'] == subprocess.STDOUT
    assert call['kwargs']['stdin'] == subprocess.DEVNULL
    assert call['kwargs']['stdout'].name == str(tmp_path / 'server.log')
    assert call['kwargs']['stdout'].mode == 'a'
    assert os.path.isdir(str(tmp_path / 'server'))  # app_path really created
    assert sp.uptime() is not None and sp.uptime() >= 0
    assert sp.running is True

    sp.stop()


def test_build_env_does_not_mutate_the_real_process_environment(tmp_path):
    sp = _server_process(tmp_path)
    env = sp.build_env()
    assert env['APP_PATH'] == str(tmp_path / 'server')
    assert 'APP_PATH' not in os.environ


def test_build_env_overlays_extra_env_passed_at_construction(tmp_path):
    sp = _server_process(tmp_path, extra_env={
        'STREMIO_BT_ANONYMOUS': 'true', 'BT_LISTEN_PORT': '6900',
    })
    env = sp.build_env()
    assert env['APP_PATH'] == str(tmp_path / 'server')
    assert env['HTTP_PORT'] == '9090'
    assert env['STREMIO_BT_ANONYMOUS'] == 'true'
    assert env['BT_LISTEN_PORT'] == '6900'
    assert 'STREMIO_BT_ANONYMOUS' not in os.environ


def test_start_is_a_noop_while_already_running(fake_popen, tmp_path):
    sp = _server_process(tmp_path)
    sp.start()
    sp.start()
    assert len(fake_popen) == 1  # second start() must not spawn a duplicate
    sp.stop()


def test_start_with_no_existing_log_does_not_raise(fake_popen, tmp_path):
    """`_rotate_log()`'s getsize() raises FileNotFoundError (an OSError)
    on a fresh install with no prior log -- start() must swallow it."""
    log_path = tmp_path / 'server.log'
    assert not log_path.exists()
    sp = _server_process(tmp_path)
    sp.start()
    assert sp.running is True
    sp.stop()


def test_start_rotates_log_exceeding_the_threshold(fake_popen, tmp_path):
    log_path = tmp_path / 'server.log'
    log_path.write_bytes(b'x' * (service_runner.LOG_ROTATE_BYTES + 1))

    sp = _server_process(tmp_path)
    sp.start()

    backup = tmp_path / 'server.log.1'
    assert backup.exists()
    assert backup.stat().st_size == service_runner.LOG_ROTATE_BYTES + 1
    assert log_path.stat().st_size == 0  # reopened fresh in append mode after the rename
    sp.stop()


def test_start_overwrites_a_stale_existing_backup_on_rotation(fake_popen, tmp_path):
    log_path = tmp_path / 'server.log'
    backup = tmp_path / 'server.log.1'
    log_path.write_bytes(b'y' * (service_runner.LOG_ROTATE_BYTES + 1))
    backup.write_bytes(b'stale-backup-from-last-rotation')

    sp = _server_process(tmp_path)
    sp.start()

    assert backup.read_bytes() == b'y' * (service_runner.LOG_ROTATE_BYTES + 1)
    sp.stop()


def test_start_does_not_rotate_log_at_or_under_the_threshold(fake_popen, tmp_path):
    log_path = tmp_path / 'server.log'
    log_path.write_bytes(b'z' * service_runner.LOG_ROTATE_BYTES)  # exactly at the boundary

    sp = _server_process(tmp_path)
    sp.start()

    assert not (tmp_path / 'server.log.1').exists()
    sp.stop()


# --- poll()/running/uptime() semantics --------------------------------------


def test_poll_running_and_uptime_before_any_start(tmp_path):
    sp = _server_process(tmp_path)
    assert sp.poll() is None
    assert sp.running is False
    assert sp.uptime() is None


def test_poll_and_running_reflect_child_exit(fake_popen, tmp_path):
    sp = _server_process(tmp_path)
    sp.start()
    assert sp.running is True
    assert sp.poll() is None

    fake_popen[0]['proc'].poll_result = 7
    assert sp.poll() == 7
    assert sp.running is False
    sp.stop()


def test_uptime_reflects_elapsed_monotonic_time(monkeypatch, fake_popen, tmp_path):
    times = iter([100.0, 104.5])
    monkeypatch.setattr(service_runner.time, 'monotonic', lambda: next(times))

    sp = _server_process(tmp_path)
    sp.start()
    assert sp.uptime() == pytest.approx(4.5)
    sp.stop()


# --- stop(): graceful / kill escalation / reap / never-started -------------


def test_stop_terminates_and_waits_gracefully_when_still_running(fake_popen, tmp_path):
    sp = _server_process(tmp_path)
    sp.start()
    fake_proc = fake_popen[0]['proc']

    sp.stop(grace=3.0)

    assert fake_proc.terminate_calls == 1
    assert fake_proc.kill_calls == 0
    assert fake_proc.wait_calls == [3.0]
    assert sp.running is False
    assert sp.uptime() is None


def test_stop_escalates_to_kill_after_graceful_wait_times_out(fake_popen, tmp_path):
    sp = _server_process(tmp_path)
    sp.start()
    fake_proc = fake_popen[0]['proc']
    fake_proc._wait_results = [subprocess.TimeoutExpired(cmd='stremio-server', timeout=3.0)]

    sp.stop(grace=3.0)

    assert fake_proc.terminate_calls == 1
    assert fake_proc.kill_calls == 1
    assert fake_proc.wait_calls == [3.0, 3.0]  # graceful wait, then post-kill wait


def test_stop_reaps_already_exited_child_without_terminate_or_kill(fake_popen, tmp_path):
    sp = _server_process(tmp_path)
    sp.start()
    fake_proc = fake_popen[0]['proc']
    fake_proc.poll_result = 0  # exited on its own before stop() runs

    sp.stop()

    assert fake_proc.terminate_calls == 0
    assert fake_proc.kill_calls == 0
    assert fake_proc.wait_calls == [None]  # reaped via a bare wait(), no timeout


def test_stop_is_safe_when_never_started(tmp_path):
    sp = _server_process(tmp_path)
    sp.stop()  # must not raise
    assert sp.running is False


def test_stop_closes_the_log_file_handle(fake_popen, tmp_path):
    sp = _server_process(tmp_path)
    sp.start()
    log_fh = sp._log_fh
    assert log_fh is not None and not log_fh.closed

    sp.stop()

    assert log_fh.closed is True


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
    ):
        self.binary = binary
        self.server_url = server_url
        self.app_path = app_path
        self.log_path = log_path
        self.extra_env = extra_env or {}
        self.start_calls = 0
        self.stop_calls = 0
        self._poll_sequence = list(poll_sequence or [])
        self._uptime_value = uptime_value

    def start(self):
        self.start_calls += 1

    def poll(self):
        return self._poll_sequence.pop(0) if self._poll_sequence else None

    def uptime(self):
        return self._uptime_value

    def stop(self, grace=5.0):
        self.stop_calls += 1


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
def _main_env(tmp_path, waitforabort, settings=None):
    """Installs the shared kodistubs for one `main()` run, patching the
    two Monitor/xbmcgui gaps described above and redirecting
    `xbmcvfs.translatePath` to a real pytest `tmp_path` so `main()`'s
    `os.makedirs(profile_dir, exist_ok=True)` writes somewhere hermetic
    instead of the shared fake's literal `/fake-kodi-home/...` path.
    """
    with install_kodi_stubs(reload=(), settings=settings) as ctx:
        xbmc_mod = sys.modules['xbmc']
        xbmcgui_mod = sys.modules['xbmcgui']
        xbmcvfs_mod = sys.modules['xbmcvfs']

        xbmcgui_mod.NOTIFICATION_ERROR = 'error'
        xbmcvfs_mod.translatePath = lambda path: str(tmp_path)
        xbmc_mod.Monitor.abortRequested = lambda self: False
        xbmc_mod.Monitor.waitForAbort = waitforabort

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
    assert intervals == [service_runner.HEALTHY_POLL_INTERVAL] * 3
    assert any('starting embedded server' in msg for msg, _level in ctx.env.log_calls)

    # main() returned with the child still alive -> the post-loop shutdown
    # path (scenario g) must stop it exactly once.
    assert proc.stop_calls == 1
    assert any('shutting down embedded server' in msg for msg, _level in ctx.env.log_calls)


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


def test_main_embedded_enabled_binary_missing_download_fails_then_notifies_once_and_stops_retrying(
    monkeypatch, tmp_path
):
    """When the one auto-download attempt raises (network down, no release
    asset for this platform, etc.), main() must not crash, must notify the
    failure once, and must NOT hammer GitHub again on every subsequent
    2s/5s poll -- `attempted_download` guards it. Instead it falls back to
    the pre-existing notify-once-then-recheck behavior, in case a binary
    is manually dropped into place later.
    """
    monkeypatch.setattr(service_runner, 'probe_listening', lambda *a, **kw: False)
    monkeypatch.setattr(service_runner, 'resolve_binary', lambda *a, **kw: None)

    install_calls = []

    def fake_install_binary(dest_dir, progress_cb=None):
        install_calls.append(dest_dir)
        raise serverbin.DownloadError('no network')

    monkeypatch.setattr(serverbin, 'install_binary', fake_install_binary)
    factory, spawned = _make_process_factory([])
    monkeypatch.setattr(service_runner, 'ServerProcess', factory)

    intervals = []
    wait = _scripted_wait(intervals, [None, None, None])
    with _main_env(tmp_path, wait, settings={'server_enable': True}) as ctx:
        service_runner.main()

    assert spawned == []
    assert intervals == [service_runner.MISSING_BINARY_RECHECK_INTERVAL] * 3

    # Attempted exactly once across all 3 iterations, not once per poll.
    assert install_calls == [os.path.join(str(tmp_path), 'bin')]

    setup_notifications = [n for n in ctx.env.notifications if n[1] == 'STR30069']
    failed_notifications = [n for n in ctx.env.notifications if n[1] == 'STR30063']
    missing_notifications = [n for n in ctx.env.notifications if n[1] == 'STR30031']
    assert len(setup_notifications) == 1
    assert len(failed_notifications) == 1
    assert failed_notifications[0][2] == 'error'
    # After the one failed attempt, the loop falls back to the original
    # notify-once "binary not found" behavior for the remaining iterations.
    assert len(missing_notifications) == 1
    assert missing_notifications[0][2] == 'error'

    error_logs = [msg for msg, level in ctx.env.log_calls if level == ctx.xbmc.LOGERROR]
    assert any('download failed' in msg for msg in error_logs)
    assert f'[{service_runner.ADDON_ID}] stremio-server binary not found' in error_logs


def test_main_settings_changed_resets_attempted_download_guard_for_retry(monkeypatch, tmp_path):
    """A settings change resets `attempted_download` (alongside
    `notified_missing`/`backoff_idx`) so a user who fixes whatever made the
    first download fail (e.g. flips a proxy setting, or just wants Kodi to
    try again) gets a fresh attempt without restarting the whole service.
    """
    monkeypatch.setattr(service_runner, 'probe_listening', lambda *a, **kw: False)

    install_calls = []

    def fake_install_binary(dest_dir, progress_cb=None):
        install_calls.append(dest_dir)
        if len(install_calls) == 1:
            raise serverbin.DownloadError('first attempt fails')
        return os.path.join(dest_dir, service_runner.BINARY_NAME)

    def fake_resolve_binary(explicit_path, addon_data_dir):
        # Only "sees" a binary once the second install call has succeeded.
        return None if len(install_calls) < 2 else '/opt/bin/stremio-server'

    monkeypatch.setattr(serverbin, 'install_binary', fake_install_binary)
    monkeypatch.setattr(service_runner, 'resolve_binary', fake_resolve_binary)
    factory, spawned = _make_process_factory([{}])
    monkeypatch.setattr(service_runner, 'ServerProcess', factory)

    env_box = {}

    def trigger_settings_change(monitor):
        env_box['env'].addon.settings['server_url'] = 'http://127.0.0.1:9999'
        monitor.onSettingsChanged()

    intervals = []
    wait = _scripted_wait(intervals, [None, trigger_settings_change, None, None])
    with _main_env(tmp_path, wait, settings={'server_enable': True}) as ctx:
        env_box['env'] = ctx.env
        service_runner.main()

    # Two separate install attempts: the first (pre-settings-change) fails,
    # the second (post-settings-change) succeeds -- proving the guard was
    # reset rather than permanently latched after one failure.
    assert install_calls == [os.path.join(str(tmp_path), 'bin')] * 2
    assert len(spawned) == 1
    assert spawned[0].binary == '/opt/bin/stremio-server'

    setup_notifications = [n for n in ctx.env.notifications if n[1] == 'STR30069']
    failed_notifications = [n for n in ctx.env.notifications if n[1] == 'STR30063']
    missing_notifications = [n for n in ctx.env.notifications if n[1] == 'STR30031']
    assert len(setup_notifications) == 2  # one per download attempt
    assert len(failed_notifications) == 1  # only the first attempt failed
    # notified_missing also got a chance to fire, between the failed
    # attempt and the settings change that reset it.
    assert len(missing_notifications) == 1


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

    # Spawn iterations (0, 2, 4, 6) always poll at HEALTHY_POLL_INTERVAL.
    assert [intervals[i] for i in (0, 2, 4, 6)] == [service_runner.HEALTHY_POLL_INTERVAL] * 4

    # Crash iterations (1, 3, 5) climb the backoff schedule in order.
    assert [intervals[1], intervals[3], intervals[5]] == list(service_runner.RESTART_BACKOFF)

    # The 4th crash (iteration 7) followed a run >= MIN_STABLE_UPTIME:
    # backoff resets to RESTART_BACKOFF[0] instead of staying capped at
    # RESTART_BACKOFF[-1].
    assert intervals[7] == service_runner.RESTART_BACKOFF[0]


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
    actually gate the stop()/log call -- a settings change while nothing
    is running (binary still missing, download still failing) must reset
    backoff_idx/notified_missing/attempted_download without touching a
    None proc."""
    monkeypatch.setattr(service_runner, 'probe_listening', lambda *a, **kw: False)
    monkeypatch.setattr(service_runner, 'resolve_binary', lambda *a, **kw: None)  # binary missing throughout

    def fake_install_binary(dest_dir, progress_cb=None):
        raise serverbin.DownloadError('still no network')

    monkeypatch.setattr(serverbin, 'install_binary', fake_install_binary)
    factory, spawned = _make_process_factory([])
    monkeypatch.setattr(service_runner, 'ServerProcess', factory)

    env_box = {}

    def change_binary_setting_and_signal(monitor):
        env_box['env'].addon.settings['server_binary'] = '/new/path'
        monitor.onSettingsChanged()

    # iter1: binary missing -> auto-download attempted and fails. The step
    # fires during iter2's wait, signaling a settings change while proc is
    # still None. iter3: binary still missing -> attempted_download/
    # notified_missing were reset by the settings-changed handling, so a
    # second download is attempted (and also fails).
    intervals = []
    wait = _scripted_wait(intervals, [None, change_binary_setting_and_signal, None])
    with _main_env(tmp_path, wait, settings={'server_enable': True}) as ctx:
        env_box['env'] = ctx.env
        service_runner.main()  # must not crash trying to stop() a None proc

    assert spawned == []
    setup_notifications = [n for n in ctx.env.notifications if n[1] == 'STR30069']
    failed_notifications = [n for n in ctx.env.notifications if n[1] == 'STR30063']
    missing_notifications = [n for n in ctx.env.notifications if n[1] == 'STR30031']
    # Two separate download attempts (one per settings "generation"), each
    # failing -- proving attempted_download really was reset, not just
    # notified_missing.
    assert len(setup_notifications) == 2
    assert len(failed_notifications) == 2
    assert len(missing_notifications) == 1
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
