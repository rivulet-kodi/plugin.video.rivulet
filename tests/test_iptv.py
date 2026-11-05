# SPDX-FileCopyrightText: 2026 M0Rf30
# SPDX-License-Identifier: MIT

"""Tests for lib.iptv: manifest fetching and IptvRegistrar's lifecycle of the
`rivulet-iptv` builtin addon entry."""
import io
import json
import urllib.error
import urllib.request

from lib import iptv

SERVER = "http://127.0.0.1:11470"
URL = SERVER + "/iptv/manifest.json"
PLAYLISTS = "https://raw.githubusercontent.com/Free-TV/IPTV/master/playlist.m3u8"
MANIFEST = {"id": iptv.MANIFEST_ID, "version": "0.1.0", "name": "IPTV", "types": ["tv"]}


class _FakeStore:
    """Records the builtin-addon calls IptvRegistrar makes."""

    def __init__(self):
        self.set_calls = []
        self.remove_calls = []
        self.offline_calls = []

    def set_builtin_addon(self, builtin_id, transport_url, manifest):
        self.set_calls.append((builtin_id, transport_url, manifest))

    def remove_builtin_addon(self, builtin_id):
        self.remove_calls.append(builtin_id)

    def set_builtin_addon_offline(self, builtin_id):
        self.offline_calls.append(builtin_id)


class _Engine:
    """Scriptable `fetch_fn`: `answer` is a manifest dict or a reason string."""

    def __init__(self, answer):
        self.answer = answer
        self.urls = []

    def __call__(self, server_url):
        self.urls.append(server_url)
        if isinstance(self.answer, dict):
            return self.answer, None
        return None, self.answer


def _apply(registrar, store, engine, playlists=PLAYLISTS, serving=True, logs=None):
    registrar.apply(playlists, SERVER, serving, store, fetch_fn=engine, log_fn=logs.append if logs is not None else None)


# --- helpers ------------------------------------------------------------------


def test_parse_playlists_splits_trims_and_drops_empties():
    assert iptv.parse_playlists(" http://a/x.m3u , ,http://b/y.m3u,") == ["http://a/x.m3u", "http://b/y.m3u"]
    assert iptv.parse_playlists("") == []
    assert iptv.parse_playlists(" , ") == []
    assert iptv.parse_playlists(None) == []


def test_manifest_url_ignores_trailing_slash():
    assert iptv.manifest_url("http://127.0.0.1:11470/") == URL
    assert iptv.manifest_url(SERVER) == URL


# --- fetch_manifest -----------------------------------------------------------


class _Resp:
    def __init__(self, body):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


def _serve(monkeypatch, body):
    seen = []

    def urlopen(request, timeout=None):
        seen.append((request, timeout))
        return _Resp(body)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return seen


def test_fetch_manifest_returns_the_iptv_manifest(monkeypatch):
    seen = _serve(monkeypatch, json.dumps(MANIFEST).encode("utf-8"))
    assert iptv.fetch_manifest(SERVER) == (MANIFEST, None)
    request, timeout = seen[0]
    assert request.full_url == URL
    assert request.get_header("User-agent") == iptv.USER_AGENT
    assert timeout == iptv.PROBE_TIMEOUT_SECONDS


def test_fetch_manifest_rejects_other_ids_and_non_manifests(monkeypatch):
    for body in (b'{"id": "com.other.addon"}', b'{"name": "no id"}', b"[1, 2]", b"not json", b""):
        _serve(monkeypatch, body)
        assert iptv.fetch_manifest(SERVER) == (None, "unexpected response")


def test_fetch_manifest_http_error_is_a_reason_and_closes_the_response(monkeypatch):
    closed = []

    class _Body(io.BytesIO):
        def close(self):
            closed.append(True)
            super().close()

    def urlopen(*a, **k):
        raise urllib.error.HTTPError(URL, 404, "not found", {}, _Body(b"404 page not found"))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    assert iptv.fetch_manifest(SERVER) == (None, "HTTP 404")
    assert closed


