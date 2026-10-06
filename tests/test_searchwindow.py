# SPDX-FileCopyrightText: 2026 M0Rf30
# SPDX-License-Identifier: MIT

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

lib.ui.searchwindow imports xbmcgui, lib.ui.uicommon, and `get_store`/
`get_client` (from lib.ui.dependencies) at module scope; every other
collaborator (`lib.stremio.addons.AddonError`/`iter_catalogs`,
`lib.ui.compat.L`/`log`/`notify`, `lib.ui.infowindow.open_showcase`,
`lib.ui.detailwindow.open_detail`) is imported lazily inside the method
that needs it - so this file fakes the shared Store/AddonClient
providers by assigning directly to `searchwindow.get_store`/
`searchwindow.get_client` (the same way test_addonswindow.py wires
`_wire_store`/`_wire_client`, and tests/test_views.py wires
`views.get_store`/`views.get_client`), rather than monkeypatching
`lib.store`/`lib.stremio.addons` or reloading lib.ui.searchwindow's own
module-scope bindings.

SearchWindow._run_search() also lazily `from lib.ui.infowindow import
open_showcase` / `from lib.ui.detailwindow import open_detail`, exactly
like `CatalogPickerWindow._open_catalog` does, so load_searchwindow
reloads lib.ui.infowindow/lib.ui.detailwindow fresh alongside
lib.ui.compat/lib.ui.dependencies/lib.ui.uicommon/lib.ui.searchwindow to
get handles (`ctx.infowindow`/`ctx.detailwindow`) this file monkeypatches
directly - copying tests/test_catalogpicker.py's exact mechanism.

SearchWindow.onInit()/onClick() are called directly here, never through a
real modal event loop, exactly like test_catalogpicker.py drives
CatalogPickerWindow: the fake WindowXML.doModal() is a no-op counter, and
getControl()/setFocusId() are plain in-memory fakes. SearchWindow.xml's
actual skin rendering is Kodi-skin-engine-only and is NOT, and cannot be,
exercised by this suite.
"""
import contextlib
import threading
import time
import types

import pytest

from lib.stremio.addons import AddonError
from tests.conftest import make_window, stub_confirm, wire_client, wire_store
from tests.kodistubs import install_kodi_stubs

_RELOAD_MODULE_NAMES = (
    'lib.ui.compat', 'lib.ui.dependencies', 'lib.ui.uicommon', 'lib.ui.dialogs',
    'lib.ui.infowindow', 'lib.ui.detailwindow', 'lib.ui.searchwindow',
)


class _FakeStore:
    """Fake `lib.store.Store`: an in-memory search-history list plus
    `get_addons()`/`get_enabled_addons()`'s backing list. `add_search_query`/`clear_search_history`
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

    def get_enabled_addons(self):
        return [a for a in self._addons if not (a.get('flags') or {}).get('disabled')]

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
    return make_window(searchwindow_mod.SearchWindow)


def _wire_store(searchwindow_mod, store):
    wire_store(searchwindow_mod, store)


def _wire_client(searchwindow_mod, client):
    wire_client(searchwindow_mod, client)


def _search_catalog_descriptor(transport, name='Addon'):
    return {
        'transportUrl': transport,
        'manifest': {'name': name, 'catalogs': [{'type': 'movie', 'id': 'search', 'extra': [{'name': 'search'}]}]},
    }


def _adult_search_catalog_descriptor(transport, name='Adult Addon'):
    """Same shape as `_search_catalog_descriptor()`, but the catalog's
    own id/name carry an adult marker (`xxx`, matching
    tests/test_views.py's `_ADULT_CATALOG`) - flags it adult by itself,
    independent of any meta it returns."""
    return {
        'transportUrl': transport,
        'manifest': {'name': name, 'catalogs': [
            {'type': 'movie', 'id': 'xxx-list', 'name': 'XXX Movies', 'extra': [{'name': 'search'}]},
        ]},
    }


# ---------------------------------------------------------------------------
# SearchWindow.onInit() / _reload() - item building
# ---------------------------------------------------------------------------


def test_reload_builds_only_the_new_search_row_when_history_is_empty(load_searchwindow, monkeypatch):
    ctx = load_searchwindow()
    _wire_store(ctx.searchwindow, _FakeStore())
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
    _wire_store(ctx.searchwindow, _FakeStore(history=['batman', 'robin']))
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
    _wire_store(ctx.searchwindow, _FakeStore())
    win = _make_window(ctx.searchwindow)
    win.onInit()
    calls = []
    monkeypatch.setattr(win, '_new_search', lambda: calls.append('new'))

    win.onClick(9999)

    assert calls == []


def test_onclick_list_with_no_focused_item_does_not_crash(load_searchwindow, monkeypatch):
    ctx = load_searchwindow()
    _wire_store(ctx.searchwindow, _FakeStore())
    win = _make_window(ctx.searchwindow)
    # No onInit() call -> the list control is never populated.

    win.onClick(ctx.searchwindow.LIST)  # must not raise


def test_onclick_new_position_dispatches_to_new_search(load_searchwindow, monkeypatch):
    ctx = load_searchwindow()
    _wire_store(ctx.searchwindow, _FakeStore())
    win = _make_window(ctx.searchwindow)
    win.onInit()  # focused row defaults to index 0, the New-search row
    calls = []
    monkeypatch.setattr(win, '_new_search', lambda: calls.append('new'))

    win.onClick(ctx.searchwindow.LIST)

    assert calls == ['new']


def test_onclick_clear_position_dispatches_to_clear_history(load_searchwindow, monkeypatch):
    ctx = load_searchwindow()
    _wire_store(ctx.searchwindow, _FakeStore(history=['batman']))
    win = _make_window(ctx.searchwindow)
    win.onInit()
    win.getControl(ctx.searchwindow.LIST).selected_index = 2  # the trailing Clear row
    calls = []
    monkeypatch.setattr(win, '_clear_history', lambda: calls.append('clear'))

    win.onClick(ctx.searchwindow.LIST)

    assert calls == ['clear']


def test_onclick_numeric_position_reruns_that_historys_exact_query(load_searchwindow, monkeypatch):
    ctx = load_searchwindow()
    _wire_store(ctx.searchwindow, _FakeStore(history=['batman', 'robin']))
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
    store = _FakeStore(addons=[_search_catalog_descriptor('https://a.example/manifest.json')])
    _wire_store(ctx.searchwindow, store)
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
    _wire_store(ctx.searchwindow, _FakeStore(addons=[_search_catalog_descriptor('https://a.example/manifest.json')]))
    win = _make_window(ctx.searchwindow)
    win.onInit()
    calls = []
    monkeypatch.setattr(win, '_run_search', lambda query: calls.append(query))

    win._new_search()

    assert calls == ['batman']


def test_new_search_strips_the_typed_query_before_searching_and_recording_it(load_searchwindow, monkeypatch):
    ctx = load_searchwindow(dialog_inputs=['  batman \u00a0'])
    _wire_store(ctx.searchwindow, _FakeStore(addons=[_search_catalog_descriptor('https://a.example/manifest.json')]))
    win = _make_window(ctx.searchwindow)
    win.onInit()
    calls = []
    monkeypatch.setattr(win, '_run_search', lambda query: calls.append(query))

    win._new_search()

    assert calls == ['batman']


def test_new_search_blank_entry_is_a_cancel_not_a_search_for_nothing(load_searchwindow, monkeypatch):
    ctx = load_searchwindow(dialog_inputs=['   '])
    store = _FakeStore(addons=[_search_catalog_descriptor('https://a.example/manifest.json')])
    _wire_store(ctx.searchwindow, store)
    win = _make_window(ctx.searchwindow)
    win.onInit()
    calls = []
    monkeypatch.setattr(win, '_run_search', lambda query: calls.append(query))

    win._new_search()

    assert calls == []
    assert store.search_queries == []


