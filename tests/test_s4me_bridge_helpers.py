# SPDX-License-Identifier: MIT

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
import http.client
import importlib.util
import json
import os
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from lib import s4me as rivulet_s4me

_HELPERS_PATH = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "resources", "s4me_bridge", "bridge_helpers.py")
)
_spec = importlib.util.spec_from_file_location("s4me_bridge_helpers_under_test", _HELPERS_PATH)
bh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bh)

#: `bridge.py` itself, loaded the same way (see this module's docstring) --
#: only its `resources/s4me_bridge/`-local imports run at module scope
#: (json/os/sys/threading/concurrent.futures/functools/http.server plus
#: this same sibling `bridge_helpers`), so this is safe without Kodi or
#: Stream4Me installed. Used below to test the request-orchestration
#: pieces of `_handle_stream_request()` that are not pure enough to live
#: in `bridge_helpers.py` itself (cache-key/channel-selection interplay,
#: the request budget's partial-results-on-timeout behaviour).
_BRIDGE_PATH = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "resources", "s4me_bridge", "bridge.py")
)
_bridge_spec = importlib.util.spec_from_file_location("s4me_bridge_under_test", _BRIDGE_PATH)
bridge = importlib.util.module_from_spec(_bridge_spec)
_bridge_spec.loader.exec_module(bridge)


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


def test_restore_moved_xbmc_functions_fills_kodi20_gaps():
    """Kodi 20+: xbmc lacks the helpers, xbmcvfs has them -> copied over,
    so Stream4Me's `xbmc.translatePath()` calls work in the bridge."""
    import types
    xbmc_mod = types.SimpleNamespace()
    vfs = types.SimpleNamespace(
        translatePath=lambda p: "T" + p, validatePath=lambda p: p, makeLegalFilename=lambda p: p,
    )
    assert bh.restore_moved_xbmc_functions(xbmc_mod, vfs) == list(bh.MOVED_XBMC_FUNCTIONS)
    assert xbmc_mod.translatePath("special://temp") == "Tspecial://temp"


def test_restore_moved_xbmc_functions_never_replaces_existing():
    """Kodi 18: xbmc still has them -> left untouched; and nothing missing
    from xbmcvfs is invented."""
    import types
    own = lambda p: "own"  # noqa: E731
    xbmc_mod = types.SimpleNamespace(translatePath=own)
    vfs = types.SimpleNamespace(translatePath=lambda p: "vfs")
    assert bh.restore_moved_xbmc_functions(xbmc_mod, vfs) == []
    assert xbmc_mod.translatePath is own
    assert not hasattr(xbmc_mod, "validatePath")


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


def test_result_matches_rejects_conflicting_tmdb_ids_even_with_equal_title_year():
    info_labels = {"tmdb_id": "1", "title": "The Matrix", "year": "1999"}
    assert bh.result_matches(info_labels, "603", "The Matrix", "1999") is False


def test_result_matches_title_fallback_when_candidate_has_no_tmdb_id():
    info_labels = {"tmdb_id": "", "title": "The Matrix", "year": "1999"}
    assert bh.result_matches(info_labels, "603", "The Matrix", "1999") is True


def test_result_matches_title_fallback_when_target_has_no_tmdb_id():
    info_labels = {"tmdb_id": "603", "title": "The Matrix", "year": "1999"}
    assert bh.result_matches(info_labels, None, "The Matrix", "1999") is True


# --- content_type_for_s4me / label_value_or ---------------------------------


def test_content_type_for_s4me_maps_series_to_tvshow():
    assert bh.content_type_for_s4me("series") == "tvshow"


def test_content_type_for_s4me_leaves_movie_unchanged():
    assert bh.content_type_for_s4me("movie") == "movie"


def test_label_value_or_keeps_zero_season():
    assert bh.label_value_or(0, "5") == 0


def test_label_value_or_keeps_zero_episode():
    assert bh.label_value_or(0, 3) == 0


def test_label_value_or_falls_back_when_none():
    assert bh.label_value_or(None, "attr-value") == "attr-value"


def test_label_value_or_falls_back_when_empty_string():
    assert bh.label_value_or("", "attr-value") == "attr-value"


def test_label_value_or_keeps_present_nonzero_value():
    assert bh.label_value_or(4, "attr-value") == 4


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


def test_drm_hint_needs_both_fields():
    assert bh.drm_hint("com.widevine.alpha", "https://lic") == {
        "type": "com.widevine.alpha", "license": "https://lic",
    }
    assert bh.drm_hint("com.widevine.alpha", "") is None
    assert bh.drm_hint("", "https://lic") is None
    assert bh.drm_hint(None, None) is None


def test_shape_stream_carries_drm_hint():
    drm = {"type": "com.widevine.alpha", "license": "https://lic"}
    stream = bh.shape_stream("plutotv", ".mpd", "https://x/m.mpd", adaptive_hint="mpd", drm=drm)
    assert stream["behaviorHints"]["rivuletDrm"] == drm


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


def test_ttl_cache_evicts_oldest_when_over_capacity():
    cache = bh.TTLCache(1000, max_size=2)
    cache.set("a", 1)
    cache.set("b", 2)
    cache.set("c", 3)
    assert len(cache) == 2
    assert cache.get("a") is None
    assert cache.get("b") == 2
    assert cache.get("c") == 3


def test_ttl_cache_set_purges_expired_keys_never_read_again():
    now = [0.0]
    cache = bh.TTLCache(10, clock=lambda: now[0])
    cache.set("stale", "v")
    now[0] = 11.0  # "stale" has expired, but nothing ever calls .get("stale")
    cache.set("fresh", "v2")
    assert len(cache) == 1
    assert cache.get("fresh") == "v2"


def test_ttl_cache_default_max_size_is_bounded():
    cache = bh.TTLCache(1000)
    for i in range(1000):
        cache.set("k%d" % i, i)
    assert len(cache) <= 256


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


# --- cors_allow_origin --------------------------------------------------------


def test_cors_allow_origin_matches_allowlisted_origin():
    assert bh.cors_allow_origin("https://app.strem.io") == "https://app.strem.io"


def test_cors_allow_origin_rejects_unknown_origin():
    assert bh.cors_allow_origin("https://evil.example") is None


def test_cors_allow_origin_rejects_missing_origin():
    assert bh.cors_allow_origin(None) is None


# --- collect_with_budget ------------------------------------------------------


def test_collect_with_budget_collects_all_when_nothing_expires():
    with ThreadPoolExecutor(max_workers=2) as pool:
        results, timed_out = bh.collect_with_budget(pool, {"a": lambda: [1], "b": lambda: [2, 3]}, bh.Budget(5))
    assert sorted(results) == [1, 2, 3]
    assert timed_out is False


def test_collect_with_budget_empty_tasks_returns_immediately():
    with ThreadPoolExecutor(max_workers=1) as pool:
        results, timed_out = bh.collect_with_budget(pool, {}, bh.Budget(5))
    assert results == []
    assert timed_out is False


def test_collect_with_budget_swallows_task_errors_and_reports_them():
    errors = []

    def _boom():
        raise ValueError("boom")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results, timed_out = bh.collect_with_budget(
            pool, {"ok": lambda: [1], "bad": _boom}, bh.Budget(5),
            on_error=lambda key, exc: errors.append((key, type(exc))),
        )
    assert results == [1]
    assert timed_out is False
    assert errors == [("bad", ValueError)]


