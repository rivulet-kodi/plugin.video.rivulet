"""DetailWindow: one series title's episode list, shown so the user
can pick which episode to play before opening the stream picker.

A movie has nothing to pick here - there is only one thing to do with
it, play it - so `open_detail()` skips this window entirely for a title
with no `videos` and opens `lib.ui.streamswindow.open_streams()`
directly (confirmed on a real device: a DetailWindow showing a single
"Play" row was a pointless extra step for every movie). Only a series
(which has episodes to choose from) actually shows this window: every
episode grouped by season ("S01E02 · Title", Specials last) behind a
season-selector bar (see the class docstring for the single-season
fallback). Fetches the full meta via the same `lib.ui.views._fetch_meta`
other windows use (the coverflow's meta objects are abbreviated - no
`videos` - so a fresh full fetch is required here, not a reuse of the
picked item).

Picking a row hands StreamsWindow the pre-agreed `heading`/`art` context
kwargs (`heading='<Show> \u2013 S01E02 <Episode>'`,
`art={'poster': ..., 'fanart': ...}`) so its own header can show what is
about to play without re-fetching the show's meta - the movie-skip path
in `open_detail()` does the same with the movie's own title/art.
"""
import xbmcgui

from lib.ui.uicommon import BACK_ACTIONS, ModalStackWindow, busy_dialog, open_window

BACKGROUND = 30000
POSTER = 30004
HEADING = 30005
LIST = 30002
SEASON_BAR = 30007

#: Right-aligned "N EPISODES" readout for the season the episode list
#: (LIST) is currently showing - a plain window label DetailWindow sets
#: directly via setLabel() (like HEADING), not a per-ListItem Property:
#: there is exactly one active season at a time, never one value per row.
SEASON_COUNT = 30100

#: Left column's "year \u00b7 N SEASONS \u00b7 \u2605 rating" line, built
#: from the series meta already in hand - see _metadata_line().
METADATA_LINE = 30101

#: Left column's genre line - see _genres_line().
GENRES_LINE = 30102

#: strings.po ids for the localized 'N SEASON(S)' segment _metadata_line()
#: builds and the 'N EPISODE(S)' readout _set_season_count() sets -
#: singular msgstr for a count of exactly 1, plural otherwise. Both
#: callers only reach these once the count is already known > 0 (see
#: each function's own docstring for its own zero-count guard).
_SEASON_STRING_ID = 30204
_SEASONS_STRING_ID = 30205
_EPISODE_STRING_ID = 30206
_EPISODES_STRING_ID = 30207

#: strings.po id for the range terminator a still-running series gets in
#: place of a dangling dash ('2022–now') - see `_metadata_line()`. The
#: same id the coverflow hero and StreamsWindow use.
_NOW_STRING_ID = 30234

#: strings.po id for the per-episode '%d%% WATCHED' readout
#: _build_episode_items() sets as the `watched_percent` Property -
#: DetailWindow.xml's label reads that property verbatim (one $INFO
#: substitution, no literal English appended in the skin).
_WATCHED_STRING_ID = 30212

#: ACTION_MOVE_LEFT / ACTION_MOVE_RIGHT - navigating the season bar
#: (id SEASON_BAR) with either fires onAction() while it still has focus;
#: that is this module's cue to check whether the selected season moved.
_SEASON_NAV_ACTIONS = frozenset({1, 2})

#: ACTION_CONTEXT_MENU ("C" key, a remote's menu button, long-press on
#: Android TV) opens the cast & crew picker for `self.meta` - the same
#: affordance ShowcaseWindow's identical constant offers a movie, since
#: this window only ever shows for a series (see the module docstring).
_CONTEXT_MENU_ACTION = 117


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


def _episode_code(video):
    """'S01E03' - zero-padded season/episode (Specials as S00Exx), the
    mono/blue half of `_episode_label()` now split into its own
    ListItem `code` Property so the skin can style it apart from the
    title."""
    return 'S%02dE%02d' % (video.get('season') or 0, video.get('episode') or 0)


def _episode_title(video):
    """title -> name -> id fallback chain - the other half of
    `_episode_label()`, as its own ListItem `title` Property."""
    return video.get('title') or video.get('name') or video.get('id') or ''


def _episode_label(video):
    """'S01E03 \u00b7 The Title' - `_episode_code()` and
    `_episode_title()` joined, kept as the item's plain Label (every
    existing caller - `_episode_rows()`, `_episode_heading()`, the
    row's own `xbmcgui.ListItem(...)` constructor - still reads the one
    combined string); DetailWindow.xml's itemlayout instead reads the
    two halves apart via the `code`/`title` Properties
    `_episode_properties()` sets alongside it."""
    return '%s \u00b7 %s' % (_episode_code(video), _episode_title(video))


def _episode_rows(videos):
    """Flatten+sort a meta's `videos` array (via `_ordered_videos()`)
    into `(id, label)` pairs for `DetailWindow`'s list - pure, so it is
    trivially unit-testable on its own."""
    return [(video.get('id'), _episode_label(video)) for video in _ordered_videos(videos)]


