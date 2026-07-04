"""Tests for lib.ui.views + lib.ui.compat: the addon's directory-listing
views, exercised against hand-written xbmc/xbmcgui/xbmcplugin/xbmcaddon/
xbmcvfs stubs (no real Kodi runtime, no network).

Fixture pattern (mirrors tests/test_player_buffer.py's kodi_stubs fixture,
coordinated over IRC so both files stay hermetic in the same process):
build fresh `types.ModuleType` fakes backed by a per-call `Env` recorder,
snapshot `sys.modules.get(name)` for the 5 xbmc* names plus lib.ui.compat/
lib.ui.router/lib.ui.views *before* mutating, inject the fakes, evict the
three lib.ui.* modules so they import fresh against the fakes, then restore
every snapshotted entry exactly in a `finally` block. lib.ui.router imports
lib.ui.views/player only lazily inside run() (never called here), so there
is no stale cross-module binding risk.

The data layer (lib.store.Store / lib.stremio.addons.AddonClient) is faked
by assigning to the names lib.ui.views actually imports (`views.Store`,
`views.AddonClient`) since both are constructed lazily behind a
module-global cache that a fresh import resets to None. lib.stremio.streaminfo
is exercised for real: it's the module responsible for turning hostile,
emoji-laden Stream.title/name text into the addon's single-line labels.
"""
import importlib
import sys
import types
from urllib.parse import parse_qsl, urlparse

import pytest


_FAKE_MODULE_NAMES = ('xbmc', 'xbmcgui', 'xbmcplugin', 'xbmcaddon', 'xbmcvfs')
_RELOAD_MODULE_NAMES = ('lib.ui.compat', 'lib.ui.router', 'lib.ui.views')

_DEFAULT_ADDON_INFO = {
    'id': 'plugin.video.rivulet',
    'name': 'Rivulet',
    'icon': 'special://home/addons/plugin.video.rivulet/icon.png',
    'fanart': 'special://home/addons/plugin.video.rivulet/fanart.jpg',
}


# ---------------------------------------------------------------------------
# Recorder + hand-written stub modules
# ---------------------------------------------------------------------------


class Env:
    """Records every xbmc*/xbmcgui/xbmcplugin call a view under test makes."""

    def __init__(self):
        self.directory_items = []   # [{'handle', 'items', 'totalItems'}]
        self.end_of_directory = []  # [{'handle', 'succeeded', 'updateListing', 'cacheToDisc'}]
        self.content = []           # [(handle, content)]
        self.plugin_category = []   # [(handle, category)]
        self.sort_methods = []      # [(handle, sortMethod)]
        self.notifications = []     # [(heading, message, icon, time)]
        self.executed_builtins = []
        self.dialog_input_prompts = []
        self.opened_settings = False


class FakeInfoTag:
    """Records every InfoTagVideo setter call (the Kodi >=20 code path)."""

    def __init__(self):
        self.calls = {}

    def __getattr__(self, name):
        def setter(value):
            self.calls[name] = value
        return setter


class FakeListItem:
    """Stand-in for xbmcgui.ListItem: records label/art/property/info mutations."""

    def __init__(self, label='', offscreen=False):
        self._label = label
        self.art = {}
        self.properties = {}
        self.legacy_info = {}
        self.info_tag = FakeInfoTag()

    def getLabel(self):
        return self._label

    def setLabel(self, label):
        self._label = label

    def setArt(self, art):
        self.art.update(art)

    def setProperty(self, key, value):
        self.properties[key] = value

    def setInfo(self, kind, info):
        assert kind == 'video'
        self.legacy_info.update(info)

    def getVideoInfoTag(self):
        return self.info_tag


def _make_xbmc(info_labels):
    mod = types.ModuleType('xbmc')
    mod.LOGDEBUG = 0
    mod.LOGINFO = 1
    mod.LOGWARNING = 2
    mod.LOGERROR = 3

    def log(msg, level=mod.LOGDEBUG):
        pass

    def executebuiltin(cmd, env=None):
        pass

    def getInfoLabel(label):
        return info_labels.get(label, '')

    mod.log = log
    mod.executebuiltin = executebuiltin
    mod.getInfoLabel = getInfoLabel
    return mod


