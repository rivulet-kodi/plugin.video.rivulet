"""Playback resolution: turn a Stremio Stream object into a Kodi-playable URL.

Kodi calls default.py -> router.run() -> here with the ADDON_HANDLE and the
base64url-decoded stream dict for action=play. This module owns the only
xbmc* calls involved in actually starting playback.
"""
import contextlib
import os
from urllib.parse import urlencode

import xbmc
import xbmcgui
import xbmcplugin

from lib.store import Store
from lib.stremio.addons import AddonClient
from lib.stremio.server import UNKNOWN_FILE_IDX, ServerClient, guess_file_idx
from lib.stremio.subtitles import collect_subtitles, sort_subtitles
from lib.ui.compat import (
    ADDON,
    L,
    addon_profile_dir,
    log,
    notify,
    set_video_info,
    setting_bool,
    setting_int,
)

#: Extension -> MIME type for the video containers Stremio streams commonly
#: use. Keyed by `os.path.splitext()` output (lowercased, leading dot kept).
_MIME_TYPES = {
    '.mkv': 'video/x-matroska',
    '.mp4': 'video/mp4',
    '.m4v': 'video/mp4',
    '.avi': 'video/x-msvideo',
    '.mov': 'video/quicktime',
    '.ts': 'video/mp2t',
    '.m2ts': 'video/mp2t',
    '.webm': 'video/webm',
    '.flv': 'video/x-flv',
    '.wmv': 'video/x-ms-wmv',
    '.mpg': 'video/mpeg',
    '.mpeg': 'video/mpeg',
}


def _mime_for(filename):
    """Best-effort MIME type for `filename`'s extension, or None.

    Unknown/absent extensions return None so the caller skips
    `setMimeType` entirely rather than hinting a wrong/generic type.
    """
    if not filename:
        return None
    ext = os.path.splitext(filename)[1].lower()
    return _MIME_TYPES.get(ext)


def _filename_from_url(url):
    """Last path segment of a resolved playback `url`, with any baked
    `|urlencoded-headers` suffix (see the header-baking below in `play()`)
    and query string stripped first.
    """
    base = url.split('|', 1)[0].split('?', 1)[0]
    return base.rsplit('/', 1)[-1]


#: Bounded (connect, read) timeouts for the pre-buffer network calls. The
#: SHORT read timeout is what makes the "Preparing stream" dialog
#: cancellable: on a stalled/dead-swarm read the socket unblocks within a
#: few seconds so the loop can recheck dialog.iscanceled(), instead of the
#: whole UI freezing for a 60s read (the original "can't cancel" bug -
#: kodi.log showed three back-to-back 60s freezes on a dead torrent). A
#: read that keeps receiving bytes resets its own clock, so this never
#: aborts a genuinely-progressing (even very slow) download.
_FRONT_TIMEOUT = (3.05, 5)
_METADATA_TIMEOUT = (3.05, 8)

#: Pause between retry attempts; also the abort-poll interval.
_ATTEMPT_PAUSE_SECONDS = 2.0

#: Retry-attempt budgets before giving up. Each attempt is bounded by the
#: short timeouts above (so cancel is always seen within a few seconds);
#: these caps are just the give-up backstop for a genuinely dead swarm.
_MAX_METADATA_ATTEMPTS = 60
_MAX_FRONT_ATTEMPTS = 60

#: Seconds to wait for a not-yet-reachable streaming server to come up
#: (e.g. one the background service is still launching) before giving up.
_SERVER_WAIT_ATTEMPTS = 5

#: Sleep between reachability probes while waiting for the streaming
#: server to come up (see `_wait_for_server`). Also doubles as the
#: abort-poll interval for that wait via `monitor.waitForAbort`.
_SERVER_POLL_INTERVAL_SECONDS = 1.0

#: Minimum bytes streamed from the file's FRONT (offset 0) before Kodi's
#: player can reliably probe the container header and start playback (see
#: ServerClient.iter_front's docstring in lib/stremio/server.py). Reaching
#: this floor means "safe to start", not "fully pre-buffered" - the
#: server's own readahead keeps filling ahead once playback begins, and
#: it is deliberately much smaller than the user's configured buffer_mb
#: target (a minimum of 5 MiB).
_HEADER_MIN_BYTES = 512 * 1024

