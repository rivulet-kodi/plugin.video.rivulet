"""Protocol tests for lib.stremio.api.StremioAPI (api.strem.io).

Reference: stremio-core src/types/api/request.rs (APIRequest/AuthRequest/
DatastoreRequest) and src/types/api/fetch_api.rs (endpoint.join("api/").join(
version_path)) -> every call lands on https://api.strem.io/api/<method>.
Request/response shapes cross-checked against stremio-core auth unit tests.
No network access - `fake_requests` patches the real `requests.post`.
"""
import pytest

from lib.stremio.api import ApiError, StremioAPI

API_BASE = "https://api.strem.io"


def make_api():
    return StremioAPI()


# --- login -------------------------------------------------------------


def test_login_posts_to_api_login_path(fake_requests):
    fake_requests.queue_post(
        _ok({"result": {"authKey": "tok-123", "user": {"email": "a@b.com"}}})
    )
    api = make_api()
    result = api.login("a@b.com", "hunter2")

    assert result == {"authKey": "tok-123", "user": {"email": "a@b.com"}}
    call = fake_requests.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == API_BASE + "/api/login"


def test_login_body_shape_matches_auth_request(fake_requests):
    fake_requests.queue_post(_ok({"result": {"authKey": "t", "user": {}}}))
    api = make_api()
    api.login("a@b.com", "hunter2")

    body = fake_requests.calls[0]["kwargs"]["json"]
    assert body["type"] == "Login"
    assert body["email"] == "a@b.com"
    assert body["password"] == "hunter2"


def test_login_error_envelope_raises_api_error(fake_requests):
    fake_requests.queue_post(_ok({"error": {"message": "User not found", "code": 2}}))
    api = make_api()
    with pytest.raises(ApiError) as excinfo:
        api.login("a@b.com", "wrong")
    assert "User not found" in str(excinfo.value)


def test_login_http_error_status_raises_api_error(fake_requests):
    fake_requests.queue_post(_http_error(500))
    api = make_api()
    with pytest.raises(ApiError):
        api.login("a@b.com", "hunter2")


# --- logout ------------------------------------------------------------


def test_logout_posts_to_api_logout_with_auth_key(fake_requests):
    fake_requests.queue_post(_ok({"result": {"success": True}}))
    api = make_api()
    api.logout("tok-123")

    call = fake_requests.calls[0]
    assert call["url"] == API_BASE + "/api/logout"
    body = call["kwargs"]["json"]
    assert body["type"] == "Logout"
    assert body["authKey"] == "tok-123"


def test_logout_error_envelope_raises_api_error(fake_requests):
    fake_requests.queue_post(_ok({"error": {"message": "Invalid auth key", "code": 1}}))
    api = make_api()
    with pytest.raises(ApiError):
        api.logout("bad-token")


# --- addonCollectionGet ------------------------------------------------


def test_addon_collection_get_posts_correct_path_and_body(fake_requests):
    descriptors = [
        {"transportUrl": "https://a.example/manifest.json", "manifest": {"id": "a"}, "flags": {}}
    ]
    fake_requests.queue_post(_ok({"result": {"addons": descriptors, "lastModified": "now"}}))
    api = make_api()
    result = api.addon_collection_get("tok-123")

    assert result == descriptors
    call = fake_requests.calls[0]
    assert call["url"] == API_BASE + "/api/addonCollectionGet"
    body = call["kwargs"]["json"]
    assert body["type"] == "AddonCollectionGet"
    assert body["authKey"] == "tok-123"
    assert body["update"] is True


def test_addon_collection_get_error_raises_api_error(fake_requests):
    fake_requests.queue_post(_ok({"error": {"message": "Unauthorized", "code": 1}}))
    api = make_api()
    with pytest.raises(ApiError):
        api.addon_collection_get("bad-token")


def test_addon_collection_set_posts_correct_path_and_body(fake_requests):
    descriptors = [
        {"transportUrl": "https://a.example/manifest.json", "manifest": {"id": "a"}, "flags": {}}
    ]
    fake_requests.queue_post(_ok({"result": {"success": True}}))
    api = make_api()
    api.addon_collection_set("tok-123", descriptors)

    call = fake_requests.calls[0]
    assert call["url"] == API_BASE + "/api/addonCollectionSet"
    body = call["kwargs"]["json"]
    assert body["type"] == "AddonCollectionSet"
    assert body["authKey"] == "tok-123"
    assert body["addons"] == descriptors


# --- datastoreGet --------------------------------------------------------


def test_datastore_get_posts_correct_path_and_body_defaults(fake_requests):
    items = [{"_id": "tt1", "removed": False}]
    fake_requests.queue_post(_ok({"result": items}))
    api = make_api()
    result = api.datastore_get("tok-123")

    assert result == items
    call = fake_requests.calls[0]
    assert call["url"] == API_BASE + "/api/datastoreGet"
    body = call["kwargs"]["json"]
    # DatastoreCommand is #[serde(untagged)] -> no "type" tag on the wire
    assert "type" not in body
    assert body["authKey"] == "tok-123"
    assert body["collection"] == "libraryItem"
    assert body["all"] is True
    assert body["ids"] == []


def test_datastore_get_with_explicit_ids(fake_requests):
    fake_requests.queue_post(_ok({"result": []}))
    api = make_api()
    api.datastore_get("tok-123", collection="libraryItem", ids=["tt1", "tt2"], all=False)

    body = fake_requests.calls[0]["kwargs"]["json"]
    assert body["ids"] == ["tt1", "tt2"]
    assert body["all"] is False
    assert body["collection"] == "libraryItem"


def test_datastore_get_error_raises_api_error(fake_requests):
    fake_requests.queue_post(_ok({"error": {"message": "Unauthorized", "code": 1}}))
    api = make_api()
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
