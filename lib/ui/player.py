"""Playback resolution: turn a Stremio Stream object into a Kodi-playable URL.

Kodi calls default.py -> router.run() -> here with the ADDON_HANDLE and the
base64url-decoded stream dict for action=play. This module owns the only
xbmc* calls involved in actually starting playback.
"""
from urllib.parse import urlencode

import xbmc
import xbmcgui
import xbmcplugin

from lib.store import Store
from lib.stremio.addons import AddonClient
from lib.stremio.server import ServerClient, UNKNOWN_FILE_IDX, buffered_bytes, guess_file_idx
from lib.stremio.subtitles import collect_subtitles, sort_subtitles
from lib.ui.compat import ADDON, L, addon_profile_dir, log, notify, setting_bool, setting_int

#: Hard cap on total pre-buffer wait time (contract step 5: "120s elapsed").
_BUFFER_MAX_WAIT_SECONDS = 120

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


def _await_file_idx(server, stream, info_hash, url, dialog, monitor, elapsed):
    """Poll `GET /create` until stremio-server-go resolves a file index for
    streams with no fileIdx of their own, sharing `elapsed`/`dialog`/
    `monitor` with the caller's per-file byte-poll loop so both phases
    spend one combined `_BUFFER_MAX_WAIT_SECONDS` budget rather than one
    each.

    Live-verified gap this closes: against stremio-server-go v0.8.5,
    `/create` returns BEFORE metadata resolves and its response never
    gains `guessedFileIdx` later - only a `files` array once metadata
    lands (see `guess_file_idx()`). Older/other server builds that DO
    emit `guessedFileIdx` up front resolve on the very first iteration
    below, so this costs them nothing.

    Returns `(file_idx, url, elapsed, proceed)`. `proceed` is False only
    on cancellation (caller must resolve False, matching the byte-poll
    loop's cancel semantics). When the budget runs out with no usable
    metadata, `file_idx` is UNKNOWN_FILE_IDX and `proceed` is True - the
    caller then falls back to "proceed without polling".
    """
    while elapsed < _BUFFER_MAX_WAIT_SECONDS:
        if dialog.iscanceled():
            return UNKNOWN_FILE_IDX, url, elapsed, False

        stats = server.create_engine(info_hash)
        idx = guess_file_idx(stats)
        if idx is not None:
            rebuilt = server.torrent_url(stream['infoHash'], idx, stream.get('announce') or [])
            return idx, rebuilt, elapsed, True

        peers = (stats or {}).get('peers')
        message = L(30080)
        if peers is not None:
            speed = _human_size((stats or {}).get('downloadSpeed') or 0)
            message += '\n' + L(30082) % (speed, peers)
        dialog.update(0, message)

        if monitor.waitForAbort(1.0):
            return UNKNOWN_FILE_IDX, url, elapsed, False
        elapsed += 1

    return UNKNOWN_FILE_IDX, url, elapsed, True


