"""Tests for the pure process-management core of lib.service_runner:
resolve_binary(), is_bundled_binary(), probe_listening(), and
extra_env_from_settings()/EXTRA_ENV_SETTINGS, plus the ServerProcess class
that wraps a subprocess.Popen child. This half has no xbmc* imports at
module scope -- importable and testable with plain python3, exercised
below with NO Kodi stubs at all: real filesystem via tmp_path, mocked
subprocess.Popen, and a real loopback listener for probe_listening()'s
function-local urllib probe, via the real_socket fixture which lifts
tests/conftest.py's autouse network-block guard for those tests.
"""
import contextlib
import os
import socket
import subprocess
import sys
import threading

import pytest

import lib.serverbin as serverbin
import lib.service_runner as service_runner

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


def test_resolve_binary_finds_the_android_private_dir_location(monkeypatch, tmp_path):
    """serverbin.install_dir() may pick a location other than
    <addon_data_dir>/bin (the Android app-private directory) --
    resolve_binary() must still find a binary installed there even though
    addon_data/bin itself has nothing."""
    addon_data = tmp_path / 'addon_data'
    private_dir = tmp_path / 'private' / 'bin'
    private_dir.mkdir(parents=True)
    bundled = private_dir / service_runner.BINARY_NAME
    _make_executable(bundled)
    monkeypatch.setattr(serverbin, 'install_dir', lambda profile_dir, addon_id: str(private_dir))
    assert service_runner.resolve_binary('', str(addon_data)) == str(bundled)


def test_resolve_binary_prefers_plain_bundled_dir_over_android_private_dir(monkeypatch, tmp_path):
    """<addon_data_dir>/bin is checked before serverbin.install_dir()'s
    pick -- a binary already sitting at the historical location wins."""
    addon_data = tmp_path / 'addon_data'
    bin_dir = addon_data / 'bin'
    bin_dir.mkdir(parents=True)
    plain_bundled = bin_dir / service_runner.BINARY_NAME
    _make_executable(plain_bundled)

    private_dir = tmp_path / 'private' / 'bin'
    private_dir.mkdir(parents=True)
    private_bundled = private_dir / service_runner.BINARY_NAME
    _make_executable(private_bundled)

    monkeypatch.setattr(serverbin, 'install_dir', lambda profile_dir, addon_id: str(private_dir))
    assert service_runner.resolve_binary('', str(addon_data)) == str(plain_bundled)


# ===========================================================================
# is_bundled_binary
# ===========================================================================


def test_is_bundled_binary_true_for_the_plain_addon_data_bin_dir(tmp_path):
    addon_data = tmp_path / 'addon_data'
    bundled = os.path.join(str(addon_data), 'bin', service_runner.BINARY_NAME)
    assert service_runner.is_bundled_binary(bundled, str(addon_data))


def test_is_bundled_binary_true_for_the_plain_addon_data_bin_dir_exe_variant(tmp_path):
    addon_data = tmp_path / 'addon_data'
    bundled = os.path.join(str(addon_data), 'bin', service_runner.BINARY_NAME + '.exe')
    assert service_runner.is_bundled_binary(bundled, str(addon_data))


def test_is_bundled_binary_true_for_the_android_private_dir(monkeypatch, tmp_path):
    addon_data = tmp_path / 'addon_data'
    private_dir = tmp_path / 'private' / 'bin'
    monkeypatch.setattr(serverbin, 'install_dir', lambda profile_dir, addon_id: str(private_dir))
    bundled = os.path.join(str(private_dir), service_runner.BINARY_NAME)
    assert service_runner.is_bundled_binary(bundled, str(addon_data))


def test_is_bundled_binary_false_for_a_path_hit(tmp_path):
    addon_data = tmp_path / 'addon_data'
    assert not service_runner.is_bundled_binary('/usr/bin/' + service_runner.BINARY_NAME, str(addon_data))


def test_is_bundled_binary_false_for_an_explicit_user_path(tmp_path):
    addon_data = tmp_path / 'addon_data'
    assert not service_runner.is_bundled_binary(str(tmp_path / 'my-own-server'), str(addon_data))


# ===========================================================================
# probe_listening
# ===========================================================================


