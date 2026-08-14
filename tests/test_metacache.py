"""Tests for lib.ui.metacache: the short-TTL disk cache _fetch_meta()
sits behind. Pure filesystem tests, no Kodi stubs needed - the module
has no xbmc dependency."""
import json
import os

from lib.ui import metacache


def _data_dir(tmp_path):
    return str(tmp_path)


def test_round_trip_hit(tmp_path):
    data_dir = _data_dir(tmp_path)
    meta = {'id': 'tt1', 'name': 'A Movie'}
    metacache.store_cached_meta(data_dir, 'movie', 'tt1', meta)
    assert metacache.load_cached_meta(data_dir, 'movie', 'tt1') == meta


def test_miss_for_unknown_key(tmp_path):
    data_dir = _data_dir(tmp_path)
    assert metacache.load_cached_meta(data_dir, 'movie', 'tt404') is None


def test_distinct_keys_do_not_collide(tmp_path):
    data_dir = _data_dir(tmp_path)
    metacache.store_cached_meta(data_dir, 'movie', 'tt1', {'name': 'Movie'})
    metacache.store_cached_meta(data_dir, 'series', 'tt1', {'name': 'Series'})
    assert metacache.load_cached_meta(data_dir, 'movie', 'tt1') == {'name': 'Movie'}
    assert metacache.load_cached_meta(data_dir, 'series', 'tt1') == {'name': 'Series'}


def test_none_result_is_never_cached(tmp_path):
    data_dir = _data_dir(tmp_path)
    metacache.store_cached_meta(data_dir, 'movie', 'tt1', None)
    assert not os.path.exists(metacache._path(data_dir))
    assert metacache.load_cached_meta(data_dir, 'movie', 'tt1') is None


def test_empty_dict_result_is_never_cached(tmp_path):
    data_dir = _data_dir(tmp_path)
    metacache.store_cached_meta(data_dir, 'movie', 'tt1', {})
    assert metacache.load_cached_meta(data_dir, 'movie', 'tt1') is None


def test_expired_entry_is_a_miss(tmp_path, monkeypatch):
    data_dir = _data_dir(tmp_path)
    metacache.store_cached_meta(data_dir, 'movie', 'tt1', {'name': 'Old'})
    future = metacache.time.time() + metacache.TTL_SECONDS + 1
    monkeypatch.setattr(metacache.time, 'time', lambda: future)
    assert metacache.load_cached_meta(data_dir, 'movie', 'tt1') is None


def test_missing_cache_file_is_a_miss(tmp_path):
    assert metacache.load_cached_meta(_data_dir(tmp_path), 'movie', 'tt1') is None


def test_corrupt_cache_file_is_a_miss(tmp_path):
    data_dir = _data_dir(tmp_path)
    with open(metacache._path(data_dir), 'w') as fh:
        fh.write('not json{{{')
    assert metacache.load_cached_meta(data_dir, 'movie', 'tt1') is None


def test_entries_evicted_past_max_cap(tmp_path, monkeypatch):
    data_dir = _data_dir(tmp_path)
    monkeypatch.setattr(metacache, 'MAX_ENTRIES', 3)
    for i in range(5):
        metacache.store_cached_meta(data_dir, 'movie', 'tt%d' % i, {'name': str(i)})
    with open(metacache._path(data_dir)) as fh:
        entries = json.load(fh)
    assert len(entries) == 3
    # The most recently written entries survive; the earliest are evicted.
    assert metacache.load_cached_meta(data_dir, 'movie', 'tt0') is None
    assert metacache.load_cached_meta(data_dir, 'movie', 'tt1') is None
    assert metacache.load_cached_meta(data_dir, 'movie', 'tt4') == {'name': '4'}


def test_cache_file_stays_within_the_byte_budget(tmp_path, monkeypatch):
    """The whole file is rewritten on every store, so the budget - not the
    entry count - is what bounds write amplification on flash. A real
    profile found SERIES metas at 25-30KB each (they embed every episode),
    so an entry cap alone let one store rewrite megabytes."""
    data_dir = _data_dir(tmp_path)
    monkeypatch.setattr(metacache, 'MAX_BYTES', 8 * 1024)
    big = {'name': 'Show', 'videos': [{'id': 'ep%d' % i, 'title': 'x' * 60} for i in range(40)]}

    for i in range(12):
        metacache.store_cached_meta(data_dir, 'series', 'tt%d' % i, big)

    assert os.path.getsize(metacache._path(data_dir)) <= metacache.MAX_BYTES
    # The most recent write always survives eviction.
    assert metacache.load_cached_meta(data_dir, 'series', 'tt11') == big


def test_a_single_meta_larger_than_the_budget_is_still_cached(tmp_path, monkeypatch):
    """Evicting it would leave an empty cache AND force a refetch next
    visit - strictly worse than one oversized file."""
    data_dir = _data_dir(tmp_path)
    monkeypatch.setattr(metacache, 'MAX_BYTES', 128)
    huge = {'name': 'Show', 'blob': 'x' * 4000}

    metacache.store_cached_meta(data_dir, 'series', 'tt1', huge)

    assert metacache.load_cached_meta(data_dir, 'series', 'tt1') == huge


def test_expired_entries_are_dropped_on_write(tmp_path, monkeypatch):
    """An expired entry can never be served again, so carrying it forward
    only inflates every subsequent write."""
    data_dir = _data_dir(tmp_path)
    metacache.store_cached_meta(data_dir, 'movie', 'old', {'name': 'old'})
    # Captured BEFORE patching: the replacement must not call through to the
    # attribute it is replacing.
    later = metacache.time.time() + metacache.TTL_SECONDS + 1
    monkeypatch.setattr(metacache.time, 'time', lambda: later)

    metacache.store_cached_meta(data_dir, 'movie', 'new', {'name': 'new'})

    with open(metacache._path(data_dir)) as fh:
        entries = json.load(fh)
    assert list(entries) == ['movie:new']


def test_unwritable_dir_is_a_silent_no_op(tmp_path):
    # A directory that does not exist and cannot be created (nested under
    # a bogus parent) must degrade to "no cache", never raise.
    data_dir = str(tmp_path / 'missing' / 'nested')
    metacache.store_cached_meta(data_dir, 'movie', 'tt1', {'name': 'X'})  # must not raise
    assert metacache.load_cached_meta(data_dir, 'movie', 'tt1') is None
