# SPDX-FileCopyrightText: 2026 M0Rf30
# SPDX-License-Identifier: MIT

"""First catalog page through the slim cache (#13)."""
import contextlib

import pytest

from tests.kodistubs import install_kodi_stubs


@pytest.fixture
def pages():
    with contextlib.ExitStack() as stack:
        ctx = stack.enter_context(install_kodi_stubs(reload=(
            'lib.ui.compat', 'lib.ui.views', 'lib.ui.catalogpages',
        )))
        yield ctx


def _cache(tmp_path, clock):
    from lib.stremio.pagecache import PageCache

    return PageCache(str(tmp_path / 'catalog_pages.json'), clock=lambda: clock['now'])


def test_an_expired_page_paints_and_is_revalidated(pages, tmp_path, monkeypatch):
    module = pages.catalogpages
    clock = {'now': 0.0}
    cache = _cache(tmp_path, clock)
    catalog = {'type': 'movie', 'id': 'top'}
    from lib.stremio.pagecache import cache_key

    key = cache_key('https://addon.test/manifest.json', 'movie', 'top', None)
    cache.store(key, [{'id': 'tt1', 'type': 'movie', 'name': 'One'}])
    clock['now'] = 8000.0
    monkeypatch.setattr(module, 'get_page_cache', lambda: cache)
    fetched = []
    monkeypatch.setattr(module, '_fetch_first_page', lambda *args: fetched.append(args) or [])
    revalidated = []
    monkeypatch.setattr(module, '_schedule_revalidate', lambda *args: revalidated.append(args[-1]))

    metas = module.cached_first_page('https://addon.test/manifest.json', {}, catalog)

    assert [meta['id'] for meta in metas] == ['tt1']
    assert fetched == []
    assert revalidated == [key]


def test_deferred_saves_write_the_file_once(pages, tmp_path, monkeypatch):
    module = pages.catalogpages
    clock = {'now': 0.0}
    cache = _cache(tmp_path, clock)
    saves = []
    real_save = cache.save
    monkeypatch.setattr(cache, 'save', lambda: saves.append(1) or real_save())
    monkeypatch.setattr(module, 'get_page_cache', lambda: cache)
    monkeypatch.setattr(
        module, '_fetch_first_page',
        lambda transport, manifest, catalog, extra: [{'id': catalog['id'], 'type': 'movie', 'name': 'x'}],
    )

    with module.deferred_saves():
        for cid in ('a', 'b', 'c'):
            module.cached_first_page('https://addon.test/manifest.json', {}, {'type': 'movie', 'id': cid})
        assert saves == []
    assert saves == [1]

    module.cached_first_page('https://addon.test/manifest.json', {}, {'type': 'movie', 'id': 'd'})
    assert saves == [1, 1]


def test_a_cached_page_follows_the_current_hide_adult_setting(pages, tmp_path, monkeypatch):
    """Pages are stored as the addon sent them and filtered on the way
    out: a page cached while hide-adult was off loses its adult item as
    soon as the setting is turned on, without waiting for it to expire."""
    module = pages.catalogpages
    clock = {'now': 0.0}
    cache = _cache(tmp_path, clock)
    monkeypatch.setattr(module, 'get_page_cache', lambda: cache)
    page = [
        {'id': 'tt1', 'type': 'movie', 'name': 'Family Film'},
        {'id': 'tt2', 'type': 'movie', 'name': 'Late Night', 'adult': True},
    ]
    fetched = []
    monkeypatch.setattr(module, '_fetch_first_page', lambda *args: fetched.append(1) or [dict(m) for m in page])
    monkeypatch.setattr(module, '_schedule_revalidate', lambda *args: None)
    catalog = {'type': 'movie', 'id': 'top'}

    pages.env.addon.settings['home_hide_adult'] = 'false'
    shown = module.cached_first_page('https://addon.test/manifest.json', {}, catalog)
    assert [meta['id'] for meta in shown] == ['tt1', 'tt2']

    pages.env.addon.settings['home_hide_adult'] = 'true'
    shown = module.cached_first_page('https://addon.test/manifest.json', {}, catalog)
    assert [meta['id'] for meta in shown] == ['tt1']
    assert fetched == [1]  # the second read came from the cache

    from lib.stremio.pagecache import cache_key

    _state, stored = cache.lookup(cache_key('https://addon.test/manifest.json', 'movie', 'top', None), allow_expired=True)
    assert [meta['id'] for meta in stored] == ['tt1', 'tt2']  # kept unfiltered on disk


