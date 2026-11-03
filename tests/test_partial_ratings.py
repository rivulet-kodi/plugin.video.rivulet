"""Partial-provider caching, shared quota budgets, refresh and safe diagnostics."""
import hashlib
import json
import threading

import pytest

from lib.stremio import ratings
from tests.kodistubs import install_kodi_stubs
from tests.test_home import _RELOAD_MODULE_NAMES
from tests.test_ratings import _get, _Response
from tests.test_ratingsched import _Thread
from tests.test_ratingsched import cache as cache
from tests.test_ratingsched import sched as sched

_SECRET = 'SECRETKEY'
SID = 'tt33764258'
OMDB = {'imdbRating': '7.4', 'Metascore': 'N/A', 'Ratings': [{'Source': 'Internet Movie Database', 'Value': '7.4/10'}]}
MDB = {'ratings': [{'source': 'tomatoes', 'value': 91}, {'source': 'popcorn', 'value': 94}, {'source': 'metacritic', 'value': 78}]}


def response(status=200, payload=None, headers=None):
    reply = _Response(status, {} if payload is None else payload)
    reply.headers = headers or {}
    return reply


@pytest.mark.parametrize('status,payload', [(429, {}), (401, {}), (403, {}), (500, {}), (503, {}), (503, {'error': 'Invalid API key or Rate Limiter Reached!'}), (200, ValueError('bad JSON'))])
def test_omdb_survives_provider_errors_as_partial(status, payload):
    answer = ratings.fetch_scores(SID, _SECRET, _SECRET, get=_get([response(payload=OMDB), response(status, payload)]))
    assert answer == {'imdbRating': '7.4'} and answer.partial
    assert set(answer.providers) == {'omdb', 'mdblist'}


def test_timeout_partial_and_other_provider_success():
    calls = []
    def get(url, params=None, timeout=None):
        calls.append(url)
        if url == ratings.OMDB_URL:
            return response(payload=OMDB)
        raise TimeoutError('private transport failure')
    result = ratings.fetch_scores(SID, _SECRET, _SECRET, get=get)
    assert result.partial and result == {'imdbRating': '7.4'} and len(calls) == 2
    result = ratings.fetch_scores(SID, _SECRET, _SECRET, get=_get([response(503), response(payload=MDB)]))
    assert result.partial and result['rtAudience'] == '94%'


@pytest.mark.parametrize('payload', [MDB, {}])
def test_success_or_definitive_miss_is_complete(payload):
    result = ratings.fetch_scores(SID, _SECRET, _SECRET, get=_get([response(payload=OMDB), response(payload=payload)]))
    assert not result.partial
    result = ratings.fetch_scores(SID, _SECRET, _SECRET, get=_get([response(payload=OMDB), response(404)]))
    assert result == {'imdbRating': '7.4'} and not result.partial


@pytest.mark.parametrize('status,payload,category', [(429, {}, 'quota'), (503, {'error': 'Rate Limiter Reached'}, 'quota'), (503, {}, 'http 503')])
def test_quota_hints_are_numeric_and_not_request_data(monkeypatch, status, payload, category):
    monkeypatch.setattr(ratings.time, 'time', lambda: 1000)
    reply = response(status, payload, {'Retry-After': '900', 'X-RateLimit-Remaining': '0', 'X-RateLimit-Reset': '2200'})
    with pytest.raises(ratings.RatingsError) as caught:
        ratings._get_json(_get([reply]), ratings.MDBLIST_URL, {'apikey': _SECRET}, 8)
    exc = caught.value
    assert exc.category == category and exc.retry_after == 1200 and exc.reset_after == 1200
    assert exc.remaining == 0 and reply.closed
    assert _SECRET not in str(exc) and 'apikey' not in str(exc)


@pytest.mark.parametrize('ttl', [True, False, float('nan'), float('inf'), -1, 0, '600', None])
def test_invalid_persisted_ttl_is_rejected(ttl):
    assert not ratings._valid_entry({'at': 100, 'scores': {}, 'ttl': ttl})


