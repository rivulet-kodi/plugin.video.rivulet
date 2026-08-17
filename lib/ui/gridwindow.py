"""GridWindow: a fullscreen wrapping poster grid, opened via
`open_grid()`. Built for `lib.ui.mystuff`'s merged "My Stuff" screen,
which routinely holds the whole Stremio library plus every played title -
past what `lib.ui.infowindow`'s single-row coverflow is browsable at.

The grid itself is `GridWindow.xml`'s `panel` control (id 30002); this
module only projects merged items onto `xbmcgui.ListItem`s and returns
the one picked. Each cell carries three runtime properties the skin
draws (Kodi's skin engine can neither compute nor format, so all three
are prepared here):

    thumbnail       poster URL
    badge           the short band caption under the title ("62%",
                    "NEXT UP", "WATCHED")
    progress_width  the filled width, in skin pixels, of the 240px
                    progress track - empty string for a title with no
                    meaningful progress, which is also what the skin's
                    `String.IsEmpty` visibility test keys off, so the
                    whole bar disappears rather than rendering at zero.

Control ids mirror the other list screens (`CatalogPickerWindow`,
`AddonsWindow`) rather than the coverflow's, since this is a plain
list-shaped screen:
    BACKGROUND = 30000  fanart of the focused item
    LIST       = 30002  the panel/grid
    HEADING    = 30006  "RIVULET / MY STUFF" breadcrumb

Like every other Rivulet screen, the grid's actual rendering is
Kodi-skin-engine-only and cannot be exercised by this test suite - see
tests/test_gridwindow.py for what is covered here (the pure projection)
and what a real device must confirm.
"""
import xbmcgui

from lib.ui.uicommon import BaseWindow, open_window

BACKGROUND = 30000
LIST = 30002
HEADING = 30006

#: Width, in skin pixels, of a cell's progress track - must match the
#: 216px bar in `GridWindow.xml`'s item layouts, since `_progress_width()`
#: scales into it.
#:
#: 216 is the poster's REAL rendered width, not merely its layout box: the
#: artwork is drawn `aspectratio=keep` into a 216x324 box, a true 2:3, so
#: a standard poster fills it edge to edge. The two must stay equal - at
#: an earlier 240 track against that same 216-wide rendered poster the bar
#: overhung the artwork by 12px each side, plainly visible on a real
#: device against the focused cell's outline.
PROGRESS_TRACK_WIDTH = 216

#: Floor on a rendered progress bar, in skin pixels. A title watched 1-2%
#: scales to a sub-pixel sliver that renders as nothing at all, which
#: reads as "no progress" rather than "just started" - the one visual
#: state the bar exists to distinguish.
_MIN_PROGRESS_WIDTH = 4


def _progress_width(item):
    """The filled width of a cell's progress track, as the string the
    skin's `<width>` expects - or '' when this item should draw no bar
    at all (anything but a part-watched title: next-up, watched, and
    never-played library entries all have nothing meaningful to fill).

    Clamped into `[_MIN_PROGRESS_WIDTH, PROGRESS_TRACK_WIDTH]` so a
    barely-started title still shows a visible sliver and a percent
    over 100 (a corrupt cache entry, or a duration that shrank between
    samples) can never overhang the track.
    """
    from lib.ui.mystuff import BAND_RESUME

    if item.get('band') != BAND_RESUME:
        return ''
    percent = item.get('percent')
    if not isinstance(percent, (int, float)) or isinstance(percent, bool):
        return ''
    width = int(round(PROGRESS_TRACK_WIDTH * (percent / 100.0)))
    return str(max(_MIN_PROGRESS_WIDTH, min(PROGRESS_TRACK_WIDTH, width)))


#: band -> the accent colour its badge is drawn in. The bands are what
#: order the grid, but on a real device a flat wall of posters gave no
#: way to tell a half-watched title from a saved one: the badge text was
#: the only cue, and at font10 in one dim grey it read as noise. Colour
#: is what makes the distinction legible at a glance, across a whole
#: screen, without spending a poster row on section headers.
#:
#: Blue is the skin's own accent (resume - the common case, and what the
#: progress bar under it is already tinted). Amber lifts next-up out of
#: that blue so "there is a new episode waiting" is the one thing that
#: catches the eye. Watched is deliberately the dimmest thing on screen.
_BAND_COLOURS = {
    'resume': 'FF38BDF8',
    'next_up': 'FFFBBF24',
    'recent': '80EEF3F6',
}


def _badge(item):
    """The short caption under a cell's title, naming why the title is on
    this screen: its percent for a part-watched title, the localized band
    name for next-up/watched (with the episode that would actually play,
    when it is known), and nothing at all for a library title that has
    never been played - the absence IS the state, and a grid where every
    cell carries a badge makes the badges worthless.

    Wrapped in the band's `[COLOR]` (see `_BAND_COLOURS`) so the bands
    stay distinguishable across a screenful of posters."""
    from lib.ui.compat import L
    from lib.ui.mystuff import BAND_NEXT_UP, BAND_RECENT, BAND_RESUME

    band = item.get('band')
    text = ''
    if band == BAND_RESUME:
        percent = item.get('percent')
        if isinstance(percent, (int, float)) and not isinstance(percent, bool):
            text = '%d%%' % int(percent)
    elif band == BAND_NEXT_UP:
        # The episode itself is the useful part - "S1E3" says more than
        # "NEXT UP", and says it in less room.
        text = item.get('next_label') or L(30242)
    elif band == BAND_RECENT:
        text = L(30243)
    if not text:
        return ''
    colour = _BAND_COLOURS.get(band)
    return '[COLOR %s]%s[/COLOR]' % (colour, text) if colour else text


