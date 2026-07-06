"""Tests for lib.ui.searchwindow: open_search(), Rivulet's custom
replacement for the classical `search()` directory - prompts a query, then
shows the aggregated results directly in the coverflow overlay - exercised
against the shared fake xbmc/xbmcgui stubs in tests/kodistubs (no real Kodi
runtime, no network).

Unlike the other three new modules, lib.ui.searchwindow imports its data
layer (lib.store.Store, lib.stremio.addons.AddonClient/AddonError/
iter_catalogs) at MODULE scope rather than lazily, so - mirroring
tests/test_views.py's `_wire_data_layer` pattern for lib.ui.views - the data
layer is faked by assigning directly to the names lib.ui.searchwindow itself
imported (`searchwindow.Store`, `searchwindow.AddonClient`) rather than via
monkeypatching lib.store/lib.stremio.addons. `iter_catalogs` is exercised for
real (it is pure, no xbmc dependency) so the search-extra filtering and
manifest/catalog aggregation logic - the exact behavior
tests/test_views.py's own search() test defends - is exercised here too,
ported to a bare function rather than a Kodi-directory view.

open_search() also lazily `from lib.ui.infowindow import open_showcase`, so
load_searchwindow reloads lib.ui.infowindow fresh alongside lib.ui.compat/
lib.ui.uicommon/lib.ui.router/lib.ui.searchwindow to get a handle
(`ctx.infowindow`) this file monkeypatches directly.
"""
import contextlib

import pytest

from lib.stremio.addons import AddonError
from tests.kodistubs import install_kodi_stubs

_RELOAD_MODULE_NAMES = (
    'lib.ui.compat', 'lib.ui.uicommon', 'lib.ui.router',
    'lib.ui.infowindow', 'lib.ui.detailwindow', 'lib.ui.searchwindow',
)


class _FakeStore:
    """Fake `lib.store.Store`: only `get_addons()` matters to open_search()."""

    def __init__(self, addons=None):
        self._addons = addons or []

    def get_addons(self):
        return self._addons


class _FakeAddonClient:
    """Fake `lib.stremio.addons.AddonClient`. `catalog_results` maps
    transport_url -> a list of metas, or an Exception instance to raise
    instead (standing in for an addon-request failure). `.calls` records
    every `catalog(transport, ctype, cid, extra=...)` invocation so a test
    can assert exactly which catalogs were queried (and with what `extra`).
    """

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
    """Factory fixture: `load_searchwindow(addon_info=None,
    dialog_inputs=None)` installs fresh stubs (via
    tests.kodistubs.install_kodi_stubs) reloading lib.ui.compat/
    lib.ui.uicommon/lib.ui.router/lib.ui.infowindow/lib.ui.searchwindow, and
    returns a namespace with `.searchwindow`, `.compat`, `.router`,
    `.infowindow`, and `.env`. Every call is torn down automatically, in
    reverse order, at test end.
    """
    with contextlib.ExitStack() as stack:
        def _load(addon_info=None, dialog_inputs=None):
            return stack.enter_context(install_kodi_stubs(
                reload=_RELOAD_MODULE_NAMES,
                addon_info=addon_info,
                dialog_inputs=dialog_inputs,
            ))

        yield _load


def _wire_data_layer(searchwindow_mod, store, client):
    searchwindow_mod.Store = lambda *a, **k: store
    searchwindow_mod.AddonClient = lambda *a, **k: client


# ---------------------------------------------------------------------------
# open_search() - cancelled dialog short-circuit
# ---------------------------------------------------------------------------


def test_open_search_cancelled_dialog_returns_false_without_querying_addons(load_searchwindow):
    ctx = load_searchwindow()  # default dialog_inputs=None -> Dialog.input() returns ''
    searchwindow = ctx.searchwindow

    def _unexpected(*a, **k):
        raise AssertionError('Store should never be constructed when the query is cancelled')

    searchwindow.Store = _unexpected

    result = searchwindow.open_search()

    assert result is False
    assert ctx.env.dialog_input_prompts == ['STR30001']


# ---------------------------------------------------------------------------
# open_search() - aggregation across catalogs, skipping AddonError
# ---------------------------------------------------------------------------


