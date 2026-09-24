"""Tests for lib.libserver: the ctypes-based supervisor for
libstremio-server.so (c-shared library mode, the SELinux-enforcing-
Android fallback for lib.service_runner.ServerProcess's fork+exec model).

Every test that touches `_load_library()`'s module-level cache resets
`libserver._loaded_lib`/`_loaded_path` to None via `monkeypatch` first, so
tests never leak the process-global cache across each other (order-
randomized by pytest-randomly) -- `monkeypatch` restores whatever value
was there before the test on teardown, chaining back to the real None
baseline every time.

`ctypes.CDLL` itself is monkeypatched (not `libserver.ctypes`, which
IS the real `ctypes` module) to return a small fake exposing
`ServerStart`/`ServerStop`/`ServerVersion` as plain callable objects --
plain objects, not bound methods, because `_load_library()` assigns
`.argtypes`/`.restype` onto each one, which a real bound method does not
support.
"""
import os
import threading
import time

import pytest

import lib.libserver as libserver
from lib.libserver import LibraryLoadError, LibraryServer, _load_library


class _FakeCFunc:
    """Stand-in for one ctypes function pointer: supports the
    `.argtypes`/`.restype` assignment `_load_library()` does, and
    records/drives calls via a plain Python callable."""

    def __init__(self, func):
        self.argtypes = None
        self.restype = None
        self._func = func
        self.call_count = 0
        self.calls = []

    def __call__(self, *args):
        self.call_count += 1
        self.calls.append(args)
        return self._func(*args)


class FakeLib:
    """Stand-in for a `ctypes.CDLL(path)` handle onto libstremio-server.so."""

    def __init__(self, server_start=None, server_stop=None, server_version=b"v-test"):
        self.ServerStart = _FakeCFunc(server_start or (lambda log_path, env_json: 0))
        self.ServerStop = _FakeCFunc(server_stop or (lambda: 0))
        self.ServerVersion = _FakeCFunc(lambda: server_version)


def _reset_cache(monkeypatch):
    monkeypatch.setattr(libserver, "_loaded_lib", None)
    monkeypatch.setattr(libserver, "_loaded_path", None)


# --- _load_library -----------------------------------------------------


def test_load_library_caches_and_returns_the_same_handle_for_the_same_path(monkeypatch):
    _reset_cache(monkeypatch)
    fake = FakeLib()
    cdll_calls = []
    monkeypatch.setattr(libserver.ctypes, "CDLL", lambda path: cdll_calls.append(path) or fake)

    first = _load_library("/fake/libstremio-server.so")
    second = _load_library("/fake/libstremio-server.so")

    assert first is fake
    assert second is fake
    assert cdll_calls == ["/fake/libstremio-server.so"]  # dlopen'd exactly once


def test_load_library_sets_argtypes_and_restype_on_every_export(monkeypatch):
    _reset_cache(monkeypatch)
    fake = FakeLib()
    monkeypatch.setattr(libserver.ctypes, "CDLL", lambda path: fake)

    _load_library("/fake/libstremio-server.so")

    assert fake.ServerStart.argtypes == [libserver.ctypes.c_char_p, libserver.ctypes.c_char_p]
    assert fake.ServerStart.restype == libserver.ctypes.c_int
    assert fake.ServerStop.argtypes == []
    assert fake.ServerStop.restype == libserver.ctypes.c_int
    assert fake.ServerVersion.argtypes == []
    assert fake.ServerVersion.restype == libserver.ctypes.c_char_p


def test_load_library_refuses_a_different_path_in_the_same_process(monkeypatch):
    """A CDLL is process-global from the moment dlopen() runs -- a second,
    different path must be refused rather than silently keeping the first
    load's symbols under a path that would claim otherwise."""
    _reset_cache(monkeypatch)
    monkeypatch.setattr(libserver.ctypes, "CDLL", lambda path: FakeLib())

    _load_library("/first/libstremio-server.so")

    logged = []
    with pytest.raises(LibraryLoadError, match="already loaded"):
        _load_library("/second/libstremio-server.so", log_fn=logged.append)

    assert len(logged) == 1
    assert "/first/libstremio-server.so" in logged[0]
    assert "/second/libstremio-server.so" in logged[0]


