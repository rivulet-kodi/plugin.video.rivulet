"""One-page search: native control types, real flow and bounded hero geometry."""
import contextlib
import itertools
import threading
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests.kodistubs import install_kodi_stubs
from tests.test_newhome import _RELOAD_MODULE_NAMES
from tests.test_searchwindow import _FakeStore

ROOT = Path(__file__).resolve().parents[1]
SKIN = ROOT / 'resources/skins/Default/1080i'
XML = SKIN / 'NewSearchWindow.xml'
PLOTS = ['', 'One line.', 'First.\nSecond.\nThird.', '\n'.join('Line %d' % n for n in range(12)), 'A long synthesized synopsis about a crew exploring the stars. ' * 35]
OMDB = 'IMDb 8.3\nRotten Tomatoes 98%\nMetascore 67\n1979\n117 min\nScience Fiction, Horror'
META = {'id': 'tt0078748', 'type': 'movie', 'name': 'Alien', 'description': 'A crew receives an unexpected signal from a distant world. ' * 5, 'poster': 'poster', 'background': 'backdrop', 'logo': 'logo', 'runtime': '117 min', 'year': '1979', 'imdbRating': '8.3'}


@pytest.fixture
def load_search(monkeypatch):
    with contextlib.ExitStack() as stack:
        windows = []
        def load(**kwargs):
            ctx = stack.enter_context(install_kodi_stubs(
                reload=_RELOAD_MODULE_NAMES + ('lib.ui.newsearch', 'lib.ui.ratingsched', 'lib.ui.logocheck', 'lib.ui.pairwindow'),
                **kwargs,
            ))
            monkeypatch.setattr(ctx.newhome, '_logo_known_good', lambda url: False)
            monkeypatch.setattr(ctx.logocheck, 'ask', lambda urls: None)
            ctx.store = _FakeStore()
            monkeypatch.setattr(ctx.dependencies, 'get_store', lambda: ctx.store)
            monkeypatch.setattr(ctx.dependencies, 'get_client', lambda: object())
            win = ctx.newsearch.NewSearchWindow('NewSearchWindow.xml', '/addon/path', 'Default', '1080i')
            windows.append(win)
            ctx.win = win
            return ctx
        yield load
        for win in windows:
            win.close()


def _init(ctx, monkeypatch):
    monkeypatch.setattr(ctx.win, '_load_discover', lambda: None)
    ctx.win.onInit()
    return ctx.win


def _results(ctx, monkeypatch, metas=None, canceled=False, failure=False, notice=False):
    calls = []
    def query(store, client, text, report):
        calls.append((store, text, threading.get_ident()))
        report.total_catalogs = report.queried = 2 if notice else 1
        report.canceled = canceled
        if failure or notice:
            report.failures = [('Provider', 'timeout')]
        return [META] if metas is None else metas
    monkeypatch.setattr(ctx.searchwindow, 'run_query', query)
    return calls


def test_welcome_discover_letters_search_history_detail_flow(load_search, monkeypatch):
    ctx = load_search()
    win = _init(ctx, monkeypatch)
    s = ctx.newsearch
    assert win.getFocusId() == s.QUERY
    assert not win.getProperty('SearchHasRow')
    assert win.getControl(30020).label == '[B]STR30508[/B]'
    assert win.getControl(30022).text == 'STR30509'
    assert win.getControl(30000).image == ctx.newlook.fanart()
    assert win.getControl(30025).visible is False
    assert win.getControl(s.LETTERS).size() == 28
    assert [item.getProperty('key') for item in win.getControl(s.LETTERS).items] == list(s.KEYS)
    assert win.getControl(s.LETTERS).getListItem(26).getLabel() == '_'
    # Worker answers queue data and a noop; all control writes wait for onAction.
    worker = threading.Thread(target=lambda: win._post(('discover', '', [META])))
    worker.start()
    worker.join(1)
    assert not worker.is_alive() and not win.getProperty('SearchHasRow')
    assert ctx.env.executed_builtins[-1] == 'Action(noop)'
    import xbmcgui
    win.onAction(xbmcgui.Action(0))
    assert win._discover == [META] and win._ready == []
    assert win.getProperty('SearchHasRow') == '1'
    assert win.getControl(s.ROW_HEADING).label == '[B]STR30506[/B]'
    item = win.getControl(s.ROW).getListItem(0)
    assert item.getProperty('sid') == META['id'] and item.getProperty('position') == '0'
    assert item.getProperty('thumbnail') == 'poster'
    for letter in 'ALIEN':
        win.getControl(s.LETTERS).selectItem(s.KEYS.index(letter))
        win.onClick(s.LETTERS)
    assert win.query == 'Alien'
    assert win.getControl(s.QUERY).getText() == 'Alien'
    calls = _results(ctx, monkeypatch)
    win.onClick(s.GO)
    assert calls == [(ctx.store, 'Alien', threading.get_ident())]
    assert win.getFocusId() == s.ROW and win._metas == [META]
    assert win.getControl(s.ROW_HEADING).label == '[B]STR30507[/B]'
    assert ctx.store.search_queries == []  # Save only when a result is picked.
    opened = []
    monkeypatch.setattr(ctx.detailwindow, 'open_detail', lambda kind, sid: opened.append((kind, sid)) or True)
    win.onClick(s.ROW)
    assert opened == [('movie', 'tt0078748')]
    assert ctx.store.search_queries == ['Alien']
    assert win.should_close_caller and win.closed


