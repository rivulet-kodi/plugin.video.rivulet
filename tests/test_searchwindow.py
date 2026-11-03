# SPDX-FileCopyrightText: 2026 M0Rf30
# SPDX-License-Identifier: MIT

"""Tests for lib.ui.searchwindow: the search engine `run_query()` (fan-out
across every searchable catalog, ranking, `SearchReport`, the user-facing
notice) shared by the one-page search (`lib.ui.search`) and the
coverflow's person/discover links. Exercised against the shared fake
xbmc/xbmcgui stubs in tests/kodistubs (no real Kodi runtime, no network).
"""
import contextlib
import threading
import time
import types

import pytest

from lib.stremio.addons import AddonError
from tests.kodistubs import install_kodi_stubs

_RELOAD_MODULE_NAMES = (
    'lib.ui.compat', 'lib.ui.dependencies', 'lib.ui.uicommon', 'lib.ui.dialogs',
    'lib.ui.infowindow', 'lib.ui.detailwindow', 'lib.ui.searchwindow', 'lib.ui.search',
)


class _FakeStore:
    """Fake `lib.store.Store`: an in-memory search-history list plus
    `get_addons()`/`get_enabled_addons()`'s backing list. `add_search_query`
    reproduces the real Store's move-to-front dedup (see lib/store.py)
    closely enough that `_run_search`'s post-search `_reload()` reflects
    the just-recorded query in the same order the real Store would;
    `.search_queries` additionally records every call so a test can
    assert exactly what was persisted."""

    def __init__(self, addons=None, history=None):
        self._addons = addons or []
        self._history = list(history or [])
        self.search_queries = []  # [query, ...] - every add_search_query() call

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
# run_query() - the fan-out shared with
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


def test_feed_wait_budget_is_capped_by_the_remaining_search_deadline(load_searchwindow, monkeypatch):
    """CodeRabbit on #68: a search that hit the 25s deadline could still
    wait up to 3s more for the feed. The feed only gets what is left."""
    sw = load_searchwindow().searchwindow
    clock = {'now': 0.0}
    monkeypatch.setattr(sw, 'time', types.SimpleNamespace(monotonic=lambda: clock['now']))

    clock['now'] = 5.0
    assert sw._feed_wait_budget(0.0) == sw._FEED_WAIT_SECONDS
    clock['now'] = sw._SEARCH_DEADLINE_SECONDS - 1.0
    assert sw._feed_wait_budget(0.0) == 1.0
    clock['now'] = sw._SEARCH_DEADLINE_SECONDS
    assert sw._feed_wait_budget(0.0) == 0.0
    clock['now'] = sw._SEARCH_DEADLINE_SECONDS + 4.0
    assert sw._feed_wait_budget(0.0) == 0.0


def test_run_query_waits_for_the_feed_only_within_the_search_deadline(load_searchwindow, monkeypatch):
    ctx = load_searchwindow()
    sw = ctx.searchwindow
    store, (ta,) = _addons('A')
    seen = {}

    class _Loader:
        def __init__(self, store_arg, client_arg):
            pass

        def wait(self, dialog, timeout=sw._FEED_WAIT_SECONDS):
            seen['timeout'] = timeout
            return None

    monkeypatch.setattr(sw, '_FeedIndexLoader', _Loader)
    monkeypatch.setattr(sw, '_feed_wait_budget', lambda started: 0.25)
    client = _ScriptedClient({ta: lambda: [_meta('tt1')]})

    result = sw.run_query(store, client, 'x')

    assert [m['id'] for m in result] == ['tt1']
    assert seen['timeout'] == 0.25

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
# run_query() - telling the user what went wrong
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


class _NeverCanceled:
    def iscanceled(self):
        return False

    def update(self, *args, **kwargs):
        pass


def _clocked(monkeypatch, sw, ticks):
    """`time.monotonic()` for the collector: each call steps the clock."""
    clock = {'now': 0.0}

    def monotonic():
        clock['now'] += ticks
        return clock['now']

    monkeypatch.setattr(sw, 'time', types.SimpleNamespace(monotonic=monotonic))
    monkeypatch.setattr(sw, '_SEARCH_POLL_SECONDS', 0.01)
    return clock


