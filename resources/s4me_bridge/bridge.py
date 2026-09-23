#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
#
# Copyright (C) 2026 the Rivulet contributors
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
"""Stream4Me (S4Me) -> Stremio protocol bridge.

Launched by `lib.s4me.BridgeSupervisor` (see `lib/service_runner.py`) via
`xbmc.executebuiltin('RunScript(<addon path>/resources/s4me_bridge/bridge.py,<port>)')`,
ONLY when the opt-in `s4me_enable` setting is on AND
`System.HasAddon(plugin.video.s4me)` is true. Serves the Stremio addon
protocol on `127.0.0.1:<port>` for as long as this process's own
`xbmc.Monitor` stays un-aborted (i.e. for the rest of the Kodi session, or
until Kodi shuts down).

`sys.argv[1]` is the port -- the ONLY thing passed as a `RunScript()` arg
(see `lib.s4me.run_script_command`'s docstring for why a second
comma-separated arg would silently fragment). Every other bridge setting
(`s4me_channels`) is read directly from Rivulet's own addon settings via
`xbmcaddon.Addon(ADDON_ID)` -- a `RunScript()`-launched script still runs
inside Kodi's own embedded Python interpreter with full `xbmc*` binding
access, so this needs no IPC of its own.

**This script must NEVER import Rivulet's `lib` package.** Both Rivulet's
addon root and Stream4Me's addon root ship a top-level package literally
named `lib` (Stream4Me's is a vendored third-party library tree -
httplib2, dateutil, babelfish, ... - required by its `core/*` modules'
absolute `from lib import ...` imports). `_install_s4me_path()` below
prepends Stream4Me's root (and its `lib/`) to `sys.path` so THOSE imports
resolve correctly; if this process also ever did `from lib import s4me`
for Rivulet's own module, whichever `lib` sys.path resolves first would
shadow the other silently, with no error - just wrong, hard-to-diagnose
behaviour. The fix used throughout this file: never import anything
named `lib` that belongs to Rivulet. `bridge_helpers` (this directory's
own sibling module, pure logic, needs neither Kodi nor Stream4Me) is
imported by plain sibling-file name instead - see its own docstring.

Every Stream4Me call is wrapped defensively (broad `except Exception`):
an import failure at startup logs and exits immediately (`sys.exit(1)`),
after which `System.HasAddon()` still reports Stream4Me installed but the
store's protected addon entry is never created (`BridgeSupervisor` only
adds it once ITS launch succeeds in staying up - see that class's
docstring), so `lib.ui.addonswindow` simply never lists an addon that
never answered its manifest.
"""
import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# When Kodi's RunScript() invokes this file directly, CPython already put
# its own directory at sys.path[0] - this is belt-and-suspenders for any
# other invocation path (e.g. a manual `python bridge.py` from elsewhere).
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import bridge_helpers as bh  # noqa: E402 - sys.path must be set up first

ADDON_ID = "plugin.video.rivulet"
S4ME_ADDON_ID = "plugin.video.s4me"

#: Fan-out bound for the per-channel search pool -- this addon's own
#: convention (see AGENTS.md: every fan-out call site carries its own
#: local constant, deliberately not shared, since this runs on low-power
#: ARM boxes) applied to Stream4Me's channel count instead of Rivulet's
#: own addon count.
_MAX_CHANNEL_WORKERS = 4
_SEARCH_LANGUAGE = "it"
#: Overall wall-clock budget for one /stream request's whole channel
#: fan-out -- "return whatever resolved in time" rather than block on a
#: slow/dead channel.
_REQUEST_BUDGET_SECONDS = 12.0
_CACHE_TTL_SECONDS = 30 * 60


def _log(message, level_error=False):
    try:
        import xbmc
        xbmc.log("[%s] s4me bridge: %s" % (ADDON_ID, message),
                  xbmc.LOGERROR if level_error else xbmc.LOGINFO)
    except Exception:  # noqa: BLE001 - logging must never crash the bridge
        pass


def _install_s4me_path(s4me_root):
    """Prepend Stream4Me's own root and its vendored `lib/` to
    `sys.path`, root first, so ITS internal `from core import ...`/`from
    lib import ...` absolute imports resolve against its own tree. See
    this module's docstring for why Rivulet's own `lib` package is never
    involved here."""
    for candidate in (s4me_root, os.path.join(s4me_root, "lib")):
        if candidate not in sys.path:
            sys.path.insert(0, candidate)


class _ChannelCatalog:
    """Lazily reads Stream4Me's `channels/<id>.json` `active` flags
    exactly once per process."""

    def __init__(self, s4me_root):
        self._dir = os.path.join(s4me_root, "channels")
        self._active = None

    def active_channel_ids(self):
        if self._active is None:
            ids = []
            try:
                names = sorted(os.listdir(self._dir))
            except OSError:
                names = []
            for name in names:
                if not name.endswith(".json"):
                    continue
                try:
                    with open(os.path.join(self._dir, name), encoding="utf-8") as fh:
                        data = json.load(fh)
                except (OSError, ValueError):
                    continue
                if isinstance(data, dict) and data.get("active") and data.get("id"):
                    ids.append(data["id"])
            self._active = tuple(ids)
        return self._active