def test_letters_spacing_casing_delete_limit_and_empty_discover(load_search, monkeypatch):
    ctx = load_search()
    win = _init(ctx, monkeypatch)
    for key in [' ', 'A', 'L', ' ', ' ', 'I', 'E', 'N', '<']:
        win._key(key)
    assert win.query == 'Al Ie'
    win.query = 'A' * 60
    win._key('B')
    win._key(' ')
    assert len(win.query) == ctx.newsearch.MAX_QUERY == 60
    win._discover = [META]
    win._metas = [dict(META, id='other')]
    win.query = 'A'
    win._key('<')
    assert win.query == '' and win._metas == []
    assert win.getControl(ctx.newsearch.ROW_HEADING).label == '[B]STR30506[/B]'
    win._discover = []
    win._key('<')
    assert not win.getProperty('SearchHasRow')
    assert win.getControl(30020).label == '[B]STR30508[/B]'
    calls = _results(ctx, monkeypatch)
    win.onClick(ctx.newsearch.GO)
    assert calls == []


@pytest.mark.parametrize('metas', [[], [META]])
def test_canceled_search_preserves_the_entire_visible_screen(load_search, monkeypatch, metas):
    ctx = load_search()
    win = _init(ctx, monkeypatch)
    win._discover = [META]
    win._show(win._discover, False)
    win.query = 'Other'
    win._paint_query()
    win._status('prior status')
    before = (dict(win.properties), {cid: {key: value for key, value in vars(c).items() if key != 'items'} for cid, c in win._controls.items()}, win.getControl(30022).text, win.getControl(30205).label, win.getFocusId())
    _results(ctx, monkeypatch, metas=metas, canceled=True)
    win._go()
    after = (dict(win.properties), {cid: {key: value for key, value in vars(c).items() if key != 'items'} for cid, c in win._controls.items()}, win.getControl(30022).text, win.getControl(30205).label, win.getFocusId())
    assert before == after and win._metas == [] and ctx.store.search_queries == []


@pytest.mark.parametrize('failure', [False, True])
def test_no_results_or_all_catalog_failure_status(load_search, monkeypatch, failure):
    ctx = load_search()
    win = _init(ctx, monkeypatch)
    win.query = 'Alien'
    _results(ctx, monkeypatch, metas=[], failure=failure)
    win._go()
    expected = 'STR30382' if failure else 'No results for “Alien”'
    assert win.getControl(ctx.newsearch.STATUS).label == expected
    assert win._metas == [] and ctx.store.search_queries == []


def test_partial_notice_and_phone_search(load_search, monkeypatch):
    ctx = load_search()
    win = _init(ctx, monkeypatch)
    monkeypatch.setattr(ctx.pairwindow, 'phone_search', lambda: ' Alien ')
    calls = _results(ctx, monkeypatch, notice=True)
    win.onClick(ctx.newsearch.PHONE)
    assert win.query == 'Alien' and calls[0][1] == 'Alien'
    assert win.getFocusId() == ctx.newsearch.ROW
    assert win.getControl(ctx.newsearch.STATUS).label
    assert win.getControl(ctx.newsearch.QUERY).getText() == 'Alien'
    monkeypatch.setattr(ctx.pairwindow, 'phone_search', lambda: None)
    win._phone()
    assert len(calls) == 1


