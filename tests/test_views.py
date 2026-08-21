"""Tests for lib.ui.views: the shared addon-fetch/sync helpers
(`_fetch_meta`, `_fetch_catalog`, `_sync_addons_if_logged_in`,
`_refresh_addon_manifests`) the custom WindowXML dialogs import lazily,
the RunPlugin script actions (`login`, `logout`, `sync_addons_now`,
`open_settings`), and the minimal `home()` recovery directory -
exercised against the shared fake xbmc/xbmcgui/xbmcplugin/xbmcaddon/
xbmcvfs stubs in tests/kodistubs (no real Kodi runtime, no network).

`load_views` wraps `tests.kodistubs.install_kodi_stubs()`, (re)importing
lib.ui.compat/lib.ui.router/lib.ui.views fresh against the stubs for each
call, and restoring sys.modules / the lib.ui package's leaf attributes
exactly at teardown. lib.ui.router imports lib.ui.views/player only
lazily inside run() (never called here), so there is no stale
cross-module binding risk.

The data layer (lib.store.Store / lib.stremio.addons.AddonClient) is faked
by assigning to the provider names lib.ui.views actually calls
(`views.get_store`, `views.get_client`, imported from
lib.ui.dependencies), since both are single process-wide singletons owned
by that module.
"""
import contextlib
import threading
import time

import pytest

from lib.stremio.addons import AddonError
from lib.stremio.api import ApiError
from lib.ui import urlutil
from tests.kodistubs import install_kodi_stubs

_RELOAD_MODULE_NAMES = ('lib.ui.compat', 'lib.ui.router', 'lib.ui.views', 'lib.ui.infowindow', 'lib.ui.dialogs')

#: Generous safety-valve timeout (seconds) for the Barrier/Event-gated
#: fanout tests below: bounds a genuine deadlock/regression as a test
#: failure instead of hanging the suite. Not used to assert performance.
_GATE_TIMEOUT = 5.0


# ---------------------------------------------------------------------------
# load_views: installs the shared stubs, imports lib.ui.views fresh
# ---------------------------------------------------------------------------


@pytest.fixture
def load_views():
    """Factory fixture: `load_views(addon_info=None, settings=None,
    info_labels=None, dialog_inputs=None, dialog_yesno=None,
    localized=None)` installs fresh stubs (via
    tests.kodistubs.install_kodi_stubs) reloading lib.ui.compat/
    lib.ui.router/lib.ui.views/lib.ui.infowindow, and returns a namespace
    with `.views`, `.compat`, `.router`, `.infowindow`, and `.env` (the
    call recorder). Every call is torn down automatically, in reverse
    order, at test end.
    """
    with contextlib.ExitStack() as stack:
        def _load(addon_info=None, settings=None, info_labels=None, dialog_inputs=None,
                   dialog_yesno=None, localized=None):
            return stack.enter_context(install_kodi_stubs(
                reload=_RELOAD_MODULE_NAMES,
                addon_info=addon_info,
                settings=settings,
                info_labels=info_labels or {'System.BuildVersion': '21.0 Git:abcdef'},
                dialog_inputs=dialog_inputs,
                dialog_yesno=dialog_yesno,
                localized=localized,
            ))

        yield _load


# ---------------------------------------------------------------------------
# Fake data layer (lib.store.Store / lib.stremio.addons.AddonClient)
# ---------------------------------------------------------------------------




class FakeStore:
    """Fake `lib.store.Store`: an in-memory addons list + auth dict, with
    the same set_auth/update_addons contracts as the real filesystem-backed
    Store (see lib/store.py) so login()/logout()/sync_addons_now() exercise
    realistic behavior.
    """

    def __init__(self, addons=None, auth=None, data_dir=None):
        self._addons = addons if addons is not None else []
        self._auth = auth
        self.data_dir = data_dir     # None unless a test opts into metacache exercise
        self.auth_set_calls = []     # [auth_dict_or_None, ...]
        self.addons_set_calls = []   # [[descriptor, ...], ...]
        self.update_addons_calls = []  # [transform, ...]

    def get_addons(self):
        return self._addons

    def get_enabled_addons(self):
        return [a for a in self._addons if not (a.get('flags') or {}).get('disabled')]

    def get_auth(self):
        return self._auth

    def set_auth(self, auth):
        self.auth_set_calls.append(auth)
        self._auth = auth

    def set_addons(self, addons):
        addons = list(addons)
        self.addons_set_calls.append(addons)
        self._addons = addons

    def update_addons(self, transform, max_attempts=3):
        """Matches the real Store.update_addons(transform) contract: call
        `transform(current_addons)` and persist the result via set_addons -
        no actual concurrency/retry to simulate here (FakeStore is
        single-threaded, in-memory), just the same call shape callers rely
        on (login()'s merge closure / views._refresh_addon_manifests).
        Records each call in `update_addons_calls` so tests can prove a
        write went through this CAS-safe path rather than a raw
        `set_addons()`."""
        self.update_addons_calls.append(transform)
        new_addons = transform(self._addons)
        self.set_addons(new_addons)
        return new_addons


class FakeAddonClient:
    """Fake `lib.stremio.addons.AddonClient`. `meta_results`/`manifest_results`
    (transport_url -> value-or-Exception) let a test script different
    addons differently - a dict value that is an Exception instance is
    raised instead of returned, standing in for a network/manifest
    failure. `manifest_result`/`manifest_error` remain as the single
    catch-all default for `manifest()` when a transport_url has no entry
    in `manifest_results`.

    `delays` (transport_url -> seconds) makes meta() call time.sleep()
    before returning/raising - used where a test needs one addon to
    genuinely answer later than another (e.g. proving a faster addon's
    result wins), not to prove concurrency itself.

    `gates` (transport_url -> threading.Event) blocks that addon's call
    until the test sets the event, for proving a caller returned without
    ever waiting on a given addon: if the caller only proceeds after the
    gate opens, it read that addon's result. `_GATE_TIMEOUT` bounds the
    wait so a regression that truly blocks fails on a timeout rather than
    hanging the suite, instead of asserting on elapsed time.
    """

    def __init__(self, meta_results=None, manifest_result=None, manifest_error=None,
                 manifest_results=None, delays=None, gates=None):
        self._meta_results = meta_results or {}
        self._manifest_result = manifest_result
        self._manifest_error = manifest_error
        self._manifest_results = manifest_results or {}
        self._delays = delays or {}
        self._gates = gates or {}
        self.manifest_calls = []
        self.meta_calls = []

    def _delay(self, transport_url):
        gate = self._gates.get(transport_url)
        if gate is not None:
            gate.wait(timeout=_GATE_TIMEOUT)
            return
        seconds = self._delays.get(transport_url)
        if seconds:
            time.sleep(seconds)

    def meta(self, transport_url, stype, sid):
        self.meta_calls.append(transport_url)
        self._delay(transport_url)
        result = self._meta_results.get(transport_url)
        if isinstance(result, Exception):
            raise result
        return result

    def manifest(self, url):
        self.manifest_calls.append(url)
        if url in self._manifest_results:
            result = self._manifest_results[url]
            if isinstance(result, Exception):
                raise result
            return result
        if self._manifest_error is not None:
            raise self._manifest_error
        return self._manifest_result


