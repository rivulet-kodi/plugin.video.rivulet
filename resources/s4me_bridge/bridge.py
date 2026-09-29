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
until Kodi shuts down) OR until a `POST /shutdown` request arrives (see
`_shutdown_cleanup()` -- the ONE cleanup path both exits share).

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
from concurrent.futures import ThreadPoolExecutor
from functools import partial
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
#: Six, not four: after `bh.channels_for_type()` filtering a request fans
#: out to about nine searchable channels, so six workers finish in two
#: rounds inside `_REQUEST_BUDGET_SECONDS` where four needed three. The
#: work is network-bound (threads mostly wait on sockets), so this stays
#: cheap on the low-power ARM boxes this runs on.
_MAX_CHANNEL_WORKERS = 6
#: How long a timed-out fan-out may keep running in the background to
#: finish and cache its complete result (see `_cache_late()`).
_LATE_COMPLETION_SECONDS = 60.0
_SEARCH_LANGUAGE = "it"
#: Overall wall-clock budget for one /stream request, counted from its
#: arrival (the TMDB title lookup included) -- "return whatever resolved
#: in time" rather than block on a slow/dead channel. It must fit inside
#: `lib.stremio.addons.AddonClient`'s 15s request timeout with room for
#: the response itself: counted only from the fan-out's start, as it once
#: was, a first request measured 13.4s end to end on a desktop, so on a
#: slower box Rivulet gave up before the bridge answered.
_REQUEST_BUDGET_SECONDS = 11.0
#: Blocking-I/O ceiling applied to Stream4Me's own HTTP layer (see
#: `_bound_channel_io_timeout()`) -- bounds every `core.httptools
#: .downloadpage()` call a channel's search/findvideos/resolve makes
#: that does not set its own explicit `timeout=`, so a stalled channel's
#: worker thread eventually raises instead of hanging past the request
#: budget above (and past process exit, since Python 3.8's ThreadPoolExecutor
#: cannot cancel a running worker -- see `_handle_stream_request()`).
#: Comfortably under `_REQUEST_BUDGET_SECONDS` so one slow channel unblocks
#: with the budget still open for the others' results to be collected.
#:
#: Deliberately NOT `socket.setdefaulttimeout()`: that is process/
#: interpreter-global, so it would also reset the timeout on THIS
#: server's own accepted HTTP connections, and on CPython builds whose
#: socket module default-timeout state is not per-subinterpreter, it
#: could leak into Kodi's OTHER addon subinterpreters entirely
#: unrelated to this bridge. Scoping it to Stream4Me's own httptools
#: module attribute affects only the HTTP calls this bridge's own
#: channel fan-out makes, in this process alone.
_CHANNEL_IO_TIMEOUT_SECONDS = 8.0
_CACHE_TTL_SECONDS = 30 * 60

#: A channel whose pipeline raises `_CHANNEL_FAILURE_THRESHOLD` times in
#: a row is skipped for `_CHANNEL_COOLDOWN_SECONDS` -- see
#: `bh.ChannelBackoff`'s own docstring and `_channel_task()` below for the
#: success/failure boundary.
_CHANNEL_FAILURE_THRESHOLD = 3
_CHANNEL_COOLDOWN_SECONDS = 10 * 60

#: How often the main loop wakes to check `_SHUTDOWN_EVENT` between
#: `xbmc.Monitor.waitForAbort()` polls -- short enough that a `POST
#: /shutdown` response (already sent before this is even noticed) is
#: followed by an actual process exit within a fraction of a second,
#: not the ~1s a coarser poll would risk added on top of Kodi's own
#: "script didn't stop in 5 seconds" patience.
_MONITOR_POLL_SECONDS = 0.5


def _log(message, level_error=False, level_debug=False):
    try:
        import xbmc
        if level_error:
            level = xbmc.LOGERROR
        elif level_debug:
            level = xbmc.LOGDEBUG
        else:
            level = xbmc.LOGINFO
        xbmc.log("[%s] s4me bridge: %s" % (ADDON_ID, message), level)
    except Exception:  # noqa: BLE001 - logging must never crash the bridge
        pass


