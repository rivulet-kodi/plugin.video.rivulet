"""Kodi wrapper failures must not prevent fixed plot/ratings placement."""
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests.test_look import _look_hero
from tests.test_look import load_homewindow as load_homewindow

SKIN = Path(__file__).resolve().parents[1] / 'resources/skins/Default/1080i/HomeWindow.xml'
DEMON_PLOT = ('Tanjiro and the Demon Slayer Corps enter the Infinity Castle. '
              'The team faces another battle as they search for a way forward. ') * 5
PLOTS = ['', 'One line.', 'First line.\nSecond line.\nThird line.',
         '\n'.join('Plot line %d.' % n for n in range(12)), DEMON_PLOT]
OMDB_PANEL = 'IMDb 8.3\nRotten Tomatoes 98%\nMetascore 67\n2025\n156 min\nAnimation, Action'


@pytest.mark.parametrize('plot', PLOTS, ids=['empty', 'one', 'three', 'twelve', 'demon-slayer'])
@pytest.mark.parametrize('scores', [False, True])
@pytest.mark.parametrize('stars', [False, True])
@pytest.mark.parametrize('logo', [False, True])
@pytest.mark.parametrize('facts', [False, True])
def test_fixed_boxes_do_not_overlap_at_any_plot_length(
        load_homewindow, plot, scores, stars, logo, facts):
    ctx = load_homewindow()
    look = ctx.look
    win = ctx.home.HomeWindow('HomeWindow.xml', '/addon/path', 'Default', '1080i')
    look.place_hero(win, plot, facts, scores, stars)
    widths = {30020: 1100, 30023: 1000, 30024: 1200, 30027: 150, 30022: 920, 30025: 1400}
    boxes = []
    def box(cid):
        c = win.getControl(cid)
        height = 28 if cid == 30027 else c.height
        result = (c.x, c.y, c.x + widths[cid], c.y + height)
        boxes.append(result)
        return result
    box(30023 if logo else 30020)  # Title and logo are mutually exclusive.
    if facts:
        box(30024)
    if stars:
        box(30027)
    if plot:
        p = box(30022)
        assert p[3] - p[1] == look._STACK['plot_h'] == 123
        assert p[3] == (550 if scores else 590)
    if scores:
        s = box(30025)
        assert s[1] == 550 and s[3] == 590 < 630
        if plot:
            assert p[3] == s[1]
    for index, a in enumerate(boxes):
        for b in boxes[index + 1:]:
            assert a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1], (a, b)


def test_full_omdb_hero_places_wrappable_plot_and_scores_on_real_kodi_rules(load_homewindow):
    ctx = load_homewindow()
    win = _look_hero(ctx, OMDB_PANEL, plot=DEMON_PLOT, runtime='156')
    ctx.home.paint_hero(win)
    plot, scores = win.getControl(30022), win.getControl(30025)
    assert (plot.y, plot.height, scores.y, scores.height) == (427, 123, 550, 40)
    assert win.getControl(30024).y == 377 and win.getControl(30027).y == 383
    assert win.getControl(30029).label == '[COLOR B3EDEDED]IMDb[/COLOR] 8.3'
    assert win.getControl(30033).label == ' : 98%' and win.getControl(30035).label == ' : 67'
    assert win.getControl(30036).label.startswith('Ends at ')
    assert not [msg for msg, _ in ctx.env.log_calls if 'hero control failed' in msg]


@pytest.mark.parametrize('cid', [30001, 30011])
def test_fake_rejects_home_grouplists_that_kodi_cannot_wrap(load_homewindow, cid):
    ctx = load_homewindow()
    win = ctx.home.HomeWindow('HomeWindow.xml', '/addon/path', 'Default', '1080i')
    with pytest.raises(RuntimeError, match='Unknown control type for python'):
        win.getControl(cid)


