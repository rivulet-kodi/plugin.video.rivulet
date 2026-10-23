"""Continue metadata shares startup time, with a hard budget and idle workers."""
import threading
from types import SimpleNamespace

import pytest

from tests.test_home_shutdown import join_promptly
from tests.test_home_shutdown import session as session
from tests.test_home_startup import prepare
from tests.test_newhome import load_homewindow as load_homewindow
from tests.test_newhome_tabs import metas


def continue_plan(ctx, monkeypatch, count=3):
    win, store = prepare(ctx, monkeypatch)
    layout = store.get_home_layout()
    layout['tabs']['home'].insert(0, 'continue')
    store._progress_entries = [{'type': 'movie', 'id': 'tt%d' % n, 'position_ms': 60000,
                                'duration_ms': 120000, 'updated_at': '2026-10-21T00:00:00Z'}
                               for n in range(count)]
    monkeypatch.setattr(ctx.mystuff, 'get_store', lambda: store)
    return win, store


def test_three_ten_second_lookups_cannot_hold_startup_and_retry_next_load(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    hw = ctx.newhome
    win, store = continue_plan(ctx, monkeypatch)
    release = threading.Event()
    started = threading.Barrier(4)
    calls = []
    before = set(threading.enumerate())

    def fetch(stype, sid, should_stop, **kw):
        calls.append(sid)
        if not release.is_set():
            started.wait(5)
            assert release.wait(10)
        return {'id': sid, 'name': 'too late', 'poster': 'late'}

    monkeypatch.setattr(ctx.views, '_fetch_meta_for_home', fetch)
    now = [0.0]
    monkeypatch.setattr(hw, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    collect = hw._collect_home_rows
    def cutoff(results, count, threads, stopped, deadline=None):
        if deadline is not None and not release.is_set():
            started.wait(5)
            assert deadline == now[0] + hw._CONTINUE_ENRICH_SECONDS
            now[0] = deadline + 1
        return collect(results, count, threads, stopped, deadline)
    monkeypatch.setattr(hw, '_collect_home_rows', cutoff)
    collector = threading.Thread(target=win.onInit)
    collector.start()
    try:
        join_promptly([collector])
        assert win.getControl(hw.CONTINUE_SHELF).size() == 0
        assert hw._continue_meta_misses == set()
        release.set()
        for worker in set(threading.enumerate()) - before:
            if worker is not collector:
                worker.join(5)
        rows = hw._load_rows(win, store, None)
        assert len(rows[0]['items']) == 3 and len(calls) == 6
    finally:
        release.set()
        collector.join(5)
        for worker in set(threading.enumerate()) - before:
            worker.join(5)
    assert win.getControl(hw.CONTINUE_SHELF).size() == 0
    win.close()


def test_continue_lookup_starts_while_catalog_first_page_is_still_running(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    win, _ = continue_plan(ctx, monkeypatch, count=1)
    together = threading.Barrier(2)
    fetched = []

    def page(*args):
        together.wait(5)
        return metas(0, 30)

    def meta(stype, sid, should_stop, **kw):
        if 'on_not_found' not in kw:
            # The focused poster's lookup, not the Continue one under test.
            # Left waiting on the barrier it outlives the test, and fails a
            # later one once the stubs are restored.
            return None
        together.wait(5)
        fetched.append(sid)
        return {'id': sid, 'name': 'resolved', 'poster': 'p'}

    monkeypatch.setattr(ctx.newhome, '_one_catalog_page', page)
    monkeypatch.setattr(ctx.views, '_fetch_meta_for_home', meta)
    win.onInit()
    assert fetched == ['tt0']
    assert win.getControl(ctx.newhome.CONTINUE_SHELF).size() == 1
    win.close()


def test_missing_meta_is_not_retried_in_a_second_home_load(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    win, store = continue_plan(ctx, monkeypatch, count=1)
    calls = []
    def missing(*args, **kw):
        calls.append(True)
        kw['on_not_found']()
        return None
    monkeypatch.setattr(ctx.views, '_fetch_meta_for_home', missing)
    win.onInit()
    ctx.newhome._load_rows(win, store, None)
    assert calls == [True]
    assert ctx.newhome._continue_meta_misses == {('movie', 'tt0')}
    win.close()


def test_continue_budget_starts_after_twelve_slow_catalog_jobs(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    win, store = continue_plan(ctx, monkeypatch, count=1)
    layout = store.get_home_layout()
    catalog_id = layout['tabs']['home'][1]
    # Repeated descriptors exercise the real queue starvation independently
    # of the layout model's per-tab row limit.
    from lib.homelayout import installed_catalogs, resolve
    win._active_tab, win._layout = 'home', layout
    win._catalogs = installed_catalogs(store.get_enabled_addons())
    plan = resolve(layout, 'home', win._catalogs)
    catalog = next(job for job in plan if job[2] is not None)
    plan = [plan[0]] + [(30041 + i, *catalog[1:]) for i in range(12)]
    monkeypatch.setattr('lib.homelayout.resolve', lambda *args: plan)
    now, clock_lock = [0.0], threading.Lock()
    monkeypatch.setattr(ctx.newhome, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    def page(*args):
        with clock_lock:
            now[0] += 0.7  # Simulate catalog time without sleeping.
        return metas(0, 20)
    monkeypatch.setattr(ctx.newhome, '_one_catalog_page', page)
    calls = []
    def fetch(stype, sid, should_stop, **kw):
        calls.append(sid)
        assert not should_stop()
        return {'id': sid, 'name': 'resolved', 'poster': 'p'}
    monkeypatch.setattr(ctx.views, '_fetch_meta_for_home', fetch)
    rows = ctx.newhome._load_rows(win, store, None)
    assert catalog_id and calls == ['tt0']
    assert len(rows[0]['items']) == 1
    assert not ctx.newhome._continue_meta_misses
    win.close()


def test_seventeen_lookups_use_eight_workers_and_cutoff_is_not_a_miss(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    win, store = continue_plan(ctx, monkeypatch, count=17)
    from lib.homelayout import installed_catalogs
    win._active_tab, win._layout = 'home', store.get_home_layout()
    win._catalogs = installed_catalogs(store.get_enabled_addons())
    lock = threading.Lock()
    running = [0, 0]
    before = set(threading.enumerate())
    now, finished, release = [0.0], [], threading.Event()
    wave = threading.Barrier(8)
    monkeypatch.setattr(ctx.newhome, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    def fetch(stype, sid, should_stop, **kw):
        with lock:
            running[0] += 1
            running[1] = max(running)
            index = int(sid[2:])
        if index < 16:
            wave.wait(5)
        else:
            assert release.wait(5)
        with lock:
            running[0] -= 1
        return None if should_stop() else {'id': sid, 'name': 'resolved', 'poster': 'p'}
    collect = ctx.newhome._collect_home_rows
    def cutoff(results, count, threads, stopped, deadline=None):
        if deadline is None:
            return collect(results, count, threads, stopped)
        fetched = {}
        for _ in range(16):
            key, value = results.get(timeout=5)  # Hang guard, not a timing assertion.
            fetched[key] = value
        finished.extend(fetched)
        now[0] = deadline + 1
        release.set()
        return fetched
    monkeypatch.setattr(ctx.newhome, '_collect_home_rows', cutoff)
    monkeypatch.setattr(ctx.views, '_fetch_meta_for_home', fetch)
    try:
        rows = ctx.newhome._load_rows(win, store, None)
        assert running[1] == 8 and len(finished) == 16
        assert len(rows[0]['items']) == min(16, ctx.newhome.SHELF_CAP)
        assert not ctx.newhome._continue_meta_misses
    finally:
        for worker in set(threading.enumerate()) - before:
            worker.join(5)
        win.close()


def test_stopping_a_thousand_times_constructs_one_monitor(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    import xbmc
    original = xbmc.Monitor
    calls = []
    monkeypatch.setattr(xbmc, 'Monitor', lambda: calls.append(True) or original())
    for _ in range(1000):
        assert not ctx.newhome._stopping()
    assert calls == [True]


@pytest.mark.parametrize('abort', [False, True])
def test_idle_workers_request_one_second_waits_and_stop_on_close(session, monkeypatch, tmp_path, abort):
    ctx, win = session, session.window
    original_event = threading.Event
    release = original_event()
    names = ('rivulet-library-meta', 'rivulet-ratings', 'rivulet-logocheck')
    observed = {name: original_event() for name in names}
    waits = {name: [] for name in names}

    class Event(original_event):
        def wait(self, timeout=None):
            name = threading.current_thread().name
            if name in waits:
                waits[name].append(timeout)
                observed[name].set()
                if not self.is_set():
                    assert release.wait(5)
                    return self.is_set()
            return super().wait(timeout)

    for module, attr in ((ctx.ratingsched, '_wake'), (ctx.logocheck, '_WAKE')):
        monkeypatch.setattr(module, attr, Event())
    monkeypatch.setattr(ctx.newhome.threading, 'Event', Event)
    monkeypatch.setattr(ctx.newhome, '_fetch_library_meta', lambda *_: None)
    ctx.newhome._schedule_library_meta(win, ctx.newhome._poster_item({'id': 'tt1', 'type': 'movie'}, sparse=True))
    from lib.stremio.ratings import RatingsCache
    monkeypatch.setattr(ctx.ratingsched, '_DWELL_SECONDS', 60)
    ctx.ratingsched._schedule('tt0000001', 'key', '', RatingsCache(str(tmp_path / 'ratings.json')))
    monkeypatch.setattr(ctx.logocheck, 'fetch_is_image', lambda _: False)
    ctx.logocheck.ask(['logo'])
    try:
        for event in observed.values():
            assert event.wait(5)
        workers = [thread for thread in threading.enumerate() if thread.name in names]
        if abort:
            ctx.env.monitor_abort_requested = True
        else:
            win.close()
    finally:
        release.set()
    join_promptly(workers)
    assert all(values and all(timeout == 1.0 for timeout in values) for values in waits.values()), waits
