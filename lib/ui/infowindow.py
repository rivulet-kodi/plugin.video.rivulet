"""ShowcaseWindow: a fullscreen coverflow overlay for one catalog page.

Ports the reference addon's `platformcode/xbmc_info_window.py::InfoWindow`
(`resources/skins/Default/1080i/InfoWindow.xml`) to Rivulet/Stremio metas:
a caller opens it via `open_showcase()` (below) with one already-fetched
catalog page - `catalogpicker.CatalogPickerWindow._open_catalog()`,
`searchwindow.SearchWindow._run_search()`,
and this module's own
`open_credits_picker()` - the user scrolls a horizontal poster
coverflow (the fanart background updates to match the focused item).
Picking a movie poster jumps straight to
`lib.ui.streamswindow.open_streams()` (a movie has nothing
else to pick, same shortcut `lib.ui.detailwindow.open_detail()` takes -
see that module's docstring) using this poster's own title/art, no
extra meta fetch; picking anything else (a series) returns that meta to
the caller so it can navigate there.

Control ids mirror the reference addon's InfoWindow 1:1 (see
`ShowcaseWindow.xml`):
    BACKGROUND = 30000  fullscreen fanart image, changes as you scroll
    LOADING    = 30001  busy indicator, hidden once items are loaded
    SELECT     = 30002  horizontal fixedlist - the coverflow itself
    CLOSE      = 30003  close button
HEADER = 30100 is a Rivulet-only addition (the reference addon has no
breadcrumb) for the header wordmark label, so `onInit()` can set its
`RIVULET [/ CATALOG]` text - see `_header_label()`.

The coverflow's visual rendering (ShowcaseWindow.xml's fixedlist/
focusedlayout, the background crossfade) is Kodi-skin-engine-only and
cannot be exercised by this test suite - see tests/test_infowindow.py's
module docstring for what a real device must confirm.
"""
import threading

import xbmcgui

from lib.ui.uicommon import ModalStackWindow

BACKGROUND = 30000
LOADING = 30001
SELECT = 30002
CLOSE = 30003
HEADER = 30100

#: Settle time before a focus change fires its meta fetch. Scrolling through
#: a sparse catalog would otherwise spawn one fetch per item passed over, and
#: `views._fetch_meta` fans out to a pool of its own per call - so the cost of
#: a fast scroll is multiplied, and its stragglers are abandoned rather than
#: cancelled. Only the item focus actually settles on is worth fetching.
_ENRICH_SETTLE_SECS = 0.2

#: Ceiling on concurrent enrich fetches. Each one can hold up to
#: `views._MAX_ADDON_WORKERS` request threads, so this bounds the tail left
#: behind by a scroll that outruns the settle window above.
_ENRICH_MAX_INFLIGHT = 2

# Back/Nav-Back, PreviousMenu/Esc, Backspace - any of these closes the
# overlay without a selection, same as the reference InfoWindow.
_BACK_ACTIONS = frozenset({9, 10, 92})

# ACTION_SHOW_INFO ("info" button) has nothing to show beyond what the
# focused poster's own focusedlayout already renders (title/genre/plot)
# - swallow it rather than let it fall through to back-action handling.
_INFO_ACTION = 11

# ACTION_CONTEXT_MENU ("C" key, a remote's menu button, long-press on
# Android TV) opens the cast & crew picker for whichever poster is
# focused - the coverflow is the only place a movie's cast/crew can be
# reached in the custom-window path (a series gets the same affordance
# on its own onAction - see detailwindow.py's identical constant).
_CONTEXT_MENU_ACTION = 117


#: Separator between the hero's metadata segments, matching the
#: ' · '-joined lines `detailwindow._metadata_line()` and
#: `streamswindow._rebuild_list()` already build. U+00B7 is in
#: `_SAFE_ANYWHERE` (both Estuary faces have it), so the joined line
#: renders whatever font the label ends up using.
_META_SEPARATOR = ' · '

#: strings.po id for the range terminator a still-running series gets
#: in place of a bare dangling dash ("2022–now"). Translated in all 14
#: locales. DetailWindow and StreamsWindow print the same range from
#: the same id, so the three screens agree.
_NOW_STRING_ID = 30234

