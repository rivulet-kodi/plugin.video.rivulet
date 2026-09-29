"""Kodi-independent glue for the optional Stream4Me (S4Me) bridge addon.

Stream4Me (`plugin.video.s4me`, GPL-3, https://github.com/monkeynator/s4me
-- see `resources/s4me_bridge/bridge.py` for the actual runtime bridge) is a
community Kodi addon bundling dozens of Italian-language scraper "channels".
This module holds ONLY the small, pure, Kodi-independent pieces `lib.store`
and `lib.service_runner` need to treat it as an opt-in, protected, built-in
Stremio addon:

  - the manifest/descriptor shape the store's `addons.json` should carry
    while the bridge is enabled and Stream4Me is installed;
  - `RunScript()` command construction for launching the bridge script;
  - `BridgeSupervisor`, a tiny state machine deciding whether to (re)launch
    the bridge and whether the store descriptor should exist this tick.

The actual protocol server (HTTP handler, TMDB/channel search, stream
shaping) lives entirely in `resources/s4me_bridge/`, which runs in a
separate `RunScript()` invocation and must NEVER import this addon's `lib`
package (see that directory's `bridge.py` module docstring for why) -- so
its own pure-logic helpers are deliberately duplicated in
`resources/s4me_bridge/bridge_helpers.py` rather than imported from here.
"""

import os
import time

#: Kodi addon id of the community Stream4Me addon this bridge wraps.
S4ME_ADDON_ID = "plugin.video.s4me"

#: Marks the protected addon-store descriptor this bridge maintains, via
#: `flags.builtin` -- distinguishes it from every other protected/official
#: entry (and from any future second built-in bridge) without relying on
#: `transportUrl`, which changes whenever the `s4me_port` setting does.
BUILTIN_ID = "s4me"

#: settings.xml's `<default>` for `s4me_port`.
DEFAULT_PORT = 11480

#: Path (relative to the addon root) of the script `RunScript()` launches.
BRIDGE_SCRIPT_RELPATH = os.path.join("resources", "s4me_bridge", "bridge.py")

#: The Stremio addon manifest this bridge serves at `/manifest.json`. Kept
#: in sync BY HAND with `resources/s4me_bridge/bridge_helpers.py`'s own
#: identical copy -- that module cannot import this one (see this module's
#: docstring), so `tests/test_s4me.py` and
#: `tests/test_s4me_bridge_helpers.py` both assert against this exact
#: dict, which pins the two copies together across any future edit.
MANIFEST = {
    "id": "org.rivulet.s4me",
    "name": "Stream4Me",
    "version": "1.1.0",
    "description": (
        "Bridges the Stream4Me (S4Me) Kodi addon's Italian channel "
        "scrapers into the Stremio protocol."
    ),
    "resources": ["stream"],
    "types": ["movie", "series"],
    "idPrefixes": ["tt"],
    "catalogs": [],
}

#: Values that mark a language setting as Italian. Kodi reports the
#: interface language as an ISO 639-1 code via `xbmc.getLanguage()`, but
#: stores `locale.audiolanguage` as the language's English name (or a
#: code, for users who typed one), so both spellings are accepted.
_ITALIAN_VALUES = frozenset({"it", "ita", "italian", "italiano"})


def is_italian_user(ui_language, audio_language=None):
    """Whether this Kodi install belongs to an Italian-speaking user.

    Stream4Me's channels are Italian-language sites, so the bridge is
    only offered to Italian users. Kodi has no country setting, so this
    reads the user's language choices instead: the interface language
    first, then the preferred audio language as a fallback for people
    who run Kodi in English but watch in Italian. Either being Italian is
    enough; anything missing or unrecognised counts as not Italian."""
    for value in (ui_language, audio_language):
        if isinstance(value, str) and value.strip().lower() in _ITALIAN_VALUES:
            return True
    return False


def bridge_script_path(addon_path):
    """Absolute path to `bridge.py`, given Rivulet's own addon root path
    (`xbmcaddon.Addon().getAddonInfo("path")`)."""
    return os.path.join(addon_path, BRIDGE_SCRIPT_RELPATH)


