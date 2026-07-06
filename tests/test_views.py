"""Tests for lib.ui.views + lib.ui.compat: the addon's directory-listing
views, exercised against the shared fake xbmc/xbmcgui/xbmcplugin/xbmcaddon/
xbmcvfs stubs in tests/kodistubs (no real Kodi runtime, no network).

`load_views` wraps `tests.kodistubs.install_kodi_stubs()`, (re)importing
lib.ui.compat/lib.ui.router/lib.ui.views fresh against the stubs for each
call, and restoring sys.modules / the lib.ui package's leaf attributes
exactly at teardown. lib.ui.router imports lib.ui.views/player only
lazily inside run() (never called here), so there is no stale
cross-module binding risk.

The data layer (lib.store.Store / lib.stremio.addons.AddonClient) is faked
by assigning to the names lib.ui.views actually imports (`views.Store`,
`views.AddonClient`) since both are constructed lazily behind a
module-global cache that a fresh import resets to None. lib.stremio.streaminfo
is exercised for real: it's the module responsible for turning hostile,
emoji-laden Stream.title/name text into the addon's single-line labels.
"""
import contextlib
import sys
from urllib.parse import parse_qsl, urlparse

import pytest

from lib.stremio.addons import AddonError
from lib.stremio.api import ApiError
from tests.kodistubs import install_kodi_stubs

_RELOAD_MODULE_NAMES = ('lib.ui.compat', 'lib.ui.router', 'lib.ui.views')


# ---------------------------------------------------------------------------
# load_views: installs the shared stubs, imports lib.ui.views fresh
# ---------------------------------------------------------------------------


@pytest.fixture
def load_views():
    """Factory fixture: `load_views(addon_info=None, settings=None,
    info_labels=None, dialog_inputs=None, dialog_yesno=None,
    localized=None)` installs fresh stubs (via
    tests.kodistubs.install_kodi_stubs) reloading lib.ui.compat/
    lib.ui.router/lib.ui.views, and returns a namespace with `.views`,
    `.compat`, `.router`, and `.env` (the call recorder). Every call is
    torn down automatically, in reverse order, at test end.
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
    the same install/remove/set_auth/set_addons contracts as the real
    filesystem-backed Store (see lib/store.py) so addon_install()/
    addon_remove()/login()/logout() exercise realistic behavior (e.g.
    remove_addon() raising ValueError for a protected descriptor).
    """

    def __init__(self, addons=None, auth=None):
        self._addons = addons if addons is not None else []
        self._auth = auth
        self.installed = []          # [(transport_url, manifest), ...]
        self.auth_set_calls = []     # [auth_dict_or_None, ...]
        self.addons_set_calls = []   # [[descriptor, ...], ...]

    def get_addons(self):
        return self._addons

    def get_auth(self):
        return self._auth

    def install_addon(self, transport_url, manifest):
        self.installed.append((transport_url, manifest))
        self._addons = [a for a in self._addons if a.get('transportUrl') != transport_url]
        self._addons.append({'transportUrl': transport_url, 'manifest': manifest, 'flags': {}})

    def remove_addon(self, transport_url):
        target = next((a for a in self._addons if a.get('transportUrl') == transport_url), None)
        if target is None:
            return
        if (target.get('flags') or {}).get('protected'):
            raise ValueError('cannot remove protected addon: %s' % transport_url)
        self._addons = [a for a in self._addons if a.get('transportUrl') != transport_url]

    def set_auth(self, auth):
        self.auth_set_calls.append(auth)
        self._auth = auth

    def set_addons(self, addons):
        addons = list(addons)
        self.addons_set_calls.append(addons)
        self._addons = addons


class FakeAddonClient:
    """Fake `lib.stremio.addons.AddonClient`. `catalog_result` is the
    single-addon default every existing catalog()/search() test relies
    on; `catalog_results`/`stream_results`/`meta_results` (transport_url
    -> list-or-Exception) let a test script different addons differently
    - a dict value that is an Exception instance is raised instead of
    returned, standing in for a network/manifest failure.
    """

    def __init__(self, catalog_result=None, stream_results=None, catalog_results=None,
                 meta_results=None, manifest_result=None, manifest_error=None):
        self._catalog_result = catalog_result if catalog_result is not None else []
        self._catalog_results = catalog_results or {}
        self._stream_results = stream_results or {}
        self._meta_results = meta_results or {}
        self._manifest_result = manifest_result
        self._manifest_error = manifest_error
        self.manifest_calls = []

    def catalog(self, transport, ctype, cid, extra=None):
        result = self._catalog_results.get(transport, self._catalog_result)
        if isinstance(result, Exception):
            raise result
        return result

    def streams(self, transport_url, stype, sid):
        result = self._stream_results.get(transport_url, [])
        if isinstance(result, Exception):
            raise result
        return result

    def meta(self, transport_url, stype, sid):
        result = self._meta_results.get(transport_url)
        if isinstance(result, Exception):
            raise result
        return result

    def manifest(self, url):
        self.manifest_calls.append(url)
        if self._manifest_error is not None:
            raise self._manifest_error
        return self._manifest_result


class FakeStremioAPI:
    """Fake `lib.stremio.api.StremioAPI` for login()/logout()/library()."""

    def __init__(self, login_result=None, login_error=None, addon_collection_result=None,
                 addon_collection_error=None, logout_error=None,
                 datastore_result=None, datastore_error=None):
        self._login_result = login_result
        self._login_error = login_error
        self._addon_collection_result = addon_collection_result
        self._addon_collection_error = addon_collection_error
        self._logout_error = logout_error
        self._datastore_result = datastore_result if datastore_result is not None else []
        self._datastore_error = datastore_error
        self.logout_calls = []

    def login(self, email, password):
        if self._login_error is not None:
            raise self._login_error
        return self._login_result

    def addon_collection_get(self, auth_key):
        if self._addon_collection_error is not None:
            raise self._addon_collection_error
        return self._addon_collection_result

    def logout(self, auth_key):
        self.logout_calls.append(auth_key)
        if self._logout_error is not None:
            raise self._logout_error

    def datastore_get(self, auth_key, collection='libraryItem', all=True):
        if self._datastore_error is not None:
            raise self._datastore_error
        return self._datastore_result


