"""open_my_stuff(): the merged "My Stuff" screen - one grid combining
what the Home menu used to split across two rows ("Continue watching",
`lib.ui.continuewatching`, and "Library", `lib.ui.librarywindow`) plus a
third band those two never surfaced at all: titles finished recently.

The two old rows were already the same shape - build a list of meta
dicts, hand it to a coverflow - and they overlap heavily in practice (a
title you are part-way through is usually also in your library). Showing
them as separate destinations made the viewer guess which one held the
thing they wanted. This module merges both sources on their common
`(type, id)` key and renders the result as one labelled poster row per
band (`lib.ui.gridwindow`) - see `merge_entries()` for the merge and
`group_by_band()` for the split into rows.

Band ordering (the flat list is grouped, not sorted by one key):

    BAND_RESUME   part-way through, most recently touched first
    BAND_NEXT_UP  a series whose last episode is finished - the NEXT
                  episode is what the viewer actually wants
    BAND_RECENT   finished recently, nothing left to resume
    BAND_LIBRARY  saved but never played on this device

`BAND_NEXT_UP` is why finishing an episode is not the same as finishing
a title: at 99% of S1E2 the old "Continue watching" row dropped the show
entirely (its >=95% cutoff), exactly when the viewer most wants S1E3.
Resolving which episode that is needs the series meta, so the band is
computed here as "this series is next-up" and the actual episode is
resolved lazily by `lib.ui.binge.next_video()` once the meta is in hand
(see `resolve_next_up()`).

Everything above `open_my_stuff()` is a pure function of its arguments -
no store, no addon, no Kodi imports - so tests/test_mystuff.py exercises
every band with plain dicts.
"""
from lib.ui.dependencies import get_store

#: Progress band edges, as a percent of duration. Mirrors
#: `lib.ui.player`'s RESUME_MIN_PERCENT/RESUME_MAX_PERCENT, and matches
#: what `lib.ui.continuewatching` used before this module replaced it
#: (importing `lib.ui.player` for two floats would pull in its
#: xbmcplayer-derived playback machinery, which this module never needs).
RESUME_MIN_PERCENT = 1.0
RESUME_MAX_PERCENT = 95.0

#: Band identifiers, in display order. Carried on each merged entry as
#: `band`, so the grid can badge a card ("62%", "NEXT UP", "WATCHED")
#: without re-deriving which band it landed in.
BAND_RESUME = 'resume'
BAND_NEXT_UP = 'next_up'
BAND_RECENT = 'recent'
BAND_LIBRARY = 'library'

#: Display order of the bands - `merge_entries()` emits its flat list in
#: exactly this sequence.
BAND_ORDER = (BAND_RESUME, BAND_NEXT_UP, BAND_RECENT, BAND_LIBRARY)

#: band -> the localized-string id naming it. The grid names the band of
#: whichever title is focused (`gridwindow._caption()`), which is what
#: tells the viewer WHY that title is on the screen; the per-card badge
#: is the same information compressed to a colour and a few characters.
BAND_HEADINGS = {
    BAND_RESUME: 30245,
    BAND_NEXT_UP: 30242,
    BAND_RECENT: 30246,
    BAND_LIBRARY: 30247,
}

#: Cap on the number of titles fetched for the played bands (resume /
#: next-up / recent). Each one costs a `views._fetch_meta` fan-out on a
#: cache miss, so this bounds a cold open's worst case - the same reason
#: `lib.ui.continuewatching` capped its row at 15.
MAX_PLAYED_ITEMS = 24

#: Cap on library titles appended after the played bands. The library
#: needs no per-title fetch (the datastore entry already carries name and
#: poster), so this is a grid-length bound rather than a fetch bound and
#: can be far more generous.
MAX_LIBRARY_ITEMS = 200


