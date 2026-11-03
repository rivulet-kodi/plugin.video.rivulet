"""The new episode page under Kodi's native control-wrapper contract."""
import contextlib
import itertools
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from lib.ui.creditsrow import CREDITS
from tests.kodistubs import install_kodi_stubs
from tests.test_detailwindow import load_detailwindow as load_detailwindow
from tests.test_home import _RELOAD_MODULE_NAMES

ROOT = Path(__file__).resolve().parents[1]
SKIN = ROOT / 'resources/skins/Default/1080i'
XML = SKIN / 'DetailWindow.xml'


@pytest.fixture
def load_detail_page():
    with contextlib.ExitStack() as stack:
        def load():
            return stack.enter_context(install_kodi_stubs(
                reload=_RELOAD_MODULE_NAMES + ('lib.ui.detailwindow',),
            ))
        yield load


def _rect(control):
    return tuple(int(control.findtext(key, '0')) for key in ('left', 'top', 'width', 'height'))


def _apart(a, b):
    x, y, w, h = a
    xx, yy, ww, hh = b
    return x + w <= xx or xx + ww <= x or y + h <= yy or yy + hh <= y


def test_static_control_contract_and_textures():
    root = ET.parse(XML).getroot()
    declared = {int(c.get('id')): c.get('type') for c in root.iter('control') if c.get('id')}
    from tests.kodistubs.modules import WRAPPABLE_CONTROL_TYPES
    used = {30000, 30004, 30005, 30002, 30007, 30009, 30012, 30023, 30100, 30101, 30102, 30098, 30099}
    assert all(declared[cid] in WRAPPABLE_CONTROL_TYPES for cid in used)
    assert declared[30006] == 'scrollbar' and 30006 not in used
    assert not root.findall('.//animation[@type="WindowClose"]')
    for texture in root.iter():
        if texture.tag.startswith('texture') and texture.text and not texture.text.startswith('$'):
            assert (ROOT / 'resources/skins/Default/media' / texture.text).is_file()



@pytest.mark.parametrize('logo', [False, True])
def test_episode_page_geometry(logo):
    controls = ET.parse(XML).getroot().find('controls/control').findall('control')
    # Decorative background plates and hidden compatibility controls are intentionally layered.
    selected = [c for c in controls if c.get('id') in {'30002', '30004', '30007', '30009', '30012', '30030', '30100'}]
    selected += [c for c in controls if c.get('id') == ('30023' if logo else '30005')]
    selected += [c for c in controls if not c.get('id') and _rect(c)[1] in {36, 290, 1000, 1044}]
    for a, b in itertools.combinations(selected, 2):
        assert _apart(_rect(a), _rect(b)), (_rect(a), _rect(b))
    episodes = next(c for c in controls if c.get('id') == '30002')
    assert _rect(episodes) == (60, 396, 1310, 600)
    for name in ('itemlayout', 'focusedlayout'):
        layout = episodes.find(name)
        assert int(layout.get('height')) == 200 and 600 // 200 == 3
        children = [c for c in layout.findall('control') if c.findtext('label', '').find('Property(') >= 0]
        children += [c for c in layout.findall('control') if c.findtext('texture') == 'look_check.png']
        still = (16, 18, 294, 165)
        for c in children:
            x, y, w, h = _rect(c)
            assert 0 <= x < x + w <= 1310 and 0 <= y < y + h <= 200
            assert _apart(_rect(c), still)
        # Focus/non-focus duplicates are mutually exclusive; each distinct field must fit apart.
        for a, b in itertools.combinations(set(map(_rect, children)), 2):
            assert _apart(a, b), (a, b)


def test_one_season_navigation_walks_hidden_tab(load_detailwindow):
    ctx = load_detailwindow()
    d = ctx.detailwindow
    win = d.DetailWindow('DetailWindow.xml', '/addon/path', 'Default', '1080i')
    win.start({'id': 'tt1', 'videos': [{'id': 'v1', 'season': 1, 'episode': 1}]}, 'series')
    win.onInit()
    assert win.getControl(d.SEASON_BAR).visible is False
    assert win.getControl(CREDITS).visible is False  # no links: unfocusable, so walked past
    controls = {int(c.get('id')): c for c in ET.parse(XML).iter('control') if c.get('id')}
    def follow(cid, direction):
        target = int(controls[cid].findtext(direction))
        return follow(target, direction) if target in (d.SEASON_BAR, CREDITS) else target
    assert follow(d.LIST, 'onup') == d.OPTIONS
    assert follow(d.OPTIONS, 'ondown') == follow(d.TRAILER, 'ondown') == d.LIST
    assert win.getFocusId() == d.LIST
    with pytest.raises(RuntimeError, match='Unknown control type'):
        win.getControl(30006)
    win.close()