def _bound_channel_io_timeout():
    """Lower Stream4Me's own `core.httptools.downloadpage()` default
    timeout to `_CHANNEL_IO_TIMEOUT_SECONDS`, scoped to this process's
    already-imported copy of Stream4Me's `core.httptools` module --
    see that constant's own comment for why this, and not
    `socket.setdefaulttimeout()`, is the right lever. `downloadpage()`
    (used by essentially every channel/server module's own HTTP calls,
    per Stream4Me's own convention) only falls back to this
    module-level default when a caller does not pass its own explicit
    `timeout=` kwarg, so an occasional channel that already sets one
    keeps that value untouched. Best-effort: swallows any failure
    (missing/renamed attribute in a Stream4Me fork, for example) since a
    slightly-later channel timeout is far better than refusing to serve
    requests at all.
    """
    try:
        from core import httptools
        httptools.HTTPTOOLS_DEFAULT_DOWNLOAD_TIMEOUT = _CHANNEL_IO_TIMEOUT_SECONDS
    except Exception as exc:  # noqa: BLE001 - a best-effort timeout tweak must never block startup
        _log("could not bound Stream4Me's httptools timeout: %r" % (exc,), level_error=True)


def _log_s4me_version(s4me_root):
    """Log Stream4Me's own addon version and, if readable, the local git
    commit its updater last synced to (`platformcode.updater`'s own
    `trackingFile`, a `last_commit.txt` at its addon root) -- one line of
    startup context a bug report against a specific S4Me install/fork
    state otherwise has no way to recover, since nothing else this bridge
    logs identifies which S4Me code it is actually running against.
    Best-effort throughout: neither read is allowed to block startup."""
    version = "unknown"
    try:
        import xbmcaddon
        version = xbmcaddon.Addon(S4ME_ADDON_ID).getAddonInfo("version") or "unknown"
    except Exception as exc:  # noqa: BLE001 - a version-read failure must never block startup
        _log("could not read Stream4Me's addon version: %r" % (exc,), level_error=True)
    commit = None
    try:
        with open(os.path.join(s4me_root, "last_commit.txt"), encoding="utf-8") as fh:
            commit = fh.read().strip()
    except OSError:
        commit = None
    _log("Stream4Me version %s (commit %s)" % (version, commit or "unknown"))


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
    """Lazily reads Stream4Me's `channels/<id>.json` files exactly once per
    process: which channels are `active`, which content `categories` each
    declares, and whether its module defines a `search()` entry point."""

    def __init__(self, s4me_root):
        self._dir = os.path.join(s4me_root, "channels")
        self._channels = None

    def _load(self):
        if self._channels is None:
            channels = []
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
                if not (isinstance(data, dict) and data.get("active") and data.get("id")):
                    continue
                channels.append((
                    data["id"],
                    tuple(data.get("categories") or ()),
                    self._defines_search(name[:-len(".json")]),
                ))
            self._channels = tuple(channels)
        return self._channels

    def _defines_search(self, module_name):
        # Read the source instead of importing it: importing every channel
        # up front would run their module-level code (some fetch their
        # host) for channels this request may never touch.
        try:
            with open(os.path.join(self._dir, module_name + ".py"), encoding="utf-8") as fh:
                return bh.defines_search(fh.read())
        except OSError:
            return False

    def active_channel_ids(self, content_type=None):
        """Active channels, restricted to those that can search for
        `content_type` (Stremio `movie`/`series`) when one is given."""
        channels = self._load()
        if content_type is None:
            return tuple(cid for cid, _cats, _search in channels)
        return bh.channels_for_type(channels, content_type)


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
    raw itemlist (a list of `core.item.Item`, possibly empty -- a
    legitimate "no match on this channel"), or `None` if the call itself
    raised: a real breakage `_channel_task()` counts against
    `bh.ChannelBackoff`, unlike an empty result."""
    try:
        from core.item import Item
        module = __import__("channels.%s" % channel_id, None, None, ["channels.%s" % channel_id])
        item = Item(
            channel=channel_id, global_search=True,
            contentType=bh.content_type_for_s4me(content_type),
        )
        return module.search(item, title) or []
    except Exception as exc:  # noqa: BLE001 - one broken channel must never abort the request
        _log("channel %s search failed: %r" % (channel_id, exc), level_error=True)
        return None


def _episodes_list(channel_id, show_item):
    """Call the channel's `episodios()` on the matched show item once --
    cached at show level by `_streams_for_channel()` alongside the match
    itself (`_MATCH_CACHE`), so a second episode of the same show never
    re-fetches it. Returns the raw itemlist (possibly empty), or `None`
    if the call itself raised (see `_search_channel()`'s docstring for
    the same empty-vs-`None` distinction and why it matters for
    `bh.ChannelBackoff`)."""
    try:
        module = __import__("channels.%s" % channel_id, None, None, ["channels.%s" % channel_id])
        return module.episodios(show_item) or []
    except Exception as exc:  # noqa: BLE001 - never abort the request over one channel's listing
        _log("channel %s episodios failed: %r" % (channel_id, exc), level_error=True)
        return None


def _find_videos(channel_id, item):
    """Call the channel's `findvideos()` on a matched movie/episode item.
    Returns the resulting itemlist (server items, possibly empty), or
    `None` if the call itself raised (see `_search_channel()`'s
    docstring)."""
    try:
        module = __import__("channels.%s" % channel_id, None, None, ["channels.%s" % channel_id])
        return module.findvideos(item) or []
    except Exception as exc:  # noqa: BLE001 - never abort the request over one channel's resolve
        _log("channel %s findvideos failed: %r" % (channel_id, exc), level_error=True)
        return None


#: Serializes resolves per Stream4Me server -- see `bh.KeyedLocks`. Also
#: guards `servers.mega`'s own module-global `c`/`files` state across
#: `_resolve_mega_target()` calls -- see that function's docstring.
_SERVER_LOCKS = bh.KeyedLocks()

#: Per-(channel, show/movie identity) cache of `(matched_item, episodes)`
#: -- see `bh.match_cache_key()`. `episodes` is the channel's own
#: `episodios()` result for a series (`None` for a movie, or when not yet
#: fetched), so a second episode of an already-matched show skips BOTH
#: the search and the episodios() call, not just the search.
_MATCH_CACHE = bh.TTLCache(_CACHE_TTL_SECONDS)

#: Per-channel consecutive-failure tracker -- see `bh.ChannelBackoff` and
#: `_channel_task()`.
_CHANNEL_BACKOFF = bh.ChannelBackoff(
    failure_threshold=_CHANNEL_FAILURE_THRESHOLD, cooldown_seconds=_CHANNEL_COOLDOWN_SECONDS,
)

#: This process's own listening port, set once by `main()` before it
#: starts serving. Module-level rather than threaded through every call:
#: `_shape_mega_stream()` needs to build a `/play/<key>` url from deep
#: inside the per-channel resolve pipeline, and there is exactly one port
#: for this whole process's lifetime.
_BRIDGE_PORT = None

#: key -> `(channel_id, raw_url)` for a mega server item awaiting its
#: deferred `/play/<key>` resolve -- see `_shape_mega_stream()`'s
#: docstring for why mega cannot resolve at LIST time like every other
#: server. Shares `_CACHE_TTL_SECONDS` with `_BridgeState.cache`: a
#: stream response offering this url can be served from that cache for
#: just as long, so the key it references must stay valid at least that
#: long too.
_MEGA_PLAY_REGISTRY = bh.TTLCache(_CACHE_TTL_SECONDS)

#: key -> `{"client": <servers.mega Client>, "target": url}`. NOT a
#: `TTLCache`: entries are pruned by the megaserver `Client`'s own
#: liveness (`.running`), not by time -- see `_resolve_mega_play()`.
#: Guarded by `_SERVER_LOCKS.lock_for("mega")`, the same lock the
#: module-global-clobbering resolve itself already needs.
_MEGA_PLAY_SESSIONS = {}  # type: dict

#: Signalled by `POST /shutdown` (see `_make_handler()`) so `main()`'s
#: loop exits without waiting for `xbmc.Monitor.abortRequested()`.
_SHUTDOWN_EVENT = threading.Event()


def _browser_user_agent():
    """Stream4Me's own browser User-Agent (`httptools.default_headers`),
    the one its `play_video()` sends to the player; `None` if unavailable."""
    try:
        from core import httptools
        return httptools.default_headers.get("User-Agent")
    except Exception:  # noqa: BLE001 - a missing UA only costs the header, never the stream
        return None


def _guard_server_module(server):
    """Install `bh.guard_video_check()` on `servers.<server>`'s
    `test_video_exists()`. `core.servertools` imports the same cached
    module object, so its resolve sees the guarded check. Call with that
    server's `_SERVER_LOCKS` lock held."""
    try:
        module = __import__("servers.%s" % server, None, None, ["servers.%s" % server])
    except Exception:  # noqa: BLE001 - servertools reports an unimportable server itself
        return
    check = getattr(module, "test_video_exists", None)
    if check is not None:
        module.test_video_exists = bh.guard_video_check(check)


