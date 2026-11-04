"""Home owns its workers until close, independently of SWIG window lifetime."""
import threading

import pytest

from lib import homelayout
from tests.homehooks import join_library_meta
from tests.kodistubs import install_kodi_stubs
from tests.test_home import _RELOAD_MODULE_NAMES, _FakeStore
from tests.test_home_tabs import build, metas


@pytest.fixture
def session(monkeypatch):
    shared = ('lib.ui.logocheck', 'lib.ui.ratingsched', 'lib.ui.posterwarm')
    with install_kodi_stubs(reload=_RELOAD_MODULE_NAMES + shared) as ctx:
        monkeypatch.setattr(ctx.logocheck, 'fetch_is_image', lambda url: False)
        monkeypatch.setattr(ctx.logocheck, 'texture_cached', lambda url: False)
        win = ctx.home.HomeWindow('HomeWindow.xml', '/addon/path', 'Default', '1080i')
        ctx.window = win
        before = set(threading.enumerate())
        yield ctx
        win.close()
        for worker in set(threading.enumerate()) - before:
            if worker.name.startswith('rivulet-'):
                worker.join(5)
                assert not worker.is_alive(), worker.name


def workers_since(before):
    return [t for t in threading.enumerate() if t not in before and t.name.startswith('rivulet-')]


def join_promptly(workers):
    for worker in workers:
        worker.join(5)  # Generous hang guard; requested wait bounds are checked separately.
    assert not [worker.name for worker in workers if worker.is_alive()]


@pytest.mark.parametrize('abort', [False, True])
def test_all_idle_home_workers_stop_while_window_is_still_referenced(session, monkeypatch, tmp_path, abort):
    ctx, win = session, session.window
    hw = ctx.home
    monkeypatch.setattr(hw, '_fetch_library_meta', lambda *_: None)
    before = set(threading.enumerate())
    item = hw._poster_item({'id': 'tt1', 'type': 'movie'}, sparse=True)
    hw._schedule_library_meta(win, item)
    join_library_meta(win)
    # Exercise real shared workers as well as the window's metadata worker.
    warmed = threading.Event()
    original_post = ctx.posterwarm._post_batch
    def post(window, urls):
        ticket = original_post(window, urls)
        warmed.set()
        return ticket
    monkeypatch.setattr(ctx.posterwarm, '_post_batch', post)
    ctx.posterwarm.request_warm(win, ['p%d' % i for i in range(90)])
    assert warmed.wait(5)
    ctx.posterwarm.apply_pending(win)
    from lib.stremio.ratings import RatingsCache
    monkeypatch.setattr(ctx.ratingsched, '_DWELL_SECONDS', 60)
    ctx.ratingsched._schedule('tt0000001', 'key', '', RatingsCache(str(tmp_path / 'ratings.json')))
    logo_done = threading.Event()
    monkeypatch.setattr(ctx.logocheck, 'fetch_is_image', lambda _: logo_done.set() or False)
    ctx.logocheck.ask(['logo'])
    assert logo_done.wait(5)
    workers = workers_since(before)
    assert {'rivulet-poster-warm', 'rivulet-ratings', 'rivulet-logocheck'} <= {t.name for t in workers}
    # Metadata retires as soon as its slot is idle.
    workers.append(win._library_meta_thread)
    if abort:
        ctx.env.monitor_abort_requested = True
    else:
        win.close()
    join_promptly(workers)
    assert win is ctx.window  # A strong reference survives, just as in Kodi.
    assert not win._library_meta_running
    ctx.posterwarm.apply_pending(win)
    assert all(win.getControl(slot).image == '' for slot in ctx.posterwarm.WARM_SLOTS)


@pytest.mark.parametrize('abort', [False, True])
def test_close_drops_queued_and_late_meta_and_can_reopen_for_playback(session, monkeypatch, abort):
    ctx, win = session, session.window
    hw = ctx.home
    entered, release = threading.Event(), threading.Event()
    fetched = []

    def fetch(_type, sid, stop=None):
        fetched.append(sid)
        entered.set()
        assert release.wait(5)
        return {'id': sid, 'description': 'late'}

    monkeypatch.setattr(hw, '_fetch_library_meta', fetch)
    first = hw._poster_item({'id': 'tt1', 'type': 'movie'}, sparse=True)
    second = hw._poster_item({'id': 'tt2', 'type': 'movie'}, sparse=True)
    hw._schedule_library_meta(win, first)
    assert entered.wait(5)
    worker = win._library_meta_thread
    hw._schedule_library_meta(win, second)
    if abort:
        ctx.env.monitor_abort_requested = True
    else:
        win.close()
    pokes = len(ctx.env.executed_builtins)
    release.set()
    join_promptly([worker])
    assert fetched == ['tt1']
    assert not win._library_meta_ready
    assert len(ctx.env.executed_builtins) == pokes
    ctx.env.monitor_abort_requested = False
    win.close()
    monkeypatch.setattr(win, '_build', lambda: None)
    win.onInit()
    hw._schedule_library_meta(win, second)
    join_library_meta(win)
    assert win._library_meta_thread is not worker
    assert fetched == ['tt1', 'tt2']
    win.close()
    join_promptly([win._library_meta_thread])


