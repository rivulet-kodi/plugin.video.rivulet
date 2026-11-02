# SPDX-FileCopyrightText: 2026 M0Rf30
# SPDX-License-Identifier: MIT

"""The IN CINEMA tag (issue #84) and the watched set behind the movie and
series card's check (issue #85): the pure rules, and that `dvdRelease` survives
every place a meta is trimmed on its way to a card."""
import datetime

import pytest

from lib import library
from lib.stremio import pagecache, ratings
from lib.ui import previews, watchmarks
from lib.ui.playbackmeta import IN_CINEMA_WINDOW_DAYS, hero_panel_text, in_cinema

NOW = datetime.datetime(2026, 6, 1, 12, 0, 0, tzinfo=datetime.timezone.utc)


def _movie(released='2026-05-01T00:00:00.000Z', **extra):
    meta = {'id': 'tt1', 'type': 'movie', 'name': 'Dune', 'released': released}
    meta.update(extra)
    return meta


def _days_ago(days):
    return (NOW.date() - datetime.timedelta(days=days)).isoformat() + 'T00:00:00.000Z'


# --- in_cinema ----------------------------------------------------------------


def test_a_recent_movie_without_a_home_release_is_in_cinema():
    assert in_cinema(_movie(), NOW) is True


def test_released_today_counts():
    assert in_cinema(_movie(_days_ago(0)), NOW) is True


def test_the_window_edge_is_inclusive():
    assert in_cinema(_movie(_days_ago(IN_CINEMA_WINDOW_DAYS)), NOW) is True
    assert in_cinema(_movie(_days_ago(IN_CINEMA_WINDOW_DAYS + 1)), NOW) is False


def test_a_future_release_is_not_in_cinema_yet():
    assert in_cinema(_movie(_days_ago(-1)), NOW) is False


@pytest.mark.parametrize('dvd', ['2026-05-20T00:00:00.000Z', 'x', 1])
def test_a_home_release_ends_it(dvd):
    assert in_cinema(_movie(dvdRelease=dvd), NOW) is False


@pytest.mark.parametrize('dvd', [None, '', '  ', 0, False])
def test_an_empty_home_release_does_not(dvd):
    assert in_cinema(_movie(dvdRelease=dvd), NOW) is True


@pytest.mark.parametrize('kind', ['series', 'tv', '', None, 5])
def test_only_movies_are_tagged(kind):
    meta = _movie()
    meta['type'] = kind
    assert in_cinema(meta, NOW) is False


@pytest.mark.parametrize('released', [None, '', '2026', '2026-05', 'garbage', 20260501, ['2026-05-01'], '2026-13-45'])
def test_a_missing_or_malformed_release_is_never_tagged(released):
    assert in_cinema(_movie(released), NOW) is False


@pytest.mark.parametrize('meta', [None, 'x', [], 5])
def test_a_non_dict_is_never_tagged(meta):
    assert in_cinema(meta, NOW) is False


def test_a_plain_date_released_is_accepted():
    assert in_cinema(_movie('2026-05-01'), NOW) is True


def test_now_may_be_a_date_a_naive_datetime_or_default():
    assert in_cinema(_movie(), datetime.date(2026, 6, 1)) is True
    assert in_cinema(_movie(), datetime.datetime(2026, 6, 1)) is True
    assert in_cinema(_movie(_days_ago(1000))) is False


def test_an_aware_now_is_read_in_utc():
    """23:30 on the 31st at UTC-5 is already June 1st UTC."""
    zone = datetime.timezone(datetime.timedelta(hours=-5))
    now = datetime.datetime(2026, 5, 31, 23, 30, tzinfo=zone)
    assert in_cinema(_movie('2026-06-01T00:00:00.000Z'), now) is True


# --- the hero line --------------------------------------------------------------


def _recent():
    """`hero_panel_text` reads the real clock, so the date is relative to it."""
    today = datetime.datetime.now(datetime.timezone.utc).date()
    return (today - datetime.timedelta(days=5)).isoformat() + 'T00:00:00.000Z'


def test_the_hero_year_line_carries_the_tag():
    meta = _movie(_recent(), releaseInfo='2026')
    assert hero_panel_text(meta, now_word='now', cinema_word='IN CINEMA').splitlines()[0] == '2026 \u00b7 IN CINEMA'


