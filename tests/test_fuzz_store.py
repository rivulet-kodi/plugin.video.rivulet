"""Property tests for ``lib.store`` (and ``lib.stremio.jsonfile``) against
garbage on disk: ``addon_data`` is a plain directory a user, a half-written
sync or a failing SD card can leave anything in, and the persistence layer's
contract is three-way - a *missing* file is seeded or empty, a *corrupt* one
is served as the default and kept as ``<name>.corrupt`` before anything
replaces it, and one that is merely *unreadable* is never written over.
"""
import json
import os
import tempfile

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from lib import library, store
from lib.stremio import jsonfile
from tests.fuzz_strategies import (
    addon_descriptors,
    json_values,
    library_items,
    noisy_record,
)

_RECOVERY_SUFFIXES = {store.HOME_LAYOUT_FILENAME: '.bak'}

#: Nesting deep enough to exhaust the interpreter's recursion limit inside
#: `json.loads`, which raises `RecursionError`, not `ValueError`. Never fed
#: through Hypothesis, which raises the recursion limit around each example:
#: on CPython 3.8 a `RecursionError` there could abort the interpreter
#: (`test (3.8)` core-dumped in CI). `test_deep_nesting_is_corrupt` runs
#: the properties on it directly.
_DEEP = b'[' * 200_000 + b']' * 200_000


def _dumps(value):
    return json.dumps(value).encode('utf-8')


_progress_files = st.dictionaries(
    st.lists(st.text(max_size=6), min_size=3, max_size=3).map('\x1f'.join) | st.text(max_size=8),
    noisy_record(position_ms=st.integers(), duration_ms=st.integers(), updated_at=st.text(max_size=24)),
    max_size=4,
)

#: What a data file may hold: arbitrary bytes, a truncated write, arbitrary
#: JSON, a deeply nested document, or a realistic file with wrong-typed fields.
file_contents = st.one_of(
    st.binary(max_size=64),
    st.just(b''),
    st.just(b'{"offset_ms": 1e400, "version": 1e999}'),
    json_values.map(_dumps),
    json_values.map(_dumps).flatmap(lambda blob: st.integers(0, len(blob)).map(lambda n: blob[:n])),
    st.lists(addon_descriptors, max_size=3).map(_dumps),
    _progress_files.map(_dumps),
    st.lists(library_items, max_size=3).map(_dumps),
)


def _valid(content, expected_type):
    try:
        return isinstance(json.loads(content.decode('utf-8')), expected_type)
    except (ValueError, RecursionError):
        return False


def _setup(directory, filename, content):
    path = os.path.join(directory, filename)
    with open(path, 'wb') as handle:
        handle.write(content)
    return path


def _read_bytes(path):
    with open(path, 'rb') as handle:
        return handle.read()


def assert_recoverable(path, original, suffix='.corrupt'):
    """The user's bytes are still on disk: untouched, or kept beside it."""
    if os.path.exists(path) and _read_bytes(path) == original:
        return
    assert os.path.exists(path + suffix), 'corrupt file replaced without a %s copy' % suffix
    assert _read_bytes(path + suffix) == original


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _reads_addons(s):
    for getter in (s.get_addons, s.get_enabled_addons):
        result = getter()
        assert isinstance(result, list) and all(isinstance(entry, dict) for entry in result)


def _reads_auth(s):
    assert s.get_auth() is None or isinstance(s.get_auth(), dict)


def _reads_search_history(s):
    history = s.get_search_history()
    assert all(isinstance(query, str) for query in history)


def _reads_now_playing(s):
    assert s.get_now_playing() is None or isinstance(s.get_now_playing(), dict)


def _reads_resume_offset(s):
    offset = s.get_resume_offset_ms()
    assert offset is None or _is_number(offset)


def _reads_last_version(s):
    version = s.get_last_seen_version()
    assert version is None or isinstance(version, str)


def _reads_progress(s):
    assert s.get_progress('movie', 'tt1') is None or isinstance(s.get_progress('movie', 'tt1'), dict)
    for entry in s.get_progress_entries():
        assert set(entry) == {'type', 'id', 'video_id', 'position_ms', 'duration_ms', 'updated_at'}
        assert _is_number(entry['position_ms']) and _is_number(entry['duration_ms'])
        # Downstream readers run on whatever survives the filter.
        library.progress_finished(entry)
    library.watched_title_ids({}, s.get_progress_entries())


def _reads_dismissals(s):
    for (content_type, content_id), dismissed_at in s.get_continue_dismissals().items():
        assert isinstance(content_type, str) and isinstance(content_id, str)
        assert isinstance(dismissed_at, str)


def _reads_library(s):
    records = s.get_library_cache()
    assert isinstance(records, list)
    library.title_verdicts(records)


def _reads_home_layout(s):
    from lib import homelayout

    layout = s.get_home_layout()
    assert homelayout.normalize(layout) == layout


#: file -> (value type the store requires of it, the read that must survive it)
FILES = {
    store.ADDONS_FILENAME: (list, _reads_addons),
    store.AUTH_FILENAME: (dict, _reads_auth),
    store.SEARCH_HISTORY_FILENAME: (list, _reads_search_history),
    store.NOW_PLAYING_FILENAME: (dict, _reads_now_playing),
    store.RESUME_OFFSET_FILENAME: (dict, _reads_resume_offset),
    store.LAST_VERSION_FILENAME: (dict, _reads_last_version),
    store.PROGRESS_FILENAME: (dict, _reads_progress),
    store.PROGRESS_DISMISSED_FILENAME: (dict, _reads_dismissals),
    store.LIBRARY_FILENAME: (list, _reads_library),
    store.HOME_LAYOUT_FILENAME: (dict, _reads_home_layout),
}