class FakeStremioAPI:
    """Fake `lib.stremio.api.StremioAPI` for login()/logout()/sync_addons_now()
    (the addon_collection_* methods are the push/pull side of addon sync)."""

    def __init__(self, login_result=None, login_error=None, addon_collection_result=None,
                 addon_collection_error=None, addon_collection_set_error=None, logout_error=None):
        self._login_result = login_result
        self._login_error = login_error
        self._addon_collection_result = addon_collection_result
        self._addon_collection_error = addon_collection_error
        self._addon_collection_set_error = addon_collection_set_error
        self._logout_error = logout_error
        self.logout_calls = []
        self.addon_collection_set_calls = []  # [(auth_key, [descriptor, ...]), ...]

    def login(self, email, password):
        if self._login_error is not None:
            raise self._login_error
        return self._login_result

    def addon_collection_get(self, auth_key):
        if self._addon_collection_error is not None:
            raise self._addon_collection_error
        return self._addon_collection_result

    def addon_collection_set(self, auth_key, addons):
        self.addon_collection_set_calls.append((auth_key, list(addons)))
        if self._addon_collection_set_error is not None:
            raise self._addon_collection_set_error

    def logout(self, auth_key):
        self.logout_calls.append(auth_key)
        if self._logout_error is not None:
            raise self._logout_error


def _wire_data_layer(views, store, client):
    views.get_store = lambda: store
    views.get_client = lambda: client


def _wire_api(views, api):
    views.StremioAPI = lambda *a, **k: api


# ---------------------------------------------------------------------------
# home()
# ---------------------------------------------------------------------------


def test_home_lists_notice_and_settings_rows(load_views):
    ctx = load_views()
    views, compat, router = ctx.views, ctx.compat, ctx.router

    views.home()

    call = ctx.env.directory_items[-1]
    items = call['items']
    assert len(items) == 2
    notice_url, notice_li, notice_is_folder = items[0]
    settings_url, settings_li, settings_is_folder = items[1]
    assert notice_li.getLabel() == 'STR30032'
    assert notice_is_folder is True
    assert settings_li.getLabel() == 'STR30004'
    assert settings_is_folder is False
    assert settings_url == urlutil.url_for(router.BASE_URL, 'settings')
    assert ctx.env.content[-1] == (call['handle'], 'files')
    assert ctx.env.plugin_category[-1] == (call['handle'], compat.ADDON_NAME)
    assert ctx.env.end_of_directory[-1]['succeeded'] is True


def test_home_notice_row_is_a_folder_reopening_the_recovery_directory(load_views):
    """The notice row MUST be a folder, not an isFolder=False action row.
    Kodi treats a non-folder row as a playable item, so selecting it would
    make Kodi try to play `?action=home` - which answers with
    endOfDirectory() and fails playback, breaking a row in the one screen
    that exists to survive breakage. As a folder it just redraws this
    directory, i.e. a harmless retry."""
    ctx = load_views()
    views, router = ctx.views, ctx.router

    views.home()

    notice_url, _li, _is_folder = ctx.env.directory_items[-1]['items'][0]
    assert notice_url == urlutil.url_for(router.BASE_URL, 'home')


# ---------------------------------------------------------------------------
# open_settings()
# ---------------------------------------------------------------------------


def test_open_settings_opens_addon_settings_and_ends_directory_failed(load_views):
    ctx = load_views()
    views = ctx.views

    views.open_settings()

    assert ctx.env.opened_settings is True
    assert ctx.env.end_of_directory[-1] == {
        'handle': -1, 'succeeded': False, 'updateListing': False, 'cacheToDisc': False,
    }


# ---------------------------------------------------------------------------
# compat helpers
# ---------------------------------------------------------------------------


def test_addon_media_path_builds_from_addon_id_not_hardcoded(load_views):
    ctx = load_views(addon_info={'id': 'org.custom.testaddon'})
    compat = ctx.compat

    path = compat.addon_media_path('discover.png')

    assert path.endswith('org.custom.testaddon/resources/media/discover.png')
    assert 'plugin.video.rivulet' not in path


def test_addon_fanart_returns_configured_fanart_path(load_views):
    sentinel = 'special://home/addons/plugin.video.rivulet/resources/custom-fanart.jpg'
    ctx = load_views(addon_info={'fanart': sentinel})

    assert ctx.compat.addon_fanart() == sentinel

# ---------------------------------------------------------------------------
# _fetch_meta()
# ---------------------------------------------------------------------------


def test_fetch_meta_skips_unsupported_and_erroring_addons_first_hit_wins(load_views):
    ctx = load_views()
    views = ctx.views
    descriptor_skip = {
        'transportUrl': 't1',
        'manifest': {'id': 'org.skip', 'resources': ['catalog'], 'types': ['series'], 'idPrefixes': ['tt']},
    }
    descriptor_error = {
        'transportUrl': 'https://err.example/manifest.json',
        'manifest': {'id': 'org.err', 'resources': ['meta'], 'types': ['series'], 'idPrefixes': ['tt']},
    }
    descriptor_hit = {
        'transportUrl': 't3',
        'manifest': {'id': 'org.hit', 'resources': ['meta'], 'types': ['series'], 'idPrefixes': ['tt']},
    }
    hit_meta = {
        'id': 'tt1', 'name': 'A Show', 'type': 'series',
        'videos': [{'season': 1, 'episode': 1, 'id': 'ep1'}],
    }
    client = FakeAddonClient(meta_results={
        'https://err.example/manifest.json': AddonError('meta fetch boom'),
        't3': hit_meta,
    })
    _wire_data_layer(views, FakeStore(addons=[descriptor_skip, descriptor_error, descriptor_hit]), client)

    result = views._fetch_meta('series', 'tt1')

    assert result == hit_meta
    all_messages = ' '.join(msg for msg, _level in ctx.env.log_calls)
    assert 'err.example' in all_messages
    assert 'meta fetch boom' not in all_messages


def test_fetch_meta_skips_addon_returning_no_usable_result(load_views):
    ctx = load_views()
    views = ctx.views
    descriptor_empty = {
        'transportUrl': 't1',
        'manifest': {'id': 'org.empty', 'resources': ['meta'], 'types': ['series'], 'idPrefixes': ['tt']},
    }
    descriptor_hit = {
        'transportUrl': 't2',
        'manifest': {'id': 'org.hit', 'resources': ['meta'], 'types': ['series'], 'idPrefixes': ['tt']},
    }
    hit_meta = {
        'id': 'tt1', 'name': 'A Show', 'type': 'series',
        'videos': [{'season': 1, 'episode': 1, 'id': 'ep1'}],
    }
    client = FakeAddonClient(meta_results={
        't1': None,  # claims support but returns nothing usable -> aggregation must keep going
        't2': hit_meta,
    })
    _wire_data_layer(views, FakeStore(addons=[descriptor_empty, descriptor_hit]), client)

    result = views._fetch_meta('series', 'tt1')

    assert result == hit_meta


def test_fetch_meta_never_dispatches_to_a_disabled_addon(load_views):
    """_fetch_meta()'s fan-out must read get_enabled_addons(), not
    get_addons(): a disabled meta-capable addon must receive no request,
    even though it would otherwise be the only hit."""
    ctx = load_views()
    views = ctx.views
    descriptor_disabled = {
        'transportUrl': 'https://disabled.example/manifest.json',
        'manifest': {'id': 'org.disabled', 'resources': ['meta'], 'types': ['series'], 'idPrefixes': ['tt']},
        'flags': {'disabled': True},
    }
    client = FakeAddonClient(meta_results={
        'https://disabled.example/manifest.json': {'id': 'tt1', 'name': 'Should Never Be Fetched', 'type': 'series'},
    })
    _wire_data_layer(views, FakeStore(addons=[descriptor_disabled]), client)

    result = views._fetch_meta('series', 'tt1')

    assert result is None
    assert client.meta_calls == []