def test_fetch_manifest_tolerates_an_http_error_without_a_body(monkeypatch):
    def urlopen(*a, **k):
        raise urllib.error.HTTPError(URL, 404, "not found", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    assert iptv.fetch_manifest(SERVER) == (None, "HTTP 404")


def test_fetch_manifest_unreachable(monkeypatch):
    def refused(*a, **k):
        raise urllib.error.URLError(ConnectionRefusedError())

    monkeypatch.setattr(urllib.request, "urlopen", refused)
    assert iptv.fetch_manifest(SERVER) == (None, "unreachable")

    def timed_out(*a, **k):
        raise TimeoutError()

    monkeypatch.setattr(urllib.request, "urlopen", timed_out)
    assert iptv.fetch_manifest(SERVER) == (None, "unreachable")


def test_fetch_manifest_schemeless_server_url_is_unreachable():
    # Request() itself raises ValueError for a URL without a scheme.
    assert iptv.fetch_manifest("127.0.0.1") == (None, "unreachable")


def test_fetch_manifest_truncated_body_is_unreachable(monkeypatch):
    import http.client

    class _Truncated(_Resp):
        def read(self):
            raise http.client.IncompleteRead(b'{"id"', 100)

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Truncated(b""))
    assert iptv.fetch_manifest(SERVER) == (None, "unreachable")


# --- IptvRegistrar ------------------------------------------------------------


class _FailingStore(_FakeStore):
    """Raises on the first remove/offline write, succeeds afterwards."""

    def __init__(self):
        super().__init__()
        self.fail = {"remove", "offline"}

    def remove_builtin_addon(self, builtin_id):
        if "remove" in self.fail:
            self.fail.discard("remove")
            raise OSError("addons.json locked")
        super().remove_builtin_addon(builtin_id)

    def set_builtin_addon_offline(self, builtin_id):
        if "offline" in self.fail:
            self.fail.discard("offline")
            raise OSError("addons.json locked")
        super().set_builtin_addon_offline(builtin_id)


def test_a_failed_store_write_is_retried_next_tick():
    store, registrar = _FailingStore(), iptv.IptvRegistrar()
    _apply(registrar, store, _Engine(MANIFEST))
    for kwargs, calls in (({"playlists": ""}, store.remove_calls), ({}, store.offline_calls)):
        engine = _Engine("unreachable")
        try:
            _apply(registrar, store, engine, **kwargs)
        except OSError:
            pass
        assert calls == []
        _apply(registrar, store, engine, **kwargs)
        assert calls == ["rivulet-iptv"]


def test_publishes_when_engine_serves_the_manifest():
    store, registrar, logs = _FakeStore(), iptv.IptvRegistrar(), []
    engine = _Engine(MANIFEST)
    _apply(registrar, store, engine, logs=logs)
    assert store.set_calls == [("rivulet-iptv", URL, dict(MANIFEST, **{iptv.STAMP_FIELD: iptv.playlists_stamp(PLAYLISTS)}))]
    assert store.remove_calls == [] and store.offline_calls == []
    assert engine.urls == [SERVER]
    assert len(logs) == 1


def test_republishes_every_tick_but_logs_once():
    """set_builtin_addon is a no-op write while the entry matches, so the
    registrar calls it each tick (self-healing) without log spam."""
    store, registrar, logs = _FakeStore(), iptv.IptvRegistrar(), []
    engine = _Engine(MANIFEST)
    for _ in range(3):
        _apply(registrar, store, engine, logs=logs)
    assert len(store.set_calls) == 3
    assert len(logs) == 1


def test_follows_a_changed_manifest():
    store, registrar = _FakeStore(), iptv.IptvRegistrar()
    engine = _Engine(MANIFEST)
    _apply(registrar, store, engine)
    changed = dict(MANIFEST, catalogs=[{"type": "tv", "id": "iptv0", "extra": [{"name": "genre", "options": ["News"]}]}])
    engine.answer = changed
    _apply(registrar, store, engine)
    assert store.set_calls[-1] == ("rivulet-iptv", URL, dict(changed, **{iptv.STAMP_FIELD: iptv.playlists_stamp(PLAYLISTS)}))


def test_stamp_follows_the_playlists_not_their_whitespace():
    assert iptv.playlists_stamp("") == "" and iptv.playlists_stamp(" , ") == ""
    assert iptv.playlists_stamp(" http://a/x.m3u ,http://b/y.m3u,") == iptv.playlists_stamp("http://a/x.m3u,http://b/y.m3u")
    assert iptv.playlists_stamp("http://a/x.m3u") != iptv.playlists_stamp("http://b/y.m3u")
    assert iptv.playlists_stamp("http://a,http://b") != iptv.playlists_stamp("http://b,http://a")
    assert len(iptv.playlists_stamp("http://a")) == 12


