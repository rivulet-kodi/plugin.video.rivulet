"""Protocol tests for lib.stremio.server (streaming-server URL resolution).

Reference: stremio-core src/types/resource/stream.rs (Stream::convert) and
src/constants.rs (STREAMING_SERVER_URL). No network access - `fake_requests`
patches the real `requests.get`/`requests.post`.
"""
from urllib.parse import urlencode

import pytest
import requests

from lib.stremio.server import ServerClient

BASE = "http://127.0.0.1:11470"


def make_client():
    return ServerClient(BASE)


# --- torrent_url -------------------------------------------------------


def test_torrent_url_basic_no_trackers():
    client = make_client()
    url = client.torrent_url("aabbccddeeff00112233445566778899aabbccdd", 0)
    assert url == BASE + "/aabbccddeeff00112233445566778899aabbccdd/0"


def test_torrent_url_lowercases_info_hash():
    client = make_client()
    url = client.torrent_url("AABBCCDDEEFF00112233445566778899AABBCCDD", 1)
    assert url.startswith(BASE + "/aabbccddeeff00112233445566778899aabbccdd/1")


def test_torrent_url_multiple_trackers_urlencoded():
    client = make_client()
    trackers = ["udp://tracker.opentrackr.org:1337/announce", "udp://tracker.leechers-paradise.org:6969/announce"]
    url = client.torrent_url("aabbccddeeff00112233445566778899aabbccdd", 3, announce=trackers)
    base_path = BASE + "/aabbccddeeff00112233445566778899aabbccdd/3"
    assert url.startswith(base_path + "?")
    query = url[len(base_path) + 1:]
    expected = urlencode([("tr", t) for t in trackers])
    assert query == expected
    # sanity: repeated tr= params, form-urlencoded (colons/slashes escaped)
    assert query.count("tr=") == 2
    assert "%3A" in query or "%2F" in query


def test_torrent_url_no_trackers_omits_query_string():
    client = make_client()
    url = client.torrent_url("aa" * 20, 0, announce=[])
    assert "?" not in url


def test_torrent_url_file_idx_negative_one_for_unspecified():
    """stream.rs: file_idx.map_or_else(|| "-1", ...) -> server auto-picks largest file."""
    client = make_client()
    url = client.torrent_url("aa" * 20, -1)
    assert url == BASE + "/" + "aa" * 20 + "/-1"


# --- resolve_stream ------------------------------------------------------


def test_resolve_stream_https_url_passthrough():
    client = make_client()
    stream = {"url": "https://example.com/video.mp4"}
    assert client.resolve_stream(stream) == "https://example.com/video.mp4"


def test_resolve_stream_info_hash_default_file_idx_minus_one():
    client = make_client()
    stream = {"infoHash": "aa" * 20}
    resolved = client.resolve_stream(stream)
    assert resolved == client.torrent_url("aa" * 20, -1, [])


def test_resolve_stream_info_hash_with_file_idx_and_announce():
    client = make_client()
    stream = {"infoHash": "bb" * 20, "fileIdx": 2, "announce": ["udp://tracker1/announce"]}
    resolved = client.resolve_stream(stream)
    assert resolved == client.torrent_url("bb" * 20, 2, ["udp://tracker1/announce"])


def test_resolve_stream_forwards_sources_when_announce_absent():
    """stremio-core deserializes torrent trackers from `announce` with
    `#[serde(alias = "sources")]` (stream.rs:812) - Torrentio/AIOStreams-
    style addons ship trackers under `sources`, not `announce`. Live bug
    fix: resolve_stream must fall back to `sources` so the server actually
    receives tracker URLs (it strips "tracker:"/ignores "dht:" itself)."""
    client = make_client()
    stream = {
        "infoHash": "cc" * 20,
        "fileIdx": 26,
        "sources": ["tracker:udp://tracker1/announce", "dht:" + "cc" * 20],
    }
    resolved = client.resolve_stream(stream)
    assert resolved == client.torrent_url(
        "cc" * 20, 26, ["tracker:udp://tracker1/announce", "dht:" + "cc" * 20]
    )


def test_resolve_stream_prefers_announce_over_sources_when_both_present():
    client = make_client()
    stream = {
        "infoHash": "dd" * 20,
        "fileIdx": 1,
        "announce": ["udp://real-tracker/announce"],
        "sources": ["tracker:udp://ignored/announce"],
    }
    resolved = client.resolve_stream(stream)
    assert resolved == client.torrent_url("dd" * 20, 1, ["udp://real-tracker/announce"])