def _season_label(season):
    """Localized 'Season N' for season N >= 1, 'Specials' for season 0 -
    the two label shapes DetailWindow.xml's season bar (`SEASON_BAR`/
    30007) shows."""
    from lib.ui.compat import L

    return L(30188) % season if season else L(30189)


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


def _season_count(season_groups):
    """Count of distinct non-Specials seasons in `season_groups` (see
    `_group_by_season()`) - the left column's 'N SEASONS' segment.
    Specials (season 0) is a bucket, never a season, so it's excluded."""
    return sum(1 for season, _label, _videos in season_groups if season != 0)


def _episode_properties(video):
    """Map one video/episode meta to the string Properties
    `DetailWindow.xml`'s itemlayout reads via
    `$INFO[ListItem.Property(...)]`: `thumb` (episode still - may be
    empty, the row's thumb `<control>` degrades gracefully to nothing),
    `line2` (first line of the episode's plot, falling back to its air
    date, falling back to empty), and `code`/`title` - the episode code
    and title split back out of `_episode_label()`'s one combined
    string so the skin can style them apart (mono/blue code, bold white
    title). Per-episode `runtime` was investigated (neither the Stremio
    Video shape nor anything this addon's meta/store carries one) and
    is deliberately NOT exposed here - there is no real value to bind.
    `watched_percent` - the localized '%d%% WATCHED' readout, when there
    is any - is set separately by `DetailWindow._build_episode_items()`, which
    needs a `Store` this pure function deliberately does not touch."""
    video = video or {}
    plot = (video.get('overview') or '').strip()
    line1 = plot.splitlines()[0] if plot else ''
    released = video.get('released') or ''
    aired = released.split('T', 1)[0] if released else ''
    return {
        'thumb': video.get('thumbnail') or '',
        'line2': line1 or aired or '',
        'code': _episode_code(video),
        'title': _episode_title(video),
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


def _metadata_line(meta, season_count):
    """'2025 \u00b7 2 SEASONS \u00b7 [COLOR FF38BDF8]\u2605 7.6[/COLOR]'
    for the left column (METADATA_LINE/30101) - built from series-level
    meta only, never a per-episode video. Any segment whose source
    value is missing (no year, no real season yet, no rating) is
    skipped outright rather than left as a dangling ' \u00b7 ' - the
    same join-only-what-exists shape
    lib.ui.streamswindow._rebuild_list() already uses for its own
    year/rating/genres info panel.

    A still-running series' open-ended range is closed with the
    localized "now" rather than left as a dangling dash - see
    `lib.ui.playbackmeta.year_range()`, which the coverflow hero
    (`infowindow._year_text()`) and StreamsWindow print from too, so
    the three screens agree on how a running series prints its years."""
    from lib.ui.compat import L
    from lib.ui.playbackmeta import year_range

    meta = meta or {}
    segments = []
    year = year_range(meta.get('releaseInfo') or meta.get('year') or '', L(_NOW_STRING_ID))
    if year:
        segments.append(year)
    if season_count > 0:
        segments.append(L(_SEASON_STRING_ID if season_count == 1 else _SEASONS_STRING_ID) % season_count)
    rating = meta.get('imdbRating')
    if rating:
        segments.append('[COLOR FF38BDF8]\u2605 %s[/COLOR]' % rating)
    return ' \u00b7 '.join(segments)


#: Left column genre line cap - matches the [:3] StreamsWindow's own
#: info-panel genre line already applies (streamswindow.py's
#: _rebuild_list()), so a long genre array can't overflow the 340px
#: column.
_MAX_GENRES = 3


def _genres_line(meta):
    """'Comedy \u00b7 Mystery \u00b7 Crime' for the left column
    (GENRES_LINE/30102), capped at `_MAX_GENRES` genres - empty string,
    never a lone separator, when the meta carries no genres at all."""
    genres = ((meta or {}).get('genres') or [])[:_MAX_GENRES]
    return ' \u00b7 '.join(genres)


def _watched_percent(progress):
    """0-100 int watched percentage from a `Store.get_progress()`
    payload (`{'position_ms', 'duration_ms', ...}`) - None (never
    0-as-"no data") when there is nothing recorded or the duration is
    unusable, mirroring `lib.ui.player._maybe_resume_offset_ms()`'s own
    position/duration guard."""
    if not progress:
        return None
    position_ms = progress.get('position_ms') or 0
    duration_ms = progress.get('duration_ms') or 0
    if duration_ms <= 0 or position_ms <= 0:
        return None
    return min(100, int(round((position_ms / duration_ms) * 100)))


class DetailWindow(ModalStackWindow, xbmcgui.WindowXMLDialog):
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
        property plus `_episode_properties()`'s thumb/line2/code/title,
        plus a best-effort `watched_percent` Property (the localized
        '%d%% WATCHED' text, WATCHED_STRING_ID/30212 - DetailWindow.xml's
        label reads it verbatim, no literal English baked into the skin)
        and matching legacy resumetime/totaltime video info, for the
        skin's ListItem.PercentPlayed-bound progress bar, from the local
        Store.get_progress() cache - the row-building logic the initial
        populate and every season switch both reuse, factored out so
        either can build from any video subset."""
        from lib.ui.compat import L

        sid = self.meta.get('id')
        items = []
        for video in videos:
            item = xbmcgui.ListItem(_episode_label(video))
            properties = _episode_properties(video)
            properties['row_id'] = video.get('id')
            progress = self._episode_progress(self.stype, sid, video.get('id'))
            percent = _watched_percent(progress)
            properties['watched_percent'] = L(_WATCHED_STRING_ID) % percent if percent is not None else ''
            # One setProperties() call instead of one setProperty() call
            # per key - each is a Python->C++ boundary crossing.
            item.setProperties(properties)
            if percent is not None:
                # The same legacy setInfo('video', {resumetime,
                # totaltime}) shape any Kodi video plugin uses to badge
                # a "continue watching" row - independent of this
                # addon's own `state.watched` bitfield (lib.library
                # never touches that; see its module docstring).
                item.setInfo('video', {
                    'resumetime': progress['position_ms'] / 1000.0,
                    'totaltime': progress['duration_ms'] / 1000.0,
                })
            items.append(item)
        return items

    @staticmethod
    def _episode_progress(stype, sid, video_id):
        """Best-effort per-episode `Store.get_progress()` lookup for
        `watched_percent` - resolving the store itself, or the lookup
        call, must never break the episode list (a corrupt/unwritable
        local cache, or a test with no real Kodi profile directory at
        all): mirrors `lib.ui.player._maybe_resume_offset_ms()`'s own
        defensive guard around the same call."""
        from lib.ui.dependencies import get_store
        try:
            return get_store().get_progress(stype, sid, video_id)
        except Exception:
            return None

    def _populate_episode_list(self, videos):
        """Replace `LIST`'s contents with `videos`' rows, reset the
        selection to the top, and refresh SEASON_COUNT's 'N EPISODES'
        readout to match - used for the initial populate and for every
        season switch alike, so the count always tracks whichever
        season is actually on screen."""
        control = self.getControl(LIST)
        control.reset()
        control.addItems(self._build_episode_items(videos))
        control.selectItem(0)
        self._set_season_count(len(videos))

    def _set_season_count(self, count):
        """SEASON_COUNT/30100's 'N EPISODES' text for the active
        season - empty (never '0 EPISODES') when there is nothing to
        show, so its XML `<visible>` guard (keyed off
        Control.GetLabel(SEASON_COUNT)) collapses it cleanly."""
        if not count:
            self.getControl(SEASON_COUNT).setLabel('')
            return
        from lib.ui.compat import L
        label = L(_EPISODE_STRING_ID if count == 1 else _EPISODES_STRING_ID) % count
        self.getControl(SEASON_COUNT).setLabel(label)

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
        # [B]...[/B] applied here, not in the skin XML, because this
        # label is always Python-set: any markup baked into the
        # <label> itself would just be overwritten the moment this
        # call runs.
        self.getControl(HEADING).setLabel('[B]%s[/B]' % (self.meta.get('name') or self.meta.get('id') or '').upper())
        self.getControl(METADATA_LINE).setLabel(_metadata_line(self.meta, _season_count(self.season_groups)))
        self.getControl(GENRES_LINE).setLabel(_genres_line(self.meta))

        self._build_season_bar()
        self._populate_episode_list(self._active_videos())
        self.setFocusId(LIST)

    def onAction(self, action):
        action_id = action.getId()
        if action_id in _SEASON_NAV_ACTIONS and self.getFocusId() == SEASON_BAR:
            self._sync_season_from_bar()
        if action_id == _CONTEXT_MENU_ACTION:
            self._open_credits()
            return
        if action_id in BACK_ACTIONS:
            self.close()

    def _open_credits(self):
        """ACTION_CONTEXT_MENU: `self.meta` is already the full meta
        `open_detail()` fetched to build this window, so - unlike
        ShowcaseWindow's own version of this affordance, where a catalog
        preview has no `links` - there is nothing left to fetch here."""
        from lib.ui.dependencies import get_client, get_store
        from lib.ui.infowindow import open_credits_picker

        open_credits_picker(get_store(), get_client(), self.meta)

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
        # open_streams() only ever returns False (see its own module
        # docstring) - a played pick reopens the SAME StreamsWindow round
        # trip instead of returning True, so this branch is dormant while
        # playback succeeds. If close_windows_for_playback() force-closed
        # THIS window mid-round-trip (it was still an open ancestor), that
        # close() cannot actually take effect until this onClick() call -
        # and therefore open_streams() - returns, at which point
        # ModalStackWindow.doModal() reopens it fresh; the user never sees
        # more than the one real close their own Back action eventually
        # causes.
        if open_streams(
            self.stype, sid, poster=self.meta.get('poster'),
            heading=_episode_heading(self.meta.get('name') or self.meta.get('id') or '', video),
            art=_show_art(self.meta), meta=self.meta, video_id=sid,
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
            art=_show_art(meta_obj), meta=meta_obj,
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
