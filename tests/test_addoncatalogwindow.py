"""Tests for lib.ui.addoncatalogwindow: AddonCatalogWindow, Rivulet's addon
addon-catalog browser (see the module docstring), exercised against the
shared fake xbmc/xbmcgui stubs in tests/kodistubs (no real Kodi runtime,
no network).

Same shape as tests/test_addonswindow.py: AddonCatalogWindow.onInit()/
onClick() are called directly (never through a real modal event loop),
`get_store()`/`get_client()` are monkeypatched by assignment on the
reloaded module (both bound at lib.ui.addoncatalogwindow module scope, exactly
like lib.ui.addonswindow), and AddonCatalogWindow.xml's actual skin rendering
is Kodi-skin-engine-only and is NOT, and cannot be, exercised here - the
fake WindowXML only validates that every control id Python touches
(AddonCatalogWindow.LIST) exists in the real skin file.

The fake `xbmcaddon.Addon.getLocalizedString()` returns a plain
`'STR<id>'` placeholder unless a test overrides it via
`load_addoncatalogwindow(localized={id: '...%s...'})` - required whenever
production code applies `%` to an `L(id)` result (see
tests/test_views.py's own `localized=` usage for `L(30022) % email`).
`_search_row()` always applies `%` to `#30348` once `self.entries` is
non-empty, so `load_addoncatalogwindow()` below merges a base override
for it into every call - a test only supplies its OWN extra ids.

Paging/search tests follow tests/test_infowindow.py's own convention for
its `_paging_worker`/`_apply_pending_pages()` pair: `_spawn_paging()` is
monkeypatched to capture the pages rather than truly start a thread, and
`_paging_worker()` is then called directly (synchronously, on the test
thread) - no real threading, no real sleep (the fake `xbmc.Monitor.
waitForAbort()` returns instantly)."""
import contextlib
import sys

import pytest
import requests

from lib.stremio import addoncatalogs
from tests.conftest import FakeResponse, FakeSession
from tests.kodistubs import install_kodi_stubs

_RELOAD_MODULE_NAMES = (
    'lib.ui.compat', 'lib.ui.dependencies', 'lib.ui.uicommon', 'lib.ui.router', 'lib.ui.views',
    'lib.ui.dialogs', 'lib.ui.addonswindow', 'lib.ui.addoncatalogwindow',
)

#: `_search_row()` formats this unconditionally once there is at least
#: one entry - see the module docstring above.
_BASE_LOCALIZED = {30348: 'Showing %d of %d'}


@pytest.fixture(autouse=True)
def _clear_catalog_cache():
    """`addoncatalogs._catalog_cache` is a module-level dict shared by every
    test in this process (see its own docstring for why it cannot be a
    per-call fixture-scoped object) - without resetting it, whichever test
    happens to run first for a given (transport_url, type, id) key decides
    every later test's fetched entries regardless of that test's OWN queued
    FakeSession response, since pytest-randomly reorders tests every run.
    Mirrors tests/test_addoncatalogs.py's own fixture of the same name;
    duplicated rather than imported/shared because that module imports
    addoncatalogs directly with no kodi stubs, while every test here
    reloads a fresh addoncatalogwindow module instead."""
    addoncatalogs._catalog_cache.clear()
    yield
    addoncatalogs._catalog_cache.clear()


def _source_addon():
    """An installed addon (NOT Cinemeta) that publishes one
    addon_catalog - proves AddonCatalogWindow has no Cinemeta special-casing,
    same as test_addoncatalogs.py does for iter_addon_catalogs()."""
    return {
        'transportUrl': 'https://source.example/manifest.json',
        'manifest': {
            'id': 'org.source',
            'name': 'Source Addon',
            'version': '1.0.0',
            'addonCatalogs': [{'type': 'movie', 'id': 'community', 'name': 'Community'}],
        },
        'flags': {},
    }


def _already_installed_addon():
    return {
        'transportUrl': 'https://already.example/manifest.json',
        'manifest': {'id': 'already', 'version': '1.0.0'},
        'flags': {},
    }


CATALOG_ENVELOPE = {
    'addons': [
        {'transportUrl': 'https://new.example/manifest.json', 'transportName': 'New Addon',
         'manifest': {'id': 'new', 'name': 'New Addon', 'version': '1.0.0', 'description': 'A new addon'}},
        {'transportUrl': 'https://cfg.example/manifest.json', 'transportName': 'Needs Config',
         'manifest': {'id': 'cfg', 'name': 'Needs Config', 'version': '1.0.0',
                      'behaviorHints': {'configurationRequired': True}}},
        {'transportUrl': 'https://already.example/manifest.json', 'transportName': 'Already',
         'manifest': {'id': 'already', 'name': 'Already Installed', 'version': '1.0.0'}},
    ]
}