def _resolve_title(imdb_id, search_type):
    """Look up `imdb_id` (an IMDb id string) via Stream4Me's TMDB client,
    in Italian. Returns `(title, year, tmdb_id)` or `None` on any
    failure/miss. `search_type` is `"movie"` or `"tv"`."""
    try:
        from core.tmdb import Tmdb
        result = Tmdb(
            external_id=imdb_id, external_source="imdb_id",
            search_type=search_type, search_language=_SEARCH_LANGUAGE,
        )
        tmdb_id = result.result.get("id") if result.result else None
        if not tmdb_id:
            return None
        title = result.result.get("title") or result.result.get("name") or ""
        date = result.result.get("release_date") or result.result.get("first_air_date") or ""
        year = date[:4] if date and len(date) >= 4 else None
        return title, year, str(tmdb_id)
    except Exception as exc:  # noqa: BLE001 - a TMDB lookup failure must never crash the request
        _log("tmdb lookup failed for %s: %r" % (imdb_id, exc), level_error=True)
        return None


def _search_channel(channel_id, title, content_type):
    """Run one Stream4Me channel's own `search()` entry point. Returns its
    raw itemlist (a list of `core.item.Item`), or `[]` on any failure --
    an unavailable/broken channel must never abort the whole request."""
    try:
        from core.item import Item
        module = __import__("channels.%s" % channel_id, None, None, ["channels.%s" % channel_id])
        item = Item(channel=channel_id, global_search=True, contentType=content_type)
        return module.search(item, title) or []
    except Exception as exc:  # noqa: BLE001 - one broken channel must never abort the request
        _log("channel %s search failed: %r" % (channel_id, exc), level_error=True)
        return []


def _episodes_for(channel_id, show_item, season, episode):
    """Call the channel's `episodios()` on the matched show item, and
    return the single episode Item matching `(season, episode)`, or
    `None`."""
    try:
        module = __import__("channels.%s" % channel_id, None, None, ["channels.%s" % channel_id])
        episodes = module.episodios(show_item) or []
        for ep_item in episodes:
            ep_labels = getattr(ep_item, "infoLabels", {}) or {}
            ep_season = str(ep_labels.get("season") or getattr(ep_item, "season", ""))
            ep_episode = str(ep_labels.get("episode") or getattr(ep_item, "episode", ""))
            if ep_season == str(season) and ep_episode == str(episode):
                return ep_item
    except Exception as exc:  # noqa: BLE001 - never abort the request over one channel's listing
        _log("channel %s episodios failed: %r" % (channel_id, exc), level_error=True)
    return None


def _find_videos(channel_id, item):
    """Call the channel's `findvideos()` on a matched movie/episode item.
    Returns the resulting itemlist (server items), or `[]`."""
    try:
        module = __import__("channels.%s" % channel_id, None, None, ["channels.%s" % channel_id])
        return module.findvideos(item) or []
    except Exception as exc:  # noqa: BLE001 - never abort the request over one channel's resolve
        _log("channel %s findvideos failed: %r" % (channel_id, exc), level_error=True)
        return []


def _resolve_streams_for_server_item(channel_id, server_item):
    """Resolve one server item (as produced by a channel's
    `findvideos()`) into zero or more Stremio `stream` objects via
    `core.servertools.resolve_video_urls_for_playing()`."""
    server = getattr(server_item, "server", "") or ""
    raw_url = getattr(server_item, "url", "") or ""
    if not server or not raw_url:
        return []
    url, headers = bh.split_kodi_url(raw_url)
    try:
        from core import servertools
        video_urls, video_exists, _errors = servertools.resolve_video_urls_for_playing(
            server, url, muestra_dialogo=False,
        )
    except Exception as exc:  # noqa: BLE001 - never abort the request over one server's resolve
        _log("resolve failed for server %s: %r" % (server, exc), level_error=True)
        return []
    if not video_exists or not video_urls:
        return []
    quality = getattr(server_item, "quality", None)
    return [
        bh.shape_stream(channel_id, description, resolved_url, headers=headers, quality=quality)
        for description, resolved_url in video_urls
        if resolved_url
    ]


def _streams_for_channel(channel_id, imdb_id, title, year, tmdb_id, season, episode, content_type):
    """The full per-channel pipeline: search -> title/tmdb_id match ->
    (episodios for a series) -> findvideos -> resolved streams. Every
    step already wraps its own S4Me call defensively; this only sequences
    them and applies the match filter."""
    results = _search_channel(channel_id, title, content_type)
    matched = None
    for candidate in results:
        info_labels = dict(getattr(candidate, "infoLabels", {}) or {})
        if bh.result_matches(info_labels, tmdb_id, title, year):
            matched = candidate
            break
    if matched is None:
        return []

    target_item = matched
    if season is not None and episode is not None:
        target_item = _episodes_for(channel_id, matched, season, episode)
        if target_item is None:
            return []

    streams = []
    for server_item in _find_videos(channel_id, target_item):
        streams.extend(_resolve_streams_for_server_item(channel_id, server_item))
    return streams


