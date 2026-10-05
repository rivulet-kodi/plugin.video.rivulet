# SPDX-FileCopyrightText: 2026 M0Rf30
# SPDX-License-Identifier: MIT

"""Tests for lib.s4me: the Kodi-independent S4Me bridge glue (manifest/
descriptor shape, RunScript() command construction, and BridgeSupervisor's
launch/store-sync state machine)."""
import pytest

from lib import s4me


class _FakeStore:
    """Minimal in-memory stand-in for lib.store.Store's builtin-addon
    methods -- records every call so tests can assert exactly what
    BridgeSupervisor asked for, without touching disk."""

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


class _FakeClock:
    """Controllable monotonic clock for backoff tests."""

    def __init__(self, now=0.0):
        self._now = now

    def __call__(self):
        return self._now

    def advance(self, seconds):
        self._now += seconds


# --- manifest_url / bridge_script_path / run_script_command -----------------


@pytest.mark.parametrize("ui, audio, expected", [
    ("it", None, True),
    ("IT", None, True),
    ("en", "Italian", True),
    ("en", "italiano", True),
    ("en", "ita", True),
    ("en", "original", False),
    ("en", "English", False),
    (None, None, False),
    ("", "", False),
    (123, ["it"], False),  # non-strings never match
])
def test_is_italian_user(ui, audio, expected):
    assert s4me.is_italian_user(ui, audio) is expected


def test_manifest_url_uses_localhost_and_port():
    assert s4me.manifest_url(11480) == "http://127.0.0.1:11480/manifest.json"


def test_bridge_script_path_joins_addon_root():
    path = s4me.bridge_script_path("/addon/root")
    assert path.endswith("resources/s4me_bridge/bridge.py".replace("/", __import__("os").sep))
    assert path.startswith("/addon/root")


def test_run_script_command_has_single_comma_separated_arg():
    command = s4me.run_script_command("/addon/root", 11480)
    assert command.startswith("RunScript(")
    assert command.endswith(",11480)")
    # exactly one comma inside the parens (path, port) -- a second comma
    # would fragment into an extra positional RunScript() arg.
    inner = command[len("RunScript("):-1]
    assert inner.count(",") == 1


# --- descriptor_for -----------------------------------------------------


def test_descriptor_for_is_protected_unofficial_and_flagged_builtin():
    descriptor = s4me.descriptor_for(11480)
    assert descriptor["transportUrl"] == "http://127.0.0.1:11480/manifest.json"
    assert descriptor["manifest"] == s4me.MANIFEST
    assert descriptor["flags"] == {"official": False, "protected": True, "builtin": "s4me"}


def test_descriptor_for_different_ports_differ_only_in_transport_url():
    a = s4me.descriptor_for(11480)
    b = s4me.descriptor_for(11481)
    assert a["transportUrl"] != b["transportUrl"]
    assert a["manifest"] == b["manifest"]
    assert a["flags"] == b["flags"]


# --- parse_channels_setting -----------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    (None, None),
    ("", None),
    ("   ", None),
    ("vvvvid", ("vvvvid",)),
    ("vvvvid,eurostreaming", ("vvvvid", "eurostreaming")),
    (" vvvvid , eurostreaming ,, ", ("vvvvid", "eurostreaming")),
])
def test_parse_channels_setting(raw, expected):
    assert s4me.parse_channels_setting(raw) == expected


# --- BridgeSupervisor -----------------------------------------------------


def test_apply_inactive_when_disabled_removes_and_never_launches():
    store = _FakeStore()
    launches = []
    supervisor = s4me.BridgeSupervisor("/addon/root")

    supervisor.apply(False, 11480, lambda: True, launches.append, store)

    assert launches == []
    assert store.set_calls == []
    assert store.remove_calls == ["s4me"]


def test_apply_inactive_when_addon_missing_even_if_enabled():
    store = _FakeStore()
    launches = []
    supervisor = s4me.BridgeSupervisor("/addon/root")

    supervisor.apply(True, 11480, lambda: False, launches.append, store)

    assert launches == []
    assert store.remove_calls == ["s4me"]