def test_collect_with_budget_returns_partial_results_without_waiting_for_stragglers():
    pool = ThreadPoolExecutor(max_workers=2)
    try:
        tasks = {"fast": lambda: [1], "slow": lambda: (time.sleep(0.5) or [2])}
        started = time.monotonic()
        results, timed_out = bh.collect_with_budget(pool, tasks, bh.Budget(0.05))
        elapsed = time.monotonic() - started
    finally:
        pool.shutdown(wait=False)
    assert results == [1]
    assert timed_out is True
    assert elapsed < 0.4


def test_collect_with_budget_late_complete_receives_every_result():
    """After a timeout, on_late_complete gets ALL tasks' results once the
    stragglers finish -- not just the ones returned in time."""
    done = threading.Event()
    late = []

    def _late(all_results):
        late.extend(all_results)
        done.set()

    pool = ThreadPoolExecutor(max_workers=2)
    try:
        tasks = {"fast": lambda: [1], "slow": lambda: (time.sleep(0.3) or [2])}
        results, timed_out = bh.collect_with_budget(
            pool, tasks, bh.Budget(0.05), on_late_complete=_late, late_timeout=5,
        )
        assert (results, timed_out) == ([1], True)
        assert done.wait(3)
    finally:
        pool.shutdown(wait=False)
    assert sorted(late) == [1, 2]


def test_collect_with_budget_late_complete_not_called_when_in_time():
    called = []
    with ThreadPoolExecutor(max_workers=1) as pool:
        bh.collect_with_budget(pool, {"a": lambda: [1]}, bh.Budget(5), on_late_complete=called.append)
    assert called == []


def test_collect_with_budget_returns_immediately_when_budget_already_expired():
    """Regression test: `Budget.remaining()` returns `0` once expired, and
    `0 or None` evaluates to `None` -- passing that straight to
    `as_completed(timeout=...)` would wait with NO timeout at all for a
    still-running task, exactly defeating the 'never wait past the
    budget' contract."""
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        tasks = {"stuck": lambda: (time.sleep(1.0) or [1])}
        budget = bh.Budget(0)  # already expired by the time it's used below
        started = time.monotonic()
        results, timed_out = bh.collect_with_budget(pool, tasks, budget)
        elapsed = time.monotonic() - started
    finally:
        pool.shutdown(wait=False)
    assert results == []
    assert timed_out is True
    assert elapsed < 0.3


# --- bridge.py: _handle_stream_request orchestration -------------------------


class _FakeCache:
    def __init__(self):
        self._store = {}

    def get(self, key):
        return self._store.get(key)

    def set(self, key, value):
        self._store[key] = value


def test_defines_search_matches_top_level_def_only():
    assert bh.defines_search("import x\n\ndef search(item, text):\n    pass\n")
    assert not bh.defines_search("def mainlist(item):\n    def search(x): pass\n")
    assert not bh.defines_search("")
    assert not bh.defines_search(None)


@pytest.mark.parametrize("content_type, expected", [
    ("movie", ("films", "anime", "untagged")),
    ("series", ("shows", "anime", "untagged")),
    ("other", ("films", "shows", "anime", "untagged", "live")),
])
def test_channels_for_type_filters_by_category_and_search(content_type, expected):
    channels = (
        ("films", ("movie",), True),
        ("shows", ("tvshow", "documentary"), True),
        ("anime", ("anime",), True),
        ("untagged", (), True),
        ("live", ("live",), True),
        ("nosearch", ("movie", "tvshow"), False),
    )
    assert bh.channels_for_type(channels, content_type) == expected


def test_channel_catalog_reads_categories_and_search(tmp_path):
    """The catalog skips inactive channels and flags ones lacking search()."""
    import json as _json
    ch = tmp_path / "channels"
    ch.mkdir()
    specs = {
        "films": ({"id": "films", "active": True, "categories": ["movie"]}, "def search(i, t):\n    pass\n"),
        "radio": ({"id": "radio", "active": True, "categories": ["movie"]}, "def mainlist(i):\n    pass\n"),
        "off": ({"id": "off", "active": False, "categories": ["movie"]}, "def search(i, t):\n    pass\n"),
    }
    for name, (meta, src) in specs.items():
        (ch / (name + ".json")).write_text(_json.dumps(meta))
        (ch / (name + ".py")).write_text(src)
    catalog = bridge._ChannelCatalog(str(tmp_path))
    assert catalog.active_channel_ids() == ("films", "radio")
    assert catalog.active_channel_ids("movie") == ("films",)
    assert catalog.active_channel_ids("series") == ()


class _FakeState:
    def __init__(self, channels):
        self.cache = _FakeCache()
        self._channels = channels

    def channels_for_request(self, content_type=None):
        return self._channels


def test_handle_stream_request_cache_key_includes_channel_selection(monkeypatch):
    """A response cached under one channel selection must never be reused
    for a request that resolves a different one (e.g. after the user
    edits s4me_channels) -- see channels_for_request()'s callers."""
    monkeypatch.setattr(bridge, "_resolve_title", lambda imdb_id, search_type: ("Title", "1999", "603"))
    monkeypatch.setattr(
        bridge, "_streams_for_channel",
        lambda channel_id, *a, **k: [{"url": "u-%s" % channel_id}],
    )
    shared_cache = _FakeCache()
    state_a = _FakeState(("chan1",))
    state_a.cache = shared_cache
    state_b = _FakeState(("chan2",))
    state_b.cache = shared_cache

    result_a = bridge._handle_stream_request(state_a, "movie", "tt1234567")
    result_b = bridge._handle_stream_request(state_b, "movie", "tt1234567")

    assert result_a["streams"] == [{"url": "u-chan1"}]
    assert result_b["streams"] == [{"url": "u-chan2"}]


def test_handle_stream_request_cache_hit_for_repeated_same_selection(monkeypatch):
    calls = []
    monkeypatch.setattr(bridge, "_resolve_title", lambda imdb_id, search_type: ("Title", "1999", "603"))

    def _fake(channel_id, *a, **k):
        calls.append(channel_id)
        return [{"url": "u"}]

    monkeypatch.setattr(bridge, "_streams_for_channel", _fake)
    state = _FakeState(("chan1",))

    bridge._handle_stream_request(state, "movie", "tt1234567")
    bridge._handle_stream_request(state, "movie", "tt1234567")

    assert calls == ["chan1"]


def test_handle_stream_request_returns_partial_results_on_budget_timeout(monkeypatch):
    """A channel exceeding the request budget must yield whatever the
    other channels already returned, not raise/500 and not block on the
    straggler -- see _handle_stream_request()'s use of collect_with_budget()."""
    monkeypatch.setattr(bridge, "_resolve_title", lambda imdb_id, search_type: ("Title", "1999", "603"))
    monkeypatch.setattr(bridge, "_REQUEST_BUDGET_SECONDS", 0.1)

    def _fake(channel_id, *a, **k):
        if channel_id == "slow":
            time.sleep(0.5)
            return [{"url": "slow-result"}]
        return [{"url": "fast-result"}]

    monkeypatch.setattr(bridge, "_streams_for_channel", _fake)
    state = _FakeState(("fast", "slow"))

    started = time.monotonic()
    result = bridge._handle_stream_request(state, "movie", "tt1234567")
    elapsed = time.monotonic() - started

    assert result["streams"] == [{"url": "fast-result"}]
    assert elapsed < 0.4
    assert state.cache.get(("movie", "tt1234567", None, None, ("fast", "slow"))) is None


