"""Home paints cached page one without waiting on a host's later pages."""
import threading
import time

import pytest

from lib import homelayout
from lib.stremio.addons import AddonError
from tests.homehooks import join_shelf_extends
from tests.test_home import _FakeStore
from tests.test_home import load_homewindow as load_homewindow
from tests.test_home_tabs import metas


def prepare(ctx, monkeypatch, count=1, first=None):
    hw = ctx.home
    installed = [{'transportUrl': 'https://user:SECRET@addon.test/private?key=TOKEN', 'manifest': {'catalogs': [
        {'type': 'movie', 'id': 'top%d' % i, 'extra': [{'name': 'skip'}]} for i in range(count)]}}]
    store = _FakeStore(addons=installed)
    layout = homelayout.default_layout()
    layout['tabs']['home'] = [entry['list_id'] for entry in homelayout.installed_catalogs(installed)]
    store.get_home_layout = lambda: layout
    monkeypatch.setattr(hw, 'get_store', lambda: store)
    monkeypatch.setattr(hw, '_fetch_library', lambda *_: [])
    monkeypatch.setattr(hw, '_one_catalog_page', first or (lambda *_: metas(0, 20)))
    monkeypatch.setattr(hw, '_warm_shelf_posters', lambda *_: None)
    win = hw.HomeWindow('HomeWindow.xml', '/addon/path', 'Default', '1080i')
    return win, store


@pytest.mark.parametrize('leave', ['', 'tab', 'close'])
def test_first_paint_ignores_a_thirteen_second_page_then_applies_only_current_result(load_homewindow, monkeypatch, leave):
    ctx = load_homewindow()
    hw = ctx.home
    entered, release = threading.Event(), threading.Event()
    calls = []

    def fetch(*args, extra=None):
        calls.append(dict(extra)['skip'])
        entered.set()
        assert release.wait(13)
        return metas(20, 20)

    monkeypatch.setattr(ctx.views, '_fetch_catalog', fetch)
    win, _ = prepare(ctx, monkeypatch)
    started = time.monotonic()
    try:
        win.onInit()
        assert time.monotonic() - started < 0.3
        state = win._rows[30041]
        assert len(state['items']) == 20
        assert entered.wait(0.5)
        if leave == 'tab':
            hw._switch_tab(win, 'mystuff')
        elif leave == 'close':
            win.close()
    finally:
        release.set()
    join_shelf_extends(win)
    hw._apply_shelf_extends(win)
    assert len(state['items']) == (20 if leave else 24)
    assert calls == [20]
    win.close()


