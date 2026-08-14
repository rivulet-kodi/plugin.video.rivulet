"""Protocol tests for lib.stremio.server (streaming-server URL resolution).

Reference: stremio-core src/types/resource/stream.rs (Stream::convert) and
src/constants.rs (STREAMING_SERVER_URL). No network access - ServerClient
methods that hit stremio-server-go are exercised by substituting
`client.session` with `tests.conftest.FakeSession`.
"""
from urllib.parse import urlencode

import pytest
import requests

from lib.stremio.server import ServerClient, UnsupportedStreamError
from tests.conftest import FakeSession

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


def test_resolve_stream_external_url_raises_unsupported():
    """externalUrl (StreamSource::External) is a native-app deep link,
    never a URL Kodi's player can open - resolve_stream must raise
    UnsupportedStreamError, not silently return None (see
    test_resolve_stream_player_frame_url_raises_unsupported for the
    companion PlayerFrame case)."""
    client = make_client()
    stream = {"externalUrl": "https://example.com/watch"}
    with pytest.raises(UnsupportedStreamError):
        client.resolve_stream(stream)


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


# --- resolve_stream: direct url scheme allowlist (_DIRECT_URL_SCHEMES) -----


@pytest.mark.parametrize("scheme,host_path", [
    ("http", "example.com/video.mp4"),
    ("https", "example.com/video.mp4"),
    ("smb", "nas.local/share/movie.mkv"),
    ("nfs", "nas.local/share/movie.mkv"),
    ("rtmp", "live.example.com/app/stream"),
    ("rtmps", "live.example.com/app/stream"),
    ("rtsp", "cam.example.com/stream1"),
    ("rtp", "239.0.0.1:1234"),
    ("udp", "239.0.0.1:1234"),
])
def test_resolve_stream_allows_every_direct_network_media_scheme(scheme, host_path):
    client = make_client()
    url = "%s://%s" % (scheme, host_path)
    stream = {"url": url}
    assert client.resolve_stream(stream) == url


@pytest.mark.parametrize("url", [
    "plugin://plugin.video.rivulet/?action=play",
    "script://special/path",
    "special://home/addons/evil",
    "file:///etc/passwd",
    "javascript:alert(1)",
])
def test_resolve_stream_rejects_kodi_control_and_local_schemes(url):
    client = make_client()
    with pytest.raises(UnsupportedStreamError):
        client.resolve_stream({"url": url})


@pytest.mark.parametrize("url", [
    "example.com/video.mp4",  # relative: no scheme
    "https://",  # malformed: no host
    "https:///video.mp4",  # malformed: empty host
    "not a url at all",
])
def test_resolve_stream_rejects_empty_relative_and_malformed_direct_urls(url):
    client = make_client()
    with pytest.raises(UnsupportedStreamError):
        client.resolve_stream({"url": url})


def test_resolve_stream_empty_url_falls_back_to_info_hash():
    """An empty `url` string is treated as absent, not validated - old
    compatibility restored: a Stream carrying both an empty url
    placeholder and an infoHash must still resolve via the torrent
    fallback, not raise UnsupportedStreamError."""
    client = make_client()
    stream = {"url": "", "infoHash": "aa" * 20}
    resolved = client.resolve_stream(stream)
    assert resolved == client.torrent_url("aa" * 20, -1, [])


def test_resolve_stream_empty_url_falls_back_to_yt_id():
    client = make_client()
    stream = {"url": "", "ytId": "dQw4w9WgXcQ"}
    assert client.resolve_stream(stream) == BASE + "/yt/dQw4w9WgXcQ"


def test_resolve_stream_empty_url_with_no_fallback_returns_none():
    client = make_client()
    assert client.resolve_stream({"url": ""}) is None


def test_resolve_stream_rejects_non_string_direct_url():
    client = make_client()
    with pytest.raises(UnsupportedStreamError):
        client.resolve_stream({"url": 12345})


def test_resolve_stream_rejected_scheme_error_omits_userinfo_path_query_fragment():
    """A rejected disallowed-scheme url may embed a credential/token a
    malicious or misconfigured addon put there - the raised message
    must carry only the bare scheme, never the userinfo/host/path/
    query/fragment (this message reaches kodi.log and a user-visible
    notification via lib.ui.player)."""
    client = make_client()
    secret_url = "plugin://user:SUPERSECRETPASS@evil.example.com/steal?token=SECRETTOKEN#frag"
    with pytest.raises(UnsupportedStreamError) as excinfo:
        client.resolve_stream({"url": secret_url})
    message = str(excinfo.value)
    assert "plugin" in message
    for secret in ("SUPERSECRETPASS", "SECRETTOKEN", "steal", "frag", "evil.example.com"):
        assert secret not in message