def test_discover_worker_is_daemon_and_only_ui_thread_applies(load_search, monkeypatch):
    ctx = load_search()
    win = ctx.win
    entered, release = threading.Event(), threading.Event()
    def discover():
        entered.set()
        assert release.wait(3)
        return [META]
    monkeypatch.setattr(ctx.newsearch, 'discover_metas', discover)
    before = threading.get_ident()
    threads = []
    real_thread = threading.Thread
    def thread(*args, **kwargs):
        t = real_thread(*args, **kwargs)
        threads.append(t)
        return t
    monkeypatch.setattr(ctx.newsearch.threading, 'Thread', thread)
    win.onInit()
    assert entered.wait(3)
    assert threads[0].daemon and threads[0].name == 'rivulet-search-discover'
    assert not win.getProperty('SearchHasRow')
    release.set()
    threads[0].join(3)
    assert not threads[0].is_alive() and not win.getProperty('SearchHasRow')
    applied = []
    show = win._show
    monkeypatch.setattr(win, '_show', lambda *args, **kw: (applied.append(threading.get_ident()), show(*args, **kw)))
    import xbmcgui
    win.onAction(xbmcgui.Action(0))
    assert applied == [before] and win.getProperty('SearchHasRow') == '1'


@pytest.mark.parametrize('catalogs,empty_movie,expected', [
    ([('series', 'series'), ('movie', 'movie')], False, 'movie'),
    ([('series', 'series'), ('movie', 'movie')], True, 'series'),
    ([], False, ''),
])
def test_discover_first_open_movie_else_series_and_limit(load_search, monkeypatch, catalogs, empty_movie, expected):
    ctx = load_search()
    from lib.stremio import addons
    entries = [('url', {}, {'type': kind, 'id': name}) for kind, name in catalogs]
    monkeypatch.setattr(addons, 'iter_catalogs', lambda installed: iter(entries))
    calls = []
    def page(url, manifest, catalog):
        calls.append(catalog['id'])
        if empty_movie and catalog['type'] == 'movie':
            raise ctx.newhome.CatalogUnavailable('movie')
        return [dict(META, id='%s-%d' % (catalog['id'], n)) for n in range(45)]
    monkeypatch.setattr(ctx.newhome, '_one_catalog_page', page)
    metas = ctx.newsearch.discover_metas()
    assert len(metas) == (40 if expected else 0)
    if expected:
        assert metas[0]['id'] == expected + '-0'
        assert calls[0] == 'movie'


def test_discover_skips_required_extras_and_handles_empty_disabled_addons(load_search, monkeypatch):
    ctx = load_search()
    calls = []
    monkeypatch.setattr(ctx.newhome, '_one_catalog_page', lambda *args: calls.append(args) or [])
    ctx.store._addons = [{'transportUrl': 'https://example.invalid/manifest.json', 'manifest': {'catalogs': [
        {'type': 'movie', 'id': 'search', 'extra': [{'name': 'search', 'isRequired': True}]},
        {'type': 'movie', 'id': 'genre', 'extra': [{'name': 'genre', 'isRequired': True}]},
        {'type': 'movie', 'id': 'other', 'extra': [{'name': 'unsupported', 'isRequired': True}]},
    ]}}]
    assert ctx.newsearch.discover_metas() == [] and calls == []
    ctx.store._addons[0]['flags'] = {'disabled': True}
    assert ctx.newsearch.discover_metas() == [] and calls == []


