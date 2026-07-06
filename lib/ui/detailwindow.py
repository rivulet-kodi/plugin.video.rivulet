"""DetailWindow: one title's playable content - Rivulet's custom
replacement for the classical `meta()`/`videos()` directories.

A movie (no `videos` array) shows a single "Play" row; a series shows
every episode flattened across seasons ("SxEE. Title", Specials last).
Picking either opens `lib.ui.streamswindow.open_streams()` for that
(stype, id). Fetches the full meta via the same
`lib.ui.views._fetch_meta` every classical view already uses (the
catalog/search coverflow's meta objects are abbreviated - no `videos` -
so a fresh fetch is required here, not a reuse of the picked item).

A richer layout (poster/plot/cast alongside the episode list) is a
follow-up; v1 intentionally keeps to what the existing test fakes
support (background art + a list), matching `HomeWindow`/
`CatalogPickerWindow`'s structure.
"""
import xbmcgui

from lib.ui.uicommon import BACK_ACTIONS, open_window

BACKGROUND = 30000
LIST = 30002

#: Sentinel row id for a movie's single "Play" row (an episode's row id
#: is always its real Stremio video id, which is never this string).
PLAY_ROW_ID = '__play__'


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
    """See module docstring. Built/run via `open_detail()`."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.meta = {}
        self.stype = 'movie'
        self.rows = []
        self.should_close_caller = False

    def start(self, meta, stype):
        """doModal() showing `meta`'s (already-fetched, full) content.
        Returns True if playback started somewhere down the chain (the
        caller should also close)."""
        self.meta = meta or {}
        self.stype = stype
        self.should_close_caller = False
        videos = self.meta.get('videos') or []
        self.rows = _episode_rows(videos) if videos else [(PLAY_ROW_ID, 'Play')]
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
        row_id = focused.getProperty('row_id')
        sid = self.meta.get('id') if row_id == PLAY_ROW_ID else row_id

        from lib.ui.streamswindow import open_streams
        if open_streams(self.stype, sid, poster=self.meta.get('poster')):
            self.should_close_caller = True
            self.close()


def open_detail(stype, sid):
    """Fetch (stype, sid)'s full meta and show its detail/episode list;
    opens StreamsWindow on a pick. Returns True if playback started
    somewhere down the chain (the caller should also close)."""
    from lib.ui.compat import L, notify
    from lib.ui.views import _fetch_meta

    meta_obj = _fetch_meta(stype, sid)
    if not meta_obj:
        notify(L(30030))
        return False

    win = open_window(DetailWindow, 'DetailWindow.xml')
    return win.start(meta_obj, stype)