def test_resolve_stream_yt_id_builds_yt_endpoint():
    client = make_client()
    stream = {"ytId": "dQw4w9WgXcQ"}
    assert client.resolve_stream(stream) == BASE + "/yt/dQw4w9WgXcQ"


def test_resolve_stream_external_url_returns_none():
    client = make_client()
    stream = {"externalUrl": "https://example.com/watch"}
    assert client.resolve_stream(stream) is None


def test_resolve_stream_magnet_parses_btih_and_trackers():
    client = make_client()
    info_hash = "aabbccddeeff00112233445566778899aabbccdd"
    stream = {
        "url": "magnet:?xt=urn:btih:%s&dn=Some+Movie&tr=http://t1.example/announce&tr=http://t2.example/announce"
        % info_hash
    }
    resolved = client.resolve_stream(stream)
    expected = client.torrent_url(
        info_hash, -1, ["http://t1.example/announce", "http://t2.example/announce"]
    )
    assert resolved == expected


def test_resolve_stream_magnet_case_insensitive_hash():
    client = make_client()
    stream = {"url": "magnet:?xt=urn:btih:AABBCCDDEEFF00112233445566778899AABBCCDD"}
    resolved = client.resolve_stream(stream)
    assert resolved is not None
    assert "aabbccddeeff00112233445566778899aabbccdd" in resolved.lower()


def test_resolve_stream_magnet_without_btih_returns_none():
    client = make_client()
    stream = {"url": "magnet:?dn=NoHashHere"}
    assert client.resolve_stream(stream) is None


def test_resolve_stream_unknown_source_returns_none():
    client = make_client()
    assert client.resolve_stream({}) is None


# --- is_available ----------------------------------------------------------


def test_is_available_true_when_settings_ok(fake_requests):
    fake_requests.queue_get(_ok_response())
    client = make_client()
    assert client.is_available() is True
    assert fake_requests.calls[0]["url"] == BASE + "/settings"


def test_is_available_falls_back_to_stats_json(fake_requests):
    fake_requests.queue_get(_not_ok_response())
    fake_requests.queue_get(_ok_response())
    client = make_client()
    assert client.is_available() is True
    urls = [c["url"] for c in fake_requests.calls]
    assert urls == [BASE + "/settings", BASE + "/stats.json"]


def test_is_available_false_on_connection_error(fake_requests):
    fake_requests.queue_get(requests.exceptions.ConnectionError("refused"))
    fake_requests.queue_get(requests.exceptions.ConnectionError("refused"))
    client = make_client()
    assert client.is_available() is False


def test_is_available_false_when_both_endpoints_fail(fake_requests):
    fake_requests.queue_get(_not_ok_response())
    fake_requests.queue_get(_not_ok_response())
    client = make_client()
    assert client.is_available() is False


def _ok_response():
    class _Resp:
        ok = True
        status_code = 200

        def json(self):
            return {}

    return _Resp()


def _not_ok_response():
    class _Resp:
        ok = False
        status_code = 500

        def json(self):
            return {}

    return _Resp()



# ============================================================================
# NEW SECTION (UxTests) - pre-buffer support: create_engine / file_stats /
# buffered_bytes. Added independently of the tests above; do not edit the
# tests above when touching this section.
#
# Confirmed shapes from ServerStatsLib (lib/stremio/server.py):
#   create_engine(info_hash) -> GET {base}/{hash.lower()}/create, timeout=100
#   file_stats(info_hash, file_idx) -> GET {base}/{hash.lower()}/{idx}/stats.json, timeout=10
#   both call resp.raise_for_status() then resp.json(); any requests.RequestException,
#   non-2xx, or invalid JSON (or requests is None) raises ServerError(Exception).
#   buffered_bytes(stats) -> int(round(float(streamProgress)*float(streamLen))),
#   clamped >=0; returns 0 for non-dict input, missing/None fields, or any
#   TypeError/ValueError during conversion - never raises.
# ============================================================================
from lib.stremio.server import ServerError, buffered_bytes, guess_file_idx


def _json_response(data, status_code=200):
    class _Resp:
        ok = 200 <= status_code < 400

        def __init__(self):
            self.status_code = status_code

        def json(self):
            return data

        def raise_for_status(self):
            if not self.ok:
                raise requests.exceptions.HTTPError(
                    "%s error" % self.status_code, response=self
                )

    return _Resp()


