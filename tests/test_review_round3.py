"""Round-three Home failure, cancellation and GUI handoff regressions."""
import threading
from types import SimpleNamespace

import pytest

from tests.test_catalogpages import _cache, _Client
from tests.test_catalogpages import pages as pages
from tests.test_home import load_homewindow as load_homewindow
from tests.test_home_tabs import build
from tests.test_ratingsched import cache as cache
from tests.test_ratingsched import sched as sched


def test_failed_library_refetches_on_next_tab_and_keeps_last_good(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    hw = ctx.home
    win, store, _ = build(ctx, monkeypatch, auth={'authKey': 'fake'})
    # Start with no successful account read, then recover on the next tab.
    if hasattr(win, '_library_entries'):
        del win._library_entries
    calls = []
    entries = [{'_id': 'tt9', 'type': 'movie', 'name': 'Recovered', 'poster': 'p'}]
    def read(*args):
        calls.append(True)
        return None if len(calls) == 1 else entries
    monkeypatch.setattr(hw, '_fetch_library', read)
    hw._switch_tab(win, 'mystuff')
    assert getattr(win, '_library_entries', None) is None and win._library_failed
    assert win.getControl(30041).getListItem(0).getLabel() != 'STR30426'
    hw._switch_tab(win, 'movies')
    hw._switch_tab(win, 'mystuff')
    assert len(calls) == 2 and win._library_entries == entries
    assert win.getControl(30041).getListItem(0).getProperty('sid') == 'tt9'
    monkeypatch.setattr(hw, '_fetch_library', lambda *a: None)
    hw._refresh_saved_rows(win, store)
    assert win._library_entries == entries and win._library_failed
    assert win.getControl(30041).getListItem(0).getProperty('sid') == 'tt9'
    win.close()


def test_api_library_error_is_a_failed_read_and_nonlists_are_rejected(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    from lib.stremio.api import ApiError
    store = SimpleNamespace(get_auth=lambda: {'authKey': 'fake'})
    class API:
        def datastore_get(self, *args, **kwargs):
            raise ApiError('unavailable')
    monkeypatch.setattr('lib.stremio.api.StremioAPI', API)
    assert ctx.home._fetch_library(store, store.get_auth()) is None
    monkeypatch.setattr(ctx.mystuff, '_read_library_entries', lambda store: {'bad': 'shape'})
    assert ctx.home._fetch_library(store, store.get_auth()) is None


def test_adult_title_survives_raw_cache_when_hiding_is_disabled(pages, tmp_path, monkeypatch):
    module = pages.catalogpages
    cache = _cache(tmp_path, {'now': 0})
    monkeypatch.setattr(module, 'get_page_cache', lambda: cache)
    client = _Client([{'id': 'safe'}, {'id': 'adult', 'adult': True}])
    monkeypatch.setattr(pages.views, 'get_client', lambda: client)
    catalog = {'type': 'movie', 'id': 'all'}
    pages.env.addon.settings['home_hide_adult'] = 'true'
    assert [m['id'] for m in module.cached_first_page('t', {}, catalog)] == ['safe']
    pages.env.addon.settings['home_hide_adult'] = 'false'
    assert [m['id'] for m in module.cached_first_page('t', {}, catalog)] == ['safe', 'adult']
    assert len(client.calls) == 1


def test_fifty_stale_keys_use_one_bounded_worker_and_stop_discards_late(pages, monkeypatch, tmp_path):
    cp = pages.catalogpages
    entered, release = threading.Event(), threading.Event()
    cache = _cache(tmp_path, {'now': 0})
    monkeypatch.setattr(cp, 'get_page_cache', lambda: cache)
    def fetch(*args):
        entered.set()
        assert release.wait(5)
        return [{'id': 'late'}]
    monkeypatch.setattr(cp, '_fetch_first_page', fetch)
    cp._schedule_revalidate('t', {}, {}, None, 'first')
    worker = cp._revalidate_worker
    assert entered.wait(5)
    try:
        for i in range(50):
            cp._schedule_revalidate('t', {}, {}, None, str(i))
            assert cp._revalidate_worker is worker
        assert len(cp._revalidate_queue) == 32
        assert '0' not in cp._revalidate_queue and '49' in cp._revalidate_queue
        cp.stop()
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive() and not cp._revalidate_queue
    assert cache.lookup('first')[0] == 'miss'


def test_failed_revalidation_waits_five_minutes(pages, monkeypatch):
    cp = pages.catalogpages
    now = [100]
    monkeypatch.setattr(cp, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    def fail(*args):
        raise OSError('down')
    monkeypatch.setattr(cp, '_fetch_first_page', fail)
    cp._revalidate('t', {}, {}, None, 'failed')
    assert cp._revalidate_failed_until['failed'] == 400
    cp._schedule_revalidate('t', {}, {}, None, 'failed')
    assert cp._revalidate_worker is None
    now[0] = 401
    # Record start without creating a real idle worker for this queue assertion.
    started = []
    monkeypatch.setattr(cp.threading, 'Thread', lambda **kw: SimpleNamespace(start=lambda: started.append(True)))
    cp._schedule_revalidate('t', {}, {}, None, 'failed')
    assert started == [True]
    cp.stop()


def test_shelf_worker_never_reads_controls_and_failure_releases_loading(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    hw = ctx.home
    win, _, _ = build(ctx, monkeypatch)
    gui = threading.current_thread()
    original = win.getControl
    calls = []
    def control(cid):
        calls.append(threading.current_thread())
        assert threading.current_thread() is gui
        return original(cid)
    monkeypatch.setattr(win, 'getControl', control)
    def fail(*args):
        raise RuntimeError('page failed')
    monkeypatch.setattr(hw, '_pull_more', fail)
    hw._schedule_shelf_extend(win, 30041, 24)
    for worker in win._shelf_threads:
        worker.join(5)
    assert win._shelf_running == 0 and 30041 not in win._shelf_inflight
    assert not win._shelf_ready
    assert all(thread is gui for thread in calls)
    monkeypatch.setattr(hw, '_pull_more', lambda *args: [{'id': 'next', 'name': 'Next'}])
    hw._schedule_shelf_extend(win, 30041, 24)
    for worker in win._shelf_threads:
        worker.join(5)
    assert len(win._shelf_ready) == 1
    hw._apply_shelf_extends(win)
    assert not win._shelf_ready and 30041 not in win._shelf_inflight
    win.close()


def test_poster_batches_only_touch_controls_on_gui_after_handoff(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    from lib.ui import posterwarm
    win, _, _ = build(ctx, monkeypatch)
    gui = threading.current_thread()
    posted = threading.Event()
    hold_started, waits = threading.Event(), []
    original_wait = win._home_stop.wait
    def hold(seconds):
        waits.append(seconds)
        hold_started.set()
        return original_wait(seconds)
    monkeypatch.setattr(win._home_stop, 'wait', hold)
    original_get = win.getControl
    for cid in posterwarm.WARM_SLOTS:
        slot = original_get(cid)
        original_image = slot.setImage
        def image(url, setter=original_image):
            assert threading.current_thread() is gui
            setter(url)
        monkeypatch.setattr(slot, 'setImage', image)
    def control(cid):
        assert threading.current_thread() is gui
        return original_get(cid)
    monkeypatch.setattr(win, 'getControl', control)
    original_post = posterwarm._post_batch
    def post(window, urls):
        ticket = original_post(window, urls)
        posted.set()
        return ticket
    monkeypatch.setattr(posterwarm, '_post_batch', post)
    worker = threading.Thread(target=posterwarm._queued_warm, args=(win, ['poster']), name='rivulet-test-warm')
    worker.start()
    assert posted.wait(5)
    assert not original_get(posterwarm.WARM_SLOTS[0]).image and waits == []
    posterwarm.apply_pending(win)
    assert original_get(posterwarm.WARM_SLOTS[0]).image == 'poster'
    assert hold_started.wait(5) and waits == [posterwarm._HOLD_SECONDS]
    win.close()
    worker.join(5)
    assert not worker.is_alive() and original_get(posterwarm.WARM_SLOTS[0]).image == ''


def test_deleted_cache_files_rebuild_home_once_without_a_generation_change(load_homewindow, monkeypatch, tmp_path):
    ctx = load_homewindow()
    from lib.ui import cacherefresh
    hw = ctx.home
    win, store, _ = build(ctx, monkeypatch)
    monkeypatch.setattr(cacherefresh, '_cache_dirs', lambda: [str(tmp_path)])
    cache_file = tmp_path / 'catalog_pages.json'
    cache_file.write_text('{}')
    win._catalog_pages = {'held': [{}]}
    hw._drop_memory_if_refreshed(win)
    clears, builds = [], []
    monkeypatch.setattr(cacherefresh, 'clear_memory', lambda: clears.append(True))
    monkeypatch.setattr(win, '_build', lambda **kw: builds.append(True))
    win._signature = hw._home_signature(store)
    cache_file.unlink()
    hw._after_child(win)
    assert clears == [True] and builds == [True]
    # A real rebuild owns pages again; the absent-file state is already recorded.
    win._catalog_pages = {'rebuilt': [{}]}
    hw._after_child(win)
    assert clears == [True] and builds == [True]
    win.close()


def test_negative_imdb_lookup_is_memoized_and_refresh_clears_it(sched, monkeypatch, tmp_path):
    from lib.ui import dependencies, metacache, previews
    monkeypatch.setattr(dependencies, 'get_store', lambda: SimpleNamespace(data_dir=str(tmp_path)))
    s = sched.module
    monkeypatch.setattr(previews, 'lookup', lambda sid: None)
    reads = []
    monkeypatch.setattr(metacache, 'load_cached_meta', lambda *args: reads.append(args) or None)
    meta = {'id': 'tmdb:unknown', 'type': 'movie'}
    for _ in range(20):
        assert s._facts_with_imdb(meta) is meta
    assert len(reads) == 1
    s.reset()
    s._facts_with_imdb(meta)
    assert len(reads) == 2
    for i in range(2010):
        s._facts_with_imdb(dict(meta, id=str(i)))
    assert len(s._meta_ids) == 2000


def test_resolved_imdb_lookup_is_memoized_too(sched, monkeypatch, tmp_path):
    """A cache hit is not re-parsed from disk on every focus change."""
    from lib.ui import dependencies, metacache, previews
    monkeypatch.setattr(dependencies, 'get_store', lambda: SimpleNamespace(data_dir=str(tmp_path)))
    s = sched.module
    monkeypatch.setattr(previews, 'lookup', lambda sid: None)
    reads = []
    monkeypatch.setattr(metacache, 'load_cached_meta',
                        lambda *args: reads.append(args) or {'imdb_id': 'tt1234567'})
    meta = {'id': 'tmdb:1', 'type': 'movie'}
    for _ in range(5):
        assert s._facts_with_imdb(meta)['imdb_id'] == 'tt1234567'
    assert len(reads) == 1


@pytest.mark.parametrize('action', [5, 6, 104, 105])
def test_page_and_wheel_actions_extend_near_end(load_homewindow, monkeypatch, action):
    ctx = load_homewindow()
    win, _, _ = build(ctx, monkeypatch)
    row = win.getControl(30041)
    row.selectItem(row.size() - 1)
    win.setFocusId(30041)
    scheduled = []
    monkeypatch.setattr(ctx.home, '_schedule_shelf_extend', lambda *a: scheduled.append(a[1]))
    import xbmcgui
    win.onAction(xbmcgui.Action(action))
    assert scheduled and set(scheduled) == {30041}
    win.close()


def test_repeated_hero_errors_log_once_per_session(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    win, _, _ = build(ctx, monkeypatch)
    def fail(*args):
        raise RuntimeError('https://user:private@host/?token=hidden')
    monkeypatch.setattr(ctx.look, 'paint_hero', fail)
    for title in ('First', 'Second'):
        item = ctx.home._poster_item({'id': title, 'name': title})
        ctx.home.paint_hero(win, item)
    lines = [(msg, level) for msg, level in ctx.env.log_calls if 'hero paint failed' in msg]
    assert len(lines) == 1 and lines[0][1] == 0
    assert 'private' not in lines[0][0] and 'hidden' not in lines[0][0]
    win.close()
