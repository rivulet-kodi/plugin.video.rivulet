"""Audience data, source pictograms and the Home hero's exact score format."""
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from lib.stremio.ratings import (
    RatingsCache,
    _valid_entry,
    fetch_scores,
    panel_facts,
    parse_mdblist,
    parse_omdb,
)
from lib.ui.newlook import score_icon
from lib.ui.playbackmeta import hero_panel_text, new_look_hero_parts
from tests.test_newlook import load_homewindow as load_homewindow
from tests.test_ratings import _get, _Response

ROOT = Path(__file__).resolve().parents[1]
MEDIA = ROOT / 'resources/skins/Default/media'
SKIN = ROOT / 'resources/skins/Default/1080i/NewHomeWindow.xml'
ICONS = (
    'rating_rt_fresh.png', 'rating_rt_rotten.png',
    'rating_rt_audience_up.png', 'rating_rt_audience_spilled.png',
    'rating_mc_green.png', 'rating_mc_yellow.png', 'rating_mc_red.png',
)
SAMPLE = {'ratings': [
    {'source': 'tomatoes', 'value': 90},
    {'source': 'popcorn', 'value': 97},
    {'source': 'metacritic', 'value': 66},
    {'source': 'metacriticuser', 'value': 7.4},
    {'source': 'imdb', 'value': 8},
]}


def test_mdblist_audience_is_exact_and_never_the_tomatometer():
    assert parse_mdblist(SAMPLE) == {
        'rottenTomatoes': '90%', 'rtAudience': '97%', 'metascore': '66', 'imdbRating': '8.0',
    }
    assert parse_mdblist({'ratings': [
        {'source': 'metacriticuser', 'value': 7.4}, {'source': 'popcorn', 'value': 97},
    ]}) == {'rtAudience': '97%'}
    assert parse_mdblist({'ratings': [
        {'source': 'popcornuser', 'value': 99}, {'source': 'tomatoesaudienceextra', 'value': 99},
    ]}) == {}


@pytest.mark.parametrize('source', ['popcorn', 'tomatoesaudience', 'audience'])
def test_older_audience_sources_are_supported_exactly(source):
    assert parse_mdblist({'ratings': [{'source': source, 'value': 92}]}) == {'rtAudience': '92%'}


def test_omdb_only_makes_one_request_and_has_no_audience():
    payload = {'imdbRating': '8.3', 'Metascore': '67', 'tomatoUserMeter': 'N/A',
               'Ratings': [{'Source': 'Rotten Tomatoes', 'Value': '98%'}]}
    assert 'rtAudience' not in parse_omdb(payload)
    get = _get([_Response(200, payload)])
    scores = fetch_scores('tt1234567', omdb_key='test-key', get=get)
    assert len(get.calls) == 1
    assert scores == {'imdbRating': '8.3', 'metascore': '67', 'rottenTomatoes': '98%'}


def test_both_keys_fill_audience_even_after_complete_omdb_reply():
    get = _get([
        _Response(200, {'imdbRating': '8.3', 'Metascore': '67',
                        'Ratings': [{'Source': 'Rotten Tomatoes', 'Value': '98%'}]}),
        _Response(200, SAMPLE),
    ])
    assert fetch_scores('tt1234567', omdb_key='test-key', mdblist_key='other-key', get=get) == {
        'imdbRating': '8.3', 'metascore': '67', 'rottenTomatoes': '98%', 'rtAudience': '97%',
    }
    assert len(get.calls) == 2
    get = _get([_Response(200, SAMPLE)])
    assert fetch_scores('tt1234567', mdblist_key='test-key', get=get) == parse_mdblist(SAMPLE)
    assert len(get.calls) == 1


@pytest.mark.parametrize('keys', [('', ''), ('omdb-key', 'mdb-key')])
def test_cache_schema_refetches_old_entries_and_round_trips_audience(tmp_path, keys):
    old_token = hashlib.sha256(json.dumps(keys).encode()).hexdigest() if any(keys) else ''
    old_key = 'tt1234567\n' + old_token if old_token else 'tt1234567'
    entry = {'at': 1, 'scores': {'imdbRating': '8.3', 'rottenTomatoes': '98%', 'metascore': '67'}}
    assert _valid_entry(entry)
    path = tmp_path / 'ratings.json'
    path.write_text(json.dumps({old_key: entry}))
    cache = RatingsCache(str(path), keys=keys, clock=lambda: 2)
    assert cache.lookup('tt1234567') is None
    assert old_key in cache._entries  # Readable for eviction, not reused.
    scores = dict(entry['scores'], rtAudience='92%')
    cache.store('tt1234567', scores)
    assert RatingsCache(str(path), keys=keys, clock=lambda: 2).lookup('tt1234567') == scores


