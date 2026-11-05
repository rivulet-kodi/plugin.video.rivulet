"""Property tests for ``lib.livetv``: a channel's schedule is a third-party
add-on's JSON (`videos` with `startTime`/`endTime`), read inside paint and
refresh loops, so nothing here may raise on hostile input, and the
``programs_at`` verdict must be stremio-core's (`MetaItem::programs_at`),
checked against a direct statement of the rule.
"""
import datetime

from hypothesis import example, given
from hypothesis import strategies as st

from lib import livetv
from tests.fuzz_strategies import hostile_json, json_values, manifests

BASE = 1_800_000_000.0


def _iso(epoch):
    stamp = datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc)
    return stamp.strftime('%Y-%m-%dT%H:%M:%SZ')


_offsets = st.integers(min_value=-7200, max_value=7200)
_lengths = st.integers(min_value=-600, max_value=7200)


@st.composite
def programmes(draw):
    start = BASE + draw(_offsets)
    return {'id': draw(st.text(max_size=4)), 'title': draw(st.text(max_size=6)),
            'startTime': _iso(start), 'endTime': _iso(start + draw(_lengths))}


_schedules = st.lists(programmes(), max_size=8)
_nows = st.integers(min_value=-3600, max_value=3600).map(lambda delta: BASE + delta)


def _expected(videos, now):
    """`programs_at` restated: every programme as a plain tuple first."""
    spans = [(v, livetv.parse_time(v['startTime']), livetv.parse_time(v['endTime'])) for v in videos]
    live = [s for s in spans if s[1] <= now < s[2]]
    current = None
    for entry in live:
        if current is None or entry[1] >= current[1]:
            current = entry
    later = [s for s in spans if s[1] < s[2] and s[1] > now and (current is None or s[1] >= current[2])]
    upcoming = None
    for entry in later:
        if upcoming is None or entry[1] < upcoming[1]:
            upcoming = entry
    return (current[0] if current else None, upcoming[0] if upcoming else None)


@given(_schedules, _nows)
def test_programs_at_is_the_core_rule(videos, now):
    current, upcoming = livetv.programs_at(videos, now)
    expected_current, expected_next = _expected(videos, now)
    assert current is expected_current
    assert upcoming is expected_next
    if current is not None:
        assert livetv.parse_time(current['startTime']) <= now < livetv.parse_time(current['endTime'])
    if upcoming is not None:
        start = livetv.parse_time(upcoming['startTime'])
        assert start > now
        if current is not None:
            assert start >= livetv.parse_time(current['endTime'])


@example([{'startTime': '2026-01-01T00:00:00Z', 'endTime': '2026-01-01T01:00:00Z'}], float('nan'))
@given(hostile_json(st.lists(json_values, max_size=5)), st.floats(allow_nan=True, allow_infinity=True))
def test_hostile_schedules_never_raise(videos, now):
    livetv.programs_at(videos, now)
    livetv.coverage_end(videos)
    livetv.refresh_interval(videos, now if now == now else 0.0)
    result = livetv.now_next(videos, now if now == now else 0.0)
    assert result is None or set(result) == {'current', 'next'}


@given(hostile_json(json_values))
def test_hostile_values_never_raise(value):
    livetv.parse_time(value)
    livetv.is_live(value)
    livetv.is_epg_provider(value)
    livetv.guide_videos(value, 'x')


@given(_schedules, _nows, st.booleans())
def test_refresh_interval_is_one_of_the_two_core_periods(videos, now, failed):
    interval = livetv.refresh_interval(videos, now, failed)
    end = livetv.coverage_end(videos)
    if failed or end is None or end <= now:
        assert interval == livetv.RETRY_SECONDS
    else:
        assert interval == livetv.REFRESH_SECONDS


@given(st.integers(min_value=0, max_value=4_000_000_000),
       st.integers(min_value=-14 * 3600, max_value=14 * 3600))
def test_guide_dates_cover_exactly_the_local_day(now, offset):
    dates = livetv.guide_dates(now, offset)
    assert 1 <= len(dates) <= 2
    day = datetime.date.fromisoformat(dates[0])
    local_start = datetime.datetime(day.year, day.month, day.day, tzinfo=datetime.timezone.utc)
    local_midnight = (datetime.datetime.fromtimestamp(now + offset, datetime.timezone.utc)
                      .replace(hour=0, minute=0, second=0, microsecond=0))
    utc_start = local_midnight - datetime.timedelta(seconds=offset)
    assert dates[0] == utc_start.date().isoformat()
    assert dates[-1] == (utc_start + datetime.timedelta(days=1, seconds=-1)).date().isoformat()
    # The instant itself lies inside the dates asked for.
    assert datetime.datetime.fromtimestamp(now, datetime.timezone.utc).date().isoformat() in dates
    assert local_start.date() == day


