"""Tests for resources/s4me_bridge/bridge_helpers.py: the pure,
Kodi-independent AND Stream4Me-independent request/response shaping logic
the S4Me bridge script uses.

Loaded via `importlib` from its exact file path rather than a normal
package import -- `resources/` ships Kodi skin/language assets, not a
Python package, and `bridge_helpers.py` is deliberately NOT part of
Rivulet's `lib` package (see its own module docstring for why `bridge.py`
must never import anything named `lib` that belongs to Rivulet). This
mirrors exactly how `bridge.py` itself loads it at runtime: a plain
sibling-file import once its own directory is on `sys.path`.
"""
import importlib.util
import os

import pytest

from lib import s4me as rivulet_s4me

_HELPERS_PATH = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "resources", "s4me_bridge", "bridge_helpers.py")
)
_spec = importlib.util.spec_from_file_location("s4me_bridge_helpers_under_test", _HELPERS_PATH)
bh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bh)


# --- manifest kept in sync with lib.s4me ------------------------------------


def test_manifest_matches_lib_s4me_manifest_exactly():
    assert bh.MANIFEST == rivulet_s4me.MANIFEST


def test_build_manifest_returns_a_copy_not_the_module_constant():
    copy = bh.build_manifest()
    copy["id"] = "mutated"
    assert bh.MANIFEST["id"] == "org.rivulet.s4me"


# --- parse_stream_id --------------------------------------------------------


@pytest.mark.parametrize("id_,expected", [
    ("tt1234567", ("tt1234567", None, None)),
    ("tt1234567:1:2", ("tt1234567", 1, 2)),
    ("tt0000001:10:22", ("tt0000001", 10, 22)),
])
def test_parse_stream_id_valid(id_, expected):
    assert bh.parse_stream_id(id_) == expected


@pytest.mark.parametrize("id_", [
    None, "", "notanid", "tt", "1234567", "tt1234567:1", "tt1234567:a:b",
])
def test_parse_stream_id_invalid_returns_none(id_):
    assert bh.parse_stream_id(id_) is None


# --- split_kodi_url ----------------------------------------------------------


def test_split_kodi_url_no_pipe_returns_url_and_empty_headers():
    assert bh.split_kodi_url("https://example.com/video.mp4") == ("https://example.com/video.mp4", {})


def test_split_kodi_url_none_returns_empty_string_and_empty_headers():
    assert bh.split_kodi_url(None) == ("", {})


def test_split_kodi_url_splits_and_decodes_headers():
    raw = "https://example.com/video.mp4|Referer=https%3A%2F%2Fexample.com&User-Agent=Mozilla%2F5.0"
    url, headers = bh.split_kodi_url(raw)
    assert url == "https://example.com/video.mp4"
    assert headers == {"Referer": "https://example.com", "User-Agent": "Mozilla/5.0"}


def test_split_kodi_url_trailing_pipe_with_no_query_yields_empty_headers():
    assert bh.split_kodi_url("https://example.com/video.mp4|") == ("https://example.com/video.mp4", {})


# --- normalize_title / tmdb_id_matches / title_matches / result_matches -----


@pytest.mark.parametrize("text,expected", [
    ("", ""),
    (None, ""),
    ("The Matrix", "the matrix"),
    ("Amélie", "amelie"),
    ("Spider-Man: Homecoming", "spider man homecoming"),
])
def test_normalize_title(text, expected):
    assert bh.normalize_title(text) == expected


def test_tmdb_id_matches_true_for_equal_ids():
    assert bh.tmdb_id_matches("603", "603") is True
    assert bh.tmdb_id_matches(603, "603") is True


def test_tmdb_id_matches_false_when_target_missing():
    assert bh.tmdb_id_matches("603", None) is False
    assert bh.tmdb_id_matches("603", "") is False


def test_tmdb_id_matches_false_for_different_ids():
    assert bh.tmdb_id_matches("603", "604") is False


def test_title_matches_normalizes_and_ignores_missing_year():
    assert bh.title_matches("The Matrix", None, "the matrix", "1999") is True
    assert bh.title_matches("The Matrix", "1999", "the matrix", None) is True


def test_title_matches_false_on_year_mismatch_when_both_present():
    assert bh.title_matches("The Matrix", "1999", "The Matrix", "2003") is False