def manifest_url(port):
    """The local URL the bridge serves its manifest at for `port`."""
    return "http://127.0.0.1:%d/manifest.json" % port


def shutdown_url(port):
    """The local URL `shutdown_bridge()` POSTs to for `port` -- see
    `resources/s4me_bridge/bridge.py`'s `/shutdown` route."""
    return "http://127.0.0.1:%d/shutdown" % port


#: Timeout for one `probe_manifest()` HTTP round-trip -- short because this
#: runs on the same settings-refresh cadence as the rest of `main()`'s
#: supervision tick and must never stall it (mirrors
#: `lib.service_runner.PROBE_TIMEOUT`).
PROBE_TIMEOUT_SECONDS = 2.0

#: Minimum gap between two launch attempts once a launch is judged to have
#: failed (readiness probe still failing) -- stops a persistently broken
#: Stream4Me install from spawning a fresh `RunScript()` on every
#: settings-poll tick.
RELAUNCH_BACKOFF_SECONDS = 30.0

#: Delay before the FIRST relaunch attempt after a STALE bridge is
#: detected (see `probe_manifest_version()`/`BridgeSupervisor.apply()`)
#: -- deliberately much shorter than `RELAUNCH_BACKOFF_SECONDS`: an old
#: bridge process surviving a Rivulet update is already being told to
#: exit via `shutdown_bridge()`, and only needs on the order of a second
#: to actually close its listening socket, not a full 30s. If the port
#: is STILL held by an old process on the next tick (the shutdown
#: request was ignored, or a second old process is somehow still
#: around), `apply()` falls back to `RELAUNCH_BACKOFF_SECONDS` instead of
#: retrying this fast forever.
STALE_RELAUNCH_DELAY_SECONDS = 2.0


def probe_manifest(port, timeout=PROBE_TIMEOUT_SECONDS):
    """True if `/manifest.json` answers at `127.0.0.1:port` -- confirms the
    `RunScript()` bridge actually imported Stream4Me and bound its socket,
    rather than trusting `xbmc.executebuiltin()`'s fire-and-forget return
    (see `BridgeSupervisor.apply()`).

    Any completed HTTP exchange, including an HTTP error status, counts as
    "answering" -- same convention as
    `lib.service_runner.probe_listening()`, whose docstring explains why.

    `urllib.request`/`urllib.error` are imported here, not at module scope,
    for the same import-cost reason as `probe_listening()`: this module is
    imported on every settings refresh, but the probe itself only runs
    while the bridge is enabled and not yet confirmed ready.
    """
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(manifest_url(port), timeout=timeout):
            pass
        return True
    except urllib.error.HTTPError:
        return True
    except (urllib.error.URLError, OSError, ValueError):
        return False


