"""Regression tests for `lib.stremio.streaminfo` against REAL addon payloads.

Every other streaminfo test uses hand-written strings that encode what we
believe an addon emits. This file uses what one actually sent: the fixture
in `tests/fixtures/torrentio_streams.json` is a live capture from Torrentio
(https://torrentio.strem.fun), the most widely used Stremio stream addon,
for a movie (tt0111161) and a series episode (tt0903747:1:1).

It exists because a hand-written corpus agreed with the parser while the
parser disagreed with reality: `_parse_seeders` recovered a seeder count for
only 5% of live Torrentio streams, and got 4 of those 5 WRONG, because
Torrentio puts the count BEFORE the size behind a U+1F464 marker
("\U0001F464 109 \U0001F4BE 54.33 GB \u2699\uFE0F TorrentGalaxy") while the
fallback heuristic only knew how to read a number that trails a size/speed
group. No synthetic test caught it. These do.

The fixture is verbatim apart from `infoHash` - see its own `_note`.
"""
import json
import os
import re

import pytest

from lib.stremio import streaminfo

FIXTURE = os.path.join(os.path.dirname(__file__), 'fixtures', 'torrentio_streams.json')

#: Torrentio's own seeder marker, read straight from the raw payload so the
#: expected value comes from the addon's text rather than from the parser
#: being tested against itself.
SEEDER_MARKER_RE = re.compile('\U0001F464\uFE0F?\\s*(\\d+)')

#: Torrentio's tracker/indexer marker (gear), same reasoning.
TRACKER_MARKER_RE = re.compile('\u2699\uFE0F?\\s*([^\n]+)')


def _load():
    with open(FIXTURE, encoding='utf-8') as handle:
        return json.load(handle)['streams']


REAL_STREAMS = _load()


def _raw_text(stream):
    return '\n'.join(p for p in (stream.get('name'), stream.get('title')) if p)


def test_fixture_is_a_real_and_diverse_capture():
    """Guard the corpus itself: if it ever shrinks or loses its variety the
    tests below keep passing while covering much less."""
    assert len(REAL_STREAMS) >= 15
    parsed = [streaminfo.parse_stream(s, 'Torrentio') for s in REAL_STREAMS]
    assert len({p['resolution'] for p in parsed}) >= 3
    assert len({p['source'] for p in parsed}) >= 3
    assert any(p['languages'] for p in parsed)
    assert any(p['hdr'] for p in parsed)
    assert {s['_query_type'] for s in REAL_STREAMS} == {'movie', 'series'}


@pytest.mark.parametrize('stream', REAL_STREAMS, ids=lambda s: s['title'][:40])
def test_seeders_match_the_addons_own_marker(stream):
    """The regression this file was written for: every live Torrentio stream
    carries a seeder count, and the parser must report exactly it - never a
    number scavenged from elsewhere in the release name."""
    expected = SEEDER_MARKER_RE.search(_raw_text(stream))
    assert expected, 'fixture stream lost its seeder marker'

    info = streaminfo.parse_stream(stream, 'Torrentio')

    assert info['seeders'] == int(expected.group(1))


@pytest.mark.parametrize('stream', REAL_STREAMS, ids=lambda s: s['title'][:40])
def test_every_real_stream_yields_a_usable_row(stream):
    """Nothing in a real payload may crash the parser or produce an empty
    row: a stream the user cannot see is as bad as one that raises."""
    info = streaminfo.parse_stream(stream, 'Torrentio')

    assert info['title']
    assert info['size_text'], 'Torrentio always states a size'
    assert info['tracker'], 'Torrentio always names its indexer'
    assert info['is_torrent'] is True
    assert info['binge_group']
    assert streaminfo.format_label(info).strip()
    assert streaminfo.format_plot(info).strip()


@pytest.mark.parametrize('stream', REAL_STREAMS, ids=lambda s: s['title'][:40])
def test_tracker_matches_the_addons_own_marker(stream):
    """`tracker` must be the indexer behind the gear marker, not a fragment
    of the release name that happened to sit nearby."""
    expected = TRACKER_MARKER_RE.search(_raw_text(stream))
    assert expected, 'fixture stream lost its tracker marker'

    info = streaminfo.parse_stream(stream, 'Torrentio')

    assert info['tracker'] == expected.group(1).strip()[:24]


def test_no_real_stream_reports_an_implausible_swarm():
    """A parse that silently turns a resolution or a year into a seeder
    count shows up here as an absurd number rather than as a wrong-but-
    believable one."""
    for stream in REAL_STREAMS:
        seeders = streaminfo.parse_stream(stream, 'Torrentio')['seeders']
        assert seeders is None or 0 <= seeders <= 100000


def test_sort_by_quality_orders_the_real_corpus_best_first():
    """End-to-end on real data: the streams view sorts with `sort_streams`,
    so 2160p rows must land above 720p ones rather than in payload order."""
    pairs = [(streaminfo.parse_stream(s, 'Torrentio'), s) for s in REAL_STREAMS]

    ordered = streaminfo.sort_streams(pairs, key='quality')
    tiers = [streaminfo._RESOLUTION_TIER.get(info['resolution'], 0) for info, _ in ordered]

    assert tiers == sorted(tiers, reverse=True)
