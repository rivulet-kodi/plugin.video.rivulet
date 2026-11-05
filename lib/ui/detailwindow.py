# SPDX-FileCopyrightText: 2026 M0Rf30
# SPDX-License-Identifier: MIT

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

Under the season bar a row of chips shows the title's Genres, Cast,
Directors and Writers (`lib.ui.creditsrow`, control `CREDITS`); a click
runs the same dispatch as the 'Cast & Crew' picker
(`lib.ui.infowindow.open_credit_link()`). A series with no links hides
the row.

ACTION_CONTEXT_MENU opens `_open_context_menu()`'s combined actions
menu for the whole title: 'Cast & Crew' plus the Stremio account
library write actions this addon otherwise only READS from
(`lib.ui.mystuff`) - Add/Remove library and Mark (un)watched, both
pushed through `lib.ui.libraryaction.push_write()` (the addon's one
library write path, which also tells Home) using
payloads `lib.library` builds (`library_add_payload`/
`library_remove_payload`/`mark_watched_payload`). See
`_open_context_menu()` for why the row wording is fetched live every
time rather than cached.
"""
import threading
import time

import xbmcgui

from lib import livetv
from lib.library import episode_ticked, item_watched
from lib.stremio.addons import _base_type
from lib.stremio.watchedbitfield import sorted_video_ids, watched_video_ids
from lib.ui.creditsrow import CREDITS, fill_credits, open_credit
from lib.ui.playbackmeta import episode_code as _pm_episode_code
from lib.ui.playbackmeta import resolve_art
from lib.ui.uicommon import (
    BACK_ACTIONS,
    CANCELLED,
    ModalStackWindow,
    close_quietly,
    escape_label,
    gated_input,
    open_window,
    run_cancellable,
)
from lib.ui.urlutil import sized_image

BACKGROUND = 30000
POSTER = 30004
HEADING = 30005
LIST = 30002
SEASON_BAR = 30007
OPTIONS = 30009
TRAILER = 30012
LOGO = 30023

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
    title. Delegates to `lib.ui.playbackmeta.episode_code()` (shared
    with `lib.ui.mystuff`'s identical episode-code readout) so both
    tolerate a string `season`/`episode` from an addon instead of
    raising on `'%02d' % 'x'`."""
    return _pm_episode_code(video.get('season'), video.get('episode'))


def _episode_title(video):
    """title -> name -> id fallback chain - the other half of
    `_episode_label()`, as its own ListItem `title` Property. Escaped
    via `escape_label()`: this text is addon-supplied and ends up in a
    ListItem label/Property the skin renders verbatim."""
    title = video.get('title') or video.get('name') or video.get('id') or ''
    return escape_label(title)


def _episode_label(video):
    """'S01E03 \u00b7 The Title' - `_episode_code()` and
    `_episode_title()` joined, kept as the item's plain Label (every
    existing caller - `DetailWindow.start()`'s `rows`, `_episode_heading()`, the
    row's own `xbmcgui.ListItem(...)` constructor - still reads the one
    combined string); DetailWindow.xml's itemlayout instead reads the
    two halves apart via the `code`/`title` Properties
    `_episode_properties()` sets alongside it."""
    return '%s \u00b7 %s' % (_episode_code(video), _episode_title(video))


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


def _episode_properties(video, fallback=''):
    """Legacy code/title/line2 plus the new row still, full plot, date and runtime."""
    video = video or {}
    plot = (video.get('overview') or '').strip()
    line1 = plot.splitlines()[0] if plot else ''
    released = video.get('released') or ''
    aired = released.split('T', 1)[0] if released else ''
    raw_thumb = video.get('thumbnail')
    thumb = raw_thumb.strip() if isinstance(raw_thumb, str) else ''
    return {
        'thumb': sized_image(thumb or fallback or '', 'poster'),
        'line2': escape_label(line1 or aired or ''),
        'code': _episode_code(video),
        'title': _episode_title(video),
        # The rows (DetailWindow.xml): "1x01. Title", the air
        # date (ISO here; _build_episode_items formats it), the whole
        # plot, and a runtime when the meta has one (TMDB-backed metas do).
        'ep_title': _episode_row_title(video),
        'aired': aired,
        'plot': escape_label(plot),
        'runtime': _short_runtime(video.get('runtime')),
    }


def _episode_row_title(video):
    """'1\u00d701. Title': season x episode, the way the episode list reads."""
    title = _episode_title(video)
    try:
        season = int(video.get('season'))
        episode = int(video.get('episode'))
    except (TypeError, ValueError):
        return title
    return '%d\u00d7%02d. %s' % (season, episode, title)


