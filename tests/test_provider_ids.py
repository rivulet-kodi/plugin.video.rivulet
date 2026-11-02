"""Provider lists (Netflix, Disney+, Hulu, HBO...): previews without a
type, and ids no stream or meta addon takes."""
import contextlib

import pytest

from tests.kodistubs import install_kodi_stubs
from tests.test_streamswindow_fetch import _FakeAddonClient, _FakeStore, _wire_data_layer

_RELOAD = (
    'lib.ui.compat', 'lib.ui.dependencies', 'lib.ui.uicommon', 'lib.ui.dialogs', 'lib.ui.player',
    'lib.ui.streamswindow', 'lib.ui.previews', 'lib.ui.detailwindow', 'lib.ui.views',
)


@pytest.fixture
def load():
    with contextlib.ExitStack() as stack:
        def _load():
            return stack.enter_context(install_kodi_stubs(reload=_RELOAD))
        yield _load


# --- previews without a type ---------------------------------------------------


def test_a_preview_without_a_type_takes_the_catalogs():
    from lib.stremio.addons import default_meta_types

    metas = [{'id': 'tt1'}, {'id': 'tt2', 'type': 'series'}, 'junk']
    assert default_meta_types(metas, 'movie') == [{'id': 'tt1', 'type': 'movie'}, {'id': 'tt2', 'type': 'series'}, 'junk']
    assert default_meta_types([{'id': 'tt3'}], '') == [{'id': 'tt3'}]


def test_filling_in_a_type_leaves_the_callers_dicts_alone():
    from lib.stremio.addons import default_meta_types

    original = {'id': 'tmdb:603'}
    metas = [original]
    filled = default_meta_types(metas, 'movie')
    assert filled == [{'id': 'tmdb:603', 'type': 'movie'}]
    assert original == {'id': 'tmdb:603'}
    assert metas == [{'id': 'tmdb:603'}]


# --- the id stream addons are asked for ---------------------------------------------


def test_the_imdb_id_comes_from_the_fields_stremio_uses(load):
    sw = load().streamswindow
    assert sw.imdb_id_for({'id': 'tmdb:603', 'imdb_id': 'tt0133093'}) == 'tt0133093'
    assert sw.imdb_id_for({'id': 'tmdb:603', 'behaviorHints': {'defaultVideoId': 'tt0133093'}}) == 'tt0133093'
    assert sw.imdb_id_for({'id': 'tt0133093'}) == 'tt0133093'
    assert sw.imdb_id_for({'id': 'tmdb:603'}) == ''
    assert sw.imdb_id_for(None) == ''


def test_a_provider_keyed_movie_is_asked_for_by_its_imdb_id(load):
    sw = load().streamswindow
    meta = {'id': 'tmdb:603', 'imdb_id': 'tt0133093'}
    assert sw.stream_alias('tmdb:603', meta) == 'tt0133093'
    assert sw.stream_alias('tt0133093', meta) == ''


def test_a_provider_keyed_episode_maps_to_season_and_episode(load):
    sw = load().streamswindow
    meta = {'id': 'tmdb:1399', 'imdb_id': 'tt0944947', 'videos': [
        {'id': 'tmdb:1399:1:3', 'season': 1, 'episode': 3},
        {'id': 'x-odd', 'season': 2, 'episode': 5},
    ]}
    assert sw.stream_alias('tmdb:1399:1:3', meta) == 'tt0944947:1:3'
    assert sw.stream_alias('x-odd', meta) == 'tt0944947:2:5'
    assert sw.stream_alias('tmdb:1399:4:7', meta) == 'tt0944947:4:7'  # not in the list: its own numbers
    assert sw.stream_alias('tmdb:1399', meta) == 'tt0944947'


def test_imdb_shows_and_anime_ids_are_asked_for_as_they_are(load):
    sw = load().streamswindow
    imdb_show = {'id': 'tt1', 'videos': [{'id': 's1e1', 'season': 1, 'episode': 1}]}
    assert sw.stream_alias('s1e1', imdb_show) == ''
    assert sw.stream_alias('kitsu:123:5', {'id': 'kitsu:123', 'imdb_id': 'tt9'}) == ''