def test_load_library_is_idempotent_for_the_same_path_even_with_a_log_fn(monkeypatch):
    _reset_cache(monkeypatch)
    fake = FakeLib()
    monkeypatch.setattr(libserver.ctypes, "CDLL", lambda path: fake)
    logged = []

    _load_library("/fake/libstremio-server.so", log_fn=logged.append)
    result = _load_library("/fake/libstremio-server.so", log_fn=logged.append)

    assert result is fake
    assert logged == []  # same path never logs/raises


def test_load_library_raises_when_ctypes_unavailable(monkeypatch):
    _reset_cache(monkeypatch)
    monkeypatch.setattr(libserver, "ctypes", None)

    with pytest.raises(LibraryLoadError, match="ctypes is unavailable"):
        _load_library("/fake/libstremio-server.so")


def test_load_library_wraps_a_dlopen_oserror(monkeypatch):
    _reset_cache(monkeypatch)

    def _raise(path):
        raise OSError("cannot load: wrong ELF class")

    monkeypatch.setattr(libserver.ctypes, "CDLL", _raise)

    with pytest.raises(LibraryLoadError, match="failed to load"):
        _load_library("/fake/libstremio-server.so")


# --- LibraryServer.build_env -------------------------------------------


def test_build_env_pins_app_path_and_http_port_and_overlays_extra_env():
    server = LibraryServer(
        "/fake/libstremio-server.so", "http://127.0.0.1:9999", "/app", "/log",
        extra_env={"STREMIO_DISABLE_TRACKERS": "true"},
    )
    env = server.build_env()
    assert env == {
        "APP_PATH": "/app",
        "HTTP_PORT": "9999",
        "STREMIO_DISABLE_TRACKERS": "true",
    }


def test_build_env_does_not_inherit_this_process_os_environ(monkeypatch):
    """Unlike ServerProcess.build_env() (a real subprocess needs a full
    environment block), LibraryServer runs in-process -- forwarding this
    whole process's os.environ risks a same-named var meaning something
    different to the embedded Go runtime than it does here."""
    monkeypatch.setenv("A_VAR_THAT_SHOULD_NEVER_LEAK", "leaked")
    server = LibraryServer("/fake/libstremio-server.so", "http://127.0.0.1:11470", "/app", "/log")
    assert "A_VAR_THAT_SHOULD_NEVER_LEAK" not in server.build_env()


def test_build_env_falls_back_to_default_http_port_when_server_url_has_none():
    server = LibraryServer("/fake/libstremio-server.so", "http://127.0.0.1", "/app", "/log")
    assert server.build_env()["HTTP_PORT"] == str(libserver.DEFAULT_HTTP_PORT)


# --- LibraryServer lifecycle: start/poll/running/uptime/stop ------------


def test_library_server_never_started_reports_not_running_and_poll_none():
    server = LibraryServer("/fake/libstremio-server.so", "http://127.0.0.1:11470", "/app", "/log")
    assert server.running is False
    assert server.poll() is None
    assert server.uptime() is None


