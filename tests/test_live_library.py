# SPDX-FileCopyrightText: 2026 M0Rf30
# SPDX-License-Identifier: MIT

"""Live channels in `lib.library` and the local recent-channels file of
`lib.store.Store`: stremio-core's live semantics (src/models/player.rs,
src/models/ctx/update_library.rs, src/models/live_tv_continue_watching.rs).
"""
import datetime
import json
import os

from hypothesis import given
from hypothesis import strategies as st

from lib import library, livetv
from lib.store import RECENT_CHANNELS_FILENAME, Store
from tests.fuzz_strategies import hostile_json, library_item_lists, library_items

NOW = datetime.datetime(2026, 1, 2, 3, 4, 5, tzinfo=datetime.timezone.utc)
NOW_ISO = '2026-01-02T03:04:05Z'


def _channel(**extra):
    record = {
        '_id': 'iptv:ch1', 'type': 'tv', 'name': 'Ch 1', 'removed': True, 'temp': True,
        '_mtime': '2025-01-01T00:00:00Z',
        'state': {'lastWatched': '2025-06-01T10:00:00Z', 'timeOffset': 5, 'duration': 9,
                  'timeWatched': 7, 'flaggedWatched': 0, 'timesWatched': 2, 'video_id': 'old',
                  'watched': 'bits', 'overallTimeWatched': 3},
    }
    record.update(extra)
    return record


# --- build_library_item ------------------------------------------------------


def test_build_library_item_records_isLive_for_hint_and_for_legacy_tv_type():
    by_hint = library.build_library_item({'id': 'a', 'type': 'movie', 'behaviorHints': {'isLive': True}})
    legacy = library.build_library_item({'id': 'b', 'type': 'tv'})
    movie = library.build_library_item({'id': 'c', 'type': 'movie'})
    assert by_hint['behaviorHints']['isLive'] is True
    assert legacy['behaviorHints']['isLive'] is True
    assert 'isLive' not in movie['behaviorHints']


def test_build_library_item_keeps_a_live_items_shape_or_leaves_it_out():
    declared = library.build_library_item({'id': 'a', 'type': 'tv', 'posterShape': 'square'})
    undeclared = library.build_library_item({'id': 'b', 'type': 'tv'})
    movie = library.build_library_item({'id': 'c', 'type': 'movie'})
    assert declared['posterShape'] == 'square'
    assert 'posterShape' not in undeclared  # a null would not parse in stremio-core
    assert movie['posterShape'] == library.DEFAULT_POSTER_SHAPE


# --- merge_live_playback / rewind_live ----------------------------------------


def test_merge_live_playback_only_touches_last_watched_offset_video_and_mtime():
    original = _channel()
    snapshot = json.loads(json.dumps(original))
    merged = library.merge_live_playback(original, now=NOW)
    assert original == snapshot  # never mutated
    assert merged['_mtime'] == NOW_ISO
    state = merged['state']
    assert state['lastWatched'] == NOW_ISO and state['timeOffset'] == 0 and state['video_id'] == 'iptv:ch1'
    for key in ('timeWatched', 'duration', 'flaggedWatched', 'timesWatched', 'watched', 'overallTimeWatched'):
        assert state[key] == original['state'][key]
    assert {k: v for k, v in merged.items() if k not in ('state', '_mtime')} == \
        {k: v for k, v in original.items() if k not in ('state', '_mtime')}


def test_merge_live_playback_builds_a_state_for_a_record_without_one():
    merged = library.merge_live_playback({'_id': 'x', 'type': 'tv'}, now=NOW)
    assert merged['state']['lastWatched'] == NOW_ISO and merged['state']['flaggedWatched'] == 0


def test_rewind_live_clears_last_watched_and_offset_keeping_the_rest():
    original = _channel()
    rewound = library.rewind_live(original, now=NOW)
    assert rewound['state']['lastWatched'] is None and rewound['state']['timeOffset'] == 0
    assert rewound['_mtime'] == NOW_ISO
    assert rewound['state']['timesWatched'] == 2 and original['state']['lastWatched'] == '2025-06-01T10:00:00Z'
    assert library.recent_live_records([rewound]) == []