def test_each_addon_gets_the_id_it_takes(load):
    ctx = load()
    sw = ctx.streamswindow
    imdb_only = {'transportUrl': 'tt-addon', 'manifest': {
        'name': 'Torrents', 'resources': ['stream'], 'types': ['movie'], 'idPrefixes': ['tt']}}
    tmdb_only = {'transportUrl': 'tmdb-addon', 'manifest': {
        'name': 'TMDB only', 'resources': ['stream'], 'types': ['movie'], 'idPrefixes': ['tmdb:']}}
    both = {'transportUrl': 'both', 'manifest': {
        'name': 'Both', 'resources': ['stream'], 'types': ['movie'], 'idPrefixes': ['tt', 'tmdb:']}}
    client = _FakeAddonClient({'tt-addon': [], 'tmdb-addon': [], 'both': []})
    _wire_data_layer(sw, _FakeStore(addons=[imdb_only, tmdb_only, both]), client)
    targets = sw._stream_targets('movie', 'tmdb:603', {'id': 'tmdb:603', 'imdb_id': 'tt0133093'})
    assert [(d['transportUrl'], rid) for d, _m, rid in targets] == [
        ('tt-addon', 'tt0133093'), ('tmdb-addon', 'tmdb:603'), ('both', 'tt0133093'),
    ]
    results = sw._start_stream_fetch_workers('movie', 'tmdb:603', targets)
    for _ in targets:
        results.get(timeout=5)
    assert sorted(call[2] for call in client.calls) == ['tmdb:603', 'tt0133093', 'tt0133093']


def test_without_an_imdb_id_nothing_changes(load):
    sw = load().streamswindow
    imdb_only = {'transportUrl': 'tt-addon', 'manifest': {
        'name': 'Torrents', 'resources': ['stream'], 'types': ['movie'], 'idPrefixes': ['tt']}}
    _wire_data_layer(sw, _FakeStore(addons=[imdb_only]), _FakeAddonClient({'tt-addon': []}))
    assert sw._stream_targets('movie', 'tmdb:603', {'id': 'tmdb:603'}) == []


# --- opening a title whose id no meta addon resolves ---------------------------------


def test_search_match_needs_the_same_name_type_and_year():
    from lib.ui.previews import best_match

    preview = {'name': 'The Matrix', 'type': 'movie', 'releaseInfo': '1999'}
    found = [
        {'id': 'tt30849200', 'type': 'movie', 'name': 'The Matrix Collection'},
        {'id': 'tt0234215', 'type': 'movie', 'name': 'The Matrix Reloaded', 'releaseInfo': '2003'},
        {'id': 'tt9999999', 'type': 'series', 'name': 'The Matrix'},
        {'id': 'tt0133093', 'type': 'movie', 'name': 'The Matrix', 'releaseInfo': '1999'},
    ]
    assert best_match(preview, found, 'movie') == 'tt0133093'
    assert best_match(dict(preview, releaseInfo='2021'), found, 'movie') == ''
    assert best_match({'name': ''}, found, 'movie') == ''


def test_previews_are_remembered_by_id(load):
    from lib.ui import previews

    previews.remember({'id': 'tmdb:603', 'name': 'The Matrix', 'type': 'movie', 'poster': 'p.jpg', 'genres': ['x']})
    assert previews.lookup('tmdb:603') == {'id': 'tmdb:603', 'name': 'The Matrix', 'type': 'movie', 'poster': 'p.jpg'}
    assert previews.lookup('nope') is None


