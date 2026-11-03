"""lib/ui/ratingsched.py: the hero's ratings, fetched without blocking.

These drive the plan and the worker by hand on a fake clock: no thread is
started and nothing goes on the network.
"""
import pytest

from lib.stremio.ratings import RatingsCache, RatingsError
from tests.kodistubs import install_kodi_stubs

_OMDB = 'OMDB-SECRET-KEY'


class _Thread:
    started = []

    def __init__(self, target=None, name=None, daemon=None):
        self.target = target
        self.name = name
        self.daemon = daemon

    def start(self):
        _Thread.started.append(self)


class _Clock:
    def __init__(self, now=100.0):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def sched(monkeypatch):
    with install_kodi_stubs(reload=('lib.ui.compat', 'lib.ui.ratingsched'),
                            settings={'ratings_omdb_key': '', 'ratings_mdblist_key': ''}) as ctx:
        module = ctx.ratingsched
        _Thread.started = []
        clock = _Clock()
        monkeypatch.setattr(module.threading, 'Thread', _Thread)
        monkeypatch.setattr(module.time, 'monotonic', clock)
        ctx.module = module
        ctx.clock = clock
        yield ctx


@pytest.fixture
def cache(tmp_path):
    return RatingsCache(str(tmp_path / 'ratings.json'))


# --- keys and ids ----------------------------------------------------------------


def test_the_key_token_hides_the_key(sched):
    s = sched.module
    assert s._token('') == '' and s._token('   ') == '' and s._token(None) == ''
    token = s._token(_OMDB)
    assert len(token) == 12 and _OMDB not in token
    assert s._token('  %s  ' % _OMDB) == token
    assert s._token('another') != token
    fail_id = s._fail_id('tt0111161', _OMDB, '')
    assert _OMDB not in fail_id
    # A fixed typo is a different failure, so it is retried at once.
    assert s._fail_id('tt0111161', _OMDB + 'x', '') != fail_id


def test_keys_are_read_trimmed_from_settings(sched):
    sched.env.addon.setSetting('ratings_omdb_key', '  %s ' % _OMDB)
    sched.env.addon.setSetting('ratings_mdblist_key', '')
    assert sched.module._keys() == (_OMDB, '')


def test_the_cache_is_kept_until_the_profile_moves(sched, monkeypatch, tmp_path):
    import lib.ui.compat as compat

    monkeypatch.setattr(compat, 'addon_profile_dir', lambda: str(tmp_path / 'one'))
    first = sched.module._get_cache()
    assert first.path == str(tmp_path / 'one' / 'ratings.json')
    assert sched.module._get_cache() is first
    monkeypatch.setattr(compat, 'addon_profile_dir', lambda: str(tmp_path / 'two'))
    second = sched.module._get_cache()
    assert second is not first and second.path == str(tmp_path / 'two' / 'ratings.json')


@pytest.mark.parametrize('meta, stype', [
    ({'type': ' series '}, 'series'), ({'type': None}, ''), ({'type': 5}, ''), ({}, ''), (None, ''), ('tt1', ''),
])
def test_type_of(sched, meta, stype):
    assert sched.module._type_of(meta) == stype


def test_misses_skip_cached_unknown_and_repeated_titles(sched, cache, monkeypatch):
    cache.store('tt0000002', {'imdbRating': '7.0'})
    cache.store('tt0000004', {})
    real = sched.module.imdb_id_of

    def _imdb(meta):
        if meta == 'explodes':
            raise ValueError('odd meta')
        return real(meta)

    monkeypatch.setattr(sched.module, 'imdb_id_of', _imdb)
    metas = [
        {'id': 'tt0000001', 'type': 'movie'}, {'id': 'tt0000002'}, {'id': 'kitsu:1'}, 'explodes',
        {'id': 'tt0000001', 'type': 'movie'}, {'id': 'tt0000003', 'type': 'series'}, {'id': 'tt0000004'},
    ]
    assert sched.module._misses(cache, metas) == [('tt0000001', 'movie'), ('tt0000003', 'series')]
    assert sched.module._misses(cache, None) == []


