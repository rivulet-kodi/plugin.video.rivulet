"""Hero rendering and shared Home helpers for the single Home."""
import contextlib
import os
import time
import xml.etree.ElementTree as ET

import pytest

from lib.ui.playbackmeta import look_hero_parts
from tests.kodistubs import install_kodi_stubs
from tests.test_home import (
    _RELOAD_MODULE_NAMES,
    _THREE,
    _FakeStore,
    _patch_library,
)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SKIN = os.path.join(_REPO_ROOT, 'resources', 'skins', 'Default', '1080i')
_MEDIA = os.path.join(_REPO_ROOT, 'resources', 'skins', 'Default', 'media')


@pytest.fixture
def load_homewindow():
    """`load_homewindow(settings=None)`: fresh Kodi stubs, lib.ui.look included."""
    with contextlib.ExitStack() as stack:
        def _load(settings=None, localized=None):
            ctx = stack.enter_context(install_kodi_stubs(
                reload=_RELOAD_MODULE_NAMES,
                settings=settings,
                localized=localized,
            ))
            ctx.homewindow = ctx.home
            return ctx

        yield _load


def _ids(name):
    root = ET.parse(os.path.join(_SKIN, name)).getroot()
    return {int(c.get('id')) for c in root.iter('control') if c.get('id')}


# --- the files ---------------------------------------------------------------


def test_home_declares_every_control_the_window_code_uses(load_homewindow):
    """The fake window raises on an unknown id, and so does Kodi: every
    control id lib/ui/home.py and lib/ui/look.py name is in the file."""
    ctx = load_homewindow()
    home = ctx.home
    look = ctx.look
    from lib.ui.posterwarm import WARM_SLOTS

    declared = _ids('HomeWindow.xml')
    used = {
        home.BACKGROUND, home.STATUS_LABEL, home.BROWSE_LIST, home.TOOLS_LIST,
        home.HERO_TITLE, home.HERO_META, home.HERO_PLOT, home.HERO_LOGO,
        home.HERO_FILL, home.CONTINUE_SHELF, home.LIBRARY_SHELF,
        home.BTN_SEARCH, home.BTN_ADDONS, home.BTN_SETTINGS, home.BTN_SIGNIN,
        look.HERO_FACTS, look.HERO_SCORES, look.HERO_STARS,
    }
    used |= {list_id for _action, list_id, _label in home._SHELVES}
    used |= {label_id for _action, _list_id, label_id in home._SHELVES}
    used |= {counter for _list_id, _prop, counter in home._ROW_COUNTERS}
    used |= set(home.LOGO_WARM_SLOTS) | set(WARM_SLOTS)
    used |= {cid for _, image_id, label_id in look._SCORE_CONTROLS
             for cid in (image_id, label_id) if cid is not None}
    assert not used - declared, sorted(used - declared)
    from tests.kodistubs.modules import WRAPPABLE_CONTROL_TYPES
    types = {int(c.get('id')): c.get('type') for c in
             ET.parse(os.path.join(_SKIN, 'HomeWindow.xml')).iter('control') if c.get('id')}
    # TOOLS_LIST is a focus/navigation target; Python accesses its buttons.
    assert all(types[cid] in WRAPPABLE_CONTROL_TYPES for cid in used - {home.TOOLS_LIST})


def test_every_texture_the_home_names_is_shipped():
    for name in ('HomeWindow.xml',):
        root = ET.parse(os.path.join(_SKIN, name)).getroot()
        for texture in root.iter():
            if texture.tag not in ('texture', 'texturefocus', 'texturenofocus'):
                continue
            value = (texture.text or '').strip()
            if value and not value.startswith('$'):
                assert os.path.isfile(os.path.join(_MEDIA, value)), value
    for half in range(11):
        assert os.path.isfile(os.path.join(_MEDIA, 'look_stars_%02d.png' % half))




# --- picking the file --------------------------------------------------------


# --- the hero lines ----------------------------------------------------------