def test_playback_reopen_hands_pending_meta_to_a_new_session(session, monkeypatch):
    ctx, win = session, session.window
    hw = ctx.home
    entered, release = threading.Event(), threading.Event()
    fetched = []

    def fetch(_type, sid, stop=None):
        fetched.append(sid)
        entered.set()
        assert release.wait(5)
        return {'id': sid}

    monkeypatch.setattr(hw, '_fetch_library_meta', fetch)
    item = hw._poster_item({'id': 'tt1', 'type': 'movie'}, sparse=True)
    hw._schedule_library_meta(win, item)
    assert entered.wait(5)
    old_worker = win._library_meta_thread
    win.close()
    monkeypatch.setattr(win, '_build', lambda: None)
    win.onInit()
    hw._schedule_library_meta(win, item)
    release.set()
    join_promptly([old_worker])
    join_library_meta(win)
    assert fetched == ['tt1', 'tt1']
    assert win._library_meta_ready == [('movie', 'tt1', {'id': 'tt1'})]
    win.close()
    join_promptly([win._library_meta_thread])


@pytest.mark.parametrize('abort', [False, True])
def test_page_workers_stop_before_another_page_and_discard_late_results(session, monkeypatch, abort):
    ctx = session
    hw = ctx.home
    win, _, _ = build(ctx, monkeypatch)
    ctx.window.close()
    ctx.window = win
    from lib.ui import catalogpages, views
    catalogpages.clear_later_pages()
    state = win._rows[30041]
    state.update(pending=[], served=24, filter='series', exhausted=False)
    entered, release = threading.Event(), threading.Event()
    fetched = []

    def fetch(*args, extra=None):
        fetched.append(dict(extra)['skip'])
        entered.set()
        assert release.wait(5)
        return metas(24, 24)  # No series: normally another page is fetched.

    monkeypatch.setattr(views, '_fetch_catalog', fetch)
    hw._schedule_shelf_extend(win, 30041, 24)
    assert entered.wait(5)
    workers = list(win._shelf_threads)
    if abort:
        ctx.env.monitor_abort_requested = True
    else:
        win.close()
    release.set()
    join_promptly(workers)
    assert fetched == [24]
    assert not win._shelf_ready
    assert state['served'] == 24
    assert win._shelf_running == 0
    win.close()


@pytest.mark.parametrize('mode', ['close', 'abort', 'cancel'])
def test_first_load_collects_with_timed_waits_and_stops_queued_rows(session, monkeypatch, mode):
    ctx, win = session, session.window
    hw = ctx.home
    installed = [{'transportUrl': 't', 'manifest': {'catalogs': [
        {'type': 'movie', 'id': 'c%d' % n} for n in range(10)]}}]
    store = _FakeStore(addons=installed)
    win._active_tab = 'home'
    win._layout = homelayout.default_layout()
    win._layout['tabs']['home'] = ['rest:movie']
    win._catalogs = homelayout.installed_catalogs(installed)
    entered = threading.Barrier(5)
    release = threading.Event()
    fetched = []
    collectors = []
    before = set(threading.enumerate())

    def fetch(descriptor):
        fetched.append(descriptor['catalog']['id'])
        entered.wait(5)
        assert release.wait(5)
        return {'metas': metas(), 'exhausted': True}

    def cancellable(load, _label):
        collector = threading.Thread(target=load, name='test-home-collector')
        collectors.append(collector)
        collector.start()
        entered.wait(5)
        if mode == 'close':
            win.close()
        elif mode == 'abort':
            ctx.env.monitor_abort_requested = True
        return ctx.uicommon.CANCELLED

    monkeypatch.setattr(hw, '_open_shelf', fetch)
    monkeypatch.setattr(ctx.uicommon, 'run_cancellable', cancellable)
    assert hw._load_rows(win, store, None) == []
    workers = workers_since(before)
    assert len(workers) == 4
    # Collector must return even while in-flight calls haven't completed.
    join_promptly(collectors)
    release.set()
    join_promptly(workers)
    assert len(fetched) == 4
    win.close()