def test_search_omdb_overlay_moves_plot_and_scores_and_reuses_helpers(load_search, monkeypatch):
    ctx = load_search()
    win = _init(ctx, monkeypatch)
    win._show([META, dict(META, id='tt2', logo='later-logo')], True)
    win.setFocusId(ctx.newsearch.ROW)
    item = win.getControl(ctx.newsearch.ROW).getListItem(0)
    overlaid = []
    monkeypatch.setattr(ctx.ratingsched, 'overlay_text', lambda facts, meta, upcoming: overlaid.append((facts, upcoming)) or OMDB)
    monkeypatch.setattr(ctx.newhome, '_logo_known_good', lambda url: True)
    logos = []
    monkeypatch.setattr(ctx.logocheck, 'ask', lambda urls: logos.append(urls))
    ctx.newsearch._paint_hero(win, item)
    assert item.getProperty('facts_done') == '1' and item.getProperty('hero_meta') == OMDB
    assert overlaid[0][0]['id'] == META['id'] and len(overlaid[0][1]) == 1
    assert logos[-1] == ['later-logo']
    assert win.getProperty('HeroLogo') == 'logo'
    plot, scores = win.getControl(30022), win.getControl(30025)
    assert (plot.y, plot.height, scores.y, scores.height) == (468, 82, 550, 40)
    assert win.getControl(30024).y == 418 and win.getControl(30027).y == 424
    assert win.getControl(30023).y == 302 and win.getControl(30020).y == 322
    assert win.getControl(30029).label == '[COLOR B3EDEDED]IMDb[/COLOR] 8.3'
    assert win.getControl(30032).image == 'rating_rt_fresh.png' and win.getControl(30033).label == ' : 98%'
    assert win.getControl(30034).image == 'rating_mc_green.png' and win.getControl(30035).label == ' : 67'
    assert win.getControl(30036).label.startswith('Ends at ')
    paints = []
    monkeypatch.setattr(ctx.newlook, 'paint_hero', lambda *args: paints.append(args))
    ctx.newsearch._paint_hero(win, item)
    assert paints == [] and len(overlaid) == 1


def test_search_fallbacks_and_empty_item_leave_hero(load_search, monkeypatch):
    ctx = load_search()
    win = _init(ctx, monkeypatch)
    win._show([{'name': 'No artwork', 'id': 'tt1', 'type': 'movie'}], False)
    item = win.getControl(ctx.newsearch.ROW).getListItem(0)
    assert not item.getProperty('thumbnail')
    assert win.getControl(30000).image == ctx.newlook.fanart()
    assert not win.getProperty('HeroLogo') and not win.getControl(30025).visible
    ctx.newsearch._paint_hero(win, None)
    import xbmcgui
    ctx.newsearch._paint_hero(win, xbmcgui.ListItem(''))
    assert win.getControl(30020).label == 'No artwork'
    win._show([dict(META, background='')], False)
    assert win.getProperty('HeroFill') == '1' and win.getControl(30028).image == 'poster'
    win.query = ''
    win._key('<')
    assert not win.getProperty('HeroFill') and win.getControl(30028).image == ''


def _rect(c):
    return tuple(int(c.findtext(k, '0')) for k in ('left', 'top', 'width', 'height'))


def _apart(a, b):
    x, y, w, h = a
    xx, yy, ww, hh = b
    return x + w <= xx or xx + ww <= x or y + h <= yy or yy + hh <= y


@pytest.mark.parametrize('plot', PLOTS, ids=['empty', 'one', 'three', 'twelve', 'long'])
@pytest.mark.parametrize('scores,stars,facts,logo', list(itertools.product([False, True], repeat=4)))
def test_eighty_search_hero_combinations(load_search, plot, scores, stars, facts, logo):
    ctx = load_search()
    win = ctx.win
    ctx.newlook.place_hero(win, plot, facts, scores, stars, ctx.newsearch.SEARCH_STACK)
    widths = {30020: 1100, 30023: 1000, 30024: 1200, 30027: 150, 30022: 920, 30025: 1400}
    boxes = []
    def box(cid):
        c = win.getControl(cid)
        result = (c.x, c.y, widths[cid], 28 if cid == 30027 else c.height)
        boxes.append(result)
        return result
    title = box(30023 if logo else 30020)
    assert title[1] >= 302 > 296
    if facts:
        box(30024)
    if stars:
        box(30027)
    if plot:
        p = box(30022)
        assert p[3] == 82 and p[1] + p[3] == (550 if scores else 590)
    if scores:
        s = box(30025)
        assert s[1] + s[3] == 590 < 630
        if plot:
            assert p[1] + p[3] == s[1]
    for a, b in itertools.combinations(boxes, 2):
        assert _apart(a, b), (a, b)
    assert all(_apart(a, (100, 192, 728, 104)) and _apart(a, (880, 214, 960, 60)) for a in boxes)