def percent_watched(entry):
    """Percent of `entry`'s duration already watched, or None when the
    entry carries no usable duration (`duration_ms` missing, zero, or
    non-numeric) - the same tolerance `Store.get_progress_entries()`
    applies to a partly-corrupt cache file."""
    duration_ms = entry.get('duration_ms') or 0
    if not isinstance(duration_ms, (int, float)) or isinstance(duration_ms, bool):
        return None
    if duration_ms <= 0:
        return None
    position_ms = entry.get('position_ms') or 0
    if not isinstance(position_ms, (int, float)) or isinstance(position_ms, bool):
        return None
    return (position_ms / duration_ms) * 100.0


def _band_for(percent, content_type):
    """Which band a single progress entry belongs to, or None when it is
    not worth surfacing at all.

    Below `RESUME_MIN_PERCENT` is "barely started" - a few seconds in,
    usually a stream the viewer opened and immediately backed out of, so
    it is dropped rather than presented as something to resume. At or
    above `RESUME_MAX_PERCENT` the title is finished: a series goes to
    `BAND_NEXT_UP` (the next episode is the real target - see the module
    docstring), anything else to `BAND_RECENT`.
    """
    if percent is None or percent < RESUME_MIN_PERCENT:
        return None
    if percent < RESUME_MAX_PERCENT:
        return BAND_RESUME
    return BAND_NEXT_UP if content_type == 'series' else BAND_RECENT


def latest_by_title(entries):
    """Reduce raw `Store.get_progress_entries()` dicts to one entry per
    `(type, id)` - the most recently updated - so a binged series shows
    once, not once per episode.

    Deliberately reduces BEFORE banding rather than after: the band of a
    series is a property of its LATEST episode (finished S1E2 -> next-up),
    and banding first would let an older half-watched episode outrank it
    and pin the show in `BAND_RESUME` forever. Returns a list sorted by
    `updated_at` descending (most recent first); entries with no usable
    percent are dropped here, since they can never land in a band.
    """
    best = {}
    for entry in entries or []:
        if percent_watched(entry) is None:
            continue
        key = (entry.get('type'), entry.get('id'))
        if key[1] is None:
            continue
        updated_at = entry.get('updated_at') or ''
        current = best.get(key)
        if current is None or updated_at > (current.get('updated_at') or ''):
            best[key] = entry
    return sorted(best.values(), key=lambda e: e.get('updated_at') or '', reverse=True)


def library_metas(entries):
    """Reduce a Stremio `libraryItem` datastore response to meta dicts,
    dropping entries the user removed and any without an `_id`. Same
    projection `lib.ui.librarywindow.open_library()` used before this
    module replaced it."""
    return [
        {
            'id': entry['_id'],
            'name': entry.get('name'),
            'type': entry.get('type'),
            'poster': entry.get('poster'),
            'background': entry.get('background'),
        }
        for entry in entries or []
        if not entry.get('removed') and entry.get('_id')
    ]


def merge_entries(progress_entries, library_entries):
    """Merge local playback progress with the Stremio library into one
    banded, flat list of dicts:

        {'type', 'id', 'band', 'percent', 'video_id', 'updated_at',
         'name', 'poster', 'background'}

    Played titles come first, in `BAND_ORDER`, each band most-recently-
    touched first; library titles never played on this device follow, in
    datastore order. A title present in BOTH sources appears exactly once
    - in its played band, enriched with the library's name/poster so the
    grid can draw it without waiting on an addon fetch.

    Either source may be empty: logged out (no library) still yields the
    played bands, and a fresh install with an empty cache still yields
    the library. Pure function - the caller does all I/O.
    """
    library = library_metas(library_entries)
    by_key = {(meta.get('type'), meta.get('id')): meta for meta in library}

    banded = {band: [] for band in BAND_ORDER}
    played_keys = set()
    for entry in latest_by_title(progress_entries)[:MAX_PLAYED_ITEMS]:
        content_type = entry.get('type')
        percent = percent_watched(entry)
        band = _band_for(percent, content_type)
        if band is None:
            continue
        key = (content_type, entry.get('id'))
        played_keys.add(key)
        known = by_key.get(key) or {}
        banded[band].append({
            'type': content_type,
            'id': entry.get('id'),
            'band': band,
            'percent': percent,
            'video_id': entry.get('video_id'),
            'updated_at': entry.get('updated_at'),
            'name': known.get('name'),
            'poster': known.get('poster'),
            'background': known.get('background'),
        })

    merged = []
    for band in BAND_ORDER:
        merged.extend(banded[band])
    remaining = MAX_LIBRARY_ITEMS
    for meta in library:
        if remaining <= 0:
            break
        if (meta.get('type'), meta.get('id')) in played_keys:
            continue
        merged.append(dict(meta, band=BAND_LIBRARY, percent=None, video_id=None, updated_at=None))
        remaining -= 1
    return merged


