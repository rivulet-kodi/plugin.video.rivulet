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
    assert ctx.gridwindow._badge(_item(ctx.mystuff.BAND_RESUME, percent=62.4)) == '62%'


def test_badge_names_the_next_up_and_watched_bands(ctx):
    assert ctx.gridwindow._badge(_item(ctx.mystuff.BAND_NEXT_UP)) == 'STR30242'
    assert ctx.gridwindow._badge(_item(ctx.mystuff.BAND_RECENT)) == 'STR30243'


def test_badge_is_empty_for_a_never_played_library_title(ctx):
    """The absence is the state: a grid where every cell carries a badge
    makes the badges worthless."""
    assert ctx.gridwindow._badge(_item(ctx.mystuff.BAND_LIBRARY)) == ''


# ---------------------------------------------------------------------------
# _caption() - the dim line under the focused title
# ---------------------------------------------------------------------------


def test_caption_appends_the_resolved_next_episode(ctx):
    item = _item(ctx.mystuff.BAND_NEXT_UP, next_label='S1E3 · The Great Game')

    assert ctx.gridwindow._caption(item) == 'STR30242 · S1E3 · The Great Game'


def test_caption_falls_back_to_the_badge_alone(ctx):
    assert ctx.gridwindow._caption(_item(ctx.mystuff.BAND_RESUME, percent=62.0)) == '62%'


def test_caption_is_empty_for_a_bare_library_title(ctx):
    assert ctx.gridwindow._caption(_item(ctx.mystuff.BAND_LIBRARY)) == ''


# ---------------------------------------------------------------------------
# make_list_item()
# ---------------------------------------------------------------------------


def test_make_list_item_sets_every_property_the_skin_draws(ctx):
    item = _item(ctx.mystuff.BAND_RESUME, percent=50.0, name='Dune', poster='p.jpg')

    list_item = ctx.gridwindow.make_list_item(item)

    assert list_item.getLabel() == 'Dune'
    assert list_item.getProperty('thumbnail') == 'p.jpg'
    assert list_item.getProperty('badge') == '50%'
    assert list_item.getProperty('progress_width') == str(ctx.gridwindow.PROGRESS_TRACK_WIDTH // 2)
    assert list_item.getProperty('caption') == '50%'


def test_make_list_item_tolerates_a_title_with_no_name_or_poster(ctx):
    list_item = ctx.gridwindow.make_list_item(_item(ctx.mystuff.BAND_LIBRARY, name=None, poster=None))

    assert list_item.getLabel() == ''
    assert list_item.getProperty('thumbnail') == ''


# ---------------------------------------------------------------------------
# GridWindow - onInit/onClick/onAction
# ---------------------------------------------------------------------------


def _window(ctx, items):
    win = ctx.gridwindow.GridWindow('GridWindow.xml', '/addon/path', 'Default', '1080i')
    win.items = list(items)
    win.heading = 'MY STUFF'
    win.selected = None
    return win


def test_oninit_populates_the_grid_and_focuses_it(ctx):
    win = _window(ctx, [_item(ctx.mystuff.BAND_RESUME, percent=50.0, name='A'),
                        _item(ctx.mystuff.BAND_LIBRARY, id='tt2', name='B')])

    win.onInit()

    control = win.getControl(ctx.gridwindow.LIST)
    assert [i.getLabel() for i in control.items] == ['A', 'B']
    assert win.getFocusId() == ctx.gridwindow.LIST
    assert win.getControl(ctx.gridwindow.HEADING).label == 'RIVULET / MY STUFF'


def test_oninit_resets_before_adding_so_a_playback_reopen_does_not_double_cells(ctx):
    """ModalStackWindow reopens a screen force-closed for playback, which
    re-runs onInit() on a list that still holds the old cells."""
    win = _window(ctx, [_item(ctx.mystuff.BAND_LIBRARY, name='A')])

    win.onInit()
    win.onInit()

    assert len(win.getControl(ctx.gridwindow.LIST).items) == 1


def test_onclick_selects_the_focused_item_and_closes(ctx):
    items = [_item(ctx.mystuff.BAND_RESUME, id='tt1', name='A'),
             _item(ctx.mystuff.BAND_LIBRARY, id='tt2', name='B')]
    win = _window(ctx, items)
    win.onInit()
    win.getControl(ctx.gridwindow.LIST).selected_index = 1

    win.onClick(ctx.gridwindow.LIST)

    assert win.selected['id'] == 'tt2'
    assert win.closed is True


def test_onclick_ignores_a_control_that_is_not_the_grid(ctx):
    win = _window(ctx, [_item(ctx.mystuff.BAND_LIBRARY)])
    win.onInit()

    win.onClick(ctx.gridwindow.HEADING)

    assert win.selected is None
    assert win.closed is False


def test_onaction_back_closes_without_selecting(ctx):
    win = _window(ctx, [_item(ctx.mystuff.BAND_LIBRARY)])
    win.onInit()

    import xbmcgui
    win.onAction(xbmcgui.Action(10))  # PreviousMenu/Esc

    assert win.closed is True
    assert win.selected is None


def test_onaction_updates_the_background_to_the_focused_item(ctx):
    items = [_item(ctx.mystuff.BAND_RESUME, id='tt1', background='one.jpg'),
             _item(ctx.mystuff.BAND_LIBRARY, id='tt2', background='two.jpg')]
    win = _window(ctx, items)
    win.onInit()
    win.getControl(ctx.gridwindow.LIST).selected_index = 1

    import xbmcgui
    win.onAction(xbmcgui.Action(4))  # a plain move, not a back action

    assert win.getControl(ctx.gridwindow.BACKGROUND).image == 'two.jpg'


def test_start_returns_none_for_an_empty_grid_without_opening_it(ctx):
    """An empty merged screen must never open a blank window - open_my_stuff()
    notifies instead."""
    win = ctx.gridwindow.GridWindow('GridWindow.xml', '/addon/path', 'Default', '1080i')

    assert win.start([]) is None
    assert win.modal_calls == 0