def test_search_xml_native_controls_and_textures(load_search):
    ctx = load_search()
    text = XML.read_text()
    root = ET.fromstring(text)
    ids = {int(c.get('id')): c.get('type') for c in root.iter('control') if c.get('id')}
    from tests.kodistubs.modules import WRAPPABLE_CONTROL_TYPES
    used = {30201, 30202, 30203, 30204, 30205, 30215, 30140, 30000, 30020, 30021, 30022, 30023, 30024, 30025, 30027, 30028, 30098, 30099} | set(range(30029, 30037))
    assert all(ids[cid] in WRAPPABLE_CONTROL_TYPES for cid in used)
    for cid in used:
        ctx.win.getControl(cid)
    assert 30006 not in ids
    assert not [c for c in root.iter('control') if c.get('type') == 'grouplist' and c.get('id')]
    assert root.findtext('defaultcontrol') == '30201'
    assert ids[30201] == 'edit'
    assert not root.findall('.//animation[@type="WindowClose"]')
    for texture in root.iter():
        if texture.tag in {'texture', 'texturefocus', 'texturenofocus'} and texture.text and not texture.text.startswith('$'):
            assert (ROOT / 'resources/skins/Default/media' / texture.text).is_file()
    bar = next(c for c in root.findall('controls/control/control') if _rect(c) == (60, 96, 800, 76))
    assert bar.find('texture').get('border') == '38'


@pytest.mark.parametrize('logo', [False, True])
def test_search_static_geometry_and_navigation(load_search, monkeypatch, logo):
    ctx = load_search()
    root = ET.parse(XML).getroot()
    top = root.findall('controls/control/control')
    ids = {int(c.get('id')): c for c in root.iter('control') if c.get('id')}
    selected = [ids[cid] for cid in [30201, 30202, 30203, 30204, 30205, 30215, 30140, 30024, 30027, 30022, 30025, 30023 if logo else 30020]]
    selected += [c for c in top if not c.get('id') and _rect(c)[1] in {36, 630, 680}]
    for a, b in itertools.combinations(selected, 2):
        assert _apart(_rect(a), _rect(b)), (_rect(a), _rect(b))
    nav = {30201: (30201, 30202, 30201, 30204), 30202: (30201, 30215, 30202, 30204), 30204: (30204, 30202, 30201, 30203), 30203: (30203, 30202, 30204, 30203), 30215: (30202, 30215, 30215, 30215)}
    for cid, expected in nav.items():
        assert tuple(int(ids[cid].findtext(n)) for n in ['onup', 'ondown', 'onleft', 'onright']) == expected
    win = _init(ctx, monkeypatch)
    import xbmcgui
    assert win.getFocusId() == 30201
    win.setFocusId(30202)
    win.onAction(xbmcgui.Action(4))
    assert win.getFocusId() == 30202 and not win.getProperty('SearchHasRow')
    win.onAction(xbmcgui.Action(92))
    assert win.closed




def test_pending_discover_cannot_replace_search_results(load_search, monkeypatch):
    ctx = load_search()
    win = _init(ctx, monkeypatch)
    win.query = 'Alien'
    _results(ctx, monkeypatch)
    win._go()
    win._post(('discover', '', [dict(META, name='Discover title')]))
    import xbmcgui
    win.onAction(xbmcgui.Action(0))
    assert win._discover[0]['name'] == 'Discover title'
    assert win.getControl(ctx.newsearch.ROW).getListItem(0).getLabel() == 'Alien'
    assert win.getControl(ctx.newsearch.ROW_HEADING).label == '[B]STR30507[/B]'


def test_real_query_with_no_addons_keeps_welcome_and_explains(load_search, monkeypatch):
    ctx = load_search()
    win = _init(ctx, monkeypatch)
    win.query = 'Alien'
    win._go()
    assert win.getControl(ctx.newsearch.STATUS).label == 'STR30381'
    assert not win.getProperty('SearchHasRow') and win._metas == []
    assert win.getControl(30020).label == '[B]STR30508[/B]'


def test_open_discover_poster_does_not_record_query(load_search, monkeypatch):
    ctx = load_search()
    win = _init(ctx, monkeypatch)
    win._discover = [META]
    win._show(win._discover, False)
    opened = []
    monkeypatch.setattr(ctx.detailwindow, 'open_detail', lambda *args: opened.append(args) or False)
    win.onClick(ctx.newsearch.ROW)
    assert opened == [('movie', META['id'])]
    assert ctx.store.search_queries == [] and not win.should_close_caller