def _many_addons_envelope(count):
    """`count` distinct, logo-bearing entries - enough (with `count` >
    `AddonCatalogWindow._PAGE_SIZE`) to force pagination."""
    return {
        'addons': [
            {
                'transportUrl': 'https://addon%d.example/manifest.json' % n,
                'transportName': 'Addon %d' % n,
                'manifest': {
                    'id': 'addon%d' % n,
                    'name': 'Addon %d' % n,
                    'version': '1.0.0',
                    'description': 'Description %d' % n,
                    'logo': 'https://addon%d.example/logo.png' % n,
                },
            }
            for n in range(count)
        ]
    }


def _search_envelope():
    """Three entries for filter tests: 'alpha' matches entry 0 by name
    and entry 1 by description only; entry 2 matches neither."""
    return {
        'addons': [
            {'transportUrl': 'https://alpha.example/manifest.json', 'transportName': 'Alpha',
             'manifest': {'id': 'alpha', 'name': 'Alpha Streams', 'version': '1.0.0',
                          'description': 'Great sports catalog'}},
            {'transportUrl': 'https://beta.example/manifest.json', 'transportName': 'Beta',
             'manifest': {'id': 'beta', 'name': 'Beta Films', 'version': '1.0.0',
                          'description': 'Alpha rated documentaries'}},
            {'transportUrl': 'https://gamma.example/manifest.json', 'transportName': 'Gamma',
             'manifest': {'id': 'gamma', 'name': 'Gamma Shows', 'version': '1.0.0',
                          'description': 'Kids cartoons'}},
        ]
    }


class _PerUrlSession:
    """Fake session that raises for one URL prefix and answers everything
    else with a fixed response - mirrors tests/test_addoncatalogs.py's own
    helper of the same name; needed because a single FakeSession cannot
    fail selectively per source URL."""

    def __init__(self, dead_url_prefix, ok_response, exc):
        self._dead_url_prefix = dead_url_prefix
        self._ok_response = ok_response
        self._exc = exc
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append({'method': 'GET', 'url': url, 'kwargs': kwargs})
        if url.startswith(self._dead_url_prefix):
            raise self._exc
        return self._ok_response


def _other_source_addon():
    """A second, healthy installed addon declaring its own single
    addon_catalog under a distinct id/transportUrl from `_source_addon()`
    - proves one dead source never hides another source's entries (see
    `_fetch_entries()`'s docstring)."""
    return {
        'transportUrl': 'https://healthy.example/manifest.json',
        'manifest': {
            'id': 'org.healthy',
            'name': 'Healthy Addon',
            'version': '1.0.0',
            'addonCatalogs': [{'type': 'movie', 'id': 'top'}],
        },
        'flags': {},
    }


#: Cinemeta's own 11 declared (type, id) pairs - 4 under id "official", 7
#: under id "community" - each set including an "all" variant, mirroring
#: tests/test_addoncatalogs.py's `_CINEMETA_ADDON_CATALOGS` fixture. See
#: lib.stremio.addoncatalogs's module docstring for the live 11-request
#: (297-row) to 2-request (102-row) census this collapsing is built from.
_CINEMETA_ADDON_CATALOGS = (
    [{'type': t, 'id': 'official'} for t in ('all', 'movie', 'series', 'channel')]
    + [
        {'type': t, 'id': 'community'}
        for t in ('all', 'movie', 'series', 'channel', 'tv', 'Podcasts', 'other')
    ]
)


def _cinemeta_like_addon():
    """An installed addon declaring Cinemeta's own 11 addonCatalogs pairs
    across 2 ids - proves `AddonCatalogWindow.onInit()` itself, not just
    the lib-level helpers it calls, collapses them to exactly 2 fetches."""
    return {
        'transportUrl': 'https://cinemeta.example/manifest.json',
        'manifest': {
            'id': 'com.linvo.cinemeta',
            'name': 'Cinemeta',
            'version': '1.0.0',
            'addonCatalogs': list(_CINEMETA_ADDON_CATALOGS),
        },
        'flags': {},
    }


class _FakeStore:
    """Fake `lib.store.Store`: tracks `get_addons()`'s backing list plus
    every `install_addon()` call."""

    def __init__(self, addons=None):
        self.addons = list(addons or [])
        self.installed = []

    def get_addons(self):
        return self.addons

    def install_addon(self, transport_url, manifest):
        self.installed.append((transport_url, manifest))
        self.addons.append({'transportUrl': transport_url, 'manifest': manifest, 'flags': {}})

    def get_auth(self):
        return None