def test_fetch_meta_returns_without_waiting_for_a_slower_addon(load_views):
    """The first-listed (preferred) addon is the slow one; a second addon
    answers almost immediately with an equally usable result. The slow
    addon's call is gated on a threading.Event the test never sets, so
    if `_fetch_meta` still returns a result, that result cannot have come
    from the slow addon - deterministic proof that the concurrent version
    returned as soon as the fast addon answered instead of waiting for
    the slow one. `_GATE_TIMEOUT` bounds how long a regression that truly
    waits can block the test, without asserting on elapsed time.
    """
    ctx = load_views()
    views = ctx.views
    descriptor_slow = {
        'transportUrl': 't-slow',
        'manifest': {'id': 'org.slow', 'resources': ['meta'], 'types': ['series'], 'idPrefixes': ['tt']},
    }
    descriptor_fast = {
        'transportUrl': 't-fast',
        'manifest': {'id': 'org.fast', 'resources': ['meta'], 'types': ['series'], 'idPrefixes': ['tt']},
    }
    slow_gate = threading.Event()  # deliberately never set: t-slow must not be allowed to answer
    client = FakeAddonClient(
        meta_results={
            't-slow': {'id': 'tt1', 'name': 'Slow Show', 'type': 'series'},
            't-fast': {'id': 'tt1', 'name': 'Fast Show', 'type': 'series'},
        },
        gates={'t-slow': slow_gate},
    )
    # descriptor_slow is listed FIRST (the preferred addon), but it is the
    # slow one - _fetch_meta must not serialize behind it.
    _wire_data_layer(views, FakeStore(addons=[descriptor_slow, descriptor_fast]), client)

    result = views._fetch_meta('series', 'tt1')

    assert result['name'] == 'Fast Show'
    slow_gate.set()  # release the still-blocked background worker thread


def test_fetch_meta_prefers_earlier_addon_when_it_answers_at_least_as_fast(load_views):
    """When the first-listed (preferred) addon is not the straggler, the
    old sequential "first hit in store.get_addons() order wins" behavior
    is fully preserved: a slower second addon's answer must lose even
    though it is also usable.
    """
    ctx = load_views()
    views = ctx.views
    descriptor_first = {
        'transportUrl': 't-first',
        'manifest': {'id': 'org.first', 'resources': ['meta'], 'types': ['series'], 'idPrefixes': ['tt']},
    }
    descriptor_second = {
        'transportUrl': 't-second',
        'manifest': {'id': 'org.second', 'resources': ['meta'], 'types': ['series'], 'idPrefixes': ['tt']},
    }
    client = FakeAddonClient(
        meta_results={
            't-first': {'id': 'tt1', 'name': 'First Wins', 'type': 'series'},
            't-second': {'id': 'tt1', 'name': 'Second Loses', 'type': 'series'},
        },
        delays={'t-second': 0.3},
    )
    _wire_data_layer(views, FakeStore(addons=[descriptor_first, descriptor_second]), client)

    result = views._fetch_meta('series', 'tt1')

    assert result['name'] == 'First Wins'


def test_fetch_meta_cache_hit_skips_addon_fanout(load_views, tmp_path):
    """`_fetch_meta` only caches when `store.data_dir` is set (the real
    Store has one, `FakeStore` normally does not) - opting a FakeStore
    into a real tmp_path here proves a second call for the same
    (stype, sid) is served from disk without touching any addon."""
    ctx = load_views()
    views = ctx.views
    transport = 't1'
    descriptor = {
        'transportUrl': transport,
        'manifest': {'id': 'org.a', 'resources': ['meta'], 'types': ['movie'], 'idPrefixes': ['tt']},
    }
    client = FakeAddonClient(meta_results={transport: {'id': 'tt1', 'name': 'Cached', 'type': 'movie'}})
    _wire_data_layer(views, FakeStore(addons=[descriptor], data_dir=str(tmp_path)), client)

    first = views._fetch_meta('movie', 'tt1')
    second = views._fetch_meta('movie', 'tt1')

    assert first == second == {'id': 'tt1', 'name': 'Cached', 'type': 'movie'}
    assert client.meta_calls == [transport]  # second call never reached the addon


def test_fetch_meta_cache_is_scoped_per_stype_and_sid(load_views, tmp_path):
    ctx = load_views()
    views = ctx.views
    transport = 't1'
    descriptor = {
        'transportUrl': transport,
        'manifest': {
            'id': 'org.a', 'resources': ['meta'], 'types': ['movie', 'series'], 'idPrefixes': ['tt'],
        },
    }
    client = FakeAddonClient(meta_results={transport: {'id': 'tt1', 'name': 'X'}})
    _wire_data_layer(views, FakeStore(addons=[descriptor], data_dir=str(tmp_path)), client)

    views._fetch_meta('movie', 'tt1')
    views._fetch_meta('series', 'tt1')

    assert client.meta_calls == [transport, transport]  # distinct keys - no false cache hit


def test_fetch_meta_does_not_cache_a_failed_lookup(load_views, tmp_path):
    ctx = load_views()
    views = ctx.views
    descriptor = {
        'transportUrl': 't1',
        'manifest': {'id': 'org.a', 'resources': ['meta'], 'types': ['movie'], 'idPrefixes': ['tt']},
    }
    client = FakeAddonClient(meta_results={'t1': None})  # claims support, returns nothing usable
    _wire_data_layer(views, FakeStore(addons=[descriptor], data_dir=str(tmp_path)), client)

    assert views._fetch_meta('movie', 'tt1') is None
    assert views._fetch_meta('movie', 'tt1') is None
    assert client.meta_calls == ['t1', 't1']  # every call re-tried the addon, none was cached


def test_fetch_meta_store_false_skips_the_write_but_still_returns_a_fresh_fetch(load_views, tmp_path):
    """`store=False` (mystuff._enrich()'s batching
    path, Finding 7) must not touch the on-disk cache on a fresh fetch -
    the caller takes over persisting the result itself via
    `metacache.store_cached_metas()`."""
    ctx = load_views()
    views = ctx.views
    transport = 't1'
    descriptor = {
        'transportUrl': transport,
        'manifest': {'id': 'org.a', 'resources': ['meta'], 'types': ['movie'], 'idPrefixes': ['tt']},
    }
    client = FakeAddonClient(meta_results={transport: {'id': 'tt1', 'name': 'Fresh', 'type': 'movie'}})
    _wire_data_layer(views, FakeStore(addons=[descriptor], data_dir=str(tmp_path)), client)

    result = views._fetch_meta('movie', 'tt1', store=False)

    assert result == {'id': 'tt1', 'name': 'Fresh', 'type': 'movie'}
    from lib.ui import metacache
    assert metacache.load_cached_meta(str(tmp_path), 'movie', 'tt1') is None  # never written to disk