def test_handle_stream_request_caches_only_the_complete_late_result(monkeypatch):
    """A timed-out request returns partial results WITHOUT caching them
    (that would hide the slow channel for the 30-minute TTL). Once the
    slow channel finishes in the background, the complete result is
    cached, so the next identical request is an instant, full cache hit."""
    monkeypatch.setattr(bridge, "_resolve_title", lambda imdb_id, search_type: ("Title", "1999", "603"))
    monkeypatch.setattr(bridge, "_REQUEST_BUDGET_SECONDS", 0.1)

    calls = []
    finished = threading.Event()

    def _fake(channel_id, *a, **k):
        calls.append(channel_id)
        if channel_id == "slow":
            time.sleep(0.4)
            finished.set()
        return [{"url": "%s-result" % channel_id}]

    monkeypatch.setattr(bridge, "_streams_for_channel", _fake)
    state = _FakeState(("fast", "slow"))

    first = bridge._handle_stream_request(state, "movie", "tt1234567")
    assert first["streams"] == [{"url": "fast-result"}]
    assert state.cache.get(("movie", "tt1234567", None, None, ("fast", "slow"))) is None

    assert finished.wait(3)
    for _ in range(50):  # the late callback runs just after the task returns
        if state.cache.get(("movie", "tt1234567", None, None, ("fast", "slow"))):
            break
        time.sleep(0.02)

    calls.clear()
    second = bridge._handle_stream_request(state, "movie", "tt1234567")
    assert calls == []  # served from cache, no new fan-out
    assert sorted(s["url"] for s in second["streams"]) == ["fast-result", "slow-result"]


# --- bridge.py: _bound_channel_io_timeout ------------------------------------


def test_bound_channel_io_timeout_sets_stream4me_httptools_default(monkeypatch):
    """Must scope the timeout to Stream4Me's OWN `core.httptools` default
    (used by `downloadpage()` -- Stream4Me's near-universal per-channel
    HTTP call convention) rather than `socket.setdefaulttimeout()`, which
    would leak into this server's own accepted connections and
    potentially into other Kodi addon subinterpreters."""
    import types

    fake_httptools = types.ModuleType("core.httptools")
    fake_httptools.HTTPTOOLS_DEFAULT_DOWNLOAD_TIMEOUT = 5
    fake_core = types.ModuleType("core")
    fake_core.httptools = fake_httptools
    monkeypatch.setitem(sys.modules, "core", fake_core)
    monkeypatch.setitem(sys.modules, "core.httptools", fake_httptools)

    bridge._bound_channel_io_timeout()

    assert fake_httptools.HTTPTOOLS_DEFAULT_DOWNLOAD_TIMEOUT == bridge._CHANNEL_IO_TIMEOUT_SECONDS


def test_bound_channel_io_timeout_swallows_missing_core(monkeypatch):
    monkeypatch.delitem(sys.modules, "core", raising=False)
    monkeypatch.delitem(sys.modules, "core.httptools", raising=False)

    bridge._bound_channel_io_timeout()  # must not raise even without Stream4Me installed


def test_bridge_module_does_not_import_socket():
    """Regression guard for the reverted `socket.setdefaulttimeout()`
    approach: bridge.py must not import `socket` at all."""
    assert not hasattr(bridge, "socket")


# --- is_servable / playback_headers / guard_video_check / KeyedLocks --------


@pytest.mark.parametrize("server, url, expected", [
    # mega is servable via bridge.py's deferred /play/<key> redirect --
    # see UNSERVABLE_SERVERS's own docstring for why it is no longer
    # excluded here.
    ("mega", "https://mega.nz/#!abc!key", True),
    ("torrent", "magnet:?xt=urn:btih:abc", True),
    ("torrent", "https://example.com/file.torrent", False),
    ("torrent", None, False),
    ("streamingcommunityws", "https://example.com/iframe/1", True),
])
def test_is_servable(server, url, expected):
    assert bh.is_servable(server, url) is expected


def test_playback_headers_default_to_browser_ua_and_page_referer():
    assert bh.playback_headers("voe", "https://page/1", "", {}, "Mozilla/5.0") == {
        "User-Agent": "Mozilla/5.0", "Referer": "https://page/1",
    }


def test_playback_headers_resolver_headers_win_outright():
    got = bh.playback_headers("voe", "https://page/1", "", {"Referer": "https://r"}, "Mozilla/5.0")
    assert got == {"Referer": "https://r"}


def test_playback_headers_directo_uses_item_referer():
    got = bh.playback_headers("directo", "https://cdn/v.mp4", "https://site/", {}, "UA")
    assert got == {"User-Agent": "UA", "Referer": "https://site/"}


def test_playback_headers_directo_without_referer_falls_back_to_page():
    got = bh.playback_headers("directo", "https://cdn/v.mp4", "", {}, "UA")
    assert got == {"User-Agent": "UA", "Referer": "https://cdn/v.mp4"}


def test_playback_headers_referer_false_sends_nothing():
    assert bh.playback_headers("voe", "https://page/1", False, {}, "UA") == {}


def test_playback_headers_without_user_agent_keeps_referer():
    assert bh.playback_headers("voe", "https://page/1", "", {}, None) == {"Referer": "https://page/1"}


def test_playback_headers_adaptive_ignores_referer_false():
    """StreamingCommunity sets referer=False, yet Stream4Me still hands its
    HLS to inputstream.adaptive with the browser headers -- and vixcloud
    403s without them."""
    got = bh.playback_headers("scws", "https://page/1", False, {}, "UA", adaptive=True)
    assert got == {"User-Agent": "UA", "Referer": "https://page/1"}


def test_playback_headers_adaptive_layers_resolver_headers_on_defaults():
    got = bh.playback_headers(
        "voe", "https://page/1", "", {"Referer": "https://r", "Cookie": "c"}, "UA", adaptive=True,
    )
    assert got == {"User-Agent": "UA", "Referer": "https://r", "Cookie": "c"}


@pytest.mark.parametrize("entry, manifest, expected", [
    (["hls [HD]", "u"], None, True),
    ([".mpd [Diretto]", "u"], None, True),
    (["mp4 [voe]", "u"], None, False),
    (["mp4 [voe]", "u"], "hls", True),
    (["x", "u", 0, "sub", "lic"], None, True),
    ([None, "u"], None, False),
])
def test_is_adaptive_entry(entry, manifest, expected):
    assert bh.is_adaptive_entry(entry, manifest) is expected


def test_guard_video_check_turns_a_raise_into_does_not_exist():
    def check(page_url):
        raise OSError(98, "Address already in use")

    exists, message = bh.guard_video_check(check)(page_url="x")
    assert exists is False
    assert "Address already in use" in message


def test_guard_video_check_passes_results_through_and_is_idempotent():
    guarded = bh.guard_video_check(lambda page_url: (True, ""))
    assert guarded(page_url="x") == (True, "")
    assert bh.guard_video_check(guarded) is guarded


def test_keyed_locks_one_lock_per_key():
    locks = bh.KeyedLocks()
    assert locks.lock_for("mega") is locks.lock_for("mega")
    assert locks.lock_for("mega") is not locks.lock_for("voe")


# --- bridge.py: _resolve_streams_for_server_item -----------------------------


class _ServerItem:
    def __init__(self, server, url, quality=None, referer=""):
        self.server = server
        self.url = url
        self.quality = quality
        self.referer = referer