def test_no_tag_for_a_title_that_is_not_in_cinema():
    meta = _movie(_recent(), releaseInfo='2026', dvdRelease='2026-05-20T00:00:00.000Z')
    assert hero_panel_text(meta, cinema_word='IN CINEMA') == '2026'
    # No word passed (a caller that does not tag): nothing added.
    assert hero_panel_text(_movie(_recent(), releaseInfo='2026')) == '2026'


# --- dvdRelease survives the trims -----------------------------------------------


def test_the_page_cache_keeps_dvd_release():
    slim = pagecache.slim_meta(_movie(dvdRelease='2026-05-20T00:00:00.000Z', cast=['x']))
    assert slim['dvdRelease'] == '2026-05-20T00:00:00.000Z'
    assert slim['released'] == '2026-05-01T00:00:00.000Z'


def test_the_preview_cache_keeps_dvd_release():
    previews.remember(_movie(dvdRelease='2026-05-20T00:00:00.000Z'))
    assert previews.lookup('tt1')['dvdRelease'] == '2026-05-20T00:00:00.000Z'


def test_hero_facts_keep_what_the_tag_reads():
    facts = ratings.panel_facts(_movie(dvdRelease='2026-05-20T00:00:00.000Z'))
    assert facts['dvdRelease'] == '2026-05-20T00:00:00.000Z'
    assert facts['released'] and facts['type'] == 'movie'


# --- watchmarks ----------------------------------------------------------------


class _Store:
    def __init__(self, auth=None, progress=None, fail=False, cache=None):
        self._auth = auth
        self._progress = progress or []
        self._fail = fail
        self._cache = cache or []

    def get_library_cache(self):
        if self._fail:
            raise OSError('disk')
        return self._cache

    def get_auth(self):
        if self._fail:
            raise OSError('disk')
        return self._auth

    def get_progress_entries(self):
        if self._fail:
            raise OSError('disk')
        return self._progress


def _record(item_id='tt1', mtime='2026-05-01T00:00:00Z', **state):
    return {'_id': item_id, 'type': 'movie', '_mtime': mtime, 'removed': False, 'state': state}


def _progress(item_id='tt1', position=100000, updated_at='2026-05-02T00:00:00Z'):
    return {'id': item_id, 'type': 'movie', 'video_id': None, 'position_ms': position,
            'duration_ms': 100000, 'updated_at': updated_at}


def test_nothing_is_watched_before_the_first_refresh():
    assert watchmarks.is_watched({'id': 'tt1', 'type': 'movie'}) is False


def test_an_account_flag_marks_the_movie():
    watchmarks.refresh(_Store(auth={'authKey': 'k'}), [_record(timesWatched=1)])
    assert watchmarks.is_watched({'id': 'tt1', 'type': 'movie'}) is True
    assert watchmarks.is_watched({'id': 'tt2', 'type': 'movie'}) is False


def test_a_series_with_a_watched_episode_count_wears_the_check():
    """Stremio's rule is `timesWatched > 0` for every type."""
    series = dict(_record('tt9', timesWatched=1), type='series')
    watchmarks.refresh(_Store(auth={'authKey': 'k'}), [series])
    assert watchmarks.is_watched({'id': 'tt9', 'type': 'series'}) is True


def test_a_series_with_a_stale_flag_and_no_count_shows_no_check():
    series = dict(_record('tt9', flaggedWatched=1, timesWatched=0), type='series')
    watchmarks.refresh(_Store(auth={'authKey': 'k'}), [series])
    assert watchmarks.is_watched({'id': 'tt9', 'type': 'series'}) is False


def test_a_locally_finished_episode_marks_the_series_without_an_account_record():
    episode = {'id': 'tt9', 'type': 'series', 'video_id': 'tt9:1:2', 'position_ms': 90000,
               'duration_ms': 100000, 'updated_at': '2026-05-02T00:00:00Z'}
    watchmarks.refresh(_Store(progress=[episode]))
    assert watchmarks.is_watched({'id': 'tt9', 'type': 'series'}) is True
    assert watchmarks.is_watched({'id': 'tt8', 'type': 'series'}) is False


def test_cards_without_an_id_never_wear_the_check():
    watchmarks.refresh(_Store(auth={'authKey': 'k'}), [_record(timesWatched=1)])
    assert watchmarks.is_watched({'type': 'movie'}) is False
    assert watchmarks.is_watched({}) is False
    assert watchmarks.is_watched(None) is False


