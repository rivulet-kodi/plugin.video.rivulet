"""Tests for lib.stremio.addoncatalogs: the `addon_catalog` protocol
client and installed-addon-catalog aggregation.

Like lib.stremio.addons (see tests/test_addons.py), this module has zero
`xbmc*` imports, so it is imported directly here with no Kodi stubs at
all - if it ever grew one, that import would fail collection outright.
"""
import pytest
import requests

from lib.stremio.addoncatalogs import (
    STATE_INSTALLABLE,
    STATE_INSTALLED,
    STATE_NEEDS_CONFIGURATION,
    STATE_UPDATE_AVAILABLE,
    descriptor_state,
    fetch_addon_catalog,
    iter_addon_catalogs,
)
from lib.stremio.addons import AddonError
from tests.conftest import FakeResponse, FakeSession


def _addon(transport_url, manifest):
    return {"transportUrl": transport_url, "manifest": manifest, "flags": {}}


# ---------------------------------------------------------------------------
# iter_addon_catalogs
# ---------------------------------------------------------------------------


def test_iter_addon_catalogs_finds_a_non_cinemeta_addon():
    """Proves there is no Cinemeta special-casing: an unrelated addon
    publishing addonCatalogs is picked up exactly like any other."""
    addons = [
        _addon("https://other.example/manifest.json", {
            "id": "org.other",
            "addonCatalogs": [{"type": "movie", "id": "top", "name": "Top"}],
        }),
    ]
    results = list(iter_addon_catalogs(addons))
    assert len(results) == 1
    transport_url, manifest, addon_catalog = results[0]
    assert transport_url == "https://other.example/manifest.json"
    assert manifest["id"] == "org.other"
    assert addon_catalog["id"] == "top"


def test_iter_addon_catalogs_ignores_manifest_catalogs_field():
    """manifest['catalogs'] (meta catalogs) is a different field from
    manifest['addonCatalogs'] and must never be confused with it."""
    addons = [
        _addon("https://a.example/manifest.json", {
            "id": "org.a",
            "catalogs": [{"type": "movie", "id": "top"}],
        }),
    ]
    assert list(iter_addon_catalogs(addons)) == []


def test_iter_addon_catalogs_aggregates_across_multiple_addons():
    addons = [
        _addon("https://a.example/manifest.json", {"id": "a", "addonCatalogs": [{"type": "movie", "id": "x"}]}),
        _addon("https://b.example/manifest.json", {"id": "b", "addonCatalogs": [{"type": "series", "id": "y"}]}),
    ]
    results = list(iter_addon_catalogs(addons))
    assert len(results) == 2
    assert {r[0] for r in results} == {"https://a.example/manifest.json", "https://b.example/manifest.json"}


def test_iter_addon_catalogs_no_addon_catalogs_field_yields_nothing():
    addons = [_addon("https://a.example/manifest.json", {"id": "a"})]
    assert list(iter_addon_catalogs(addons)) == []


def test_iter_addon_catalogs_no_addons_yields_nothing():
    assert list(iter_addon_catalogs([])) == []
    assert list(iter_addon_catalogs(None)) == []


def test_iter_addon_catalogs_yields_every_descriptor_for_one_addon():
    addons = [
        _addon("https://a.example/manifest.json", {
            "id": "a",
            "addonCatalogs": [
                {"type": "movie", "id": "top"},
                {"type": "series", "id": "top"},
            ],
        }),
    ]
    assert len(list(iter_addon_catalogs(addons))) == 2


# ---------------------------------------------------------------------------
# fetch_addon_catalog
# ---------------------------------------------------------------------------


ENVELOPE = {
    "addons": [
        {"transportUrl": "https://new.example/manifest.json", "transportName": "New",
         "manifest": {"id": "new", "name": "New", "version": "1.0.0"}},
    ]
}


class _FakeClient:
    """Stand-in for AddonClient: exposes the `.session`/`.timeout`
    fetch_addon_catalog() reads, mirroring AddonClient._get_json()."""

    def __init__(self, session, timeout=15):
        self.session = session
        self.timeout = timeout


def test_fetch_addon_catalog_returns_addons_list_and_builds_correct_url():
    session = FakeSession(responses=[FakeResponse(ENVELOPE)])
    client = _FakeClient(session)
    result = fetch_addon_catalog(client, "https://a.example/manifest.json", "movie", "top")
    assert result == ENVELOPE["addons"]
    assert session.calls[0]["url"] == "https://a.example/addon_catalog/movie/top.json"


def test_fetch_addon_catalog_accepts_a_bare_session_without_a_client_wrapper():
    session = FakeSession(responses=[FakeResponse({"addons": []})])
    assert fetch_addon_catalog(session, "https://a.example/manifest.json", "movie", "top") == []


def test_fetch_addon_catalog_rejects_envelope_missing_addons_key():
    client = _FakeClient(FakeSession(responses=[FakeResponse({"nope": []})]))
    with pytest.raises(AddonError):
        fetch_addon_catalog(client, "https://a.example/manifest.json", "movie", "top")