class _FakeClient:
    """Fake `lib.stremio.addons.AddonClient`: `.session`/`.timeout` are
    what `fetch_addon_catalog()` reads (see
    `tests/test_addoncatalogs.py`'s own `_FakeClient`); `manifest(url)`
    backs the needs-configuration paste-URL flow."""

    def __init__(self, session=None, manifest_result=None, manifest_error=None):
        self.session = session if session is not None else FakeSession()
        self.timeout = 15
        self.manifest_result = manifest_result
        self.manifest_error = manifest_error
        self.manifest_calls = []

    def manifest(self, url):
        self.manifest_calls.append(url)
        if self.manifest_error is not None:
            raise self.manifest_error
        return self.manifest_result


@pytest.fixture
def load_addoncatalogwindow():
    """Factory fixture: `load_addoncatalogwindow(**kwargs)` installs fresh
    stubs and returns a namespace with `.addoncatalogwindow`, `.dialogs`,
    `.views`, and `.env`. Every call torn down automatically, in reverse
    order, at test end."""
    with contextlib.ExitStack() as stack:
        def _load(**kwargs):
            kwargs['localized'] = {**_BASE_LOCALIZED, **(kwargs.get('localized') or {})}
            return stack.enter_context(install_kodi_stubs(reload=_RELOAD_MODULE_NAMES, **kwargs))

        yield _load


def _make_window(addoncatalogwindow_mod):
    return addoncatalogwindow_mod.AddonCatalogWindow('AddonCatalogWindow.xml', '/addon/path', 'Default', '1080i')


def _wire_store(addoncatalogwindow_mod, store):
    addoncatalogwindow_mod.get_store = lambda: store


def _wire_client(addoncatalogwindow_mod, client):
    addoncatalogwindow_mod.get_client = lambda: client


def _select(win, addoncatalogwindow_mod, index):
    win.getControl(addoncatalogwindow_mod.LIST).selected_index = index


def _select_position(win, addoncatalogwindow_mod, position):
    """Focus the row whose `position` Property equals `position` - robust
    against the sentinel ("search"/"clear") rows shifting a real entry's
    place in the control's own item list."""
    control = win.getControl(addoncatalogwindow_mod.LIST)
    for row_index, item in enumerate(control.items):
        if item.getProperty('position') == position:
            control.selected_index = row_index
            return
    raise AssertionError('no row with position %r' % (position,))


def _capture_spawn_paging(monkeypatch, addoncatalogwindow_mod):
    """Monkeypatch `_spawn_paging()` to record its argument instead of
    starting a real thread - see the module docstring's paging-test
    convention."""
    spawned = []
    monkeypatch.setattr(
        addoncatalogwindow_mod.AddonCatalogWindow, '_spawn_paging',
        lambda self, pages: spawned.append(pages),
    )
    return spawned


# ---------------------------------------------------------------------------
# onInit() - rendering
# ---------------------------------------------------------------------------


def test_oninit_renders_every_catalog_entry_plus_the_search_row(load_addoncatalogwindow):
    ctx = load_addoncatalogwindow()
    store = _FakeStore(addons=[_source_addon(), _already_installed_addon()])
    client = _FakeClient(session=FakeSession(responses=[FakeResponse(CATALOG_ENVELOPE)]))
    _wire_store(ctx.addoncatalogwindow, store)
    _wire_client(ctx.addoncatalogwindow, client)

    win = _make_window(ctx.addoncatalogwindow)
    win.onInit()

    items = win.getControl(ctx.addoncatalogwindow.LIST).items
    assert len(items) == 4  # search row + 3 entries
    assert items[0].getProperty('position') == 'search'
    labels = [item.getLabel() for item in items]
    assert any('New Addon' in label and 'STR30335' not in label for label in labels)
    assert any('Needs Config' in label and 'STR30337' in label for label in labels)
    assert any('Already Installed' in label and 'STR30335' in label for label in labels)
    assert win.getFocusId() == ctx.addoncatalogwindow.LIST


def test_oninit_with_no_addon_catalog_sources_shows_empty_placeholder(load_addoncatalogwindow):
    ctx = load_addoncatalogwindow()
    _wire_store(ctx.addoncatalogwindow, _FakeStore(addons=[]))
    _wire_client(ctx.addoncatalogwindow, _FakeClient())

    win = _make_window(ctx.addoncatalogwindow)
    win.onInit()

    items = win.getControl(ctx.addoncatalogwindow.LIST).items
    assert len(items) == 1
    assert items[0].getLabel() == 'STR30339'
    assert items[0].getProperty('position') == ''


