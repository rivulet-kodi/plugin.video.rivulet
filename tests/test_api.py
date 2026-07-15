"""Protocol tests for lib.stremio.api.StremioAPI (api.strem.io).

Reference: stremio-core src/types/api/request.rs (APIRequest/AuthRequest/
DatastoreRequest) and src/types/api/fetch_api.rs (endpoint.join("api/").join(
version_path)) -> every call lands on https://api.strem.io/api/<method>.
Request/response shapes cross-checked against stremio-core auth unit tests.
No network access - StremioAPI is exercised by substituting `api.session`
with `tests.conftest.FakeSession`.
"""
import pytest
import requests

from lib.stremio.api import ApiError, StremioAPI
from tests.conftest import FakeSession

API_BASE = "https://api.strem.io"


def make_api():
    return StremioAPI()


# --- login -------------------------------------------------------------


def test_login_posts_to_api_login_path():
    api = make_api()
    api.session = FakeSession(
        responses=[_ok({"result": {"authKey": "tok-123", "user": {"email": "a@b.com"}}})]
    )
    result = api.login("a@b.com", "hunter2")

    assert result == {"authKey": "tok-123", "user": {"email": "a@b.com"}}
    call = api.session.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == API_BASE + "/api/login"


def test_login_body_shape_matches_auth_request():
    api = make_api()
    api.session = FakeSession(responses=[_ok({"result": {"authKey": "t", "user": {}}})])
    api.login("a@b.com", "hunter2")

    body = api.session.calls[0]["kwargs"]["json"]
    assert body["type"] == "Login"
    assert body["email"] == "a@b.com"
    assert body["password"] == "hunter2"


def test_login_error_envelope_raises_api_error():
    api = make_api()
    api.session = FakeSession(
        responses=[_ok({"error": {"message": "User not found", "code": 2}})]
    )
    with pytest.raises(ApiError) as excinfo:
        api.login("a@b.com", "wrong")
    assert "User not found" in str(excinfo.value)
    # A 200-OK JSON error envelope carries no HTTP-status signal -- the
    # undocumented numeric `code` must never be treated as an auth signal.
    assert excinfo.value.status_code is None
    assert excinfo.value.is_auth_error is False


def test_login_http_error_status_raises_api_error():
    api = make_api()
    api.session = FakeSession(responses=[_http_error(500)])
    with pytest.raises(ApiError) as excinfo:
        api.login("a@b.com", "hunter2")
    assert excinfo.value.status_code == 500
    assert excinfo.value.is_auth_error is False


def test_login_http_error_401_sets_status_code_and_is_auth_error():
    api = make_api()
    api.session = FakeSession(responses=[_http_error(401)])
    with pytest.raises(ApiError) as excinfo:
        api.login("a@b.com", "hunter2")
    assert excinfo.value.status_code == 401
    assert excinfo.value.is_auth_error is True


def test_login_http_error_403_sets_status_code_and_is_auth_error():
    api = make_api()
    api.session = FakeSession(responses=[_http_error(403)])
    with pytest.raises(ApiError) as excinfo:
        api.login("a@b.com", "hunter2")
    assert excinfo.value.status_code == 403
    assert excinfo.value.is_auth_error is True


def test_connection_failure_has_no_status_code_and_is_not_auth_error():
    api = make_api()
    api.session = FakeSession(exc=requests.exceptions.ConnectionError("connection refused"))
    with pytest.raises(ApiError) as excinfo:
        api.login("a@b.com", "hunter2")
    assert excinfo.value.status_code is None
    assert excinfo.value.is_auth_error is False


# --- logout ------------------------------------------------------------


def test_logout_posts_to_api_logout_with_auth_key():
    api = make_api()
    api.session = FakeSession(responses=[_ok({"result": {"success": True}})])
    api.logout("tok-123")

    call = api.session.calls[0]
    assert call["url"] == API_BASE + "/api/logout"
    body = call["kwargs"]["json"]
    assert body["type"] == "Logout"
    assert body["authKey"] == "tok-123"


