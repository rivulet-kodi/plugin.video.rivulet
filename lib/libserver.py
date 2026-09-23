"""ctypes-based supervisor for the stremio-server-go c-shared library
(``libstremio-server.so``), the SELinux-*enforcing*-Android fallback for
``lib.service_runner.ServerProcess``'s fork+exec model. See
``lib.serverbin.UnsupportedPlatformError``'s docstring for exactly which
devices need this: a targetSdk>=29 device running SELinux *enforcing*
denies exec() of anything the app itself can write to, no matter where
it is placed -- but dlopen()ing a library from that same app-private
location is not the exec() syscall that policy targets, so a c-shared
build of the same server can still run in-process there.

Pure Python, no ``xbmc*`` imports -- unit-testable directly, same
discipline as ``lib.serverbin``/``lib.service_runner``'s process-
management core.

Library-mode contract (matches ``cmd/libstremio`` in stremio-server-go,
built as a ``c-shared`` buildmode alongside the normal executable in the
same Android release archives -- see ``lib.serverbin.LIBRARY_NAME``):

    int ServerStart(char* logPath, char* envJSON)
        BLOCKING until the server stops. envJSON is a JSON object of the
        same env vars the executable reads (APP_PATH, HTTP_PORT,
        STREMIO_*, ...). Logs are appended to logPath. Returns 0 on a
        clean stop, 1 on an init error, 2 if already running.
    int ServerStop(void)
        Graceful shutdown (5s internal timeout). Returns 0; safe to call
        even when nothing is running.
    char* ServerVersion(void)
        A static string; the caller must not free it.

``ServerStart`` blocks its calling thread for the server's entire
lifetime, so ``LibraryServer.start()`` runs it on a background daemon
thread and treats "the thread has exited" as
``ServerProcess.poll()`` treats "the child process has exited" -- the
two classes converge on the same
``running``/``build_env``/``start``/``stop``/``poll``/``uptime``/
``maybe_rotate_log`` surface so ``lib.service_runner``'s supervision
loop can hold either kind of instance in its ``state.proc`` slot without
caring which one it has.
"""
import json
import os
import threading
import time
from urllib.parse import urlparse

try:
    import ctypes
except ImportError:  # pragma: no cover - no known CPython build lacks ctypes
    ctypes = None  # type: ignore[assignment]

#: False when ctypes itself is unavailable in this Python build -- the
#: single gate every caller (lib.service_runner's library-mode fallback)
#: must check before touching anything else in this module. There is no
#: known CPython distribution that actually lacks ctypes, but the guard
#: costs nothing and keeps the "unsupported" story explicit rather than
#: an AttributeError surfacing from deep inside _load_library().
LIBRARY_SUPPORTED = ctypes is not None

#: Duplicated from lib.service_runner's identical constants rather than
#: imported, on purpose: lib.service_runner is the orchestration layer
#: that reaches for THIS module (lazily, to keep import cost off every
#: platform that never needs it -- see its _resolve_library_candidate()),
#: so the reverse import direction would invert that layering. Keep in
#: sync with lib.service_runner.LOG_ROTATE_BYTES/LOG_ROTATE_CHECK_INTERVAL
#: if either ever changes.
LOG_ROTATE_BYTES = 5 * 1024 * 1024
LOG_ROTATE_CHECK_INTERVAL = 300.0

#: Same duplication rationale as the rotation constants above, mirroring
#: lib.service_runner.DEFAULT_HTTP_PORT.
DEFAULT_HTTP_PORT = 11470


def _http_port_from_url(server_url, default=DEFAULT_HTTP_PORT):
    """Trimmed-down copy of lib.service_runner.http_port_from_url() --
    see that function's docstring; duplicated here for the same reason
    as the constants above."""
    try:
        port = urlparse(server_url).port
    except (ValueError, AttributeError):
        return default
    return port if port is not None else default


_loaded_lib = None
_loaded_path = None
_load_lock = threading.Lock()


class LibraryLoadError(Exception):
    """Raised when libstremio-server.so cannot be dlopen()'d, or when a
    second, *different* path is requested from this same process (see
    ``_load_library()``'s docstring for why that is refused rather than
    silently honored)."""


