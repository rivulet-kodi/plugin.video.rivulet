"""Tests for lib.procflags: the Windows-only subprocess kwargs that
suppress the console window Windows would otherwise allocate for a
console-subsystem child spawned from a GUI process (Kodi).

Background (issue #30): stremio-server.exe is built with Go's default
console subsystem. Spawned from kodi.exe -- a GUI-subsystem process with
no console of its own -- Windows allocates the child a brand-new console
window. A user closing that empty window sends it CTRL_CLOSE_EVENT, which
kills stremio-server.exe; lib.service_runner's supervisor then sees the
child gone and restarts it, popping a fresh window right back up ("when I
close it, it opens again"). `no_window_kwargs()` is the single source of
the `subprocess.Popen`/`subprocess.run` kwargs (`creationflags` +
`startupinfo`) that stop Windows from ever allocating that console.

This module is pure stdlib (`os`, `subprocess`) with zero project imports
and zero `xbmc*` imports, so it is exercised directly here with no Kodi
stubs at all.
"""
from lib import procflags


def test_no_window_kwargs_empty_on_the_real_platform_when_not_windows():
    """Sanity check against the real, un-monkeypatched `os.name`: this
    suite runs on POSIX, so the function must return an empty dict without
    any patching at all."""
    if procflags.os.name != "nt":
        assert procflags.no_window_kwargs() == {}


def test_no_window_kwargs_empty_when_os_name_is_posix(monkeypatch):
    monkeypatch.setattr(procflags.os, "name", "posix")
    assert procflags.no_window_kwargs() == {}


class _FakeStartupInfo:
    """Stand-in for `subprocess.STARTUPINFO`, which only exists on
    Windows -- real CPython raises AttributeError for it on POSIX, so
    `no_window_kwargs()` can't be exercised end-to-end here without a
    fake. Seeds `dwFlags` with an unrelated bit already set, to prove the
    real code ORs `STARTF_USESHOWWINDOW` in rather than assigning over it
    and clobbering whatever the caller (or a future flag) already set."""

    def __init__(self):
        self.dwFlags = 0x2
        self.wShowWindow = None


def test_no_window_kwargs_suppresses_console_on_windows(monkeypatch):
    """Issue #30, the fix itself: on a simulated Windows (`os.name ==
    'nt'`), `no_window_kwargs()` must return exactly the two kwargs that
    keep Windows from allocating a console for the spawned child --
    `creationflags=CREATE_NO_WINDOW` and a `STARTUPINFO` with
    `STARTF_USESHOWWINDOW` set and `wShowWindow=SW_HIDE`. Without these, a
    console-subsystem binary (stremio-server.exe) spawned from Kodi's
    GUI-subsystem process pops an empty cmd window; closing that window
    sends CTRL_CLOSE_EVENT, which kills the child, which the
    service_runner supervisor then restarts -- popping a fresh window
    right back, which is exactly the "closing it reopens it" symptom
    reported in #30.
    """
    monkeypatch.setattr(procflags.os, "name", "nt")
    monkeypatch.setattr(procflags.subprocess, "STARTUPINFO", _FakeStartupInfo, raising=False)

    result = procflags.no_window_kwargs()

    assert result["creationflags"] == 0x08000000  # CREATE_NO_WINDOW
    startupinfo = result["startupinfo"]
    assert isinstance(startupinfo, _FakeStartupInfo)
    assert startupinfo.dwFlags == 0x3  # pre-set 0x2 survives; 0x1 (STARTF_USESHOWWINDOW) ORed in
    assert startupinfo.wShowWindow == 0  # SW_HIDE
    assert set(result) == {"creationflags", "startupinfo"}