def test_resolve_stream_malformed_url_error_omits_embedded_secrets():
    """A malformed (no scheme/host) url that still embeds secret-shaped
    text (e.g. a query string) must raise a generic message with no
    part of the input echoed back."""
    client = make_client()
    secret_url = "not-a-url?token=SECRETTOKEN"
    with pytest.raises(UnsupportedStreamError) as excinfo:
        client.resolve_stream({"url": secret_url})
    assert "SECRETTOKEN" not in str(excinfo.value)
    assert secret_url not in str(excinfo.value)


# --- is_available ----------------------------------------------------------


def test_is_available_true_when_settings_ok():
    client = make_client()
    client.session = FakeSession(responses=[_ok_response()])
    assert client.is_available() is True
    assert client.session.calls[0]["url"] == BASE + "/settings"


def test_is_available_falls_back_to_stats_json():
    client = make_client()
    client.session = FakeSession(responses=[_not_ok_response(), _ok_response()])
    assert client.is_available() is True
    urls = [c["url"] for c in client.session.calls]
    assert urls == [BASE + "/settings", BASE + "/stats.json"]


def test_is_available_false_on_connection_error():
    client = make_client()
    client.session = FakeSession(
        responses=[
            requests.exceptions.ConnectionError("refused"),
            requests.exceptions.ConnectionError("refused"),
        ]
    )
    assert client.is_available() is False


def test_is_available_false_when_both_endpoints_fail():
    client = make_client()
    client.session = FakeSession(responses=[_not_ok_response(), _not_ok_response()])
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


def test_create_engine_hits_create_endpoint():
    client = make_client()
    client.session = FakeSession(
        responses=[_json_response({"guessedFileIdx": 2, "infoHash": "aa" * 20})]
    )
    stats = client.create_engine("aa" * 20)
    assert client.session.calls[0]["method"] == "GET"
    assert client.session.calls[0]["url"] == BASE + "/" + "aa" * 20 + "/create"
    assert stats["guessedFileIdx"] == 2


def test_create_engine_lowercases_info_hash():
    client = make_client()
    client.session = FakeSession(responses=[_json_response({})])
    client.create_engine("AA" * 20)
    assert client.session.calls[0]["url"] == BASE + "/" + "aa" * 20 + "/create"


def test_create_engine_uses_100s_timeout():
    client = make_client()
    client.session = FakeSession(responses=[_json_response({})])
    client.create_engine("aa" * 20)
    assert client.session.calls[0]["kwargs"].get("timeout") == 100


def test_create_engine_raises_server_error_on_connection_error():
    client = make_client()
    client.session = FakeSession(exc=requests.exceptions.ConnectionError("refused"))
    with pytest.raises(ServerError):
        client.create_engine("aa" * 20)


def test_create_engine_raises_server_error_on_http_error():
    client = make_client()
    client.session = FakeSession(responses=[_json_response({}, status_code=500)])
    with pytest.raises(ServerError):
        client.create_engine("aa" * 20)


def test_create_engine_raises_server_error_on_invalid_json():
    client = make_client()
    client.session = FakeSession(responses=[_bad_json_response()])
    with pytest.raises(ServerError):
        client.create_engine("aa" * 20)


# --- file_stats ----------------------------------------------------------


def test_file_stats_hits_per_file_endpoint():
    client = make_client()
    client.session = FakeSession(
        responses=[_json_response({"streamProgress": 0.5, "streamLen": 1000})]
    )
    stats = client.file_stats("bb" * 20, 3)
    assert client.session.calls[0]["method"] == "GET"
    assert client.session.calls[0]["url"] == BASE + "/" + "bb" * 20 + "/3/stats.json"
    assert stats["streamProgress"] == 0.5


def test_file_stats_lowercases_info_hash():
    client = make_client()
    client.session = FakeSession(responses=[_json_response({})])
    client.file_stats("BB" * 20, 0)
    assert client.session.calls[0]["url"] == BASE + "/" + "bb" * 20 + "/0/stats.json"


