"""Recorder + configurable fake objects shared by every fake xbmc* module.

`Env` is the one recorder instance a single `install_kodi_stubs()` call
threads through all five fake modules; tests assert on `env.<field>`
rather than poking at the fakes themselves. `FakeAddon` is a configurable
stand-in for `xbmcaddon.Addon()`. `FakeListItem`/`FakeInfoTag` stand in
for `xbmcgui.ListItem` and its Kodi->=20 `InfoTagVideo` metadata object.
"""

# Default addon-info dict (`xbmcaddon.Addon.getAddonInfo`), matching the
# addon's real addon.xml identity.
_DEFAULT_ADDON_INFO = {
    'id': 'plugin.video.rivulet',
    'name': 'Rivulet',
    'icon': 'special://home/addons/plugin.video.rivulet/icon.png',
    'fanart': 'special://home/addons/plugin.video.rivulet/fanart.jpg',
}

# Default settings dict (`xbmcaddon.Addon.getSetting`/getSettingBool/
# getSettingInt), covering every key lib/ui/player.py's pre-buffer/
# resume flow reads. Any key not read by the code under test is simply
# inert.
_DEFAULT_SETTINGS = {
    'server_url': '',
    'buffer_enable': True,
    'buffer_mb': 1,
    'subs_enable': False,
    'subs_language': 'en',
    'resume_ask': True,
}

# Default localized-string map (`xbmcaddon.Addon.getLocalizedString`). Only
# ids production code formats with `%` need real placeholders (see
# `_prebuffer_torrent`'s dialog.update() text, lib/ui's "Searching %s...",
# "Checking %s...", "Season %d" strings, streamswindow.py/detailwindow.py's
# singular/plural 'N SOURCE(S)'/'N ADDON(S)'/'N SEASON(S)'/'N EPISODE(S)'/
# 'N CACHED' summary labels, detailwindow.py's per-episode '%d%% WATCHED'
# readout, and streamswindow.py's singular/plural 'N source(s) hidden by
# filters' line); every other id not explicitly configured falls back to
# a deterministic 'STR<id>' marker (see `FakeAddon.getLocalizedString`)
# so assertions never need real strings.po text. 30189-30192, 30194,
# 30196, 30197, and 30209-30211 (select/back/exit) are plain (non-`%`)
# strings, so they deliberately stay out of this dict and fall through
# to their 'STR<id>' marker.
_DEFAULT_LOCALIZED = {
    30081: 'buffered %s of %s',
    30082: 'speed %s, %s peers',
    30183: 'Playing %s in %d s',
    30186: 'Searching %s...',
    30187: 'Checking %s...',
    30188: 'Season %d',
    30200: '%d SOURCE',
    30201: '%d SOURCES',
    30202: '%d ADDON',
    30203: '%d ADDONS',
    30204: '%d SEASON',
    30205: '%d SEASONS',
    30206: '%d EPISODE',
    30207: '%d EPISODES',
    30208: '%d CACHED',
    30212: '%d%% WATCHED',
    30262: '%d source hidden by filters',
    30263: '%d sources hidden by filters',
}


class FakeAddon:
    """Stand-in for `xbmcaddon.Addon()`: a configurable settings dict, an
    addon-info dict, and a localized-string map.

    `env` is the shared recorder this addon instance is bound to (only
    `openSettings()` currently writes back to it, as
    `env.opened_settings`). One `FakeAddon` is created per
    `install_kodi_stubs()` call and is the SAME object every
    `xbmcaddon.Addon()` call returns, so tests can mutate
    `env.addon.settings[...]` after setup and have every subsequent
    `getSetting()`/`getSettingBool()`/`getSettingInt()` see the change.
    """

    def __init__(self, env, settings=None, addon_info=None, localized=None):
        self._env = env
        self.settings = dict(_DEFAULT_SETTINGS)
        self.settings.update(settings or {})
        self.info = dict(_DEFAULT_ADDON_INFO)
        self.info.update(addon_info or {})
        self._localized = dict(_DEFAULT_LOCALIZED)
        self._localized.update(localized or {})

    def getSetting(self, key):
        value = self.settings.get(key, '')
        return '' if value is None else str(value)

    def setSetting(self, key, value):
        """Write a setting, as `xbmcaddon.Addon.setSetting()` does. Real
        Kodi stores everything as a string, so this does too - a test that
        writes `'false'` must read back `'false'`, not `False`."""
        self.settings[key] = '' if value is None else str(value)

    def getSettingBool(self, key):
        return bool(self.settings.get(key, False))

    def getSettingInt(self, key):
        return int(self.settings.get(key, 0))

    def getLocalizedString(self, string_id):
        return self._localized.get(string_id, 'STR%d' % string_id)

    def getAddonInfo(self, key):
        return self.info.get(key, '')

    def openSettings(self):
        self._env.opened_settings = True


