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

import datetime
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

from lib import library
from lib import settings as _settings
from lib.store import Store
from lib.stremio.api import StremioAPI

ADDON_ID = "plugin.video.rivulet"
DEFAULT_SERVER_URL = "http://127.0.0.1:11470"
DEFAULT_HTTP_PORT = 11470

BINARY_NAME = "stremio-server"
PROBE_PATHS = ("/settings", "/stats.json")
PROBE_TIMEOUT = 2.0

# Restart backoff schedule for a crashing child: 5s, 10s, 30s, then capped at 30s.
RESTART_BACKOFF = (5, 10, 30)

# Backoff schedule for a failing binary download attempt: retry after
# 30s, 60s, then every 5 minutes -- recovers automatically from a
# transient network/GitHub hiccup without hammering GitHub's API.
DOWNLOAD_RETRY_BACKOFF = (30, 60, 300)

#: Minimum interval between datastorePut pushes for one continuously-
#: playing session -- matches stremio-core's own player model
#: (`PUSH_TO_LIBRARY_EVERY`, src/models/player.rs:47), reused here
#: rather than inventing a different cadence.
LIBRARY_PUSH_INTERVAL_SECONDS = 90
#: Minimum interval between local `progress.json` writes for one
#: continuously-playing session -- far shorter than
#: `LIBRARY_PUSH_INTERVAL_SECONDS` (the remote push is rate-limited
#: separately), but well above `HEALTHY_POLL_INTERVAL` so a long
#: playback does not rewrite the whole cache file on every ~2s poll.
LOCAL_PROGRESS_WRITE_INTERVAL_SECONDS = 15
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

#: settings.xml's <default> for `server_enable` plus every EXTRA_ENV_SETTINGS
#: row of kind "bool"/"int"/"mb_to_bytes" -- the fallback ServiceMonitor._refresh()
#: passes to lib.settings.setting_bool()/setting_int() so an untouched setting
#: resolves to the same value the old getSettingBool()/getSettingInt() calls
#: produced, string-kind rows need no typed default (read raw via getSetting()).
EXTRA_ENV_TYPED_DEFAULTS = {
    "server_enable": True,
    "bt_listen_port": 0,
    "peers_per_torrent": 0,
    "torrent_idle_timeout": 300,
    "bt_anonymous": False,
    "disable_trackers": False,
    "disable_webtorrent": True,
    "trackers_max": 5,
    "memory_cache_size_mb": 0,
    "mem_limit_mb": 0,
    "proxy_prebuffer": 3,
    "proxy_seg_cache_ttl": 300,
    "enable_dlna": False,
    "local_imdb": True,
    "https_port": 12470,
}


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
        try:
            self._log_fh = open(self.log_path, "a", buffering=1)
            self._proc = subprocess.Popen(
                [self.binary],
                env=self.build_env(),
                stdout=self._log_fh,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
            )
        except Exception:
            if self._log_fh is not None:
                self._log_fh.close()
            self._proc = None
            self._log_fh = None
            raise
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
        """Terminate the child, escalating to kill() after `grace` seconds.

        Log-file cleanup always runs, in `finally`, even when the child
        cannot be confirmed dead. A second `TimeoutExpired` after kill()
        propagates instead of being swallowed, and `_proc`/`_started_at`
        are left untouched in that case: `running` keeps reporting True
        so a caller does not spawn a duplicate next to a possibly-still-
        alive, unkillable child -- only confirmed termination discards
        the process/start state."""
        proc = self._proc
        try:
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=grace)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=grace)
            elif proc is not None:
                proc.wait()
        finally:
            if self._log_fh is not None:
                self._log_fh.close()
                self._log_fh = None
        self._proc = None
        self._started_at = None


class _AbortRequested(Exception):
    """Raised by main()'s download-progress callback (passed to
    serverbin.install_binary()) so a multi-minute binary download unwinds
    the instant Kodi requests shutdown, instead of blocking
    monitor.abortRequested() from ever being re-polled until the transfer
    finishes on its own.
    """


