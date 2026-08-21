"""Tests for lib.stremio.addoncatalogs: the `addon_catalog` protocol
client and installed-addon-catalog aggregation.

Like lib.stremio.addons (see tests/test_addons.py), this module has zero
`xbmc*` imports, so it is imported directly here with no Kodi stubs at
all - if it ever grew one, that import would fail collection outright.
"""
import inspect
import re

import pytest
import requests

from lib.stremio import addoncatalogs
from lib.stremio.addoncatalogs import (
    STATE_INSTALLABLE,
    STATE_INSTALLED,
    STATE_NEEDS_CONFIGURATION,
    STATE_UPDATE_AVAILABLE,
    descriptor_state,
    fetch_addon_catalog,
    fetch_addon_catalog_cached,
    fetch_addon_catalogs,
    iter_addon_catalogs,
    iter_unique_addon_catalogs,
)
from lib.stremio.addons import AddonError
from tests.conftest import FakeResponse, FakeSession


@pytest.fixture(autouse=True)
def _clear_catalog_cache():
    """`addoncatalogs._catalog_cache` is a module-level dict shared by every
    test in this process (see its own docstring for why it cannot be a
    per-call fixture-scoped object) - without resetting it, whichever test
    happens to run first for a given URL would decide every later test's
    cache-hit/miss outcome under pytest-randomly. Scoped to this file only:
    no other test module touches this cache."""
    addoncatalogs._catalog_cache.clear()
    yield
    addoncatalogs._catalog_cache.clear()


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


# ---------------------------------------------------------------------------
# iter_unique_addon_catalogs
# ---------------------------------------------------------------------------


#: The 11 (type, id) pairs Cinemeta's own manifest declares - 4 under id
#: "official", 7 under id "community" - each set including an "all" variant
#: whose response already contains every narrower type's rows. See
#: lib.stremio.addoncatalogs's module docstring for the live 297/102 census.
_CINEMETA_ADDON_CATALOGS = (
    [{"type": t, "id": "official"} for t in ("all", "movie", "series", "channel")]
    + [
        {"type": t, "id": "community"}
        for t in ("all", "movie", "series", "channel", "tv", "Podcasts", "other")
    ]
)


def test_iter_unique_addon_catalogs_collapses_cinemeta_shaped_pairs_to_broadest_type():
    """11 declared pairs across 2 ids, each with an "all" variant, must
    collapse to exactly 2 entries using type "all" - the 11->2 reduction
    this helper exists for."""
    assert len(_CINEMETA_ADDON_CATALOGS) == 11
    addons = [_addon("https://cinemeta.example/manifest.json", {
        "id": "com.linvo.cinemeta",
        "addonCatalogs": _CINEMETA_ADDON_CATALOGS,
    })]
    results = list(iter_unique_addon_catalogs(addons))
    assert len(results) == 2
    by_id = {addon_catalog["id"]: addon_catalog for _, _, addon_catalog in results}
    assert set(by_id) == {"official", "community"}
    assert by_id["official"]["type"] == "all"
    assert by_id["community"]["type"] == "all"


def test_iter_unique_addon_catalogs_keeps_first_declared_type_when_no_all_variant():
    """An id never declared with type "all" is still fetched exactly once,
    under whichever type it declared first."""
    addons = [_addon("https://a.example/manifest.json", {
        "id": "a",
        "addonCatalogs": [
            {"type": "movie", "id": "foo"},
            {"type": "series", "id": "foo"},
        ],
    })]
    results = list(iter_unique_addon_catalogs(addons))
    assert len(results) == 1
    _, _, addon_catalog = results[0]
    assert addon_catalog["type"] == "movie"


def test_iter_unique_addon_catalogs_no_addon_catalogs_field_yields_nothing():
    addons = [_addon("https://a.example/manifest.json", {"id": "a"})]
    assert list(iter_unique_addon_catalogs(addons)) == []


def test_iter_unique_addon_catalogs_no_addons_yields_nothing():
    assert list(iter_unique_addon_catalogs([])) == []
    assert list(iter_unique_addon_catalogs(None)) == []


def test_iter_unique_addon_catalogs_aggregates_across_multiple_addons():
    addons = [
        _addon("https://a.example/manifest.json", {"id": "a", "addonCatalogs": [{"type": "movie", "id": "x"}]}),
        _addon("https://b.example/manifest.json", {"id": "b", "addonCatalogs": [{"type": "series", "id": "y"}]}),
    ]
    results = list(iter_unique_addon_catalogs(addons))
    assert len(results) == 2
    assert {r[0] for r in results} == {"https://a.example/manifest.json", "https://b.example/manifest.json"}


# ---------------------------------------------------------------------------
# fetch_addon_catalogs (bounded concurrent fetch)
# ---------------------------------------------------------------------------


def test_fetch_addon_catalogs_no_sources_returns_empty_without_a_pool():
    assert fetch_addon_catalogs(_FakeClient(FakeSession()), []) == ([], [])