def test_apply_launch_does_not_publish_until_probe_succeeds():
    store = _FakeStore()
    launches = []
    supervisor = s4me.BridgeSupervisor("/addon/root", clock=_FakeClock())

    supervisor.apply(True, 11480, lambda: True, launches.append, store, probe_fn=lambda port: None)

    assert launches == [s4me.run_script_command("/addon/root", 11480)]
    assert store.set_calls == []


def test_apply_publishes_once_probe_confirms_manifest_answers():
    store = _FakeStore()
    launches = []
    supervisor = s4me.BridgeSupervisor("/addon/root", clock=_FakeClock())

    supervisor.apply(True, 11480, lambda: True, launches.append, store, probe_fn=lambda port: None)
    supervisor.apply(True, 11480, lambda: True, launches.append, store, probe_fn=lambda port: s4me.MANIFEST["version"])

    assert launches == [s4me.run_script_command("/addon/root", 11480)]
    assert store.set_calls == [("s4me", s4me.manifest_url(11480), s4me.MANIFEST)]


def test_apply_active_second_call_same_port_does_not_relaunch_but_resyncs_store():
    store = _FakeStore()
    launches = []
    supervisor = s4me.BridgeSupervisor("/addon/root", clock=_FakeClock())

    supervisor.apply(True, 11480, lambda: True, launches.append, store, probe_fn=lambda port: s4me.MANIFEST["version"])
    supervisor.apply(True, 11480, lambda: True, launches.append, store, probe_fn=lambda port: s4me.MANIFEST["version"])
    supervisor.apply(True, 11480, lambda: True, launches.append, store, probe_fn=lambda port: s4me.MANIFEST["version"])

    assert launches == [s4me.run_script_command("/addon/root", 11480)]
    assert len(store.set_calls) == 2


def test_apply_marks_offline_immediately_when_bridge_stops_answering():
    store = _FakeStore()
    launches = []
    clock = _FakeClock()
    supervisor = s4me.BridgeSupervisor("/addon/root", clock=clock)

    supervisor.apply(True, 11480, lambda: True, launches.append, store, probe_fn=lambda port: s4me.MANIFEST["version"])
    supervisor.apply(True, 11480, lambda: True, launches.append, store, probe_fn=lambda port: s4me.MANIFEST["version"])
    assert store.set_calls  # published once

    # Still within the relaunch backoff window: marked offline, but not
    # relaunched yet, and NOT removed -- a transient blip must preserve the
    # user's flags.disabled choice and the entry's position in addons.json.
    supervisor.apply(True, 11480, lambda: True, launches.append, store, probe_fn=lambda port: None)

    assert store.offline_calls == ["s4me", "s4me"]  # first activation + this stop
    assert store.remove_calls == []
    assert launches == [s4me.run_script_command("/addon/root", 11480)]


def test_apply_relaunches_dead_bridge_only_after_backoff_elapses():
    store = _FakeStore()
    launches = []
    clock = _FakeClock()
    supervisor = s4me.BridgeSupervisor("/addon/root", clock=clock)

    supervisor.apply(True, 11480, lambda: True, launches.append, store, probe_fn=lambda port: None)
    assert launches == [s4me.run_script_command("/addon/root", 11480)]

    # Backoff has not elapsed yet: no second launch attempt.
    supervisor.apply(True, 11480, lambda: True, launches.append, store, probe_fn=lambda port: None)
    assert launches == [s4me.run_script_command("/addon/root", 11480)]

    clock.advance(s4me.RELAUNCH_BACKOFF_SECONDS + 1)
    supervisor.apply(True, 11480, lambda: True, launches.append, store, probe_fn=lambda port: None)
    assert launches == [
        s4me.run_script_command("/addon/root", 11480),
        s4me.run_script_command("/addon/root", 11480),
    ]


# --- BridgeSupervisor: stale bridge (answers, wrong/missing version) -------