# ---------------------------------------------------------------------------
# run_query() - the fan-out extracted from _run_search() for reuse by
# lib.ui.infowindow.open_credits_picker()'s "person" (search-kind link)
# dispatch, the contract's second caller.
# ---------------------------------------------------------------------------


def test_run_query_returns_metas_with_type_defaulted_from_the_catalog(load_searchwindow):
    ctx = load_searchwindow()
    transport = 'https://a.example/manifest.json'
    store = _FakeStore(addons=[_search_catalog_descriptor(transport)])
    metas = [{'id': 'tt1', 'name': 'No Type'}, {'id': 'tt2', 'name': 'Has Type', 'type': 'series'}]
    client = _FakeAddonClient(catalog_results={transport: metas})

    result = ctx.searchwindow.run_query(store, client, 'batman')

    assert [m.get('type') for m in result] == ['movie', 'series']


def test_run_query_isolates_a_failing_addon_and_still_aggregates_the_rest(load_searchwindow):
    ctx = load_searchwindow()
    transport_a = 'https://a.example/manifest.json'
    transport_b = 'https://b.example/manifest.json'
    store = _FakeStore(addons=[
        _search_catalog_descriptor(transport_a, 'A'),
        _search_catalog_descriptor(transport_b, 'B'),
    ])
    metas_b = [{'id': 'tt1', 'name': 'Batman', 'type': 'movie'}]
    client = _FakeAddonClient(catalog_results={
        transport_a: AddonError('upstream down'),
        transport_b: metas_b,
    })

    result = ctx.searchwindow.run_query(store, client, 'batman')

    assert result == metas_b
    assert any('a.example' in msg and 'AddonError' in msg for msg, _level in ctx.env.log_calls)
    assert not any('upstream down' in msg or transport_a in msg for msg, _level in ctx.env.log_calls)


def test_run_query_writes_no_search_history(load_searchwindow):
    ctx = load_searchwindow()
    store = _FakeStore(addons=[])
    client = _FakeAddonClient(catalog_results={})

    ctx.searchwindow.run_query(store, client, 'batman')

    assert store.search_queries == []


def test_run_query_sends_no_request_to_a_disabled_search_capable_addon(load_searchwindow):
    """A search-capable addon Store.get_enabled_addons() filters out as
    disabled must receive zero catalog requests - run_query() must fan
    out through get_enabled_addons(), not get_addons()."""
    ctx = load_searchwindow()
    enabled_transport = 'https://a.example/manifest.json'
    disabled_transport = 'https://b.example/manifest.json'
    disabled = _search_catalog_descriptor(disabled_transport, 'B')
    disabled['flags'] = {'disabled': True}
    store = _FakeStore(addons=[_search_catalog_descriptor(enabled_transport, 'A'), disabled])
    client = _FakeAddonClient(catalog_results={enabled_transport: [], disabled_transport: []})

    ctx.searchwindow.run_query(store, client, 'batman')

    assert [call[0] for call in client.calls] == [enabled_transport]


# ---------------------------------------------------------------------------
# run_query() - home_hide_adult filtering (reuses lib.stremio.contentrating,
# same policy as lib.ui.views.iter_catalog_pages()).
# ---------------------------------------------------------------------------


def test_run_query_never_fetches_an_adult_catalog_by_default(load_searchwindow):
    """home_hide_adult defaults to True - an adult-flagged catalog
    declared by a search-capable addon must never be fetched: not one
    HTTP request is made for it."""
    ctx = load_searchwindow()
    transport = 'https://a.example/manifest.json'
    store = _FakeStore(addons=[_adult_search_catalog_descriptor(transport)])
    client = _FakeAddonClient(catalog_results={transport: [{'id': 'ttx', 'name': 'Adult'}]})

    result = ctx.searchwindow.run_query(store, client, 'batman')

    assert client.calls == []
    assert result == []


def test_run_query_fetches_and_keeps_an_adult_catalog_when_setting_is_off(load_searchwindow):
    ctx = load_searchwindow(settings={'home_hide_adult': 'false'})
    transport = 'https://a.example/manifest.json'
    store = _FakeStore(addons=[_adult_search_catalog_descriptor(transport)])
    metas = [{'id': 'ttx', 'name': 'Adult', 'type': 'movie'}]
    client = _FakeAddonClient(catalog_results={transport: metas})

    result = ctx.searchwindow.run_query(store, client, 'batman')

    assert [call[0] for call in client.calls] == [transport]
    assert result == metas


def test_run_query_drops_adult_flagged_metas_from_an_ordinary_catalog_by_default(load_searchwindow):
    """The catalog itself isn't adult, but one returned meta is flagged
    `adult: True` - it must be dropped, the rest kept."""
    ctx = load_searchwindow()
    transport = 'https://a.example/manifest.json'
    store = _FakeStore(addons=[_search_catalog_descriptor(transport)])
    metas = [{'id': 'tt1', 'name': 'Batman', 'type': 'movie'},
             {'id': 'tt2', 'name': 'Adult One', 'type': 'movie', 'adult': True}]
    client = _FakeAddonClient(catalog_results={transport: metas})

    result = ctx.searchwindow.run_query(store, client, 'batman')

    assert [m['id'] for m in result] == ['tt1']


def test_run_query_keeps_adult_flagged_metas_when_setting_is_off(load_searchwindow):
    ctx = load_searchwindow(settings={'home_hide_adult': 'false'})
    transport = 'https://a.example/manifest.json'
    store = _FakeStore(addons=[_search_catalog_descriptor(transport)])
    metas = [{'id': 'tt1', 'name': 'Batman', 'type': 'movie'},
             {'id': 'tt2', 'name': 'Adult One', 'type': 'movie', 'adult': True}]
    client = _FakeAddonClient(catalog_results={transport: metas})

    result = ctx.searchwindow.run_query(store, client, 'batman')

    assert {m['id'] for m in result} == {'tt1', 'tt2'}


# ---------------------------------------------------------------------------
# _rank_by_credit() - moved from the deleted lib.ui.views.search(); applied
# inside run_query() to every collected meta before it is returned.
# ---------------------------------------------------------------------------


def test_rank_by_credit_ranks_a_cast_credit_first_without_dropping_the_title_only_match(load_searchwindow):
    ctx = load_searchwindow()
    title_only = {'id': 'tt1', 'name': 'Listen to Me Marlon'}
    cast_credit = {'id': 'tt2', 'name': 'One-Eyed Jacks', 'cast': ['Marlon Brando']}

    result = ctx.searchwindow._rank_by_credit([title_only, cast_credit], 'Marlon Brando')

    assert result[0] is cast_credit
    assert title_only in result


def test_rank_by_credit_ranks_a_director_credit_first(load_searchwindow):
    ctx = load_searchwindow()
    title_only = {'id': 'tt1', 'name': 'Listen to Me Marlon'}
    director_credit = {'id': 'tt2', 'name': 'One-Eyed Jacks', 'director': ['Marlon Brando']}

    result = ctx.searchwindow._rank_by_credit([title_only, director_credit], 'Marlon Brando')

    assert result[0] is director_credit
    assert title_only in result


