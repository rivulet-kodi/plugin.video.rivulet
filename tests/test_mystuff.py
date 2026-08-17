"""Tests for lib.ui.mystuff's merge logic - the pure half of the merged
"My Stuff" screen (`merge_entries()`, `latest_by_title()`,
`percent_watched()`, `_band_for()`, `resolve_next_up()`), which takes
plain dicts and needs no Kodi runtime, no store, and no network.

The band boundaries here are the behaviour the merge exists to fix: the
old "Continue watching" row dropped a series the moment its last episode
crossed 95%, which is exactly when the viewer wants the NEXT episode -
see `test_finished_series_episode_lands_in_next_up_band` and the
`lib.ui.mystuff` module docstring.

`open_my_stuff()` itself (busy dialog, addon fan-out, grid) is not
exercised here: it is I/O and window orchestration, the same split
tests/test_librarywindow.py drew before this module replaced it.
"""
import contextlib

import pytest

from tests.kodistubs import install_kodi_stubs

_RELOAD_MODULE_NAMES = (
    'lib.ui.compat', 'lib.ui.dependencies', 'lib.ui.uicommon', 'lib.ui.router',
    'lib.ui.views', 'lib.ui.binge', 'lib.ui.gridwindow', 'lib.ui.detailwindow',
    'lib.ui.mystuff',
)


@pytest.fixture
def mystuff():
    """lib.ui.mystuff imports `get_store` (from lib.ui.dependencies) at
    module scope, which imports xbmc - so the module cannot be imported
    at file scope without the fake Kodi runtime in place. Installs fresh
    stubs (via tests.kodistubs.install_kodi_stubs) and returns the
    reloaded module, torn down automatically at test end."""
    with contextlib.ExitStack() as stack:
        yield stack.enter_context(install_kodi_stubs(reload=_RELOAD_MODULE_NAMES)).mystuff


def _progress(ctype, cid, percent, updated_at, video_id=None, duration_ms=1000):
    """A `Store.get_progress_entries()`-shaped dict at `percent` watched."""
    return {
        'type': ctype,
        'id': cid,
        'video_id': video_id,
        'position_ms': duration_ms * (percent / 100.0),
        'duration_ms': duration_ms,
        'updated_at': updated_at,
    }


# ---------------------------------------------------------------------------
# percent_watched()
# ---------------------------------------------------------------------------


def test_percent_watched_computes_percentage(mystuff):
    assert mystuff.percent_watched(_progress('movie', 'tt1', 25.0, 'x')) == pytest.approx(25.0)


@pytest.mark.parametrize('duration_ms', [0, None, 'nope', True])
def test_percent_watched_is_none_without_usable_duration(mystuff, duration_ms):
    """Mirrors `Store.get_progress_entries()`'s tolerance for a partly
    corrupt cache: an unusable entry is skipped, never raised on."""
    entry = _progress('movie', 'tt1', 25.0, 'x')
    entry['duration_ms'] = duration_ms
    assert mystuff.percent_watched(entry) is None


def test_percent_watched_rejects_bool_position(mystuff):
    entry = _progress('movie', 'tt1', 25.0, 'x')
    entry['position_ms'] = True
    assert mystuff.percent_watched(entry) is None


# ---------------------------------------------------------------------------
# _band_for()
# ---------------------------------------------------------------------------


@pytest.mark.parametrize('percent,ctype,expected', [
    (0.5, 'movie', None),            # barely started - dropped
    (0.99, 'movie', None),
    (1.0, 'movie', 'resume'),        # inclusive lower edge
    (62.0, 'movie', 'resume'),
    (94.99, 'movie', 'resume'),
    (95.0, 'movie', 'recent'),       # inclusive upper edge
    (99.7, 'movie', 'recent'),
    (95.0, 'series', 'next_up'),     # a finished EPISODE means next-up
    (99.7, 'series', 'next_up'),
    (62.0, 'series', 'resume'),      # mid-episode still resumes
    (None, 'movie', None),
])
def test_band_for_edges(mystuff, percent, ctype, expected):
    assert mystuff._band_for(percent, ctype) == expected


# ---------------------------------------------------------------------------
# latest_by_title()
# ---------------------------------------------------------------------------