def _current_mega_client():
    """The `Client` instance Stream4Me's own `servers.mega` module last
    created. `test_video_exists()` sets it via a bare `global c`
    assignment (exactly like the `files` module global `guard_video_check()`'s
    docstring describes), so this is only ever meaningful right after a
    resolve. `None` if that module was never imported/resolved."""
    module = sys.modules.get("servers.mega")
    return getattr(module, "c", None) if module is not None else None


def _resolve_mega_target(raw_url):
    """Run Stream4Me's own mega resolve chain (guarded
    `test_video_exists()` + `get_video_url()`, via
    `core.servertools.resolve_video_urls_for_playing()`) for one raw
    server-item url. Returns the FIRST resolved play url -- the
    megaserver proxy's own `http://127.0.0.1:80xx/...` address the
    player must actually GET -- or `None` on any failure.

    Must be called with `_SERVER_LOCKS.lock_for("mega")` held: like every
    other server this shares that lock with, `servers/mega.py` hands
    state from its check to its resolve through module globals (`c`,
    `files`), so two concurrent resolves can each read the other's
    result."""
    try:
        from core import servertools
        _guard_server_module("mega")
        video_urls, video_exists, _errors = servertools.resolve_video_urls_for_playing(
            "mega", raw_url, muestra_dialogo=False,
        )
    except Exception as exc:  # noqa: BLE001 - never fail the redirect over one resolve's crash
        _log("mega resolve failed: %r" % (exc,), level_error=True)
        return None
    if not video_exists or not video_urls:
        return None
    for entry in video_urls:
        if isinstance(entry, (list, tuple)) and len(entry) >= 2 and entry[1]:
            url, _resolved_headers = bh.split_kodi_url(entry[1])
            return url
    return None