def test_fetch_addon_catalog_rejects_envelope_where_addons_is_not_a_list():
    client = _FakeClient(FakeSession(responses=[FakeResponse({"addons": "nope"})]))
    with pytest.raises(AddonError):
        fetch_addon_catalog(client, "https://a.example/manifest.json", "movie", "top")


def test_fetch_addon_catalog_rejects_invalid_json():
    class _BadJsonResponse(FakeResponse):
        def json(self):
            raise ValueError("boom")

    client = _FakeClient(FakeSession(responses=[_BadJsonResponse({})]))
    with pytest.raises(AddonError):
        fetch_addon_catalog(client, "https://a.example/manifest.json", "movie", "top")


def test_fetch_addon_catalog_dead_endpoint_raises_addon_error():
    client = _FakeClient(FakeSession(exc=requests.exceptions.ConnectionError("boom")))
    with pytest.raises(AddonError):
        fetch_addon_catalog(client, "https://dead.example/manifest.json", "movie", "top")


def test_fetch_addon_catalog_http_error_raises_addon_error():
    client = _FakeClient(FakeSession(responses=[FakeResponse({}, status_code=500)]))
    with pytest.raises(AddonError):
        fetch_addon_catalog(client, "https://a.example/manifest.json", "movie", "top")


def test_fetch_addon_catalog_error_never_repeats_raw_url_or_query_token():
    secret_url = "https://a.example/manifest.json?api_key=SECRET-TOKEN"
    client = _FakeClient(FakeSession(responses=[FakeResponse({}, status_code=500)]))
    with pytest.raises(AddonError) as exc_info:
        fetch_addon_catalog(client, secret_url, "movie", "top")
    assert "SECRET-TOKEN" not in str(exc_info.value)
    assert secret_url not in str(exc_info.value)


def test_fetch_addon_catalog_connection_error_never_repeats_url():
    client = _FakeClient(FakeSession(exc=requests.exceptions.ConnectionError("boom")))
    with pytest.raises(AddonError) as exc_info:
        fetch_addon_catalog(client, "https://dead.example/manifest.json?token=abc", "movie", "top")
    assert "token=abc" not in str(exc_info.value)


# ---------------------------------------------------------------------------
# descriptor_state
# ---------------------------------------------------------------------------


def _installed(transport_url, version="1.0.0"):
    return {"transportUrl": transport_url, "manifest": {"id": "x", "version": version}, "flags": {}}


def test_descriptor_state_installed_when_transport_url_already_installed():
    installed = [_installed("https://a.example/manifest.json")]
    descriptor = {"transportUrl": "https://a.example/manifest.json", "manifest": {"id": "x", "version": "1.0.0"}}
    assert descriptor_state(descriptor, installed) == STATE_INSTALLED


def test_descriptor_state_update_available_when_catalog_version_is_newer():
    installed = [_installed("https://a.example/manifest.json", version="1.0.0")]
    descriptor = {"transportUrl": "https://a.example/manifest.json", "manifest": {"id": "x", "version": "1.1.0"}}
    assert descriptor_state(descriptor, installed) == STATE_UPDATE_AVAILABLE


def test_descriptor_state_installed_when_catalog_version_is_not_newer():
    installed = [_installed("https://a.example/manifest.json", version="1.1.0")]
    descriptor = {"transportUrl": "https://a.example/manifest.json", "manifest": {"id": "x", "version": "1.0.0"}}
    assert descriptor_state(descriptor, installed) == STATE_INSTALLED


def test_descriptor_state_installable_when_not_installed_and_no_configuration_required():
    descriptor = {"transportUrl": "https://new.example/manifest.json", "manifest": {"id": "y"}}
    assert descriptor_state(descriptor, []) == STATE_INSTALLABLE


def test_descriptor_state_needs_configuration_when_behavior_hint_set():
    descriptor = {
        "transportUrl": "https://new.example/manifest.json",
        "manifest": {"id": "y", "behaviorHints": {"configurationRequired": True}},
    }
    assert descriptor_state(descriptor, []) == STATE_NEEDS_CONFIGURATION


def test_descriptor_state_installed_addon_ignores_configuration_required():
    """An addon the user already runs is presumed already configured -
    a stale configurationRequired flag in the catalog listing must never
    downgrade an installed row back to "needs configuration"."""
    installed = [_installed("https://a.example/manifest.json")]
    descriptor = {
        "transportUrl": "https://a.example/manifest.json",
        "manifest": {"id": "x", "version": "1.0.0", "behaviorHints": {"configurationRequired": True}},
    }
    assert descriptor_state(descriptor, installed) == STATE_INSTALLED


def test_descriptor_state_matches_by_transport_url_not_manifest_id():
    """Two different addons may reuse the same manifest id; matching by
    transportUrl (the protocol's own identity key) must not conflate them."""
    installed = [_installed("https://other.example/manifest.json")]
    descriptor = {"transportUrl": "https://new.example/manifest.json", "manifest": {"id": "x", "version": "1.0.0"}}
    assert descriptor_state(descriptor, installed) == STATE_INSTALLABLE