def test_latest_by_title_keeps_only_most_recent_episode_per_series(mystuff):
    """A binge writes one progress entry per episode; the screen must show
    the series once, at its latest episode."""
    entries = [
        _progress('series', 'tt1', 99.5, '2026-08-15T10:00:00Z', video_id='tt1:1:1'),
        _progress('series', 'tt1', 99.7, '2026-08-16T05:00:00Z', video_id='tt1:1:2'),
        _progress('series', 'tt1', 40.0, '2026-08-14T10:00:00Z', video_id='tt1:1:3'),
    ]

    result = mystuff.latest_by_title(entries)

    assert len(result) == 1
    assert result[0]['video_id'] == 'tt1:1:2'


def test_latest_by_title_sorts_most_recent_first(mystuff):
    entries = [
        _progress('movie', 'old', 20.0, '2026-08-10T00:00:00Z'),
        _progress('movie', 'new', 20.0, '2026-08-16T00:00:00Z'),
        _progress('movie', 'mid', 20.0, '2026-08-13T00:00:00Z'),
    ]

    assert [e['id'] for e in mystuff.latest_by_title(entries)] == ['new', 'mid', 'old']


def test_latest_by_title_drops_entries_without_usable_duration(mystuff):
    entries = [_progress('movie', 'tt1', 20.0, 'x')]
    entries[0]['duration_ms'] = 0

    assert mystuff.latest_by_title(entries) == []


def test_latest_by_title_reduces_before_banding_so_a_finished_series_is_next_up(mystuff):
    """Regression guard for the ordering the module docstring calls out:
    reducing to the latest episode must happen BEFORE banding, or an older
    half-watched episode pins the show in `BAND_RESUME` forever even though
    the viewer has since finished a later one."""
    entries = [
        _progress('series', 'tt1', 40.0, '2026-08-10T00:00:00Z', video_id='tt1:1:1'),
        _progress('series', 'tt1', 99.0, '2026-08-16T00:00:00Z', video_id='tt1:1:2'),
    ]

    merged = mystuff.merge_entries(entries, [])

    assert [item['band'] for item in merged] == [mystuff.BAND_NEXT_UP]
    assert merged[0]['video_id'] == 'tt1:1:2'


# ---------------------------------------------------------------------------
# merge_entries() - banding and ordering
# ---------------------------------------------------------------------------


def test_merge_entries_orders_bands_resume_next_up_recent_library(mystuff):
    progress = [
        _progress('movie', 'watched', 98.0, '2026-08-16T04:00:00Z'),
        _progress('series', 'nextup', 99.0, '2026-08-16T03:00:00Z', video_id='nextup:1:1'),
        _progress('movie', 'resuming', 50.0, '2026-08-16T02:00:00Z'),
    ]
    library = [{'_id': 'saved', 'type': 'movie', 'name': 'Saved', 'poster': 'p'}]

    merged = mystuff.merge_entries(progress, library)

    assert [(item['id'], item['band']) for item in merged] == [
        ('resuming', mystuff.BAND_RESUME),
        ('nextup', mystuff.BAND_NEXT_UP),
        ('watched', mystuff.BAND_RECENT),
        ('saved', mystuff.BAND_LIBRARY),
    ]


def test_merge_entries_sorts_within_a_band_most_recent_first(mystuff):
    progress = [
        _progress('movie', 'older', 50.0, '2026-08-10T00:00:00Z'),
        _progress('movie', 'newer', 50.0, '2026-08-16T00:00:00Z'),
    ]

    merged = mystuff.merge_entries(progress, [])

    assert [item['id'] for item in merged] == ['newer', 'older']


def test_merge_entries_shows_a_title_in_both_sources_once_enriched_from_library(mystuff):
    """The whole point of the merge: a title that is both part-watched and
    saved appears once, in its played band, carrying the library's own
    name/poster so the grid can draw it with no addon fetch."""
    progress = [_progress('movie', 'tt1', 62.0, '2026-08-16T00:00:00Z')]
    library = [{'_id': 'tt1', 'type': 'movie', 'name': 'Dune', 'poster': 'poster.jpg',
                'background': 'bg.jpg'}]

    merged = mystuff.merge_entries(progress, library)

    assert len(merged) == 1
    assert merged[0]['band'] == mystuff.BAND_RESUME
    assert merged[0]['name'] == 'Dune'
    assert merged[0]['poster'] == 'poster.jpg'
    assert merged[0]['background'] == 'bg.jpg'
    assert merged[0]['percent'] == pytest.approx(62.0)


