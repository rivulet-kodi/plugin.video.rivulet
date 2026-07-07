"""Client for the Stremio account API (https://api.strem.io).

Pure Python -- no ``xbmc*`` imports. Every call is a POST to
``https://api.strem.io/api/<method>`` with a JSON body, mirroring
stremio-core's ``APIRequest``/``DatastoreRequest`` wire format
(src/types/api/{request,response}.rs -- verified against stremio-core's
own unit test fixtures in src/unit_tests/ctx/authenticate.rs). The server
answers with either ``{"result": ...}`` or ``{"error": {"message": ...,
"code": ...}}``.

``requests`` is imported lazily inside :meth:`StremioAPI._call` so this
module stays importable (and unit-testable) even where the dependency
isn't installed yet.
"""

API_URL = "https://api.strem.io"
DEFAULT_TIMEOUT = 15


class ApiError(Exception):
    """Raised for any failed call to the Stremio API.

    Covers both explicit ``{"error": ...}`` responses from the server and
    network-level failures (timeout, connection refused, malformed JSON),
    so callers only ever need to catch one exception type.

    ``status_code`` carries the HTTP status of the response that triggered
    ``response.raise_for_status()`` (e.g. 401/403 for an ``authKey`` that
    was invalidated server-side). It's ``None`` for connection-level
    failures (timeout, DNS, refused) and for JSON-decode errors, neither of
    which has an HTTP response to read a status from. The JSON error
    envelope's own ``code`` field is a raw, undocumented ``u64`` on the
    wire (see module docstring) and is deliberately NOT folded into
    ``status_code``/``is_auth_error`` below.
    """

    def __init__(self, message, code=None, status_code=None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code

    @property
    def is_auth_error(self):
        """True when the HTTP status says the ``authKey`` itself is no
        longer valid (401 Unauthorized / 403 Forbidden) rather than a
        transient network/server problem -- the conventional REST signal
        for "this credential is no longer valid", regardless of the JSON
        body's own (undocumented) error scheme."""
        return self.status_code in (401, 403)


class StremioAPI:
    """Thin client for the Stremio account/sync API."""

    def __init__(self, base_url=API_URL, timeout=DEFAULT_TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _call(self, method, payload):
        import requests

        url = "%s/api/%s" % (self.base_url, method)
        try:
            response = requests.post(url, json=payload, timeout=self.timeout)
            response.raise_for_status()
            data = response.json()
        except requests.exceptions.RequestException as exc:
            status_code = exc.response.status_code if exc.response is not None else None
            raise ApiError(str(exc), status_code=status_code)
        except ValueError as exc:
            raise ApiError("invalid API response: %s" % exc)

        if isinstance(data, dict) and data.get("error"):
            error = data["error"]
            if isinstance(error, dict):
                raise ApiError(error.get("message", "unknown API error"), error.get("code"))
            raise ApiError(str(error))
        if isinstance(data, dict) and "result" in data:
            return data["result"]
        return data

    def login(self, email, password):
        """Log in with email/password, returning ``{"authKey", "user"}``."""
        return self._call(
            "login",
            {
                "type": "Login",
                "email": email,
                "password": password,
                "facebook": False,
            },
        )

    def logout(self, auth_key):
        """Invalidate ``auth_key`` server-side."""
        self._call("logout", {"type": "Logout", "authKey": auth_key})

    def addon_collection_get(self, auth_key):
        """Return the user's synced addon descriptors."""
        result = self._call(
            "addonCollectionGet",
            {"type": "AddonCollectionGet", "authKey": auth_key, "update": True},
        )
        if isinstance(result, dict):
            return result.get("addons", []) or []
        return []

    def addon_collection_set(self, auth_key, addons):
        """Replace the user's synced addon collection with ``addons``."""
        self._call(
            "addonCollectionSet",
            {"type": "AddonCollectionSet", "authKey": auth_key, "addons": list(addons)},
        )

    def datastore_get(self, auth_key, collection="libraryItem", ids=None, all=True):
        """Fetch datastore records (library items) for ``collection``."""
        result = self._call(
            "datastoreGet",
            {
                "authKey": auth_key,
                "collection": collection,
                "ids": list(ids) if ids else [],
                "all": bool(all),
            },
        )
        return result if isinstance(result, list) else []