#: DialogProgress percent bands for the staged "Preparing stream" dialog
#: `_resolve_playable_item` owns (created once, threaded through every
#: helper below). Order matches the real stage order so the whole
#: progression reads as monotonic forward motion: connect -> resolve ->
#: metadata -> engine warm -> buffer. Buffering gets the lion's share
#: (40-100%) since it is the only stage with a real, user-meaningful
#: ratio (bytes obtained so far / target); the others are coarse "still
#: working" ticks with no true fraction to report.
_CONNECT_PERCENT_MAX = 10
_RESOLVE_PERCENT = 15
_METADATA_PERCENT_BASE = 20
_METADATA_PERCENT_SPAN = 15  # 20-35%
_ENGINE_WARM_PERCENT = 38
_BUFFER_PERCENT_BASE = 40
_BUFFER_PERCENT_SPAN = 60  # 40-100%

# Stream source kinds that require the local streaming server to produce a
# playable URL at all (see stremio-protocol-spec.md gotcha #3).
_SERVER_DEPENDENT_KEYS = (
    'infoHash', 'ytId', 'rarUrls', 'zipUrls', '7zipUrls',
    'tarUrls', 'tgzUrls', 'nzbUrl', 'nzbUrls',
)


def _server_client():
    base_url = ADDON.getSetting('server_url') or 'http://127.0.0.1:11470'
    return ServerClient(base_url)


_STORE = None
_CLIENT = None


def _get_store():
    global _STORE
    if _STORE is None:
        _STORE = Store(addon_profile_dir())
    return _STORE


def _get_client():
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = AddonClient()
    return _CLIENT


def _attach_subtitles(list_item, behavior_hints, stype, sid):
    """Best-effort addon-subtitle lookup: never raises, never blocks
    playback - a broken subtitle addon just means a missing subtitle track.
    """
    if not setting_bool('subs_enable', True):
        return
    try:
        extra = []
        if 'videoSize' in behavior_hints:
            extra.append(('videoSize', str(behavior_hints['videoSize'])))
        if 'filename' in behavior_hints:
            extra.append(('filename', behavior_hints['filename']))
        subs = collect_subtitles(
            _get_client(), _get_store().get_addons(), stype, sid, extra=extra or None
        )
        subs = sort_subtitles(subs, ADDON.getSetting('subs_language') or 'en')
        urls = [sub['url'] for sub in subs[:20]]
        if urls:
            list_item.setSubtitles(urls)
    except Exception as exc:  # noqa: BLE001 - subtitles are a bonus, never fatal
        log('player: subtitle fetch failed for %s/%s: %r' % (stype, sid, exc), xbmc.LOGWARNING)


def _human_size(num_bytes):
    """Format a byte count as e.g. '12.3 MB' (B/KB/MB/GB, 1 decimal)."""
    value = float(num_bytes or 0)
    for unit in ('B', 'KB', 'MB'):
        if value < 1024.0:
            return '%.1f %s' % (value, unit)
        value /= 1024.0
    return '%.1f GB' % value


def _lfmt(string_id, *args):
    """Format localized string `string_id` with `args`, degrading to the
    bare space-joined args when the translation is stale/empty or has
    mismatched placeholders (e.g. a hot-deployed strings.po Kodi hasn't
    reloaded yet: `'' % (1, 60)` raises TypeError). Dialog cosmetics
    must never abort stream preparation.
    """
    try:
        return L(string_id) % args
    except (TypeError, ValueError):
        return ' '.join(str(arg) for arg in args)


def _stats_line(stats):
    """Best-effort 'speed - N peers' line from a `/create` stats snapshot
    (the same shape `_await_file_idx` and the buffering loop below poll),
    or '' once there is nothing worth showing yet - a still-warming
    engine with no `peers` key, or a poll that failed and passed `None`
    through. Never raises.
    """
    peers = (stats or {}).get('peers')
    if peers is None:
        return ''
    speed = _human_size((stats or {}).get('downloadSpeed') or 0)
    return _lfmt(30082, speed, peers)


def _poll_stats_best_effort(server, info_hash):
    """Live stats snapshot for the buffering dialog's second line - the
    SAME `/create` poll `_await_file_idx` uses for its own speed/peers
    line, reused here so a torrent already past metadata resolution still
    shows live numbers while its front is being primed. Throttled to one
    call per outer front-priming attempt (never per chunk): a fast swarm
    can yield many chunks in one attempt and this must not turn into a
    stats-server hammering loop. A failure here is purely cosmetic - the
    dialog just shows no stats line for that attempt - and must never
    break the front-priming loop itself.
    """
    try:
        return server.create_engine(info_hash, timeout=_METADATA_TIMEOUT)
    except Exception as exc:  # noqa: BLE001 - stats are a bonus, never fatal to buffering
        log('player: buffer stats poll failed for %s: %r' % (info_hash, exc), xbmc.LOGWARNING)
        return None


