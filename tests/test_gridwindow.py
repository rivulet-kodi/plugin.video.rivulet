"""Tests for lib.ui.gridwindow: the merged "My Stuff" poster grid,
exercised against the shared fake xbmc/xbmcgui stubs in tests/kodistubs
(no real Kodi runtime, no network).

What IS covered here is the pure projection from a merged
`lib.ui.mystuff` item onto the properties `GridWindow.xml` draws
(`make_list_item`, `_badge`, `_progress_width`, `_caption`) plus
GridWindow's own selection/back/background behaviour, driven by calling
onInit()/onClick()/onAction() directly - the fake WindowXML.doModal() is
a no-op counter, exactly as tests/test_homewindow.py drives HomeWindow.

What CANNOT be covered here is GridWindow.xml's actual rendering: the
`panel` control's wrapping, the 6-column geometry, the focused cell's
accent outline, and whether the progress bar's computed width lands on
the 240px track are all Kodi-skin-engine-only. A real device must
confirm those - see the repo's device-verification notes.
"""
import contextlib

import pytest

from tests.kodistubs import install_kodi_stubs

_RELOAD_MODULE_NAMES = (
    'lib.ui.compat', 'lib.ui.dependencies', 'lib.ui.uicommon', 'lib.ui.router',
    'lib.ui.binge', 'lib.ui.mystuff', 'lib.ui.gridwindow',
)


@pytest.fixture
def ctx():
    """Fresh stubs with lib.ui.gridwindow (and the lib.ui.mystuff whose
    band constants it reads) reloaded against them; returns the namespace
    install_kodi_stubs yields, so tests reach `.gridwindow`/`.mystuff`."""
    with contextlib.ExitStack() as stack:
        yield stack.enter_context(install_kodi_stubs(reload=_RELOAD_MODULE_NAMES))


