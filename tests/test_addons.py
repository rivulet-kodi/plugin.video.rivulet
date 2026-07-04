"""Protocol tests for lib.stremio.addons.

Reference: stremio-core src/types/addon/manifest.rs (Manifest::is_resource_supported),
src/addon_transport/http_transport/http_transport.rs (URL patterns), and
src/constants.rs URI_COMPONENT_ENCODE_SET (safe set: -_.!~*'()).
"""
import pytest

from lib.stremio.addons import (
    AddonClient,
    AddonError,
    addon_supports,
    build_resource_url,
    encode_extra,
    iter_catalogs,
)
from tests.conftest import FakeSession


# --- encode_extra ------------------------------------------------------


def test_encode_extra_single_pair():
    assert encode_extra([("skip", "100")]) == "skip=100"


def test_encode_extra_percent_encodes_space():
    assert encode_extra([("search", "breaking bad")]) == "search=breaking%20bad"


def test_encode_extra_joins_multiple_pairs_with_ampersand_in_order():
    result = encode_extra([("search", "breaking bad"), ("skip", "100")])
    assert result == "search=breaking%20bad&skip=100"


def test_encode_extra_preserves_order_reversed():
    result = encode_extra([("skip", "100"), ("search", "breaking bad")])
    assert result == "skip=100&search=breaking%20bad"


def test_encode_extra_safe_chars_untouched():
    # Safe set explicitly excluded from percent-encoding: -_.!~*'()
    safe = "-_.!~*'()"
    assert encode_extra([("id", safe)]) == "id=" + safe


def test_encode_extra_percent_encodes_reserved_chars():
    result = encode_extra([("id", "tt1234:1/2")])
    assert result == "id=tt1234%3A1%2F2"


def test_encode_extra_empty_list_is_empty_string():
    assert encode_extra([]) == ""


def test_encode_extra_encodes_name_too():
    result = encode_extra([("last videos ids", "a,b")])
    assert result == "last%20videos%20ids=a%2Cb"


# --- build_resource_url --------------------------------------------------


def test_build_resource_url_strips_manifest_json_suffix():
    url = build_resource_url(
        "https://v3-cinemeta.strem.io/manifest.json", "catalog", "movie", "top"
    )
    assert url == "https://v3-cinemeta.strem.io/catalog/movie/top.json"


def test_build_resource_url_without_manifest_suffix():
    url = build_resource_url("https://v3-cinemeta.strem.io", "catalog", "movie", "top")
    assert url == "https://v3-cinemeta.strem.io/catalog/movie/top.json"


def test_build_resource_url_no_extra_segment_when_extra_falsy():
    url = build_resource_url(
        "https://addon.example/manifest.json", "meta", "series", "tt1234567"
    )
    assert url == "https://addon.example/meta/series/tt1234567.json"


def test_build_resource_url_with_extra_list_of_pairs():
    url = build_resource_url(
        "https://v3-cinemeta.strem.io/manifest.json",
        "catalog",
        "movie",
        "top",
        extra=[("search", "breaking bad"), ("skip", "100")],
    )
    assert url == (
        "https://v3-cinemeta.strem.io/catalog/movie/top/"
        "search=breaking%20bad&skip=100.json"
    )


def test_build_resource_url_percent_encodes_id():
    url = build_resource_url(
        "https://addon.example/manifest.json", "meta", "series", "tt1234567:1:2"
    )
    assert url == "https://addon.example/meta/series/tt1234567%3A1%3A2.json"


# --- addon_supports --------------------------------------------------------


def _manifest(**overrides):
    base = {
        "id": "org.test.addon",
        "types": ["movie", "series"],
        "idPrefixes": ["tt"],
        "resources": ["catalog", "meta", "stream"],
    }
    base.update(overrides)
    return base


def test_addon_supports_short_form_uses_global_types_and_prefixes():
    manifest = _manifest()
    assert addon_supports(manifest, "meta", "movie", "tt1234567") is True


def test_addon_supports_short_form_type_not_in_global_types():
    manifest = _manifest()
    assert addon_supports(manifest, "meta", "channel", "tt1234567") is False


def test_addon_supports_short_form_id_prefix_mismatch():
    manifest = _manifest()
    assert addon_supports(manifest, "meta", "movie", "kitsu:1234") is False


def test_addon_supports_short_form_empty_global_prefixes_matches_any_id():
    manifest = _manifest(idPrefixes=[])
    assert addon_supports(manifest, "meta", "movie", "anything:1") is True