def _register_mega_play(channel_id, raw_url):
    """Register one mega server item for on-demand `/play/<key>` resolve
    -- see `_shape_mega_stream()`'s docstring. Returns the fresh key."""
    key = bh.generate_play_key()
    _MEGA_PLAY_REGISTRY.set(key, (channel_id, raw_url))
    return key


def _shape_mega_stream(channel_id, raw_url, quality):
    """A mega server item becomes a stream pointing at THIS bridge's own
    `/play/<key>` instead of a directly resolved url.

    Resolving now (at LIST time, like every other server) would almost
    certainly hand out a dead url: `servers/mega.py` resolves by starting
    an in-process megaserver proxy on a random `127.0.0.1:80xx` port that
    auto-shuts-down ~20s later unless a player connects (see
    `lib.megaserver.client.Client`), but this bridge's response can sit in
    `_BridgeState.cache` for up to `_CACHE_TTL_SECONDS` before anyone
    plays it. Worse, a SECOND listing-time resolve (a different title, or
    the same one re-listed) clobbers `servers/mega.py`'s own
    module-global file list before the first title ever played --
    observed live as "Il padrino" served "Le ali della libertà".

    Deferring the resolve to the moment the player actually GETs the url
    (`_resolve_mega_play()`) sidesteps both: at most one resolve happens
    per key, exactly when its proxy is actually needed, and Kodi's own
    re-GET on seek reuses that SAME proxy instead of starting another.

    `description` mirrors `servers/mega.py`'s own `get_video_url()`
    convention (`"<ext> [mega]"`) as closely as possible without knowing
    the real filename ahead of the deferred resolve."""
    key = _register_mega_play(channel_id, raw_url)
    play_url = "http://127.0.0.1:%d/play/%s" % (_BRIDGE_PORT, key)
    return bh.shape_stream(channel_id, "[mega]", play_url, quality=quality)