def test_oninit_source_fetch_failure_notifies_and_skips_that_source(load_addoncatalogwindow):
    """One dead source (`fetch_addon_catalogs()` isolates each source's
    own try/except) must notify and skip only itself - the healthy
    source's entries still render, per `_fetch_entries()`'s docstring."""
    ctx = load_addoncatalogwindow(localized={30340: "Could not load %s's addon catalog"})
    session = _PerUrlSession(
        dead_url_prefix='https://source.example',
        ok_response=FakeResponse(CATALOG_ENVELOPE),
        exc=requests.exceptions.ConnectionError('dead'),
    )
    _wire_store(ctx.addoncatalogwindow, _FakeStore(addons=[_source_addon(), _other_source_addon()]))
    _wire_client(ctx.addoncatalogwindow, _FakeClient(session=session))

    win = _make_window(ctx.addoncatalogwindow)
    win.onInit()

    items = win.getControl(ctx.addoncatalogwindow.LIST).items
    entry_rows = [item for item in items if item.getProperty('position').isdigit()]
    assert len(entry_rows) == len(CATALOG_ENVELOPE['addons'])  # the healthy source's rows, unhidden
    assert ctx.env.notifications == [('Rivulet', "Could not load Source Addon's addon catalog", 'info', 4000)]
    xbmc_mod = sys.modules['xbmc']  # installed by the fixture above, not importable at module scope
    warnings = [msg for msg, level in ctx.env.log_calls if level == xbmc_mod.LOGWARNING]
    assert len(warnings) == 1
    assert 'source.example' in warnings[0]


def test_oninit_collapses_cinemetas_eleven_pairs_into_exactly_two_fetches(load_addoncatalogwindow):
    """The 11->2 fan-out reduction `_fetch_entries()`'s docstring measures
    (and tests/test_addoncatalogs.py already proves at the lib level), as
    observed through the window itself: opening it for an addon declaring
    Cinemeta-shaped addonCatalogs must issue exactly 2 HTTP requests, both
    for the broadest ("all") declared type - not 11, one per pair."""
    ctx = load_addoncatalogwindow()
    official_envelope = {'addons': [
        {'transportUrl': 'https://official1.example/manifest.json', 'transportName': 'Official 1',
         'manifest': {'id': 'official1', 'name': 'Official One', 'version': '1.0.0'}},
    ]}
    community_envelope = {'addons': [
        {'transportUrl': 'https://community1.example/manifest.json', 'transportName': 'Community 1',
         'manifest': {'id': 'community1', 'name': 'Community One', 'version': '1.0.0'}},
    ]}
    store = _FakeStore(addons=[_cinemeta_like_addon()])
    client = _FakeClient(session=FakeSession(
        responses=[FakeResponse(official_envelope), FakeResponse(community_envelope)],
    ))
    _wire_store(ctx.addoncatalogwindow, store)
    _wire_client(ctx.addoncatalogwindow, client)

    win = _make_window(ctx.addoncatalogwindow)
    win.onInit()

    assert len(client.session.calls) == 2
    assert {call['url'] for call in client.session.calls} == {
        'https://cinemeta.example/addon_catalog/all/official.json',
        'https://cinemeta.example/addon_catalog/all/community.json',
    }
    labels = [item.getLabel() for item in win.getControl(ctx.addoncatalogwindow.LIST).items]
    assert len(labels) == 3  # search row + one entry per collapsed source
    assert any('Official One' in label for label in labels)
    assert any('Community One' in label for label in labels)


# ---------------------------------------------------------------------------
# Incremental rendering - only the rendered page carries artwork
# ---------------------------------------------------------------------------


def test_render_builds_only_the_first_page_with_artwork_only_on_that_page(load_addoncatalogwindow, monkeypatch):
    ctx = load_addoncatalogwindow()
    page_size = ctx.addoncatalogwindow._PAGE_SIZE
    total = page_size + 5
    store = _FakeStore(addons=[_source_addon()])
    client = _FakeClient(session=FakeSession(responses=[FakeResponse(_many_addons_envelope(total))]))
    _wire_store(ctx.addoncatalogwindow, store)
    _wire_client(ctx.addoncatalogwindow, client)
    spawned = _capture_spawn_paging(monkeypatch, ctx.addoncatalogwindow)

    win = _make_window(ctx.addoncatalogwindow)
    win.onInit()

    control = win.getControl(ctx.addoncatalogwindow.LIST)
    entry_rows = control.items[1:]  # skip the search row
    assert len(entry_rows) == page_size
    assert [item.getProperty('position') for item in entry_rows] == [str(n) for n in range(page_size)]
    assert all(item.art.get('icon') for item in entry_rows)

    # The rest was queued for the background walker, not built yet.
    assert len(spawned) == 1
    flattened_remaining = [index for page in spawned[0] for index in page]
    assert flattened_remaining == list(range(page_size, total))