def _short_runtime(value):
    """'1h 11m' / '54m' from a runtime ('71 min', 71, '71'), or ''."""
    from lib.ui.playbackmeta import parse_duration_seconds

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = '%d' % value
    seconds = parse_duration_seconds(value)
    if not seconds:
        return ''
    minutes = int(seconds // 60)
    hours, minutes = divmod(minutes, 60)
    return '%dh %dm' % (hours, minutes) if hours else '%dm' % minutes


def _short_date(iso_date):
    """``2024-02-27`` in Kodi's short date format (``27/02/2024``), or
    the input when it cannot be read."""
    text = str(iso_date or '').strip()
    if not text:
        return ''
    try:
        stamp = time.strptime(text[:10], '%Y-%m-%d')
    except ValueError:
        return text
    fmt = ''
    try:
        import xbmc

        fmt = xbmc.getRegion('dateshort') or ''
    except Exception:
        fmt = ''
    try:
        return time.strftime(fmt or '%d/%m/%Y', stamp)
    except Exception:
        return text


def _show_logo(window, logo_url):
    """Keep the text title until the existing logo cache knows the image."""
    from lib.ui.home import _logo_known_good, _warm_logos

    wanted = sized_image(logo_url or '', 'fill')
    logo = wanted if _logo_known_good(wanted) else ''
    try:
        if wanted and not logo:
            _warm_logos(window, [wanted])
        window.setProperty('HeroLogo', logo)
        window.getControl(LOGO).setImage(logo)
    except Exception:
        pass


def _episode_heading(show_name, video):
    """'<Show> \u2013 S01E02 <Episode Title>' for StreamsWindow's
    pre-agreed `heading` kwarg - the cross-agent contract every
    DetailWindow/ShowcaseWindow call site into `open_streams()` honors."""
    video = video or {}
    code_and_title = _episode_label(video).replace(' \u00b7 ', ' ', 1)
    return '%s \u2013 %s' % (show_name, code_and_title) if show_name else code_and_title


def _show_art(meta):
    """`art={'poster': ..., 'fanart': ...}` for StreamsWindow's
    pre-agreed `art` kwarg - the poster/fanart fields of
    `lib.ui.playbackmeta.resolve_art()`, the same background > logo >
    poster fallback `DetailWindow.onInit()` uses for its own
    background image, so both agree byte-for-byte instead of each
    keeping its own copy of the fallback order. Only `poster`/`fanart`
    are forwarded (never `resolve_art()`'s `icon`/`thumb` extras) to
    keep StreamsWindow's pre-agreed `art` kwarg shape unchanged."""
    resolved = resolve_art(None, meta)
    poster = resolved.get('poster')
    return {'poster': poster, 'fanart': resolved.get('fanart') or poster}


def _metadata_line(meta, season_count):
    """'2025 \u00b7 2 SEASONS \u00b7 [COLOR FFEDEDED]\u2605 7.6[/COLOR]'
    for the left column (METADATA_LINE/30101) - built from series-level
    meta only, never a per-episode video. Any segment whose source
    value is missing (no year, no real season yet, no rating) is
    skipped outright rather than left as a dangling ' \u00b7 ' - the
    same join-only-what-exists shape
    lib.ui.streamswindow._rebuild_list() already uses for its own
    year/rating/genres info panel.

    A still-running series' open-ended range is closed with the
    localized "now" rather than left as a dangling dash - see
    `lib.ui.playbackmeta.year_range()`, which StreamsWindow prints
    from too, so the screens agree on how a running series prints its
    years."""
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
        segments.append('[COLOR FFEDEDED]\u2605 %s[/COLOR]' % escape_label(str(rating)))
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
    return ' \u00b7 '.join(escape_label(genre) for genre in genres)


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


def _prefetch_s4me_streams(stype, video_id):
    """Best-effort, non-blocking warm-up of the S4Me bridge's own
    per-stream cache (see resources/s4me_bridge/bridge.py's 30-minute
    cache) for `video_id`, fired from a background daemon thread while
    the user is still browsing this episode list rather than waiting on
    it once they actually open the streams picker.

    Only ever targets the builtin `flags.builtin == lib.s4me.BUILTIN_ID`
    addon entry (see `lib.store.Store.set_builtin_addon`) - firing an
    extra request at every OTHER installed addon just to warm one of
    them would be needlessly invasive, and this addon's own transport
    url/cache semantics are the only ones this is even trying to help.
    `Store.get_enabled_addons()` already excludes a disabled/offline
    entry, so a bridge the user turned off (or that is not currently
    running) is silently skipped, exactly like every other addon fetch
    already treats it.

    No-ops immediately (no thread spawned) for a falsy `video_id` -
    nothing to prefetch, e.g. a season with zero episodes. Every
    failure inside the background thread (no such addon, a dead/slow
    bridge, a malformed response) is swallowed silently, with no
    `xbmc.log()` call from inside the thread at all: this exists purely
    to warm a cache with no actionable signal of its own - the real,
    user-visible fetch `lib.ui.streamswindow.open_streams()` makes
    later is what must actually succeed or fail visibly, not this one -
    and a background thread reaching back into `xbmc`/`lib.ui.compat`
    well after this window (and its test harness, if any) may already
    have torn down is one dependency fewer to get wrong.
    """
    if not video_id:
        return

    def _worker():
        try:
            from lib.s4me import BUILTIN_ID
            from lib.ui.dependencies import get_client, get_store
            addon = next(
                (a for a in get_store().get_enabled_addons()
                 if (a.get('flags') or {}).get('builtin') == BUILTIN_ID),
                None,
            )
            transport_url = addon.get('transportUrl') if addon else None
            if not transport_url:
                return
            get_client().streams(transport_url, stype, video_id)
        except Exception:  # noqa: BLE001 - best-effort cache warm-up only, never surfaces
            pass

    threading.Thread(target=_worker, daemon=True).start()



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
        #: The parsed links behind `CREDITS`' chips, by list position.
        self._credit_links = []
        #: `(season_index, row)` of the episode the user last opened, so
        #: `onInit()` can put the cursor back there when Kodi re-runs it
        #: after a playback or trailer round trip.
        self._resume_position = None
        self.should_close_caller = False
        self._watched_ids = set()
        #: Worker->GUI handoff for the open-time library fetch (see
        #: `_start_watched_fetch()`), same queue + `Action(noop)` shape as
        #: `StreamsWindow`'s live merge. `_watched_epoch` is bumped under
        #: `_watched_lock` by every authoritative writer of `_watched_ids`
        #: (`_remember_watched()`), by `_start_watched_fetch()` and by
        #: `close()`: a fetch started under an older epoch is stale and
        #: its result is dropped instead of queued or applied.
        self._watched_lock = threading.Lock()
        self._watched_epoch = 0
        self._watched_pending = None
        self._watched_thread = None

    def start(self, meta, stype):
        """doModal() showing `meta`'s episode list. Returns True if
        playback started somewhere down the chain (the caller should
        also close). Also fires `_prefetch_s4me_streams()` for the
        first episode of the default season - the one already selected
        (row 0) the moment this window is shown - so its cache entry is
        warm by the time the user actually picks it."""
        self.meta = meta or {}
        self.stype = stype
        self.should_close_caller = False
        self.videos = _ordered_videos(self.meta.get('videos'))
        self.rows = [(video.get('id'), _episode_label(video)) for video in self.videos]
        self._video_by_id = {video.get('id'): video for video in self.videos}
        self.season_groups = _group_by_season(self.meta.get('videos'))
        self.season_index = self._default_season_index()
        self._watched_ids = set()
        active = self._active_videos()
        _prefetch_s4me_streams(self.stype, active[0].get('id') if active else None)
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
        raw_poster = self.meta.get('poster')
        fallback = raw_poster.strip() if isinstance(raw_poster, str) else ''
        items = []
        for video in videos:
            item = xbmcgui.ListItem(_episode_label(video))
            properties = _episode_properties(video, fallback)
            properties['row_id'] = video.get('id')
            progress = self._episode_progress(self.stype, sid, video.get('id'))
            percent = _watched_percent(progress)
            properties['watched_percent'] = L(_WATCHED_STRING_ID) % percent if percent is not None else ''
            properties['watched'] = '1' if episode_ticked(percent) else ''
            properties['aired'] = _short_date(properties.get('aired'))
            # One setProperties() call instead of one setProperty() call
            # per key - each is a Python->C++ boundary crossing.
            if video.get('id') in self._watched_ids:
                properties['watched'] = '1'
            item.setProperties(properties)
            if video.get('id') in self._watched_ids:
                from lib.ui.compat import set_watched
                set_watched(item, True)
            if percent is not None:
                # The same resume badge any Kodi video plugin uses on
                # a "continue watching" row - independent of this
                # addon's own `state.watched` bitfield (lib.library
                # never touches that; see its module docstring).
                from lib.ui.compat import set_resume_point
                set_resume_point(
                    item,
                    progress['position_ms'] / 1000.0,
                    progress['duration_ms'] / 1000.0,
                )
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
        from lib.ui.compat import log
        from lib.ui.dependencies import get_store
        try:
            return get_store().get_progress(stype, sid, video_id)
        except Exception as exc:
            log('detailwindow: get_progress failed for %r/%r/%r: %r' % (stype, sid, video_id, exc))
            return None

    def _remember_position(self):
        """Note the focused episode row so a round trip through the
        player or the trailer lands back on it (see `onInit()`)."""
        row = self.getControl(LIST).getSelectedPosition()
        self._resume_position = (self.season_index, max(row, 0))

    def _populate_episode_list(self, videos, select=0):
        """Replace `LIST`'s contents with `videos`' rows, select row
        `select` (the top by default), and refresh SEASON_COUNT's 'N EPISODES'
        readout to match - used for the initial populate and for every
        season switch alike, so the count always tracks whichever
        season is actually on screen."""
        control = self.getControl(LIST)
        control.reset()
        control.addItems(self._build_episode_items(videos))
        control.selectItem(select if 0 <= select < len(videos) else 0)
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
        self._resume_position = None
        self._populate_episode_list(self._active_videos())

    def onInit(self):
        from lib.ui.compat import addon_fanart

        background = self.meta.get('background') or self.meta.get('logo') or self.meta.get('poster')
        self.getControl(BACKGROUND).setImage(
            sized_image(background, 'backdrop') if background else addon_fanart()
        )
        self.getControl(POSTER).setImage(sized_image(self.meta.get('poster'), 'poster'))
        # [B]...[/B] applied here, not in the skin XML, because this
        # label is always Python-set: any markup baked into the
        # <label> itself would just be overwritten the moment this
        # call runs.
        self.getControl(HEADING).setLabel(
            '[B]%s[/B]' % escape_label((self.meta.get('name') or self.meta.get('id') or '').upper()),
        )
        _show_logo(self, self.meta.get('logo'))
        self.getControl(METADATA_LINE).setLabel(_metadata_line(self.meta, _season_count(self.season_groups)))
        self.getControl(GENRES_LINE).setLabel(_genres_line(self.meta))

        self._build_season_bar()
        self._credit_links = fill_credits(self, self.meta)
        select = 0
        if self._resume_position and self._resume_position[0] == self.season_index:
            select = self._resume_position[1]
        self._populate_episode_list(self._active_videos(), select)
        self.setFocusId(LIST)
        self._start_watched_fetch()

    def close(self):
        # Invalidate any in-flight watched fetch before the controls go
        # away: its result is then discarded by the worker, never queued
        # for a window that is no longer on screen.
        with self._watched_lock:
            self._watched_epoch += 1
            self._watched_pending = None
        super().close()

    def _start_watched_fetch(self):
        """Open-time watched state: when logged in, look up this title's
        library item on a daemon thread and let `_apply_watched_pending()`
        paint the episodes' `watched` marks once it lands. Logged out
        makes no request at all, and `onInit()` never waits on the
        network (the context menu's own lookup, which needs the answer to
        build its rows, is the blocking counterpart).

        The worker never touches a control or a ListItem: it only queues
        the item under `_watched_lock` and wakes the GUI thread with
        `Action(noop)` (a ListItem must not be written off the GUI
        thread, and the window may be gone by the time the answer
        arrives - `close()` bumps the epoch so a late answer is dropped).
        A failed lookup leaves the rows unmarked: no guessed state, and
        no log call from the thread (see `_prefetch_s4me_streams()` for
        the same reason)."""
        from lib.ui.compat import log
        from lib.ui.dependencies import get_api, get_store

        try:
            auth = get_store().get_auth()
        except Exception as exc:  # noqa: BLE001 - an unreadable local cache must never break onInit()
            log('detailwindow: auth lookup failed, watched marks skipped: %r' % (exc,))
            return
        video_id = self.meta.get('id')
        if not auth or not video_id:
            return
        auth_key = auth.get('authKey')
        with self._watched_lock:
            self._watched_epoch += 1
            epoch = self._watched_epoch
            self._watched_pending = None

        def _worker():
            try:
                item = get_api().get_library_item(auth_key, video_id)
            except Exception:  # noqa: BLE001 - best-effort paint; the context menu reports real failures
                return
            if not item:
                return
            with self._watched_lock:
                if epoch != self._watched_epoch:
                    return
                self._watched_pending = item
            try:
                import xbmc
                xbmc.executebuiltin('Action(noop)')
            except Exception:  # noqa: BLE001 - interpreter/Kodi teardown
                pass

        self._watched_thread = threading.Thread(target=_worker, daemon=True)
        self._watched_thread.start()

    def _apply_watched_pending(self):
        """Paint whatever `_start_watched_fetch()`'s worker queued - GUI
        thread only (`onAction()`'s `Action(noop)` drain)."""
        with self._watched_lock:
            item, self._watched_pending = self._watched_pending, None
        if item is not None:
            self._remember_watched(item)

    @gated_input
    def onAction(self, action):
        self._apply_watched_pending()
        action_id = action.getId()
        if action_id in _SEASON_NAV_ACTIONS and self.getFocusId() == SEASON_BAR:
            self._sync_season_from_bar()
        elif action_id in _SEASON_NAV_ACTIONS and self.getFocusId() == LIST:
            self._step_episode(-1 if action_id == 1 else 1)
        if action_id == _CONTEXT_MENU_ACTION:
            self._open_context_menu()
            return
        if action_id in BACK_ACTIONS:
            self.close()

    def _step_episode(self, delta):
        """Select the episode `delta` rows away, clamped to the season.

        `selectItem` scrolls a row that sits below the visible window
        into view. The list already holds every episode of the season;
        this only moves the cursor."""
        control = self.getControl(LIST)
        count = control.size()
        if count <= 0:
            return
        pos = control.getSelectedPosition()
        if pos < 0:
            pos = 0
        nxt = pos + delta
        if nxt < 0:
            nxt = 0
        elif nxt >= count:
            nxt = count - 1
        if nxt != pos:
            control.selectItem(nxt)

    def _open_credits(self):
        """'Cast & Crew' row of `_open_context_menu()`: `self.meta` is
        already the full meta `open_detail()` fetched to build this
        window, so - unlike ShowcaseWindow's own version of this
        affordance, where a catalog preview has no `links` - there is
        nothing left to fetch here."""
        from lib.ui.dependencies import get_client, get_store
        from lib.ui.infowindow import open_credits_picker

        open_credits_picker(get_store(), get_client(), self.meta)

    def _open_context_menu(self):
        """ACTION_CONTEXT_MENU (117): one `dialogs.choose()` actions menu
        for `self.meta` as a whole -- 'Cast & Crew' (the pre-existing,
        unchanged `_open_credits()` flow) is always row 0, followed by
        the library write rows this feature adds (Add/Remove library,
        Mark (un)watched), mirroring `lib.ui.addonswindow._open_actions()`'s
        own per-row `choose()` shape (`dialogs.choose(name, rows)` then
        dispatch by index) rather than a menu-of-menus.

        The Add/Remove and watched ROW WORDING must reflect the
        account's CURRENT state, never a locally-cached guess (the
        service's `library.json` copy can be a pull interval old, and a
        write built from it would overwrite newer state) --
        `_current_library_state()` fetches it fresh every time
        this menu opens, logged in or not. `lookup_ok` is forwarded
        unchanged to `_toggle_library()`/`_toggle_watched()`: a failed
        lookup still renders the (unavoidably guessed) default wording
        below, but the two mutation handlers refuse to act on that
        guess -- see `_current_library_state()`'s own docstring for why
        a failed lookup must never be treated as "no record"."""
        from lib.ui import dialogs
        from lib.ui.compat import L

        state = self._current_library_state()
        if state is CANCELLED:
            # Back on the lookup spinner: no menu, stay on the episode list.
            return
        auth, item, lookup_ok = state
        self._remember_watched(item)
        in_library = bool(item) and not item.get('removed')
        # `timesWatched > 0`, the account's own rule (`item_watched`): an
        # official client's unmark leaves `flaggedWatched` at 1, and reading
        # that flag offered "Mark as unwatched" on a series every other app
        # shows as unwatched.
        watched = item_watched(item)

        rows = [
            L(30196),
            L(30294) if in_library else L(30293),
            L(30296) if watched else L(30295),
            L(30499),
        ]
        picked = dialogs.choose(self.meta.get('name') or self.meta.get('id') or '', rows)
        if picked == 0:
            self._open_credits()
        elif picked == 1:
            self._toggle_library(auth, item, add=not in_library, lookup_ok=lookup_ok)
        elif picked == 2:
            self._toggle_watched(auth, item, watched=not watched, lookup_ok=lookup_ok)
        elif picked == 3:
            self._play_trailer()

    def _play_trailer(self):
        """The show's trailer (lib/ui/trailer.py). The episode list comes
        back where it was when the trailer stops."""
        from lib.ui.trailer import play_trailer

        self._remember_position()
        meta = self.meta if isinstance(self.meta, dict) else {}
        play_trailer(meta, meta.get('type') or self.stype, meta.get('id') or '')

    def _current_library_state(self):
        """`(auth, item, lookup_ok)` for `self.meta` -- `auth` is
        `Store.get_auth()`'s dict or `None` when logged out (skips the
        network call entirely: a write nobody's account can accept has
        nothing to look up, and `lookup_ok` is `True` in that case --
        there is nothing left ambiguous about it). When logged in,
        `item` is the account's live libraryItem record for this title
        (`None` meaning it genuinely has no record yet -- never
        watched, never added) and `lookup_ok` says whether that `item`
        value is trustworthy.

        A failed lookup returns `(auth, None, False)`, NOT the same
        `(auth, None)` shape a genuinely-absent record produces: those
        two outcomes used to collapse onto the same `item=None`
        sentinel, which is the actual bug this tri-state return fixes
        -- `_toggle_library()`/`_toggle_watched()` would otherwise
        build a FRESH payload from `self.meta` and have
        `libraryaction.push_write()` overwrite the account's existing
        server-side record (watch counts, timestamps, flags) because a
        transient network error looked identical to "no such record".
        Callers MUST check `lookup_ok` before trusting `item`, never
        just its truthiness.

        The network round trip itself runs under `run_cancellable()`
        (a worker thread behind a `busy_dialog()` spinner) so a slow
        lookup shows progress instead of a frozen window AND Back on the
        spinner stops the wait; in that case this returns `CANCELLED`
        (not the tuple) and the caller must open nothing."""
        import xbmc

        from lib.stremio.api import ApiError
        from lib.ui.compat import L, log
        from lib.ui.dependencies import get_api, get_store

        auth = get_store().get_auth()
        if not auth:
            return None, None, True
        try:
            lookup = run_cancellable(
                lambda: get_api().get_library_item(auth.get('authKey'), self.meta.get('id')), L(30033),
            )
            if lookup is CANCELLED:
                return CANCELLED
            item = lookup
        except ApiError as exc:
            log('detailwindow: library lookup failed: %r' % (exc,), xbmc.LOGWARNING)
            return auth, None, False
        return auth, item, True

    def _remember_watched(self, item):
        """Cache the episodes `item.state.watched` marks as watched, for
        `_build_episode_items()` to badge. An unreadable bitmap (or no
        item) caches nothing -- never a guessed mark."""
        ids = watched_video_ids(((item or {}).get('state') or {}).get('watched'), sorted_video_ids(self.videos))
        ids = ids or set()
        with self._watched_lock:
            # Authoritative answer: any open-time fetch still in flight
            # predates it and must not overwrite it.
            self._watched_epoch += 1
            self._watched_pending = None
        changed = ids != self._watched_ids
        self._watched_ids = ids
        if changed:
            self._paint_watched()

    def _paint_watched(self):
        """Re-mark the live `LIST` rows from `self._watched_ids` in place.
        Deliberately not `_populate_episode_list()`: a reset() would snap
        the selection back to the top under the user's cursor, which an
        open-time fetch landing late would do mid-scroll."""
        from lib.ui.compat import set_watched

        control = self.getControl(LIST)
        for position in range(control.size()):
            item = control.getListItem(position)
            watched = item.getProperty('row_id') in self._watched_ids
            item.setProperty('watched', '1' if watched else '')
            set_watched(item, watched)

    def _toggle_library(self, auth, item, add, lookup_ok):
        """Push an explicit `library_add_payload()` (built fresh from
        `self.meta` -- see that function's own docstring for why it
        never takes `item`) or a `library_remove_payload()` tombstone of
        the already-fetched `item` (only reachable with `add=False` when
        `_open_context_menu()` found `item` actually in the library, so
        it is never `None` here). Logged out notifies rather than
        failing silently or attempting a write with no `authKey` to send
        (the real API would just reject a `None` authKey as a normal
        auth error -- notifying directly here is both cheaper and
        clearer). `lookup_ok=False` (see `_current_library_state()`)
        notifies and bails the SAME way, before either payload builder
        runs -- a failed lookup's `item=None` is not "no record", and
        both payload builders would otherwise silently overwrite the
        account's actual server-side state. A network failure notifies
        too and returns before the success notification, so a failed
        write never leaves the user believing it landed."""
        from lib import library
        from lib.ui.compat import L, notify
        from lib.ui.libraryaction import push_write

        if auth is None:
            notify(L(30190))
            return
        if not lookup_ok:
            notify(L(30302))
            return
        payload = library.library_add_payload(self.meta) if add else library.library_remove_payload(item)
        if not push_write(auth, payload):
            return
        notify(L(30297) if add else L(30298))

    def _toggle_watched(self, auth, item, watched, lookup_ok):
        """Push a `mark_watched_payload()` built from the already-fetched
        `item`, or a fresh `build_library_item(self.meta)` when the
        account has no record at all yet (marking watched with no prior
        record is exactly the IMPLICIT `removed=True`/`temp=True` record
        `build_library_item()`'s own docstring describes stremio-core
        creating the first time a title is watched without ever being
        explicitly added). Same logged-out/lookup-failed/failed-write
        notifications as `_toggle_library()` -- `lookup_ok=False` must
        block the fresh-`build_library_item()` fallback too, since that
        fallback is only correct when the account truly has no record,
        which a failed lookup never establishes."""
        from lib import library
        from lib.ui.compat import L, notify
        from lib.ui.libraryaction import push_write

        if auth is None:
            notify(L(30190))
            return
        if not lookup_ok:
            notify(L(30302))
            return
        base = item if item is not None else library.build_library_item(self.meta)
        payload = library.mark_watched_payload(base, watched, video_ids=sorted_video_ids(self.videos))
        if not push_write(auth, payload):
            return
        self._remember_watched(payload)
        notify(L(30299) if watched else L(30300))

    @gated_input
    def onClick(self, control_id):
        if control_id == OPTIONS:
            self._open_context_menu()
            return
        if control_id == TRAILER:
            self._play_trailer()
            return
        if control_id == CREDITS:
            open_credit(self, self._credit_links)
            return
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
        self._remember_position()

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


#: How quickly `_fetch_meta_any_type()`'s first attempts must come back
#: empty for it to also try the other of movie/series. Each attempt is a
#: fan-out over every addon with its own 15s timeout, so retrying after
#: a slow miss could double the wait on a weak ARM box.
_SWAPPED_TYPE_MAX_SECONDS = 3.0

def _canonical_type(stype):
    """`stype` as Stremio's own ``movie``/``series`` where a provider list
    names it loosely (``Movies``, ``tv``, ``shows``...); any other type
    reduced to its base (`_base_type()`)."""
    base = _base_type(stype)
    return {'movies': 'movie', 'film': 'movie', 'films': 'movie', 'shows': 'series', 'show': 'series',
            'tvshows': 'series', 'tv': 'series'}.get(base, base)

def _fetch_meta_any_type(stype, sid):
    """``(meta, type)`` for `sid`: as `stype` first, then as its base type
    (``movies`` -> ``movie``), since provider lists label types loosely.
    The other of movie/series is tried last, and only when those attempts
    came back empty within `_SWAPPED_TYPE_MAX_SECONDS`: a quick "nobody
    has it" is a mislabelled type worth one more fan-out, a slow one
    (addons timing out on a weak box) is not. A type no enabled addon
    serves costs no request (`views._fetch_meta()` finds no targets).
    ``(None, stype)`` when nothing answers; `CANCELLED` on Back."""
    from lib.ui.compat import L
    from lib.ui.views import _fetch_meta

    kinds = []
    base = _canonical_type(stype)
    for kind in (stype, base):
        if kind and kind not in kinds:
            kinds.append(kind)
    swapped = {'movie': 'series', 'series': 'movie'}.get(base, '')
    if swapped in kinds:
        swapped = ''

    import xbmc

    from lib.ui.compat import log

    log('detailwindow: looking %s up as %s%s' % (
        sid, ', then '.join(kinds), (' (then %s after a quick miss)' % swapped) if swapped else ''), xbmc.LOGINFO)

    def _work():
        started = time.monotonic()
        for kind in kinds:
            meta = _fetch_meta(kind, sid)
            if meta:
                return meta, kind
        if swapped and time.monotonic() - started < _SWAPPED_TYPE_MAX_SECONDS:
            meta = _fetch_meta(swapped, sid)
            if meta:
                return meta, swapped
        return None, stype

    # One spinner for every type tried.
    return run_cancellable(_work, L(30033))


def _resolve_meta(stype, sid, preview):
    """``(meta, type, id)`` for a title, trying its id as each plausible
    type, then (for an id that is not IMDb-keyed) the IMDb id of the same
    title. ``meta`` is None when nothing answers; `CANCELLED` on Back."""
    import xbmc

    from lib.ui.compat import log

    found = _fetch_meta_any_type(stype, sid)
    if found is CANCELLED:
        return CANCELLED
    if found[0] or str(sid or '').startswith('tt'):
        return found[0], found[1], sid
    # A provider list's own id (tmdb:...) that no meta addon takes: open
    # the same title by its IMDb id, as a search would.
    alternative = _imdb_alternative(stype, sid, preview)
    if alternative is CANCELLED:
        return CANCELLED
    if not alternative:
        return None, found[1], sid
    log('detailwindow: %s/%s opened as %s' % (stype, sid, alternative), xbmc.LOGINFO)
    again = _fetch_meta_any_type(stype, alternative)
    if again is CANCELLED:
        return CANCELLED
    if again[0]:
        return again[0], again[1], alternative
    return None, found[1], sid


def _imdb_alternative(stype, sid, preview):
    """The ``tt`` id for a title whose own id no meta addon resolves: the
    IMDb id its catalog preview names, else the same title (name, type,
    year) from a search. ``''`` when neither finds it; `CANCELLED` on
    Back."""
    from lib.ui import previews
    from lib.ui.streamswindow import imdb_id_for

    named = imdb_id_for(preview)
    if named:
        return named
    if not preview.get('name'):
        return ''
    from lib.ui.dependencies import get_client, get_store
    from lib.ui.searchwindow import SearchReport, run_query

    report = SearchReport()
    try:
        found = run_query(get_store(), get_client(), preview.get('name'), report=report)
    except Exception as exc:  # noqa: BLE001 - the lookup is a fallback and must not break opening
        import xbmc

        from lib.ui.compat import log

        # The type name only, like run_query()'s own failure log: an
        # addon error's text can carry its transport URL.
        log('detailwindow: IMDb lookup by search for %s/%s failed: %s' % (stype, sid, type(exc).__name__),
            xbmc.LOGWARNING)
        return ''
    if report.canceled and not found:
        return CANCELLED
    # A provider list's "tv" is the search's "series".
    return previews.best_match(preview, found, _canonical_type(stype))


def open_detail(stype, sid):
    """Fetch (stype, sid)'s full meta. A movie (no `videos`) has nothing
    to pick, so it opens StreamsWindow directly; a series opens this
    window first to pick an episode. Returns True if playback started
    somewhere down the chain (the caller should also close)."""
    import xbmc

    from lib.ui import previews
    from lib.ui.compat import L, log, notify

    preview = previews.lookup(sid) or {}
    log('detailwindow: opening %s/%s from the list (list type %s, name %s, IMDb id %s)' % (
        stype or '-', sid, preview.get('type') or '-', preview.get('name') or '-',
        preview.get('imdb_id') or preview.get('imdbId') or '-'), xbmc.LOGINFO)
    if not stype:
        # A catalog that left `type` off its previews.
        stype = preview.get('type') or 'movie'
    channel = dict(preview, id=sid, type=stype)
    if livetv.is_live(channel):
        # A live channel has no page: its description is the programme
        # now playing (the hero shows it) and the meta's `videos` are the
        # schedule, not episodes. One click goes to its streams, as
        # stremio-core's live items do. Every opener (Home, Search, the
        # catalog and credit links) comes through here.
        from lib.ui.streamswindow import open_live

        return open_live(
            stype, sid, poster=channel.get('poster') or channel.get('logo'),
            heading=channel.get('name') or sid, art=_show_art(channel), meta=channel,
        )
    resolved = _resolve_meta(stype, sid, preview)
    if resolved is CANCELLED:
        log('detailwindow: meta fetch for %s/%s cancelled by the user' % (stype, sid), xbmc.LOGINFO)
        return False
    meta_obj, stype, sid = resolved
    if not meta_obj and preview and _base_type(stype) not in ('series', 'tv'):
        # The coverflow opens a movie's streams straight from its
        # preview; do the same when no addon has a meta for it.
        log('detailwindow: no meta for %s/%s; opening its streams from the catalog preview' % (stype, sid),
            xbmc.LOGINFO)
        from lib.ui.streamswindow import open_streams

        movie = dict(preview, id=sid, type=stype)
        return open_streams(
            stype, sid, poster=movie.get('poster'), heading=movie.get('name') or sid,
            art=_show_art(movie), meta=movie,
        )
    if not meta_obj:
        log('detailwindow: no addon returned a meta for %s/%s' % (stype, sid), xbmc.LOGWARNING)
        notify(L(30030))
        return False

    videos = meta_obj.get('videos')
    log('detailwindow: %s/%s resolved to a meta with id %s and %d episode(s)' % (
        stype, sid, meta_obj.get('id'), len(videos) if isinstance(videos, list) else 0), xbmc.LOGINFO)
    from lib.ui.streamswindow import open_streams
    if not videos:
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
        close_quietly(win)
