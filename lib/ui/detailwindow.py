"""DetailWindow: one series title's episode list - Rivulet's custom
replacement for the classical `videos()` directory.

A movie has nothing to pick here - there is only one thing to do with
it, play it - so `open_detail()` skips this window entirely for a title
with no `videos` and opens `lib.ui.streamswindow.open_streams()`
directly (confirmed on a real device: a DetailWindow showing a single
"Play" row was a pointless extra step for every movie). Only a series
(which has episodes to choose from) actually shows this window: every
episode flattened across seasons ("S01E02 · Title", Specials last;
season-tab grouping is intentionally out of scope - see the class
docstring). Fetches the full meta via the same `lib.ui.views._fetch_meta`
every classical view already uses (the catalog/search coverflow's meta
objects are abbreviated - no `videos` - so a fresh fetch is required
here, not a reuse of the picked item).

Picking a row hands StreamsWindow the pre-agreed `heading`/`art` context
kwargs (`heading='<Show> \u2013 S01E02 <Episode>'`,
`art={'poster': ..., 'fanart': ...}`) so its own header can show what is
about to play without re-fetching the show's meta - the movie-skip path
in `open_detail()` does the same with the movie's own title/art.
"""
import xbmcgui

from lib.ui.uicommon import BACK_ACTIONS, busy_dialog, open_window

BACKGROUND = 30000
POSTER = 30004
HEADING = 30005
LIST = 30002


def _ordered_videos(videos):
    """Filter out any entry missing an `id` (nothing to open streams
    with) and sort the rest into flat episode-list order: (season == 0,
    season, episode) ascending, so Specials (season 0) sort last - the
    same rule `lib.stremio.types.video_sort_key` documented before it
    was removed as unused dead code; applied here where it is actually
    needed. Keeps the full video dict (not just id/label) so callers can
    also pull thumb/plot/aired art and the episode code for a
    StreamsWindow heading."""
    return sorted(
        (v for v in videos or [] if v.get('id')),
        key=lambda v: ((v.get('season') or 0) == 0, v.get('season') or 0, v.get('episode') or 0),
    )


def _episode_label(video):
    """'S01E03 · The Title' - zero-padded season/episode (Specials as
    S00Exx), falling back title -> name -> id exactly like the classical
    `videos()` view's row label."""
    title = video.get('title') or video.get('name') or video.get('id')
    return 'S%02dE%02d \u00b7 %s' % (video.get('season') or 0, video.get('episode') or 0, title)


def _episode_rows(videos):
    """Flatten+sort a meta's `videos` array (via `_ordered_videos()`)
    into `(id, label)` pairs for `DetailWindow`'s list - pure, so it is
    trivially unit-testable on its own."""
    return [(video.get('id'), _episode_label(video)) for video in _ordered_videos(videos)]


def _episode_properties(video):
    """Map one video/episode meta to the string Properties
    `DetailWindow.xml`'s itemlayout reads via
    `$INFO[ListItem.Property(...)]`: `thumb` (episode still - may be
    empty, the row's thumb `<control>` degrades gracefully to nothing)
    and `line2` (first line of the episode's plot, falling back to its
    air date, falling back to empty)."""
    video = video or {}
    plot = (video.get('overview') or '').strip()
    line1 = plot.splitlines()[0] if plot else ''
    released = video.get('released') or ''
    aired = released.split('T', 1)[0] if released else ''
    return {
        'thumb': video.get('thumbnail') or '',
        'line2': line1 or aired or '',
    }


def _episode_heading(show_name, video):
    """'<Show> \u2013 S01E02 <Episode Title>' for StreamsWindow's
    pre-agreed `heading` kwarg - the cross-agent contract every
    DetailWindow/ShowcaseWindow call site into `open_streams()` honors."""
    video = video or {}
    code_and_title = _episode_label(video).replace(' \u00b7 ', ' ', 1)
    return '%s \u2013 %s' % (show_name, code_and_title) if show_name else code_and_title


def _show_art(meta):
    """`art={'poster': ..., 'fanart': ...}` for StreamsWindow's
    pre-agreed `art` kwarg, derived from a title's meta the same way
    `DetailWindow.onInit()` resolves its own background image
    (background > logo > poster)."""
    meta = meta or {}
    poster = meta.get('poster')
    fanart = meta.get('background') or meta.get('logo') or poster
    return {'poster': poster, 'fanart': fanart}


class DetailWindow(xbmcgui.WindowXMLDialog):
    """See module docstring. Built/run via `open_detail()` - only for a
    series (a title with episodes); a movie never reaches this window.
    Deliberately a single flat list (no per-season tabs/grouping - out
    of scope here; every episode across every season is one row)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.meta = {}
        self.stype = 'series'
        self.rows = []
        self.videos = []
        self._video_by_id = {}
        self.should_close_caller = False

    def start(self, meta, stype):
        """doModal() showing `meta`'s episode list. Returns True if
        playback started somewhere down the chain (the caller should
        also close)."""
        self.meta = meta or {}
        self.stype = stype
        self.should_close_caller = False
        self.videos = _ordered_videos(self.meta.get('videos'))
        self.rows = [(video.get('id'), _episode_label(video)) for video in self.videos]
        self._video_by_id = {video.get('id'): video for video in self.videos}
        self.doModal()
        return self.should_close_caller

    def onInit(self):
        from lib.ui.compat import addon_fanart

        background = self.meta.get('background') or self.meta.get('logo') or self.meta.get('poster')
        self.getControl(BACKGROUND).setImage(background or addon_fanart())
        self.getControl(POSTER).setImage(self.meta.get('poster') or '')
        self.getControl(HEADING).setLabel((self.meta.get('name') or self.meta.get('id') or '').upper())

        items = []
        for row_id, label in self.rows:
            item = xbmcgui.ListItem(label)
            item.setProperty('row_id', row_id)
            for key, value in _episode_properties(self._video_by_id.get(row_id)).items():
                item.setProperty(key, value)
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
        video = self._video_by_id.get(sid)

        from lib.ui.streamswindow import open_streams
        if open_streams(
            self.stype, sid, poster=self.meta.get('poster'),
            heading=_episode_heading(self.meta.get('name') or self.meta.get('id') or '', video),
            art=_show_art(self.meta),
        ):
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

    with busy_dialog(L(30033)):
        meta_obj = _fetch_meta(stype, sid)
    if not meta_obj:
        notify(L(30030))
        return False

    from lib.ui.streamswindow import open_streams
    if not meta_obj.get('videos'):
        return open_streams(
            stype, sid, poster=meta_obj.get('poster'),
            heading=meta_obj.get('name') or meta_obj.get('id') or '',
            art=_show_art(meta_obj),
        )

    log('detailwindow: opening DetailWindow for %s/%s' % (stype, sid), xbmc.LOGINFO)
    win = None
    try:
        win = open_window(DetailWindow, 'DetailWindow.xml')
        return win.start(meta_obj, stype)
    except Exception as exc:  # a skin/UI failure must surface, not vanish
        log('detailwindow: window failed to open: %r' % (exc,), xbmc.LOGERROR)
        notify(L(30032))
        return False
    finally:
        # A normal return means DetailWindow already closed itself (its own
        # onAction/onClick calls self.close()) before .start() returned -
        # but an exception raised from WITHIN .start() (onInit(), or a
        # callback mid-doModal()) skips that self-close entirely. Close
        # unconditionally here so no exit path leaves a zombie modal
        # window behind; closing an already-closed window is a safe no-op.
        if win is not None:
            try:
                win.close()
            except Exception:
                pass