def _wire_data_layer(views, store, client):
    views.Store = lambda *a, **k: store
    views.AddonClient = lambda *a, **k: client


def _wire_api(views, api):
    views.StremioAPI = lambda *a, **k: api


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
        # every entry navigates as a folder except Settings, which runs
        # in place (openSettings) and must NOT trigger GetDirectory
        assert is_folder is (not suffix.endswith('settings.png'))
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
# discover()
# ---------------------------------------------------------------------------


def test_discover_lists_catalogs_across_addons_with_art_and_url_fallbacks(load_views):
    ctx = load_views()
    views, compat, router = ctx.views, ctx.compat, ctx.router
    descriptor_x = {
        'transportUrl': 'https://x.example/manifest.json',
        'manifest': {
            'id': 'org.x', 'name': 'Addon X', 'background': 'https://x.example/bg.jpg',
            'catalogs': [{'type': 'movie', 'id': 'top', 'name': 'Top Movies'}],
        },
    }
    descriptor_y = {
        'transportUrl': 'https://y.example/manifest.json',
        'manifest': {
            'id': 'org.y', 'name': 'Addon Y', 'logo': 'https://y.example/logo.png',
            'catalogs': [{'type': 'series', 'id': 'popular'}],  # unnamed -> falls back to id
        },
    }
    _wire_data_layer(views, FakeStore(addons=[descriptor_x, descriptor_y]), FakeAddonClient())

    views.discover()

    call = ctx.env.directory_items[-1]
    items = call['items']
    assert len(items) == 2

    url0, li0, is_folder0 = items[0]
    assert is_folder0 is True
    assert li0.getLabel() == 'Addon X: Top Movies (movie)'
    assert li0.art['fanart'] == 'https://x.example/bg.jpg'
    assert 'icon' not in li0.art
    assert url0 == router.url_for('catalog', transport=descriptor_x['transportUrl'], type='movie', id='top')

    url1, li1, _ = items[1]
    assert li1.getLabel() == 'Addon Y: popular (series)'
    assert li1.art['icon'] == 'https://y.example/logo.png'
    assert li1.art['fanart'] == compat.addon_fanart()
    assert url1 == router.url_for('catalog', transport=descriptor_y['transportUrl'], type='series', id='popular')

    assert ctx.env.content[-1] == (call['handle'], 'files')
    assert ctx.env.end_of_directory[-1]['succeeded'] is True


# ---------------------------------------------------------------------------
# catalog() - error/empty/pagination edge cases
# ---------------------------------------------------------------------------


def test_catalog_addon_error_notifies_and_fails_without_building_items(load_views):
    ctx = load_views()
    views = ctx.views
    transport = 'https://addon-a.example/manifest.json'
    descriptor = {'transportUrl': transport, 'manifest': {'id': 'org.a', 'catalogs': []}, 'flags': {}}
    client = FakeAddonClient(catalog_results={transport: AddonError('upstream timeout')})
    _wire_data_layer(views, FakeStore(addons=[descriptor]), client)

    views.catalog(transport, 'movie', 'top')

    assert ctx.env.notifications[-1][1] == 'upstream timeout'
    assert ctx.env.end_of_directory[-1]['succeeded'] is False
    assert ctx.env.directory_items == []


def test_catalog_empty_result_notifies_no_content_but_ends_successfully(load_views):
    ctx = load_views()
    views = ctx.views
    transport = 'https://addon-a.example/manifest.json'
    descriptor = {'transportUrl': transport, 'manifest': {'id': 'org.a', 'catalogs': []}, 'flags': {}}
    _wire_data_layer(views, FakeStore(addons=[descriptor]), FakeAddonClient(catalog_result=[]))

    views.catalog(transport, 'movie', 'top')

    call = ctx.env.directory_items[-1]
    assert call['items'] == []
    assert ctx.env.notifications[-1][1] == 'STR30030'
    assert ctx.env.end_of_directory[-1]['succeeded'] is True


def test_catalog_content_type_defaults_to_videos_for_unrecognized_type(load_views):
    ctx = load_views()
    views = ctx.views
    transport = 'https://addon-a.example/manifest.json'
    descriptor = {'transportUrl': transport, 'manifest': {'id': 'org.a', 'catalogs': []}, 'flags': {}}
    metas = [{'id': 'ch1', 'name': 'Channel One', 'type': 'channel'}]
    _wire_data_layer(views, FakeStore(addons=[descriptor]), FakeAddonClient(catalog_result=metas))

    views.catalog(transport, 'channel', 'top')

    assert ctx.env.content[-1][1] == 'videos'


@pytest.mark.parametrize(
    ('existing_extra', 'expected_next_skip'),
    [
        pytest.param('skip=4', '6', id='resumes-from-existing-skip'),
        pytest.param('skip=not-a-number', '2', id='invalid-skip-value-resets-to-zero'),
    ],
)
def test_catalog_next_page_skip_math(load_views, existing_extra, expected_next_skip):
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

    views.catalog(transport, 'movie', 'top', extra=existing_extra)

    url, _, _ = ctx.env.directory_items[-1]['items'][-1]
    outer_query = dict(parse_qsl(urlparse(url).query))
    forwarded_extra = dict(parse_qsl(outer_query['extra']))
    assert forwarded_extra['skip'] == expected_next_skip