def test_addon_supports_resource_not_declared_returns_false():
    manifest = _manifest(resources=["catalog"])
    assert addon_supports(manifest, "stream", "movie", "tt1234567") is False


def test_addon_supports_long_form_own_types_and_prefixes():
    manifest = _manifest(
        resources=[
            "catalog",
            {"name": "meta", "types": ["movie"], "idPrefixes": ["tt"]},
        ]
    )
    assert addon_supports(manifest, "meta", "movie", "tt1234567") is True
    assert addon_supports(manifest, "meta", "series", "tt1234567") is False
    assert addon_supports(manifest, "meta", "movie", "kitsu:1") is False


def test_addon_supports_long_form_explicit_empty_id_prefixes_matches_any_id():
    manifest = _manifest(
        resources=[{"name": "meta", "types": ["movie"], "idPrefixes": []}]
    )
    assert addon_supports(manifest, "meta", "movie", "anything:1") is True


def test_addon_supports_long_form_explicit_empty_types_matches_nothing():
    manifest = _manifest(
        resources=[{"name": "meta", "types": [], "idPrefixes": ["tt"]}]
    )
    assert addon_supports(manifest, "meta", "movie", "tt1234567") is False


def test_addon_supports_long_form_absent_types_falls_back_to_global():
    manifest = _manifest(
        types=["movie", "series"],
        resources=[{"name": "meta", "idPrefixes": ["tt"]}],
    )
    assert addon_supports(manifest, "meta", "movie", "tt1234567") is True
    assert addon_supports(manifest, "meta", "channel", "tt1234567") is False


def test_addon_supports_long_form_absent_id_prefixes_falls_back_to_global():
    manifest = _manifest(
        idPrefixes=["tt"],
        resources=[{"name": "meta", "types": ["movie"]}],
    )
    assert addon_supports(manifest, "meta", "movie", "tt1234567") is True
    assert addon_supports(manifest, "meta", "movie", "kitsu:1") is False


def test_addon_supports_rid_none_skips_id_check_entirely():
    manifest = _manifest(idPrefixes=["tt"])
    # rid omitted -> only resource+type checked, id-prefix restriction bypassed
    assert addon_supports(manifest, "meta", "movie") is True
    assert addon_supports(manifest, "meta", "movie", None) is True


# --- iter_catalogs -----------------------------------------------------


def _addon(transport_url, catalogs, manifest_extra=None):
    manifest = {"id": "org.test", "catalogs": catalogs}
    if manifest_extra:
        manifest.update(manifest_extra)
    return {"transportUrl": transport_url, "manifest": manifest, "flags": {}}


def test_iter_catalogs_yields_all_when_no_extra_required():
    addons = [
        _addon(
            "https://a.example/manifest.json",
            [{"id": "top", "type": "movie", "name": "Top"}],
        )
    ]
    results = list(iter_catalogs(addons))
    assert len(results) == 1
    transport_url, manifest, catalog = results[0]
    assert transport_url == "https://a.example/manifest.json"
    assert manifest["id"] == "org.test"
    assert catalog["id"] == "top"


def test_iter_catalogs_filters_by_search_extra_modern_form():
    addons = [
        _addon(
            "https://a.example/manifest.json",
            [
                {
                    "id": "top",
                    "type": "movie",
                    "name": "Top",
                    "extra": [{"name": "skip", "isRequired": False}],
                },
                {
                    "id": "search",
                    "type": "movie",
                    "name": "Search",
                    "extra": [{"name": "search", "isRequired": False}],
                },
            ],
        )
    ]
    results = list(iter_catalogs(addons, extra_required="search"))
    assert len(results) == 1
    assert results[0][2]["id"] == "search"


def test_iter_catalogs_filters_by_search_extra_legacy_form():
    addons = [
        _addon(
            "https://a.example/manifest.json",
            [
                {"id": "top", "type": "movie", "name": "Top", "extraSupported": ["skip"]},
                {
                    "id": "search",
                    "type": "movie",
                    "name": "Search",
                    "extraSupported": ["search", "skip"],
                },
            ],
        )
    ]
    results = list(iter_catalogs(addons, extra_required="search"))
    assert len(results) == 1
    assert results[0][2]["id"] == "search"


def test_iter_catalogs_aggregates_across_multiple_addons():
    addons = [
        _addon("https://a.example/manifest.json", [{"id": "top", "type": "movie", "name": "Top"}]),
        _addon("https://b.example/manifest.json", [{"id": "trending", "type": "series", "name": "Trending"}]),
    ]
    results = list(iter_catalogs(addons))
    assert {r[0] for r in results} == {"https://a.example/manifest.json", "https://b.example/manifest.json"}
    assert len(results) == 2