@pytest.fixture
def real_socket(monkeypatch):
    """Lifts tests/conftest.py's autouse `_block_real_network` guard for
    tests that legitimately open a real loopback socket. `probe_listening()`
    speaks real HTTP via `urllib.request` -- exercising its
    connect/accept/timeout semantics end-to-end (TLS support, scheme-default
    ports, HTTPError-as-listening, ...) needs a real local TCP peer, not a
    mock. Both fixtures share the same per-test `monkeypatch` instance, so
    `undo()` here reverts exactly that guard's two patches and nothing else.
    """
    monkeypatch.undo()


class _FakeHTTPServer:
    """Minimal real TCP listener on 127.0.0.1, run on a background thread,
    used to exercise probe_listening()'s urllib.request-based GET end-to-end.

    - `respond_to=_RESPOND_TO_ANY` (default): every accepted connection
      gets `status_line` written back regardless of which path it asked
      for.
    - `respond_to=<path>`: only a request for exactly that path gets
      `status_line`; any other path is closed with no response at all.
    - `respond_to=None`: no path ever gets a response -- every connection
      is closed with no response, so probe_listening() must exhaust all
      of PROBE_PATHS and report nothing listening.
    - `hold_open=True`: connections are accepted but never read, written
      to, or closed until the server itself is closed -- forces a
      client-side read timeout.
    """

    _RESPOND_TO_ANY = object()

    def __init__(self, respond_to=_RESPOND_TO_ANY, status_line=b'HTTP/1.1 200 OK\r\n\r\n', hold_open=False):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind(('127.0.0.1', 0))
        self._sock.listen(8)
        self.port = self._sock.getsockname()[1]
        self.requested_paths = []
        self._conns = []
        thread = threading.Thread(target=self._serve, args=(respond_to, status_line, hold_open), daemon=True)
        thread.start()

    def _serve(self, respond_to, status_line, hold_open):
        while True:
            try:
                conn, _addr = self._sock.accept()
            except OSError:
                return
            self._conns.append(conn)
            if hold_open:
                continue
            try:
                request = conn.recv(4096)
                path = request.split(b' ')[1].decode('ascii') if request else None
                if path:
                    self.requested_paths.append(path)
                if respond_to is self._RESPOND_TO_ANY or path == respond_to:
                    conn.sendall(status_line)
            finally:
                conn.close()

    def close(self):
        self._sock.close()
        for conn in self._conns:
            try:
                conn.close()
            except OSError:
                pass