def test_appending_a_page_neither_resets_the_control_nor_loses_focus(load_addoncatalogwindow, monkeypatch):
    ctx = load_addoncatalogwindow()
    page_size = ctx.addoncatalogwindow._PAGE_SIZE
    total = page_size + 5
    store = _FakeStore(addons=[_source_addon()])
    client = _FakeClient(session=FakeSession(responses=[FakeResponse(_many_addons_envelope(total))]))
    _wire_store(ctx.addoncatalogwindow, store)
    _wire_client(ctx.addoncatalogwindow, client)
    spawned = _capture_spawn_paging(monkeypatch, ctx.addoncatalogwindow)

    win = _make_window(ctx.addoncatalogwindow)
    win.onInit()

    control = win.getControl(ctx.addoncatalogwindow.LIST)
    control.selected_index = 3  # focus a mid-first-page row
    focused_before = control.items[3]

    win._paging_worker(spawned[0])  # synchronous, on the test thread

    import xbmcgui
    win.onAction(xbmcgui.Action(0))  # drains _pending_pages, as the real noop wake-up would

    assert len(control.items) == 1 + total
    assert control.selected_index == 3
    assert control.items[3] is focused_before
    tail = control.items[-5:]
    assert [item.getProperty('position') for item in tail] == [str(n) for n in range(page_size, total)]
    assert all(item.art.get('icon') for item in tail)


def test_paging_worker_stops_once_the_window_closed(load_addoncatalogwindow, monkeypatch):
    ctx = load_addoncatalogwindow()
    page_size = ctx.addoncatalogwindow._PAGE_SIZE
    total = page_size + 5
    store = _FakeStore(addons=[_source_addon()])
    client = _FakeClient(session=FakeSession(responses=[FakeResponse(_many_addons_envelope(total))]))
    _wire_store(ctx.addoncatalogwindow, store)
    _wire_client(ctx.addoncatalogwindow, client)
    spawned = _capture_spawn_paging(monkeypatch, ctx.addoncatalogwindow)

    win = _make_window(ctx.addoncatalogwindow)
    win.onInit()
    remaining_pages = spawned[0]

    win.close()
    consumed = []

    def _pages():
        for page in remaining_pages:
            consumed.append(page)
            yield page

    win._paging_worker(_pages())

    assert consumed == [remaining_pages[0]]  # first page already "in flight"; no more walked
    assert win._pending_pages == []


# ---------------------------------------------------------------------------
# In-memory search
# ---------------------------------------------------------------------------


def test_search_row_prompts_with_the_search_heading(load_addoncatalogwindow):
    ctx = load_addoncatalogwindow(dialog_inputs=[''])
    store = _FakeStore(addons=[_source_addon()])
    client = _FakeClient(session=FakeSession(responses=[FakeResponse(_search_envelope())]))
    _wire_store(ctx.addoncatalogwindow, store)
    _wire_client(ctx.addoncatalogwindow, client)

    win = _make_window(ctx.addoncatalogwindow)
    win.onInit()
    _select_position(win, ctx.addoncatalogwindow, 'search')
    win.onClick(ctx.addoncatalogwindow.LIST)

    assert ctx.env.dialog_input_prompts == ['STR30346']


def test_search_filters_by_name_and_description_case_insensitively_with_zero_http(load_addoncatalogwindow):
    ctx = load_addoncatalogwindow(dialog_inputs=['ALPHA'])
    store = _FakeStore(addons=[_source_addon()])
    client = _FakeClient(session=FakeSession(responses=[FakeResponse(_search_envelope())]))
    _wire_store(ctx.addoncatalogwindow, store)
    _wire_client(ctx.addoncatalogwindow, client)

    win = _make_window(ctx.addoncatalogwindow)
    win.onInit()
    calls_before = list(client.session.calls)
    assert len(calls_before) == 1  # exactly the one source fetch, not zero via a stale cache

    _select_position(win, ctx.addoncatalogwindow, 'search')
    win.onClick(ctx.addoncatalogwindow.LIST)

    assert client.session.calls == calls_before  # filtering never issues a new GET

    control = win.getControl(ctx.addoncatalogwindow.LIST)
    entry_labels = [item.getLabel() for item in control.items if item.getProperty('position').isdigit()]
    assert len(entry_labels) == 2
    assert any('Alpha Streams' in label for label in entry_labels)   # matched by name
    assert any('Beta Films' in label for label in entry_labels)     # matched by description
    assert not any('Gamma Shows' in label for label in entry_labels)

    search_row = control.items[0]
    assert search_row.getProperty('position') == 'search'
    assert '"ALPHA"' in search_row.label2
    assert 'Showing 2 of 3' in search_row.label2
    # A filter is active - the clear row must be offered.
    assert any(item.getProperty('position') == 'clear' for item in control.items)


