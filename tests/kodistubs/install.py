"""Install/restore mechanism for injecting the fake xbmc* modules into
`sys.modules` around a test, and (re)importing the `lib.ui.*` modules that
need to bind against them.

`lib.ui.compat`/`lib.ui.router`/`lib.ui.views`/`lib.ui.player` all import
one or more of `xbmc`/`xbmcgui`/`xbmcplugin`/`xbmcaddon`/`xbmcvfs` at
module scope (`lib.ui.compat` even binds `ADDON = xbmcaddon.Addon()` at
import time), and none of those five modules exist off a real Kodi
runtime. `install_kodi_stubs()` injects fakes for them, evicts+reimports
the requested `lib.ui.*` modules fresh so they bind against the fakes,
and restores every bit of import-machinery state it touched - exactly,
even on a mid-test exception - so no other test file ever observes a
stubbed module.
"""
import contextlib
import importlib
import sys
import types

from .fakes import Env, FakeAddon
from .modules import make_xbmc, make_xbmcaddon, make_xbmcgui, make_xbmcplugin, make_xbmcvfs

#: The five Kodi modules that don't exist off a real Kodi runtime.
STUB_MODULE_NAMES = ('xbmc', 'xbmcgui', 'xbmcplugin', 'xbmcaddon', 'xbmcvfs')

#: Sentinel distinguishing "the parent package had no such attribute
#: before" from "it had the attribute set to None" when snapshotting a
#: `lib.ui.*` leaf attribute for restore.
_MISSING = object()


@contextlib.contextmanager
def install_kodi_stubs(reload=(), addon_info=None, settings=None, localized=None,
                        info_labels=None, dialog_inputs=None, dialog_yesno=None,
                        cancel=False, monitor_abort=False):
    """Inject fresh fake xbmc*/xbmcgui/xbmcplugin/xbmcaddon/xbmcvfs
    modules bound to a fresh `Env`, then (re)import every dotted module
    name in `reload` fresh against them.

    `reload` is the tuple of `lib.ui.*` dotted module names the caller
    needs freshly imported (e.g. `('lib.ui.compat', 'lib.ui.player')`) -
    it differs per test file, since each exercises a different slice of
    the Kodi-facing layer. The remaining keyword arguments configure the
    fakes (see `fakes.Env`/`fakes.FakeAddon`/`modules.make_xbmc`/
    `modules.make_xbmcgui`).

    Yields a `types.SimpleNamespace(env=<Env>, <leaf>=<module>, ...)`:
    one attribute per `reload` entry, keyed by its last dotted component
    (`'lib.ui.compat'` -> `.compat`), plus `.env`, the recorder every fake
    call was made against.

    On exit (including via an exception), every `sys.modules` entry this
    call touched - the 5 stub names plus every `reload` name - is
    restored to exactly what it was before (popped if it was absent).
    Because `from lib.ui import compat` (e.g. `lib/ui/views.py`) resolves
    via `getattr()` on the already-imported `lib.ui` package object
    *before* falling back to `sys.modules`, popping `sys.modules` alone
    would leave a stale, orphaned attribute for a sibling module to
    silently reuse; the leaf attribute Python's import machinery sets on
    the cached `lib.ui` package object for each `reload` name is
    snapshotted and restored too.
    """
    reload_names = tuple(reload)
    leaves = [name.rsplit('.', 1)[-1] for name in reload_names]
    parent_names = [name.rsplit('.', 1)[0] if '.' in name else None for name in reload_names]

    env = Env(cancel=cancel, monitor_abort=monitor_abort)
    env.addon = FakeAddon(env, settings=settings, addon_info=addon_info, localized=localized)

    fake_modules = {
        'xbmc': make_xbmc(env, info_labels=info_labels),
        'xbmcgui': make_xbmcgui(env, dialog_inputs=dialog_inputs, dialog_yesno=dialog_yesno),
        'xbmcplugin': make_xbmcplugin(env),
        'xbmcaddon': make_xbmcaddon(env),
        'xbmcvfs': make_xbmcvfs(),
    }

    saved_modules = {name: sys.modules.get(name) for name in STUB_MODULE_NAMES + reload_names}
    # Resolved once, up front, before anything is mutated: every current
    # `reload` name shares the same 'lib.ui' parent package object, which
    # is never itself evicted/replaced.
    parents = {pname: sys.modules.get(pname) for pname in parent_names if pname is not None}
    saved_attrs = {
        name: (getattr(parents[pname], leaf, _MISSING) if pname in parents else _MISSING)
        for name, leaf, pname in zip(reload_names, leaves, parent_names)
    }

    try:
        sys.modules.update(fake_modules)
        for name, leaf, pname in zip(reload_names, leaves, parent_names):
            sys.modules.pop(name, None)
            parent = parents.get(pname)
            if parent is not None and leaf in vars(parent):
                delattr(parent, leaf)

        reloaded = {leaf: importlib.import_module(name) for name, leaf in zip(reload_names, leaves)}
        yield types.SimpleNamespace(env=env, **reloaded)
    finally:
        for name, original in saved_modules.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original
        for name, leaf, pname in zip(reload_names, leaves, parent_names):
            parent = parents.get(pname)
            if parent is None:
                continue
            original_attr = saved_attrs[name]
            if original_attr is _MISSING:
                if hasattr(parent, leaf):
                    delattr(parent, leaf)
            else:
                setattr(parent, leaf, original_attr)
