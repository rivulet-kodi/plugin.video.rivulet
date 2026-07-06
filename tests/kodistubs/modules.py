"""Factory functions building fresh, per-invocation `xbmc`/`xbmcgui`/
`xbmcplugin`/`xbmcaddon`/`xbmcvfs` fake module objects, all wired to a
single shared `Env` recorder (see `fakes.py`).

Each `make_*` function returns a brand-new `types.ModuleType` so two
concurrent `install_kodi_stubs()` calls (e.g. under `pytest-xdist`, which
runs each worker in its own process, or nested calls within one test)
never share mutable module state.
"""
import types

from .fakes import FakeListItem


def make_xbmc(env, info_labels=None):
    module = types.ModuleType('xbmc')
    module.LOGDEBUG = 0
    module.LOGINFO = 1
    module.LOGWARNING = 2
    module.LOGERROR = 3

    labels = dict(info_labels or {})

    def log(msg, level=module.LOGDEBUG):
        env.log_calls.append((msg, level))

    def executebuiltin(function, wait=False):
        env.executed_builtins.append(function)

    def getInfoLabel(label):
        return labels.get(label, '')

    module.log = log
    module.executebuiltin = executebuiltin
    module.getInfoLabel = getInfoLabel

    class Monitor:
        def waitForAbort(self, timeout=None):
            env.monitor_abort_calls += 1
            abort = env.monitor_abort
            return bool(abort(env.monitor_abort_calls)) if callable(abort) else bool(abort)

    module.Monitor = Monitor
    return module


def make_xbmcgui(env, dialog_inputs=None, dialog_yesno=None):
    module = types.ModuleType('xbmcgui')
    module.NOTIFICATION_INFO = 'info'
    module.ALPHANUM_HIDE_INPUT = 1
    module.ListItem = FakeListItem

    inputs = list(dialog_inputs or [])
    yesno_answers = list(dialog_yesno or [])

    class Dialog:
        def input(self, heading, **kwargs):
            env.dialog_input_prompts.append(heading)
            return inputs.pop(0) if inputs else ''

        def yesno(self, heading, message, **kwargs):
            env.dialog_yesno_prompts.append((heading, message))
            # Exhausted queue defaults to False ("declined"), matching
            # input()'s falsy-on-exhaustion behavior above - a test that
            # forgets to script a confirmation for a destructive action
            # (e.g. views.addon_remove()) gets a loud, safe no-op instead
            # of a silently-approved action.
            return yesno_answers.pop(0) if yesno_answers else False

        def notification(self, heading, message, icon=None, time=4000):
            env.notifications.append((heading, message, icon, time))

    class DialogProgress:
        def create(self, heading, message=''):
            env.dialog_created.append((heading, message))

        def iscanceled(self):
            cancel = env.cancel
            return bool(cancel()) if callable(cancel) else bool(cancel)

        def update(self, percent, message=''):
            env.dialog_updates.append((percent, message))

        def close(self):
            env.dialog_closed_count += 1

    module.Dialog = Dialog
    module.DialogProgress = DialogProgress
    return module


def make_xbmcplugin(env):
    module = types.ModuleType('xbmcplugin')
    module.SORT_METHOD_NONE = 0

    def addDirectoryItems(handle, items, totalItems):
        env.directory_items.append({'handle': handle, 'items': list(items), 'totalItems': totalItems})
        return True

    def setContent(handle, content):
        env.content.append((handle, content))

    def setPluginCategory(handle, category):
        env.plugin_category.append((handle, category))

    def endOfDirectory(handle, succeeded=True, updateListing=False, cacheToDisc=True):
        env.end_of_directory.append({
            'handle': handle, 'succeeded': succeeded,
            'updateListing': updateListing, 'cacheToDisc': cacheToDisc,
        })

    def addSortMethod(handle, sortMethod):
        env.sort_methods.append((handle, sortMethod))

    def setResolvedUrl(handle, succeeded, listitem):
        env.resolved.append((handle, succeeded, listitem))

    module.addDirectoryItems = addDirectoryItems
    module.setContent = setContent
    module.setPluginCategory = setPluginCategory
    module.endOfDirectory = endOfDirectory
    module.addSortMethod = addSortMethod
    module.setResolvedUrl = setResolvedUrl
    return module


def make_xbmcaddon(env):
    module = types.ModuleType('xbmcaddon')
    # Every xbmcaddon.Addon() call returns the SAME env.addon instance
    # (matching real Kodi: the addon only ever constructs its own Addon()
    # once, at lib.ui.compat module scope) so tests can mutate
    # `env.addon.settings[...]` after setup and have it observed anywhere.
    module.Addon = lambda *a, **k: env.addon
    return module


def make_xbmcvfs():
    module = types.ModuleType('xbmcvfs')

    def translatePath(path):
        if path.startswith('special://'):
            return '/fake-kodi-home/' + path[len('special://'):]
        return path

    module.translatePath = translatePath
    module.exists = lambda path: True
    module.mkdirs = lambda path: True
    return module