@pytest.mark.parametrize('ttl', [True, float('nan'), -1, '600'])
def test_invalid_store_ttl_is_rejected(tmp_path, ttl):
    cache = ratings.RatingsCache(str(tmp_path / 'ratings.json'))
    with pytest.raises(ValueError, match='invalid ratings ttl'):
        cache.store(SID, {}, ttl=ttl)


def test_ttl_round_trip_and_v3_invalidates_v2_without_keys_on_disk(tmp_path):
    path = tmp_path / 'ratings.json'
    token = hashlib.sha256(json.dumps(('v2', _SECRET, '')).encode()).hexdigest()
    path.write_text(json.dumps({SID + '\n' + token: {'at': 100, 'scores': {'imdbRating': '7.4'}}}))
    cache = ratings.RatingsCache(str(path), clock=lambda: 100, keys=(_SECRET, ''))
    assert cache.lookup(SID) is None
    cache.store(SID, {'imdbRating': '7.4'}, ttl=600)
    again = ratings.RatingsCache(str(path), clock=lambda: 699, keys=(_SECRET, ''))
    assert again.lookup(SID) == {'imdbRating': '7.4'} and again.lookup(SID).partial
    assert again.lookup(SID, now=701) is None
    assert json.loads(path.read_text())[cache._entry_key(SID)]['ttl'] == 600
    assert _SECRET not in path.read_text()
    cache.store(SID, {}, now=100)
    assert cache.lookup(SID, now=100 + ratings.TTL_SECONDS) == {}
    assert not cache.lookup(SID, now=100).partial


def test_old_entries_without_ttl_use_full_day(tmp_path):
    cache = ratings.RatingsCache(str(tmp_path / 'ratings.json'), clock=lambda: 100)
    cache.store(SID, {'imdbRating': '7.4'})
    assert cache.lookup(SID, now=100 + 601) and cache.lookup(SID, now=100 + 86401) is None
    assert 'ttl' not in cache._entries[cache._entry_key(SID)]


def test_partial_worker_ttl_and_next_focus_refetches(sched, tmp_path, monkeypatch):
    s = sched.module
    now = [1000]
    cache = ratings.RatingsCache(str(tmp_path / 'ratings.json'), clock=lambda: now[0])
    calls = []
    def get(url, params=None, timeout=None):
        calls.append(url)
        return response(payload=OMDB) if url == ratings.OMDB_URL else response(503)
    monkeypatch.setattr('requests.get', get)
    s._worker(SID, _SECRET, _SECRET, cache, 'failure')
    assert cache.lookup(SID).partial
    entry = cache._entries[cache._entry_key(SID)]
    assert entry['ttl'] == s._PARTIAL_TTL_SECONDS == 600
    assert len(calls) == 2
    now[0] = 1601
    monkeypatch.setattr(s, '_keys', lambda: (_SECRET, _SECRET))
    monkeypatch.setattr(s, '_get_cache', lambda *args: cache)
    assert s.overlay_text({'id': SID}, 'catalog') == 'catalog'
    sched.clock.now += 1
    s._loop()
    assert len(calls) == 4 and calls.count(ratings.mdblist_url(SID)) == 2


def test_cooldown_amplifier_stores_every_other_title_for_ten_minutes(sched, cache, monkeypatch):
    s = sched.module
    calls = []
    def get(url, params=None, timeout=None):
        calls.append(url)
        return response(payload=OMDB) if url == ratings.OMDB_URL else response(429)
    monkeypatch.setattr('requests.get', get)
    for sid in [SID, 'tt0000002', 'tt0000003']:
        s._worker(sid, _SECRET, _SECRET, cache, sid)
        assert cache._entries[cache._entry_key(sid)]['ttl'] == 600
    assert len(calls) == 4 and calls.count(ratings.OMDB_URL) == 3
    assert len([m for m, _ in sched.env.log_calls if 'ratings: mdblist quota -' in m]) == 1
    assert len([m for m, _ in sched.env.log_calls if ' partial (' in m]) == 3


