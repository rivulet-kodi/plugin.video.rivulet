"""Shared pytest fixtures for the pure-Python (non-xbmc) layer.

No test in this suite may touch the network. Every HTTP-capable module under
test (``lib.stremio.addons``, ``lib.stremio.server``, ``lib.stremio.api``)
either binds ``requests`` at module scope or lazily does ``import requests``
inside a method - either way it ends up looking up attributes on the single
``requests`` module object cached in ``sys.modules``. Patching that object's
``get``/``post``/``Session`` directly (rather than patching each
module-under-test's own namespace) therefore works uniformly regardless of
which import style a given module uses.
"""
import socket
import sys
from pathlib import Path

import pytest
import requests

# --- sys.path bootstrap ----------------------------------------------------
# tests/ lives at the repo root alongside lib/, default.py, service.py.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# --- hard network block (defense in depth) ----------------------------------
@pytest.fixture(autouse=True)
def _block_real_network(monkeypatch):
    """Fail loudly if any code path tries to open a real socket."""

    def _guard(*args, **kwargs):
        raise AssertionError("real network access attempted in a unit test")

    monkeypatch.setattr(socket.socket, "connect", _guard)
    monkeypatch.setattr(socket, "create_connection", _guard)


# --- fake requests plumbing --------------------------------------------------
class FakeResponse:
    """Stand-in for requests.Response."""

    def __init__(self, json_data=None, status_code=200, text=""):
        self._json = {} if json_data is None else json_data
        self.status_code = status_code
        self.ok = 200 <= status_code < 400
        self.text = text or ""

    def json(self):
        return self._json

    def raise_for_status(self):
        if not self.ok:
            raise requests.exceptions.HTTPError(
                "%s error" % self.status_code, response=self
            )


class FakeSession:
    """Stand-in for a requests.Session instance (AddonClient.session)."""

    def __init__(self, responses=None, exc=None):
        self.calls = []
        self._responses = list(responses or [])
        self._exc = exc

    def get(self, url, **kwargs):
        self.calls.append({"url": url, "kwargs": kwargs})
        if self._exc is not None:
            raise self._exc
        if not self._responses:
            raise AssertionError("FakeSession: no queued response for %s" % url)
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeRequests:
    """Patches the real `requests` module's get/post at the call site.

    Covers module-level `import requests` AND per-call lazy `import requests`
    since both resolve to the same cached module object in sys.modules.
    """

    def __init__(self, monkeypatch):
        self.calls = []
        self._get_queue = []
        self._post_queue = []
        monkeypatch.setattr(requests, "get", self._fake_get)
        monkeypatch.setattr(requests, "post", self._fake_post)

    def queue_get(self, item):
        self._get_queue.append(item)

    def queue_post(self, item):
        self._post_queue.append(item)

    def _fake_get(self, url, **kwargs):
        self.calls.append({"method": "GET", "url": url, "kwargs": kwargs})
        return self._pop(self._get_queue, url)

    def _fake_post(self, url, **kwargs):
        self.calls.append({"method": "POST", "url": url, "kwargs": kwargs})
        return self._pop(self._post_queue, url)

    @staticmethod
    def _pop(queue, url):
        if not queue:
            raise AssertionError("FakeRequests: no queued response for %s" % url)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def fake_requests(monkeypatch):
    """Patches requests.get/requests.post globally; returns the controller."""
    return FakeRequests(monkeypatch)
