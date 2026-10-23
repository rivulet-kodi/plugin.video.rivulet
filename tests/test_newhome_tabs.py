"""Home's tab, slot, paging and menu contract against the shipped skin."""
import threading
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from lib import homelayout
from tests.test_newhome import _FakeStore
from tests.test_newhome import load_homewindow as load_homewindow


def metas(start=0, count=30, stype='movie'):
    return [{'id': 'tt%d' % n, 'type': stype, 'name': 'Title %d' % n, 'poster': 'p'}
            for n in range(start, start + count)]


def addons():
    return [{'transportUrl': 'https://addon/manifest.json', 'manifest': {'name': 'Addon', 'catalogs': [
        {'type': 'movie', 'id': 'movies', 'name': 'New movies', 'extra': [{'name': 'skip'}]},
        {'type': 'series', 'id': 'shows', 'name': 'New shows', 'extra': [{'name': 'skip'}]},
        {'type': 'anime', 'id': 'anime', 'name': 'Anime'},
        {'type': 'channel', 'id': 'channels', 'name': 'Channels'},
    ]}}]


def build(ctx, monkeypatch, layout=None, installed=None, progress=None, library=None, auth=None):
    hw = ctx.newhome
    store = _FakeStore(auth=auth, addons=addons() if installed is None else installed,
                       progress_entries=progress or [])
    store.get_home_layout = lambda: layout or homelayout.default_layout()
    calls = []

    def first(_transport, _manifest, catalog, extra=None):
        calls.append(catalog['id'])
        if catalog['type'] == 'anime':
            return metas(100, 24) + metas(200, 24, 'anime')
        return metas(0, 30, catalog['type'])

    monkeypatch.setattr(hw, 'get_store', lambda: store)
    monkeypatch.setattr(hw, '_one_catalog_page', first)
    monkeypatch.setattr(hw, '_fetch_library', lambda *_: list(library or []))
    monkeypatch.setattr(hw, '_fetch_library_meta', lambda *_: None)
    monkeypatch.setattr(ctx.mystuff, '_enrich', lambda items, **kw: [item.update(name='Played', poster='p') for item in items])
    monkeypatch.setattr(hw, '_warm_shelf_posters', lambda *_: None)
    win = hw.NewHomeWindow('NewHomeWindow.xml', '/addon/path', 'Default', '1080i')
    win.onInit()
    return win, store, calls


def chip_actions(ctx, win):
    return [item.getProperty('action') for item in win.getControl(ctx.newhome.BROWSE_LIST).items]


@pytest.mark.parametrize('ctype, expected', [
    ('movie', ['home', 'mystuff', 'movies']), ('TV', ['home', 'mystuff', 'series']),
    ('anime.movie', ['home', 'mystuff', 'anime']), ('anime.series', ['home', 'mystuff', 'anime']),
    ('channel', ['home', 'mystuff']),
])
def test_chip_order_visibility_and_unsupported_types(load_homewindow, ctype, expected):
    ctx = load_homewindow()
    installed = [{'transportUrl': 't', 'manifest': {'catalogs': [{'type': ctype, 'id': 'top'}]}}]
    assert [i.getProperty('action') for i in ctx.newhome._menu_items(installed)] == expected
    assert [i.getProperty('action') for i in ctx.newhome._menu_items([])] == ['home', 'mystuff']