@pytest.mark.parametrize('status,retry,expected', [(429, '30', 300), (429, '1200', 1200), (429, '999999', 21600), (401, '1200', 300)])
def test_cooldown_duration_and_cap(sched, cache, monkeypatch, status, retry, expected):
    monkeypatch.setattr('requests.get', lambda *a, **kw: response(status, headers={'Retry-After': retry}))
    s = sched.module
    s._worker(SID, '', _SECRET, cache, 'failure')
    assert s._provider_until[s._provider_key('mdblist', _SECRET)] == sched.clock.now + expected
    assert cache.lookup(SID) is None and 'failure' in s._failed_until


@pytest.mark.parametrize('remaining', [0, 25])
def test_low_quota_skips_only_that_providers_prefetch_until_reset(sched, cache, monkeypatch, remaining):
    s = sched.module
    monkeypatch.setattr(ratings.time, 'time', lambda: 1000)
    calls = []
    def get(url, params=None, timeout=None):
        calls.append(url)
        if url == ratings.OMDB_URL:
            return response(payload=OMDB)
        return response(payload=MDB, headers={'x-ratelimit-remaining': str(remaining), 'x-ratelimit-reset': '1600'})
    monkeypatch.setattr('requests.get', get)
    s._schedule(SID, _SECRET, _SECRET, cache, ['tt0000002', 'tt0000003'])
    sched.clock.now += 1
    s._loop()
    assert len(calls) == 4 and calls.count(ratings.OMDB_URL) == 3
    assert cache.lookup('tt0000002').partial
    assert cache.lookup(SID)['rtAudience'] == '94%'
    # A newly focused title may still use the last requests.
    s._schedule('tt0000004', _SECRET, _SECRET, cache)
    sched.clock.now += 1
    s._loop()
    assert calls[-1] == ratings.mdblist_url('tt0000004')
    # Reset unpauses MDBList prefetch.
    sched.clock.now += 601
    s._plan['focus'] = 'tt0000004'
    assert s._worker_keys({'omdb': _SECRET, 'mdblist': _SECRET}, 'tt0000005')[0]['mdblist']


def test_provider_transitions_are_info_once_and_do_not_leak(sched, cache, monkeypatch):
    import xbmc
    s = sched.module
    replies = [response(429, headers={'Retry-After': '300'}), response(payload=MDB), response(payload=MDB)]
    monkeypatch.setattr('requests.get', _get(replies))
    s._worker(SID, '', _SECRET, cache, SID)
    s._worker('tt0000002', '', _SECRET, cache, 'second')
    sched.clock.now += 301
    s._worker('tt0000003', '', _SECRET, cache, 'third')
    s._worker('tt0000004', '', _SECRET, cache, 'fourth')
    states = [(m, level) for m, level in sched.env.log_calls if 'ratings: mdblist ' in m]
    assert len(states) == 2 and all(level == xbmc.LOGINFO for _, level in states)
    assert 'quota - cooldown 300 s' in states[0][0] and 'ok - cooldown 0 s' in states[1][0]
    logged = '\n'.join(m for m, _ in sched.env.log_calls)
    assert all(value not in logged for value in [_SECRET, 'apikey', 'http'])
    assert '?' not in logged.replace('(remaining ?)', '')


