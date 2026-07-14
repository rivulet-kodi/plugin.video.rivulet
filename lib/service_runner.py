"""Kodi background service: supervises a local stremio-server-go process.

Launch interface verified against ~/M0Rf30/stremio-server-go @ cmd/stremio-server/main.go:
  - No CLI flags besides a `version`/`-version`/`--version` subcommand that just
    prints and exits (main.go:126-135) -- the daemon itself takes NO arguments.
  - All runtime config is via environment variables (main.go:141-181):
      APP_PATH   - data/cache root, default "~/.stremio-server" (main.go:141-144)
      HTTP_PORT  - enginefs HTTP API port, default 11470 (main.go:150)
    (APP_PATH and HTTP_PORT are pinned internally so the addon and the child
    process agree on where data lives and which port `server_url` points at;
    every other env var main.go reads is exposed as its own Kodi setting via
    EXTRA_ENV_SETTINGS/extra_env_from_settings below instead of being pinned.)
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
# After an auto-download completes, recheck almost immediately so the
# freshly-installed binary is picked up on the very next loop iteration
# instead of waiting out a full missing-binary recheck cycle.
POST_DOWNLOAD_RECHECK_INTERVAL = 0.5

# Every env var stremio-server-go's main() reads besides APP_PATH/HTTP_PORT
# (which stay pinned in ServerProcess.build_env()), one row per Kodi setting:
# (kodi_setting_id, env_var, kind). `kind` drives both how ServiceMonitor
# (inside main() below) reads the raw Kodi setting value and how
# extra_env_from_settings() turns it into an env var string:
#   'string'      - forwarded only when truthy (matches main.go treating ""
#                   as "use the binary's own default").
#   'int'         - always forwarded as str(value); the Kodi default equals
#                   the binary's own default, so an untouched setting is a
#                   no-op.
#   'mb_to_bytes' - Kodi setting stores MB; always forwarded as
#                   str(value * 1024 * 1024).
#   'bool'        - always forwarded as 'true'/'false'.
EXTRA_ENV_SETTINGS = (
    ("bt_listen_port", "BT_LISTEN_PORT", "int"),
    ("peers_per_torrent", "STREMIO_PEERS_PER_TORRENT", "int"),
    ("torrent_idle_timeout", "STREMIO_TORRENT_IDLE_TIMEOUT", "int"),
    ("bt_encryption", "STREMIO_BT_ENCRYPTION", "string"),
    ("bt_anonymous", "STREMIO_BT_ANONYMOUS", "bool"),
    ("disable_trackers", "STREMIO_DISABLE_TRACKERS", "bool"),
    ("bt_proxy", "STREMIO_BT_PROXY", "string"),
    ("disable_webtorrent", "STREMIO_DISABLE_WEBTORRENT", "bool"),
    ("trackers_max", "STREMIO_TRACKERS_MAX", "int"),
    ("trackers_url", "STREMIO_TRACKERS_URL", "string"),
    ("dht_bootstrap", "STREMIO_DHT_BOOTSTRAP", "string"),
    ("memory_cache_size_mb", "STREMIO_MEMORY_CACHE_SIZE", "mb_to_bytes"),
    ("mem_limit_mb", "STREMIO_MEM_LIMIT", "mb_to_bytes"),
    ("proxy_prebuffer", "STREMIO_PROXY_PREBUFFER", "int"),
    ("proxy_seg_cache_ttl", "STREMIO_PROXY_SEG_CACHE_TTL", "int"),
    ("proxy_password", "STREMIO_PROXY_PASSWORD", "string"),
    ("proxy_ip_acl", "STREMIO_PROXY_IP_ACL", "string"),
    ("proxy_public_url", "STREMIO_PROXY_PUBLIC_URL", "string"),
    ("proxy_upstream", "STREMIO_PROXY_UPSTREAM", "string"),
    ("proxy_secret", "STREMIO_PROXY_SECRET", "string"),
    ("enable_dlna", "STREMIO_ENABLE_DLNA", "bool"),
    ("local_imdb", "STREMIO_LOCAL_IMDB", "bool"),
    ("metadata_url", "STREMIO_METADATA_URL", "string"),
    ("bitmagnet_url", "STREMIO_BITMAGNET_URL", "string"),
    ("torznab_url", "STREMIO_TORZNAB_URL", "string"),
    ("torznab_apikey", "STREMIO_TORZNAB_APIKEY", "string"),
    ("web_ui_location", "WEB_UI_LOCATION", "string"),
    ("https_port", "HTTPS_PORT", "int"),
    ("pprof_addr", "STREMIO_PPROF", "string"),
    ("cert_authkey", "STREMIO_CERT_AUTHKEY", "string"),
)


def extra_env_from_settings(values):
    """Turn a `{kodi_setting_id: raw_value}` dict into the `{env_var: str}`
    overlay `ServerProcess.build_env()` applies, per EXTRA_ENV_SETTINGS's
    `kind` semantics. A `kodi_setting_id` missing from `values` is treated
    as if the setting were absent -- its row is skipped, never a KeyError.
    """
    env = {}
    for setting_id, env_var, kind in EXTRA_ENV_SETTINGS:
        if setting_id not in values:
            continue
        value = values[setting_id]
        if kind == "string":
            if value:
                env[env_var] = str(value)
        elif kind == "int":
            env[env_var] = str(value)
        elif kind == "mb_to_bytes":
            env[env_var] = str(value * 1024 * 1024)
        elif kind == "bool":
            env[env_var] = "true" if value else "false"
    return env


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

    def __init__(self, binary, server_url, app_path, log_path, extra_env=None):
        self.binary = binary
        self.server_url = server_url
        self.app_path = app_path
        self.log_path = log_path
        self.extra_env = extra_env or {}
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
        env.update(self.extra_env)
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
            self.extra_settings = {}
            self.extra_env = {}
            self._refresh()

        def _refresh(self):
            self.enabled = addon.getSettingBool("server_enable")
            self.binary_setting = addon.getSetting("server_binary")
            self.server_url = addon.getSetting("server_url") or DEFAULT_SERVER_URL
            values = {}
            for setting_id, _env_var, kind in EXTRA_ENV_SETTINGS:
                if kind == "bool":
                    values[setting_id] = addon.getSettingBool(setting_id)
                elif kind in ("int", "mb_to_bytes"):
                    values[setting_id] = addon.getSettingInt(setting_id)
                else:
                    values[setting_id] = addon.getSetting(setting_id)
            self.extra_settings = values
            self.extra_env = extra_env_from_settings(values)

        def _snapshot(self):
            return (
                self.enabled, self.binary_setting, self.server_url,
                tuple(sorted(self.extra_settings.items())),
            )

        def onSettingsChanged(self):
            prev = self._snapshot()
            self._refresh()
            if prev != self._snapshot():
                self.restart_requested = True

    monitor = ServiceMonitor()
    proc = None
    backoff_idx = 0
    notified_missing = False
    attempted_download = False

    while not monitor.abortRequested():
        if monitor.restart_requested:
            monitor.restart_requested = False
            if proc is not None:
                log(xbmc.LOGINFO, "settings changed, restarting embedded server")
                proc.stop()
                proc = None
            backoff_idx = 0
            notified_missing = False
            attempted_download = False

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
                    interval = MISSING_BINARY_RECHECK_INTERVAL
                    if not attempted_download:
                        attempted_download = True
                        xbmcgui.Dialog().notification(
                            addon.getAddonInfo("name"),
                            addon.getLocalizedString(30069),
                        )
                        log(xbmc.LOGINFO, "auto-downloading stremio-server binary")
                        from lib import serverbin

                        try:
                            serverbin.install_binary(os.path.join(profile_dir, "bin"))
                            log(xbmc.LOGINFO, "stremio-server binary download complete")
                            interval = POST_DOWNLOAD_RECHECK_INTERVAL
                        except Exception as exc:
                            log(xbmc.LOGERROR, f"stremio-server binary download failed: {exc}")
                            xbmcgui.Dialog().notification(
                                addon.getAddonInfo("name"),
                                addon.getLocalizedString(30063),
                                xbmcgui.NOTIFICATION_ERROR,
                            )
                    elif not notified_missing:
                        xbmcgui.Dialog().notification(
                            addon.getAddonInfo("name"),
                            addon.getLocalizedString(30031),
                            xbmcgui.NOTIFICATION_ERROR,
                        )
                        log(xbmc.LOGERROR, "stremio-server binary not found")
                        notified_missing = True
                else:
                    notified_missing = False
                    log(xbmc.LOGINFO, f"starting embedded server: {binary}")
                    proc = ServerProcess(
                        binary, monitor.server_url, app_path, log_path, extra_env=monitor.extra_env,
                    )
                    proc.start()
                    interval = HEALTHY_POLL_INTERVAL

        if monitor.waitForAbort(interval):
            break

    if proc is not None:
        log(xbmc.LOGINFO, "shutting down embedded server")
        proc.stop()
