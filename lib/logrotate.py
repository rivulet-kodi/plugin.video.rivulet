# SPDX-FileCopyrightText: 2026 M0Rf30
# SPDX-License-Identifier: MIT

"""Size-capped rotation of the rivulet-engine log, shared by both ways of
running the engine: `lib.service_runner.ServerProcess` (a child process)
and `lib.libserver.LibraryServer` (the c-shared library, in-process).

It lives below both of them rather than in either: `lib.service_runner`
is the orchestration layer that lazily imports `lib.libserver`, so the
library server reaching back up into it for these helpers would invert
that layering. Before this module the two carried line-for-line copies
of the rename and the size check, plus hand-synced copies of the two
constants. What still differs between them - how each one bounds `.1`
once a live rotation has happened, and the Windows open-file fallback
only the child-process side needs - stays in each class.

Pure Python, no `xbmc*` imports.
"""

import os

#: A log past this size is renamed to `<log>.1`, replacing any older backup.
LOG_ROTATE_BYTES = 5 * 1024 * 1024

#: main()'s HEALTHY branch calls `maybe_rotate_log()` every tick
#: (HEALTHY_POLL_INTERVAL, 2s), but a live engine's log is only ever worth
#: re-checking this coarsely: `rotate_if_oversized()` at start already
#: handles the common case (rotate right before spawning), so the periodic
#: check exists only to bound growth across a crash-free multi-day session
#: - a real risk on a near-full SD/eMMC box with a chatty rivulet-engine
#: binary. Gating the stat() to once per this many seconds keeps the
#: idle-loop no-disk-touch property intact for every tick except one: 288
#: stats/day instead of 43200 (one per 2s tick) for the exact same
#: detection latency that matters here.
LOG_ROTATE_CHECK_INTERVAL = 300.0


def rename_to_backup(log_path):
    """Rename `log_path` -> `log_path + ".1"`, replacing any existing
    backup. No size check here: callers that already know the file is
    oversized call this directly, so the check itself is never repeated.
    Failures are swallowed; on Windows the rename of a file a child holds
    open fails (CPython never requests FILE_SHARE_DELETE, bpo-15244), and
    `ServerProcess.maybe_rotate_log()` checks for that afterwards."""
    try:
        backup = log_path + ".1"
        if os.path.exists(backup):
            os.remove(backup)
        os.rename(log_path, backup)
    except OSError:
        pass


def rotate_if_oversized(log_path):
    """The once-per-start rotation, run right before the engine opens
    `log_path`: rotate it aside when it is over `LOG_ROTATE_BYTES`. A
    missing log (first start) is not an error."""
    try:
        if os.path.getsize(log_path) > LOG_ROTATE_BYTES:
            rename_to_backup(log_path)
    except OSError:
        pass


def check_due(last_check, now):
    """True when the periodic check may stat() again: never checked, or at
    least `LOG_ROTATE_CHECK_INTERVAL` seconds since `last_check` (both
    `time.monotonic()` values)."""
    return last_check is None or now - last_check >= LOG_ROTATE_CHECK_INTERVAL


def oversized_live_log(log_path):
    """The file the engine is appending to right now, when it is over
    `LOG_ROTATE_BYTES`; else None.

    That is `log_path`, unless a live rotation already renamed it aside
    this run: the engine keeps appending to the same file, now reachable
    only as `log_path + ".1"`, and nothing recreates `log_path` until the
    next start. A stat failure counts as "not oversized"."""
    live_path = log_path if os.path.exists(log_path) else log_path + ".1"
    try:
        oversized = os.path.getsize(live_path) > LOG_ROTATE_BYTES
    except OSError:
        return None
    return live_path if oversized else None