def _make_xbmcgui(env, dialog_inputs):
    mod = types.ModuleType('xbmcgui')
    mod.NOTIFICATION_INFO = 'info'
    mod.ListItem = FakeListItem

    inputs = list(dialog_inputs)

    class Dialog:
        def input(self, heading, **kwargs):
            env.dialog_input_prompts.append(heading)
            return inputs.pop(0) if inputs else ''

        def notification(self, heading, message, icon=None, time=4000):
            env.notifications.append((heading, message, icon, time))

    mod.Dialog = Dialog
    return mod


def _make_xbmcplugin(env):
    mod = types.ModuleType('xbmcplugin')
    mod.SORT_METHOD_NONE = 0

    def addDirectoryItems(handle, items, totalItems):
        env.directory_items.append({'handle': handle, 'items': list(items), 'totalItems': totalItems})
        return True

    def setContent(handle, content):
        env.content.append((handle, content))

    def setPluginCategory(handle, category):
        env.plugin_category.append((handle, category))

    def endOfDirectory(handle, succeeded=True, updateListing=False, cacheToDisc=True):
        env.end_of_directory.append({
            'handle': handle, 'succeeded': succeeded,
            'updateListing': updateListing, 'cacheToDisc': cacheToDisc,
        })

    def addSortMethod(handle, sortMethod):
        env.sort_methods.append((handle, sortMethod))

    def setResolvedUrl(handle, succeeded, listitem):
        pass

    mod.addDirectoryItems = addDirectoryItems
    mod.setContent = setContent
    mod.setPluginCategory = setPluginCategory
    mod.endOfDirectory = endOfDirectory
    mod.addSortMethod = addSortMethod
    mod.setResolvedUrl = setResolvedUrl
    return mod


def _make_xbmcaddon(env, addon_info, settings):
    mod = types.ModuleType('xbmcaddon')

    class Addon:
        def __init__(self, id=None):
            self._id = id

        def getAddonInfo(self, key):
            return addon_info.get(key, '')

        def getSetting(self, key):
            return settings.get(key, '')

        def getLocalizedString(self, string_id):
            return 'L%d' % string_id

        def openSettings(self):
            env.opened_settings = True

    mod.Addon = Addon
    return mod


def _make_xbmcvfs():
    mod = types.ModuleType('xbmcvfs')

    def translatePath(path):
        if path.startswith('special://'):
            return '/fake-kodi-home/' + path[len('special://'):]
        return path

    mod.translatePath = translatePath
    mod.exists = lambda path: True
    mod.mkdirs = lambda path: True
    return mod


# ---------------------------------------------------------------------------
# load_views: injects the stubs, imports lib.ui.views fresh, tears down
# ---------------------------------------------------------------------------


@pytest.fixture
def load_views():
    state = {}

    def _load(addon_info=None, settings=None, info_labels=None, dialog_inputs=None):
        env = Env()
        info = dict(_DEFAULT_ADDON_INFO)
        info.update(addon_info or {})
        fake_modules = {
            'xbmc': _make_xbmc(info_labels or {'System.BuildVersion': '21.0 Git:abcdef'}),
            'xbmcgui': _make_xbmcgui(env, dialog_inputs or []),
            'xbmcplugin': _make_xbmcplugin(env),
            'xbmcaddon': _make_xbmcaddon(env, info, dict(settings or {})),
            'xbmcvfs': _make_xbmcvfs(),
        }
        saved = {
            name: sys.modules.get(name)
            for name in list(fake_modules) + list(_RELOAD_MODULE_NAMES)
        }
        for name, mod in fake_modules.items():
            sys.modules[name] = mod
        lib_ui_pkg = sys.modules.get('lib.ui')
        for name in _RELOAD_MODULE_NAMES:
            sys.modules.pop(name, None)
            leaf = name.rsplit('.', 1)[-1]
            # `from lib.ui import compat, router` short-circuits via
            # getattr(lib.ui_pkg, leaf) if that attribute already exists,
            # bypassing sys.modules entirely -- clear it too, or a stale
            # attribute from a previous test's import silently survives
            # the sys.modules.pop() above and never gets refreshed.
            if lib_ui_pkg is not None and leaf in vars(lib_ui_pkg):
                delattr(lib_ui_pkg, leaf)

        views_mod = importlib.import_module('lib.ui.views')
        compat_mod = sys.modules['lib.ui.compat']
        router_mod = sys.modules['lib.ui.router']

        state['saved'] = saved
        return types.SimpleNamespace(views=views_mod, compat=compat_mod, router=router_mod, env=env)

    yield _load

    saved = state.get('saved')
    if saved is not None:
        for name in _FAKE_MODULE_NAMES + _RELOAD_MODULE_NAMES:
            sys.modules.pop(name, None)
        for name, mod in saved.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod


