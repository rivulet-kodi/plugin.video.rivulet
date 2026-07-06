"""Kodi background service: supervises a local stremio-server-go process.

Launch interface verified against ~/M0Rf30/stremio-server-go @ cmd/stremio-server/main.go:
  - No CLI flags besides a `version`/`-version`/`--version` subcommand that just
    prints and exits (main.go:126-135) -- the daemon itself takes NO arguments.
  - All runtime config is via environment variables (main.go:141-181):
      APP_PATH   - data/cache root, default "~/.stremio-server" (main.go:141-144)
      HTTP_PORT  - enginefs HTTP API port, default 11470 (main.go:150)
    (HTTPS_PORT/BT_LISTEN_PORT/etc. are left at their own defaults; we only need
    to pin APP_PATH and HTTP_PORT to keep the addon and the child process
    agreeing on where data lives and which port `server_url` points at.)
  - Logging goes to os.Stderr via internal/logging (logging.go:28-29), text or
    json per STREMIO_LOG_FORMAT/STREMIO_LOG_LEVEL -- there is no built-in log
    file, so this module captures the child's stdout+stderr into a file itself.
  - Shutdown is graceful: main.go:387-412 listens for os.Interrupt/SIGTERM and
    calls http.Server.Shutdown with a 5s timeout per listener. Popen.terminate()
    sends SIGTERM on POSIX, so a plain terminate()-then-wait(grace) drives the
    same path; kill() is only the fallback for a wedged process.

This module is split in two halves:
  - Pure process-management core (resolve_binary, probe_listening, ServerProcess,
    backoff helpers) -- no `xbmc*` imports anywhere at module scope, so it can be
    imported and unit-tested with plain python3.
  - main(), which does all `xbmc*` imports locally and drives an xbmc.Monitor
    loop on top of the pure core.
"""

import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

ADDON_ID = "plugin.video.rivulet"
DEFAULT_SERVER_URL = "http://127.0.0.1:11470"
DEFAULT_HTTP_PORT = 11470

BINARY_NAME = "stremio-server"
PROBE_PATHS = ("/settings", "/stats.json")
PROBE_TIMEOUT = 2.0

# Restart backoff schedule for a crashing child: 5s, 10s, 30s, then capped at 30s.
RESTART_BACKOFF = (5, 10, 30)
# A run shorter than this does not count as "stable" -- backoff keeps climbing
# instead of resetting, so a crash loop is actually throttled.
MIN_STABLE_UPTIME = 60.0

LOG_FILENAME = "server.log"
LOG_ROTATE_BYTES = 5 * 1024 * 1024

IDLE_POLL_INTERVAL = 2.0
HEALTHY_POLL_INTERVAL = 2.0
EXTERNAL_RECHECK_INTERVAL = 10.0
MISSING_BINARY_RECHECK_INTERVAL = 5.0


def http_port_from_url(server_url, default=DEFAULT_HTTP_PORT):
    """Extract the TCP port `server_url` points at, falling back to `default`."""
    try:
        port = urlparse(server_url).port
    except (ValueError, AttributeError):
        return default
    return port if port is not None else default


def resolve_binary(explicit_path, addon_data_dir):
    """Resolve the stremio-server-go binary path.

    Priority: explicit setting -> <addon_data_dir>/bin/stremio-server[.exe] ->
    PATH lookup. Returns None when nothing usable is found.
    """
    if explicit_path and os.path.isfile(explicit_path) and os.access(explicit_path, os.X_OK):
        return explicit_path

    bundled = os.path.join(addon_data_dir, "bin", BINARY_NAME)
    for candidate in (bundled, bundled + ".exe"):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate

    return shutil.which(BINARY_NAME)


def probe_listening(server_url, timeout=PROBE_TIMEOUT):
    """Return True if something is already answering at server_url.

    Any completed HTTP exchange (including an HTTP error status) means a
    server is bound to that port -- only connection-level failures (refused,
    timed out, unresolvable) count as "nothing listening".
    """
    base = server_url.rstrip("/")
    for path in PROBE_PATHS:
        try:
            urllib.request.urlopen(base + path, timeout=timeout)
            return True
        except urllib.error.HTTPError:
            return True
        except Exception:
            continue
    return False