def test_search_declining_the_input_leaves_any_existing_filter_unchanged(load_addoncatalogwindow):
    ctx = load_addoncatalogwindow(dialog_inputs=['alpha', ''])
    store = _FakeStore(addons=[_source_addon()])
    client = _FakeClient(session=FakeSession(responses=[FakeResponse(_search_envelope())]))
    _wire_store(ctx.addoncatalogwindow, store)
    _wire_client(ctx.addoncatalogwindow, client)

    win = _make_window(ctx.addoncatalogwindow)
    win.onInit()
    _select_position(win, ctx.addoncatalogwindow, 'search')
    win.onClick(ctx.addoncatalogwindow.LIST)  # sets the filter to 'alpha'

    control = win.getControl(ctx.addoncatalogwindow.LIST)
    before = [item.getProperty('position') for item in control.items]

    _select_position(win, ctx.addoncatalogwindow, 'search')
    win.onClick(ctx.addoncatalogwindow.LIST)  # backs out with an empty answer

    after = [item.getProperty('position') for item in control.items]
    assert before == after
    assert ctx.addoncatalogwindow.get_client  # sanity: module still wired, nothing raised


def test_clear_search_row_restores_the_full_unfiltered_list(load_addoncatalogwindow):
    ctx = load_addoncatalogwindow(dialog_inputs=['alpha'])
    store = _FakeStore(addons=[_source_addon()])
    client = _FakeClient(session=FakeSession(responses=[FakeResponse(_search_envelope())]))
    _wire_store(ctx.addoncatalogwindow, store)
    _wire_client(ctx.addoncatalogwindow, client)

    win = _make_window(ctx.addoncatalogwindow)
    win.onInit()
    _select_position(win, ctx.addoncatalogwindow, 'search')
    win.onClick(ctx.addoncatalogwindow.LIST)  # filtered down to 2 matches

    _select_position(win, ctx.addoncatalogwindow, 'clear')
    win.onClick(ctx.addoncatalogwindow.LIST)

    control = win.getControl(ctx.addoncatalogwindow.LIST)
    digit_positions = sorted(
        item.getProperty('position') for item in control.items if item.getProperty('position').isdigit()
    )
    assert digit_positions == ['0', '1', '2']
    assert not any(item.getProperty('position') == 'clear' for item in control.items)
    assert ctx.addoncatalogwindow.AddonCatalogWindow  # module still importable/usable


def test_search_with_no_matches_shows_the_no_match_message_not_an_empty_list(load_addoncatalogwindow):
    ctx = load_addoncatalogwindow(dialog_inputs=['nothing-matches-this'], localized={30349: 'No addons match your search'})
    store = _FakeStore(addons=[_source_addon()])
    client = _FakeClient(session=FakeSession(responses=[FakeResponse(_search_envelope())]))
    _wire_store(ctx.addoncatalogwindow, store)
    _wire_client(ctx.addoncatalogwindow, client)

    win = _make_window(ctx.addoncatalogwindow)
    win.onInit()
    _select_position(win, ctx.addoncatalogwindow, 'search')
    win.onClick(ctx.addoncatalogwindow.LIST)

    control = win.getControl(ctx.addoncatalogwindow.LIST)
    assert not any(item.getProperty('position').isdigit() for item in control.items)
    message_rows = [item for item in control.items if item.getProperty('position') == '']
    assert len(message_rows) == 1
    assert message_rows[0].getLabel() == 'No addons match your search'
    # The user must still be able to search again or clear - never stranded.
    assert any(item.getProperty('position') == 'search' for item in control.items)
    assert any(item.getProperty('position') == 'clear' for item in control.items)


# ---------------------------------------------------------------------------
# onClick() - installable entry
# ---------------------------------------------------------------------------


def test_onclick_installable_entry_confirmed_installs_the_catalog_manifest(load_addoncatalogwindow, monkeypatch):
    ctx = load_addoncatalogwindow()
    store = _FakeStore(addons=[_source_addon()])
    client = _FakeClient(session=FakeSession(responses=[FakeResponse(CATALOG_ENVELOPE), FakeResponse(CATALOG_ENVELOPE)]))
    _wire_store(ctx.addoncatalogwindow, store)
    _wire_client(ctx.addoncatalogwindow, client)
    monkeypatch.setattr(ctx.dialogs, 'confirm', lambda *a, **k: True)

    win = _make_window(ctx.addoncatalogwindow)
    win.onInit()
    _select_position(win, ctx.addoncatalogwindow, '0')  # "New Addon" - installable
    win.onClick(ctx.addoncatalogwindow.LIST)

    assert store.installed == [
        ('https://new.example/manifest.json', CATALOG_ENVELOPE['addons'][0]['manifest']),
    ]
    assert ('Rivulet', 'STR30012', 'info', 4000) in ctx.env.notifications


