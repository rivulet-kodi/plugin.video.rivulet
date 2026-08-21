"""Tests for lib.newepisodes: pure "what's new to watch" logic, no Kodi
imports, no store, no addon client - every branch is exercised here with
plain dicts and a fixed `now`, mirroring tests/test_binge.py's style for
the other pure "what plays next" module.
"""
import datetime

from lib.newepisodes import MAX_SEEN_EPISODES, mark_seen, new_episodes

NOW = datetime.datetime(2024, 6, 15, 12, 0, 0)
PAST = "2024-06-01T00:00:00Z"
FUTURE = "2024-06-20T00:00:00Z"


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


def _meta(name='Show', poster='p.jpg', background='b.jpg', videos=None):
    meta = {'name': name, 'poster': poster, 'background': background}
    if videos is not None:
        meta['videos'] = videos
    return meta


def _series(sid='tt1', video_id=None, stype='series'):
    return {'type': stype, 'id': sid, 'video_id': video_id}


# ---------------------------------------------------------------------------
# new_episodes()
# ---------------------------------------------------------------------------


def test_new_episodes_returns_empty_for_no_series_items():
    assert new_episodes([], {}, {}, NOW) == []


def test_new_episodes_excludes_an_unaired_episode():
    meta = _meta(videos=[_video('s1e1', 1, 1, PAST), _video('s1e2', 1, 2, FUTURE)])
    result = new_episodes([_series(video_id='s1e1')], {'tt1': meta}, {}, NOW)
    assert result == []


def test_new_episodes_includes_an_aired_episode_after_last_watched():
    meta = _meta(videos=[_video('s1e1', 1, 1, PAST), _video('s1e2', 1, 2, PAST)])
    result = new_episodes([_series(video_id='s1e1')], {'tt1': meta}, {}, NOW)
    assert [e['video_id'] for e in result] == ['s1e2']


def test_new_episodes_excludes_an_already_watched_episode():
    # s1e1 is the last-watched pointer itself - it must never be re-offered.
    meta = _meta(videos=[_video('s1e1', 1, 1, PAST)])
    result = new_episodes([_series(video_id='s1e1')], {'tt1': meta}, {}, NOW)
    assert result == []


def test_new_episodes_excludes_an_episode_before_the_last_watched_pointer():
    meta = _meta(videos=[_video('s1e1', 1, 1, PAST), _video('s1e2', 1, 2, PAST)])
    # Last-watched is s1e2; s1e1 sorts before it and must not resurface.
    result = new_episodes([_series(video_id='s1e2')], {'tt1': meta}, {}, NOW)
    assert result == []


def test_new_episodes_excludes_an_already_seen_episode():
    meta = _meta(videos=[
        _video('s1e1', 1, 1, PAST), _video('s1e2', 1, 2, PAST), _video('s1e3', 1, 3, PAST),
    ])
    seen = {'series\x1ftt1\x1fs1e2': True}
    result = new_episodes([_series(video_id='s1e1')], {'tt1': meta}, seen, NOW)
    assert [e['video_id'] for e in result] == ['s1e3']


def test_new_episodes_tolerates_a_missing_released_date_without_raising():
    meta = _meta(videos=[_video('s1e1', 1, 1, PAST), _video('s1e2', 1, 2, None)])
    result = new_episodes([_series(video_id='s1e1')], {'tt1': meta}, {}, NOW)
    assert result == []  # unparseable/missing date is never treated as aired


def test_new_episodes_tolerates_a_malformed_released_date_without_raising():
    meta = _meta(videos=[
        _video('s1e1', 1, 1, PAST), _video('s1e2', 1, 2, 'not-a-date'),
    ])
    result = new_episodes([_series(video_id='s1e1')], {'tt1': meta}, {}, NOW)
    assert result == []


def test_new_episodes_tolerates_a_series_with_no_videos_key():
    result = new_episodes([_series()], {'tt1': _meta(videos=None)}, {}, NOW)
    assert result == []


def test_new_episodes_tolerates_a_series_missing_from_metas():
    result = new_episodes([_series()], {}, {}, NOW)
    assert result == []


