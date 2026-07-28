"""Tests for lib.ui.binge: pure "what plays next" logic, no Kodi imports
and no tests/kodistubs fakes needed - every case here is a plain dict in,
plain dict/tuple out.
"""
from lib.ui.binge import next_video, pick_binge_stream


def _video(video_id, season=None, episode=None, released=None, **extra):
    video = {'id': video_id}
    if season is not None:
        video['season'] = season
    if episode is not None:
        video['episode'] = episode
    if released is not None:
        video['released'] = released
    video.update(extra)
    return video


# ---------------------------------------------------------------------------
# next_video()
# ---------------------------------------------------------------------------


def test_next_video_returns_the_following_episode_within_the_same_season():
    meta = {'videos': [
        _video('s1e1', 1, 1),
        _video('s1e2', 1, 2),
        _video('s1e3', 1, 3),
    ]}
    assert next_video(meta, 's1e1')['id'] == 's1e2'


def test_next_video_crosses_a_season_boundary_to_the_next_seasons_first_episode():
    meta = {'videos': [
        _video('s1e1', 1, 1),
        _video('s1e2', 1, 2),
        _video('s2e1', 2, 1),
        _video('s2e2', 2, 2),
    ]}
    assert next_video(meta, 's1e2')['id'] == 's2e1'


def test_next_video_ignores_input_order_and_sorts_by_season_then_episode():
    # Deliberately shuffled - next_video() must sort before walking, not
    # trust meta['videos']'s own (addon-controlled) array order.
    meta = {'videos': [
        _video('s2e1', 2, 1),
        _video('s1e2', 1, 2),
        _video('s1e1', 1, 1),
    ]}
    assert next_video(meta, 's1e1')['id'] == 's1e2'
    assert next_video(meta, 's1e2')['id'] == 's2e1'


def test_next_video_returns_none_for_the_last_episode_overall():
    meta = {'videos': [_video('s1e1', 1, 1), _video('s1e2', 1, 2)]}
    assert next_video(meta, 's1e2') is None


def test_next_video_returns_none_when_current_video_id_is_not_found():
    meta = {'videos': [_video('s1e1', 1, 1), _video('s1e2', 1, 2)]}
    assert next_video(meta, 'does-not-exist') is None


def test_next_video_returns_none_for_an_empty_videos_list():
    assert next_video({'videos': []}, 's1e1') is None


def test_next_video_returns_none_when_videos_key_is_missing():
    assert next_video({'id': 'tt1', 'type': 'series'}, 's1e1') is None


def test_next_video_returns_none_for_a_none_meta():
    assert next_video(None, 's1e1') is None


def test_next_video_does_not_advance_into_specials_after_a_regular_episode():
    # The last regular-season episode's "next" slot in sorted order IS the
    # first Special (season 0 sorts last) - the guard must reject it, not
    # silently treat "watch the finale" as "watch a Special next".
    meta = {'videos': [
        _video('s1e1', 1, 1),
        _video('s1e2', 1, 2),
        _video('special1', 0, 1),
        _video('special2', 0, 2),
    ]}
    assert next_video(meta, 's1e2') is None


def test_next_video_allows_continuing_from_one_special_into_the_next():
    meta = {'videos': [
        _video('s1e1', 1, 1),
        _video('special1', 0, 1),
        _video('special2', 0, 2),
    ]}
    assert next_video(meta, 'special1')['id'] == 'special2'


def test_next_video_handles_missing_series_info_without_crashing():
    # A malformed/partial addon response: no 'season'/'episode' keys at
    # all on some entries. Must not raise, and must never be offered as
    # "next" right after a real episode (same rule as a literal Special).
    meta = {'videos': [
        _video('s1e1', 1, 1),
        _video('s1e2', 1, 2),
        {'id': 'no-series-info'},
    ]}
    assert next_video(meta, 's1e2') is None


def test_next_video_missing_series_info_entry_is_never_a_next_target_itself():
    # 'no-series-info' sorts to the very end (same bucket as Specials) -
    # nothing follows it either.
    meta = {'videos': [
        _video('s1e1', 1, 1),
        {'id': 'no-series-info'},
    ]}
    assert next_video(meta, 'no-series-info') is None


def test_next_video_handles_partial_series_info_missing_episode_number():
    # season present, episode missing - defaults to episode 0, sorting
    # before every numbered episode of the same season.
    meta = {'videos': [
        {'id': 'season-only', 'season': 1},
        _video('s1e1', 1, 1),
        _video('s1e2', 1, 2),
    ]}
    assert next_video(meta, 'season-only')['id'] == 's1e1'


def test_next_video_tiebreaks_by_released_date_when_season_episode_are_equal():
    meta = {'videos': [
        _video('newer', 0, 1, released='2020-02-01T00:00:00.000Z'),
        _video('older', 0, 1, released='2020-01-01T00:00:00.000Z'),
    ]}
    assert next_video(meta, 'older')['id'] == 'newer'


# ---------------------------------------------------------------------------
# pick_binge_stream()
# ---------------------------------------------------------------------------


def test_pick_binge_stream_prefers_the_pair_matching_the_played_binge_group():
    pairs = [
        ({'binge_group': 'other'}, 'stream-a'),
        ({'binge_group': 'rivulet|1080p'}, 'stream-b'),
        ({'binge_group': 'rivulet|1080p'}, 'stream-c'),
    ]
    info, stream = pick_binge_stream(pairs, 'rivulet|1080p')
    assert stream == 'stream-b'  # first match, not just any match
    assert info['binge_group'] == 'rivulet|1080p'


def test_pick_binge_stream_falls_back_to_the_first_pair_when_nothing_matches():
    pairs = [({'binge_group': 'a'}, 'stream-a'), ({'binge_group': 'b'}, 'stream-b')]
    assert pick_binge_stream(pairs, 'no-such-group') == pairs[0]


def test_pick_binge_stream_falls_back_to_the_first_pair_when_binge_group_is_none():
    pairs = [({'binge_group': None}, 'stream-a'), ({'binge_group': 'b'}, 'stream-b')]
    assert pick_binge_stream(pairs, None) == pairs[0]


def test_pick_binge_stream_falls_back_to_the_first_pair_when_binge_group_is_empty_string():
    pairs = [({'binge_group': None}, 'stream-a'), ({'binge_group': 'b'}, 'stream-b')]
    assert pick_binge_stream(pairs, '') == pairs[0]


def test_pick_binge_stream_returns_none_for_an_empty_pairs_list():
    assert pick_binge_stream([], 'rivulet|1080p') is None
