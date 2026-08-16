"""Tests for lib.ui.continuewatching: the Home "Continue watching" row's
data (`resumable_candidates`, `has_resumable`) and its coverflow action
(`open_continue_watching`) - exercised against the shared fake
xbmc/xbmcgui stubs in tests/kodistubs (no real Kodi runtime, no network).

lib.ui.continuewatching imports `get_store` (from lib.ui.dependencies) at
module scope, and lazily `from lib.ui import views` /
`from lib.ui.infowindow import open_showcase` / `from lib.ui.detailwindow
import open_detail` from inside open_continue_watching() itself - so this
file fakes the shared Store provider by assigning directly to
`continuewatching.get_store` (the same way tests/test_librarywindow.py
wires `librarywindow.get_store`), and fakes the addon-fetch layer by
assigning directly to `views._map_addons`/`views._fetch_meta` (the same
way tests/test_views.py wires `views.get_store`/`views.get_client`).
"""
import contextlib

import pytest

from tests.kodistubs import install_kodi_stubs

_RELOAD_MODULE_NAMES = (
    'lib.ui.compat', 'lib.ui.dependencies', 'lib.ui.uicommon', 'lib.ui.router',
    'lib.ui.views', 'lib.ui.infowindow', 'lib.ui.detailwindow', 'lib.ui.continuewatching',
)


class _FakeStore:
    """Fake `lib.store.Store`: only `get_progress_entries()` matters to
    `has_resumable()`/`open_continue_watching()`."""

    def __init__(self, entries=None):
        self._entries = entries or []

    def get_progress_entries(self):
        return self._entries


def _entry(type_='movie', id_='tt1', video_id=None, position_ms=500, duration_ms=1000,
           updated_at='2020-01-01T00:00:00Z'):
    return {
        'type': type_, 'id': id_, 'video_id': video_id,
        'position_ms': position_ms, 'duration_ms': duration_ms, 'updated_at': updated_at,
    }


@pytest.fixture
def load_continuewatching():
    """Factory fixture: `load_continuewatching(addon_info=None)` installs
    fresh stubs (via tests.kodistubs.install_kodi_stubs) reloading
    lib.ui.compat/lib.ui.uicommon/lib.ui.router/lib.ui.views/
    lib.ui.infowindow/lib.ui.detailwindow/lib.ui.continuewatching, and
    returns a namespace with `.continuewatching`, `.views`,
    `.infowindow`, `.detailwindow`, and `.env`. Every call is torn down
    automatically, in reverse order, at test end.
    """
    with contextlib.ExitStack() as stack:
        def _load(addon_info=None):
            return stack.enter_context(install_kodi_stubs(
                reload=_RELOAD_MODULE_NAMES,
                addon_info=addon_info,
            ))

        yield _load


def _wire_data_layer(continuewatching_mod, views_mod, store, fetch_meta=None):
    continuewatching_mod.get_store = lambda: store
    # Sequential stand-in for the real bounded thread pool - deterministic,
    # and still proves `open_continue_watching()` drives `_fetch_meta`
    # through `_map_addons` rather than calling it directly.
    views_mod._map_addons = lambda fn, items: [fn(item) for item in items]
    if fetch_meta is not None:
        views_mod._fetch_meta = fetch_meta


# ---------------------------------------------------------------------------
# resumable_candidates() - resumable band boundaries
# ---------------------------------------------------------------------------


def test_resumable_candidates_excludes_just_below_min_percent(load_continuewatching):
    cw = load_continuewatching().continuewatching
    entries = [_entry(position_ms=9, duration_ms=1000)]  # 0.9%
    assert cw.resumable_candidates(entries) == []


def test_resumable_candidates_includes_at_min_percent(load_continuewatching):
    cw = load_continuewatching().continuewatching
    entries = [_entry(position_ms=10, duration_ms=1000)]  # 1.0%
    assert cw.resumable_candidates(entries) == entries


def test_resumable_candidates_includes_just_below_max_percent(load_continuewatching):
    cw = load_continuewatching().continuewatching
    entries = [_entry(position_ms=949, duration_ms=1000)]  # 94.9%
    assert cw.resumable_candidates(entries) == entries


def test_resumable_candidates_excludes_at_max_percent(load_continuewatching):
    cw = load_continuewatching().continuewatching
    entries = [_entry(position_ms=950, duration_ms=1000)]  # 95.0%
    assert cw.resumable_candidates(entries) == []


def test_resumable_candidates_excludes_zero_duration(load_continuewatching):
    cw = load_continuewatching().continuewatching
    entries = [_entry(position_ms=500, duration_ms=0)]
    assert cw.resumable_candidates(entries) == []


# ---------------------------------------------------------------------------
# resumable_candidates() - grouping, recency, cap, movie video_id
# ---------------------------------------------------------------------------


def test_resumable_candidates_groups_by_type_id_keeping_most_recent(load_continuewatching):
    cw = load_continuewatching().continuewatching
    older = _entry(type_='series', id_='tt9', video_id='tt9:1:1', updated_at='2020-01-01T00:00:00Z')
    newer = _entry(type_='series', id_='tt9', video_id='tt9:1:2', updated_at='2020-01-02T00:00:00Z')

    result = cw.resumable_candidates([older, newer])

    assert result == [newer]


def test_resumable_candidates_sorted_by_updated_at_descending(load_continuewatching):
    cw = load_continuewatching().continuewatching
    first = _entry(id_='tt1', updated_at='2020-01-01T00:00:00Z')
    second = _entry(id_='tt2', updated_at='2020-03-01T00:00:00Z')
    third = _entry(id_='tt3', updated_at='2020-02-01T00:00:00Z')

    result = cw.resumable_candidates([first, second, third])

    assert [entry['id'] for entry in result] == ['tt2', 'tt3', 'tt1']