@pytest.mark.parametrize('key', ['rtAudience', 'audienceScore', 'popcornmeter'])
def test_catalog_audience_keys_survive_panel_facts_and_do_not_become_rt(key):
    facts = panel_facts({'id': 'tt1234567', key: '97%'})
    assert facts[key] == '97%'
    assert new_look_hero_parts(hero_panel_text(facts))[2] == [('RT Audience', '97%')]


def test_audience_label_is_matched_before_the_rt_prefix_in_display_order():
    panel = 'Rotten Tomatoes Audience 97%\nRotten Tomatoes 90%\nMetascore 66\nIMDb 8.0'
    assert new_look_hero_parts(panel)[2] == [
        ('IMDb', '8.0'), ('RT Audience', '97%'), ('RT', '90%'), ('MC', '66'),
    ]
    panel = hero_panel_text(SAMPLE)
    assert new_look_hero_parts(panel)[2] == [
        ('IMDb', '8'), ('RT Audience', '97'), ('RT', '90'), ('MC', '66'),
    ]
    assert new_look_hero_parts(hero_panel_text({'ratings': [
        {'source': 'tomatoesaudience', 'value': 97},
    ]}))[2] == [('RT Audience', '97')]


@pytest.mark.parametrize('kind,value,texture', [
    ('RT', '59%', 'rating_rt_rotten.png'), ('RT', '60%', 'rating_rt_fresh.png'),
    ('RT Audience', '59%', 'rating_rt_audience_spilled.png'),
    ('RT Audience', '60%', 'rating_rt_audience_up.png'),
    ('MC', '39', 'rating_mc_red.png'), ('MC', '40', 'rating_mc_yellow.png'),
    ('MC', '60', 'rating_mc_yellow.png'), ('MC', '61', 'rating_mc_green.png'),
    ('MC', 'not numeric', ''), ('RT', 'nan', ''), ('RT', 'inf', ''),
    ('RT Audience', '-1', ''), ('RT', '101', ''), ('unknown', '60', ''),
])
def test_score_icon_bands(kind, value, texture):
    assert score_icon(kind, value) == texture


def test_paint_formats_values_and_removes_absent_pairs_and_their_icons(load_homewindow):
    ctx = load_homewindow()
    look = ctx.newlook
    win = ctx.newhome.NewHomeWindow('NewHomeWindow.xml', '/addon/path', 'Default', '1080i')
    panel = 'IMDb 8.3\nRotten Tomatoes Audience 92%\nRotten Tomatoes 98%\nMetascore 67'
    look.paint_hero(win, '', panel, '')
    assert win.getControl(30029).label == '[COLOR B3EDEDED]IMDb[/COLOR] 8.3'
    for image_id, label_id, text, texture in [
        (30030, 30031, ' : 92%', 'rating_rt_audience_up.png'),
        (30032, 30033, ' : 98%', 'rating_rt_fresh.png'),
        (30034, 30035, ' : 67', 'rating_mc_green.png'),
    ]:
        assert win.getControl(label_id).label == text
        assert re.fullmatch(r' : \d{1,3}' + ('%' if label_id != 30035 else ''), text)
        assert win.getControl(image_id).image == texture and win.getControl(image_id).visible
    look.paint_hero(win, '', 'IMDb 8.3\nRotten Tomatoes 98%\nMetascore 67', '')
    assert win.getControl(30030).visible is False and win.getControl(30031).visible is False
    assert win.getControl(30031).label == '' and win.getControl(30030).image == ''
    look.paint_hero(win, '', '', '')
    assert win.getControl(30025).visible is False
    assert all(not win.getControl(i).visible for i in range(30029, 30037))


@pytest.mark.parametrize('panel,image_id,label_id,text', [
    ('Rotten Tomatoes unusual', 30032, 30033, 'RT unusual'),
    ('Rotten Tomatoes Audience unusual', 30030, 30031, 'RT Audience unusual'),
    ('Metascore unusual', 30034, 30035, 'MC unusual'),
])
def test_non_numeric_scores_fall_back_to_text_with_no_dangling_colon(
        load_homewindow, panel, image_id, label_id, text):
    ctx = load_homewindow()
    win = ctx.newhome.NewHomeWindow('NewHomeWindow.xml', '/addon/path', 'Default', '1080i')
    ctx.newlook.paint_hero(win, '', panel, '')
    assert win.getControl(image_id).visible is False
    assert win.getControl(label_id).label == text and win.getControl(label_id).visible
    assert win.getControl(30025).visible