class _DirStore:
    def __init__(self, data_dir):
        self.data_dir = data_dir


def test_the_page_cache_lives_in_the_store_folder_and_is_shared(pages, tmp_path, monkeypatch):
    module = pages.catalogpages

    import lib.ui.dependencies as dependencies

    monkeypatch.setattr(dependencies, 'get_store', lambda: _DirStore(str(tmp_path)))
    module._caches.clear()
    first = module.get_page_cache()
    assert first.path == str(tmp_path / 'catalog_pages.json')
    assert module.get_page_cache() is first
    # A store with no folder (the test fakes) has no cache at all.
    monkeypatch.setattr(dependencies, 'get_store', lambda: _DirStore(''))
    assert module.get_page_cache() is None
    module._caches.clear()


def test_with_no_cache_every_call_fetches(pages, monkeypatch):
    module = pages.catalogpages
    monkeypatch.setattr(module, 'get_page_cache', lambda: None)
    fetched = []
    monkeypatch.setattr(module, '_fetch_first_page',
                        lambda *args: fetched.append(1) or [{'id': 'tt1', 'type': 'movie'}])
    assert module.cached_first_page('https://addon.test/manifest.json', {}, {'type': 'movie', 'id': 'top'})[0]['id'] == 'tt1'
    module.cached_first_page('https://addon.test/manifest.json', {}, {'type': 'movie', 'id': 'top'})
    assert fetched == [1, 1]


def test_a_fresh_page_is_served_without_a_fetch_and_gets_its_type(pages, tmp_path, monkeypatch):
    module = pages.catalogpages
    clock = {'now': 0.0}
    cache = _cache(tmp_path, clock)
    from lib.stremio.pagecache import cache_key

    cache.store(cache_key('https://addon.test/manifest.json', 'series', 'top', None), [{'id': 'tt9', 'name': 'Typeless'}])
    monkeypatch.setattr(module, 'get_page_cache', lambda: cache)
    monkeypatch.setattr(module, '_fetch_first_page', lambda *args: pytest.fail('fetched a fresh page'))
    metas = module.cached_first_page('https://addon.test/manifest.json', {}, {'type': 'series', 'id': 'top'})
    assert metas == [{'id': 'tt9', 'name': 'Typeless', 'type': 'series'}]


class _Client:
    def __init__(self, metas):
        self.calls = []
        self._metas = metas

    def catalog(self, transport, ctype, cid, extra=None):
        self.calls.append((transport, ctype, cid, extra))
        return list(self._metas)


def test_fetch_keeps_raw_adult_titles_for_read_time_filtering(pages, monkeypatch):
    module = pages.catalogpages

    client = _Client([{'id': 'a', 'name': 'A'}, 'junk', {'id': 'b', 'adult': True}])
    monkeypatch.setattr(pages.views, 'get_client', lambda: client)
    pages.env.addon.settings['home_hide_adult'] = 'true'
    metas = module._fetch_first_page('https://addon.test/manifest.json', {}, {'type': 'movie', 'id': 'top'}, [('genre', 'x')])
    assert [meta['id'] for meta in metas] == ['a', 'b']
    assert client.calls == [('https://addon.test/manifest.json', 'movie', 'top', [('genre', 'x')])]


def test_an_adult_catalog_is_not_requested_while_hidden(pages, monkeypatch):
    module = pages.catalogpages

    client = _Client([{'id': 'a'}])
    monkeypatch.setattr(pages.views, 'get_client', lambda: client)
    adult = {'type': 'movie', 'id': 'xxx', 'name': 'Porn'}
    pages.env.addon.settings['home_hide_adult'] = 'true'
    assert module._fetch_first_page('https://addon.test/manifest.json', {}, adult, None) == []
    assert client.calls == []
    pages.env.addon.settings['home_hide_adult'] = 'false'
    assert module._fetch_first_page('https://addon.test/manifest.json', {}, adult, None)[0]['id'] == 'a'


