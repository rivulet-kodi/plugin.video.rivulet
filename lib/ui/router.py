# SPDX-FileCopyrightText: 2026 M0Rf30
# SPDX-License-Identifier: MIT

"""Plugin URL router: sys.argv -> action dispatch.

Kodi invokes default.py with argv = [base_url, handle, "?querystring"].
Everything here is UI glue only; the actual list-building/playback logic
lives in views.py / player.py. Pure URL-building/stream-token logic
(url_for/decode_stream) needs no dispatch state and lives in
lib.ui.urlutil instead; this module keeps only the mutable
ADDON_HANDLE/BASE_URL dispatch state and the querystring parser.
"""
import os
import sys
import time
from urllib.parse import parse_qs

ADDON_HANDLE = -1
BASE_URL = ''

#: Minimum gap between two `deliver_queued_input()` calls while the server
#: binary downloads (same value and reasoning as `player._CANCEL_PUMP_SECONDS`).
_CANCEL_PUMP_SECONDS = 0.25


def _parse_params(raw_qs):
    if raw_qs.startswith('?'):
        raw_qs = raw_qs[1:]
    if not raw_qs:
        return {}
    return {key: values[0] for key, values in parse_qs(raw_qs).items()}


def run():
    """Entry point called by default.py."""
    global ADDON_HANDLE, BASE_URL
    # Deferred imports: xbmc is needed unconditionally for the error-log
    # path below; player/views/urlutil are each pulled in lazily by only
    # the specific dispatch closures that use them (do_play,
    # _view_action()) - every plugin:// call is a FRESH Python process
    # (native library resume, favourites, widgets, Settings RunPlugin
    # buttons), so an action like 'play' never pays for views.py's import
    # cost (~3.7ms on an i7, 5-10x that on a Pi) and login/logout/
    # settings/sync_addons_now/server_download/advancedsettings_install
    # never pay for player.py's (~1.3ms). Also avoids the import cycle
    # views/player would form by themselves importing `from lib.ui import
    # router` at their own module scope.
    import xbmc

    from lib.ui.compat import log

    BASE_URL = sys.argv[0] if len(sys.argv) > 0 else 'plugin://plugin.video.rivulet/'
    try:
        ADDON_HANDLE = int(sys.argv[1])
    except (IndexError, ValueError):
        ADDON_HANDLE = -1

    params = _parse_params(sys.argv[2] if len(sys.argv) > 2 else '')
    action = params.get('action', 'home')

    def do_play(p):
        from lib.ui import player, urlutil
        stream = urlutil.decode_stream(p.get('stream'))
        player.play(ADDON_HANDLE, stream, p.get('type'), p.get('id'))

    def _view_action(name):
        """Build a dispatch closure calling `views.<name>()`, importing
        lib.ui.views only when the closure actually runs - so actions that
        never touch it (do_play, server_download, advancedsettings_install)
        never pay for its import."""
        def _dispatch(p):
            from lib.ui import views
            getattr(views, name)()
        return _dispatch

    dispatch = {
        'home': _view_action('home'),
        'play': do_play,
        'login': _view_action('login'),
        'logout': _view_action('logout'),
        'settings': _view_action('open_settings'),
        'server_download': lambda p: _download_server_binary(),
        'advancedsettings_install': lambda p: _install_advancedsettings(),
        'sync_addons_now': _view_action('sync_addons_now'),
        'pair_key': lambda p: _pair_key(p.get('setting')),
        'refresh_cache': lambda p: _refresh_cache(),
        'home_lists': lambda p: _home_lists(),
    }

    # Any action not in this dict - unrecognized, or a favourite/.strm URL
    # saved against an action a later release deleted - falls back to
    # 'home'. views.home() is now a minimal recovery directory (not a
    # classical listing replacement), so this fallback lands the user on a
    # screen that explains something went wrong and offers Settings,
    # rather than raising or dead-ending.
    handler = dispatch.get(action, dispatch['home'])
    try:
        handler(params)
    except Exception as exc:  # noqa: BLE001 - last-resort guard, must never crash Kodi
        log('router: action "%s" failed: %r' % (action, exc), xbmc.LOGERROR)
        _fail_gracefully(action)


def _refresh_cache():
    """Action 'refresh_cache': Settings > Home screen > Refresh cached
    data (`lib.ui.cacherefresh`)."""
    from lib.ui.cacherefresh import refresh_from_settings
    refresh_from_settings()


def _pair_key(setting=None):
    """Action 'pair_key': the Settings button that shows a code and a QR.

    Imported here so a playback launch never loads the dialog.
    """
    from lib.ui.compat import L
    from lib.ui.pairwindow import PAIRABLE, open_pair_picker, pair_setting

    for setting_id, label_id, _url, _line_id in PAIRABLE:
        if setting == setting_id:
            pair_setting(setting_id, L(label_id))
            return
    open_pair_picker()


