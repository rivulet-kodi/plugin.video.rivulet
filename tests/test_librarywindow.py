"""Tests for lib.ui.librarywindow: open_library(), Rivulet's custom
replacement for the classical `library()` directory - fetches the
logged-in user's Stremio library datastore, then shows it directly in the
coverflow overlay - exercised against the shared fake xbmc/xbmcgui stubs in
tests/kodistubs (no real Kodi runtime, no network).

lib.ui.librarywindow imports its ENTIRE data layer (lib.store.Store,
lib.stremio.api.StremioAPI) lazily, from inside open_library() itself, so -
mirroring tests/test_catalogpicker.py's `store_module.Store` monkeypatch for
catalogpicker.open_catalog_picker()'s own lazy `from lib.store import
Store` - the data layer is faked by monkeypatching the attribute on the
real `lib.store`/`lib.stremio.api` modules (`from lib.X import Y` resolves
`Y` off `sys.modules['lib.X']` at call time, so patching that attribute is
enough; lib.ui.librarywindow is never reloaded with a rebound name of its
own to patch).

open_library() also lazily `from lib.ui.infowindow import open_showcase`/
`from lib.ui.detailwindow import open_detail`, so load_librarywindow
reloads lib.ui.infowindow/lib.ui.detailwindow fresh alongside
lib.ui.compat/lib.ui.uicommon/lib.ui.router/lib.ui.librarywindow to get a
handle (`ctx.infowindow`/`ctx.detailwindow`) this file monkeypatches
directly.
"""
import contextlib

import pytest

import lib.store as store_module
import lib.stremio.api as api_module
from lib.stremio.api import ApiError
from tests.kodistubs import install_kodi_stubs

_RELOAD_MODULE_NAMES = (
    'lib.ui.compat', 'lib.ui.uicommon', 'lib.ui.router',
    'lib.ui.infowindow', 'lib.ui.detailwindow', 'lib.ui.librarywindow',
)


class _FakeStore:
    """Fake `lib.store.Store`: only `get_auth()` matters to open_library()."""

    def __init__(self, auth=None):
        self._auth = auth

    def get_auth(self):
        return self._auth


class _FakeStremioAPI:
    """Fake `lib.stremio.api.StremioAPI`. `datastore_result`/
    `datastore_error` stand in for a successful `datastore_get()` call vs
    one that raises `ApiError`; `.calls` records every `(auth_key,
    collection, all)` invocation."""

    def __init__(self, datastore_result=None, datastore_error=None):
        self._datastore_result = datastore_result or []
        self._datastore_error = datastore_error
        self.calls = []

    def datastore_get(self, auth_key, collection='libraryItem', all=True):
        self.calls.append((auth_key, collection, all))
        if self._datastore_error is not None:
            raise self._datastore_error
        return self._datastore_result


@pytest.fixture
def load_librarywindow():
    """Factory fixture: `load_librarywindow(addon_info=None)` installs
    fresh stubs (via tests.kodistubs.install_kodi_stubs) reloading
    lib.ui.compat/lib.ui.uicommon/lib.ui.router/lib.ui.infowindow/
    lib.ui.detailwindow/lib.ui.librarywindow, and returns a namespace with
    `.librarywindow`, `.compat`, `.router`, `.infowindow`, `.detailwindow`,
    and `.env`. Every call is torn down automatically, in reverse order, at
    test end.
    """
    with contextlib.ExitStack() as stack:
        def _load(addon_info=None):
            return stack.enter_context(install_kodi_stubs(
                reload=_RELOAD_MODULE_NAMES,
                addon_info=addon_info,
            ))

        yield _load


def _wire_data_layer(monkeypatch, store, api):
    monkeypatch.setattr(store_module, 'Store', lambda *a, **k: store)
    monkeypatch.setattr(api_module, 'StremioAPI', lambda *a, **k: api)


# ---------------------------------------------------------------------------
# open_library() - not logged in
# ---------------------------------------------------------------------------


def test_open_library_without_auth_notifies_and_returns_false(load_librarywindow, monkeypatch):
    ctx = load_librarywindow()
    librarywindow = ctx.librarywindow
    api = _FakeStremioAPI()
    _wire_data_layer(monkeypatch, _FakeStore(auth=None), api)

    result = librarywindow.open_library()

    assert result is False
    assert ctx.env.notifications == [('Rivulet', 'STR30020', 'info', 4000)]
    assert api.calls == []  # datastore_get() is never even attempted


# ---------------------------------------------------------------------------
# open_library() - fetch: filters removed entries and entries without _id
# ---------------------------------------------------------------------------