def test_open_search_aggregates_across_catalogs_and_skips_addonerror(load_searchwindow, monkeypatch):
    ctx = load_searchwindow(dialog_inputs=['batman'])
    searchwindow = ctx.searchwindow
    transport_a = 'https://a.example/manifest.json'
    transport_b = 'https://b.example/manifest.json'
    descriptor_a = {
        'transportUrl': transport_a,
        'manifest': {'name': 'Addon A', 'catalogs': [
            {'type': 'movie', 'id': 'search', 'extra': [{'name': 'search'}]},
        ]},
    }
    descriptor_b = {
        'transportUrl': transport_b,
        'manifest': {'name': 'Addon B', 'catalogs': [
            {'type': 'series', 'id': 'search', 'extraSupported': ['search']},
        ]},
    }
    descriptor_c = {
        'transportUrl': 'https://c.example/manifest.json',
        'manifest': {'name': 'Addon C', 'catalogs': [
            {'type': 'movie', 'id': 'top'},  # no search extra -> excluded before any request
        ]},
    }
    client = _FakeAddonClient({
        transport_a: AddonError('addon a down'),
        transport_b: [{'id': 'tt1', 'name': 'Batman'}],  # no 'type' -> tagged from its catalog
    })
    _wire_data_layer(searchwindow, _FakeStore(addons=[descriptor_a, descriptor_b, descriptor_c]), client)
    captured = {}

    def fake_open_showcase(metas):
        captured['metas'] = metas
        return None

    monkeypatch.setattr(ctx.infowindow, 'open_showcase', fake_open_showcase)

    result = searchwindow.open_search()

    # addon A is queried (its catalog declares 'search') but raises
    # AddonError - skipped, not surfaced; addon C is never queried at all
    # (iter_catalogs' extra_required='search' filter excludes its catalog
    # up front) -> only B's result is aggregated, tagged with its catalog's
    # type.
    assert captured['metas'] == [{'id': 'tt1', 'name': 'Batman', 'type': 'series'}]
    assert [call[0] for call in client.calls] == [transport_a, transport_b]
    assert all(call[3] == [('search', 'batman')] for call in client.calls)
    assert result is False


# ---------------------------------------------------------------------------
# open_search() - empty aggregate result
# ---------------------------------------------------------------------------


def test_open_search_no_results_notifies_and_returns_false(load_searchwindow, monkeypatch):
    ctx = load_searchwindow(dialog_inputs=['nomatch'])
    searchwindow = ctx.searchwindow
    descriptor = {
        'transportUrl': 't1',
        'manifest': {'catalogs': [{'type': 'movie', 'id': 'search', 'extra': [{'name': 'search'}]}]},
    }
    _wire_data_layer(searchwindow, _FakeStore(addons=[descriptor]), _FakeAddonClient({'t1': []}))
    opened = []
    monkeypatch.setattr(ctx.infowindow, 'open_showcase', lambda metas: opened.append(metas))

    result = searchwindow.open_search()

    assert result is False
    assert ctx.env.notifications == [('Rivulet', 'STR30030', 'info', 4000)]
    assert opened == []  # the coverflow is never opened on an empty aggregate


# ---------------------------------------------------------------------------
# open_search() - a selection falls back to the classical meta directory
# ---------------------------------------------------------------------------


def test_open_search_selection_opens_detail_and_returns_its_result(load_searchwindow, monkeypatch):
    ctx = load_searchwindow(dialog_inputs=['batman'])
    searchwindow = ctx.searchwindow
    descriptor = {
        'transportUrl': 't1',
        'manifest': {'catalogs': [{'type': 'movie', 'id': 'search', 'extra': [{'name': 'search'}]}]},
    }
    chosen = {'id': 'tt9', 'name': 'Batman', 'type': 'movie'}
    _wire_data_layer(searchwindow, _FakeStore(addons=[descriptor]), _FakeAddonClient({'t1': [chosen]}))
    monkeypatch.setattr(ctx.infowindow, 'open_showcase', lambda metas: chosen)
    captured = {}

    def fake_open_detail(stype, sid):
        captured['args'] = (stype, sid)
        return True

    monkeypatch.setattr(ctx.detailwindow, 'open_detail', fake_open_detail)

    result = searchwindow.open_search()

    assert result is True
    assert captured['args'] == ('movie', 'tt9')