def _bad_json_response(status_code=200):
    class _Resp:
        ok = 200 <= status_code < 400
        status_code_ = status_code

        def json(self):
            raise ValueError("invalid json")

        def raise_for_status(self):
            pass

    return _Resp()


# --- create_engine -----------------------------------------------------


def test_create_engine_hits_create_endpoint(fake_requests):
    fake_requests.queue_get(_json_response({"guessedFileIdx": 2, "infoHash": "aa" * 20}))
    client = make_client()
    stats = client.create_engine("aa" * 20)
    assert fake_requests.calls[0]["method"] == "GET"
    assert fake_requests.calls[0]["url"] == BASE + "/" + "aa" * 20 + "/create"
    assert stats["guessedFileIdx"] == 2


def test_create_engine_lowercases_info_hash(fake_requests):
    fake_requests.queue_get(_json_response({}))
    client = make_client()
    client.create_engine("AA" * 20)
    assert fake_requests.calls[0]["url"] == BASE + "/" + "aa" * 20 + "/create"


def test_create_engine_uses_100s_timeout(fake_requests):
    fake_requests.queue_get(_json_response({}))
    client = make_client()
    client.create_engine("aa" * 20)
    assert fake_requests.calls[0]["kwargs"].get("timeout") == 100


def test_create_engine_raises_server_error_on_connection_error(fake_requests):
    fake_requests.queue_get(requests.exceptions.ConnectionError("refused"))
    client = make_client()
    with pytest.raises(ServerError):
        client.create_engine("aa" * 20)


def test_create_engine_raises_server_error_on_http_error(fake_requests):
    fake_requests.queue_get(_json_response({}, status_code=500))
    client = make_client()
    with pytest.raises(ServerError):
        client.create_engine("aa" * 20)


def test_create_engine_raises_server_error_on_invalid_json(fake_requests):
    fake_requests.queue_get(_bad_json_response())
    client = make_client()
    with pytest.raises(ServerError):
        client.create_engine("aa" * 20)


# --- file_stats ----------------------------------------------------------


def test_file_stats_hits_per_file_endpoint(fake_requests):
    fake_requests.queue_get(_json_response({"streamProgress": 0.5, "streamLen": 1000}))
    client = make_client()
    stats = client.file_stats("bb" * 20, 3)
    assert fake_requests.calls[0]["method"] == "GET"
    assert fake_requests.calls[0]["url"] == BASE + "/" + "bb" * 20 + "/3/stats.json"
    assert stats["streamProgress"] == 0.5


def test_file_stats_lowercases_info_hash(fake_requests):
    fake_requests.queue_get(_json_response({}))
    client = make_client()
    client.file_stats("BB" * 20, 0)
    assert fake_requests.calls[0]["url"] == BASE + "/" + "bb" * 20 + "/0/stats.json"


def test_file_stats_uses_10s_timeout(fake_requests):
    fake_requests.queue_get(_json_response({}))
    client = make_client()
    client.file_stats("cc" * 20, 0)
    assert fake_requests.calls[0]["kwargs"].get("timeout") == 10


def test_file_stats_raises_server_error_on_connection_error(fake_requests):
    fake_requests.queue_get(requests.exceptions.ConnectionError("refused"))
    client = make_client()
    with pytest.raises(ServerError):
        client.file_stats("cc" * 20, 0)


def test_file_stats_raises_server_error_on_http_error(fake_requests):
    fake_requests.queue_get(_json_response({}, status_code=404))
    client = make_client()
    with pytest.raises(ServerError):
        client.file_stats("cc" * 20, 0)


# --- buffered_bytes ------------------------------------------------------


def test_buffered_bytes_normal_progress():
    assert buffered_bytes({"streamProgress": 0.5, "streamLen": 1000}) == 500


def test_buffered_bytes_rounds_to_nearest_int():
    assert buffered_bytes({"streamProgress": 1.0 / 3.0, "streamLen": 10}) == 3


def test_buffered_bytes_missing_fields_returns_zero():
    assert buffered_bytes({}) == 0


def test_buffered_bytes_none_progress_returns_zero():
    assert buffered_bytes({"streamProgress": None, "streamLen": 1000}) == 0


