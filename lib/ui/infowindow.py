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

import xbmc
import xbmcgui

from lib.ui.uicommon import ModalStackWindow

BACKGROUND = 30000
LOADING = 30001
SELECT = 30002
CLOSE = 30003

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
        #: indices whose full meta has been fetched (or found already complete)
        self._enriched = set()

    def start(self, metas):
        """doModal() with `metas` (a list of Stremio meta dicts) loaded as
        the coverflow's items; returns the selected meta, or None if the
        window closed without a selection. An empty `metas` never opens
        the modal at all and returns None immediately."""
        self.metas = list(metas or [])
        self.selected = None
        self._enriched = set()
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
        # onAction() enriches on every focus change, but never fires for the
        # item that opens focused - enrich item 0 here so the window doesn't
        # open on a blank plot until the user scrolls off it and back.
        if items:
            self._enrich_focused(items[0])

    def onAction(self, action):
        if self.getFocusId() == SELECT:
            focused = self.getControl(SELECT).getSelectedItem()
            if focused is not None:
                self.getControl(BACKGROUND).setImage(focused.getProperty('fanart'))
                self._enrich_focused(focused)
        action_id = action.getId()
        if action_id == _INFO_ACTION:
            return
        if action_id in _BACK_ACTIONS:
            self.close()

    def _enrich_focused(self, item):
        """Fill in the description/genres a catalog meta preview lacks.

        Stremio's `catalog` resource returns meta *previews*, which carry
        name/poster/year but commonly omit `description` and `genres` -
        those only exist on the full `meta` resource. Discover's catalogs
        happen to include them often enough that the coverflow looked
        complete; search results routinely do not, leaving the window's
        plot/genre labels blank.

        Fetch the full meta lazily for whichever poster is focused, on a
        daemon thread (`_fetch_meta` is a blocking HTTP call - doing it
        inline would stall the scroll), write the result back onto both
        the ListItem and `self.metas[index]`, and cache per index so
        scrolling back and forth re-fetches nothing. Every failure is
        non-fatal: the labels simply stay as they were.
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
            self._enriched.add(index)
            return
        self._enriched.add(index)
        threading.Thread(target=self._enrich_worker, args=(index, item, meta), daemon=True).start()

    def _enrich_worker(self, index, item, meta):
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
        for key in ('description', 'genres', 'imdbRating', 'releaseInfo'):
            if full.get(key) and not meta.get(key):
                meta[key] = full[key]
        props = _item_properties(meta)
        for key in ('genre', 'rating', 'plot', 'year'):
            item.setProperty(key, props.get(key, ''))

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
