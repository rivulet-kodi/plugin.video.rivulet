"""Short-TTL disk cache for `views._fetch_meta()` results.

Kodi runs every ``plugin://`` call in a fresh sub-interpreter, so an
in-memory cache would not survive a back-navigation - each classical
view is a brand-new process. Kodi's own directory cache
(`xbmcplugin.endOfDirectory(..., cacheToDisc=True)`, the default)
already covers repeat visits to the exact same listing URL, but it does
NOT cover `views.meta()` -> `views.videos()`: picking season 1, backing
out, then picking season 2 hits a *different* URL each time, and
`views.videos()` re-fetches the identical full meta object from every
addon again to get at its `videos` list - once per season, every time,
for the same show, within one continuous browsing session. This module
closes exactly that gap: a small, short-lived, on-disk memo keyed by
(stype, sid), written next to the rest of `Store`'s on-disk state.

Deliberately NOT used for catalog/search results: those run 50-500 KB
per response, so caching them would trade network latency for worse
eMMC wear on the exact hardware this optimisation targets. Meta
responses are an order of magnitude smaller (1-10 KB), so the tradeoff
runs the other way.

Every operation is best-effort: a missing/corrupt cache file, or a
write that fails outright (read-only filesystem, permissions), must
never break the caller's real fetch - it is treated as a cache miss.
"""
import json
import os
import tempfile
import time

FILENAME = 'meta_cache.json'

#: How long a cached meta object is served before being treated as
#: stale. Short on purpose - covers one browsing session (picking
#: through a show's seasons, or backing in and out of the same title)
#: without risking a long-lived stale answer if the addon's own data
#: changes.
TTL_SECONDS = 300

#: Hard cap on the number of cached shows/movies kept at once. A
#: browsing session touches a handful of titles, not hundreds - once
#: over the cap, the oldest entries are dropped first.
MAX_ENTRIES = 64


def _key(stype, sid):
    return '%s:%s' % (stype, sid)


def _path(data_dir):
    return os.path.join(data_dir, FILENAME)


def _read_entries(data_dir):
    try:
        with open(_path(data_dir)) as fh:
            entries = json.load(fh)
    except (OSError, ValueError):
        return {}
    return entries if isinstance(entries, dict) else {}


def _atomic_write(path, data):
    directory = os.path.dirname(path) or '.'
    fd, tmp_path = tempfile.mkstemp(prefix='.tmp-', dir=directory)
    try:
        with os.fdopen(fd, 'w') as fh:
            json.dump(data, fh, separators=(',', ':'))
        os.replace(tmp_path, path)
    except OSError:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


def load_cached_meta(data_dir, stype, sid):
    """Return the cached meta object for (stype, sid), or None on a miss
    (absent, expired, or any I/O/parse failure)."""
    try:
        entry = _read_entries(data_dir).get(_key(stype, sid))
        if not entry or time.time() - entry.get('ts', 0) > TTL_SECONDS:
            return None
        return entry.get('meta')
    except OSError:
        return None


def store_cached_meta(data_dir, stype, sid, meta):
    """Cache a *successful* meta object for (stype, sid). `meta` must
    already be known-usable - callers must not pass None/empty results,
    so a failed or empty upstream answer is never cached as if it were
    a real one."""
    if not meta:
        return
    try:
        entries = _read_entries(data_dir)
        entries[_key(stype, sid)] = {'ts': time.time(), 'meta': meta}
        if len(entries) > MAX_ENTRIES:
            oldest_first = sorted(entries.items(), key=lambda kv: kv[1].get('ts', 0))
            entries = dict(oldest_first[-MAX_ENTRIES:])
        _atomic_write(_path(data_dir), entries)
    except OSError:
        pass
