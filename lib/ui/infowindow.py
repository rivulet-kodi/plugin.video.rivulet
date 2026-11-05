# SPDX-FileCopyrightText: 2026 M0Rf30
# SPDX-License-Identifier: MIT

"""ShowcaseWindow: a fullscreen coverflow overlay for one catalog page.

Ports the reference addon's `platformcode/xbmc_info_window.py::InfoWindow`
(`resources/skins/Default/1080i/InfoWindow.xml`) to Rivulet/Stremio metas:
a caller opens it via `open_showcase()` (below) with one already-fetched
catalog page - a Home catalog's "see all" (`catalogpicker`) and every
credit link (`open_credit_link()`: a person's titles, a genre) - so every
list of titles outside Search looks the same; the user scrolls a horizontal poster
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
header) for the page title: the catalog's title, or `RIVULET` - see
`_header_label()`.

The coverflow's visual rendering (ShowcaseWindow.xml's fixedlist/
focusedlayout, the background crossfade) is Kodi-skin-engine-only and
cannot be exercised by this test suite - see tests/test_infowindow.py's
module docstring for what a real device must confirm.
"""
import threading

import xbmcgui

from lib import livetv
from lib.ui.uicommon import (
    ModalStackWindow,
    append_keeping_focus,
    close_quietly,
    escape_label,
    gated_input,
    log_paging_failure,
    take_pending_pages,
)
from lib.ui.urlutil import sized_image

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


#: The cell properties a fetched meta can change: everything the hero
#: paints from, plus the IN CINEMA strip a release date feeds.
_HERO_KEYS = (
    'hero_meta', 'hero_plot', 'hero_facts', 'hero_runtime', 'hero_fill', 'in_cinema',
)


def _hero_properties(meta):
    """`_HERO_KEYS` for `meta`, as a dict a worker may build off the GUI
    thread (`home._poster_properties` never makes a `ListItem`). `facts_done`
    is cleared so the next paint merges the cached ratings into the new
    catalog line again."""
    from lib.ui.home import _poster_properties

    props = _poster_properties(meta)
    merged = {key: props[key] for key in _HERO_KEYS}
    merged['facts_done'] = ''
    return merged


def _header_label(catalog_title=None):
    """The header text: the catalog's own title, bold and as written, like
    Search's and Add-ons' plain page titles; bare `RIVULET` when a caller
    has none. The catalog title is addon text, so it is escaped.

    Pure helper - no xbmc - so it is trivially unit-testable on its own.
    """
    from lib.stremio.streaminfo import escape_label

    if not catalog_title:
        return '[B]RIVULET[/B]'
    return '[B]%s[/B]' % escape_label(catalog_title)