def probe_manifest_version(port, timeout=PROBE_TIMEOUT_SECONDS):
    """Return the `version` the bridge answering at `127.0.0.1:port`
    reports in its `/manifest.json`, distinguishing three outcomes
    `BridgeSupervisor.apply()` needs told apart:

      * a `str` -- the manifest's own `version` field, verbatim. Compared
        against `MANIFEST["version"]` by the caller to detect a STALE
        bridge: a leftover process from before a Rivulet update, still
        holding the port and answering, but serving the previous
        build's manifest. A running bridge whose served version differs
        from this module's `MANIFEST["version"]` can never be the one
        `RunScript()` would launch now, so it must be shut down and
        replaced rather than trusted.
      * `""` -- the bridge answered (even with an HTTP error status,
        same convention as `probe_manifest()`) but the body was not
        valid JSON, was not a JSON object, or had no string `version`
        field -- treated exactly like a version mismatch by `apply()`,
        since a well-formed current bridge always serves one.
      * `None` -- nothing answered at all (connection refused, timed
        out, DNS/OS-level failure) -- the ordinary "not launched yet, or
        crashed" case, handled the same as `probe_manifest()` returning
        `False`.

    `urllib.request`/`urllib.error`/`json` are imported here, not at
    module scope, for the same import-cost reason as `probe_manifest()`.
    """
    import json
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(manifest_url(port), timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read()
    except (urllib.error.URLError, OSError, ValueError):
        return None

    try:
        manifest = json.loads(body)
    except ValueError:
        return ""
    version = manifest.get("version") if isinstance(manifest, dict) else None
    return version if isinstance(version, str) else ""


def shutdown_bridge(port, timeout=PROBE_TIMEOUT_SECONDS):
    """Best-effort `POST /shutdown` to the bridge at `127.0.0.1:port`,
    asking it to stop serving and exit its `RunScript()` cleanly --
    `BridgeSupervisor.apply()`'s way of evicting a STALE bridge (see
    `probe_manifest_version()`) before relaunching a fresh one on the
    same port, since a bare `RunScript()` would just fail to bind while
    the old process still holds it.

    Every failure is swallowed: an old bridge build that predates the
    `/shutdown` route answers 404 (`HTTPError`, caught same as
    `probe_manifest()`'s "answering" case elsewhere -- here it just means
    "nothing to wait for, `apply()`'s short relaunch delay covers Kodi's
    own zombie-reap time instead"); one already gone by the time this
    fires refuses the connection. Callers must never let this raise --
    `BridgeSupervisor.apply()` wraps its `shutdown_fn` call in `try`
    anyway, but a fixed contract here (always returns `None`, never
    raises) keeps that belt-and-braces rather than load-bearing.

    `urllib.request`/`urllib.error` imported here for the same
    import-cost reason as `probe_manifest()`.
    """
    import urllib.error
    import urllib.request

    request = urllib.request.Request(shutdown_url(port), data=b"", method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout):
            pass
    except (urllib.error.URLError, OSError, ValueError):
        pass


def run_script_command(addon_path, port):
    """The exact `RunScript(...)` builtin `main()` passes to
    `xbmc.executebuiltin()`.

    A single positional arg (`port`) ONLY -- never add a second
    comma-separated argument here: `RunScript()` splits its whole
    parameter list on `,`, so a comma-separated value (e.g. the
    `s4me_channels` setting) would silently fragment into extra
    positional args. `resources/s4me_bridge/bridge.py` instead reads every
    other bridge setting itself via
    `xbmcaddon.Addon('plugin.video.rivulet')`, sidestepping that limit
    entirely.
    """
    return "RunScript(%s,%s)" % (bridge_script_path(addon_path), port)


def descriptor_for(port):
    """The addon-store descriptor `Store.set_builtin_addon()` should hold
    while the bridge is enabled and Stream4Me is installed."""
    return {
        "transportUrl": manifest_url(port),
        "manifest": MANIFEST,
        "flags": {"official": False, "protected": True, "builtin": BUILTIN_ID},
    }


def parse_channels_setting(raw):
    """Parse the `s4me_channels` comma-list setting into a tuple of
    stripped, non-empty channel ids, or `None` when blank.

    `None` is the sentinel the bridge reads as "use every one of
    Stream4Me's own active channels" rather than a fixed allow-list.
    """
    if not raw or not raw.strip():
        return None
    return tuple(c.strip() for c in raw.split(",") if c.strip())


class BridgeSupervisor:
    """Decide, once per settings refresh, whether the S4Me bridge script
    should be launched and whether the store's protected builtin addon
    entry should exist.

    Kept entirely Kodi-free: `has_addon_fn`/`launch_fn` are injected
    callables (`main()` passes
    `lambda: xbmc.getCondVisibility(...)`/`xbmc.executebuiltin`), and
    `probe_fn`/`shutdown_fn` default to `probe_manifest_version`/
    `shutdown_bridge` but are equally injectable, so this is
    unit-testable without `xbmc`, a real `RunScript` call, or a real HTTP
    round-trip.

    `xbmc.executebuiltin()` returns as soon as `RunScript()` is queued --
    long before the script has imported Stream4Me and bound its port (or
    failed to do either). Publishing the store descriptor on that return
    alone would let `Store.get_enabled_addons()` fan a request out to a
    port nothing is listening on yet, or ever, on a failed launch. So
    `apply()` only calls `Store.set_builtin_addon()` once `probe_fn(port)`
    confirms `/manifest.json` actually answers; marks the descriptor
    offline (`Store.set_builtin_addon_offline()`) the moment a
    previously-answering bridge stops answering, or while a fresh launch
    (first activation or a port change) is in flight; and relaunches --
    no more often than `RELAUNCH_BACKOFF_SECONDS` apart -- while a launch
    has not yet produced a working bridge. Marking offline, rather than
    removing the descriptor outright, is what a TRANSIENT unavailability
    calls for: it keeps the user's `flags.disabled` choice and the
    entry's position in `addons.json` intact for whenever it comes back;
    `remove_builtin_addon()` is reserved for the permanent case below.

    A running bridge that ANSWERS but reports a `version` other than
    this module's `MANIFEST["version"]` (including no parseable version
    at all -- see `probe_manifest_version()`) is STALE: almost always an
    old build's process that survived a Rivulet addon update and is
    still holding the port, which would otherwise make `apply()` wrongly
    conclude everything is healthy while a bare `RunScript()` for the
    new build silently fails to bind. `apply()` treats this exactly like
    "stopped answering" for the store (marks it offline immediately, see
    above) but ALSO POSTs `/shutdown` (`shutdown_fn`) to the stale
    process before relaunching, and uses a much shorter
    `STALE_RELAUNCH_DELAY_SECONDS` for that first relaunch attempt
    instead of the full `RELAUNCH_BACKOFF_SECONDS` -- the old process
    only needs on the order of a second to release its socket once
    asked. If the port is STILL held by a stale bridge on the very next
    attempt (the shutdown was ignored, or something keeps relaunching an
    old build), `apply()` falls back to the normal
    `RELAUNCH_BACKOFF_SECONDS` cadence and logs the situation exactly
    once via `log_fn` -- never once per tick -- so a persistently stuck
    port is visible without flooding the Kodi log.

    `apply()` is idempotent per port: readiness/backoff state is tracked
    per launched port, so repeated calls that keep finding the bridge
    healthy neither relaunch nor spam `launch_fn`, but do keep re-syncing
    the store descriptor once published -- cheap (a no-op write once it
    already matches, see `Store.set_builtin_addon`), and self-healing if
    something else touched `addons.json`.

    A `port` change while already active DOES re-launch (a fresh
    `RunScript()` bound to the new port), marking any descriptor for the
    old port offline immediately -- the store entry only repoints at the
    new port once THAT launch answers, avoiding a window where the
    descriptor names a port nothing yet serves. The previous bridge
    process, if still running, is simply left listening on its old port
    with nothing pointing at it anymore until Kodi restarts -- Kodi's
    `RunScript()` builtin hands back no handle to stop it, so this (and
    leaving it running when the bridge is disabled) is an accepted,
    low-cost trade-off for an opt-in feature, avoided entirely by leaving
    `s4me_port` alone.
    """

    def __init__(self, addon_path, clock=time.monotonic):
        self.addon_path = addon_path
        self._clock = clock
        self._launched_port = None
        self._published = False
        self._next_relaunch_at = 0.0
        #: How many consecutive STALE-triggered relaunches have happened
        #: for the current `_launched_port` without ever seeing a
        #: matching version answer in between -- 0 means "not currently
        #: chasing a stale bridge". Drives the short-delay-then-backoff
        #: escalation and the log-once guard in `apply()`.
        self._stale_relaunches = 0
        self._stale_logged = False
        #: Separate gate timer for the stale-bridge relaunch cadence
        #: (short delay first, then `RELAUNCH_BACKOFF_SECONDS`) -- kept
        #: apart from `_next_relaunch_at` (which `_launch()` always sets
        #: to the full backoff) so a stale sighting reacts on its OWN
        #: schedule instead of inheriting whatever gate the fresh launch
        #: that preceded it happened to set.
        self._next_stale_retry_at = 0.0

    def apply(
        self,
        enabled,
        port,
        has_addon_fn,
        launch_fn,
        store,
        probe_fn=probe_manifest_version,
        shutdown_fn=shutdown_bridge,
        log_fn=None,
    ):
        active = bool(enabled) and bool(has_addon_fn())
        if not active:
            self._launched_port = None
            self._published = False
            self._next_relaunch_at = 0.0
            self._stale_relaunches = 0
            self._stale_logged = False
            self._next_stale_retry_at = 0.0
            store.remove_builtin_addon(BUILTIN_ID)
            return

        if self._launched_port != port:
            self._published = False
            self._stale_relaunches = 0
            self._stale_logged = False
            self._next_stale_retry_at = 0.0
            # Transient: a fresh launch (first activation, or a port
            # change) is in flight. Mark any existing descriptor offline
            # rather than removing it, so the user's flags.disabled choice
            # and its position in addons.json survive until it republishes.
            store.set_builtin_addon_offline(BUILTIN_ID)
            self._launch(port, launch_fn)
            return

        answer = probe_fn(port)
        if answer is not None and answer != MANIFEST["version"]:
            # Stale: something answers, but not the build this Rivulet
            # install would launch now -- most likely a leftover process
            # from before an update. Evict it before relaunching, rather
            # than trusting a bare RunScript() to just take over the port.
            if self._published:
                self._published = False
                store.set_builtin_addon_offline(BUILTIN_ID)
            if self._clock() >= self._next_stale_retry_at:
                try:
                    shutdown_fn(port)
                except Exception:  # noqa: BLE001 - a bridge with no /shutdown route (old build) or already gone must never block relaunch
                    pass
                self._stale_relaunches += 1
                if self._stale_relaunches > 1:
                    if not self._stale_logged and log_fn is not None:
                        log_fn(
                            "s4me bridge on port %d is still stale after a shutdown+relaunch attempt "
                            "(served version != %s); backing off %.0fs between further attempts"
                            % (port, MANIFEST["version"], RELAUNCH_BACKOFF_SECONDS)
                        )
                        self._stale_logged = True
                    delay = RELAUNCH_BACKOFF_SECONDS
                else:
                    delay = STALE_RELAUNCH_DELAY_SECONDS
                self._next_stale_retry_at = self._clock() + delay
                # Launch on the NEXT tick, once the port stops answering
                # (the `answer is None` branch below, whose backoff this
                # clears). Launching right after the shutdown POST raced
                # the old bridge for the port: observed live, the new
                # script failed to bind while the old one was still
                # winding down, and the 30s relaunch backoff then left
                # Stream4Me unavailable for half a minute after an update.
                self._next_relaunch_at = self._clock()
            return

        if answer is None:
            if self._published:
                # It answered before but has stopped -- mark it offline
                # right away so get_enabled_addons() never fans a request
                # out to a dead port. The backoff check below decides
                # whether it is also time to try relaunching it.
                self._published = False
                store.set_builtin_addon_offline(BUILTIN_ID)
            if self._clock() >= self._next_relaunch_at:
                self._launch(port, launch_fn)
            return

        self._published = True
        self._stale_relaunches = 0
        self._stale_logged = False
        self._next_stale_retry_at = 0.0

        if self._published:
            store.set_builtin_addon(BUILTIN_ID, manifest_url(port), MANIFEST)

    def launched_port(self):
        """The port the bridge was last launched on, or `None` when this
        supervisor has not launched one (disabled, or Stream4Me absent).
        `lib.service_runner.main()` uses it on Kodi shutdown to POST
        /shutdown to the bridge -- see the comment there for why."""
        return self._launched_port

    def _launch(self, port, launch_fn, delay=RELAUNCH_BACKOFF_SECONDS):
        launch_fn(run_script_command(self.addon_path, port))
        self._launched_port = port
        self._published = False
        self._next_relaunch_at = self._clock() + delay
