# SPDX-FileCopyrightText: 2026 M0Rf30
# SPDX-License-Identifier: MIT

"""Kodi-independent glue for the rivulet-engine's opt-in IPTV add-on.

When the engine is started with `RIVULET_IPTV_PLAYLISTS` (the
`iptv_playlists` setting, forwarded by `lib.service_runner`'s
`EXTRA_ENV_SETTINGS`) it serves a Stremio add-on at
`<engine>/iptv/manifest.json`: M3U playlists as `tv` catalogs, with
`stream` answers for each channel. Unset or empty, every `/iptv` route
answers 404, as it does on an engine too old to know the add-on at all.
`RIVULET_IPTV_EPG` (the `iptv_epg` setting: comma-separated XMLTV guide
URLs) adds programme schedules to those channels; the engine then also
declares `behaviorHints.epgProvider`. Both settings feed the published
stamp (`playlists_stamp()`).

This module keeps the matching protected builtin entry of `addons.json`
(`Store.set_builtin_addon()`, the same mechanism `lib.s4me` uses for the
Stream4Me bridge) in step with that: `IptvRegistrar.apply()` runs on the
service loop and publishes the entry only while the engine really answers
with the add-on's manifest, so `Store.get_enabled_addons()` never fans a
request out to a server that is restarting or has no `/iptv`.

Unlike the S4Me bridge nothing here launches a process: the engine is the
one `lib.service_runner.Supervisor` already supervises, so there is no
launch/backoff machinery -- only "does it answer right now".
"""

#: `flags.builtin` of the addons.json entry this module maintains --
#: distinguishes it from every other protected entry (`lib.s4me.BUILTIN_ID`
#: is the other one) without relying on `transportUrl`, which follows the
#: `server_url` setting.
BUILTIN_ID = "rivulet-iptv"

#: The `id` the engine's IPTV manifest carries. Anything else answering at
#: `MANIFEST_PATH` (a reverse proxy's catch-all page, another service on
#: the port) is not this add-on and is never published.
MANIFEST_ID = "community.rivulet-engine.iptv"

#: Path of the add-on's manifest on the engine's HTTP port.
MANIFEST_PATH = "/iptv/manifest.json"

#: One manifest round-trip. Short, because `IptvRegistrar.apply()` runs on
#: the service loop's tick and must never stall it (mirrors
#: `lib.service_runner.PROBE_TIMEOUT`).
PROBE_TIMEOUT_SECONDS = 2.0

#: Same User-Agent every other request of this add-on carries
#: (`lib.serverbin.USER_AGENT`, `lib.stremio.addons.USER_AGENT`); duplicated
#: rather than imported to keep this module free of both import graphs.
USER_AGENT = "plugin.video.rivulet"

# `IptvRegistrar` states: what the store was last told.
_OFF = "off"
_PUBLISHED = "published"
_OFFLINE = "offline"

# Why `fetch_manifest()` returned no manifest (log text, `_UNREACHABLE` aside).
_UNREACHABLE = "unreachable"


def parse_playlists(raw):
    """The playlist URLs in the raw `iptv_playlists` setting: split on
    commas, whitespace trimmed, empty entries dropped. `[]` means off."""
    if not isinstance(raw, str):
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


#: Private manifest field `IptvRegistrar` adds before publishing: a short
#: hash of the playlist URLs the engine was started with. The engine names
#: playlist catalogs by POSITION (`iptv0`, `iptv1`, ...) at a fixed
#: transport, so a changed URL in slot 0 keeps the same (transport, type,
#: id) and every cache keyed on those (`lib.ui.catalogpages`, Home's row
#: signature) would keep serving the old channels. The stamp is what tells
#: them apart. Not part of the Stremio manifest schema; consumers ignore
#: unknown fields.
STAMP_FIELD = "rivuletPlaylists"


def playlists_stamp(raw, epg_raw=""):
    """A short stable hash of the playlist URLs in the raw setting, or `""`
    when it is off. Order matters (it is the catalog numbering), and the
    hash follows `parse_playlists()`, so whitespace and empty entries do
    not change it.

    `epg_raw` is the raw `iptv_epg` setting (XMLTV guide URLs, same comma
    syntax): the guide changes what the add-on serves (`epgProvider`, guide
    catalogs, programmes) under the very same catalog ids, so it belongs in
    the stamp or Home's caches would keep the guide-less channels. With no
    guide the hash is exactly the playlists-only one, so an existing
    install's published stamp does not churn on upgrade. A guide without
    playlists is nothing (the engine has no channels to join it to): `""`.
    """
    import hashlib

    urls = parse_playlists(raw)
    if not urls:
        return ""
    material = "\n".join(urls)
    guides = parse_playlists(epg_raw)
    if guides:
        material += "\nepg\n" + "\n".join(guides)
    return hashlib.sha1(material.encode("utf-8")).hexdigest()[:12]


def manifest_stamp(manifest):
    """The `STAMP_FIELD` of a published manifest, `""` for any other add-on
    (so a non-IPTV add-on's cache keys are unchanged)."""
    value = manifest.get(STAMP_FIELD) if isinstance(manifest, dict) else None
    return value if isinstance(value, str) else ""


def published_stamp(addons):
    """The stamp of the enabled `rivulet-iptv` entry in `addons` (the list
    `Store.get_enabled_addons()` returns, which already leaves out an
    offline or disabled entry), or `None` when there is none."""
    for descriptor in addons or ():
        if isinstance(descriptor, dict) and (descriptor.get("flags") or {}).get("builtin") == BUILTIN_ID:
            return manifest_stamp(descriptor.get("manifest"))
    return None


