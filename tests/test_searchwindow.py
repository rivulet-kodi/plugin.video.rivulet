"""Tests for lib.ui.searchwindow: SearchWindow, Rivulet's custom
persistent search-history/new-query picker that replaces the old bare
`open_search()` function. The old function opened the results coverflow
directly with no window underneath it on the navigation stack, so Back
from the results fell all the way to Home (the reported
"backspace from results goes to main menu" bug); SearchWindow stays open
under the coverflow the same way `lib.ui.catalogpicker.CatalogPickerWindow`
does for Discover, fixing that bug as a side effect of the architecture.
Row 0 is always "New search…"; every history row re-runs that past query
(the closest thing to autocompletion `xbmcgui.Dialog().input()` allows);
a trailing "Clear search history" row appears once there is history.
Exercised against the shared fake xbmc/xbmcgui stubs in tests/kodistubs
(no real Kodi runtime, no network).

lib.ui.searchwindow imports xbmcgui and lib.ui.uicommon at module scope;
every other collaborator (`lib.store.Store`, `lib.stremio.addons.
AddonClient`/`AddonError`/`iter_catalogs`, `lib.ui.compat.L`/`log`/
`notify`/`addon_profile_dir`, `lib.ui.infowindow.open_showcase`,
`lib.ui.detailwindow.open_detail`) is imported lazily inside the method
that needs it - so this file fakes `lib.store.Store` and
`lib.stremio.addons.AddonClient` by monkeypatching those modules'
attributes directly (the same way test_addonswindow.py patches them via
`_wire_store`/`_wire_client`), rather than reloading lib.ui.searchwindow's
own module-scope bindings.

SearchWindow._run_search() also lazily `from lib.ui.infowindow import
open_showcase` / `from lib.ui.detailwindow import open_detail`, exactly
like `CatalogPickerWindow._open_catalog` does, so load_searchwindow
reloads lib.ui.infowindow/lib.ui.detailwindow fresh alongside
lib.ui.compat/lib.ui.uicommon/lib.ui.searchwindow to get handles
(`ctx.infowindow`/`ctx.detailwindow`) this file monkeypatches directly -
copying tests/test_catalogpicker.py's exact mechanism.

SearchWindow.onInit()/onClick() are called directly here, never through a
real modal event loop, exactly like test_catalogpicker.py drives
CatalogPickerWindow: the fake WindowXML.doModal() is a no-op counter, and
getControl()/setFocusId() are plain in-memory fakes. SearchWindow.xml's
actual skin rendering is Kodi-skin-engine-only and is NOT, and cannot be,
exercised by this suite.
"""
import contextlib

import pytest

import lib.store as store_module
import lib.stremio.addons as addons_module
from lib.stremio.addons import AddonError
from tests.kodistubs import install_kodi_stubs

_RELOAD_MODULE_NAMES = (
    'lib.ui.compat', 'lib.ui.uicommon', 'lib.ui.infowindow', 'lib.ui.detailwindow', 'lib.ui.searchwindow',
)


class _FakeStore:
    """Fake `lib.store.Store`: an in-memory search-history list plus
    `get_addons()`'s backing list. `add_search_query`/`clear_search_history`
    reproduce the real Store's move-to-front dedup and clear contract (see
    lib/store.py, not touched by this change) closely enough that
    `_run_search`'s post-search `_reload()` reflects the just-recorded
    query in the same order the real Store would; `.search_queries`/
    `.cleared` additionally record every call so a test can assert
    exactly what was persisted."""

    def __init__(self, addons=None, history=None):
        self._addons = addons or []
        self._history = list(history or [])
        self.search_queries = []  # [query, ...] - every add_search_query() call
        self.cleared = 0          # clear_search_history() call count

    def get_addons(self):
        return self._addons

    def get_search_history(self):
        return list(self._history)

    def add_search_query(self, query):
        self.search_queries.append(query)
        query = (query or '').strip()
        if not query:
            return
        self._history = [q for q in self._history if q.lower() != query.lower()]
        self._history.insert(0, query)

    def clear_search_history(self):
        self.cleared += 1
        self._history = []