def test_chip_hero_active_property_and_arrow_keys(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    import xbmcgui
    hw = ctx.newhome
    win, _, calls = build(ctx, monkeypatch)
    chips = win.getControl(hw.BROWSE_LIST)
    assert chip_actions(ctx, win) == ['home', 'mystuff', 'movies', 'series', 'anime']
    assert chips.items[0].getProperty('hero_plot') == ''
    assert chips.items[1].getProperty('hero_plot') == 'STR30232'
    chips.selectItem(2)
    win.setFocusId(hw.BROWSE_LIST)
    for key in (1, 2):
        win.onAction(xbmcgui.Action(key))
        assert win._active_tab == 'home'
    win.onClick(hw.BROWSE_LIST)
    assert win._active_tab == 'movies'
    assert win.getFocusId() == hw.BROWSE_LIST
    assert chips.getSelectedItem().getProperty('active') == '1'
    assert chips.items[0].getProperty('active') == ''
    assert calls == ['movies', 'shows', 'movies']
    win.onAction(xbmcgui.Action(4))
    assert win.getFocusId() == 30041
    assert chips.getSelectedPosition() == 2


@pytest.mark.parametrize('tab, ids, fetched', [
    ('home', ['continue', 'catalog', 'catalog'], ['movies', 'shows']),
    ('mystuff', ['library', 'library'], []),
    ('movies', ['continue', 'catalog'], ['movies']),
    ('series', ['continue', 'catalog'], ['shows']),
    ('anime', ['continue', 'catalog', 'catalog'], ['anime', 'anime']),
])
def test_default_tabs_only_fetch_the_active_plan(load_homewindow, monkeypatch, tab, ids, fetched):
    ctx = load_homewindow()
    hw = ctx.newhome
    win, _, calls = build(ctx, monkeypatch)
    calls.clear()
    if tab != 'home':
        hw._switch_tab(win, tab)
    else:
        # Home's first load is checked separately; no work on reselection.
        calls[:] = ['movies', 'shows']
    assert [state['kind'] for state in win._rows.values()] == ids
    assert sorted(calls) == sorted(fetched)
    assert all(panel <= 30052 for panel in win._rows)
    assert all(not win.getProperty('shelf_slot%d' % i) for i in range(len(ids), 12))


def test_twelve_poster_slots_use_the_new_ids_and_number_visible_rows(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    layout = homelayout.default_layout()
    layout['tabs']['home'] = ['rest:movie']
    installed = addons()
    installed[0]['manifest']['catalogs'] = [{'id': 'c%d' % i, 'type': 'movie', 'name': 'Catalog %d' % i}
                                          for i in range(20)]
    win, _, calls = build(ctx, monkeypatch, layout=layout, installed=installed)
    assert len(win._rows) == 12
    assert len(calls) == 12
    assert list(win._rows) == list(range(30041, 30053))
    assert win.getControl(30160).label == '[B]CATALOG 6[/B]'
    assert win.getControl(30176).label == '12 / 12'
    assert win.getProperty('shelf_slot11') == '1'


def test_back_in_a_scrolled_row_resets_only_that_row(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    import xbmcgui
    win, _, _ = build(ctx, monkeypatch)
    win.getControl(30041).selectItem(9)
    win.getControl(30042).selectItem(4)
    win.setFocusId(30041)
    win.onAction(xbmcgui.Action(92))
    assert win.getFocusId() == 30041
    assert win.getControl(30041).getSelectedPosition() == 0
    assert win._rows[30041]['position'] == 0
    assert win.getControl(30042).getSelectedPosition() == 4


def test_back_at_row_start_focuses_active_chip(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    win, _, _ = build(ctx, monkeypatch)
    ctx.newhome._switch_tab(win, 'movies')
    win.setFocusId(30041)
    ctx.newhome._back(win)
    assert win.getFocusId() == 30010
    assert win.getControl(30010).getSelectedItem().getProperty('action') == 'movies'
    assert not win.closed


def test_back_from_nonhome_chip_switches_home(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    win, _, _ = build(ctx, monkeypatch)
    ctx.newhome._switch_tab(win, 'mystuff')
    ctx.newhome._back(win)
    assert win._active_tab == 'home'
    assert win.getFocusId() == 30010
    assert win.getControl(30010).getSelectedPosition() == 0


def test_back_from_home_chip_closes(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    win, _, _ = build(ctx, monkeypatch)
    win.setFocusId(30010)
    ctx.newhome._back(win)
    assert win.closed


def test_up_down_and_append_retain_native_position_without_reset(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    import xbmcgui
    hw = ctx.newhome
    win, _, _ = build(ctx, monkeypatch)
    row = win.getControl(30041)
    row.selectItem(8)
    reset_calls = row.reset_calls
    win.setFocusId(30042)
    win.onAction(xbmcgui.Action(4))
    win.setFocusId(30041)
    win.onAction(xbmcgui.Action(3))
    old_items = win._rows[30041]['items']
    hw._append_to_shelf(win, 30041, metas(30, 6))
    assert row.reset_calls == reset_calls
    assert row.getSelectedPosition() == 8
    assert old_items is win._rows[30041]['items']
    assert len(old_items) == row.size() == 30


def test_tab_snapshot_restores_all_loaded_posters_and_positions_without_http(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    hw = ctx.newhome
    win, _, calls = build(ctx, monkeypatch)
    hw._append_to_shelf(win, 30041, metas(30, 100))
    win.getControl(30041).selectItem(90)
    win.getControl(30042).selectItem(11)
    win.setFocusId(30041)
    hw._switch_tab(win, 'mystuff')
    assert win._tab_snapshots['home'][1] == 30041
    count = len(calls)
    hw._switch_tab(win, 'home')
    assert len(calls) == count
    assert win.getControl(30041).size() == 124
    assert win.getControl(30041).getSelectedPosition() == 90
    assert win.getControl(30042).getSelectedPosition() == 11
    # Returning keeps chip focus; Down enters the first visible row.
    assert win.getFocusId() == 30010
    assert win.getControl(30010).getSelectedItem().getProperty('action') == 'home'
    import xbmcgui
    win.onAction(xbmcgui.Action(4))
    assert win.getFocusId() == 30041
    assert win.getControl(30041).getSelectedPosition() == 90


def test_active_tab_survives_playback_and_settings_generation_clears_snapshots(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    win, _, _ = build(ctx, monkeypatch)
    hw = ctx.newhome
    hw._switch_tab(win, 'mystuff')
    win.onInit()
    assert win._active_tab == 'mystuff'
    hw._switch_tab(win, 'home')
    ctx.compat.ADDON.setSetting('home_layout_generation', 'changed')
    hw._after_child(win)
    assert win._tab_snapshots == {}


def test_signed_out_signin_only_on_first_library_row_and_empty_notices(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    win, _, _ = build(ctx, monkeypatch)
    ctx.newhome._switch_tab(win, 'mystuff')
    assert win.getControl(30041).items[0].getProperty('action') == 'signin'
    assert win.getControl(30042).size() == 0
    assert win.getProperty('shelf_slot1') == ''
    win, _, _ = build(ctx, monkeypatch, auth={'authKey': 'k'})
    ctx.newhome._switch_tab(win, 'mystuff')
    assert win.getControl(30041).items[0].getLabel() == 'STR30426'
    assert win.getControl(30042).items[0].getLabel() == 'STR30426'


def test_library_split_keeps_newest_first_and_lazily_extends(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    entries = [{'_id': 'tt%d' % i, 'name': 'T', 'type': 'movie', '_ctime': '%04d' % i}
               for i in range(60)] + [{'_id': 's', 'type': 'series', 'name': 'Show'}]
    win, _, _ = build(ctx, monkeypatch, auth={'authKey': 'k'}, library=entries)
    hw = ctx.newhome
    hw._switch_tab(win, 'mystuff')
    assert win.getControl(30041).size() == 24
    assert win.getControl(30041).items[0].getProperty('sid') == 'tt59'
    assert win.getControl(30042).items[0].getProperty('sid') == 's'
    win.setFocusId(30041)
    win.getControl(30041).selectItem(19)
    hw._maybe_extend_focused_shelf(win)
    hw._join_shelf_extends(win)
    hw._apply_shelf_extends(win)
    assert win.getControl(30041).size() == 48
    assert win.getControl(30041).getSelectedPosition() == 19


def test_prefetch_threshold_is_exactly_five_and_append_keeps_the_selected_poster(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    win, _, _ = build(ctx, monkeypatch)
    hw = ctx.newhome
    win.setFocusId(30041)
    control = win.getControl(30041)
    control.selectItem(18)
    hw._maybe_extend_focused_shelf(win)
    assert not getattr(win, '_shelf_inflight', {})
    control.selectItem(19)
    selected = control.getSelectedItem()
    resets = control.reset_calls
    hw._maybe_extend_focused_shelf(win)
    hw._join_shelf_extends(win)
    hw._apply_shelf_extends(win)
    assert control.size() == 30
    assert control.getSelectedItem() is selected
    assert control.reset_calls == resets


@pytest.mark.parametrize('end', [[], metas(0, 24)])
def test_pages_dedupe_across_the_whole_row_and_stop_at_empty_or_all_seen(load_homewindow, monkeypatch, end):
    ctx = load_homewindow()
    from lib.ui import catalogpages, views
    catalogpages.clear_later_pages()
    win, _, _ = build(ctx, monkeypatch)
    hw = ctx.newhome
    state = win._rows[30041]
    state['pending'] = []
    state['served'] = 24
    state['seen_ids'] = set(m['id'] for m in metas(0, 24))
    offsets = []

    def fetch(_t, _type, _id, extra=None):
        skip = dict(extra)['skip']
        offsets.append(skip)
        return metas(20, 10) if skip == 24 else end

    monkeypatch.setattr(views, '_fetch_catalog', fetch)
    batch = hw._pull_more(state, 24)
    assert [m['id'] for m in batch] == ['tt%d' % i for i in range(24, 30)]
    assert state['exhausted']
    assert hw._pull_more(state, 24) == []
    assert offsets == [24, 34]


def test_split_anime_pulls_at_most_five_pages_per_step(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    from lib.ui import catalogpages, views
    catalogpages.clear_later_pages()
    descriptor = homelayout.installed_catalogs(addons())[-2]
    descriptor['catalog']['extra'] = [{'name': 'skip'}]
    fetched = []
    monkeypatch.setattr(ctx.newhome, '_one_catalog_page', lambda *_: metas(0, 1, 'anime'))

    def fetch(_t, _type, _id, extra=None):
        offset = dict(extra)['skip']
        fetched.append(offset)
        return metas(offset, 1, 'anime' if offset < 6 else 'movie')

    monkeypatch.setattr(views, '_fetch_catalog', fetch)
    state = ctx.newhome._open_shelf(descriptor)
    assert state['metas'] == []
    assert fetched == []  # First paint is only page one, even when filtered empty.
    assert not state['exhausted']
    result = ctx.newhome._pull_more(state, 24)
    assert fetched == [1, 2, 3, 4, 5]
    assert result == []
    result = ctx.newhome._pull_more(state, 24)
    assert len(fetched) == 10
    assert [m['id'] for m in result] == ['tt6', 'tt7', 'tt8', 'tt9', 'tt10']


def test_later_page_lru_is_reused_bounded_and_cleared(load_homewindow, monkeypatch):
    load_homewindow()
    from lib.ui import cacherefresh, catalogpages, views
    catalogpages.clear_later_pages()
    calls = []
    monkeypatch.setattr(views, '_fetch_catalog', lambda *args, **kwargs: calls.append(kwargs['extra']) or metas(0, 1))
    for _ in range(2):
        assert catalogpages.cached_later_page('a', 'movie', 'top', [('skip', 24)]) == metas(0, 1)
    assert len(calls) == 1
    for n in range(200):
        catalogpages.cached_later_page('a', 'movie', 'top', [('skip', 25 + n)])
    assert len(catalogpages._later_pages) == 200
    assert ('a', 'movie', 'top', (), 24) not in catalogpages._later_pages
    cacherefresh.clear_memory()
    assert not catalogpages._later_pages


def test_page_safety_ceiling_stops_at_2000_and_logs(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    win, _, _ = build(ctx, monkeypatch)
    hw = ctx.newhome
    state = win._rows[30041]
    state['metas'] = metas(0, 1990)
    state['pending'] = metas(1990, 40)
    win.setFocusId(30041)
    win.getControl(30041).selectItem(23)
    hw._maybe_extend_focused_shelf(win)
    hw._join_shelf_extends(win)
    hw._apply_shelf_extends(win)
    assert len(state['metas']) == 2000
    assert state['exhausted']
    assert any('2000-item safety ceiling' in msg for msg, _ in ctx.env.log_calls)


@pytest.mark.parametrize('ready_before_switch', [True, False])
def test_tab_switch_discards_old_arrivals_and_already_ready_batches(load_homewindow, monkeypatch, ready_before_switch):
    ctx = load_homewindow()
    win, _, _ = build(ctx, monkeypatch)
    hw = ctx.newhome
    started, release = threading.Event(), threading.Event()

    def pull(state, room):
        started.set()
        release.wait(2)
        state['pending'] = []
        return metas(1000, 2)

    monkeypatch.setattr(hw, '_pull_more', pull)
    original = win._rows[30041]
    pending = list(original['pending'])
    hw._schedule_shelf_extend(win, 30041, 24)
    assert started.wait(1)
    if ready_before_switch:
        release.set()
        hw._join_shelf_extends(win)
        assert win._shelf_ready
    hw._switch_tab(win, 'mystuff')
    release.set()
    hw._join_shelf_extends(win)
    hw._apply_shelf_extends(win)
    assert original['pending'] == pending
    assert all(m['id'] != 'tt1000' for m in original['metas'])
    assert not win._shelf_ready


def test_page_worker_bound_one_per_row_two_overall_and_newest_queued_first(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    layout = homelayout.default_layout()
    layout['tabs']['home'] = ['new:movie', 'new:series', 'new:anime-movie', 'new:anime-series']
    win, _, _ = build(ctx, monkeypatch, layout=layout)
    hw = ctx.newhome
    started, release = threading.Event(), threading.Event()
    lock = threading.Lock()
    active, order = [], []
    peak = [0]

    def pull(state, room):
        with lock:
            order.append(state['list_id'])
            active.append(state)
            peak[0] = max(peak[0], len(active))
            if len(active) == 2:
                started.set()
        release.wait(2)
        with lock:
            active.remove(state)
        return []

    monkeypatch.setattr(hw, '_pull_more', pull)
    for panel in (30041, 30042):
        hw._schedule_shelf_extend(win, panel, 24)
        hw._schedule_shelf_extend(win, panel, 24)
    assert started.wait(1)
    hw._schedule_shelf_extend(win, 30043, 24)
    hw._schedule_shelf_extend(win, 30044, 24)
    release.set()
    hw._join_shelf_extends(win)
    hw._apply_shelf_extends(win)
    assert peak == [2]
    assert len(order) == 4
    assert order[2] == win._rows[30044]['list_id']
    assert win._shelf_inflight == {}


def test_append_warms_just_the_appended_batch(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    from lib.ui import posterwarm
    win, _, _ = build(ctx, monkeypatch)
    calls = []
    monkeypatch.setattr(posterwarm, 'request_warm', lambda w, urls: calls.append(urls))
    # Restore the real warming helper hidden by the build fixture.
    from importlib import reload
    reload(ctx.newhome)
    ctx.newhome._append_to_shelf(win, 30041, [{'id': 'new', 'type': 'movie', 'poster': 'https://image/poster.jpg'}])
    assert calls == [['https://image/poster.jpg']]


def test_menu_browse_catalog_only_and_remove_continue_confirmation(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    from lib.ui import dialogs, libraryaction
    progress = [{'type': 'movie', 'id': 'tt9', 'name': 'Nine', 'poster': 'p', 'position_ms': 20000, 'duration_ms': 100000}]
    win, store, _ = build(ctx, monkeypatch, progress=progress)
    hw = ctx.newhome
    menus = []
    monkeypatch.setattr(libraryaction, 'poster_menu', lambda meta, extra: menus.append(extra))
    hw._offer_library(win, win.getControl(30041).items[0], 30041)
    assert [label for label, _ in menus[-1]] == ['STR30537']
    hw._offer_library(win, win.getControl(30040).items[0], 30040)
    assert [label for label, _ in menus[-1]] == ['STR30517']
    calls = []
    store.remove_progress_title = lambda stype, sid: (calls.append((stype, sid)), store._progress_entries.clear())
    monkeypatch.setattr(dialogs, 'confirm', lambda *args: False)
    menus[-1][0][1]()
    assert not calls
    resets = win.getControl(30041).reset_calls
    monkeypatch.setattr(dialogs, 'confirm', lambda *args: True)
    menus[-1][0][1]()
    assert calls == [('movie', 'tt9')]
    assert win.getControl(30040).size() == 0
    assert win.getControl(30041).reset_calls == resets


def test_browse_full_catalog_passes_the_exact_row_descriptor_and_filter(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    win, _, _ = build(ctx, monkeypatch)
    calls = []
    monkeypatch.setattr(ctx.catalogpicker, 'open_single_catalog', lambda *args, **kw: calls.append((args, kw)) or True)
    for tab, variant in (('home', ''), ('anime', 'movie')):
        if tab != 'home':
            ctx.newhome._switch_tab(win, tab)
        state = win._rows[30041]
        state['extra'] = [('genre', 'Drama')]
        descriptor = state['descriptor']
        ctx.newhome._browse_catalog(win, state)
        assert calls[-1] == ((descriptor['transport'], descriptor['manifest'], descriptor['catalog']),
                             {'extra': [('genre', 'Drama')], 'meta_filter': variant})
        assert not win.closed

def test_skin_slot_copies_and_active_pills_are_the_existing_primitives():
    root = ET.parse(Path(__file__).parents[1] / 'resources/skins/Default/1080i/NewHomeWindow.xml').getroot()
    controls = {int(c.attrib['id']): c for c in root.iter('control') if 'id' in c.attrib}
    chips = controls[30010]
    assert chips.findtext('left') == '80'
    for layout in ('itemlayout', 'focusedlayout'):
        images = chips.find(layout).findall("control[@type='image']")
        active = next(image for image in images if image.find('texture').attrib['colordiffuse'] == '24FFFFFF')
        assert active.find('texture').attrib['border'] == '28'
        assert 'ListItem.Property(active)' in active.findtext('visible')
    assert controls[30040].findtext('onup') == '30010'
    assert controls[30041].findtext('onup') == '30040'
    assert controls[30052].findtext('ondown') == '30052'
    original = ET.tostring(controls[30045].find('itemlayout'))
    for panel in range(30046, 30053):
        assert ET.tostring(controls[panel].find('itemlayout')) == original


def test_first_load_is_bounded_to_four_workers_and_keeps_plan_order(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    hw = ctx.newhome
    layout = homelayout.default_layout()
    layout['tabs']['home'] = ['continue', 'rest:movie']
    installed = addons()
    installed[0]['manifest']['catalogs'] = [{'id': 'c%d' % i, 'type': 'movie'} for i in range(9)]
    store = _FakeStore(auth={'authKey': 'k'}, addons=installed)
    win = hw.NewHomeWindow('NewHomeWindow.xml', '/addon/path', 'Default', '1080i')
    win._active_tab = 'home'
    win._layout = layout
    win._catalogs = homelayout.installed_catalogs(installed)
    barrier = threading.Barrier(4)
    lock = threading.Lock()
    active, peak = [0], [0]

    def work(value):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        barrier.wait(timeout=2)
        with lock:
            active[0] -= 1
        return value

    # Nine jobs: one library plus eight rest catalogs. Allow only the first
    # eight into the barrier; the final one must not wait for another batch.
    counter = [0]

    def task(value):
        with lock:
            counter[0] += 1
            number = counter[0]
        return work(value) if number <= 8 else value

    monkeypatch.setattr(hw, '_fetch_library', lambda *_: task([]))
    monkeypatch.setattr(hw, '_open_shelf', lambda descriptor: task({'metas': [{'id': descriptor['catalog']['id']}]}))
    rows = hw._load_rows(win, store, installed, store.get_auth())
    assert peak == [4]
    assert [state['metas'][0]['id'] for state in rows[1:]] == ['c%d' % i for i in range(1, 9)]


def test_catalog_worker_failure_and_cancelled_load_leave_home_available(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    win, store, _ = build(ctx, monkeypatch)
    hw = ctx.newhome
    from lib.ui import uicommon

    def fail(_descriptor):
        raise OSError('https://u:secret@host/path?token=hidden')

    monkeypatch.setattr(hw, '_open_shelf', fail)
    rows = hw._load_rows(win, store, store.get_enabled_addons(), None)
    assert all(not state['items'] for state in rows)
    assert all('secret' not in msg and 'hidden' not in msg for msg, _ in ctx.env.log_calls)
    monkeypatch.setattr(uicommon, 'run_cancellable', lambda *_: uicommon.CANCELLED)
    assert hw._load_rows(win, store, store.get_enabled_addons(), None) == []


def test_visiting_catalog_only_tab_does_not_cache_an_unfetched_library(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    layout = homelayout.default_layout()
    layout['tabs']['home'] = ['new:movie']
    win, _, _ = build(ctx, monkeypatch, layout=layout, auth={'authKey': 'k'})
    assert getattr(win, '_library_entries', None) is None
    fetched = []
    monkeypatch.setattr(ctx.newhome, '_fetch_library', lambda *_: fetched.append(True) or [])
    ctx.newhome._switch_tab(win, 'mystuff')
    assert fetched == [True]


def test_personal_refresh_retains_catalog_snapshots_and_library_page_positions(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    hw = ctx.newhome
    entries = [{'_id': 'tt%d' % i, 'name': 'T', 'type': 'movie', '_ctime': '%04d' % i} for i in range(90)]
    win, store, calls = build(ctx, monkeypatch, auth={'authKey': 'k'}, library=entries)
    hw._switch_tab(win, 'mystuff')
    hw._append_to_shelf(win, 30041, metas(1000, 40), sparse=True)
    win.getControl(30041).selectItem(50)
    win.setFocusId(30041)
    hw._refresh_saved_rows(win, store)
    assert win.getControl(30041).getSelectedPosition() == 50
    assert win.getControl(30041).size() == 64
    count = len(calls)
    hw._switch_tab(win, 'home')
    assert len(calls) == count
    hw._switch_tab(win, 'mystuff')
    assert win.getControl(30041).getSelectedPosition() == 50


def test_remove_continue_keeps_neighbour_and_failed_save_keeps_row(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    from lib.ui import dialogs
    progress = [{'type': 'movie', 'id': 'tt%d' % i, 'position_ms': 20, 'duration_ms': 100,
                 'updated_at': str(i)} for i in range(3)]
    win, store, _ = build(ctx, monkeypatch, progress=progress)
    hw = ctx.newhome
    monkeypatch.setattr(dialogs, 'confirm', lambda *_: True)
    control = win.getControl(30040)
    control.selectItem(1)
    win.setFocusId(30040)
    picked = control.getSelectedItem()

    def fail(*_):
        raise OSError('disk full')

    store.remove_progress_title = fail
    hw._remove_continue(win, picked)
    assert control.size() == 3 and control.getSelectedPosition() == 1
    store.remove_progress_title = lambda stype, sid: store._progress_entries.__setitem__(
        slice(None), [entry for entry in store._progress_entries if entry['id'] != sid])
    hw._remove_continue(win, picked)
    assert control.size() == 2 and control.getSelectedPosition() == 1
    assert win.getFocusId() == 30040


def test_remove_continue_never_writes_to_an_item_from_the_reset_shelf(load_homewindow, monkeypatch):
    """Kodi frees a list's items on reset, and a ListItem that
    getSelectedItem() returned does not keep its item alive: writing to it
    afterwards segfaulted Kodi. The pending library-meta request held such
    an item while the Continue shelf was rebuilt."""
    ctx = load_homewindow()
    from lib.ui import dialogs
    progress = [{'type': 'movie', 'id': 'tt%d' % i, 'position_ms': 20, 'duration_ms': 100,
                 'updated_at': str(i)} for i in range(2)]
    win, store, _ = build(ctx, monkeypatch, progress=progress)
    monkeypatch.setattr(dialogs, 'confirm', lambda *_: True)
    store.remove_progress_title = lambda stype, sid: store._progress_entries.__setitem__(
        slice(None), [entry for entry in store._progress_entries if entry['id'] != sid])

    class FreedItem:
        def setProperty(self, *_):
            raise AssertionError('wrote to an item freed by the shelf reset')

    win.setFocusId(30040)
    picked = win.getControl(30040).getSelectedItem()
    win._library_meta_latest = (FreedItem(), 'movie', picked.getProperty('sid'))
    ctx.newhome._remove_continue(win, picked)
    assert win.getControl(30040).size() == 1



def test_cache_generation_clears_later_pages_before_rebuild(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    from lib.ui import catalogpages
    win, _, _ = build(ctx, monkeypatch)
    catalogpages._later_pages['old'] = []
    ctx.compat.ADDON.setSetting('cache_generation', 'new')
    ctx.newhome._after_child(win)
    assert not catalogpages._later_pages


def test_reopen_signed_out_keeps_only_one_library_signin_card(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    win, _, _ = build(ctx, monkeypatch)
    ctx.newhome._switch_tab(win, 'mystuff')
    win.onInit()
    assert win.getControl(30041).size() == 1
    assert win.getControl(30041).items[0].getProperty('action') == 'signin'
    assert win.getControl(30042).size() == 0
    assert win.getProperty('shelf_slot1') == ''


def test_removed_account_continue_stays_hidden_for_all_variants_until_a_fresh_window(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    from lib.ui import dialogs
    hw = ctx.newhome
    entries = [{'_id': 'tt9', 'name': 'Nine', 'type': 'movie', 'poster': 'p',
                'state': {'timeOffset': 20000, 'duration': 100000}}]
    win, store, _ = build(ctx, monkeypatch, auth={'authKey': 'k'}, library=entries)
    monkeypatch.setattr(dialogs, 'confirm', lambda *_: True)
    removed = []
    store.remove_progress_title = lambda stype, sid: removed.append((stype, sid))
    # Cache the filtered Continue row before removing it from Home.
    hw._switch_tab(win, 'movies')
    assert win.getControl(30040).size() == 1
    hw._switch_tab(win, 'home')
    hw._remove_continue(win, win.getControl(30040).items[0])
    assert removed == [('movie', 'tt9')]
    assert win._continue_suppressed == {'tt9'}
    assert win.getControl(30040).size() == 0
    assert entries[0]['state']['timeOffset'] == 20000  # No account mutation.
    hw._refresh_saved_rows(win, store)
    assert win.getControl(30040).size() == 0
    hw._switch_tab(win, 'movies')
    assert win.getControl(30040).size() == 0
    # A never-visited tab exercises new row construction with suppression.
    layout = homelayout.default_layout()
    layout['tabs']['mystuff'] = ['continue', 'library:movie']
    win._layout = layout
    hw._switch_tab(win, 'mystuff')
    assert win.getControl(30040).size() == 0
    win.onInit()  # Playback reopen reuses this Home instance.
    assert win.getControl(30040).size() == 0
    win.close()
    fresh, _, _ = build(ctx, monkeypatch, auth={'authKey': 'k'}, library=entries)
    assert fresh.getControl(30040).items[0].getProperty('sid') == 'tt9'
    assert not getattr(fresh, '_continue_suppressed', set())


@pytest.mark.parametrize('child_result', [False, True])
def test_addons_button_never_closes_home(load_homewindow, monkeypatch, child_result):
    ctx = load_homewindow()
    win, _, _ = build(ctx, monkeypatch)
    opened = []
    monkeypatch.setattr(ctx.addonswindow, 'open_addons', lambda: opened.append(True) or child_result)
    win.onClick(ctx.newhome.BTN_ADDONS)
    assert opened == [True]
    assert not win.closed


def test_context_key_dispatches_poster_menu_and_refreshes_after_a_library_write(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    import xbmcgui

    from lib.ui import libraryaction
    hw = ctx.newhome
    win, _, _ = build(ctx, monkeypatch, auth={'authKey': 'k'})
    menus, refreshes = [], []
    monkeypatch.setattr(libraryaction, 'poster_menu', lambda meta, extra: menus.append(meta['id']) or 'added')
    monkeypatch.setattr(hw, '_fetch_library', lambda *_: refreshes.append(True) or [])
    win.setFocusId(30041)
    win.onAction(xbmcgui.Action(117))
    assert menus == ['tt0']
    assert refreshes == [True]
    assert win.getFocusId() == 30041
    assert not win.closed
    win.setFocusId(hw.BROWSE_LIST)
    win.onAction(xbmcgui.Action(117))
    assert len(menus) == 1


def test_disk_layout_change_rebuilds_active_and_later_tabs_with_unchanged_settings(load_homewindow, monkeypatch, tmp_path):
    ctx = load_homewindow()
    from lib.store import Store
    hw = ctx.newhome
    win, _, _ = build(ctx, monkeypatch)
    store = Store(str(tmp_path))
    store.get_enabled_addons = addons
    layout = homelayout.default_layout()
    ids = ['cat:https://addon/manifest.json|movie|movies', 'cat:https://addon/manifest.json|series|shows']
    layout['tabs']['home'] = list(ids)
    layout['tabs']['movies'] = list(ids)
    store.set_home_layout(layout)
    monkeypatch.setattr(hw, 'get_store', lambda: store)
    win._build()
    hw._switch_tab(win, 'movies')
    hw._switch_tab(win, 'home')
    assert win._tab_snapshots
    settings = tuple(ctx.compat.ADDON.getSetting(key) for key in hw._ROW_SETTINGS)
    # The Settings action writes in another interpreter; no setting changes
    # reach the Home interpreter in this regression.
    layout['tabs']['home'].reverse()
    layout['tabs']['movies'].reverse()
    Store(str(tmp_path)).set_home_layout(layout)
    rebuilt = []
    real_build = win._build
    monkeypatch.setattr(win, '_build', lambda place=None: rebuilt.append(True) or real_build(place))
    hw._after_child(win)
    assert rebuilt == [True]
    assert win._tab_snapshots == {}
    assert [row['list_id'] for row in win._rows.values()] == ids[::-1]
    assert tuple(ctx.compat.ADDON.getSetting(key) for key in hw._ROW_SETTINGS) == settings
    hw._switch_tab(win, 'movies')
    assert [row['list_id'] for row in win._rows.values()] == ids[::-1]


def test_equivalent_disk_layout_keeps_snapshots_selection_and_focus(load_homewindow, monkeypatch, tmp_path):
    ctx = load_homewindow()
    import json

    from lib.store import Store
    hw = ctx.newhome
    win, _, _ = build(ctx, monkeypatch)
    store = Store(str(tmp_path))
    store.get_enabled_addons = addons
    store.set_home_layout(homelayout.default_layout())
    monkeypatch.setattr(hw, 'get_store', lambda: store)
    win._build()
    hw._switch_tab(win, 'mystuff')
    hw._switch_tab(win, 'home')
    snapshots = win._tab_snapshots
    win.getControl(30041).selectItem(8)
    win.setFocusId(30041)
    # Different JSON whitespace/key order still denotes the same layout.
    layout = store.get_home_layout()
    (tmp_path / 'home_layout.json').write_text(json.dumps({'tabs': layout['tabs'], 'version': 1}))
    rebuilt = []
    monkeypatch.setattr(win, '_build', lambda **kwargs: rebuilt.append(True))
    hw._after_child(win)
    assert rebuilt == []
    assert win._tab_snapshots is snapshots
    assert win.getControl(30041).getSelectedPosition() == 8
    assert win.getFocusId() == 30041


def test_explicit_series_alone_makes_anime_chip_and_toggle_rebuilds(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    hw = ctx.newhome
    layout = homelayout.default_layout()
    layout['tabs']['anime'] = ['cat:https://addon/manifest.json|series|shows']
    win, _, calls = build(ctx, monkeypatch, layout=layout)
    assert 'anime' in chip_actions(ctx, win)
    # Opening Home never follows the inactive Anime plan or recurses into it.
    assert sorted(calls) == ['movies', 'shows']
    calls.clear()
    hw._switch_tab(win, 'anime')
    assert calls == ['shows']
    assert [state['list_id'] for state in win._rows.values()] == layout['tabs']['anime']
    calls.clear()
    ctx.compat.ADDON.setSetting('home_show_anime', 'false')
    hw._after_child(win)
    assert 'anime' not in chip_actions(ctx, win)
    assert win._active_tab == 'home'
    assert [state['kind'] for state in win._rows.values()] == ['continue', 'catalog', 'catalog']
    assert sorted(calls) == ['movies', 'shows']
    assert win._tab_loaded['home'] is True
    assert hw._load_rows(type('Window', (), {'_active_tab': 'anime'})(), None, [], None) == []
    calls.clear()
    hw._switch_tab(win, 'anime')
    assert calls == []
    ctx.compat.ADDON.setSetting('home_show_anime', 'true')
    hw._after_child(win)
    assert 'anime' in chip_actions(ctx, win)


def test_anime_off_keeps_explicit_placement_and_continue_elsewhere(load_homewindow, monkeypatch):
    ctx = load_homewindow(settings={'home_show_anime': 'false'})
    layout = homelayout.default_layout()
    layout['tabs']['series'].append('cat:t|series|cru')
    installed = [{'transportUrl': 't', 'manifest': {'id': 'pw.ers.netflix-catalog', 'catalogs': [
        {'id': 'cru', 'type': 'series', 'name': 'Crunchyroll'}]}}]
    win, _, calls = build(ctx, monkeypatch, layout=layout, installed=installed)
    assert calls == []
    assert 'anime' not in chip_actions(ctx, win)
    assert [row['kind'] for row in win._rows.values()] == ['continue']
    ctx.newhome._switch_tab(win, 'series')
    assert calls == ['cru']
    assert [row['kind'] for row in win._rows.values()] == ['continue', 'catalog']
