"""Protocol tests for lib.stremio.addons.

Reference: stremio-core src/types/addon/manifest.rs (Manifest::is_resource_supported),
src/addon_transport/http_transport/http_transport.rs (URL patterns), and
src/constants.rs URI_COMPONENT_ENCODE_SET (safe set: -_.!~*'()).
"""
import pytest

from lib.stremio.addons import (
    AddonClient,
    AddonError,
    _catalog_extra_names,
    addon_supports,
    build_resource_url,
    catalog_extra_options,
    catalog_required_extra_names,
    encode_extra,
    iter_catalogs,
    safe_url_for_log,
    validate_transport_url,
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


# --- catalog_extra_options ----------------------------------------------


def test_catalog_extra_options_modern_form():
    catalog = {"extra": [{"name": "genre", "options": ["Action", "Comedy"]}]}
    assert catalog_extra_options(catalog, "genre") == ["Action", "Comedy"]


def test_catalog_extra_options_legacy_genres_fallback():
    catalog = {"genres": ["2026", "2025", "1920"]}
    assert catalog_extra_options(catalog, "genre") == ["2026", "2025", "1920"]


def test_catalog_extra_options_modern_wins_over_legacy():
    catalog = {
        "extra": [{"name": "genre", "options": ["Action"]}],
        "genres": ["Comedy"],
    }
    assert catalog_extra_options(catalog, "genre") == ["Action"]


def test_catalog_extra_options_undeclared_extra_returns_empty():
    catalog = {"extra": [{"name": "search"}]}
    assert catalog_extra_options(catalog, "genre") == []


def test_catalog_extra_options_declared_without_options_returns_empty():
    catalog = {"extra": [{"name": "genre"}]}
    assert catalog_extra_options(catalog, "genre") == []


def test_catalog_extra_options_none_catalog_returns_empty():
    assert catalog_extra_options(None, "genre") == []


def test_catalog_extra_options_non_dict_catalog_returns_empty():
    assert catalog_extra_options(["not", "a", "dict"], "genre") == []


def test_catalog_extra_options_options_as_string_not_list_returns_empty():
    catalog = {"extra": [{"name": "genre", "options": "Action"}]}
    assert catalog_extra_options(catalog, "genre") == []


def test_catalog_extra_options_preserves_order_and_dedupes():
    catalog = {"extra": [{"name": "genre", "options": ["2026", "2025", "2026", "2025"]}]}
    assert catalog_extra_options(catalog, "genre") == ["2026", "2025"]


def test_catalog_extra_options_stringifies_int_options():
    catalog = {"extra": [{"name": "year", "options": [2026, 2025, 1920]}]}
    assert catalog_extra_options(catalog, "year") == ["2026", "2025", "1920"]


def test_catalog_extra_options_real_cinemeta_year_catalog_shape():
    catalog = {
        "type": "movie",
        "id": "year",
        "extra": [
            {"name": "genre", "options": ["2026", "2025", "1920"]},
            {"name": "skip"},
        ],
        "genres": ["2026", "2025", "1920"],
        "extraSupported": ["genre", "skip"],
    }
    assert catalog_extra_options(catalog, "genre") == ["2026", "2025", "1920"]


# --- catalog_required_extra_names --------------------------------------


def test_catalog_required_extra_names_modern_is_required_true():
    catalog = {"extra": [{"name": "search", "isRequired": True}]}
    assert catalog_required_extra_names(catalog) == {"search"}


def test_catalog_required_extra_names_modern_is_required_false_not_picked_up():
    catalog = {"extra": [{"name": "search", "isRequired": False}]}
    assert catalog_required_extra_names(catalog) == set()


def test_catalog_required_extra_names_modern_is_required_absent_not_picked_up():
    catalog = {"extra": [{"name": "skip"}]}
    assert catalog_required_extra_names(catalog) == set()


def test_catalog_required_extra_names_legacy_extra_required():
    catalog = {"extraRequired": ["search"]}
    assert catalog_required_extra_names(catalog) == {"search"}


def test_catalog_required_extra_names_unions_modern_and_legacy():
    catalog = {
        "extra": [{"name": "search", "isRequired": True}],
        "extraRequired": ["genre"],
    }
    assert catalog_required_extra_names(catalog) == {"search", "genre"}


def test_catalog_required_extra_names_none_catalog_returns_empty():
    assert catalog_required_extra_names(None) == set()


def test_catalog_required_extra_names_non_dict_catalog_returns_empty():
    assert catalog_required_extra_names(["not", "a", "dict"]) == set()


def test_catalog_required_extra_names_extra_as_string_returns_empty():
    assert catalog_required_extra_names({"extra": "search"}) == set()


def test_catalog_required_extra_names_extra_required_as_string_returns_empty():
    assert catalog_required_extra_names({"extraRequired": "search"}) == set()


def test_catalog_required_extra_names_non_dict_entries_in_extra_ignored():
    catalog = {"extra": ["search", None, {"name": "genre", "isRequired": True}]}
    assert catalog_required_extra_names(catalog) == {"genre"}


def test_catalog_required_extra_names_empty_or_none_names_ignored():
    catalog = {
        "extra": [{"name": None, "isRequired": True}, {"isRequired": True}],
        "extraRequired": ["", None],
    }
    assert catalog_required_extra_names(catalog) == set()


def test_catalog_required_extra_names_search_only_catalog_real_shape():
    catalog = {
        "type": "movie",
        "id": "search",
        "name": "Foo Search",
        "extra": [{"name": "search", "isRequired": True}, {"name": "skip"}],
    }
    assert catalog_required_extra_names(catalog) == {"search"}


def test_catalog_required_extra_names_does_not_confuse_with_catalog_extra_names():
    catalog = {
        "type": "movie",
        "id": "search",
        "name": "Foo Search",
        "extra": [{"name": "search", "isRequired": True}, {"name": "skip"}],
    }
    assert _catalog_extra_names(catalog) == {"search", "skip"}
    assert catalog_required_extra_names(catalog) == {"search"}


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


# --- validate_transport_url -------------------------------------------------


@pytest.mark.parametrize('url,expected', [
    ('https://v3-cinemeta.strem.io/manifest.json', 'https://v3-cinemeta.strem.io/manifest.json'),
    ('https://192.0.2.1/manifest.json', 'https://192.0.2.1/manifest.json'),  # HTTPS is fine for any host, public IP included
    ('http://localhost/manifest.json', 'http://localhost/manifest.json'),
    ('http://LOCALHOST/manifest.json', 'http://localhost/manifest.json'),  # host normalized to lowercase
    ('http://127.0.0.1:11470/manifest.json', 'http://127.0.0.1:11470/manifest.json'),
    ('http://[::1]:11470/manifest.json', 'http://[::1]:11470/manifest.json'),
    ('http://192.168.1.5/manifest.json', 'http://192.168.1.5/manifest.json'),   # private (RFC 1918)
    ('http://169.254.1.1/manifest.json', 'http://169.254.1.1/manifest.json'),   # link-local (RFC 3927)
    ('http://[fe80::1]/manifest.json', 'http://[fe80::1]/manifest.json'),       # link-local (RFC 4291)
], ids=[
    'public-https', 'https-public-ip', 'localhost', 'localhost-mixed-case',
    'loopback-v4', 'loopback-v6', 'private-v4', 'link-local-v4', 'link-local-v6',
])
def test_validate_transport_url_accepts(url, expected):
    assert validate_transport_url(url) == expected


@pytest.mark.parametrize('url', [
    'http://example.com/manifest.json',
    'http://8.8.8.8/manifest.json',
    'https://user:pass@example.com/manifest.json',
    'https://token@example.com/manifest.json',
    'file:///etc/passwd',
    'plugin://plugin.video.rivulet/play',
    'https:///manifest.json',
    'https://example.com:notaport/manifest.json',
    'https://example.com/manifest.json#frag',
    'ftp://example.com/manifest.json',
], ids=[
    'plaintext-public-host', 'plaintext-public-ip', 'userinfo-user-pass',
    'userinfo-token-only', 'file-scheme', 'plugin-scheme', 'missing-host',
    'malformed-port', 'fragment', 'unsupported-scheme',
])
def test_validate_transport_url_rejects(url):
    with pytest.raises(AddonError) as exc_info:
        validate_transport_url(url)
    assert url not in str(exc_info.value)


def test_validate_transport_url_normalizes_scheme_and_host_case():
    url = validate_transport_url('HTTPS://Addon.Example/manifest.json')
    assert url == 'https://addon.example/manifest.json'


def test_validate_transport_url_preserves_path_and_query():
    url = validate_transport_url('https://addon.example/catalog/movie/top.json?skip=100')
    assert url == 'https://addon.example/catalog/movie/top.json?skip=100'


# --- safe_url_for_log --------------------------------------------------------


def test_safe_url_for_log_strips_userinfo_path_query_fragment():
    url = 'https://user:token@addon.example:8443/manifest.json?api_key=SECRET#frag'
    assert safe_url_for_log(url) == 'https://addon.example:8443'


def test_safe_url_for_log_normalizes_scheme_and_host_case():
    assert safe_url_for_log('HTTP://Addon.Example/x') == 'http://addon.example'


def test_safe_url_for_log_keeps_ipv6_brackets():
    assert safe_url_for_log('http://[::1]:11470/manifest.json') == 'http://[::1]:11470'


@pytest.mark.parametrize('url', [
    'not a url at all \t\n',
    'https:///manifest.json',
    'https://example.com:notaport/manifest.json',
    '',
])
def test_safe_url_for_log_returns_sentinel_for_unparseable_urls(url):
    assert safe_url_for_log(url) == '<invalid-url>'


# --- AddonClient enforces validate_transport_url ----------------------------


def test_addon_client_rejects_plaintext_public_host_before_any_request():
    client = AddonClient()
    client.session = FakeSession()  # no queued responses: a real GET would fail the test
    with pytest.raises(AddonError):
        client.manifest('http://example.com/manifest.json')
    assert client.session.calls == []


def test_addon_client_error_message_never_repeats_full_url_or_query_token():
    import requests

    secret_url = 'https://addon.example/manifest.json?api_key=SECRET-TOKEN'
    client = AddonClient()
    client.session = FakeSession(
        exc=requests.exceptions.ConnectionError(
            'Failed to establish a new connection: ' + secret_url
        )
    )
    with pytest.raises(AddonError) as exc_info:
        client.manifest(secret_url)
    message = str(exc_info.value)
    assert 'SECRET-TOKEN' not in message
    assert 'api_key' not in message
    assert message == 'GET https://addon.example failed: ConnectionError'


def test_addon_client_error_message_uses_http_status_category():
    from tests.conftest import FakeResponse

    client = AddonClient()
    client.session = FakeSession(responses=[FakeResponse(status_code=500)])
    with pytest.raises(AddonError) as exc_info:
        client.catalog('https://addon.example', 'movie', 'top')
    assert str(exc_info.value) == 'GET https://addon.example failed: HTTP 500'


def test_addon_client_invalid_json_error_message_is_safe():
    client = AddonClient()
    client.session = FakeSession(responses=[_invalid_json_response()])
    with pytest.raises(AddonError) as exc_info:
        client.manifest('https://addon.example/manifest.json?api_key=SECRET')
    message = str(exc_info.value)
    assert 'SECRET' not in message
    assert message == 'GET https://addon.example returned invalid JSON'