def test_fetch_addon_catalogs_fetches_collapsed_cinemeta_sources_exactly_twice():
    """End-to-end: collapsing Cinemeta's 11 pairs to 2 unique ids and
    fetching those through fetch_addon_catalogs() issues exactly 2 HTTP
    requests, both for the "all" variant - the measured 11->2 fan-out
    reduction the module docstring documents."""
    addons = [_addon("https://cinemeta.example/manifest.json", {
        "id": "com.linvo.cinemeta",
        "addonCatalogs": _CINEMETA_ADDON_CATALOGS,
    })]
    sources = [
        (transport_url, addon_catalog["type"], addon_catalog["id"])
        for transport_url, _, addon_catalog in iter_unique_addon_catalogs(addons)
    ]
    assert len(sources) == 2

    session = FakeSession(responses=[FakeResponse(ENVELOPE), FakeResponse(ENVELOPE)])
    client = _FakeClient(session)
    entries, failures = fetch_addon_catalogs(client, sources)

    assert failures == []
    assert entries == ENVELOPE["addons"] * 2
    assert len(session.calls) == 2
    assert {call["url"] for call in session.calls} == {
        "https://cinemeta.example/addon_catalog/all/official.json",
        "https://cinemeta.example/addon_catalog/all/community.json",
    }


class _PerUrlSession:
    """Fake session that raises for one URL prefix and answers everything
    else with a fixed response - needed because a single FakeSession
    cannot fail selectively per source URL."""

    def __init__(self, dead_url_prefix, ok_response, exc):
        self._dead_url_prefix = dead_url_prefix
        self._ok_response = ok_response
        self._exc = exc
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        if url.startswith(self._dead_url_prefix):
            raise self._exc
        return self._ok_response


def test_fetch_addon_catalogs_one_dead_source_never_loses_the_others_entries():
    """About 16% of a community catalog's transportUrls are dead in
    practice (module docstring); one AddonError must never cost the
    other, healthy sources their rows."""
    session = _PerUrlSession(
        dead_url_prefix="https://dead.example",
        ok_response=FakeResponse(ENVELOPE),
        exc=requests.exceptions.ConnectionError("boom"),
    )
    client = _FakeClient(session)
    sources = [
        ("https://dead.example/manifest.json", "movie", "top"),
        ("https://a.example/manifest.json", "movie", "top"),
    ]
    entries, failures = fetch_addon_catalogs(client, sources)

    assert entries == ENVELOPE["addons"]
    assert len(failures) == 1
    transport_url, type_, id_, error = failures[0]
    assert (transport_url, type_, id_) == ("https://dead.example/manifest.json", "movie", "top")
    assert isinstance(error, AddonError)


# ---------------------------------------------------------------------------
# fetch_addon_catalog_cached (TTL cache)
# ---------------------------------------------------------------------------


def test_fetch_addon_catalog_cached_second_fetch_within_ttl_issues_zero_requests():
    session = FakeSession(responses=[FakeResponse(ENVELOPE)])
    client = _FakeClient(session)
    url = "https://cache-hit.example/manifest.json"

    first = fetch_addon_catalog_cached(client, url, "movie", "top")
    second = fetch_addon_catalog_cached(client, url, "movie", "top")

    assert first == ENVELOPE["addons"]
    assert second == ENVELOPE["addons"]
    assert len(session.calls) == 1


def test_fetch_addon_catalog_cached_refetches_after_ttl_expires(monkeypatch):
    session = FakeSession(responses=[FakeResponse(ENVELOPE), FakeResponse(ENVELOPE)])
    client = _FakeClient(session)
    url = "https://cache-expiry.example/manifest.json"
    start = 1000.0

    monkeypatch.setattr(addoncatalogs.time, "monotonic", lambda: start)
    fetch_addon_catalog_cached(client, url, "movie", "top")
    assert len(session.calls) == 1

    monkeypatch.setattr(
        addoncatalogs.time, "monotonic",
        lambda: start + addoncatalogs._CATALOG_CACHE_TTL_SECONDS + 1,
    )
    fetch_addon_catalog_cached(client, url, "movie", "top")
    assert len(session.calls) == 2


def test_fetch_addon_catalog_cached_keys_are_specific_to_transport_url_type_and_id():
    """A cache hit on one (transport_url, type_, id_) must never leak into
    a different one - each triple gets its own HTTP call."""
    session = FakeSession(responses=[FakeResponse(ENVELOPE), FakeResponse(ENVELOPE)])
    client = _FakeClient(session)
    url = "https://cache-key.example/manifest.json"

    fetch_addon_catalog_cached(client, url, "movie", "top")
    fetch_addon_catalog_cached(client, url, "series", "top")

    assert len(session.calls) == 2


# ---------------------------------------------------------------------------
# Kodi-independence
# ---------------------------------------------------------------------------


def test_module_has_no_xbmc_import():
    """This module must stay Kodi-independent (AGENTS.md, module docstring)
    - an xbmc* import here would break test collection for every test in
    this file, since none of them install kodi stubs."""
    source = inspect.getsource(addoncatalogs)
    assert not re.search(r"^\s*(import|from)\s+xbmc", source, re.MULTILINE)
