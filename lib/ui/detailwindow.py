"""DetailWindow: one series title's episode list - Rivulet's custom
replacement for the classical `videos()` directory.

A movie has nothing to pick here - there is only one thing to do with
it, play it - so `open_detail()` skips this window entirely for a title
with no `videos` and opens `lib.ui.streamswindow.open_streams()`
directly (confirmed on a real device: a DetailWindow showing a single
"Play" row was a pointless extra step for every movie). Only a series
(which has episodes to choose from) actually shows this window: every
episode flattened across seasons ("SxEE. Title", Specials last).
Fetches the full meta via the same `lib.ui.views._fetch_meta` every
classical view already uses (the catalog/search coverflow's meta
objects are abbreviated - no `videos` - so a fresh fetch is required
here, not a reuse of the picked item).
"""
import xbmcgui

from lib.ui.uicommon import BACK_ACTIONS, open_window

BACKGROUND = 30000
LIST = 30002


def _episode_rows(videos):
    """Flatten a meta's `videos` array into `(id, label)` pairs, ordered
    (season == 0, season, episode) ascending so Specials (season 0) sort
    last - the same rule `lib.stremio.types.video_sort_key` documented
    before it was removed as unused dead code; applied here where it is
    actually needed."""
    ordered = sorted(
        (v for v in videos or [] if v.get('id')),
        key=lambda v: ((v.get('season') or 0) == 0, v.get('season') or 0, v.get('episode') or 0),
    )
    rows = []
    for video in ordered:
        title = video.get('title') or video.get('name') or video.get('id')
        label = '%dx%02d. %s' % (video.get('season') or 0, video.get('episode') or 0, title)
        rows.append((video.get('id'), label))
    return rows


class DetailWindow(xbmcgui.WindowXMLDialog):
    """See module docstring. Built/run via `open_detail()` - only for a
    series (a title with episodes); a movie never reaches this window."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.meta = {}
        self.stype = 'series'
        self.rows = []
        self.should_close_caller = False

    def start(self, meta, stype):
        """doModal() showing `meta`'s episode list. Returns True if
        playback started somewhere down the chain (the caller should
        also close)."""
        self.meta = meta or {}
        self.stype = stype
        self.should_close_caller = False
        self.rows = _episode_rows(self.meta.get('videos'))
        self.doModal()
        return self.should_close_caller

    def onInit(self):
        from lib.ui.compat import addon_fanart

        art = self.meta.get('background') or self.meta.get('logo') or self.meta.get('poster')
        self.getControl(BACKGROUND).setImage(art or addon_fanart())

        items = []
        for row_id, label in self.rows:
            item = xbmcgui.ListItem(label)
            item.setProperty('row_id', row_id)
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
        sid = focused.getProperty('row_id')

        from lib.ui.streamswindow import open_streams
        if open_streams(self.stype, sid, poster=self.meta.get('poster')):
            self.should_close_caller = True
            self.close()


def open_detail(stype, sid):
    """Fetch (stype, sid)'s full meta. A movie (no `videos`) has nothing
    to pick, so it opens StreamsWindow directly; a series opens this
    window first to pick an episode. Returns True if playback started
    somewhere down the chain (the caller should also close)."""
    import xbmc

    from lib.ui.compat import L, log, notify
    from lib.ui.views import _fetch_meta

    meta_obj = _fetch_meta(stype, sid)
    if not meta_obj:
        notify(L(30030))
        return False

    from lib.ui.streamswindow import open_streams
    if not meta_obj.get('videos'):
        return open_streams(stype, sid, poster=meta_obj.get('poster'))

    log('detailwindow: opening DetailWindow for %s/%s' % (stype, sid), xbmc.LOGINFO)
    try:
        win = open_window(DetailWindow, 'DetailWindow.xml')
        return win.start(meta_obj, stype)
    except Exception as exc:  # a skin/UI failure must surface, not vanish
        log('detailwindow: window failed to open: %r' % (exc,), xbmc.LOGERROR)
        notify(L(30032))
        return False