def _resolve_mega_play(key):
    """Resolve (or reuse) the actual megaserver proxy url one
    `GET /play/<key>` request should redirect to. Returns the target url,
    or `None` for an unknown/expired key or a failed resolve (the
    handler turns that into a 404).

    Reuses the PREVIOUS session's target while its megaserver `Client` is
    still `.running` -- Kodi re-GETs the same url on every seek, and a
    fresh resolve per seek would each start a brand new megaserver proxy
    on a random port, orphaning the one already feeding the player mid-
    playback. Once that `Client` has auto-shut-down, the next GET for the
    same key resolves fresh (a new proxy, a new session)."""
    entry = _MEGA_PLAY_REGISTRY.get(key)
    if entry is None:
        return None
    _channel_id, raw_url = entry
    with _SERVER_LOCKS.lock_for("mega"):
        session = _MEGA_PLAY_SESSIONS.get(key)
        if session is not None and getattr(session["client"], "running", False):
            return session["target"]
        target = _resolve_mega_target(raw_url)
        if target is None:
            _MEGA_PLAY_SESSIONS.pop(key, None)
            return None
        _MEGA_PLAY_SESSIONS[key] = {"client": _current_mega_client(), "target": target}
        if len(_MEGA_PLAY_SESSIONS) > 256:
            # Bounded: drop sessions whose registry entry has since
            # expired (their stream response is long gone, so no future
            # GET can possibly reuse them).
            for stale_key in list(_MEGA_PLAY_SESSIONS):
                if _MEGA_PLAY_REGISTRY.get(stale_key) is None:
                    _MEGA_PLAY_SESSIONS.pop(stale_key, None)
        return target


def _resolve_streams_for_server_item(channel_id, server_item):
    """Resolve one server item (as produced by a channel's
    `findvideos()`) into zero or more Stremio `stream` objects, carrying
    the request headers Stream4Me's own player would send (see
    `bh.playback_headers()`), the adaptive manifest hint (see
    `bh.adaptive_manifest_type()`), and any subtitle url (see
    `bh.subtitle_url_from_entry()`). `mega` items are deferred instead of
    resolved here -- see `_shape_mega_stream()`."""
    server = getattr(server_item, "server", "") or ""
    raw_url = getattr(server_item, "url", "") or ""
    if not server or not raw_url or not bh.is_servable(server, raw_url):
        return []
    quality = getattr(server_item, "quality", None)
    if server == "torrent":
        # Stream4Me's torrent "resolver" only echoes the url back (to hand
        # it to Elementum); Rivulet streams a magnet itself.
        return [bh.shape_stream(channel_id, "magnet [torrent]", raw_url, quality=quality)]
    if server == "mega":
        return [_shape_mega_stream(channel_id, raw_url, quality)]
    page_url, _page_headers = bh.split_kodi_url(raw_url)
    try:
        from core import servertools
        with _SERVER_LOCKS.lock_for(server):
            _guard_server_module(server)
            # The raw url, headers and all, exactly as Stream4Me's own
            # play path passes it (some servers parse the `|` suffix).
            video_urls, video_exists, _errors = servertools.resolve_video_urls_for_playing(
                server, raw_url, muestra_dialogo=False,
            )
    except Exception as exc:  # noqa: BLE001 - never abort the request over one server's resolve
        _log("resolve failed for server %s: %r" % (server, exc), level_error=True)
        return []
    if not video_exists or not video_urls:
        return []
    user_agent = _browser_user_agent()
    referer = getattr(server_item, "referer", "")
    manifest = getattr(server_item, "manifest", "")
    drm = bh.drm_hint(getattr(server_item, "drm", ""), getattr(server_item, "license", ""))
    streams = []
    for entry in video_urls:
        # Servers return [label, url] and sometimes extra trailing fields
        # ([label, url, 0, subtitle]); only the first two matter for the
        # url itself.
        if not isinstance(entry, (list, tuple)) or len(entry) < 2 or not entry[1]:
            continue
        description, resolved = entry[0], entry[1]
        url, resolved_headers = bh.split_kodi_url(resolved)
        adaptive = bh.is_adaptive_entry(entry, manifest)
        headers = bh.playback_headers(
            server, page_url, referer, resolved_headers, user_agent, adaptive=adaptive,
        )
        adaptive_hint = bh.adaptive_manifest_type(entry, manifest) if adaptive else None
        subtitle_url = bh.subtitle_url_from_entry(entry)
        streams.append(bh.shape_stream(
            channel_id, description, url, headers=headers, quality=quality,
            adaptive_hint=adaptive_hint, subtitle_url=subtitle_url,
            drm=drm if adaptive else None,
        ))
    return streams