def group_by_band(items):
    """Split the merged list into `[(band, [item, ...]), ...]` in
    `BAND_ORDER`, dropping bands with nothing in them.

    The grid draws one labelled row per band (see `lib.ui.gridwindow`),
    so this is the shape that screen consumes. Bands with no members are
    dropped rather than passed through empty: the skin would hide the row
    anyway, and an empty entry here would make the first-band focus in
    `GridWindow.onInit()` land on a row that is not drawn.
    """
    grouped = []
    for band in BAND_ORDER:
        members = [item for item in items or [] if item.get('band') == band]
        if members:
            grouped.append((band, members))
    return grouped


def resolve_next_up(item, meta):
    """For a `BAND_NEXT_UP` item, the `meta['videos']` entry after the
    episode the viewer just finished (`item['video_id']`), via
    `lib.ui.binge.next_video()` - or None when the series has no next
    episode (a finished final season), the meta carries no videos, or
    `item` is not in that band.

    Kept separate from `merge_entries()` on purpose: the merge is a pure
    local-cache operation, while this needs the series meta, which the
    caller only has after its fetch fan-out.
    """
    if not item or item.get('band') != BAND_NEXT_UP:
        return None
    video_id = item.get('video_id')
    if not video_id:
        return None
    from lib.ui.binge import next_video
    return next_video(meta, video_id)


def has_content(store):
    """Cheap gate for whether the Home "My Stuff" row should be shown at
    all - True iff the local progress cache holds at least one bandable
    entry, or the user is logged in (in which case they may have a
    library worth opening, which cannot be checked without a network
    call). `get_progress_entries()`/`get_auth()` are both pure local
    reads, so this is safe on every Home render."""
    if any(_band_for(percent_watched(e), e.get('type')) for e in latest_by_title(store.get_progress_entries())):
        return True
    return bool(store.get_auth())


def _fetch_library_entries(store):
    """The user's Stremio library datastore, or [] when logged out or the
    call fails. A library failure must never sink the whole screen - the
    local played bands work fully offline and logged out, which is
    exactly the state a fresh device is in - so this logs and degrades to
    empty rather than propagating (`open_library()`'s old behaviour was
    to notify and show nothing at all)."""
    import xbmc

    from lib.stremio.api import ApiError, StremioAPI
    from lib.ui.compat import log

    auth = store.get_auth()
    if not auth:
        return []
    try:
        return StremioAPI().datastore_get(auth.get('authKey'), collection='libraryItem', all=True) or []
    except ApiError as exc:
        log('mystuff: datastore_get failed, showing local bands only: %r' % (exc,), xbmc.LOGWARNING)
        return []


