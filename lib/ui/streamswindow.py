"""StreamsWindow: the resolved-source picker for one title/episode -
Rivulet's custom replacement for the classical `streams()` directory.

Picking a row resolves and plays it DIRECTLY via
`lib.ui.player.play_direct` (no ADDON_HANDLE/`setResolvedUrl` - see that
function's docstring), so Kodi's player takes over the full screen.
`open_streams()` returns True when playback actually started, and every
caller up the chain (`DetailWindow`, `CatalogPickerWindow`, `SearchWindow`
via `open_detail`) propagates that by closing itself too, so nothing
custom lingers behind the player once picked.
"""
import xbmcgui

from lib.store import Store
from lib.stremio import streaminfo
from lib.stremio.addons import AddonClient, AddonError, addon_supports
from lib.ui.uicommon import BACK_ACTIONS, open_window

BACKGROUND = 30000
LIST = 30002


class StreamsWindow(xbmcgui.WindowXMLDialog):
    """See module docstring. Built/run via `open_streams()`."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.pairs = []
        self.stype = 'movie'
        self.sid = None
        self.poster = None
        self.played = False

    def start(self, pairs, stype, sid, poster=None):
        """doModal() showing `pairs` (a list of `(info, stream)` as
        `lib.stremio.streaminfo.parse_stream`/`sort_streams` produce).
        Returns True if playback started (the caller should also
        close)."""
        self.pairs = list(pairs or [])
        self.stype = stype
        self.sid = sid
        self.poster = poster
        self.played = False
        if not self.pairs:
            return False
        self.doModal()
        return self.played

    def onInit(self):
        from lib.ui.compat import addon_fanart

        self.getControl(BACKGROUND).setImage(self.poster or addon_fanart())

        items = []
        for index, (info, _stream) in enumerate(self.pairs):
            label = streaminfo.format_label(info) or info.get('raw') or info.get('addon') or '?'
            label = label.replace('\r', ' ').replace('\n', ' ')
            item = xbmcgui.ListItem(label)
            item.setProperty('position', str(index))
            items.append(item)
        self.getControl(LIST).addItems(items)
        self.setFocusId(LIST)

    def onAction(self, action):
        if action.getId() in BACK_ACTIONS:
            self.close()

    def onClick(self, control_id):
        if control_id != LIST:
            return
        focused = self.getControl(LIST).getSelectedItem()
        if focused is None:
            return
        _info, stream = self.pairs[int(focused.getProperty('position'))]

        from lib.ui.player import play_direct
        if play_direct(stream, self.stype, self.sid):
            self.played = True
            self.close()


def open_streams(stype, sid, poster=None):
    """Fetch+sort every installed addon's streams for (stype, sid) and
    show them; a pick resolves+plays directly. Returns True if playback
    started (the caller should also close)."""
    import xbmc

    from lib.ui.compat import ADDON, L, addon_profile_dir, log, notify

    store = Store(addon_profile_dir())
    client = AddonClient()
    pairs = []
    for descriptor in store.get_addons():
        manifest = descriptor.get('manifest') or {}
        transport_url = descriptor.get('transportUrl')
        if not addon_supports(manifest, 'stream', stype, sid):
            continue
        try:
            results = client.streams(transport_url, stype, sid)
        except AddonError as exc:
            log('streamswindow: %s failed: %r' % (transport_url, exc), xbmc.LOGERROR)
            continue
        addon_name = manifest.get('name', '?')
        for stream in results or []:
            pairs.append((streaminfo.parse_stream(stream, addon_name=addon_name), stream))

    if not pairs:
        notify(L(30030))
        return False

    sort_key = ADDON.getSetting('stream_sort') or 'quality'
    pairs = streaminfo.sort_streams(pairs, key=sort_key)

    log('streamswindow: opening StreamsWindow (%d streams)' % len(pairs), xbmc.LOGINFO)
    try:
        win = open_window(StreamsWindow, 'StreamsWindow.xml')
        return win.start(pairs, stype, sid, poster=poster)
    except Exception as exc:  # a skin/UI failure must surface, not vanish
        log('streamswindow: window failed to open: %r' % (exc,), xbmc.LOGERROR)
        notify(L(30032))
        return False