def _install_fake_s4me(monkeypatch, video_urls, server="voe", check=None):
    """Fake just enough of Stream4Me's `core`/`servers` packages for
    `_resolve_streams_for_server_item()`; returns the recorded calls."""
    import types

    calls = []
    server_module = types.ModuleType("servers.%s" % server)
    if check is not None:
        server_module.test_video_exists = check
    servers_pkg = types.ModuleType("servers")
    setattr(servers_pkg, server, server_module)

    def resolve(server_name, url, muestra_dialogo=False):
        calls.append((server_name, url))
        if hasattr(server_module, "test_video_exists"):
            exists, _msg = server_module.test_video_exists(page_url=url)
            if not exists:
                return [], False, "gone"
        return video_urls, True, ""

    servertools = types.ModuleType("core.servertools")
    servertools.resolve_video_urls_for_playing = resolve
    httptools = types.ModuleType("core.httptools")
    httptools.default_headers = {"User-Agent": "Mozilla/5.0 Test"}
    core = types.ModuleType("core")
    core.servertools = servertools
    core.httptools = httptools
    for name, module in (
        ("core", core), ("core.servertools", servertools), ("core.httptools", httptools),
        ("servers", servers_pkg), ("servers.%s" % server, server_module),
    ):
        monkeypatch.setitem(sys.modules, name, module)
    return calls, server_module


def test_resolve_adds_stream4me_playback_headers(monkeypatch):
    """vixcloud/Pluto answer Kodi's own User-Agent with 403: every stream
    must carry the browser UA and page Referer Stream4Me would send."""
    _install_fake_s4me(monkeypatch, [["hls [HD]", "https://cdn/playlist"]])
    streams = bridge._resolve_streams_for_server_item("sc", _ServerItem("voe", "https://page/1"))
    assert streams == [{
        "name": "S4Me sc",
        "title": "hls [HD]",
        "url": "https://cdn/playlist",
        "behaviorHints": {
            "notWebReady": True,
            "proxyHeaders": {"request": {
                "User-Agent": "Mozilla/5.0 Test", "Referer": "https://page/1",
            }},
            "rivuletManifestType": "hls",
        },
    }]


def test_resolve_adds_subtitle_from_entry(monkeypatch):
    """A video_urls entry's 4th field, when an http(s) url, becomes a
    Stremio `subtitles[]` entry -- see bh.subtitle_url_from_entry()."""
    _install_fake_s4me(monkeypatch, [["mp4", "https://cdn/v.mp4", 0, "https://subs/it.srt"]])
    (stream,) = bridge._resolve_streams_for_server_item("ch", _ServerItem("voe", "https://page/1"))
    assert stream["subtitles"] == [{
        "id": "https://subs/it.srt", "url": "https://subs/it.srt", "lang": "ita",
    }]


def test_resolve_moves_resolver_pipe_headers_into_behavior_hints(monkeypatch):
    """A `url|Header=...` from the resolver must not reach Rivulet as-is:
    Rivulet appends proxyHeaders with its own `|`, which would yield
    `url|a|b`."""
    _install_fake_s4me(monkeypatch, [["mp4", "https://cdn/v.mp4|Referer=https%3A%2F%2Fr", 0, ""]])
    (stream,) = bridge._resolve_streams_for_server_item("ch", _ServerItem("voe", "https://page/1"))
    assert stream["url"] == "https://cdn/v.mp4"
    assert stream["behaviorHints"]["proxyHeaders"]["request"] == {"Referer": "https://r"}


def test_resolve_passes_the_raw_item_url_to_servertools(monkeypatch):
    calls, _module = _install_fake_s4me(monkeypatch, [["mp4", "https://cdn/v.mp4"]])
    bridge._resolve_streams_for_server_item("ch", _ServerItem("voe", "https://page/1|X=1"))
    assert calls == [("voe", "https://page/1|X=1")]


def test_mega_play_registry_sized_to_outlive_the_response_cache():
    """The module docstring above `_MEGA_PLAY_REGISTRY` requires a key to
    stay valid at least as long as `_BridgeState.cache` (same TTL) can
    still serve a `/stream` response referencing it. `bh.TTLCache`'s
    default `max_size=256` evicts the oldest key long before its TTL
    expires once more than 256 mega items are listed within that window
    -- a real risk with stream prefetch from the detail window. Must be
    large enough that a realistic listing burst never starves it."""
    assert bridge._MEGA_PLAY_REGISTRY._max_size >= 4096
    assert bridge._MEGA_PLAY_REGISTRY._ttl == bridge._CACHE_TTL_SECONDS


def test_resolve_defers_mega_to_play_time(monkeypatch):
    """A mega server item must NOT resolve at LIST time (see
    `bridge._shape_mega_stream()`'s docstring for why) -- it becomes a
    stream url pointing at this bridge's own `/play/<key>`, and the real
    Stream4Me resolve must never run for it here."""
    calls, _module = _install_fake_s4me(monkeypatch, [["mkv", "http://127.0.0.1:8059/f.mkv"]], server="mega")
    monkeypatch.setattr(bridge, "_BRIDGE_PORT", 39876)
    monkeypatch.setattr(bridge, "_MEGA_PLAY_REGISTRY", bh.TTLCache(1800))
    (stream,) = bridge._resolve_streams_for_server_item("hd4me", _ServerItem("mega", "https://mega.nz/#!a!b"))
    assert calls == []
    assert stream["url"].startswith("http://127.0.0.1:39876/play/")
    assert stream["title"] == "[mega]"
    assert stream["name"] == "S4Me hd4me"


def test_resolve_passes_magnets_through_and_drops_torrent_files(monkeypatch):
    calls, _module = _install_fake_s4me(monkeypatch, [], server="torrent")
    magnet = "magnet:?xt=urn:btih:abc"
    (stream,) = bridge._resolve_streams_for_server_item("1337x", _ServerItem("torrent", magnet))
    assert stream["url"] == magnet
    assert "behaviorHints" not in stream
    assert bridge._resolve_streams_for_server_item(
        "1337x", _ServerItem("torrent", "https://x/f.torrent"),
    ) == []
    assert calls == []


def test_resolve_guards_a_raising_video_check(monkeypatch):
    """A raising `test_video_exists()` must mean "no video", not let
    servertools serve the stale result a previous title left behind."""
    def check(page_url):
        raise OSError(98, "Address already in use")

    _calls, module = _install_fake_s4me(
        monkeypatch, [["mkv", "https://cdn/OTHER-TITLE.mkv"]], check=check,
    )
    assert bridge._resolve_streams_for_server_item("ch", _ServerItem("voe", "https://page/1")) == []
    assert module.test_video_exists._rivulet_guarded is True


def test_resolve_serializes_the_same_server(monkeypatch):
    """Two threads resolving one server must not overlap: server modules
    hand state from the check to the resolve through module globals."""
    active = []
    overlap = []

    def check(page_url):
        active.append(page_url)
        if len(active) > 1:
            overlap.append(page_url)
        time.sleep(0.05)
        active.remove(page_url)
        return True, ""

    _install_fake_s4me(monkeypatch, [["mp4", "https://cdn/v.mp4"]], check=check)
    monkeypatch.setattr(bridge, "_SERVER_LOCKS", bh.KeyedLocks())
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(
            lambda n: bridge._resolve_streams_for_server_item("ch", _ServerItem("voe", "https://p/%d" % n)),
            range(4),
        ))
    assert overlap == []


def test_handle_stream_request_budget_includes_the_title_lookup(monkeypatch):
    """The budget runs from the request's arrival: a slow TMDB lookup
    must shrink the fan-out's share, or the whole response can outlive
    Rivulet's own 15s AddonClient timeout."""
    def slow_lookup(imdb_id, search_type):
        time.sleep(0.2)
        return ("Title", "1999", "603")

    monkeypatch.setattr(bridge, "_resolve_title", slow_lookup)
    monkeypatch.setattr(bridge, "_REQUEST_BUDGET_SECONDS", 0.3)

    def _fake(channel_id, *a, **k):
        if channel_id == "slow":
            time.sleep(0.2)
        return [{"url": "%s-result" % channel_id}]

    monkeypatch.setattr(bridge, "_streams_for_channel", _fake)
    result = bridge._handle_stream_request(_FakeState(("fast", "slow")), "movie", "tt1234567")
    assert result["streams"] == [{"url": "fast-result"}]