def test_fanart_path_comes_from_the_installed_addon_dir(load_homewindow):
    """`special://home/addons/<id>` is a guess: a system-wide or portable
    install lives elsewhere. Ask Kodi where the add-on actually is."""
    ctx = load_homewindow()
    ctx.env.addon.info['path'] = '/opt/kodi/addons/plugin.video.rivulet'
    assert ctx.look.fanart() == '/opt/kodi/addons/plugin.video.rivulet/resources/fanart_grey.jpg'


def test_hero_parts_split_scores_from_facts():
    text = 'Rotten Tomatoes 94%\nMetascore 71\nIMDb 7.4\n2025\n1h 52m\nThriller, Mystery'
    stars, facts, scores = look_hero_parts(text)
    assert stars == 7  # 7.4 of 10 is three and a half stars
    assert facts == ['2025', '1h 52m', 'Thriller, Mystery']
    assert scores == [('IMDb', '7.4'), ('RT', '94%'), ('MC', '71')]


@pytest.mark.parametrize('text, stars', [
    ('2025', None), ('IMDb n/a\n2025', None), ('IMDb 10', 10), ('IMDb 0.2', 0), ('IMDb 8.75/10', 9),
])
def test_hero_parts_stars(text, stars):
    assert look_hero_parts(text)[0] == stars


def test_hero_parts_keep_an_unknown_line_as_a_fact():
    assert look_hero_parts('Ended\n\n2019')[1] == ['Ended', '2019']


def test_hero_lines_join_facts_and_end_scores_with_the_end_time(load_homewindow, monkeypatch):
    load_homewindow()
    from lib.ui import look

    monkeypatch.setattr(look, '_clock_format', lambda: '%H:%M')
    now = time.mktime((2026, 10, 17, 20, 36, 0, 0, 0, -1))
    stars, facts, scores = look.hero_lines('IMDb 7.4\nRotten Tomatoes 88%\n2025\n1h 52m', '112', now=now)
    assert stars == 'look_stars_07.png'
    assert facts == '2025  •  1h 52m'
    assert scores == [('IMDb', '7.4'), ('RT', '88%'), ('Ends at', 'Ends at 22:28')]


def test_no_runtime_no_end_time(load_homewindow):
    load_homewindow()
    from lib.ui import look

    assert look.ends_at('') == '' and look.ends_at(None) == '' and look.ends_at('0') == ''
    assert all(kind != 'Ends at' for kind, _value in look.hero_lines('IMDb 7', '')[2])


def test_twelve_hour_clock_drops_the_leading_zero(load_homewindow, monkeypatch):
    load_homewindow()
    from lib.ui import look

    monkeypatch.setattr(look, '_clock_format', lambda: '%I:%M %p')
    now = time.mktime((2026, 10, 17, 20, 0, 0, 0, 0, -1))
    assert look.ends_at('65', now=now) == '9:05 PM'


# --- Home in the Home file ---------------------------------------------------

def _look_hero(ctx, meta, plot='A storm-chaser returns.', runtime='112', logo=''):
    import xbmcgui
    win = ctx.homewindow.HomeWindow('HomeWindow.xml', '/addon/path', 'Default', '1080i')
    item = xbmcgui.ListItem('Hollow Tide')
    item.setProperties({
        'hero_title': 'Hollow Tide', 'hero_meta': meta, 'hero_plot': plot,
        'hero_runtime': runtime, 'fanart': 'https://img/b.jpg', 'sid': 'tt1', 'stype': 'movie',
        'hero_logo': logo,
    })
    win.getControl(ctx.homewindow.CONTINUE_SHELF).addItems([item])
    win.setFocusId(ctx.homewindow.CONTINUE_SHELF)
    return win


