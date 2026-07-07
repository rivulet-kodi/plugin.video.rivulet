"""StreamsWindow: the resolved-source picker for one title/episode -
Rivulet's custom replacement for the classical `streams()` directory.

Picking a row resolves and plays it DIRECTLY via
`lib.ui.player.play_direct` (no ADDON_HANDLE/`setResolvedUrl` - see that
function's docstring), so Kodi's player takes over the full screen.
`open_streams()` returns True when playback actually started, and every
caller up the chain (`DetailWindow`, `CatalogPickerWindow`, `SearchWindow`
via `open_detail`) propagates that by closing itself too, so nothing
custom lingers behind the player once picked.

`open_streams()`/`StreamsWindow.start()` also take optional `heading`/
`art` context kwargs (`heading='<title>'`, `art={'poster': ...,
'fanart': ...}`) - the pre-agreed cross-agent contract `DetailWindow`
(an episode's "<Show> - SxxExx <Title>" + the show's own art) and
`ShowcaseWindow`'s movie path (the movie's own title/art) both call into.
Both default to "nothing supplied" (`''`/`None`) so a bare `poster=`
kwarg, or no context at all, keeps every pre-existing call site working
unchanged: an empty heading falls back to a generic localized "Streams"
title, and no `art` simply means the side poster panel stays empty.
"""
import xbmcgui

from lib.store import Store
from lib.stremio import streaminfo
from lib.stremio.addons import AddonClient, AddonError, addon_supports
from lib.ui.uicommon import BACK_ACTIONS, busy_dialog, open_window

BACKGROUND = 30000
LIST = 30002
POSTER = 30004
HEADING = 30005


class StreamsWindow(xbmcgui.WindowXMLDialog):
    """See module docstring. Built/run via `open_streams()`."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.pairs = []
        self.stype = 'movie'
        self.sid = None
        self.poster = None
        self.heading = ''
        self.art = None
        self.played = False

    def start(self, pairs, stype, sid, poster=None, heading='', art=None):
        """doModal() showing `pairs` (a list of `(info, stream)` as
        `lib.stremio.streaminfo.parse_stream`/`sort_streams` produce).
        `heading`/`art` are the optional caller-context kwargs described
        in the module docstring. Returns True if playback started (the
        caller should also close)."""
        self.pairs = list(pairs or [])
        self.stype = stype
        self.sid = sid
        self.poster = poster
        self.heading = heading or ''
        self.art = art
        self.played = False
        if not self.pairs:
            return False
        self.doModal()
        return self.played

    def onInit(self):
        from lib.ui.compat import L, addon_fanart

        art = self.art or {}
        background = art.get('fanart') or art.get('poster') or self.poster or addon_fanart()
        self.getControl(BACKGROUND).setImage(background)
        self.getControl(POSTER).setImage(art.get('poster') or self.poster or '')
        self.getControl(HEADING).setLabel((self.heading or L(30041)).upper())

        items = []
        for index, (info, _stream) in enumerate(self.pairs):
            line1 = streaminfo.format_label(info, include_addon=False) or info.get('raw') or '?'
            line1 = line1.replace('\r', ' ').replace('\n', ' ')
            line2 = (info.get('addon') or '').replace('\r', ' ').replace('\n', ' ')
            item = xbmcgui.ListItem(line1, label2=line2)
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


def open_streams(stype, sid, poster=None, heading='', art=None):
    """Fetch+sort every installed addon's streams for (stype, sid) and
    show them; a pick resolves+plays directly. `heading`/`art` are
    forwarded to `StreamsWindow.start()` unchanged (see the module
    docstring). Returns True if playback started (the caller should
    also close)."""
    import xbmc

    from lib.ui.compat import ADDON, L, addon_profile_dir, log, notify

    store = Store(addon_profile_dir())
    client = AddonClient()
    pairs = []
    addons = []
    for descriptor in store.get_addons():
        manifest = descriptor.get('manifest') or {}
        if addon_supports(manifest, 'stream', stype, sid):
            addons.append((descriptor, manifest))
    total_addons = len(addons)
    failed_addons = 0
    with busy_dialog(L(30033)) as dialog:
        for index, (descriptor, manifest) in enumerate(addons):
            if dialog.iscanceled():
                break
            transport_url = descriptor.get('transportUrl')
            addon_name = manifest.get('name', '?')
            percent = int(index * 100 / total_addons) if total_addons else 0
            dialog.update(percent, 'Checking %s...' % addon_name)
            try:
                results = client.streams(transport_url, stype, sid)
            except AddonError as exc:
                # One addon failing (offline, misconfigured, slow) is
                # routine, not exceptional - logging each at ERROR with a
                # full exception repr drowned real problems in noise on
                # every single fetch. DEBUG + a single-line message here
                # (never trust an upstream error string not to embed a
                # stray CR/LF); one aggregate WARNING below covers
                # "something's wrong" without spamming per-addon detail
                # into the normal log.
                message = 'streamswindow: %s failed: %s' % (transport_url, exc)
                log(message.replace('\r', ' ').replace('\n', ' '), xbmc.LOGDEBUG)
                failed_addons += 1
                continue
            for stream in results or []:
                pairs.append((streaminfo.parse_stream(stream, addon_name=addon_name), stream))

    if failed_addons:
        log('streamswindow: %d addon(s) failed' % failed_addons, xbmc.LOGWARNING)

    if not pairs:
        notify(L(30030))
        return False

    sort_key = ADDON.getSetting('stream_sort') or 'quality'
    pairs = streaminfo.sort_streams(pairs, key=sort_key)

    log('streamswindow: opening StreamsWindow (%d streams)' % len(pairs), xbmc.LOGINFO)
    win = None
    try:
        win = open_window(StreamsWindow, 'StreamsWindow.xml')
        return win.start(pairs, stype, sid, poster=poster, heading=heading, art=art)
    except Exception as exc:  # a skin/UI failure must surface, not vanish
        log('streamswindow: window failed to open: %r' % (exc,), xbmc.LOGERROR)
        notify(L(30032))
        return False
    finally:
        # A normal return means StreamsWindow already closed itself (its
        # own onAction/onClick calls self.close()) before .start() returned
        # - but an exception raised from WITHIN .start() (onInit(), or a
        # callback mid-doModal()) skips that self-close entirely. Close
        # unconditionally here so no exit path leaves a zombie modal
        # window behind; closing an already-closed window is a safe no-op.
        if win is not None:
            try:
                win.close()
            except Exception:
                pass