def test_buffered_bytes_none_len_returns_zero():
    assert buffered_bytes({"streamProgress": 0.5, "streamLen": None}) == 0


def test_buffered_bytes_negative_progress_clamped_to_zero():
    assert buffered_bytes({"streamProgress": -0.1, "streamLen": 1000}) == 0


def test_buffered_bytes_non_dict_input_returns_zero():
    assert buffered_bytes(None) == 0
    assert buffered_bytes("not a dict") == 0
    assert buffered_bytes([]) == 0


def test_buffered_bytes_non_numeric_fields_returns_zero():
    assert buffered_bytes({"streamProgress": "half", "streamLen": 1000}) == 0


def test_buffered_bytes_full_progress():
    assert buffered_bytes({"streamProgress": 1.0, "streamLen": 123456}) == 123456


# --- guess_file_idx --------------------------------------------------------
#
# Live-verified gap (stremio-server-go v0.8.5, Sintel torrent
# 08ada5a7a6183aae1e09d831df6748d566095a10): /create's response never
# gains guessedFileIdx - only per-file stats.json responses do - but DOES
# carry a `files` array once metadata resolves:
# [{name, path, length, offset}, ...].


def test_guess_file_idx_explicit_guessed_file_idx_wins():
    stats = {"guessedFileIdx": 3, "files": [{"length": 1}, {"length": 999}]}
    assert guess_file_idx(stats) == 3


def test_guess_file_idx_zero_guessed_file_idx_is_valid():
    stats = {"guessedFileIdx": 0, "files": [{"length": 1}, {"length": 999}]}
    assert guess_file_idx(stats) == 0


def test_guess_file_idx_negative_guessed_file_idx_falls_back_to_files():
    stats = {"guessedFileIdx": -1, "files": [{"length": 10}, {"length": 20}]}
    assert guess_file_idx(stats) == 1


def test_guess_file_idx_picks_largest_file_when_no_guess():
    stats = {"files": [{"length": 100}, {"length": 5000}, {"length": 2000}]}
    assert guess_file_idx(stats) == 1


def test_guess_file_idx_ties_pick_first_index():
    stats = {"files": [{"length": 500}, {"length": 500}]}
    assert guess_file_idx(stats) == 0


def test_guess_file_idx_missing_length_treated_as_zero():
    stats = {"files": [{"name": "no-length"}, {"length": 5}]}
    assert guess_file_idx(stats) == 1


def test_guess_file_idx_empty_files_returns_none():
    assert guess_file_idx({"files": []}) is None


def test_guess_file_idx_no_files_no_guess_returns_none():
    assert guess_file_idx({}) is None


def test_guess_file_idx_files_not_a_list_returns_none():
    assert guess_file_idx({"files": "nope"}) is None


def test_guess_file_idx_non_dict_stats_returns_none():
    assert guess_file_idx(None) is None
    assert guess_file_idx("garbage") is None
    assert guess_file_idx([]) is None


def test_guess_file_idx_garbage_file_entries_treated_as_zero_length():
    stats = {"files": [None, "x", 42, {"length": "not-a-number"}, {"length": 7}]}
    assert guess_file_idx(stats) == 4


# ============================================================================
# iter_front: front-priming readiness probe (live-verified fix)
#
# Bug this defends against: pre-buffer used to poll aggregate
# streamProgress/streamLen (buffered_bytes()), which can report megabytes
# "buffered" while the file's FRONT (offset 0 - where ffmpeg's container
# probe reads from) was never downloaded, since torrent pieces arrive out
# of order. Live-verified against a real stremio-server-go instance: a
# 1-peer torrent reached buffered=22.7MB by that metric yet a Range read
# of the front returned 0 bytes, reproducing Kodi's exact
# CURLE_PARTIAL_FILE(18)/"error probing input format" failure; a
# well-seeded torrent's front read returned data immediately. iter_front()
# streams a `Range: bytes=0-(want_bytes-1)` GET and yields each chunk's
# length, so pre-buffer can measure (and drive) front availability
# directly instead of trusting an aggregate/scattered-pieces metric.
# ============================================================================


class _StreamResp:
    """Stand-in for a streamed requests.Response (resp.iter_content())."""

    def __init__(self, chunks, status_code=200, raise_after=None):
        self._chunks = list(chunks)
        self.status_code = status_code
        self.ok = 200 <= status_code < 400
        self._raise_after = raise_after
        self.closed = False

    def raise_for_status(self):
        if not self.ok:
            raise requests.exceptions.HTTPError("%s error" % self.status_code, response=self)

    def iter_content(self, chunk_size=None):
        for chunk in self._chunks:
            yield chunk
        if self._raise_after is not None:
            raise self._raise_after

    def close(self):
        self.closed = True


