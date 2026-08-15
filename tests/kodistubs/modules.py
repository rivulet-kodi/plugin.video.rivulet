"""Factory functions building fresh, per-invocation `xbmc`/`xbmcgui`/
`xbmcplugin`/`xbmcaddon`/`xbmcvfs` fake module objects, all wired to a
single shared `Env` recorder (see `fakes.py`).

Each `make_*` function returns a brand-new `types.ModuleType` so two
concurrent `install_kodi_stubs()` calls (e.g. under `pytest-xdist`, which
runs each worker in its own process, or nested calls within one test)
never share mutable module state.
"""
import os
import types
import xml.etree.ElementTree as ET

from .fakes import FakeListItem

#: tests/kodistubs/modules.py -> tests/kodistubs -> tests -> repo root, the
#: same layout tests/conftest.py's REPO_ROOT relies on.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SKIN_DIR = os.path.join(_REPO_ROOT, 'resources', 'skins', 'Default', '1080i')

#: xml filename -> frozenset of every id a <control id="..."> element
#: declares in resources/skins/Default/1080i/<filename>, or None for a
#: filename with no such file. Populated lazily by _declared_control_ids()
#: and never invalidated - the skin files don't change mid test-run, and
#: this is on WindowXML.__init__'s path for every window built across a
#: 1510-test suite, so parsing each file more than once would be wasted
#: work many times over.
_SKIN_CONTROL_IDS = {}