@pytest.mark.parametrize('known', [False, True])
def test_full_episode_page_local_progress_logo_and_fallback(load_detail_page, monkeypatch, known):
    ctx = load_detail_page()
    import xbmcgui

    d = ctx.detailwindow
    monkeypatch.setattr(ctx.home, '_logo_known_good', lambda url: known)
    monkeypatch.setattr(ctx.home, '_warm_logos', lambda win, urls: win.getControl(30098).setImage(urls[0]))
    monkeypatch.setattr(d, '_prefetch_s4me_streams', lambda *args: None)
    monkeypatch.setattr(d.DetailWindow, '_episode_progress', staticmethod(lambda st, sid, vid: {'position_ms': 90000 if vid == 'v2' else 42000, 'duration_ms': 100000}))
    videos = [dict(id='v%d' % n, season=1 if n < 3 else 2, episode=n, title='Episode', released='2026-10-21T00:00:00Z', overview='Whole plot\nSecond line', runtime='71 min', thumbnail='still' if n == 1 else '') for n in range(1, 4)]
    win = d.DetailWindow('DetailWindow.xml', '/addon/path', 'Default', '1080i')
    win.start(dict(id='tt1', name='Show', poster='poster', logo='logo', videos=videos), 'series')
    win.onInit()
    rows = win.getControl(d.LIST)
    first, second = rows.getListItem(0), rows.getListItem(1)
    assert first.getProperty('thumb') == 'still' and second.getProperty('thumb') == 'poster'
    assert first.getProperty('ep_title') == '1×01. Episode'
    assert first.getProperty('runtime') == '1h 11m'
    assert first.getProperty('plot') == 'Whole plot\nSecond line'
    assert first.getProperty('watched') == '' and second.getProperty('watched') == '1'
    assert first.getProperty('watched_percent') == '42% WATCHED'
    assert win.getProperty('HeroLogo') == ('logo' if known else '')
    assert win.getControl(30023).image == ('logo' if known else '')
    assert win.getControl(d.SEASON_BAR).visible
    for action, expected in [(1, 0), (2, 1), (2, 1), (1, 0)]:
        win.onAction(xbmcgui.Action(action))
        assert rows.getSelectedPosition() == expected
    calls = []
    monkeypatch.setattr(win, '_open_context_menu', lambda: calls.append('options'))
    monkeypatch.setattr(win, '_play_trailer', lambda: calls.append('trailer'))
    win.onClick(d.OPTIONS)
    win.onClick(d.TRAILER)
    assert calls == ['options', 'trailer']
    win.close()


@pytest.mark.parametrize('value,expected', [('54 min', '54m'), (71, '1h 11m'), ('bad', ''), (None, '')])
def test_short_runtime(load_detailwindow, value, expected):
    assert load_detailwindow().detailwindow._short_runtime(value) == expected


def test_date_and_missing_fields(load_detailwindow, monkeypatch):
    d = load_detailwindow().detailwindow
    import xbmc
    monkeypatch.setattr(xbmc, 'getRegion', lambda key: '%m.%d.%Y', raising=False)
    assert d._short_date('2026-10-21') == '10.21.2026'
    monkeypatch.setattr(xbmc, 'getRegion', lambda key: '', raising=False)
    assert d._short_date('2026-10-21') == '21/10/2026'
    assert d._short_date('invalid') == 'invalid' and d._short_date(None) == ''
    props = d._episode_properties({})
    assert all(props[key] == '' for key in ('thumb', 'aired', 'runtime', 'plot', 'ep_title'))


def test_trailer_context_menu_last_and_dispatch(load_detailwindow, monkeypatch):
    ctx = load_detailwindow()
    d = ctx.detailwindow
    win = d.DetailWindow('DetailWindow.xml', '/addon/path', 'Default', '1080i')
    monkeypatch.setattr(win, '_current_library_state', lambda: (None, None, True))
    seen = []
    monkeypatch.setattr(win, '_play_trailer', lambda: seen.append('trailer'))
    def choose(heading, rows):
        assert rows[-1] == 'STR30499' and len(rows) == 4
        return 3
    monkeypatch.setattr(ctx.dialogs, 'choose', choose)
    win._open_context_menu()
    assert seen == ['trailer']
    win.close()


def test_episode_textures_match_generator():
    pytest.importorskip('PIL')
    import importlib.util
    spec = importlib.util.spec_from_file_location('episode_textures', ROOT / 'tools/make_look_textures.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    from PIL import Image
    drawings = module.textures()
    for name, size in [('look_panel.png', (96, 96)), ('look_check.png', (64, 64))]:
        path = ROOT / module.MEDIA / name
        with Image.open(path) as image:
            assert image.size == size
        assert module._same(path, drawings[name])
    assert module.main(['--check']) == 0
