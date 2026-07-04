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