@given(hostile_json(manifests))
def test_guide_catalog_needs_date_extra_and_epg_provider(manifest):
    catalogs = manifest.get('catalogs') if isinstance(manifest, dict) else None
    for catalog in catalogs if isinstance(catalogs, list) else ():
        if livetv.is_guide_catalog(manifest, catalog):
            assert livetv.is_epg_provider(manifest)
            assert livetv.DATE_EXTRA in livetv._extra_names(catalog)


def test_guide_catalog_rule_examples():
    guide = {'type': 'tv', 'id': 'g', 'extra': [{'name': 'date'}, {'name': 'skip'}]}
    plain = {'type': 'tv', 'id': 'c', 'extra': [{'name': 'skip'}]}
    provider = {'behaviorHints': {'epgProvider': True}}
    assert livetv.is_guide_catalog(provider, guide)
    assert not livetv.is_guide_catalog(provider, plain)
    # A `date` filter on an add-on that is not an EPG provider stays browsable.
    assert not livetv.is_guide_catalog({}, guide)
    assert not livetv.is_guide_catalog({'behaviorHints': {'epgProvider': 'yes'}}, guide)


def test_is_live_follows_core():
    assert livetv.is_live({'type': 'tv'})
    assert livetv.is_live({'type': 'movie', 'behaviorHints': {'isLive': True}})
    assert not livetv.is_live({'type': 'movie', 'behaviorHints': {'isLive': 'true'}})
    assert not livetv.is_live({'type': 'series'})


def test_guide_dates_two_dates_when_zone_not_aligned():
    # 2026-11-05 23:30 UTC at +01:00 is already 11-06 locally: asks 11-05 and 11-06.
    now = datetime.datetime(2026, 11, 5, 23, 30, tzinfo=datetime.timezone.utc).timestamp()
    assert livetv.guide_dates(now, 3600) == ['2026-11-05', '2026-11-06']
    assert livetv.guide_dates(now, 0) == ['2026-11-05']


def test_guide_videos_merges_dates_without_duplicates():
    one = {'id': 'c', 'videos': [{'id': 'c:2', 'startTime': '2026-11-05T10:00:00Z'},
                                 {'id': 'c:1', 'startTime': '2026-11-05T09:00:00Z'}]}
    two = {'id': 'c', 'videos': [{'id': 'c:2', 'startTime': '2026-11-05T10:00:00Z'},
                                 {'id': 'c:3', 'startTime': '2026-11-06T00:00:00Z'}]}
    other = {'id': 'd', 'videos': [{'id': 'd:1', 'startTime': '2026-11-05T09:30:00Z'}]}
    merged = livetv.guide_videos([one, other, two], 'c')
    assert [video['id'] for video in merged] == ['c:1', 'c:2', 'c:3']


def test_now_next_progress_and_overview():
    videos = [{'id': 'a', 'title': 'News', 'overview': 'Headlines',
               'startTime': '2026-11-05T10:00:00Z', 'endTime': '2026-11-05T11:00:00Z'},
              {'id': 'b', 'title': 'Film',
               'startTime': '2026-11-05T11:00:00Z', 'endTime': '2026-11-05T12:00:00Z'}]
    now = livetv.parse_time('2026-11-05T10:15:00Z')
    result = livetv.now_next(videos, now)
    assert result['current']['title'] == 'News'
    assert result['current']['overview'] == 'Headlines'
    assert result['current']['progress'] == 25
    assert result['next']['title'] == 'Film'
    assert livetv.now_next([], now) is None


def test_parse_time_offsets_and_fractions():
    assert livetv.parse_time('2026-11-05T10:00:00+01:00') == livetv.parse_time('2026-11-05T09:00:00Z')
    assert livetv.parse_time('2026-11-05T10:00:00.5Z') == livetv.parse_time('2026-11-05T10:00:00Z') + 0.5
    assert livetv.parse_time('2026-11-05T10:00:00.123456789Z') is not None
    assert livetv.parse_time('nope') is None
    assert livetv.parse_time(5) is None