# --- recent_live_records -------------------------------------------------------


def _at(item_id, stamp, **extra):
    return _channel(_id=item_id, state={'lastWatched': stamp}, **extra)


def test_recent_live_records_newest_first_capped_and_filtered():
    entries = [
        _at('old', '2025-01-01T00:00:00Z'),
        _at('new', '2025-03-01T00:00:00Z'),
        _at('hinted', '2025-02-01T00:00:00Z', type='movie', behaviorHints={'isLive': True}),
        _at('movie', '2025-04-01T00:00:00Z', type='movie'),
        _at('gone', '2025-05-01T00:00:00Z', removed=True, temp=False),
        _at('never', None),
        _at('frac', '2025-03-01T00:00:00.512Z', removed=False),
    ]
    ids = [i['_id'] for i in library.recent_live_records(entries)]
    assert ids == ['new', 'frac', 'hinted', 'old']  # tie on 'new'/'frac': entries order
    assert [i['_id'] for i in library.recent_live_records(entries, limit=2)] == ['new', 'frac']
    assert library.recent_live_records(entries, limit=0) == []


def test_recent_live_records_default_cap_is_the_live_row_size():
    entries = [_at('c%d' % n, '2025-01-%02dT00:00:00Z' % (n + 1)) for n in range(15)]
    assert len(library.recent_live_records(entries)) == livetv.RECENT_LIMIT


@given(st.lists(hostile_json(library_items), max_size=12))
def test_recent_live_records_never_raises_and_only_returns_live_records(entries):
    result = library.recent_live_records(entries)
    assert len(result) <= livetv.RECENT_LIMIT
    for record in result:
        assert livetv.is_live(record) and isinstance(record['_id'], str)
        assert not (record.get('removed') and not record.get('temp'))


@given(hostile_json(library_items), st.integers(min_value=0, max_value=2**40))
def test_live_helpers_never_raise_on_hostile_records(item, seconds):
    if not isinstance(item, dict):
        return
    when = datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc)
    merged = library.merge_live_playback(item, now=when)
    assert merged['state']['timeOffset'] == 0 and merged['state']['lastWatched'] == library.iso8601_utc(when)
    assert library.rewind_live(item, now=when)['state']['lastWatched'] is None


@given(library_item_lists)
def test_a_live_record_never_becomes_a_progress_entry(items):
    for item in items:
        if isinstance(item, dict) and livetv.is_live(item):
            assert library.progress_from_library_item(item) is None


def test_progress_from_library_item_skips_a_channel_that_has_offset_and_duration():
    record = _channel()
    assert record['state']['timeOffset'] and record['state']['duration']
    assert library.progress_from_library_item(record) is None
    movie = dict(record, type='movie')
    assert library.progress_from_library_item(movie) is not None


# --- Store recent channels --------------------------------------------------------


def _store(tmp_path):
    return Store(str(tmp_path / 'addon_data'))


def _meta(n, **extra):
    meta = {'id': 'ch%d' % n, 'type': 'tv', 'name': 'Channel %d' % n, 'poster': 'http://x/%d.png' % n,
            'posterShape': 'square'}
    meta.update(extra)
    return meta


def test_recent_channels_empty_when_never_set(tmp_path):
    assert _store(tmp_path).get_recent_channels() == []


def test_note_recent_channel_keeps_newest_first_and_dedupes(tmp_path):
    store = _store(tmp_path)
    store.note_recent_channel(_meta(1), '2026-01-01T00:00:00Z')
    store.note_recent_channel(_meta(2), '2026-01-02T00:00:00Z')
    store.note_recent_channel(_meta(1, name='Renamed'), '2026-01-03T00:00:00Z')
    channels = store.get_recent_channels()
    assert [c['id'] for c in channels] == ['ch1', 'ch2']
    assert channels[0] == {'id': 'ch1', 'type': 'tv', 'name': 'Renamed', 'poster': 'http://x/1.png',
                           'posterShape': 'square', 'last_watched': '2026-01-03T00:00:00Z'}


