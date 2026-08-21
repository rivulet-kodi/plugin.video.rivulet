"""Tests for lib.ui.marketwindow: MarketWindow, Rivulet's addon
marketplace browser (see the module docstring), exercised against the
shared fake xbmc/xbmcgui stubs in tests/kodistubs (no real Kodi runtime,
no network).

Same shape as tests/test_addonswindow.py: MarketWindow.onInit()/
onClick() are called directly (never through a real modal event loop),
`get_store()`/`get_client()` are monkeypatched by assignment on the
reloaded module (both bound at lib.ui.marketwindow module scope, exactly
like lib.ui.addonswindow), and MarketWindow.xml's actual skin rendering
is Kodi-skin-engine-only and is NOT, and cannot be, exercised here - the
fake WindowXML only validates that every control id Python touches
(MarketWindow.LIST) exists in the real skin file.

The fake `xbmcaddon.Addon.getLocalizedString()` returns a plain
`'STR<id>'` placeholder unless a test overrides it via
`load_marketwindow(localized={id: '...%s...'})` - required whenever
production code applies `%` to an `L(id)` result (see
tests/test_views.py's own `localized=` usage for `L(30022) % email`).
"""
import contextlib

import pytest
import requests

from tests.conftest import FakeResponse, FakeSession
from tests.kodistubs import install_kodi_stubs

_RELOAD_MODULE_NAMES = (
    'lib.ui.compat', 'lib.ui.dependencies', 'lib.ui.uicommon', 'lib.ui.router', 'lib.ui.views',
    'lib.ui.dialogs', 'lib.ui.addonswindow', 'lib.ui.marketwindow',
)