#: How the hero labels its IMDb rating. The bare number rendered as
#: "9.0" in an accent-coloured box, which reads as a percentage, a
#: season count or a price as easily as a rating - the scale it is on
#: was carried entirely by the colour. Spelled out rather than drawn as
#: a `★`: the star is in `_SANS_ONLY` (NotoMono has no glyph and
#: renders tofu - the exact bug tests/test_glyph_coverage.py exists to
#: catch), and "IMDb" additionally names *which* 10-point scale this is,
#: which the star alone never did. Left untranslated, like the RIVULET
#: wordmark, because it is a brand name - tests/test_skin_xml.py's
#: `_ALLOWED_LITERAL_WORDS` carries the matching entry for the XML side.
_RATING_LABEL = 'IMDb'


def _rating_segment(rating):
    """`'IMDb 9.0'` for a hero metadata line, or `''` when the meta
    carries no rating - never a dangling label with nothing after it.

    Pure helper - no xbmc - so it is trivially unit-testable on its own.
    """
    rating = str(rating or '').strip()
    return '%s %s' % (_RATING_LABEL, rating) if rating else ''


def _year_range(meta, now_word):
    """The hero's release-year text: Stremio's `releaseInfo`, falling
    back to the date part of `released`, rendered by
    `playbackmeta.year_range()` (which is where the open-ended-range
    handling and its reasoning live).

    Pure helper - no xbmc - so it is trivially unit-testable on its own.
    """
    from lib.ui.playbackmeta import year_range

    meta = meta or {}
    released = meta.get('released')
    date_only = released.split('T', 1)[0] if released else ''
    return year_range(meta.get('releaseInfo') or date_only or '', now_word)


def _year_text(meta):
    """`_year_range()` with the localized "now" resolved - see there."""
    from lib.ui.compat import L

    return _year_range(meta, L(_NOW_STRING_ID))


def _meta_line(props):
    """Join the hero's year/rating/runtime/genre into the single line
    ShowcaseWindow.xml renders, skipping whatever the meta does not
    carry so a sparse item never shows a dangling separator.

    Built here rather than laid out in the skin because Kodi sizes a
    control from the XML at load time and cannot fit a box to its text:
    the hero used three fixed 110px boxes, which is wide enough for a
    bare `2019` but truncates a real series range (`2010-2017` needs
    ~140px at Mono26 and rendered as `2010...`) and leaves under a pixel
    either side of a `120 min` runtime. One flowing label has no width
    to overflow, and is the same shape the other two screens that print
    this metadata already use.

    Genre rides on the end of this same line, where the chip layout
    always put it. It is the one segment that used to need its own
    control, because it had to be placed in whichever slot the chips
    ahead of it left open; joined into the string there are no slots to
    place it in, so the reflow matrix that placement needed is gone
    with it.

    Pure helper - no xbmc - so it is trivially unit-testable on its own.
    """
    props = props or {}
    segments = (
        props.get('year') or '',
        _rating_segment(props.get('rating')),
        props.get('runtime') or '',
        props.get('genre') or '',
    )
    return _META_SEPARATOR.join(segment for segment in segments if segment)


def _item_properties(meta):
    """Map one Stremio catalog meta to the string Properties
    ShowcaseWindow.xml's coverflow reads via `$INFO[ListItem.Property(...)]`.

    Pure helper - no xbmc - so it is trivially unit-testable on its own.
    """
    meta = meta or {}
    poster = meta.get('poster')
    logo = meta.get('logo')
    background = meta.get('background')
    props = {
        'thumbnail': poster or logo or '',
        'fanart': background or logo or poster or '',
        'genre': ', '.join(meta.get('genres') or []),
        'rating': meta.get('imdbRating') or '',
        'plot': meta.get('description') or '',
        'year': _year_text(meta),
        'runtime': meta.get('runtime') or '',
    }
    # Precomposed for the skin: `meta_line` is what the hero actually
    # renders, but `year`/`rating`/`runtime`/`genre` stay individually
    # exposed - the enrich merge in `_enrich_fetch()` reads them back
    # per field. Built last, so it sees the fields above it.
    props['meta_line'] = _meta_line(props)
    return props