def test_iter_front_requests_range_header_and_streams(fake_requests):
    fake_requests.queue_get(_StreamResp([b"a" * 1024, b"b" * 1024]))
    client = make_client()

    lengths = list(client.iter_front("AA" * 20, 5, want_bytes=2048))

    assert lengths == [1024, 1024]
    call = fake_requests.calls[0]
    assert call["url"] == BASE + "/" + "aa" * 20 + "/5"  # info_hash lower-cased like other methods
    assert call["kwargs"]["headers"] == {"Range": "bytes=0-2047"}
    assert call["kwargs"]["stream"] is True


def test_iter_front_stops_early_once_want_bytes_satisfied(fake_requests):
    fake_requests.queue_get(_StreamResp([b"a" * 1024, b"b" * 1024, b"c" * 1024]))
    client = make_client()

    lengths = list(client.iter_front("bb" * 20, 0, want_bytes=1500))

    assert lengths == [1024, 1024]  # stops after the SECOND chunk crosses want_bytes


def test_iter_front_skips_empty_chunks(fake_requests):
    fake_requests.queue_get(_StreamResp([b"", b"abcd", b""]))
    client = make_client()

    assert list(client.iter_front("cc" * 20, 0, want_bytes=4)) == [4]


def test_iter_front_default_chunk_size_and_timeout_passed_through(fake_requests):
    fake_requests.queue_get(_StreamResp([b"x"]))
    client = make_client()

    list(client.iter_front("dd" * 20, 0, want_bytes=1))

    assert fake_requests.calls[0]["kwargs"].get("timeout") == 60


def test_iter_front_raises_server_error_on_connection_failure(fake_requests):
    fake_requests.queue_get(requests.exceptions.ConnectionError("refused"))
    client = make_client()
    with pytest.raises(ServerError):
        list(client.iter_front("ee" * 20, 0, want_bytes=1024))


def test_iter_front_raises_server_error_on_http_error(fake_requests):
    fake_requests.queue_get(_StreamResp([], status_code=500))
    client = make_client()
    with pytest.raises(ServerError):
        list(client.iter_front("ff" * 20, 0, want_bytes=1024))


def test_iter_front_raises_server_error_when_zero_bytes_then_stream_error(fake_requests):
    """A torrent with no peers at offset 0: the request succeeds but the
    stream yields nothing before the connection drops - the exact live
    symptom (Batman 1989, 1 peer: 0 bytes, instantly). Zero usable bytes
    from this attempt -> raise, so the caller (player.py's retry loop)
    knows this attempt produced nothing and should wait before retrying.
    """
    fake_requests.queue_get(
        _StreamResp([], raise_after=requests.exceptions.ChunkedEncodingError("closed"))
    )
    client = make_client()
    with pytest.raises(ServerError):
        list(client.iter_front("aa" * 20, 1, want_bytes=1024))


def test_iter_front_tolerates_partial_read_then_mid_stream_close(fake_requests):
    """Some front bytes arrived before the connection closed early - the
    exact live symptom for a well-seeded torrent under load (Sintel: 254KB
    delivered then IncompleteRead). Still useful data, so this must NOT
    raise; the generator just ends, yielding what it got.
    """
    fake_requests.queue_get(
        _StreamResp([b"x" * 512], raise_after=requests.exceptions.ChunkedEncodingError("closed"))
    )
    client = make_client()

    lengths = list(client.iter_front("bb" * 20, 3, want_bytes=4096))

    assert lengths == [512]


def test_iter_front_raises_when_requests_module_unavailable(fake_requests, monkeypatch):
    import lib.stremio.server as server_module

    monkeypatch.setattr(server_module, "requests", None)
    client = make_client()
    with pytest.raises(ServerError):
        list(client.iter_front("cc" * 20, 0, want_bytes=1024))


def test_iter_front_closes_response_when_done(fake_requests):
    resp = _StreamResp([b"a" * 10])
    fake_requests.queue_get(resp)
    client = make_client()

    list(client.iter_front("dd" * 20, 0, want_bytes=10))

    assert resp.closed is True