def test_rank_by_credit_ranks_a_writer_credit_first(load_searchwindow):
    ctx = load_searchwindow()
    title_only = {'id': 'tt1', 'name': 'Some Movie'}
    writer_credit = {'id': 'tt2', 'name': 'Another Movie', 'writer': ['Charlie Kaufman']}

    result = ctx.searchwindow._rank_by_credit([title_only, writer_credit], 'Charlie Kaufman')

    assert result[0] is writer_credit
    assert title_only in result


def test_rank_by_credit_matching_is_case_insensitive(load_searchwindow):
    ctx = load_searchwindow()
    title_only = {'id': 'tt1', 'name': 'Unrelated'}
    cast_credit = {'id': 'tt2', 'name': 'Credited', 'cast': ['marlon brando']}

    result = ctx.searchwindow._rank_by_credit([title_only, cast_credit], 'MARLON BRANDO')

    assert result[0] is cast_credit


def test_rank_by_credit_is_stable_within_each_group(load_searchwindow):
    ctx = load_searchwindow()
    credited_a = {'id': 'tt1', 'name': 'Credited A', 'cast': ['Marlon Brando']}
    credited_b = {'id': 'tt2', 'name': 'Credited B', 'director': ['Marlon Brando']}
    uncredited_a = {'id': 'tt3', 'name': 'Uncredited A'}
    uncredited_b = {'id': 'tt4', 'name': 'Uncredited B'}
    metas = [uncredited_a, credited_a, uncredited_b, credited_b]

    result = ctx.searchwindow._rank_by_credit(metas, 'Marlon Brando')

    assert result == [credited_a, credited_b, uncredited_a, uncredited_b]


def test_rank_by_credit_tolerates_malformed_credit_fields_and_an_empty_query(load_searchwindow):
    ctx = load_searchwindow()
    metas = [
        {'id': 'tt1', 'name': 'String cast', 'cast': 'Marlon Brando'},
        {'id': 'tt2', 'name': 'Non-string director entries', 'director': [None, 42, {'name': 'x'}]},
        {'id': 'tt3', 'name': 'Null writer', 'writer': None},
        {'id': 'tt4', 'name': 'Plain'},
    ]

    result = ctx.searchwindow._rank_by_credit(metas, '')  # must not raise

    assert result == metas


def test_run_query_ranks_the_returned_metas_by_credit(load_searchwindow):
    ctx = load_searchwindow()
    transport = 'https://a.example/manifest.json'
    store = _FakeStore(addons=[_search_catalog_descriptor(transport)])
    title_only = {'id': 'tt1', 'name': 'Listen to Me Marlon', 'type': 'movie'}
    cast_credit = {'id': 'tt2', 'name': 'One-Eyed Jacks', 'type': 'movie', 'cast': ['Marlon Brando']}
    client = _FakeAddonClient(catalog_results={transport: [title_only, cast_credit]})

    result = ctx.searchwindow.run_query(store, client, 'Marlon Brando')

    assert result[0]['id'] == 'tt2'
    assert {m['id'] for m in result} == {'tt1', 'tt2'}


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
    _wire_store(ctx.searchwindow, store)
    metas_b = [{'id': 'tt1', 'name': 'Batman', 'type': 'movie'}]
    client = _FakeAddonClient(catalog_results={
        transport_a: AddonError('upstream down'),
        transport_b: metas_b,
    })
    _wire_client(ctx.searchwindow, client)
    win = _make_window(ctx.searchwindow)
    win.onInit()
    captured = {}

    def _fake_open_showcase(metas, catalog_title=None):
        captured['metas'] = metas
        return None

    monkeypatch.setattr(ctx.infowindow, 'open_showcase', _fake_open_showcase)

    win._run_search('batman')

    assert captured['metas'] == metas_b
    assert store.search_queries == ['batman']
    assert sorted(client.calls, key=lambda call: call[0]) == [
        (transport_a, 'movie', 'search', [('search', 'batman')]),
        (transport_b, 'movie', 'search', [('search', 'batman')]),
    ]
    assert any('a.example' in msg and 'AddonError' in msg for msg, _level in ctx.env.log_calls)
    assert not any('upstream down' in msg or transport_a in msg for msg, _level in ctx.env.log_calls)


def test_run_search_addon_failure_log_never_leaks_credentials_path_or_query(load_searchwindow, monkeypatch):
    ctx = load_searchwindow()
    import xbmc
    secret_transport = 'https://user:hunter2@evil.example:8443/private/path/manifest.json?token=abc123'
    store = _FakeStore(addons=[_search_catalog_descriptor(secret_transport)])
    _wire_store(ctx.searchwindow, store)
    _wire_client(ctx.searchwindow, _FakeAddonClient(catalog_results={
        secret_transport: AddonError('GET %s failed: bad request' % secret_transport),
    }))
    win = _make_window(ctx.searchwindow)
    win.onInit()

    win._run_search('nomatch')

    all_messages = ' '.join(msg for msg, _level in ctx.env.log_calls)
    assert 'hunter2' not in all_messages
    assert 'token=abc123' not in all_messages
    assert '/private/path' not in all_messages
    assert 'bad request' not in all_messages
    error_msgs = [msg for msg, lvl in ctx.env.log_calls if lvl == xbmc.LOGERROR]
    assert any('evil.example:8443' in msg and 'AddonError' in msg for msg in error_msgs)


def test_run_search_records_query_even_when_every_addon_fails(load_searchwindow, monkeypatch):
    ctx = load_searchwindow()
    transport = 'https://a.example/manifest.json'
    store = _FakeStore(addons=[_search_catalog_descriptor(transport)])
    _wire_store(ctx.searchwindow, store)
    _wire_client(ctx.searchwindow, _FakeAddonClient(catalog_results={transport: AddonError('upstream down')}))
    win = _make_window(ctx.searchwindow)
    win.onInit()

    win._run_search('nomatch')

    assert store.search_queries == ['nomatch']
    assert ctx.env.notifications == [('Rivulet', 'STR30382', 'info', 6000)]


def test_run_search_no_results_notifies_and_does_not_open_the_coverflow(load_searchwindow, monkeypatch):
    ctx = load_searchwindow()
    store = _FakeStore(addons=[])
    _wire_store(ctx.searchwindow, store)
    _wire_client(ctx.searchwindow, _FakeAddonClient(catalog_results={}))
    win = _make_window(ctx.searchwindow)
    win.onInit()
    opened = []
    monkeypatch.setattr(ctx.infowindow, 'open_showcase', lambda metas, catalog_title=None: opened.append(metas))

    win._run_search('nomatch')

    assert opened == []
    assert ctx.env.notifications == [('Rivulet', 'STR30381', 'info', 6000)]
    assert win.closed is False


def test_run_search_all_adult_results_filtered_out_hits_the_same_no_results_path(load_searchwindow, monkeypatch):
    """Filtering must not make a populated result set look like a failed
    search with a blank coverflow - when every returned meta is adult and
    home_hide_adult is on, the window must take its existing no-results
    branch (notify + no coverflow), exactly like a genuinely empty
    search."""
    ctx = load_searchwindow()
    transport = 'https://a.example/manifest.json'
    store = _FakeStore(addons=[_search_catalog_descriptor(transport)])
    _wire_store(ctx.searchwindow, store)
    metas = [{'id': 'tt1', 'name': 'Adult One', 'type': 'movie', 'adult': True}]
    _wire_client(ctx.searchwindow, _FakeAddonClient(catalog_results={transport: metas}))
    win = _make_window(ctx.searchwindow)
    win.onInit()
    opened = []
    monkeypatch.setattr(ctx.infowindow, 'open_showcase', lambda m, catalog_title=None: opened.append(m))

    win._run_search('batman')

    assert opened == []
    assert ctx.env.notifications == [('Rivulet', 'STR30380', 'info', 4000)]
    assert win.closed is False