def _declared_control_ids(xml_name):
    """Return the set of ids `resources/skins/Default/1080i/<xml_name>`
    declares via `<control id="...">`, or None if `xml_name` is falsy or
    names a file that does not exist there.

    None is WindowXML's escape hatch, not an error: a test that builds
    the fake directly with no args (or a bogus name) never went near a
    real window's controls and must keep today's permissive
    getControl(), same as before this check existed."""
    if not xml_name:
        return None
    if xml_name in _SKIN_CONTROL_IDS:
        return _SKIN_CONTROL_IDS[xml_name]
    path = os.path.join(_SKIN_DIR, xml_name)
    if not os.path.isfile(path):
        ids = None
    else:
        ids = frozenset(
            int(control.get('id'))
            for control in ET.parse(path).getroot().iter('control')
            if control.get('id') is not None
        )
    _SKIN_CONTROL_IDS[xml_name] = ids
    return ids


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

    #: Kodi's own core-string catalog (id < 1000), distinct from the addon's
    #: strings.po - only the two ids `dialogs.confirm()` callers pass for
    #: default Yes/No buttons are faked; anything else falls back to the
    #: same 'STR<id>' marker `FakeAddon.getLocalizedString` uses.
    _CORE_STRINGS = {106: 'No', 107: 'Yes'}

    def getLocalizedString(string_id):
        return _CORE_STRINGS.get(string_id, 'STR%d' % string_id)

    module.log = log
    module.executebuiltin = executebuiltin
    module.getInfoLabel = getInfoLabel
    module.getLocalizedString = getLocalizedString

    class Monitor:
        def waitForAbort(self, timeout=None):
            env.monitor_abort_calls += 1
            abort = env.monitor_abort
            return bool(abort(env.monitor_abort_calls)) if callable(abort) else bool(abort)

        def abortRequested(self):
            env.monitor_abort_requested_calls += 1
            requested = env.monitor_abort_requested
            return bool(requested(env.monitor_abort_requested_calls)) if callable(requested) else bool(requested)

    class Player:
        """Stand-in for `xbmc.Player()`: records every `.play(url, listitem)`
        call `lib.ui.player.play_direct()` makes on it - the custom-window
        direct-play path (no ADDON_HANDLE/xbmcplugin.setResolvedUrl
        involved). `isPlaying()`/`isPlayingVideo()` answer from
        `env.player_is_playing` (plain bool, or a callable taking the
        1-based call count - same convention as
        `Monitor.waitForAbort()`/`env.monitor_abort` above), for
        `lib.ui.streamswindow._wait_for_playback_end()`'s poll loop and
        `lib.service_runner`'s progress-tracking player.
        `getTime()`/`getTotalTime()` (seconds, matching the real
        `xbmc.Player` API) answer from `env.player_get_time`/
        `env.player_get_total_time` the same way; `seekTime()` just
        records its argument into `env.player_seek_calls`.

        Whenever an `isPlaying()` poll transitions from playing to
        not-playing, this dispatches `self.onPlayBackEnded()` or
        `self.onPlayBackStopped()` (per `env.player_end_reason`, default
        'ended') on ITSELF, exactly like real Kodi calls back a live
        `Player` instance - so a subclass overriding either (e.g.
        `streamswindow._PlaybackEndWatcher`, `lib.service_runner`'s
        `_RivuletPlayer`) sees it without the test driving the callback
        by hand. The base no-op implementations below make that dispatch
        safe when nothing overrides them. Transition state is tracked
        per INSTANCE (`self._was_playing`), matching real Kodi's
        per-Player callbacks - not on `env`, which is shared."""

        def __init__(self):
            self._was_playing = False

        def play(self, item='', listitem=None, windowed=False, startpos=-1):
            env.player_play_calls.append((item, listitem))

        def isPlaying(self):
            env.player_is_playing_calls += 1
            playing = env.player_is_playing
            result = bool(playing(env.player_is_playing_calls)) if callable(playing) else bool(playing)
            if self._was_playing and not result:
                if env.player_end_reason == 'stopped':
                    self.onPlayBackStopped()
                else:
                    self.onPlayBackEnded()
            self._was_playing = result
            return result

        def isPlayingVideo(self):
            return self.isPlaying()

        def getTime(self):
            env.player_get_time_calls += 1
            value = env.player_get_time
            return value(env.player_get_time_calls) if callable(value) else value

        def getTotalTime(self):
            env.player_get_total_time_calls += 1
            value = env.player_get_total_time
            return value(env.player_get_total_time_calls) if callable(value) else value

        def seekTime(self, seek_time):
            env.player_seek_calls.append(seek_time)

        def onPlayBackEnded(self):
            pass

        def onPlayBackStopped(self):
            pass

        def onPlayBackError(self):
            pass

    module.Monitor = Monitor
    module.Player = Player

    class Actor:
        """Stand-in for `xbmc.Actor` - a class only added in Kodi 20, used
        by `lib.ui.compat.set_video_cast()`'s Kodi >=20 path via
        `InfoTagVideo.setCast()`. Matches the real constructor signature
        and getters so tests assert on actual attribute values rather
        than `repr()`.
        """

        def __init__(self, name=None, role=None, order=None, thumbnail=None):
            self._name = name
            self._role = role
            self._order = order
            self._thumbnail = thumbnail

        def getName(self):
            return self._name

        def getRole(self):
            return self._role

        def getOrder(self):
            return self._order

        def getThumbnail(self):
            return self._thumbnail

    module.Actor = Actor
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
            # (e.g. `AddonsWindow`'s removal prompt) gets a loud, safe no-op
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

    class FakeWindowControl:
        """Stand-in for one WindowXML control (`getControl(id)`'s
        return value): records addItems()/setImage()/setVisible()/
        setLabel()/setText()/setPercent() calls. `getSelectedItem()`
        returns `self.items[self.selected_index]` (default 0) - a test
        scripts a scroll position by setting `.selected_index` before
        calling onAction()/onClick(). `reset()`/`selectItem()`/
        `getSelectedPosition()` stand in for `ControlList`'s same-named
        methods (added for DetailWindow's season bar - a control
        repopulated/re-selected at runtime, unlike every other fake user
        of this class so far). `setPercent()`/`getPercent()` stand in for
        `ControlProgress`'s same-named methods (added for
        lib.ui.dialogs' RivuletProgress/RivuletBusy/RivuletCountdown).

        `addItems()` resets the selection to 0, as the real ControlList
        does. That is not a detail worth faking for its own sake - it is
        the behaviour behind a real bug (a catalog page landing while the
        user scrolled the coverflow snapped them back to the first
        poster), which this fake previously could not reproduce because
        it left `selected_index` alone."""

        def __init__(self):
            self.items = []
            self.image = None
            self.visible = True
            self.selected_index = 0
            self.label = None
            self.text = None
            self.percent = None

        def addItems(self, items):
            self.items.extend(items)
            # Kodi moves the cursor back to the top when items are added;
            # a caller that needs to keep it must save/restore around
            # this call (see infowindow._apply_pending_pages()).
            self.selected_index = 0

        def reset(self):
            self.items = []

        def setImage(self, image):
            self.image = image

        def setVisible(self, visible):
            self.visible = visible

        def setLabel(self, label):
            self.label = label

        def setText(self, text):
            self.text = text

        def setPercent(self, percent):
            self.percent = percent

        def getPercent(self):
            return self.percent

        def getSelectedItem(self):
            if not self.items:
                return None
            return self.items[self.selected_index]

        def selectItem(self, position):
            self.selected_index = position

        def getSelectedPosition(self):
            return self.selected_index if self.items else -1

        def size(self):
            return len(self.items)

        def getListItem(self, position):
            # Real ControlList raises rather than returning None for an
            # out-of-range index; ShowcaseWindow._apply_enriched() bounds
            # its own lookups against size() on the strength of that.
            if not 0 <= position < len(self.items):
                raise ValueError('Index out of range')
            return self.items[position]

    class FakeAction:
        """Stand-in for `xbmcgui.Action`: only `getId()` is used by
        `lib.ui.infowindow.ShowcaseWindow.onAction()`; a test builds one
        directly (`xbmcgui.Action(action_id)`) to drive it."""

        def __init__(self, action_id):
            self._id = action_id

        def getId(self):
            return self._id

    class WindowXML:
        """Stand-in for `xbmcgui.WindowXML` (and `WindowXMLDialog`):
        enough surface to construct `lib.ui.infowindow.ShowcaseWindow`
        and drive its onInit()/onClick()/onAction() directly from a
        test. No real window ever opens - doModal() only counts calls;
        a test drives the modal's effect (e.g. a click) itself
        before/around it. `show()`/`.shown` stand in for the real
        non-blocking `WindowXMLDialog.show()` (used by
        lib.ui.dialogs' RivuletProgress/RivuletBusy/RivuletCountdown,
        which must never doModal() - see that module's docstring).

        `getControl()` enforces the same "Non-Existent Control" contract
        real Kodi does: `lib.ui.uicommon.open_window()` always calls
        `window_cls(xml_name, addon_skin_path(), 'Default', '1080i')`, so
        `args[0]` is the skin filename - resolved here to the set of ids
        that file's own <control id="..."> elements declare. Asking for
        any other id raises, exactly like a real device. This is what
        would have caught `RuntimeError: Non-Existent Control 30340/
        30341` before it shipped: ConfirmDialog/OptionListDialog wrote to
        controls before doModal() had loaded the window, a try/except
        swallowed the real exception, and the picker rendered empty."""

        def __init__(self, *args, **kwargs):
            self._controls = {}
            self._focus_id = None
            self.modal_calls = 0
            self.closed = False
            self.shown = False
            self._valid_control_ids = _declared_control_ids(args[0] if args else None)

        def getControl(self, control_id):
            if self._valid_control_ids is not None and control_id not in self._valid_control_ids:
                raise RuntimeError('Non-Existent Control %d' % control_id)
            return self._controls.setdefault(control_id, FakeWindowControl())

        def setFocusId(self, control_id):
            self._focus_id = control_id

        def getFocusId(self):
            return self._focus_id

        def show(self):
            self.shown = True

        def doModal(self):
            self.modal_calls += 1

        def close(self):
            self.closed = True

    module.Dialog = Dialog
    module.DialogProgress = DialogProgress
    module.WindowXML = WindowXML
    module.WindowXMLDialog = WindowXML
    module.Action = FakeAction
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
