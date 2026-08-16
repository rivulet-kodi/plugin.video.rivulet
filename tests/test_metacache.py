"""Tests for lib.ui.metacache: the short-TTL disk cache _fetch_meta()
sits behind. Pure filesystem tests, no Kodi stubs needed - the module
has no xbmc dependency."""
import concurrent.futures
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


def test_concurrent_stores_from_a_thread_pool_all_survive(tmp_path):
    """Reproduces the measured race: `views._map_addons()` fans
    `_fetch_meta()` cache-miss stores out over a
    `ThreadPoolExecutor(max_workers=8)`. Before `_store_lock` serialised
    the read-evict-write sequence, 15 concurrent stores through such a
    pool left only 4 of 15 entries on disk - every thread that read the
    on-disk snapshot before a sibling's write landed clobbered that
    sibling's entry on its own write. All 15 distinct keys must survive.
    """
    data_dir = _data_dir(tmp_path)
    keys = [('movie', 'tt%d' % i) for i in range(15)]

    def store(key):
        stype, sid = key
        metacache.store_cached_meta(data_dir, stype, sid, {'name': sid})

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(store, keys))

    for stype, sid in keys:
        assert metacache.load_cached_meta(data_dir, stype, sid) == {'name': sid}


# ---------------------------------------------------------------------------
# store_cached_metas() - batched writes (Finding 7)
# ---------------------------------------------------------------------------


def test_store_cached_metas_writes_once_for_multiple_entries(tmp_path, monkeypatch):
    """A batch of N entries must cost exactly one `_atomic_write()` call -
    the whole point of the batch API is collapsing what used to be one
    full-file rewrite per `store_cached_meta()` call into one rewrite for
    the entire batch."""
    data_dir = _data_dir(tmp_path)
    write_calls = []
    real_atomic_write = metacache._atomic_write

    def counting_atomic_write(path, data):
        write_calls.append(dict(data))
        real_atomic_write(path, data)

    monkeypatch.setattr(metacache, '_atomic_write', counting_atomic_write)

    entries = [('movie', 'tt%d' % i, {'name': str(i)}) for i in range(5)]
    metacache.store_cached_metas(data_dir, entries)

    assert len(write_calls) == 1
    for stype, sid, meta in entries:
        assert metacache.load_cached_meta(data_dir, stype, sid) == meta


def test_store_cached_metas_skips_falsy_entries(tmp_path):
    data_dir = _data_dir(tmp_path)
    metacache.store_cached_metas(data_dir, [
        ('movie', 'tt1', {'name': 'Real'}),
        ('movie', 'tt2', None),
        ('movie', 'tt3', {}),
    ])
    assert metacache.load_cached_meta(data_dir, 'movie', 'tt1') == {'name': 'Real'}
    assert metacache.load_cached_meta(data_dir, 'movie', 'tt2') is None
    assert metacache.load_cached_meta(data_dir, 'movie', 'tt3') is None


def test_store_cached_metas_all_falsy_is_a_no_op(tmp_path):
    data_dir = _data_dir(tmp_path)
    metacache.store_cached_metas(data_dir, [('movie', 'tt1', None), ('movie', 'tt2', {})])
    assert not os.path.exists(metacache._path(data_dir))


def test_store_cached_metas_empty_list_is_a_no_op(tmp_path):
    data_dir = _data_dir(tmp_path)
    metacache.store_cached_metas(data_dir, [])
    assert not os.path.exists(metacache._path(data_dir))


def test_store_cached_metas_applies_eviction_across_the_whole_batch(tmp_path, monkeypatch):
    data_dir = _data_dir(tmp_path)
    monkeypatch.setattr(metacache, 'MAX_ENTRIES', 3)
    entries = [('movie', 'tt%d' % i, {'name': str(i)}) for i in range(5)]

    metacache.store_cached_metas(data_dir, entries)

    with open(metacache._path(data_dir)) as fh:
        stored = json.load(fh)
    assert len(stored) == 3


def test_store_cached_meta_delegates_through_the_batch_api_single_entry_unchanged(tmp_path, monkeypatch):
    """`store_cached_meta()` is now `store_cached_metas()` with a
    single-entry list - its own single-entry contract (round trip, one
    write) must be unaffected by the refactor."""
    data_dir = _data_dir(tmp_path)
    write_calls = []
    real_atomic_write = metacache._atomic_write

    def counting_atomic_write(path, data):
        write_calls.append(dict(data))
        real_atomic_write(path, data)

    monkeypatch.setattr(metacache, '_atomic_write', counting_atomic_write)

    metacache.store_cached_meta(data_dir, 'movie', 'tt1', {'name': 'Solo'})

    assert len(write_calls) == 1
    assert metacache.load_cached_meta(data_dir, 'movie', 'tt1') == {'name': 'Solo'}