def test_apply_stale_version_shuts_down_then_relaunches_once_port_is_free():
    """A stale answer only POSTs /shutdown; the relaunch waits for the next
    tick where the port has gone quiet -- launching in the same tick raced
    the old bridge for the port and failed to bind (observed live)."""
    store = _FakeStore()
    launches = []
    shutdowns = []
    supervisor = s4me.BridgeSupervisor("/addon/root", clock=_FakeClock())

    # First call: fresh activation, no probe consulted yet.
    supervisor.apply(True, 11480, lambda: True, launches.append, store)
    assert launches == [s4me.run_script_command("/addon/root", 11480)]

    # The port answers with an old build's version -- a leftover bridge
    # process that survived a Rivulet update: shut it down, launch nothing.
    supervisor.apply(
        True, 11480, lambda: True, launches.append, store,
        probe_fn=lambda port: "1.0.0",
        shutdown_fn=shutdowns.append,
    )
    assert shutdowns == [11480]
    assert store.set_calls == []
    assert len(launches) == 1

    # Next tick: the old bridge is gone, so the relaunch happens at once,
    # not after RELAUNCH_BACKOFF_SECONDS.
    supervisor.apply(True, 11480, lambda: True, launches.append, store, probe_fn=lambda port: None)
    assert launches == [
        s4me.run_script_command("/addon/root", 11480),
        s4me.run_script_command("/addon/root", 11480),
    ]


def test_apply_stale_version_retries_shutdown_after_short_delay_not_full_backoff():
    store = _FakeStore()
    shutdowns = []
    clock = _FakeClock()
    supervisor = s4me.BridgeSupervisor("/addon/root", clock=clock)

    supervisor.apply(True, 11480, lambda: True, lambda cmd: None, store)
    supervisor.apply(
        True, 11480, lambda: True, lambda cmd: None, store,
        probe_fn=lambda port: "1.0.0", shutdown_fn=shutdowns.append,
    )
    # Inside the short delay: no second shutdown yet.
    supervisor.apply(
        True, 11480, lambda: True, lambda cmd: None, store,
        probe_fn=lambda port: "1.0.0", shutdown_fn=shutdowns.append,
    )
    assert shutdowns == [11480]

    clock.advance(s4me.STALE_RELAUNCH_DELAY_SECONDS + 0.5)
    supervisor.apply(
        True, 11480, lambda: True, lambda cmd: None, store,
        probe_fn=lambda port: "1.0.0", shutdown_fn=shutdowns.append,
    )
    assert shutdowns == [11480, 11480]


def test_apply_missing_version_field_counts_as_stale():
    store = _FakeStore()
    launches = []
    shutdowns = []
    supervisor = s4me.BridgeSupervisor("/addon/root", clock=_FakeClock())

    supervisor.apply(True, 11480, lambda: True, launches.append, store)
    supervisor.apply(
        True, 11480, lambda: True, launches.append, store,
        probe_fn=lambda port: "",  # answers, but no usable version
        shutdown_fn=shutdowns.append,
    )

    assert shutdowns == [11480]
    assert store.set_calls == []


def test_apply_repeated_stale_backs_off_and_logs_once():
    """A bridge that keeps answering stale (e.g. a pre-1.1.0 build with no
    /shutdown route) gets retried on the full backoff, logged once, and is
    never raced with a launch while it still holds the port."""
    store = _FakeStore()
    launches = []
    shutdowns = []
    logs = []
    clock = _FakeClock()
    supervisor = s4me.BridgeSupervisor("/addon/root", clock=clock)

    def stale_tick():
        supervisor.apply(
            True, 11480, lambda: True, launches.append, store,
            probe_fn=lambda port: "1.0.0", shutdown_fn=shutdowns.append, log_fn=logs.append,
        )

    supervisor.apply(True, 11480, lambda: True, launches.append, store)
    stale_tick()
    assert (len(shutdowns), logs) == (1, [])

    clock.advance(s4me.STALE_RELAUNCH_DELAY_SECONDS + 0.5)
    stale_tick()
    assert len(shutdowns) == 2
    assert len(logs) == 1

    # Within the now-longer backoff window: nothing further.
    clock.advance(s4me.STALE_RELAUNCH_DELAY_SECONDS + 0.5)
    stale_tick()
    assert len(shutdowns) == 2

    clock.advance(s4me.RELAUNCH_BACKOFF_SECONDS + 1)
    stale_tick()
    assert len(shutdowns) == 3
    assert len(logs) == 1  # logged only once
    assert len(launches) == 1  # never launched while the port was held