def test_refresh_clears_cooldowns_plan_and_stale_cache_cannot_resurrect(sched, tmp_path):
    from lib.ui import cacherefresh
    s = sched.module
    path = tmp_path / 'ratings.json'
    cache = ratings.RatingsCache(str(path))
    cache.store(SID, {'imdbRating': '7.4'})
    s._cache = cache
    s._provider_until['provider'] = 1e9
    s._failed_until['title'] = 1e9
    s._inflight.add(SID)
    s._plan['items'] = [(SID, _SECRET, '', cache, 'movie')]
    cacherefresh.clear_memory()
    if path.exists():
        path.unlink()
    cache.store(SID, {'imdbRating': '9.9'})
    assert not path.exists()
    assert s._cache is None and not s._provider_until and not s._failed_until
    assert not s._inflight and not s._plan['items'] and not s._stop.is_set()
    assert not s._plan['running']
    s._schedule(SID, _SECRET, '', ratings.RatingsCache(str(path)))
    assert s._plan['items'] and _Thread.started


def test_refresh_discards_inflight_results_and_provider_state(tmp_path, monkeypatch):
    with install_kodi_stubs(reload=('lib.ui.compat', 'lib.ui.ratingsched', 'lib.ui.cacherefresh')) as ctx:
        s = ctx.ratingsched
        cache = ratings.RatingsCache(str(tmp_path / 'ratings.json'))
        s._cache = cache
        entered, release = threading.Event(), threading.Event()
        def fetch(*args, **kwargs):
            entered.set()
            assert release.wait(3)
            kwargs['on_rejected']('mdblist')
            return ratings.Scores({'imdbRating': '7.4'}, {'mdblist': ('quota', False, {})})
        monkeypatch.setattr(ratings, 'fetch_scores', fetch)
        worker = threading.Thread(target=s._worker, args=(SID, _SECRET, _SECRET, cache, 'fid'))
        worker.start()
        assert entered.wait(3)
        ctx.cacherefresh.clear_memory()
        release.set()
        worker.join(3)
        assert not worker.is_alive()
        assert not (tmp_path / 'ratings.json').exists()
        assert not s._provider_until and not s._failed_until and not s._inflight
        assert 'Action(noop)' not in ctx.env.executed_builtins


@pytest.mark.parametrize('source', ['previews', 'disk', 'absent'])
def test_missing_imdb_facts_use_only_existing_memory_or_disk(sched, cache, tmp_path, monkeypatch, source):
    from lib.ui import dependencies, metacache, previews
    s = sched.module
    meta = {'id': 'tmdb:1368337', 'type': 'movie', 'imdb_id': SID}
    if source == 'previews':
        previews.remember(meta)
    if source == 'disk':
        metacache.store_cached_meta(str(tmp_path), 'movie', meta['id'], meta)
    monkeypatch.setattr(dependencies, 'get_store', lambda: type('Store', (), {'data_dir': str(tmp_path)})())
    monkeypatch.setattr(s, '_keys', lambda: (_SECRET, _SECRET))
    monkeypatch.setattr(s, '_get_cache', lambda *args: cache)
    def forbidden(*args, **kwargs):
        raise AssertionError('no GUI network')
    monkeypatch.setattr('requests.get', forbidden)
    for _ in range(3):
        assert s.overlay_text({'id': meta['id'], 'type': 'movie'}, 'catalog') == 'catalog'
    assert s._session['missing'] == 1
    assert bool(s._plan['items']) is (source != 'absent')
    if source != 'absent':
        assert s._plan['items'][0][0] == SID
    s.stop()
    s.stop()
    summaries = [m for m, _ in sched.env.log_calls if 'ratings: session fetched ' in m]
    assert len(summaries) == 1 and '1 missing-imdb-id' in summaries[0]