def test_note_recent_channel_caps_at_the_live_row_size(tmp_path):
    store = _store(tmp_path)
    for n in range(livetv.RECENT_LIMIT + 5):
        store.note_recent_channel(_meta(n))
    channels = store.get_recent_channels()
    assert len(channels) == livetv.RECENT_LIMIT
    assert channels[0]['id'] == 'ch%d' % (livetv.RECENT_LIMIT + 4)
    with open(os.path.join(store.data_dir, RECENT_CHANNELS_FILENAME)) as handle:
        assert len(json.load(handle)) == livetv.RECENT_LIMIT


def test_note_recent_channel_defaults_the_time_to_now_and_rejects_bad_meta(tmp_path):
    store = _store(tmp_path)
    assert store.note_recent_channel({'name': 'no id'}) is False
    assert store.note_recent_channel({'id': 5}) is False
    assert store.note_recent_channel('x') is False
    assert store.note_recent_channel({'id': 'a', 'name': 7, 'poster': 3}) is True
    entry = store.get_recent_channels()[0]
    assert entry['name'] == '' and entry['poster'] is None and entry['type'] == 'tv'
    assert entry['last_watched'].endswith('Z')


def test_note_recent_channel_keeps_a_corrupt_file_and_starts_over(tmp_path):
    store = _store(tmp_path)
    path = os.path.join(store.data_dir, RECENT_CHANNELS_FILENAME)
    with open(path, 'w') as handle:
        handle.write('{not json')
    assert store.get_recent_channels() == []
    assert store.note_recent_channel(_meta(1)) is True
    with open(path + '.corrupt') as handle:
        assert handle.read() == '{not json'
    assert [c['id'] for c in store.get_recent_channels()] == ['ch1']


def test_note_recent_channel_never_writes_over_an_unreadable_file(tmp_path, monkeypatch):
    import lib.store as store_module

    store = _store(tmp_path)
    store.note_recent_channel(_meta(1))
    path = os.path.join(store.data_dir, RECENT_CHANNELS_FILENAME)
    with open(path) as handle:
        before = handle.read()
    monkeypatch.setattr(store_module, '_read_text', lambda _path: (store_module._UNREADABLE, None))
    assert store.note_recent_channel(_meta(2)) is False
    monkeypatch.undo()
    with open(path) as handle:
        assert handle.read() == before


def test_hand_edited_recent_channels_do_not_break_reads_or_writes(tmp_path):
    store = _store(tmp_path)
    path = os.path.join(store.data_dir, RECENT_CHANNELS_FILENAME)
    with open(path, 'w') as handle:
        json.dump([5, None, {'id': 7}, {'id': ''}, {'id': 'ok', 'name': 'Ok'}, 'x'], handle)
    assert [c['id'] for c in store.get_recent_channels()] == ['ok']
    store.note_recent_channel(_meta(1))
    assert [c['id'] for c in store.get_recent_channels()] == ['ch1', 'ok']


def test_remove_recent_channel(tmp_path):
    store = _store(tmp_path)
    store.note_recent_channel(_meta(1))
    store.note_recent_channel(_meta(2))
    assert store.remove_recent_channel('ch1') is True
    assert store.remove_recent_channel('ch1') is False
    assert store.remove_recent_channel('nope') is False
    assert [c['id'] for c in store.get_recent_channels()] == ['ch2']


def test_remove_recent_channel_leaves_a_corrupt_file_alone(tmp_path):
    store = _store(tmp_path)
    path = os.path.join(store.data_dir, RECENT_CHANNELS_FILENAME)
    with open(path, 'w') as handle:
        handle.write('[1')
    assert store.remove_recent_channel('ch1') is False
    with open(path) as handle:
        assert handle.read() == '[1'