def _caption(item):
    """The line under the focused title at the top of the screen: the
    band's own name, plus the next episode when one has been resolved
    (`lib.ui.mystuff.resolve_next_up()`), so a next-up title says which
    episode it would play. Unlike `_badge()` this has room for the band's
    full name, which is what actually tells the viewer WHY the title is
    on this screen."""
    from lib.ui.compat import L
    from lib.ui.mystuff import BAND_HEADINGS, BAND_NEXT_UP, BAND_RESUME

    band = item.get('band')
    heading = BAND_HEADINGS.get(band)
    parts = [L(heading)] if heading else []
    if band == BAND_RESUME:
        percent = item.get('percent')
        if isinstance(percent, (int, float)) and not isinstance(percent, bool):
            parts.append('%d%%' % int(percent))
    elif band == BAND_NEXT_UP and item.get('next_label'):
        parts.append(item['next_label'])
    return ' · '.join(parts)


def make_list_item(item):
    """Project one merged `lib.ui.mystuff` item onto a `ListItem` for the
    grid. Pure apart from `L()`, so tests can assert every property
    without a window."""
    from lib.ui.mystuff import BAND_RECENT

    list_item = xbmcgui.ListItem(label=item.get('name') or '')
    list_item.setProperties({
        'thumbnail': item.get('poster') or '',
        'badge': _badge(item),
        'progress_width': _progress_width(item),
        'caption': _caption(item),
        # Finished titles are dimmed by the skin so the eye skips past
        # them, the way a watched episode greys out in most apps. A
        # non-empty property is the skin's visibility condition, so the
        # value itself is arbitrary - only its presence matters.
        'watched': '1' if item.get('band') == BAND_RECENT else '',
    })
    return list_item


class GridWindow(BaseWindow):
    """See module docstring. Built/run via `open_grid()`."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.items = []
        self.heading = ''
        self.selected = None

    def start(self, items, heading=''):
        """doModal() with `items` (merged `lib.ui.mystuff` dicts) as the
        grid's cells, under `heading`. Returns the item picked, or None
        if the user backed out without picking one."""
        self.items = list(items or [])
        self.heading = heading or ''
        self.selected = None
        if not self.items:
            return None
        self.doModal()
        return self.selected

    def onInit(self):
        from lib.ui.compat import L

        control = self.getControl(LIST)
        # reset() before addItems(): onInit() runs again when
        # uicommon.ModalStackWindow reopens a screen force-closed for
        # playback, and re-adding onto a retained list would double every
        # cell.
        control.reset()
        control.addItems([make_list_item(item) for item in self.items])
        self.getControl(HEADING).setLabel('RIVULET / %s' % ((self.heading or L(30241)).upper(),))
        self.setFocusId(LIST)
        self._update_background()

    def _focused_item(self):
        """The merged item under the grid's current selection, or None.

        Indexes `self.items` by the control's own position rather than
        carrying an index property per cell: the grid never reorders or
        filters after `onInit()`, so position and list index stay in
        lockstep. Bounds-checked anyway - `getSelectedPosition()` returns
        -1 for an empty container."""
        try:
            position = self.getControl(LIST).getSelectedPosition()
        except Exception:
            return None
        if position is None or position < 0 or position >= len(self.items):
            return None
        return self.items[position]

    def _update_background(self):
        """Swap the fanart to the focused item's own background. Silently
        does nothing when the item has none (the dimmed base plate below
        it is a perfectly good empty state) - and never raises, since a
        background swap must not be able to break navigation."""
        item = self._focused_item()
        try:
            self.getControl(BACKGROUND).setImage((item or {}).get('background') or '')
        except Exception:
            pass

    def onAction(self, action):
        from lib.ui.uicommon import BACK_ACTIONS

        if action.getId() in BACK_ACTIONS:
            self.close()
            return
        # Any non-back action may have moved the focus; the background
        # follows it. Cheap - a property read plus a setImage() - so it
        # does not need the coverflow's settle-timer treatment (that one
        # debounces a network meta fetch, not an image swap).
        self._update_background()

    def onClick(self, control_id):
        if control_id != LIST:
            return
        item = self._focused_item()
        if item is None:
            return
        self.selected = item
        self.close()


def open_grid(items, heading=''):
    """Build and run a GridWindow over `items`; returns the selected item
    dict, or None if the user closed it without picking one (or `items`
    was empty).

    The caller (`lib.ui.mystuff.open_my_stuff()`) wraps this in its own
    try/except and logs+notifies on failure, so an exception from
    `.start()` keeps propagating unchanged - this only guarantees the
    window is closed first (it may not have had a chance to self-close,
    e.g. if onInit() or a mid-modal callback raised)."""
    win = open_window(GridWindow, 'GridWindow.xml')
    try:
        return win.start(items, heading=heading)
    finally:
        try:
            win.close()
        except Exception:
            pass