# --- pick_episode -------------------------------------------------------


def test_pick_episode_matches_season_and_episode():
    import types
    ep1 = types.SimpleNamespace(infoLabels={"season": 1, "episode": 1})
    ep2 = types.SimpleNamespace(infoLabels={"season": 1, "episode": 2})
    assert bh.pick_episode([ep1, ep2], 1, 2) is ep2


def test_pick_episode_keeps_zero_season():
    import types
    ep = types.SimpleNamespace(infoLabels={"season": 0, "episode": 3})
    assert bh.pick_episode([ep], 0, 3) is ep


def test_pick_episode_no_match_returns_none():
    assert bh.pick_episode([], 1, 1) is None
    assert bh.pick_episode(None, 1, 1) is None


# --- match_cache_key ----------------------------------------------------


def test_match_cache_key_prefers_tmdb_id():
    assert bh.match_cache_key("ch", "movie", "603", "The Matrix", "1999") == ("ch", "movie", "603")


def test_match_cache_key_falls_back_to_normalized_title_year():
    assert bh.match_cache_key("ch", "movie", None, "The Matrix", "1999") == (
        "ch", "movie", ("the matrix", "1999"),
    )


def test_match_cache_key_stable_across_calls():
    """No season/episode argument exists at all -- see
    `_streams_for_channel()`'s docstring for why the key is independent
    of them on purpose."""
    assert bh.match_cache_key("ch", "series", "603", "T", "1999") == bh.match_cache_key(
        "ch", "series", "603", "T", "1999",
    )


# --- adaptive_manifest_type / subtitle_url_from_entry --------------------


@pytest.mark.parametrize("entry, manifest, expected", [
    (["hls [HD]", "u"], None, "hls"),
    ([".mpd [Diretto]", "u"], None, "mpd"),
    (["mp4 [voe]", "u"], "mpd", "mpd"),
    (["mp4 [voe]", "u"], "hls", "hls"),
    (["x", "u", 0, "sub", "lic"], None, "mpd"),
])
def test_adaptive_manifest_type(entry, manifest, expected):
    assert bh.adaptive_manifest_type(entry, manifest) == expected


@pytest.mark.parametrize("entry, expected", [
    (["hls", "u", 0, "https://subs/it.srt"], "https://subs/it.srt"),
    (["hls", "u", 0, "http://subs/it.srt"], "http://subs/it.srt"),
    (["hls", "u", 0, ""], None),
    (["hls", "u", 0, "not-a-url"], None),
    (["hls", "u"], None),
    ([], None),
    (None, None),
])
def test_subtitle_url_from_entry(entry, expected):
    assert bh.subtitle_url_from_entry(entry) == expected


# --- shape_stream: adaptive hint / subtitles ------------------------------


def test_shape_stream_adaptive_hint_sets_behavior_hint():
    stream = bh.shape_stream("ch", "hls", "https://cdn/pl.m3u8", adaptive_hint="hls")
    assert stream["behaviorHints"] == {"rivuletManifestType": "hls"}


def test_shape_stream_adaptive_hint_merges_with_headers():
    stream = bh.shape_stream(
        "ch", "hls", "https://cdn/pl.m3u8", headers={"Referer": "https://r"}, adaptive_hint="mpd",
    )
    assert stream["behaviorHints"] == {
        "notWebReady": True,
        "proxyHeaders": {"request": {"Referer": "https://r"}},
        "rivuletManifestType": "mpd",
    }


def test_shape_stream_subtitle_url_sets_subtitles():
    stream = bh.shape_stream("ch", "hls", "https://cdn/pl.m3u8", subtitle_url="https://subs/it.srt")
    assert stream["subtitles"] == [{
        "id": "https://subs/it.srt", "url": "https://subs/it.srt", "lang": "ita",
    }]


def test_shape_stream_no_subtitle_url_omits_subtitles_key():
    stream = bh.shape_stream("ch", "hls", "https://cdn/pl.m3u8")
    assert "subtitles" not in stream


# --- UNSERVABLE_SERVERS ----------------------------------------------------


def test_unservable_servers_is_empty():
    """mega used to be the sole member -- see its docstring for why it no
    longer is, now that bridge.py defers its resolve instead."""
    assert bh.UNSERVABLE_SERVERS == frozenset()


# --- generate_play_key ------------------------------------------------------


def test_generate_play_key_returns_distinct_urlsafe_strings():
    key1 = bh.generate_play_key()
    key2 = bh.generate_play_key()
    assert key1 != key2
    assert all(c.isalnum() or c in "-_" for c in key1)
    assert len(key1) >= 16


# --- ChannelBackoff -----------------------------------------------------


def test_channel_backoff_available_until_threshold_reached():
    backoff = bh.ChannelBackoff(failure_threshold=3, cooldown_seconds=100)
    backoff.record_failure("ch")
    backoff.record_failure("ch")
    assert backoff.is_available("ch") is True
    backoff.record_failure("ch")
    assert backoff.is_available("ch") is False


def test_channel_backoff_success_resets_failure_streak():
    backoff = bh.ChannelBackoff(failure_threshold=2, cooldown_seconds=100)
    backoff.record_failure("ch")
    backoff.record_success("ch")
    backoff.record_failure("ch")
    assert backoff.is_available("ch") is True  # streak reset, only one failure since


def test_channel_backoff_becomes_available_again_after_cooldown():
    now = [0.0]
    backoff = bh.ChannelBackoff(failure_threshold=1, cooldown_seconds=10, clock=lambda: now[0])
    backoff.record_failure("ch")
    assert backoff.is_available("ch") is False
    now[0] = 11.0
    assert backoff.is_available("ch") is True


def test_channel_backoff_unknown_channel_is_available():
    assert bh.ChannelBackoff().is_available("never-seen") is True


def test_channel_backoff_record_failure_reports_only_the_cooldown_start():
    backoff = bh.ChannelBackoff(failure_threshold=2, cooldown_seconds=100)
    assert backoff.record_failure("ch") is False
    assert backoff.record_failure("ch") is True  # this one tripped the cooldown
    assert backoff.record_failure("ch") is False  # already cooling down


def test_channel_backoff_is_thread_safe_under_concurrent_updates():
    backoff = bh.ChannelBackoff(failure_threshold=1000000, cooldown_seconds=100)
    errors = []

    def _hammer(_n):
        try:
            for _ in range(200):
                backoff.record_failure("ch")
                backoff.record_success("ch")
                backoff.is_available("ch")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(_hammer, range(8)))
    assert errors == []


# --- TTLCache: thread-safety ----------------------------------------------


def test_ttl_cache_concurrent_get_set_does_not_raise():
    cache = bh.TTLCache(0.01, max_size=8)
    errors = []

    def _hammer(n):
        try:
            for i in range(200):
                cache.set("k%d" % (i % 8), n)
                cache.get("k%d" % (i % 8))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(_hammer, range(8)))
    assert errors == []


# --- bridge.py: mega on-demand /play/<key> resolve -------------------------


