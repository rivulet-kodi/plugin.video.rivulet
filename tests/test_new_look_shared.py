# SPDX-FileCopyrightText: 2026 M0Rf30
# SPDX-License-Identifier: MIT

"""Hero rendering and shared Home helpers for the single Home."""
import datetime

import pytest

from lib import library
from lib.newepisodes import _episode_label, episode_still_url
from lib.ui.playbackmeta import (
    episode_label_from_id,
    format_resume_clock,
    hero_panel_text,
    new_look_hero_parts,
)

NOW = datetime.datetime(2020, 1, 1, 0, 0, 0, tzinfo=datetime.timezone.utc)
LATER = datetime.datetime(2020, 1, 1, 0, 5, 0, tzinfo=datetime.timezone.utc)
NOW_ISO = '2020-01-01T00:00:00Z'
LATER_ISO = '2020-01-01T00:05:00Z'


# --- playbackmeta: the hero's facts ---------------------------------------------

def test_panel_prints_whatever_the_meta_actually_carries():
    text = hero_panel_text({
        'imdbRating': '8.4',
        'rottenTomatoes': '91%',
        'metascore': 80,
        'releaseInfo': '2019',
        'runtime': '132 min',
        'genres': ['Action', 'Sci-Fi', 'Adventure', 'Drama'],
    }, now_word='now')
    assert text == '\n'.join([
        'Rotten Tomatoes 91%',
        'Metascore 80',
        'IMDb 8.4',
        '2019',
        '132 min',
        'Action, Sci-Fi, Adventure',
    ])


def test_panel_reads_a_ratings_list_and_skips_what_is_missing():
    text = hero_panel_text({
        'ratings': [
            {'source': 'Internet Movie Database', 'value': '7.2'},
            {'source': 'Rotten Tomatoes', 'value': '64%'},
            {'source': 'Trakt', 'value': '70'},
        ],
        'releaseInfo': '2022–',
    }, now_word='now')
    assert text == 'Rotten Tomatoes 64%\nIMDb 7.2\n2022–now'


def test_panel_hides_an_empty_rating_instead_of_a_bare_label():
    assert hero_panel_text({'imdbRating': 'n/a', 'year': '1999'}) == '1999'
    assert hero_panel_text({}) == ''
    assert hero_panel_text(None) == ''


def test_passed_in_runtime_wins_over_the_raw_field():
    assert hero_panel_text({'runtime': 98}, runtime_text='98 min') == '98 min'


def test_hero_parts_round_trip_the_panel():
    stars, facts, scores = new_look_hero_parts(hero_panel_text({
        'imdbRating': '7.4', 'rottenTomatoes': '88%', 'releaseInfo': '2025', 'runtime': '112 min',
    }))
    assert stars == 7
    assert facts == ['2025', '112 min']
    assert scores == [('IMDb', '7.4'), ('RT', '88%')]


@pytest.mark.parametrize('seconds, text', [(724, '12:04'), (5, '0:05'), (3725, '1:02:05'), (-3, '0:00')])
def test_resume_clock(seconds, text):
    assert format_resume_clock(seconds) == text


@pytest.mark.parametrize('video_id, code', [
    ('tt1:1:3', 'S1E3'), ('tt1:12:10', 'S12E10'), ('tt1', ''), ('tt1:x:3', ''), (None, ''),
])
def test_episode_label_from_id(video_id, code):
    assert episode_label_from_id(video_id) == code


# --- newepisodes: the Continue card ------------------------------------------------

def test_episode_still_url_is_the_metahub_still_for_cinemeta_ids_only():
    assert episode_still_url('tt0944947', 1, 2) == 'https://episodes.metahub.space/tt0944947/1/2/w780.jpg'
    assert episode_still_url('kitsu:1', 1, 2) == ''
    assert episode_still_url('tt0944947', 0, 2) == ''


def test_episode_label_is_unpadded_and_keeps_the_title():
    assert _episode_label(1, 3, 'Lord Snow') == 'S1E3 · Lord Snow'
    assert _episode_label(1, 3, '') == 'S1E3'
    assert _episode_label(None, None, 'Special') == 'Special'


# --- library: Add to Library from a poster -------------------------------------------

def _meta(**overrides):
    meta = {'id': 'tt1', 'name': 'A Movie', 'type': 'movie'}
    meta.update(overrides)
    return meta


def test_library_add_from_existing_keeps_state_and_stamps_added_time():
    """A pulled playback row is not replaced. removed is cleared, the
    resume point stays, and a tombstone's _ctime becomes the new add."""
    existing = library.build_library_item(_meta(), now=NOW)
    existing = library.merge_playback(existing, 5000, 90000, video_id='tt1', now=NOW)
    existing['removed'] = True
    existing['temp'] = True
    added = library.library_add_from_existing(_meta(), existing, now=LATER)
    assert added['removed'] is False
    assert added['temp'] is False
    assert added['_mtime'] == LATER_ISO
    assert added['_ctime'] == LATER_ISO
    assert added['state']['timeOffset'] == 5000
    assert existing['removed'] is True


def test_library_add_from_existing_without_a_row_is_a_fresh_add():
    added = library.library_add_from_existing(_meta(), None, now=NOW)
    assert added == library.library_add_payload(_meta(), now=NOW)
    assert added['_ctime'] == NOW_ISO


# --- libraryaction: a library write shows before the account echoes it ------------

@pytest.fixture
def ledger():
    from lib.ui import libraryaction

    return libraryaction


def test_no_pending_write_leaves_the_read_alone(ledger):
    rows = [{'_id': 'tt1', 'name': 'One'}]
    assert ledger.apply_library_writes(rows) == rows
    assert ledger.apply_library_writes(None) == []


def test_a_noted_add_and_remove_overlay_the_account_read(ledger):
    added = {'_id': 'tt9', 'type': 'series', 'name': 'Added', 'removed': False,
             '_ctime': '2026-10-15T00:00:00Z', '_mtime': '2026-10-15T00:00:00Z'}
    account = [{'_id': 'tt1', 'type': 'movie', 'name': 'Older', '_ctime': '2020-01-01T00:00:00Z'}]
    ledger.note_library_write(added)
    assert [row['_id'] for row in ledger.apply_library_writes(account)] == ['tt1', 'tt9']

    removed = dict(added, removed=True, _mtime='2026-10-15T00:01:00Z')
    ledger.note_library_write(removed)
    rows = ledger.apply_library_writes(account + [dict(added)])
    assert [row['_id'] for row in rows if not row.get('removed')] == ['tt1']


def test_the_account_catching_up_retires_the_write(ledger):
    added = {'_id': 'tt9', 'removed': False, '_mtime': '2026-10-15T00:00:00Z'}
    ledger.note_library_write(added)
    echoed = [{'_id': 'tt9', 'removed': False, '_mtime': '2026-10-15T00:00:05Z'}]
    assert ledger.apply_library_writes(echoed) == echoed
    assert ledger.apply_library_writes([]) == []



def test_home_controls_are_available_without_a_visibility_dependency():
    import os
    import xml.etree.ElementTree as ET

    root = ET.parse(os.path.join(os.path.dirname(os.path.dirname(__file__)),
                                 'resources', 'settings.xml')).getroot()
    for key in ('refresh_cache', 'ratings_omdb_key', 'ratings_mdblist_key'):
        assert root.find(".//setting[@id='%s']/dependencies" % key) is None
    assert root.find(".//setting[@id='cache_generation']/visible").text == 'false'