class ServerProcess:
    """Owns the lifecycle of one stremio-server-go child process.

    Pure process management: no `xbmc*` imports, safe to unit test directly.
    """

    def __init__(self, binary, server_url, app_path, log_path):
        self.binary = binary
        self.server_url = server_url
        self.app_path = app_path
        self.log_path = log_path
        self._proc = None
        self._log_fh = None
        self._started_at = None

    @property
    def running(self):
        return self._proc is not None and self._proc.poll() is None

    def build_env(self):
        env = os.environ.copy()
        env["APP_PATH"] = self.app_path
        env["HTTP_PORT"] = str(http_port_from_url(self.server_url))
        return env

    def _rotate_log(self):
        try:
            if os.path.getsize(self.log_path) > LOG_ROTATE_BYTES:
                backup = self.log_path + ".1"
                if os.path.exists(backup):
                    os.remove(backup)
                os.rename(self.log_path, backup)
        except OSError:
            pass

    def start(self):
        if self.running:
            return
        os.makedirs(self.app_path, exist_ok=True)
        self._rotate_log()
        self._log_fh = open(self.log_path, "a", buffering=1)
        self._proc = subprocess.Popen(
            [self.binary],
            env=self.build_env(),
            stdout=self._log_fh,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
        )
        self._started_at = time.monotonic()

    def poll(self):
        """Return the exit code if the child has died, else None."""
        if self._proc is None:
            return None
        return self._proc.poll()

    def uptime(self):
        """Seconds since start(), or None if never started."""
        if self._started_at is None:
            return None
        return time.monotonic() - self._started_at

    def stop(self, grace=5.0):
        """Terminate the child, escalating to kill() after `grace` seconds."""
        proc, self._proc = self._proc, None
        self._started_at = None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=grace)
        elif proc is not None:
            proc.wait()
        if self._log_fh is not None:
            self._log_fh.close()
            self._log_fh = None


def main():
    """Entry point for service.py: xbmc.Monitor-driven supervision loop."""
    import xbmc
    import xbmcaddon
    import xbmcgui
    import xbmcvfs

    addon = xbmcaddon.Addon()
    profile_dir = xbmcvfs.translatePath(addon.getAddonInfo("profile"))
    os.makedirs(profile_dir, exist_ok=True)

    app_path = os.path.join(profile_dir, "server")
    log_path = os.path.join(profile_dir, LOG_FILENAME)

    def log(level, message):
        xbmc.log(f"[{ADDON_ID}] {message}", level)

    class ServiceMonitor(xbmc.Monitor):
        def __init__(self):
            super().__init__()
            self.restart_requested = False
            self.enabled = False
            self.binary_setting = ""
            self.server_url = DEFAULT_SERVER_URL
            self._refresh()

        def _refresh(self):
            self.enabled = addon.getSettingBool("server_enable")
            self.binary_setting = addon.getSetting("server_binary")
            self.server_url = addon.getSetting("server_url") or DEFAULT_SERVER_URL

        def onSettingsChanged(self):
            prev = (self.enabled, self.binary_setting, self.server_url)
            self._refresh()
            if prev != (self.enabled, self.binary_setting, self.server_url):
                self.restart_requested = True

    monitor = ServiceMonitor()
    proc = None
    backoff_idx = 0
    notified_missing = False

    while not monitor.abortRequested():
        if monitor.restart_requested:
            monitor.restart_requested = False
            if proc is not None:
                log(xbmc.LOGINFO, "settings changed, restarting embedded server")
                proc.stop()
                proc = None
            backoff_idx = 0
            notified_missing = False

        interval = IDLE_POLL_INTERVAL

        if not monitor.enabled:
            if proc is not None:
                log(xbmc.LOGINFO, "embedded server disabled, stopping")
                proc.stop()
                proc = None
        elif proc is not None:
            code = proc.poll()
            if code is None:
                interval = HEALTHY_POLL_INTERVAL
            else:
                if (proc.uptime() or 0) >= MIN_STABLE_UPTIME:
                    backoff_idx = 0
                log(xbmc.LOGWARNING, f"embedded server exited (code {code}), restarting")
                interval = RESTART_BACKOFF[min(backoff_idx, len(RESTART_BACKOFF) - 1)]
                backoff_idx = min(backoff_idx + 1, len(RESTART_BACKOFF) - 1)
                proc = None
        else:
            # Nothing of ours running: prefer an already-reachable instance
            # (external or manually-started) over spawning a duplicate.
            if probe_listening(monitor.server_url):
                notified_missing = False
                interval = EXTERNAL_RECHECK_INTERVAL
            else:
                binary = resolve_binary(monitor.binary_setting, profile_dir)
                if binary is None:
                    if not notified_missing:
                        xbmcgui.Dialog().notification(
                            addon.getAddonInfo("name"),
                            addon.getLocalizedString(30031),
                            xbmcgui.NOTIFICATION_ERROR,
                        )
                        log(xbmc.LOGERROR, "stremio-server binary not found")
                        notified_missing = True
                    interval = MISSING_BINARY_RECHECK_INTERVAL
                else:
                    notified_missing = False
                    log(xbmc.LOGINFO, f"starting embedded server: {binary}")
                    proc = ServerProcess(binary, monitor.server_url, app_path, log_path)
                    proc.start()
                    interval = HEALTHY_POLL_INTERVAL

        if monitor.waitForAbort(interval):
            break

    if proc is not None:
        log(xbmc.LOGINFO, "shutting down embedded server")
        proc.stop()