def test_open_detail_falls_back_to_the_imdb_id(load, monkeypatch):
    ctx = load()
    import lib.ui.detailwindow as detailwindow
    import lib.ui.streamswindow as streamswindow
    import lib.ui.views as views
    from lib.ui import previews

    previews.remember({'id': 'tmdb:603', 'name': 'The Matrix', 'imdb_id': 'tt0133093'})
    asked = []
    movie = {'id': 'tt0133093', 'type': 'movie', 'name': 'The Matrix'}
    monkeypatch.setattr(views, '_fetch_meta', lambda stype, sid, **kw: asked.append((stype, sid)) or (movie if sid.startswith('tt') else None))
    opened = []
    monkeypatch.setattr(streamswindow, 'open_streams', lambda stype, sid, **kw: opened.append((stype, sid)) or False)
    detailwindow.open_detail('', 'tmdb:603')
    # No type on the preview: a movie is tried, then a series; the IMDb id opens it.
    assert asked == [('movie', 'tmdb:603'), ('series', 'tmdb:603'), ('movie', 'tt0133093')]
    assert opened == [('movie', 'tt0133093')]
    assert 'STR30030' not in [n[1] for n in ctx.env.notifications]


def test_a_movie_nobody_has_a_meta_for_opens_its_streams_from_the_preview(load, monkeypatch):
    """What stock Rivulet does with a catalog movie."""
    load()
    import lib.ui.detailwindow as detailwindow
    import lib.ui.streamswindow as streamswindow
    import lib.ui.views as views
    from lib.ui import previews

    previews.remember({'id': 'tt0133093', 'type': 'movie', 'name': 'The Matrix', 'poster': 'p.jpg'})
    monkeypatch.setattr(views, '_fetch_meta', lambda stype, sid, **kw: None)
    opened = []
    monkeypatch.setattr(streamswindow, 'open_streams', lambda stype, sid, **kw: opened.append((stype, sid, kw['meta']['name'])) or False)
    detailwindow.open_detail('movie', 'tt0133093')
    assert opened == [('movie', 'tt0133093', 'The Matrix')]


def test_a_title_listed_as_the_wrong_type_still_opens(load, monkeypatch):
    load()
    import lib.ui.detailwindow as detailwindow
    import lib.ui.views as views

    show = {'id': 'tt0944947', 'type': 'series', 'name': 'GoT', 'videos': []}
    monkeypatch.setattr(views, '_fetch_meta', lambda stype, sid, **kw: show if stype == 'series' else None)
    assert detailwindow._fetch_meta_any_type('Movies', 'tt0944947') == (show, 'series')


def test_the_swapped_type_is_tried_after_a_quick_miss(load, monkeypatch):
    load()
    import lib.ui.detailwindow as detailwindow
    import lib.ui.views as views

    show = {'id': 'tt0944947', 'type': 'series', 'name': 'GoT', 'videos': []}
    asked = []
    monkeypatch.setattr(views, '_fetch_meta', lambda stype, sid, **kw: asked.append(stype) or (show if stype == 'series' else None))
    assert detailwindow._fetch_meta_any_type('movie', 'tt0944947') == (show, 'series')
    assert asked == ['movie', 'series']


def test_the_swapped_type_is_skipped_after_a_slow_miss(load, monkeypatch):
    """A movie lookup that timed out across addons is not repeated as a series."""
    load()
    import types

    import lib.ui.detailwindow as detailwindow
    import lib.ui.views as views

    clock = {'now': 0.0}
    monkeypatch.setattr(detailwindow, 'time', types.SimpleNamespace(monotonic=lambda: clock['now']))
    asked = []

    def _slow_miss(stype, sid, **kw):
        asked.append(stype)
        clock['now'] += detailwindow._SWAPPED_TYPE_MAX_SECONDS + 1
        return None

    monkeypatch.setattr(views, '_fetch_meta', _slow_miss)
    assert detailwindow._fetch_meta_any_type('movie', 'tt0944947') == (None, 'movie')
    assert asked == ['movie']