class _FakeAddonClient:
    """Fake `lib.stremio.addons.AddonClient`. `catalog_results` maps
    transport_url -> a list of metas, or an Exception instance to raise
    instead (standing in for an addon-request failure). `.calls` records
    every `catalog(transport, ctype, cid, extra=...)` invocation so a test
    can assert exactly which catalogs were queried (and with what
    `extra`)."""

    def __init__(self, catalog_results):
        self._catalog_results = catalog_results
        self.calls = []

    def catalog(self, transport, ctype, cid, extra=None):
        self.calls.append((transport, ctype, cid, extra))
        result = self._catalog_results[transport]
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture
def load_searchwindow():
    """Factory fixture: `load_searchwindow(**kwargs)` installs fresh stubs
    (via tests.kodistubs.install_kodi_stubs) reloading lib.ui.compat/
    lib.ui.uicommon/lib.ui.infowindow/lib.ui.detailwindow/
    lib.ui.searchwindow, and returns a namespace with `.searchwindow`,
    `.compat`, `.infowindow`, `.detailwindow`, and `.env`. Every call is
    torn down automatically, in reverse order, at test end."""
    with contextlib.ExitStack() as stack:
        def _load(**kwargs):
            return stack.enter_context(install_kodi_stubs(reload=_RELOAD_MODULE_NAMES, **kwargs))

        yield _load


def _make_window(searchwindow_mod):
    return searchwindow_mod.SearchWindow('SearchWindow.xml', '/addon/path', 'Default', '720p')


def _wire_store(monkeypatch, store):
    monkeypatch.setattr(store_module, 'Store', lambda *a, **k: store)


def _wire_client(monkeypatch, client):
    monkeypatch.setattr(addons_module, 'AddonClient', lambda *a, **k: client)


def _search_catalog_descriptor(transport, name='Addon'):
    return {
        'transportUrl': transport,
        'manifest': {'name': name, 'catalogs': [{'type': 'movie', 'id': 'search', 'extra': [{'name': 'search'}]}]},
    }


# ---------------------------------------------------------------------------
# SearchWindow.onInit() / _reload() - item building
# ---------------------------------------------------------------------------


def test_reload_builds_only_the_new_search_row_when_history_is_empty(load_searchwindow, monkeypatch):
    ctx = load_searchwindow()
    _wire_store(monkeypatch, _FakeStore())
    win = _make_window(ctx.searchwindow)

    win.onInit()

    items = win.getControl(ctx.searchwindow.LIST).items
    assert len(items) == 1
    assert items[0].getProperty('position') == 'new'
    assert items[0].getLabel() == 'STR30042'
    assert items[0].label2 == 'STR30043'
    assert win.getFocusId() == ctx.searchwindow.LIST


def test_reload_builds_new_row_plus_history_rows_plus_trailing_clear_row(load_searchwindow, monkeypatch):
    ctx = load_searchwindow()
    _wire_store(monkeypatch, _FakeStore(history=['batman', 'robin']))
    win = _make_window(ctx.searchwindow)

    win.onInit()

    items = win.getControl(ctx.searchwindow.LIST).items
    assert [item.getLabel() for item in items] == ['STR30042', 'batman', 'robin', 'STR30044']
    assert [item.getProperty('position') for item in items] == ['new', '0', '1', 'clear']
    assert items[1].label2 == 'STR30045'
    assert items[2].label2 == 'STR30045'


# ---------------------------------------------------------------------------
# SearchWindow.onClick() - dispatch
# ---------------------------------------------------------------------------


def test_onclick_ignores_control_ids_other_than_list(load_searchwindow, monkeypatch):
    ctx = load_searchwindow()
    _wire_store(monkeypatch, _FakeStore())
    win = _make_window(ctx.searchwindow)
    win.onInit()
    calls = []
    monkeypatch.setattr(win, '_new_search', lambda: calls.append('new'))

    win.onClick(9999)

    assert calls == []


def test_onclick_list_with_no_focused_item_does_not_crash(load_searchwindow, monkeypatch):
    ctx = load_searchwindow()
    _wire_store(monkeypatch, _FakeStore())
    win = _make_window(ctx.searchwindow)
    # No onInit() call -> the list control is never populated.

    win.onClick(ctx.searchwindow.LIST)  # must not raise