def _install_fake_mega(monkeypatch, client_running=True, video_urls=None):
    import types
    if video_urls is None:
        video_urls = [["mkv", "http://127.0.0.1:8059/f.mkv"]]
    calls = []
    fake_client = types.SimpleNamespace(running=client_running)
    mega_module = types.ModuleType("servers.mega")

    def resolve(server_name, url, muestra_dialogo=False):
        calls.append((server_name, url))
        mega_module.c = fake_client
        return video_urls, True, ""

    servertools = types.ModuleType("core.servertools")
    servertools.resolve_video_urls_for_playing = resolve
    core = types.ModuleType("core")
    core.servertools = servertools
    servers_pkg = types.ModuleType("servers")
    servers_pkg.mega = mega_module
    for name, module in (
        ("core", core), ("core.servertools", servertools),
        ("servers", servers_pkg), ("servers.mega", mega_module),
    ):
        monkeypatch.setitem(sys.modules, name, module)
    return calls, fake_client


def test_resolve_mega_play_unknown_key_returns_none(monkeypatch):
    monkeypatch.setattr(bridge, "_MEGA_PLAY_REGISTRY", bh.TTLCache(1800))
    assert bridge._resolve_mega_play("nope") is None


def test_resolve_mega_play_resolves_and_reuses_while_client_running(monkeypatch):
    calls, _client = _install_fake_mega(monkeypatch, client_running=True)
    monkeypatch.setattr(bridge, "_MEGA_PLAY_REGISTRY", bh.TTLCache(1800))
    monkeypatch.setattr(bridge, "_MEGA_PLAY_SESSIONS", {})
    monkeypatch.setattr(bridge, "_SERVER_LOCKS", bh.KeyedLocks())
    key = bridge._register_mega_play("ch", "https://mega.nz/#!a!b")

    first = bridge._resolve_mega_play(key)
    second = bridge._resolve_mega_play(key)

    assert first == "http://127.0.0.1:8059/f.mkv"
    assert second == first
    assert calls == [("mega", "https://mega.nz/#!a!b")]  # resolved only once


def test_resolve_mega_play_resolves_again_once_client_stopped(monkeypatch):
    calls, client = _install_fake_mega(monkeypatch, client_running=True)
    monkeypatch.setattr(bridge, "_MEGA_PLAY_REGISTRY", bh.TTLCache(1800))
    monkeypatch.setattr(bridge, "_MEGA_PLAY_SESSIONS", {})
    monkeypatch.setattr(bridge, "_SERVER_LOCKS", bh.KeyedLocks())
    key = bridge._register_mega_play("ch", "https://mega.nz/#!a!b")

    bridge._resolve_mega_play(key)
    client.running = False
    bridge._resolve_mega_play(key)

    assert len(calls) == 2  # the first proxy died, a fresh one was started


def test_resolve_mega_play_failed_resolve_returns_none(monkeypatch):
    import types
    servertools = types.ModuleType("core.servertools")
    servertools.resolve_video_urls_for_playing = lambda *a, **k: ([], False, "gone")
    core = types.ModuleType("core")
    core.servertools = servertools
    monkeypatch.setitem(sys.modules, "core", core)
    monkeypatch.setitem(sys.modules, "core.servertools", servertools)
    monkeypatch.setattr(bridge, "_MEGA_PLAY_REGISTRY", bh.TTLCache(1800))
    monkeypatch.setattr(bridge, "_MEGA_PLAY_SESSIONS", {})
    monkeypatch.setattr(bridge, "_SERVER_LOCKS", bh.KeyedLocks())
    key = bridge._register_mega_play("ch", "https://mega.nz/#!a!b")
    assert bridge._resolve_mega_play(key) is None


# --- bridge.py: HTTP routes (/shutdown, /play/<key>) -----------------------
#
# `tests/conftest.py`'s autouse `_block_real_network` fixture patches
# `socket.socket.connect`/`socket.create_connection` process-wide (even
# loopback), so these drive the real `Handler` class through a
# `socket.socketpair()` (which is a pre-connected pair -- no `connect()`
# call at all, hence unaffected by that guard) instead of a real
# `http.client.HTTPConnection`. Constructing a `BaseHTTPRequestHandler`
# subclass runs `setup()`/`handle()`/`finish()` synchronously in `__init__`,
# so no server thread is needed either.


def _run_handler_request(handler_cls, request_bytes):
    server_sock, client_sock = socket.socketpair()
    try:
        client_sock.sendall(request_bytes)
        handler_cls(server_sock, ("127.0.0.1", 0), object())
        response = http.client.HTTPResponse(client_sock)
        response.begin()
        body = response.read()
        return response.status, dict(response.getheaders()), body
    finally:
        server_sock.close()
        client_sock.close()


def test_get_shutdown_returns_405_and_does_not_set_event(monkeypatch):
    """A GET must never be able to trigger a shutdown -- see
    Handler.do_GET()'s own comment for the <img src=...> CSRF-adjacent
    concern this guards against."""
    event = threading.Event()
    monkeypatch.setattr(bridge, "_SHUTDOWN_EVENT", event)
    handler_cls = bridge._make_handler(object())

    status, _headers, _body = _run_handler_request(handler_cls, b"GET /shutdown HTTP/1.0\r\n\r\n")

    assert status == 405
    assert not event.is_set()


def test_post_shutdown_without_header_returns_403_and_does_not_set_event(monkeypatch):
    """A same-path cross-origin form/img POST (the CSRF vector -- see
    SHUTDOWN_HEADER's own docstring) carries no custom header and must
    never be able to trigger a shutdown."""
    event = threading.Event()
    monkeypatch.setattr(bridge, "_SHUTDOWN_EVENT", event)
    handler_cls = bridge._make_handler(object())

    status, _headers, body = _run_handler_request(
        handler_cls, b"POST /shutdown HTTP/1.0\r\nContent-Length: 0\r\n\r\n",
    )

    assert status == 403
    assert json.loads(body) == {"error": "forbidden"}
    assert not event.is_set()


def test_post_shutdown_with_header_returns_ok_and_sets_event(monkeypatch):
    event = threading.Event()
    monkeypatch.setattr(bridge, "_SHUTDOWN_EVENT", event)
    handler_cls = bridge._make_handler(object())

    status, _headers, body = _run_handler_request(
        handler_cls,
        b"POST /shutdown HTTP/1.0\r\n"
        + ("%s: 1\r\n" % bridge.SHUTDOWN_HEADER).encode("ascii")
        + b"Content-Length: 0\r\n\r\n",
    )

    assert status == 200
    assert json.loads(body) == {"ok": True}
    assert event.is_set()


def test_get_play_unknown_key_returns_404(monkeypatch):
    monkeypatch.setattr(bridge, "_MEGA_PLAY_REGISTRY", bh.TTLCache(1800))
    handler_cls = bridge._make_handler(object())

    status, _headers, _body = _run_handler_request(handler_cls, b"GET /play/unknown-key HTTP/1.0\r\n\r\n")

    assert status == 404


def test_get_play_known_key_redirects_to_megaserver_proxy(monkeypatch):
    _install_fake_mega(monkeypatch, client_running=True)
    monkeypatch.setattr(bridge, "_MEGA_PLAY_REGISTRY", bh.TTLCache(1800))
    monkeypatch.setattr(bridge, "_MEGA_PLAY_SESSIONS", {})
    monkeypatch.setattr(bridge, "_SERVER_LOCKS", bh.KeyedLocks())
    key = bridge._register_mega_play("ch", "https://mega.nz/#!a!b")
    handler_cls = bridge._make_handler(object())

    status, headers, _body = _run_handler_request(
        handler_cls, ("GET /play/%s HTTP/1.0\r\n\r\n" % key).encode(),
    )

    assert status == 302
    assert headers["Location"] == "http://127.0.0.1:8059/f.mkv"


# --- bridge.py: _shutdown_cleanup -------------------------------------------


