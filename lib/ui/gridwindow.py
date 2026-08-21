"""GridWindow: the merged "Continue" screen, opened via `open_grid()` -
one labelled, horizontally-scrolling poster row per band of
`lib.ui.mystuff`'s merge, stacked vertically in `GridWindow.xml`'s
`grouplist` so several bands are on screen at once.

This module fills those rows and returns the title picked. Each cell
carries four runtime properties the skin draws (Kodi's skin engine can
neither compute nor format, so all four are prepared here):

    thumbnail       poster URL
    badge           the short band caption under the title - the percent
                    watched, the next episode ("S1E3"), or "WATCHED",
                    tinted per band (see `_BAND_COLOURS`)
    progress_width  the filled width, in skin pixels, of the progress
                    track - empty string for a title with no meaningful
                    progress, which is also what the skin's
                    `String.IsEmpty` visibility test keys off, so the
                    whole bar disappears rather than rendering at zero
    watched         non-empty on a finished title, which the skin draws
                    dimmed so the eye skips past it

Each band owns a (list, header-label) control-id pair - see `BAND_ROWS`.
A band with no titles is not drawn at all: the skin hides both controls
on the list's own `NumItems`, so an empty band leaves no stray heading
behind, and `usecontrolcoords` closes the gap it would have taken.

Control ids:
    BACKGROUND = 30000  fanart of the focused item
    ROWS       = 30001  the vertical grouplist holding every band row
    HEADING    = 30006  "RIVULET / CONTINUE" breadcrumb
    300x2/301x7        per-band list and header label (see `BAND_ROWS`)

Like every other Rivulet screen, the actual rendering is
Kodi-skin-engine-only and cannot be exercised by this test suite - see
tests/test_gridwindow.py for what is covered here (the pure projection
and the control wiring) and what a real device must confirm.
"""
import xbmcgui

from lib.ui.uicommon import BaseWindow, open_window

BACKGROUND = 30000
ROWS = 30001
HEADING = 30006

#: band -> (list control id, header label control id), in the order the
#: skin stacks them. Must match `GridWindow.xml` exactly: the skin hard-
#: codes these ids in each row's `visible` condition, since a Kodi skin
#: cannot be told its control ids at runtime.
BAND_ROWS = {
    'resume': (30002, 30107),
    'next_up': (30012, 30117),
    'recent': (30022, 30127),
    'library': (30032, 30137),
}

#: Reused by `lib.ui.homewindow`'s "New Episodes" Home row for its own,
#: standalone single-band GridWindow session - that session never runs
#: alongside "My Stuff"'s four real bands, so borrowing the 'library'
#: row's (list, header) control ids for it is safe. The skin has no 5th
#: pair to give a genuinely new band, which the no-new-skin-XML rule
#: forbids adding; `open_grid()`'s `labels=` overrides that row's header
#: text away from "Your library" for the borrowing session.
NEW_EPISODES_BAND = 'library'

#: The band whose row is focused when the screen opens - the first one
#: with anything in it, in `mystuff.BAND_ORDER`.
LIST = BAND_ROWS['resume'][0]