def _await_file_idx(server, stream, info_hash, url, dialog, monitor):
    """Poll `GET /create` until stremio-server-go resolves a file index for
    streams with no fileIdx of their own, sharing the caller's `dialog`/
    `monitor` (owned by `_resolve_playable_item` for the whole resolve -
    see that function) so the flow stays cancellable and ticks the
    "Fetching torrent metadata…" stage (20-35%, with a live attempt
    counter and, once available, a speed/peers line).

    Live-verified gap this closes: against stremio-server-go v0.8.5,
    `/create` returns BEFORE metadata resolves and its response never
    gains `guessedFileIdx` later - only a `files` array once metadata
    lands (see `guess_file_idx()`). Older/other server builds that DO
    emit `guessedFileIdx` up front resolve on the very first iteration.

    Each poll uses a SHORT client timeout (`_METADATA_TIMEOUT`) so a
    still-warming `/create` cannot freeze the loop between cancel checks -
    a timed-out poll just re-hits the same warming engine next iteration.

    Returns `(file_idx, url, proceed)`. `proceed` is False only on
    cancellation (caller must resolve False). When the budget runs out
    with no usable metadata, `file_idx` is UNKNOWN_FILE_IDX and `proceed`
    is True - the caller then falls back to "proceed without polling".
    """
    for attempt in range(_MAX_METADATA_ATTEMPTS):
        if dialog.iscanceled():
            return UNKNOWN_FILE_IDX, url, False

        try:
            stats = server.create_engine(info_hash, timeout=_METADATA_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 - a slow/failed poll just means "try again"
            log('player: metadata poll failed for %s: %r' % (info_hash, exc), xbmc.LOGWARNING)
            stats = None

        idx = guess_file_idx(stats)
        if idx is not None:
            trackers = stream.get('announce') or stream.get('sources') or []
            rebuilt = server.torrent_url(stream['infoHash'], idx, trackers)
            return idx, rebuilt, True

        percent = min(_METADATA_PERCENT_BASE + _METADATA_PERCENT_SPAN, _METADATA_PERCENT_BASE + attempt)
        message = '%s\n%s' % (L(30088), _lfmt(30090, attempt + 1, _MAX_METADATA_ATTEMPTS))
        stats_line = _stats_line(stats)
        if stats_line:
            message += '\n' + stats_line
        dialog.update(percent, message)

        if monitor.waitForAbort(1.0):
            return UNKNOWN_FILE_IDX, url, False

    return UNKNOWN_FILE_IDX, url, True


def _prebuffer_torrent(server, stream, url, dialog, monitor):
    """Warm the torrent engine and show cancellable, truthful progress
    before playback, ticking the shared `dialog` (owned/closed by
    `_resolve_playable_item` for the whole resolve, not here - see that
    function) through its engine-warm/metadata (20-38%) and buffering
    (40-100%) stages.

    Only called for torrent streams (`infoHash` present) once the server
    is already known available and the stream itself has been resolved.
    Returns `(proceed, url)`: `proceed` is False when the user cancelled
    OR no usable front data could be obtained (caller must resolve
    False); `url` is the original url, or the rebuilt one when the server
    had to guess the file index. ANY unexpected error degrades to `(True,
    url)` - a broken pre-buffer must never block playback.
    """
    buffer_enable = setting_bool('buffer_enable', True)
    log(
        'player: pre-buffer entry: buffer_enable=%s fileIdx=%r' % (buffer_enable, stream.get('fileIdx')),
        xbmc.LOGINFO,
    )
    if not buffer_enable:
        return True, url

    info_hash = stream['infoHash']
    try:
        if dialog.iscanceled():
            return False, url

        file_idx = stream.get('fileIdx')
        if file_idx is None:
            file_idx = UNKNOWN_FILE_IDX
        if file_idx == UNKNOWN_FILE_IDX:
            file_idx, url, proceed = _await_file_idx(server, stream, info_hash, url, dialog, monitor)
            if not proceed:
                return False, url
            if file_idx == UNKNOWN_FILE_IDX:
                # Metadata never arrived within budget; nothing to stream
                # the front of, so just start playback.
                notify(L(30083))
                return True, url
        else:
            # Warm the engine, but bounded: a cold /create would otherwise
            # block for its full timeout with no cancel check. The front
            # reads below drive the engine anyway, so a failed/slow warm is
            # non-fatal.
            dialog.update(_ENGINE_WARM_PERCENT, L(30089))
            try:
                server.create_engine(info_hash, timeout=_METADATA_TIMEOUT)
            except Exception as exc:  # noqa: BLE001 - front reads drive the engine regardless
                log('player: engine warm failed for %s: %r (continuing)' % (info_hash, exc), xbmc.LOGWARNING)

        buffer_mb = setting_int('buffer_mb', 20, minimum=5)
        target = buffer_mb * 1024 * 1024
        log(
            'player: pre-buffer target: buffer_mb=%d target_bytes=%d' % (buffer_mb, target),
            xbmc.LOGINFO,
        )

        # Front-priming readiness loop. Streams the file FRONT (offset 0,
        # where ffmpeg's container probe reads) directly rather than
        # trusting aggregate download stats, which can report megabytes
        # "buffered" from out-of-order pieces while the front is still
        # missing (the live CURLE_PARTIAL_FILE / "error probing input
        # format" bug). Short per-read timeout keeps the dialog cancellable;
        # a genuinely dead swarm fails honestly (30084) after the budget
        # rather than hanging or handing Kodi a doomed URL.
        for attempt in range(_MAX_FRONT_ATTEMPTS):
            if dialog.iscanceled():
                return False, url

            # Best-effort live speed/peers for this attempt's updates,
            # throttled to one poll per attempt (see
            # `_poll_stats_best_effort`'s docstring). Re-checked right
            # after so a slow poll never delays the next cancel check
            # past the front-read call that follows it.
            stats_line = _stats_line(_poll_stats_best_effort(server, info_hash))
            if dialog.iscanceled():
                return False, url

            got = 0
            try:
                for chunk_len in server.iter_front(info_hash, file_idx, target, timeout=_FRONT_TIMEOUT):
                    got += chunk_len
                    percent = min(100, _BUFFER_PERCENT_BASE + got * _BUFFER_PERCENT_SPAN // target) if target else 100
                    message = _lfmt(30081, _human_size(got), _human_size(target))
                    if stats_line:
                        message += '\n' + stats_line
                    dialog.update(percent, message)
                    if dialog.iscanceled():
                        return False, url
                    if got >= target:
                        break
            except Exception as exc:  # noqa: BLE001 - a front-read hiccup must not brick playback
                log('player: front read failed for %s: %r' % (info_hash, exc), xbmc.LOGWARNING)

            if got >= _HEADER_MIN_BYTES:
                log(
                    'player: pre-buffer complete for %s: buffered=%d target=%d' % (info_hash, got, target),
                    xbmc.LOGINFO,
                )
                return True, url

            # About to sleep _ATTEMPT_PAUSE_SECONDS before retrying - show a
            # retrying hint so that silent pause isn't a dead-looking dialog.
            percent = min(100, _BUFFER_PERCENT_BASE + got * _BUFFER_PERCENT_SPAN // target) if target else 100
            retry_message = '%s\n%s' % (
                _lfmt(30081, _human_size(got), _human_size(target)),
                _lfmt(30090, attempt + 1, _MAX_FRONT_ATTEMPTS),
            )
            if stats_line:
                retry_message += '\n' + stats_line
            dialog.update(percent, retry_message)

            if monitor.waitForAbort(_ATTEMPT_PAUSE_SECONDS):
                return False, url

        log(
            'player: pre-buffer timed out for %s after %d attempts with no usable front data'
            % (info_hash, _MAX_FRONT_ATTEMPTS),
            xbmc.LOGINFO,
        )
        notify(L(30084))
        return False, url
    except Exception as exc:  # noqa: BLE001 - pre-buffer is a bonus, never fatal
        log('player: pre-buffer failed for %s: %r' % (stream.get('infoHash'), exc), xbmc.LOGWARNING)
        return True, url


def _wait_for_server(server, dialog, monitor):
    """Return True as soon as the streaming server answers, waiting briefly
    for a not-yet-reachable instance (e.g. one the background service is
    still launching) to come up rather than failing instantly on the first
    probe. Cancellable via the shared `dialog`/`monitor`
    `_resolve_playable_item` owns for the whole resolve (created/closed
    once there, not here); ticks the "Connecting to streaming server…"
    stage (0-10%) while it waits.
    """
    if server.is_available():
        return True
    for attempt in range(_SERVER_WAIT_ATTEMPTS):
        if dialog.iscanceled():
            return False
        percent = min(_CONNECT_PERCENT_MAX, (attempt + 1) * _CONNECT_PERCENT_MAX // _SERVER_WAIT_ATTEMPTS)
        dialog.update(percent, L(30086))
        if monitor.waitForAbort(_SERVER_POLL_INTERVAL_SECONDS):
            return False
        if server.is_available():
            return True
    return False


def _resolve_playable_item(stream, stype, sid):
    """Resolve `stream` (Stremio Stream object for content `stype`/`sid`)
    to a `(url, list_item)` pair ready to hand to Kodi's player, or
    `(None, None)` on failure - a notification has already been shown
    (either here or inside `_prebuffer_torrent`) by the time this
    returns `None`.

    Owns the single "Preparing stream" `DialogProgress` for the WHOLE
    resolve - created once here, threaded through `_wait_for_server`, the
    `resolve_stream()` call, and `_prebuffer_torrent` (which used to each
    create/close their own dialog, so a torrent stream that also had to
    wait for the server could briefly show two in a row). Every stage
    below updates this same instance and only this function ever creates
    or closes it (see the `finally` below), so a cancel raised anywhere
    inside always surfaces here as `(None, None)`.

    Shared by `play()` (the classical GetDirectory path -
    `xbmcplugin.setResolvedUrl`) and `play_direct()` (the custom-window
    path - `xbmc.Player().play()`): neither `xbmcplugin` nor an
    `ADDON_HANDLE` is touched here, only stream resolution.
    """
    stream = stream or {}
    behavior_hints = stream.get('behaviorHints') or {}
    title = behavior_hints.get('filename') or stream.get('title') or stream.get('name') or ''

    server = _server_client()
    dialog = xbmcgui.DialogProgress()
    dialog.create(L(30080), title)
    try:
        monitor = xbmc.Monitor()

        if any(key in stream for key in _SERVER_DEPENDENT_KEYS) and not _wait_for_server(server, dialog, monitor):
            notify(L(30031))
            return None, None

        dialog.update(_RESOLVE_PERCENT, L(30087))
        try:
            url = server.resolve_stream(stream)
        except Exception as exc:  # noqa: BLE001 - a broken server response must not crash Kodi
            log('player: resolve_stream failed for %s/%s: %r' % (stype, sid, exc), xbmc.LOGERROR)
            url = None

        if not url:
            notify(L(30030))
            return None, None

        if dialog.iscanceled():
            return None, None

        if stream.get('infoHash'):
            proceed, url = _prebuffer_torrent(server, stream, url, dialog, monitor)
            if not proceed:
                return None, None
    finally:
        # A raising close() must never replace an exception already
        # unwinding through this try (e.g. a cancel/notify path above) -
        # best-effort cleanup only.
        with contextlib.suppress(Exception):
            dialog.close()

    request_headers = (behavior_hints.get('proxyHeaders') or {}).get('request') or {}
    if request_headers:
        # Kodi convention: "|urlencoded=headers" appended to the path makes
        # the player send these headers with every request for that URL.
        url = '%s|%s' % (url, urlencode(request_headers))

    filename = behavior_hints.get('filename')

    list_item = xbmcgui.ListItem(path=url)
    # Disable Kodi's content-type HEAD probe: it races/aborts against a
    # torrent engine that is still (re)priming a range on open/seek, which
    # is the primary cause of seek-exits-playback. setMimeType (when the
    # extension is known) gives Kodi the same information up front so the
    # probe was never needed.
    list_item.setContentLookup(False)
    mime = _mime_for(filename or _filename_from_url(url))
    if mime:
        list_item.setMimeType(mime)

    if filename:
        list_item.setLabel(filename)

    set_video_info(list_item, {
        'title': filename or list_item.getLabel(),
        'mediatype': 'episode' if stype == 'series' else 'movie',
    })

    _attach_subtitles(list_item, behavior_hints, stype, sid)

    return url, list_item


def play(handle, stream, stype, sid):
    """Resolve `stream` and hand it to Kodi via `setResolvedUrl` - the
    classical GetDirectory play path (action=play)."""
    _url, list_item = _resolve_playable_item(stream, stype, sid)
    if list_item is None:
        xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem())
        return
    xbmcplugin.setResolvedUrl(handle, True, list_item)


def play_direct(stream, stype, sid):
    """Resolve `stream` and hand it DIRECTLY to `xbmc.Player()` - the
    custom-window path (`lib.ui.streamswindow`), where there is no
    `ADDON_HANDLE`/GetDirectory call to satisfy. Returns True if
    playback was started, False on a resolution failure (already
    notified by `_resolve_playable_item`)."""
    url, list_item = _resolve_playable_item(stream, stype, sid)
    if list_item is None:
        return False
    xbmc.Player().play(url, list_item)
    return True
