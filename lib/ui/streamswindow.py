"""StreamsWindow: the resolved-source picker for one title/episode -
Rivulet's custom replacement for the classical `streams()` directory.

Picking a row resolves and plays it DIRECTLY via
`lib.ui.player.play_direct` (no ADDON_HANDLE/`setResolvedUrl` - see that
function's docstring), so Kodi's player takes over the full screen.

Once playback actually starts and later stops - or never starts at all
within a short timeout, see `_wait_for_playback_end()` - `open_streams()`
reopens a fresh `StreamsWindow` over the SAME already-fetched
pairs/heading/art/poster (no addon re-fetch) so the user lands back on
the picker instead of falling all the way through to Kodi's main menu;
"Back" out of THAT reopened window is what finally returns control to
whatever opened `open_streams()` in the first place. Consequently
`open_streams()` now only ever returns False: once for the user backing
out of a window (the first one, or a reopened one) with no pick, and
once more for a fetch/window failure - it never returns True. Every
`if open_streams(...): self.close()` (or `return open_streams(...)`)
branch up the call chain (`DetailWindow`, `CatalogPickerWindow`,
`SearchWindow` via `open_detail`) is consequently dormant: those
callers stay open underneath for the round trip's "reopen" to sit on
top of, and simply resume (natural Back navigation) once
`open_streams()` finally returns.

`open_streams()`/`StreamsWindow.start()` also take optional `heading`/
`art` context kwargs (`heading='<title>'`, `art={'poster': ...,
'fanart': ...}`) - the pre-agreed cross-agent contract `DetailWindow`
(an episode's "<Show> - SxxExx <Title>" + the show's own art) and
`ShowcaseWindow`'s movie path (the movie's own title/art) both call into.
Both default to "nothing supplied" (`''`/`None`) so a bare `poster=`
kwarg, or no context at all, keeps every pre-existing call site working
unchanged: an empty heading falls back to a generic localized "Streams"
title, and no `art` simply means the side poster panel stays empty.

`meta` is a similar optional caller-context kwarg - the same Stremio
meta dict `DetailWindow`/`ShowcaseWindow`/`open_detail()` already have in
hand - rendered read-only into the side info panel (year/runtime/
rating/genres, plus a single-provider dedupe note); omitted or `None`
leaves that panel empty exactly like today.

`open_streams()`/`StreamsWindow.start()` also take an optional
`video_id=None` kwarg - the id of the episode whose streams are being
shown, threaded from `lib.ui.detailwindow`'s episode click (the only
caller that has one; a movie or any other context-free call keeps
defaulting to `None`, so nothing below ever runs for it). Once playback
of that episode ends NATURALLY (played through to completion, not
stopped by the user - see `_wait_for_playback_end()`), if it's a
series, the 'binge_enable' setting is on, `meta['videos']` has a next
episode (`lib.ui.binge.next_video()`), and that episode's own streams
can be fetched, `_try_binge_watch()` shows a cancellable/skippable countdown
offering to auto-play it - see that
function's docstring for exactly how it reuses `play_direct()`'s
`item_meta=`/`on_ready=close_windows_for_playback` contract and loops
for the episode after that. The user stopping the episode instead of
letting it end, cancelling the countdown, a monitor abort, no next
episode, or no fetchable stream for it all fall back to the
SAME "reopen the picker" round trip described above - `open_streams()`
still only ever returns False.

Every installed addon's own stream fetch runs CONCURRENTLY, not one at
a time: real user logs showed the old serial `for` loop - each addon
call bounded by `AddonClient`'s own 15s timeout - taking as long as
27.6s to open this picker, and 5.0s even in the ordinary case, because
one slow or unresponsive addon held up every addon queued behind it
(`streamswindow: 1 addon(s) failed` on every single fetch, whether or
not that addon was the slow one). `_fetch_stream_pairs()` (still used,
unchanged, by `_try_binge_watch()`) fetches every addon concurrently
but still blocks until all of them answer; `open_streams()` goes
further and opens the picker as soon as the FIRST addon answers,
letting the rest stream their own results in live onto the open
`StreamsWindow` (`StreamsWindow.add_pairs()`/`set_loading()`) exactly
like `infowindow.ShowcaseWindow`'s background meta-enrich worker feeds
an already-open coverflow - see that module's docstring and
`add_pairs()`'s own docstring below for the worker->GUI handoff both
share. That live phase only ever feeds the FIRST window a picker opens;
every reopen after a played pick (or a binge-watched one) works off
whatever `StreamsWindow.pairs` had already accumulated by then, a
static snapshot, never resumes listening for more addons.
"""
import queue
import threading

import xbmcgui

from lib.stremio import streaminfo
from lib.ui.binge import next_video, pick_binge_stream
from lib.ui.dependencies import get_client, get_store
from lib.ui.uicommon import BaseWindow, busy_dialog, close_windows_for_playback, open_window

BACKGROUND = 30000
LIST = 30002
POSTER = 30004
HEADING = 30005
INFO_PANEL = 30008
#: The left column's "NN SOURCES" / "NN ADDONS" / "NN CACHED" summary -
#: three separate labels (see StreamsWindow.xml) rather than 3 lines of
#: INFO_PANEL, since a textbox only supports one font/colour for its
#: whole content and the CACHED line needs its own green tint.
SOURCES_COUNT = 30100
ADDONS_COUNT = 30101
CACHED_COUNT = 30102

#: Cap on concurrent addon HTTP calls a streams fan-out below opens at
#: once - mirrors `lib.ui.views._MAX_ADDON_WORKERS`'s bounded-pool
#: convention (kept as its own local constant, since each fan-out point
#: in this addon bounds its OWN pool rather than sharing one - this one
#: also runs on low-power ARM boxes, where an unbounded thread-per-addon
#: fan-out would be its own liability). Each AddonClient call still
#: carries its own 15s timeout; this only lets that timeout run
#: concurrently across addons instead of serializing N of them one
#: after another - see the module docstring for the measured 5.0s/
#: 27.6s cost of the old serial loop this replaces.
_MAX_STREAM_ADDON_WORKERS = 8

#: strings.po id for the transient "more sources still loading" line
#: `StreamsWindow._rebuild_list()` appends to INFO_PANEL while
#: `open_streams()`'s fan-out still has an addon outstanding.
_LOADING_STRING_ID = 30185

#: strings.po ids for the left column's localized 'N SOURCE(S)' / 'N
#: ADDON(S)' / 'N CACHED' summary counts (see _rebuild_list()) -
#: singular msgstr picked for a count of exactly 1, plural otherwise
#: (0 included, matching the plural rule every target language's own
#: translation uses for "not one"). CACHED has only one form: it
#: describes how many of the sources are cached, not a countable noun
#: of its own.
_SOURCE_STRING_ID = 30200
_SOURCES_STRING_ID = 30201
_ADDON_STRING_ID = 30202
_ADDONS_STRING_ID = 30203
_CACHED_STRING_ID = 30208

#: strings.po id for the range terminator a still-running series gets in
#: place of a dangling dash ('2022–now') - see `_rebuild_list()`. The
#: same id DetailWindow and the coverflow hero use.
_NOW_STRING_ID = 30234