#: The header wordmark's separator, at the design's rgba(238,243,246,.18) -
#: not one of the addon's named text tints (those stop at .2/33), computed
#: the same way here.
_HEADER_SEPARATOR_COLOR = '2EEEF3F6'
_HEADER_TITLE_COLOR = '9EEEF3F6'


def _header_label(catalog_title=None):
    """Build the inline-markup ShowcaseWindow.xml's header wordmark label
    renders: bare `RIVULET` when `catalog_title` is falsy (every caller
    that predates `open_showcase(metas, catalog_title=...)`), or
    `RIVULET / <TITLE>` once a caller hands over its own catalog/section
    title - see `open_showcase()`.

    Pure helper - no xbmc - so it is trivially unit-testable on its own.
    """
    if not catalog_title:
        return '[COLOR 57EEF3F6]RIVULET[/COLOR]'
    return (
        '[COLOR 57EEF3F6]RIVULET[/COLOR] [COLOR %s]/[/COLOR] [COLOR %s]%s[/COLOR]'
        % (_HEADER_SEPARATOR_COLOR, _HEADER_TITLE_COLOR, catalog_title.upper())
    )


class ShowcaseWindow(ModalStackWindow, xbmcgui.WindowXMLDialog):
    """Fullscreen coverflow modal (`ShowcaseWindow.xml`). Build/run it via
    `open_showcase()` below rather than constructing it directly."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.metas = []
        self.selected = None
        #: catalog/section title for the header breadcrumb - None keeps
        #: the header a bare "RIVULET", see `_header_label()`/`start()`.
        self.catalog_title = None
        #: fanart last pushed to BACKGROUND - lets onAction() skip
        #: setImage() when a keypress doesn't actually change focus (e.g.
        #: the enrich worker's Action(noop) wake-up), the per-keypress
        #: cost the coverflow's scroll feel is most sensitive to.
        self._last_background = None
        self._reset_enrich_state()
        self._reset_paging_state()

    def _reset_paging_state(self):
        #: pages a `more_pages` walker has produced but the UI thread has
        #: not appended yet. Written under `_paging_lock`, drained by
        #: `_apply_pending_pages()` on the UI thread - the same queue +
        #: Action(noop) handoff the enrich workers use, for the same
        #: reason (a ListItem must not be touched off the GUI thread).
        self._pending_pages = []
        self._paging_lock = threading.Lock()
        #: set by `close()` to tell a running walker to stop between
        #: pages. Deliberately NOT a "walker finished" flag: the worker
        #: must be able to tell "nobody cancelled me" from "the window
        #: went away", and only the latter should end the walk.
        self._paging_stopped = threading.Event()

    def _reset_enrich_state(self):
        #: indices whose full meta has been fetched (or found already complete)
        self._enriched = set()
        #: (index -> props) merged by a worker, waiting for the UI thread to
        #: apply them. Written under `_enrich_lock`, drained in `_apply_enriched`.
        self._enrich_pending = {}
        self._enrich_lock = threading.Lock()
        #: bounds concurrent fetches (see `_ENRICH_MAX_INFLIGHT`)
        self._enrich_slots = threading.BoundedSemaphore(_ENRICH_MAX_INFLIGHT)
        #: the timer armed by the most recent focus change, if it has not
        #: fired yet - superseded (and cancelled) by the next focus change.
        self._enrich_timer = None

    def start(self, metas, catalog_title=None, more_pages=None):
        """doModal() with `metas` (a list of Stremio meta dicts) loaded as
        the coverflow's items; returns the selected meta, or None if the
        window closed without a selection. An empty `metas` never opens
        the modal at all and returns None immediately.

        `catalog_title`, if given, renders as a "RIVULET / <TITLE>"
        breadcrumb in the header - see `_header_label()`. Left as None,
        the header stays the bare "RIVULET" it always was.

        `more_pages`, if given, is an iterable of further meta pages
        (`lib.ui.views.iter_catalog_pages` past its first page). It is
        walked on a daemon thread and each page appended to the live
        coverflow as it arrives, so a catalog the addon serves 20 at a
        time opens immediately on those 20 instead of behind a spinner
        counting to 400. Exhausting it is best-effort: a page that never
        arrives, or an addon that fails halfway, just leaves the strip at
        whatever did land."""
        self.metas = list(metas or [])
        self.selected = None
        self.catalog_title = catalog_title
        self._reset_enrich_state()
        self._reset_paging_state()
        if not self.metas:
            return None
        if more_pages is not None:
            self._spawn_paging(more_pages)
        self.doModal()
        return self.selected

    def _spawn_paging(self, more_pages):
        """Walk `more_pages` on a daemon thread, queueing each page for
        the UI thread (see `_reset_paging_state`)."""
        thread = threading.Thread(target=self._paging_worker, args=(more_pages,), daemon=True)
        thread.start()

    def _paging_worker(self, more_pages):
        import xbmc

        try:
            for page in more_pages:
                if self._paging_stopped.is_set():
                    # The window closed while this page was in flight -
                    # stop walking rather than fetch for a dead strip.
                    return
                if not page:
                    continue
                with self._paging_lock:
                    self._pending_pages.append(list(page))
                # Wake the UI thread to drain the queue, exactly as the
                # enrich workers do - see `_enrich_fetch`'s tail.
                xbmc.executebuiltin('Action(noop)')
        except Exception:
            # Daemon thread with nobody to catch for it; a failed walk
            # must never take the window down. Same nested guard as
            # `_enrich_worker`: reporting can itself fail during teardown.
            try:
                from lib.ui.compat import log

                log('infowindow: catalog paging worker failed', xbmc.LOGDEBUG)
            except Exception:
                pass

    def _apply_pending_pages(self):
        """Append whatever the paging worker has queued onto the live
        coverflow. Always call this from the UI thread.

        Appending (rather than rebuilding) keeps every already-enriched
        item untouched: indices only ever grow, so a page landing
        mid-scroll cannot renumber the item under focus.

        The cursor, however, is NOT preserved for free: `addItems()` on a
        ControlList resets the selected position to 0, so a page landing
        while the user scrolls would otherwise yank them back to the
        first poster - the further they had scrolled, the more jarring.
        Save the position and restore it after the append, the same
        guard `streamswindow._merge_and_refresh()` applies for the same
        reason. Restoring by INDEX is sound here where that one needs
        object identity: this only ever appends, so every existing item
        keeps the index it already had.
        """
        with self._paging_lock:
            if not self._pending_pages:
                return
            pages, self._pending_pages = self._pending_pages, []
        items = []
        for page in pages:
            for meta in page:
                items.append(self._make_item(len(self.metas), meta))
                self.metas.append(meta)
        if not items:
            return
        control = self.getControl(SELECT)
        focus_index = control.getSelectedPosition()
        control.addItems(items)
        # A negative position means nothing was focused (an empty list
        # cannot happen here - the window never opens without a first
        # page) - leave Kodi's own default alone in that case.
        if focus_index > 0:
            control.selectItem(focus_index)

    def _make_item(self, index, meta, props=None):
        item = xbmcgui.ListItem(meta.get('name') or meta.get('id') or '?')
        properties = dict(props) if props is not None else _item_properties(meta)
        properties['position'] = str(index)
        # One setProperties() call instead of one setProperty() per key -
        # each crosses the Python->C++ boundary, so this collapses what
        # was 7 crossings per item into 1.
        item.setProperties(properties)
        return item

    def onInit(self):
        if not self.metas:
            return
        self.getControl(HEADER).setLabel(_header_label(self.catalog_title))
        # Computed once per meta and reused for the background fallback
        # below - _make_item() used to call _item_properties() again just
        # for metas[0], recomputing what this loop already has in hand.
        props_by_index = [_item_properties(meta) for meta in self.metas]
        items = [
            self._make_item(index, meta, props_by_index[index])
            for index, meta in enumerate(self.metas)
        ]
        control = self.getControl(SELECT)
        # reset() before addItems(): onInit() runs again when
        # uicommon.ModalStackWindow reopens a screen force-closed for
        # playback, and re-adding onto a retained list would double every
        # item.
        control.reset()
        control.addItems(items)
        background = props_by_index[0].get('fanart', '')
        self.getControl(BACKGROUND).setImage(background)
        self._last_background = background
        self.getControl(LOADING).setVisible(False)
        self.setFocusId(SELECT)
        # A rebuild discards the ListItems a pending merge was queued
        # against, so apply whatever landed while we were away onto the
        # fresh ones before enriching further.
        self._apply_enriched()
        # onAction() enriches on every focus change, but never fires for the
        # item that opens focused - enrich item 0 here so the window doesn't
        # open on a blank plot until the user scrolls off it and back.
        if items:
            self._enrich_focused(items[0], settle=False)

    def onAction(self, action):
        # Before reading focus: a queued page appends items, and doing it
        # first means the strip is whole by the time anything below asks
        # what is focused.
        self._apply_pending_pages()
        if self.getFocusId() == SELECT:
            focused = self.getControl(SELECT).getSelectedItem()
            if focused is not None:
                fanart = focused.getProperty('fanart')
                # Skip the setImage() boundary crossing when focus fired
                # onAction() but the fanart didn't actually change - e.g.
                # the enrich worker's Action(noop) wake-up below, or any
                # other non-navigation key while the same item stays
                # focused. This is the call scrolling feel is most
                # sensitive to: it used to run unconditionally on every
                # keypress.
                if fanart != self._last_background:
                    self.getControl(BACKGROUND).setImage(fanart)
                    self._last_background = fanart
                # Both on the UI thread: drain anything a worker finished
                # since the last action, then arm a fetch for this item.
                self._apply_enriched()
                self._enrich_focused(focused)
        action_id = action.getId()
        if action_id == _INFO_ACTION:
            return
        if action_id == _CONTEXT_MENU_ACTION:
            self._open_credits()
            return
        if action_id in _BACK_ACTIONS:
            self.close()

    def _open_credits(self):
        """ACTION_CONTEXT_MENU on the coverflow: resolve the focused
        poster the exact same way onAction()'s own background swap does
        above (getFocusId()/getSelectedItem()/position -> self.metas) -
        no second "what's focused" mechanism - then fetch its full meta
        (a catalog preview has no `links`, see `_item_properties()`) and
        hand it to `open_credits_picker()`."""
        if self.getFocusId() != SELECT:
            return
        focused = self.getControl(SELECT).getSelectedItem()
        if focused is None:
            return
        meta = self.metas[int(focused.getProperty('position'))]
        stype = meta.get('type') or 'movie'
        sid = meta.get('id')
        if not sid:
            return

        from lib.ui.compat import L
        from lib.ui.dependencies import get_client, get_store
        from lib.ui.uicommon import busy_dialog
        from lib.ui.views import _fetch_meta

        with busy_dialog(L(30033)):
            full_meta = _fetch_meta(stype, sid)
        open_credits_picker(get_store(), get_client(), full_meta)

    def close(self):
        # Any exit path - a back action, a selection, or a force-close for
        # playback - must not leave a settle timer armed to fetch for a
        # window that is no longer on screen.
        self._cancel_enrich_timer()
        # Same for the paging walk: setting this is what the worker polls
        # between pages to stop fetching for a strip nobody is looking at.
        self._paging_stopped.set()
        super().close()

    def _cancel_enrich_timer(self):
        timer, self._enrich_timer = self._enrich_timer, None
        if timer is not None:
            timer.cancel()

    def _apply_enriched(self):
        """Apply merged properties queued by workers onto the live ListItems.

        Always call this from the UI thread. Workers only ever touch
        `self.metas[index]` and this queue - never a ListItem - so that
        every mutation of the rendered list happens in one place, in focus
        order. Resolving the item by index here (rather than closing over
        the handle the worker was spawned with) is what keeps a fetch that
        lands across a reopen-for-playback rebuild from writing to a
        detached item that is no longer on screen.
        """
        with self._enrich_lock:
            if not self._enrich_pending:
                return
            pending, self._enrich_pending = self._enrich_pending, {}
        control = self.getControl(SELECT)
        size = control.size()
        for index, props in pending.items():
            if not 0 <= index < size:
                continue
            item = control.getListItem(index)
            # One setProperties() call instead of one setProperty() per
            # key (genre/rating/plot/year).
            item.setProperties(props)

    def _enrich_focused(self, item, settle=True):
        """Fill in the description/genres a catalog meta preview lacks.

        Stremio's `catalog` resource returns meta *previews*, which carry
        name/poster/year but commonly omit `description` and `genres` -
        those only exist on the full `meta` resource. Discover's catalogs
        happen to include them often enough that the coverflow looked
        complete; search results routinely do not, leaving the window's
        plot/genre labels blank.

        Fetch the full meta lazily for whichever poster is focused, on a
        daemon thread (`_fetch_meta` is a blocking HTTP call - doing it
        inline would stall the scroll), and cache per index so scrolling
        back and forth re-fetches nothing. The result is queued for the UI
        thread to apply, never written to a ListItem from the worker; see
        `_apply_enriched`. Every failure is non-fatal: the labels simply
        stay as they were.

        `settle` delays the fetch by `_ENRICH_SETTLE_SECS` so scrolling
        past an item does not fetch it - only the one focus stops on. It is
        off for the item the window opens on, which is already settled.
        """
        try:
            index = int(item.getProperty('position'))
        except (TypeError, ValueError):
            return
        if index in self._enriched:
            return
        meta = self.metas[index] if 0 <= index < len(self.metas) else None
        if not meta or not meta.get('id'):
            return
        # Already complete (Discover's catalogs usually are) - nothing to do.
        if meta.get('description'):
            with self._enrich_lock:
                self._enriched.add(index)
            return
        # Note the index is *not* marked here: the worker marks it once it
        # actually has a slot to fetch with, so an item scrolled past
        # before its timer fires stays eligible for a later focus.
        self._cancel_enrich_timer()
        if not settle:
            self._spawn_enrich(index, meta)
            return
        timer = threading.Timer(_ENRICH_SETTLE_SECS, self._spawn_enrich, args=(index, meta))
        timer.daemon = True
        self._enrich_timer = timer
        timer.start()

    def _spawn_enrich(self, index, meta):
        threading.Thread(target=self._enrich_worker, args=(index, meta), daemon=True).start()

    def _enrich_worker(self, index, meta):
        # Bound concurrent fetches: each _fetch_meta fans out to a pool of
        # its own, and abandons (rather than cancels) whatever is still in
        # flight when it returns. Without a slot the index stays unmarked,
        # so a later focus can retry it.
        if not self._enrich_slots.acquire(blocking=False):
            return
        with self._enrich_lock:
            self._enriched.add(index)
        try:
            self._enrich_fetch(index, meta)
        except Exception:
            # Last-resort guard: this runs on a daemon thread nobody joins,
            # so an escaping exception has no caller to surface it. Even
            # reporting it can fail - a thread still running while the
            # interpreter (or, under test, the injected xbmc stubs) is torn
            # down raises ModuleNotFoundError on `import xbmc` - hence the
            # nested guard rather than no diagnostic at all.
            try:
                import xbmc

                from lib.ui.compat import log

                log('infowindow: meta enrich worker failed', xbmc.LOGDEBUG)
            except Exception:
                pass
        finally:
            self._enrich_slots.release()

    def _enrich_fetch(self, index, meta):
        import xbmc

        from lib.ui.compat import log

        try:
            from lib.ui.views import _fetch_meta

            full = _fetch_meta(meta.get('type') or 'movie', meta.get('id'))
        except Exception as exc:  # never let a lookup failure break the UI
            log('infowindow: meta enrich failed for %s: %r' % (meta.get('id'), exc), xbmc.LOGDEBUG)
            return
        if not full:
            return
        # Merge rather than replace: the preview's own poster/background are
        # what the strip is already showing, and re-setting them would make
        # the focused art flicker as the fetch lands.
        #
        # `full` is unvalidated third-party addon JSON, so check the shape
        # each field is consumed as. _item_properties() joins `genres`, and
        # a str is iterable - an addon sending "Drama" instead of ["Drama"]
        # would otherwise render as "D, r, a, m, a"; a list of anything but
        # strings is dropped to a blank label on purpose, for the same
        # reason. `imdbRating`, `releaseInfo` and `released` are routinely
        # numeric in the wild (streamswindow.py str()s the one,
        # playbackmeta.py float()s the other), so those are coerced rather
        # than rejected. `released` is merged as well as `releaseInfo`
        # because _item_properties() derives `year` from either.
        for key in ('description', 'genres', 'imdbRating', 'releaseInfo', 'released'):
            value = full.get(key)
            if not value or meta.get(key):
                continue
            if key == 'genres':
                if isinstance(value, list):
                    meta[key] = [g for g in value if isinstance(g, str)]
            elif key == 'description':
                if isinstance(value, str):
                    meta[key] = value
            elif isinstance(value, (str, int, float)):
                meta[key] = str(value)
        props = _item_properties(meta)
        # Hand off to the UI thread rather than touching the ListItem here:
        # it is inside the ControlList the skin renders every frame.
        with self._enrich_lock:
            # `meta_line` travels with them: it is what the hero renders,
            # and a fetch that lands a rating or a release range would
            # otherwise merge the field but leave the composed line as
            # the sparse one built before the fetch.
            self._enrich_pending[index] = {
                key: props.get(key, '')
                for key in ('genre', 'rating', 'plot', 'year', 'meta_line')
            }
        # ...and wake it, or nothing would apply the queue until the user's
        # next keypress - by which time focus has usually left the item
        # that was fetched. executebuiltin() posts to Kodi's application
        # messenger, which runs Action(noop) on the GUI thread and delivers
        # it to this dialog's onAction(): the drain, and the repaint, then
        # happen there. ACTION_NOOP itself does nothing else, and is not a
        # back action.
        xbmc.executebuiltin('Action(noop)')

    def onClick(self, control_id):
        if control_id == SELECT:
            focused = self.getControl(SELECT).getSelectedItem()
            if focused is None:
                return
            meta = self.metas[int(focused.getProperty('position'))]
            if meta.get('type') == 'movie' and meta.get('id'):
                self._play_movie(meta)
                self.close()
                return
            self.selected = meta
            self.close()
        elif control_id == CLOSE:
            self.close()

    def _play_movie(self, meta):
        """A movie has nothing left to pick beyond what this poster
        already shows - jump straight to StreamsWindow with its own
        title/art (no extra meta fetch, unlike the DetailWindow path a
        series still needs - see `lib.ui.detailwindow.open_detail`).
        This fully handles the click itself, so `self.selected` stays
        None: every caller's own `if selected: ...` branch is a no-op,
        same as the user closing the overlay without picking anything.

        `onClick()` unconditionally closes `self` right after this
        returns - `open_streams()` only ever returns False (see its own
        module docstring), so that close() is the ONE real close this
        window gets from the user's perspective even though
        `close_windows_for_playback()` may also force-close it mid-call
        while it sits underneath the StreamsWindow round trip: that
        force-close cannot take effect until `open_streams()` (called
        below, still on this stack frame) returns, at which point
        `onClick()`'s own `self.close()` immediately follows - so
        `ModalStackWindow.doModal()` never gets a chance to reopen a
        window that was about to close anyway."""
        from lib.ui.streamswindow import open_streams

        poster = meta.get('poster')
        fanart = meta.get('background') or meta.get('logo') or poster
        open_streams(
            meta.get('type'), meta.get('id'),
            poster=poster,
            heading=meta.get('name') or meta.get('id') or '',
            art={'poster': poster, 'fanart': fanart}, meta=meta,
        )


def open_showcase(metas, catalog_title=None, more_pages=None):
    """Build and run a ShowcaseWindow over `metas`; returns the selected
    meta dict, or None if the user closed the overlay without picking one
    (or `metas` was empty). Every caller already wraps this call in its own
    try/except (catalogpicker._open_catalog, searchwindow._run_search,
    ) and logs+notifies on failure, so an
    exception from .start() keeps propagating unchanged here - this
    only guarantees
    the window is closed first (it may not have had a chance to self-close,
    e.g. if onInit() or a mid-modal callback raised).

    `catalog_title`, if given, becomes the header's "RIVULET / <TITLE>"
    breadcrumb - see `ShowcaseWindow.start()`. `more_pages` is the
    catalog's remaining pages, appended to the strip as they arrive -
    see `ShowcaseWindow.start()` for why they are not waited on."""
    from lib.ui.compat import ADDON
    path = ADDON.getAddonInfo('path')
    win = ShowcaseWindow('ShowcaseWindow.xml', path, 'Default', '1080i')
    try:
        return win.start(metas, catalog_title=catalog_title, more_pages=more_pages)
    finally:
        try:
            win.close()
        except Exception:
            pass


def open_credits_picker(store, client, meta):
    """Cast & crew affordance shared by `ShowcaseWindow` (called above
    with a freshly fetched full meta - a catalog preview has no
    `links`) and `lib.ui.detailwindow.DetailWindow` (which already holds
    its own full meta and passes it straight through, no re-fetch).

    Lives here rather than in detailwindow.py because every dispatch
    branch below needs `open_showcase` - already this module's own
    function - while only the rare detail-kind branch needs
    `detailwindow.open_detail`, the exact same lazy import every other
    `open_showcase()` caller (searchwindow, catalogpicker)
    already makes once a coverflow returns a selection. The
    reverse edge this creates - DetailWindow lazily importing this
    function - is just as function-scoped, so neither module needs the
    other at import time; no real cycle.

    Groups `meta['links']` via `lib.stremio.metalinks.iter_link_groups()`
    and shows a `dialogs.choose()` picker, one two-line row per link
    (name as the primary line, its category as the dim sublabel, in
    group order). A cancelled picker, or a meta
    with no usable links (the latter after `notify(L(30197))`), does
    nothing. Otherwise dispatches the pick:
      - search (a person): reruns `lib.ui.searchwindow.run_query()` and
        opens the results the same way a coverflow selection normally
        would (`_open_results()` below).
      - discover (a genre): fetches that catalog and does the same -
        but only once its transport_url is confirmed to name one of the
        user's own installed addons - an addon-supplied URL must never
        send Rivulet to an arbitrary host.
      - detail: opens `lib.ui.detailwindow.open_detail()` directly.
    """
    import xbmc

    from lib.stremio import metalinks
    from lib.ui import dialogs
    from lib.ui.compat import L, log, notify

    groups = metalinks.iter_link_groups(meta)
    if not groups:
        notify(L(30197))
        return

    rows = []
    links = []
    for category, members in groups:
        for name, parsed in members:
            rows.append((name, category))
            links.append(parsed)

    choice = dialogs.choose(L(30196), rows)
    if choice < 0:
        return
    parsed = links[choice]
    kind = parsed['kind']

    if kind == 'search':
        from lib.ui.searchwindow import run_query
        _open_results(run_query(store, client, parsed['query']))
        return

    if kind == 'discover':
        from lib.stremio.addons import AddonError, safe_url_for_log

        installed = {descriptor.get('transportUrl') for descriptor in store.get_addons()}
        # SECURITY: an addon-supplied discover link's transport_url must
        # never cause a fetch to an arbitrary host - only dispatch it
        # once it's confirmed to name one of the user's own installed
        # addons (resolved against store.get_addons(), never fetched
        # from the link itself).
        if parsed['transport_url'] not in installed:
            log('infowindow: discover link transport not installed: %s' % safe_url_for_log(parsed['transport_url']), xbmc.LOGWARNING)
            return
        from lib.ui.views import _fetch_catalog
        try:
            metas = _fetch_catalog(parsed['transport_url'], parsed['type'], parsed['catalog_id'], extra=parsed['extra'])
        except AddonError as exc:
            log('infowindow: discover fetch %s failed: %s' % (safe_url_for_log(parsed['transport_url']), type(exc).__name__), xbmc.LOGERROR)
            notify(L(30032))
            return
        _open_results(metas)
        return

    if kind == 'detail':
        from lib.ui.detailwindow import open_detail
        open_detail(parsed['type'], parsed['id'])


def _open_results(metas):
    """The tail every `open_showcase()` caller already runs on a fresh
    result set (searchwindow._run_search, catalogpicker._open_catalog):
    notify if empty, else open a coverflow and, if the user picks a
    series there, follow through to `detailwindow.open_detail()` - same
    as those callers, so a pick made from this nested picker is never a
    dead end."""
    from lib.ui.compat import L, notify

    if not metas:
        notify(L(30030))
        return
    selected = open_showcase(metas)
    if not selected:
        return
    from lib.ui.detailwindow import open_detail
    open_detail(selected.get('type') or 'movie', selected.get('id'))
