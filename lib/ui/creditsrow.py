# SPDX-FileCopyrightText: 2026 M0Rf30
# SPDX-License-Identifier: MIT

"""The row of clickable Genres / Cast / Directors / Writers chips that
`lib.ui.detailwindow.DetailWindow` (a series page) and
`lib.ui.streamswindow.StreamsWindow` (the page a movie opens straight
onto) both show, as Stremio's own title page does.

One module for both windows because the row is the same control in both
skins (`CREDITS`, a horizontal list whose items carry the name as the
label and the category as the dim `category` property) and the same
behaviour: a click hands the link to
`lib.ui.infowindow.open_credit_link()`, the dispatch the "Cast & crew"
picker also uses, so a person opens Search results, a genre opens its
catalog and both keep the installed-addon check.

The row is built from the window's own meta on the GUI thread
(`fill_credits()`) - a ListItem is never written from a worker. A meta
without usable links hides the control, which also makes it unfocusable,
so a remote never lands on an empty row.
"""
from lib.stremio.metalinks import iter_link_groups
from lib.stremio.streaminfo import escape_label

#: The credits list's id in both `DetailWindow.xml` and
#: `StreamsWindow.xml`.
CREDITS = 30030

#: Stremio's own order. Matched casefolded and in singular too: real
#: addons capitalise and pluralise their category names inconsistently
#: (see `lib.stremio.metalinks.iter_link_groups()`). Any other category
#: (an addon's own, e.g. a collection) follows in the order it arrived.
_CATEGORY_RANK = {
    'genres': 0, 'genre': 0,
    'cast': 1,
    'directors': 2, 'director': 2,
    'writers': 3, 'writer': 3,
}
_OTHER_RANK = max(_CATEGORY_RANK.values()) + 1

#: Kodi's own strings for the categories it has words for, so the caption
#: follows the UI language; addons send them in English. Singular for
#: directors and writers: Kodi has no plural. Any other category is
#: shown as the addon wrote it.
_CATEGORY_STRING = {0: 135, 1: 206, 2: 20339, 3: 20417}


def credit_links(meta):
    """`[(name, category, parsed_link), ...]` for the chips, in display
    order, or `[]` when `meta` has nothing usable."""
    groups = sorted(
        iter_link_groups(meta),
        key=lambda group: _CATEGORY_RANK.get(group[0].casefold(), _OTHER_RANK),
    )
    return [
        (name, category, parsed)
        for category, members in groups
        for name, parsed in members
    ]


def fill_credits(window, meta):
    """Populate `window`'s `CREDITS` list from `meta` and return the
    `(name, parsed_link)` pairs by list position (what `open_credit()`
    takes; the name heads the results coverflow). Hides the
    control when there are none."""
    # Imported here, not at module scope, so the module holds no xbmcgui
    # binding of its own: the windows importing it are what the test
    # fixtures reload against fresh fakes.
    import xbmcgui

    control = window.getControl(CREDITS)
    control.reset()
    credits = credit_links(meta)
    if not credits:
        control.setVisible(False)
        return []
    import xbmc

    items = []
    previous = None
    for name, category, _parsed in credits:
        item = xbmcgui.ListItem(escape_label(name))
        # The caption heads its group only: over every chip it read as
        # "Genres Genres Cast Cast Cast".
        rank = _CATEGORY_RANK.get(category.casefold(), _OTHER_RANK)
        key = rank if rank != _OTHER_RANK else category.casefold()
        if key != previous:
            string_id = _CATEGORY_STRING.get(rank)
            item.setProperty('category', xbmc.getLocalizedString(string_id) if string_id else escape_label(category))
        previous = key
        items.append(item)
    control.addItems(items)
    control.setVisible(True)
    return [(name, parsed) for name, _category, parsed in credits]


def open_credit(window, links):
    """Run the chip `window`'s `CREDITS` list has selected through
    `open_credit_link()`. A position outside `links` (an empty row) does
    nothing.

    Focus goes back to the chip afterwards: the result windows it opens are
    modals over this one, and on a real device the window under them came
    back with no control focused (arrow keys did nothing until Back)."""
    position = window.getControl(CREDITS).getSelectedPosition()
    if not 0 <= position < len(links):
        return
    # A second OK queued while the search spinner pumps input would
    # otherwise stack a second results screen over the first.
    if getattr(window, '_credit_open', False):
        return
    from lib.ui.dependencies import get_client, get_store
    from lib.ui.infowindow import open_credit_link

    window._credit_open = True
    try:
        name, parsed = links[position]
        open_credit_link(get_store(), get_client(), parsed, name)
    finally:
        window._credit_open = False
    window.setFocusId(CREDITS)