def test_changed_playlists_republish_with_a_new_stamp_on_the_same_catalog_ids():
    store, registrar = _FakeStore(), iptv.IptvRegistrar()
    engine = _Engine(MANIFEST)
    _apply(registrar, store, engine, playlists="http://a/x.m3u")
    _apply(registrar, store, engine, playlists="http://b/y.m3u")
    first, second = (call[2] for call in store.set_calls)
    assert first[iptv.STAMP_FIELD] != second[iptv.STAMP_FIELD]
    assert {k: v for k, v in first.items() if k != iptv.STAMP_FIELD} == {
        k: v for k, v in second.items() if k != iptv.STAMP_FIELD}
    assert engine.answer == MANIFEST  # the fetched manifest is not mutated


def test_manifest_and_published_stamps():
    assert iptv.manifest_stamp({iptv.STAMP_FIELD: "abc"}) == "abc"
    assert iptv.manifest_stamp({}) == "" and iptv.manifest_stamp(None) == ""
    assert iptv.manifest_stamp({iptv.STAMP_FIELD: 3}) == ""
    entry = {"flags": {"builtin": iptv.BUILTIN_ID}, "manifest": {iptv.STAMP_FIELD: "abc"}}
    assert iptv.published_stamp([{"flags": {}}, "junk", entry]) == "abc"
    assert iptv.published_stamp([{"flags": {}}]) is None
    assert iptv.published_stamp(None) is None


def test_empty_setting_removes_once_and_never_probes():
    store, registrar = _FakeStore(), iptv.IptvRegistrar()
    engine = _Engine(MANIFEST)
    for _ in range(3):
        _apply(registrar, store, engine, playlists="")
    assert store.remove_calls == ["rivulet-iptv"]
    assert engine.urls == [] and store.set_calls == []


def test_whitespace_only_setting_counts_as_empty():
    store, registrar = _FakeStore(), iptv.IptvRegistrar()
    _apply(registrar, store, _Engine(MANIFEST), playlists=" , ")
    assert store.remove_calls == ["rivulet-iptv"] and store.set_calls == []


def test_clearing_the_setting_removes_a_published_entry():
    store, registrar = _FakeStore(), iptv.IptvRegistrar()
    engine = _Engine(MANIFEST)
    _apply(registrar, store, engine)
    _apply(registrar, store, engine, playlists="")
    assert store.remove_calls == ["rivulet-iptv"]
    # ...and a later re-enable publishes again.
    _apply(registrar, store, engine)
    assert len(store.set_calls) == 2


def test_not_serving_removes_without_probing():
    """Embedded server disabled / external server: our env never reached
    the engine, so nothing is probed and nothing stays registered."""
    store, registrar = _FakeStore(), iptv.IptvRegistrar()
    engine = _Engine(MANIFEST)
    _apply(registrar, store, engine, serving=False)
    _apply(registrar, store, engine, serving=False)
    assert store.remove_calls == ["rivulet-iptv"]
    assert engine.urls == [] and store.set_calls == []


def test_goes_offline_when_the_engine_stops_answering_and_recovers():
    store, registrar, logs = _FakeStore(), iptv.IptvRegistrar(), []
    engine = _Engine(MANIFEST)
    _apply(registrar, store, engine, logs=logs)
    engine.answer = "unreachable"  # engine restarting after a settings change
    for _ in range(3):
        _apply(registrar, store, engine, logs=logs)
    assert store.offline_calls == ["rivulet-iptv"]
    assert store.remove_calls == []
    assert len(logs) == 1  # only the "registered" line: a restart is not news
    engine.answer = MANIFEST
    _apply(registrar, store, engine, logs=logs)
    assert len(store.set_calls) == 2
    assert len(logs) == 2


def test_a_404_is_offline_not_published_and_logged_once():
    """An engine without /iptv (older pin, or started without the env var)."""
    store, registrar, logs = _FakeStore(), iptv.IptvRegistrar(), []
    engine = _Engine("HTTP 404")
    for _ in range(3):
        _apply(registrar, store, engine, logs=logs)
    assert store.set_calls == []
    assert store.offline_calls == ["rivulet-iptv"]
    assert len(logs) == 1 and "HTTP 404" in logs[0]


def test_wrong_manifest_id_is_never_published():
    store, registrar = _FakeStore(), iptv.IptvRegistrar()
    _apply(registrar, store, _Engine("unexpected response"))
    assert store.set_calls == []
    assert store.offline_calls == ["rivulet-iptv"]


def test_first_tick_marks_a_leftover_entry_offline_until_the_engine_answers():
    store, registrar, logs = _FakeStore(), iptv.IptvRegistrar(), []
    _apply(registrar, store, _Engine("unreachable"), logs=logs)
    assert store.offline_calls == ["rivulet-iptv"]
    assert logs == []  # an engine that is simply not up yet is not logged