def test_first_load_cannot_wait_forever_if_a_worker_exits_without_a_result(session, monkeypatch):
    ctx, win = session, session.window
    hw = ctx.home
    win._active_tab = 'home'
    win._layout = homelayout.default_layout()
    win._catalogs = homelayout.installed_catalogs([{'transportUrl': 't', 'manifest': {
        'catalogs': [{'id': 'c', 'type': 'movie'}]}}])
    # Simulate a dead worker without producing an unhandled-thread warning.
    class DeadWorker:
        def __init__(self, **kwargs):
            pass

        def start(self):
            pass

        def is_alive(self):
            return False

    monkeypatch.setattr(hw.threading, 'Thread', DeadWorker)
    monkeypatch.setattr(ctx.uicommon, 'run_cancellable', lambda load, _: load())
    import queue
    class NoBlockingGet(queue.Queue):
        def get(self, block=True, timeout=None):
            if block:
                pytest.fail('a dead worker must not leave the collector waiting')
            return super().get(block=False)
    monkeypatch.setattr(hw.queue, 'Queue', NoBlockingGet)
    rows = hw._load_rows(win, _FakeStore(), None)
    assert rows == []
    assert win._tab_loaded['home'] is False


def test_home_meta_wrapper_discards_running_fanout_and_skips_next_focus(session, monkeypatch):
    ctx, win = session, session.window
    installed = [{'transportUrl': str(n), 'manifest': {'types': ['movie'], 'resources': ['meta']}}
                 for n in range(8)]
    store = _FakeStore(addons=installed)
    monkeypatch.setattr(ctx.views, 'get_store', lambda: store)
    entered = threading.Barrier(9)
    release = threading.Event()
    fetched = []

    class Client:
        def meta(self, transport, _type, sid):
            fetched.append((transport, sid))
            entered.wait(5)
            assert release.wait(5)
            return None

    monkeypatch.setattr(ctx.views, 'get_client', Client)
    before = set(threading.enumerate())
    item = ctx.home._poster_item({'id': 'tt1', 'type': 'movie'}, sparse=True)
    ctx.home._schedule_library_meta(win, item)
    entered.wait(5)
    worker = win._library_meta_thread
    second = ctx.home._poster_item({'id': 'tt2', 'type': 'movie'}, sparse=True)
    ctx.home._schedule_library_meta(win, second)
    win.close()
    # The already-running upstream lookup may finish. Its next focus job
    # must not run, and its result must not reach the closed Home window.
    assert worker.is_alive()
    release.set()
    join_promptly(list(set(threading.enumerate()) - before))
    assert len(fetched) == 8
    assert all(sid == 'tt1' for _transport, sid in fetched)
    assert not win._library_meta_ready
    assert ctx.home._fetch_library_meta('movie', 'tt1', win._home_stop) is None
    assert len(fetched) == 8


def test_close_during_first_page_prevents_filtered_followup_pages(session, monkeypatch):
    ctx, win = session, session.window
    descriptor = homelayout.installed_catalogs([{'transportUrl': 't', 'manifest': {'catalogs': [
        {'id': 'anime', 'type': 'anime', 'extra': [{'name': 'skip'}]}]}}])[0]

    def first(*args):
        win.close()
        return metas(stype='series')

    monkeypatch.setattr(ctx.home, '_one_catalog_page', first)
    monkeypatch.setattr(ctx.home, '_shelf_page_gen', lambda _: pytest.fail('fetch after close'))
    state = ctx.home._open_shelf(dict(descriptor, _stop=ctx.home._home_stop(win)))
    assert state['metas'] == []


@pytest.mark.parametrize('stop_at', ['failure', 'backoff', 'abort'])
def test_upstream_meta_retry_stops_on_home_close_or_abort(session, monkeypatch, stop_at):
    from lib.stremio.addons import AddonError

    ctx, win = session, session.window
    installed = [{'transportUrl': 't', 'manifest': {'types': ['movie'], 'resources': ['meta']}}]
    monkeypatch.setattr(ctx.views, 'get_store', lambda: _FakeStore(addons=installed))
    calls = []

    class Client:
        def meta(self, *_):
            calls.append(True)
            if stop_at == 'failure':
                win.close()
            raise AddonError('temporary failure', category='HTTP 503')

    monkeypatch.setattr(ctx.views, 'get_client', Client)
    import xbmc

    monitor = xbmc.Monitor()
    stop = ctx.home._home_stop(win)
    wait = stop.wait
    waits = []

    def stop_during_pause(seconds):
        waits.append(seconds)
        if stop_at == 'abort':
            ctx.env.monitor_abort_requested = True
        else:
            win.close()
        return wait(0)

    stop.wait = stop_during_pause
    monkeypatch.setattr(xbmc, 'Monitor', lambda: monitor)
    result = ctx.home._fetch_library_meta('movie', 'tt1', ctx.home._home_stop(win))
    assert result is None
    assert calls == [True]
    assert len(waits) == (0 if stop_at == 'failure' else 1)
    assert all(seconds <= 1.0 for seconds in waits)