def test_fetch_reads_only_the_first_catalog_page(pages, monkeypatch):
    module = pages.catalogpages

    monkeypatch.setattr(pages.views, 'get_client', lambda: object())
    seen = []

    def first(transport, ctype, cid, extra=None):
        seen.append((ctype, cid))
        return [{'id': 'a'}, None]
    monkeypatch.setattr(pages.views, '_fetch_catalog', first)
    assert module._fetch_first_page('https://addon.test/manifest.json', {}, {'type': 'movie', 'id': 'top'}, None) == [{'id': 'a'}]
    monkeypatch.setattr(pages.views, '_fetch_catalog', lambda *a, **k: [])
    assert module._fetch_first_page('https://addon.test/manifest.json', {}, {'type': 'movie', 'id': 'top'}, None) == []
    assert seen == [('movie', 'top')]


def test_revalidate_stores_the_new_page_and_ignores_a_failure(pages, tmp_path, monkeypatch):
    module = pages.catalogpages
    cache = _cache(tmp_path, {'now': 0.0})
    monkeypatch.setattr(module, 'get_page_cache', lambda: cache)
    monkeypatch.setattr(module, '_fetch_first_page', lambda *args: [{'id': 'new'}])
    module._revalidate('https://addon.test/manifest.json', {}, {'type': 'movie', 'id': 'top'}, None, 'key')
    assert cache.lookup('key', now=1) == ('fresh', [{'id': 'new'}])
    assert (tmp_path / 'catalog_pages.json').exists()

    def boom(*args):
        raise RuntimeError('addon down')

    monkeypatch.setattr(module, '_fetch_first_page', boom)
    module._revalidate('https://addon.test/manifest.json', {}, {'type': 'movie', 'id': 'top'}, None, 'key')
    assert cache.lookup('key', now=1) == ('fresh', [{'id': 'new'}])
    # No cache to write to: the fetched page is dropped quietly.
    monkeypatch.setattr(module, '_fetch_first_page', lambda *args: [{'id': 'other'}])
    monkeypatch.setattr(module, 'get_page_cache', lambda: None)
    module._revalidate('https://addon.test/manifest.json', {}, {'type': 'movie', 'id': 'top'}, None, 'key')


def test_one_revalidation_per_page_at_a_time(pages, monkeypatch):
    import threading

    module = pages.catalogpages
    gate = threading.Event()
    calls = []

    entered, finished = threading.Event(), threading.Event()
    def slow(*args):
        calls.append(args[-2])
        entered.set()
        assert gate.wait(5)
        finished.set()
    monkeypatch.setattr(module, '_revalidate', slow)
    module._schedule_revalidate('https://addon.test/manifest.json', {}, {}, None, 'k1')
    worker = module._revalidate_worker
    assert entered.wait(5)
    module._schedule_revalidate('https://addon.test/manifest.json', {}, {}, None, 'k1')
    gate.set()
    assert finished.wait(5)
    module.stop()
    worker.join(5)
    assert calls == ['k1'] and not worker.is_alive()
    assert 'k1' not in module._revalidate_inflight


def test_a_failed_deferred_save_does_not_escape(pages, tmp_path, monkeypatch):
    module = pages.catalogpages
    cache = _cache(tmp_path, {'now': 0.0})

    def broken():
        raise OSError('read-only')

    monkeypatch.setattr(cache, 'save', broken)
    monkeypatch.setattr(module, 'get_page_cache', lambda: cache)
    monkeypatch.setattr(module, '_fetch_first_page', lambda *args: [{'id': 'a'}])
    with module.deferred_saves():
        module.cached_first_page('https://addon.test/manifest.json', {}, {'type': 'movie', 'id': 'top'})
    assert module._defer == {'depth': 0, 'dirty': False}