def test_meta_item_maps_year_runtime_rating_and_skips_missing_fields(load_views):
    ctx = load_views()
    views = ctx.views
    transport = 'https://addon-a.example/manifest.json'
    descriptor = {'transportUrl': transport, 'manifest': {'id': 'org.a', 'catalogs': []}, 'flags': {}}
    metas = [
        {
            'id': 'tt1', 'name': 'Full Meta', 'type': 'movie',
            'releaseInfo': '2014-2020', 'runtime': '169 min', 'imdbRating': '8.6',
            'genres': ['Sci-Fi', 'Drama'], 'logo': 'https://x.example/logo.png',
            'certification': 'PG-13', 'country': 'USA', 'director': ['Chris Nolan'],
            'writer': ['Jonathan Nolan'], 'tagline': 'Mankind was born on Earth.',
        },
        {'id': 'tt2', 'name': 'Bare Meta', 'type': 'movie'},
    ]
    _wire_data_layer(views, FakeStore(addons=[descriptor]), FakeAddonClient(catalog_result=metas))

    views.catalog(transport, 'movie', 'top')

    items = ctx.env.directory_items[-1]['items']
    _, li_full, _ = items[0]
    assert li_full.info_tag.calls.get('setYear') == 2014
    assert li_full.info_tag.calls.get('setDuration') == 169 * 60
    assert li_full.info_tag.calls.get('setRating') == 8.6
    assert li_full.info_tag.calls.get('setGenres') == ['Sci-Fi', 'Drama']
    assert li_full.info_tag.calls.get('setOriginalTitle') == 'Full Meta'
    assert li_full.info_tag.calls.get('setMpaa') == 'PG-13'
    assert li_full.info_tag.calls.get('setCountries') == ['USA']
    assert li_full.info_tag.calls.get('setDirectors') == ['Chris Nolan']
    assert li_full.info_tag.calls.get('setWriters') == ['Jonathan Nolan']
    assert li_full.info_tag.calls.get('setPlotOutline') == 'Mankind was born on Earth.'
    assert li_full.art['clearlogo'] == 'https://x.example/logo.png'
    assert li_full.getLabel() == 'Full Meta [COLOR grey](2014)[/COLOR] [COLOR gold]8.6[/COLOR]'

    _, li_bare, _ = items[1]
    assert 'setYear' not in li_bare.info_tag.calls
    assert 'setDuration' not in li_bare.info_tag.calls
    assert 'setRating' not in li_bare.info_tag.calls
    assert 'setMpaa' not in li_bare.info_tag.calls
    assert 'setCountries' not in li_bare.info_tag.calls
    assert 'setDirectors' not in li_bare.info_tag.calls
    assert 'setWriters' not in li_bare.info_tag.calls
    assert 'setPlotOutline' not in li_bare.info_tag.calls
    assert 'clearlogo' not in li_bare.art
    assert li_bare.getLabel() == 'Bare Meta'


# ---------------------------------------------------------------------------
# search()
# ---------------------------------------------------------------------------


def test_search_cancelled_dialog_ends_directory_without_querying_addons(load_views):
    ctx = load_views()  # default dialog_inputs=None -> Dialog.input() returns ''
    views = ctx.views
    _wire_data_layer(views, FakeStore(addons=[{'transportUrl': 't', 'manifest': {'catalogs': []}}]), FakeAddonClient())

    views.search()

    assert ctx.env.dialog_input_prompts == ['STR30001']
    assert ctx.env.directory_items == []
    assert ctx.env.end_of_directory[-1] == {
        'handle': -1, 'succeeded': False, 'updateListing': False, 'cacheToDisc': False,
    }


def test_search_aggregates_labelled_results_and_skips_addon_errors(load_views):
    ctx = load_views(dialog_inputs=['batman'])
    views = ctx.views
    transport_a = 'https://a.example/manifest.json'
    transport_b = 'https://b.example/manifest.json'
    descriptor_a = {
        'transportUrl': transport_a,
        'manifest': {
            'id': 'org.a', 'name': 'Addon A',
            'catalogs': [{'type': 'movie', 'id': 'search', 'extra': [{'name': 'search'}]}],
        },
        'flags': {},
    }
    descriptor_b = {
        'transportUrl': transport_b,
        'manifest': {
            'id': 'org.b', 'name': 'Addon B',
            'catalogs': [{'type': 'movie', 'id': 'search', 'extraSupported': ['search']}],
        },
        'flags': {},
    }
    descriptor_c = {
        'transportUrl': 'https://c.example/manifest.json',
        'manifest': {
            'id': 'org.c', 'name': 'Addon C',
            'catalogs': [{'type': 'movie', 'id': 'search', 'extra': [{'name': 'search'}]}],
        },
        'flags': {},
    }
    client = FakeAddonClient(catalog_results={
        transport_a: AddonError('addon a down'),
        transport_b: [{'id': 'tt1', 'name': 'Batman', 'type': 'movie'}],
        'https://c.example/manifest.json': [],  # no error, just nothing found -> skipped too
    })
    _wire_data_layer(views, FakeStore(addons=[descriptor_a, descriptor_b, descriptor_c]), client)

    views.search()

    items = ctx.env.directory_items[-1]['items']
    assert len(items) == 1
    _, li, is_folder = items[0]
    assert is_folder is True
    assert li.getLabel() == '[Addon B] Batman'
    assert ctx.env.content[-1][1] == 'videos'


