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
import datetime
import os
import subprocess
import sys
import urllib.error

import pytest

import lib.serverbin as serverbin
import lib.service_runner as service_runner
import lib.settings as lib_settings
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
    session is exactly right."""
    monkeypatch.setattr(service_runner, 'probe_listening', lambda *a, **kw: False)
    monkeypatch.setattr(service_runner, 'resolve_binary', lambda *a, **kw: None)

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
    assert intervals == [service_runner.MISSING_BINARY_RECHECK_INTERVAL] * 3

    # Attempted exactly once across all 3 iterations, never retried.
    assert install_calls == [os.path.join(str(tmp_path), 'bin')]

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


def test_main_crash_path_closes_the_log_file_handle(fake_popen, monkeypatch, tmp_path):
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


# ===========================================================================
# lib.settings: setting_bool / setting_int -- pure, Kodi-independent setting
# parsing shared by lib.ui.compat's setting_bool()/setting_int() UI wrappers
# (see tests/test_uicommon.py for delegation parity) and by
# ServiceMonitor._refresh() above (server_enable + every EXTRA_ENV_SETTINGS
# bool/int/mb_to_bytes row). Exercised directly here with a minimal
# `getSetting()`-only stand-in -- no xbmc* stubs needed, this module takes
# no Kodi imports at all.
# ===========================================================================


class _RawSettingAddon:
    """Minimal `addon.getSetting(key) -> str` stand-in -- lib.settings
    never calls any other Addon method."""

    def __init__(self, values=None, raises=False):
        self._values = values or {}
        self._raises = raises

    def getSetting(self, key):
        if self._raises:
            raise RuntimeError('boom')
        return self._values.get(key, '')


def test_setting_bool_missing_key_returns_default():
    addon = _RawSettingAddon({})
    assert lib_settings.setting_bool(addon, 'missing', True) is True
    assert lib_settings.setting_bool(addon, 'missing', False) is False


def test_setting_bool_malformed_value_returns_default():
    addon = _RawSettingAddon({'k': 'not-a-bool'})
    assert lib_settings.setting_bool(addon, 'k', True) is True


@pytest.mark.parametrize('raw,expected', [
    ('true', True), ('TRUE', True), ('True', True), ('1', True),
    ('yes', True), ('YES', True), ('on', True), ('On', True),
    ('false', False), ('FALSE', False), ('0', False),
    ('no', False), ('NO', False), ('off', False), ('OFF', False),
])
def test_setting_bool_parses_mixed_case_and_synonyms(raw, expected):
    addon = _RawSettingAddon({'k': raw})
    assert lib_settings.setting_bool(addon, 'k', not expected) is expected


def test_setting_bool_never_raises_when_getsetting_raises():
    addon = _RawSettingAddon(raises=True)
    assert lib_settings.setting_bool(addon, 'k', True) is True


def test_setting_int_missing_key_returns_default():
    addon = _RawSettingAddon({})
    assert lib_settings.setting_int(addon, 'missing', 42) == 42


def test_setting_int_malformed_value_returns_default():
    addon = _RawSettingAddon({'k': 'not-a-number'})
    assert lib_settings.setting_int(addon, 'k', 7) == 7


def test_setting_int_zero_is_not_treated_as_missing():
    addon = _RawSettingAddon({'k': '0'})
    assert lib_settings.setting_int(addon, 'k', 99) == 0


def test_setting_int_negative_value_parsed_without_minimum():
    addon = _RawSettingAddon({'k': '-5'})
    assert lib_settings.setting_int(addon, 'k', 0) == -5


def test_setting_int_negative_value_clamped_up_to_minimum():
    addon = _RawSettingAddon({'k': '-5'})
    assert lib_settings.setting_int(addon, 'k', 0, minimum=1) == 1


def test_setting_int_value_at_or_above_minimum_is_unclamped():
    addon = _RawSettingAddon({'k': '10'})
    assert lib_settings.setting_int(addon, 'k', 0, minimum=1) == 10


def test_setting_int_never_raises_when_getsetting_raises():
    addon = _RawSettingAddon(raises=True)
    assert lib_settings.setting_int(addon, 'k', 42) == 42
