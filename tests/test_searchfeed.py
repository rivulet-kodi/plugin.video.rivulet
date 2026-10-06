# SPDX-FileCopyrightText: 2026 M0Rf30
# SPDX-License-Identifier: MIT

"""Tests for lib.ui.searchfeed: Cinemeta's `feed.json` used as a ranking
oracle for `lib.ui.searchwindow._rank_by_title()`.

The module imports `xbmc` and `lib.ui.compat.log` at module scope, so it is
loaded fresh against the shared fake xbmc stubs (tests/kodistubs) the same
way the window tests load theirs; no real Kodi, no network (the session is a
`tests.conftest.FakeSession`).
"""
import contextlib
import json
import os
import time

import pytest
import requests

from tests.conftest import FakeResponse, FakeSession
from tests.kodistubs import install_kodi_stubs


@pytest.fixture
def load_searchfeed():
    with contextlib.ExitStack() as stack:
        def _load(**kwargs):
            return stack.enter_context(install_kodi_stubs(reload=('lib.ui.compat', 'lib.ui.searchfeed'), **kwargs))

        yield _load


def _write_cache(data_dir, records, ts=None):
    path = os.path.join(str(data_dir), 'search-feed.json')
    with open(path, 'w') as handle:
        json.dump({'ts': time.time() if ts is None else ts, 'records': records}, handle)
    return path


RECORDS = [
    {'id': 'tt1', 'type': 'movie', 'name': 'One', 'imdbRating': '8.0', 'popularity': 100},
    {'id': 'tt2', 'type': 'movie', 'name': 'Two', 'imdbRating': 4.0, 'popularity': 50},
    {'id': 'tt3', 'type': 'series', 'name': 'Three'},
]


# --- _read_cached -----------------------------------------------------------


def test_read_cached_returns_the_records_while_fresh(load_searchfeed, tmp_path):
    ctx = load_searchfeed()
    _write_cache(tmp_path, RECORDS)

    assert ctx.searchfeed._read_cached(str(tmp_path)) == RECORDS


def test_read_cached_is_none_once_the_ttl_has_passed(load_searchfeed, tmp_path):
    ctx = load_searchfeed()
    _write_cache(tmp_path, RECORDS, ts=time.time() - ctx.searchfeed.TTL_SECONDS - 5)

    assert ctx.searchfeed._read_cached(str(tmp_path)) is None


def test_read_cached_is_none_for_missing_malformed_or_oddly_shaped_files(load_searchfeed, tmp_path):
    ctx = load_searchfeed()
    path = os.path.join(str(tmp_path), 'search-feed.json')
    assert ctx.searchfeed._read_cached(str(tmp_path)) is None  # no file

    for content in ('{not json', '[1, 2, 3]', json.dumps({'ts': time.time(), 'records': {'a': 1}}),
                    json.dumps({'records': RECORDS})):  # the last has no ts -> epoch -> expired
        with open(path, 'w') as handle:
            handle.write(content)
        assert ctx.searchfeed._read_cached(str(tmp_path)) is None


# --- _fetch -------------------------------------------------------------------


def test_fetch_streams_the_feed_and_returns_its_records(load_searchfeed):
    ctx = load_searchfeed()
    response = FakeResponse(RECORDS)
    session = FakeSession(responses=[response])

    assert ctx.searchfeed._fetch(session, 30) == RECORDS
    assert session.calls[0]['url'] == ctx.searchfeed.FEED_URL
    assert session.calls[0]['kwargs'] == {'timeout': 30, 'stream': True}
    assert response.closed is True


def test_fetch_http_error_is_none_and_never_logs_the_message(load_searchfeed):
    ctx = load_searchfeed()
    import xbmc

    response = FakeResponse(status_code=503)
    session = FakeSession(responses=[response])

    assert ctx.searchfeed._fetch(session, 30) is None
    warnings = [msg for msg, level in ctx.env.log_calls if level == xbmc.LOGWARNING]
    assert any('fetch failed: HTTPError' in m for m in warnings)
    assert not any('503' in m for m in warnings)
    assert response.closed is True


def test_fetch_connection_error_is_none(load_searchfeed):
    ctx = load_searchfeed()
    session = FakeSession(exc=requests.exceptions.ConnectionError('refused'))

    assert ctx.searchfeed._fetch(session, 30) is None


class _DyingBody(FakeResponse):
    """Headers arrive, then the body dies mid-download."""

    def iter_content(self, chunk_size=None):
        yield b'[{"id": '
        raise requests.exceptions.ChunkedEncodingError('connection broken')


def test_fetch_body_that_dies_mid_download_is_none_and_closed(load_searchfeed):
    ctx = load_searchfeed()
    response = _DyingBody()

    assert ctx.searchfeed._fetch(FakeSession(responses=[response]), 30) is None
    assert response.closed is True