def test_search_no_matching_catalogs_notifies_empty_result(load_views):
    ctx = load_views(dialog_inputs=['batman'])
    views = ctx.views
    descriptor = {
        'transportUrl': 't1',
        'manifest': {'id': 'org.a', 'catalogs': [{'type': 'movie', 'id': 'top'}]},  # no 'search' extra
        'flags': {},
    }
    _wire_data_layer(views, FakeStore(addons=[descriptor]), FakeAddonClient())

    views.search()

    assert ctx.env.directory_items[-1]['items'] == []
    assert ctx.env.notifications[-1][1] == 'STR30030'


# ---------------------------------------------------------------------------
# meta() / _fetch_meta() / _ordered_seasons()
# ---------------------------------------------------------------------------


def test_meta_not_found_notifies_and_ends_failed(load_views):
    ctx = load_views()
    views = ctx.views
    _wire_data_layer(views, FakeStore(addons=[]), FakeAddonClient())

    views.meta('movie', 'tt0')

    assert ctx.env.notifications[-1][1] == 'STR30030'
    assert ctx.env.end_of_directory[-1]['succeeded'] is False
    assert ctx.env.directory_items == []


def test_meta_movie_without_videos_delegates_to_streams_view(load_views, monkeypatch):
    monkeypatch.setattr(sys, 'argv', ['default.py'])
    ctx = load_views()
    views = ctx.views
    transport = 'https://addon-a.example/manifest.json'
    descriptor = {
        'transportUrl': transport,
        'manifest': {
            'id': 'org.a', 'name': 'Addon A',
            'resources': ['meta', 'stream'], 'types': ['movie'], 'idPrefixes': ['tt'],
        },
        'flags': {},
    }
    client = FakeAddonClient(
        meta_results={transport: {'id': 'tt1', 'name': 'Interstellar', 'type': 'movie', 'poster': 'poster.jpg'}},
        stream_results={transport: [STREAM_1080P]},
    )
    _wire_data_layer(views, FakeStore(addons=[descriptor]), client)

    views.meta('movie', 'tt1')

    call = ctx.env.directory_items[-1]
    assert ctx.env.content[-1] == (call['handle'], 'videos')
    assert len(call['items']) == 1
    _, li, is_folder = call['items'][0]
    assert is_folder is False
    assert li.art['icon'] == 'poster.jpg'
    assert li.info_tag.calls.get('setTitle') == 'Interstellar'


def test_fetch_meta_skips_unsupported_and_erroring_addons_first_hit_wins(load_views):
    ctx = load_views()
    views = ctx.views
    descriptor_skip = {
        'transportUrl': 't1',
        'manifest': {'id': 'org.skip', 'resources': ['catalog'], 'types': ['series'], 'idPrefixes': ['tt']},
    }
    descriptor_error = {
        'transportUrl': 't2',
        'manifest': {'id': 'org.err', 'resources': ['meta'], 'types': ['series'], 'idPrefixes': ['tt']},
    }
    descriptor_hit = {
        'transportUrl': 't3',
        'manifest': {'id': 'org.hit', 'resources': ['meta'], 'types': ['series'], 'idPrefixes': ['tt']},
    }
    client = FakeAddonClient(meta_results={
        't2': AddonError('meta fetch boom'),
        't3': {
            'id': 'tt1', 'name': 'A Show', 'type': 'series',
            'videos': [{'season': 1, 'episode': 1, 'id': 'ep1'}],
        },
    })
    _wire_data_layer(views, FakeStore(addons=[descriptor_skip, descriptor_error, descriptor_hit]), client)

    views.meta('series', 'tt1')

    assert ctx.env.content[-1][1] == 'seasons'
    items = ctx.env.directory_items[-1]['items']
    assert len(items) == 1
    assert any('meta fetch boom' in msg for msg, _level in ctx.env.log_calls)


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
    client = FakeAddonClient(meta_results={
        't1': None,  # claims support but returns nothing usable -> aggregation must keep going
        't2': {
            'id': 'tt1', 'name': 'A Show', 'type': 'series',
            'videos': [{'season': 1, 'episode': 1, 'id': 'ep1'}],
        },
    })
    _wire_data_layer(views, FakeStore(addons=[descriptor_empty, descriptor_hit]), client)

    views.meta('series', 'tt1')

    assert ctx.env.content[-1][1] == 'seasons'
    assert len(ctx.env.directory_items[-1]['items']) == 1


def test_meta_series_orders_seasons_specials_last_with_poster_fallback_fanart(load_views):
    ctx = load_views()
    views, router = ctx.views, ctx.router
    transport = 't1'
    descriptor = {
        'transportUrl': transport,
        'manifest': {'id': 'org.a', 'resources': ['meta'], 'types': ['series'], 'idPrefixes': ['tt']},
    }
    meta_obj = {
        'id': 'tt1', 'name': 'A Show', 'type': 'series', 'poster': 'poster.jpg',
        'videos': [
            {'season': 1, 'episode': 1, 'id': 'ep1'},
            {'season': 0, 'episode': 1, 'id': 'sp1'},
            {'season': 2, 'episode': 1, 'id': 'ep2'},
        ],
    }
    client = FakeAddonClient(meta_results={transport: meta_obj})
    _wire_data_layer(views, FakeStore(addons=[descriptor]), client)

    views.meta('series', 'tt1')

    items = ctx.env.directory_items[-1]['items']
    labels = [li.getLabel() for _, li, _ in items]
    assert labels == ['Season 1', 'Season 2', 'Specials']
    for (url, li, is_folder), season in zip(items, (1, 2, 0)):
        assert is_folder is True
        # meta_obj has no explicit 'background'/'logo': fanart falls back to poster
        assert li.art['fanart'] == 'poster.jpg'
        assert li.art['poster'] == 'poster.jpg'
        assert li.info_tag.calls.get('setTvShowTitle') == 'A Show'
        assert li.info_tag.calls.get('setSeason') == season
        assert li.info_tag.calls.get('setMediaType') == 'season'
        assert url == router.url_for('videos', type='series', id='tt1', season=str(season))
    assert ctx.env.content[-1][1] == 'seasons'


