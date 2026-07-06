"""Shared fake xbmc*/xbmcgui/xbmcplugin/xbmcaddon/xbmcvfs stub modules for
the addon's Kodi-facing layer (`lib.ui.*`), plus a robust install/restore
mechanism for injecting them into `sys.modules` around a test.

This is the single, configurable source of truth the xbmc-dependent test
files (`tests/test_views.py`, `tests/test_player_buffer.py`) build on - no
test file hand-rolls `types.ModuleType('xbmc', ...)` fakes.

Usage - either the `kodi_stubs` fixture in `tests/conftest.py` (a thin
wrapper managing the context manager's lifetime per-test), or directly::

    from tests.kodistubs import install_kodi_stubs

    with install_kodi_stubs(reload=('lib.ui.compat', 'lib.ui.player')) as ctx:
        ctx.player.play(handle, stream, stype, sid)
        assert ctx.env.resolved == [...]
"""
from .fakes import Env, FakeAddon, FakeInfoTag, FakeListItem
from .install import STUB_MODULE_NAMES, install_kodi_stubs
from .modules import make_xbmc, make_xbmcaddon, make_xbmcgui, make_xbmcplugin, make_xbmcvfs

__all__ = [
    'Env',
    'FakeAddon',
    'FakeInfoTag',
    'FakeListItem',
    'STUB_MODULE_NAMES',
    'install_kodi_stubs',
    'make_xbmc',
    'make_xbmcaddon',
    'make_xbmcgui',
    'make_xbmcplugin',
    'make_xbmcvfs',
]