def test_a_failing_imdb_lookup_is_logged_not_swallowed(load, monkeypatch):
    ctx = load()
    import xbmc

    import lib.ui.detailwindow as detailwindow
    import lib.ui.searchwindow as searchwindow

    def _boom(*args, **kwargs):
        raise RuntimeError('https://secret.example/token/manifest.json')

    import lib.ui.dependencies as dependencies

    monkeypatch.setattr(dependencies, 'get_store', lambda: object())
    monkeypatch.setattr(dependencies, 'get_client', lambda: object())
    monkeypatch.setattr(searchwindow, 'run_query', _boom)
    assert detailwindow._imdb_alternative('movie', 'tmdb:603', {'id': 'tmdb:603', 'name': 'The Matrix'}) == ''
    warnings = [msg for msg, level in ctx.env.log_calls if level == xbmc.LOGWARNING]
    assert any('IMDb lookup by search for movie/tmdb:603 failed: RuntimeError' in m for m in warnings)
    assert not any('secret.example' in m for m in warnings)


# --- streams: learning the IMDb id before asking IMDb-only addons -----------


_IMDB_ONLY = {'transportUrl': 'tt-addon', 'manifest': {
    'name': 'Torrents', 'resources': ['stream'], 'types': ['movie'], 'idPrefixes': ['tt']}}


def test_a_fetched_imdb_id_lets_an_imdb_only_addon_be_asked(load, monkeypatch):
    ctx = load()
    sw = ctx.streamswindow
    import lib.ui.uicommon as uicommon
    import lib.ui.views as views

    _wire_data_layer(sw, _FakeStore(addons=[_IMDB_ONLY]), _FakeAddonClient({}))
    monkeypatch.setattr(views, '_fetch_meta', lambda stype, sid, **kw: {'id': 'tt0133093', 'type': 'movie'})
    monkeypatch.setattr(uicommon, 'run_cancellable', lambda callback, _heading: callback())
    preview = {'id': 'tmdb:603', 'name': 'The Matrix'}

    assert sw._stream_targets('movie', 'tmdb:603', preview) == []
    merged = sw._meta_with_imdb('movie', 'tmdb:603', preview)
    assert merged['imdb_id'] == 'tt0133093'
    assert preview == {'id': 'tmdb:603', 'name': 'The Matrix'}  # the caller's preview is not changed
    assert [entry[2] for entry in sw._stream_targets('movie', 'tmdb:603', merged)] == ['tt0133093']


def test_back_on_the_imdb_lookup_cancels_the_stream_lookup_too(load, monkeypatch):
    """Back on the IMDb spinner used to mean "carry on": a second spinner for
    the stream fetch opened and the user needed a second Back."""
    ctx = load()
    sw = ctx.streamswindow
    import lib.ui.uicommon as uicommon

    preview = {'id': 'tmdb:603', 'name': 'The Matrix'}
    monkeypatch.setattr(uicommon, 'run_cancellable', lambda *_args: uicommon.CANCELLED)
    assert sw._meta_with_imdb('movie', 'tmdb:603', preview) is uicommon.CANCELLED

    monkeypatch.setattr(sw, '_stream_targets', lambda *a, **k: pytest.fail('went on to the stream lookup'))
    assert sw.open_streams('movie', 'tmdb:603', meta=preview) is False


def test_a_failing_imdb_meta_lookup_keeps_the_preview_and_is_logged(load, monkeypatch):
    ctx = load()
    sw = ctx.streamswindow
    import xbmc

    import lib.ui.uicommon as uicommon

    def _boom(*_args):
        raise RuntimeError('https://secret.example/token/manifest.json')

    preview = {'id': 'tmdb:603', 'name': 'The Matrix'}
    monkeypatch.setattr(uicommon, 'run_cancellable', _boom)
    assert sw._meta_with_imdb('movie', 'tmdb:603', preview) is preview
    warnings = [msg for msg, level in ctx.env.log_calls if level == xbmc.LOGWARNING]
    assert any('IMDb meta lookup for movie/tmdb:603 failed: RuntimeError' in m for m in warnings)
    assert not any('secret.example' in m for m in warnings)


def test_an_imdb_keyed_title_needs_no_lookup(load, monkeypatch):
    ctx = load()
    sw = ctx.streamswindow
    import lib.ui.uicommon as uicommon

    monkeypatch.setattr(uicommon, 'run_cancellable', lambda *_args: pytest.fail('looked up'))
    meta = {'id': 'tt0133093'}
    assert sw._meta_with_imdb('movie', 'tt0133093', meta) is meta