def test_new_episodes_tolerates_a_meta_that_is_not_a_dict():
    result = new_episodes([_series()], {'tt1': None}, {}, NOW)
    assert result == []


def test_new_episodes_never_watched_series_has_no_lower_bound():
    # No progress at all for this series - every aired episode qualifies.
    meta = _meta(videos=[_video('s1e1', 1, 1, PAST), _video('s1e2', 1, 2, PAST)])
    result = new_episodes([_series(video_id=None)], {'tt1': meta}, {}, NOW)
    assert {e['video_id'] for e in result} == {'s1e1', 's1e2'}


def test_new_episodes_skips_a_series_whose_pointer_id_is_absent_from_videos():
    # video_id is set (unlike the never-watched case above) but names an
    # id this meta's videos no longer carries - addon id scheme changed,
    # a season got trimmed, or a stale progress entry. Unresolvable must
    # fail closed (skip the series), never fall through to "no lower
    # bound" and re-offer episodes the user already watched.
    meta = _meta(videos=[_video('s1e1', 1, 1, PAST), _video('s1e2', 1, 2, PAST)])
    result = new_episodes([_series(video_id='does-not-exist')], {'tt1': meta}, {}, NOW)
    assert result == []


def test_new_episodes_excludes_a_special_when_not_already_watching_specials():
    meta = _meta(videos=[
        _video('s1e1', 1, 1, PAST), _video('s1e2', 1, 2, PAST), _video('special1', 0, 1, PAST),
    ])
    result = new_episodes([_series(video_id='s1e1')], {'tt1': meta}, {}, NOW)
    assert [e['video_id'] for e in result] == ['s1e2']


def test_new_episodes_includes_a_special_when_last_watched_was_also_a_special():
    meta = _meta(videos=[_video('special1', 0, 1, PAST), _video('special2', 0, 2, PAST)])
    result = new_episodes([_series(video_id='special1')], {'tt1': meta}, {}, NOW)
    assert [e['video_id'] for e in result] == ['special2']


def test_new_episodes_treats_missing_season_and_episode_like_a_special():
    # No season/episode info at all is bucketed with Specials (mirrors
    # lib.ui.binge.next_video's guard) - never advertised right after a
    # real episode.
    meta = _meta(videos=[
        _video('s1e1', 1, 1, PAST), _video('no-info', released=PAST),
    ])
    result = new_episodes([_series(video_id='s1e1')], {'tt1': meta}, {}, NOW)
    assert result == []


def test_new_episodes_sorts_out_of_order_episode_numbering():
    # Deliberately shuffled - new_episodes() must sort before walking, not
    # trust meta['videos']'s own (addon-controlled) array order.
    meta = _meta(videos=[
        _video('s1e3', 1, 3, PAST), _video('s1e1', 1, 1, PAST), _video('s1e2', 1, 2, PAST),
    ])
    result = new_episodes([_series(video_id='s1e1')], {'tt1': meta}, {}, NOW)
    assert [e['video_id'] for e in result] == ['s1e2', 's1e3']


def test_new_episodes_drops_malformed_video_entries():
    meta = _meta(videos=[
        _video('s1e1', 1, 1, PAST), 'not-a-dict', {'season': 1, 'episode': 2},  # no 'id'
    ])
    result = new_episodes([_series(video_id='s1e1')], {'tt1': meta}, {}, NOW)
    assert result == []  # nothing usable follows s1e1


def test_new_episodes_covers_every_followed_series_independently():
    meta1 = _meta(videos=[_video('a1', 1, 1, PAST), _video('a2', 1, 2, PAST)])
    meta2 = _meta(videos=[_video('b1', 1, 1, PAST), _video('b2', 1, 2, PAST)])
    result = new_episodes(
        [_series(sid='tt1', video_id='a1'), _series(sid='tt2', video_id='b1')],
        {'tt1': meta1, 'tt2': meta2}, {}, NOW,
    )
    assert {(e['id'], e['video_id']) for e in result} == {('tt1', 'a2'), ('tt2', 'b2')}