def test_title_matches_false_on_different_titles():
    assert bh.title_matches("The Matrix", "1999", "Inception", "1999") is False


def test_result_matches_prefers_tmdb_id():
    info_labels = {"tmdb_id": "603", "title": "Something Else", "year": "2020"}
    assert bh.result_matches(info_labels, "603", "The Matrix", "1999") is True


def test_result_matches_falls_back_to_title_year():
    info_labels = {"tmdb_id": "", "title": "The Matrix", "year": "1999"}
    assert bh.result_matches(info_labels, None, "The Matrix", "1999") is True


def test_result_matches_false_when_neither_matches():
    info_labels = {"tmdb_id": "1", "title": "Other", "year": "2001"}
    assert bh.result_matches(info_labels, "603", "The Matrix", "1999") is False


def test_result_matches_handles_none_info_labels():
    assert bh.result_matches(None, "603", "The Matrix", "1999") is False


# --- shape_stream ------------------------------------------------------------


def test_shape_stream_basic_no_headers():
    stream = bh.shape_stream("vvvvid", "Server1", "https://example.com/v.mp4")
    assert stream["name"] == "S4Me vvvvid"
    assert stream["title"] == "Server1"
    assert stream["url"] == "https://example.com/v.mp4"
    assert "behaviorHints" not in stream


def test_shape_stream_with_quality_appends_to_title():
    stream = bh.shape_stream("vvvvid", "Server1", "https://example.com/v.mp4", quality="1080p")
    assert stream["title"] == "Server1 - 1080p"


def test_shape_stream_with_headers_sets_behavior_hints():
    headers = {"Referer": "https://example.com"}
    stream = bh.shape_stream("vvvvid", "Server1", "https://example.com/v.mp4", headers=headers)
    assert stream["behaviorHints"] == {
        "notWebReady": True,
        "proxyHeaders": {"request": {"Referer": "https://example.com"}},
    }


def test_shape_stream_no_server_name_falls_back_to_channel_title():
    stream = bh.shape_stream("vvvvid", "", "https://example.com/v.mp4")
    assert stream["title"] == "vvvvid"


# --- parse_channels_setting / select_channels --------------------------------


@pytest.mark.parametrize("raw,expected", [
    (None, None),
    ("", None),
    ("  ", None),
    ("vvvvid", ("vvvvid",)),
    ("vvvvid,eurostreaming", ("vvvvid", "eurostreaming")),
])
def test_parse_channels_setting(raw, expected):
    assert bh.parse_channels_setting(raw) == expected


def test_select_channels_none_returns_all_active():
    assert bh.select_channels(None, ["a", "b", "c"]) == ("a", "b", "c")


def test_select_channels_filters_to_active_only():
    assert bh.select_channels(("a", "z"), ["a", "b", "c"]) == ("a",)


def test_select_channels_empty_configured_tuple_returns_empty():
    assert bh.select_channels((), ["a", "b"]) == ()


# --- TTLCache -----------------------------------------------------------------


def test_ttl_cache_returns_value_before_expiry():
    now = [0.0]
    cache = bh.TTLCache(10, clock=lambda: now[0])
    cache.set("k", "v")
    now[0] = 5.0
    assert cache.get("k") == "v"


def test_ttl_cache_expires_after_ttl():
    now = [0.0]
    cache = bh.TTLCache(10, clock=lambda: now[0])
    cache.set("k", "v")
    now[0] = 10.0
    assert cache.get("k") is None
    assert len(cache) == 0


def test_ttl_cache_missing_key_returns_none():
    cache = bh.TTLCache(10)
    assert cache.get("missing") is None


# --- Budget --------------------------------------------------------------


def test_budget_not_expired_before_deadline():
    now = [0.0]
    budget = bh.Budget(12, clock=lambda: now[0])
    now[0] = 5.0
    assert budget.expired() is False
    assert budget.remaining() == pytest.approx(7.0)


def test_budget_expired_after_deadline():
    now = [0.0]
    budget = bh.Budget(12, clock=lambda: now[0])
    now[0] = 12.0
    assert budget.expired() is True
    assert budget.remaining() == 0.0


def test_budget_remaining_never_negative():
    now = [0.0]
    budget = bh.Budget(5, clock=lambda: now[0])
    now[0] = 100.0
    assert budget.remaining() == 0.0