def test_home_completed_card_guard_retries_partial_and_expired_scores(tmp_path, monkeypatch):
    with install_kodi_stubs(reload=_RELOAD_MODULE_NAMES + ('lib.ui.ratingsched', 'lib.ui.logocheck')) as ctx:
        s = ctx.ratingsched
        now = [1000]
        cache = ratings.RatingsCache(str(tmp_path / 'ratings.json'), clock=lambda: now[0])
        monkeypatch.setattr(s, '_keys', lambda: (_SECRET, _SECRET))
        monkeypatch.setattr(s, '_get_cache', lambda *args: cache)
        planned = []
        monkeypatch.setattr(s, '_schedule', lambda imdb_id, *args, **kw: planned.append(imdb_id))
        monkeypatch.setattr(ctx.home, '_logo_known_good', lambda url: False)
        monkeypatch.setattr(ctx.logocheck, 'ask', lambda urls: None)
        meta = {'id': SID, 'type': 'movie', 'name': 'Title', 'imdbRating': '7.4', 'description': 'Plot.'}
        item = ctx.home._poster_item(meta)
        win = ctx.home.HomeWindow('HomeWindow.xml', '/addon/path', 'Default', '1080i')
        cache.store(SID, {'imdbRating': '7.4', 'rottenTomatoes': '91%'}, ttl=600)
        ctx.home.paint_hero(win, item)
        assert item.getProperty('facts_done') == '1'
        assert s.needs_refresh(item.getProperty('hero_facts'))
        assert planned == []
        now[0] += 601
        ctx.home.paint_hero(win, item)
        assert planned == [SID]
        cache.store(SID, {'imdbRating': '7.4', 'rottenTomatoes': '91%', 'rtAudience': '94%'})
        assert not s.needs_refresh(item.getProperty('hero_facts'))
        planned.clear()
        ctx.home.paint_hero(win, item)
        assert planned == []
        now[0] += ratings.TTL_SECONDS + 1
        ctx.home.paint_hero(win, item)
        assert planned == [SID]
        win.close()




def test_session_counts_complete_partial_cache_hits_and_missing_ids_once(sched, cache, tmp_path, monkeypatch):
    from lib.ui import dependencies
    s = sched.module
    calls = [response(payload=OMDB), response(payload=MDB), response(payload=OMDB), response(503)]
    monkeypatch.setattr('requests.get', _get(calls))
    s._worker(SID, _SECRET, _SECRET, cache, 'complete')
    s._worker('tt0000002', _SECRET, _SECRET, cache, 'partial')
    monkeypatch.setattr(s, '_keys', lambda: (_SECRET, _SECRET))
    monkeypatch.setattr(s, '_get_cache', lambda *args: cache)
    monkeypatch.setattr(dependencies, 'get_store', lambda: type('Store', (), {'data_dir': str(tmp_path)})())
    s.overlay_text({'id': SID}, 'catalog')
    s.overlay_text({'id': SID}, 'catalog')
    s.overlay_text({'id': 'kitsu:nomatch', 'type': 'series'}, 'catalog')
    s.stop()
    s.stop()
    lines = [m for m, _ in sched.env.log_calls if 'ratings: session fetched ' in m]
    assert len(lines) == 1
    assert '1 complete, 1 partial, 1 missing-imdb-id, 1 cache hits' in lines[0]


def test_unavailable_provider_state_is_safe_and_recovers_once(sched, cache, monkeypatch):
    s = sched.module
    replies = [response(503), response(503), response(payload=MDB)]
    monkeypatch.setattr('requests.get', _get(replies))
    for index in range(3):
        s._worker('tt%07d' % index, '', _SECRET, cache, str(index))
    states = [m for m, _ in sched.env.log_calls if 'ratings: mdblist ' in m]
    assert len(states) == 2
    assert 'unavailable:status-503' in states[0] and 'ok - cooldown 0 s' in states[1]
    assert all(_SECRET not in m and 'http' not in m and 'apikey' not in m for m in states)
    partial = [m for m, _ in sched.env.log_calls if ' partial (' in m]
    assert len(partial) == 2 and all('cached 0 s' in m for m in partial)


@pytest.mark.parametrize('value', ['n/a', 'NaN', 'inf', '-1', None])
def test_invalid_rate_headers_do_not_become_deadlines(value):
    reply = response(headers={'Retry-After': value, 'x-ratelimit-reset': value, 'x-ratelimit-remaining': value})
    hints = ratings._rate_hints(reply)
    assert hints == {'retry_after': 0, 'reset_after': 0, 'remaining': None}