def test_apply_recovering_to_matching_version_clears_stale_state_and_publishes():
    store = _FakeStore()
    launches = []
    clock = _FakeClock()
    supervisor = s4me.BridgeSupervisor("/addon/root", clock=clock)

    supervisor.apply(True, 11480, lambda: True, launches.append, store)
    supervisor.apply(
        True, 11480, lambda: True, launches.append, store,
        probe_fn=lambda port: "1.0.0", shutdown_fn=lambda port: None,
    )
    clock.advance(s4me.STALE_RELAUNCH_DELAY_SECONDS + 0.5)
    supervisor.apply(
        True, 11480, lambda: True, launches.append, store,
        probe_fn=lambda port: s4me.MANIFEST["version"], shutdown_fn=lambda port: None,
    )

    assert store.set_calls == [("s4me", s4me.manifest_url(11480), s4me.MANIFEST)]


def test_apply_stale_shutdown_fn_raising_does_not_block_relaunch():
    """An old bridge build predating the /shutdown route (or one whose
    connection drops mid-request) must never break the sequence: once the
    port goes quiet, the fresh bridge still launches."""
    store = _FakeStore()
    launches = []

    def _raising_shutdown(port):
        raise OSError("connection refused")

    supervisor = s4me.BridgeSupervisor("/addon/root", clock=_FakeClock())

    supervisor.apply(True, 11480, lambda: True, launches.append, store)
    supervisor.apply(
        True, 11480, lambda: True, launches.append, store,
        probe_fn=lambda port: "1.0.0", shutdown_fn=_raising_shutdown,
    )
    supervisor.apply(True, 11480, lambda: True, launches.append, store, probe_fn=lambda port: None)

    assert launches == [
        s4me.run_script_command("/addon/root", 11480),
        s4me.run_script_command("/addon/root", 11480),
    ]


def test_apply_port_change_while_active_relaunches_and_repoints_store_once_ready():
    store = _FakeStore()
    launches = []
    shutdowns = []
    supervisor = s4me.BridgeSupervisor("/addon/root", clock=_FakeClock())

    supervisor.apply(
        True, 11480, lambda: True, launches.append, store,
        probe_fn=lambda port: s4me.MANIFEST["version"], shutdown_fn=shutdowns.append,
    )
    supervisor.apply(
        True, 11480, lambda: True, launches.append, store,
        probe_fn=lambda port: s4me.MANIFEST["version"], shutdown_fn=shutdowns.append,
    )
    assert store.set_calls[-1] == ("s4me", s4me.manifest_url(11480), s4me.MANIFEST)
    assert shutdowns == []  # no port change yet: the healthy 11480 bridge is left alone

    # Port changes: the stale descriptor for the old port is marked
    # offline right away (not removed), and the new port is not
    # published until it answers in turn. The bridge still listening on
    # the OLD port must be told to shut down -- otherwise it is orphaned
    # (nothing points at it, and Kodi's own /shutdown fan-out at exit
    # only reaches the CURRENT launched_port) -- see the class docstring.
    supervisor.apply(
        True, 11481, lambda: True, launches.append, store,
        probe_fn=lambda port: None, shutdown_fn=shutdowns.append,
    )
    assert shutdowns == [11480]
    assert store.offline_calls[-1] == "s4me"
    assert store.remove_calls == []
    assert store.set_calls[-1] == ("s4me", s4me.manifest_url(11480), s4me.MANIFEST)

    supervisor.apply(
        True, 11481, lambda: True, launches.append, store,
        probe_fn=lambda port: s4me.MANIFEST["version"], shutdown_fn=shutdowns.append,
    )

    assert launches == [
        s4me.run_script_command("/addon/root", 11480),
        s4me.run_script_command("/addon/root", 11481),
    ]
    assert store.set_calls[-1] == ("s4me", s4me.manifest_url(11481), s4me.MANIFEST)
    # No further shutdown once the new port is up and unchanged.
    assert shutdowns == [11480]