def test_run_search_reloads_the_list_after_the_fetch_loop(load_searchwindow, monkeypatch):
    """The just-recorded query must show up as a history row if the user
    backs out without picking anything - even on an empty-results run
    (a search is worth remembering even if it comes up empty, e.g. a
    flaky addon)."""
    ctx = load_searchwindow()
    store = _FakeStore(addons=[])
    _wire_store(ctx.searchwindow, store)
    _wire_client(ctx.searchwindow, _FakeAddonClient(catalog_results={}))
    win = _make_window(ctx.searchwindow)
    win.onInit()

    win._run_search('batman')

    items = win.getControl(ctx.searchwindow.LIST).items
    assert [item.getLabel() for item in items] == ['STR30042', 'batman', 'STR30044']


def test_run_search_nonempty_aggregate_opens_the_coverflow(load_searchwindow, monkeypatch):
    ctx = load_searchwindow()
    transport = 'https://a.example/manifest.json'
    store = _FakeStore(addons=[_search_catalog_descriptor(transport)])
    _wire_store(ctx.searchwindow, store)
    metas = [{'id': 'tt1', 'name': 'Batman', 'type': 'movie'}]
    _wire_client(ctx.searchwindow, _FakeAddonClient(catalog_results={transport: metas}))
    win = _make_window(ctx.searchwindow)
    win.onInit()
    captured = {}

    def fake_open_showcase(passed_metas, catalog_title=None):
        captured['metas'] = passed_metas
        captured['catalog_title'] = catalog_title
        return None

    monkeypatch.setattr(ctx.infowindow, 'open_showcase', fake_open_showcase)

    win._run_search('batman')

    assert captured['metas'] == metas
    assert captured['catalog_title'] == 'STR30001 \u00b7 batman'
    assert win.closed is False


def test_run_search_no_selection_from_the_coverflow_does_not_close(load_searchwindow, monkeypatch):
    ctx = load_searchwindow()
    transport = 'https://a.example/manifest.json'
    store = _FakeStore(addons=[_search_catalog_descriptor(transport)])
    _wire_store(ctx.searchwindow, store)
    metas = [{'id': 'tt1', 'name': 'Batman', 'type': 'movie'}]
    _wire_client(ctx.searchwindow, _FakeAddonClient(catalog_results={transport: metas}))
    win = _make_window(ctx.searchwindow)
    win.onInit()
    monkeypatch.setattr(ctx.infowindow, 'open_showcase', lambda m, catalog_title=None: None)

    win._run_search('batman')

    assert win.should_close_caller is False
    assert win.closed is False


def test_run_search_selection_that_opens_detail_sets_should_close_caller_and_closes(load_searchwindow, monkeypatch):
    ctx = load_searchwindow()
    transport = 'https://a.example/manifest.json'
    store = _FakeStore(addons=[_search_catalog_descriptor(transport)])
    _wire_store(ctx.searchwindow, store)
    metas = [{'id': 'tt9', 'name': 'Batman', 'type': 'movie'}]
    _wire_client(ctx.searchwindow, _FakeAddonClient(catalog_results={transport: metas}))
    win = _make_window(ctx.searchwindow)
    win.onInit()
    monkeypatch.setattr(ctx.infowindow, 'open_showcase', lambda m, catalog_title=None: m[0])
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
    _wire_store(ctx.searchwindow, store)
    metas = [{'id': 'tt9', 'name': 'No Type'}]  # no 'type' key on the selected meta
    _wire_client(ctx.searchwindow, _FakeAddonClient(catalog_results={transport: metas}))
    win = _make_window(ctx.searchwindow)
    win.onInit()
    monkeypatch.setattr(ctx.infowindow, 'open_showcase', lambda m, catalog_title=None: m[0])
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
    _wire_store(ctx.searchwindow, store)
    metas = [{'id': 'tt9', 'name': 'Batman', 'type': 'movie'}]
    _wire_client(ctx.searchwindow, _FakeAddonClient(catalog_results={transport: metas}))
    win = _make_window(ctx.searchwindow)
    win.onInit()
    monkeypatch.setattr(ctx.infowindow, 'open_showcase', lambda m, catalog_title=None: m[0])
    monkeypatch.setattr(ctx.detailwindow, 'open_detail', lambda stype, sid: False)

    win._run_search('batman')

    assert win.should_close_caller is False
    assert win.closed is False


def test_run_search_coverflow_open_failure_is_logged_notified_and_does_not_close(load_searchwindow, monkeypatch):
    ctx = load_searchwindow()
    transport = 'https://a.example/manifest.json'
    store = _FakeStore(addons=[_search_catalog_descriptor(transport)])
    _wire_store(ctx.searchwindow, store)
    metas = [{'id': 'tt9', 'name': 'Batman', 'type': 'movie'}]
    _wire_client(ctx.searchwindow, _FakeAddonClient(catalog_results={transport: metas}))
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
# run_query() - concurrent fan-out, cancel-awareness, SearchReport
# ---------------------------------------------------------------------------


class _ScriptedClient:
    """Fake `AddonClient` whose `catalog()` runs a per-transport callable
    INSIDE the worker thread `run_query()` dispatched it on - so a test
    can make one catalog block, sleep, raise or rendezvous with another,
    which a plain canned-result fake cannot."""

    def __init__(self, handlers):
        self._handlers = handlers
        self.calls = []

    def catalog(self, transport, ctype, cid, extra=None):
        self.calls.append((transport, ctype, cid, extra))
        return self._handlers[transport]()


def _addons(*names):
    """A store holding one search-capable addon per name, and their
    transport urls in the same order."""
    transports = ['https://%s.example/manifest.json' % name.lower() for name in names]
    store = _FakeStore(addons=[_search_catalog_descriptor(t, n) for t, n in zip(transports, names)])
    return store, transports


def _meta(meta_id, name=None, **extra):
    meta = {'id': meta_id, 'name': name or meta_id, 'type': 'movie'}
    meta.update(extra)
    return meta


def _raising(exc):
    """A `_ScriptedClient` handler that raises `exc` from the worker thread."""
    def _handler():
        raise exc
    return _handler


def test_run_query_requests_every_catalog_concurrently(load_searchwindow):
    """A barrier only opens once all three requests are in flight AT THE
    SAME TIME: the serial loop this replaced would sit on the first one
    until the barrier timed out, and every catalog would come back failed."""
    ctx = load_searchwindow()
    store, (ta, tb, tc) = _addons('A', 'B', 'C')
    barrier = threading.Barrier(3, timeout=5)

    def _meet(meta_id):
        def _handler():
            barrier.wait()
            return [_meta(meta_id)]
        return _handler

    client = _ScriptedClient({ta: _meet('tt1'), tb: _meet('tt2'), tc: _meet('tt3')})
    report = ctx.searchwindow.SearchReport()

    result = ctx.searchwindow.run_query(store, client, 'x', report=report)

    assert sorted(m['id'] for m in result) == ['tt1', 'tt2', 'tt3']
    assert report.failures == []