def test_open_search_selection_without_any_type_falls_back_to_movie(load_searchwindow, monkeypatch):
    ctx = load_searchwindow(dialog_inputs=['batman'])
    searchwindow = ctx.searchwindow
    descriptor = {
        'transportUrl': 't1',
        # catalog itself declares no 'type' either, so the aggregation loop's
        # `meta_obj.get('type') or cat.get('type')` tag ends up None too.
        'manifest': {'catalogs': [{'id': 'search', 'extra': [{'name': 'search'}]}]},
    }
    raw_result = {'id': 'tt9', 'name': 'Mystery'}
    _wire_data_layer(searchwindow, _FakeStore(addons=[descriptor]), _FakeAddonClient({'t1': [raw_result]}))
    monkeypatch.setattr(ctx.infowindow, 'open_showcase', lambda metas: metas[0])
    captured = {}

    def fake_open_detail(stype, sid):
        captured['args'] = (stype, sid)
        return True

    monkeypatch.setattr(ctx.detailwindow, 'open_detail', fake_open_detail)

    result = searchwindow.open_search()

    assert result is True
    assert captured['args'] == ('movie', 'tt9')


def test_open_search_no_selection_returns_false_without_fallback(load_searchwindow, monkeypatch):
    ctx = load_searchwindow(dialog_inputs=['batman'])
    searchwindow = ctx.searchwindow
    descriptor = {
        'transportUrl': 't1',
        'manifest': {'catalogs': [{'type': 'movie', 'id': 'search', 'extra': [{'name': 'search'}]}]},
    }
    metas = [{'id': 'tt1', 'name': 'One', 'type': 'movie'}]
    _wire_data_layer(searchwindow, _FakeStore(addons=[descriptor]), _FakeAddonClient({'t1': metas}))
    monkeypatch.setattr(ctx.infowindow, 'open_showcase', lambda m: None)

    result = searchwindow.open_search()

    assert result is False
    assert ctx.env.executed_builtins == []


# ---------------------------------------------------------------------------
# open_search() - busy_dialog progress reporting/cancellation
# ---------------------------------------------------------------------------


def _cancel_after(n):
    """Builds a zero-arg closure for `ctx.env.cancel` that reports
    cancelled (True) starting from its (n+1)th call onward. Mirrors
    DialogProgress.iscanceled()'s no-arg call convention (unlike
    Monitor.waitForAbort()'s 1-based-count-arg convention)."""
    state = {'calls': 0}

    def _check():
        state['calls'] += 1
        return state['calls'] > n
    return _check


def test_open_search_busy_dialog_reports_progress_and_skips_addonerror(load_searchwindow, monkeypatch):
    ctx = load_searchwindow(dialog_inputs=['batman'])
    searchwindow = ctx.searchwindow
    transport_a = 'https://a.example/manifest.json'
    transport_b = 'https://b.example/manifest.json'
    descriptor_a = {
        'transportUrl': transport_a,
        'manifest': {'name': 'Addon A', 'catalogs': [
            {'type': 'movie', 'id': 'search', 'extra': [{'name': 'search'}]},
        ]},
    }
    descriptor_b = {
        'transportUrl': transport_b,
        'manifest': {'name': 'Addon B', 'catalogs': [
            {'type': 'series', 'id': 'search', 'extraSupported': ['search']},
        ]},
    }
    descriptor_c = {
        'transportUrl': 'https://c.example/manifest.json',
        'manifest': {'name': 'Addon C', 'catalogs': [
            {'type': 'movie', 'id': 'top'},  # no search extra -> excluded before total_catalogs is even computed
        ]},
    }
    client = _FakeAddonClient({
        transport_a: AddonError('addon a down'),
        transport_b: [{'id': 'tt1', 'name': 'Batman'}],
    })
    _wire_data_layer(searchwindow, _FakeStore(addons=[descriptor_a, descriptor_b, descriptor_c]), client)
    monkeypatch.setattr(ctx.infowindow, 'open_showcase', lambda metas: None)

    result = searchwindow.open_search()

    assert ctx.env.dialog_created == [('STR30033', 'batman')]
    # addon C never enters the loop at all (iter_catalogs' extra_required='search'
    # filter excludes it up front), so total_catalogs is 2, not 3: index 0 -> 0%,
    # index 1 -> 50%. Addon A raises AddonError but its update() still fires
    # first (iscanceled() -> update() -> fetch, in that order).
    assert ctx.env.dialog_updates == [
        (0, 'batman'),                 # busy_dialog's own initial update(0, message)
        (0, 'Searching Addon A...'),   # index 0 of 2 -> int(0 * 100 / 2)
        (50, 'Searching Addon B...'),  # index 1 of 2 -> int(1 * 100 / 2)
    ]
    assert ctx.env.dialog_closed_count == 1  # closed exactly once, even though A raised AddonError
    assert result is False  # open_showcase returned None -> nothing selected