def test_apply_port_change_shutdown_fn_raising_does_not_block_relaunch():
    """Same never-block contract as the STALE-eviction shutdown_fn call:
    an unreachable/old-build bridge on the previous port must never stop
    the new port's launch."""
    store = _FakeStore()
    launches = []
    supervisor = s4me.BridgeSupervisor("/addon/root", clock=_FakeClock())

    supervisor.apply(
        True, 11480, lambda: True, launches.append, store,
        probe_fn=lambda port: s4me.MANIFEST["version"], shutdown_fn=lambda port: None,
    )

    def _raising_shutdown(port):
        raise ConnectionRefusedError("refused")

    supervisor.apply(
        True, 11481, lambda: True, launches.append, store,
        probe_fn=lambda port: None, shutdown_fn=_raising_shutdown,
    )

    assert launches == [
        s4me.run_script_command("/addon/root", 11480),
        s4me.run_script_command("/addon/root", 11481),
    ]


def test_apply_disabling_after_active_removes_and_relaunches_if_reenabled():
    store = _FakeStore()
    launches = []
    shutdowns = []
    supervisor = s4me.BridgeSupervisor("/addon/root", clock=_FakeClock())

    supervisor.apply(
        True, 11480, lambda: True, launches.append, store,
        probe_fn=lambda port: s4me.MANIFEST["version"], shutdown_fn=shutdowns.append,
    )
    supervisor.apply(
        True, 11480, lambda: True, launches.append, store,
        probe_fn=lambda port: s4me.MANIFEST["version"], shutdown_fn=shutdowns.append,
    )
    # Deactivation (user turns off s4me_enable, or _detect_italian_user()
    # flips) must shut down the still-running bridge on the old port too --
    # RunScript() itself hands back no handle to stop it, so this is the
    # ONLY way it does not keep listening with nothing pointing at it.
    supervisor.apply(
        False, 11480, lambda: True, launches.append, store,
        probe_fn=lambda port: s4me.MANIFEST["version"], shutdown_fn=shutdowns.append,
    )
    assert shutdowns == [11480]
    supervisor.apply(
        True, 11480, lambda: True, launches.append, store,
        probe_fn=lambda port: s4me.MANIFEST["version"], shutdown_fn=shutdowns.append,
    )

    assert launches == [
        s4me.run_script_command("/addon/root", 11480),
        s4me.run_script_command("/addon/root", 11480),
    ]
    assert store.remove_calls[-1] == "s4me"


def test_apply_disabling_never_active_skips_shutdown():
    """No bridge was ever launched (`_launched_port` still `None`): there
    is nothing to shut down, and `shutdown_fn` must not be called with a
    bogus port."""
    store = _FakeStore()
    shutdowns = []
    supervisor = s4me.BridgeSupervisor("/addon/root", clock=_FakeClock())

    supervisor.apply(False, 11480, lambda: True, lambda cmd: None, store, shutdown_fn=shutdowns.append)

    assert shutdowns == []
    assert store.remove_calls == ["s4me"]



# --- probe_manifest ---------------------------------------------------------


def test_probe_manifest_true_on_successful_response(monkeypatch):
    import urllib.request

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Resp())

    assert s4me.probe_manifest(11480) is True


def test_probe_manifest_true_on_http_error_status(monkeypatch):
    import urllib.error
    import urllib.request

    def _raise(*a, **k):
        raise urllib.error.HTTPError("url", 404, "not found", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", _raise)

    assert s4me.probe_manifest(11480) is True


def test_probe_manifest_false_on_connection_failure(monkeypatch):
    import urllib.request

    def _raise(*a, **k):
        raise ConnectionRefusedError("refused")

    monkeypatch.setattr(urllib.request, "urlopen", _raise)

    assert s4me.probe_manifest(11480) is False


# --- probe_manifest_version ---------------------------------------------------


class _JsonResp:
    def __init__(self, body):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


def test_probe_manifest_version_returns_the_served_version(monkeypatch):
    import json
    import urllib.request

    body = json.dumps({"version": "1.1.0"}).encode("utf-8")
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _JsonResp(body))

    assert s4me.probe_manifest_version(11480) == "1.1.0"


def test_probe_manifest_version_empty_string_on_invalid_json(monkeypatch):
    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _JsonResp(b"not json"))

    assert s4me.probe_manifest_version(11480) == ""