def test_fetch_stops_downloading_at_the_size_cap(load_searchfeed, monkeypatch):
    """The cap is enforced WHILE the body streams: the generator below would
    happily yield forever, so a check that only ran after buffering it all
    would never return."""
    ctx = load_searchfeed()
    import xbmc

    monkeypatch.setattr(ctx.searchfeed, 'MAX_FEED_BYTES', 100)
    pulled = []

    class _Endless(FakeResponse):
        def iter_content(self, chunk_size=None):
            while True:
                pulled.append(1)
                yield b'x' * 60
                assert len(pulled) < 50, 'kept downloading past the cap'

    response = _Endless()

    assert ctx.searchfeed._fetch(FakeSession(responses=[response]), 30) is None
    assert len(pulled) == 2  # 60 bytes fit, 120 do not
    assert response.closed is True
    assert any('too large' in msg for msg, level in ctx.env.log_calls if level == xbmc.LOGWARNING)


@pytest.mark.parametrize('body,fragment', [
    ({'not': 'a list'}, 'was not a list'),
    ('{truncated', 'invalid JSON'),
], ids=['not-a-list', 'invalid-json'])
def test_fetch_rejects_a_body_that_is_not_a_json_list(load_searchfeed, body, fragment):
    ctx = load_searchfeed()
    if isinstance(body, str):
        class _Raw(FakeResponse):
            def iter_content(self, chunk_size=None):
                yield body.encode('utf-8')

        response = _Raw()
    else:
        response = FakeResponse(body)

    assert ctx.searchfeed._fetch(FakeSession(responses=[response]), 30) is None
    assert any(fragment in msg for msg, _level in ctx.env.log_calls)


# --- load_records -------------------------------------------------------------


def test_load_records_serves_a_fresh_cache_without_touching_the_network(load_searchfeed, tmp_path):
    ctx = load_searchfeed()
    _write_cache(tmp_path, RECORDS)
    session = FakeSession()  # any request raises: nothing is queued

    assert ctx.searchfeed.load_records(str(tmp_path), session) == RECORDS
    assert session.calls == []


def test_load_records_fetches_and_caches_on_a_miss(load_searchfeed, tmp_path):
    ctx = load_searchfeed()
    session = FakeSession(responses=[FakeResponse(RECORDS)])

    assert ctx.searchfeed.load_records(str(tmp_path), session) == RECORDS
    assert ctx.searchfeed._read_cached(str(tmp_path)) == RECORDS  # next call is a cache hit


def test_load_records_is_empty_and_writes_nothing_when_the_feed_is_unavailable(load_searchfeed, tmp_path):
    ctx = load_searchfeed()
    session = FakeSession(exc=requests.exceptions.ConnectionError('offline'))

    assert ctx.searchfeed.load_records(str(tmp_path), session) == []
    assert os.listdir(str(tmp_path)) == []


# --- build_index / boost ------------------------------------------------------


def test_build_index_keys_by_type_and_id_and_tracks_the_maxima(load_searchfeed):
    ctx = load_searchfeed()

    index, max_rating, max_popularity = ctx.searchfeed.build_index(RECORDS + ['junk', {'type': 'movie'}])

    assert set(index) == {('movie', 'tt1'), ('movie', 'tt2'), ('series', 'tt3')}
    assert max_rating == 8.0
    assert max_popularity == 100.0


def test_build_index_of_an_empty_or_fieldless_feed_never_divides_by_zero(load_searchfeed):
    ctx = load_searchfeed()

    for records in ([], [{'id': 'tt3', 'type': 'series'}]):
        index, max_rating, max_popularity = ctx.searchfeed.build_index(records)
        assert max_rating > 0 and max_popularity > 0
        assert ctx.searchfeed.boost({'id': 'tt3', 'type': 'series'}, index, max_rating, max_popularity) == 1.0


def test_boost_rewards_rating_and_popularity_and_is_neutral_for_unknown_titles(load_searchfeed):
    ctx = load_searchfeed()
    index, max_rating, max_popularity = ctx.searchfeed.build_index(RECORDS)

    def boost(meta):
        return ctx.searchfeed.boost(meta, index, max_rating, max_popularity)

    assert boost({'id': 'tt1', 'type': 'movie'}) > boost({'id': 'tt2', 'type': 'movie'}) > 1.0
    assert boost({'id': 'tt404', 'type': 'movie'}) == 1.0  # not in the feed
    assert boost({'id': 'tt1', 'type': 'series'}) == 1.0   # same id, different type


def test_number_coerces_loosely_typed_feed_fields(load_searchfeed):
    ctx = load_searchfeed()

    assert ctx.searchfeed._number('7.5') == 7.5
    assert ctx.searchfeed._number(3) == 3.0
    assert ctx.searchfeed._number(None) == 0.0
    assert ctx.searchfeed._number('n/a') == 0.0
