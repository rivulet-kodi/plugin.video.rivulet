"""Plugin entry point for plugin.video.rivulet.

Kodi invokes this script directly (not as an import), passing the
plugin:// invocation as sys.argv. Bootstrap sys.path so the addon's own
``lib`` package is importable, then either open the custom HomeWindow
(a bare invocation - clicking the addon - is Rivulet's own app UI now)
or hand off to the router for every other action (context-menu
RunPlugin callbacks, Settings buttons, ``play``, the still-classical
``meta``/``videos``/``streams``/``library``/``addons`` directories, ...).
"""
import os
import sys
from urllib.parse import parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lib.ui import router

router.BASE_URL = sys.argv[0] if len(sys.argv) > 0 else 'plugin://plugin.video.rivulet/'
try:
    router.ADDON_HANDLE = int(sys.argv[1])
except (IndexError, ValueError):
    router.ADDON_HANDLE = -1

_raw_qs = sys.argv[2] if len(sys.argv) > 2 else ''
if _raw_qs.startswith('?'):
    _raw_qs = _raw_qs[1:]
_action = parse_qs(_raw_qs).get('action', ['home'])[0]

if _action == 'home':
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
        from lib.ui.homewindow import open_home
        from lib.ui.uicommon import dismiss_busy_dialog

        dismiss_busy_dialog()
        open_home()
    except Exception as exc:  # the custom UI must never leave the addon unusable
        log(
            'default: HomeWindow failed, falling back to classical home: %r' % (exc,),
            xbmc.LOGERROR,
        )
        # 'home_classical' is intentionally NOT a registered router action:
        # this is a FRESH plugin:// invocation (its own handle), and
        # router.run()'s dispatch falls back to views.home() (the classical
        # directory) for any unrecognized action - see router.run()'s
        # `dispatch.get(action, dispatch['home'])`. This handle was already
        # closed above, so recovery must go through a new invocation rather
        # than a second endOfDirectory() call on the same (spent) handle.
        xbmc.executebuiltin('Container.Update(%s)' % router.url_for('home_classical'))
else:
    router.run()