def test_onclick_new_position_dispatches_to_new_search(load_searchwindow, monkeypatch):
    ctx = load_searchwindow()
    _wire_store(monkeypatch, _FakeStore())
    win = _make_window(ctx.searchwindow)
    win.onInit()  # focused row defaults to index 0, the New-search row
    calls = []
    monkeypatch.setattr(win, '_new_search', lambda: calls.append('new'))

    win.onClick(ctx.searchwindow.LIST)

    assert calls == ['new']


def test_onclick_clear_position_dispatches_to_clear_history(load_searchwindow, monkeypatch):
    ctx = load_searchwindow()
    _wire_store(monkeypatch, _FakeStore(history=['batman']))
    win = _make_window(ctx.searchwindow)
    win.onInit()
    win.getControl(ctx.searchwindow.LIST).selected_index = 2  # the trailing Clear row
    calls = []
    monkeypatch.setattr(win, '_clear_history', lambda: calls.append('clear'))

    win.onClick(ctx.searchwindow.LIST)

    assert calls == ['clear']


def test_onclick_numeric_position_reruns_that_historys_exact_query(load_searchwindow, monkeypatch):
    ctx = load_searchwindow()
    _wire_store(monkeypatch, _FakeStore(history=['batman', 'robin']))
    win = _make_window(ctx.searchwindow)
    win.onInit()
    win.getControl(ctx.searchwindow.LIST).selected_index = 2  # the 'robin' row
    calls = []
    monkeypatch.setattr(win, '_run_search', lambda query: calls.append(query))

    win.onClick(ctx.searchwindow.LIST)

    assert calls == ['robin']


# ---------------------------------------------------------------------------
# SearchWindow._new_search()
# ---------------------------------------------------------------------------


def test_new_search_cancelled_dialog_never_runs_search_or_touches_the_store(load_searchwindow, monkeypatch):
    ctx = load_searchwindow()  # default dialog_inputs=None -> Dialog.input() returns ''
    store = _FakeStore()
    _wire_store(monkeypatch, store)
    win = _make_window(ctx.searchwindow)
    win.onInit()
    calls = []
    monkeypatch.setattr(win, '_run_search', lambda query: calls.append(query))

    win._new_search()

    assert calls == []
    assert store.search_queries == []
    assert ctx.env.dialog_input_prompts == ['STR30001']


def test_new_search_with_a_query_runs_search_with_it(load_searchwindow, monkeypatch):
    ctx = load_searchwindow(dialog_inputs=['batman'])
    _wire_store(monkeypatch, _FakeStore())
    win = _make_window(ctx.searchwindow)
    win.onInit()
    calls = []
    monkeypatch.setattr(win, '_run_search', lambda query: calls.append(query))

    win._new_search()

    assert calls == ['batman']


# ---------------------------------------------------------------------------
# SearchWindow._run_search() - aggregation, error handling, history recording
# ---------------------------------------------------------------------------


def test_run_search_addonerror_from_one_addon_is_skipped_others_still_aggregate(load_searchwindow, monkeypatch):
    ctx = load_searchwindow()
    transport_a = 'https://a.example/manifest.json'
    transport_b = 'https://b.example/manifest.json'
    store = _FakeStore(addons=[
        _search_catalog_descriptor(transport_a, 'A'),
        _search_catalog_descriptor(transport_b, 'B'),
    ])
    _wire_store(monkeypatch, store)
    metas_b = [{'id': 'tt1', 'name': 'Batman', 'type': 'movie'}]
    client = _FakeAddonClient(catalog_results={
        transport_a: AddonError('upstream down'),
        transport_b: metas_b,
    })
    _wire_client(monkeypatch, client)
    win = _make_window(ctx.searchwindow)
    win.onInit()
    captured = {}

    def _fake_open_showcase(metas):
        captured['metas'] = metas
        return None

    monkeypatch.setattr(ctx.infowindow, 'open_showcase', _fake_open_showcase)

    win._run_search('batman')

    assert captured['metas'] == metas_b
    assert store.search_queries == ['batman']
    assert client.calls == [
        (transport_a, 'movie', 'search', [('search', 'batman')]),
        (transport_b, 'movie', 'search', [('search', 'batman')]),
    ]
    assert any('failed' in msg and transport_a in msg for msg, _level in ctx.env.log_calls)