# ---------------------------------------------------------------------------
# videos()
# ---------------------------------------------------------------------------


def test_videos_not_found_notifies_and_ends_failed(load_views):
    ctx = load_views()
    views = ctx.views
    _wire_data_layer(views, FakeStore(addons=[]), FakeAddonClient())

    views.videos('series', 'tt1', '1')

    assert ctx.env.notifications[-1][1] == 'STR30030'
    assert ctx.env.end_of_directory[-1]['succeeded'] is False


def test_videos_filters_sorts_episodes_and_forwards_poster_title(load_views):
    ctx = load_views()
    views = ctx.views
    transport = 't1'
    descriptor = {
        'transportUrl': transport,
        'manifest': {'id': 'org.a', 'resources': ['meta'], 'types': ['series'], 'idPrefixes': ['tt']},
    }
    meta_obj = {
        'id': 'tt1', 'name': 'A Show', 'poster': 'show-poster.jpg',
        'videos': [
            {'season': 1, 'episode': 2, 'id': 'ep2', 'title': 'Ep Two',
             'thumbnail': 'ep2-thumb.jpg', 'overview': 'plot2', 'released': '2020-05-02T00:00:00.000Z'},
            {'season': 1, 'episode': 1, 'id': 'ep1', 'title': 'Ep One',
             'overview': 'plot1', 'released': '2020-05-01T00:00:00.000Z'},
            {'season': 2, 'episode': 1, 'id': 'ep-other-season'},
        ],
    }
    client = FakeAddonClient(meta_results={transport: meta_obj})
    _wire_data_layer(views, FakeStore(addons=[descriptor]), client)

    views.videos('series', 'tt1', '1')

    items = ctx.env.directory_items[-1]['items']
    assert len(items) == 2  # season-2 episode excluded

    labels = [li.getLabel() for _, li, _ in items]
    assert labels == ['1x01. Ep One', '1x02. Ep Two']

    url0, li0, is_folder0 = items[0]
    assert is_folder0 is True
    assert li0.art['thumb'] == 'show-poster.jpg'  # no per-episode thumbnail -> falls back to show poster
    assert li0.info_tag.calls.get('setEpisode') == 1
    assert li0.info_tag.calls.get('setFirstAired') == '2020-05-01'
    assert li0.info_tag.calls.get('setTvShowTitle') == 'A Show'
    query0 = dict(parse_qsl(urlparse(url0).query))
    assert query0['poster'] == 'show-poster.jpg'
    assert query0['title'] == '1x01. Ep One'

    _, li1, _ = items[1]
    assert li1.art['thumb'] == 'ep2-thumb.jpg'
    assert li1.info_tag.calls.get('setFirstAired') == '2020-05-02'

    assert ctx.env.content[-1][1] == 'episodes'


def test_videos_unparseable_season_param_matches_only_seasonless_entries(load_views):
    ctx = load_views()
    views = ctx.views
    transport = 't1'
    descriptor = {
        'transportUrl': transport,
        'manifest': {'id': 'org.a', 'resources': ['meta'], 'types': ['series'], 'idPrefixes': ['tt']},
    }
    meta_obj = {
        'id': 'tt1', 'name': 'A Show',
        'videos': [
            {'season': 1, 'episode': 1, 'id': 'ep1'},
            {'episode': 1, 'id': 'no-season', 'title': 'Loose Episode'},
        ],
    }
    client = FakeAddonClient(meta_results={transport: meta_obj})
    _wire_data_layer(views, FakeStore(addons=[descriptor]), client)

    views.videos('series', 'tt1', 'not-a-number')

    items = ctx.env.directory_items[-1]['items']
    assert len(items) == 1
    _, li, _ = items[0]
    assert li.getLabel() == '0x01. Loose Episode'


# ---------------------------------------------------------------------------
# streams() - query extras, per-addon skip/error, empty results
# ---------------------------------------------------------------------------


def test_stream_query_extras_read_from_argv_when_poster_title_omitted(load_views, monkeypatch):
    monkeypatch.setattr(sys, 'argv', ['default.py', '1', '?poster=http%3A%2F%2Fx%2Fp.jpg&title=My+Title'])
    ctx = load_views()
    views = ctx.views
    transport = 't1'
    descriptor = {
        'transportUrl': transport,
        'manifest': {
            'id': 'org.a', 'name': 'Addon A',
            'resources': ['stream'], 'types': ['movie'], 'idPrefixes': ['tt'],
        },
        'flags': {},
    }
    client = FakeAddonClient(stream_results={transport: [STREAM_1080P]})
    _wire_data_layer(views, FakeStore(addons=[descriptor]), client)

    views.streams('movie', 'tt1')

    _, li, _ = ctx.env.directory_items[-1]['items'][0]
    assert li.art['icon'] == 'http://x/p.jpg'
    assert li.info_tag.calls.get('setTitle') == 'My Title'


