"""Protocol/persistence tests for lib.store.Store.

Reference: DEFAULT_ADDONS should mirror stremio-core's OFFICIAL_ADDONS baseline
(Cinemeta + OpenSubtitles v3), src/types/addon/descriptor.rs DescriptorFlags
shape ({"official":bool,"protected":bool}).
"""
import json
import os
import tempfile
import time

import pytest

import lib.store as store_module
from lib.store import DEFAULT_ADDONS, ConcurrentUpdateError, Store


def make_store(tmp_path):
    return Store(str(tmp_path / "addon_data"))


# --- construction ------------------------------------------------------


def test_store_creates_data_dir(tmp_path):
    data_dir = tmp_path / "addon_data"
    assert not data_dir.exists()
    Store(str(data_dir))
    assert data_dir.exists()


# --- DEFAULT_ADDONS shape --------------------------------------------------


def test_default_addons_has_at_least_two_entries():
    assert len(DEFAULT_ADDONS) >= 2


def test_default_addons_entries_have_required_fields():
    for descriptor in DEFAULT_ADDONS:
        assert isinstance(descriptor.get("transportUrl"), str) and descriptor["transportUrl"]
        assert isinstance(descriptor.get("manifest"), dict) and descriptor["manifest"]


def test_default_addons_are_protected():
    for descriptor in DEFAULT_ADDONS:
        assert descriptor.get("flags", {}).get("protected") is True


def test_default_addons_include_cinemeta():
    urls = [d["transportUrl"] for d in DEFAULT_ADDONS]
    assert any("cinemeta" in u for u in urls)


# --- get_addons seeding --------------------------------------------------


def test_get_addons_seeds_defaults_on_first_call(tmp_path):
    store = make_store(tmp_path)
    addons = store.get_addons()
    assert len(addons) == len(DEFAULT_ADDONS)
    urls = {a["transportUrl"] for a in addons}
    assert urls == {d["transportUrl"] for d in DEFAULT_ADDONS}


def test_get_addons_persists_seed_to_disk(tmp_path):
    data_dir = tmp_path / "addon_data"
    store = Store(str(data_dir))
    store.get_addons()
    addons_file = data_dir / "addons.json"
    assert addons_file.exists()
    on_disk = json.loads(addons_file.read_text())
    assert len(on_disk) == len(DEFAULT_ADDONS)


# --- set_addons / install_addon / remove_addon -----------------------------


def test_set_addons_round_trip(tmp_path):
    store = make_store(tmp_path)
    custom = [
        {
            "transportUrl": "https://custom.example/manifest.json",
            "manifest": {"id": "org.custom", "name": "Custom"},
            "flags": {},
        }
    ]
    store.set_addons(custom)
    assert store.get_addons() == custom


def test_install_addon_appends_new_descriptor(tmp_path):
    store = make_store(tmp_path)
    before = len(store.get_addons())
    manifest = {"id": "org.custom", "name": "Custom Addon"}
    store.install_addon("https://custom.example/manifest.json", manifest)
    addons = store.get_addons()
    assert len(addons) == before + 1
    installed = next(a for a in addons if a["transportUrl"] == "https://custom.example/manifest.json")
    assert installed["manifest"] == manifest
    assert installed.get("flags", {}) == {}


def test_install_addon_upserts_existing_transport_url(tmp_path):
    store = make_store(tmp_path)
    url = "https://custom.example/manifest.json"
    store.install_addon(url, {"id": "org.custom", "name": "V1"})
    before = len(store.get_addons())
    store.install_addon(url, {"id": "org.custom", "name": "V2"})
    addons = store.get_addons()
    assert len(addons) == before
    matches = [a for a in addons if a["transportUrl"] == url]
    assert len(matches) == 1
    assert matches[0]["manifest"]["name"] == "V2"


def test_remove_addon_deletes_unprotected_entry(tmp_path):
    store = make_store(tmp_path)
    url = "https://custom.example/manifest.json"
    store.install_addon(url, {"id": "org.custom", "name": "Custom"})
    before = len(store.get_addons())
    store.remove_addon(url)
    addons = store.get_addons()
    assert len(addons) == before - 1
    assert all(a["transportUrl"] != url for a in addons)


def test_remove_addon_refuses_protected_addon(tmp_path):
    store = make_store(tmp_path)
    protected_url = DEFAULT_ADDONS[0]["transportUrl"]
    store.get_addons()  # seed
    with pytest.raises(ValueError):
        store.remove_addon(protected_url)
    # still present after the refused removal
    assert any(a["transportUrl"] == protected_url for a in store.get_addons())


def test_remove_addon_nonexistent_url_does_not_raise_valueerror_for_protection(tmp_path):
    store = make_store(tmp_path)
    store.get_addons()
    before = len(store.get_addons())
    store.remove_addon("https://does-not-exist.example/manifest.json")
    assert len(store.get_addons()) == before


# --- get_enabled_addons / set_addon_disabled --------------------------------


def test_set_addon_disabled_excludes_from_enabled_but_not_from_all(tmp_path):
    store = make_store(tmp_path)
    url = "https://custom.example/manifest.json"
    store.install_addon(url, {"id": "org.custom", "name": "Custom"})

    store.set_addon_disabled(url, True)

    assert url not in {a["transportUrl"] for a in store.get_enabled_addons()}
    assert url in {a["transportUrl"] for a in store.get_addons()}


def test_set_addon_disabled_false_removes_disabled_key_entirely(tmp_path):
    store = make_store(tmp_path)
    url = "https://custom.example/manifest.json"
    store.install_addon(url, {"id": "org.custom", "name": "Custom"})
    store.set_addon_disabled(url, True)

    store.set_addon_disabled(url, False)

    descriptor = next(a for a in store.get_addons() if a["transportUrl"] == url)
    assert "disabled" not in descriptor["flags"]
    assert url in {a["transportUrl"] for a in store.get_enabled_addons()}