def test_new_episodes_result_carries_display_fields_from_the_series_meta():
    meta = _meta(name='A Show', poster='poster.jpg', background='bg.jpg',
                 videos=[_video('s1e1', 1, 1, PAST), _video('s1e2', 1, 2, PAST)])
    result = new_episodes([_series(video_id='s1e1')], {'tt1': meta}, {}, NOW)
    assert result == [{
        'type': 'series', 'id': 'tt1', 'video_id': 's1e2', 'season': 1, 'episode': 2,
        'released': PAST, 'name': 'A Show', 'poster': 'poster.jpg', 'background': 'bg.jpg',
    }]


def test_new_episodes_ignores_a_series_item_missing_type_or_id():
    meta = _meta(videos=[_video('s1e1', 1, 1, PAST)])
    assert new_episodes([{'type': 'series', 'id': None, 'video_id': None}],
                         {'tt1': meta}, {}, NOW) == []
    assert new_episodes([{'type': None, 'id': 'tt1', 'video_id': None}],
                         {'tt1': meta}, {}, NOW) == []


# ---------------------------------------------------------------------------
# mark_seen()
# ---------------------------------------------------------------------------


def _episode(sid='tt1', video_id='s1e1'):
    return {'type': 'series', 'id': sid, 'video_id': video_id}


def test_mark_seen_adds_the_given_episodes_keys():
    updated = mark_seen({}, [_episode(video_id='s1e1'), _episode(video_id='s1e2')])
    assert set(updated) == {'series\x1ftt1\x1fs1e1', 'series\x1ftt1\x1fs1e2'}


def test_mark_seen_preserves_existing_entries_not_touched_this_call():
    existing = {'series\x1ftt1\x1fs1e1': True}
    updated = mark_seen(existing, [_episode(video_id='s1e2')])
    assert set(updated) == {'series\x1ftt1\x1fs1e1', 'series\x1ftt1\x1fs1e2'}


def test_mark_seen_marking_the_same_episode_twice_does_not_duplicate():
    once = mark_seen({}, [_episode(video_id='s1e1')])
    twice = mark_seen(once, [_episode(video_id='s1e1')])
    assert twice == {'series\x1ftt1\x1fs1e1': True}


def test_mark_seen_ignores_malformed_episode_dicts():
    updated = mark_seen({}, [None, 'not-a-dict', {'type': 'series', 'id': None}])
    assert updated == {}


def test_mark_seen_returns_a_new_dict_without_mutating_the_input():
    existing = {'series\x1ftt1\x1fs1e1': True}
    updated = mark_seen(existing, [_episode(video_id='s1e2')])
    assert existing == {'series\x1ftt1\x1fs1e1': True}
    assert updated is not existing


def test_mark_seen_caps_at_the_boundary_evicting_the_oldest_first():
    seen = {'series\x1ftt1\x1fs1e%d' % i: True for i in range(MAX_SEEN_EPISODES)}
    oldest_key = next(iter(seen))
    updated = mark_seen(seen, [_episode(sid='tt2', video_id='new')])
    assert len(updated) == MAX_SEEN_EPISODES
    assert oldest_key not in updated
    assert 'series\x1ftt2\x1fnew' in updated


def test_mark_seen_never_exceeds_the_cap_across_repeated_calls():
    seen = {}
    for i in range(MAX_SEEN_EPISODES + 50):
        seen = mark_seen(seen, [_episode(video_id='s1e%d' % i)])
    assert len(seen) == MAX_SEEN_EPISODES


def test_mark_seen_of_new_episodes_output_hides_it_from_a_later_new_episodes_call():
    meta = _meta(videos=[_video('s1e1', 1, 1, PAST), _video('s1e2', 1, 2, PAST)])
    series_items = [_series(video_id='s1e1')]
    metas = {'tt1': meta}
    first_pass = new_episodes(series_items, metas, {}, NOW)
    assert [e['video_id'] for e in first_pass] == ['s1e2']

    seen = mark_seen({}, first_pass)
    second_pass = new_episodes(series_items, metas, seen, NOW)
    assert second_pass == []