def test_streams_skips_addon_not_supporting_stream_resource(load_views, monkeypatch):
    monkeypatch.setattr(sys, 'argv', ['default.py'])
    ctx = load_views()
    views = ctx.views
    unsupported_transport = 't-unsupported'
    supported_transport = 't-supported'
    descriptor_unsupported = {
        'transportUrl': unsupported_transport,
        'manifest': {
            'id': 'org.unsupported', 'name': 'Unsupported Addon',
            'resources': ['catalog'], 'types': ['movie'], 'idPrefixes': ['tt'],
        },
        'flags': {},
    }
    descriptor_supported = {
        'transportUrl': supported_transport,
        'manifest': {
            'id': 'org.supported', 'name': 'Supported Addon',
            'resources': ['stream'], 'types': ['movie'], 'idPrefixes': ['tt'],
        },
        'flags': {},
    }
    client = FakeAddonClient(stream_results={
        # would show up in the listing if the resource-support check on line
        # `if not addons_lib.addon_supports(...): continue` were ever skipped
        unsupported_transport: [STREAM_2160P],
        supported_transport: [STREAM_720P],
    })
    _wire_data_layer(views, FakeStore(addons=[descriptor_unsupported, descriptor_supported]), client)

    views.streams('movie', 'tt1234567', poster='p.jpg', title='T')

    items = ctx.env.directory_items[-1]['items']
    assert len(items) == 1
    _, li, _ = items[0]
    assert '720p' in li.getLabel()


def test_streams_addon_error_is_skipped_other_addons_still_listed(load_views, monkeypatch):
    monkeypatch.setattr(sys, 'argv', ['default.py'])
    ctx = load_views()
    views = ctx.views
    failing_transport = 't-failing'
    ok_transport = 't-ok'
    descriptor_failing = {
        'transportUrl': failing_transport,
        'manifest': {
            'id': 'org.failing', 'name': 'Failing Addon',
            'resources': ['stream'], 'types': ['movie'], 'idPrefixes': ['tt'],
        },
        'flags': {},
    }
    descriptor_ok = {
        'transportUrl': ok_transport,
        'manifest': {
            'id': 'org.ok', 'name': 'OK Addon',
            'resources': ['stream'], 'types': ['movie'], 'idPrefixes': ['tt'],
        },
        'flags': {},
    }
    client = FakeAddonClient(stream_results={
        failing_transport: AddonError('addon offline'),
        ok_transport: [STREAM_1080P],
    })
    _wire_data_layer(views, FakeStore(addons=[descriptor_failing, descriptor_ok]), client)

    views.streams('movie', 'tt1234567', poster='p.jpg', title='T')

    items = ctx.env.directory_items[-1]['items']
    assert len(items) == 1
    assert any('addon offline' in msg for msg, _level in ctx.env.log_calls)


def test_streams_no_results_notifies_but_still_ends_successfully(load_views, monkeypatch):
    monkeypatch.setattr(sys, 'argv', ['default.py'])
    ctx = load_views()
    views = ctx.views
    transport = 't1'
    descriptor = {
        'transportUrl': transport,
        'manifest': {
            'id': 'org.a', 'name': 'Addon A',
            'resources': ['stream'], 'types': ['movie'], 'idPrefixes': ['tt'],
        },
        'flags': {},
    }
    client = FakeAddonClient(stream_results={transport: []})
    _wire_data_layer(views, FakeStore(addons=[descriptor]), client)

    views.streams('movie', 'tt1234567', poster='p.jpg', title='T')

    call = ctx.env.directory_items[-1]
    assert call['items'] == []
    assert ctx.env.notifications[-1][1] == 'STR30030'
    assert ctx.env.end_of_directory[-1]['succeeded'] is True


def test_stream_item_size_property_falls_back_to_parsed_text_size(load_views, monkeypatch):
    monkeypatch.setattr(sys, 'argv', ['default.py'])
    ctx = load_views()
    views = ctx.views
    transport = 't1'
    descriptor = {
        'transportUrl': transport,
        'manifest': {
            'id': 'org.a', 'name': 'Addon A',
            'resources': ['stream'], 'types': ['movie'], 'idPrefixes': ['tt'],
        },
        'flags': {},
    }
    stream_no_size_hint = _stream(name='Group Release', title='Interstellar 1080p WEB-DL 4.39 GB')
    client = FakeAddonClient(stream_results={transport: [stream_no_size_hint]})
    _wire_data_layer(views, FakeStore(addons=[descriptor]), client)

    views.streams('movie', 'tt1234567', poster='p.jpg', title='T')

    _, li, _ = ctx.env.directory_items[-1]['items'][0]
    expected_size_bytes = int(round(4.39 * 1024 ** 3))
    assert li.properties.get('size') == str(expected_size_bytes)


# ---------------------------------------------------------------------------
# addons()
# ---------------------------------------------------------------------------


def test_addons_lists_protected_and_removable_entries_with_login_action(load_views):
    ctx = load_views()
    views, compat, router = ctx.views, ctx.compat, ctx.router
    protected_transport = 'https://official.example/manifest.json'
    community_transport = 'https://community.example/manifest.json'
    protected_descriptor = {
        'transportUrl': protected_transport,
        'manifest': {'id': 'org.official', 'name': 'Official', 'version': '1.0.0'},
        'flags': {'protected': True},
    }
    community_descriptor = {
        'transportUrl': community_transport,
        'manifest': {
            'id': 'org.community', 'name': 'Community Addon', 'version': '2.1.0',
            'logo': 'https://community.example/logo.png', 'background': 'https://community.example/bg.png',
            'description': 'A community addon',
        },
        'flags': {},
    }
    _wire_data_layer(
        views, FakeStore(addons=[protected_descriptor, community_descriptor], auth=None), FakeAddonClient(),
    )

    views.addons()

    items = ctx.env.directory_items[-1]['items']
    assert len(items) == 4

    url0, li0, is_folder0 = items[0]
    assert is_folder0 is True
    assert url0 == router.url_for('discover')
    assert 'icon' not in li0.art
    assert li0.art['fanart'] == compat.addon_fanart()

    url1, li1, is_folder1 = items[1]
    assert is_folder1 is False
    assert li1.getLabel() == 'Community Addon v2.1.0'
    assert li1.art['icon'] == 'https://community.example/logo.png'
    assert li1.art['fanart'] == 'https://community.example/bg.png'
    assert li1.info_tag.calls.get('setPlot') == 'A community addon'
    assert url1 == router.url_for('addon_remove', transport=community_transport)
    assert li1.context_menu_items == [('STR30011', 'RunPlugin(%s)' % url1)]

    url2, li2, _ = items[2]
    assert url2 == router.url_for('addon_install')
    assert li2.art['icon'] == 'DefaultAddonNone.png'

    url3, li3, _ = items[3]
    assert li3.getLabel() == 'STR30020'
    assert url3 == router.url_for('login')

    assert ctx.env.content[-1] == (ctx.env.directory_items[-1]['handle'], 'files')