def test_merge_entries_matches_the_two_sources_on_type_and_id(mystuff):
    """A library movie and a played series that share an id are different
    titles and must not be merged into one card."""
    progress = [_progress('series', 'tt1', 50.0, '2026-08-16T00:00:00Z')]
    library = [{'_id': 'tt1', 'type': 'movie', 'name': 'A Movie'}]

    merged = mystuff.merge_entries(progress, library)

    assert [(item['type'], item['band']) for item in merged] == [
        ('series', mystuff.BAND_RESUME),
        ('movie', mystuff.BAND_LIBRARY),
    ]
    assert merged[0]['name'] is None  # not enriched from the unrelated movie


def test_merge_entries_drops_barely_started_titles(mystuff):
    progress = [_progress('movie', 'tt1', 0.5, '2026-08-16T00:00:00Z')]

    assert mystuff.merge_entries(progress, []) == []


def test_merge_entries_works_logged_out_with_no_library(mystuff):
    """The played bands are a pure local-cache read, so a logged-out device
    (no auth, hence no library) still gets a full screen - the state the
    old Library row could only answer with "Login to Stremio"."""
    progress = [_progress('movie', 'tt1', 62.0, '2026-08-16T00:00:00Z')]

    merged = mystuff.merge_entries(progress, [])

    assert [item['band'] for item in merged] == [mystuff.BAND_RESUME]


def test_merge_entries_works_with_empty_progress_cache(mystuff):
    library = [{'_id': 'tt1', 'type': 'movie', 'name': 'Saved'}]

    merged = mystuff.merge_entries([], library)

    assert [item['band'] for item in merged] == [mystuff.BAND_LIBRARY]


def test_merge_entries_drops_removed_library_entries(mystuff):
    library = [
        {'_id': 'gone', 'type': 'movie', 'removed': True},
        {'_id': 'kept', 'type': 'movie'},
        {'type': 'movie'},  # no _id
    ]

    assert [item['id'] for item in mystuff.merge_entries([], library)] == ['kept']


def test_merge_entries_caps_played_and_library_bands(mystuff):
    progress = [
        _progress('movie', 'tt%d' % i, 50.0, '2026-08-%02dT00:00:00Z' % (i + 1))
        for i in range(mystuff.MAX_PLAYED_ITEMS + 10)
    ]
    library = [{'_id': 'lib%d' % i, 'type': 'movie'} for i in range(mystuff.MAX_LIBRARY_ITEMS + 10)]

    merged = mystuff.merge_entries(progress, library)

    played = [i for i in merged if i['band'] != mystuff.BAND_LIBRARY]
    saved = [i for i in merged if i['band'] == mystuff.BAND_LIBRARY]
    assert len(played) == mystuff.MAX_PLAYED_ITEMS
    assert len(saved) == mystuff.MAX_LIBRARY_ITEMS


def test_merge_entries_tolerates_none_sources(mystuff):
    assert mystuff.merge_entries(None, None) == []


# ---------------------------------------------------------------------------
# resolve_next_up()
# ---------------------------------------------------------------------------


def test_resolve_next_up_returns_the_following_episode(mystuff):
    item = {'band': mystuff.BAND_NEXT_UP, 'video_id': 'tt1:1:2'}
    meta = {'videos': [
        {'id': 'tt1:1:1', 'season': 1, 'episode': 1},
        {'id': 'tt1:1:2', 'season': 1, 'episode': 2},
        {'id': 'tt1:1:3', 'season': 1, 'episode': 3},
    ]}

    assert mystuff.resolve_next_up(item, meta)['id'] == 'tt1:1:3'


def test_resolve_next_up_returns_none_after_the_final_episode(mystuff):
    item = {'band': mystuff.BAND_NEXT_UP, 'video_id': 'tt1:1:2'}
    meta = {'videos': [
        {'id': 'tt1:1:1', 'season': 1, 'episode': 1},
        {'id': 'tt1:1:2', 'season': 1, 'episode': 2},
    ]}

    assert mystuff.resolve_next_up(item, meta) is None


@pytest.mark.parametrize('item', [
    None,
    {'band': 'resume', 'video_id': 'tt1:1:2'},
    {'band': 'next_up', 'video_id': None},
])
def test_resolve_next_up_returns_none_outside_the_next_up_band(mystuff, item):
    meta = {'videos': [{'id': 'tt1:1:2', 'season': 1, 'episode': 2},
                       {'id': 'tt1:1:3', 'season': 1, 'episode': 3}]}

    assert mystuff.resolve_next_up(item, meta) is None
