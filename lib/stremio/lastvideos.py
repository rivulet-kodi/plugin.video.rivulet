# SPDX-FileCopyrightText: 2026 M0Rf30
# SPDX-License-Identifier: MIT

"""New episodes of library series: the source of Stremio's notifications.

stremio-core keeps a caught-up series on Continue once an episode it has
not seen airs (``models/continue_watching_preview.rs`` lists every
library item with a notification). The notifications come from the
``lastVideosIds`` extra of the user's catalogs (Cinemeta's
``series/last-videos``): one request per catalog for up to its
``optionsLimit`` series ids, answering ``metasDetailed``, each series'
full meta with its ``videos`` (``models/ctx/update_notifications.rs``). This module plans
those requests the way ``AggrRequest::CatalogsFiltered`` does, reduces
the answers to ``{series id: [[video id, released], ...]}`` for
``Store.set_last_videos``, and decides which of those videos are new for
a library record.

Kodi-independent (see ``AGENTS.md``), like the rest of ``lib.stremio``.
"""

from lib.stremio.addons import AddonError, _catalog_extra_names, addon_supports, iter_manifests

#: The catalog extra a notifications catalog declares.
LAST_VIDEOS_EXTRA = 'lastVideosIds'

#: Series ids per request when the catalog sets no lower ``optionsLimit``
#: (stremio-core ``NOTIFICATION_ITEMS_COUNT``).
MAX_IDS = 100


def should_pull(entry):
    """Whether a libraryItem record gets new-episode checks:
    stremio-core ``LibraryItem::should_pull_notifications``. A series
    (or other non-movie, non-``other`` type) in the library, not a
    ``temp`` watch record, with notifications on and no
    ``behaviorHints.defaultVideoId`` (a single-video title)."""
    if not isinstance(entry, dict):
        return False
    if entry.get('removed') or entry.get('temp'):
        return False
    if not isinstance(entry.get('type'), str) or entry.get('type') in ('movie', 'other'):
        return False
    state = entry.get('state') if isinstance(entry.get('state'), dict) else {}
    if state.get('noNotif'):
        return False
    hints = entry.get('behaviorHints') if isinstance(entry.get('behaviorHints'), dict) else {}
    if hints.get('defaultVideoId'):
        return False
    return isinstance(entry.get('_id'), str) and bool(entry.get('_id'))


def pull_ids(entries):
    """``[(id, type)]`` of the records `should_pull` accepts, most
    recently modified first: the order the per-catalog limit trims."""
    kept = [entry for entry in entries or [] if should_pull(entry)]
    kept.sort(key=lambda entry: str(entry.get('_mtime') or ''), reverse=True)
    return [(entry['_id'], entry['type']) for entry in kept]


def _options_limit(catalog):
    extra = catalog.get('extra')
    for prop in extra if isinstance(extra, list) else ():
        if isinstance(prop, dict) and prop.get('name') == LAST_VIDEOS_EXTRA:
            limit = prop.get('optionsLimit')
            if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0:
                return min(limit, MAX_IDS)
    return MAX_IDS


def plan_requests(addons, id_types):
    """``[(transport_url, catalog type, catalog id, extra)]`` to ask.

    Per catalog that declares ``lastVideosIds``: the ids whose type is
    the catalog's and which the addon's catalog resource serves
    (`addon_supports`), first `optionsLimit` of them in `id_types` order,
    then sorted (stremio-core sorts them so addon caches hit) and joined
    with commas."""
    plans = []
    for transport_url, manifest in iter_manifests(addons):
        if not isinstance(transport_url, str) or not transport_url:
            continue
        catalogs = manifest.get('catalogs')
        for catalog in catalogs if isinstance(catalogs, list) else ():
            if not isinstance(catalog, dict) or LAST_VIDEOS_EXTRA not in _catalog_extra_names(catalog):
                continue
            ctype, cid = catalog.get('type'), catalog.get('id')
            if not isinstance(ctype, str) or not isinstance(cid, str) or not ctype or not cid:
                continue
            ids = [sid for sid, stype in id_types
                   if stype == ctype and addon_supports(manifest, 'catalog', stype, sid)]
            ids = sorted(ids[:_options_limit(catalog)])
            if ids:
                plans.append((transport_url, ctype, cid, [(LAST_VIDEOS_EXTRA, ','.join(ids))]))
    return plans


def _released(video):
    released = video.get('released')
    if not isinstance(released, str) or len(released) < 19:
        return None
    return released[:19] + 'Z'


def reduce_metas(metas):
    """``{series id: [[video id, released], ...]}`` from a catalog answer:
    each meta's videos that carry an id and a release time, oldest first."""
    out = {}
    for meta in metas or []:
        if not isinstance(meta, dict) or not isinstance(meta.get('id'), str):
            continue
        videos = meta.get('videos')
        pairs = []
        for video in videos if isinstance(videos, list) else ():
            if not isinstance(video, dict) or not isinstance(video.get('id'), str) or not video['id']:
                continue
            released = _released(video)
            if released is not None:
                pairs.append([video['id'], released])
        if pairs:
            pairs.sort(key=lambda pair: pair[1])
            out[meta['id']] = pairs
    return out


def fetch(client, addons, id_types, log=None):
    """Ask every planned catalog and merge the answers. The first
    catalog that answers a series wins (stremio-core takes the first
    occurrence). A failing catalog is skipped (`log` gets the reason)."""
    result = {}
    for transport_url, ctype, cid, extra in plan_requests(addons, id_types):
        try:
            metas = client.catalog_detailed(transport_url, ctype, cid, extra)
        except AddonError as exc:
            if log is not None:
                log('last videos: %s/%s failed: %s' % (ctype, cid, exc))
            continue
        for sid, pairs in reduce_metas(metas).items():
            result.setdefault(sid, pairs)
    return result


def new_videos(entry, pairs, now_iso):
    """The ``[video id, released]`` pairs of `pairs` that are new for the
    library record `entry`: released after its ``state.lastWatched`` and
    not in the future (stremio-core ``should_retail_video_released``). A
    series never watched has no notifications. Oldest first."""
    if not should_pull(entry) or not isinstance(pairs, list):
        return []
    state = entry.get('state') if isinstance(entry.get('state'), dict) else {}
    last = state.get('lastWatched')
    if not isinstance(last, str) or len(last) < 19:
        return []
    last = last[:19] + 'Z'
    return [
        pair for pair in pairs
        if isinstance(pair, list) and len(pair) == 2
        and isinstance(pair[0], str) and isinstance(pair[1], str)
        and last < pair[1] <= now_iso
    ]
