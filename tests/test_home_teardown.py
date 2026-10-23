# SPDX-FileCopyrightText: 2026 M0Rf30
# SPDX-License-Identifier: MIT

"""Home gives every `ListItem` back while its window still exists.

Kodi segfaulted on the way out of Home: `CPythonInvoker::onExecutionDone`
freed the Python `ListItem`s last, after the window and its controls were
gone, and `CGUIListItem::~CGUIListItem` -> `CGUIControlLookup::RemoveLookup`
walked item layouts into freed memory. What a test can see is the
precondition: no `ListItem` Home built may still be referenced - by the
window, a module global or a worker thread - once `open_home()` returns,
while the window object itself is still alive (Kodi keeps its own
reference to it, so it is never dropped first).
"""
import gc
import threading
import weakref

import pytest

from lib import homelayout
from tests.test_newhome import _FakeStore
from tests.test_newhome import load_homewindow as load_homewindow  # noqa: F401 - fixture
from tests.test_newhome_tabs import addons, build, metas

_HOME_WORKERS = ('rivulet-library-meta', 'rivulet-home-page', 'rivulet-home-load', 'rivulet-poster-warm')


class _Recorder:
    """Every `xbmcgui.ListItem` built from here on, and who built it."""

    def __init__(self, monkeypatch):
        import xbmcgui

        self.refs = []
        self.threads = set()
        recorder = self
        base = xbmcgui.ListItem

        class Recorded(base):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                recorder.refs.append(weakref.ref(self))
                recorder.threads.add(threading.current_thread())

        monkeypatch.setattr(xbmcgui, 'ListItem', Recorded)

    def alive(self):
        gc.collect()
        return [ref for ref in self.refs if ref() is not None]


def _entries():
    return [{'_id': 'tt%d' % i, 'name': 'T%d' % i, 'type': 'movie', '_ctime': '%04d' % i}
            for i in range(60)]


def _progress():
    return [{'type': 'movie', 'id': 'tt%d' % i, 'position_ms': 20, 'duration_ms': 100,
             'updated_at': str(i)} for i in range(3)]


def _hold_requests_until_stopped(ctx, monkeypatch):
    """Library-meta and next-page fetches that block until Home stops them.

    Like the real HTTP calls, which poll their stop event. Returns the
    events that say each worker is inside its fetch.
    """
    hw = ctx.newhome
    in_meta, in_page = threading.Event(), threading.Event()

    def meta(_stype, _sid, stop=None):
        in_meta.set()
        stop.wait(5)
        return None

    def page(state, _room):
        in_page.set()
        state['_stop'].wait(5)
        return []

    monkeypatch.setattr(hw, '_fetch_library_meta', meta)
    monkeypatch.setattr(hw, '_pull_more', page)
    return in_meta, in_page


def _request_from_the_library_row(ctx, win):
    """What focusing a library poster does: a meta fetch and a next page."""
    hw = ctx.newhome
    hw._switch_tab(win, 'mystuff')
    item = win.getControl(hw.LIBRARY_SHELF).getListItem(0)
    hw._schedule_library_meta(win, item)
    hw._schedule_shelf_extend(win, hw.LIBRARY_SHELF, 24)


def test_open_home_frees_every_list_item_while_the_window_is_alive(load_homewindow, monkeypatch):  # noqa: F811
    ctx = load_homewindow()
    from lib.ui import homewindow

    hw = ctx.newhome
    items = _Recorder(monkeypatch)
    store = _FakeStore(auth={'authKey': 'k'}, addons=addons(), progress_entries=_progress())
    store.get_home_layout = homelayout.default_layout
    monkeypatch.setattr(hw, 'get_store', lambda: store)
    monkeypatch.setattr(hw, '_one_catalog_page',
                        lambda _t, _m, catalog, extra=None: metas(0, 30, catalog['type']))
    monkeypatch.setattr(hw, '_fetch_library', lambda *_: _entries())
    monkeypatch.setattr(ctx.mystuff, '_enrich',
                        lambda rows, **_kw: [row.update(name='Played', poster='p') for row in rows])
    monkeypatch.setattr(hw, '_warm_shelf_posters', lambda *_: None)
    monkeypatch.setattr(homewindow, '_notify_if_updated', lambda *_: None)
    monkeypatch.setattr(homewindow, '_migrate_home_layout', lambda: None)
    in_meta, in_page = _hold_requests_until_stopped(ctx, monkeypatch)
    kept = {}

    class Modal(hw.NewHomeWindow):
        def doModal(self):
            kept['window'] = self  # Kodi holds the window too; it is not dropped first
            self.onInit()
            _request_from_the_library_row(ctx, self)
            assert in_meta.wait(2) and in_page.wait(2)

    monkeypatch.setattr(hw, 'NewHomeWindow', Modal)
    before = set(threading.enumerate())

    homewindow.open_home()

    win = kept['window']
    assert len(items.refs) > 50  # the rows really were built
    assert items.alive() == []
    assert win._rows == {} and win._tab_snapshots == {}
    assert win._library_meta_latest is None
    assert not win._shelf_ready and not win._shelf_inflight and not win._shelf_queue
    assert all(win.getControl(list_id).size() == 0 for list_id in hw._place_lists())
    assert not [thread for thread in threading.enumerate()
                if thread not in before and thread.name in _HOME_WORKERS]


