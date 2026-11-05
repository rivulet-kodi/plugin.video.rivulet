"""Live-TV browse surfaces: guide catalogs never listed, channels as square cards.

stremio-core hides guide catalogs (a `date` extra on an `epgProvider` add-on)
from Discover; here the one filter is `iter_catalogs`, so Home rows, Arrange
lists, the picker and search all lose them at once. `card_shape` and the
skin's square layout decide how a channel is drawn.
"""
import contextlib
import re
from pathlib import Path

import pytest
from hypothesis import given

from lib import homelayout as h
from lib.stremio.addons import guide_catalog_for, iter_catalogs
from lib.ui.playbackmeta import card_shape
from tests.fuzz_strategies import json_values
from tests.kodistubs import install_kodi_stubs
from tests.test_home import _RELOAD_MODULE_NAMES


@pytest.fixture
def load_homewindow():
    with contextlib.ExitStack() as stack:
        def _load():
            ctx = stack.enter_context(install_kodi_stubs(reload=_RELOAD_MODULE_NAMES))
            ctx.homewindow = ctx.home
            return ctx

        yield _load


DATE = {'name': 'date', 'isRequired': False}


def _addon(catalogs, epg=True):
    manifest = {'id': 'iptv', 'name': 'IPTV', 'catalogs': catalogs}
    if epg:
        manifest['behaviorHints'] = {'epgProvider': True}
    return {'transportUrl': 'http://x/iptv/manifest.json', 'manifest': manifest}


def _channels(cid='iptv0'):
    return {'id': cid, 'type': 'tv', 'name': 'Channels',
            'extra': [{'name': 'search'}, {'name': 'skip'}]}


def _guide(cid='iptv0-guide', ctype='tv'):
    return {'id': cid, 'type': ctype, 'name': 'Guide', 'extra': [DATE, {'name': 'skip'}]}


def test_guide_catalogs_are_not_iterated_but_channels_are():
    addons = [_addon([_channels(), _guide()])]
    assert [c['id'] for _, _, c in iter_catalogs(addons)] == ['iptv0']
    assert [c['id'] for _, _, c in iter_catalogs(addons, extra_required='skip')] == ['iptv0']


def test_a_date_extra_without_epg_provider_stays_browsable():
    addons = [_addon([_guide('plain')], epg=False)]
    assert [c['id'] for _, _, c in iter_catalogs(addons)] == ['plain']


def test_home_never_offers_a_guide_catalog_and_a_saved_row_for_it_does_not_render():
    addons = [_addon([_channels(), _guide()])]
    catalogs = h.installed_catalogs(addons)
    ids = [c['list_id'] for c in catalogs]
    assert ids == ['cat:http://x/iptv/manifest.json|tv|iptv0']
    saved = {'version': 1, 'tabs': {tab: [] for tab in h.TABS}}
    saved['tabs']['live'] = ['cat:http://x/iptv/manifest.json|tv|iptv0-guide', ids[0]]
    rows = h.resolve(h.normalize(saved), 'live', catalogs)
    assert [row[0] for row in rows] == ids


def test_guide_catalog_for_prefers_the_named_guide_then_the_only_one():
    manifest = _addon([_channels('a'), _channels('b'), _guide('b-guide'), _guide('a-guide')])['manifest']
    assert guide_catalog_for(manifest, {'id': 'a', 'type': 'tv'})['id'] == 'a-guide'
    only = _addon([_channels('a'), _guide('whatever')])['manifest']
    assert guide_catalog_for(only, {'id': 'a', 'type': 'tv'})['id'] == 'whatever'


