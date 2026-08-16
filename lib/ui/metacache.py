"""Short-TTL disk cache for `views._fetch_meta()` results.

Kodi runs every ``plugin://`` call in a fresh sub-interpreter, so an
in-memory cache would not survive a back-navigation - each directory
listing is a brand-new process. Kodi's own directory cache
(`xbmcplugin.endOfDirectory(..., cacheToDisc=True)`, the default)
covers repeat visits to the exact same listing URL, but nothing covers
`views._fetch_meta()` itself being called repeatedly for the SAME
(stype, sid) within one continuous custom-window session (one
`HomeWindow` invocation, which nests every other screen as blocking
`doModal()` calls in the same process - see `lib.ui.uicommon`'s module
docstring): `lib.ui.infowindow.ShowcaseWindow` fetches a focused
poster's full meta once in its background enrich worker (to fill in a
catalog preview's missing description/genres) and again, independently,
if the user opens that same poster's cast & crew picker
(`_open_credits()`) - both landing on the identical (stype, sid); and
`lib.ui.detailwindow.open_detail()` re-fetches from scratch every time
the same title's `DetailWindow` is reopened (backing out to a
picker/search/library screen and picking it again), since a closed
`DetailWindow` keeps nothing in memory. This module closes exactly that
gap: a small, short-lived, on-disk memo keyed by (stype, sid), written
next to the rest of `Store`'s on-disk state.

Deliberately NOT used for catalog/search results: those run 50-500 KB
per response, so caching them would trade network latency for worse
eMMC wear on the exact hardware this optimisation targets.

Meta responses were assumed to be 1-10 KB, but a real profile showed
otherwise: a SERIES meta embeds its whole `videos` array, one entry per
episode, and measured 25-30 KB apiece. Since the whole cache is one
JSON file rewritten on every store, an entry-count cap alone let the
per-write cost grow with the cache - 64 series would mean rewriting
~2 MB to record one fetch, which is precisely the flash wear this was
meant to avoid. The cap below is therefore a BYTE budget.

Every operation is best-effort: a missing/corrupt cache file, or a
write that fails outright (read-only filesystem, permissions), must
never break the caller's real fetch - it is treated as a cache miss.
"""
import json
import os
import tempfile
import threading
import time

FILENAME = 'meta_cache.json'

#: How long a cached meta object is served before being treated as
#: stale. Short on purpose - covers one browsing session (picking
#: through a show's seasons, or backing in and out of the same title)
#: without risking a long-lived stale answer if the addon's own data
#: changes.
TTL_SECONDS = 300

#: Byte budget for the whole cache file. This bounds the cost of a
#: single store: the file is rewritten wholesale each time, so the
#: budget - not the entry count - is what caps write amplification.
#: 256 KB holds a healthy browsing session (a handful of 25-30 KB
#: series plus a good number of ~3 KB movies) while keeping any one
#: write small enough to be cheap on eMMC/SD.
MAX_BYTES = 256 * 1024

#: Belt-and-braces cap on entry count, so a pathological run of tiny
#: metas cannot grow the index unboundedly under the byte budget.
MAX_ENTRIES = 64

#: Guards the read-evict-write sequence in `store_cached_meta()` and
#: `store_cached_metas()` (the former delegates to the latter with a
#: single-entry list, so there is exactly one lock/read/write path). This
#: cache's only in-process writers are `views._map_addons()`'s cache-miss
#: fetches, fanned out over a `ThreadPoolExecutor(max_workers=8)`; without
#: serialising them, concurrent stores each read the same on-disk snapshot,
#: add their own entry to their own copy, and `_atomic_write` whichever
#: copy lands last - every other thread's entry silently vanishes.
#: Measured: 15 concurrent stores through the pool left only 4 entries on
#: disk. The lock only covers threads sharing this Python process/GIL; it
#: does nothing for two separate `plugin://` invocations (Kodi's
#: fresh-sub-interpreter-per-call model, see module docstring) storing at
#: the same instant, so cross-process last-writer-wins is still possible.
#: That residual race is accepted: this is a best-effort cache where the
#: worst outcome is one dropped entry (forcing a refetch), never a
#: corrupt file - `_atomic_write`'s `os.replace` already guarantees the
#: file on disk is always one complete write or the other.
_store_lock = threading.Lock()


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


