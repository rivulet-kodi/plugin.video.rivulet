# SPDX-FileCopyrightText: 2026 M0Rf30
# SPDX-License-Identifier: MIT

"""Whole-file JSON reads and atomic writes for the addon's disposable
caches: `pagecache`, `ratings`, `lib.ui.metacache` and `lib.ui.searchfeed`.

Each of those once carried its own copy of the same temp-file-and-rename
writer and the same tolerant reader. It lives in `lib.stremio` because the
two protocol-layer caches need it and this layer imports nothing outside
itself (Kodi-independent; see AGENTS.md); `lib.ui` may import from here.

Not used by `lib.store`, on purpose: the store's writer also fsyncs and
retries `os.replace` on `PermissionError`, because losing `addons.json`
or `auth.json` to a power cut costs the user their setup. A lost cache
file only costs a refetch, so these writes skip the fsync and the flash
wear it brings.
"""

import json
import math
import os
import tempfile


def write_json(path, payload, prefix):
    """Write `payload` as compact JSON to a temp file named `prefix*` in
    `path`'s directory, then `os.replace()` it over `path`, so a reader
    sees the whole old file or the whole new one, never a torn one.

    The temp file is removed on any failure and the error re-raised; each
    caller decides whether a failed cache write matters. `prefix` keeps
    each cache's leftovers identifiable (`lib.store` sweeps stale
    `.tmp-*` files from the data directory)."""
    directory = os.path.dirname(path) or '.'
    fd, tmp_path = tempfile.mkstemp(prefix=prefix, dir=directory)
    try:
        with os.fdopen(fd, 'w') as handle:
            # json.dumps takes CPython's C one-shot encoder; json.dump
            # always runs the pure-Python iterencode.
            handle.write(json.dumps(payload, separators=(',', ':')))
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def read_json(path):
    """The decoded contents of `path`, or None when it is missing,
    unreadable or not JSON. Callers still check the shape: a cache file
    is whatever an older release, or a half-broken disk, left behind."""
    try:
        with open(path) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def finite_number(value):
    """True for a real, finite int/float: what a stored timestamp must be
    before age or eviction math touches it. `bool` is an int subclass and
    is rejected; `nan`/`inf` would make every comparison lie."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False