def _streams_for_channel(channel_id, imdb_id, title, year, tmdb_id, season, episode, content_type):
    """The full per-channel pipeline: search -> title/tmdb_id match ->
    (episodios for a series) -> findvideos -> resolved streams.

    The search match AND (for a series) the episodios() list are cached
    together per show/movie identity (`_MATCH_CACHE`, keyed by
    `bh.match_cache_key()`) independent of season/episode: a second
    episode request for an already-matched show skips straight to
    `bh.pick_episode()` and `findvideos()`, without re-searching or
    re-listing episodes.

    Raises if a Stream4Me call itself broke (see `_search_channel()`/
    `_episodes_list()`/`_find_videos()`'s `None`-vs-empty-list
    distinction) -- `_channel_task()` is the one place that catches this
    to drive `bh.ChannelBackoff`; every OTHER "no result" path here
    (`imdb_id` not found by this channel, no matching episode, no
    servable server item) returns `[]` and must never count as a
    failure."""
    cache_key = bh.match_cache_key(channel_id, content_type, tmdb_id, title, year)
    cached = _MATCH_CACHE.get(cache_key)
    if cached is not None:
        matched, episodes = cached
    else:
        results = _search_channel(channel_id, title, content_type)
        if results is None:
            raise RuntimeError("channel %s search() raised" % channel_id)
        matched = None
        for candidate in results:
            info_labels = dict(getattr(candidate, "infoLabels", {}) or {})
            if bh.result_matches(info_labels, tmdb_id, title, year):
                matched = candidate
                break
        episodes = None
        if matched is not None and season is not None and episode is not None:
            episodes = _episodes_list(channel_id, matched)
            if episodes is None:
                raise RuntimeError("channel %s episodios() raised" % channel_id)
        _MATCH_CACHE.set(cache_key, (matched, episodes))

    if matched is None:
        return []

    target_item = matched
    if season is not None and episode is not None:
        target_item = bh.pick_episode(episodes, season, episode)
        if target_item is None:
            return []

    videos = _find_videos(channel_id, target_item)
    if videos is None:
        raise RuntimeError("channel %s findvideos() raised" % channel_id)

    streams = []
    for server_item in videos:
        streams.extend(_resolve_streams_for_server_item(channel_id, server_item))
    return streams


def _channel_task(channel_id, imdb_id, title, year, tmdb_id, season, episode, content_type):
    """Wraps `_streams_for_channel()` to feed `_CHANNEL_BACKOFF`: a raised
    exception counts as one consecutive failure (see `bh.ChannelBackoff`),
    a normal return -- EMPTY or not -- resets it. This is the ONE place
    that decides "channel really broke" apart from "channel searched
    fine and simply found nothing", so a channel with no matching title
    is never penalized for it."""
    try:
        streams = _streams_for_channel(
            channel_id, imdb_id, title, year, tmdb_id, season, episode, content_type,
        )
    except Exception:
        _CHANNEL_BACKOFF.record_failure(channel_id)
        raise
    _CHANNEL_BACKOFF.record_success(channel_id)
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

    def channels_for_request(self, content_type=None):
        active = self.catalog.active_channel_ids(content_type)
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

    # Resolve the channel selection BEFORE the cache lookup, and fold it
    # into the cache key: `s4me_channels` can change between requests, and
    # a response cached under the old selection must never be replayed
    # for a request that would now fan out to a different channel set.
    channels = state.channels_for_request(content_type_param)
    cache_key = (content_type_param, imdb_id, season, episode, channels)
    cached = state.cache.get(cache_key)
    if cached is not None:
        return {"streams": cached}

    budget = bh.Budget(_REQUEST_BUDGET_SECONDS)
    search_type = "tv" if content_type_param == "series" else "movie"
    resolved = _resolve_title(imdb_id, search_type)
    if resolved is None:
        return {"streams": []}
    title, year, tmdb_id = resolved

    streams = []
    timed_out = False
    # A channel currently serving its `bh.ChannelBackoff` cooldown is
    # skipped outright -- not even submitted to the pool -- so a
    # persistently broken channel costs this request nothing beyond one
    # dict lookup, instead of a full worker slot and I/O timeout.
    available_channels = tuple(c for c in channels if _CHANNEL_BACKOFF.is_available(c))
    if available_channels:
        pool = ThreadPoolExecutor(max_workers=min(_MAX_CHANNEL_WORKERS, len(available_channels)))
        tasks = {
            channel_id: partial(
                _channel_task, channel_id, imdb_id, title, year, tmdb_id,
                season, episode, content_type_param,
            )
            for channel_id in available_channels
        }
        try:
            def _cache_late(all_streams):
                # The fan-out outlived the request budget; once every
                # channel has finished, cache the complete answer so the
                # next request for this title is served instantly.
                state.cache.set(cache_key, all_streams)
                _log(
                    "late fan-out finished for %s, cached %d result(s)"
                    % (id_param, len(all_streams)),
                )

            streams, timed_out = bh.collect_with_budget(
                pool, tasks, budget,
                on_error=lambda channel_id, exc: _log(
                    "channel %s raised: %r" % (channel_id, exc), level_error=True,
                ),
                on_late_complete=_cache_late,
                late_timeout=_LATE_COMPLETION_SECONDS,
            )
        finally:
            # Python 3.8 has no shutdown(cancel_futures=True) -- waiting
            # here for a straggler would defeat the whole point of the
            # budget above. A left-over worker keeps running in the
            # background (bounded by _CHANNEL_IO_TIMEOUT_SECONDS, see
            # main()) and is simply discarded once it finishes.
            pool.shutdown(wait=False)
        if timed_out:
            _log(
                "channel fan-out exceeded the %.1fs request budget, returning %d partial result(s)"
                % (_REQUEST_BUDGET_SECONDS, len(streams))
            )

    # A timed-out fan-out only reflects whichever channels happened to
    # finish first -- caching that partial would keep re-serving it for
    # the full _CACHE_TTL_SECONDS (30 minutes). The complete result is
    # cached instead by _cache_late() above once the stragglers finish.
    if not timed_out:
        state.cache.set(cache_key, streams)
    return {"streams": streams}