def test_library_server_start_poll_stop_full_lifecycle(monkeypatch, tmp_path):
    _reset_cache(monkeypatch)
    stop_event = threading.Event()
    started = threading.Event()

    def fake_server_start(log_path, env_json):
        started.set()
        stop_event.wait(timeout=5)
        return 0

    def fake_server_stop():
        stop_event.set()
        return 0

    fake = FakeLib(server_start=fake_server_start, server_stop=fake_server_stop)
    monkeypatch.setattr(libserver.ctypes, "CDLL", lambda path: fake)

    app_path = str(tmp_path / "server")
    log_path = str(tmp_path / "server.log")
    server = LibraryServer(
        "/fake/libstremio-server.so", "http://127.0.0.1:11470", app_path, log_path,
        extra_env={"STREMIO_DISABLE_TRACKERS": "true"},
    )

    server.start()
    assert started.wait(timeout=5), "ServerStart was never invoked on the background thread"
    assert server.running is True
    assert server.poll() is None  # still blocked inside ServerStart
    assert server.uptime() is not None and server.uptime() >= 0
    assert os.path.isdir(app_path)  # start() creates it, matching ServerProcess.start()

    log_path_arg, env_json_arg = fake.ServerStart.calls[0]
    assert log_path_arg == log_path.encode("utf-8")
    assert b'"STREMIO_DISABLE_TRACKERS": "true"' in env_json_arg

    server.stop()
    assert fake.ServerStop.call_count == 1
    assert server.running is False
    # Mirrors ServerProcess.stop(): a confirmed-stopped run discards its
    # process/start state, so a subsequent poll() reports None again
    # rather than the exit code -- callers are expected to have already
    # read `running`/discarded the instance by then, exactly like
    # ServerProcess's own `_proc = None` at the end of stop().
    assert server.poll() is None


def test_library_server_poll_reports_the_exit_code_when_the_thread_ends_on_its_own(
        monkeypatch, tmp_path):
    """A run that exits by itself (crash, or a clean self-shutdown never
    routed through THIS instance's stop()) must still be visible via
    poll() before anything discards the state -- the supervision loop's
    crash-restart branch depends on seeing the real exit code."""
    _reset_cache(monkeypatch)
    finish_event = threading.Event()

    def fake_server_start(log_path, env_json):
        finish_event.wait(timeout=5)
        return 7

    fake = FakeLib(server_start=fake_server_start)
    monkeypatch.setattr(libserver.ctypes, "CDLL", lambda path: fake)

    server = LibraryServer(
        "/fake/libstremio-server.so", "http://127.0.0.1:11470",
        str(tmp_path / "server"), str(tmp_path / "server.log"),
    )
    server.start()
    finish_event.set()  # simulate the server exiting on its own, not via this instance's stop()

    deadline = time.monotonic() + 5
    while server.running and time.monotonic() < deadline:
        time.sleep(0.01)

    assert server.poll() == 7
    assert server.running is False


def test_library_server_start_is_a_no_op_when_already_running(monkeypatch, tmp_path):
    _reset_cache(monkeypatch)
    stop_event = threading.Event()

    def fake_server_start(log_path, env_json):
        stop_event.wait(timeout=5)
        return 0

    def fake_server_stop():
        stop_event.set()
        return 0

    fake = FakeLib(server_start=fake_server_start, server_stop=fake_server_stop)
    monkeypatch.setattr(libserver.ctypes, "CDLL", lambda path: fake)

    server = LibraryServer(
        "/fake/libstremio-server.so", "http://127.0.0.1:11470",
        str(tmp_path / "server"), str(tmp_path / "server.log"),
    )
    server.start()
    server.start()  # must not spawn a second thread / call ServerStart again

    server.stop()
    assert fake.ServerStart.call_count == 1


def test_library_server_stop_is_safe_when_never_started():
    server = LibraryServer("/fake/libstremio-server.so", "http://127.0.0.1:11470", "/app", "/log")
    server.stop()  # must not raise
    assert server.running is False


def test_library_server_poll_reports_nonzero_when_run_thread_raises(monkeypatch, tmp_path):
    """A thread that dies from an unexpected exception must still report
    a nonzero exit code, never None -- otherwise the supervision loop
    would mistake a dead thread for a healthy one."""
    _reset_cache(monkeypatch)

    def _raise(log_path, env_json):
        raise RuntimeError("native call blew up")

    fake = FakeLib(server_start=_raise)
    monkeypatch.setattr(libserver.ctypes, "CDLL", lambda path: fake)

    logged = []
    server = LibraryServer(
        "/fake/libstremio-server.so", "http://127.0.0.1:11470",
        str(tmp_path / "server"), str(tmp_path / "server.log"),
        log_fn=logged.append,
    )
    server.start()
    deadline = time.monotonic() + 5
    while server.running and time.monotonic() < deadline:
        time.sleep(0.01)

    assert server.poll() == -1
    assert any("ServerStart raised" in message for message in logged)