# ---------------------------------------------------------------------------
# Fake data layer (lib.store.Store / lib.stremio.addons.AddonClient)
# ---------------------------------------------------------------------------


class FakeStore:
    def __init__(self, addons=None, auth=None):
        self._addons = addons if addons is not None else []
        self._auth = auth

    def get_addons(self):
        return self._addons

    def get_auth(self):
        return self._auth


class FakeAddonClient:
    def __init__(self, catalog_result=None, stream_results=None):
        self._catalog_result = catalog_result if catalog_result is not None else []
        self._stream_results = stream_results or {}

    def catalog(self, transport, ctype, cid, extra=None):
        return self._catalog_result

    def streams(self, transport_url, stype, sid):
        return self._stream_results.get(transport_url, [])


def _wire_data_layer(views, store, client):
    views.Store = lambda *a, **k: store
    views.AddonClient = lambda *a, **k: client


# ---------------------------------------------------------------------------
# Hostile, emoji-laden stream fixtures (real Stream4Me/AIOStreams-style junk)
# ---------------------------------------------------------------------------


def _stream(name='', title='', video_size=None, info_hash='deadbeef'):
    behavior_hints = {}
    if video_size is not None:
        behavior_hints['videoSize'] = video_size
    return {'name': name, 'title': title, 'infoHash': info_hash, 'behaviorHints': behavior_hints}


STREAM_2160P = _stream(
    name='\U0001F4E1 [AIOStreams] 4K REMUX',
    title='Interstellar 2160p BluRay REMUX HDR10\n\u26A1 Seeds: 20\n\U0001F3A5',
    video_size=15 * 1024 ** 3,
)
STREAM_1080P = _stream(
    name='1080p WEB-DL Group',
    title='Interstellar 1080p WEB-DL x264\n\u26A1 Seeds: 300',
    video_size=2 * 1024 ** 3,
)
STREAM_720P = _stream(
    name='\U0001F4A9 720p CAM',
    title='Interstellar 720p CAM\n\u26A1 Seeds: 500',
    video_size=700 * 1024 ** 2,
)


# ---------------------------------------------------------------------------
# home()
# ---------------------------------------------------------------------------


def test_home_without_auth_lists_four_entries_no_library(load_views):
    ctx = load_views()
    views, compat = ctx.views, ctx.compat
    _wire_data_layer(views, FakeStore(auth=None), FakeAddonClient())

    views.home()

    call = ctx.env.directory_items[-1]
    items = call['items']
    assert len(items) == 4
    suffixes = [
        'resources/media/discover.png',
        'resources/media/search.png',
        'resources/media/addons.png',
        'resources/media/settings.png',
    ]
    for (_, li, is_folder), suffix in zip(items, suffixes):
        assert is_folder is True
        assert li.art['icon'].endswith(suffix)
        assert li.art['thumb'].endswith(suffix)
        assert li.art['fanart'] == compat.addon_fanart()
    assert ctx.env.content[-1] == (call['handle'], 'files')
    assert ctx.env.plugin_category[-1] == (call['handle'], compat.ADDON_NAME)
    assert ctx.env.end_of_directory[-1]['succeeded'] is True


def test_home_with_auth_lists_five_entries_including_library(load_views):
    ctx = load_views()
    views = ctx.views
    _wire_data_layer(views, FakeStore(auth={'authKey': 'abc', 'user': {}}), FakeAddonClient())

    views.home()

    items = ctx.env.directory_items[-1]['items']
    assert len(items) == 5
    suffixes = [
        'resources/media/discover.png',
        'resources/media/search.png',
        'resources/media/library.png',
        'resources/media/addons.png',
        'resources/media/settings.png',
    ]
    for (_, li, _), suffix in zip(items, suffixes):
        assert li.art['icon'].endswith(suffix)