def test_addons_shows_logout_action_with_user_label_when_authenticated(load_views):
    ctx = load_views(localized={30022: 'Logout (%s)'})
    views, router = ctx.views, ctx.router
    descriptor = {
        'transportUrl': 't1', 'manifest': {'id': 'org.a', 'name': 'A', 'version': '1'}, 'flags': {},
    }
    auth = {'authKey': 'abc', 'user': {'email': 'me@example.com'}}
    _wire_data_layer(views, FakeStore(addons=[descriptor], auth=auth), FakeAddonClient())

    views.addons()

    url, li, _ = ctx.env.directory_items[-1]['items'][-1]
    assert li.getLabel() == 'Logout (me@example.com)'
    assert url == router.url_for('logout')


# ---------------------------------------------------------------------------
# addon_install()
# ---------------------------------------------------------------------------


def test_addon_install_cancelled_prompt_is_a_noop(load_views):
    ctx = load_views()  # no dialog_inputs -> Dialog.input() returns ''
    views = ctx.views
    store = FakeStore()
    _wire_data_layer(views, store, FakeAddonClient())

    views.addon_install()

    assert store.installed == []
    assert ctx.env.notifications == []
    assert 'Container.Refresh' not in ctx.env.executed_builtins
    assert ctx.env.end_of_directory[-1]['succeeded'] is True


def test_addon_install_manifest_fetch_failure_notifies_and_does_not_install(load_views):
    ctx = load_views(dialog_inputs=['https://bad.example/manifest.json'])
    views = ctx.views
    store = FakeStore()
    client = FakeAddonClient(manifest_error=AddonError('404'))
    _wire_data_layer(views, store, client)

    views.addon_install()

    assert store.installed == []
    assert ctx.env.notifications[-1][1] == 'STR30014'
    assert 'Container.Refresh' not in ctx.env.executed_builtins


def test_addon_install_manifest_missing_id_notifies_and_does_not_install(load_views):
    ctx = load_views(dialog_inputs=['https://bad2.example/manifest.json'])
    views = ctx.views
    store = FakeStore()
    client = FakeAddonClient(manifest_result={'name': 'No Id Here'})
    _wire_data_layer(views, store, client)

    views.addon_install()

    assert store.installed == []
    assert ctx.env.notifications[-1][1] == 'STR30014'


def test_addon_install_success_persists_notifies_and_refreshes_container(load_views):
    url = 'https://new.example/manifest.json'
    ctx = load_views(dialog_inputs=[url])
    views = ctx.views
    store = FakeStore()
    manifest = {'id': 'org.new', 'name': 'New Addon'}
    client = FakeAddonClient(manifest_result=manifest)
    _wire_data_layer(views, store, client)

    views.addon_install()

    assert store.installed == [(url, manifest)]
    assert client.manifest_calls == [url]
    assert ctx.env.notifications[-1][1] == 'STR30012'
    assert 'Container.Refresh' in ctx.env.executed_builtins
    assert ctx.env.end_of_directory[-1] == {
        'handle': -1, 'succeeded': True, 'updateListing': False, 'cacheToDisc': False,
    }


# ---------------------------------------------------------------------------
# addon_remove()
# ---------------------------------------------------------------------------


def test_addon_remove_without_transport_is_a_noop(load_views):
    ctx = load_views()
    views = ctx.views
    _wire_data_layer(views, FakeStore(), FakeAddonClient())

    views.addon_remove(None)

    assert ctx.env.notifications == []
    assert 'Container.Refresh' not in ctx.env.executed_builtins
    assert ctx.env.end_of_directory[-1]['succeeded'] is True


def test_addon_remove_declined_confirmation_is_a_noop(load_views):
    ctx = load_views(dialog_yesno=[False])
    views = ctx.views
    transport = 'https://x.example/manifest.json'
    store = FakeStore(addons=[{'transportUrl': transport, 'flags': {}}])
    _wire_data_layer(views, store, FakeAddonClient())

    views.addon_remove(transport)

    assert store.get_addons() == [{'transportUrl': transport, 'flags': {}}]
    assert ctx.env.notifications == []
    assert 'Container.Refresh' not in ctx.env.executed_builtins


def test_addon_remove_protected_addon_notifies_failure_but_still_refreshes(load_views):
    ctx = load_views(dialog_yesno=[True])
    views = ctx.views
    transport = 'https://official.example/manifest.json'
    store = FakeStore(addons=[{'transportUrl': transport, 'flags': {'protected': True}}])
    _wire_data_layer(views, store, FakeAddonClient())

    views.addon_remove(transport)

    assert store.get_addons() == [{'transportUrl': transport, 'flags': {'protected': True}}]
    assert 'cannot remove protected addon' in ctx.env.notifications[-1][1]
    assert 'Container.Refresh' in ctx.env.executed_builtins


def test_addon_remove_success_notifies_and_refreshes(load_views):
    ctx = load_views(dialog_yesno=[True])
    views = ctx.views
    transport = 'https://community.example/manifest.json'
    store = FakeStore(addons=[{'transportUrl': transport, 'flags': {}}])
    _wire_data_layer(views, store, FakeAddonClient())

    views.addon_remove(transport)

    assert store.get_addons() == []
    assert ctx.env.notifications[-1][1] == 'STR30013'
    assert 'Container.Refresh' in ctx.env.executed_builtins


