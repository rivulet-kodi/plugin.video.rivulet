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
import sys
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


class _FakeState:
    def __init__(self, channels):
        self.cache = _FakeCache()
        self._channels = channels

    def channels_for_request(self):
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


def test_handle_stream_request_does_not_cache_on_timeout(monkeypatch):
    """A partial, timed-out result must not be cached: caching it would
    keep re-serving that same partial result -- missing whichever
    channel was still slow -- to every repeat request for the full
    30-minute cache TTL instead of giving a fresh fan-out another
    chance to complete in full."""
    monkeypatch.setattr(bridge, "_resolve_title", lambda imdb_id, search_type: ("Title", "1999", "603"))
    monkeypatch.setattr(bridge, "_REQUEST_BUDGET_SECONDS", 0.1)

    calls = []

    def _fake(channel_id, *a, **k):
        calls.append(channel_id)
        if channel_id == "slow":
            time.sleep(0.5)
        return [{"url": "%s-result" % channel_id}]

    monkeypatch.setattr(bridge, "_streams_for_channel", _fake)
    state = _FakeState(("fast", "slow"))

    first = bridge._handle_stream_request(state, "movie", "tt1234567")
    assert first["streams"] == [{"url": "fast-result"}]

    # A second, immediate request must NOT be a cache hit: both channels
    # are queried again, not just replayed from a cached partial result.
    calls.clear()
    second = bridge._handle_stream_request(state, "movie", "tt1234567")

    assert set(calls) == {"fast", "slow"}
    assert second["streams"] == [{"url": "fast-result"}]


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