def test_a_local_finish_marks_it_signed_out():
    watchmarks.refresh(_Store(progress=[_progress(position=80000)]))
    assert watchmarks.is_watched({'id': 'tt1', 'type': 'movie'}) is True


def test_a_refresh_without_entries_reuses_the_last_account_read():
    store = _Store(auth={'authKey': 'k'})
    watchmarks.refresh(store, [_record(timesWatched=1)])
    watchmarks.refresh(store)
    assert watchmarks.is_watched({'id': 'tt1', 'type': 'movie'}) is True


def test_signing_out_drops_the_previous_accounts_answer():
    watchmarks.refresh(_Store(auth={'authKey': 'k'}), [_record(timesWatched=1)])
    watchmarks.refresh(_Store(auth=None))
    assert watchmarks.is_watched({'id': 'tt1', 'type': 'movie'}) is False


def test_an_unmark_clears_the_check_despite_the_stale_local_finish():
    store = _Store(auth={'authKey': 'k'}, progress=[_progress(updated_at='2026-05-02T00:00:00Z')])
    watchmarks.refresh(store, [_record(mtime='2026-05-01T00:00:00Z', timesWatched=1)])
    assert watchmarks.is_watched({'id': 'tt1', 'type': 'movie'}) is True
    unmarked = library.mark_watched_payload(_record(timesWatched=1), False, now=datetime.datetime(2026, 6, 1, tzinfo=datetime.timezone.utc))
    watchmarks.refresh(store, [unmarked])
    assert watchmarks.is_watched({'id': 'tt1', 'type': 'movie'}) is False


def test_a_failing_store_never_raises_and_leaves_no_marks():
    watchmarks.refresh(_Store(fail=True), [_record(timesWatched=1)])
    assert watchmarks.is_watched({'id': 'tt1', 'type': 'movie'}) is False


def test_a_stale_flag_with_no_count_shows_no_check():
    watchmarks.refresh(_Store(auth={'authKey': 'k'}), [_record(flaggedWatched=1, timesWatched=0)])
    assert watchmarks.is_watched({'id': 'tt1', 'type': 'movie'}) is False


def test_the_service_cache_marks_cards_with_no_read_of_its_own():
    """The coverflow and a catalog-only Home read nothing; the service's
    last pull answers for the account."""
    watchmarks.refresh(_Store(auth={'authKey': 'k'}, cache=[_record(timesWatched=1)]))
    assert watchmarks.is_watched({'id': 'tt1', 'type': 'movie'}) is True


def test_no_cache_yet_waits_for_the_first_pull():
    watchmarks.refresh(_Store(auth={'authKey': 'k'}))
    assert watchmarks.is_watched({'id': 'tt1', 'type': 'movie'}) is False


def test_a_pending_write_overrides_the_cache_it_postdates():
    from lib.ui import libraryaction

    watched = _record(mtime='2026-05-01T00:00:00Z', timesWatched=1)
    unmarked = library.mark_watched_payload(watched, False, now=datetime.datetime(2026, 6, 1, tzinfo=datetime.timezone.utc))
    libraryaction.note_library_write(unmarked)
    watchmarks.refresh(_Store(auth={'authKey': 'k'}, cache=[watched]))
    assert watchmarks.is_watched({'id': 'tt1', 'type': 'movie'}) is False


@pytest.mark.parametrize('cache_mtime, live_mtime, watched', [
    ('2026-06-01T00:00:00Z', '2026-05-01T00:00:00Z', True),
    ('2026-05-01T00:00:00Z', '2026-06-01T00:00:00Z', False),
])
def test_the_newer_of_the_cache_and_a_live_read_wins(cache_mtime, live_mtime, watched):
    store = _Store(auth={'authKey': 'k'}, cache=[_record(mtime=cache_mtime, timesWatched=1)])
    watchmarks.refresh(store, [_record(mtime=live_mtime, timesWatched=0)])
    assert watchmarks.is_watched({'id': 'tt1', 'type': 'movie'}) is watched


def test_signed_out_ignores_a_cache_left_on_disk():
    watchmarks.refresh(_Store(auth=None, cache=[_record(timesWatched=1)]))
    assert watchmarks.is_watched({'id': 'tt1', 'type': 'movie'}) is False