class ShowcaseWindow(ModalStackWindow, xbmcgui.WindowXMLDialog):
    """Fullscreen coverflow modal (`ShowcaseWindow.xml`). Build/run it via
    `open_showcase()` below rather than constructing it directly."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.metas = []
        self.selected = None
        #: catalog/section title for the header - None keeps it a bare
        #: "RIVULET", see `_header_label()`/`start()`.
        self.catalog_title = None
        #: What the hero last painted (`look.paint_poster_hero`), which
        #: skips an unchanged hero - a keypress that does not change focus
        #: (e.g. the enrich worker's Action(noop) wake-up) costs nothing.
        self._hero_painted = None
        #: The live now/next worker (`lib.ui.livenow.NowNextFeed`), started
        #: when a live poster is first focused and stopped by `close()`.
        self._now_feed = None
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

        `catalog_title`, if given, is the header's page title - see
        `_header_label()`. Left as None, the header stays a bare
        "RIVULET".

        `more_pages`, if given, is an iterable of further meta pages
        (`lib.ui.views.iter_catalog_pages` past its first page). It is
        walked on a daemon thread and each page appended to the live
        coverflow as it arrives, so a catalog the addon serves 20 at a
        time opens immediately on those 20 instead of behind a spinner
        counting to 400. Exhausting it is best-effort: a page that never
        arrives, or an addon that fails halfway, just leaves the strip at
        whatever did land."""
        # Catalog pages keep non-dict entries on purpose (see
        # views._dedupe_page); the strip reads every meta with .get().
        self.metas = [m for m in metas or [] if isinstance(m, dict)]
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
                page = [m for m in page or [] if isinstance(m, dict)]
                if not page:
                    continue
                with self._paging_lock:
                    self._pending_pages.append(page)
                # Wake the UI thread to drain the queue, exactly as the
                # enrich workers do - see `_enrich_fetch`'s tail.
                xbmc.executebuiltin('Action(noop)')
        except Exception as exc:
            # A failed walk must never take the window down; a paging
            # addon failing silently used to leave a truncated strip with
            # no trace in the log at the default log level.
            log_paging_failure('infowindow', exc)

    def _apply_pending_pages(self):
        """Append whatever the paging worker has queued onto the live
        coverflow. Always call this from the UI thread.

        Appending keeps every already-enriched item untouched: indices
        only ever grow, so a page landing mid-scroll cannot renumber the
        item under focus. The cursor is not preserved for free, though -
        see `append_keeping_focus()`; `streamswindow._merge_and_refresh()`
        guards it for the same reason, by object identity there because
        it re-sorts rather than appends.
        """
        items = []
        for page in take_pending_pages(self):
            for meta in page:
                items.append(self._make_item(len(self.metas), meta))
                self.metas.append(meta)
        if items:
            append_keeping_focus(self.getControl(SELECT), items)

    def _make_item(self, index, meta):
        """One cell, built like Search's: `home._poster_item` carries the
        poster, the badges and every property the shared hero reads."""
        from lib.ui.home import _poster_item

        item = _poster_item(meta)
        if not meta.get('name'):
            item.setLabel(escape_label(meta.get('id') or '?'))
        if not item.getProperty('thumbnail') and meta.get('logo'):
            # A logo-only meta still needs a thumbnail; Home's cell has none.
            item.setProperty('thumbnail', sized_image(meta['logo'], 'poster'))
        item.setProperty('position', str(index))
        return item

    def onInit(self):
        if not self.metas:
            return
        self.getControl(HEADER).setLabel(_header_label(self.catalog_title))
        # The check is a set lookup per card (`watchmarks`); recompute the
        # set once here, which is also what a reopen after playback runs,
        # so a movie just finished shows watched when the strip comes back.
        # The account's answer is the service's cached copy of the library,
        # so even a coverflow opened in a fresh interpreter reads no network.
        try:
            from lib.ui import watchmarks
            from lib.ui.dependencies import get_store

            watchmarks.refresh(get_store())
        except Exception:
            pass
        items = [self._make_item(index, meta) for index, meta in enumerate(self.metas)]
        control = self.getControl(SELECT)
        # reset() before addItems(): onInit() runs again when
        # uicommon.ModalStackWindow reopens a screen force-closed for
        # playback, and re-adding onto a retained list would double every
        # item.
        control.reset()
        control.addItems(items)
        # A reopen rebuilds the window, so the last painted hero is stale.
        self._hero_painted = None
        self.getControl(LOADING).setVisible(False)
        self.setFocusId(SELECT)
        # A rebuild discards the ListItems a pending merge was queued
        # against, so apply whatever landed while we were away onto the
        # fresh ones before enriching further.
        self._apply_enriched()
        # Painted after the focus is set: the hero warms the logos of the
        # posters after the focused one, which it finds through the focus.
        self._paint_hero(items[0])
        # onAction() enriches on every focus change, but never fires for the
        # item that opens focused - enrich item 0 here so the window doesn't
        # open on a blank plot until the user scrolls off it and back.
        if items:
            self._enrich_focused(items[0], settle=False)

    @gated_input
    def onAction(self, action):
        # Before reading focus: a queued page appends items, and doing it
        # first means the strip is whole by the time anything below asks
        # what is focused.
        self._apply_pending_pages()
        if self.getFocusId() == SELECT:
            focused = self.getControl(SELECT).getSelectedItem()
            if focused is not None:
                # Both on the UI thread: drain anything a worker finished
                # since the last action, paint the hero from it (the
                # ratings worker's wake-up lands here too), then arm a
                # fetch for this item.
                self._apply_enriched()
                self._paint_hero(focused)
                self._enrich_focused(focused)
        action_id = action.getId()
        if action_id == _INFO_ACTION:
            return
        if action_id == _CONTEXT_MENU_ACTION:
            self._open_credits()
            return
        if action_id in _BACK_ACTIONS:
            self.close()

    def _paint_hero(self, item):
        """The shared poster hero (logo or title, stars and facts, scores,
        plot, backdrop) for `item`, placed above the strip."""
        from lib.ui import livenow, look

        # A live poster's now/next goes onto the item first: the hero
        # below reads the item, and a changed plot/facts repaints it.
        # The coverflow does not know the channel's guide catalog, so the
        # schedule comes from the channel's own meta (`livenow`).
        self._now_feed = livenow.ensure_feed(self._now_feed)
        livenow.track_item(self._now_feed, item)
        # Home's stack: the row sits where Search's does, its position
        # line at y=630, so the hero ends at the same y=590.
        look.paint_poster_hero(self, item, None, BACKGROUND)

    def _open_credits(self):
        """ACTION_CONTEXT_MENU on the coverflow: resolve the focused
        poster the exact same way onAction()'s own background swap does
        above (getFocusId()/getSelectedItem()/position -> self.metas) -
        no second "what's focused" mechanism - then fetch its full meta
        (a catalog preview has no `links`, see `_poster_item()`) and
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
        from lib.ui.uicommon import CANCELLED, run_cancellable
        from lib.ui.views import _fetch_meta

        full_meta = run_cancellable(lambda: _fetch_meta(stype, sid), L(30033))
        if full_meta is CANCELLED:
            return
        open_credits_picker(get_store(), get_client(), full_meta)

    def close(self):
        # Any exit path - a back action, a selection, or a force-close for
        # playback - must not leave a settle timer armed to fetch for a
        # window that is no longer on screen.
        self._cancel_enrich_timer()
        # Same for the paging walk: setting this is what the worker polls
        # between pages to stop fetching for a strip nobody is looking at.
        self._paging_stopped.set()
        # And the live schedule worker: plain data only, so stopping it is
        # all a close owes it (it never held an item).
        if self._now_feed is not None:
            self._now_feed.stop()
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
            # The merge just rewrote hero_plot/hero_meta: a live card's
            # now/next must be composed again over the new values.
            from lib.ui import livenow

            livenow.reset_item(item)

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
            # LOGWARNING, not DEBUG: an addon failing every enrich fetch
            # used to leave the coverflow's plot/genre blank with nothing
            # in the log at the default level to explain why.
            from lib.stremio.addons import addon_error_detail

            log('infowindow: meta enrich failed for %s: %s' % (meta.get('id'), addon_error_detail(exc)), xbmc.LOGWARNING)
            return
        if not full:
            return
        # Merge rather than replace: the preview's own poster/background are
        # what the strip is already showing, and re-setting them would make
        # the focused art flicker as the fetch lands.
        #
        # `full` is unvalidated third-party addon JSON, so check the shape
        # each field is consumed as. `_hero_meta_text()` joins `genres`, and
        # a str is iterable - an addon sending "Drama" instead of ["Drama"]
        # would otherwise render as "D, r, a, m, a"; a list of anything but
        # strings is dropped to a blank label on purpose, for the same
        # reason. `imdbRating`, `releaseInfo` and `released` are routinely
        # numeric in the wild (streamswindow.py str()s the one,
        # playbackmeta.py float()s the other), so those are coerced rather
        # than rejected. `released` is merged as well as `releaseInfo`
        # because the hero's facts line derives the year from either.
        for key in ('description', 'genres', 'imdbRating', 'releaseInfo', 'released', 'dvdRelease'):
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
        props = _hero_properties(meta)
        # Hand off to the UI thread rather than touching the ListItem here:
        # it is inside the ControlList the skin renders every frame.
        with self._enrich_lock:
            self._enrich_pending[index] = props
        # ...and wake it, or nothing would apply the queue until the user's
        # next keypress - by which time focus has usually left the item
        # that was fetched. executebuiltin() posts to Kodi's application
        # messenger, which runs Action(noop) on the GUI thread and delivers
        # it to this dialog's onAction(): the drain, and the repaint, then
        # happen there. ACTION_NOOP itself does nothing else, and is not a
        # back action.
        xbmc.executebuiltin('Action(noop)')

    @gated_input
    def onClick(self, control_id):
        if control_id == SELECT:
            focused = self.getControl(SELECT).getSelectedItem()
            if focused is None:
                return
            meta = self.metas[int(focused.getProperty('position'))]
            if (meta.get('type') == 'movie' or livetv.is_live(meta)) and meta.get('id'):
                self._play_movie(meta)
                # Playback force-closed this window (flag set) so doModal()
                # would reopen it behind the player; this click is the one
                # real close, so cancel the reopen.
                self._closed_for_playback = False
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
        `onClick()` clears `_closed_for_playback` and calls its own
        `self.close()` - so `ModalStackWindow.doModal()` never reopens a
        window that was about to close anyway."""
        from lib.ui.streamswindow import open_live, open_streams

        # A live channel plays at once when the add-ons offer one stream
        # (`streamswindow.open_live`); it has no page to fall back on.
        live = livetv.is_live(meta)
        poster = meta.get('poster') or (meta.get('logo') if live else None)
        fanart = meta.get('background') or meta.get('logo') or poster
        opener = open_live if live else open_streams
        opener(
            meta.get('type'), meta.get('id'),
            poster=poster,
            heading=meta.get('name') or meta.get('id') or '',
            art={'poster': poster, 'fanart': fanart}, meta=meta,
        )