def test_run_search_records_query_even_when_every_addon_fails(load_searchwindow, monkeypatch):
    ctx = load_searchwindow()
    transport = 'https://a.example/manifest.json'
    store = _FakeStore(addons=[_search_catalog_descriptor(transport)])
    _wire_store(monkeypatch, store)
    _wire_client(monkeypatch, _FakeAddonClient(catalog_results={transport: AddonError('upstream down')}))
    win = _make_window(ctx.searchwindow)
    win.onInit()

    win._run_search('nomatch')

    assert store.search_queries == ['nomatch']
    assert ctx.env.notifications == [('Rivulet', 'STR30030', 'info', 4000)]


def test_run_search_no_results_notifies_and_does_not_open_the_coverflow(load_searchwindow, monkeypatch):
    ctx = load_searchwindow()
    store = _FakeStore(addons=[])
    _wire_store(monkeypatch, store)
    _wire_client(monkeypatch, _FakeAddonClient(catalog_results={}))
    win = _make_window(ctx.searchwindow)
    win.onInit()
    opened = []
    monkeypatch.setattr(ctx.infowindow, 'open_showcase', lambda metas: opened.append(metas))

    win._run_search('nomatch')

    assert opened == []
    assert ctx.env.notifications == [('Rivulet', 'STR30030', 'info', 4000)]
    assert win.closed is False


def test_run_search_reloads_the_list_after_the_fetch_loop(load_searchwindow, monkeypatch):
    """The just-recorded query must show up as a history row if the user
    backs out without picking anything - even on an empty-results run
    (a search is worth remembering even if it comes up empty, e.g. a
    flaky addon)."""
    ctx = load_searchwindow()
    store = _FakeStore(addons=[])
    _wire_store(monkeypatch, store)
    _wire_client(monkeypatch, _FakeAddonClient(catalog_results={}))
    win = _make_window(ctx.searchwindow)
    win.onInit()

    win._run_search('batman')

    items = win.getControl(ctx.searchwindow.LIST).items
    assert [item.getLabel() for item in items] == ['STR30042', 'batman', 'STR30044']


def test_run_search_nonempty_aggregate_opens_the_coverflow(load_searchwindow, monkeypatch):
    ctx = load_searchwindow()
    transport = 'https://a.example/manifest.json'
    store = _FakeStore(addons=[_search_catalog_descriptor(transport)])
    _wire_store(monkeypatch, store)
    metas = [{'id': 'tt1', 'name': 'Batman', 'type': 'movie'}]
    _wire_client(monkeypatch, _FakeAddonClient(catalog_results={transport: metas}))
    win = _make_window(ctx.searchwindow)
    win.onInit()
    captured = {}

    def fake_open_showcase(passed_metas):
        captured['metas'] = passed_metas
        return None

    monkeypatch.setattr(ctx.infowindow, 'open_showcase', fake_open_showcase)

    win._run_search('batman')

    assert captured['metas'] == metas
    assert win.closed is False


def test_run_search_no_selection_from_the_coverflow_does_not_close(load_searchwindow, monkeypatch):
    ctx = load_searchwindow()
    transport = 'https://a.example/manifest.json'
    store = _FakeStore(addons=[_search_catalog_descriptor(transport)])
    _wire_store(monkeypatch, store)
    metas = [{'id': 'tt1', 'name': 'Batman', 'type': 'movie'}]
    _wire_client(monkeypatch, _FakeAddonClient(catalog_results={transport: metas}))
    win = _make_window(ctx.searchwindow)
    win.onInit()
    monkeypatch.setattr(ctx.infowindow, 'open_showcase', lambda m: None)

    win._run_search('batman')

    assert win.should_close_caller is False
    assert win.closed is False