def test_library_server_start_propagates_library_load_error(monkeypatch, tmp_path):
    _reset_cache(monkeypatch)
    monkeypatch.setattr(libserver, "ctypes", None)

    server = LibraryServer(
        "/fake/libstremio-server.so", "http://127.0.0.1:11470",
        str(tmp_path / "server"), str(tmp_path / "server.log"),
    )
    with pytest.raises(LibraryLoadError):
        server.start()
    assert server.running is False


def test_library_server_version_before_start_returns_none():
    server = LibraryServer("/fake/libstremio-server.so", "http://127.0.0.1:11470", "/app", "/log")
    assert server.version() is None


def test_library_server_version_after_load(monkeypatch, tmp_path):
    _reset_cache(monkeypatch)
    fake = FakeLib(server_version=b"stremio-server-go v0.14.0 (libstremio)")
    monkeypatch.setattr(libserver.ctypes, "CDLL", lambda path: fake)

    server = LibraryServer(
        "/fake/libstremio-server.so", "http://127.0.0.1:11470",
        str(tmp_path / "server"), str(tmp_path / "server.log"),
    )
    server._lib = _load_library("/fake/libstremio-server.so")
    assert server.version() == "stremio-server-go v0.14.0 (libstremio)"


# --- maybe_rotate_log ----------------------------------------------------


def test_maybe_rotate_log_is_a_noop_below_the_check_interval(tmp_path):
    log_path = tmp_path / "server.log"
    log_path.write_bytes(b"x" * (libserver.LOG_ROTATE_BYTES + 1))
    server = LibraryServer("/fake/libstremio-server.so", "http://127.0.0.1:11470", "/app", str(log_path))
    server._last_rotate_check = time.monotonic()

    server.maybe_rotate_log()

    assert log_path.exists()
    assert not (tmp_path / "server.log.1").exists()


def test_maybe_rotate_log_renames_an_oversized_log_once_the_interval_elapses(tmp_path):
    log_path = tmp_path / "server.log"
    log_path.write_bytes(b"x" * (libserver.LOG_ROTATE_BYTES + 1))
    server = LibraryServer("/fake/libstremio-server.so", "http://127.0.0.1:11470", "/app", str(log_path))
    server._last_rotate_check = time.monotonic() - libserver.LOG_ROTATE_CHECK_INTERVAL - 1

    server.maybe_rotate_log()

    assert not log_path.exists()
    backup = tmp_path / "server.log.1"
    assert backup.exists()
    assert backup.stat().st_size == libserver.LOG_ROTATE_BYTES + 1


def test_maybe_rotate_log_truncates_the_backup_when_it_grows_oversized_again(tmp_path):
    log_path = tmp_path / "server.log"
    backup = tmp_path / "server.log.1"
    backup.write_bytes(b"x" * (libserver.LOG_ROTATE_BYTES + 1))
    server = LibraryServer("/fake/libstremio-server.so", "http://127.0.0.1:11470", "/app", str(log_path))
    server._last_rotate_check = time.monotonic() - libserver.LOG_ROTATE_CHECK_INTERVAL - 1

    server.maybe_rotate_log()  # log_path itself is gone, so `.1` is the live file this time

    assert backup.exists()
    assert backup.stat().st_size == 0


def test_maybe_rotate_log_does_nothing_when_under_threshold(tmp_path):
    log_path = tmp_path / "server.log"
    log_path.write_bytes(b"small")
    server = LibraryServer("/fake/libstremio-server.so", "http://127.0.0.1:11470", "/app", str(log_path))
    server._last_rotate_check = time.monotonic() - libserver.LOG_ROTATE_CHECK_INTERVAL - 1

    server.maybe_rotate_log()

    assert log_path.read_bytes() == b"small"


# --- module sanity --------------------------------------------------------


def test_library_supported_matches_real_ctypes_availability():
    import ctypes as real_ctypes
    assert libserver.LIBRARY_SUPPORTED == (real_ctypes is not None)