def test_iter_catalogs_no_catalogs_yields_nothing():
    addons = [_addon("https://a.example/manifest.json", [])]
    assert list(iter_catalogs(addons)) == []


# --- AddonClient -----------------------------------------------------------


MANIFEST_URL = "https://addon.example/manifest.json"


def test_addon_client_manifest_returns_dict():
    client = AddonClient()
    client.session = FakeSession(
        responses=[_json_response({"id": "org.test", "name": "Test"})]
    )
    manifest = client.manifest(MANIFEST_URL)
    assert manifest == {"id": "org.test", "name": "Test"}
    assert client.session.calls[0]["url"] == MANIFEST_URL


def test_addon_client_catalog_unwraps_metas():
    client = AddonClient()
    client.session = FakeSession(
        responses=[_json_response({"metas": [{"id": "tt1", "name": "Movie 1"}]})]
    )
    metas = client.catalog("https://addon.example", "movie", "top")
    assert metas == [{"id": "tt1", "name": "Movie 1"}]


def test_addon_client_catalog_tolerates_missing_metas_key():
    client = AddonClient()
    client.session = FakeSession(responses=[_json_response({})])
    metas = client.catalog("https://addon.example", "movie", "top")
    assert metas == []


def test_addon_client_meta_unwraps_meta_key():
    client = AddonClient()
    client.session = FakeSession(
        responses=[_json_response({"meta": {"id": "tt1", "name": "Movie 1"}})]
    )
    meta = client.meta("https://addon.example", "movie", "tt1")
    assert meta == {"id": "tt1", "name": "Movie 1"}


def test_addon_client_meta_tolerates_missing_meta_key():
    client = AddonClient()
    client.session = FakeSession(responses=[_json_response({})])
    meta = client.meta("https://addon.example", "movie", "tt1")
    assert meta is None


def test_addon_client_streams_tolerates_missing_streams_key():
    client = AddonClient()
    client.session = FakeSession(responses=[_json_response({})])
    assert client.streams("https://addon.example", "movie", "tt1") == []


def test_addon_client_streams_unwraps_streams_key():
    client = AddonClient()
    client.session = FakeSession(
        responses=[_json_response({"streams": [{"url": "https://x/y.mp4"}]})]
    )
    assert client.streams("https://addon.example", "movie", "tt1") == [
        {"url": "https://x/y.mp4"}
    ]


def test_addon_client_subtitles_tolerates_missing_subtitles_key():
    client = AddonClient()
    client.session = FakeSession(responses=[_json_response({})])
    assert client.subtitles("https://addon.example", "movie", "tt1") == []


def test_addon_client_subtitles_unwraps_subtitles_key():
    client = AddonClient()
    client.session = FakeSession(
        responses=[_json_response({"subtitles": [{"id": "os:1", "lang": "en", "url": "https://x/y.vtt"}]})]
    )
    subs = client.subtitles("https://addon.example", "movie", "tt1")
    assert subs == [{"id": "os:1", "lang": "en", "url": "https://x/y.vtt"}]


def test_addon_client_raises_addon_error_on_http_failure():
    client = AddonClient()
    client.session = FakeSession(responses=[_error_response(500)])
    with pytest.raises(AddonError):
        client.catalog("https://addon.example", "movie", "top")


def test_addon_client_raises_addon_error_on_connection_failure():
    import requests

    client = AddonClient()
    client.session = FakeSession(exc=requests.exceptions.ConnectionError("refused"))
    with pytest.raises(AddonError):
        client.manifest(MANIFEST_URL)


def test_addon_client_raises_addon_error_on_invalid_json():
    client = AddonClient()
    client.session = FakeSession(responses=[_invalid_json_response()])
    with pytest.raises(AddonError):
        client.manifest(MANIFEST_URL)


def _json_response(data, status_code=200):
    class _Resp:
        ok = status_code < 400

        def __init__(self):
            self.status_code = status_code

        def raise_for_status(self):
            if not self.ok:
                import requests

                raise requests.exceptions.HTTPError("%s error" % self.status_code)

        def json(self):
            return data

    return _Resp()


def _error_response(status_code):
    return _json_response({}, status_code=status_code)


def _invalid_json_response():
    class _Resp:
        ok = True
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            raise ValueError("invalid json")

    return _Resp()