def test_run_search_selection_that_opens_detail_sets_should_close_caller_and_closes(load_searchwindow, monkeypatch):
    ctx = load_searchwindow()
    transport = 'https://a.example/manifest.json'
    store = _FakeStore(addons=[_search_catalog_descriptor(transport)])
    _wire_store(monkeypatch, store)
    metas = [{'id': 'tt9', 'name': 'Batman', 'type': 'movie'}]
    _wire_client(monkeypatch, _FakeAddonClient(catalog_results={transport: metas}))
    win = _make_window(ctx.searchwindow)
    win.onInit()
    monkeypatch.setattr(ctx.infowindow, 'open_showcase', lambda m: m[0])
    captured = {}

    def fake_open_detail(stype, sid):
        captured['args'] = (stype, sid)
        return True

    monkeypatch.setattr(ctx.detailwindow, 'open_detail', fake_open_detail)

    win._run_search('batman')

    assert captured['args'] == ('movie', 'tt9')
    assert win.should_close_caller is True
    assert win.closed is True


def test_run_search_selection_without_a_type_falls_back_to_movie(load_searchwindow, monkeypatch):
    ctx = load_searchwindow()
    transport = 'https://a.example/manifest.json'
    store = _FakeStore(addons=[_search_catalog_descriptor(transport)])
    _wire_store(monkeypatch, store)
    metas = [{'id': 'tt9', 'name': 'No Type'}]  # no 'type' key on the selected meta
    _wire_client(monkeypatch, _FakeAddonClient(catalog_results={transport: metas}))
    win = _make_window(ctx.searchwindow)
    win.onInit()
    monkeypatch.setattr(ctx.infowindow, 'open_showcase', lambda m: m[0])
    captured = {}

    def fake_open_detail(stype, sid):
        captured['args'] = (stype, sid)
        return True

    monkeypatch.setattr(ctx.detailwindow, 'open_detail', fake_open_detail)

    win._run_search('batman')

    assert captured['args'] == ('movie', 'tt9')


def test_run_search_detail_returning_false_does_not_close(load_searchwindow, monkeypatch):
    ctx = load_searchwindow()
    transport = 'https://a.example/manifest.json'
    store = _FakeStore(addons=[_search_catalog_descriptor(transport)])
    _wire_store(monkeypatch, store)
    metas = [{'id': 'tt9', 'name': 'Batman', 'type': 'movie'}]
    _wire_client(monkeypatch, _FakeAddonClient(catalog_results={transport: metas}))
    win = _make_window(ctx.searchwindow)
    win.onInit()
    monkeypatch.setattr(ctx.infowindow, 'open_showcase', lambda m: m[0])
    monkeypatch.setattr(ctx.detailwindow, 'open_detail', lambda stype, sid: False)

    win._run_search('batman')

    assert win.should_close_caller is False
    assert win.closed is False


def test_run_search_coverflow_open_failure_is_logged_notified_and_does_not_close(load_searchwindow, monkeypatch):
    ctx = load_searchwindow()
    transport = 'https://a.example/manifest.json'
    store = _FakeStore(addons=[_search_catalog_descriptor(transport)])
    _wire_store(monkeypatch, store)
    metas = [{'id': 'tt9', 'name': 'Batman', 'type': 'movie'}]
    _wire_client(monkeypatch, _FakeAddonClient(catalog_results={transport: metas}))
    win = _make_window(ctx.searchwindow)
    win.onInit()

    def _raise(passed_metas):
        raise RuntimeError('skin failed to parse')

    monkeypatch.setattr(ctx.infowindow, 'open_showcase', _raise)

    win._run_search('batman')

    assert win.should_close_caller is False
    assert win.closed is False
    assert ctx.env.notifications == [('Rivulet', 'STR30032', 'info', 4000)]


# ---------------------------------------------------------------------------
# SearchWindow._clear_history()
# ---------------------------------------------------------------------------


def test_clear_history_declined_leaves_history_untouched_and_does_not_reload(load_searchwindow, monkeypatch):
    ctx = load_searchwindow(dialog_yesno=[False])
    store = _FakeStore(history=['batman'])
    _wire_store(monkeypatch, store)
    win = _make_window(ctx.searchwindow)
    win.onInit()

    win._clear_history()

    assert store.cleared == 0
    items = win.getControl(ctx.searchwindow.LIST).items
    assert [item.getLabel() for item in items] == ['STR30042', 'batman', 'STR30044']


