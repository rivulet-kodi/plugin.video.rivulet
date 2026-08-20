"""Suppress the console window Windows allocates for a console-subsystem
child process spawned from a windowless GUI parent (issue #30).

stremio-server.exe (M0Rf30/stremio-server-go) is built with the default Go
console subsystem. Kodi on Windows is a GUI-subsystem process with no
console of its own. When a GUI process calls CreateProcess() on a console
subsystem binary without CREATE_NO_WINDOW, Win32 allocates that child a
brand new, visible console window -- there is nothing Kodi-specific or
Rivulet-specific about this, it is CreateProcess()'s documented default
behaviour for that exact subsystem mismatch. The window has no controls
tying it to Rivulet and appears to just be an empty cmd box; closing it
delivers CTRL_CLOSE_EVENT to the console's process group, which kills
stremio-server.exe, which `lib.service_runner`'s supervisor then dutifully
restarts -- popping a fresh window right back open. That loop is what
issue #30 reports as "when I close it, it opens again".

The fix is the standard Win32 answer to this exact mismatch: tell
CreateProcess() not to allocate a console at all (`CREATE_NO_WINDOW`,
Python 3.7+'s documented `subprocess.creationflags` value for this), and
additionally hide the STARTUPINFO window in case some code path along the
way still ends up with an inherited/attached console
(`STARTF_USESHOWWINDOW` + `wShowWindow=SW_HIDE`). This mirrors the
approach elgatito/plugin.video.elementum's `daemon.py` uses in
`start_elementumd()` for the same console-subsystem-under-Kodi problem,
with two of its workarounds deliberately dropped as unnecessary here:

- Elementum's `clear_fd_inherit_flags()` walks the process's open file
  descriptors with ctypes and strips `HANDLE_FLAG_INHERIT` from each one
  before spawning, because Python 2's `subprocess` on Windows inherited
  ALL open handles by default. Python 3.8 (this addon's floor) already
  defaults `close_fds=True` and, when handles must be shared, uses the
  `STARTUPINFO.lpAttributeList["handle_list"]` mechanism to pass an
  explicit, minimal handle list to CreateProcess() -- the CPython
  standard library already does the handle hygiene Elementum had to
  hand-roll, so redoing it here would just be dead code shadowing the
  interpreter's own behaviour.
- Elementum's `getWindowsShortPath()` converts the executable path to its
  8.3 short form before exec, working around a Python 2 bytes/`mbcs`
  encoding limitation when a path contains non-ASCII characters.
  `subprocess.Popen`/`subprocess.run` on Python 3 pass `str` straight
  through to the `CreateProcessW` (wide-char) API, so arbitrary Unicode
  paths already work with no conversion needed.

Both `ServerProcess.start()` (lib/service_runner.py, the long-lived server
process) and `verify_executable()` (lib/serverbin.py, the one-shot
`<binary> version` sanity check run right after install) hit this same
CreateProcess() behaviour and so both splat `no_window_kwargs()` into
their `subprocess` call.
"""
import os
import subprocess

CREATE_NO_WINDOW = 0x08000000
STARTF_USESHOWWINDOW = 0x00000001
SW_HIDE = 0


def no_window_kwargs():
    """Extra `subprocess.Popen`/`subprocess.run` kwargs that suppress the
    console window Windows would otherwise allocate for a console-subsystem
    child (see module docstring).

    Returns `{}` on every non-Windows platform: `CREATE_NO_WINDOW` is a
    Windows-only creationflags bit (passing any non-zero `creationflags` on
    POSIX raises ValueError, since POSIX `subprocess` has no such
    parameter), and `subprocess.STARTUPINFO` does not exist there at all --
    an empty dict splatted via `**no_window_kwargs()` is a true no-op, so
    POSIX callers keep byte-identical behaviour to before this fix existed.
    """
    if os.name != "nt":
        return {}
    # `subprocess.STARTUPINFO` is Windows-only in typeshed and mypy runs with a
    # POSIX target here ("python_version = 3.9", no --platform), so the attribute
    # is genuinely absent from its view of the module -- hence the ignore.
    startupinfo = subprocess.STARTUPINFO()  # type: ignore[attr-defined]
    startupinfo.dwFlags |= STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = SW_HIDE
    return {"creationflags": CREATE_NO_WINDOW, "startupinfo": startupinfo}