def test_failed_page_is_shared_by_tab_return_rebuild_and_a_fresh_home_then_retried(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    hw = ctx.home
    calls = []
    healthy = [False]

    def fetch(*args, extra=None):
        offset = dict(extra)['skip']
        calls.append(offset)
        if not healthy[0]:
            raise AddonError('HTTP 504', category='HTTP 504')
        return metas(20, 20) if offset == 20 else []

    monkeypatch.setattr(ctx.views, '_fetch_catalog', fetch)
    win, _ = prepare(ctx, monkeypatch)
    win.onInit()
    join_shelf_extends(win)
    hw._apply_shelf_extends(win)
    assert calls == [20]
    hw._switch_tab(win, 'mystuff')
    hw._switch_tab(win, 'home')
    win._build()
    second, _ = prepare(ctx, monkeypatch)
    second.onInit()
    assert calls == [20]
    state = second._rows[30041]
    assert len(state['metas']) == 20 and state['failed']['count'] == 1
    skips = [line for line, _ in ctx.env.log_calls if 'page 2 skipped' in line]
    assert skips and all('SECRET' not in line and 'TOKEN' not in line and '/private' not in line for line in skips)
    healthy[0] = True
    state['failed']['at'] -= 31
    for failed in ctx.catalogpages._later_failures.values():
        failed['at'] -= 31
    second.getControl(30041).selectItem(19)
    second.setFocusId(30041)
    hw._maybe_extend_focused_shelf(second)
    join_shelf_extends(second)
    hw._apply_shelf_extends(second)
    assert calls == [20, 20, 40]
    assert len(state['metas']) == 40
    assert len({meta['id'] for meta in state['metas']}) == 40
    assert state['failed'] is None
    win.close()
    second.close()


def test_a_remembered_failure_logs_one_info_without_a_generic_warning(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    hw = ctx.home
    win, store = prepare(ctx, monkeypatch)
    from lib.homelayout import installed_catalogs
    descriptor = installed_catalogs(store.get_enabled_addons())[0]
    state = hw._open_shelf(descriptor)
    def fail(*args, **kwargs):
        raise AddonError('HTTP 504', category='HTTP 504')
    monkeypatch.setattr(ctx.views, '_fetch_catalog', fail)
    catalog = state['catalog']
    with pytest.raises(ctx.catalogpages.LaterPageFailure):
        ctx.catalogpages.cached_later_page(state['transport'], catalog['type'], catalog['id'], [('skip', 20)])
    # Another row failed the page after this row's first-page check.
    ctx.env.log_calls.clear()
    assert hw._pull_more(state, 5) == []
    logs = ctx.env.log_calls
    assert len(logs) == 1 and 'page 2 skipped' in logs[0][0]
    import xbmc
    assert logs[0][1] == xbmc.LOGINFO
    assert state['failed']['count'] == 1 and not state['exhausted']
    win.close()


def test_six_rows_keep_four_worker_parallelism(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    active, peak = [0], [0]
    lock = threading.Lock()

    def first(*args):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.3)
        with lock:
            active[0] -= 1
        return metas(0, 30)

    win, _ = prepare(ctx, monkeypatch, count=6, first=first)
    started = time.monotonic()
    win.onInit()
    assert time.monotonic() - started <= 0.7
    assert len(win._rows) == 6 and peak[0] == 4
    timing = [line for line, _ in ctx.env.log_calls if 'home: tab home painted' in line]
    assert len(timing) == 1
    assert 'slowest row cat:https://addon.test|movie|' in timing[0]
    assert 'cancelled=False' in timing[0]
    assert 'SECRET' not in timing[0] and 'TOKEN' not in timing[0]
    win.close()


@pytest.mark.parametrize('category', ['HTTP 503', 'ReadTimeout', 'ConnectionError'])
def test_later_failure_memory_is_bounded_and_does_not_count_a_skip_as_a_new_failure(load_homewindow, monkeypatch, category):
    ctx = load_homewindow()
    pages = ctx.catalogpages
    calls = []

    def fail(*args, **kw):
        calls.append(True)
        raise AddonError('bad host', category=category)

    monkeypatch.setattr(ctx.views, '_fetch_catalog', fail)
    extra = [('genre', 'Drama'), ('skip', 20)]
    for _ in range(2):
        with pytest.raises(pages.LaterPageFailure) as caught:
            pages.cached_later_page('t', 'movie', 'c', extra)
        assert caught.value.failed['count'] == 1
    assert calls == [True]
    for i in range(pages._MAX_LATER_PAGES + 2):
        with pytest.raises(pages.LaterPageFailure):
            pages.cached_later_page('t', 'movie', str(i), extra)
    assert len(pages._later_failures) == pages._MAX_LATER_PAGES
    pages.clear_later_pages()
    assert not pages._later_failures


def test_home_entry_to_shown_timing_is_logged_once(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    from lib.ui import homewindow

    class Window(ctx.home.HomeWindow):
        def doModal(self):
            self._home_on_shown()

    monkeypatch.setattr(ctx.home, 'HomeWindow', Window)
    monkeypatch.setattr(homewindow, '_notify_if_updated', lambda *_: None)
    homewindow.open_home()
    lines = [line for line, _ in ctx.env.log_calls if 'HomeWindow shown in ' in line]
    assert len(lines) == 1 and lines[0].endswith(' s')