def _load_library(path, log_fn=None):
    """Load and cache `path`'s CDLL for the remaining lifetime of this
    process.

    A CDLL is process-global from the moment dlopen() actually runs --
    CPython exposes no "unload and load a different .so at the same
    symbol names" primitive, and libc's dlclose() is unreliable once a
    library has spawned threads of its own (which ServerStart's
    blocking call always does). So the first successful load wins for
    the rest of this process: a later call with a DIFFERENT path is
    refused outright -- logged, then raised -- rather than silently
    keeping the stale symbols from the first load under a path that
    would claim otherwise, which would run the wrong server binary
    without any signal that it happened. A repeated call with the SAME
    path is idempotent and just returns the cached handle; this is also
    why any pending SERVER_TAG upgrade of the .so on disk must wait for
    the next Kodi start (a fresh process) to actually take effect --
    lib.service_runner's library-mode path never calls
    serverbin's upgrade-if-stale helper for exactly this reason.
    """
    global _loaded_lib, _loaded_path
    with _load_lock:
        if _loaded_lib is not None:
            if _loaded_path != path:
                message = (
                    "libstremio-server.so already loaded from %r in this process; "
                    "refusing to load a different path %r (restart Kodi to switch)"
                    % (_loaded_path, path)
                )
                if log_fn is not None:
                    log_fn(message)
                raise LibraryLoadError(message)
            return _loaded_lib

        if ctypes is None:
            raise LibraryLoadError("ctypes is unavailable; c-shared library mode is unsupported")

        try:
            lib = ctypes.CDLL(path)
        except OSError as exc:
            raise LibraryLoadError("failed to load %s: %s" % (path, exc))

        lib.ServerStart.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
        lib.ServerStart.restype = ctypes.c_int
        lib.ServerStop.argtypes = []
        lib.ServerStop.restype = ctypes.c_int
        lib.ServerVersion.argtypes = []
        lib.ServerVersion.restype = ctypes.c_char_p

        _loaded_lib = lib
        _loaded_path = path
        return lib


