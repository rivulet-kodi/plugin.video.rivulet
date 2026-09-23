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

    def set_builtin_addon(self, builtin_id, transport_url, manifest):
        self.set_calls.append((builtin_id, transport_url, manifest))

    def remove_builtin_addon(self, builtin_id):
        self.remove_calls.append(builtin_id)


# --- manifest_url / bridge_script_path / run_script_command -----------------


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


def test_apply_active_launches_once_and_syncs_store():
    store = _FakeStore()
    launches = []
    supervisor = s4me.BridgeSupervisor("/addon/root")

    supervisor.apply(True, 11480, lambda: True, launches.append, store)

    assert launches == [s4me.run_script_command("/addon/root", 11480)]
    assert store.set_calls == [("s4me", s4me.manifest_url(11480), s4me.MANIFEST)]


def test_apply_active_second_call_same_port_does_not_relaunch_but_resyncs_store():
    store = _FakeStore()
    launches = []
    supervisor = s4me.BridgeSupervisor("/addon/root")

    supervisor.apply(True, 11480, lambda: True, launches.append, store)
    supervisor.apply(True, 11480, lambda: True, launches.append, store)

    assert launches == [s4me.run_script_command("/addon/root", 11480)]
    assert len(store.set_calls) == 2


def test_apply_port_change_while_active_relaunches_and_repoints_store():
    store = _FakeStore()
    launches = []
    supervisor = s4me.BridgeSupervisor("/addon/root")

    supervisor.apply(True, 11480, lambda: True, launches.append, store)
    supervisor.apply(True, 11481, lambda: True, launches.append, store)

    assert launches == [
        s4me.run_script_command("/addon/root", 11480),
        s4me.run_script_command("/addon/root", 11481),
    ]
    assert store.set_calls[-1] == ("s4me", s4me.manifest_url(11481), s4me.MANIFEST)


def test_apply_disabling_after_active_removes_and_relaunches_if_reenabled():
    store = _FakeStore()
    launches = []
    supervisor = s4me.BridgeSupervisor("/addon/root")

    supervisor.apply(True, 11480, lambda: True, launches.append, store)
    supervisor.apply(False, 11480, lambda: True, launches.append, store)
    supervisor.apply(True, 11480, lambda: True, launches.append, store)

    assert launches == [
        s4me.run_script_command("/addon/root", 11480),
        s4me.run_script_command("/addon/root", 11480),
    ]
    assert store.remove_calls == ["s4me"]