def _merged_with(ctx, monkeypatch, preview, full):
    sw = ctx.streamswindow
    import lib.ui.uicommon as uicommon
    import lib.ui.views as views

    monkeypatch.setattr(views, '_fetch_meta', lambda stype, sid, **kw: full)
    monkeypatch.setattr(uicommon, 'run_cancellable', lambda callback, _heading: callback())
    return sw._meta_with_imdb('movie', 'tmdb:603', preview)


def test_an_empty_imdb_id_on_the_preview_does_not_hide_the_full_metas(load, monkeypatch):
    """CodeRabbit on #68: ``imdb_id: null`` blocked a provider-keyed meta's IMDb id."""
    ctx = load()
    preview = {'id': 'tmdb:603', 'name': 'The Matrix', 'imdb_id': None}
    merged = _merged_with(ctx, monkeypatch, preview, {'id': 'tmdb:603', 'imdb_id': 'tt0133093'})
    assert merged['imdb_id'] == 'tt0133093'
    assert ctx.streamswindow.stream_alias('tmdb:603', merged) == 'tt0133093'
    assert preview['imdb_id'] is None


def test_empty_preview_hints_take_the_full_metas(load, monkeypatch):
    ctx = load()
    preview = {'id': 'tmdb:603', 'behaviorHints': {}}
    full = {'id': 'tmdb:603', 'behaviorHints': {'defaultVideoId': 'tt0133093', 'hasScheduledVideos': False}}
    merged = _merged_with(ctx, monkeypatch, preview, full)
    assert merged['behaviorHints'] == full['behaviorHints']
    assert merged['behaviorHints'] is not full['behaviorHints']
    assert ctx.streamswindow.imdb_id_for(merged) == 'tt0133093'


def test_the_default_video_id_joins_the_previews_own_hints(load, monkeypatch):
    ctx = load()
    preview = {'id': 'tmdb:603', 'behaviorHints': {'bingeGroup': 'matrix'}}
    full = {'id': 'tmdb:603', 'behaviorHints': {'defaultVideoId': 'tt0133093', 'bingeGroup': 'other'}}
    merged = _merged_with(ctx, monkeypatch, preview, full)
    assert merged['behaviorHints'] == {'bingeGroup': 'matrix', 'defaultVideoId': 'tt0133093'}
    assert preview['behaviorHints'] == {'bingeGroup': 'matrix'}


def test_the_previews_own_imdb_fields_are_kept(load, monkeypatch):
    ctx = load()
    preview = {'id': 'tmdb:603', 'imdbId': 'x', 'behaviorHints': {'defaultVideoId': 'kept'}}
    full = {'id': 'tmdb:603', 'imdbId': 'tt0133093', 'behaviorHints': {'defaultVideoId': 'tt0133093'}}
    merged = _merged_with(ctx, monkeypatch, preview, full)
    assert merged['imdbId'] == 'x'
    assert merged['behaviorHints'] == {'defaultVideoId': 'kept'}


def test_a_tv_listed_show_is_found_by_search_as_a_series(load, monkeypatch):
    """A provider list that calls its shows "tv": the search answers "series"."""
    load()
    import lib.ui.dependencies as dependencies
    import lib.ui.detailwindow as detailwindow
    import lib.ui.searchwindow as searchwindow

    monkeypatch.setattr(dependencies, 'get_store', lambda: object())
    monkeypatch.setattr(dependencies, 'get_client', lambda: object())
    monkeypatch.setattr(searchwindow, 'run_query', lambda *a, **kw: [
        {'id': 'tt0944947', 'type': 'series', 'name': 'Game of Thrones', 'releaseInfo': '2011\u20132019'},
    ])
    preview = {'id': 'tmdb:1399', 'type': 'tv', 'name': 'Game of Thrones', 'releaseInfo': '2011'}
    assert detailwindow._imdb_alternative('tv', 'tmdb:1399', preview) == 'tt0944947'


