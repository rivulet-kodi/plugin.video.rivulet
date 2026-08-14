"""ShowcaseWindow: a fullscreen coverflow overlay for one catalog page.

Ports the reference addon's `platformcode/xbmc_info_window.py::InfoWindow`
(`resources/skins/Default/720p/InfoWindow.xml`) to Rivulet/Stremio metas:
`lib.ui.views.showcase()` opens it with one already-fetched catalog page,
the user scrolls a horizontal poster coverflow (the fanart background
updates to match the focused item). Picking a movie poster jumps
straight to `lib.ui.streamswindow.open_streams()` (a movie has nothing
else to pick, same shortcut `lib.ui.detailwindow.open_detail()` takes -
see that module's docstring) using this poster's own title/art, no
extra meta fetch; picking anything else (a series) returns that meta to
the caller so it can navigate there (`views.showcase()`,
`searchwindow.open_search()`, ...).

Control ids mirror the reference addon's InfoWindow 1:1 (see
`ShowcaseWindow.xml`):
    BACKGROUND = 30000  fullscreen fanart image, changes as you scroll
    LOADING    = 30001  busy indicator, hidden once items are loaded
    SELECT     = 30002  horizontal fixedlist - the coverflow itself
    CLOSE      = 30003  close button

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


def _item_properties(meta):
    """Map one Stremio catalog meta to the string Properties
    ShowcaseWindow.xml's coverflow reads via `$INFO[ListItem.Property(...)]`.

    Pure helper - no xbmc - so it is trivially unit-testable on its own.
    """
    meta = meta or {}
    poster = meta.get('poster')
    logo = meta.get('logo')
    background = meta.get('background')
    released = meta.get('released')
    date_only = released.split('T', 1)[0] if released else ''
    return {
        'thumbnail': poster or logo or '',
        'fanart': background or logo or poster or '',
        'genre': ', '.join(meta.get('genres') or []),
        'rating': meta.get('imdbRating') or '',
        'plot': meta.get('description') or '',
        'year': meta.get('releaseInfo') or date_only or '',
    }


class ShowcaseWindow(ModalStackWindow, xbmcgui.WindowXMLDialog):
    """Fullscreen coverflow modal (`ShowcaseWindow.xml`). Build/run it via
    `open_showcase()` below rather than constructing it directly."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.metas = []
        self.selected = None
        self._reset_enrich_state()

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

    def start(self, metas):
        """doModal() with `metas` (a list of Stremio meta dicts) loaded as
        the coverflow's items; returns the selected meta, or None if the
        window closed without a selection. An empty `metas` never opens
        the modal at all and returns None immediately."""
        self.metas = list(metas or [])
        self.selected = None
        self._reset_enrich_state()
        if not self.metas:
            return None
        self.doModal()
        return self.selected

    def _make_item(self, index, meta):
        item = xbmcgui.ListItem(meta.get('name') or meta.get('id') or '?')
        for key, value in _item_properties(meta).items():
            item.setProperty(key, value)
        item.setProperty('position', str(index))
        return item

    def onInit(self):
        if not self.metas:
            return
        items = [self._make_item(index, meta) for index, meta in enumerate(self.metas)]
        control = self.getControl(SELECT)
        # reset() before addItems(): onInit() runs again when
        # uicommon.ModalStackWindow reopens a screen force-closed for
        # playback, and re-adding onto a retained list would double every
        # item.
        control.reset()
        control.addItems(items)
        self.getControl(BACKGROUND).setImage(_item_properties(self.metas[0]).get('fanart', ''))
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
        if self.getFocusId() == SELECT:
            focused = self.getControl(SELECT).getSelectedItem()
            if focused is not None:
                self.getControl(BACKGROUND).setImage(focused.getProperty('fanart'))
                # Both on the UI thread: drain anything a worker finished
                # since the last action, then arm a fetch for this item.
                self._apply_enriched()
                self._enrich_focused(focused)
        action_id = action.getId()
        if action_id == _INFO_ACTION:
            return
        if action_id in _BACK_ACTIONS:
            self.close()

    def close(self):
        # Any exit path - a back action, a selection, or a force-close for
        # playback - must not leave a settle timer armed to fetch for a
        # window that is no longer on screen.
        self._cancel_enrich_timer()
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
            for key, value in props.items():
                item.setProperty(key, value)

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
            self._enrich_pending[index] = {
                key: props.get(key, '') for key in ('genre', 'rating', 'plot', 'year')
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


def open_showcase(metas):
    """Build and run a ShowcaseWindow over `metas`; returns the selected
    meta dict, or None if the user closed the overlay without picking one
    (or `metas` was empty). Every caller already wraps this call in its own
    try/except (catalogpicker._open_catalog, searchwindow.open_search,
    views.showcase/search) and logs+notifies on failure, so an exception
    from .start() keeps propagating unchanged here - this only guarantees
    the window is closed first (it may not have had a chance to self-close,
    e.g. if onInit() or a mid-modal callback raised)."""
    from lib.ui.compat import ADDON
    path = ADDON.getAddonInfo('path')
    win = ShowcaseWindow('ShowcaseWindow.xml', path, 'Default', '720p')
    try:
        return win.start(metas)
    finally:
        try:
            win.close()
        except Exception:
            pass