def _download_server_binary():
    """Action 'server_download': fetch+install the rivulet-engine binary
    into the location lib.service_runner.resolve_binary() already searches
    (serverbin.install_dir()'s pick -- the Android app-private bin dir when
    applicable, else the plain addon_data bin dir).
    """
    import xbmc

    from lib import serverbin
    from lib.ui.compat import ADDON_ID, L, addon_profile_dir, log, notify
    from lib.ui.dialogs import RivuletProgress
    from lib.ui.uicommon import deliver_queued_input

    dest_dir = serverbin.install_dir(addon_profile_dir(), ADDON_ID)

    dialog = RivuletProgress()
    dialog.create(L(30061))
    last_pump = time.monotonic()

    def progress_cb(done, total):
        # The download runs on this (script) thread, and Kodi hands it a
        # queued Back press only from inside `Monitor.waitForAbort()`, which
        # nothing here calls: without the pump `iscanceled()` stayed False
        # until the download finished. Throttled because the callback fires
        # per chunk and four pumps a second still honour Back inside ~0.25 s.
        nonlocal last_pump
        now = time.monotonic()
        if now - last_pump >= _CANCEL_PUMP_SECONDS:
            last_pump = now
            if deliver_queued_input():
                raise serverbin.DownloadError('Kodi is shutting down')
        if dialog.iscanceled():
            raise serverbin.DownloadError('cancelled by user')
        percent = int(done * 100 / total) if total else 0
        dialog.update(min(percent, 100), L(30061))

    try:
        path = serverbin.install_binary(dest_dir, progress_cb=progress_cb)
    except serverbin.UnsupportedPlatformError as exc:
        log('router: server_download: %s' % exc, xbmc.LOGWARNING)
        notify(L(30091))
    except serverbin.NoAssetError as exc:
        log('router: server_download: %s' % exc, xbmc.LOGWARNING)
        os_name, arch = serverbin.platform_key()
        notify('%s (%s/%s)' % (L(30064), os_name, arch))
    except serverbin.DownloadError as exc:
        log('router: server_download failed: %s' % exc, xbmc.LOGERROR)
        notify(L(30063))
    else:
        notify(L(30062) % path)
    finally:
        dialog.close()


def _install_advancedsettings():
    """Action 'advancedsettings_install': install the addon's bundled
    advancedsettings template into the user's Kodi userdata dir
    (special://masterprofile/advancedsettings.xml) so its cURL timeouts
    (and, on Kodi 19/20, its video cache) apply globally - opt-in, and
    never overwrites an advancedsettings.xml the user or another addon
    already placed there.

    Kodi 21 moved the <cache> tags to Settings > Services > Caching and
    ignores them in this file, so it gets the template without them and
    a toast naming where the cache went. Kodi reads the active profile's
    own advancedsettings.xml instead of the master profile's when one
    exists, so that file counts as already installed too.
    """
    import xbmc
    import xbmcvfs

    from lib import advancedsettings
    from lib.ui.compat import ADDON, L, kodi_major_version, log, notify

    cache_in_gui = kodi_major_version() >= 21
    template = 'advancedsettings_kodi21.xml' if cache_in_gui else 'advancedsettings.xml'
    # The add-on's own install path, wherever Kodi found it (user, system
    # or portable add-on dir), not a guess under special://home/addons.
    source = os.path.join(xbmcvfs.translatePath(ADDON.getAddonInfo('path')), 'resources', template)
    dest = xbmcvfs.translatePath('special://masterprofile/advancedsettings.xml')
    profile = xbmcvfs.translatePath('special://profile/advancedsettings.xml')

    try:
        status = advancedsettings.install(source, dest, shadow_paths=(profile,))
    except advancedsettings.AdvancedSettingsError as exc:
        log('router: advancedsettings_install failed: %s' % exc, xbmc.LOGERROR)
        notify(L(30068))
        return

    if status == advancedsettings.STATUS_EXISTS:
        notify(L(30067))
    else:
        notify(L(30627) if cache_in_gui else L(30066))


def _fail_gracefully(action):
    import xbmcgui
    import xbmcplugin

    from lib.ui.compat import L, notify

    notify(L(30032))
    if action == 'play':
        xbmcplugin.setResolvedUrl(ADDON_HANDLE, False, xbmcgui.ListItem())
    else:
        xbmcplugin.endOfDirectory(ADDON_HANDLE, succeeded=False)


def _home_lists():
    """Settings action; import dialogs only when the configurator opens."""
    from lib.ui.homelists import open_configurator

    open_configurator()