def test_fetch_meta_store_false_still_serves_an_existing_cache_hit(load_views, tmp_path):
    """The cache READ is unaffected by `store` - only the write is gated."""
    ctx = load_views()
    views = ctx.views
    transport = 't1'
    descriptor = {
        'transportUrl': transport,
        'manifest': {'id': 'org.a', 'resources': ['meta'], 'types': ['movie'], 'idPrefixes': ['tt']},
    }
    client = FakeAddonClient(meta_results={transport: {'id': 'tt1', 'name': 'X', 'type': 'movie'}})
    _wire_data_layer(views, FakeStore(addons=[descriptor], data_dir=str(tmp_path)), client)
    views._fetch_meta('movie', 'tt1')  # default store=True primes the cache

    result = views._fetch_meta('movie', 'tt1', store=False)

    assert result == {'id': 'tt1', 'name': 'X', 'type': 'movie'}
    assert client.meta_calls == [transport]  # second call, with store=False, still hit the cache


def test_fetch_meta_on_miss_is_called_for_a_fresh_fetch(load_views, tmp_path):
    """`on_miss` (mystuff._enrich()'s warm-reopen
    fix) must fire exactly when this call bypasses the cache and reaches
    for a fresh addon answer, regardless of `store`."""
    ctx = load_views()
    views = ctx.views
    transport = 't1'
    descriptor = {
        'transportUrl': transport,
        'manifest': {'id': 'org.a', 'resources': ['meta'], 'types': ['movie'], 'idPrefixes': ['tt']},
    }
    client = FakeAddonClient(meta_results={transport: {'id': 'tt1', 'name': 'Fresh', 'type': 'movie'}})
    _wire_data_layer(views, FakeStore(addons=[descriptor], data_dir=str(tmp_path)), client)
    misses = []

    result = views._fetch_meta('movie', 'tt1', store=False, on_miss=lambda: misses.append(True))

    assert result == {'id': 'tt1', 'name': 'Fresh', 'type': 'movie'}
    assert misses == [True]


def test_fetch_meta_on_miss_is_not_called_for_a_cache_hit(load_views, tmp_path):
    ctx = load_views()
    views = ctx.views
    transport = 't1'
    descriptor = {
        'transportUrl': transport,
        'manifest': {'id': 'org.a', 'resources': ['meta'], 'types': ['movie'], 'idPrefixes': ['tt']},
    }
    client = FakeAddonClient(meta_results={transport: {'id': 'tt1', 'name': 'X', 'type': 'movie'}})
    _wire_data_layer(views, FakeStore(addons=[descriptor], data_dir=str(tmp_path)), client)
    views._fetch_meta('movie', 'tt1')  # primes the cache; on_miss omitted here on purpose
    misses = []

    result = views._fetch_meta('movie', 'tt1', store=False, on_miss=lambda: misses.append(True))

    assert result == {'id': 'tt1', 'name': 'X', 'type': 'movie'}
    assert client.meta_calls == [transport]  # served from cache, addon never called again
    assert misses == []                      # ... so on_miss must not fire either


# ---------------------------------------------------------------------------
# fetch_catalog_pages()
# ---------------------------------------------------------------------------

#: A catalog declaring `skip`, in the modern `extra` array form AIOLists
#: (the addon whose 20-per-page lists motivated paging) ships.
_PAGED_CATALOG = {'id': 'list', 'type': 'movie', 'extra': [{'name': 'skip'}]}


class FakePagingClient:
    """Fake AddonClient serving `pages` (a list of meta lists) as
    successive `skip` windows.

    A page is served by the running offset the addon has handed out, not
    by dividing `skip` by a fixed page size, so pages of DIFFERENT
    lengths are expressible: `pages=[20, 25, 10]` answers `skip=0` with
    the first, `skip=20` with the second, `skip=45` with the third. That
    is exactly the arithmetic `fetch_catalog_pages` is expected to
    perform, so a walker that counted in first-page multiples instead
    would ask for an offset no page starts at and get an empty answer.
    An entry that is an Exception is raised instead.

    `ignores_skip` models a non-compliant addon that answers every
    request with page one, whatever `skip` it was given.
    """

    def __init__(self, pages, ignores_skip=False):
        self._pages = pages
        self._ignores_skip = ignores_skip
        self.calls = []  # [extra, ...] exactly as fetch_catalog_pages passed it

    def catalog(self, transport, rtype, cid, extra=None):
        self.calls.append(extra)
        skip = 0
        for name, value in extra or []:
            if name == 'skip':
                skip = int(value)
        first = self._pages[0] if self._pages else []
        if isinstance(first, Exception):
            raise first
        if self._ignores_skip:
            page = first
        else:
            page = []
            offset = 0
            for candidate in self._pages:
                if isinstance(candidate, Exception):
                    if offset == skip:
                        raise candidate
                    break
                if offset == skip:
                    page = candidate
                    break
                offset += len(candidate)
        if isinstance(page, Exception):
            raise page
        return page


def _metas(start, count):
    return [{'id': 'tt%d' % n, 'name': 'Movie %d' % n} for n in range(start, start + count)]


def test_fetch_catalog_pages_follows_skip_past_the_first_page(load_views):
    """The bug this fixes: an addon serving 20 metas per page (AIOLists)
    showed only its first 20 titles, because the catalog was fetched with
    a single un-skipped call."""
    ctx = load_views()
    views = ctx.views
    client = FakePagingClient([_metas(0, 20), _metas(20, 20), _metas(40, 5)])
    _wire_data_layer(views, FakeStore(), client)

    metas = views.fetch_catalog_pages('t1', 'movie', 'list', catalog=_PAGED_CATALOG)

    assert len(metas) == 45
    assert [m['id'] for m in metas] == ['tt%d' % n for n in range(45)]
    assert client.calls == [None, [('skip', 20)], [('skip', 40)]]


def test_fetch_catalog_pages_stops_on_a_short_page(load_views):
    """A page shorter than the first is the last one - no request is made
    past it, even though the addon would answer an empty page."""
    ctx = load_views()
    views = ctx.views
    client = FakePagingClient([_metas(0, 20), _metas(20, 3)])
    _wire_data_layer(views, FakeStore(), client)

    metas = views.fetch_catalog_pages('t1', 'movie', 'list', catalog=_PAGED_CATALOG)

    assert len(metas) == 23
    assert len(client.calls) == 2


def test_fetch_catalog_pages_counts_skip_from_what_was_actually_served(load_views):
    """A page LONGER than the first: `skip` must be the running served
    count, not a first-page multiple. Counting in multiples of 20 would
    ask for skip=40 after a 25-long second page, skipping five titles the
    addon served and never re-serves."""
    ctx = load_views()
    views = ctx.views
    client = FakePagingClient([_metas(0, 20), _metas(20, 25), _metas(45, 10)])
    _wire_data_layer(views, FakeStore(), client)

    metas = views.fetch_catalog_pages('t1', 'movie', 'list', catalog=_PAGED_CATALOG)

    assert [m['id'] for m in metas] == ['tt%d' % n for n in range(55)]
    assert client.calls == [None, [('skip', 20)], [('skip', 45)]]


def test_fetch_catalog_pages_stops_on_a_short_page_even_after_a_longer_one(load_views):
    """Oscillating page sizes [20, 5, 20]: the 5-long page is shorter
    than the first, so paging ends there and the third page is never
    requested - the short-page rule is judged against the first page's
    size, not the previous page's."""
    ctx = load_views()
    views = ctx.views
    client = FakePagingClient([_metas(0, 20), _metas(20, 5), _metas(25, 20)])
    _wire_data_layer(views, FakeStore(), client)

    metas = views.fetch_catalog_pages('t1', 'movie', 'list', catalog=_PAGED_CATALOG)

    assert len(metas) == 25
    assert client.calls == [None, [('skip', 20)]]