def test_all_scores_and_the_end_time_fit_the_1400_pixel_row(load_homewindow):
    from lib.ui.heroscrim import text_width
    ctx = load_homewindow()
    panel = 'IMDb 10.0\nRotten Tomatoes Audience 100%\nRotten Tomatoes 100%\nMetascore 100'
    win = ctx.newhome.NewHomeWindow('NewHomeWindow.xml', '/addon/path', 'Default', '1080i')
    ctx.newlook.paint_hero(win, '', panel, '')
    # Conservative existing Noto Sans advances at the unchanged font13 size.
    labels = [win.getControl(i).label for i in (30029, 30031, 30033, 30035)]
    labels.append('Ends at 12:59 PM')
    assert sum(text_width(text, 30) for text in labels) + 3 * 32 + 4 * 24 <= 1400


def test_cached_audience_reaches_the_ratings_overlay(load_homewindow, tmp_path, monkeypatch):
    ctx = load_homewindow()
    import lib.ui.ratingsched as sched
    cache = RatingsCache(str(tmp_path / 'ratings.json'), keys=('', 'test-key'))
    cache.store('tt1234567', {'rtAudience': '92%', 'rottenTomatoes': '98%', 'metascore': '67'})
    monkeypatch.setattr(sched, '_keys', lambda: ('', 'test-key'))
    monkeypatch.setattr(sched, '_get_cache', lambda *args: cache)
    panel = sched.overlay_text({'id': 'tt1234567', 'imdbRating': '8.3'}, '')
    assert new_look_hero_parts(panel)[2] == [
        ('IMDb', '8.3'), ('RT Audience', '92%'), ('RT', '98%'), ('MC', '67'),
    ]
    assert ctx.env.log_calls == []


@pytest.mark.parametrize('name', ICONS)
def test_original_icons_are_small_rgba_with_transparent_corners(name):
    image_module = pytest.importorskip('PIL.Image')
    path = MEDIA / name
    assert path.stat().st_size <= 8192
    with image_module.open(path) as image:
        assert image.mode == 'RGBA' and image.size == (64, 64)
        assert all(image.getpixel(p)[3] == 0 for p in ((0, 0), (63, 0), (0, 63), (63, 63)))
        assert image.getbbox() is not None


def test_score_grouplist_layout():
    root = ET.parse(SKIN).getroot()
    scores = root.find(".//control[@id='30025']")
    assert scores.get('type') == 'group'
    assert [scores.findtext(n) for n in ('left', 'top', 'width', 'height')] == ['80', '550', '1400', '40']
    inner = scores.findall('control')
    assert len(inner) == 1 and inner[0].get('type') == 'grouplist' and not inner[0].get('id')
    inner = inner[0]
    assert inner.findtext('orientation') == 'horizontal' and inner.findtext('itemgap') == '0'
    assert [inner.findtext(n) for n in ('left', 'top', 'width', 'height')] == ['0', '0', '1400', '40']
    children = {int(c.get('id')): c for c in inner.findall('control') if c.get('id')}
    assert set(children) == set(range(30029, 30037))
    for cid, c in children.items():
        assert c.get('type') == ('image' if cid in (30030, 30032, 30034) else 'label')
        if c.get('type') == 'image':
            assert c.findtext('width') == c.findtext('height') == '32'
            assert c.findtext('top') == '4' and c.findtext('aspectratio') == 'keep'
        else:
            assert c.findtext('width') == 'auto' and c.findtext('font') == 'font13'
            assert c.findtext('aligny') == 'center' and c.findtext('textcolor') == 'E6EDEDED'
            assert c.findtext('shadowcolor') == 'C8000000'
    spacers = [c for c in inner.findall('control') if not c.get('id')]
    assert len(spacers) == 4
    for spacer, after in zip(spacers, (30031, 30033, 30035, 30036)):
        assert spacer.findtext('width') == '24' and not spacer.findtext('texture')
        assert '!String.IsEmpty(Control.GetLabel(%d))' % after in spacer.findtext('visible')
    # These children, including spacers and conditions, match bc2883a.
    for node in inner.iter():
        node.text = (node.text or '').strip() or None
        node.tail = None
    child_xml = b''.join(ET.tostring(c) for c in inner.findall('control'))
    assert hashlib.sha256(child_xml).hexdigest() == 'd3d194f753dce6e76824d5c3a9533f2f7bb94b221d4877f695659dd159062ad5'
    plot = root.find(".//control[@id='30022']")
    assert plot.findtext('top') == '427' and plot.findtext('height') == '123'
    assert int(plot.findtext('top')) + int(plot.findtext('height')) == int(scores.findtext('top'))
    for name in ICONS:
        assert (MEDIA / name).is_file() and (MEDIA / name).stat().st_size <= 8192
