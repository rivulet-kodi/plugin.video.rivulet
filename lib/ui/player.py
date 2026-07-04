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
from lib.stremio.server import ServerClient
from lib.stremio.subtitles import collect_subtitles, sort_subtitles
from lib.ui.compat import ADDON, L, addon_profile_dir, log, notify

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
    if not ADDON.getSettingBool('subs_enable'):
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
