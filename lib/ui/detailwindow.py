"""DetailWindow: one series title's episode list - Rivulet's custom
replacement for the classical `videos()` directory.

A movie has nothing to pick here - there is only one thing to do with
it, play it - so `open_detail()` skips this window entirely for a title
with no `videos` and opens `lib.ui.streamswindow.open_streams()`
directly (confirmed on a real device: a DetailWindow showing a single
"Play" row was a pointless extra step for every movie). Only a series
(which has episodes to choose from) actually shows this window: every
episode grouped by season ("S01E02 · Title", Specials last) behind a
season-selector bar (see the class docstring for the single-season
fallback). Fetches the full meta via the same `lib.ui.views._fetch_meta`
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
SEASON_BAR = 30007

#: ACTION_MOVE_LEFT / ACTION_MOVE_RIGHT - navigating the season bar
#: (id SEASON_BAR) with either fires onAction() while it still has focus;
#: that is this module's cue to check whether the selected season moved.
_SEASON_NAV_ACTIONS = frozenset({1, 2})


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


def _season_label(season):
    """'Season N' for season N >= 1, 'Specials' for season 0 - the two
    label shapes DetailWindow.xml's season bar (`SEASON_BAR`/30007)
    shows."""
    return 'Season %d' % season if season else 'Specials'


def _group_by_season(videos):
    """Group `_ordered_videos(videos)` into per-season buckets, preserving
    the season-0-last order `_ordered_videos()` already establishes.
    Returns a list of `(season, label, videos)` tuples, one per distinct
    season, in season-bar order - pure, so it is trivially unit-testable
    on its own."""
    groups = []
    index_by_season = {}
    for video in _ordered_videos(videos):
        season = video.get('season') or 0
        if season not in index_by_season:
            index_by_season[season] = len(groups)
            groups.append((season, _season_label(season), []))
        groups[index_by_season[season]][2].append(video)
    return groups


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
    Every episode is grouped by season behind a season-selector bar
    (`SEASON_BAR`/30007) that only ever shows when there is more than one
    season to switch between - a single-season (or season-less) title
    hides the bar and shows every episode in one flat list, exactly like
    before season grouping existed."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.meta = {}
        self.stype = 'series'
        self.rows = []
        self.videos = []
        self._video_by_id = {}
        self.season_groups = []
        self.season_index = 0
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
        self.season_groups = _group_by_season(self.meta.get('videos'))
        self.season_index = self._default_season_index()
        self.doModal()
        return self.should_close_caller

    def _default_season_index(self):
        """Index into `self.season_groups` of the first non-Specials
        season, or 0 (Specials) if that is the only season present."""
        for index, (season, _label, _videos) in enumerate(self.season_groups):
            if season != 0:
                return index
        return 0

    def _active_videos(self):
        """Videos the episode list (`LIST`) should currently show: every
        episode when there is nothing to group (0 or 1 season - today's
        flat-list behaviour, unchanged byte-for-byte), else just the
        active season's slice of `self.season_groups`."""
        if len(self.season_groups) <= 1:
            return self.videos
        return self.season_groups[self.season_index][2]

    def _build_episode_items(self, videos):
        """One `xbmcgui.ListItem` per video in `videos` - `row_id`
        property plus `_episode_properties()`'s thumb/line2 - the
        row-building logic the initial populate and every season switch
        both reuse, factored out so either can build from any video
        subset."""
        items = []
        for video in videos:
            item = xbmcgui.ListItem(_episode_label(video))
            item.setProperty('row_id', video.get('id'))
            for key, value in _episode_properties(video).items():
                item.setProperty(key, value)
            items.append(item)
        return items

    def _populate_episode_list(self, videos):
        """Replace `LIST`'s contents with `videos`' rows and reset the
        selection to the top - used for the initial populate and for
        every season switch alike."""
        control = self.getControl(LIST)
        control.reset()
        control.addItems(self._build_episode_items(videos))
        control.selectItem(0)

    def _build_season_bar(self):
        """Populate `SEASON_BAR` once, in bar order, each item's `season`
        property holding its season number as a string. Hidden via
        `setVisible(False)` whenever there is nothing to switch between
        (0 or 1 season) so the flat list behaves exactly as it did before
        season grouping existed."""
        control = self.getControl(SEASON_BAR)
        control.reset()
        if len(self.season_groups) <= 1:
            control.setVisible(False)
            return
        items = []
        for season, label, _videos in self.season_groups:
            item = xbmcgui.ListItem(label)
            item.setProperty('season', str(season))
            items.append(item)
        control.addItems(items)
        control.setVisible(True)
        control.selectItem(self.season_index)

    def _sync_season_from_bar(self):
        """If `SEASON_BAR`'s selected position has moved since the last
        sync, repopulate `LIST` with the newly-selected season's episodes
        and remember the new position. A no-op with 0/1 season groups
        (the bar is hidden) or when the position hasn't actually moved."""
        if len(self.season_groups) <= 1:
            return
        position = self.getControl(SEASON_BAR).getSelectedPosition()
        if position == self.season_index or not 0 <= position < len(self.season_groups):
            return
        self.season_index = position
        self._populate_episode_list(self._active_videos())

    def onInit(self):
        from lib.ui.compat import addon_fanart

        background = self.meta.get('background') or self.meta.get('logo') or self.meta.get('poster')
        self.getControl(BACKGROUND).setImage(background or addon_fanart())
        self.getControl(POSTER).setImage(self.meta.get('poster') or '')
        self.getControl(HEADING).setLabel((self.meta.get('name') or self.meta.get('id') or '').upper())

        self._build_season_bar()
        self._populate_episode_list(self._active_videos())
        self.setFocusId(LIST)

    def onAction(self, action):
        action_id = action.getId()
        if action_id in _SEASON_NAV_ACTIONS and self.getFocusId() == SEASON_BAR:
            self._sync_season_from_bar()
        if action_id in BACK_ACTIONS:
            self.close()

    def onClick(self, control_id):
        if control_id == SEASON_BAR:
            self._sync_season_from_bar()
            self.setFocusId(LIST)
            return
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