def test_open_library_fetch_filters_removed_and_missing_id(load_librarywindow, monkeypatch):
    ctx = load_librarywindow()
    librarywindow = ctx.librarywindow
    auth = {'authKey': 'abc123'}
    entries = [
        {'_id': 'tt1', 'name': 'Kept Movie', 'type': 'movie', 'poster': 'p1', 'background': 'b1'},
        {'_id': 'tt2', 'name': 'Removed Show', 'type': 'series', 'removed': True},
        {'name': 'No Id Here', 'type': 'movie'},  # dropped: no `_id`
    ]
    api = _FakeStremioAPI(datastore_result=entries)
    _wire_data_layer(monkeypatch, _FakeStore(auth=auth), api)
    captured = {}
    monkeypatch.setattr(ctx.infowindow, 'open_showcase', lambda metas: captured.setdefault('metas', metas) and None)

    result = librarywindow.open_library()

    assert result is False  # open_showcase() returned None -> nothing selected
    assert captured['metas'] == [
        {'id': 'tt1', 'name': 'Kept Movie', 'type': 'movie', 'poster': 'p1', 'background': 'b1'},
    ]
    assert api.calls == [('abc123', 'libraryItem', True)]


def test_open_library_empty_fetch_notifies_and_returns_false(load_librarywindow, monkeypatch):
    ctx = load_librarywindow()
    librarywindow = ctx.librarywindow
    auth = {'authKey': 'abc123'}
    api = _FakeStremioAPI(datastore_result=[])
    _wire_data_layer(monkeypatch, _FakeStore(auth=auth), api)
    opened = []
    monkeypatch.setattr(ctx.infowindow, 'open_showcase', lambda metas: opened.append(metas))

    result = librarywindow.open_library()

    assert result is False
    assert ctx.env.notifications == [('Rivulet', 'STR30030', 'info', 4000)]
    assert opened == []  # the coverflow is never opened on an empty library


# ---------------------------------------------------------------------------
# open_library() - a selection routes to open_detail()
# ---------------------------------------------------------------------------


def test_open_library_selection_opens_detail_and_returns_its_result(load_librarywindow, monkeypatch):
    ctx = load_librarywindow()
    librarywindow = ctx.librarywindow
    auth = {'authKey': 'abc123'}
    entry = {'_id': 'tt9', 'name': 'Batman', 'type': 'movie', 'poster': None, 'background': None}
    api = _FakeStremioAPI(datastore_result=[entry])
    _wire_data_layer(monkeypatch, _FakeStore(auth=auth), api)
    chosen = {'id': 'tt9', 'name': 'Batman', 'type': 'movie'}
    monkeypatch.setattr(ctx.infowindow, 'open_showcase', lambda metas: chosen)
    captured = {}

    def fake_open_detail(stype, sid):
        captured['args'] = (stype, sid)
        return True

    monkeypatch.setattr(ctx.detailwindow, 'open_detail', fake_open_detail)

    result = librarywindow.open_library()

    assert result is True
    assert captured['args'] == ('movie', 'tt9')


def test_open_library_no_selection_returns_false_without_opening_detail(load_librarywindow, monkeypatch):
    ctx = load_librarywindow()
    librarywindow = ctx.librarywindow
    auth = {'authKey': 'abc123'}
    entry = {'_id': 'tt1', 'name': 'One', 'type': 'movie'}
    api = _FakeStremioAPI(datastore_result=[entry])
    _wire_data_layer(monkeypatch, _FakeStore(auth=auth), api)
    monkeypatch.setattr(ctx.infowindow, 'open_showcase', lambda metas: None)

    def _unexpected(*a, **k):
        raise AssertionError('open_detail must not be called without a selection')

    monkeypatch.setattr(ctx.detailwindow, 'open_detail', _unexpected)

    result = librarywindow.open_library()

    assert result is False


# ---------------------------------------------------------------------------
# open_library() - datastore_get() failure
# ---------------------------------------------------------------------------


def test_open_library_datastore_error_notifies_and_returns_false(load_librarywindow, monkeypatch):
    ctx = load_librarywindow()
    librarywindow = ctx.librarywindow
    auth = {'authKey': 'abc123'}
    api = _FakeStremioAPI(datastore_error=ApiError('down'))
    _wire_data_layer(monkeypatch, _FakeStore(auth=auth), api)

    def _unexpected(*a, **k):
        raise AssertionError('open_showcase must not be reached when the fetch failed')

    monkeypatch.setattr(ctx.infowindow, 'open_showcase', _unexpected)

    result = librarywindow.open_library()

    assert result is False
    assert ctx.env.notifications == [('Rivulet', 'STR30032', 'info', 4000)]
    assert any('down' in msg for msg, _level in ctx.env.log_calls)