def test_a_hidden_adult_catalog_shows_again_as_soon_as_hiding_is_off(pages, tmp_path, monkeypatch):
    """An adult catalog read while hide-adult is on is answered empty and
    nothing is cached for it, so turning the setting off fetches it at
    once instead of serving that empty page until it expires."""
    module = pages.catalogpages
    from lib.stremio.pagecache import cache_key

    cache = _cache(tmp_path, {'now': 0.0})
    monkeypatch.setattr(module, 'get_page_cache', lambda: cache)
    client = _Client([{'id': 'a', 'type': 'movie', 'name': 'A'}, {'id': 'b', 'type': 'movie', 'name': 'B'}])
    monkeypatch.setattr(pages.views, 'get_client', lambda: client)
    adult = {'type': 'movie', 'id': 'xxx', 'name': 'Porn'}
    key = cache_key('https://addon.test/manifest.json', 'movie', 'xxx', None)

    pages.env.addon.settings['home_hide_adult'] = 'true'
    assert module.cached_first_page('https://addon.test/manifest.json', {}, adult) == []
    assert client.calls == []
    assert cache.lookup(key, allow_expired=True) == ('miss', None)
    assert not (tmp_path / 'catalog_pages.json').exists()

    pages.env.addon.settings['home_hide_adult'] = 'false'
    shown = module.cached_first_page('https://addon.test/manifest.json', {}, adult)
    assert [meta['id'] for meta in shown] == ['a', 'b']
    assert len(client.calls) == 1
    assert [meta['id'] for meta in cache.lookup(key)[1]] == ['a', 'b']

    # Hiding again: empty, and the page cached while it was off is kept.
    pages.env.addon.settings['home_hide_adult'] = 'true'
    assert module.cached_first_page('https://addon.test/manifest.json', {}, adult) == []
    assert [meta['id'] for meta in cache.lookup(key)[1]] == ['a', 'b']


def test_a_revalidation_that_lands_after_hiding_is_on_keeps_the_cached_page(pages, tmp_path, monkeypatch):
    module = pages.catalogpages
    cache = _cache(tmp_path, {'now': 0.0})
    monkeypatch.setattr(module, 'get_page_cache', lambda: cache)
    cache.store('key', [{'id': 'a'}])
    adult = {'type': 'movie', 'id': 'xxx', 'name': 'Porn'}

    def _fetch(*args):
        # The setting is turned on while the request is out.
        pages.env.addon.settings['home_hide_adult'] = 'true'
        return []

    monkeypatch.setattr(module, '_fetch_first_page', _fetch)
    pages.env.addon.settings['home_hide_adult'] = 'false'
    module._revalidate('https://addon.test/manifest.json', {}, adult, None, 'key')
    assert cache.lookup('key', now=1) == ('fresh', [{'id': 'a'}])


@pytest.mark.parametrize('method', ['store', 'save'])
def test_a_cache_write_failure_keeps_the_loaded_row(pages, tmp_path, monkeypatch, method):
    module = pages.catalogpages
    cache = _cache(tmp_path, {'now': 0.0})
    monkeypatch.setattr(module, 'get_page_cache', lambda: cache)
    monkeypatch.setattr(module, '_fetch_first_page', lambda *args: [{'id': 'loaded'}])

    def fail(*args):
        raise OSError('https://user:secret@host/private')

    monkeypatch.setattr(cache, method, fail)
    assert module.cached_first_page('t', {}, {'type': 'movie', 'id': 'top'}) == [{'id': 'loaded'}]
    module._revalidate('t', {}, {'type': 'movie', 'id': 'top'}, None, 'k')
    messages = [msg for msg, _ in pages.env.log_calls if 'cache save failed' in msg]
    assert len(messages) == 2
    assert all('OSError' in msg and 'secret' not in msg for msg in messages)


def test_home_later_page_lru_reuses_raw_pages_and_has_a_200_page_bound(pages, monkeypatch):
    module = pages.catalogpages
    module.clear_later_pages()
    calls = []
    monkeypatch.setattr(pages.views, '_fetch_catalog', lambda *args, **kw: calls.append(kw['extra']) or [{'id': 'tt1'}])
    for _ in range(2):
        assert module.cached_later_page('a', 'movie', 'top', [('genre', 'Drama'), ('skip', 24)]) == [{'id': 'tt1'}]
    assert len(calls) == 1
    for offset in range(25, 225):
        module.cached_later_page('a', 'movie', 'top', [('genre', 'Drama'), ('skip', offset)])
    assert len(module._later_pages) == 200
    assert ('a', 'movie', 'top', (('genre', 'Drama'),), 24) not in module._later_pages
    module.clear_later_pages()
    assert not module._later_pages


