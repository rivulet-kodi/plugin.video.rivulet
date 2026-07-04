"""Protocol/persistence tests for lib.store.Store.

Reference: DEFAULT_ADDONS should mirror stremio-core's OFFICIAL_ADDONS baseline
(Cinemeta + OpenSubtitles v3), src/types/addon/descriptor.rs DescriptorFlags
shape ({"official":bool,"protected":bool}).
"""
import json

import pytest

from lib.store import DEFAULT_ADDONS, Store


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