def test_clear_history_confirmed_clears_and_reloads_to_new_search_only(load_searchwindow, monkeypatch):
    ctx = load_searchwindow(dialog_yesno=[True])
    store = _FakeStore(history=['batman'])
    _wire_store(monkeypatch, store)
    win = _make_window(ctx.searchwindow)
    win.onInit()

    win._clear_history()

    assert store.cleared == 1
    assert ctx.env.dialog_yesno_prompts == [('STR30044', 'STR30046')]
    items = win.getControl(ctx.searchwindow.LIST).items
    assert len(items) == 1
    assert items[0].getProperty('position') == 'new'


# ---------------------------------------------------------------------------
# SearchWindow.start()
# ---------------------------------------------------------------------------


def test_start_resets_should_close_caller_calls_domodal_once_and_returns_it(load_searchwindow, monkeypatch):
    ctx = load_searchwindow()
    _wire_store(monkeypatch, _FakeStore())
    win = _make_window(ctx.searchwindow)
    win.should_close_caller = True  # leftover from a previous run

    result = win.start()

    assert result is False
    assert win.should_close_caller is False
    assert win.modal_calls == 1


def test_start_returns_true_when_the_modal_run_sets_should_close_caller(load_searchwindow, monkeypatch):
    ctx = load_searchwindow()
    transport = 'https://a.example/manifest.json'
    store = _FakeStore(addons=[_search_catalog_descriptor(transport)], history=['batman'])
    _wire_store(monkeypatch, store)
    metas = [{'id': 'tt9', 'name': 'Batman', 'type': 'movie'}]
    _wire_client(monkeypatch, _FakeAddonClient(catalog_results={transport: metas}))
    win = _make_window(ctx.searchwindow)
    monkeypatch.setattr(ctx.infowindow, 'open_showcase', lambda m: m[0])
    monkeypatch.setattr(ctx.detailwindow, 'open_detail', lambda stype, sid: True)

    # The fake doModal() is a no-op counter; simulate what a real modal
    # event loop would drive around it (onInit(), the user picking the
    # 'batman' history row), exactly as Kodi calls back into the window.
    real_domodal = win.doModal

    def fake_domodal():
        real_domodal()
        win.onInit()
        win.getControl(ctx.searchwindow.LIST).selected_index = 1  # the 'batman' history row
        win.onClick(ctx.searchwindow.LIST)

    win.doModal = fake_domodal

    result = win.start()

    assert result is True
    assert win.modal_calls == 1


# ---------------------------------------------------------------------------
# open_search()
# ---------------------------------------------------------------------------


def test_open_search_opens_window_against_the_right_skin_and_returns_start_result(load_searchwindow, monkeypatch):
    ctx = load_searchwindow(addon_info={'path': '/addon/path'})
    captured = {}

    class RecordingWindow(ctx.searchwindow.SearchWindow):
        def __init__(self, *args, **kwargs):
            captured['init_args'] = args
            super().__init__(*args, **kwargs)

        def start(self):
            captured['started'] = True
            return True

    monkeypatch.setattr(ctx.searchwindow, 'SearchWindow', RecordingWindow)

    result = ctx.searchwindow.open_search()

    assert result is True
    assert captured['init_args'] == ('SearchWindow.xml', '/addon/path', 'Default', '720p')
    assert captured['started'] is True


def test_open_search_window_is_closed_exactly_once_when_start_raises(load_searchwindow, monkeypatch):
    ctx = load_searchwindow(addon_info={'path': '/addon/path'})
    captured = {}

    class ExplodingWindow(ctx.searchwindow.SearchWindow):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.close_calls = 0
            captured['window'] = self

        def close(self):
            self.close_calls += 1
            super().close()

        def start(self):
            # Stands in for a crash inside onInit()/onClick() while the
            # modal loop is running - self.close() (the window's own,
            # normal-path close) never gets a chance to run.
            raise RuntimeError('onInit blew up')

    monkeypatch.setattr(ctx.searchwindow, 'SearchWindow', ExplodingWindow)

    result = ctx.searchwindow.open_search()

    assert result is False
    win = captured['window']
    assert win.close_calls == 1
    assert win.closed is True
    assert ctx.env.notifications == [('Rivulet', 'STR30032', 'info', 4000)]