def test_home_resume_can_page_past_twenty_and_short_pages(pages):
    offsets = []

    def fetch(_transport, _type, _id, extra=None):
        skip = dict(extra)['skip']
        offsets.append(skip)
        return [{'id': str(skip)}] if skip <= 25 else []

    catalog = {'id': 'top', 'type': 'movie', 'extra': [{'name': 'skip'}]}
    rows = list(pages.views.iter_catalog_pages_after('t', 'movie', 'top', 10, ['first'],
                catalog=catalog, page_limit=None, fetch_page=fetch, stop_short=False))
    assert len(rows) == 16
    assert offsets == list(range(10, 27))


def test_same_catalog_id_with_a_new_playlist_stamp_is_a_cache_miss(pages, tmp_path, monkeypatch):
    module = pages.catalogpages
    cache = _cache(tmp_path, {'now': 0.0})
    monkeypatch.setattr(module, 'get_page_cache', lambda: cache)
    fetched = []

    def fetch(transport, manifest, catalog, extra):
        fetched.append((manifest.get('rivuletPlaylists'), extra))
        return [{'id': 'ch-%s' % manifest['rivuletPlaylists'], 'type': 'tv', 'name': 'x'}]

    monkeypatch.setattr(module, '_fetch_first_page', fetch)
    catalog = {'type': 'tv', 'id': 'iptv0'}
    url = 'http://127.0.0.1:11470/iptv/manifest.json'

    def shown(stamp):
        return [m['id'] for m in module.cached_first_page(url, {'rivuletPlaylists': stamp}, catalog)]

    assert shown('old') == ['ch-old']
    assert shown('old') == ['ch-old'] and len(fetched) == 1
    assert shown('new') == ['ch-new'] and len(fetched) == 2
    assert fetched[-1][1] is None  # the stamp never reaches the add-on


def test_unstamped_manifest_keeps_the_plain_cache_key(pages, tmp_path, monkeypatch):
    from lib.stremio.pagecache import cache_key

    module = pages.catalogpages
    cache = _cache(tmp_path, {'now': 0.0})
    monkeypatch.setattr(module, 'get_page_cache', lambda: cache)
    monkeypatch.setattr(module, '_fetch_first_page', lambda *a: [{'id': 'tt1', 'type': 'movie', 'name': 'x'}])
    module.cached_first_page('t', {}, {'type': 'movie', 'id': 'top'}, [('genre', 'Drama')])
    state, metas = cache.lookup(cache_key('t', 'movie', 'top', [('genre', 'Drama')]), allow_expired=True)
    assert state == 'fresh' and metas


def test_later_pages_and_their_failures_are_keyed_by_the_stamp(pages, monkeypatch):
    module = pages.catalogpages
    module.clear_later_pages()
    calls = []
    monkeypatch.setattr(pages.views, '_fetch_catalog', lambda *a, **kw: calls.append(1) or [{'id': 'tt%d' % len(calls)}])
    extra = [('skip', 24)]
    assert module.cached_later_page('a', 'tv', 'iptv0', extra, stamp='old') == [{'id': 'tt1'}]
    assert module.cached_later_page('a', 'tv', 'iptv0', extra, stamp='old') == [{'id': 'tt1'}]
    assert module.cached_later_page('a', 'tv', 'iptv0', extra, stamp='new') == [{'id': 'tt2'}]
    module.clear_later_pages()

    def boom(*a, **kw):
        raise module.AddonError('x', category='HTTP 503')

    monkeypatch.setattr(pages.views, '_fetch_catalog', boom)
    try:
        module.cached_later_page('a', 'tv', 'iptv0', extra, stamp='old')
    except module.LaterPageFailure:
        pass
    assert module.later_page_failure('a', 'tv', 'iptv0', extra, stamp='old')
    assert module.later_page_failure('a', 'tv', 'iptv0', extra, stamp='new') is None
    module.clear_later_pages()