def _free_local_port():
    """A port on 127.0.0.1 that is free at the moment of the call -- used
    to build a "connection refused" target with no server behind it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


def test_probe_listening_true_on_first_probe_path_success(real_socket):
    server = _FakeHTTPServer(respond_to=service_runner.PROBE_PATHS[0])
    try:
        assert service_runner.probe_listening('http://127.0.0.1:%d' % server.port) is True
    finally:
        server.close()
    assert server.requested_paths == [service_runner.PROBE_PATHS[0]]


@pytest.mark.parametrize('responding_path', service_runner.PROBE_PATHS)
def test_probe_listening_true_when_any_probe_path_responds(real_socket, responding_path):
    """Every entry in PROBE_PATHS must be tried, in order, until one
    completes -- not just the first."""
    server = _FakeHTTPServer(respond_to=responding_path)
    try:
        assert service_runner.probe_listening('http://127.0.0.1:%d' % server.port) is True
    finally:
        server.close()
    assert server.requested_paths[-1] == responding_path


def test_probe_listening_true_on_http_error_status(real_socket):
    """An HTTP-level error status still proves *something* is bound to
    the port -- only connection-level failures mean "nothing listening"."""
    server = _FakeHTTPServer(status_line=b'HTTP/1.1 404 Not Found\r\n\r\n')
    try:
        assert service_runner.probe_listening('http://127.0.0.1:%d' % server.port) is True
    finally:
        server.close()


def test_probe_listening_false_when_connection_is_refused(real_socket):
    port = _free_local_port()
    assert service_runner.probe_listening('http://127.0.0.1:%d' % port, timeout=0.5) is False


def test_probe_listening_false_when_every_probe_path_gets_no_response(real_socket):
    server = _FakeHTTPServer(respond_to=None)
    try:
        assert service_runner.probe_listening('http://127.0.0.1:%d' % server.port) is False
    finally:
        server.close()
    assert server.requested_paths == list(service_runner.PROBE_PATHS)


def test_probe_listening_false_on_read_timeout(real_socket):
    server = _FakeHTTPServer(hold_open=True)
    try:
        assert service_runner.probe_listening('http://127.0.0.1:%d' % server.port, timeout=0.2) is False
    finally:
        server.close()


def test_probe_listening_passes_https_url_unmodified_to_urlopen(monkeypatch):
    """Regression guard for the raw-socket rewrite (since reverted) that
    silently mishandled both of these: `server_url` is a free-text setting
    with no scheme/loopback constraint (settings.xml), documented as the
    way to point at "an already-reachable instance (external or
    manually-started)" -- including a TLS-fronted one behind a port-less
    https:// URL. probe_listening() must hand the caller's URL to
    urllib.request untouched, never re-deriving host/port itself, so TLS
    and the scheme's real default port (443) apply -- not a hardcoded
    HTTP-only port fallback for a port-less URL."""
    seen = []

    def fake_urlopen(url, timeout=None):
        seen.append((url, timeout))
        return contextlib.nullcontext()


    monkeypatch.setattr('urllib.request.urlopen', fake_urlopen)
    assert service_runner.probe_listening('https://example.com', timeout=3.0) is True
    assert seen == [('https://example.com' + service_runner.PROBE_PATHS[0], 3.0)]


def test_probe_listening_forwards_caller_supplied_timeout(monkeypatch):
    seen = []

    def fake_urlopen(url, timeout=None):
        seen.append(timeout)
        return contextlib.nullcontext()


    monkeypatch.setattr('urllib.request.urlopen', fake_urlopen)
    service_runner.probe_listening('http://host', timeout=7.5)
    assert seen == [7.5]


def test_import_does_not_pull_in_urllib_request_or_http_client():
    """Regression guard: `probe_listening()` imports `urllib.request`/
    `urllib.error` locally (see its own docstring) precisely so a fresh
    interpreter importing lib.service_runner alone never ends up with
    urllib.request or http.client in sys.modules -- both pull in `ssl` +
    `email` purely to support features this module's own import path
    never uses."""
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    code = (
        'import sys\n'
        'import lib.service_runner\n'
        'assert "urllib.request" not in sys.modules, sorted(sys.modules)\n'
        'assert "http.client" not in sys.modules, sorted(sys.modules)\n'
    )
    result = subprocess.run(
        [sys.executable, '-c', code],
        cwd=repo_root, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


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


def test_start_omits_windows_only_kwargs_on_posix(monkeypatch, fake_popen, tmp_path):
    """`no_window_kwargs()` is `{}` off Windows; a stray `creationflags=0`
    would make POSIX's real `Popen` raise ValueError outright, so the
    absence of these keys (not merely a falsy value) is the contract.
    os.name is pinned rather than inherited from the host so this stays a
    POSIX-branch test even when the suite itself runs ON Windows."""
    monkeypatch.setattr(service_runner.procflags.os, 'name', 'posix')
    sp = _server_process(tmp_path)
    sp.start()

    kwargs = fake_popen[0]['kwargs']
    assert 'creationflags' not in kwargs
    assert 'startupinfo' not in kwargs

    sp.stop()


class _FakeStartupInfo:
    def __init__(self):
        self.dwFlags = 0
        self.wShowWindow = None


def test_start_forwards_no_window_kwargs_on_windows(monkeypatch, fake_popen, tmp_path):
    """Issue #30: stremio-server.exe is a console-subsystem Go binary;
    spawned from Kodi (a GUI process with no console), Windows allocates
    it a fresh console window. Closing that window sends
    CTRL_CLOSE_EVENT, killing the child, which the supervisor above then
    restarts -- popping a new window right back ("closing it reopens
    it"). start() must splat lib.procflags.no_window_kwargs() into the
    Popen() call to suppress that window, without disturbing any of the
    argv/env/stdout/stderr/stdin values the POSIX case already asserts."""
    monkeypatch.setattr(service_runner.procflags.os, 'name', 'nt')
    monkeypatch.setattr(
        service_runner.procflags.subprocess, 'STARTUPINFO', _FakeStartupInfo, raising=False)

    sp = _server_process(tmp_path, server_url='http://127.0.0.1:9090')
    sp.start()

    call = fake_popen[0]
    assert call['argv'] == ['/opt/bin/stremio-server']
    assert call['kwargs']['env']['APP_PATH'] == str(tmp_path / 'server')
    assert call['kwargs']['env']['HTTP_PORT'] == '9090'
    assert call['kwargs']['stderr'] == subprocess.STDOUT
    assert call['kwargs']['stdin'] == subprocess.DEVNULL
    assert call['kwargs']['stdout'].name == str(tmp_path / 'server.log')
    assert call['kwargs']['stdout'].mode == 'a'
    assert call['kwargs']['creationflags'] == 0x08000000  # CREATE_NO_WINDOW
    assert call['kwargs']['startupinfo'].wShowWindow == 0  # SW_HIDE

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


# --- maybe_rotate_log(): coarse-cadence periodic check for a LIVE process --


def test_maybe_rotate_log_does_not_stat_before_the_check_interval_elapses(monkeypatch, fake_popen, tmp_path):
    """Called repeatedly while the check interval has not yet elapsed since
    start() (or the last check), maybe_rotate_log() must not touch the
    filesystem at all -- this is what keeps main()'s HEALTHY-tick cost at
    zero disk touches between gates, even though it is called every tick."""
    log_path = tmp_path / 'server.log'
    log_path.write_bytes(b'x' * (service_runner.LOG_ROTATE_BYTES + 1))  # already oversized

    times = iter([
        1000.0,
        1000.0 + service_runner.LOG_ROTATE_CHECK_INTERVAL / 2,
        1000.0 + service_runner.LOG_ROTATE_CHECK_INTERVAL - 1,
    ])
    monkeypatch.setattr(service_runner.time, 'monotonic', lambda: next(times))

    sp = _server_process(tmp_path)
    sp.start()  # consumes times[0] for _started_at/_last_rotate_check; rotates the pre-existing oversized file
    log_path.write_bytes(b'y' * (service_runner.LOG_ROTATE_BYTES + 1))  # grows oversized again while "running"

    getsize_calls = []
    real_getsize = os.path.getsize

    def spy_getsize(path):
        getsize_calls.append(path)
        return real_getsize(path)

    monkeypatch.setattr(service_runner.os.path, 'getsize', spy_getsize)

    sp.maybe_rotate_log()  # interval not yet elapsed -> gated, no stat
    sp.maybe_rotate_log()

    assert getsize_calls == []
    assert log_path.stat().st_size == service_runner.LOG_ROTATE_BYTES + 1  # untouched
    sp.stop()


def test_maybe_rotate_log_rotates_an_oversized_log_once_the_interval_elapses(monkeypatch, fake_popen, tmp_path):
    log_path = tmp_path / 'server.log'

    times = iter([1000.0, 1000.0 + service_runner.LOG_ROTATE_CHECK_INTERVAL])
    monkeypatch.setattr(service_runner.time, 'monotonic', lambda: next(times))

    sp = _server_process(tmp_path)
    sp.start()  # log_path does not exist yet -- start()'s own rotation is a no-op
    log_path.write_bytes(b'x' * (service_runner.LOG_ROTATE_BYTES + 1))  # grew past threshold while "running"

    sp.maybe_rotate_log()  # exactly LOG_ROTATE_CHECK_INTERVAL elapsed -> gate fires

    backup = tmp_path / 'server.log.1'
    assert backup.exists()
    assert backup.stat().st_size == service_runner.LOG_ROTATE_BYTES + 1
    # Renamed away, not truncated: a live child's stdout fd follows the
    # inode to `.1` (see maybe_rotate_log()'s docstring) -- log_path itself
    # does not exist again until the next start() creates a fresh one.
    assert not log_path.exists()
    sp.stop()


def test_maybe_rotate_log_truncates_when_the_rename_itself_fails(monkeypatch, fake_popen, tmp_path):
    """Windows regression guard: os.rename() of a file the child holds open
    fails there with PermissionError (CPython never requests
    FILE_SHARE_DELETE, bpo-15244), which _rename_to_backup() swallows.
    maybe_rotate_log() must notice log_path is still there afterwards and
    fall back to truncating the live fd, or the log grows unbounded for
    the whole session on exactly the platform class the bound exists for."""
    log_path = tmp_path / 'server.log'

    times = iter([1000.0, 1000.0 + service_runner.LOG_ROTATE_CHECK_INTERVAL])
    monkeypatch.setattr(service_runner.time, 'monotonic', lambda: next(times))

    sp = _server_process(tmp_path)
    sp.start()
    log_path.write_bytes(b'x' * (service_runner.LOG_ROTATE_BYTES + 1))

    def failing_rename(src, dst):
        raise PermissionError(32, 'file in use')
    monkeypatch.setattr(service_runner.os, 'rename', failing_rename)

    sp.maybe_rotate_log()

    assert not (tmp_path / 'server.log.1').exists()
    assert log_path.exists()
    assert log_path.stat().st_size == 0  # truncated via the live fd instead
    sp.stop()


def test_maybe_rotate_log_resets_its_own_clock_after_a_check_fires(monkeypatch, fake_popen, tmp_path):
    """The gate is relative to the last check that actually ran, not to
    start(): once one check fires, the next one must wait a full interval
    from THAT check, not from process start."""
    log_path = tmp_path / 'server.log'

    times = iter([
        1000.0,                                                      # start()
        1000.0 + service_runner.LOG_ROTATE_CHECK_INTERVAL,           # 1st check: fires (nothing oversized)
        1000.0 + 2 * service_runner.LOG_ROTATE_CHECK_INTERVAL - 1,   # too soon for a 2nd check
    ])
    monkeypatch.setattr(service_runner.time, 'monotonic', lambda: next(times))

    sp = _server_process(tmp_path)
    sp.start()
    sp.maybe_rotate_log()  # 1st check -- advances the internal clock

    log_path.write_bytes(b'x' * (service_runner.LOG_ROTATE_BYTES + 1))
    getsize_calls = []
    real_getsize = os.path.getsize

    def spy_getsize(path):
        getsize_calls.append(path)
        return real_getsize(path)

    monkeypatch.setattr(service_runner.os.path, 'getsize', spy_getsize)

    sp.maybe_rotate_log()  # <1 interval since the 1st check -> gated

    assert getsize_calls == []
    sp.stop()


def test_maybe_rotate_log_truncates_a_still_growing_backup_after_the_first_rotation(monkeypatch, fake_popen, tmp_path):
    """Regression test for the defect where, after the first live rotation,
    every subsequent gate silently did nothing: re-checking the now-missing
    log_path raised FileNotFoundError, swallowed by a bare `except OSError`,
    while `.1` -- the file the child's fd actually keeps appending to --
    grew without bound for the rest of the run. The gate must watch `.1`
    once log_path is gone, and cap its growth by truncating the live fd."""
    log_path = tmp_path / 'server.log'
    backup = tmp_path / 'server.log.1'

    times = iter([
        1000.0,                                                    # start()
        1000.0 + service_runner.LOG_ROTATE_CHECK_INTERVAL,         # gate 1: rotates (rename)
        1000.0 + 2 * service_runner.LOG_ROTATE_CHECK_INTERVAL,     # gate 2: must truncate the live `.1`
    ])
    monkeypatch.setattr(service_runner.time, 'monotonic', lambda: next(times))

    sp = _server_process(tmp_path)
    sp.start()  # log_path does not exist yet -- start()'s own rotation is a no-op

    log_path.write_bytes(b'x' * (service_runner.LOG_ROTATE_BYTES + 1))
    sp.maybe_rotate_log()  # gate 1
    assert not log_path.exists()
    assert backup.stat().st_size == service_runner.LOG_ROTATE_BYTES + 1

    # The child keeps appending to the same inode, now only reachable at
    # `.1` -- simulate it growing past the threshold again.
    with open(backup, 'ab') as fh:
        fh.write(b'y' * (service_runner.LOG_ROTATE_BYTES + 1))

    sp.maybe_rotate_log()  # gate 2 -- must find `.1` oversized, not skip it

    assert not log_path.exists()  # nothing recreates it mid-run
    assert backup.exists()
    assert backup.stat().st_size == 0  # truncated back to zero via the still-open fd
    sp.stop()


def test_maybe_rotate_log_keeps_total_on_disk_bytes_bounded_across_many_gates(monkeypatch, fake_popen, tmp_path):
    """The documented invariant: total on-disk log bytes never exceed
    roughly 2x LOG_ROTATE_BYTES at any point across an arbitrarily long
    healthy session, not just for the first rotation cycle. Exercises
    four consecutive gate firings against a log that keeps growing
    between every one of them (gate 1 rotates; gates 2-4 must each
    truncate the still-growing `.1` back under the cap)."""
    log_path = tmp_path / 'server.log'
    backup = tmp_path / 'server.log.1'

    times = iter([1000.0 + i * service_runner.LOG_ROTATE_CHECK_INTERVAL for i in range(5)])
    monkeypatch.setattr(service_runner.time, 'monotonic', lambda: next(times))

    sp = _server_process(tmp_path)
    sp.start()  # consumes the first time value

    def total_bytes():
        return (
            (log_path.stat().st_size if log_path.exists() else 0)
            + (backup.stat().st_size if backup.exists() else 0)
        )

    def grow_live_file():
        live = log_path if log_path.exists() else backup
        with open(live, 'ab') as fh:
            fh.write(b'x' * (service_runner.LOG_ROTATE_BYTES + 1))

    for _ in range(4):
        grow_live_file()
        sp.maybe_rotate_log()
        assert total_bytes() <= 2 * service_runner.LOG_ROTATE_BYTES

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


def test_start_closes_opened_log_and_resets_state_when_popen_raises(monkeypatch, tmp_path):
    """start() is transactional: if Popen() fails after the log file was
    already opened, the fd must not leak and the object must look
    exactly like it never started (so a caller can safely retry)."""
    sp = _server_process(tmp_path)
    opened = []
    real_open = open

    def tracking_open(*args, **kwargs):
        fh = real_open(*args, **kwargs)
        opened.append(fh)
        return fh

    monkeypatch.setattr(service_runner, 'open', tracking_open, raising=False)
    monkeypatch.setattr(
        service_runner.subprocess, 'Popen',
        lambda *a, **kw: (_ for _ in ()).throw(OSError('exec failed')),
    )

    with pytest.raises(OSError):
        sp.start()

    assert len(opened) == 1
    assert opened[0].closed is True
    assert sp._proc is None
    assert sp._log_fh is None
    assert sp.running is False


def test_start_resets_state_when_log_open_raises(monkeypatch, tmp_path):
    sp = _server_process(tmp_path)
    monkeypatch.setattr(
        service_runner, 'open', lambda *a, **kw: (_ for _ in ()).throw(OSError('disk full')),
        raising=False,
    )

    with pytest.raises(OSError):
        sp.start()

    assert sp._proc is None
    assert sp._log_fh is None
    assert sp.running is False


def test_stop_propagates_second_post_kill_timeout_but_still_closes_log(fake_popen, tmp_path):
    """A child that survives even kill() (wedged/zombie) must not have its
    failure silently swallowed: the log fd still closes (in `finally`),
    but the exception propagates and `_proc`/`_started_at` are left
    alone -- `running` keeps reporting True so a caller never spawns a
    duplicate next to a possibly-still-alive process."""
    sp = _server_process(tmp_path)
    sp.start()
    fake_proc = fake_popen[0]['proc']
    fake_proc._wait_results = [
        subprocess.TimeoutExpired(cmd='stremio-server', timeout=3.0),
        subprocess.TimeoutExpired(cmd='stremio-server', timeout=3.0),
    ]
    log_fh = sp._log_fh

    with pytest.raises(subprocess.TimeoutExpired):
        sp.stop(grace=3.0)

    assert fake_proc.terminate_calls == 1
    assert fake_proc.kill_calls == 1
    assert fake_proc.wait_calls == [3.0, 3.0]
    assert log_fh.closed is True
    assert sp._log_fh is None
    assert sp.running is True  # not confirmed dead -- state intentionally kept
