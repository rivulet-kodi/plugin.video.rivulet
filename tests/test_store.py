"""Protocol/persistence tests for lib.store.Store.

Reference: DEFAULT_ADDONS should mirror stremio-core's OFFICIAL_ADDONS baseline
(Cinemeta + OpenSubtitles v3), src/types/addon/descriptor.rs DescriptorFlags
shape ({"official":bool,"protected":bool}).
"""
import json

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