def _evict(entries):
    """Drop the oldest entries until the cache fits both budgets.

    Expired entries go first regardless of size - they can never be
    served again, so keeping them only inflates the next write. Then,
    while the serialised file would still exceed `MAX_BYTES` (or hold
    more than `MAX_ENTRIES`), the oldest surviving entry is dropped.
    Size is measured on the real serialised form, since that is exactly
    what each store has to write out.
    """
    now = time.time()
    kept = {
        key: entry for key, entry in entries.items()
        if now - entry.get('ts', 0) <= TTL_SECONDS
    }
    # Never evict away the entry just stored: if a single meta is bigger
    # than the whole budget, caching just that one is still the right
    # answer, and an empty cache would make the next visit refetch it.
    oldest_first = sorted(kept.items(), key=lambda kv: kv[1].get('ts', 0))
    while len(oldest_first) > 1 and (
        len(oldest_first) > MAX_ENTRIES
        or len(json.dumps(dict(oldest_first), separators=(',', ':'))) > MAX_BYTES
    ):
        oldest_first.pop(0)
    return dict(oldest_first)


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
    (absent, expired, or any I/O/parse failure).

    Deliberately lock-free: `_atomic_write()` finishes with `os.replace()`,
    which POSIX guarantees is atomic, so a concurrent store can only ever
    leave this read seeing the whole old file or the whole new one - never
    a torn/partial one. Serialising reads against `store_cached_meta()`'s
    lock would buy nothing but contention.
    """
    try:
        entry = _read_entries(data_dir).get(_key(stype, sid))
        if not entry or time.time() - entry.get('ts', 0) > TTL_SECONDS:
            return None
        return entry.get('meta')
    except OSError:
        return None


def store_cached_metas(data_dir, entries):
    """Cache every *successful* (stype, sid, meta) triple in `entries` in
    one read-evict-write cycle, instead of one per meta.

    Exists for fan-outs like `continuewatching.open_continue_watching()`,
    which can trigger up to 15 cache-miss fetches in a single Home
    "Continue watching" open (`views._map_addons()`'s pool width): calling
    `store_cached_meta()` once per fetch meant up to 15 full-file
    read-evict-writes of the same cache (measured 3.46ms CPU each on an
    i7-13700H at budget saturation; 15-35ms on a Pi, plus real flash
    latency/wear per write) where one write, holding every entry, does
    the same job. `store_cached_meta()` is now just this function called
    with a single-entry list - one lock/read/write path either way.

    Entries with a falsy `meta` are skipped, exactly like
    `store_cached_meta()` - callers must not pass None/empty results, so
    a failed or empty upstream answer is never cached as if it were real.
    A batch that ends up with nothing usable is a no-op: no lock taken,
    no file touched.
    """
    usable = [(stype, sid, meta) for stype, sid, meta in entries if meta]
    if not usable:
        return
    try:
        with _store_lock:
            stored = _read_entries(data_dir)
            now = time.time()
            for stype, sid, meta in usable:
                stored[_key(stype, sid)] = {'ts': now, 'meta': meta}
            stored = _evict(stored)
            _atomic_write(_path(data_dir), stored)
    except OSError:
        pass


def store_cached_meta(data_dir, stype, sid, meta):
    """Cache a *successful* meta object for (stype, sid). `meta` must
    already be known-usable - callers must not pass None/empty results,
    so a failed or empty upstream answer is never cached as if it were
    a real one.

    Delegates to `store_cached_metas()` with a single-entry list - see
    its docstring for the read-evict-write/lock contract this shares."""
    store_cached_metas(data_dir, [(stype, sid, meta)])