def _make_handler(state):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # noqa: A003 - overriding BaseHTTPRequestHandler's own name
            # Debug, not info: Rivulet probes /manifest.json every 10 s,
            # which would otherwise add a kodi.log line each time.
            _log(fmt % args, level_debug=True)

        def _send_json(self, payload, status=200):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            origin = bh.cors_allow_origin(self.headers.get("Origin"))
            if origin is not None:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
            self.end_headers()
            self.wfile.write(body)

        def _redirect(self, target):
            self.send_response(302)
            self.send_header("Location", target)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's own naming convention
            path = self.path.split("?", 1)[0]
            parts = [p for p in path.split("/") if p]
            try:
                if path == "/manifest.json":
                    self._send_json(bh.build_manifest())
                    return
                if path == "/shutdown":
                    # Only POST may trigger a shutdown: this server has no
                    # other authentication, and a GET is exactly what a
                    # third-party page's <img src="http://127.0.0.1:<port>/shutdown">
                    # would issue -- see lib.s4me.shutdown_bridge()'s own
                    # POST.
                    self._send_json({"error": "method not allowed"}, status=405)
                    return
                if len(parts) == 2 and parts[0] == "play":
                    target = _resolve_mega_play(parts[1])
                    if target is None:
                        self._send_json({"error": "not found"}, status=404)
                        return
                    self._redirect(target)
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

        def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler's own naming convention
            path = self.path.split("?", 1)[0]
            try:
                if path == "/shutdown":
                    # Reply BEFORE signalling: lib.s4me.shutdown_bridge()
                    # must see its {"ok": true} even though this process
                    # is about to stop serving.
                    self._send_json({"ok": True})
                    _SHUTDOWN_EVENT.set()
                    return
                self._send_json({"error": "not found"}, status=404)
            except Exception as exc:  # noqa: BLE001 - a handler crash must never take the whole server down
                _log("request handler failed for %s: %r" % (self.path, exc), level_error=True)
                self._send_json({"error": "internal error"}, status=500)

    return Handler