def test_run_query_merges_answers_in_catalog_order_not_arrival_order(load_searchwindow):
    """Addon order is the user's priority and decides which copy of a
    duplicate title wins `_dedupe()` - that must not depend on which
    request happened to be fastest. A answers LAST here, yet its copy wins
    and B's extra field is still merged in."""
    ctx = load_searchwindow()
    store, (ta, tb) = _addons('A', 'B')
    b_answered = threading.Event()

    def _slow_first():
        assert b_answered.wait(5)
        time.sleep(0.1)  # let B's answer reach the queue first
        return [_meta('tt1', 'From A'), _meta('tt3', 'Only A')]

    def _fast_second():
        b_answered.set()
        return [_meta('tt1', 'From B', poster='https://p.example/p.jpg'), _meta('tt2', 'Only B')]

    result = ctx.searchwindow.run_query(store, _ScriptedClient({ta: _slow_first, tb: _fast_second}), 'zzz')

    assert [m['id'] for m in result] == ['tt1', 'tt3', 'tt2']
    assert result[0]['name'] == 'From A'
    assert result[0]['poster'] == 'https://p.example/p.jpg'


def test_run_query_never_runs_more_than_the_worker_cap_at_once(load_searchwindow, monkeypatch):
    ctx = load_searchwindow()
    monkeypatch.setattr(ctx.searchwindow, '_MAX_SEARCH_WORKERS', 2)
    store, transports = _addons('A', 'B', 'C', 'D', 'E', 'F')
    lock = threading.Lock()
    state = {'active': 0, 'peak': 0}

    def _handler():
        with lock:
            state['active'] += 1
            state['peak'] = max(state['peak'], state['active'])
        time.sleep(0.02)
        with lock:
            state['active'] -= 1
        return []

    client = _ScriptedClient({t: _handler for t in transports})

    ctx.searchwindow.run_query(store, client, 'x')

    assert len(client.calls) == 6
    assert 1 <= state['peak'] <= 2


def test_run_query_isolates_a_non_addon_error_and_never_logs_its_text(load_searchwindow):
    """A worker thread that raised would die before queueing an answer and
    wedge the collector - any exception must cost one catalog, no more."""
    ctx = load_searchwindow()
    store, (ta, tb) = _addons('A', 'B')

    client = _ScriptedClient({
        ta: _raising(RuntimeError('boom https://user:hunter2@secret.example/private?token=abc')),
        tb: lambda: [_meta('tt1')],
    })
    report = ctx.searchwindow.SearchReport()

    result = ctx.searchwindow.run_query(store, client, 'x', report=report)

    assert [m['id'] for m in result] == ['tt1']
    assert report.failures == [('A', 'RuntimeError')]
    messages = ' '.join(msg for msg, _level in ctx.env.log_calls)
    assert 'RuntimeError' in messages
    assert 'hunter2' not in messages and 'secret.example' not in messages and 'token=abc' not in messages


def test_run_query_skips_entries_that_are_not_metas(load_searchwindow):
    ctx = load_searchwindow()
    store, (ta,) = _addons('A')
    client = _ScriptedClient({ta: lambda: [None, 'tt-str', 42, _meta('tt1')]})

    result = ctx.searchwindow.run_query(store, client, 'x')

    assert [m['id'] for m in result] == ['tt1']


def test_run_query_report_counts_catalogs_and_names_failures(load_searchwindow):
    ctx = load_searchwindow()
    store, (ta, tb) = _addons('A', 'B')
    adult_transport = 'https://adult.example/manifest.json'
    store._addons.append(_adult_search_catalog_descriptor(adult_transport, 'Adult'))
    client = _ScriptedClient({
        ta: _raising(AddonError('down', category='HTTP 500')),
        tb: lambda: [_meta('tt1')],
        adult_transport: _raising(AssertionError('an adult catalog must never be requested')),
    })
    report = ctx.searchwindow.SearchReport()

    ctx.searchwindow.run_query(store, client, 'x', report=report)

    assert report.total_catalogs == 3
    assert report.queried == 2
    assert report.failures == [('A', 'HTTP 500')]
    assert report.failed_addon_names() == ['A']
    assert report.unanswered == []
    assert report.canceled is False
    assert report.all_failed is False


def test_run_query_failure_reason_falls_back_to_the_exception_type_without_a_category(load_searchwindow):
    ctx = load_searchwindow()
    store, (ta,) = _addons('A')
    client = _ScriptedClient({ta: _raising(AddonError('upstream down'))})
    report = ctx.searchwindow.SearchReport()

    ctx.searchwindow.run_query(store, client, 'x', report=report)

    assert report.failures == [('A', 'AddonError')]
    assert report.all_failed is True


def test_search_report_dedupes_failed_addon_names_and_only_counts_a_full_outage_as_all_failed(load_searchwindow):
    ctx = load_searchwindow()
    report = ctx.searchwindow.SearchReport()
    assert report.all_failed is False  # nothing asked is not an outage

    report.queried = 3
    report.failures = [('A', 'x'), ('A', 'y'), ('B', 'z')]

    assert report.failed_addon_names() == ['A', 'B']
    assert report.all_failed is True
    report.queried = 4
    assert report.all_failed is False


def test_run_query_reusing_a_report_starts_from_a_clean_slate(load_searchwindow):
    ctx = load_searchwindow()
    store, (ta,) = _addons('A')
    report = ctx.searchwindow.SearchReport()
    report.failures = [('stale', 'x')]
    report.unanswered = ['stale']
    report.canceled = True

    ctx.searchwindow.run_query(store, _ScriptedClient({ta: lambda: [_meta('tt1')]}), 'x', report=report)

    assert (report.failures, report.unanswered, report.canceled) == ([], [], False)


def test_run_query_cancel_stops_waiting_and_reports_the_unanswered_addons(load_searchwindow, monkeypatch):
    ctx = load_searchwindow()
    store, (ta, tb) = _addons('A', 'B')
    release = threading.Event()

    def _blocked():
        release.wait(5)
        return []

    client = _ScriptedClient({ta: _blocked, tb: _blocked})
    monkeypatch.setattr(ctx.dialogs.RivuletBusy, 'iscanceled', lambda self: True)
    report = ctx.searchwindow.SearchReport()

    started = time.monotonic()
    result = ctx.searchwindow.run_query(store, client, 'x', report=report)
    elapsed = time.monotonic() - started
    release.set()

    assert result == []
    assert report.canceled is True
    assert report.unanswered == ['A', 'B']
    assert report.failures == []
    assert elapsed < 4  # did not sit out the blocked requests


def test_run_query_cancel_still_returns_what_had_already_arrived(load_searchwindow, monkeypatch):
    ctx = load_searchwindow()
    store, (ta, tb) = _addons('A', 'B')
    release = threading.Event()

    def _blocked():
        release.wait(5)
        return []

    client = _ScriptedClient({ta: lambda: [_meta('tt1')], tb: _blocked})
    began = time.monotonic()
    monkeypatch.setattr(ctx.dialogs.RivuletBusy, 'iscanceled', lambda self: time.monotonic() - began > 0.5)
    report = ctx.searchwindow.SearchReport()

    result = ctx.searchwindow.run_query(store, client, 'x', report=report)
    release.set()

    assert [m['id'] for m in result] == ['tt1']
    assert report.canceled is True
    assert report.unanswered == ['B']


def test_run_query_opens_no_busy_dialog_when_nothing_can_search(load_searchwindow, monkeypatch):
    ctx = load_searchwindow()
    created = []
    monkeypatch.setattr(ctx.dialogs.RivuletBusy, 'create', lambda self, heading, message='': created.append(heading))
    report = ctx.searchwindow.SearchReport()

    result = ctx.searchwindow.run_query(_FakeStore(addons=[]), _FakeAddonClient({}), 'x', report=report)

    assert result == []
    assert created == []
    assert report.total_catalogs == 0


