# SPDX-FileCopyrightText: 2026 M0Rf30
# SPDX-License-Identifier: MIT

"""Round-trip against the stremio-watched-bitfield wire format.

The pinned string was produced by the official JavaScript library
(``lib/watchedBitfield.js`` + pako), not by this port: ids
``['tt1','tt1:1:1','tt1:1:2','tt1:1:3']``, bits ``[false, true, false, true]``.
"""
from lib.stremio.watchedbitfield import (
    WatchedBitfield,
    set_all_watched,
    sorted_video_ids,
    video_sort_key,
    watched_video_ids,
)

# Official serialize() of that field. Anchor is the last watched id.
SERIAL = 'tt1:1:3:4:eJzjAgAACwAL'
IDS = ['tt1', 'tt1:1:1', 'tt1:1:2', 'tt1:1:3']


def test_official_serial_round_trip():
    assert watched_video_ids(SERIAL, IDS) == {'tt1:1:1', 'tt1:1:3'}
    assert WatchedBitfield.construct_and_resize(SERIAL, IDS).serialize() == SERIAL


def test_construct_from_array_matches_the_official_serial():
    field = WatchedBitfield.construct_from_array([False, True, False, False], IDS)
    field.set_video('tt1:1:3', True)
    assert field.serialize() == SERIAL
    assert [field.get_video(video_id) for video_id in IDS] == [False, True, False, True]


def test_resize_shifts_when_a_video_is_prepended():
    shifted = ['new'] + IDS
    field = WatchedBitfield.construct_and_resize(SERIAL, shifted)
    assert field.anchor_not_found is False
    assert [field.get_video(video_id) for video_id in shifted] == [False, False, True, False, True]


def test_unknown_anchor_serializes_blank_and_writer_refuses_it():
    blank = WatchedBitfield.construct_from_array([False], ['nope'])
    assert blank.serialize() == 'nope:1:eJxjAAAAAQAB'
    loaded = WatchedBitfield.construct_and_resize(SERIAL, ['nope'])
    assert loaded.anchor_not_found is True
    assert set_all_watched(SERIAL, ['nope'], True) is None
    assert watched_video_ids(SERIAL, ['nope']) is None


def test_unparseable_string_is_not_replaced():
    assert set_all_watched('no-colons', IDS, True) is None
    assert set_all_watched('tt1:1:1:4:!!!not-base64!!!', IDS, True) is None
    assert watched_video_ids('no-colons', IDS) is None


def test_fresh_field_marks_every_episode():
    serial = set_all_watched(None, IDS, True)
    assert watched_video_ids(serial, IDS) == set(IDS)


def test_sort_puts_missing_series_info_before_season_zero():
    videos = [
        {'id': 's1', 'season': 1, 'episode': 1, 'released': '2020-01-01'},
        {'id': 'special', 'season': 0, 'episode': 1, 'released': '2019-01-01'},
        {'id': 'bare'},
        {'id': 's1-later', 'season': 1, 'episode': 1, 'released': '2021-01-01'},
    ]
    assert sorted_video_ids(videos) == ['bare', 'special', 's1', 's1-later']
    assert video_sort_key({'id': 'bare'}) < video_sort_key({'id': 'special', 'season': 0, 'episode': 1})