class Env:
    """Records every xbmc*/xbmcgui/xbmcplugin call a unit under test makes,
    and carries the scripted behavior (dialog cancellation, Monitor abort,
    queued Dialog.input() answers) the fakes below consult. One fresh
    `Env` is created per `install_kodi_stubs()` call.

    `cancel`, `monitor_abort`, `monitor_abort_requested`, and
    `player_is_playing` may each be a plain bool (fixed answer for every
    call) or a callable taking the 1-based call count and returning a
    truthy/falsy value, for tests that need cancellation/abort/
    playback-state to change only after N attempts. `player_end_reason`
    ('ended' or 'stopped') picks which of `onPlayBackEnded()`/
    `onPlayBackStopped()` the fake `Player` dispatches on itself when a
    poll transitions from playing to not-playing.
    """

    def __init__(self, cancel=False, monitor_abort=False, monitor_abort_requested=False):
        # xbmcplugin recorders
        self.directory_items = []   # [{'handle', 'items', 'totalItems'}]
        self.end_of_directory = []  # [{'handle', 'succeeded', 'updateListing', 'cacheToDisc'}]
        self.content = []           # [(handle, content)]
        self.plugin_category = []   # [(handle, category)]
        self.sort_methods = []      # [(handle, sortMethod)]
        self.resolved = []          # [(handle, succeeded, list_item)]

        # xbmcgui.Dialog / DialogProgress recorders
        self.notifications = []         # [(heading, message, icon, time)]
        self.dialog_input_prompts = []  # [heading, ...]
        self.dialog_yesno_prompts = []  # [(heading, message), ...]
        self.dialog_created = []        # [(heading, message)]
        self.dialog_updates = []        # [(percent, message)]
        self.dialog_closed_count = 0

        # xbmc recorders
        self.log_calls = []          # [(msg, level)]
        self.executed_builtins = []  # [cmd, ...]
        self.monitor_abort_calls = 0
        self.monitor_abort_requested_calls = 0
        self.player_play_calls = []  # [(url, list_item)] - xbmc.Player().play() calls
        self.player_is_playing_calls = 0  # xbmc.Player().isPlaying() poll count
        self.player_get_time_calls = 0     # xbmc.Player().getTime() poll count
        self.player_get_total_time_calls = 0  # xbmc.Player().getTotalTime() poll count
        self.player_seek_calls = []        # [seek_seconds, ...] - xbmc.Player().seekTime() calls

        # xbmcaddon.Addon.openSettings recorder
        self.opened_settings = False

        # scripted behavior consulted by DialogProgress.iscanceled()/
        # xbmc.Monitor.waitForAbort()/xbmc.Player().isPlaying()
        self.cancel = cancel
        self.monitor_abort = monitor_abort
        self.monitor_abort_requested = monitor_abort_requested
        self.player_is_playing = False  # see streamswindow._wait_for_playback_end()
        self.player_end_reason = 'ended'  # 'ended' or 'stopped' - see fakes' Player.isPlaying()
        self.player_get_time = 0.0      # seconds - see lib.service_runner's progress player
        self.player_get_total_time = 0.0  # seconds - see lib.service_runner's progress player

        # bound by install_kodi_stubs() once the FakeAddon exists
        self.addon = None


class FakeInfoTag:
    """Records every InfoTagVideo setter call (the Kodi >=20 code path
    `lib.ui.compat.set_video_info()` takes via `ListItem.getVideoInfoTag()`).
    """

    def __init__(self):
        self.calls = {}

    def __getattr__(self, name):
        def setter(value):
            self.calls[name] = value
        return setter


class FakeListItem:
    """Stand-in for `xbmcgui.ListItem`: records label/art/property/info
    mutations (test_views.py's directory-listing assertions) as well as
    the `path`/subtitles/mimetype/content_lookup fields
    `lib.ui.player.play()` sets on the item it hands to
    `xbmcplugin.setResolvedUrl()` (test_player_buffer.py). `getProperty()`
    reads a property back, mirroring real `xbmcgui.ListItem` - needed by
    `lib.ui.infowindow.ShowcaseWindow`, which reads the `fanart`/
    `position` Properties back off the coverflow's focused/selected item.
    """

    def __init__(self, label='', label2='', path='', offscreen=False):
        self._label = label
        self.label2 = label2
        self.path = path
        self.offscreen = offscreen
        self.art = {}
        self.properties = {}
        self.legacy_info = {}
        self.subtitles = None
        self.context_menu_items = []
        self.info_tag = FakeInfoTag()
        self.mimetype = None
        self.content_lookup = None
        self.legacy_cast = None  # `xbmcgui.ListItem.setCast()` calls - Kodi 19 cast path
        self._selected = False

    def getLabel(self):
        return self._label

    def setLabel(self, label):
        self._label = label

    def setArt(self, art):
        self.art.update(art)

    def setProperty(self, key, value):
        self.properties[key] = value

    def setProperties(self, values):
        # Real xbmcgui.ListItem.setProperties(dict) - Kodi 19+ - sets many
        # Properties in one call; mirrors looping setProperty() per key
        # without clearing any property not passed in.
        self.properties.update(values)

    def getProperty(self, key):
        return self.properties.get(key, '')

    def setInfo(self, kind, info):
        assert kind == 'video'
        self.legacy_info.update(info)

    def setMimeType(self, value):
        self.mimetype = value

    def setContentLookup(self, enable):
        self.content_lookup = enable

    def getVideoInfoTag(self):
        return self.info_tag

    def setSubtitles(self, urls):
        self.subtitles = urls

    def addContextMenuItems(self, items, replaceItems=False):
        if replaceItems:
            self.context_menu_items = list(items)
        else:
            self.context_menu_items.extend(items)

    def setCast(self, actors):
        # Real `xbmcgui.ListItem.setCast(actors)` - Kodi 19's legacy cast
        # API (Kodi >=20 uses `InfoTagVideo.setCast()` via `FakeInfoTag`
        # instead - see `lib.ui.compat.set_video_cast()`).
        self.legacy_cast = actors

    def select(self, selected):
        self._selected = bool(selected)

    def isSelected(self):
        return self._selected