# ---------------------------------------------------------------------------
# catalog()
# ---------------------------------------------------------------------------


def test_catalog_background_fallback_and_content_type_movies(load_views):
    ctx = load_views()
    views, compat = ctx.views, ctx.compat
    transport = 'https://addon-a.example/manifest.json'
    descriptor = {
        'transportUrl': transport,
        'manifest': {'id': 'org.a', 'name': 'Addon A', 'catalogs': [{'type': 'movie', 'id': 'top'}]},
        'flags': {},
    }
    metas = [
        {'id': 'tt1', 'name': 'Has Background', 'type': 'movie', 'background': 'https://x.example/bg.jpg'},
        {'id': 'tt2', 'name': 'No Background', 'type': 'movie'},
    ]
    _wire_data_layer(views, FakeStore(addons=[descriptor]), FakeAddonClient(catalog_result=metas))

    views.catalog(transport, 'movie', 'top')

    call = ctx.env.directory_items[-1]
    items = call['items']
    # no next-page item: the catalog declares no 'skip' extra
    assert len(items) == 2
    _, li_with_bg, _ = items[0]
    _, li_without_bg, _ = items[1]
    assert li_with_bg.art['fanart'] == 'https://x.example/bg.jpg'
    assert li_without_bg.art['fanart'] == compat.addon_fanart()
    assert ctx.env.content[-1] == (call['handle'], 'movies')


def test_catalog_content_type_tvshows_for_series(load_views):
    ctx = load_views()
    views = ctx.views
    transport = 'https://addon-a.example/manifest.json'
    descriptor = {'transportUrl': transport, 'manifest': {'id': 'org.a', 'catalogs': []}, 'flags': {}}
    metas = [{'id': 'tt9', 'name': 'A Show', 'type': 'series'}]
    _wire_data_layer(views, FakeStore(addons=[descriptor]), FakeAddonClient(catalog_result=metas))

    views.catalog(transport, 'series', 'top')

    assert ctx.env.content[-1][1] == 'tvshows'


def test_catalog_appends_next_page_item_when_skip_declared(load_views):
    ctx = load_views()
    views = ctx.views
    transport = 'https://addon-a.example/manifest.json'
    descriptor = {
        'transportUrl': transport,
        'manifest': {
            'id': 'org.a',
            'catalogs': [{'type': 'movie', 'id': 'top', 'extra': [{'name': 'skip'}]}],
        },
        'flags': {},
    }
    metas = [{'id': 'tt1', 'name': 'One', 'type': 'movie'}, {'id': 'tt2', 'name': 'Two', 'type': 'movie'}]
    _wire_data_layer(views, FakeStore(addons=[descriptor]), FakeAddonClient(catalog_result=metas))

    views.catalog(transport, 'movie', 'top')

    items = ctx.env.directory_items[-1]['items']
    assert len(items) == 3
    url, _, is_folder = items[-1]
    assert is_folder is True
    outer_query = dict(parse_qsl(urlparse(url).query))
    # the forwarded catalog "extra" blob is itself a query-string, nested
    # (and re-percent-encoded) inside this plugin URL's own 'extra' param
    forwarded_extra = dict(parse_qsl(outer_query['extra']))
    assert forwarded_extra['skip'] == '2'


# ---------------------------------------------------------------------------
# streams()
# ---------------------------------------------------------------------------