def test_resumable_candidates_capped_at_max_items(load_continuewatching):
    cw = load_continuewatching().continuewatching
    entries = [
        _entry(id_='tt%d' % i, updated_at='2020-01-%02dT00:00:00Z' % (i + 1))
        for i in range(20)
    ]

    result = cw.resumable_candidates(entries)

    assert len(result) == cw._MAX_ITEMS == 15
    # The 15 most recent (highest day-of-month) survive, most recent first.
    assert [entry['id'] for entry in result] == ['tt%d' % i for i in range(19, 4, -1)]


def test_resumable_candidates_movie_none_video_id_flows_through(load_continuewatching):
    cw = load_continuewatching().continuewatching
    entry = _entry(type_='movie', id_='tt1', video_id=None)

    result = cw.resumable_candidates([entry])

    assert result == [entry]
    assert result[0]['video_id'] is None


# ---------------------------------------------------------------------------
# has_resumable()
# ---------------------------------------------------------------------------


def test_has_resumable_true_when_a_candidate_is_in_band(load_continuewatching):
    cw = load_continuewatching().continuewatching
    store = _FakeStore(entries=[_entry(position_ms=500, duration_ms=1000)])
    assert cw.has_resumable(store) is True


def test_has_resumable_false_when_store_empty(load_continuewatching):
    cw = load_continuewatching().continuewatching
    assert cw.has_resumable(_FakeStore(entries=[])) is False


def test_has_resumable_false_when_nothing_in_band(load_continuewatching):
    cw = load_continuewatching().continuewatching
    store = _FakeStore(entries=[_entry(position_ms=950, duration_ms=1000)])  # 95.0% - out of band
    assert cw.has_resumable(store) is False


# ---------------------------------------------------------------------------
# open_continue_watching() - meta fetch
# ---------------------------------------------------------------------------


def test_open_continue_watching_skips_failed_meta_fetch(load_continuewatching, monkeypatch):
    ctx = load_continuewatching()
    cw = ctx.continuewatching
    entries = [
        _entry(type_='movie', id_='tt-fail', updated_at='2020-01-02T00:00:00Z'),
        _entry(type_='movie', id_='tt-ok', updated_at='2020-01-01T00:00:00Z'),
    ]
    metas_by_id = {'tt-ok': {'id': 'tt-ok', 'type': 'movie', 'name': 'OK'}}

    def fake_fetch_meta(stype, sid):
        return metas_by_id.get(sid)

    _wire_data_layer(cw, ctx.views, _FakeStore(entries=entries), fake_fetch_meta)
    captured = {}
    monkeypatch.setattr(
        ctx.infowindow, 'open_showcase',
        lambda metas, catalog_title=None: captured.update(metas=metas, catalog_title=catalog_title) or None,
    )

    result = cw.open_continue_watching()

    assert result is False  # open_showcase() returned None -> nothing selected
    assert captured['metas'] == [{'id': 'tt-ok', 'type': 'movie', 'name': 'OK'}]


def test_open_continue_watching_empty_after_fetch_notifies_and_returns_false(load_continuewatching, monkeypatch):
    ctx = load_continuewatching()
    cw = ctx.continuewatching
    entries = [_entry(type_='movie', id_='tt1')]
    _wire_data_layer(cw, ctx.views, _FakeStore(entries=entries), lambda stype, sid: None)

    def _unexpected(*a, **k):
        raise AssertionError('open_showcase must not be reached when every fetch failed')

    monkeypatch.setattr(ctx.infowindow, 'open_showcase', _unexpected)

    result = cw.open_continue_watching()

    assert result is False
    assert ctx.env.notifications == [('Rivulet', 'STR30030', 'info', 4000)]


# ---------------------------------------------------------------------------
# open_continue_watching() - a selection routes to open_detail()
# ---------------------------------------------------------------------------


def test_open_continue_watching_selection_opens_detail_and_returns_its_result(load_continuewatching, monkeypatch):
    ctx = load_continuewatching()
    cw = ctx.continuewatching
    entries = [_entry(type_='movie', id_='tt9')]
    meta = {'id': 'tt9', 'type': 'movie', 'name': 'Batman'}
    _wire_data_layer(cw, ctx.views, _FakeStore(entries=entries), lambda stype, sid: meta)
    captured = {}
    monkeypatch.setattr(
        ctx.infowindow, 'open_showcase',
        lambda metas, catalog_title=None: captured.update(catalog_title=catalog_title) or meta,
    )

    def fake_open_detail(stype, sid):
        captured['args'] = (stype, sid)
        return True

    monkeypatch.setattr(ctx.detailwindow, 'open_detail', fake_open_detail)

    result = cw.open_continue_watching()

    assert result is True
    assert captured['args'] == ('movie', 'tt9')
    assert captured['catalog_title'] == 'STR30231'


def test_open_continue_watching_no_selection_returns_false_without_opening_detail(load_continuewatching, monkeypatch):
    ctx = load_continuewatching()
    cw = ctx.continuewatching
    entries = [_entry(type_='movie', id_='tt1')]
    meta = {'id': 'tt1', 'type': 'movie', 'name': 'One'}
    _wire_data_layer(cw, ctx.views, _FakeStore(entries=entries), lambda stype, sid: meta)
    monkeypatch.setattr(ctx.infowindow, 'open_showcase', lambda metas, catalog_title=None: None)

    def _unexpected(*a, **k):
        raise AssertionError('open_detail must not be called without a selection')

    monkeypatch.setattr(ctx.detailwindow, 'open_detail', _unexpected)

    result = cw.open_continue_watching()

    assert result is False