#: Width, in skin pixels, of a cell's progress track - must match the
#: 288px bar in `GridWindow.xml`'s cell layouts, since `_progress_width()`
#: scales into it.
#:
#: 288 is the poster's REAL rendered width, not merely its layout box: the
#: artwork is drawn `aspectratio=keep` into a 288x432 box, a true 2:3, so
#: a standard poster fills it edge to edge. The two must stay equal - at
#: an earlier 240 track against a 216-wide rendered poster the bar
#: overhung the artwork by 12px each side, plainly visible on a real
#: device against the focused cell's outline. A test pins them together.
PROGRESS_TRACK_WIDTH = 288

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
    """The line under the focused title at the top of the screen.

    Deliberately does NOT name the band. The focused card always sits
    directly under its own band header, so repeating "Continue watching"
    here put the same words on screen twice within about 80 pixels - and
    a third time in the breadcrumb. What the header cannot say is where
    THIS title stands, so that is all this line carries: how far in you
    are, or which episode is next.
    """
    band = item.get('band')
    if band == 'resume':
        percent = item.get('percent')
        if isinstance(percent, (int, float)) and not isinstance(percent, bool):
            return '%d%% watched' % int(percent)
        return ''
    if band == 'next_up':
        return item.get('next_label') or ''
    return ''


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
        """Start empty; `start()` supplies the bands and runs the modal."""
        super().__init__(*args, **kwargs)
        self.bands = []
        self.heading = ''
        self.labels = None
        self.selected = None

    def start(self, bands, heading='', labels=None):
        """doModal() with `bands` (a `[(band, [item, ...]), ...]` list, as
        `mystuff.group_by_band()` returns) filling one row each. Returns
        the item picked, or None if the user backed out without picking
        one.

        `labels` optionally overrides one or more rows' own header text
        (`{band: string_id}`), for a caller reusing an existing band's
        control ids for an unrelated single-band screen - see
        `NEW_EPISODES_BAND` and `lib.ui.homewindow._open_new_episodes()`,
        which needs the borrowed 'library' row to say "New episodes"
        rather than "Your library" for its own session."""
        self.bands = [(band, list(items)) for band, items in bands or [] if items]
        self.heading = heading or ''
        self.labels = labels or None
        self.selected = None
        if not self.bands:
            return None
        self.doModal()
        return self.selected

    def onInit(self):
        """Fill one row per band, label it, and focus the first populated
        one. Re-runs whenever `uicommon.ModalStackWindow` reopens a screen
        force-closed for playback, so every row is reset first."""
        from lib.ui.compat import L
        from lib.ui.mystuff import BAND_HEADINGS

        self.getControl(HEADING).setLabel(
            'RIVULET / %s' % ((self.heading or L(30241)).upper(),))

        # Every row is reset first, not just the ones being filled: onInit()
        # re-runs when uicommon.ModalStackWindow reopens a screen
        # force-closed for playback, and a row left populated from the
        # previous pass would both double its items and keep a band on
        # screen that the merge no longer produces.
        filled = dict(self.bands)
        # `self.labels` overrides BAND_HEADINGS only for THIS instance/
        # session (never mutates the shared dict), so a caller borrowing
        # a row's control ids (see `start()`'s docstring) never affects
        # any other GridWindow session's rendering of that same band.
        headings = dict(BAND_HEADINGS)
        if self.labels:
            headings.update(self.labels)
        for band, (list_id, label_id) in BAND_ROWS.items():
            control = self.getControl(list_id)
            control.reset()
            items = filled.get(band) or []
            if items:
                control.addItems([make_list_item(item) for item in items])
            self.getControl(label_id).setLabel(L(headings[band]).upper() if items else '')
            # Drives both the header's and the row's visibility. The
            # obvious condition - Integer.IsGreater(Container(id).NumItems,0)
            # - cannot be used: NumItems still reads 0 in this same pass
            # (it settles ~100ms later), so a row gated on it is invisible
            # exactly when setFocusId() runs below, Kodi will not focus an
            # invisible control, and a window with nothing focused never
            # redraws. This property is true the moment the row is filled.
            self.setProperty('band_%s' % band, '1' if items else '')

        self.setFocusId(BAND_ROWS[self.bands[0][0]][0])
        self._update_background()

    def _focused_item(self):
        """The merged item under the focused row's selection, or None.

        Resolves the focused CONTROL first, since the four band rows are
        separate lists and only one of them holds the cursor. Indexes that
        band's items by the control's own position: a row is never
        reordered or filtered after `onInit()`, so position and list index
        stay in lockstep."""
        try:
            focus_id = self.getFocusId()
        except Exception:
            return None
        for band, items in self.bands:
            list_id, _label_id = BAND_ROWS[band]
            if list_id != focus_id:
                continue
            try:
                position = self.getControl(list_id).getSelectedPosition()
            except Exception:
                return None
            if position is None or position < 0 or position >= len(items):
                return None
            return items[position]
        return None

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
        """Close on a back action; otherwise refresh the hero and fanart,
        since any other action may have moved the focus - including up/down
        between band rows, which the grouplist handles itself."""
        from lib.ui.uicommon import BACK_ACTIONS

        if action.getId() in BACK_ACTIONS:
            self.close()
            return
        # Any non-back action may have moved the focus - including up/down
        # between band rows, which the grouplist handles itself. Cheap (a
        # property read plus a setImage()), so it needs no settle timer.
        self._update_background()

    def onClick(self, control_id):
        """Record the picked title and close, so `start()` can return it.
        Clicks on anything but a band row are ignored."""
        if control_id not in {list_id for list_id, _label in BAND_ROWS.values()}:
            return
        item = self._focused_item()
        if item is None:
            return
        self.selected = item
        self.close()


def open_grid(bands, heading='', labels=None):
    """Build and run a GridWindow over `bands` (as
    `mystuff.group_by_band()` returns); returns the selected item dict, or
    None if the user closed it without picking one (or `bands` was
    empty). `labels` is forwarded to `GridWindow.start()` - see its
    docstring.

    The caller (`lib.ui.mystuff.open_my_stuff()`) wraps this in its own
    try/except and logs+notifies on failure, so an exception from
    `.start()` keeps propagating unchanged - this only guarantees the
    window is closed first (it may not have had a chance to self-close,
    e.g. if onInit() or a mid-modal callback raised)."""
    win = open_window(GridWindow, 'GridWindow.xml')
    try:
        return win.start(bands, heading=heading, labels=labels)
    finally:
        try:
            win.close()
        except Exception:
            pass