def test_open_failure_before_window_creation_notifies(load_search, monkeypatch):
    ctx = load_search()
    def fail(*args):
        raise RuntimeError('skin error')
    monkeypatch.setattr(ctx.newsearch, 'open_window', fail)
    assert ctx.newsearch.open_search() is False
    assert ctx.env.notifications == [('Rivulet', 'STR30032', 'info', 4000)]


def test_malformed_facts_and_broken_control_do_not_skip_hero_geometry(load_search, monkeypatch):
    ctx = load_search()
    win = _init(ctx, monkeypatch)
    item = ctx.newhome._poster_item(META)
    item.setProperty('hero_facts', 'not-json')
    item.setProperty('hero_meta', OMDB)
    def fail(*args):
        raise RuntimeError('unwrappable')
    monkeypatch.setattr(win.getControl(30023), 'setImage', fail)
    ctx.newsearch._paint_hero(win, item)
    assert win.getControl(30022).y == 468 and win.getControl(30025).y == 550
    assert win.getControl(30020).label == 'Alien'
    assert any('newlook: hero control failed: RuntimeError' in message for message, _ in ctx.env.log_calls)


def test_phone_limit_and_query_markup_escaping(load_search, monkeypatch):
    ctx = load_search()
    win = _init(ctx, monkeypatch)
    monkeypatch.setattr(ctx.pairwindow, 'phone_search', lambda: 'A' * 100)
    calls = _results(ctx, monkeypatch)
    win._phone()
    assert len(win.query) == 60 and calls[0][1] == 'A' * 60
    assert ctx.newsearch._with_query('No results %s', '[B]Title[/B]') == 'No results Title'


@pytest.mark.parametrize('results', [False, True], ids=['discover', 'results'])
def test_context_menu_uses_the_selected_poster_meta(load_search, monkeypatch, results):
    ctx = load_search()
    win = _init(ctx, monkeypatch)
    selected = dict(META, id='tt0944947', type='series', name='Selected show')
    win._show([META, selected], results=results)
    win.getControl(ctx.newsearch.ROW).selectItem(1)
    win.setFocusId(ctx.newsearch.ROW)
    from lib.ui import libraryaction

    offered = []
    monkeypatch.setattr(libraryaction, 'poster_menu', lambda meta: offered.append(meta))
    import xbmcgui
    win.onAction(xbmcgui.Action(117))
    assert len(offered) == 1
    assert offered[0]['id'] == selected['id'] and offered[0]['type'] == 'series'


@pytest.mark.parametrize('control', ['LETTERS', 'PHONE', 'GO'])
def test_context_menu_off_the_poster_row_does_nothing(load_search, monkeypatch, control):
    ctx = load_search()
    win = _init(ctx, monkeypatch)
    win._show([META], results=True)
    win.setFocusId(getattr(ctx.newsearch, control))
    from lib.ui import libraryaction

    monkeypatch.setattr(libraryaction, 'poster_menu', lambda meta: pytest.fail('poster menu off row'))
    import xbmcgui
    win.onAction(xbmcgui.Action(117))
    assert not win.closed and win.query == ''


def test_context_menu_on_an_empty_poster_row_does_nothing(load_search, monkeypatch):
    ctx = load_search()
    win = _init(ctx, monkeypatch)
    win.setFocusId(ctx.newsearch.ROW)
    from lib.ui import libraryaction

    monkeypatch.setattr(libraryaction, 'poster_menu', lambda meta: pytest.fail('empty poster menu'))
    import xbmcgui
    win.onAction(xbmcgui.Action(117))
    assert not win.closed


@pytest.mark.parametrize('results', [False, True], ids=['discover', 'results'])
def test_context_menu_add_writes_the_selected_title_to_the_library(load_search, monkeypatch, results):
    from tests.test_libraryaction import _Api, _wire

    ctx = load_search()
    win = _init(ctx, monkeypatch)
    win._show([META], results=results)
    win.setFocusId(ctx.newsearch.ROW)
    api = _Api()
    _, menus = _wire(monkeypatch, api, pick=0)
    import xbmcgui
    win.onAction(xbmcgui.Action(117))
    assert len(menus) == 1 and menus[0][1] == ['STR30293', 'STR30295', 'STR30499']
    assert len(api.puts) == 1
    payload = api.puts[0][1]
    assert payload['_id'] == META['id'] and payload['type'] == META['type']
    assert payload['removed'] is False
    assert ctx.newhome._library_dirty