def test_a_failure_in_release_items_does_not_stop_open_home(load_homewindow, monkeypatch):  # noqa: F811
    ctx = load_homewindow()
    from lib.ui import homewindow

    hw = ctx.newhome
    monkeypatch.setattr(homewindow, '_notify_if_updated', lambda *_: None)
    monkeypatch.setattr(homewindow, '_migrate_home_layout', lambda: None)

    class Modal(hw.NewHomeWindow):
        def doModal(self):
            pass

    def explode(_window):
        raise RuntimeError('controls already unloaded')

    monkeypatch.setattr(hw, 'NewHomeWindow', Modal)
    monkeypatch.setattr(hw, 'release_items', explode)

    homewindow.open_home()

    assert any('releasing Home items failed' in line for line, _ in ctx.env.log_calls)


def test_release_items_tolerates_controls_kodi_already_unloaded(load_homewindow, monkeypatch):  # noqa: F811
    ctx = load_homewindow()
    hw = ctx.newhome
    items = _Recorder(monkeypatch)
    win, _, _ = build(ctx, monkeypatch, auth={'authKey': 'k'}, library=_entries())

    def gone(control_id):
        raise RuntimeError('Non-Existent Control %d' % control_id)

    win.getControl = gone  # not monkeypatch: its undo list would keep the window alive
    hw.release_items(win)
    hw.release_items(win)  # idempotent

    assert win._rows == {} and win._tab_snapshots == {}
    # Kodi frees what the window still owns with it: nothing else holds them.
    del win
    assert items.alive() == []


def test_workers_in_flight_hold_neither_the_window_nor_its_items(load_homewindow, monkeypatch):  # noqa: F811
    ctx = load_homewindow()
    hw = ctx.newhome
    items = _Recorder(monkeypatch)
    before = set(threading.enumerate())
    win, _, _ = build(ctx, monkeypatch, auth={'authKey': 'k'}, library=_entries(),
                      progress=_progress())
    in_meta, in_page = _hold_requests_until_stopped(ctx, monkeypatch)
    _request_from_the_library_row(ctx, win)
    assert in_meta.wait(2) and in_page.wait(2)
    stop = hw._home_stop(win)
    workers = [thread for thread in threading.enumerate()
               if thread not in before and thread.name in _HOME_WORKERS]
    assert {thread.name for thread in workers} >= {'rivulet-library-meta', 'rivulet-home-page'}
    window = weakref.ref(win)

    # Both workers are inside their fetch. Drop the window the way a
    # finished script does: only the workers could still hold it.
    del win
    gc.collect()

    assert window() is None
    assert items.alive() == []
    stop.set()
    for thread in workers:
        thread.join(5)
        assert not thread.is_alive()


def test_row_items_are_built_on_the_calling_thread_never_by_a_loader(load_homewindow, monkeypatch):  # noqa: F811
    """A worker that makes a `ListItem` can be the last to drop it, off the
    thread that owns the window. Loaders return records; the GUI thread
    turns them into items, for first paint and for the personal refresh."""
    ctx = load_homewindow()
    hw = ctx.newhome
    items = _Recorder(monkeypatch)
    win, store, _ = build(ctx, monkeypatch, auth={'authKey': 'k'}, library=_entries(),
                          progress=_progress())
    hw._switch_tab(win, 'mystuff')
    hw._switch_tab(win, 'home')
    hw._refresh_saved_rows(win, store)

    assert items.refs
    assert items.threads == {threading.current_thread()}
    assert win.getControl(hw.CONTINUE_SHELF).size() == 3


def test_a_loaded_row_state_is_plain_data_until_the_gui_attaches_items(load_homewindow, monkeypatch):  # noqa: F811
    ctx = load_homewindow()
    hw = ctx.newhome
    items = _Recorder(monkeypatch)
    plan = [('cat:t|movie|top', 'catalog', {}, '', ('Top', 'Top'))]

    states = hw._make_row_states(None, None, [], plan, {'cat:t|movie|top': {'metas': metas(0, 3)}})

    assert items.refs == []
    assert states[0]['items'] == [] and len(states[0]['cells']) == 3
    hw._attach_items(states)
    assert len(states[0]['items']) == 3 and 'cells' not in states[0]


@pytest.mark.parametrize('abort', [False, True])
def test_release_items_stops_and_joins_the_workers_it_finds(load_homewindow, monkeypatch, abort):  # noqa: F811
    ctx = load_homewindow()
    hw = ctx.newhome
    win, _, _ = build(ctx, monkeypatch, auth={'authKey': 'k'}, library=_entries())
    in_meta, in_page = _hold_requests_until_stopped(ctx, monkeypatch)
    _request_from_the_library_row(ctx, win)
    assert in_meta.wait(2) and in_page.wait(2)
    workers = list(win._shelf_threads) + [win._library_meta_thread]
    if abort:
        ctx.env.monitor_abort_requested = True

    hw.release_items(win)

    assert not any(thread.is_alive() for thread in workers)


def test_the_stub_session_waits_for_home_workers_it_started():
    """A worker that outlives its test re-imports `lib.ui.background` after
    `sys.modules` is restored and fails some unrelated later test."""
    import time

    from tests.kodistubs import install_kodi_stubs

    done = threading.Event()

    def slow():
        time.sleep(0.2)
        done.set()

    with install_kodi_stubs():
        thread = threading.Thread(target=slow, name='rivulet-library-meta', daemon=True)
        thread.start()

    assert done.is_set() and not thread.is_alive()
