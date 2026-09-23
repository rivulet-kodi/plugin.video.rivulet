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


def bridge_script_path(addon_path):
    """Absolute path to `bridge.py`, given Rivulet's own addon root path
    (`xbmcaddon.Addon().getAddonInfo("path")`)."""
    return os.path.join(addon_path, BRIDGE_SCRIPT_RELPATH)


def manifest_url(port):
    """The local URL the bridge serves its manifest at for `port`."""
    return "http://127.0.0.1:%d/manifest.json" % port


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
    `lambda: xbmc.getCondVisibility(...)`/`xbmc.executebuiltin`), so this
    is unit-testable without `xbmc` or a real `RunScript` call.

    `apply()` is idempotent per port: repeated calls with the same
    `(active, port)` outcome launch the script at most once (Kodi has no
    "is this RunScript still running" query, so this guards against
    spawning a second bridge process on every settings poll) but always
    re-syncs the store descriptor -- cheap (a no-op write once it already
    matches, see `Store.set_builtin_addon`), and self-healing if something
    else touched `addons.json`.

    A `port` change while already active DOES re-launch (a fresh
    `RunScript()` bound to the new port) and repoints the store entry at
    it immediately; the previous bridge process, if still running, is
    simply left listening on its old port with nothing pointing at it
    anymore until Kodi restarts -- an accepted, low-cost trade-off for an
    opt-in feature, avoided entirely by leaving `s4me_port` alone.
    """

    def __init__(self, addon_path):
        self.addon_path = addon_path
        self._launched_port = None

    def apply(self, enabled, port, has_addon_fn, launch_fn, store):
        active = bool(enabled) and bool(has_addon_fn())
        if active:
            store.set_builtin_addon(BUILTIN_ID, manifest_url(port), MANIFEST)
            if self._launched_port != port:
                launch_fn(run_script_command(self.addon_path, port))
                self._launched_port = port
        else:
            self._launched_port = None
            store.remove_builtin_addon(BUILTIN_ID)