def test_no_scores_during_cooldown_remains_a_retryable_failure(sched, cache, monkeypatch):
    s = sched.module
    s._provider_until[s._provider_key('mdblist', _SECRET)] = sched.clock.now + 300
    monkeypatch.setattr('requests.get', lambda *a, **k: response(payload={'Response': 'True'}))
    s._worker(SID, _SECRET, _SECRET, cache, 'failure')
    assert cache.lookup(SID) is None and 'failure' in s._failed_until


def test_guard_handles_missing_keys_bad_facts_and_read_failures(sched, cache, monkeypatch):
    s = sched.module
    monkeypatch.setattr(s, '_keys', lambda: ('', ''))
    assert not s.needs_refresh('{}')
    monkeypatch.setattr(s, '_keys', lambda: (_SECRET, ''))
    assert not s.needs_refresh('not json')
    assert not s.needs_refresh('{}')
    def fail(*args):
        raise OSError('unreadable')
    monkeypatch.setattr(s, '_get_cache', fail)
    assert not s.needs_refresh(json.dumps({'id': SID}))


def test_home_refresh_removes_a_file_recreated_after_settings_deleted_it(sched, tmp_path):
    from lib.ui import cacherefresh
    path = tmp_path / 'ratings.json'
    old = ratings.RatingsCache(str(path))
    old.store(SID, {'imdbRating': '7.4'})
    sched.module._cache = old
    path.unlink()  # The Settings interpreter's clear_files.
    old.store('tt0000002', {'imdbRating': '7.5'})  # Home finishes an older response.
    assert path.exists()
    cacherefresh.clear_memory()  # Home observes the changed generation.
    assert not path.exists()
    old.store('tt0000003', {'imdbRating': '7.6'})
    assert not path.exists()


def test_closing_during_dwell_logs_a_zero_fetch_session_once(sched, cache, monkeypatch):
    s = sched.module
    monkeypatch.setattr(s, '_keys', lambda: (_SECRET, ''))
    monkeypatch.setattr(s, '_get_cache', lambda *args: cache)
    s.overlay_text({'id': SID}, 'catalog')
    s.stop()
    s.stop()
    logs = [m for m, _ in sched.env.log_calls if 'ratings: session fetched ' in m]
    assert len(logs) == 1 and '0 complete, 0 partial, 0 missing-imdb-id, 0 cache hits' in logs[0]


def test_close_does_not_start_the_second_provider_or_publish_a_late_result(sched, cache, monkeypatch):
    s = sched.module
    calls = []
    def get(url, params=None, timeout=None):
        calls.append(url)
        s.stop()
        return response(payload=OMDB)
    monkeypatch.setattr('requests.get', get)
    s._worker(SID, _SECRET, _SECRET, cache, 'fid')
    assert calls == [ratings.OMDB_URL]
    assert cache.lookup(SID) is None and not s._failed_until and not s._inflight
    assert not sched.env.executed_builtins


def test_all_failed_exception_preserves_the_retry_hint():
    with pytest.raises(ratings.RatingsError) as caught:
        ratings.fetch_scores(SID, mdblist_key=_SECRET, get=_get([response(429, headers={'Retry-After': '1200'})]))
    assert caught.value.retry_after == 1200
    assert caught.value.providers['mdblist'][0] == 'quota'


def test_another_quota_window_logs_one_new_cooldown_after_expiry(sched, cache, monkeypatch):
    s = sched.module
    calls = []
    monkeypatch.setattr('requests.get', lambda *a, **kw: calls.append(1) or response(429))
    s._worker(SID, '', _SECRET, cache, 'first')
    s._worker('tt0000002', '', _SECRET, cache, 'paused')
    sched.clock.now += 301
    s._worker('tt0000003', '', _SECRET, cache, 'next-window')
    lines = [m for m, _ in sched.env.log_calls if 'ratings: mdblist quota -' in m]
    assert len(lines) == len(calls) == 2