def test_run_query_logs_one_summary_line_with_the_counts(load_searchwindow):
    """A short or empty result used to leave NOTHING in the log: no
    failure line, no count. The summary is what makes the next "1
    results" report diagnosable from a default-level kodi.log."""
    ctx = load_searchwindow()
    import xbmc

    store, (ta, tb) = _addons('A', 'B')
    client = _ScriptedClient({
        ta: _raising(AddonError('down', category='HTTP 503')),
        tb: lambda: [_meta('tt1')],
    })

    ctx.searchwindow.run_query(store, client, 'private query text')

    infos = [msg for msg, level in ctx.env.log_calls if level == xbmc.LOGINFO]
    assert any('2 catalog(s) declared, 2 asked, 2 answered, 1 failed, 0 unanswered -> 1 result(s)' in m for m in infos)
    assert any('failed catalogs: A (HTTP 503)' in m for m in infos)
    assert 'private query text' not in ' '.join(msg for msg, _level in ctx.env.log_calls)


# ---------------------------------------------------------------------------
# run_query() - the ranking feed is loaded off the UI path
# ---------------------------------------------------------------------------


def test_run_query_loads_the_ranking_feed_while_the_catalogs_are_in_flight(load_searchwindow, monkeypatch):
    """The feed used to be fetched after the busy dialog had closed, so
    the one search a day with a stale cache froze the UI behind no
    spinner. The catalog handler below can only succeed if the feed
    thread has ALREADY started when its request is running."""
    ctx = load_searchwindow()
    store, (ta,) = _addons('A')
    store.data_dir = '/does/not/matter'
    feed_started = threading.Event()
    sentinel = object()

    def _fake_feed_index(store_arg, client_arg):
        feed_started.set()
        return sentinel

    def _handler():
        assert feed_started.wait(5), 'the ranking feed was not being loaded during the fan-out'
        return [_meta('tt1')]

    seen = {}

    def _fake_rank(metas, query, feed_index=None):
        seen['feed_index'] = feed_index
        return metas

    monkeypatch.setattr(ctx.searchwindow, '_feed_index', _fake_feed_index)
    monkeypatch.setattr(ctx.searchwindow, '_rank_by_title', _fake_rank)
    client = _ScriptedClient({ta: _handler})
    client.session = object()

    result = ctx.searchwindow.run_query(store, client, 'x')

    assert [m['id'] for m in result] == ['tt1']
    assert seen['feed_index'] is sentinel


def _feed_loader(ctx, monkeypatch, feed_index):
    monkeypatch.setattr(ctx.searchwindow, '_feed_index', feed_index)
    store = types.SimpleNamespace(data_dir='/does/not/matter')
    client = types.SimpleNamespace(session=object())
    return ctx.searchwindow._FeedIndexLoader(store, client)


def test_feed_loader_gives_up_on_a_slow_feed_without_blocking_then_serves_it_later(load_searchwindow, monkeypatch):
    ctx = load_searchwindow()
    release = threading.Event()
    loader = _feed_loader(ctx, monkeypatch, lambda store, client: 'late' if release.wait(5) else None)
    dialog = types.SimpleNamespace(iscanceled=lambda: False)

    assert loader.wait(dialog, timeout=0.05) is None
    release.set()
    assert loader.wait(dialog, timeout=2) == 'late'


def test_feed_loader_wait_stops_as_soon_as_the_user_cancels(load_searchwindow, monkeypatch):
    ctx = load_searchwindow()
    release = threading.Event()
    loader = _feed_loader(ctx, monkeypatch, lambda store, client: release.wait(5))
    dialog = types.SimpleNamespace(iscanceled=lambda: True)

    started = time.monotonic()
    assert loader.wait(dialog, timeout=4) is None
    release.set()

    assert time.monotonic() - started < 2


def test_feed_loader_swallows_a_feed_failure_and_ranks_without_it(load_searchwindow, monkeypatch):
    ctx = load_searchwindow()
    import xbmc

    def _broken(store, client):
        raise ValueError('corrupt feed')

    loader = _feed_loader(ctx, monkeypatch, _broken)
    dialog = types.SimpleNamespace(iscanceled=lambda: False)

    assert loader.wait(dialog, timeout=2) is None
    warnings = [msg for msg, level in ctx.env.log_calls if level == xbmc.LOGWARNING]
    assert any('ranking feed failed: ValueError' in m for m in warnings)
    assert not any('corrupt feed' in m for m in warnings)


def test_feed_loader_is_idle_without_a_data_dir_or_a_session(load_searchwindow, monkeypatch):
    ctx = load_searchwindow()
    called = []
    monkeypatch.setattr(ctx.searchwindow, '_feed_index', lambda store, client: called.append(1))

    for store, client in (
        (types.SimpleNamespace(), types.SimpleNamespace(session=object())),
        (types.SimpleNamespace(data_dir='/x'), types.SimpleNamespace()),
    ):
        loader = ctx.searchwindow._FeedIndexLoader(store, client)
        assert loader._done.is_set()  # nothing to wait for - no thread was started
        assert loader.wait(types.SimpleNamespace(iscanceled=lambda: False), timeout=0) is None
    assert called == []


# ---------------------------------------------------------------------------
# run_query()/_run_search() - telling the user what went wrong
# ---------------------------------------------------------------------------


def test_run_query_without_a_report_notifies_a_total_outage_itself(load_searchwindow):
    """`open_credits_picker()` calls run_query() with no report: it must
    not be left presenting an outage as an empty search."""
    ctx = load_searchwindow()
    store, (ta,) = _addons('A')

    result = ctx.searchwindow.run_query(
        store, _FakeAddonClient({ta: AddonError('down', category='HTTP 500')}), 'x',
    )

    assert result == []
    assert ctx.env.notifications == [('Rivulet', 'STR30382', 'info', 6000)]


def test_run_query_without_a_report_notifies_a_partial_failure_itself(load_searchwindow):
    ctx = load_searchwindow()
    store, (ta, tb) = _addons('A', 'B')

    result = ctx.searchwindow.run_query(
        store, _FakeAddonClient({ta: AddonError('down'), tb: [_meta('tt1')]}), 'x',
    )

    assert [m['id'] for m in result] == ['tt1']
    assert ctx.env.notifications == [('Rivulet', 'Search incomplete, no answer from: A', 'info', 6000)]


def test_run_query_with_a_report_leaves_the_notification_to_the_caller(load_searchwindow):
    ctx = load_searchwindow()
    store, (ta,) = _addons('A')

    ctx.searchwindow.run_query(
        store, _FakeAddonClient({ta: AddonError('down')}), 'x', report=ctx.searchwindow.SearchReport(),
    )

    assert ctx.env.notifications == []


def test_run_query_without_a_report_says_nothing_about_a_clean_search(load_searchwindow):
    ctx = load_searchwindow()
    store, (ta,) = _addons('A')

    ctx.searchwindow.run_query(store, _FakeAddonClient({ta: []}), 'x')

    assert ctx.env.notifications == []


def test_run_search_nothing_enabled_can_search_hints_at_enabling_an_addon(load_searchwindow, monkeypatch):
    """Cinemeta switched off in the addon manager leaves no search addon at
    all: that is not "no results"."""
    ctx = load_searchwindow()
    disabled = _search_catalog_descriptor('https://cinemeta.example/manifest.json', 'Cinemeta')
    disabled['flags'] = {'disabled': True}
    store = _FakeStore(addons=[disabled])
    _wire_store(ctx.searchwindow, store)
    _wire_client(ctx.searchwindow, _FakeAddonClient({}))
    win = _make_window(ctx.searchwindow)
    win.onInit()

    win._run_search('batman')

    assert ctx.env.notifications == [('Rivulet', 'STR30381', 'info', 6000)]