def test_results_in_hand_are_not_held_for_a_slow_catalog(load_searchwindow, monkeypatch):
    """Andor sat on the spinner until Back: one catalog never answered."""
    import queue

    sw = load_searchwindow().searchwindow
    clock = _clocked(monkeypatch, sw, 0.5)
    results = queue.Queue()
    results.put((0, ([{'id': 'tt9253284', 'name': 'Andor'}], None)))  # the second catalog never answers
    jobs = [('https://a', {'name': 'Cinemeta'}, {}), ('https://b', {'name': 'Slow'}, {})]
    report = sw.SearchReport()
    answered = sw._collect_answers(_NeverCanceled(), results, jobs, report)
    assert list(answered) == [0]
    assert report.canceled is False
    # It was the grace period after the first titles that ended the wait,
    # not the overall deadline.
    assert sw._SEARCH_GRACE_SECONDS <= clock['now'] < sw._SEARCH_GRACE_SECONDS + 2
    assert clock['now'] < sw._SEARCH_DEADLINE_SECONDS


def test_the_wait_ends_at_the_deadline_with_nothing_in(load_searchwindow, monkeypatch):
    import queue

    sw = load_searchwindow().searchwindow
    _clocked(monkeypatch, sw, 2.0)
    jobs = [('https://a', {'name': 'Slow'}, {})]
    report = sw.SearchReport()
    assert sw._collect_answers(_NeverCanceled(), queue.Queue(), jobs, report) == {}
    assert report.canceled is False


def test_an_empty_answer_does_not_start_the_grace(load_searchwindow, monkeypatch):
    import queue

    sw = load_searchwindow().searchwindow
    clock = _clocked(monkeypatch, sw, 0.5)
    late_at = sw._SEARCH_GRACE_SECONDS + 3  # after a wrongly started grace, before the deadline
    assert late_at < sw._SEARCH_DEADLINE_SECONDS

    class _LateSecondAnswer:
        """The empty answer at once; the titles only once the clock passes `late_at`."""

        def __init__(self):
            self.pending = [(0, ([], None))]

        def get(self, timeout=None):
            if self.pending:
                return self.pending.pop()
            if clock['now'] >= late_at:
                return 1, ([{'id': 'tt1', 'name': 'Andor'}], None)
            raise queue.Empty

    jobs = [('https://a', {'name': 'A'}, {}), ('https://b', {'name': 'B'}, {})]
    answered = sw._collect_answers(_NeverCanceled(), _LateSecondAnswer(), jobs, sw.SearchReport())
    assert sorted(answered) == [0, 1]
    assert clock['now'] >= late_at


def test_catalogs_left_out_by_the_wait_are_counted_in_the_notice(load_searchwindow):
    """M0Rf30 on #68: a slow but good catalog must not go silently missing."""
    sw = load_searchwindow().searchwindow
    report = sw.SearchReport()
    report.total_catalogs = report.queried = 3
    report.unanswered = ['Slow', 'Slower']
    assert sw._search_notice(report) == 'Search incomplete, catalogs too slow to answer: 2'


def test_a_failed_catalog_is_named_before_slow_ones_are_counted(load_searchwindow):
    sw = load_searchwindow().searchwindow
    report = sw.SearchReport()
    report.total_catalogs = report.queried = 3
    report.failures = [('A', 'timeout')]
    report.unanswered = ['Slow']
    assert sw._search_notice(report) == 'Search incomplete, no answer from: A'


def test_a_cancelled_search_says_nothing_about_the_catalogs_still_out(load_searchwindow):
    sw = load_searchwindow().searchwindow
    report = sw.SearchReport()
    report.total_catalogs = report.queried = 2
    report.unanswered = ['Slow']
    report.canceled = True
    assert sw._search_notice(report) is None