def test_home_meta_retry_keeps_upstream_backoff_while_session_is_open(session):
    waits = []

    class Stop:
        def wait(self, seconds):
            waits.append(seconds)
            return False

    assert session.views._pause_home_meta_retry(0.4, lambda: False, Stop()) is False
    assert waits == [0.4]


def test_home_meta_wrapper_checks_closed_before_call_and_discards_late_result(session, monkeypatch):
    views = session.views
    closed = threading.Event()
    calls = []

    def fetch(*args, **kw):
        calls.append((args, kw))
        closed.set()
        return {'id': 'tt1'}

    monkeypatch.setattr(views, '_fetch_meta', fetch)
    assert views._fetch_meta_for_home('movie', 'tt1', closed.is_set) is None
    assert len(calls) == 1
    assert views._fetch_meta_for_home('movie', 'tt2', closed.is_set) is None
    assert len(calls) == 1


@pytest.mark.parametrize('fail', [False, True])
def test_home_meta_wrapper_forwards_options_and_restores_retry_context(session, monkeypatch, fail):
    views = session.views
    missed = []

    def marker():
        return False

    def on_miss():
        missed.append(True)

    def fetch(stype, sid, **kw):
        assert (stype, sid, kw) == ('movie', 'tt1', {'store': False, 'on_miss': on_miss})
        assert views._HOME_META_RETRY_CONTEXT.should_stop is marker
        if fail:
            raise OSError('failed')
        kw['on_miss']()
        return {'id': sid}

    monkeypatch.setattr(views, '_fetch_meta', fetch)
    if fail:
        with pytest.raises(OSError, match='failed'):
            views._fetch_meta_for_home('movie', 'tt1', marker, store=False, on_miss=on_miss)
    else:
        assert views._fetch_meta_for_home('movie', 'tt1', marker, store=False, on_miss=on_miss) == {'id': 'tt1'}
        assert missed == [True]
    assert views._HOME_META_RETRY_CONTEXT.should_stop is None


def test_home_retry_pause_does_not_cancel_concurrent_stock_caller(session, monkeypatch):
    views = session.views
    closed = threading.Event()
    stock_waits = []
    stock_result = []
    monkeypatch.setattr(views, '_STOCK_META_RETRY_PAUSE', lambda seconds: stock_waits.append(seconds) or False)

    def fetch(*args, **kw):
        closed.set()
        stock = threading.Thread(target=lambda: stock_result.append(views._pause_before_meta_retry(0.4)))
        stock.start()
        join_promptly([stock])
        assert views._pause_before_meta_retry(0.4) is True
        return {'id': 'tt1'}

    monkeypatch.setattr(views, '_fetch_meta', fetch)
    assert views._fetch_meta_for_home('movie', 'tt1', closed.is_set) is None
    assert stock_waits == [0.4]
    assert stock_result == [False]


@pytest.mark.parametrize('answers,miss', [
    ([{}], True), ([404], True), ([503], False), ([{}, 503], False),
    ([{'id': 'tt1', 'name': 'found'}], False),
])
def test_home_meta_remembers_only_definitive_not_found(session, monkeypatch, answers, miss):
    from types import SimpleNamespace

    from lib.stremio.addons import AddonError

    views = session.views
    descriptors = [{'transportUrl': str(i), 'manifest': {'resources': ['meta'], 'types': ['movie']}}
                   for i in range(len(answers))]
    def meta(transport, *args):
        answer = answers[int(transport)]
        if isinstance(answer, int):
            raise AddonError('provider failure', category='HTTP %d' % answer)
        return answer
    monkeypatch.setattr(views, 'get_store', lambda: SimpleNamespace(get_enabled_addons=lambda: descriptors))
    monkeypatch.setattr(views, '_STOCK_META_CLIENT', lambda: SimpleNamespace(meta=meta))
    monkeypatch.setattr(views, '_pause_before_meta_retry', lambda seconds: False)
    missed = []
    result = views._fetch_meta_for_home('movie', 'tt1', lambda: False,
                                       on_not_found=lambda: missed.append(True))
    assert bool(missed) is miss
    assert bool(result) is bool(isinstance(answers[0], dict) and answers[0].get('id'))
    assert views._HOME_META_RETRY_CONTEXT.client is None