def test_file_stats_uses_10s_timeout():
    client = make_client()
    client.session = FakeSession(responses=[_json_response({})])
    client.file_stats("cc" * 20, 0)
    assert client.session.calls[0]["kwargs"].get("timeout") == 10


def test_file_stats_raises_server_error_on_connection_error():
    client = make_client()
    client.session = FakeSession(exc=requests.exceptions.ConnectionError("refused"))
    with pytest.raises(ServerError):
        client.file_stats("cc" * 20, 0)


def test_file_stats_raises_server_error_on_http_error():
    client = make_client()
    client.session = FakeSession(responses=[_json_response({}, status_code=404)])
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
        yield from self._chunks
        if self._raise_after is not None:
            raise self._raise_after

    def close(self):
        self.closed = True


def test_iter_front_requests_range_header_and_streams():
    client = make_client()
    client.session = FakeSession(responses=[_StreamResp([b"a" * 1024, b"b" * 1024])])

    lengths = list(client.iter_front("AA" * 20, 5, want_bytes=2048))

    assert lengths == [1024, 1024]
    call = client.session.calls[0]
    assert call["url"] == BASE + "/" + "aa" * 20 + "/5"  # info_hash lower-cased like other methods
    assert call["kwargs"]["headers"] == {"Range": "bytes=0-2047"}
    assert call["kwargs"]["stream"] is True


def test_iter_front_stops_early_once_want_bytes_satisfied():
    client = make_client()
    client.session = FakeSession(
        responses=[_StreamResp([b"a" * 1024, b"b" * 1024, b"c" * 1024])]
    )

    lengths = list(client.iter_front("bb" * 20, 0, want_bytes=1500))

    assert lengths == [1024, 1024]  # stops after the SECOND chunk crosses want_bytes


def test_iter_front_skips_empty_chunks():
    client = make_client()
    client.session = FakeSession(responses=[_StreamResp([b"", b"abcd", b""])])

    assert list(client.iter_front("cc" * 20, 0, want_bytes=4)) == [4]


def test_iter_front_default_chunk_size_and_timeout_passed_through():
    client = make_client()
    client.session = FakeSession(responses=[_StreamResp([b"x"])])

    list(client.iter_front("dd" * 20, 0, want_bytes=1))

    assert client.session.calls[0]["kwargs"].get("timeout") == 60


def test_iter_front_raises_server_error_on_connection_failure():
    client = make_client()
    client.session = FakeSession(exc=requests.exceptions.ConnectionError("refused"))
    with pytest.raises(ServerError):
        list(client.iter_front("ee" * 20, 0, want_bytes=1024))


def test_iter_front_raises_server_error_on_http_error():
    client = make_client()
    client.session = FakeSession(responses=[_StreamResp([], status_code=500)])
    with pytest.raises(ServerError):
        list(client.iter_front("ff" * 20, 0, want_bytes=1024))


def test_iter_front_raises_server_error_when_zero_bytes_then_stream_error():
    """A torrent with no peers at offset 0: the request succeeds but the
    stream yields nothing before the connection drops - the exact live
    symptom (Batman 1989, 1 peer: 0 bytes, instantly). Zero usable bytes
    from this attempt -> raise, so the caller (player.py's retry loop)
    knows this attempt produced nothing and should wait before retrying.
    """
    client = make_client()
    client.session = FakeSession(
        responses=[_StreamResp([], raise_after=requests.exceptions.ChunkedEncodingError("closed"))]
    )
    with pytest.raises(ServerError):
        list(client.iter_front("aa" * 20, 1, want_bytes=1024))


def test_iter_front_tolerates_partial_read_then_mid_stream_close():
    """Some front bytes arrived before the connection closed early - the
    exact live symptom for a well-seeded torrent under load (Sintel: 254KB
    delivered then IncompleteRead). Still useful data, so this must NOT
    raise; the generator just ends, yielding what it got.
    """
    client = make_client()
    client.session = FakeSession(
        responses=[_StreamResp([b"x" * 512], raise_after=requests.exceptions.ChunkedEncodingError("closed"))]
    )

    lengths = list(client.iter_front("bb" * 20, 3, want_bytes=4096))

    assert lengths == [512]


def test_iter_front_raises_when_requests_module_unavailable(monkeypatch):
    import lib.stremio.server as server_module

    monkeypatch.setattr(server_module, "requests", None)
    client = make_client()
    with pytest.raises(ServerError):
        list(client.iter_front("cc" * 20, 0, want_bytes=1024))