#: resources/settings.xml keys for the four 'playback_*' stream-filter
#: controls (see that file's 'playback' category). Read fresh on every
#: `_rebuild_list()` call by `_read_stream_filters()` below rather than
#: cached anywhere on the window - Kodi's Settings dialog is its own
#: modal the user can open and change these in without ever closing
#: this picker first.
_MIN_QUALITY_SETTING = 'playback_min_quality'
_MAX_SIZE_GB_SETTING = 'playback_max_size_gb'
_EXCLUDE_CAM_SETTING = 'playback_exclude_cam'
_CACHED_ONLY_SETTING = 'playback_cached_only'

#: `playback_min_quality`'s select values -> `streaminfo.filter_streams()`'s
#: `min_height`. 'any' (and anything unrecognised, e.g. a stale value
#: left over from a settings.xml downgrade) maps to 0, filter_streams()'s
#: own "no filtering on this axis" sentinel.
_MIN_QUALITY_HEIGHTS = {'480p': 480, '720p': 720, '1080p': 1080, '2160p': 2160}

#: strings.po ids for the INFO_PANEL's "N hidden by filters" line and the
#: "filters matched nothing" fallback notice - see `_rebuild_list()`'s
#: filtering step. Singular/plural picked the same way as
#: `_SOURCE_STRING_ID`/`_SOURCES_STRING_ID` above.
_HIDDEN_STRING_ID = 30262
_HIDDEN_PLURAL_STRING_ID = 30263
_FILTERS_MATCHED_NOTHING_STRING_ID = 30264

#: Brief settle pause before reopening the picker after playback ends -
#: gives Kodi's player teardown a moment to finish before a fresh modal
#: window is drawn on top of it. Also reused as the settle pause after
#: each binge-watching auto-played episode, below.
_REOPEN_SETTLE_SECONDS = 0.5

#: resources/settings.xml keys for the two binge-watching controls.
_BINGE_ENABLE_SETTING = 'binge_enable'
_BINGE_COUNTDOWN_SETTING = 'binge_countdown'

#: Fallback/floor for the 'binge_countdown' setting - mirrors its own
#: <default>/<constraints><minimum> in resources/settings.xml.
_BINGE_COUNTDOWN_DEFAULT_SECONDS = 10
_BINGE_COUNTDOWN_MIN_SECONDS = 3

#: strings.po ids for the countdown dialog: the heading, and the
#: '%s'/'%d'-parameterized "Playing <label> in <N> s" message, which
#: `_binge_countdown()` re-formats on every tick because the remaining
#: seconds are part of the sentence.
_BINGE_DIALOG_HEADING_STRING_ID = 30182
_BINGE_DIALOG_MESSAGE_STRING_ID = 30183


def _read_stream_filters():
    """Read the four 'playback_*' settings into
    `streaminfo.filter_streams()`'s kwargs (see the constants above).
    `playback_max_size_gb` is a GB slider - a human picks a size in GB,
    not bytes - converted here to the byte unit `filter_streams()`/
    `parse_stream()`'s own `size_bytes` already uses."""
    from lib.ui.compat import ADDON, setting_bool, setting_int

    min_quality = ADDON.getSetting(_MIN_QUALITY_SETTING) or 'any'
    max_size_gb = setting_int(_MAX_SIZE_GB_SETTING, 0, minimum=0)
    return {
        'min_height': _MIN_QUALITY_HEIGHTS.get(min_quality, 0),
        'max_size_bytes': max_size_gb * 1024 ** 3,
        'exclude_cam': setting_bool(_EXCLUDE_CAM_SETTING, False),
        'cached_only': setting_bool(_CACHED_ONLY_SETTING, False),
    }