def _shutdown_cleanup(server):
    """Stop serving and best-effort close Stream4Me's own sqlitedict
    worker (`core.db`) -- its own `service.py`/`platformcode/launcher.py`
    do the exact same thing on every one of THEIR exit paths ("db need to
    be closed when not used, it will cause freezes"), so this bridge
    follows suit rather than leaving that connection's worker thread
    (`lib.sqlitedict.SqliteMultithread`) to whatever its own daemon-thread
    fate is. Shared by BOTH exit paths this script has -- Kodi's own
    `xbmc.Monitor` abort AND `POST /shutdown` -- so neither skips it.

    Logs every thread still alive afterwards at debug (mirrors
    `launcher.py`'s own `logger.debug(threading.enumerate())`), so a slow
    Kodi RunScript stop is diagnosable instead of a bare "script didn't
    stop in 5 seconds" warning with no further clue."""
    try:
        server.shutdown()
        server.server_close()
    except Exception as exc:  # noqa: BLE001 - shutdown must proceed regardless
        _log("error stopping HTTP server: %r" % (exc,), level_error=True)
    try:
        from core import db
        db.close()
    except Exception as exc:  # noqa: BLE001 - best-effort, mirrors Stream4Me's own db.close() call sites
        _log("could not close Stream4Me's db: %r" % (exc,), level_error=True)
    # Every megaserver proxy this bridge started runs its own HTTP server
    # thread; stop them too rather than leave them for Kodi to kill.
    clients = [session.get("client") for session in list(_MEGA_PLAY_SESSIONS.values())]
    clients.append(_current_mega_client())
    for client in clients:
        try:
            if client is not None and getattr(client, "running", False):
                client.stop()
        except Exception:  # noqa: BLE001 - best-effort, like db.close() above
            pass
    try:
        remaining = [t.name for t in threading.enumerate()]
        _log("threads remaining at shutdown: %r" % (remaining,), level_debug=True)
    except Exception:  # noqa: BLE001 - logging must never crash shutdown
        pass


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

    # Stream4Me still calls xbmc.translatePath()/validatePath()/
    # makeLegalFilename(), which Kodi 19 moved to xbmcvfs and Kodi 20+
    # removed from xbmc. Its own entry points (default.py, service.py,
    # contextmenu.py) copy them back onto xbmc before importing anything,
    # so the bridge must do the same or `import core` fails on
    # platformcode.config's first translatePath() call.
    try:
        import xbmcvfs
        bh.restore_moved_xbmc_functions(xbmc, xbmcvfs)
    except Exception as exc:  # noqa: BLE001 - Kodi 18 has no xbmcvfs.translatePath; nothing to restore
        _log("could not restore xbmc path helpers: %r" % (exc,), level_error=True)

    _install_s4me_path(s4me_root)
    try:
        import core  # noqa: F401 - proves Stream4Me's tree actually imports before serving anything
    except Exception as exc:  # noqa: BLE001 - a broken Stream4Me install must exit cleanly, never half-serve
        _log("failed to import Stream4Me's core package, exiting: %r" % (exc,), level_error=True)
        sys.exit(1)
    _bound_channel_io_timeout()
    _log_s4me_version(s4me_root)
    # Also mirrors Stream4Me's default.py: point TMPDIR at its temp dir so
    # anything it writes through tempfile lands where it expects.
    try:
        from platformcode import config
        os.environ["TMPDIR"] = config.get_temp_file("")
    except Exception as exc:  # noqa: BLE001 - best effort, same as Stream4Me itself
        _log("could not set TMPDIR for Stream4Me: %r" % (exc,), level_error=True)

    state = _BridgeState(s4me_root)
    global _BRIDGE_PORT
    _BRIDGE_PORT = port
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), _make_handler(state))
    except OSError as exc:
        # A stale bridge still holding the port (lib.s4me.shutdown_bridge()
        # not having taken effect yet, or a second Kodi instance) must
        # exit quietly -- not with a raw traceback that looks like a bug
        # in THIS bridge -- so BridgeSupervisor's own relaunch backoff is
        # the only thing that retries it.
        _log(
            "could not bind 127.0.0.1:%d, port busy, another bridge is running: %r" % (port, exc),
            level_error=True,
        )
        sys.exit(1)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    _log("serving on 127.0.0.1:%d" % port)

    monitor = xbmc.Monitor()
    try:
        while not monitor.abortRequested() and not _SHUTDOWN_EVENT.is_set():
            if monitor.waitForAbort(_MONITOR_POLL_SECONDS):
                break
    finally:
        _log("shutting down")
        _shutdown_cleanup(server)


if __name__ == "__main__":
    main()