def manifest_url(server_url):
    """The URL the engine at `server_url` serves the IPTV manifest at."""
    return server_url.rstrip("/") + MANIFEST_PATH


def fetch_manifest(server_url, timeout=PROBE_TIMEOUT_SECONDS):
    """GET the IPTV manifest from the engine at `server_url`.

    Returns `(manifest, reason)`: `(dict, None)` for a JSON object whose
    `id` is `MANIFEST_ID`, else `(None, reason)` with a short log-ready
    reason -- `"unreachable"` (connection refused/timed out: the engine is
    starting, restarting or stopped), `"HTTP 404"` (or any other error
    status: an engine without `/iptv`, or one started without
    `RIVULET_IPTV_PLAYLISTS`) or `"unexpected response"` (something else
    answers there). None of these is an error to the caller: `apply()`
    treats them all as "not available right now".

    `urllib`/`json` are imported here, not at module scope, for the import
    cost `lib.service_runner.probe_listening()` documents: the fetch only
    runs while the setting is on.
    """
    import http.client
    import json
    import urllib.error
    import urllib.request

    try:
        # Inside the try: a free-text server_url without a scheme makes
        # Request() itself raise ValueError.
        request = urllib.request.Request(manifest_url(server_url), headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        # Close the response HTTPError carries: leaking its socket raises
        # ResourceWarning, which the test suite promotes to a failure. On
        # Python 3.8 an HTTPError built without a body raises KeyError here.
        try:
            exc.close()
        except Exception:  # noqa: BLE001 - closing is best-effort cleanup
            pass
        return None, "HTTP %d" % exc.code
    except (urllib.error.URLError, OSError, ValueError, http.client.IncompleteRead):
        return None, _UNREACHABLE

    try:
        manifest = json.loads(body)
    except ValueError:
        return None, "unexpected response"
    if not isinstance(manifest, dict) or manifest.get("id") != MANIFEST_ID:
        return None, "unexpected response"
    return manifest, None


class IptvRegistrar:
    """Keep the `rivulet-iptv` builtin entry of `addons.json` in step with
    the engine, once per service-loop tick.

    State is only what the store was last told (`off`/`published`/
    `offline`, `None` before the first tick), so store writes and log lines
    happen on TRANSITIONS only:

      * `apply()` with no playlists, or with an engine the add-on cannot be
        served from (`serving=False`: embedded server disabled, or an
        external one that never received our environment) ->
        `Store.remove_builtin_addon()`. The permanent case, as for the
        S4Me bridge being switched off.
      * playlists set and the engine answers with the add-on's manifest ->
        `Store.set_builtin_addon(BUILTIN_ID, <manifest url>, <manifest>)`,
        on every such tick: it is a no-op write while the entry already
        matches (`Store.update_addons` writes only a changed list), and it
        is what republishes a manifest that changed (the genre options
        follow the playlists) and repairs an entry something else touched.
        The manifest carries `STAMP_FIELD`, the hash of the playlists, so a
        changed playlist URL is a changed entry even though the engine's
        catalog ids (`iptv0`, ...) stay the same.
      * playlists set but nothing usable answers (engine restarting after a
        settings change, still starting, or an older engine whose `/iptv`
        is a 404) -> `Store.set_builtin_addon_offline()`: the transient
        case, which keeps the user's `flags.disabled` choice and the entry's
        position. The first tick of a session counts as a transition, so an
        entry left published by the previous session is marked offline until
        the engine proves itself again.

    The restart a settings change triggers needs no special handling: the
    engine stops answering, the entry goes offline, and the first tick after
    the new process serves its manifest publishes it again.
    """

    def __init__(self):
        self._state = None

    def apply(self, playlists, server_url, serving, store, fetch_fn=fetch_manifest, log_fn=None, epg=""):
        """`playlists`: the raw setting value; `epg`: the raw `iptv_epg`
        value the engine was started with (part of the published stamp, see
        `playlists_stamp()`). `server_url`: the engine's
        base URL. `serving`: whether the engine this add-on talks to can
        carry our `RIVULET_IPTV_PLAYLISTS` (see this class's docstring).
        `fetch_fn(server_url)` returns `(manifest, reason)` like
        `fetch_manifest()`. `log_fn(message)` receives state changes only.
        """
        if not parse_playlists(playlists) or not serving:
            if self._state != _OFF:
                # State follows a successful write only: a failed one is
                # retried on the next tick.
                store.remove_builtin_addon(BUILTIN_ID)
                self._state = _OFF
            return

        manifest, reason = fetch_fn(server_url)
        if manifest is None:
            if self._state != _OFFLINE:
                store.set_builtin_addon_offline(BUILTIN_ID)
                self._state = _OFFLINE
                # "Unreachable" is the ordinary engine-starting/restarting
                # case, which the supervisor already logs; only an engine
                # that ANSWERS without the add-on is news.
                if reason != _UNREACHABLE and log_fn is not None:
                    log_fn(
                        "the engine has no IPTV add-on at %s (%s); an older engine "
                        "needs updating, or it was not started with the playlists" % (MANIFEST_PATH, reason)
                    )
            return

        if self._state != _PUBLISHED:
            self._state = _PUBLISHED
            if log_fn is not None:
                log_fn("IPTV add-on registered (%s)" % MANIFEST_PATH)
        store.set_builtin_addon(
            BUILTIN_ID, manifest_url(server_url), dict(manifest, **{STAMP_FIELD: playlists_stamp(playlists, epg)})
        )