def test_iter_front_closes_response_when_done():
    resp = _StreamResp([b"a" * 10])
    client = make_client()
    client.session = FakeSession(responses=[resp])

    list(client.iter_front("dd" * 20, 0, want_bytes=10))

    assert resp.closed is True


# ============================================================================
# NEW SECTION (StreamSources) - base32 info hashes, percent-encoded
# trackers, archive/nzb/ftp `/create` payload building, and the
# UnsupportedStreamError contract for externalUrl/playerFrameUrl. Added
# independently of the sections above; do not edit them when touching
# this section.
# ============================================================================
import json
from urllib.parse import parse_qs, urlsplit

from lib.stremio.lzstring import decompress_from_encoded_uri_component
from lib.stremio.server import UNKNOWN_FILE_IDX, normalize_info_hash, normalize_trackers

_HEX_HASH = "aabbccddeeff00112233445566778899aabbccdd"
_BASE32_HASH = "VK54ZXPO74ABCIRTIRKWM54ITGVLXTG5"  # base32(unhexlify(_HEX_HASH))


def _lz_payload(url):
    """Pull the `lz` query param out of `url`, percent-decode it, run it
    through the real LZ-String decompressor, and json.loads() the
    result - i.e. reverse exactly what `ServerClient._lz_query_url`
    built, to assert on the JSON body a real stremio-server-go would
    see."""
    query = parse_qs(urlsplit(url).query)
    compressed = query["lz"][0]
    decompressed = decompress_from_encoded_uri_component(compressed)
    assert decompressed is not None
    return json.loads(decompressed)


# --- normalize_info_hash / normalize_trackers -----------------------------


def test_normalize_info_hash_hex_lowercased():
    assert normalize_info_hash(_HEX_HASH.upper()) == _HEX_HASH


def test_normalize_info_hash_base32_decodes_to_hex():
    assert normalize_info_hash(_BASE32_HASH) == _HEX_HASH
    assert normalize_info_hash(_BASE32_HASH.lower()) == _HEX_HASH


def test_normalize_info_hash_wrong_length_returns_none():
    assert normalize_info_hash(_HEX_HASH[:-1]) is None  # 39 chars
    assert normalize_info_hash(_BASE32_HASH[:-1]) is None  # 31 chars


def test_normalize_info_hash_non_hex_non_base32_garbage_returns_none():
    assert normalize_info_hash("g" * 40) is None  # right length, not valid hex
    assert normalize_info_hash("0" * 32) is None  # right length, '0'/'1' not in RFC 4648 base32


def test_normalize_info_hash_non_string_returns_none():
    assert normalize_info_hash(None) is None
    assert normalize_info_hash(12345) is None


def test_normalize_trackers_percent_decodes_dedupes_preserves_order():
    trackers = [
        "udp%3A%2F%2Ftracker1.example%2Fannounce",
        "udp://tracker1.example/announce",  # decodes to the same value - dropped
        "udp://tracker2.example/announce",
        "",  # empty - dropped
    ]
    assert normalize_trackers(trackers) == [
        "udp://tracker1.example/announce",
        "udp://tracker2.example/announce",
    ]


def test_torrent_url_percent_encoded_tracker_decoded_and_deduped():
    client = make_client()
    trackers = [
        "udp%3A%2F%2Ftracker1.example%2Fannounce",
        "udp://tracker1.example/announce",
        "udp://tracker2.example/announce",
    ]
    url = client.torrent_url(_HEX_HASH, 0, trackers)
    query = parse_qs(urlsplit(url).query)
    assert query["tr"] == ["udp://tracker1.example/announce", "udp://tracker2.example/announce"]


# --- resolve_stream: base32 magnet / garbage infoHash ----------------------


def test_resolve_stream_magnet_base32_info_hash_resolves_to_hex_url():
    client = make_client()
    stream = {"url": "magnet:?xt=urn:btih:%s" % _BASE32_HASH}
    resolved = client.resolve_stream(stream)
    assert resolved == client.torrent_url(_HEX_HASH, UNKNOWN_FILE_IDX, [])


def test_resolve_stream_garbage_info_hash_returns_none():
    client = make_client()
    stream = {"infoHash": "not-a-valid-hash-at-all-and-31-chars"}
    assert client.resolve_stream(stream) is None