def should_push_now(last_pushed_at, now, final, interval=LIBRARY_PUSH_INTERVAL_SECONDS):
    """True if a `datastorePut` push should happen now: always on the
    FINAL sample of a session (the last chance to sync before playback
    ends), the very first sample (`last_pushed_at is None`), or once
    `interval` seconds have passed since the last push. Keeps
    `build_progress_player`'s tracker from hammering the Stremio API on
    every `sample_if_playing()` tick.
    """
    if final or last_pushed_at is None:
        return True
    return (now - last_pushed_at).total_seconds() >= interval


#: Ceiling on how old a persisted now-playing context's `started_at`
#: may be before `_RivuletPlayer` treats it as still actionable. Bounds
#: the window between `lib.ui.player` writing the context (right
#: before handing the resolved stream to Kodi -- see its own docstring)
#: and this SAME player instance accepting it via `onAVStarted`, so a
#: crashed previous session's leftover context can never steer an
#: unrelated LATER video's resume seek or progress sample.
MAX_STARTUP_AGE_SECONDS = 60


def is_context_stale(started_at, now, max_age_seconds=MAX_STARTUP_AGE_SECONDS):
    """True if `started_at` (the ISO 8601 UTC string `library.iso8601_utc()`
    produces) is missing, malformed, or more than `max_age_seconds` older
    than `now` (both `datetime.datetime`). Pure and side-effect-free --
    callers decide what "stale" means for their own now-playing context."""
    if not started_at:
        return True
    try:
        parsed = datetime.datetime.strptime(started_at, "%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return True
    parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return (now - parsed).total_seconds() > max_age_seconds


def _context_key(context):
    """Stable identity for a now-playing context dict -- lets
    `_RivuletPlayer.sample_if_playing` tell whether it is looking at the
    EXACT context `onAVStarted` already accepted for this instance."""
    return (context.get("type"), context.get("id"), context.get("video_id"), context.get("started_at"))


def build_progress_player(xbmc_module, store, api, log_fn, sync_enabled_fn):
    """Return an `xbmc.Player` subclass instance reporting playback
    progress for Rivulet-originated playback only, and performing the
    one-shot resume seek `lib.ui.player` queues via
    `Store.set_resume_offset_ms`.

    `xbmc_module` is the `xbmc` package itself (the real one, or in
    tests the shared `tests/kodistubs` fake) -- taken as a PARAMETER
    rather than imported at this module's top so the whole module stays
    plain-python3 importable (see the module docstring's "pure core"
    split): only `main()` (and this factory, once it is actually
    called) ever needs a real or stubbed `xbmc.Player` to exist.

    Kodi invokes `onAVStarted`/`onPlayBackStopped`/`onPlayBackEnded` on
    every live `Player` instance for ANY playback in the whole Kodi
    session, not just this addon's -- hence every method below first
    checks `store.get_now_playing()` and no-ops when no Rivulet context
    is active. `sample_if_playing()` is NOT a Kodi callback -- `main()`'s
    own poll loop calls it once per iteration, approximating
    "periodically while playing"; `xbmc.Player` has no native
    periodic-tick hook to drive that instead.

    `sync_enabled_fn`/`log_fn` are plain callables (`sync_enabled_fn() ->
    bool`, `log_fn(level, message)`) rather than an `xbmcaddon.Addon`/
    `xbmc.log` reference directly, so this factory itself needs no
    xbmc-specific type beyond the `xbmc_module.Player` base class.
    """

    class _RivuletPlayer(xbmc_module.Player):
        def __init__(self):
            super().__init__()
            self._last_pushed_at = None
            self._last_local_write_at = None
            self._accepted_key = None

        def onAVStarted(self):
            context = store.get_now_playing()
            if context is None:
                return
            now = datetime.datetime.now(datetime.timezone.utc)
            if is_context_stale(context.get("started_at"), now):
                store.set_now_playing(None)
                store.set_resume_offset_ms(None)
                return
            self._accepted_key = _context_key(context)
            self._last_local_write_at = None
            offset_ms = store.get_resume_offset_ms()
            if not offset_ms:
                return
            store.set_resume_offset_ms(None)
            try:
                self.seekTime(offset_ms / library.MS_PER_SECOND)
            except Exception as exc:  # noqa: BLE001 - a seek failure must never crash the service or interrupt playback
                log_fn(xbmc_module.LOGWARNING, "resume seek failed: %r" % (exc,))

        def onPlayBackStopped(self):
            self._terminate()

        def onPlayBackEnded(self):
            self._terminate()

        def onPlayBackError(self):
            # Kodi calls this instead of (not necessarily followed by)
            # onPlayBackStopped/onPlayBackEnded when playback fails
            # outright (dead/expired link, unsupported codec, network
            # drop mid-attempt). Without this, a failed attempt's
            # context/resume offset would stay persisted for the next
            # unrelated video the long-lived Player instance sees start.
            self._terminate()

        def _terminate(self):
            # Shared terminal-callback cleanup: the final flush only
            # happens for a context this SAME instance already accepted
            # via onAVStarted (an error/stop firing before that point
            # must never persist/push a sample of playback that never
            # really started for Rivulet). Clearing BOTH the now-playing
            # context and any queued resume offset stays unconditional.
            context = store.get_now_playing()
            if context is not None and _context_key(context) == self._accepted_key:
                try:
                    self._flush(final=True)
                except Exception as exc:  # noqa: BLE001 - final flush is best-effort; cleanup below is unconditional
                    log_fn(xbmc_module.LOGWARNING, "final flush failed: %r" % (exc,))
            self._accepted_key = None
            self._last_local_write_at = None
            store.set_now_playing(None)
            store.set_resume_offset_ms(None)

        def sample_if_playing(self):
            """Call once per `main()` loop tick -- a no-op unless BOTH a
            Rivulet now-playing context is active AND Kodi is actually
            mid-playback. Only flushes a context this instance already
            accepted via onAVStarted (exact identity match) -- a
            different/not-yet-accepted context is left alone (waiting
            for onAVStarted) unless it is stale, in which case it is
            cleared instead of ever being sampled. One onAVStarted
            already accepted keeps sampling for as long as it keeps
            playing, however old `started_at` gets."""
            context = store.get_now_playing()
            if context is None:
                return
            try:
                playing = self.isPlayingVideo()
            except Exception:  # noqa: BLE001 - isPlayingVideo() must never crash the service loop
                playing = False
            if not playing:
                return
            if _context_key(context) != self._accepted_key:
                now = datetime.datetime.now(datetime.timezone.utc)
                if is_context_stale(context.get("started_at"), now):
                    store.set_now_playing(None)
                    store.set_resume_offset_ms(None)
                return
            self._flush(final=False)

        def _flush(self, final):
            context = store.get_now_playing()
            if context is None:
                return
            try:
                position_ms = int(self.getTime() * library.MS_PER_SECOND)
                duration_ms = int(self.getTotalTime() * library.MS_PER_SECOND)
            except Exception as exc:  # noqa: BLE001 - getTime()/getTotalTime() must never crash the service
                log_fn(xbmc_module.LOGWARNING, "playback sample failed: %r" % (exc,))
                return
            if duration_ms <= 0:
                return
            now = datetime.datetime.now(datetime.timezone.utc)
            if should_push_now(self._last_local_write_at, now, final,
                                interval=LOCAL_PROGRESS_WRITE_INTERVAL_SECONDS):
                self._last_local_write_at = now
                store.set_progress(
                    context["type"], context["id"], context.get("video_id"),
                    position_ms, duration_ms, library.iso8601_utc(),
                )
            if not should_push_now(self._last_pushed_at, now, final):
                return
            self._last_pushed_at = now
            self._push(context, position_ms, duration_ms)

        def _push(self, context, position_ms, duration_ms):
            """Best-effort `datastorePut`: only when logged in AND the
            'sync_progress' setting is on. A failure here is logged and
            swallowed -- `_flush` above has already written the local
            progress cache regardless, so a Stremio API hiccup never
            costs the user their local resume position."""
            if not sync_enabled_fn():
                return
            auth = store.get_auth()
            if not auth or not auth.get("authKey"):
                return
            try:
                existing = api.datastore_get(auth["authKey"], ids=[context["id"]], all=False)
                base = existing[0] if existing else library.build_library_item({
                    "id": context["id"],
                    "type": context["type"],
                    "name": context.get("name", ""),
                    "poster": context.get("poster"),
                })
                merged = library.merge_playback(
                    base, position_ms, duration_ms, video_id=context.get("video_id"),
                )
                api.datastore_put(auth["authKey"], [merged])
            except Exception as exc:  # noqa: BLE001 - a Stremio API hiccup must never interrupt playback or crash the service
                log_fn(xbmc_module.LOGWARNING, "library push failed: %r" % (exc,))

    return _RivuletPlayer()


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

    store = Store(profile_dir)
    progress_player = build_progress_player(
        xbmc, store, StremioAPI(), log, lambda: _settings.setting_bool(addon, "sync_progress", True),
    )

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
            self.enabled = _settings.setting_bool(addon, "server_enable", EXTRA_ENV_TYPED_DEFAULTS["server_enable"])
            self.binary_setting = addon.getSetting("server_binary")
            self.server_url = addon.getSetting("server_url") or DEFAULT_SERVER_URL
            values = {}
            for setting_id, _env_var, kind in EXTRA_ENV_SETTINGS:
                default = EXTRA_ENV_TYPED_DEFAULTS.get(setting_id)
                if kind == "bool":
                    values[setting_id] = _settings.setting_bool(addon, setting_id, default)
                elif kind in ("int", "mb_to_bytes"):
                    values[setting_id] = _settings.setting_int(addon, setting_id, default)
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

    def _stop_process(target):
        """Best-effort `target.stop()`. Returns True once stop()
        completed (state is safe to discard), False if it raised (an
        unkillable/wedged child) -- callers must then keep polling the
        same instance next iteration instead of spawning a duplicate
        next to a possibly-still-alive process."""
        try:
            target.stop()
        except Exception as exc:  # noqa: BLE001 - a failed stop must never crash the supervision loop
            log(xbmc.LOGERROR, f"failed to stop embedded server: {exc}")
            return False
        return True

    monitor = ServiceMonitor()
    proc = None
    backoff_idx = 0
    notified_missing = False
    unsupported_platform = False
    download_backoff_idx = 0
    next_download_at = None
    download_attempt_notified = False
    download_failure_notified = False

    while not monitor.abortRequested():
        try:
            progress_player.sample_if_playing()
        except Exception as exc:  # noqa: BLE001 - playback-progress sampling must never crash the service loop
            log(xbmc.LOGWARNING, "progress sampling failed: %r" % (exc,))

        if monitor.restart_requested:
            monitor.restart_requested = False
            if proc is not None:
                log(xbmc.LOGINFO, "settings changed, restarting embedded server")
                if _stop_process(proc):
                    proc = None
            backoff_idx = 0
            notified_missing = False
            unsupported_platform = False
            download_backoff_idx = 0
            next_download_at = None
            download_attempt_notified = False
            download_failure_notified = False

        interval = IDLE_POLL_INTERVAL

        if not monitor.enabled:
            if proc is not None:
                log(xbmc.LOGINFO, "embedded server disabled, stopping")
                if _stop_process(proc):
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
                if _stop_process(proc):
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
                    if unsupported_platform:
                        # This platform can never execute a downloaded
                        # binary (e.g. Android 10+'s W^X ban on exec()
                        # inside the app's writable storage) -- no retry
                        # would ever succeed, so stay latched and warn
                        # once per session instead of retrying.
                        if not notified_missing:
                            xbmcgui.Dialog().notification(
                                addon.getAddonInfo("name"),
                                addon.getLocalizedString(30031),
                                xbmcgui.NOTIFICATION_ERROR,
                            )
                            log(xbmc.LOGERROR, "stremio-server binary not found")
                            notified_missing = True
                    elif next_download_at is not None and time.monotonic() < next_download_at:
                        # Still cooling down from the last failed attempt --
                        # gated on a monotonic deadline (not just this
                        # iteration's sleep) so no combination of other
                        # branches running in between can retry early.
                        pass
                    else:
                        if not download_attempt_notified:
                            xbmcgui.Dialog().notification(
                                addon.getAddonInfo("name"),
                                addon.getLocalizedString(30069),
                            )
                            download_attempt_notified = True
                        log(xbmc.LOGINFO, "auto-downloading stremio-server binary")
                        from lib import serverbin

                        def _abort_progress(done, total):
                            # install_binary() forwards this to
                            # serverbin._download_to_file(), which calls it
                            # once per chunk -- so a multi-minute download
                            # notices a Kodi shutdown request within one
                            # chunk instead of blocking abortRequested()
                            # from ever being polled again until it finishes
                            # on its own.
                            if monitor.abortRequested():
                                raise _AbortRequested()

                        try:
                            serverbin.install_binary(
                                os.path.join(profile_dir, "bin"), progress_cb=_abort_progress,
                            )
                        except _AbortRequested:
                            # Not a failure -- Kodi is shutting down. No
                            # error notification, and unwind the loop right
                            # away instead of falling through to the
                            # waitForAbort() at the bottom (abort is already
                            # known, waiting on it again just adds latency).
                            log(xbmc.LOGINFO, "stremio-server binary download aborted, shutting down")
                            break
                        except serverbin.UnsupportedPlatformError as exc:
                            unsupported_platform = True
                            next_download_at = None
                            log(xbmc.LOGWARNING, f"stremio-server binary cannot run on this device: {exc}")
                            xbmcgui.Dialog().notification(
                                addon.getAddonInfo("name"),
                                addon.getLocalizedString(30091),
                                xbmcgui.NOTIFICATION_ERROR,
                            )
                        except Exception as exc:
                            # Transient failure (network hiccup, GitHub
                            # outage, no release asset published yet, ...)
                            # -- retry automatically after a bounded
                            # backoff instead of giving up for the
                            # session, but only surface the failure
                            # notification once per cycle so a prolonged
                            # outage does not spam the user.
                            wait_s = DOWNLOAD_RETRY_BACKOFF[
                                min(download_backoff_idx, len(DOWNLOAD_RETRY_BACKOFF) - 1)
                            ]
                            download_backoff_idx = min(download_backoff_idx + 1, len(DOWNLOAD_RETRY_BACKOFF) - 1)
                            next_download_at = time.monotonic() + wait_s
                            log(
                                xbmc.LOGERROR,
                                f"stremio-server binary download failed: {exc}, retrying in {wait_s}s",
                            )
                            if not download_failure_notified:
                                xbmcgui.Dialog().notification(
                                    addon.getAddonInfo("name"),
                                    addon.getLocalizedString(30063),
                                    xbmcgui.NOTIFICATION_ERROR,
                                )
                                download_failure_notified = True
                        else:
                            log(xbmc.LOGINFO, "stremio-server binary download complete")
                            download_backoff_idx = 0
                            next_download_at = None
                            download_attempt_notified = False
                            download_failure_notified = False
                            interval = POST_DOWNLOAD_RECHECK_INTERVAL
                else:
                    notified_missing = False
                    unsupported_platform = False
                    download_backoff_idx = 0
                    next_download_at = None
                    download_attempt_notified = False
                    download_failure_notified = False
                    log(xbmc.LOGINFO, f"starting embedded server: {binary}")
                    candidate = ServerProcess(
                        binary, monitor.server_url, app_path, log_path, extra_env=monitor.extra_env,
                    )
                    try:
                        candidate.start()
                    except Exception as exc:  # noqa: BLE001 - a failed spawn must never crash the supervision loop
                        log(xbmc.LOGERROR, f"failed to start embedded server: {exc}")
                        interval = RESTART_BACKOFF[min(backoff_idx, len(RESTART_BACKOFF) - 1)]
                        backoff_idx = min(backoff_idx + 1, len(RESTART_BACKOFF) - 1)
                    else:
                        proc = candidate
                        interval = HEALTHY_POLL_INTERVAL

        if monitor.waitForAbort(interval):
            break

    if proc is not None:
        log(xbmc.LOGINFO, "shutting down embedded server")
        _stop_process(proc)
