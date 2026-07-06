"""ShowcaseWindow: a fullscreen coverflow overlay for one catalog page.

Ports the reference addon's `platformcode/xbmc_info_window.py::InfoWindow`
(`resources/skins/Default/720p/InfoWindow.xml`) to Rivulet/Stremio metas:
`lib.ui.views.showcase()` opens it with one already-fetched catalog page,
the user scrolls a horizontal poster coverflow (the fanart background
updates to match the focused item), and picking a poster returns that
meta to the caller so `views.showcase()` can navigate there.

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
import xbmcgui

BACKGROUND = 30000
LOADING = 30001
SELECT = 30002
CLOSE = 30003

# Back/Nav-Back, PreviousMenu/Esc, Backspace - any of these closes the
# overlay without a selection, same as the reference InfoWindow.
_BACK_ACTIONS = frozenset({9, 10, 92})


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


class ShowcaseWindow(xbmcgui.WindowXMLDialog):
    """Fullscreen coverflow modal (`ShowcaseWindow.xml`). Build/run it via
    `open_showcase()` below rather than constructing it directly."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.metas = []
        self.selected = None

    def start(self, metas):
        """doModal() with `metas` (a list of Stremio meta dicts) loaded as
        the coverflow's items; returns the selected meta, or None if the
        window closed without a selection. An empty `metas` never opens
        the modal at all and returns None immediately."""
        self.metas = list(metas or [])
        self.selected = None
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
        self.getControl(SELECT).addItems(items)
        self.getControl(BACKGROUND).setImage(_item_properties(self.metas[0]).get('fanart', ''))
        self.getControl(LOADING).setVisible(False)
        self.setFocusId(SELECT)

    def onAction(self, action):
        if self.getFocusId() == SELECT:
            focused = self.getControl(SELECT).getSelectedItem()
            if focused is not None:
                self.getControl(BACKGROUND).setImage(focused.getProperty('fanart'))
        if action.getId() in _BACK_ACTIONS:
            self.close()

    def onClick(self, control_id):
        if control_id == SELECT:
            focused = self.getControl(SELECT).getSelectedItem()
            self.selected = self.metas[int(focused.getProperty('position'))]
            self.close()
        elif control_id == CLOSE:
            self.close()


def open_showcase(metas):
    """Build and run a ShowcaseWindow over `metas`; returns the selected
    meta dict, or None if the user closed the overlay without picking one
    (or `metas` was empty)."""
    from lib.ui.compat import ADDON
    path = ADDON.getAddonInfo('path')
    win = ShowcaseWindow('ShowcaseWindow.xml', path, 'Default', '720p')
    return win.start(metas)