@pytest.mark.parametrize('kind', ['grouplist', 'scrollbar', 'multiimage', 'rss', 'unknown'])
def test_fake_rejects_every_type_outside_the_native_wrapper_cases(
        load_homewindow, monkeypatch, tmp_path, kind):
    load_homewindow()
    import xbmcgui

    from tests.kodistubs import modules
    name = 'Unsupported.xml'
    (tmp_path / name).write_text('<window><controls><control type="%s" id="1"/></controls></window>' % kind)
    monkeypatch.setattr(modules, '_SKIN_DIR', str(tmp_path))
    monkeypatch.setattr(modules, '_SKIN_CONTROLS', {})
    win = xbmcgui.WindowXML(name, '/addon/path', 'Default', '1080i')
    with pytest.raises(RuntimeError, match='^Unknown control type for python$'):
        win.getControl(1)


@pytest.mark.parametrize('failure', ['getter', 'label', 'visibility'])
def test_one_control_failure_still_paints_others_and_places_boxes_once_logged(
        load_homewindow, monkeypatch, failure):
    ctx = load_homewindow()
    look = ctx.look
    win = ctx.home.HomeWindow('HomeWindow.xml', '/addon/path', 'Default', '1080i')
    def fail(*args, **kwargs):
        raise RuntimeError('SECRET plot and key https://private.invalid/?key=SECRET')
    if failure == 'getter':
        get = win.getControl
        monkeypatch.setattr(win, 'getControl', lambda cid: fail() if cid == 30027 else get(cid))
    elif failure == 'label':
        monkeypatch.setattr(win.getControl(30033), 'setLabel', fail)
    else:
        monkeypatch.setattr(win.getControl(30025), 'setVisible', fail)
    called = []
    place = look.place_hero
    def placed(*args, **kwargs):
        called.append(True)
        return place(*args, **kwargs)
    monkeypatch.setattr(look, 'place_hero', placed)
    look.paint_hero(win, DEMON_PLOT, OMDB_PANEL, '156')
    look.paint_hero(win, DEMON_PLOT, OMDB_PANEL, '156')
    assert called == [True, True]
    assert win.getControl(30022).y + win.getControl(30022).height == win.getControl(30025).y == 550
    assert win.getControl(30035).label == ' : 67'
    assert win.getControl(30033).visible and win.getControl(30032).image == 'rating_rt_fresh.png'
    import xbmc
    logs = [(msg, level) for msg, level in ctx.env.log_calls if 'hero control failed' in msg]
    assert len(logs) == 1 and logs[0][1] == xbmc.LOGDEBUG
    assert logs[0][0].endswith('RuntimeError') and 'SECRET' not in logs[0][0]


def test_an_unsupported_scores_group_cannot_skip_other_placement(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    win = ctx.home.HomeWindow('HomeWindow.xml', '/addon/path', 'Default', '1080i')
    monkeypatch.setattr(win, '_control_types', dict(win._control_types))
    win._control_types[30025] = 'grouplist'  # The device bug before the wrapper.
    ctx.look.paint_hero(win, DEMON_PLOT, OMDB_PANEL, '156')
    assert (win.getControl(30022).y, win.getControl(30022).height) == (427, 123)
    assert win.getControl(30024).y == 377
    assert win.getControl(30035).label == ' : 67'
    assert len([msg for msg, _ in ctx.env.log_calls if 'hero control failed' in msg]) == 1


def test_static_plot_box_meets_scores_and_retains_autoscroll():
    root = ET.parse(SKIN).getroot()
    plot = root.find(".//control[@id='30022']")
    scores = root.find(".//control[@id='30025']")
    assert int(plot.findtext('top')) + int(plot.findtext('height')) == int(scores.findtext('top')) == 550
    assert plot.findtext('height') == '123'
    auto = plot.find('autoscroll')
    assert auto.text == 'true' and auto.attrib == {'delay': '3000', 'time': '4000', 'repeat': '6000'}
