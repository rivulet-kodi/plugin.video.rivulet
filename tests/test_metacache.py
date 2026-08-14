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


def test_unwritable_dir_is_a_silent_no_op(tmp_path):
    # A directory that does not exist and cannot be created (nested under
    # a bogus parent) must degrade to "no cache", never raise.
    data_dir = str(tmp_path / 'missing' / 'nested')
    metacache.store_cached_meta(data_dir, 'movie', 'tt1', {'name': 'X'})  # must not raise
    assert metacache.load_cached_meta(data_dir, 'movie', 'tt1') is None