def _item(band, **overrides):
    base = {
        'type': 'movie', 'id': 'tt1', 'band': band, 'percent': None,
        'video_id': None, 'updated_at': None,
        'name': 'A Title', 'poster': 'poster.jpg', 'background': 'bg.jpg',
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# _progress_width() - only a part-watched title draws a bar
# ---------------------------------------------------------------------------


def test_progress_width_scales_percent_onto_the_track(ctx):
    gw = ctx.gridwindow
    item = _item(ctx.mystuff.BAND_RESUME, percent=50.0)

    assert gw._progress_width(item) == str(gw.PROGRESS_TRACK_WIDTH // 2)


def test_progress_width_floors_a_barely_started_title_to_a_visible_sliver(ctx):
    """A 1%-watched title scales to ~2px, which renders as nothing at all -
    the one state the bar exists to distinguish from "not started"."""
    gw = ctx.gridwindow

    assert gw._progress_width(_item(ctx.mystuff.BAND_RESUME, percent=1.0)) == '4'


def test_progress_width_clamps_a_corrupt_over_100_percent_to_the_track(ctx):
    gw = ctx.gridwindow

    assert gw._progress_width(_item(ctx.mystuff.BAND_RESUME, percent=250.0)) == str(gw.PROGRESS_TRACK_WIDTH)


@pytest.mark.parametrize('band_attr', ['BAND_NEXT_UP', 'BAND_RECENT', 'BAND_LIBRARY'])
def test_progress_width_is_empty_outside_the_resume_band(ctx, band_attr):
    """The skin keys the whole bar's visibility off this being empty, so
    a next-up/watched/library cell draws no track at all."""
    gw = ctx.gridwindow
    band = getattr(ctx.mystuff, band_attr)

    assert gw._progress_width(_item(band, percent=99.0)) == ''


def test_progress_width_is_empty_when_percent_is_not_a_number(ctx):
    gw = ctx.gridwindow

    assert gw._progress_width(_item(ctx.mystuff.BAND_RESUME, percent=None)) == ''
    assert gw._progress_width(_item(ctx.mystuff.BAND_RESUME, percent=True)) == ''


# ---------------------------------------------------------------------------
# _badge() - why this title is on the screen
# ---------------------------------------------------------------------------


def test_badge_shows_percent_for_a_resuming_title(ctx):
    assert ctx.gridwindow._badge(_item(ctx.mystuff.BAND_RESUME, percent=62.4)) == (
        '[COLOR FF38BDF8]62%[/COLOR]')


def test_badge_names_the_next_up_and_watched_bands(ctx):
    assert ctx.gridwindow._badge(_item(ctx.mystuff.BAND_NEXT_UP)) == (
        '[COLOR FFFBBF24]STR30242[/COLOR]')
    assert ctx.gridwindow._badge(_item(ctx.mystuff.BAND_RECENT)) == (
        '[COLOR 80EEF3F6]STR30243[/COLOR]')


def test_badge_prefers_the_episode_over_the_band_name(ctx):
    """"S1E3" says more than "NEXT UP", in less room."""
    item = _item(ctx.mystuff.BAND_NEXT_UP, next_label='S1E3')

    assert ctx.gridwindow._badge(item) == '[COLOR FFFBBF24]S1E3[/COLOR]'


def test_each_band_badge_is_a_distinct_colour(ctx):
    """The colour IS the distinction between bands on a flat grid - two
    bands sharing one would put the screen back where it started."""
    colours = ctx.gridwindow._BAND_COLOURS
    assert len(set(colours.values())) == len(colours)
    for band in (ctx.mystuff.BAND_RESUME, ctx.mystuff.BAND_NEXT_UP, ctx.mystuff.BAND_RECENT):
        assert band in colours


def test_badge_is_empty_for_a_never_played_library_title(ctx):
    """The absence is the state: a grid where every cell carries a badge
    makes the badges worthless."""
    assert ctx.gridwindow._badge(_item(ctx.mystuff.BAND_LIBRARY)) == ''


# ---------------------------------------------------------------------------
# _caption() - the dim line under the focused title
# ---------------------------------------------------------------------------


def test_caption_appends_the_resolved_next_episode(ctx):
    item = _item(ctx.mystuff.BAND_NEXT_UP, next_label='S1E3')

    assert ctx.gridwindow._caption(item) == 'STR30242 · S1E3'


def test_caption_names_the_band_and_the_percent(ctx):
    """The caption has room for the band's full name, which is what says
    WHY the title is on this screen - the badge is the same fact
    compressed to a colour and a few characters."""
    assert ctx.gridwindow._caption(_item(ctx.mystuff.BAND_RESUME, percent=62.0)) == (
        'STR30245 · 62%')


def test_caption_names_the_band_for_a_library_title(ctx):
    """Even a never-played title says why it is here - it is saved."""
    assert ctx.gridwindow._caption(_item(ctx.mystuff.BAND_LIBRARY)) == 'STR30247'


# ---------------------------------------------------------------------------
# make_list_item()
# ---------------------------------------------------------------------------


def test_make_list_item_sets_every_property_the_skin_draws(ctx):
    item = _item(ctx.mystuff.BAND_RESUME, percent=50.0, name='Dune', poster='p.jpg')

    list_item = ctx.gridwindow.make_list_item(item)

    assert list_item.getLabel() == 'Dune'
    assert list_item.getProperty('thumbnail') == 'p.jpg'
    assert list_item.getProperty('badge') == '[COLOR FF38BDF8]50%[/COLOR]'
    assert list_item.getProperty('progress_width') == str(ctx.gridwindow.PROGRESS_TRACK_WIDTH // 2)
    assert list_item.getProperty('caption') == 'STR30245 · 50%'
    assert list_item.getProperty('watched') == ''


def test_make_list_item_marks_a_watched_title_for_dimming(ctx):
    watched = ctx.gridwindow.make_list_item(_item(ctx.mystuff.BAND_RECENT))
    resuming = ctx.gridwindow.make_list_item(_item(ctx.mystuff.BAND_RESUME, percent=50.0))

    assert watched.getProperty('watched') == '1'
    assert resuming.getProperty('watched') == ''


def test_make_list_item_tolerates_a_title_with_no_name_or_poster(ctx):
    list_item = ctx.gridwindow.make_list_item(_item(ctx.mystuff.BAND_LIBRARY, name=None, poster=None))

    assert list_item.getLabel() == ''
    assert list_item.getProperty('thumbnail') == ''


# ---------------------------------------------------------------------------
# GridWindow - onInit/onClick/onAction
# ---------------------------------------------------------------------------


def _window(ctx, items=None, bands=None):
    """A GridWindow loaded with `bands` (as `mystuff.group_by_band()`
    returns), or with `items` as a single resume band for the tests that
    do not care which band they are in. Focus starts on the first band's
    row, as onInit() leaves it."""
    win = ctx.gridwindow.GridWindow('GridWindow.xml', '/addon/path', 'Default', '1080i')
    if bands is None:
        bands = [(ctx.mystuff.BAND_RESUME, list(items or []))]
    win.bands = [(band, list(members)) for band, members in bands if members]
    win.heading = 'CONTINUE'
    win.selected = None
    return win


def _row(ctx, band):
    """The (list id, header label id) pair the skin gives `band`."""
    return ctx.gridwindow.BAND_ROWS[band]


def test_oninit_fills_each_band_row_and_labels_it(ctx):
    win = _window(ctx, bands=[
        (ctx.mystuff.BAND_RESUME, [_item(ctx.mystuff.BAND_RESUME, id='r', name='R', percent=50.0)]),
        (ctx.mystuff.BAND_RECENT, [_item(ctx.mystuff.BAND_RECENT, id='w', name='W')]),
    ])

    win.onInit()

    resume_list, resume_label = _row(ctx, ctx.mystuff.BAND_RESUME)
    recent_list, recent_label = _row(ctx, ctx.mystuff.BAND_RECENT)
    assert [i.getLabel() for i in win.getControl(resume_list).items] == ['R']
    assert [i.getLabel() for i in win.getControl(recent_list).items] == ['W']
    assert win.getControl(resume_label).label == 'STR30245'
    assert win.getControl(recent_label).label == 'STR30246'
    assert win.getControl(ctx.gridwindow.HEADING).label == 'RIVULET / CONTINUE'


def test_oninit_leaves_an_absent_band_empty_and_unlabelled(ctx):
    """The skin hides a row on its own NumItems, so an empty band must
    contribute no items AND no heading - a stray "NEXT UP" over nothing
    is exactly what the per-band rows exist to avoid."""
    win = _window(ctx, items=[_item(ctx.mystuff.BAND_RESUME, name='R', percent=50.0)])

    win.onInit()

    for band in (ctx.mystuff.BAND_NEXT_UP, ctx.mystuff.BAND_RECENT, ctx.mystuff.BAND_LIBRARY):
        list_id, label_id = _row(ctx, band)
        assert win.getControl(list_id).items == []
        assert win.getControl(label_id).label == ''


def test_oninit_focuses_the_first_band_that_has_items(ctx):
    win = _window(ctx, bands=[
        (ctx.mystuff.BAND_RECENT, [_item(ctx.mystuff.BAND_RECENT, name='W')]),
        (ctx.mystuff.BAND_LIBRARY, [_item(ctx.mystuff.BAND_LIBRARY, name='L')]),
    ])

    win.onInit()

    assert win.getFocusId() == _row(ctx, ctx.mystuff.BAND_RECENT)[0]


def test_oninit_resets_every_row_so_a_playback_reopen_does_not_double_cells(ctx):
    """ModalStackWindow reopens a screen force-closed for playback, which
    re-runs onInit() over rows that still hold the previous pass's items."""
    win = _window(ctx, items=[_item(ctx.mystuff.BAND_RESUME, name='A')])

    win.onInit()
    win.onInit()

    assert len(win.getControl(_row(ctx, ctx.mystuff.BAND_RESUME)[0]).items) == 1


def test_onclick_selects_from_the_focused_band_row(ctx):
    """Two rows hold different titles at the same position, so the click
    must read the FOCUSED row - not just position 0 of the first band."""
    win = _window(ctx, bands=[
        (ctx.mystuff.BAND_RESUME, [_item(ctx.mystuff.BAND_RESUME, id='r1', name='R1')]),
        (ctx.mystuff.BAND_LIBRARY, [
            _item(ctx.mystuff.BAND_LIBRARY, id='l1', name='L1'),
            _item(ctx.mystuff.BAND_LIBRARY, id='l2', name='L2'),
        ]),
    ])
    win.onInit()
    library_list, _label = _row(ctx, ctx.mystuff.BAND_LIBRARY)
    win.setFocusId(library_list)
    win.getControl(library_list).selected_index = 1

    win.onClick(library_list)

    assert win.selected['id'] == 'l2'
    assert win.closed is True


def test_onclick_ignores_a_control_that_is_not_a_band_row(ctx):
    win = _window(ctx, items=[_item(ctx.mystuff.BAND_RESUME, name='A')])
    win.onInit()

    win.onClick(ctx.gridwindow.HEADING)

    assert win.selected is None
    assert win.closed is False


def test_onaction_back_closes_without_selecting(ctx):
    import xbmcgui
    win = _window(ctx, items=[_item(ctx.mystuff.BAND_RESUME, name='A')])
    win.onInit()

    win.onAction(xbmcgui.Action(10))  # PreviousMenu/Esc

    assert win.closed is True
    assert win.selected is None


def test_onaction_updates_the_background_from_the_focused_row(ctx):
    import xbmcgui
    win = _window(ctx, bands=[
        (ctx.mystuff.BAND_RESUME, [_item(ctx.mystuff.BAND_RESUME, id='r', background='one.jpg')]),
        (ctx.mystuff.BAND_LIBRARY, [_item(ctx.mystuff.BAND_LIBRARY, id='l', background='two.jpg')]),
    ])
    win.onInit()
    win.setFocusId(_row(ctx, ctx.mystuff.BAND_LIBRARY)[0])

    win.onAction(xbmcgui.Action(4))  # a plain move, not a back action

    assert win.getControl(ctx.gridwindow.BACKGROUND).image == 'two.jpg'


def test_start_returns_none_for_an_empty_screen_without_opening_it(ctx):
    """A merge that produced nothing must never open a blank window -
    open_my_stuff() notifies instead."""
    win = ctx.gridwindow.GridWindow('GridWindow.xml', '/addon/path', 'Default', '1080i')

    assert win.start([]) is None
    assert win.modal_calls == 0


def test_start_drops_bands_that_have_no_items(ctx):
    win = ctx.gridwindow.GridWindow('GridWindow.xml', '/addon/path', 'Default', '1080i')

    assert win.start([(ctx.mystuff.BAND_RESUME, []), (ctx.mystuff.BAND_LIBRARY, [])]) is None
    assert win.bands == []
    assert win.modal_calls == 0


# ---------------------------------------------------------------------------
# Skin/Python agreement
# ---------------------------------------------------------------------------


def _grid_xml():
    """GridWindow.xml's source, for the geometry invariants the Python and
    the skin have to agree on."""
    import os
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(repo_root, 'resources', 'skins', 'Default', '1080i', 'GridWindow.xml')
    with open(path, encoding='utf-8') as handle:
        return handle.read()


def test_progress_track_sits_on_the_poster_edges(ctx):
    """`_progress_width()` scales a percent into PROGRESS_TRACK_WIDTH and
    the skin draws the result as a literal pixel width, so the two must
    agree - AND the track must sit on the poster's own box, or the bar
    overhangs the artwork (which it visibly did at a 240 track over a
    216-wide rendered poster)."""
    import re

    xml = _grid_xml()
    tracks = re.findall(
        r'<control type="image"><top>\d+</top><left>(\d+)</left><width>(\d+)</width>'
        r'<height>6</height><texture colordiffuse="59FFFFFF">',
        xml,
    )
    # Two per band row (item + focused layout), one row per band. The skin
    # has no Includes.xml to share a cell layout through, so the copies are
    # generated - this asserts they really are all the same.
    expected = 2 * len(ctx.gridwindow.BAND_ROWS)
    assert len(tracks) == expected, (
        'expected %d progress tracks (2 per band row), found %d' % (expected, len(tracks)))
    assert len(set(tracks)) == 1, 'band rows disagree on the progress track: %s' % sorted(set(tracks))
    for _left, width in tracks:
        assert int(width) == ctx.gridwindow.PROGRESS_TRACK_WIDTH

    posters = re.findall(
        r'<top>0</top><left>(\d+)</left><width>(\d+)</width><height>\d+</height>\s*'
        r'<texture[^>]*background="true">\$INFO\[ListItem.Property\(thumbnail\)\]</texture>',
        xml,
    )
    assert posters, 'no poster box found'
    # There are more poster controls than tracks (the item layout draws a
    # normal and a dimmed-watched variant, the focused layout one more),
    # so compare the set of EDGES they occupy rather than pairing them up.
    assert set(posters) == set(tracks), (
        'poster boxes %s and progress tracks %s do not share the same edges'
        % (sorted(set(posters)), sorted(set(tracks))))


def test_poster_box_is_a_true_two_thirds_aspect():
    """The poster is drawn `aspectratio=keep`, so a box that is not 2:3
    renders the artwork narrower than its box - exactly what put the
    progress bar and the labels out of line with the visible poster."""
    import re

    boxes = re.findall(
        r'<left>\d+</left><width>(\d+)</width><height>(\d+)</height>\s*'
        r'<texture[^>]*background="true">\$INFO\[ListItem.Property\(thumbnail\)\]</texture>',
        _grid_xml(),
    )
    assert boxes, 'no poster box found'
    for width, height in boxes:
        assert int(width) / int(height) == pytest.approx(2 / 3, abs=0.001), (
            'poster box %sx%s is not 2:3' % (width, height))


def test_panel_height_is_a_whole_number_of_rows():
    """A panel sized to a fraction of a row draws the next row's posters
    with their labels cut off by the panel edge, which reads as a
    rendering fault rather than as "scroll for more" - the exact bug a
    468px cell in an 820px panel produced on a real device."""
    import re

    block = _grid_xml()
    block = block[block.index('<control type="panel" id="30002">'):]
    panel = re.search(
        r'<left>\d+</left><top>\d+</top><width>\d+</width><height>(\d+)</height>', block)
    assert panel, 'panel 30002 geometry not found'
    cell = re.search(r'<itemlayout width="\d+" height="(\d+)">', block)
    assert cell, 'panel itemlayout not found'

    panel_h, cell_h = int(panel.group(1)), int(cell.group(1))
    assert panel_h % cell_h == 0, (
        'panel %d is not a whole number of %dpx rows (%.2f)' % (panel_h, cell_h, panel_h / cell_h))


def test_panel_fits_inside_the_frame():
    """Nothing below the fold: the panel must end inside the 1080 frame,
    or its last row is drawn off-screen."""
    import re

    block = _grid_xml()
    block = block[block.index('<control type="panel" id="30002">'):]
    geom = re.search(
        r'<left>\d+</left><top>(\d+)</top><width>\d+</width><height>(\d+)</height>', block)
    assert geom, 'panel 30002 geometry not found'
    assert int(geom.group(1)) + int(geom.group(2)) <= 1080


def test_band_rows_match_the_control_ids_the_skin_declares(ctx):
    """`BAND_ROWS` is the contract between this module and the skin: the
    Python fills these controls by id and the skin hard-codes the same ids
    in each row's `visible` condition, so a typo either side would leave a
    band silently undrawn (and `getControl` raising on a real device)."""
    import re

    xml = _grid_xml()
    declared = {int(i) for i in re.findall(r'<control type="(?:panel|label)" id="(\d+)"', xml)}
    for band, (list_id, label_id) in ctx.gridwindow.BAND_ROWS.items():
        assert list_id in declared, '%s: list %d not declared in the skin' % (band, list_id)
        assert label_id in declared, '%s: header %d not declared in the skin' % (band, label_id)


def test_every_band_has_a_row(ctx):
    """A band with no row would be merged, ordered, and then never drawn."""
    for band in ctx.mystuff.BAND_ORDER:
        assert band in ctx.gridwindow.BAND_ROWS


def test_each_band_row_hides_itself_when_empty(ctx):
    """Both the poster row AND its header are gated on the list's own
    NumItems, so an empty band collapses instead of leaving a heading over
    nothing - the whole reason the rows are separate controls."""
    import re

    xml = _grid_xml()
    for band, (list_id, label_id) in ctx.gridwindow.BAND_ROWS.items():
        condition = 'Integer.IsGreater(Container(%d).NumItems,0)' % list_id
        # Once for the header label, once for the panel.
        assert xml.count('<visible>%s</visible>' % condition) == 2, (
            '%s: expected both its header and its panel gated on %s' % (band, condition))
        assert re.search(r'<control type="label" id="%d"' % label_id, xml)