def test_clicks_delivered_during_a_running_search_are_ignored(load_search, monkeypatch):
    """The spinner delivers queued input; a second GO or a letter must not
    start a nested search or change the query the results are recorded for."""
    ctx = load_search()
    win = _init(ctx, monkeypatch)
    win.query = 'Alien'
    s = ctx.newsearch
    seen = []

    def query(store, client, text, report):
        seen.append(text)
        win.onClick(s.GO)
        item = win.getControl(s.LETTERS).getListItem(0)
        win.getControl(s.LETTERS).selectItem(0)
        assert item is not None
        win.onClick(s.LETTERS)
        report.total_catalogs = report.queried = 1
        return [META]

    monkeypatch.setattr(ctx.searchwindow, 'run_query', query)
    win._go()
    assert seen == ['Alien'] and win.query == 'Alien'
    assert win._metas and win._searching is False
    monkeypatch.setattr(ctx.detailwindow, 'open_detail', lambda *args: False)
    win.onClick(s.ROW)
    assert ctx.store.search_queries == ['Alien']


def _type(win, s, text):
    """What Kodi does for each key typed into the focused bar: the edit
    control changes its text and sends a click, then Python sees the key
    action (KEY_UNICODE)."""
    import xbmcgui
    for size in range(1, len(text) + 1):
        win.getControl(s.QUERY).setText(text[:size])
        win.onClick(s.QUERY)
        win.onAction(xbmcgui.Action(0xF200))


def test_physical_keyboard_types_into_the_bar_and_confirmed_enter_searches(load_search, monkeypatch):
    ctx = load_search()
    win = _init(ctx, monkeypatch)
    s = ctx.newsearch
    assert win.getFocusId() == s.QUERY
    assert win.getControl(s.QUERY).input_type == (0, 'STR30001')
    calls = _results(ctx, monkeypatch)
    _type(win, s, 'alien')
    assert win.query == 'alien' and calls == []
    import xbmcgui
    # Enter: Kodi opens its keyboard first; Done sends the bar's click,
    # then Python gets the Select.
    win.onClick(s.QUERY)
    win.onAction(xbmcgui.Action(7))
    assert [text for _, text, _ in calls] == ['alien']
    assert win.getFocusId() == s.ROW and win._metas == [META]


def test_cancelled_keyboard_on_the_bar_does_not_search(load_search, monkeypatch):
    ctx = load_search()
    win = _init(ctx, monkeypatch)
    s = ctx.newsearch
    calls = _results(ctx, monkeypatch)
    _type(win, s, 'alien')
    import xbmcgui
    win.onAction(xbmcgui.Action(7))  # keyboard cancelled: no click came first
    assert calls == [] and win.getFocusId() == s.QUERY


def test_bar_text_is_cut_to_the_limit_and_clearing_it_shows_discover(load_search, monkeypatch):
    ctx = load_search()
    win = _init(ctx, monkeypatch)
    s = ctx.newsearch
    win.getControl(s.QUERY).setText('A' * 100)
    win.onClick(s.QUERY)
    assert win.query == 'A' * s.MAX_QUERY
    assert win.getControl(s.QUERY).getText() == 'A' * s.MAX_QUERY
    win._discover = [META]
    win._metas = [dict(META, id='other')]
    win.getControl(s.QUERY).setText('')
    win.onClick(s.QUERY)
    assert win.query == '' and win._metas == []
    assert win.getControl(s.ROW_HEADING).label == '[B]STR30506[/B]'


def test_picked_letters_show_in_the_bar_and_typing_continues_from_them(load_search, monkeypatch):
    ctx = load_search()
    win = _init(ctx, monkeypatch)
    s = ctx.newsearch
    for letter in 'AL':
        win.getControl(s.LETTERS).selectItem(s.KEYS.index(letter))
        win.onClick(s.LETTERS)
    assert win.getControl(s.QUERY).getText() == 'Al'
    _type(win, s, 'Alien')
    assert win.query == 'Alien'