# ---------------------------------------------------------------------------
# login() / logout()
# ---------------------------------------------------------------------------


def test_login_cancelled_email_prompt_is_a_noop(load_views):
    ctx = load_views()  # no dialog_inputs -> first input() call returns ''
    views = ctx.views
    store = FakeStore()
    _wire_data_layer(views, store, FakeAddonClient())

    views.login()

    assert ctx.env.dialog_input_prompts == ['STR30020']
    assert store.auth_set_calls == []
    assert 'Container.Refresh' not in ctx.env.executed_builtins


def test_login_cancelled_password_prompt_is_a_noop(load_views):
    ctx = load_views(dialog_inputs=['me@example.com'])
    views = ctx.views
    store = FakeStore()
    _wire_data_layer(views, store, FakeAddonClient())

    views.login()

    assert ctx.env.dialog_input_prompts == ['STR30020', 'STR30020']
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


def test_login_success_merges_protected_addons_with_remote_collection(load_views):
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
    assert store.addons_set_calls == [[protected, remote_new]]
    assert ctx.env.notifications[-1][1] == 'Logged in as me@example.com'
    assert 'Container.Refresh' in ctx.env.executed_builtins


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


def test_logout_without_auth_is_a_noop(load_views):
    ctx = load_views()
    views = ctx.views
    store = FakeStore(auth=None)
    _wire_data_layer(views, store, FakeAddonClient())

    views.logout()

    assert store.auth_set_calls == []
    assert 'Container.Refresh' not in ctx.env.executed_builtins


def test_logout_declined_confirmation_is_a_noop(load_views):
    ctx = load_views(dialog_yesno=[False])
    views = ctx.views
    store = FakeStore(auth={'authKey': 'abc'})
    _wire_data_layer(views, store, FakeAddonClient())

    views.logout()

    assert store.auth_set_calls == []
    assert 'Container.Refresh' not in ctx.env.executed_builtins


def test_logout_clears_auth_even_when_api_call_fails(load_views):
    ctx = load_views(dialog_yesno=[True])
    views = ctx.views
    store = FakeStore(auth={'authKey': 'abc'})
    _wire_data_layer(views, store, FakeAddonClient())
    _wire_api(views, FakeStremioAPI(logout_error=ApiError('network down')))

    views.logout()

    assert store.auth_set_calls == [None]
    assert any('network down' in msg for msg, _level in ctx.env.log_calls)
    assert 'Container.Refresh' in ctx.env.executed_builtins


# ---------------------------------------------------------------------------
# library()
# ---------------------------------------------------------------------------


def test_library_without_auth_is_empty(load_views):
    ctx = load_views()
    views = ctx.views
    _wire_data_layer(views, FakeStore(auth=None), FakeAddonClient())

    views.library()

    call = ctx.env.directory_items[-1]
    assert call['items'] == []
    assert ctx.env.content[-1] == (call['handle'], 'videos')
    assert ctx.env.end_of_directory[-1]['succeeded'] is True


def test_library_lists_entries_filtering_removed_and_mapping_mediatype(load_views):
    ctx = load_views()
    views, router = ctx.views, ctx.router
    auth = {'authKey': 'abc'}
    entries = [
        {'_id': 'tt1', 'name': 'Movie One', 'type': 'movie', 'poster': 'p1.jpg'},
        {'_id': 'tt2', 'name': 'Show One', 'type': 'series', 'background': 'bg2.jpg'},
        {'_id': 'tt3', 'name': 'Gone', 'type': 'movie', 'removed': True},
    ]
    _wire_data_layer(views, FakeStore(auth=auth), FakeAddonClient())
    _wire_api(views, FakeStremioAPI(datastore_result=entries))

    views.library()

    items = ctx.env.directory_items[-1]['items']
    assert len(items) == 2

    url0, li0, is_folder0 = items[0]
    assert is_folder0 is True
    assert li0.getLabel() == 'Movie One'
    assert li0.art['poster'] == 'p1.jpg'
    assert li0.info_tag.calls.get('setMediaType') == 'movie'
    assert url0 == router.url_for('meta', type='movie', id='tt1')

    _, li1, _ = items[1]
    assert li1.art['fanart'] == 'bg2.jpg'
    assert 'poster' not in li1.art
    assert li1.info_tag.calls.get('setMediaType') == 'tvshow'


def test_library_datastore_error_yields_empty_listing(load_views):
    ctx = load_views()
    views = ctx.views
    auth = {'authKey': 'abc'}
    _wire_data_layer(views, FakeStore(auth=auth), FakeAddonClient())
    _wire_api(views, FakeStremioAPI(datastore_error=ApiError('down')))

    views.library()

    assert ctx.env.directory_items[-1]['items'] == []
    assert any('down' in msg for msg, _level in ctx.env.log_calls)


# ---------------------------------------------------------------------------
# _safe_listing() decorator
# ---------------------------------------------------------------------------


def test_safe_listing_decorator_catches_exception_notifies_and_fails(load_views):
    ctx = load_views()
    views = ctx.views

    class ExplodingStore:
        def get_addons(self):
            raise RuntimeError('disk on fire')

    _wire_data_layer(views, ExplodingStore(), FakeAddonClient())

    views.discover()

    assert ctx.env.notifications[-1][1] == 'disk on fire'
    assert ctx.env.end_of_directory[-1] == {
        'handle': -1, 'succeeded': False, 'updateListing': False, 'cacheToDisc': True,
    }
    assert ctx.env.log_calls[-1][1] == 3  # xbmc.LOGERROR