def test_new_search_with_nothing_able_to_search_hints_before_the_keyboard_opens(load_searchwindow, monkeypatch):
    ctx = load_searchwindow(dialog_inputs=['batman'])
    store = _FakeStore(addons=[])
    _wire_store(ctx.searchwindow, store)
    win = _make_window(ctx.searchwindow)
    win.onInit()
    calls = []
    monkeypatch.setattr(win, '_run_search', lambda query: calls.append(query))

    win._new_search()

    assert ctx.env.dialog_input_prompts == []
    assert calls == []
    assert store.search_queries == []
    assert ctx.env.notifications == [('Rivulet', 'STR30381', 'info', 6000)]


def test_run_search_genuinely_empty_search_says_no_results_found(load_searchwindow, monkeypatch):
    ctx = load_searchwindow()
    store, (ta,) = _addons('A')
    _wire_store(ctx.searchwindow, store)
    _wire_client(ctx.searchwindow, _FakeAddonClient({ta: []}))
    win = _make_window(ctx.searchwindow)
    win.onInit()
    opened = []
    monkeypatch.setattr(ctx.infowindow, 'open_showcase', lambda m, catalog_title=None: opened.append(m))

    win._run_search('nomatch')

    assert opened == []
    assert ctx.env.notifications == [('Rivulet', 'STR30380', 'info', 4000)]


def test_run_search_some_failed_and_nothing_found_reports_the_failure_not_no_results(load_searchwindow, monkeypatch):
    ctx = load_searchwindow()
    store, (ta, tb) = _addons('A', 'B')
    _wire_store(ctx.searchwindow, store)
    _wire_client(ctx.searchwindow, _FakeAddonClient({ta: AddonError('down'), tb: []}))
    win = _make_window(ctx.searchwindow)
    win.onInit()

    win._run_search('nomatch')

    assert ctx.env.notifications == [('Rivulet', 'Search incomplete, no answer from: A', 'info', 6000)]


def test_run_search_partial_failure_is_announced_before_the_coverflow_opens(load_searchwindow, monkeypatch):
    ctx = load_searchwindow()
    store, (ta, tb) = _addons('A', 'B')
    _wire_store(ctx.searchwindow, store)
    metas = [_meta('tt1', 'Batman')]
    _wire_client(ctx.searchwindow, _FakeAddonClient({ta: AddonError('down'), tb: metas}))
    win = _make_window(ctx.searchwindow)
    win.onInit()
    seen = {}

    def _fake_open_showcase(passed, catalog_title=None):
        seen['metas'] = passed
        seen['notified_before'] = list(ctx.env.notifications)
        return None

    monkeypatch.setattr(ctx.infowindow, 'open_showcase', _fake_open_showcase)

    win._run_search('batman')

    assert seen['metas'] == metas
    assert seen['notified_before'] == [('Rivulet', 'Search incomplete, no answer from: A', 'info', 6000)]


def test_run_search_names_at_most_three_failing_addons_and_escapes_their_names(load_searchwindow, monkeypatch):
    ctx = load_searchwindow()
    store, transports = _addons('[B]Evil[/B]', 'B', 'C', 'D', 'E', 'Good')
    _wire_store(ctx.searchwindow, store)
    results = {t: AddonError('down') for t in transports[:-1]}
    results[transports[-1]] = [_meta('tt1')]
    _wire_client(ctx.searchwindow, _FakeAddonClient(results))
    win = _make_window(ctx.searchwindow)
    win.onInit()
    monkeypatch.setattr(ctx.infowindow, 'open_showcase', lambda m, catalog_title=None: None)

    win._run_search('x')

    evil = ctx.uicommon.escape_label('[B]Evil[/B]')
    assert ctx.env.notifications == [
        ('Rivulet', 'Search incomplete, no answer from: %s, B, C, +2' % evil, 'info', 6000),
    ]


def test_run_search_an_addon_whose_catalogs_all_fail_is_named_once(load_searchwindow, monkeypatch):
    """One aggregator failing several of its search catalogs is ONE name in
    the toast, not one per catalog."""
    ctx = load_searchwindow()
    multi = 'https://multi.example/manifest.json'
    good = 'https://good.example/manifest.json'
    store = _FakeStore(addons=[
        {
            'transportUrl': multi,
            'manifest': {'name': 'Multi', 'catalogs': [
                {'type': 'movie', 'id': 'one', 'extra': [{'name': 'search'}]},
                {'type': 'series', 'id': 'two', 'extra': [{'name': 'search'}]},
            ]},
        },
        _search_catalog_descriptor(good, 'Good'),
    ])
    _wire_store(ctx.searchwindow, store)
    _wire_client(ctx.searchwindow, _FakeAddonClient({multi: AddonError('down'), good: [_meta('tt1')]}))
    win = _make_window(ctx.searchwindow)
    win.onInit()
    monkeypatch.setattr(ctx.infowindow, 'open_showcase', lambda m, catalog_title=None: None)

    win._run_search('x')

    assert ctx.env.notifications == [('Rivulet', 'Search incomplete, no answer from: Multi', 'info', 6000)]


def test_run_search_cancelled_with_nothing_arrived_says_nothing_and_opens_nothing(load_searchwindow, monkeypatch):
    ctx = load_searchwindow()
    store, (ta,) = _addons('A')
    _wire_store(ctx.searchwindow, store)
    release = threading.Event()

    def _blocked():
        release.wait(5)
        return []

    _wire_client(ctx.searchwindow, _ScriptedClient({ta: _blocked}))
    monkeypatch.setattr(ctx.dialogs.RivuletBusy, 'iscanceled', lambda self: True)
    win = _make_window(ctx.searchwindow)
    win.onInit()
    opened = []
    monkeypatch.setattr(ctx.infowindow, 'open_showcase', lambda m, catalog_title=None: opened.append(m))

    win._run_search('x')
    release.set()

    assert opened == []
    assert ctx.env.notifications == []
    assert store.search_queries == ['x']  # the query is still worth remembering


def test_run_search_cancelled_with_partial_results_still_shows_them_without_a_failure_toast(load_searchwindow, monkeypatch):
    ctx = load_searchwindow()
    store, (ta, tb) = _addons('A', 'B')
    _wire_store(ctx.searchwindow, store)
    release = threading.Event()

    def _blocked():
        release.wait(5)
        return []

    _wire_client(ctx.searchwindow, _ScriptedClient({ta: lambda: [_meta('tt1')], tb: _blocked}))
    began = time.monotonic()
    monkeypatch.setattr(ctx.dialogs.RivuletBusy, 'iscanceled', lambda self: time.monotonic() - began > 0.5)
    win = _make_window(ctx.searchwindow)
    win.onInit()
    opened = []
    monkeypatch.setattr(ctx.infowindow, 'open_showcase', lambda m, catalog_title=None: opened.append(m))

    win._run_search('x')
    release.set()

    assert [[m['id'] for m in batch] for batch in opened] == [['tt1']]
    assert ctx.env.notifications == []


# ---------------------------------------------------------------------------
# End to end through the real AddonClient: AIOStreams' error placeholder
# ---------------------------------------------------------------------------


def _aiostreams_error_meta(description='404 - Not Found'):
    """What `StremioTransformer.createErrorMeta` (AIOStreams) answers a
    failing catalog with: HTTP 200, one meta, id `aiostreamserror.<json>`."""
    import json
    from urllib.parse import quote

    from lib.stremio.addons import ERROR_META_ID_PREFIX

    payload = json.dumps({'errorTitle': '[X] AIOStreams', 'errorDescription': description}, separators=(',', ':'))
    return {
        'id': ERROR_META_ID_PREFIX + quote(payload, safe=''),
        'name': '[X] AIOStreams',
        'description': description,
        'type': 'movie',
    }