def test_set_addon_disabled_allows_protected_addon(tmp_path):
    store = make_store(tmp_path)
    protected_url = DEFAULT_ADDONS[0]["transportUrl"]
    store.get_addons()  # seed

    store.set_addon_disabled(protected_url, True)

    descriptor = next(a for a in store.get_addons() if a["transportUrl"] == protected_url)
    assert descriptor["flags"]["disabled"] is True
    assert descriptor["flags"]["protected"] is True
    assert protected_url not in {a["transportUrl"] for a in store.get_enabled_addons()}


def test_set_addon_disabled_nonexistent_url_is_noop(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    store.get_addons()  # seed

    write_calls = []
    real_atomic_write = store_module._atomic_write

    def spy_atomic_write(path, data, compact=False):
        write_calls.append(path)
        return real_atomic_write(path, data, compact=compact)

    monkeypatch.setattr(store_module, "_atomic_write", spy_atomic_write)

    before = store.get_addons()
    store.set_addon_disabled("https://does-not-exist.example/manifest.json", True)

    assert write_calls == []
    assert store.get_addons() == before


def test_set_addon_disabled_goes_through_update_addons(tmp_path, monkeypatch):
    """The write must go through the compare-and-swap path (a fresh read
    immediately before persisting), not a plain get_addons()+set_addons()
    that could lose a concurrent writer's update."""
    store = make_store(tmp_path)
    url = "https://custom.example/manifest.json"
    store.install_addon(url, {"id": "org.custom", "name": "Custom"})

    real_read_raw = store_module._read_raw
    calls = []

    def spy_read_raw(path):
        calls.append(path)
        return real_read_raw(path)

    monkeypatch.setattr(store_module, "_read_raw", spy_read_raw)

    store.set_addon_disabled(url, True)

    monkeypatch.undo()  # stop faking reads before verifying the real on-disk result
    assert len(calls) == 2  # baseline read + pre-write conflict check
    with open(store._addons_path) as f:
        on_disk = json.load(f)
    descriptor = next(a for a in on_disk if a["transportUrl"] == url)
    assert descriptor["flags"]["disabled"] is True



# --- move_addon --------------------------------------------------------


def test_move_addon_up_swaps_with_previous(tmp_path):
    store = make_store(tmp_path)
    store.install_addon("https://a.example/manifest.json", {"id": "a"})
    store.install_addon("https://b.example/manifest.json", {"id": "b"})

    store.move_addon("https://b.example/manifest.json", -1)

    urls = [a["transportUrl"] for a in store.get_addons()]
    assert urls[-2:] == ["https://b.example/manifest.json", "https://a.example/manifest.json"]


def test_move_addon_down_swaps_with_next(tmp_path):
    store = make_store(tmp_path)
    store.install_addon("https://a.example/manifest.json", {"id": "a"})
    store.install_addon("https://b.example/manifest.json", {"id": "b"})

    store.move_addon("https://a.example/manifest.json", 1)

    urls = [a["transportUrl"] for a in store.get_addons()]
    assert urls[-2:] == ["https://b.example/manifest.json", "https://a.example/manifest.json"]


def test_move_addon_delta_beyond_start_clamps_to_first_position(tmp_path):
    store = make_store(tmp_path)
    store.install_addon("https://a.example/manifest.json", {"id": "a"})
    store.install_addon("https://b.example/manifest.json", {"id": "b"})
    total = len(store.get_addons())

    store.move_addon("https://b.example/manifest.json", -total)

    urls = [a["transportUrl"] for a in store.get_addons()]
    assert urls[0] == "https://b.example/manifest.json"


def test_move_addon_delta_beyond_end_clamps_to_last_position(tmp_path):
    store = make_store(tmp_path)
    store.install_addon("https://a.example/manifest.json", {"id": "a"})
    total = len(store.get_addons())

    store.move_addon(DEFAULT_ADDONS[0]["transportUrl"], total)

    urls = [a["transportUrl"] for a in store.get_addons()]
    assert urls[-1] == DEFAULT_ADDONS[0]["transportUrl"]


def test_move_addon_up_at_first_position_is_a_noop_and_skips_write(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    addons = store.get_addons()  # seed
    first_url = addons[0]["transportUrl"]

    write_calls = []
    real_atomic_write = store_module._atomic_write

    def spy_atomic_write(path, data, compact=False):
        write_calls.append(path)
        return real_atomic_write(path, data, compact=compact)

    monkeypatch.setattr(store_module, "_atomic_write", spy_atomic_write)

    store.move_addon(first_url, -1)

    assert write_calls == []
    assert store.get_addons() == addons


def test_move_addon_down_at_last_position_is_a_noop_and_skips_write(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    addons = store.get_addons()  # seed
    last_url = addons[-1]["transportUrl"]

    write_calls = []
    real_atomic_write = store_module._atomic_write

    def spy_atomic_write(path, data, compact=False):
        write_calls.append(path)
        return real_atomic_write(path, data, compact=compact)

    monkeypatch.setattr(store_module, "_atomic_write", spy_atomic_write)

    store.move_addon(last_url, 1)

    assert write_calls == []
    assert store.get_addons() == addons


def test_move_addon_unknown_transport_url_raises_valueerror(tmp_path):
    store = make_store(tmp_path)
    store.get_addons()  # seed
    with pytest.raises(ValueError):
        store.move_addon("https://does-not-exist.example/manifest.json", -1)


def test_move_addon_preserves_disabled_and_protected_flags(tmp_path):
    store = make_store(tmp_path)
    url = "https://custom.example/manifest.json"
    store.install_addon(url, {"id": "org.custom", "name": "Custom"})
    store.set_addon_disabled(url, True)
    protected_url = DEFAULT_ADDONS[0]["transportUrl"]

    store.move_addon(url, -1)

    moved = next(a for a in store.get_addons() if a["transportUrl"] == url)
    assert moved["flags"]["disabled"] is True
    protected = next(a for a in store.get_addons() if a["transportUrl"] == protected_url)
    assert protected["flags"]["protected"] is True


def test_move_addon_goes_through_update_addons(tmp_path, monkeypatch):
    """The write must go through the compare-and-swap path (a fresh read
    immediately before persisting), not a plain get_addons()+set_addons()
    that could lose a concurrent writer's update."""
    store = make_store(tmp_path)
    store.install_addon("https://a.example/manifest.json", {"id": "a"})
    store.install_addon("https://b.example/manifest.json", {"id": "b"})

    real_read_raw = store_module._read_raw
    calls = []

    def spy_read_raw(path):
        calls.append(path)
        return real_read_raw(path)

    monkeypatch.setattr(store_module, "_read_raw", spy_read_raw)

    store.move_addon("https://b.example/manifest.json", -1)

    monkeypatch.undo()  # stop faking reads before verifying the real on-disk result
    assert len(calls) == 2  # baseline read + pre-write conflict check
    urls = [a["transportUrl"] for a in store.get_addons()]
    assert urls[-2:] == ["https://b.example/manifest.json", "https://a.example/manifest.json"]


def test_move_addon_retries_and_reapplies_against_fresh_data_on_conflict(tmp_path, monkeypatch):
    """A second `default.py` process installs a new addon and writes
    addons.json in the gap between our baseline read and our pre-write
    conflict check. The move must detect this, retry against the fresh
    content, and persist BOTH changes -- never discard the other
    process's write, and still apply our reorder against its result.
    """
    store = make_store(tmp_path)
    store.install_addon("https://a.example/manifest.json", {"id": "a"})
    store.install_addon("https://b.example/manifest.json", {"id": "b"})
    baseline_raw = store_module._read_raw(store._addons_path)

    concurrent_addons = json.loads(baseline_raw)
    concurrent_addons.append(
        {
            "transportUrl": "https://other-process.example/manifest.json",
            "manifest": {"id": "org.other"},
            "flags": {},
        }
    )
    concurrent_raw = json.dumps(concurrent_addons, indent=2)

    calls = []

    def fake_read_raw(path):
        calls.append(path)
        return baseline_raw if len(calls) == 1 else concurrent_raw

    monkeypatch.setattr(store_module, "_read_raw", fake_read_raw)

    store.move_addon("https://b.example/manifest.json", -1)

    monkeypatch.undo()  # stop faking reads before verifying the real on-disk result
    assert len(calls) == 4  # 2 reads/attempt x 2 attempts: one retry happened
    urls = [a["transportUrl"] for a in store.get_addons()]
    assert "https://other-process.example/manifest.json" in urls, (
        "the concurrent process's install must survive our retried move"
    )
    assert urls.index("https://b.example/manifest.json") < urls.index("https://a.example/manifest.json")


# --- auth ------------------------------------------------------------------


def test_get_auth_none_when_never_set(tmp_path):
    store = make_store(tmp_path)
    assert store.get_auth() is None


def test_set_and_get_auth_round_trip(tmp_path):
    store = make_store(tmp_path)
    auth = {"authKey": "tok123", "user": {"email": "a@b.com"}}
    store.set_auth(auth)
    assert store.get_auth() == auth


def test_set_auth_none_clears(tmp_path):
    store = make_store(tmp_path)
    store.set_auth({"authKey": "tok123", "user": {"email": "a@b.com"}})
    store.set_auth(None)
    assert store.get_auth() is None


def test_auth_persists_across_store_instances(tmp_path):
    data_dir = tmp_path / "addon_data"
    auth = {"authKey": "tok123", "user": {"email": "a@b.com"}}
    Store(str(data_dir)).set_auth(auth)
    reopened = Store(str(data_dir))
    assert reopened.get_auth() == auth


# --- corruption recovery ----------------------------------------------------


def test_corrupt_addons_json_falls_back_to_defaults(tmp_path):
    data_dir = tmp_path / "addon_data"
    data_dir.mkdir(parents=True)
    (data_dir / "addons.json").write_text("{not valid json at all")
    store = Store(str(data_dir))
    addons = store.get_addons()  # must not raise
    assert len(addons) == len(DEFAULT_ADDONS)


def test_corrupt_addons_json_self_heals_on_disk(tmp_path):
    data_dir = tmp_path / "addon_data"
    data_dir.mkdir(parents=True)
    (data_dir / "addons.json").write_text("{not valid json at all")
    store = Store(str(data_dir))
    store.get_addons()
    # file on disk is now valid JSON with the defaults
    on_disk = json.loads((data_dir / "addons.json").read_text())
    assert len(on_disk) == len(DEFAULT_ADDONS)


def test_corrupt_auth_json_returns_none_without_raising(tmp_path):
    data_dir = tmp_path / "addon_data"
    data_dir.mkdir(parents=True)
    (data_dir / "auth.json").write_text("{not valid json at all")
    store = Store(str(data_dir))
    assert store.get_auth() is None  # must not raise


# --- update_addons / optimistic-concurrency (lost-update protection) -------


def test_update_addons_accepts_arbitrary_transform_and_persists_result(tmp_path):
    store = make_store(tmp_path)
    store.get_addons()  # seed defaults to disk

    def add_two(addons):
        return addons + [
            {"transportUrl": "https://a.example/manifest.json", "manifest": {"id": "a"}, "flags": {}},
            {"transportUrl": "https://b.example/manifest.json", "manifest": {"id": "b"}, "flags": {}},
        ]

    result = store.update_addons(add_two)
    assert len(result) == len(DEFAULT_ADDONS) + 2
    assert store.get_addons() == result


def test_update_addons_missing_file_fallback_transform_cannot_corrupt_defaults(tmp_path):
    """The missing/corrupt fallback in update_addons must hand the transform
    a deep copy: mutating a fallback entry's nested manifest/flags in place
    (rather than replacing it) must never leak into DEFAULT_ADDONS itself.
    """
    store = make_store(tmp_path)
    # No addons.json on disk yet -- update_addons must fall back internally.

    def mutate_nested_in_place(addons):
        addons[0]["manifest"]["id"] = "corrupted"
        addons[0]["flags"]["protected"] = False
        return addons

    store.update_addons(mutate_nested_in_place)

    assert DEFAULT_ADDONS[0]["manifest"]["id"] != "corrupted"
    assert DEFAULT_ADDONS[0]["flags"]["protected"] is True


def test_update_addons_noop_transform_skips_write(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    store.get_addons()  # seed defaults to disk

    write_calls = []
    real_atomic_write = store_module._atomic_write

    def spy_atomic_write(path, data):
        write_calls.append(path)
        return real_atomic_write(path, data)

    monkeypatch.setattr(store_module, "_atomic_write", spy_atomic_write)

    result = store.update_addons(lambda addons: addons)

    assert write_calls == []
    assert result == store.get_addons()


def test_update_addons_uncontended_path_reads_file_exactly_twice(tmp_path, monkeypatch):
    """Normal (single-process) writes must not pay any retry overhead:
    exactly one baseline read and one pre-write conflict check, no more.
    """
    store = make_store(tmp_path)
    store.get_addons()  # seed defaults to disk

    real_read_raw = store_module._read_raw
    calls = []

    def spy_read_raw(path):
        calls.append(path)
        return real_read_raw(path)

    monkeypatch.setattr(store_module, "_read_raw", spy_read_raw)

    store.install_addon("https://solo.example/manifest.json", {"id": "org.solo"})

    assert len(calls) == 2
    assert store.get_addons()[-1]["transportUrl"] == "https://solo.example/manifest.json"


def test_install_addon_retries_on_detected_concurrent_write(tmp_path, monkeypatch):
    """A second `default.py` process installs a *different* addon and
    writes addons.json in the gap between our baseline read and our
    pre-write conflict check. The update must detect this, retry the
    whole read+merge against the fresh content, and persist BOTH
    changes -- never silently discard the other process's write.
    """
    store = make_store(tmp_path)
    store.get_addons()  # seed defaults to disk
    original_raw = store_module._read_raw(store._addons_path)

    concurrent_addons = json.loads(original_raw)
    concurrent_addons.append(
        {
            "transportUrl": "https://other-process.example/manifest.json",
            "manifest": {"id": "org.other", "name": "OtherProcess"},
            "flags": {},
        }
    )
    concurrent_raw = json.dumps(concurrent_addons, indent=2)

    calls = []

    def fake_read_raw(path):
        calls.append(path)
        return original_raw if len(calls) == 1 else concurrent_raw

    monkeypatch.setattr(store_module, "_read_raw", fake_read_raw)

    store.install_addon("https://mine.example/manifest.json", {"id": "org.mine", "name": "Mine"})

    monkeypatch.undo()  # stop faking reads before verifying the real on-disk result
    assert len(calls) == 4  # 2 reads/attempt x 2 attempts: one retry happened
    urls = {a["transportUrl"] for a in store.get_addons()}
    assert "https://other-process.example/manifest.json" in urls, (
        "the concurrent process's write must survive the retry, not be lost"
    )
    assert "https://mine.example/manifest.json" in urls


def test_remove_addon_retries_and_reapplies_against_fresh_data_on_conflict(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    url_to_remove = "https://mine.example/manifest.json"
    store.install_addon(url_to_remove, {"id": "org.mine", "name": "Mine"})
    baseline_raw = store_module._read_raw(store._addons_path)

    concurrent_addons = json.loads(baseline_raw)
    concurrent_addons.append(
        {
            "transportUrl": "https://other-process.example/manifest.json",
            "manifest": {"id": "org.other"},
            "flags": {},
        }
    )
    concurrent_raw = json.dumps(concurrent_addons, indent=2)

    calls = []

    def fake_read_raw(path):
        calls.append(path)
        return baseline_raw if len(calls) == 1 else concurrent_raw

    monkeypatch.setattr(store_module, "_read_raw", fake_read_raw)

    store.remove_addon(url_to_remove)

    monkeypatch.undo()  # stop faking reads before verifying the real on-disk result
    assert len(calls) == 4
    urls = {a["transportUrl"] for a in store.get_addons()}
    assert url_to_remove not in urls
    assert "https://other-process.example/manifest.json" in urls, (
        "the concurrent process's install must survive our retried removal"
    )


def test_remove_addon_protected_refusal_does_not_retry(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    protected_url = DEFAULT_ADDONS[0]["transportUrl"]
    store.get_addons()  # seed

    real_read_raw = store_module._read_raw
    calls = []

    def spy_read_raw(path):
        calls.append(path)
        return real_read_raw(path)

    monkeypatch.setattr(store_module, "_read_raw", spy_read_raw)

    with pytest.raises(ValueError):
        store.remove_addon(protected_url)

    assert len(calls) == 1  # raised before the pre-write conflict check; never retried


def test_update_addons_raises_concurrenterror_after_exhausting_retries(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    store.get_addons()  # seed

    calls = []

    def always_changing_read_raw(path):
        calls.append(path)
        return "simulated-concurrent-content-%d" % len(calls)

    monkeypatch.setattr(store_module, "_read_raw", always_changing_read_raw)

    with pytest.raises(ConcurrentUpdateError, match="attempt"):
        store.install_addon("https://mine.example/manifest.json", {"id": "org.mine"})

    assert len(calls) == 6  # 3 attempts x 2 reads, then it gives up


def test_update_addons_respects_custom_max_attempts(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    store.get_addons()  # seed

    calls = []

    def always_changing_read_raw(path):
        calls.append(path)
        return "simulated-concurrent-content-%d" % len(calls)

    monkeypatch.setattr(store_module, "_read_raw", always_changing_read_raw)

    def _add_x(addons):
        return addons + [
            {"transportUrl": "https://x.example/manifest.json", "manifest": {"id": "x"}, "flags": {}}
        ]

    with pytest.raises(ConcurrentUpdateError):
        store.update_addons(_add_x, max_attempts=1)

    assert len(calls) == 2  # a single attempt: baseline + conflict check, then give up


def test_remove_addon_on_fresh_store_still_seeds_defaults_to_disk(tmp_path):
    """A no-op removal (URL never installed) on a brand new store must
    still seed+persist DEFAULT_ADDONS, matching the pre-existing behavior
    where this was an unavoidable side effect of the internal
    get_addons() call.
    """
    data_dir = tmp_path / "addon_data"
    store = Store(str(data_dir))
    addons_file = data_dir / "addons.json"
    assert not addons_file.exists()

    store.remove_addon("https://does-not-exist.example/manifest.json")

    assert addons_file.exists()
    on_disk = json.loads(addons_file.read_text())
    assert len(on_disk) == len(DEFAULT_ADDONS)


# --- search history ---------------------------------------------------------


def test_get_search_history_empty_when_never_set(tmp_path):
    store = make_store(tmp_path)
    assert store.get_search_history() == []


def test_add_search_query_persists_most_recent_first(tmp_path):
    store = make_store(tmp_path)
    store.add_search_query("matrix")
    store.add_search_query("inception")
    assert store.get_search_history() == ["inception", "matrix"]


def test_add_search_query_dedupes_case_insensitively_and_moves_to_front(tmp_path):
    store = make_store(tmp_path)
    store.add_search_query("Matrix")
    store.add_search_query("inception")
    store.add_search_query("matrix")
    assert store.get_search_history() == ["matrix", "inception"]


def test_add_search_query_blank_or_whitespace_is_a_noop(tmp_path):
    store = make_store(tmp_path)
    store.add_search_query("")
    store.add_search_query("   ")
    store.add_search_query(None)
    assert store.get_search_history() == []


def test_add_search_query_strips_whitespace(tmp_path):
    store = make_store(tmp_path)
    store.add_search_query("  matrix  ")
    assert store.get_search_history() == ["matrix"]


def test_add_search_query_caps_at_max_search_history(tmp_path):
    from lib.store import MAX_SEARCH_HISTORY
    store = make_store(tmp_path)
    for i in range(MAX_SEARCH_HISTORY + 5):
        store.add_search_query("query-%d" % i)
    history = store.get_search_history()
    assert len(history) == MAX_SEARCH_HISTORY
    assert history[0] == "query-%d" % (MAX_SEARCH_HISTORY + 4)


def test_clear_search_history_removes_all_entries(tmp_path):
    store = make_store(tmp_path)
    store.add_search_query("matrix")
    store.clear_search_history()
    assert store.get_search_history() == []


def test_clear_search_history_on_empty_store_does_not_raise(tmp_path):
    store = make_store(tmp_path)
    store.clear_search_history()  # must not raise


def test_search_history_persists_across_store_instances(tmp_path):
    data_dir = tmp_path / "addon_data"
    store = Store(str(data_dir))
    store.add_search_query("matrix")

    reopened = Store(str(data_dir))
    assert reopened.get_search_history() == ["matrix"]


def test_corrupt_search_history_json_returns_empty_list_without_raising(tmp_path):
    data_dir = tmp_path / "addon_data"
    data_dir.mkdir()
    (data_dir / "search_history.json").write_text("{not valid json")
    store = Store(str(data_dir))
    assert store.get_search_history() == []  # must not raise


# --- now playing (LibrarySync) ----------------------------------------------


def test_get_now_playing_none_when_never_set(tmp_path):
    store = make_store(tmp_path)
    assert store.get_now_playing() is None


def test_set_and_get_now_playing_round_trip(tmp_path):
    store = make_store(tmp_path)
    context = {
        "type": "movie", "id": "tt1", "video_id": None,
        "name": "A Movie", "poster": "https://example.com/p.jpg",
        "started_at": "2020-01-01T00:00:00Z",
    }
    store.set_now_playing(context)
    assert store.get_now_playing() == context


def test_set_now_playing_none_clears(tmp_path):
    store = make_store(tmp_path)
    store.set_now_playing({"type": "movie", "id": "tt1", "video_id": None,
                            "name": "A", "poster": None, "started_at": "x"})
    store.set_now_playing(None)
    assert store.get_now_playing() is None


def test_now_playing_persists_across_store_instances(tmp_path):
    data_dir = tmp_path / "addon_data"
    context = {"type": "series", "id": "tt2", "video_id": "tt2:1:1",
               "name": "A Show", "poster": None, "started_at": "2020-01-01T00:00:00Z"}
    Store(str(data_dir)).set_now_playing(context)
    assert Store(str(data_dir)).get_now_playing() == context


def test_corrupt_now_playing_json_returns_none_without_raising(tmp_path):
    data_dir = tmp_path / "addon_data"
    data_dir.mkdir()
    (data_dir / "now_playing.json").write_text("{not valid json")
    store = Store(str(data_dir))
    assert store.get_now_playing() is None  # must not raise


# --- resume offset (LibrarySync) --------------------------------------------


def test_get_resume_offset_ms_none_when_never_set(tmp_path):
    store = make_store(tmp_path)
    assert store.get_resume_offset_ms() is None


def test_set_and_get_resume_offset_ms_round_trip(tmp_path):
    store = make_store(tmp_path)
    store.set_resume_offset_ms(42000)
    assert store.get_resume_offset_ms() == 42000


def test_set_resume_offset_ms_none_clears(tmp_path):
    store = make_store(tmp_path)
    store.set_resume_offset_ms(42000)
    store.set_resume_offset_ms(None)
    assert store.get_resume_offset_ms() is None


def test_resume_offset_ms_persists_across_store_instances(tmp_path):
    data_dir = tmp_path / "addon_data"
    Store(str(data_dir)).set_resume_offset_ms(9000)
    assert Store(str(data_dir)).get_resume_offset_ms() == 9000


# --- last seen version (homewindow update notification) --------------------


def test_get_last_seen_version_none_when_never_set(tmp_path):
    store = make_store(tmp_path)
    assert store.get_last_seen_version() is None


def test_set_and_get_last_seen_version_round_trip(tmp_path):
    store = make_store(tmp_path)
    store.set_last_seen_version("1.2.3")
    assert store.get_last_seen_version() == "1.2.3"


def test_last_seen_version_persists_across_store_instances(tmp_path):
    data_dir = tmp_path / "addon_data"
    Store(str(data_dir)).set_last_seen_version("1.2.3")
    assert Store(str(data_dir)).get_last_seen_version() == "1.2.3"


def test_corrupt_last_version_json_returns_none_without_raising(tmp_path):
    data_dir = tmp_path / "addon_data"
    data_dir.mkdir()
    (data_dir / "last_version.json").write_text("{not valid json")
    store = Store(str(data_dir))
    assert store.get_last_seen_version() is None  # must not raise


# --- local progress cache (LibrarySync) -------------------------------------


def test_get_progress_none_when_never_set(tmp_path):
    store = make_store(tmp_path)
    assert store.get_progress("movie", "tt1") is None


def test_set_and_get_progress_round_trip(tmp_path):
    store = make_store(tmp_path)
    store.set_progress("movie", "tt1", None, 5000, 90000, "2020-01-01T00:00:00Z")
    assert store.get_progress("movie", "tt1") == {
        "position_ms": 5000, "duration_ms": 90000, "updated_at": "2020-01-01T00:00:00Z",
    }


def test_progress_keys_distinguish_by_video_id(tmp_path):
    """Two different episodes of the SAME series id must never collide
    -- (type, id) alone is not a unique key for a series."""
    store = make_store(tmp_path)
    store.set_progress("series", "tt9", "tt9:1:1", 1000, 2000, "2020-01-01T00:00:00Z")
    store.set_progress("series", "tt9", "tt9:1:2", 3000, 4000, "2020-01-01T00:00:01Z")
    assert store.get_progress("series", "tt9", "tt9:1:1") == {
        "position_ms": 1000, "duration_ms": 2000, "updated_at": "2020-01-01T00:00:00Z",
    }
    assert store.get_progress("series", "tt9", "tt9:1:2") == {
        "position_ms": 3000, "duration_ms": 4000, "updated_at": "2020-01-01T00:00:01Z",
    }


def test_progress_key_with_no_video_id_distinct_from_with_video_id(tmp_path):
    store = make_store(tmp_path)
    store.set_progress("movie", "tt1", None, 100, 200, "2020-01-01T00:00:00Z")
    store.set_progress("series", "tt1", "tt1:1:1", 300, 400, "2020-01-01T00:00:01Z")
    assert store.get_progress("movie", "tt1", None) == {
        "position_ms": 100, "duration_ms": 200, "updated_at": "2020-01-01T00:00:00Z",
    }
    assert store.get_progress("series", "tt1", "tt1:1:1") == {
        "position_ms": 300, "duration_ms": 400, "updated_at": "2020-01-01T00:00:01Z",
    }


def test_set_progress_overwrites_existing_entry(tmp_path):
    store = make_store(tmp_path)
    store.set_progress("movie", "tt1", None, 100, 200, "t1")
    store.set_progress("movie", "tt1", None, 150, 200, "t2")
    assert store.get_progress("movie", "tt1") == {
        "position_ms": 150, "duration_ms": 200, "updated_at": "t2",
    }


def test_progress_persists_across_store_instances(tmp_path):
    data_dir = tmp_path / "addon_data"
    Store(str(data_dir)).set_progress("movie", "tt1", None, 100, 200, "t1")
    assert Store(str(data_dir)).get_progress("movie", "tt1") == {
        "position_ms": 100, "duration_ms": 200, "updated_at": "t1",
    }


def test_corrupt_progress_json_returns_none_without_raising(tmp_path):
    data_dir = tmp_path / "addon_data"
    data_dir.mkdir()
    (data_dir / "progress.json").write_text("{not valid json")
    store = Store(str(data_dir))
    assert store.get_progress("movie", "tt1") is None  # must not raise


# --- progress.json bounds (age/count/malformed pruning) --------------------


def test_set_progress_prunes_non_dict_entry(tmp_path):
    data_dir = tmp_path / "addon_data"
    data_dir.mkdir()
    (data_dir / "progress.json").write_text(json.dumps({"movie\x1ftt-old\x1f": "not-a-dict"}))
    store = Store(str(data_dir))
    store.set_progress("movie", "tt1", None, 100, 200, "2020-01-01T00:00:00Z")
    raw = json.loads((data_dir / "progress.json").read_text())
    assert list(raw.keys()) == [store._progress_key("movie", "tt1")]


def test_set_progress_prunes_entry_with_malformed_updated_at(tmp_path):
    data_dir = tmp_path / "addon_data"
    data_dir.mkdir()
    (data_dir / "progress.json").write_text(json.dumps({
        "movie\x1ftt-old\x1f": {"position_ms": 1, "duration_ms": 2, "updated_at": "not-a-timestamp"},
    }))
    store = Store(str(data_dir))
    store.set_progress("movie", "tt1", None, 100, 200, "2020-01-01T00:00:00Z")
    raw = json.loads((data_dir / "progress.json").read_text())
    assert list(raw.keys()) == [store._progress_key("movie", "tt1")]


def test_set_progress_prunes_entry_older_than_max_age(tmp_path):
    store = make_store(tmp_path)
    store.set_progress("movie", "tt-old", None, 1, 2, "2020-01-01T00:00:00Z")
    # The full age sweep only runs once a day (see
    # `PROGRESS_SWEEP_INTERVAL_SECONDS`) or when over cap; force it here
    # so this second call's sweep isn't skipped by the daily marker.
    store._last_progress_sweep_monotonic = None
    store.set_progress("movie", "tt-new", None, 3, 4, "2021-06-01T00:00:00Z")  # >180d later
    assert store.get_progress("movie", "tt-old") is None
    assert store.get_progress("movie", "tt-new") == {
        "position_ms": 3, "duration_ms": 4, "updated_at": "2021-06-01T00:00:00Z",
    }


def test_set_progress_keeps_entry_within_max_age(tmp_path):
    store = make_store(tmp_path)
    store.set_progress("movie", "tt-recent", None, 1, 2, "2020-01-01T00:00:00Z")
    store.set_progress("movie", "tt-new", None, 3, 4, "2020-06-01T00:00:00Z")  # <180d later
    assert store.get_progress("movie", "tt-recent") == {
        "position_ms": 1, "duration_ms": 2, "updated_at": "2020-01-01T00:00:00Z",
    }


def test_set_progress_evicts_oldest_entries_over_max_count(tmp_path, monkeypatch):
    monkeypatch.setattr(store_module, "MAX_PROGRESS_ENTRIES", 3)
    store = make_store(tmp_path)
    store.set_progress("movie", "tt1", None, 1, 2, "2020-01-01T00:00:01Z")
    store.set_progress("movie", "tt2", None, 1, 2, "2020-01-01T00:00:02Z")
    store.set_progress("movie", "tt3", None, 1, 2, "2020-01-01T00:00:03Z")
    store.set_progress("movie", "tt4", None, 1, 2, "2020-01-01T00:00:04Z")
    assert store.get_progress("movie", "tt1") is None  # oldest evicted
    for content_id in ("tt2", "tt3", "tt4"):
        assert store.get_progress("movie", content_id) is not None
    raw = json.loads((tmp_path / "addon_data" / "progress.json").read_text())
    assert len(raw) == 3


def test_set_progress_retains_just_written_entry_even_when_it_sorts_oldest(tmp_path, monkeypatch):
    """The entry `set_progress` just wrote must never be the one evicted
    by the count cap, even if its own timestamp is older than every
    other entry -- otherwise the sample a caller just persisted could
    vanish immediately."""
    monkeypatch.setattr(store_module, "MAX_PROGRESS_ENTRIES", 3)
    store = make_store(tmp_path)
    store.set_progress("movie", "tt1", None, 1, 2, "2024-01-01T00:00:01Z")
    store.set_progress("movie", "tt2", None, 1, 2, "2024-01-01T00:00:02Z")
    store.set_progress("movie", "tt3", None, 1, 2, "2024-01-01T00:00:03Z")
    store.set_progress("movie", "tt-ancient", None, 9, 9, "2000-01-01T00:00:00Z")
    assert store.get_progress("movie", "tt-ancient") == {
        "position_ms": 9, "duration_ms": 9, "updated_at": "2000-01-01T00:00:00Z",
    }
    assert store.get_progress("movie", "tt1") is None  # oldest of the REST evicted instead
    raw = json.loads((tmp_path / "addon_data" / "progress.json").read_text())
    assert len(raw) == 3


# --- get_progress_entries() (lib.ui.mystuff feed) --------------------------


def test_get_progress_entries_empty_store_returns_empty_list(tmp_path):
    store = make_store(tmp_path)
    assert store.get_progress_entries() == []


def test_get_progress_entries_round_trip(tmp_path):
    store = make_store(tmp_path)
    store.set_progress("series", "tt9", "tt9:1:1", 1000, 2000, "2020-01-01T00:00:00Z")
    assert store.get_progress_entries() == [{
        "type": "series", "id": "tt9", "video_id": "tt9:1:1",
        "position_ms": 1000, "duration_ms": 2000, "updated_at": "2020-01-01T00:00:00Z",
    }]


def test_get_progress_entries_movie_has_none_video_id(tmp_path):
    store = make_store(tmp_path)
    store.set_progress("movie", "tt1", None, 100, 200, "2020-01-01T00:00:00Z")
    entries = store.get_progress_entries()
    assert len(entries) == 1
    assert entries[0]["video_id"] is None
    assert entries[0]["type"] == "movie" and entries[0]["id"] == "tt1"


def test_get_progress_entries_skips_key_with_wrong_part_count(tmp_path):
    data_dir = tmp_path / "addon_data"
    data_dir.mkdir()
    (data_dir / "progress.json").write_text(json.dumps({
        "movie\x1ftt1": {"position_ms": 1, "duration_ms": 2, "updated_at": "2020-01-01T00:00:00Z"},
    }))
    store = Store(str(data_dir))
    assert store.get_progress_entries() == []


def test_get_progress_entries_skips_malformed_value(tmp_path):
    data_dir = tmp_path / "addon_data"
    data_dir.mkdir()
    (data_dir / "progress.json").write_text(json.dumps({
        "movie\x1ftt-not-a-dict\x1f": "not-a-dict",
        "movie\x1ftt-no-duration\x1f": {"position_ms": 1, "updated_at": "2020-01-01T00:00:00Z"},
        "movie\x1ftt1\x1f": {"position_ms": 5, "duration_ms": 10, "updated_at": "2020-01-01T00:00:00Z"},
    }))
    store = Store(str(data_dir))
    entries = store.get_progress_entries()
    assert len(entries) == 1
    assert entries[0]["id"] == "tt1"


# --- _prune_progress sweep gating / _atomic_write internals ----------------


def test_set_progress_over_cap_evicts_even_when_daily_sweep_not_due(tmp_path, monkeypatch):
    """Cap eviction must fire purely from the entry count exceeding
    MAX_PROGRESS_ENTRIES, even right after a sweep -- when the once-a-day
    marker alone would skip the next one -- and the entry `set_progress`
    just wrote must always survive it."""
    monkeypatch.setattr(store_module, "MAX_PROGRESS_ENTRIES", 3)
    store = make_store(tmp_path)
    store.set_progress("movie", "tt1", None, 1, 2, "2020-01-01T00:00:01Z")
    store._last_progress_sweep_monotonic = time.monotonic()  # sweep "just happened"
    store.set_progress("movie", "tt2", None, 1, 2, "2020-01-01T00:00:02Z")
    store.set_progress("movie", "tt3", None, 1, 2, "2020-01-01T00:00:03Z")
    store.set_progress("movie", "tt4", None, 1, 2, "2020-01-01T00:00:04Z")
    assert store.get_progress("movie", "tt1") is None  # oldest evicted despite recent sweep
    for content_id in ("tt2", "tt3", "tt4"):
        assert store.get_progress("movie", content_id) is not None
    raw = json.loads((tmp_path / "addon_data" / "progress.json").read_text())
    assert len(raw) == 3


def test_set_progress_skips_timestamp_parsing_when_under_cap_and_sweep_recent(tmp_path, monkeypatch):
    """Under the entry cap with a recent sweep, `set_progress` must not
    parse any `updated_at` timestamp at all -- that per-entry
    `datetime.strptime` sweep is the whole point of the fix (measured
    1.535ms @500 entries, 62% of the call) and must only pay for itself
    when actually needed."""
    store = make_store(tmp_path)
    store.set_progress("movie", "tt1", None, 1, 2, "2020-01-01T00:00:01Z")  # first call always sweeps

    calls = []
    real_parse = store_module._parse_progress_timestamp

    def counting_parse(value):
        calls.append(value)
        return real_parse(value)

    monkeypatch.setattr(store_module, "_parse_progress_timestamp", counting_parse)

    store.set_progress("movie", "tt2", None, 3, 4, "2020-01-01T00:00:02Z")

    assert calls == []
    assert store.get_progress("movie", "tt1") is not None
    assert store.get_progress("movie", "tt2") is not None


def test_atomic_write_matches_pure_python_json_dump_byte_for_byte(tmp_path):
    """`_atomic_write` now writes via `fh.write(json.dumps(...))` (the C
    one-shot encoder) instead of `json.dump(data, fh, ...)` (pure-Python
    `iterencode`) for a 4.35x speedup @500 entries -- output must stay
    byte-identical for both the pretty-printed and compact modes."""
    data = {"b": [1, 2, {"nested": True, "z": None, "unicode": "caf\u00e9"}], "a": "value", "n": 3.5}

    def reference_write(path, compact):
        fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=os.path.dirname(path) or ".")
        with os.fdopen(fd, "w") as fh:
            if compact:
                json.dump(data, fh, separators=(',', ':'))
            else:
                json.dump(data, fh, indent=2, sort_keys=False)
        os.replace(tmp, path)

    for compact in (False, True):
        expected_path = str(tmp_path / f"expected-{compact}.json")
        actual_path = str(tmp_path / f"actual-{compact}.json")
        reference_write(expected_path, compact)
        store_module._atomic_write(actual_path, data, compact=compact)
        with open(expected_path, "rb") as fh:
            expected_bytes = fh.read()
        with open(actual_path, "rb") as fh:
            actual_bytes = fh.read()
        assert actual_bytes == expected_bytes


# --- seen episodes (lib.newepisodes new-episode dismissal) -----------------


def test_get_seen_episodes_returns_empty_dict_when_never_set(tmp_path):
    store = make_store(tmp_path)
    assert store.get_seen_episodes() == {}


def test_set_and_get_seen_episodes_round_trip(tmp_path):
    store = make_store(tmp_path)
    seen = {"series\x1ftt1\x1ftt1:1:2": True}
    store.set_seen_episodes(seen)
    assert store.get_seen_episodes() == seen


def test_seen_episodes_persists_across_store_instances(tmp_path):
    data_dir = tmp_path / "addon_data"
    seen = {"series\x1ftt1\x1ftt1:1:1": True}
    Store(str(data_dir)).set_seen_episodes(seen)
    assert Store(str(data_dir)).get_seen_episodes() == seen


def test_corrupt_seen_episodes_json_returns_empty_dict_without_raising(tmp_path):
    data_dir = tmp_path / "addon_data"
    data_dir.mkdir()
    (data_dir / "seen_episodes.json").write_text("{not valid json")
    store = Store(str(data_dir))
    assert store.get_seen_episodes() == {}  # must not raise


def test_seen_episodes_cache_is_invalidated_after_write(tmp_path):
    """`get_seen_episodes()` is served from `_cached_read`'s per-instance
    memoisation; a `set_seen_episodes()` on the SAME instance must not
    leave a stale empty result cached from the read before it."""
    store = make_store(tmp_path)
    assert store.get_seen_episodes() == {}  # populates the read cache
    seen = {"series\x1ftt1\x1ftt1:1:3": True}
    store.set_seen_episodes(seen)
    assert store.get_seen_episodes() == seen
