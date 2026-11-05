"""Property tests for ``lib.library``: the account's ``libraryItem`` records
are third-party JSON, and every function here runs on them inside a loop
(one hostile record must not abort the whole library pull or a playback
push), so none of them may raise and what they return must keep its shape.
"""
import copy
import datetime
import json
import re

from hypothesis import example, given
from hypothesis import strategies as st

from lib import library, livetv
from tests.fuzz_strategies import (
    hostile_json,
    json_values,
    library_item_lists,
    library_items,
    library_states,
    metas,
    ms_values,
    progress_entries,
)

NOW = datetime.datetime(2026, 1, 2, 3, 4, 5, tzinfo=datetime.timezone.utc)
ISO_SECONDS = re.compile(r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$')

# Positions come from Kodi's player (seconds as a float, times 1000), never
# from JSON, so they are bounded integers - negative ones included, since a
# bogus `getTime()` reading is a documented input.
_positions = st.integers(min_value=-(10**6), max_value=2**63)
_video_ids = st.one_of(st.none(), st.text(max_size=12))


def _rest(record):
    return {k: v for k, v in record.items() if k not in ('_mtime', 'state')}


def _same(left, right):
    """Equality that survives NaN (``nan != nan``): compare reprs."""
    return repr(left) == repr(right)


@given(hostile_json(library_items))
@example({'_id': 'tt1', 'type': True, 'state': {'lastWatched': '2020-01-01T00:00:00Z', 'flaggedWatched': 1}})
@example({'_id': 5, 'type': 'movie', 'state': {'lastWatched': '2020-01-01T00:00:00Z', 'flaggedWatched': 1}})
def test_progress_from_library_item_never_raises_and_keeps_shape(item):
    entry = library.progress_from_library_item(item)
    if entry is None:
        return
    assert set(entry) == {'type', 'id', 'video_id', 'position_ms', 'duration_ms', 'updated_at'}
    assert isinstance(entry['type'], str) and entry['type']
    assert entry['id']
    assert isinstance(entry['position_ms'], int) and isinstance(entry['duration_ms'], int)
    assert 0 <= entry['position_ms'] <= entry['duration_ms']
    assert entry['duration_ms'] >= 1
    assert ISO_SECONDS.match(entry['updated_at'])
    assert entry['video_id'] is None or (isinstance(entry['video_id'], str) and entry['video_id'])
    if entry['type'] == 'series':
        assert entry['video_id'] is not None
    else:
        assert entry['video_id'] is None


@given(hostile_json(library_items))
def test_item_watched_never_raises(item):
    assert library.item_watched(item) in (True, False)


@given(library_item_lists)
def test_title_verdicts_never_raises_and_keys_are_typed_string_ids(entries):
    verdicts = library.title_verdicts(entries)
    for item_id, (watched, mtime) in verdicts.items():
        assert isinstance(item_id, str) and item_id
        assert watched in (True, False)
        assert mtime == '' or ISO_SECONDS.match(mtime)


@given(json_values)
def test_title_verdicts_tolerates_any_json(entries):
    # The datastore answer is whatever the API returned; a non-list one is
    # the caller's own guard, but a list of anything is the contract.
    if isinstance(entries, list):
        library.title_verdicts(entries)


@given(hostile_json(progress_entries))
@example({'position_ms': 1, 'duration_ms': 10**400})
@example({'position_ms': 10**400, 'duration_ms': 10**400})
def test_progress_finished_never_raises(entry):
    assert library.progress_finished(entry) in (True, False)


@given(library_item_lists, st.lists(hostile_json(progress_entries), max_size=6))
def test_watched_title_ids_never_raises_and_only_names_known_titles(entries, progress):
    verdicts = library.title_verdicts(entries)
    marks = library.watched_title_ids(verdicts, progress)
    assert isinstance(marks, frozenset)
    assert all(isinstance(item_id, str) for item_id in marks)
    # Every account verdict that says watched is kept: the account record
    # wins outright over any local progress.
    assert {item_id for item_id, (watched, _m) in verdicts.items() if watched} <= marks
    # A mark that is not an account verdict came from a typed local entry.
    local_ids = {
        entry['id'] for entry in progress
        if isinstance(entry, dict) and entry.get('type') and isinstance(entry.get('id'), str)
    }
    assert marks <= {i for i, (w, _m) in verdicts.items() if w} | local_ids


@given(library_item_lists, library_item_lists)
def test_newer_verdicts_is_a_union_that_prefers_the_later_mtime(older, newer):
    left, right = library.title_verdicts(older), library.title_verdicts(newer)
    merged = library.newer_verdicts(left, right)
    assert set(merged) == set(left) | set(right)
    for item_id, verdict in merged.items():
        candidates = [v for v in (left.get(item_id), right.get(item_id)) if v is not None]
        assert verdict[1] == max(v[1] for v in candidates)


@given(hostile_json(library_items))
def test_slim_library_item_never_raises_and_feeds_the_same_verdict(item):
    slim = library.slim_library_item(item)
    if slim is None:
        assert not (isinstance(item, dict) and isinstance(item.get('_id'), str) and item['_id'])
        return
    expected = set(library._SLIM_ITEM_KEYS)
    if isinstance(item.get('state'), dict):
        expected.add('state')
    assert set(slim) == expected
    assert slim['_id'] == item['_id']
    if isinstance(slim.get('state'), dict):
        assert set(slim['state']) == set(library._SLIM_STATE_KEYS)
    # "Still in wire shape": the cut-down record gives the verdict the
    # full one does (typed records only - that is title_verdicts' own rule).
    assert library.item_watched(slim) == library.item_watched(item)
    assert library.title_verdicts([slim]) == library.title_verdicts([item])
    # JSON-serialisable and stable under re-slimming: it is what the
    # service writes to library.json on every pull.
    assert _same(library.slim_library_item(slim), slim)


@given(ms_values | st.floats(min_value=-1e6, max_value=1e12))
def test_episode_ticked_never_raises(percent):
    assert library.episode_ticked(percent) in (True, False)
    assert library.episode_ticked(None) is False


# --- merge_playback ---------------------------------------------------------


@given(library_items, _positions, _positions, _video_ids)
@example({'state': {'timeOffset': 'abc'}}, 5, 10, None)
@example({'state': {'timeWatched': [], 'overallTimeWatched': {}, 'timesWatched': 'x'}}, 5, 10, 'v')
@example({'state': {'timeOffset': float('inf'), 'duration': float('nan')}}, 5, 10, None)
@example({'_id': 'tt1', 'behaviorHints': True}, 5, 10, None)
@example({'_id': 'tt1', 'state': {'timesWatched': -3, 'timeWatched': -9}}, 5, 10, None)
def test_merge_playback_never_raises_on_hostile_records(item, position, duration, video_id):
    before = copy.deepcopy(item)
    merged = library.merge_playback(item, position, duration, video_id=video_id, now=NOW)
    assert _same(item, before), 'merge_playback mutated its input'
    assert merged is not item
    assert isinstance(merged['state'], dict)
    assert merged['_mtime'] == merged['state']['lastWatched'] == '2026-01-02T03:04:05Z'
    # Only `_mtime` and `state` differ from the record that came in.
    assert _same(_rest(merged), _rest(item))


_whole = st.integers(min_value=0, max_value=10**9)
_clean_states = st.fixed_dictionaries({}, optional={
    'timeWatched': _whole, 'timeOffset': _whole, 'overallTimeWatched': _whole,
    'timesWatched': st.integers(min_value=0, max_value=5), 'flaggedWatched': st.sampled_from([0, 1]),
    'duration': _whole, 'video_id': _video_ids, 'lastWatched': st.just('2020-01-01T00:00:00Z'),
    'watched': st.none() | st.text(max_size=10), 'noNotif': st.booleans(),
})


@given(
    st.fixed_dictionaries({'_id': st.text(min_size=1, max_size=8)}, optional={'state': _clean_states}),
    _whole, _whole, _video_ids,
)
def test_merge_playback_keeps_the_watched_counters_monotonic(item, position, duration, video_id):
    old = item.get('state') or {}
    new = library.merge_playback(item, position, duration, video_id=video_id, now=NOW)['state']
    gained = new.get('timesWatched', 0) - int(old.get('timesWatched') or 0)
    # The first crossing of the threshold bumps the counter by exactly one,
    # nothing else ever moves it, and only a flagged state can have moved it.
    assert gained in (0, 1)
    assert gained == 0 or new['flaggedWatched'] == 1
    assert min(new.get('timeOffset', 0), new.get('duration', 0), new.get('timeWatched', 0)) >= 0
    assert new['overallTimeWatched'] >= int(old.get('overallTimeWatched') or 0)
    if new['video_id'] == old.get('video_id'):
        assert new.get('timeOffset', 0) >= int(old.get('timeOffset') or 0)
    # `timeWatched` only counts forward from the stored offset, so a replay
    # of the very same sample adds nothing: the push is retried freely.
    again = library.merge_playback(
        library.merge_playback(item, position, duration, video_id=video_id, now=NOW),
        position, duration, video_id=video_id, now=NOW,
    )['state']
    assert again == new


@given(
    hostile_json(library_items), st.booleans(), st.lists(st.text(max_size=12), max_size=4) | st.none(),
)
@example({'_id': 'tt1', 'state': {'timesWatched': 'many'}}, True, None)
@example({'_id': 'tt1', 'state': {'timesWatched': -1}}, True, None)
@example({'_id': 'tt1', 'state': {'timesWatched': [1]}}, False, ['a'])
def test_mark_watched_payload_never_raises_on_a_hostile_record(item, watched, video_ids):
    if not isinstance(item, dict):
        return
    before = copy.deepcopy(item)
    marked = library.mark_watched_payload(item, watched, now=NOW, video_ids=video_ids)
    assert _same(item, before), 'mark_watched_payload mutated its input'
    state = marked['state']
    assert state['flaggedWatched'] == (1 if watched else 0)
    assert library.item_watched(marked) == bool(watched)
    # Idempotent past the first call, as its docstring promises.
    assert library.mark_watched_payload(marked, watched, now=NOW, video_ids=video_ids)['state'] == state


@given(metas, hostile_json(library_items))
def test_add_and_remove_payloads_never_raise(meta, existing):
    added = library.library_add_from_existing(meta, existing, now=NOW)
    assert added['removed'] is False and added['temp'] is False
    if isinstance(existing, dict):
        assert added['_mtime'] == '2026-01-02T03:04:05Z'
    if isinstance(existing, dict) and existing:
        removed = library.library_remove_payload(existing, now=NOW)
        assert removed['removed'] is True


@given(metas)
@example({'id': 'tt1', 'behaviorHints': True})
@example({'id': 'tt1', 'behaviorHints': 'x'})
def test_build_library_item_never_raises_on_a_hostile_meta(meta):
    for item in (library.build_library_item(meta, now=NOW), library.library_add_payload(meta, now=NOW)):
        assert item['_id'] == meta['id']
        wire = {'defaultVideoId', 'featuredVideoId', 'hasScheduledVideos'}
        assert set(item['behaviorHints']) == (wire | {'isLive'} if livetv.is_live(meta) else wire)
        assert isinstance(item['behaviorHints']['hasScheduledVideos'], bool)
        json.dumps(item)
        # A freshly built record is a valid input for everything else.
        library.merge_playback(item, 1000, 100000, now=NOW)
        library.progress_from_library_item(item)


@given(library_states)
def test_merge_playback_state_survives_a_json_round_trip(state):
    # The record is `datastorePut` as JSON: whatever merge_playback returns
    # for a hostile state must still encode (NaN/Infinity pass through).
    merged = library.merge_playback({'_id': 'tt1', 'state': state}, 5, 10, now=NOW)
    json.dumps(merged)