def test_fetch_catalog_pages_keeps_an_id_less_meta_repeated_across_pages(load_views):
    """An id-less meta can't be compared against `seen`, so it is kept
    every time it appears: a cosmetic duplicate beats silently dropping a
    title the addon served. Pinning the behaviour, which the dedupe
    otherwise leaves unspecified."""
    ctx = load_views()
    views = ctx.views
    id_less = {'name': 'No Id'}
    client = FakePagingClient([
        _metas(0, 19) + [dict(id_less)],
        _metas(20, 19) + [dict(id_less)],
        _metas(40, 2),
    ])
    _wire_data_layer(views, FakeStore(), client)

    metas = views.fetch_catalog_pages('t1', 'movie', 'list', catalog=_PAGED_CATALOG)

    assert len(metas) == 42  # 19 + 19 + 2 ids, plus the id-less meta twice
    assert [m for m in metas if m.get('id') is None] == [id_less, id_less]


def test_fetch_catalog_pages_stops_on_an_empty_page(load_views):
    ctx = load_views()
    views = ctx.views
    client = FakePagingClient([_metas(0, 20), _metas(20, 20), []])
    _wire_data_layer(views, FakeStore(), client)

    metas = views.fetch_catalog_pages('t1', 'movie', 'list', catalog=_PAGED_CATALOG)

    assert len(metas) == 40
    assert len(client.calls) == 3


def test_fetch_catalog_pages_stops_when_the_addon_ignores_skip(load_views):
    """An addon that re-serves page one for every `skip` must terminate
    paging (nothing new arrived), not loop to the page cap."""
    ctx = load_views()
    views = ctx.views
    client = FakePagingClient([_metas(0, 20)], ignores_skip=True)
    _wire_data_layer(views, FakeStore(), client)

    metas = views.fetch_catalog_pages('t1', 'movie', 'list', catalog=_PAGED_CATALOG)

    assert len(metas) == 20
    assert len(client.calls) == 2  # the first page, then one probe that added nothing


def test_fetch_catalog_pages_drops_ids_repeated_across_pages(load_views):
    """A shifting window can re-serve a title on the next page; the
    coverflow must never be handed the same id twice."""
    ctx = load_views()
    views = ctx.views
    overlapping = _metas(15, 20)  # tt15-tt19 already came back on page one
    client = FakePagingClient([_metas(0, 20), overlapping, _metas(35, 2)])
    _wire_data_layer(views, FakeStore(), client)

    metas = views.fetch_catalog_pages('t1', 'movie', 'list', catalog=_PAGED_CATALOG)

    ids = [m['id'] for m in metas]
    assert len(ids) == len(set(ids))
    assert len(ids) == 37


def test_fetch_catalog_pages_honours_the_page_cap(load_views):
    ctx = load_views()
    views = ctx.views
    pages = [_metas(index * 20, 20) for index in range(views._MAX_CATALOG_PAGES + 5)]
    client = FakePagingClient(pages)
    _wire_data_layer(views, FakeStore(), client)

    metas = views.fetch_catalog_pages('t1', 'movie', 'list', catalog=_PAGED_CATALOG)

    assert len(client.calls) == views._MAX_CATALOG_PAGES
    assert len(metas) == views._MAX_CATALOG_PAGES * 20


def test_fetch_catalog_pages_does_not_page_a_catalog_without_skip(load_views):
    """No `skip` declaration means one request, exactly as before - a
    catalog that doesn't page must not be probed for a second page."""
    ctx = load_views()
    views = ctx.views
    client = FakePagingClient([_metas(0, 20), _metas(20, 20)])
    _wire_data_layer(views, FakeStore(), client)

    metas = views.fetch_catalog_pages(
        't1', 'movie', 'list', catalog={'id': 'list', 'type': 'movie', 'extra': [{'name': 'genre'}]},
    )

    assert len(metas) == 20
    assert client.calls == [None]


def test_fetch_catalog_pages_does_not_page_without_a_catalog(load_views):
    """The discover-link path has no catalog object to inspect - it must
    fall back to the single-page behaviour rather than assume paging."""
    ctx = load_views()
    views = ctx.views
    client = FakePagingClient([_metas(0, 20), _metas(20, 20)])
    _wire_data_layer(views, FakeStore(), client)

    assert len(views.fetch_catalog_pages('t1', 'movie', 'list')) == 20
    assert client.calls == [None]


def test_fetch_catalog_pages_recognises_legacy_extra_supported_skip(load_views):
    """`extraSupported: ["skip"]` is the legacy encoding of the same
    declaration and must page identically."""
    ctx = load_views()
    views = ctx.views
    client = FakePagingClient([_metas(0, 20), _metas(20, 4)])
    _wire_data_layer(views, FakeStore(), client)

    metas = views.fetch_catalog_pages(
        't1', 'movie', 'list', catalog={'id': 'list', 'type': 'movie', 'extraSupported': ['skip']},
    )

    assert len(metas) == 24


def test_fetch_catalog_pages_preserves_caller_extra_on_every_page(load_views):
    """A chosen genre must stay applied as paging continues, or page two
    onwards would silently come from the unfiltered catalog."""
    ctx = load_views()
    views = ctx.views
    client = FakePagingClient([_metas(0, 20), _metas(20, 2)])
    _wire_data_layer(views, FakeStore(), client)

    views.fetch_catalog_pages(
        't1', 'movie', 'list', extra=[('genre', 'Drama')], catalog=_PAGED_CATALOG,
    )

    assert client.calls == [[('genre', 'Drama')], [('genre', 'Drama'), ('skip', 20)]]


def test_fetch_catalog_pages_leaves_a_caller_supplied_skip_alone(load_views):
    ctx = load_views()
    views = ctx.views
    client = FakePagingClient([_metas(0, 20), _metas(20, 20)])
    _wire_data_layer(views, FakeStore(), client)

    metas = views.fetch_catalog_pages(
        't1', 'movie', 'list', extra=[('skip', 20)], catalog=_PAGED_CATALOG,
    )

    assert len(metas) == 20
    assert client.calls == [[('skip', 20)]]


def test_fetch_catalog_pages_propagates_a_first_page_error(load_views):
    ctx = load_views()
    views = ctx.views
    client = FakePagingClient([AddonError('boom')])
    _wire_data_layer(views, FakeStore(), client)

    with pytest.raises(AddonError):
        views.fetch_catalog_pages('t1', 'movie', 'list', catalog=_PAGED_CATALOG)


def test_fetch_catalog_pages_keeps_earlier_pages_when_a_later_one_fails(load_views):
    """A partial catalog the user can already browse beats an error
    dialog over it."""
    ctx = load_views()
    views = ctx.views
    client = FakePagingClient([_metas(0, 20), AddonError('boom')])
    _wire_data_layer(views, FakeStore(), client)

    metas = views.fetch_catalog_pages('t1', 'movie', 'list', catalog=_PAGED_CATALOG)

    assert len(metas) == 20
    assert any('boom' not in msg and 'AddonError' in msg for msg, _level in ctx.env.log_calls)