class LibraryServer:
    """Owns the lifecycle of one stremio-server-go run inside this
    process via ``libstremio-server.so``. Mirrors
    ``lib.service_runner.ServerProcess``'s public surface (``running``,
    ``build_env``, ``start``, ``stop``, ``poll``, ``uptime``,
    ``maybe_rotate_log``) so the supervision loop can treat either kind
    of instance identically.
    """

    def __init__(self, library_path, server_url, app_path, log_path, extra_env=None, log_fn=None):
        self.library_path = library_path
        self.server_url = server_url
        self.app_path = app_path
        self.log_path = log_path
        self.extra_env = extra_env or {}
        self._log_fn = log_fn
        self._lib = None
        self._thread = None
        self._exit_code = None
        self._started_at = None
        self._last_rotate_check = None

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def build_env(self):
        """Return the ``{"APP_PATH": ..., "HTTP_PORT": ..., ...}`` overlay
        JSON-encoded and passed as ``ServerStart``'s ``envJSON`` argument.

        Unlike ``ServerProcess.build_env()`` this does NOT start from a
        copy of this process's own ``os.environ``: there is no child
        process here to inherit one for, and forwarding this whole
        process's environment risks a same-named var (``PATH``,
        ``HOME``, a JVM/Android-runtime variable, ...) meaning something
        entirely different to the embedded Go runtime than it does here.
        Only the vars stremio-server-go's own main()/libstremio actually
        read matter, and every one of those is either pinned below or
        forwarded via `extra_env` (see
        lib.service_runner.extra_env_from_settings()).
        """
        env = {"APP_PATH": self.app_path, "HTTP_PORT": str(_http_port_from_url(self.server_url))}
        env.update(self.extra_env)
        return env

    def _rename_to_backup(self):
        """Same rename-log-aside rotation lib.service_runner.ServerProcess
        uses -- see that method's docstring. No open fd of our own to
        worry about here: the Go library appends to `log_path` itself,
        so a bare `os.rename()` is the whole story on every platform
        (see `maybe_rotate_log()` for why the Windows open-file caveat
        that method carries doesn't apply here)."""
        try:
            backup = self.log_path + ".1"
            if os.path.exists(backup):
                os.remove(backup)
            os.rename(self.log_path, backup)
        except OSError:
            pass

    def _rotate_log(self):
        try:
            if os.path.getsize(self.log_path) > LOG_ROTATE_BYTES:
                self._rename_to_backup()
        except OSError:
            pass

    def start(self):
        """Load the library (once per process -- see `_load_library()`)
        and run `ServerStart` on a background daemon thread, which
        blocks for the server's entire lifetime. Raises `LibraryLoadError`
        (never starts a thread) when the load fails or refuses a
        different path already loaded elsewhere in this process."""
        if self.running:
            return
        os.makedirs(self.app_path, exist_ok=True)
        self._rotate_log()

        self._lib = _load_library(self.library_path, log_fn=self._log_fn)
        env_json = json.dumps(self.build_env()).encode("utf-8")
        log_path = self.log_path.encode("utf-8")
        self._exit_code = None

        def _run():
            try:
                self._exit_code = self._lib.ServerStart(log_path, env_json)
            except Exception as exc:  # noqa: BLE001 - surface as a nonzero exit, never a silently-dead thread
                self._exit_code = -1
                if self._log_fn is not None:
                    self._log_fn("libstremio-server ServerStart raised: %r" % (exc,))

        self._thread = threading.Thread(target=_run, name="stremio-server-library", daemon=True)
        self._thread.start()
        self._started_at = time.monotonic()
        self._last_rotate_check = self._started_at

    def maybe_rotate_log(self):
        """Periodic size check for the live log, called by main()'s
        HEALTHY branch every poll tick -- same cadence/purpose as
        `ServerProcess.maybe_rotate_log()`, simplified because nothing
        in THIS process holds the log file open: the Go library appends
        to `log_path` by its own path-opened fd, so once it has been
        renamed aside (the first rotation), truncating the new live name
        (`.1`) back to zero via a plain `os.truncate()` by path has the
        exact same effect as ServerProcess's same-inode ftruncate() --
        there is no writer-owned Python fd to keep undisturbed here."""
        now = time.monotonic()
        if self._last_rotate_check is not None and now - self._last_rotate_check < LOG_ROTATE_CHECK_INTERVAL:
            return
        self._last_rotate_check = now
        live_path = self.log_path if os.path.exists(self.log_path) else self.log_path + ".1"
        try:
            oversized = os.path.getsize(live_path) > LOG_ROTATE_BYTES
        except OSError:
            return
        if not oversized:
            return
        if live_path == self.log_path:
            self._rename_to_backup()
        else:
            try:
                os.truncate(live_path, 0)
            except OSError:
                pass

    def poll(self):
        """Return the exit code once the run thread has finished, else
        None -- mirrors `ServerProcess.poll()`'s `Popen.poll()` semantics
        using `Thread.is_alive()` instead of a process id. A thread that
        died from an unexpected exception (caught in `_run()` above)
        still reports a nonzero code here rather than None, so the
        supervision loop's crash-restart branch fires instead of
        mistaking a dead thread for a healthy one."""
        if self._thread is None:
            return None
        if self._thread.is_alive():
            return None
        return self._exit_code if self._exit_code is not None else -1

    def uptime(self):
        """Seconds since start(), or None if never started."""
        if self._started_at is None:
            return None
        return time.monotonic() - self._started_at

    def stop(self, grace=5.0):
        """Call `ServerStop()` (graceful, with its own 5s internal
        timeout per the c-shared contract) and join the run thread.

        Mirrors `ServerProcess.stop()`'s contract: safe to call when not
        running, and a thread still alive after `grace` leaves
        `self._thread`/`self._started_at` untouched (raising instead) so
        `running` keeps reporting True rather than masking a wedged
        server -- the caller must keep polling the same instance rather
        than starting a duplicate next to a possibly-still-alive one.
        """
        if self._lib is not None:
            try:
                self._lib.ServerStop()
            except Exception as exc:  # noqa: BLE001 - a failed native call must never crash the supervision loop
                if self._log_fn is not None:
                    self._log_fn("libstremio-server ServerStop raised: %r" % (exc,))

        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=grace)
            if self._thread.is_alive():
                raise RuntimeError(
                    "stremio-server library thread still running %.1fs after ServerStop" % grace)

        self._thread = None
        self._started_at = None

    def version(self):
        """Return `ServerVersion()`'s string, or None before the library
        has been loaded. Best-effort/diagnostic only; nothing in
        lib.service_runner depends on this."""
        if self._lib is None:
            return None
        raw = self._lib.ServerVersion()
        return raw.decode("utf-8", "replace") if raw is not None else None
