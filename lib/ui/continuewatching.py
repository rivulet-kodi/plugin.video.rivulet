"""open_continue_watching(): build metas for the local playback-progress
cache's most recently updated resumable titles and show them in the
coverflow overlay (`lib.ui.infowindow`) - the Home "Continue watching"
row's action. Picking a title opens `lib.ui.detailwindow` for it, exactly
like `lib.ui.librarywindow.open_library()` does for the Stremio library
row.
"""
from lib.ui.dependencies import get_store

#: Resumable progress band (percent of duration already watched) -
#: mirrors `lib.ui.player`'s RESUME_MIN_PERCENT/RESUME_MAX_PERCENT
#: (importing `lib.ui.player` here just for two floats would pull in its
#: xbmcplayer-derived playback machinery, which this module otherwise
#: never needs).
_RESUME_MIN_PERCENT = 1.0
_RESUME_MAX_PERCENT = 95.0

#: Cap on the number of titles shown in the "Continue watching" row.
_MAX_ITEMS = 15


def resumable_candidates(entries):
    """Reduce raw `Store.get_progress_entries()` dicts to the ones worth
    surfacing in the "Continue watching" row: `duration_ms > 0` and
    between `_RESUME_MIN_PERCENT` (barely started, not worth asking) and
    `_RESUME_MAX_PERCENT` (basically finished, nothing meaningful left to
    resume) percent watched.

    A series can have many `video_id` entries, one per episode watched;
    only the most recently updated entry per `(type, id)` is kept, so a
    binge shows once in the row, not once per episode. The result is
    sorted by `updated_at` descending (most recent first) and capped at
    `_MAX_ITEMS`. Pure function of `entries` - no store/addon access.
    """
    best = {}
    for entry in entries:
        duration_ms = entry.get('duration_ms') or 0
        if duration_ms <= 0:
            continue
        position_ms = entry.get('position_ms') or 0
        percent = (position_ms / duration_ms) * 100.0
        if percent < _RESUME_MIN_PERCENT or percent >= _RESUME_MAX_PERCENT:
            continue
        key = (entry.get('type'), entry.get('id'))
        updated_at = entry.get('updated_at') or ''
        current = best.get(key)
        if current is None or updated_at > (current.get('updated_at') or ''):
            best[key] = entry
    candidates = sorted(best.values(), key=lambda entry: entry.get('updated_at') or '', reverse=True)
    return candidates[:_MAX_ITEMS]


def has_resumable(store):
    """Cheap gate for whether the Home "Continue watching" row should be
    shown at all - True iff at least one cached progress entry is in the
    resumable band. `Store.get_progress_entries()` is a pure local-cache
    read, so this is safe to call on every Home render (unlike
    `open_continue_watching()`, which fans out to addons)."""
    return bool(resumable_candidates(store.get_progress_entries()))


def open_continue_watching():
    """Fetch metas for the current resumable candidates and show them in
    the coverflow overlay. Returns True if the caller (HomeWindow) should
    also close (playback started somewhere down the open_detail() chain).

    Each candidate is fetched with `views._fetch_meta(..., store=False)`
    so a cache-miss result is not written to disk on its own; instead
    every genuinely fresh result from the whole fan-out is written in a
    single `metacache.store_cached_metas()` call once the fan-out
    returns. A cold open can fan out to `_MAX_ITEMS` (15) cache-miss
    fetches - storing each individually meant up to 15 full-file cache
    rewrites for one Home row open, where one write holding every entry
    does the same job (see `metacache.store_cached_metas()`'s docstring
    for the measured per-write cost this avoids).

    `_fetch_meta(..., on_miss=...)` marks which candidates were genuine
    cache misses. This distinction matters: a WARM reopen - every
    candidate already served from the on-disk cache, zero addon calls -
    must batch-store NOTHING. `store_cached_metas()` unconditionally
    re-stamps `ts` on everything it is given, so batching cache HITS
    back into it would re-arm every entry's TTL on every reopen and the
    cache would never actually re-check the addon as long as the row
    keeps getting reopened within `metacache.TTL_SECONDS` - an entirely
    normal usage pattern (adversarial-review finding on the first cut of
    this fix).
    """
    import xbmc

    from lib.ui import views
    from lib.ui.compat import L, log, notify
    from lib.ui.uicommon import busy_dialog

    store = get_store()
    candidates = resumable_candidates(store.get_progress_entries())

    def _fetch(candidate):
        was_fresh = []
        meta = views._fetch_meta(
            candidate['type'], candidate['id'], store=False,
            on_miss=lambda: was_fresh.append(True),
        )
        return meta, bool(was_fresh)

    with busy_dialog(L(30033)):
        fetched = views._map_addons(_fetch, candidates)

    cache_dir = getattr(store, 'data_dir', None)
    if cache_dir is not None:
        from lib.ui.metacache import store_cached_metas
        store_cached_metas(cache_dir, [
            (candidate['type'], candidate['id'], meta)
            for candidate, (meta, was_fresh) in zip(candidates, fetched)
            if meta and was_fresh
        ])

    metas = [meta for meta, _was_fresh in fetched if meta]
    if not metas:
        notify(L(30030))
        return False

    log('continuewatching: opening continue-watching showcase (%d items)' % len(metas), xbmc.LOGINFO)
    try:
        from lib.ui.infowindow import open_showcase
        selected = open_showcase(metas, catalog_title=L(30231))
    except Exception as exc:  # a skin/UI failure must surface, not vanish
        log('continuewatching: coverflow failed to open: %r' % (exc,), xbmc.LOGERROR)
        notify(L(30032))
        return False
    if not selected:
        return False

    from lib.ui.detailwindow import open_detail
    return open_detail(selected.get('type'), selected.get('id'))