# --- the overlay -----------------------------------------------------------------


def _overlay(sched, monkeypatch, cache, keys=(_OMDB, '')):
    planned = []
    monkeypatch.setattr(sched.module, '_keys', lambda: keys)
    monkeypatch.setattr(sched.module, '_get_cache', lambda *args: cache)
    monkeypatch.setattr(sched.module, '_schedule',
                        lambda imdb_id, omdb, mdb, c, prefetch=(), stype='': planned.append((imdb_id, list(prefetch), stype)))
    return planned


def test_no_imdb_id_or_no_key_leaves_the_catalog_text(sched, monkeypatch, cache):
    planned = _overlay(sched, monkeypatch, cache)
    assert sched.module.overlay_text({'id': 'kitsu:1'}, 'catalog') == 'catalog'
    planned = _overlay(sched, monkeypatch, cache, keys=('', ''))
    assert sched.module.overlay_text({'id': 'tt0111161'}, 'catalog') == 'catalog'
    assert planned == []


def test_a_miss_plans_a_fetch_and_paints_the_catalog_text_first(sched, monkeypatch, cache):
    planned = _overlay(sched, monkeypatch, cache)
    upcoming = [{'id': 'tt0111161'}, {'id': 'tt0000002', 'type': 'movie'}, {'id': 'tt0000003'}, {'id': 'tt0000004'}]
    assert sched.module.overlay_text({'id': 'tt0111161', 'type': 'series'}, 'catalog', upcoming) == 'catalog'
    # The focused title first, then at most PREFETCH more; not itself again.
    assert planned == [('tt0111161', [('tt0000002', 'movie'), ('tt0000003', '')][:sched.module.PREFETCH], 'series')]


def test_a_hit_paints_the_cached_scores(sched, monkeypatch, cache):
    import lib.ui.home as home

    cache.store('tt0111161', {'rottenTomatoes': '91%'})
    planned = _overlay(sched, monkeypatch, cache)
    seen = []
    monkeypatch.setattr(home, '_hero_meta_text', lambda meta: seen.append(meta) or '[B]RT 91%[/B] $INFO[System.ProfileName]')
    text = sched.module.overlay_text({'id': 'tt0111161', 'imdbRating': '9.3'}, 'catalog')
    assert seen[0]['rottenTomatoes'] == '91%' and seen[0]['imdbRating'] == '9.3'
    # Text from the services is shown plain: no Kodi markup or info labels.
    assert 'RT 91%' in text
    assert '[B]' not in text and '$INFO[' not in text
    assert planned == []


def test_a_hit_still_plans_the_next_titles(sched, monkeypatch, cache):
    import lib.ui.home as home

    cache.store('tt0111161', {'rottenTomatoes': '91%'})
    planned = _overlay(sched, monkeypatch, cache)
    monkeypatch.setattr(home, '_hero_meta_text', lambda meta: 'scores')
    upcoming = [{'id': 'tt0000002', 'type': 'series'}, {'id': 'tt0000003'}, {'id': 'tt0000004'}]
    assert sched.module.overlay_text({'id': 'tt0111161'}, 'catalog', upcoming) == 'scores'
    assert planned == [('tt0000002', [('tt0000003', '')][:sched.module.PREFETCH - 1], 'series')]


def test_a_cached_miss_keeps_the_catalog_text(sched, monkeypatch, cache):
    cache.store('tt0111161', {})
    planned = _overlay(sched, monkeypatch, cache)
    assert sched.module.overlay_text({'id': 'tt0111161'}, 'catalog') == 'catalog'
    assert planned == []


