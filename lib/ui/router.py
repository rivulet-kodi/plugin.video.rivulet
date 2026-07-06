"""Plugin URL router: sys.argv -> action dispatch.

Kodi invokes default.py with argv = [base_url, handle, "?querystring"].
Everything here is UI glue only; the actual list-building/playback logic
lives in views.py / player.py.
"""
import base64
import json
import os
import sys
from urllib.parse import parse_qs, urlencode

ADDON_HANDLE = -1
BASE_URL = ''


def _parse_params(raw_qs):
    if raw_qs.startswith('?'):
        raw_qs = raw_qs[1:]
    if not raw_qs:
        return {}
    return {key: values[0] for key, values in parse_qs(raw_qs).items()}


def url_for(action, **params):
    """Build a plugin:// URL for `action` with the given string params."""
    query = {'action': action}
    for key, value in params.items():
        if value is None or value == '':
            continue
        query[key] = value
    return '%s?%s' % (BASE_URL, urlencode(query))


def encode_stream(stream):
    """Base64url-encode a stream dict for safe passage inside a plugin URL."""
    payload = json.dumps(stream or {}, separators=(',', ':')).encode('utf-8')
    return base64.urlsafe_b64encode(payload).decode('ascii')


def decode_stream(token):
    """Inverse of encode_stream(); returns {} for empty/invalid input."""
    if not token:
        return {}
    padded = token + '=' * (-len(token) % 4)
    try:
        payload = base64.urlsafe_b64decode(padded.encode('ascii'))
        return json.loads(payload.decode('utf-8'))
    except (ValueError, TypeError):
        return {}


def run():
    """Entry point called by default.py."""
    global ADDON_HANDLE, BASE_URL
    # Deferred imports: views/player pull in xbmcgui/xbmcplugin and, more
    # importantly, `from lib.ui import router` themselves — importing them
    # eagerly at module scope here would form an import cycle.
    import xbmc

    from lib.ui import player, views
    from lib.ui.compat import log

    BASE_URL = sys.argv[0] if len(sys.argv) > 0 else 'plugin://plugin.video.rivulet/'
    try:
        ADDON_HANDLE = int(sys.argv[1])
    except (IndexError, ValueError):
        ADDON_HANDLE = -1

    params = _parse_params(sys.argv[2] if len(sys.argv) > 2 else '')
    action = params.get('action', 'home')

    def do_play(p):
        stream = decode_stream(p.get('stream'))
        player.play(ADDON_HANDLE, stream, p.get('type'), p.get('id'))

    dispatch = {
        'home': lambda p: views.home(),
        'discover': lambda p: views.discover(),
        'catalog': lambda p: views.catalog(
            p.get('transport'), p.get('type'), p.get('id'), p.get('extra')
        ),
        'showcase': lambda p: views.showcase(
            p.get('transport'), p.get('type'), p.get('id'), p.get('extra')
        ),
        'search': lambda p: views.search(),
        'meta': lambda p: views.meta(p.get('type'), p.get('id')),
        'videos': lambda p: views.videos(p.get('type'), p.get('id'), p.get('season')),
        'streams': lambda p: views.streams(p.get('type'), p.get('id')),
        'play': do_play,
        'addons': lambda p: views.addons(),
        'addon_install': lambda p: views.addon_install(),
        'addon_remove': lambda p: views.addon_remove(p.get('transport')),
        'login': lambda p: views.login(),
        'logout': lambda p: views.logout(),
        'library': lambda p: views.library(),
        'settings': lambda p: views.open_settings(),
        'server_download': lambda p: _download_server_binary(),
        'advancedsettings_install': lambda p: _install_advancedsettings(),
        'sync_addons_now': lambda p: views.sync_addons_now(),
    }

    handler = dispatch.get(action, dispatch['home'])
    try:
        handler(params)
    except Exception as exc:  # noqa: BLE001 - last-resort guard, must never crash Kodi
        log('router: action "%s" failed: %r' % (action, exc), xbmc.LOGERROR)
        _fail_gracefully(action)


def _download_server_binary():
    """Action 'server_download': fetch+install the stremio-server-go binary
    into the location lib.service_runner.resolve_binary() already searches.
    """
    import xbmc
    import xbmcgui

    from lib import serverbin
    from lib.ui.compat import L, addon_profile_dir, log, notify

    dest_dir = os.path.join(addon_profile_dir(), 'bin')

    dialog = xbmcgui.DialogProgress()
    dialog.create(L(30061))

    def progress_cb(done, total):
        if dialog.iscanceled():
            raise serverbin.DownloadError('cancelled by user')
        percent = int(done * 100 / total) if total else 0
        dialog.update(min(percent, 100), L(30061))

    try:
        path = serverbin.install_binary(dest_dir, progress_cb=progress_cb)
    except serverbin.NoAssetError as exc:
        log('router: server_download: %s' % exc, xbmc.LOGWARNING)
        notify(L(30064))
    except serverbin.DownloadError as exc:
        log('router: server_download failed: %s' % exc, xbmc.LOGERROR)
        notify(L(30063))
    else:
        notify(L(30062) % path)
    finally:
        dialog.close()


def _install_advancedsettings():
    """Action 'advancedsettings_install': install the addon's bundled
    resources/advancedsettings.xml template into the user's Kodi userdata
    dir (special://masterprofile/advancedsettings.xml) so its generous
    cURL timeouts + streaming cache apply globally - opt-in, and never
    overwrites an advancedsettings.xml the user or another addon already
    placed there.
    """
    import xbmc
    import xbmcvfs

    from lib import advancedsettings
    from lib.ui.compat import ADDON_ID, L, log, notify

    source = xbmcvfs.translatePath(
        'special://home/addons/%s/resources/advancedsettings.xml' % ADDON_ID
    )
    dest = xbmcvfs.translatePath('special://masterprofile/advancedsettings.xml')

    try:
        status = advancedsettings.install(source, dest)
    except advancedsettings.AdvancedSettingsError as exc:
        log('router: advancedsettings_install failed: %s' % exc, xbmc.LOGERROR)
        notify(L(30068))
        return

    if status == advancedsettings.STATUS_EXISTS:
        notify(L(30067))
    else:
        notify(L(30066))


def _fail_gracefully(action):
    import xbmcgui
    import xbmcplugin

    from lib.ui.compat import L, notify

    notify(L(30032))
    if action == 'play':
        xbmcplugin.setResolvedUrl(ADDON_HANDLE, False, xbmcgui.ListItem())
    else:
        xbmcplugin.endOfDirectory(ADDON_HANDLE, succeeded=False)