class _BridgeState:
    """Per-process shared state the HTTP handler reads: settings snapshot,
    the channel catalog, and the response cache. Built once in `main()`
    and handed to every request via a closure, since
    `BaseHTTPRequestHandler` subclasses are instantiated fresh per
    request by `http.server`."""

    def __init__(self, s4me_root):
        self.catalog = _ChannelCatalog(s4me_root)
        self.cache = bh.TTLCache(_CACHE_TTL_SECONDS)
        self.configured_channels = None  # refreshed per-request, see _read_channels_setting

    def channels_for_request(self):
        active = self.catalog.active_channel_ids()
        return bh.select_channels(self._read_channels_setting(), active)

    def _read_channels_setting(self):
        try:
            import xbmcaddon
            raw = xbmcaddon.Addon(ADDON_ID).getSetting("s4me_channels")
        except Exception:  # noqa: BLE001 - a settings-read failure must fall back to "every active channel"
            raw = ""
        return bh.parse_channels_setting(raw)


def _handle_stream_request(state, content_type_param, id_param):
    parsed = bh.parse_stream_id(id_param)
    if parsed is None:
        return {"streams": []}
    imdb_id, season, episode = parsed

    cache_key = (content_type_param, imdb_id, season, episode)
    cached = state.cache.get(cache_key)
    if cached is not None:
        return {"streams": cached}

    search_type = "tv" if content_type_param == "series" else "movie"
    resolved = _resolve_title(imdb_id, search_type)
    if resolved is None:
        return {"streams": []}
    title, year, tmdb_id = resolved

    channels = state.channels_for_request()
    budget = bh.Budget(_REQUEST_BUDGET_SECONDS)
    streams = []
    if channels:
        with ThreadPoolExecutor(max_workers=min(_MAX_CHANNEL_WORKERS, len(channels))) as pool:
            futures = {
                pool.submit(
                    _streams_for_channel, channel_id, imdb_id, title, year, tmdb_id,
                    season, episode, content_type_param,
                ): channel_id
                for channel_id in channels
            }
            for future in as_completed(futures, timeout=budget.remaining() or None):
                try:
                    streams.extend(future.result() or [])
                except Exception as exc:  # noqa: BLE001 - one channel's failure must never drop the others
                    _log("channel %s raised: %r" % (futures[future], exc), level_error=True)
                if budget.expired():
                    break

    state.cache.set(cache_key, streams)
    return {"streams": streams}


def _make_handler(state):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # noqa: A003 - overriding BaseHTTPRequestHandler's own name
            _log(fmt % args)

        def _send_json(self, payload, status=200):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's own naming convention
            path = self.path.split("?", 1)[0]
            parts = [p for p in path.split("/") if p]
            try:
                if path == "/manifest.json":
                    self._send_json(bh.build_manifest())
                    return
                if len(parts) == 3 and parts[0] == "stream" and parts[2].endswith(".json"):
                    content_type_param = parts[1]
                    id_param = parts[2][: -len(".json")]
                    self._send_json(_handle_stream_request(state, content_type_param, id_param))
                    return
                self._send_json({"error": "not found"}, status=404)
            except Exception as exc:  # noqa: BLE001 - a handler crash must never take the whole server down
                _log("request handler failed for %s: %r" % (self.path, exc), level_error=True)
                self._send_json({"error": "internal error"}, status=500)

    return Handler


def main():
    if len(sys.argv) < 2:
        _log("missing port argument, exiting", level_error=True)
        sys.exit(1)
    try:
        port = int(sys.argv[1])
    except ValueError:
        _log("invalid port argument %r, exiting" % (sys.argv[1],), level_error=True)
        sys.exit(1)

    try:
        import xbmc
        import xbmcaddon
    except Exception as exc:  # noqa: BLE001 - this script only ever runs inside Kodi
        _log("xbmc/xbmcaddon unavailable, exiting: %r" % (exc,), level_error=True)
        sys.exit(1)

    try:
        s4me_root = xbmcaddon.Addon(S4ME_ADDON_ID).getAddonInfo("path")
    except Exception as exc:  # noqa: BLE001 - Stream4Me must be installed for this script to be launched at all
        _log("Stream4Me addon not found, exiting: %r" % (exc,), level_error=True)
        sys.exit(1)

    _install_s4me_path(s4me_root)
    try:
        import core  # noqa: F401 - proves Stream4Me's tree actually imports before serving anything
    except Exception as exc:  # noqa: BLE001 - a broken Stream4Me install must exit cleanly, never half-serve
        _log("failed to import Stream4Me's core package, exiting: %r" % (exc,), level_error=True)
        sys.exit(1)

    state = _BridgeState(s4me_root)
    server = ThreadingHTTPServer(("127.0.0.1", port), _make_handler(state))
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    _log("serving on 127.0.0.1:%d" % port)

    monitor = xbmc.Monitor()
    try:
        while not monitor.abortRequested():
            if monitor.waitForAbort(1.0):
                break
    finally:
        _log("shutting down")
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