def test_the_overlay_never_raises(sched, monkeypatch, cache):
    def _boom():
        raise RuntimeError('settings gone')

    monkeypatch.setattr(sched.module, '_keys', _boom)
    assert sched.module.overlay_text({'id': 'tt0111161'}, 'catalog') == 'catalog'


# --- the plan --------------------------------------------------------------------


def test_the_plan_is_latest_wins_and_starts_one_worker(sched, cache):
    s = sched.module
    s._schedule('tt0000001', _OMDB, '', cache, [('tt0000002', 'movie')], stype='series')
    assert [item[0] for item in s._plan['items']] == ['tt0000001', 'tt0000002']
    assert [item[4] for item in s._plan['items']] == ['series', 'movie']
    assert s._plan['running'] is True and s._wake.is_set()
    assert len(_Thread.started) == 1 and _Thread.started[0].target == s._loop
    assert _Thread.started[0].daemon is True
    # A bare id is type unknown; repeats and blanks are dropped.
    s._schedule('tt0000003', _OMDB, '', cache, ['tt0000004', 'tt0000003', '', ('tt0000004', 'movie')])
    assert [(item[0], item[4]) for item in s._plan['items']] == [('tt0000003', ''), ('tt0000004', '')]
    assert len(_Thread.started) == 1


def test_an_empty_plan_changes_nothing(sched, cache):
    s = sched.module
    s._schedule('', _OMDB, '', cache)
    assert s._plan['items'] == [] and _Thread.started == []


def test_nothing_is_due_before_the_focus_rests(sched, cache):
    s = sched.module
    s._schedule('tt0000001', _OMDB, '', cache)
    item, wait = s._next_due()
    assert item is None and wait == pytest.approx(s._DWELL_SECONDS)
    sched.clock.now += s._DWELL_SECONDS / 2
    assert s._next_due()[1] == pytest.approx(s._DWELL_SECONDS / 2)
    sched.clock.now += s._DWELL_SECONDS
    item, wait = s._next_due()
    assert wait is None
    assert item[0] == 'tt0000001' and item[3] is cache
    assert item[5] == s._fail_id('tt0000001', _OMDB, '')
    assert 'tt0000001' in s._inflight
    # The plan is spent: the worker is told to stop.
    assert s._next_due() == (None, None)
    assert s._plan['running'] is False


def test_a_title_in_flight_or_recently_failed_is_skipped(sched, cache, monkeypatch):
    s = sched.module
    s._inflight.add('tt0000001')
    s._failed_until[s._fail_id('tt0000002', _OMDB, '')] = 10 ** 12
    s._schedule('tt0000001', _OMDB, '', cache, ['tt0000002'])
    sched.clock.now += 1
    assert s._next_due() == ((), None)
    assert s._next_due() == ((), None)
    assert s._next_due() == (None, None)


def test_a_failure_from_a_minute_ago_is_retried(sched, cache, monkeypatch):
    s = sched.module
    monkeypatch.setattr(s.time, 'time', lambda: 5000.0)
    s._failed_until[s._fail_id('tt0000001', _OMDB, '')] = 4999.0
    s._schedule('tt0000001', _OMDB, '', cache)
    sched.clock.now += 1
    assert s._next_due()[0][0] == 'tt0000001'


def test_the_worker_loop_waits_skips_and_fetches(sched, cache, monkeypatch):
    s = sched.module
    cache.store('tt0000001', {'imdbRating': '7.0'})
    waits = []
    fetched = []

    def _wait(seconds):
        waits.append(seconds)
        sched.clock.now += seconds
        return True

    monkeypatch.setattr(s._wake, 'wait', _wait)
    monkeypatch.setattr(s, '_worker', lambda imdb_id, *args: fetched.append((imdb_id, args[-1])))
    s._inflight.add('tt0000003')
    s._schedule('tt0000001', _OMDB, '', cache, ['tt0000002', 'tt0000003', ('tt0000004', 'series')])
    s._loop()
    # It waited out the dwell once, skipped the cached and the in-flight
    # titles, fetched the rest in order, then stopped.
    assert sum(waits) == pytest.approx(s._DWELL_SECONDS)
    assert max(waits) <= 1.0
    assert fetched == [('tt0000002', ''), ('tt0000004', 'series')]
    assert 'tt0000001' not in s._inflight
    assert s._plan['running'] is False