def test_onclick_installable_entry_declined_confirm_does_not_install(load_addoncatalogwindow, monkeypatch):
    ctx = load_addoncatalogwindow()
    store = _FakeStore(addons=[_source_addon()])
    client = _FakeClient(session=FakeSession(responses=[FakeResponse(CATALOG_ENVELOPE)]))
    _wire_store(ctx.addoncatalogwindow, store)
    _wire_client(ctx.addoncatalogwindow, client)
    monkeypatch.setattr(ctx.dialogs, 'confirm', lambda *a, **k: False)

    win = _make_window(ctx.addoncatalogwindow)
    win.onInit()
    _select_position(win, ctx.addoncatalogwindow, '0')
    win.onClick(ctx.addoncatalogwindow.LIST)

    assert store.installed == []


# ---------------------------------------------------------------------------
# onClick() - needs-configuration entry
# ---------------------------------------------------------------------------


def test_onclick_needs_configuration_entry_never_installs_and_opens_paste_url_flow(load_addoncatalogwindow, monkeypatch):
    ctx = load_addoncatalogwindow(
        dialog_inputs=[''],  # user backs out of the paste prompt
        localized={30341: '%s needs configuration. Open %s in a browser, configure it, '
                           'then paste the resulting manifest URL'},
    )
    store = _FakeStore(addons=[_source_addon()])
    client = _FakeClient(session=FakeSession(responses=[FakeResponse(CATALOG_ENVELOPE)]))
    _wire_store(ctx.addoncatalogwindow, store)
    _wire_client(ctx.addoncatalogwindow, client)
    monkeypatch.setattr(ctx.dialogs, 'confirm', lambda *a, **k: True)

    win = _make_window(ctx.addoncatalogwindow)
    win.onInit()
    _select_position(win, ctx.addoncatalogwindow, '1')  # "Needs Config"
    win.onClick(ctx.addoncatalogwindow.LIST)

    assert store.installed == []
    assert client.manifest_calls == []
    # The prompt shown to the user names the addon and its /configure URL.
    assert len(ctx.env.dialog_input_prompts) == 1
    assert 'Needs Config' in ctx.env.dialog_input_prompts[0]
    assert 'https://cfg.example/configure' in ctx.env.dialog_input_prompts[0]


def test_onclick_needs_configuration_entry_pasted_url_installs_configured_manifest(load_addoncatalogwindow):
    configured_manifest = {'id': 'cfg', 'name': 'Needs Config (configured)', 'version': '1.0.1'}
    ctx = load_addoncatalogwindow(dialog_inputs=['https://cfg.example/manifest.json?token=xyz'])
    store = _FakeStore(addons=[_source_addon()])
    client = _FakeClient(
        session=FakeSession(responses=[FakeResponse(CATALOG_ENVELOPE), FakeResponse(CATALOG_ENVELOPE)]),
        manifest_result=configured_manifest,
    )
    _wire_store(ctx.addoncatalogwindow, store)
    _wire_client(ctx.addoncatalogwindow, client)

    win = _make_window(ctx.addoncatalogwindow)
    win.onInit()
    _select_position(win, ctx.addoncatalogwindow, '1')  # "Needs Config"
    win.onClick(ctx.addoncatalogwindow.LIST)

    assert store.installed == [('https://cfg.example/manifest.json?token=xyz', configured_manifest)]
    assert ('Rivulet', 'STR30012', 'info', 4000) in ctx.env.notifications


def test_onclick_needs_configuration_entry_invalid_pasted_url_does_not_install(load_addoncatalogwindow):
    ctx = load_addoncatalogwindow(dialog_inputs=['not a url'])
    store = _FakeStore(addons=[_source_addon()])
    client = _FakeClient(session=FakeSession(responses=[FakeResponse(CATALOG_ENVELOPE)]))
    _wire_store(ctx.addoncatalogwindow, store)
    _wire_client(ctx.addoncatalogwindow, client)

    win = _make_window(ctx.addoncatalogwindow)
    win.onInit()
    _select_position(win, ctx.addoncatalogwindow, '1')
    win.onClick(ctx.addoncatalogwindow.LIST)

    assert store.installed == []
    assert client.manifest_calls == []
    assert ('Rivulet', 'STR30014', 'info', 4000) in ctx.env.notifications