def _aiostreams_like_store():
    transport = 'https://aio.example/stremio/token/manifest.json'
    manifest = {'name': 'AIOStreams', 'catalogs': [
        {'type': 'movie', 'id': 'search.movie', 'extra': [{'name': 'search', 'isRequired': True}]},
        {'type': 'series', 'id': 'search.series', 'extra': [{'name': 'search', 'isRequired': True}]},
        {'type': 'anime.series', 'id': 'search.anime_series', 'extra': [{'name': 'search', 'isRequired': True}]},
    ]}
    return _FakeStore(addons=[{'transportUrl': transport, 'manifest': manifest}])


def test_run_search_error_placeholders_are_a_failed_search_not_a_one_result_coverflow(load_searchwindow, monkeypatch):
    """The reported "opening coverflow (1 results)" with nothing in the
    log: a throttled AIOStreams answered EVERY search catalog with the same
    HTTP-200 placeholder, `_dedupe()` folded the lot into one entry, and
    the coverflow showed the error as its only title."""
    from lib.stremio.addons import AddonClient
    from tests.conftest import FakeResponse, FakeSession

    ctx = load_searchwindow()
    store = _aiostreams_like_store()
    _wire_store(ctx.searchwindow, store)
    client = AddonClient()
    client.session = FakeSession(responses=[FakeResponse({'metas': [_aiostreams_error_meta()]}) for _ in range(3)])
    _wire_client(ctx.searchwindow, client)
    win = _make_window(ctx.searchwindow)
    win.onInit()
    opened = []
    monkeypatch.setattr(ctx.infowindow, 'open_showcase', lambda m, catalog_title=None: opened.append(m))

    win._run_search('batman')

    assert opened == []
    assert ctx.env.notifications == [('Rivulet', 'STR30382', 'info', 6000)]
    errors = [msg for msg, _level in ctx.env.log_calls if 'failed' in msg and 'aio.example' in msg]
    assert len(errors) == 3 and all('addon reported error (HTTP 404)' in m for m in errors)


def test_run_query_drops_an_error_placeholder_appended_to_real_results(load_searchwindow):
    from lib.stremio.addons import AddonClient
    from tests.conftest import FakeResponse, FakeSession

    ctx = load_searchwindow()
    store = _aiostreams_like_store()
    client = AddonClient()
    real = _meta('tt1', 'Batman')
    client.session = FakeSession(responses=[
        FakeResponse({'metas': [real, _aiostreams_error_meta('Request timed out')]}),
        FakeResponse({'metas': []}),
        FakeResponse({'metas': []}),
    ])
    report = ctx.searchwindow.SearchReport()

    result = ctx.searchwindow.run_query(store, client, 'batman', report=report)

    assert [m['id'] for m in result] == ['tt1']
    assert report.failures == []


# ---------------------------------------------------------------------------
# SearchWindow._clear_history()
# ---------------------------------------------------------------------------


def _stub_confirm(monkeypatch, ctx, answer, capture=None):
    stub_confirm(monkeypatch, ctx, answer, capture=capture)


def test_clear_history_declined_leaves_history_untouched_and_does_not_reload(load_searchwindow, monkeypatch):
    ctx = load_searchwindow()
    _stub_confirm(monkeypatch, ctx, False)
    store = _FakeStore(history=['batman'])
    _wire_store(ctx.searchwindow, store)
    win = _make_window(ctx.searchwindow)
    win.onInit()

    win._clear_history()

    assert store.cleared == 0
    items = win.getControl(ctx.searchwindow.LIST).items
    assert [item.getLabel() for item in items] == ['STR30042', 'batman', 'STR30044']


def test_clear_history_confirmed_clears_and_reloads_to_new_search_only(load_searchwindow, monkeypatch):
    ctx = load_searchwindow()
    captured = []
    _stub_confirm(monkeypatch, ctx, True, capture=captured)
    store = _FakeStore(history=['batman'])
    _wire_store(ctx.searchwindow, store)
    win = _make_window(ctx.searchwindow)
    win.onInit()

    win._clear_history()

    assert store.cleared == 1
    assert captured == [('STR30044', 'STR30046', 'Yes', 'No')]
    items = win.getControl(ctx.searchwindow.LIST).items
    assert len(items) == 1
    assert items[0].getProperty('position') == 'new'


# ---------------------------------------------------------------------------
# SearchWindow.start()
# ---------------------------------------------------------------------------


def test_start_resets_should_close_caller_calls_domodal_once_and_returns_it(load_searchwindow, monkeypatch):
    ctx = load_searchwindow()
    _wire_store(ctx.searchwindow, _FakeStore())
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
    _wire_store(ctx.searchwindow, store)
    metas = [{'id': 'tt9', 'name': 'Batman', 'type': 'movie'}]
    _wire_client(ctx.searchwindow, _FakeAddonClient(catalog_results={transport: metas}))
    win = _make_window(ctx.searchwindow)
    monkeypatch.setattr(ctx.infowindow, 'open_showcase', lambda m, catalog_title=None: m[0])
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
    assert captured['init_args'] == ('SearchWindow.xml', '/addon/path', 'Default', '1080i')
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


# ---------------------------------------------------------------------------
# Shared process-wide Store/AddonClient (lib.ui.dependencies)
# ---------------------------------------------------------------------------


def test_reload_prefers_an_already_injected_store_over_the_shared_provider(load_searchwindow, monkeypatch):
    """`_reload()`'s `self.store = self.store or get_store()` must never
    call `get_store()` once a test (or a caller) has already injected
    `self.store` directly."""
    ctx = load_searchwindow()

    def _unexpected():
        raise AssertionError('get_store() must not be called when self.store is already set')

    monkeypatch.setattr(ctx.searchwindow, 'get_store', _unexpected)
    win = _make_window(ctx.searchwindow)
    win.store = _FakeStore(history=['batman'])

    win.onInit()  # must not raise

    assert [item.getLabel() for item in win.getControl(ctx.searchwindow.LIST).items] == [
        'STR30042', 'batman', 'STR30044',
    ]


def test_reopening_searchwindow_reuses_the_shared_store(load_searchwindow, monkeypatch):
    """`_reload()` re-runs every time the window reopens (`onInit()` fires
    again) - with no store injected it must always fetch the SAME
    `get_store()` singleton rather than constructing a fresh `Store`."""
    ctx = load_searchwindow()

    class _CountingStore:
        instances = 0

        def __init__(self, *args):
            type(self).instances += 1

        def get_search_history(self):
            return []

    monkeypatch.setattr(ctx.dependencies, 'Store', _CountingStore)
    win = _make_window(ctx.searchwindow)

    win.onInit()
    first_store = win.store
    win.onInit()  # simulates the window reopening

    assert win.store is first_store
    assert _CountingStore.instances == 1


def test_run_search_reuses_the_shared_client_across_multiple_searches(load_searchwindow, monkeypatch):
    """Two separate `_run_search()` calls must reuse the SAME
    `get_client()` singleton rather than each constructing its own
    `AddonClient`."""
    ctx = load_searchwindow()
    _wire_store(ctx.searchwindow, _FakeStore())  # no catalogs -> loop body never runs

    class _CountingClient:
        instances = 0

        def __init__(self):
            type(self).instances += 1

    monkeypatch.setattr(ctx.dependencies, 'AddonClient', _CountingClient)
    win = _make_window(ctx.searchwindow)
    win.onInit()

    win._run_search('batman')
    win._run_search('robin')

    assert _CountingClient.instances == 1