def test_the_imdb_id_is_learned_even_when_an_addon_takes_the_provider_id(load, monkeypatch):
    """An addon that also takes ``tmdb:`` ids must not keep the IMDb-only
    ones (Torrentio and the like) out of the stream request."""
    ctx = load()
    sw = ctx.streamswindow

    tmdb_too = {'transportUrl': 'tmdb-addon', 'manifest': {
        'name': 'Debrid', 'resources': ['stream'], 'types': ['movie'], 'idPrefixes': ['tt', 'tmdb:']}}
    _wire_data_layer(sw, _FakeStore(addons=[tmdb_too, _IMDB_ONLY]), _FakeAddonClient({}))
    looked_up = []

    def _with_imdb(stype, sid, meta):
        looked_up.append((stype, sid))
        return dict(meta, imdb_id='tt0133093')

    monkeypatch.setattr(sw, '_meta_with_imdb', _with_imdb)
    asked = []
    real_targets = sw._stream_targets

    def _targets(stype, sid, meta=None):
        found = real_targets(stype, sid, meta)
        asked.extend((entry[1]['name'], entry[2]) for entry in found)
        return []

    monkeypatch.setattr(sw, '_stream_targets', _targets)
    assert sw.open_streams('movie', 'tmdb:603', meta={'id': 'tmdb:603', 'name': 'The Matrix'}) is False
    assert looked_up == [('movie', 'tmdb:603')]
    assert asked == [('Debrid', 'tt0133093'), ('Torrents', 'tt0133093')]


# --- a series from a provider list, end to end ------------------------------


_GOT = {'id': 'tt0944947', 'type': 'series', 'name': 'Game of Thrones', 'videos': [
    {'id': 'tt0944947:1:1', 'season': 1, 'episode': 1, 'title': 'Winter Is Coming'},
]}


def test_a_provider_lists_empty_meta_does_not_hide_cinemetas_series(load, monkeypatch):
    """A list addon that also declares ``meta`` answers ``{"meta": {}}``
    for a show at once; Cinemeta answers later with the episodes. The
    empty answer used to win, no meta was found and the series said "No
    streams found" (a movie still played, straight from the list)."""
    ctx = load()
    import threading

    import lib.ui.detailwindow as detailwindow
    import lib.ui.views as views
    from lib.ui import previews

    empty_sent = threading.Event()

    class _Client:
        def meta(self, transport_url, stype, sid):
            if transport_url == 'lists':
                empty_sent.set()
                return {}
            assert empty_sent.wait(2)
            return dict(_GOT) if (stype, sid) == ('series', 'tt0944947') else None

    def _addon(url, name, types):
        return {'transportUrl': url, 'manifest': {
            'name': name, 'resources': ['catalog', 'meta'], 'types': types, 'idPrefixes': ['tt']}}

    store = _FakeStore(addons=[_addon('cinemeta', 'Cinemeta', ['movie', 'series']),
                               _addon('lists', 'Streaming Catalogs', ['movie', 'series'])])
    monkeypatch.setattr(views, 'get_store', lambda: store)
    monkeypatch.setattr(views, 'get_client', lambda: _Client())
    previews.remember({'id': 'tt0944947', 'type': 'series', 'name': 'Game of Thrones'})

    started = []

    class _Window:
        def start(self, meta, stype, **kw):
            started.append((meta['id'], stype, len(meta['videos'])))
            return False

        def close(self):
            pass

    monkeypatch.setattr(detailwindow, 'open_window', lambda *_args: _Window())
    detailwindow.open_detail('series', 'tt0944947')
    assert started == [('tt0944947', 'series', 1)]
    assert 'STR30030' not in [n[1] for n in ctx.env.notifications]
    logged = [msg for msg, _level in ctx.env.log_calls]
    assert any('detailwindow: opening series/tt0944947 from the list (list type series' in m for m in logged)
    assert any('views._fetch_meta: asking 2 addon(s) for series/tt0944947: Cinemeta, Streaming Catalogs' in m
               for m in logged)
    assert any('views._fetch_meta: Streaming Catalogs answered series/tt0944947 with a meta without an id' in m
               for m in logged)