def test_open_search_cancelled_mid_loop_keeps_partial_results_and_closes_dialog(load_searchwindow, monkeypatch):
    ctx = load_searchwindow(dialog_inputs=['batman'])
    searchwindow = ctx.searchwindow
    transport_a = 'https://a.example/manifest.json'
    transport_b = 'https://b.example/manifest.json'
    descriptor_a = {
        'transportUrl': transport_a,
        'manifest': {'name': 'Addon A', 'catalogs': [
            {'type': 'movie', 'id': 'search', 'extra': [{'name': 'search'}]},
        ]},
    }
    descriptor_b = {
        'transportUrl': transport_b,
        'manifest': {'name': 'Addon B', 'catalogs': [
            {'type': 'series', 'id': 'search', 'extraSupported': ['search']},
        ]},
    }
    client = _FakeAddonClient({
        transport_a: [{'id': 'tt1', 'name': 'Batman'}],  # no 'type' -> tagged from its catalog
        transport_b: [{'id': 'tt2', 'name': 'Should never be fetched'}],
    })
    _wire_data_layer(searchwindow, _FakeStore(addons=[descriptor_a, descriptor_b]), client)
    ctx.env.cancel = _cancel_after(1)  # index 0 -> not cancelled; index 1 -> cancelled, breaks
    captured = {}

    def fake_open_showcase(metas):
        captured['metas'] = metas
        return None

    monkeypatch.setattr(ctx.infowindow, 'open_showcase', fake_open_showcase)

    result = searchwindow.open_search()

    # only addon A is ever queried - iscanceled() is checked before update()/fetch,
    # so the loop breaks as soon as it reaches catalog index 1 without touching it.
    assert [call[0] for call in client.calls] == [transport_a]
    assert ctx.env.dialog_updates == [(0, 'batman'), (0, 'Searching Addon A...')]
    # the already-aggregated meta from catalog A is NOT discarded on cancel - the
    # coverflow still opens with whatever was collected before the cancel fired.
    assert captured['metas'] == [{'id': 'tt1', 'name': 'Batman', 'type': 'movie'}]
    assert ctx.env.dialog_closed_count == 1
    assert result is False


def test_open_search_cancelled_before_first_catalog_falls_back_to_no_results(load_searchwindow, monkeypatch):
    ctx = load_searchwindow(dialog_inputs=['nomatch'])
    searchwindow = ctx.searchwindow
    descriptor = {
        'transportUrl': 't1',
        'manifest': {'catalogs': [{'type': 'movie', 'id': 'search', 'extra': [{'name': 'search'}]}]},
    }
    client = _FakeAddonClient({'t1': [{'id': 'tt1', 'name': 'Should never be fetched'}]})
    _wire_data_layer(searchwindow, _FakeStore(addons=[descriptor]), client)
    ctx.env.cancel = True  # cancelled before the loop ever reaches a catalog
    opened = []
    monkeypatch.setattr(ctx.infowindow, 'open_showcase', lambda metas: opened.append(metas))

    result = searchwindow.open_search()

    assert client.calls == []  # nothing was ever fetched
    assert ctx.env.dialog_updates == [(0, 'nomatch')]  # only busy_dialog's own initial update fires
    assert ctx.env.dialog_closed_count == 1
    # cancelling with nothing collected is NOT special-cased: it falls through to
    # the exact same empty-aggregate path a genuine no-results search takes.
    assert result is False
    assert ctx.env.notifications == [('Rivulet', 'STR30030', 'info', 4000)]
    assert opened == []  # the coverflow is never opened