def test_fetch_catalog_pages_returns_first_page_result_when_empty(load_views):
    ctx = load_views()
    views = ctx.views
    client = FakePagingClient([[]])
    _wire_data_layer(views, FakeStore(), client)

    assert views.fetch_catalog_pages('t1', 'movie', 'list', catalog=_PAGED_CATALOG) == []
    assert client.calls == [None]


def test_iter_catalog_pages_fetches_nothing_until_a_page_is_consumed(load_views):
    """The generator must be lazy end to end: building it makes no
    request, and each `next()` costs exactly one. This is what lets the
    coverflow open on page one and fetch the rest afterwards."""
    ctx = load_views()
    views = ctx.views
    client = FakePagingClient([_metas(0, 20), _metas(20, 20), _metas(40, 1)])
    _wire_data_layer(views, FakeStore(), client)

    pages = views.iter_catalog_pages('t1', 'movie', 'list', catalog=_PAGED_CATALOG)

    assert client.calls == []  # constructing the generator fetched nothing
    next(pages)
    assert client.calls == [None]  # first page only
    next(pages)
    assert client.calls == [None, [('skip', 20)]]


def test_iter_catalog_pages_stops_fetching_when_the_consumer_stops(load_views):
    """A coverflow closed mid-walk abandons the generator; no further
    page may be requested once nobody is consuming it."""
    ctx = load_views()
    views = ctx.views
    client = FakePagingClient([_metas(n * 20, 20) for n in range(5)])
    _wire_data_layer(views, FakeStore(), client)

    pages = views.iter_catalog_pages('t1', 'movie', 'list', catalog=_PAGED_CATALOG)
    next(pages)
    next(pages)
    pages.close()

    assert client.calls == [None, [('skip', 20)]]


def test_fetch_catalog_pages_keeps_metas_without_an_id(load_views):
    """A meta with no `id` can't be de-duplicated, but must still reach
    the coverflow rather than being dropped by the dedupe."""
    ctx = load_views()
    views = ctx.views
    client = FakePagingClient([_metas(0, 19) + [{'name': 'No Id'}], _metas(20, 1)])
    _wire_data_layer(views, FakeStore(), client)

    metas = views.fetch_catalog_pages('t1', 'movie', 'list', catalog=_PAGED_CATALOG)

    assert len(metas) == 21
    assert {'name': 'No Id'} in metas


# ---------------------------------------------------------------------------
# login() / logout()
# ---------------------------------------------------------------------------


def test_login_cancelled_email_prompt_is_a_noop(load_views):
    ctx = load_views()  # no dialog_inputs -> first input() call returns ''
    views = ctx.views
    store = FakeStore()
    _wire_data_layer(views, store, FakeAddonClient())

    views.login()

    assert ctx.env.dialog_input_prompts == ['STR30024']
    assert store.auth_set_calls == []
    assert 'Container.Refresh' not in ctx.env.executed_builtins


def test_login_cancelled_password_prompt_is_a_noop(load_views):
    ctx = load_views(dialog_inputs=['me@example.com'])
    views = ctx.views
    store = FakeStore()
    _wire_data_layer(views, store, FakeAddonClient())

    views.login()

    assert ctx.env.dialog_input_prompts == ['STR30024', 'STR30025']
    assert store.auth_set_calls == []
    assert 'Container.Refresh' not in ctx.env.executed_builtins


def test_login_api_error_notifies_failure_without_storing_auth(load_views):
    ctx = load_views(dialog_inputs=['me@example.com', 'hunter2'])
    views = ctx.views
    store = FakeStore()
    _wire_data_layer(views, store, FakeAddonClient())
    _wire_api(views, FakeStremioAPI(login_error=ApiError('invalid credentials')))

    views.login()

    assert store.auth_set_calls == []
    assert ctx.env.notifications[-1][1] == 'STR30023'
    assert 'Container.Refresh' not in ctx.env.executed_builtins


def test_login_success_merges_all_local_addons_with_remote_collection_and_pushes_back(load_views):
    ctx = load_views(dialog_inputs=['me@example.com', 'hunter2'], localized={30022: 'Logged in as %s'})
    views = ctx.views
    protected = {'transportUrl': 'https://official.example/manifest.json', 'flags': {'protected': True}}
    community = {'transportUrl': 'https://community.example/manifest.json', 'flags': {}}
    store = FakeStore(addons=[protected, community])
    remote_duplicate = {'transportUrl': protected['transportUrl'], 'manifest': {'id': 'duplicate-of-protected'}}
    remote_new = {'transportUrl': 'https://remote.example/manifest.json', 'manifest': {'id': 'org.remote'}}
    login_result = {'authKey': 'abc123', 'user': {'email': 'me@example.com'}}
    api = FakeStremioAPI(login_result=login_result, addon_collection_result=[remote_duplicate, remote_new])
    _wire_data_layer(views, store, FakeAddonClient())
    _wire_api(views, api)

    views.login()

    assert store.auth_set_calls == [login_result]
    # union, not filter: the local community addon must survive login (it
    # previously got silently dropped - only protected+remote survived).
    assert store.addons_set_calls == [[protected, community, remote_new]]
    # and the merged (now-complete) list is pushed straight back up, so an
    # addon installed before ever logging in reaches the account immediately.
    assert api.addon_collection_set_calls == [(login_result['authKey'], [protected, community, remote_new])]
    assert ctx.env.notifications[-1][1] == 'Logged in as me@example.com'
    assert 'Container.Refresh' in ctx.env.executed_builtins


def test_login_merge_discards_unsafe_synced_descriptors_but_keeps_safe_ones(load_views):
    """Remote account addon sync must discard descriptors whose
    transportUrl fails validate_transport_url() (credentials, plaintext
    HTTP to a public host, non-HTTP(S) scheme, ...) - a hijacked/tampered
    remote account must never be able to smuggle an unsafe transport into
    the local store via login. Safe remote descriptors, and the existing
    local union/protected-preserving behavior, survive unaffected. A safe
    descriptor whose URL needs normalization (scheme/host casing) is
    persisted/pushed with ONLY the normalized transportUrl, and the
    original input descriptor is left untouched (no shared-mutation with
    the API response)."""
    ctx = load_views(dialog_inputs=['me@example.com', 'hunter2'], localized={30022: 'Logged in as %s'})
    views = ctx.views
    protected = {'transportUrl': 'https://official.example/manifest.json', 'flags': {'protected': True}}
    store = FakeStore(addons=[protected])
    remote_credentialed = {
        'transportUrl': 'https://attacker:hunter2@evil.example/manifest.json',
        'manifest': {'id': 'org.evil-creds'},
    }
    remote_plaintext_public = {
        'transportUrl': 'http://public.example/manifest.json',
        'manifest': {'id': 'org.evil-http'},
    }
    remote_bad_scheme = {
        'transportUrl': 'javascript:alert(1)',
        'manifest': {'id': 'org.evil-scheme'},
    }
    remote_missing_url = {'transportUrl': None, 'manifest': {'id': 'org.evil-missing'}}
    remote_safe = {'transportUrl': 'https://remote.example/manifest.json', 'manifest': {'id': 'org.remote'}}
    remote_needs_normalization = {
        'transportUrl': 'HTTPS://Remote-Two.Example/manifest.json',
        'manifest': {'id': 'org.remote-two'},
    }
    remote_needs_normalization_snapshot = dict(remote_needs_normalization)
    normalized_remote_two = {
        'transportUrl': 'https://remote-two.example/manifest.json',
        'manifest': {'id': 'org.remote-two'},
    }
    login_result = {'authKey': 'abc123', 'user': {'email': 'me@example.com'}}
    api = FakeStremioAPI(login_result=login_result, addon_collection_result=[
        remote_credentialed, remote_plaintext_public, remote_bad_scheme, remote_missing_url, remote_safe,
        remote_needs_normalization,
    ])
    _wire_data_layer(views, store, FakeAddonClient())
    _wire_api(views, api)

    views.login()

    # Only the protected local addon and the safe remote descriptors
    # survive the merge - every unsafe remote descriptor is dropped, and
    # the normalization-requiring one is persisted/pushed with only its
    # normalized transportUrl.
    assert store.addons_set_calls == [[protected, remote_safe, normalized_remote_two]]
    assert api.addon_collection_set_calls == [
        (login_result['authKey'], [protected, remote_safe, normalized_remote_two]),
    ]
    # The API response object itself was never mutated in place.
    assert remote_needs_normalization == remote_needs_normalization_snapshot
    all_messages = ' '.join(msg for msg, _level in ctx.env.log_calls)
    # Only safe identity/origin is logged for discarded descriptors -
    # never the raw credentialed URL.
    assert 'hunter2' not in all_messages
    assert 'evil.example' in all_messages
    assert 'org.evil-creds' in all_messages
    assert 'public.example' in all_messages
    assert 'org.evil-http' in all_messages