def test_probe_manifest_version_empty_string_on_missing_version_field(monkeypatch):
    import json
    import urllib.request

    body = json.dumps({"id": "org.rivulet.s4me"}).encode("utf-8")
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _JsonResp(body))

    assert s4me.probe_manifest_version(11480) == ""


def test_probe_manifest_version_empty_string_on_non_object_json(monkeypatch):
    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _JsonResp(b"[1, 2, 3]"))

    assert s4me.probe_manifest_version(11480) == ""


def test_probe_manifest_version_reads_body_off_http_error_responses(monkeypatch):
    import io
    import json
    import urllib.error
    import urllib.request

    body = json.dumps({"version": "1.0.0"}).encode("utf-8")

    def _raise(*a, **k):
        raise urllib.error.HTTPError("url", 404, "not found", {}, io.BytesIO(body))

    monkeypatch.setattr(urllib.request, "urlopen", _raise)

    assert s4me.probe_manifest_version(11480) == "1.0.0"


def test_probe_manifest_version_closes_http_error_response(monkeypatch):
    """`exc.read()` alone leaves the underlying socket open until GC -- see
    lib/s4me.py's own note that a leaked one raises ResourceWarning and
    that probe_fn runs on every supervisor tick."""
    import io
    import json
    import urllib.error
    import urllib.request

    body = json.dumps({"version": "1.0.0"}).encode("utf-8")
    closed = []

    class _ClosingBytesIO(io.BytesIO):
        def close(self):
            closed.append(True)
            super().close()

    def _raise(*a, **k):
        raise urllib.error.HTTPError("url", 404, "not found", {}, _ClosingBytesIO(body))

    monkeypatch.setattr(urllib.request, "urlopen", _raise)

    assert s4me.probe_manifest_version(11480) == "1.0.0"
    assert closed == [True]


def test_probe_manifest_version_none_on_connection_failure(monkeypatch):
    import urllib.request

    def _raise(*a, **k):
        raise ConnectionRefusedError("refused")

    monkeypatch.setattr(urllib.request, "urlopen", _raise)

    assert s4me.probe_manifest_version(11480) is None


# --- shutdown_bridge ---------------------------------------------------------


def test_shutdown_bridge_posts_to_shutdown_url(monkeypatch):
    import urllib.request

    captured = {}

    def _urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["header"] = next(
            (v for k, v in request.header_items() if k.lower() == s4me.SHUTDOWN_HEADER.lower()), None,
        )
        return _JsonResp(b"")

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)

    s4me.shutdown_bridge(11480)

    assert captured["url"] == s4me.shutdown_url(11480)
    assert captured["method"] == "POST"
    assert captured["header"] == "1"


def test_shutdown_bridge_swallows_missing_route_and_connection_failure(monkeypatch):
    import urllib.error
    import urllib.request

    monkeypatch.setattr(
        urllib.request, "urlopen",
        lambda *a, **k: (_ for _ in ()).throw(urllib.error.HTTPError("url", 404, "not found", {}, None)),
    )
    s4me.shutdown_bridge(11480)  # must not raise

    monkeypatch.setattr(
        urllib.request, "urlopen",
        lambda *a, **k: (_ for _ in ()).throw(ConnectionRefusedError("refused")),
    )
    s4me.shutdown_bridge(11480)  # must not raise


def test_shutdown_bridge_closes_http_error_response(monkeypatch):
    """Same leaked-socket concern as probe_manifest_version() -- an old
    bridge build without the /shutdown route answers 404 through
    HTTPError, and its fp must be closed rather than left for GC."""
    import io
    import urllib.error
    import urllib.request

    closed = []

    class _ClosingBytesIO(io.BytesIO):
        def close(self):
            closed.append(True)
            super().close()

    monkeypatch.setattr(
        urllib.request, "urlopen",
        lambda *a, **k: (_ for _ in ()).throw(
            urllib.error.HTTPError("url", 404, "not found", {}, _ClosingBytesIO(b"")),
        ),
    )
    s4me.shutdown_bridge(11480)  # must not raise

    assert closed == [True]


def test_launched_port_tracks_launch():
    sup = s4me.BridgeSupervisor("/addon")
    assert sup.launched_port() is None
    sup._launch(11480, lambda cmd: None)
    assert sup.launched_port() == 11480
