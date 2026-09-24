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
    "version": "1.0.0",
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
    `probe_fn` defaults to `probe_manifest` but is equally injectable, so
    this is unit-testable without `xbmc`, a real `RunScript` call, or a
    real HTTP round-trip.

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

    def apply(self, enabled, port, has_addon_fn, launch_fn, store, probe_fn=probe_manifest):
        active = bool(enabled) and bool(has_addon_fn())
        if not active:
            self._launched_port = None
            self._published = False
            self._next_relaunch_at = 0.0
            store.remove_builtin_addon(BUILTIN_ID)
            return

        if self._launched_port != port:
            self._published = False
            # Transient: a fresh launch (first activation, or a port
            # change) is in flight. Mark any existing descriptor offline
            # rather than removing it, so the user's flags.disabled choice
            # and its position in addons.json survive until it republishes.
            store.set_builtin_addon_offline(BUILTIN_ID)
            self._launch(port, launch_fn)
        elif not probe_fn(port):
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
        else:
            self._published = True

        if self._published:
            store.set_builtin_addon(BUILTIN_ID, manifest_url(port), MANIFEST)

    def _launch(self, port, launch_fn):
        launch_fn(run_script_command(self.addon_path, port))
        self._launched_port = port
        self._published = False
        self._next_relaunch_at = self._clock() + RELAUNCH_BACKOFF_SECONDS