def test_login_success_keeps_existing_addons_when_remote_sync_fails(load_views):
    ctx = load_views(dialog_inputs=['me@example.com', 'hunter2'], localized={30022: 'Logged in as %s'})
    views = ctx.views
    store = FakeStore()
    login_result = {'authKey': 'abc123', 'user': {'email': 'me@example.com'}}
    api = FakeStremioAPI(login_result=login_result, addon_collection_error=ApiError('sync down'))
    _wire_data_layer(views, store, FakeAddonClient())
    _wire_api(views, api)

    views.login()

    assert store.auth_set_calls == [login_result]
    assert store.addons_set_calls == []
    assert 'Container.Refresh' in ctx.env.executed_builtins


def test_sync_addons_now_success_notifies_synced(load_views):
    ctx = load_views()
    views = ctx.views
    auth = {'authKey': 'abc123'}
    store = FakeStore(addons=[{'transportUrl': 't1', 'flags': {}}], auth=auth)
    api = FakeStremioAPI()
    _wire_data_layer(views, store, FakeAddonClient())
    _wire_api(views, api)

    views.sync_addons_now()

    assert api.addon_collection_set_calls == [(auth['authKey'], store.get_addons())]
    assert ctx.env.notifications[-1][1] == 'STR30034'


def test_sync_addons_now_failure_notifies_failed(load_views):
    ctx = load_views()
    views = ctx.views
    auth = {'authKey': 'abc123'}
    store = FakeStore(addons=[{'transportUrl': 't1', 'flags': {}}], auth=auth)
    api = FakeStremioAPI(addon_collection_set_error=ApiError('sync down'))
    _wire_data_layer(views, store, FakeAddonClient())
    _wire_api(views, api)

    views.sync_addons_now()

    assert len(api.addon_collection_set_calls) == 1
    assert ctx.env.notifications[-1][1] == 'STR30035'


def test_sync_addons_now_not_logged_in_notifies_login_prompt(load_views):
    ctx = load_views()
    views = ctx.views
    store = FakeStore(auth=None)
    api = FakeStremioAPI()
    _wire_data_layer(views, store, FakeAddonClient())
    _wire_api(views, api)

    views.sync_addons_now()

    assert api.addon_collection_set_calls == []
    assert ctx.env.notifications[-1][1] == 'STR30020'


def test_refresh_addon_manifests_updates_changed_manifest_via_update_addons(load_views):
    """A remote manifest that differs from the cached one is persisted
    through Store.update_addons() (CAS-safe), never a raw set_addons()."""
    ctx = load_views()
    views = ctx.views
    transport = 'https://a.example/manifest.json'
    old_manifest = {'id': 'org.a', 'version': '1.0.0', 'catalogs': []}
    new_manifest = {'id': 'org.a', 'version': '2.0.0', 'catalogs': [{'type': 'movie', 'id': 'top'}]}
    descriptor = {'transportUrl': transport, 'manifest': old_manifest, 'flags': {}}
    store = FakeStore(addons=[descriptor])
    client = FakeAddonClient(manifest_results={transport: new_manifest})

    views._refresh_addon_manifests(store, client)

    assert store.get_addons()[0]['manifest'] == new_manifest
    assert len(store.update_addons_calls) == 1


def test_refresh_addon_manifests_keeps_cached_manifest_on_fetch_failure_and_continues(load_views):
    """One addon's manifest fetch raising AddonError keeps its previous
    cached manifest untouched and does not prevent the other installed
    addons from being refreshed - same best-effort philosophy as
    `_sync_addons_if_logged_in`. A descriptor missing `transportUrl`
    entirely (malformed data) is skipped rather than blowing up the
    whole refresh."""
    ctx = load_views()
    views = ctx.views
    failing_transport = 'https://broken.example/manifest.json'
    ok_transport = 'https://ok.example/manifest.json'
    failing_manifest = {'id': 'org.broken', 'version': '1.0.0'}
    old_ok_manifest = {'id': 'org.ok', 'version': '1.0.0'}
    new_ok_manifest = {'id': 'org.ok', 'version': '1.1.0'}
    failing_descriptor = {'transportUrl': failing_transport, 'manifest': failing_manifest, 'flags': {}}
    ok_descriptor = {'transportUrl': ok_transport, 'manifest': old_ok_manifest, 'flags': {}}
    no_transport_descriptor = {'manifest': {'id': 'org.malformed'}, 'flags': {}}
    store = FakeStore(addons=[failing_descriptor, ok_descriptor, no_transport_descriptor])
    client = FakeAddonClient(manifest_results={
        failing_transport: AddonError('upstream timeout'),
        ok_transport: new_ok_manifest,
    })

    views._refresh_addon_manifests(store, client)

    addons_by_transport = {a.get('transportUrl'): a for a in store.get_addons()}
    assert addons_by_transport[failing_transport]['manifest'] == failing_manifest
    assert addons_by_transport[ok_transport]['manifest'] == new_ok_manifest
    assert addons_by_transport[None]['manifest'] == {'id': 'org.malformed'}
    assert set(client.manifest_calls) == {failing_transport, ok_transport}
    assert len(store.update_addons_calls) == 1


def test_sync_addons_now_refreshes_manifests_before_pushing_to_account(load_views):
    """sync_addons_now() must refresh installed addons' cached manifests
    before pushing the (now up-to-date) local collection to the account -
    otherwise a freshly-changed remote manifest wouldn't make it into the
    same sync run that triggered the refresh."""
    ctx = load_views()
    views = ctx.views
    auth = {'authKey': 'abc123'}
    transport = 'https://a.example/manifest.json'
    descriptor = {'transportUrl': transport, 'manifest': {'id': 'org.a', 'version': '1.0.0'}, 'flags': {}}
    store = FakeStore(addons=[descriptor], auth=auth)
    new_manifest = {'id': 'org.a', 'version': '2.0.0'}
    calls = []

    class TrackingClient(FakeAddonClient):
        def manifest(self, url):
            calls.append('manifest')
            return super().manifest(url)

    class TrackingAPI(FakeStremioAPI):
        def addon_collection_set(self, auth_key, addons):
            calls.append('push')
            return super().addon_collection_set(auth_key, addons)

    client = TrackingClient(manifest_results={transport: new_manifest})
    api = TrackingAPI()
    _wire_data_layer(views, store, client)
    _wire_api(views, api)

    views.sync_addons_now()

    assert calls == ['manifest', 'push']
    # the push must see the freshly-refreshed manifest, not the stale one
    assert api.addon_collection_set_calls[0][1][0]['manifest'] == new_manifest