class StreamsWindow(BaseWindow):
    """See module docstring. Built/run via `open_streams()`."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.pairs = []
        self.stype = 'movie'
        self.sid = None
        self.poster = None
        self.heading = ''
        self.art = None
        self.meta = None
        self.video_id = None
        self.played = False
        self.played_pair = None
        #: True once close() has run - add_pairs()/set_loading() become
        #: silent no-ops from then on, so a straggling addon answering
        #: after the user backed out (or after playback started) never
        #: touches a torn-down window. See close()/add_pairs().
        self._closed = False
        #: Guards `_pending_batches`/`_pending_loading` below - written
        #: by add_pairs()/set_loading() from a background fetch thread,
        #: drained by `_merge_pending()` on the GUI thread only. Mirrors
        #: `infowindow.ShowcaseWindow`'s `_enrich_lock`/`_enrich_pending`
        #: worker->GUI handoff exactly.
        self._pending_lock = threading.Lock()
        self._pending_batches = []
        self._pending_loading = None
        #: True while `open_streams()`'s own fan-out still has an addon
        #: outstanding - renders `_LOADING_STRING_ID`'s status line onto
        #: INFO_PANEL until the last one answers (`set_loading(False)`).
        self._loading = False
        #: The prefix of `self.pairs` (by OBJECT IDENTITY, oldest-first)
        #: LIST currently holds - set by `_rebuild_list()` every time it
        #: runs. `_append_prefix_length()` compares this against a fresh
        #: `self.pairs` to decide whether `_apply_pending()` can append
        #: just the new suffix instead of a full reset()+rebuild - see
        #: both methods.
        self._rendered_pairs = []
        #: The `single_provider` value `_rebuild_list()` used to build
        #: `self._rendered_pairs` above - a second addon's batch turning
        #: a single-provider list into a multi-provider one changes line
        #: 1 of every ALREADY-rendered row (the addon-name dedupe), so
        #: `_append_prefix_length()` must catch that even when every
        #: pair's own identity is unchanged.
        self._rendered_single_provider = None
        #: The `display_pairs` sequence (by OBJECT IDENTITY, in
        #: `self.pairs` order) `_rebuild_list()` last actually put into
        #: LIST - a strict superset check against `self._rendered_pairs`
        #: above is not enough: a pending batch can flip
        #: `_stream_filter_view()`'s "filters matched nothing" fallback
        #: from true to false (the batch gives a previously-starved
        #: filter something real to keep), which changes which of the
        #: OLD prefix's rows are still visible even though none of them
        #: moved and `single_provider` didn't change either -
        #: `_append_prefix_length()` compares against this to catch it.
        self._rendered_visible_pairs = []

    def start(self, pairs, stype, sid, poster=None, heading='', art=None, meta=None, video_id=None):
        """doModal() showing `pairs` (a list of `(info, stream)` as
        `lib.stremio.streaminfo.parse_stream`/`sort_streams` produce).
        `heading`/`art`/`meta` are the optional caller-context kwargs
        described in the module docstring; `video_id` is the episode
        `pairs` belongs to (`None` for a movie or a context-free call -
        see the module docstring's binge-watching paragraph). A caller
        wanting the picker to open before every addon has answered
        calls `set_loading(True)`/`add_pairs()` on this window BEFORE
        this method (see `open_streams()`) - neither is reset here, so
        anything already queued still lands in `onInit()`'s first
        build. Returns True if playback started (the caller should
        also close)."""
        self.pairs = list(pairs or [])
        self.stype = stype
        self.sid = sid
        self.poster = poster
        self.heading = heading or ''
        self.art = art
        self.meta = meta
        self.video_id = video_id
        self.played = False
        self.played_pair = None
        self._closed = False
        if not self.pairs:
            return False
        self.doModal()
        return self.played

    def close(self):
        # Any exit path must stop accepting further live updates before
        # the underlying window (and its controls) actually goes away -
        # see add_pairs()/set_loading()'s own no-op-after-close contract.
        self._closed = True
        super().close()

    def add_pairs(self, pairs):
        """Thread-safe: queue one addon's OWN batch of `(info, stream)`
        pairs - called by `open_streams()`'s streaming fan-out as each
        addon answers, from a background fetch thread, NEVER the GUI
        thread. Mirrors `infowindow.ShowcaseWindow._enrich_fetch()`'s
        worker->GUI handoff exactly: this never touches a control or
        `self.pairs` directly - it only queues the batch, then wakes the
        GUI thread with `Action(noop)`, which lands on `onAction()`'s
        own `_apply_pending()` drain below. A silent no-op once this
        window has closed, or for an empty batch."""
        if not pairs:
            return
        with self._pending_lock:
            if self._closed:
                return
            self._pending_batches.append(list(pairs))
        self._wake()

    def set_loading(self, loading):
        """Thread-safe toggle for the transient "more sources still
        loading" INFO_PANEL line (see `_rebuild_list()`) - `True` while
        `open_streams()`'s fan-out still has an addon outstanding,
        `False` once the last one has answered. Same worker->GUI
        handoff as `add_pairs()`; a silent no-op once this window has
        closed."""
        with self._pending_lock:
            if self._closed:
                return
            self._pending_loading = bool(loading)
        self._wake()

    def _wake(self):
        import xbmc
        xbmc.executebuiltin('Action(noop)')

    def _merge_pending(self):
        """Pull whatever `add_pairs()`/`set_loading()` queued onto
        `self.pairs`/`self._loading` - GUI thread only. Re-sorts the
        WHOLE list with the user's `stream_sort` setting, since a fast
        addon landing after a slow one can outrank rows already on
        screen. Returns True if anything actually changed, so a caller
        only rebuilds the list when there is something new to show."""
        with self._pending_lock:
            if not self._pending_batches and self._pending_loading is None:
                return False
            batches, self._pending_batches = self._pending_batches, []
            loading, self._pending_loading = self._pending_loading, None
        if batches:
            for batch in batches:
                self.pairs.extend(batch)
            from lib.ui.compat import ADDON
            sort_key = ADDON.getSetting('stream_sort') or 'quality'
            self.pairs = streaminfo.sort_streams(self.pairs, key=sort_key)
        if loading is not None:
            self._loading = loading
        return True

    def _apply_pending(self):
        """`onAction()`'s live-merge drain - GUI thread only. Rebuilds
        LIST/INFO_PANEL when `_merge_pending()` finds something new.
        `_rebuild_list()` takes an append-only fast path (see
        `_append_prefix_length()`) whenever the previously-rendered rows
        are still an untouched PREFIX of the freshly re-sorted
        `self.pairs` - the common case, since a fast addon landing after
        a slow one usually sorts after what is already on screen. That
        path never moves the focused row (nothing before `start` moved),
        so it goes straight to the same numeric-position restore the
        full-rebuild path below falls back to. Otherwise (a re-sort
        interleaved a new row somewhere inside the already-rendered
        range, or flipped the single-provider addon-name dedupe - see
        `_rebuild_list()`) falls back to the old full reset()+rebuild,
        re-finding the focused row by OBJECT IDENTITY (`is`, not `==` -
        two different streams can compare equal as `(info, stream)`
        tuples) so a re-sort never yanks the cursor out from under a
        user about to click. Falls back to the same numeric position
        (clamped to the new length) when the previously-focused pair is
        no longer in the list at all."""
        control = self.getControl(LIST)
        focused = control.getSelectedItem()
        focus_index = control.getSelectedPosition()
        focus_pair = None
        if focused is not None:
            try:
                pos = int(focused.getProperty('position'))
            except (TypeError, ValueError):
                pos = -1
            if 0 <= pos < len(self.pairs):
                focus_pair = self.pairs[pos]

        if not self._merge_pending():
            return

        start = self._append_prefix_length()
        self._rebuild_list(0 if start is None else start)
        if not self.pairs:
            return
        if start is None and focus_pair is not None:
            for index, pair in enumerate(self.pairs):
                if pair is focus_pair:
                    control.selectItem(index)
                    return
        control.selectItem(min(max(focus_index, 0), len(self.pairs) - 1))

    def _stream_filter_view(self):
        """`(matched_nothing, display_pairs, hidden_count,
        single_provider, provider_count)` for `self.pairs` and the
        CURRENT 'playback_*' filter settings - shared by
        `_rebuild_list()` (which renders `display_pairs`) and
        `_append_prefix_length()` (which must invalidate its append-only
        fast path the instant filtering changes which addon(s) drive
        the single-provider dedupe, exactly like a raw addon batch
        arriving already does). Computed fresh every call, never cached
        on the window - see `_read_stream_filters()`'s own docstring
        for why.

        `matched_nothing` is true only when the filtered result is
        EMPTY but `self.pairs` itself is not - the "misconfigured
        filter" escape hatch: `display_pairs` then falls back to every
        pair, UNFILTERED, rather than an empty list, so a filter that
        happens to match nothing (e.g. 'minimum quality 2160p' against
        an addon that only found 1080p) can never look indistinguishable
        from `open_streams()`'s own "no sources found" empty-result
        path. `hidden_count` is 0 in that case too - nothing was
        actually left out of what's shown."""
        filters = _read_stream_filters()
        kept_pairs = streaminfo.filter_streams(self.pairs, **filters)
        matched_nothing = bool(self.pairs) and not kept_pairs
        display_pairs = self.pairs if matched_nothing else kept_pairs
        hidden_count = 0
        if not matched_nothing:
            _total, hidden_count = streaminfo.filter_summary(len(self.pairs), len(kept_pairs))
        providers = {info.get('addon') for info, _stream in display_pairs if info.get('addon')}
        single_provider = next(iter(providers)) if len(providers) == 1 else None
        return matched_nothing, display_pairs, hidden_count, single_provider, len(providers)

    def _append_prefix_length(self):
        """Returns how many of `self.pairs`, counted from the front, are
        the SAME already-rendered run `_rebuild_list()` last built into
        LIST - safe for `_apply_pending()` to leave untouched and only
        append past - or `None` if a full reset()+rebuild is required.
        Three things invalidate the prefix: (1) the re-sort moved one of
        those pairs (checked by OBJECT IDENTITY, `is` - pairs are reused
        across a merge, never rebuilt, see `_merge_pending()`, so an
        unmoved row is still the exact same tuple at the exact same
        index), (2) the single-provider addon-name dedupe
        (`_rebuild_list()`'s `single_provider`) flipped - that changes
        line 1 of every ALREADY-rendered row too, e.g. the picker
        opening on one addon's own results (no addon segment shown) and
        a second addon's batch then arriving (now shown on every row,
        including the ones already on screen) - or (3) which of the OLD
        prefix's pairs `_stream_filter_view()` actually DISPLAYS
        changed, even though none of them moved and `single_provider`
        stayed put: its "filters matched nothing" fallback can flip from
        true to false the instant this same batch gives a previously-
        starved filter something real to keep, which means some rows
        the fallback was showing unfiltered a moment ago are now hidden
        by the very filter that just started matching. Appending only
        the new suffix in that case would leave those stale fallback
        rows on screen forever, since nothing past `start` ever gets
        re-examined - see `self._rendered_visible_pairs`."""
        prev = self._rendered_pairs
        if not prev or len(prev) > len(self.pairs):
            return None
        for old, new in zip(prev, self.pairs):
            if old is not new:
                return None
        _matched_nothing, display_pairs, _hidden_count, single_provider, _n = self._stream_filter_view()
        if single_provider != self._rendered_single_provider:
            return None
        prev_ids = {id(pair) for pair in prev}
        visible_prefix = [pair for pair in display_pairs if id(pair) in prev_ids]
        if len(visible_prefix) != len(self._rendered_visible_pairs):
            return None
        for old, new in zip(self._rendered_visible_pairs, visible_prefix):
            if old is not new:
                return None
        return len(prev)

    def _rebuild_list(self, start=0):
        """Build LIST's rows from `self.pairs[start:]`, INFO_PANEL's text
        and the SOURCES_COUNT/ADDONS_COUNT/CACHED_COUNT summary labels
        from `self.meta`/`self.pairs` - shared by `onInit()` and
        `_apply_pending()`'s live merge so both build rows identically
        (single-provider dedupe included) instead of duplicating this
        logic. `start=0` (the default - `onInit()`'s first build, and
        `_apply_pending()`'s full-rebuild fallback) rebuilds every row
        via reset()+addItems(); `start` > 0 is `_apply_pending()`'s
        append-only fast path (see `_append_prefix_length()`) - only
        `self.pairs[start:]`'s rows get built and appended
        (`control.addItems()`, no `reset()`), instead of reconstructing
        Label/properties Kodi already has on screen for rows that never
        changed. Measured: a 400-stream, 20-addon-batch progressive
        fetch built 4200 rows total the old reset()+rebuild-everything-
        per-batch way (N*(M+1)/2), 400 with this path taken every time -
        it also removes the mid-scroll reset()/redraw that motivated
        `_apply_pending()`'s own focus-preservation workaround in the
        first place. INFO_PANEL and the summary counts below are always
        recomputed in full either way - they're label text, not per-row
        ListItem churn, so there is nothing to save there. Each row's
        ListItem carries both the packed Label/Label2
        (format_label()/format_details()) AND the discrete
        `streaminfo.stream_fields()` properties StreamsWindow.xml's row
        layout reads column-by-column; the packed pair is the skin's own
        fallback for a row with no discretely parsed quality/source/
        addon. Never touches focus - callers position the cursor
        themselves once this returns.

        Rows for a pair `streaminfo.filter_streams()` (via
        `_stream_filter_view()`) drops for the user's 'playback_*'
        settings are skipped here rather than removed from `self.pairs`
        itself - `position` stays a `self.pairs` index either way, so
        `_apply_pending()`'s focus-by-identity restore and this
        method's own append-only fast path both keep working exactly
        as if filtering didn't exist. When the filtered result would be
        EMPTY but `self.pairs` is not, `_stream_filter_view()` falls
        back to showing every pair unfiltered instead - a misconfigured
        filter must never look identical to `open_streams()`'s own "no
        sources found" empty-result path."""
        matched_nothing, display_pairs, hidden_count, single_provider, provider_count = self._stream_filter_view()
        kept_ids = {id(pair) for pair in display_pairs}

        items = []
        for index in range(start, len(self.pairs)):
            pair = self.pairs[index]
            if id(pair) not in kept_ids:
                continue
            info, _stream = pair
            # Multi-provider rows show the addon as a gray tail segment on
            # line 1 (format_label's own include_addon=True rendering);
            # once every pair is the SAME addon it's redundant there and
            # surfaces once instead as the info panel's 'via <addon>' line
            # below, so line 1 drops it (include_addon=False).
            line1 = streaminfo.format_label(info, include_addon=not single_provider) or info.get('raw') or '?'
            line1 = line1.replace('\r', ' ').replace('\n', ' ')
            # Line 2 is the re-derived detail line (audio/channels,
            # languages, bitrate, release tags, group, tracker) - see
            # streaminfo.format_details() - never the provider name.
            line2 = streaminfo.format_details(info).replace('\r', ' ').replace('\n', ' ')
            item = xbmcgui.ListItem(line1, label2=line2)
            # The packed label/label2 above stay the ultimate fallback
            # StreamsWindow.xml renders when a row has no discretely
            # parsed quality/source/addon at all (see the skin's own
            # comment) - `stream_fields()` never replaces them, it adds
            # the discrete per-column properties the new row layout
            # reads via $INFO[ListItem.Property(...)].
            properties = streaminfo.stream_fields(info)
            properties['position'] = str(index)
            item.setProperties(properties)
            items.append(item)
        control = self.getControl(LIST)
        if start == 0:
            # reset() before addItems(): a full rebuild - onInit()
            # reopening a screen force-closed for playback, or a live
            # add_pairs() merge whose re-sort touched the already-
            # rendered prefix - onto a retained list would double every
            # item. The append-only path above (start > 0) skips this:
            # its rows are new, past the end of what LIST already has.
            control.reset()
        control.addItems(items)
        self._rendered_pairs = list(self.pairs)
        self._rendered_single_provider = single_provider
        self._rendered_visible_pairs = list(display_pairs)

        from lib.ui.compat import L
        from lib.ui.playbackmeta import year_range

        meta = self.meta or {}
        # A still-running series' open-ended range is closed with the
        # localized "now" rather than left as a dangling dash - see
        # `playbackmeta.year_range()`; DetailWindow and the coverflow
        # hero print the same range from the same id.
        year = year_range(meta.get('releaseInfo') or meta.get('year') or '', L(_NOW_STRING_ID))
        runtime = meta.get('runtime') or ''
        top_line = ' \u00b7 '.join(part for part in (year, runtime) if part)
        lines = [top_line] if top_line else []
        rating = meta.get('imdbRating')
        if rating:
            lines.append('\u2605 %s' % rating)
        genres = (meta.get('genres') or [])[:3]
        if genres:
            lines.append(' / '.join(genres))
        if single_provider:
            lines.append('via %s' % single_provider)
        if matched_nothing:
            lines.append(L(_FILTERS_MATCHED_NOTHING_STRING_ID))
        elif hidden_count:
            lines.append(L(_HIDDEN_STRING_ID if hidden_count == 1 else _HIDDEN_PLURAL_STRING_ID) % hidden_count)
        if self._loading:
            lines.append(L(_LOADING_STRING_ID))
        self.getControl(INFO_PANEL).setText('\n'.join(lines))

        # Left column summary counts (design's stacked 'NN SOURCES' /
        # 'NN ADDONS' / 'NN CACHED' lines) - their own labels, not more
        # INFO_PANEL text, since INFO_PANEL is a textbox (one font/colour
        # for its whole content) and CACHED needs its own green tint.
        # `provider_count` is the distinct-addon count already computed
        # by `_stream_filter_view()` above for the single-provider
        # dedupe. Singular/plural msgstr picked per count - '1 ADDONS'
        # is wrong in English, and worse once translated, since most
        # locales don't pluralize like English.
        cached_count = sum(1 for info, _stream in display_pairs if info.get('cached') is True)
        source_count = len(display_pairs)
        addon_count = provider_count
        self.getControl(SOURCES_COUNT).setLabel(
            L(_SOURCE_STRING_ID if source_count == 1 else _SOURCES_STRING_ID) % source_count
        )
        self.getControl(ADDONS_COUNT).setLabel(
            L(_ADDON_STRING_ID if addon_count == 1 else _ADDONS_STRING_ID) % addon_count
        )
        self.getControl(CACHED_COUNT).setLabel(L(_CACHED_STRING_ID) % cached_count)

    def onInit(self):
        from lib.ui.compat import L, addon_fanart

        # Pick up anything open_streams() queued via set_loading()/
        # add_pairs() before doModal() actually opened this window, so
        # the ONE initial build below already reflects it.
        self._merge_pending()

        art = self.art or {}
        background = art.get('fanart') or art.get('poster') or self.poster or addon_fanart()
        self.getControl(BACKGROUND).setImage(background)
        self.getControl(POSTER).setImage(art.get('poster') or self.poster or '')
        self.getControl(HEADING).setLabel((self.heading or L(30041)).upper())

        self._rebuild_list()
        self.setFocusId(LIST)

    def onAction(self, action):
        # Drain first, on the GUI thread, exactly like
        # infowindow.ShowcaseWindow.onAction() calls _apply_enriched()
        # before its own per-focus work - see add_pairs()/set_loading().
        self._apply_pending()
        super().onAction(action)

    def onClick(self, control_id):
        if control_id != LIST:
            return
        focused = self.getControl(LIST).getSelectedItem()
        if focused is None:
            return
        info, stream = self.pairs[int(focused.getProperty('position'))]

        # item_meta carries the content title/art/meta this window
        # already has in hand (see the module docstring's heading/art/
        # meta kwargs) into the player's OSD - only the keys actually
        # known are included, so a bare "no context" open still behaves
        # exactly like item_meta=None to play_direct(). on_ready fires
        # right before Kodi actually starts playing (play_direct()'s own
        # docstring), and _close_for_player_handoff() below is what
        # tears down every OTHER live screen AND this picker itself at
        # that exact instant, so no Rivulet modal - not even this one -
        # is still live when xbmc.Player().play() runs. This window is
        # closed WITHOUT _closed_for_playback, unlike its ancestors, so
        # ModalStackWindow.doModal() does not immediately reopen it;
        # open_streams()'s own reopen loop is what brings the picker
        # back once playback actually ends.
        item_meta = {}
        label = self.heading or (self.meta or {}).get('name') or ''
        if label:
            item_meta['label'] = label
        art = self.art or ({'poster': self.poster} if self.poster else None)
        if art:
            item_meta['art'] = art
        if self.meta:
            item_meta['meta'] = self.meta

        from lib.ui.player import play_direct
        if play_direct(
            stream, self.stype, self.sid, item_meta=item_meta,
            on_ready=lambda: _close_for_player_handoff(self),
            video_id=self.video_id,
        ):
            self.played = True
            self.played_pair = (info, stream)
            self.close()  # no-op: on_ready above already closed this window


def _close_for_player_handoff(picker):
    """`play_direct()`'s `on_ready` hook for `StreamsWindow.onClick()`:
    fires immediately before `xbmc.Player().play()` (see that
    function's docstring), so this is the ONE moment every live Rivulet
    modal - including `picker` itself - must already be gone, or Kodi
    keeps routing play/pause and the OSD to whichever WindowXMLDialog
    is still topmost rather than to the player (see uicommon's module
    docstring).

    `close_windows_for_playback(exclude=picker)` tears down every OTHER
    live screen, marking each ancestor `_closed_for_playback` so
    `ModalStackWindow.doModal()` restores it once `open_streams()`'s own
    post-playback reopen loop brings a fresh picker back. `picker`
    itself is then closed the same plain way `onAction()`'s Back
    handling does - deliberately WITHOUT `_closed_for_playback`, since
    that flag would also trip `ModalStackWindow.doModal()`'s own reopen
    loop and pop a second, premature picker up immediately behind the
    player; bringing the picker back afterwards is `open_streams()`'s
    reopen loop's job alone.
    """
    close_windows_for_playback(exclude=picker)
    picker.close()


def _wait_for_playback_end(player=None, monitor=None, start_timeout=20.0, tick=0.5):
    """Block until playback `open_streams()` just started has both begun
    and ended, so it can safely reopen the streams picker underneath
    Kodi's player instead of unwinding the whole custom-window stack.

    `play_direct()`/`xbmc.Player().play()` is fire-and-forget - there is
    a short real-world gap before `xbmc.Player().isPlaying()` actually
    reports True - so this first polls up to `start_timeout` seconds (in
    `tick`-second steps) waiting for playback to begin. If it never does
    (resolution failed past the point `play_direct()` still returned
    True, or Kodi itself couldn't play the url), there is nothing left
    to wait out: the user already saw `play_direct()`'s own failure
    notification, so this returns `(True, False)` (safe to reopen,
    nothing played so nothing to binge into) once the budget runs out.
    Once playback DOES begin, it polls again until `isPlaying()` goes
    back to False (stopped/finished).

    Returns a `(proceed, ended_naturally)` tuple rather than a bare
    bool, since `isPlaying()` alone cannot tell a natural end apart from
    the user pressing stop. `proceed` keeps the meaning described above
    (and below): False only on a monitor abort or an unexpected
    exception, True otherwise. `ended_naturally` is True only when the
    polled player actually reported Kodi's `onPlayBackEnded()` (played
    through to completion) rather than `onPlayBackStopped()`/
    `onPlayBackError()` (user stop / playback failure); callers use it
    to gate auto-play-next (binge-watching) - it is always False
    whenever `proceed` is False or playback never started.

    Every poll tick is a `monitor.waitForAbort(tick)` call, exactly like
    every other cancellable wait loop in `lib.ui.player` - Kodi shutting
    down mid-wait must be seen within one tick, at either stage, and
    returns `(False, False)` immediately (the caller must NOT reopen
    into a shutting-down Kodi). Any unexpected exception anywhere in
    here (a broken Player/Monitor) degrades to that same `(False,
    False)` - this helper must never raise into
    `StreamsWindow.onClick()`'s caller.

    `player`/`monitor` are injectable (unit tests pass tiny fakes);
    an injected `player` is asked for its own `ended_naturally`
    attribute (`getattr(player, 'ended_naturally', False)`) once it
    stops. Production callers omit `player` and get a
    `_PlaybackEndWatcher` - a tiny `xbmc.Player` subclass, defined here
    rather than at module scope since this module only ever imports
    `xbmc` lazily inside the functions that need it - whose
    `onPlayBackEnded()`/`onPlayBackStopped()`/`onPlayBackError()`
    overrides record which one Kodi actually called.
    """
    import xbmc

    from lib.ui.compat import log

    class _PlaybackEndWatcher(xbmc.Player):
        """Distinguishes a natural end from a user stop/failure, which
        `isPlaying()` alone cannot - it reports False as soon as
        playback stops for ANY reason."""

        def __init__(self):
            super().__init__()
            self.ended_naturally = False

        def onPlayBackEnded(self):
            self.ended_naturally = True

        def onPlayBackStopped(self):
            self.ended_naturally = False

        def onPlayBackError(self):
            self.ended_naturally = False

    try:
        if player is None:
            player = _PlaybackEndWatcher()
        if monitor is None:
            monitor = xbmc.Monitor()

        attempts = int(start_timeout / tick)
        for _attempt in range(attempts):
            if player.isPlaying():
                break
            if monitor.waitForAbort(tick):
                return False, False
        else:
            # Never started within the budget - play_direct()'s own
            # failure notification already told the user; just reopen.
            # Nothing played, so nothing to binge into.
            return True, False

        while player.isPlaying():
            if monitor.waitForAbort(tick):
                return False, False
        return True, getattr(player, 'ended_naturally', False)
    except Exception as exc:  # noqa: BLE001 - a wait hiccup must never crash onClick()
        log('streamswindow: wait-for-playback-end failed: %r (treating as stop)' % (exc,), xbmc.LOGWARNING)
        return False, False


def _query_addon_streams(client, transport_url, addon_name, stype, sid):
    """Fetch+parse one addon's own streams for (stype, sid) - the unit
    of work every fan-out below submits CONCURRENTLY (see
    `_MAX_STREAM_ADDON_WORKERS`) instead of the old serial `for` loop's
    one-at-a-time calls (see the module docstring for the measured
    5.0s/27.6s cost that caused). Returns a `(pairs, failed)` tuple;
    `failed` is True on failure, already logged here (an `AddonError` at
    DEBUG, anything unexpected at ERROR) with the addon's NAME plus only
    `safe_url_for_log()`'s safe scheme+host and `addon_error_detail()`'s
    type-plus-safe-category summary (e.g. "AddonError (HTTP 401)") -
    never the raw url or exception text, which may embed
    credentials/paths/queries - so a caller can aggregate a single
    WARNING across every addon's own outcome without re-deriving this
    per-addon detail itself. The name matters: the aggregate is only a
    count, so without it a user reporting "addon X's streams never
    appear" (issue #32) leaves no way to tell WHICH addon dropped out.

    Parsing runs inside the same guard as the fetch. A stream resource
    is arbitrary third-party JSON: a body that is a bare list rather
    than an object (`.get` -> AttributeError), or one item hostile
    enough to break `parse_stream`, must not escape to the caller. This
    function IS a worker-thread body (`_start_stream_fetch_workers`),
    and an exception escaping it kills that thread before it can queue
    any result at all - which does not just lose this addon, it leaves
    the consumer blocking forever on a result that will never arrive."""
    import xbmc

    from lib.stremio.addons import AddonError, addon_error_detail, safe_url_for_log
    from lib.ui.compat import log

    try:
        results = client.streams(transport_url, stype, sid)
        pairs = [(streaminfo.parse_stream(stream, addon_name=addon_name), stream) for stream in results or []]
    except AddonError as exc:
        log('streamswindow: %s (%s) failed: %s' % (
            addon_name, safe_url_for_log(transport_url), addon_error_detail(exc)), xbmc.LOGDEBUG)
        return [], True
    except Exception as exc:  # noqa: BLE001 - see docstring: a worker thread that dies here wedges its consumer
        log('streamswindow: %s (%s) raised %s' % (
            addon_name, safe_url_for_log(transport_url), type(exc).__name__), xbmc.LOGERROR)
        return [], True
    return pairs, False


def _supported_stream_addons(stype, sid):
    """Every installed addon whose manifest declares `stream` support
    for (stype, sid) - the exact filter the old serial loop applied
    before its own `for`, factored out so both `_fetch_stream_pairs()`
    and `open_streams()`'s own fan-out share one list."""
    from lib.stremio.addons import addon_supports

    store = get_store()
    addons = []
    for descriptor in store.get_enabled_addons():
        manifest = descriptor.get('manifest') or {}
        if addon_supports(manifest, 'stream', stype, sid):
            addons.append((descriptor, manifest))
    return addons


#: How long each fan-out consumer's `Queue.get()` blocks before giving up
#: and retrying - short enough that `dialog.iscanceled()` keeps getting
#: rechecked while addons are still mid-flight, long enough not to spin
#: the CPU polling an empty queue.
_STREAM_RESULT_POLL_SECONDS = 0.2


def _start_stream_fetch_workers(stype, sid, addons):
    """Fan `_query_addon_streams()` out across a small, genuinely BOUNDED
    pool of raw daemon threads fed by a `queue.Queue` work queue, and
    return the results `Queue` they feed into as `(addon_name, pairs,
    failed)` tuples. Exactly `min(len(addons), _MAX_STREAM_ADDON_WORKERS)`
    threads are started, each looping `work.get_nowait()` until the work
    queue is empty - bounding BOTH the thread count and the number of
    concurrent addon HTTP calls in flight, unlike spawning one thread per
    addon.

    Deliberately raw `threading.Thread(daemon=True)`, NOT
    `concurrent.futures.ThreadPoolExecutor`: `concurrent.futures.thread`
    registers an atexit hook that JOINS every worker at interpreter
    shutdown regardless of its daemon flag. Measured directly: a single
    addon still inside `AddonClient`'s own 15s timeout blocked process
    exit for a full 6.0s on BOTH Python 3.8 (daemon=True workers) and
    3.13 (daemon=False workers) - `pool.shutdown(wait=False)` does not
    avoid that join, it only stops the POOL itself from waiting, not the
    interpreter's own atexit hook. A raw daemon thread has no such hook:
    the interpreter abandons it outright at exit rather than joining it,
    which is the actual property a fan-out that can return before every
    addon has answered (`open_streams()`) needs."""
    client = get_client()
    work = queue.Queue()
    for descriptor, manifest in addons:
        work.put((descriptor.get('transportUrl'), manifest.get('name', '?')))
    results = queue.Queue()

    def _worker():
        while True:
            try:
                transport_url, addon_name = work.get_nowait()
            except queue.Empty:
                return
            addon_pairs, failed = _query_addon_streams(client, transport_url, addon_name, stype, sid)
            results.put((addon_name, addon_pairs, failed))

    for _ in range(min(len(addons), _MAX_STREAM_ADDON_WORKERS)):
        threading.Thread(target=_worker, daemon=True).start()
    return results


def _await_stream_result(results):
    """Block for the next worker's own `(addon_name, pairs, failed)`
    tuple - in short `_STREAM_RESULT_POLL_SECONDS` slices via a retried
    `Queue.get(timeout=...)` rather than one indefinite `get()`, so a
    caller looping on this (`_fetch_stream_pairs()`/`open_streams()`
    below) keeps re-checking its own `dialog.iscanceled()` between polls
    instead of only once a full addon answer lands."""
    while True:
        try:
            return results.get(timeout=_STREAM_RESULT_POLL_SECONDS)
        except queue.Empty:
            continue


def _fetch_stream_pairs(stype, sid):
    """Fetch+parse (not sort) every installed addon's streams for
    (stype, sid) - the exact aggregate pipeline `open_streams()` has
    always run for its own initial fetch, factored out so
    `_try_binge_watch()` can silently re-run it for a next episode's OWN
    id without duplicating the busy_dialog/per-addon-failure bookkeeping.
    Sorting and the "no results" notify()/return-False handling stay the
    CALLER's job - `open_streams()`'s own caller wants a user-visible
    notify(); the binge round trip wants a silent fall-back to reopening
    the picker instead (see `_try_binge_watch()`).

    Every addon is queried CONCURRENTLY (see `_MAX_STREAM_ADDON_WORKERS`)
    rather than one at a time - see the module docstring for why. This
    still BLOCKS until every addon has answered, exactly like the old
    serial loop did - `open_streams()`'s own streaming fan-out is what
    lets THAT caller react before every addon has answered; this
    function's callers (`_try_binge_watch()`, and `open_streams()`
    itself for as long as it used to) want the plain "wait for
    everything, then decide" contract."""
    import xbmc

    from lib.ui.compat import L, log

    addons = _supported_stream_addons(stype, sid)
    pairs = []
    failed_addons = 0
    if not addons:
        return pairs

    results = _start_stream_fetch_workers(stype, sid, addons)
    total = len(addons)
    completed = 0

    def consume_next():
        """Advance by one addon's own answer, folding its pairs/failure
        into the running totals above. Returns that addon's own `(name,
        pairs)`, or None once every addon has answered."""
        nonlocal failed_addons, completed
        if completed >= total:
            return None
        addon_name, addon_pairs, failed = _await_stream_result(results)
        completed += 1
        if failed:
            failed_addons += 1
        else:
            pairs.extend(addon_pairs)
        return addon_name, addon_pairs

    with busy_dialog(L(30033)) as dialog:
        while True:
            if dialog.iscanceled():
                break
            result = consume_next()
            if result is None:
                break
            addon_name, _addon_pairs = result
            dialog.update(int(completed * 100 / total), L(30187) % addon_name)

    if failed_addons:
        log('streamswindow: %d addon(s) failed' % failed_addons, xbmc.LOGWARNING)
    return pairs


def _binge_label(video):
    """'S02E01 \u00b7 Title' - the same shape
    `lib.ui.detailwindow._episode_label()` already renders for the
    episode list, reimplemented here (not imported) so this Kodi-facing
    module never reaches into another window module's private helpers -
    there is no shared, lower-layer module beneath either of them to
    hold one copy instead."""
    title = video.get('title') or video.get('name') or video.get('id') or ''
    return 'S%02dE%02d \u00b7 %s' % (video.get('season') or 0, video.get('episode') or 0, title)


def _binge_heading(show_name, video):
    """'<Show> \u2013 S02E01 \u00b7 Title' - the OSD label for an
    auto-played binge-watching episode, the same shape
    `lib.ui.detailwindow._episode_heading()` builds for a manually
    picked one."""
    code_and_title = _binge_label(video)
    return '%s \u2013 %s' % (show_name, code_and_title) if show_name else code_and_title


def _binge_item_meta(show_name, video, art, poster, meta):
    """`item_meta` for the next episode's auto-played `play_direct()`
    call - the exact same shape `StreamsWindow.onClick()` builds for a
    user-picked stream (see that method's own comment), just keyed off
    `video`'s own heading instead of the picker's static one, since
    every auto-played episode after the first needs its own OSD title."""
    item_meta = {}
    label = _binge_heading(show_name, video)
    if label:
        item_meta['label'] = label
    effective_art = art or ({'poster': poster} if poster else None)
    if effective_art:
        item_meta['art'] = effective_art
    if meta:
        item_meta['meta'] = meta
    return item_meta


def _binge_countdown(label, seconds):
    """Thin wrapper around `RivuletCountdown().run()` - the documented
    seam `_try_binge_watch()` calls and the tests pin, kept even though
    it is now a one-liner. Mirrors `_wait_for_playback_end()`'s own
    tri-state contract: True once the countdown runs out uninterrupted
    OR the user skips it via CountdownDialog.xml's OK/Select "play now"
    affordance - a new affordance the old dialog never had, folded into
    the same True the completion case returns since both mean
    "auto-play now" (caller treats them identically; call out the skip
    path in release notes) - None if the user cancelled via Back (fall
    back to reopening the picker - a deliberate "not now", not a Back
    action), or False on a `monitor.waitForAbort()` abort (Kodi
    shutting down - the caller must return False immediately, reopening
    nothing, same as every other abort check in this module)."""
    import xbmc

    from lib.ui.compat import L
    from lib.ui.dialogs import RivuletCountdown

    # The message embeds the live countdown ("Playing <title> in 8 s"),
    # so it is a per-tick formatter rather than a fixed string - which is
    # also what CountdownDialog.xml's own design copy shows.
    return RivuletCountdown().run(
        L(_BINGE_DIALOG_HEADING_STRING_ID),
        lambda remaining: L(_BINGE_DIALOG_MESSAGE_STRING_ID) % (label, remaining),
        seconds,
        monitor=xbmc.Monitor(),
    )


def _try_binge_watch(stype, meta, poster, art, video_id, played_info):
    """After an episode has played through to its NATURAL end (the caller
    only reaches here when `_wait_for_playback_end()` reported
    `ended_naturally`; a user stop never gets this far), try
    to keep going straight into the next one(s) - see the module
    docstring's binge-watching paragraph for the user-facing shape.

    Loops internally rather than telling its caller "keep going": each
    successfully auto-played episode immediately tries the ONE after it
    too, matching the "loop for the episode after that" requirement.
    Returns `None` the moment there is nothing left to binge into (not a
    series, no `video_id`/`meta`, the 'binge_enable' setting is off,
    `lib.ui.binge.next_video()` found no next episode, its streams could
    not be fetched, the countdown was cancelled, the auto-played episode
    was stopped rather than played through to its own natural end, or
    `play_direct()` itself failed) - the caller must fall back to its own
    normal "reopen the picker" step, exactly as if this function had
    never run. Returns
    `False` the moment a `monitor.waitForAbort()` fires anywhere in the
    chain (Kodi shutting down) - the caller must return False
    immediately instead, reopening nothing, same as every other abort
    check in this module."""
    if stype != 'series' or video_id is None or not meta:
        return None

    import xbmc

    from lib.ui.compat import ADDON, log, setting_bool, setting_int
    from lib.ui.player import play_direct

    if not setting_bool(_BINGE_ENABLE_SETTING, True):
        return None

    show_name = meta.get('name') or meta.get('id') or ''
    current_video_id = video_id
    binge_group = (played_info or {}).get('binge_group')
    countdown_seconds = setting_int(
        _BINGE_COUNTDOWN_SETTING, _BINGE_COUNTDOWN_DEFAULT_SECONDS, minimum=_BINGE_COUNTDOWN_MIN_SECONDS,
    )

    try:
        while True:
            candidate = next_video(meta, current_video_id)
            if candidate is None:
                return None

            next_pairs = _fetch_stream_pairs(stype, candidate.get('id'))
            if not next_pairs:
                return None
            sort_key = ADDON.getSetting('stream_sort') or 'quality'
            next_pairs = streaminfo.sort_streams(next_pairs, key=sort_key)
            picked_info, picked_stream = pick_binge_stream(next_pairs, binge_group)

            countdown = _binge_countdown(_binge_label(candidate), countdown_seconds)
            if countdown is None:
                return None
            if countdown is False:
                return False

            item_meta = _binge_item_meta(show_name, candidate, art, poster, meta)
            if not play_direct(
                picked_stream, stype, candidate.get('id'), item_meta=item_meta,
                on_ready=close_windows_for_playback,
                video_id=candidate.get('id'),
            ):
                return None
            log('streamswindow: binge-watching auto-played %r' % candidate.get('id'), xbmc.LOGINFO)

            proceed, ended_naturally = _wait_for_playback_end()
            if not proceed:
                return False
            if not ended_naturally:
                # The user stopped this auto-played episode instead of
                # letting it end - stop the chain here, exactly like any
                # other "nothing left to binge into" case above.
                return None
            if xbmc.Monitor().waitForAbort(_REOPEN_SETTLE_SECONDS):
                return False

            current_video_id = candidate.get('id')
            binge_group = picked_info.get('binge_group')
    except Exception as exc:  # noqa: BLE001 - a binge-chain hiccup must fall back to the picker, never crash open_streams()
        log('streamswindow: binge-watching failed: %r (falling back to the picker)' % (exc,), xbmc.LOGWARNING)
        return None


class _PickerFeed:
    """Bridges `open_streams()`'s background fan-out thread to whichever
    `StreamsWindow` it opens once the first addon answers - buffering
    batches under a lock until `attach()` hands them to a live window's
    `add_pairs()`/`set_loading()`, so nothing a worker delivers in the
    narrow window between "decided to open" and "window fully attached"
    is ever dropped. `stop()` makes every later `add()` a silent no-op,
    the same contract `StreamsWindow.add_pairs()` itself has once
    closed - see `open_streams()` for when each is called."""

    def __init__(self):
        self._lock = threading.Lock()
        self._buffered = []
        self._window = None
        self._done = False
        self._stopped = False

    def add(self, pairs):
        with self._lock:
            if self._stopped:
                return
            if self._window is None:
                self._buffered.append(list(pairs))
                return
            window = self._window
        window.add_pairs(pairs)

    def mark_done(self):
        """Every addon has now answered - flip the live window's status
        line off, or remember to for whichever window `attach()` next."""
        with self._lock:
            self._done = True
            window = None if self._stopped else self._window
        if window is not None:
            window.set_loading(False)

    def attach(self, window):
        """Flush anything buffered onto `window`, then route every
        subsequent `add()` straight there. Returns True if the fan-out
        was still outstanding at attach time - the caller's initial
        `set_loading()` state."""
        with self._lock:
            if self._stopped:
                return False
            buffered, self._buffered = self._buffered, []
            self._window = window
            still_loading = not self._done
        for batch in buffered:
            window.add_pairs(batch)
        return still_loading

    def stop(self):
        """Nothing may deliver to (or even buffer for) a window again -
        called once the first picker this feeds has closed for good
        (backed out, or handed off to playback); see `open_streams()`'s
        "MUST continue with the accumulated pairs" contract."""
        with self._lock:
            self._stopped = True
            self._window = None
            self._buffered = []


def open_streams(stype, sid, poster=None, heading='', art=None, meta=None, video_id=None):
    """Fetch+sort every installed addon's streams for (stype, sid) and
    show them; a pick resolves+plays directly. `heading`/`art`/`meta` are
    forwarded to `StreamsWindow.start()` unchanged, and `video_id` (the
    id of the episode `pairs` is for, `None` for a movie or a
    context-free call) drives the binge-watching round trip below - see
    the module docstring for both.

    Every installed addon is queried CONCURRENTLY, and the picker opens
    as soon as the FIRST one answers with anything - the rest stream in
    live onto that SAME window (see the module docstring and
    `StreamsWindow.add_pairs()`/`set_loading()`) rather than blocking
    the whole picker open on whichever addon is slowest. Only that
    FIRST window gets live updates; once it closes (a pick, or the user
    backing out), a `threading.Event` tells the fan-out thread to stop
    delivering, and every later reopen below works off the accumulated
    `StreamsWindow.pairs` snapshot instead.

    Once a pick plays, this reopens a fresh `StreamsWindow` over that
    SAME snapshot once playback ends (see `_wait_for_playback_end()`)
    rather than returning - see the module docstring for why this means
    the function now only ever returns False."""
    import xbmc

    from lib.ui.compat import ADDON, L, log, notify

    addons = _supported_stream_addons(stype, sid)
    if not addons:
        notify(L(30030))
        return False

    results = _start_stream_fetch_workers(stype, sid, addons)
    total = len(addons)
    pairs = []
    failed_addons = 0
    consumed = 0

    def consume_next():
        """Advance by one addon's own answer, folding its pairs/failure
        into the running totals above. Returns that addon's own `(name,
        pairs)`, or None once every addon has answered."""
        nonlocal failed_addons, consumed
        if consumed >= total:
            return None
        addon_name, addon_pairs, failed = _await_stream_result(results)
        consumed += 1
        if failed:
            failed_addons += 1
        else:
            pairs.extend(addon_pairs)
        return addon_name, addon_pairs

    with busy_dialog(L(30033)) as dialog:
        while not pairs:
            if dialog.iscanceled():
                break
            result = consume_next()
            if result is None:
                break
            addon_name, _addon_pairs = result
            dialog.update(int(consumed * 100 / total), L(30187) % addon_name)

    if not pairs:
        if failed_addons:
            log('streamswindow: %d addon(s) failed' % failed_addons, xbmc.LOGWARNING)
        notify(L(30030))
        return False

    sort_key = ADDON.getSetting('stream_sort') or 'quality'
    pairs = streaminfo.sort_streams(pairs, key=sort_key)

    feed = _PickerFeed()
    stop_event = threading.Event()

    def drain_remaining():
        try:
            while True:
                result = consume_next()
                if result is None:
                    break
                _addon_name, addon_pairs = result
                if addon_pairs and not stop_event.is_set():
                    feed.add(addon_pairs)
        except Exception as exc:  # noqa: BLE001 - nothing else can catch a failure on this background thread; log it as loudly as open_streams()'s own window-construction failures below, rather than let it vanish silently.
            log('streamswindow: fan-out failed: %r' % (exc,), xbmc.LOGERROR)
        finally:
            if not stop_event.is_set():
                feed.mark_done()
            if failed_addons:
                log('streamswindow: %d addon(s) failed' % failed_addons, xbmc.LOGWARNING)

    still_loading = consumed < total
    if still_loading:
        threading.Thread(target=drain_remaining, daemon=True).start()
    else:
        feed.mark_done()
        if failed_addons:
            log('streamswindow: %d addon(s) failed' % failed_addons, xbmc.LOGWARNING)

    log('streamswindow: opening StreamsWindow (%d streams so far)' % len(pairs), xbmc.LOGINFO)
    live = True
    while True:
        win = None
        try:
            win = open_window(StreamsWindow, 'StreamsWindow.xml')
            if live:
                win.set_loading(feed.attach(win))
            played = win.start(
                pairs, stype, sid, poster=poster, heading=heading, art=art, meta=meta, video_id=video_id,
            )
        except Exception as exc:  # a skin/UI failure must surface, not vanish
            log('streamswindow: window failed to open: %r' % (exc,), xbmc.LOGERROR)
            notify(L(30032))
            if live:
                stop_event.set()
                feed.stop()
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

        if live:
            # The live streaming phase only ever feeds the FIRST window
            # this picker opens (see the module docstring): once it has
            # closed - a pick, or the user backing out - capture
            # whatever else streamed in before that close as the new
            # `pairs` snapshot, and stop listening for more addons. Every
            # later reopen below reuses THIS SAME list unchanged - there
            # is nothing left updating it once live is False.
            stop_event.set()
            feed.stop()
            live = False
            pairs = win.pairs

        if not played:
            return False

        # Playback started: wait it out, then reopen the SAME picker
        # underneath the player that just closed instead of unwinding the
        # whole custom-window stack (see the module docstring). A monitor
        # abort (Kodi shutting down) at any point below returns False
        # immediately, reopening nothing.
        proceed, ended_naturally = _wait_for_playback_end()
        if not proceed:
            return False
        if xbmc.Monitor().waitForAbort(_REOPEN_SETTLE_SECONDS):  # brief settle pause before reopening
            return False

        # Binge-watching: try to auto-play straight through as many
        # consecutive "next episodes" as apply (see _try_binge_watch()'s
        # own docstring) before falling back to reopening THIS picker -
        # but only when the just-played episode ran through to its own
        # natural end; the user pressing stop must fall straight through
        # to reopening the picker, never auto-play the next episode.
        # None means the normal fall-back; False means a monitor abort
        # fired mid-chain, which must return False immediately instead,
        # reopening nothing, same as every other abort check above.
        if ended_naturally:
            played_info = win.played_pair[0] if win.played_pair else None
            if _try_binge_watch(stype, meta, poster, art, video_id, played_info) is False:
                return False

        log('streamswindow: reopening StreamsWindow after playback (%d streams)' % len(pairs), xbmc.LOGINFO)