class _FakeHTTPServer:
    def __init__(self):
        self.calls = []

    def shutdown(self):
        self.calls.append("shutdown")

    def server_close(self):
        self.calls.append("server_close")


def test_shutdown_cleanup_stops_server_and_closes_stream4me_db(monkeypatch):
    """Mirrors Stream4Me's own service.py/platformcode/launcher.py
    db.close() call sites ("db need to be closed when not used, it will
    cause freezes") -- see _shutdown_cleanup()'s own docstring."""
    import types
    closed = []
    fake_core = types.ModuleType("core")
    fake_core.db = types.SimpleNamespace(close=lambda: closed.append(True))
    monkeypatch.setitem(sys.modules, "core", fake_core)

    server = _FakeHTTPServer()
    bridge._shutdown_cleanup(server)

    assert server.calls == ["shutdown", "server_close"]
    assert closed == [True]


def test_shutdown_cleanup_swallows_db_close_failure(monkeypatch):
    import types

    def _raise():
        raise RuntimeError("boom")

    fake_core = types.ModuleType("core")
    fake_core.db = types.SimpleNamespace(close=_raise)
    monkeypatch.setitem(sys.modules, "core", fake_core)

    bridge._shutdown_cleanup(_FakeHTTPServer())  # must not raise


def test_shutdown_cleanup_swallows_missing_core(monkeypatch):
    monkeypatch.delitem(sys.modules, "core", raising=False)
    bridge._shutdown_cleanup(_FakeHTTPServer())  # must not raise


def test_shutdown_cleanup_swallows_server_shutdown_failure(monkeypatch):
    class _BrokenServer:
        def shutdown(self):
            raise OSError("already gone")

        def server_close(self):
            pass

    monkeypatch.delitem(sys.modules, "core", raising=False)
    bridge._shutdown_cleanup(_BrokenServer())  # must not raise


# --- bridge.py: bind-failure handling ---------------------------------------


def test_main_exits_cleanly_on_port_bind_failure(monkeypatch):
    """A stale bridge (or a second Kodi instance) still holding the port
    must exit via sys.exit(1), logged clearly -- never a raw traceback
    that looks like a bug in this bridge itself."""
    import types

    monkeypatch.setattr(sys, "argv", ["bridge.py", "12345"])

    fake_xbmc = types.ModuleType("xbmc")
    fake_xbmc.Monitor = lambda: types.SimpleNamespace(
        abortRequested=lambda: True, waitForAbort=lambda t: True,
    )
    fake_xbmc.log = lambda *a, **k: None
    fake_xbmc.LOGERROR = 4
    fake_xbmc.LOGDEBUG = 0
    fake_xbmc.LOGINFO = 1

    class _FakeAddon:
        def __init__(self, addon_id):
            pass

        def getAddonInfo(self, key):
            return "/s4me/root"

        def getSetting(self, key):
            return ""

    fake_xbmcaddon = types.ModuleType("xbmcaddon")
    fake_xbmcaddon.Addon = _FakeAddon

    monkeypatch.setitem(sys.modules, "xbmc", fake_xbmc)
    monkeypatch.setitem(sys.modules, "xbmcaddon", fake_xbmcaddon)
    monkeypatch.delitem(sys.modules, "xbmcvfs", raising=False)
    monkeypatch.setitem(sys.modules, "core", types.ModuleType("core"))
    monkeypatch.setattr(bridge, "_install_s4me_path", lambda root: None)
    monkeypatch.setattr(bridge, "_bound_channel_io_timeout", lambda: None)
    monkeypatch.setattr(bridge, "_log_s4me_version", lambda root: None)

    def _raise_bind(address, handler):
        raise OSError(98, "Address already in use")

    monkeypatch.setattr(bridge, "ThreadingHTTPServer", _raise_bind)

    with pytest.raises(SystemExit) as exc_info:
        bridge.main()
    assert exc_info.value.code == 1


# --- bridge.py: _log_s4me_version -------------------------------------------


def test_log_s4me_version_reads_addon_info_and_commit_file(monkeypatch, tmp_path):
    import types
    (tmp_path / "last_commit.txt").write_text("abc123\n")
    fake_addon = types.SimpleNamespace(getAddonInfo=lambda key: "9.9.9")
    fake_xbmcaddon = types.ModuleType("xbmcaddon")
    fake_xbmcaddon.Addon = lambda addon_id: fake_addon
    monkeypatch.setitem(sys.modules, "xbmcaddon", fake_xbmcaddon)
    logged = []
    monkeypatch.setattr(bridge, "_log", lambda message, **k: logged.append(message))

    bridge._log_s4me_version(str(tmp_path))

    assert any("9.9.9" in m and "abc123" in m for m in logged)


def test_log_s4me_version_missing_commit_file_logs_unknown(monkeypatch, tmp_path):
    import types
    fake_addon = types.SimpleNamespace(getAddonInfo=lambda key: "9.9.9")
    fake_xbmcaddon = types.ModuleType("xbmcaddon")
    fake_xbmcaddon.Addon = lambda addon_id: fake_addon
    monkeypatch.setitem(sys.modules, "xbmcaddon", fake_xbmcaddon)
    logged = []
    monkeypatch.setattr(bridge, "_log", lambda message, **k: logged.append(message))

    bridge._log_s4me_version(str(tmp_path))

    assert any("commit unknown" in m for m in logged)


def test_log_s4me_version_swallows_addon_info_failure(monkeypatch, tmp_path):
    monkeypatch.delitem(sys.modules, "xbmcaddon", raising=False)
    logged = []
    monkeypatch.setattr(bridge, "_log", lambda message, **k: logged.append(message))

    bridge._log_s4me_version(str(tmp_path))  # must not raise

    assert any("unknown" in m for m in logged)


# --- bridge.py: per-channel failure backoff integration --------------------


def test_channel_task_records_success_and_failure(monkeypatch):
    backoff = bh.ChannelBackoff(failure_threshold=2, cooldown_seconds=100)
    monkeypatch.setattr(bridge, "_CHANNEL_BACKOFF", backoff)

    monkeypatch.setattr(bridge, "_streams_for_channel", lambda *a, **k: [{"url": "ok"}])
    assert bridge._channel_task("ch", "tt1", "T", "2000", "1", None, None, "movie") == [{"url": "ok"}]
    assert backoff.is_available("ch") is True

    def _boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(bridge, "_streams_for_channel", _boom)
    with pytest.raises(RuntimeError):
        bridge._channel_task("ch", "tt1", "T", "2000", "1", None, None, "movie")
    assert backoff.is_available("ch") is True  # one failure, threshold is 2
    with pytest.raises(RuntimeError):
        bridge._channel_task("ch", "tt1", "T", "2000", "1", None, None, "movie")
    assert backoff.is_available("ch") is False  # threshold reached


def test_handle_stream_request_skips_channels_in_cooldown(monkeypatch):
    """A channel whose failure streak already tripped its cooldown must
    not even be submitted to the pool -- `_streams_for_channel` must
    never run for it."""
    backoff = bh.ChannelBackoff(failure_threshold=1, cooldown_seconds=100)
    backoff.record_failure("down")
    monkeypatch.setattr(bridge, "_CHANNEL_BACKOFF", backoff)
    monkeypatch.setattr(bridge, "_resolve_title", lambda imdb_id, search_type: ("Title", "1999", "603"))

    calls = []

    def _fake(channel_id, *a, **k):
        calls.append(channel_id)
        return [{"url": "u-%s" % channel_id}]

    monkeypatch.setattr(bridge, "_streams_for_channel", _fake)
    state = _FakeState(("down", "up"))

    result = bridge._handle_stream_request(state, "movie", "tt1234567")

    assert calls == ["up"]
    assert result["streams"] == [{"url": "u-up"}]