def _prebuffer_torrent(server, stream, url):
    """Warm the torrent engine and show progress before playback starts.

    Only called for torrent streams (`infoHash` present) once the server is
    already known available. Returns `(proceed, url)`: `proceed` is False
    only when the user cancelled the dialog (caller must resolve False);
    `url` is the original url, or the rebuilt one when the server had to
    guess the file index. ANY unexpected error degrades to `(True, url)` -
    a broken pre-buffer must never block playback.
    """
    buffer_enable = setting_bool('buffer_enable', True)
    log(
        'player: pre-buffer entry: buffer_enable=%s fileIdx=%r' % (buffer_enable, stream.get('fileIdx')),
        xbmc.LOGINFO,
    )
    if not buffer_enable:
        return True, url

    info_hash = stream['infoHash']
    behavior_hints = stream.get('behaviorHints') or {}
    title = behavior_hints.get('filename') or stream.get('title') or stream.get('name') or ''
    dialog = xbmcgui.DialogProgress()
    try:
        dialog.create(L(30080), title)

        file_idx = stream.get('fileIdx')
        if file_idx is None:
            file_idx = UNKNOWN_FILE_IDX

        monitor = xbmc.Monitor()
        elapsed = 0
        if file_idx == UNKNOWN_FILE_IDX:
            file_idx, url, elapsed, proceed = _await_file_idx(
                server, stream, info_hash, url, dialog, monitor, elapsed
            )
            if not proceed:
                return False, url
            if file_idx == UNKNOWN_FILE_IDX:
                # Metadata never arrived within budget; nothing to poll
                # per-file stats for, so just start playback.
                notify(L(30083))
                return True, url
        else:
            server.create_engine(info_hash)

        buffer_mb = setting_int('buffer_mb', 20, minimum=5)
        configured_target = buffer_mb * 1024 * 1024
        log(
            'player: pre-buffer target: buffer_mb=%d target_bytes=%d' % (buffer_mb, configured_target),
            xbmc.LOGINFO,
        )
        while elapsed < _BUFFER_MAX_WAIT_SECONDS:
            if dialog.iscanceled():
                return False, url

            try:
                stats = server.file_stats(info_hash, file_idx)
            except Exception as exc:  # noqa: BLE001 - a stats hiccup must not brick playback
                log('player: buffer stats failed for %s: %r' % (info_hash, exc), xbmc.LOGWARNING)
                return True, url

            buffered = buffered_bytes(stats)
            stream_len = (stats or {}).get('streamLen') or 0
            target = min(configured_target, stream_len) if stream_len else configured_target

            percent = min(100, buffered * 100 // target) if target else 100
            speed = _human_size((stats or {}).get('downloadSpeed') or 0)
            peers = (stats or {}).get('peers') or 0
            dialog.update(
                percent,
                L(30081) % (_human_size(buffered), _human_size(target)) + '\n' + L(30082) % (speed, peers)
            )

            if buffered >= target:
                log(
                    'player: pre-buffer complete for %s: buffered=%d target=%d' % (info_hash, buffered, target),
                    xbmc.LOGINFO,
                )
                return True, url

            if monitor.waitForAbort(1.0):
                return False, url
            elapsed += 1

        log('player: pre-buffer timed out for %s after %ds' % (info_hash, elapsed), xbmc.LOGINFO)
        notify(L(30083))
        return True, url
    except Exception as exc:  # noqa: BLE001 - pre-buffer is a bonus, never fatal
        log('player: pre-buffer failed for %s: %r' % (stream.get('infoHash'), exc), xbmc.LOGWARNING)
        return True, url
    finally:
        dialog.close()


def play(handle, stream, stype, sid):
    """Resolve `stream` (Stremio Stream object for content `stype`/`sid`)."""
    stream = stream or {}

    server = _server_client()
    if any(key in stream for key in _SERVER_DEPENDENT_KEYS) and not server.is_available():
        notify(L(30031))
        xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem())
        return

    try:
        url = server.resolve_stream(stream)
    except Exception as exc:  # noqa: BLE001 - a broken server response must not crash Kodi
        log('player: resolve_stream failed for %s/%s: %r' % (stype, sid, exc), xbmc.LOGERROR)
        url = None

    if not url:
        notify(L(30030))
        xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem())
        return

    if stream.get('infoHash'):
        proceed, url = _prebuffer_torrent(server, stream, url)
        if not proceed:
            xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem())
            return

    behavior_hints = stream.get('behaviorHints') or {}
    request_headers = (behavior_hints.get('proxyHeaders') or {}).get('request') or {}
    if request_headers:
        # Kodi convention: "|urlencoded=headers" appended to the path makes
        # the player send these headers with every request for that URL.
        url = '%s|%s' % (url, urlencode(request_headers))

    list_item = xbmcgui.ListItem(path=url)
    filename = behavior_hints.get('filename')
    if filename:
        list_item.setLabel(filename)

    _attach_subtitles(list_item, behavior_hints, stype, sid)

    xbmcplugin.setResolvedUrl(handle, True, list_item)