# --- archive kinds (rar/zip/7zip/tgz/tar) -----------------------------------


@pytest.mark.parametrize(
    ("stream_key", "url_kind"),
    [
        ("rarUrls", "rar"),
        ("zipUrls", "zip"),
        ("7zipUrls", "7zip"),
        ("tgzUrls", "tgz"),
        ("tarUrls", "tar"),
    ],
)
def test_resolve_stream_archive_kinds_build_create_url_and_payload(stream_key, url_kind):
    client = make_client()
    stream = {
        stream_key: [["https://example.com/file.rar", 10000], ["https://example.com/file2.rar"]],
        "fileIdx": 1,
        "fileMustInclude": ["includeFile1"],
    }
    resolved = client.resolve_stream(stream)
    assert resolved.startswith("%s/%s/create?" % (BASE, url_kind))
    assert _lz_payload(resolved) == {
        "urls": [["https://example.com/file.rar", 10000], ["https://example.com/file2.rar"]],
        "fileIdx": 1,
        "fileMustInclude": ["includeFile1"],
    }


def test_resolve_stream_archive_omits_file_idx_and_file_must_include_when_absent():
    client = make_client()
    stream = {"rarUrls": [["https://example.com/file.rar"]]}
    resolved = client.resolve_stream(stream)
    assert _lz_payload(resolved) == {"urls": [["https://example.com/file.rar"]]}


def test_resolve_stream_archive_no_urls_returns_none():
    client = make_client()
    assert client.resolve_stream({"rarUrls": []}) is None


def test_resolve_stream_archive_ftp_member_rewritten_through_ftp_create():
    client = make_client()
    stream = {"zipUrls": [["ftp://ftp.example.com/path/movie.mkv", 5000]]}
    resolved = client.resolve_stream(stream)
    payload = _lz_payload(resolved)
    assert len(payload["urls"]) == 1
    ftp_proxy_url, num_bytes = payload["urls"][0]
    assert num_bytes == 5000
    assert ftp_proxy_url.startswith(BASE + "/ftp/movie.mkv?")
    assert _lz_payload(ftp_proxy_url) == {"ftpUrl": "ftp://ftp.example.com/path/movie.mkv"}


# --- nzb ---------------------------------------------------------------


def test_resolve_stream_nzb_single_url():
    client = make_client()
    stream = {
        "nzbUrl": "https://example.com/release.nzb",
        "servers": ["https://usenet1.example.com", "https://usenet2.example.com"],
    }
    resolved = client.resolve_stream(stream)
    assert resolved.startswith(BASE + "/nzb/create?")
    assert _lz_payload(resolved) == {
        "nzbUrl": "https://example.com/release.nzb",
        "servers": ["https://usenet1.example.com", "https://usenet2.example.com"],
    }


def test_resolve_stream_nzb_multi_url():
    client = make_client()
    stream = {
        "nzbUrls": ["https://example.com/a.nzb", "https://example.com/b.nzb"],
        "servers": ["https://usenet1.example.com"],
    }
    resolved = client.resolve_stream(stream)
    assert _lz_payload(resolved) == {
        "nzbUrls": ["https://example.com/a.nzb", "https://example.com/b.nzb"],
        "servers": ["https://usenet1.example.com"],
    }


def test_resolve_stream_nzb_without_servers_returns_none():
    client = make_client()
    stream = {"nzbUrl": "https://example.com/release.nzb", "servers": []}
    assert client.resolve_stream(stream) is None


# --- ftp (top-level bare stream) -----------------------------------------


def test_resolve_stream_bare_ftp_url_builds_ftp_create():
    client = make_client()
    stream = {"url": "ftp://ftp.example.com/dir/movie.mkv"}
    resolved = client.resolve_stream(stream)
    assert resolved.startswith(BASE + "/ftp/movie.mkv?")
    assert _lz_payload(resolved) == {"ftpUrl": "ftp://ftp.example.com/dir/movie.mkv"}


# --- UnsupportedStreamError --------------------------------------------


def test_resolve_stream_player_frame_url_raises_unsupported():
    client = make_client()
    stream = {"playerFrameUrl": "https://example.com/embed"}
    with pytest.raises(UnsupportedStreamError):
        client.resolve_stream(stream)


def test_unsupported_stream_error_is_a_server_error():
    assert issubclass(UnsupportedStreamError, ServerError)