# ---------------------------------------------------------------------------
# onClick() - already-installed entry
# ---------------------------------------------------------------------------


def test_onclick_already_installed_entry_is_not_offered_for_install(load_addoncatalogwindow, monkeypatch):
    ctx = load_addoncatalogwindow(localized={30338: '%s is already installed'})
    store = _FakeStore(addons=[_source_addon(), _already_installed_addon()])
    client = _FakeClient(session=FakeSession(responses=[FakeResponse(CATALOG_ENVELOPE)]))
    _wire_store(ctx.addoncatalogwindow, store)
    _wire_client(ctx.addoncatalogwindow, client)
    confirm_calls = []
    monkeypatch.setattr(ctx.dialogs, 'confirm', lambda *a, **k: confirm_calls.append(a) or True)

    win = _make_window(ctx.addoncatalogwindow)
    win.onInit()
    _select_position(win, ctx.addoncatalogwindow, '2')  # "Already Installed"
    win.onClick(ctx.addoncatalogwindow.LIST)

    assert store.installed == []
    assert confirm_calls == []
    assert ('Rivulet', 'Already Installed is already installed', 'info', 4000) in ctx.env.notifications


# ---------------------------------------------------------------------------
# onClick() - control id / focus guards
# ---------------------------------------------------------------------------


def test_onclick_ignores_control_ids_other_than_list(load_addoncatalogwindow):
    ctx = load_addoncatalogwindow()
    _wire_store(ctx.addoncatalogwindow, _FakeStore(addons=[]))
    _wire_client(ctx.addoncatalogwindow, _FakeClient())

    win = _make_window(ctx.addoncatalogwindow)
    win.onInit()
    win.onClick(999)  # must not raise, must not touch the store


def test_onclick_empty_placeholder_row_is_a_noop(load_addoncatalogwindow):
    ctx = load_addoncatalogwindow()
    store = _FakeStore(addons=[])
    _wire_store(ctx.addoncatalogwindow, store)
    _wire_client(ctx.addoncatalogwindow, _FakeClient())

    win = _make_window(ctx.addoncatalogwindow)
    win.onInit()
    win.onClick(ctx.addoncatalogwindow.LIST)  # must not raise

    assert store.installed == []


# ---------------------------------------------------------------------------
# _configure_url()
# ---------------------------------------------------------------------------


def test_configure_url_bare_manifest(load_addoncatalogwindow):
    ctx = load_addoncatalogwindow()
    assert ctx.addoncatalogwindow._configure_url('https://host/manifest.json') == 'https://host/configure'


def test_configure_url_configured_manifest_keeps_config_path_segment(load_addoncatalogwindow):
    ctx = load_addoncatalogwindow()
    url = 'https://host/eyJhIjoxfQ==/manifest.json'
    assert ctx.addoncatalogwindow._configure_url(url) == 'https://host/eyJhIjoxfQ==/configure'


def test_configure_url_with_query_string_moves_configure_into_the_path(load_addoncatalogwindow):
    # validate_transport_url() explicitly allows and preserves a `?query`
    # component (lib/stremio/addons.py) - swapping the suffix on the raw
    # string would land "/configure" after the query instead of the path.
    ctx = load_addoncatalogwindow()
    url = 'https://host/manifest.json?token=abc123'
    assert ctx.addoncatalogwindow._configure_url(url) == 'https://host/configure?token=abc123'


def test_configure_url_with_fragment_is_preserved_alongside_query(load_addoncatalogwindow):
    ctx = load_addoncatalogwindow()
    url = 'https://host/manifest.json?token=abc#frag'
    assert ctx.addoncatalogwindow._configure_url(url) == 'https://host/configure?token=abc#frag'


def test_configure_url_without_manifest_suffix_falls_back_to_appending(load_addoncatalogwindow):
    # No "/manifest.json" suffix to swap - best-effort fallback appends
    # "/configure" directly, same as build_resource_url()'s own fallback.
    ctx = load_addoncatalogwindow()
    url = 'https://host/some/other/path'
    assert ctx.addoncatalogwindow._configure_url(url) == 'https://host/some/other/path/configure'


def test_configure_url_unparsable_url_falls_back_to_whole_string_swap(load_addoncatalogwindow):
    # A malformed netloc (e.g. an invalid IPv6 literal) makes urlsplit()
    # raise ValueError; a third-party addon's transportUrl is untrusted,
    # so this must degrade to the old plain-string behaviour, not raise.
    ctx = load_addoncatalogwindow()
    url = 'https://[bad/manifest.json'
    assert ctx.addoncatalogwindow._configure_url(url) == 'https://[bad/configure'