def _stub_confirm(monkeypatch, ctx, answer, capture=None):
    """Patches `lib.ui.dialogs.confirm` directly (already exhaustively
    covered by tests/test_dialogs.py) rather than driving a real
    `doModal()` - this suite only needs to prove `logout()` passes the
    right heading/body/labels and reacts correctly to the result."""
    def _confirm(heading, body, yeslabel, nolabel):
        if capture is not None:
            capture.append((heading, body, yeslabel, nolabel))
        return answer

    monkeypatch.setattr(ctx.dialogs, 'confirm', _confirm)


def test_logout_without_auth_is_a_noop(load_views):
    ctx = load_views()
    views = ctx.views
    store = FakeStore(auth=None)
    _wire_data_layer(views, store, FakeAddonClient())

    views.logout()

    assert store.auth_set_calls == []
    assert 'Container.Refresh' not in ctx.env.executed_builtins


def test_logout_declined_confirmation_is_a_noop(load_views, monkeypatch):
    ctx = load_views()
    _stub_confirm(monkeypatch, ctx, False)
    views = ctx.views
    store = FakeStore(auth={'authKey': 'abc'})
    _wire_data_layer(views, store, FakeAddonClient())

    views.logout()

    assert store.auth_set_calls == []
    assert 'Container.Refresh' not in ctx.env.executed_builtins


def test_logout_clears_auth_even_when_api_call_fails(load_views, monkeypatch):
    ctx = load_views()
    captured = []
    _stub_confirm(monkeypatch, ctx, True, capture=captured)
    views = ctx.views
    store = FakeStore(auth={'authKey': 'abc'})
    _wire_data_layer(views, store, FakeAddonClient())
    _wire_api(views, FakeStremioAPI(logout_error=ApiError('network down')))

    views.logout()

    assert store.auth_set_calls == [None]
    assert captured == [('STR30021', 'STR30021', 'Yes', 'No')]
    assert any('network down' in msg for msg, _level in ctx.env.log_calls)
    assert 'Container.Refresh' in ctx.env.executed_builtins


# ---------------------------------------------------------------------------
# _sync_addons_if_logged_in() - auth-error handling
# ---------------------------------------------------------------------------


def test_sync_addons_if_logged_in_auth_error_clears_stale_auth(load_views):
    """Same 401/403 detection as library(), but this path also runs from
    background install/remove/login flows - it clears the dead authKey (so
    the next screen shows "not logged in") without popping a dedicated
    re-login prompt of its own; the existing generic sync-failed
    notification already covers surfacing the failure."""
    ctx = load_views()
    views = ctx.views
    auth = {'authKey': 'abc123'}
    store = FakeStore(addons=[{'transportUrl': 't1', 'flags': {}}], auth=auth)
    api = FakeStremioAPI(addon_collection_set_error=ApiError('forbidden', status_code=403))
    _wire_data_layer(views, store, FakeAddonClient())
    _wire_api(views, api)

    result = views._sync_addons_if_logged_in(store)

    assert result is False
    assert store.auth_set_calls == [None]
    assert ctx.env.notifications[-1][1] == 'STR30035'


def test_sync_addons_if_logged_in_generic_error_does_not_clear_auth(load_views):
    """A transient/network failure must not log the user out."""
    ctx = load_views()
    views = ctx.views
    auth = {'authKey': 'abc123'}
    store = FakeStore(addons=[{'transportUrl': 't1', 'flags': {}}], auth=auth)
    api = FakeStremioAPI(addon_collection_set_error=ApiError('sync down'))
    _wire_data_layer(views, store, FakeAddonClient())
    _wire_api(views, api)

    result = views._sync_addons_if_logged_in(store)

    assert result is False
    assert store.auth_set_calls == []
    assert ctx.env.notifications[-1][1] == 'STR30035'


# ---------------------------------------------------------------------------
# _safe_listing() decorator
# ---------------------------------------------------------------------------


def test_safe_listing_decorator_catches_exception_notifies_and_fails(load_views):
    ctx = load_views()
    views = ctx.views

    def _boom(*args, **kwargs):
        raise RuntimeError('disk on fire')

    views._url_for = _boom

    views.home()

    assert ctx.env.notifications[-1][1] == 'disk on fire'
    assert ctx.env.end_of_directory[-1] == {
        'handle': -1, 'succeeded': False, 'updateListing': False, 'cacheToDisc': True,
    }
    assert ctx.env.log_calls[-1][1] == 3  # xbmc.LOGERROR



# ---------------------------------------------------------------------------
# Shared process-wide Store/AddonClient (lib.ui.dependencies)
# ---------------------------------------------------------------------------


def test_player_and_views_share_the_same_store_and_client():
    """`lib.ui.player` and `lib.ui.views` both call
    `lib.ui.dependencies.get_store()`/`get_client()` - this proves each
    returns the exact same object to both consumers, and constructs it
    exactly once, so there is only one on-disk Store and one AddonClient
    per process rather than a duplicate pair per module."""
    reload_names = ('lib.ui.compat', 'lib.ui.dependencies', 'lib.ui.player', 'lib.ui.views')
    with install_kodi_stubs(reload=reload_names) as ctx:
        class _CountingStore:
            instances = 0

            def __init__(self, *args, **kwargs):
                type(self).instances += 1

        class _CountingClient:
            instances = 0

            def __init__(self, *args, **kwargs):
                type(self).instances += 1

        ctx.dependencies.Store = _CountingStore
        ctx.dependencies.AddonClient = _CountingClient

        assert ctx.player.get_store() is ctx.views.get_store()
        assert ctx.player.get_client() is ctx.views.get_client()
        assert isinstance(ctx.player.get_store(), _CountingStore)
        assert isinstance(ctx.player.get_client(), _CountingClient)
        assert _CountingStore.instances == 1
        assert _CountingClient.instances == 1


def test_get_api_resolves_and_caches_a_single_stremio_api_instance():
    """`lib.ui.dependencies.get_api()` mirrors `get_store()`/
    `get_client()`'s own lazy singleton shape - `lib.ui.detailwindow`'s
    library context menu (Add/Remove library, Mark (un)watched) is the
    first caller, added alongside `lib.stremio.api.StremioAPI.
    get_library_item()`/`put_library_item()`."""
    reload_names = ('lib.ui.compat', 'lib.ui.dependencies')
    with install_kodi_stubs(reload=reload_names) as ctx:
        class _CountingApi:
            instances = 0

            def __init__(self, *args, **kwargs):
                type(self).instances += 1

        ctx.dependencies.StremioAPI = _CountingApi

        assert ctx.dependencies.get_api() is ctx.dependencies.get_api()
        assert isinstance(ctx.dependencies.get_api(), _CountingApi)
        assert _CountingApi.instances == 1