def _source_addon():
    """An installed addon (NOT Cinemeta) that publishes one
    addon_catalog - proves MarketWindow has no Cinemeta special-casing,
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
def load_marketwindow():
    """Factory fixture: `load_marketwindow(**kwargs)` installs fresh
    stubs and returns a namespace with `.marketwindow`, `.dialogs`,
    `.views`, and `.env`. Every call torn down automatically, in reverse
    order, at test end."""
    with contextlib.ExitStack() as stack:
        def _load(**kwargs):
            return stack.enter_context(install_kodi_stubs(reload=_RELOAD_MODULE_NAMES, **kwargs))

        yield _load


def _make_window(marketwindow_mod):
    return marketwindow_mod.MarketWindow('MarketWindow.xml', '/addon/path', 'Default', '1080i')


def _wire_store(marketwindow_mod, store):
    marketwindow_mod.get_store = lambda: store


def _wire_client(marketwindow_mod, client):
    marketwindow_mod.get_client = lambda: client


def _select(win, marketwindow_mod, index):
    win.getControl(marketwindow_mod.LIST).selected_index = index


# ---------------------------------------------------------------------------
# onInit() - rendering
# ---------------------------------------------------------------------------


def test_oninit_renders_every_catalog_entry(load_marketwindow):
    ctx = load_marketwindow()
    store = _FakeStore(addons=[_source_addon(), _already_installed_addon()])
    client = _FakeClient(session=FakeSession(responses=[FakeResponse(CATALOG_ENVELOPE)]))
    _wire_store(ctx.marketwindow, store)
    _wire_client(ctx.marketwindow, client)

    win = _make_window(ctx.marketwindow)
    win.onInit()

    items = win.getControl(ctx.marketwindow.LIST).items
    assert len(items) == 3
    labels = [item.getLabel() for item in items]
    assert any('New Addon' in label and 'STR30335' not in label for label in labels)
    assert any('Needs Config' in label and 'STR30337' in label for label in labels)
    assert any('Already Installed' in label and 'STR30335' in label for label in labels)
    assert win.getFocusId() == ctx.marketwindow.LIST


def test_oninit_with_no_addon_catalog_sources_shows_empty_placeholder(load_marketwindow):
    ctx = load_marketwindow()
    _wire_store(ctx.marketwindow, _FakeStore(addons=[]))
    _wire_client(ctx.marketwindow, _FakeClient())

    win = _make_window(ctx.marketwindow)
    win.onInit()

    items = win.getControl(ctx.marketwindow.LIST).items
    assert len(items) == 1
    assert items[0].getLabel() == 'STR30339'
    assert items[0].getProperty('position') == ''


def test_oninit_source_fetch_failure_notifies_and_skips_that_source(load_marketwindow):
    ctx = load_marketwindow(localized={30340: "Could not load %s's addon catalog"})
    _wire_store(ctx.marketwindow, _FakeStore(addons=[_source_addon()]))
    _wire_client(ctx.marketwindow, _FakeClient(session=FakeSession(exc=requests.exceptions.ConnectionError('dead'))))

    win = _make_window(ctx.marketwindow)
    win.onInit()

    items = win.getControl(ctx.marketwindow.LIST).items
    assert len(items) == 1
    assert items[0].getLabel() == 'STR30339'
    assert ctx.env.notifications == [('Rivulet', "Could not load Source Addon's addon catalog", 'info', 4000)]


# ---------------------------------------------------------------------------
# onClick() - installable entry
# ---------------------------------------------------------------------------


def test_onclick_installable_entry_confirmed_installs_the_catalog_manifest(load_marketwindow, monkeypatch):
    ctx = load_marketwindow()
    store = _FakeStore(addons=[_source_addon()])
    client = _FakeClient(session=FakeSession(responses=[FakeResponse(CATALOG_ENVELOPE), FakeResponse(CATALOG_ENVELOPE)]))
    _wire_store(ctx.marketwindow, store)
    _wire_client(ctx.marketwindow, client)
    monkeypatch.setattr(ctx.dialogs, 'confirm', lambda *a, **k: True)

    win = _make_window(ctx.marketwindow)
    win.onInit()
    _select(win, ctx.marketwindow, 0)  # "New Addon" - installable
    win.onClick(ctx.marketwindow.LIST)

    assert store.installed == [
        ('https://new.example/manifest.json', CATALOG_ENVELOPE['addons'][0]['manifest']),
    ]
    assert ('Rivulet', 'STR30012', 'info', 4000) in ctx.env.notifications


def test_onclick_installable_entry_declined_confirm_does_not_install(load_marketwindow, monkeypatch):
    ctx = load_marketwindow()
    store = _FakeStore(addons=[_source_addon()])
    client = _FakeClient(session=FakeSession(responses=[FakeResponse(CATALOG_ENVELOPE)]))
    _wire_store(ctx.marketwindow, store)
    _wire_client(ctx.marketwindow, client)
    monkeypatch.setattr(ctx.dialogs, 'confirm', lambda *a, **k: False)

    win = _make_window(ctx.marketwindow)
    win.onInit()
    _select(win, ctx.marketwindow, 0)
    win.onClick(ctx.marketwindow.LIST)

    assert store.installed == []


# ---------------------------------------------------------------------------
# onClick() - needs-configuration entry
# ---------------------------------------------------------------------------


def test_onclick_needs_configuration_entry_never_installs_and_opens_paste_url_flow(load_marketwindow, monkeypatch):
    ctx = load_marketwindow(
        dialog_inputs=[''],  # user backs out of the paste prompt
        localized={30341: '%s needs configuration. Open %s in a browser, configure it, '
                           'then paste the resulting manifest URL'},
    )
    store = _FakeStore(addons=[_source_addon()])
    client = _FakeClient(session=FakeSession(responses=[FakeResponse(CATALOG_ENVELOPE)]))
    _wire_store(ctx.marketwindow, store)
    _wire_client(ctx.marketwindow, client)
    monkeypatch.setattr(ctx.dialogs, 'confirm', lambda *a, **k: True)

    win = _make_window(ctx.marketwindow)
    win.onInit()
    _select(win, ctx.marketwindow, 1)  # "Needs Config"
    win.onClick(ctx.marketwindow.LIST)

    assert store.installed == []
    assert client.manifest_calls == []
    # The prompt shown to the user names the addon and its /configure URL.
    assert len(ctx.env.dialog_input_prompts) == 1
    assert 'Needs Config' in ctx.env.dialog_input_prompts[0]
    assert 'https://cfg.example/configure' in ctx.env.dialog_input_prompts[0]


def test_onclick_needs_configuration_entry_pasted_url_installs_configured_manifest(load_marketwindow):
    configured_manifest = {'id': 'cfg', 'name': 'Needs Config (configured)', 'version': '1.0.1'}
    ctx = load_marketwindow(dialog_inputs=['https://cfg.example/manifest.json?token=xyz'])
    store = _FakeStore(addons=[_source_addon()])
    client = _FakeClient(
        session=FakeSession(responses=[FakeResponse(CATALOG_ENVELOPE), FakeResponse(CATALOG_ENVELOPE)]),
        manifest_result=configured_manifest,
    )
    _wire_store(ctx.marketwindow, store)
    _wire_client(ctx.marketwindow, client)

    win = _make_window(ctx.marketwindow)
    win.onInit()
    _select(win, ctx.marketwindow, 1)  # "Needs Config"
    win.onClick(ctx.marketwindow.LIST)

    assert store.installed == [('https://cfg.example/manifest.json?token=xyz', configured_manifest)]
    assert ('Rivulet', 'STR30012', 'info', 4000) in ctx.env.notifications


def test_onclick_needs_configuration_entry_invalid_pasted_url_does_not_install(load_marketwindow):
    ctx = load_marketwindow(dialog_inputs=['not a url'])
    store = _FakeStore(addons=[_source_addon()])
    client = _FakeClient(session=FakeSession(responses=[FakeResponse(CATALOG_ENVELOPE)]))
    _wire_store(ctx.marketwindow, store)
    _wire_client(ctx.marketwindow, client)

    win = _make_window(ctx.marketwindow)
    win.onInit()
    _select(win, ctx.marketwindow, 1)
    win.onClick(ctx.marketwindow.LIST)

    assert store.installed == []
    assert client.manifest_calls == []
    assert ('Rivulet', 'STR30014', 'info', 4000) in ctx.env.notifications


# ---------------------------------------------------------------------------
# onClick() - already-installed entry
# ---------------------------------------------------------------------------


def test_onclick_already_installed_entry_is_not_offered_for_install(load_marketwindow, monkeypatch):
    ctx = load_marketwindow(localized={30338: '%s is already installed'})
    store = _FakeStore(addons=[_source_addon(), _already_installed_addon()])
    client = _FakeClient(session=FakeSession(responses=[FakeResponse(CATALOG_ENVELOPE)]))
    _wire_store(ctx.marketwindow, store)
    _wire_client(ctx.marketwindow, client)
    confirm_calls = []
    monkeypatch.setattr(ctx.dialogs, 'confirm', lambda *a, **k: confirm_calls.append(a) or True)

    win = _make_window(ctx.marketwindow)
    win.onInit()
    _select(win, ctx.marketwindow, 2)  # "Already Installed"
    win.onClick(ctx.marketwindow.LIST)

    assert store.installed == []
    assert confirm_calls == []
    assert ('Rivulet', 'Already Installed is already installed', 'info', 4000) in ctx.env.notifications


# ---------------------------------------------------------------------------
# onClick() - control id / focus guards
# ---------------------------------------------------------------------------


def test_onclick_ignores_control_ids_other_than_list(load_marketwindow):
    ctx = load_marketwindow()
    _wire_store(ctx.marketwindow, _FakeStore(addons=[]))
    _wire_client(ctx.marketwindow, _FakeClient())

    win = _make_window(ctx.marketwindow)
    win.onInit()
    win.onClick(999)  # must not raise, must not touch the store


def test_onclick_empty_placeholder_row_is_a_noop(load_marketwindow):
    ctx = load_marketwindow()
    store = _FakeStore(addons=[])
    _wire_store(ctx.marketwindow, store)
    _wire_client(ctx.marketwindow, _FakeClient())

    win = _make_window(ctx.marketwindow)
    win.onInit()
    win.onClick(ctx.marketwindow.LIST)  # must not raise

    assert store.installed == []


# ---------------------------------------------------------------------------
# _configure_url()
# ---------------------------------------------------------------------------


def test_configure_url_bare_manifest(load_marketwindow):
    ctx = load_marketwindow()
    assert ctx.marketwindow._configure_url('https://host/manifest.json') == 'https://host/configure'


def test_configure_url_configured_manifest_keeps_config_path_segment(load_marketwindow):
    ctx = load_marketwindow()
    url = 'https://host/eyJhIjoxfQ==/manifest.json'
    assert ctx.marketwindow._configure_url(url) == 'https://host/eyJhIjoxfQ==/configure'


def test_configure_url_with_query_string_moves_configure_into_the_path(load_marketwindow):
    # validate_transport_url() explicitly allows and preserves a `?query`
    # component (lib/stremio/addons.py) - swapping the suffix on the raw
    # string would land "/configure" after the query instead of the path.
    ctx = load_marketwindow()
    url = 'https://host/manifest.json?token=abc123'
    assert ctx.marketwindow._configure_url(url) == 'https://host/configure?token=abc123'


def test_configure_url_with_fragment_is_preserved_alongside_query(load_marketwindow):
    ctx = load_marketwindow()
    url = 'https://host/manifest.json?token=abc#frag'
    assert ctx.marketwindow._configure_url(url) == 'https://host/configure?token=abc#frag'


def test_configure_url_without_manifest_suffix_falls_back_to_appending(load_marketwindow):
    # No "/manifest.json" suffix to swap - best-effort fallback appends
    # "/configure" directly, same as build_resource_url()'s own fallback.
    ctx = load_marketwindow()
    url = 'https://host/some/other/path'
    assert ctx.marketwindow._configure_url(url) == 'https://host/some/other/path/configure'


def test_configure_url_unparsable_url_falls_back_to_whole_string_swap(load_marketwindow):
    # A malformed netloc (e.g. an invalid IPv6 literal) makes urlsplit()
    # raise ValueError; a third-party addon's transportUrl is untrusted,
    # so this must degrade to the old plain-string behaviour, not raise.
    ctx = load_marketwindow()
    url = 'https://[bad/manifest.json'
    assert ctx.marketwindow._configure_url(url) == 'https://[bad/configure'