def test_a_cache_that_cannot_be_read_still_fetches(sched, monkeypatch):
    s = sched.module

    class _Broken:
        def lookup(self, imdb_id):
            raise OSError('disk')

    fetched = []
    monkeypatch.setattr(s, '_worker', lambda imdb_id, *args: fetched.append(imdb_id))
    s._schedule('tt0000001', _OMDB, '', _Broken())
    sched.clock.now += 1
    s._loop()
    assert fetched == ['tt0000001']


# --- the worker ------------------------------------------------------------------


def _fetch(monkeypatch, result=None, error=None):
    def fetch(imdb_id, omdb_key='', mdblist_key='', get=None, timeout=8, stype='', on_rejected=None):
        if error is not None:
            raise error
        return result

    monkeypatch.setattr('lib.stremio.ratings.fetch_scores', fetch)


def test_scores_are_stored_and_the_hero_repainted(sched, cache, monkeypatch):
    s = sched.module
    _fetch(monkeypatch, {'rottenTomatoes': '91%'})
    s._inflight.add('tt0111161')
    s._worker('tt0111161', _OMDB, '', cache, 'fid', 'movie')
    assert cache.lookup('tt0111161') == {'rottenTomatoes': '91%'}
    assert 'tt0111161' not in s._inflight
    assert 'fid' not in s._failed_until
    assert sched.env.executed_builtins[-1] == 'Action(noop)'


def test_an_unknown_title_is_cached_as_a_miss_not_a_failure(sched, cache, monkeypatch):
    s = sched.module
    _fetch(monkeypatch, error=RatingsError('not found', empty=True))
    s._worker('tt0111161', _OMDB, '', cache, 'fid')
    assert cache.lookup('tt0111161') == {}
    assert 'fid' not in s._failed_until


def test_a_service_error_is_held_off_for_a_minute(sched, cache, monkeypatch):
    s = sched.module
    monkeypatch.setattr(s.time, 'time', lambda: 1000.0)
    _fetch(monkeypatch, error=RatingsError('http 503'))
    s._worker('tt0111161', _OMDB, '', cache, 'fid')
    assert cache.lookup('tt0111161') is None
    assert s._failed_until['fid'] == 1000.0 + s._FAIL_SECONDS
    logged = ' '.join(msg for msg, _level in sched.env.log_calls)
    assert 'RatingsError' in logged and _OMDB not in logged


def test_a_miss_that_cannot_be_saved_counts_as_a_failure(sched, monkeypatch):
    s = sched.module

    class _ReadOnly:
        def store(self, imdb_id, scores):
            raise OSError('read only')

    _fetch(monkeypatch, error=RatingsError('not found', empty=True))
    s._worker('tt0111161', _OMDB, '', _ReadOnly(), 'fid')
    assert 'fid' in s._failed_until


def test_a_repaint_that_fails_is_ignored(sched, cache, monkeypatch):
    import xbmc

    s = sched.module

    def _gone(cmd):
        raise RuntimeError('Kodi is going away')

    monkeypatch.setattr(xbmc, 'executebuiltin', _gone)
    _fetch(monkeypatch, {'imdbRating': '7.0'})
    s._inflight.add('tt0111161')
    s._worker('tt0111161', _OMDB, '', cache, 'fid')
    assert 'tt0111161' not in s._inflight


def test_a_log_that_fails_is_ignored(sched, monkeypatch):
    import lib.ui.compat as compat

    def _boom(*args, **kwargs):
        raise RuntimeError('log closed')

    monkeypatch.setattr(compat, 'log', _boom)
    sched.module._log_failure('tt0111161', ValueError('x'))