def _enrich(items):
    """Fill in name/poster for merged items that have none - the played
    bands on a logged-out device, where no library entry supplied them.

    Mirrors `lib.ui.continuewatching.open_continue_watching()`'s caching
    discipline exactly, and for the same measured reasons: each item is
    fetched with `views._fetch_meta(..., store=False)` so a cache-miss
    result is not written on its own, and every genuinely fresh result
    from the whole fan-out is written in ONE `store_cached_metas()` call
    afterwards. Cache HITS are deliberately excluded from that batch -
    `store_cached_metas()` re-stamps `ts` on everything it is given, so
    batching hits back in would re-arm every entry's TTL on every reopen
    and the cache would never re-check the addon as long as the screen
    keeps being reopened inside `metacache.TTL_SECONDS`.

    Returns `(metas, items)` filtered to the items that resolved to
    something showable, so the two stay index-aligned for the caller.
    """
    from lib.ui import views

    needs_fetch = [item for item in items if not item.get('name') or not item.get('poster')]
    if not needs_fetch:
        return items

    def _fetch(item):
        was_fresh = []
        meta = views._fetch_meta(
            item['type'], item['id'], store=False,
            on_miss=lambda: was_fresh.append(True),
        )
        return meta, bool(was_fresh)

    fetched = views._map_addons(_fetch, needs_fetch)

    store = get_store()
    cache_dir = getattr(store, 'data_dir', None)
    if cache_dir is not None:
        from lib.ui.metacache import store_cached_metas
        store_cached_metas(cache_dir, [
            (item['type'], item['id'], meta)
            for item, (meta, was_fresh) in zip(needs_fetch, fetched)
            if meta and was_fresh
        ])

    for item, (meta, _was_fresh) in zip(needs_fetch, fetched):
        if not meta:
            continue
        item['name'] = item.get('name') or meta.get('name')
        item['poster'] = item.get('poster') or meta.get('poster')
        item['background'] = item.get('background') or meta.get('background')
        # Kept for resolve_next_up(): the grid never draws `videos`, but
        # re-fetching the same meta later just to answer "which episode
        # is next" would double this screen's addon traffic.
        if item.get('band') == BAND_NEXT_UP and meta.get('videos'):
            item['videos'] = meta['videos']
    return [item for item in items if item.get('name')]


def _label_next_episodes(items):
    """Fill in `next_label` ("S1E3") on every next-up item whose series
    meta `_enrich()` already fetched, so the card can name the episode it
    would actually play rather than a bare "NEXT UP".

    Uses the `videos` list `_enrich()` stashed on the item, so this costs
    no extra addon traffic. An item whose meta never arrived, or whose
    series has no next episode (a finished final season), simply keeps no
    label and the grid falls back to the band name. Mutates `items` in
    place, like the enrich pass above it.
    """
    for item in items:
        if item.get('band') != BAND_NEXT_UP:
            continue
        video = resolve_next_up(item, {'videos': item.get('videos') or []})
        if not video:
            continue
        season, episode = video.get('season'), video.get('episode')
        if season is None or episode is None:
            continue
        item['next_label'] = 'S%dE%d' % (season, episode)


def open_my_stuff():
    """Fetch both sources, merge them, and show the result in the grid.
    Returns True if the caller (HomeWindow) should also close (playback
    started somewhere down the open_detail() chain)."""
    import xbmc

    from lib.ui.compat import L, log, notify
    from lib.ui.uicommon import busy_dialog

    store = get_store()
    with busy_dialog(L(30033)):
        merged = merge_entries(store.get_progress_entries(), _fetch_library_entries(store))
        items = _enrich(merged)
        _label_next_episodes(items)

    if not items:
        notify(L(30030))
        return False

    log('mystuff: opening grid (%d items)' % len(items), xbmc.LOGINFO)
    try:
        from lib.ui.gridwindow import open_grid
        selected = open_grid(group_by_band(items), heading=L(30241))
    except Exception as exc:  # a skin/UI failure must surface, not vanish
        log('mystuff: grid failed to open: %r' % (exc,), xbmc.LOGERROR)
        notify(L(30032))
        return False
    if not selected:
        return False

    from lib.ui.detailwindow import open_detail
    return open_detail(selected.get('type'), selected.get('id'))