def test_guide_catalog_for_never_guesses_or_crosses_types():
    ambiguous = _addon([_channels('a'), _guide('g1'), _guide('g2')])['manifest']
    assert guide_catalog_for(ambiguous, {'id': 'a', 'type': 'tv'}) is None
    other_type = _addon([_channels('a'), _guide('a-guide', ctype='movie')])['manifest']
    assert guide_catalog_for(other_type, {'id': 'a', 'type': 'tv'}) is None
    no_epg = _addon([_channels('a'), _guide('a-guide')], epg=False)['manifest']
    assert guide_catalog_for(no_epg, {'id': 'a', 'type': 'tv'}) is None
    assert guide_catalog_for(None, {}) is None


@given(json_values, json_values)
def test_guide_catalog_for_never_raises(manifest, catalog):
    guide_catalog_for(manifest, catalog)


def test_card_shape_rule():
    assert card_shape({'posterShape': 'square'}) == 'square'
    assert card_shape({'type': 'tv'}) == 'square'
    assert card_shape({'type': 'channel', 'behaviorHints': {'isLive': True}}) == 'square'
    assert card_shape({'type': 'tv', 'posterShape': 'poster'}) == ''
    assert card_shape({'type': 'tv', 'posterShape': 'landscape'}) == ''
    assert card_shape({'type': 'movie'}) == ''
    assert card_shape({'type': 'movie', 'posterShape': 'poster'}) == ''
    assert card_shape(None) == ''


@given(json_values)
def test_card_shape_is_total_and_two_valued(value):
    assert card_shape(value) in ('', 'square')


def test_square_item_hero_uses_logo_over_real_or_default_fanart(load_homewindow):
    home = load_homewindow().homewindow
    props = home._poster_properties({'id': 'c1', 'type': 'tv', 'name': 'One', 'logo': 'logo.png'})
    assert props['shape'] == 'square' and props['live'] == '1'
    assert props['hero_fill'] == ''
    assert props['fanart'] == ''
    assert props['hero_logo'] == 'logo.png'
    assert props['thumbnail'] == 'logo.png'
    posterless = home._poster_properties(
        {'id': 'c2', 'type': 'tv', 'name': 'Two', 'poster': 'p.png', 'posterShape': 'square',
         'background': 'bg.jpg'})
    assert posterless['hero_logo'] == 'p.png'
    assert posterless['fanart'] == 'bg.jpg'
    assert posterless['hero_fill'] == ''


def test_poster_item_keeps_filled_hero_and_has_no_shape(load_homewindow):
    home = load_homewindow().homewindow
    props = home._poster_properties({'id': 'tt1', 'type': 'movie', 'name': 'M', 'poster': 'p.jpg'})
    assert props['shape'] == '' and props['live'] == ''
    assert props['hero_fill'] == '1'


_SKIN = Path(__file__).resolve().parent.parent / 'resources' / 'skins' / 'Default' / '1080i'


def test_every_poster_cell_layout_draws_both_shapes():
    """Each poster itemlayout/focusedlayout pairs its 2:3 art with a square
    copy gated on the `shape` property, so one is always visible."""
    plain = 'String.IsEmpty(ListItem.Property(shape))'
    square = '!String.IsEmpty(ListItem.Property(shape))'
    seen = 0
    for name in ('HomeWindow.xml', 'SearchWindow.xml', 'ShowcaseWindow.xml'):
        text = (_SKIN / name).read_text(encoding='utf-8')
        for layout in re.findall(r'<(?:item|focused)layout\b.*?</(?:item|focused)layout>', text, re.S):
            if 'mask_poster.png' not in layout or '<top>316</top>' not in layout:
                continue
            seen += 1
            # Every 2:3 art/plate/label control has a square twin.
            assert layout.count('<top>8</top><width>200</width><height>300</height>') \
                == layout.count('<top>58</top><width>200</width><height>200</height>') > 0
            assert layout.count('<top>316</top><width>200</width><height>300</height>') \
                == layout.count('<top>266</top><width>200</width><height>200</height>') > 0
            assert plain in layout and square in layout
            # The square copy never carries the IN CINEMA strip.
            assert layout.count('FF38BDF8') <= 1
            assert layout.count('look_ring.png') in (0, 2)
    assert seen >= 4