@pytest.mark.parametrize('provider', ['omdb', 'mdblist'])
@pytest.mark.parametrize('status', [401, 429])
def test_rejected_keys_pause_across_titles_and_changed_keys_retry(sched, cache, monkeypatch, provider, status):
    from tests.test_ratings import _Response

    calls = []

    def get(url, params=None, timeout=None):
        calls.append((url, params['apikey']))
        return _Response(status, {})

    monkeypatch.setattr('requests.get', get)
    s = sched.module

    def work(index, key):
        keys = (key, '') if provider == 'omdb' else ('', key)
        sid = 'tt%07d' % index
        s._worker(sid, keys[0], keys[1], cache, s._fail_id(sid, *keys))

    work(1, 'bad-key')
    for i in range(2, 10):
        work(i, 'bad-key')
    assert len(calls) == 1
    work(10, 'fixed-key')
    assert len(calls) == 2
    sched.clock.now += s._PROVIDER_COOLDOWN_SECONDS + 1
    work(11, 'bad-key')
    assert len(calls) == 3
    assert all('bad-key' not in key and 'fixed-key' not in key for key in s._provider_until)


def test_one_rejected_provider_does_not_pause_the_other(sched, cache, monkeypatch):
    from lib.stremio.ratings import OMDB_URL
    from tests.test_ratings import _Response

    calls = []

    def get(url, params=None, timeout=None):
        calls.append(url)
        if url == OMDB_URL:
            return _Response(200, {'Response': 'False', 'Error': 'Request limit reached!'})
        return _Response(200, {'ratings': [{'source': 'imdb', 'value': 7.0}]})

    monkeypatch.setattr('requests.get', get)
    s = sched.module
    for sid in ('tt0000001', 'tt0000002'):
        s._worker(sid, 'omdb-key', 'mdb-key', cache, 'fid')
    assert calls.count(OMDB_URL) == 1
    assert len(calls) == 3
    assert cache.lookup('tt0000002') == {'imdbRating': '7.0'}


def test_ratings_cache_changes_immediately_with_either_api_key(sched, tmp_path, monkeypatch):
    from lib.ui import compat

    monkeypatch.setattr(compat, 'addon_profile_dir', lambda: str(tmp_path))
    s = sched.module
    legacy = s._get_cache()
    legacy.store('tt0000001', {})
    original = s._get_cache('old-omdb', 'old-mdb')
    assert original.lookup('tt0000001') is None
    original.store('tt0000001', {})
    assert s._get_cache('old-omdb', 'old-mdb').lookup('tt0000001') == {}
    changed = s._get_cache('new-omdb', 'old-mdb')
    assert changed.lookup('tt0000001') is None
    changed.store('tt0000001', {'imdbRating': '8.0'})
    assert s._get_cache('new-omdb', 'new-mdb').lookup('tt0000001') is None
    payload = (tmp_path / 'ratings.json').read_text()
    assert all(key not in payload for key in ('old-omdb', 'old-mdb', 'new-omdb', 'new-mdb'))


def test_title_failures_and_provider_cooldowns_are_capped(sched, cache, monkeypatch):
    s = sched.module

    def rejected(*args, **kwargs):
        kwargs['on_rejected']('omdb')
        raise RatingsError('unavailable')

    monkeypatch.setattr('lib.stremio.ratings.fetch_scores', rejected)
    for i in range(s._MAX_CACHE_ENTRIES + 5):
        s._worker('tt%07d' % i, 'key%d' % i, '', cache, 'failure%d' % i)
    assert len(s._failed_until) == len(s._provider_until) == s._MAX_CACHE_ENTRIES
    assert 'failure0' not in s._failed_until and 'failure2004' in s._failed_until
    assert s._provider_key('omdb', 'key0') not in s._provider_until
    assert s._provider_key('omdb', 'key2004') in s._provider_until