def test_channel_task_logs_the_cooldown_start_exactly_once(monkeypatch):
    backoff = bh.ChannelBackoff(failure_threshold=2, cooldown_seconds=600)
    monkeypatch.setattr(bridge, "_CHANNEL_BACKOFF", backoff)
    monkeypatch.setattr(bridge, "_CHANNEL_FAILURE_THRESHOLD", 2)
    monkeypatch.setattr(bridge, "_CHANNEL_COOLDOWN_SECONDS", 600)
    logged = []
    monkeypatch.setattr(bridge, "_log", lambda message, **k: logged.append(message))

    def _boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(bridge, "_streams_for_channel", _boom)
    for _ in range(3):
        with pytest.raises(RuntimeError):
            bridge._channel_task("ch", "tt1", "T", "2000", "1", None, None, "movie")

    assert logged == ["channel ch failed 2 times in a row, skipping it for 10 minutes"]


def test_failing_channel_is_logged_once_with_its_real_cause(monkeypatch):
    """`_search_channel()` logs the real exception; the pipeline's own
    follow-up `RuntimeError('... raised')` must not be logged a second
    time by the fan-out's error hook (one line per failing channel per
    request, not two)."""
    monkeypatch.setattr(bridge, "_CHANNEL_BACKOFF", bh.ChannelBackoff())
    monkeypatch.setattr(bridge, "_MATCH_CACHE", bh.TTLCache(1800))
    monkeypatch.setattr(bridge, "_resolve_title", lambda imdb_id, search_type: ("Title", "1999", "603"))
    logged = []
    monkeypatch.setattr(bridge, "_log", lambda message, **k: logged.append(message))

    def _search(item, title):
        raise AttributeError("type object 'HTTPResponse' has no attribute 'cookies'")

    _install_fake_channel_module(monkeypatch, "broken", search_fn=_search)
    result = bridge._handle_stream_request(_FakeState(("broken",)), "movie", "tt1234567")

    assert result == {"streams": []}
    assert len(logged) == 1
    assert logged[0].startswith("channel broken search failed: AttributeError(")


def test_unexpected_channel_exception_is_still_logged_by_the_fan_out(monkeypatch):
    """Only the already-logged stage failures are silenced: anything else
    escaping a channel's pipeline keeps its `raised` line."""
    monkeypatch.setattr(bridge, "_CHANNEL_BACKOFF", bh.ChannelBackoff())
    monkeypatch.setattr(bridge, "_resolve_title", lambda imdb_id, search_type: ("Title", "1999", "603"))
    logged = []
    monkeypatch.setattr(bridge, "_log", lambda message, **k: logged.append(message))

    def _boom(*a, **k):
        raise ValueError("unexpected")

    monkeypatch.setattr(bridge, "_streams_for_channel", _boom)
    bridge._handle_stream_request(_FakeState(("odd",)), "movie", "tt1234567")

    assert logged == ["channel odd raised: ValueError('unexpected')"]




# --- bridge.py: _streams_for_channel show/movie match caching --------------


def _install_fake_channel_module(monkeypatch, channel_id, search_fn=None, episodios_fn=None, findvideos_fn=None):
    import types
    module = types.ModuleType("channels.%s" % channel_id)
    if search_fn is not None:
        module.search = search_fn
    if episodios_fn is not None:
        module.episodios = episodios_fn
    if findvideos_fn is not None:
        module.findvideos = findvideos_fn
    channels_pkg = types.ModuleType("channels")
    setattr(channels_pkg, channel_id, module)
    monkeypatch.setitem(sys.modules, "channels", channels_pkg)
    monkeypatch.setitem(sys.modules, "channels.%s" % channel_id, module)

    fake_item_module = types.ModuleType("core.item")

    class _Item:
        def __init__(self, **kwargs):
            self.infoLabels = {}
            for k, v in kwargs.items():
                setattr(self, k, v)

    fake_item_module.Item = _Item
    core_pkg = types.ModuleType("core")
    core_pkg.item = fake_item_module
    monkeypatch.setitem(sys.modules, "core", core_pkg)
    monkeypatch.setitem(sys.modules, "core.item", fake_item_module)
    return module


class _MatchItem:
    def __init__(self, info_labels):
        self.infoLabels = info_labels


class _EpisodeItem:
    def __init__(self, season, episode):
        self.infoLabels = {"season": season, "episode": episode}


def test_streams_for_channel_caches_show_match_and_episodes(monkeypatch):
    """S1E2 after S1E1 must skip BOTH search() and episodios() -- see
    `bridge._MATCH_CACHE`/`bh.match_cache_key()`."""
    monkeypatch.setattr(bridge, "_MATCH_CACHE", bh.TTLCache(1800))
    show = _MatchItem({"tmdb_id": "603", "title": "The Matrix", "year": "1999"})
    search_calls = []
    episodios_calls = []

    def _search(item, title):
        search_calls.append(title)
        return [show]

    def _episodios(show_item):
        episodios_calls.append(show_item)
        return [_EpisodeItem(1, 1), _EpisodeItem(1, 2)]

    _install_fake_channel_module(
        monkeypatch, "matchcache", search_fn=_search, episodios_fn=_episodios, findvideos_fn=lambda item: [],
    )

    bridge._streams_for_channel("matchcache", "tt1", "The Matrix", "1999", "603", 1, 1, "series")
    bridge._streams_for_channel("matchcache", "tt1", "The Matrix", "1999", "603", 1, 2, "series")

    assert search_calls == ["The Matrix"]
    assert len(episodios_calls) == 1


def test_streams_for_channel_caches_movie_match(monkeypatch):
    monkeypatch.setattr(bridge, "_MATCH_CACHE", bh.TTLCache(1800))
    movie = _MatchItem({"tmdb_id": "603", "title": "The Matrix", "year": "1999"})
    search_calls = []

    def _search(item, title):
        search_calls.append(title)
        return [movie]

    _install_fake_channel_module(monkeypatch, "moviecache", search_fn=_search, findvideos_fn=lambda item: [])

    bridge._streams_for_channel("moviecache", "tt1", "The Matrix", "1999", "603", None, None, "movie")
    bridge._streams_for_channel("moviecache", "tt1", "The Matrix", "1999", "603", None, None, "movie")

    assert search_calls == ["The Matrix"]


def test_streams_for_channel_raises_on_search_failure_for_backoff(monkeypatch):
    """A raised search() must surface (not swallow-and-return-[]) so
    `_channel_task()` can count it against `bh.ChannelBackoff`."""
    monkeypatch.setattr(bridge, "_MATCH_CACHE", bh.TTLCache(1800))

    def _search(item, title):
        raise ValueError("dead host")

    _install_fake_channel_module(monkeypatch, "brokench", search_fn=_search)

    with pytest.raises(RuntimeError):
        bridge._streams_for_channel("brokench", "tt1", "The Matrix", "1999", "603", None, None, "movie")


def test_streams_for_channel_no_match_returns_empty_without_raising(monkeypatch):
    """A legitimate "nothing matched here" must never be mistaken for a
    channel failure."""
    monkeypatch.setattr(bridge, "_MATCH_CACHE", bh.TTLCache(1800))

    def _search(item, title):
        return []

    _install_fake_channel_module(monkeypatch, "nomatchch", search_fn=_search)

    assert bridge._streams_for_channel(
        "nomatchch", "tt1", "The Matrix", "1999", "603", None, None, "movie",
    ) == []