def test_logout_error_envelope_raises_api_error():
    api = make_api()
    api.session = FakeSession(
        responses=[_ok({"error": {"message": "Invalid auth key", "code": 1}})]
    )
    with pytest.raises(ApiError):
        api.logout("bad-token")


# --- addonCollectionGet ------------------------------------------------


def test_addon_collection_get_posts_correct_path_and_body():
    descriptors = [
        {"transportUrl": "https://a.example/manifest.json", "manifest": {"id": "a"}, "flags": {}}
    ]
    api = make_api()
    api.session = FakeSession(
        responses=[_ok({"result": {"addons": descriptors, "lastModified": "now"}})]
    )
    result = api.addon_collection_get("tok-123")

    assert result == descriptors
    call = api.session.calls[0]
    assert call["url"] == API_BASE + "/api/addonCollectionGet"
    body = call["kwargs"]["json"]
    assert body["type"] == "AddonCollectionGet"
    assert body["authKey"] == "tok-123"
    assert body["update"] is True


def test_addon_collection_get_error_raises_api_error():
    api = make_api()
    api.session = FakeSession(
        responses=[_ok({"error": {"message": "Unauthorized", "code": 1}})]
    )
    with pytest.raises(ApiError):
        api.addon_collection_get("bad-token")


def test_addon_collection_set_posts_correct_path_and_body():
    descriptors = [
        {"transportUrl": "https://a.example/manifest.json", "manifest": {"id": "a"}, "flags": {}}
    ]
    api = make_api()
    api.session = FakeSession(responses=[_ok({"result": {"success": True}})])
    api.addon_collection_set("tok-123", descriptors)

    call = api.session.calls[0]
    assert call["url"] == API_BASE + "/api/addonCollectionSet"
    body = call["kwargs"]["json"]
    assert body["type"] == "AddonCollectionSet"
    assert body["authKey"] == "tok-123"
    assert body["addons"] == descriptors


# --- datastoreGet --------------------------------------------------------


def test_datastore_get_posts_correct_path_and_body_defaults():
    items = [{"_id": "tt1", "removed": False}]
    api = make_api()
    api.session = FakeSession(responses=[_ok({"result": items})])
    result = api.datastore_get("tok-123")

    assert result == items
    call = api.session.calls[0]
    assert call["url"] == API_BASE + "/api/datastoreGet"
    body = call["kwargs"]["json"]
    # DatastoreCommand is #[serde(untagged)] -> no "type" tag on the wire
    assert "type" not in body
    assert body["authKey"] == "tok-123"
    assert body["collection"] == "libraryItem"
    assert body["all"] is True
    assert body["ids"] == []


def test_datastore_get_with_explicit_ids():
    api = make_api()
    api.session = FakeSession(responses=[_ok({"result": []})])
    api.datastore_get("tok-123", collection="libraryItem", ids=["tt1", "tt2"], all=False)

    body = api.session.calls[0]["kwargs"]["json"]
    assert body["ids"] == ["tt1", "tt2"]
    assert body["all"] is False
    assert body["collection"] == "libraryItem"


def test_datastore_get_error_raises_api_error():
    api = make_api()
    api.session = FakeSession(
        responses=[_ok({"error": {"message": "Unauthorized", "code": 1}})]
    )
    with pytest.raises(ApiError):
        api.datastore_get("bad-token")


# --- helpers -----------------------------------------------------------


def _ok(json_body):
    class _Resp:
        status_code = 200
        ok = True

        def raise_for_status(self):
            pass

        def json(self):
            return json_body

    return _Resp()


def _http_error(status_code):
    class _Resp:
        ok = False

        def __init__(self):
            self.status_code = status_code

        def raise_for_status(self):
            import requests

            raise requests.exceptions.HTTPError("%s error" % self.status_code, response=self)

        def json(self):
            return {}

    return _Resp()