def test_look_home_paints_stars_facts_and_scores(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    monkeypatch.setattr(ctx.homewindow, 'get_store', lambda: _FakeStore(auth=None))
    from lib.ui import look

    win = _look_hero(ctx, 'IMDb 7.4\n2025\n1h 52m\nThriller')
    ctx.homewindow.paint_hero(win)

    assert win.getControl(look.HERO_STARS).image == 'look_stars_07.png'
    assert win.getControl(look.HERO_FACTS).label == '•  2025  •  1h 52m  •  Thriller'
    assert win.getControl(look.HERO_IMDB).label == '[COLOR B3EDEDED]IMDb[/COLOR] 7.4'
    assert 'Ends at' in win.getControl(look.HERO_END).label


def test_look_stack_ends_on_the_same_line_above_the_row(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    monkeypatch.setattr(ctx.homewindow, 'get_store', lambda: _FakeStore(auth=None))
    from lib.ui import look

    long_plot = 'word ' * 120
    win = _look_hero(ctx, 'IMDb 7.4\n2025', plot=long_plot)
    ctx.homewindow.paint_hero(win)
    scores = win.getControl(look.HERO_SCORES)
    assert scores.y + scores.height == look._STACK['bottom']
    plot = win.getControl(ctx.homewindow.HERO_PLOT)
    assert plot.height == look._STACK['plot_h'] == 122
    assert plot.y + plot.height == scores.y
    tall_logo_top = win.getControl(ctx.homewindow.HERO_LOGO).y

    short = _look_hero(ctx, 'IMDb 7.4\n2025', plot='Short.')
    ctx.homewindow.paint_hero(short)
    # Every non-empty plot reserves the same scrolling box.
    assert short.getControl(ctx.homewindow.HERO_LOGO).y == tall_logo_top
    assert short.getControl(ctx.homewindow.HERO_PLOT).height == 122


def test_look_facts_start_at_the_margin_without_stars(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    monkeypatch.setattr(ctx.homewindow, 'get_store', lambda: _FakeStore(auth=None))
    from lib.ui import look

    win = _look_hero(ctx, '2025\n1h 52m', runtime='')
    ctx.homewindow.paint_hero(win)
    assert win.getControl(look.HERO_STARS).image == ''
    facts = win.getControl(look.HERO_FACTS)
    assert facts.label == '2025  •  1h 52m' and facts.x == look._STACK['left']
    assert win.getControl(look.HERO_SCORES).visible is False


def test_poster_items_carry_the_runtime_in_minutes(load_homewindow):
    ctx = load_homewindow()
    item = ctx.homewindow._poster_item({'id': 'tt1', 'type': 'movie', 'name': 'X', 'runtime': '112 min'})
    assert item.getProperty('hero_runtime') == '112'
    assert ctx.homewindow._poster_item({'id': 'tt2', 'name': 'Y'}).getProperty('hero_runtime') == ''


def test_look_home_builds_its_rows(load_homewindow, monkeypatch):
    """The whole build runs against the Home file: every control Home
    touches exists there (the fake raises on an unknown id)."""
    ctx = load_homewindow()
    store = _FakeStore(auth={'authKey': 'k', 'user': {'email': 'a@b.c'}})
    monkeypatch.setattr(ctx.homewindow, 'get_store', lambda: store)
    monkeypatch.setattr(ctx.homewindow, '_one_catalog_page', lambda *args: [])
    _patch_library(monkeypatch, ctx, _THREE)
    win = ctx.homewindow.HomeWindow('HomeWindow.xml', '/addon/path', 'Default', '1080i')
    win.onInit()
    ctx.homewindow._switch_tab(win, 'mystuff')
    sids = [i.getProperty('sid') for i in win.getControl(ctx.homewindow.LIBRARY_SHELF).items]
    assert sids[:3] == ['tt1', 'tt2', 'tt3']


# --- switching looks ---------------------------------------------------------





def test_every_home_card_has_a_mirror_image():
    """Each poster and wide card is drawn twice: the art, and the same
    art flipped under it with the fade over it."""
    for name in ('HomeWindow.xml',):
        with open(os.path.join(_SKIN, name), encoding='utf-8') as handle:
            text = handle.read()
        assert 'flipy="true"' in text, name
        assert text.count('look_reflect_fade.png') >= text.count('flipy="true"') // 2, name




def test_an_uncached_logo_is_asked_about_and_shown_once_its_host_answers(load_homewindow, monkeypatch):
    """rev17 showed a logo only when Kodi had already drawn it, so most
    titles showed text. Now the host is asked, and a yes shows the logo."""
    ctx = load_homewindow()
    import lib.ui.logocheck as logocheck

    ctx.env.cached_textures = set()  # Kodi has drawn nothing
    url = 'https://img/logo.png'
    assert ctx.homewindow._logo_known_good(url) is False
    assert url in logocheck.asked

    logocheck.remember(url, True)
    assert ctx.homewindow._logo_known_good(url) is True


# --- Continue reads like every other row ----------------------------------------

def test_continue_cards_fetch_their_meta_and_fill_the_hero(load_homewindow):
    ctx = load_homewindow()
    hw = ctx.homewindow
    import xbmcgui

    # Not an imdb id: no metahub backdrop, so the fetched one fills the card.
    item = hw._continue_item({'id': 'kitsu:1', 'type': 'movie', 'name': 'Dune', 'poster': 'p.jpg'})
    assert item.getProperty('needs_meta') == '1'
    win = hw.HomeWindow('HomeWindow.xml', '/addon/path', 'Default', '1080i')
    win.getControl(hw.CONTINUE_SHELF).addItems([item])
    win.getControl(hw.LIBRARY_SHELF).addItems([xbmcgui.ListItem('other')])
    win._library_meta_ready = [('movie', 'kitsu:1', {
        'id': 'kitsu:1', 'type': 'movie', 'name': 'Dune: Part One', 'description': 'A desert.',
        'background': 'https://img/b.jpg',
    })]

    hw._apply_library_meta(win)

    assert item.getProperty('hero_plot') == 'A desert.'
    assert item.getProperty('land') == 'https://img/b.jpg'
    assert item.getLabel() == 'Dune'  # the card keeps the name it was started as
    assert item.getProperty('meta_state') == 'done'


def test_a_continue_episode_takes_the_shows_hero_not_its_still(load_homewindow):
    ctx = load_homewindow()
    hw = ctx.homewindow
    import xbmcgui

    from lib.ui.urlutil import sized_image

    item = hw._continue_item({
        'id': 'tt0944947', 'type': 'series', 'name': 'Game of Thrones',
        'poster': 'https://img/still.jpg', 'description': 'already here',
        'video_id': 'tt0944947:1:3',
    })
    # An episode always asks for the show, and does not blow its
    # record's picture up to the hero while it waits.
    assert item.getProperty('needs_meta') == '1'
    assert item.getProperty('fanart') == ''
    assert item.getProperty('hero_fill') == ''
    assert 'tt0944947/1/3' in item.getProperty('land')  # the card keeps the still

    win = hw.HomeWindow('HomeWindow.xml', '/addon/path', 'Default', '1080i')
    win.getControl(hw.CONTINUE_SHELF).addItems([item])
    win.getControl(hw.LIBRARY_SHELF).addItems([xbmcgui.ListItem('other')])
    win._library_meta_ready = [('series', 'tt0944947', {
        'id': 'tt0944947', 'type': 'series', 'name': 'Game of Thrones',
        'description': 'Nine noble families.', 'background': 'https://img/got_bg.jpg',
        'logo': 'https://img/got_logo.png', 'releaseInfo': '2011-2019',
        'videos': [{'id': 'tt0944947:1:3', 'season': 1, 'episode': 3,
                    'title': 'Lord Snow', 'overview': 'Jon impresses Tyrion.'}],
    })]
    hw._apply_library_meta(win)

    assert item.getProperty('fanart') == sized_image('https://img/got_bg.jpg', 'backdrop')
    assert item.getProperty('hero_fill') == ''
    assert item.getProperty('hero_logo') == 'https://img/got_logo.png'
    assert item.getProperty('hero_title') == 'Game of Thrones'
    lines = item.getProperty('hero_meta').split('\n')
    assert lines[0] == 'S1E3 \u00b7 Lord Snow'
    assert '2011' in item.getProperty('hero_meta')
    assert item.getProperty('hero_plot') == 'Jon impresses Tyrion.'
    assert 'tt0944947/1/3' in item.getProperty('land')


def test_episode_label_for_reads_the_show_videos(load_homewindow):
    hw = load_homewindow().homewindow
    meta = {'videos': [{'id': 'tt1:2:5', 'name': 'Five', 'description': 'Plot.'}]}
    assert hw.episode_label_for(meta, 'tt1:2:5') == ('S2E5 \u00b7 Five', 'Plot.')
    assert hw.episode_label_for(meta, 'tt1:2:6') == ('S2E6', '')
    assert hw.episode_label_for(meta, 'tt1') == ('', '')
    assert hw.episode_label_for(meta, None) == ('', '')


def test_a_failed_meta_fetch_is_tried_again_later(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    hw = ctx.homewindow
    item = hw._poster_item({'id': 'tt1', 'type': 'movie', 'name': 'Dune'}, sparse=True)
    win = hw.HomeWindow('HomeWindow.xml', '/addon/path', 'Default', '1080i')
    win.getControl(hw.LIBRARY_SHELF).addItems([item])
    win._library_meta_ready = [('movie', 'tt1', None)]
    hw._apply_library_meta(win)
    assert item.getProperty('meta_state').startswith('failed:')

    started = []
    monkeypatch.setattr(hw.threading, 'Thread', lambda **kw: type('T', (), {'start': lambda self: started.append(1)})())
    hw._schedule_library_meta(win, item)
    assert started == []  # too soon
    import time as _time
    item.setProperty('meta_state', 'failed:%d' % int(_time.time() - 60))
    hw._schedule_library_meta(win, item)
    assert started == [1]


def _home_menu(ctx, monkeypatch, pick):
    import lib.ui.dependencies as dependencies
    import lib.ui.dialogs as dialogs

    class _Store:
        def get_auth(self):
            return None

    shown = []
    opened = []
    monkeypatch.setattr(dependencies, 'get_store', lambda: _Store())
    monkeypatch.setattr(dialogs, 'choose', lambda heading, rows: shown.append(rows) or pick)
    monkeypatch.setattr(ctx.homewindow, '_open_child', lambda window, opener: opened.append(opener) or False)
    return shown, opened


def test_a_series_card_on_continue_offers_go_to_series(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    hw = ctx.homewindow
    shown, opened = _home_menu(ctx, monkeypatch, 0)
    item = hw._continue_item({'id': 'tt9253284', 'type': 'series', 'name': 'Andor', 'video_id': 'tt9253284:1:3'})
    win = hw.HomeWindow('HomeWindow.xml', '/addon/path', 'Default', '1080i')
    hw._offer_library(win, item, hw.CONTINUE_SHELF)
    assert shown == [['STR30514', 'STR30517', 'STR30499']]
    assert len(opened) == 1

    import lib.ui.detailwindow as detailwindow
    pages = []
    monkeypatch.setattr(detailwindow, 'open_detail', lambda stype, sid, **kw: pages.append((stype, sid, kw)) or False)
    opened[0]()
    # The show's page, without landing on the episode the card resumes.
    assert pages == [('series', 'tt9253284', {})]


def test_a_movie_on_continue_or_a_series_elsewhere_has_no_go_to_series(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    hw = ctx.homewindow
    shown, opened = _home_menu(ctx, monkeypatch, -1)
    win = hw.HomeWindow('HomeWindow.xml', '/addon/path', 'Default', '1080i')
    movie = hw._continue_item({'id': 'tt1', 'type': 'movie', 'name': 'Dune'})
    hw._offer_library(win, movie, hw.CONTINUE_SHELF)
    series = hw._poster_item({'id': 'tt2', 'type': 'series', 'name': 'Andor'})
    hw._offer_library(win, series, hw.LIBRARY_SHELF)
    assert shown == [['STR30517', 'STR30499'], ['STR30499']]
    assert opened == []
