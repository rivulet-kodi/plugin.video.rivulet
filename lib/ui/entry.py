# SPDX-FileCopyrightText: 2026 M0Rf30
# SPDX-License-Identifier: MIT

"""Body of the plugin entry point; ``default.py`` only bootstraps ``sys.path``
and calls :func:`run`.

Kodi invokes ``default.py`` directly (never as an import), passing the
plugin:// invocation as ``sys.argv``. The logic lives here so it is
importable under test and so ``default.py`` stays a few lines (the
add-on checker flags a "complex entry point" past 15 logical lines).

A bare invocation - clicking the add-on - is Rivulet's own app UI: the
custom HomeWindow. Every other action (context-menu RunPlugin callbacks,
Settings buttons, ``play``, ...) is handed to the router.

Only ``router`` and ``urlutil`` are imported at module scope. ``xbmc``,
``xbmcplugin`` and the Home/uicommon modules are imported inside
:func:`_open_home_or_recover`, so a non-home action never pays for them
(the same measured startup optimisation as the router's lazy handlers).
"""
from urllib.parse import parse_qs

from lib.ui import router, urlutil


def _launch_action_name(argv):
    """The ``action`` query parameter of the plugin:// invocation in
    ``argv`` (``[base_url, handle, '?query']``); ``'home'`` when absent."""
    raw_qs = argv[2] if len(argv) > 2 else ''
    if raw_qs.startswith('?'):
        raw_qs = raw_qs[1:]
    return parse_qs(raw_qs).get('action', ['home'])[0]


def run(argv):
    """Dispatch the invocation described by ``argv`` (``sys.argv``)."""
    router.BASE_URL = argv[0] if len(argv) > 0 else 'plugin://plugin.video.rivulet/'
    try:
        router.ADDON_HANDLE = int(argv[1])
    except (IndexError, ValueError):
        router.ADDON_HANDLE = -1

    if _launch_action_name(argv) == 'home':
        _open_home_or_recover()
    else:
        router.run()


def _recover_home(exc):
    """Fall back to the minimal Settings directory after a failed Home."""
    import xbmc

    from lib.stremio.addons import addon_error_detail
    from lib.ui.compat import log

    log('default: HomeWindow failed to open: %s' % addon_error_detail(exc), xbmc.LOGERROR)
    # The original handle is spent. Recovery needs a fresh invocation,
    # whose unregistered action routes to the minimal Settings directory.
    fallback_url = urlutil.url_for(router.BASE_URL, 'home_classical')
    xbmc.executebuiltin('Container.Update(%s)' % fallback_url)


def _open_home_or_recover():
    import xbmc
    import xbmcplugin

    from lib.ui.compat import log

    # Kodi's GetDirectory contract for this handle must be satisfied exactly
    # once (an empty, instant directory) before the custom window takes over
    # the screen - see lib.ui.uicommon's module docstring.
    xbmcplugin.endOfDirectory(
        router.ADDON_HANDLE, succeeded=True, updateListing=False, cacheToDisc=False
    )

    try:
        from lib.ui.homewindow import HomeOpenError, open_home
        from lib.ui.uicommon import (
            LAUNCH_IGNORE,
            LAUNCH_SHOW,
            dismiss_busy_dialog,
            launch_action,
            leave_launch_directory,
            request_ui_show,
        )
    except Exception as exc:  # import failure: no Home can be constructed
        _recover_home(exc)
        return

    verdict = None
    try:
        try:
            verdict = launch_action()
            if verdict == LAUNCH_SHOW:
                # The user left the fullscreen video and opened Rivulet while
                # the running UI waits out the playback under its force-closed
                # screens (issue #73): wake that UI rather than stack a second
                # one. See lib.ui.uicommon's "UI phases" notes.
                log(
                    'default: Rivulet UI is waiting on playback - asking it to come back',
                    xbmc.LOGINFO,
                )
                request_ui_show()
            elif verdict == LAUNCH_IGNORE:
                # Kodi re-ran the plugin root (it re-fetches the Videos container
                # the addon was started from whenever that window is activated
                # again - e.g. when playback stops: kodi.log 01:07:59.987 ->
                # 01:08:00.189) while another interpreter's UI is alive. The
                # directory handle is satisfied above; opening Home here would
                # stack a second UI over the restored picker, and
                # dismiss_busy_dialog() would close that picker. See
                # lib.ui.uicommon's "UI phases" notes for the exact rule.
                log(
                    'default: a Rivulet UI is already running - ignoring bare launch',
                    xbmc.LOGINFO,
                )
            else:
                dismiss_busy_dialog()
        except Exception as exc:
            _recover_home(exc)
        else:
            if verdict not in (LAUNCH_SHOW, LAUNCH_IGNORE):
                open_home()
                leave_launch_directory()
    except HomeOpenError as exc:
        _recover_home(exc)