def open_showcase(metas, catalog_title=None, more_pages=None):
    """Build and run a ShowcaseWindow over `metas`; returns the selected
    meta dict, or None if the user closed the overlay without picking one
    (or `metas` was empty). Every caller already wraps this call in its own
    try/except (catalogpicker.open_single_catalog, the credit links'
    callers) and logs+notifies on failure, so an
    exception from .start() keeps propagating unchanged here - this
    only guarantees
    the window is closed first (it may not have had a chance to self-close,
    e.g. if onInit() or a mid-modal callback raised).

    `catalog_title`, if given, becomes the header's page title - see
    `ShowcaseWindow.start()`. `more_pages` is the catalog's remaining pages, appended to the strip as they arrive -
    see `ShowcaseWindow.start()` for why they are not waited on."""
    from lib.ui.compat import ADDON
    path = ADDON.getAddonInfo('path')
    win = ShowcaseWindow('ShowcaseWindow.xml', path, 'Default', '1080i')
    try:
        return win.start(metas, catalog_title=catalog_title, more_pages=more_pages)
    finally:
        close_quietly(win)


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
    nothing; a pick goes to `open_credit_link()`.
    """
    from lib.stremio import metalinks
    from lib.ui import dialogs
    from lib.ui.compat import L, notify
    from lib.ui.uicommon import escape_label

    groups = metalinks.iter_link_groups(meta)
    if not groups:
        notify(L(30197))
        return

    rows = []
    links = []
    for category, members in groups:
        for name, parsed in members:
            rows.append((escape_label(name), escape_label(category)))
            links.append((name, parsed))

    choice = dialogs.choose(L(30196), rows)
    if choice < 0:
        return
    name, parsed = links[choice]
    open_credit_link(store, client, parsed, name)


def open_credit_link(store, client, parsed, title=''):
    """Dispatch one parsed Meta Link (`lib.stremio.metalinks.parse_meta_link()`)
    - the single place a cast/crew/genre pick turns into navigation, shared
    by `open_credits_picker()`'s list and the clickable credit chips on
    `lib.ui.detailwindow.DetailWindow` / `lib.ui.streamswindow.StreamsWindow`
    (`lib.ui.creditsrow`), so all three behave identically. `title` is the
    link's own name (the chip or picker row), used as the coverflow heading.

    Every list of titles a link leads to is the same coverflow a Home
    catalog's "see all" opens (`open_showcase()`): a person's filmography
    and a genre used to land on two different-looking screens (a person on
    the Search screen's row, a genre here), which read as two products.
      - search (a person): `searchwindow.run_query()` on the name, as
        Stremio's `stremio:///search` link does.
      - discover (a genre): fetches that catalog - but only once its
        transport_url is confirmed to name one of the user's own installed
        addons; an addon-supplied URL must never send Rivulet to an
        arbitrary host.
      - detail: opens `lib.ui.detailwindow.open_detail()` directly.
    """
    import xbmc

    from lib.ui.compat import L, log, notify

    kind = parsed['kind']

    if kind == 'search':
        _open_person(store, client, parsed['query'], title)
        return

    if kind == 'discover':
        from lib.stremio.addons import AddonError, addon_error_detail, safe_url_for_log
        from lib.stremio.contentrating import filter_metas, is_adult_catalog

        installed = {descriptor.get('transportUrl') for descriptor in store.get_enabled_addons()}
        # SECURITY: an addon-supplied discover link's transport_url must
        # never cause a fetch to an arbitrary host - only dispatch it
        # once it's confirmed to name one of the user's own installed
        # AND enabled addons (resolved against store.get_enabled_addons(),
        # never fetched from the link itself) - a disabled addon is not
        # dispatchable either.
        if parsed['transport_url'] not in installed:
            log('infowindow: discover link transport not installed: %s' % safe_url_for_log(parsed['transport_url']), xbmc.LOGWARNING)
            return
        from lib.ui.views import _adult_filtering_enabled, _fetch_catalog

        # Same home_hide_adult policy as lib.ui.views.iter_catalog_pages()
        # and searchwindow.run_query(): a meta's embedded "more like
        # this"/genre discover link can point straight at an
        # adult-flagged catalog, so it needs the identical two-layer
        # check - skip the fetch entirely for a catalog that is itself
        # adult, else filter adult metas from what it returns.
        hide_adult = _adult_filtering_enabled()
        if hide_adult and is_adult_catalog({'id': parsed['catalog_id'], 'type': parsed['type']}):
            _open_results([], title)
            return
        from lib.ui.uicommon import CANCELLED, run_cancellable

        try:
            metas = run_cancellable(
                lambda: _fetch_catalog(
                    parsed['transport_url'], parsed['type'], parsed['catalog_id'], extra=parsed['extra'],
                ),
                L(30033),
            )
        except AddonError as exc:
            log('infowindow: discover fetch %s failed: %s' % (safe_url_for_log(parsed['transport_url']), addon_error_detail(exc)), xbmc.LOGERROR)
            notify(L(30032))
            return
        if metas is CANCELLED:
            return
        if hide_adult:
            metas = filter_metas(metas)
        _open_results(metas, title)
        return

    if kind == 'detail':
        from lib.ui.detailwindow import open_detail
        open_detail(parsed['type'], parsed['id'])


def _open_person(store, client, query, title):
    """A person's credit: search their name across every search-capable
    catalog and show the titles in the coverflow. Back on the spinner, a
    name that is only blanks, or a search that failed outright stays where
    the user was; some catalogs failing is said in a toast over the
    results (the coverflow has no status line)."""
    import xbmc

    from lib.ui.compat import L, log, notify
    from lib.ui.search import MAX_QUERY
    from lib.ui.searchwindow import SearchReport, _search_notice, run_query

    query = (query or '').strip()[:MAX_QUERY]
    if not query:
        return
    report = SearchReport()
    try:
        metas = list(run_query(store, client, query, report=report) or [])
    except Exception as exc:
        log('infowindow: person search failed: %s' % type(exc).__name__, xbmc.LOGWARNING)
        notify(L(30032))
        return
    if report.canceled:
        return
    notice = _search_notice(report)
    if metas and notice:
        notify(notice)
    _open_results(metas, title or query, empty_notice=notice)


def _open_results(metas, title='', empty_notice=None):
    """Show a credit link's titles in the coverflow, headed by the link's
    name, and follow a pick through to `detailwindow.open_detail()` the
    way every other coverflow caller does, so a pick made from this nested
    picker is never a dead end. Nothing to show says so with the name -
    "No results for ..." - rather than the stream picker's "No streams
    found", which is what a genre with no titles used to say."""
    from lib.ui.compat import L, notify

    if not metas:
        from lib.ui.search import _with_query
        # Every caller passes the link's name (chip label, picker row).
        notify(empty_notice or _with_query(L(30513), title))
        return
    selected = open_showcase(metas, catalog_title=title or None)
    if not selected:
        return
    from lib.ui.detailwindow import open_detail
    open_detail(selected.get('type') or 'movie', selected.get('id'))