@pytest.mark.parametrize('filename', sorted(FILES))
@given(content=file_contents)
@example(content=b'\xff\xfe\x00garbage')
@example(content=b'{"version": 1e999, "offset_ms": "7"}')
@example(content=b'[{"_id": []}, {"_id": {}}, 5]')
@example(content=b'[{"flags": true}, {"flags": "x"}, {"flags": [1]}]')
def test_getters_serve_defaults_for_any_file_content_and_keep_the_bytes(filename, content):
    expected_type, read = FILES[filename]
    with tempfile.TemporaryDirectory() as directory:
        path = _setup(directory, filename, content)
        read(store.Store(directory))
        assert_recoverable(path, content, _RECOVERY_SUFFIXES.get(filename, '.corrupt'))
        if filename not in (store.ADDONS_FILENAME, store.HOME_LAYOUT_FILENAME):
            # Only addons.json (reseeded) and the layout (moved aside) are
            # ever replaced by a read; every other getter leaves the file alone.
            assert _read_bytes(path) == content
        # A read leaves no stray temp files behind.
        assert [n for n in os.listdir(directory) if n.startswith('.tmp-')] == []


@given(content=file_contents)
def test_writers_over_a_corrupt_progress_file_keep_it_as_dot_corrupt(content):
    with tempfile.TemporaryDirectory() as directory:
        path = _setup(directory, store.PROGRESS_FILENAME, content)
        s = store.Store(directory)
        s.set_progress('movie', 'tt1', None, 5, 10, library.iso8601_utc())
        s.merge_remote_progress([{
            'type': 'movie', 'id': 'tt2', 'video_id': None, 'position_ms': 1, 'duration_ms': 2,
            'updated_at': '2099-01-01T00:00:00Z',
        }])
        if not _valid(content, dict):
            assert_recoverable(path, content)
        # Whatever was there, the file is now one the store reads back.
        assert s.get_progress('movie', 'tt1')['position_ms'] == 5


@given(content=file_contents)
def test_writers_over_a_corrupt_addons_file_keep_it_as_dot_corrupt(content):
    with tempfile.TemporaryDirectory() as directory:
        path = _setup(directory, store.ADDONS_FILENAME, content)
        s = store.Store(directory)
        installed = s.install_addon('https://x.example/manifest.json', {'id': 'x', 'name': 'X'})
        if not _valid(content, list):
            assert_recoverable(path, content)
        assert any(a.get('transportUrl') == 'https://x.example/manifest.json' for a in s.get_addons())
        assert installed is None or isinstance(installed, (dict, list))


@given(content=file_contents)
def test_incremental_library_merge_never_overwrites_a_file_it_cannot_parse(content):
    with tempfile.TemporaryDirectory() as directory:
        path = _setup(directory, store.LIBRARY_FILENAME, content)
        s = store.Store(directory)
        record = library.slim_library_item({'_id': 'tt1', 'type': 'movie', '_mtime': '2026-01-01T00:00:00Z'})
        merged = s.merge_library_cache([record], live_ids=['tt1'])
        if _valid(content, list):
            assert merged is True
        else:
            assert merged is False
            assert_recoverable(path, content)
            assert _read_bytes(path) == content, 'the incremental merge wrote over a file it could not use'


@given(content=file_contents)
def test_jsonfile_read_json_is_none_or_json_for_any_content(content):
    with tempfile.TemporaryDirectory() as directory:
        path = _setup(directory, 'cache.json', content)
        jsonfile.read_json(path)
        assert _read_bytes(path) == content


# --- unreadable (as opposed to corrupt) -----------------------------------------


@pytest.mark.parametrize('filename', sorted(FILES))
def test_an_unreadable_file_is_never_replaced(filename):
    """A directory where the file should be: opening it raises an OSError
    that is not "not found" (the shape of a Windows sharing violation or a
    flaky Android mount). The content may be perfectly good, so nothing may
    be written over it and nothing may be moved aside as corrupt."""
    _expected_type, read = FILES[filename]
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, filename)
        os.mkdir(path)
        s = store.Store(directory)
        read(s)
        if filename == store.PROGRESS_FILENAME:
            s.set_progress('movie', 'tt1', None, 5, 10, library.iso8601_utc())
            assert s.merge_remote_progress([{
                'type': 'movie', 'id': 'tt2', 'video_id': None, 'position_ms': 1, 'duration_ms': 2,
                'updated_at': '2099-01-01T00:00:00Z',
            }]) == 0
        if filename == store.LIBRARY_FILENAME:
            assert s.merge_library_cache([], live_ids=[]) is False
        if filename == store.ADDONS_FILENAME:
            with pytest.raises(store.ConcurrentUpdateError):
                s.update_addons(lambda current: current + [{'transportUrl': 'x'}])
        assert os.path.isdir(path)
        assert sorted(os.listdir(directory)) == [filename]


@pytest.mark.parametrize('check', [
    lambda: [test_getters_serve_defaults_for_any_file_content_and_keep_the_bytes.hypothesis.inner_test(
        filename, _DEEP) for filename in sorted(FILES)],
    lambda: test_writers_over_a_corrupt_progress_file_keep_it_as_dot_corrupt.hypothesis.inner_test(_DEEP),
    lambda: test_writers_over_a_corrupt_addons_file_keep_it_as_dot_corrupt.hypothesis.inner_test(_DEEP),
    lambda: test_incremental_library_merge_never_overwrites_a_file_it_cannot_parse.hypothesis.inner_test(_DEEP),
    lambda: test_jsonfile_read_json_is_none_or_json_for_any_content.hypothesis.inner_test(_DEEP),
], ids=['getters', 'progress_writer', 'addons_writer', 'library_merge', 'jsonfile'])
def test_deep_nesting_is_corrupt(check):
    check()