def test_streams_view_one_line_labels_quality_sort_default_and_plot(load_views, monkeypatch):
    monkeypatch.setattr(sys, 'argv', ['default.py'])
    ctx = load_views()
    views, compat = ctx.views, ctx.compat
    transport = 'https://addon-a.example/manifest.json'
    descriptor = {
        'transportUrl': transport,
        'manifest': {
            'id': 'org.a', 'name': 'Addon A', 'logo': 'https://addon-a.example/logo.png',
            'resources': ['stream'], 'types': ['movie'], 'idPrefixes': ['tt'],
        },
        'flags': {},
    }
    store = FakeStore(addons=[descriptor])
    client = FakeAddonClient(stream_results={transport: [STREAM_2160P, STREAM_1080P, STREAM_720P]})
    _wire_data_layer(views, store, client)
    poster = 'data:image/jpeg;base64,QUJD'

    views.streams('movie', 'tt1234567', poster=poster, title='Interstellar')

    call = ctx.env.directory_items[-1]
    items = call['items']
    assert len(items) == 3

    labels = [li.getLabel() for _, li, _ in items]
    for label in labels:
        assert '\n' not in label
        assert '\r' not in label

    # 'quality' default: resolution tier desc (seeders/size are tiebreakers
    # only within the same tier, so 20/300/500 seeders does not reorder this)
    assert '2160p' in labels[0] and 'Remux' in labels[0] and 'HDR10' in labels[0]
    assert '1080p' in labels[1] and 'WEB-DL' in labels[1] and 'x264' in labels[1]
    assert '720p' in labels[2] and 'CAM' in labels[2]

    for _, li, is_folder in items:
        assert is_folder is False
        assert li.properties.get('IsPlayable') == 'true'
        assert li.art['fanart'] == compat.addon_fanart()
        # explicit poster param wins as thumb even though the addon has a logo
        assert li.art['icon'] == poster
        assert li.art['thumb'] == poster
        assert li.info_tag.calls.get('setTitle') == 'Interstellar'
        assert li.info_tag.calls.get('setMediaType') == 'movie'

    # plot populated from the parsed stream metadata (heading, size, seeders, addon)
    plot_2160p = items[0][1].info_tag.calls.get('setPlot')
    assert plot_2160p.startswith('Interstellar 2160p BluRay REMUX HDR10')
    assert '15.00 GB' in plot_2160p
    assert '20 seeders' in plot_2160p
    assert 'Addon A' in plot_2160p

    assert ctx.env.content[-1] == (call['handle'], 'videos')
    assert (call['handle'], 0) in ctx.env.sort_methods


def test_streams_view_sort_setting_overrides_default_quality_order(load_views, monkeypatch):
    monkeypatch.setattr(sys, 'argv', ['default.py'])
    ctx = load_views(settings={'stream_sort': 'seeders'})
    views = ctx.views
    transport = 'https://addon-a.example/manifest.json'
    descriptor = {
        'transportUrl': transport,
        'manifest': {
            'id': 'org.a', 'name': 'Addon A',
            'resources': ['stream'], 'types': ['movie'], 'idPrefixes': ['tt'],
        },
        'flags': {},
    }
    store = FakeStore(addons=[descriptor])
    client = FakeAddonClient(stream_results={transport: [STREAM_2160P, STREAM_1080P, STREAM_720P]})
    _wire_data_layer(views, store, client)

    views.streams('movie', 'tt1234567')

    labels = [li.getLabel() for _, li, _ in ctx.env.directory_items[-1]['items']]
    # seeders desc: 720p(500), 1080p(300), 2160p(20) -- the inverse of the
    # quality-tier order from the default-sort test above
    assert '720p' in labels[0]
    assert '1080p' in labels[1]
    assert '2160p' in labels[2]


@pytest.mark.parametrize(
    ('logo', 'expected_thumb'),
    [
        pytest.param('https://addon-a.example/logo.png', 'https://addon-a.example/logo.png', id='addon-logo-fallback'),
        pytest.param('', 'DefaultVideo.png', id='default-video-png-fallback'),
    ],
)
def test_streams_thumb_falls_back_when_no_poster(load_views, monkeypatch, logo, expected_thumb):
    monkeypatch.setattr(sys, 'argv', ['default.py'])
    ctx = load_views()
    views = ctx.views
    transport = 'https://addon-a.example/manifest.json'
    manifest = {
        'id': 'org.a', 'name': 'Addon A',
        'resources': ['stream'], 'types': ['movie'], 'idPrefixes': ['tt'],
    }
    if logo:
        manifest['logo'] = logo
    descriptor = {'transportUrl': transport, 'manifest': manifest, 'flags': {}}
    store = FakeStore(addons=[descriptor])
    client = FakeAddonClient(stream_results={transport: [STREAM_1080P]})
    _wire_data_layer(views, store, client)

    views.streams('movie', 'tt1234567')

    items = ctx.env.directory_items[-1]['items']
    assert len(items) == 1
    _, li, _ = items[0]
    assert li.art['icon'] == expected_thumb
    assert li.art['thumb'] == expected_thumb


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
