"""Directory-listing views for plugin.video.rivulet.

Each public function here backs one router action. Functions that build a
Kodi directory call addDirectoryItems()/endOfDirectory(); the handful that
are one-shot side effects (login/logout/addon install/remove/settings),
invoked via RunPlugin from inside another listing, finish with
_finish_action() instead.
"""
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import wraps
from urllib.parse import parse_qsl

import xbmc
import xbmcgui
import xbmcplugin

from lib.store import ConcurrentUpdateError, Store
from lib.stremio import addons as addons_lib
from lib.stremio import streaminfo
from lib.stremio.addons import AddonClient, AddonError
from lib.stremio.api import ApiError, StremioAPI
from lib.ui import compat, router
from lib.ui.compat import L, log, notify, set_video_info

_YEAR_RE = re.compile(r'(\d{4})')
_RUNTIME_RE = re.compile(r'(\d+)')

_STORE = None
_CLIENT = None


#: Cap on concurrent addon HTTP calls per fan-out (search()/streams()/
#: _fetch_meta()) - bounded so a user with dozens of installed addons
#: doesn't spawn dozens of threads at once. Each AddonClient call still
#: carries its own 15s timeout; this only lets that timeout run
#: concurrently across addons instead of serializing N of them one
#: after another.
_MAX_ADDON_WORKERS = 8


def _get_store():
    global _STORE
    if _STORE is None:
        _STORE = Store(compat.addon_profile_dir())
    return _STORE


def _get_client():
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = AddonClient()
    return _CLIENT


def _map_addons(fn, items):
    """Call `fn(item)` once per item in `items`, fanned out across a small
    bounded thread pool instead of one call at a time, and return the
    results in the same order as `items` - a drop-in replacement for
    `[fn(item) for item in items]` that keeps N addons' worth of blocking
    HTTP calls (each with its own 15s timeout) from serializing behind
    each other. `fn` is expected to catch its own `AddonError` (log it,
    return a falsy sentinel) so one addon's failure can never abort the
    others - that per-addon try/except still runs, just inside whichever
    worker thread executes it.
    """
    if not items:
        return []
    if len(items) == 1:
        return [fn(items[0])]
    with ThreadPoolExecutor(max_workers=min(len(items), _MAX_ADDON_WORKERS)) as pool:
        return list(pool.map(fn, items))


def _safe_listing(view):
    """Guard a directory-listing view: on any uncaught error, notify and
    end the directory as failed instead of leaving Kodi hanging."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        try:
            return view(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - last-resort guard for a Kodi directory
            log('views.%s failed: %r' % (view.__name__, exc), xbmc.LOGERROR)
            notify(str(exc) or view.__name__)
            xbmcplugin.endOfDirectory(router.ADDON_HANDLE, succeeded=False)
    return wrapper


def _finish_action(handle, refresh=True):
    """End a RunPlugin-style script action (login/logout/addon mgmt/settings)."""
    xbmcplugin.endOfDirectory(handle, succeeded=True, updateListing=False, cacheToDisc=False)
    if refresh:
        xbmc.executebuiltin('Container.Refresh')


def _folder_item(label, url, icon=None, fanart=None):
    li = xbmcgui.ListItem(label=label)
    art = {'fanart': fanart or compat.addon_fanart()}
    if icon:
        art.update({'icon': icon, 'thumb': icon})
    li.setArt(art)
    return (url, li, True)


def _action_item(label, url, icon=None):
    """A RunPlugin-style action row (login/logout/install/...): gets art
    like any other row but keeps isFolder=False, so Kodi runs it in place
    instead of pushing it onto the navigation stack."""
    li = xbmcgui.ListItem(label=label)
    art = {'fanart': compat.addon_fanart()}
    if icon:
        art.update({'icon': icon, 'thumb': icon})
    li.setArt(art)
    return (url, li, False)


def _row_fanart(background=None):
    """Fanart for a directory row: addon/catalog-provided background art
    when there is one, else Rivulet's own bundled fanart."""
    return background or compat.addon_fanart()


def _content_for_type(ctype):
    if ctype == 'movie':
        return 'movies'
    if ctype == 'series':
        return 'tvshows'
    return 'videos'


def _extract_year(value):
    if not value:
        return None
    match = _YEAR_RE.search(str(value))
    return int(match.group(1)) if match else None


def _parse_runtime_seconds(runtime):
    if not runtime:
        return None
    match = _RUNTIME_RE.search(str(runtime))
    return int(match.group(1)) * 60 if match else None


def _date_only(value):
    return value.split('T', 1)[0] if value else None


def _parse_extra(extra):
    """Decode a "name=value&name2=value2" extra blob back to (name, value)
    pairs so we can tweak it (e.g. bump skip=) before re-encoding it."""
    if not extra:
        return []
    return parse_qsl(extra, keep_blank_values=True)


def _catalog_declares_extra(manifest, ctype, cid, extra_name):
    for cat in (manifest or {}).get('catalogs') or []:
        if cat.get('type') == ctype and cat.get('id') == cid:
            return any(e.get('name') == extra_name for e in (cat.get('extra') or []))
    return False


def _find_manifest(store, transport_url):
    for descriptor in store.get_addons():
        if descriptor.get('transportUrl') == transport_url:
            return descriptor.get('manifest') or {}
    return None


def _decorate_label(name, date=None, rating=None):
    """Render a catalog/search row label as "Name   [date]   [rating]",
    mirroring the reference addon's (Stream4Me) showcase style: the
    premiered date and the rating in square brackets, spaced apart, plain
    text (the skin colours the header). The date is dropped when unknown;
    the rating always shows, defaulting to 0.0 -- matching the reference,
    which renders [0.0] for an unrated title.
    """
    parts = [name]
    if date:
        parts.append('[%s]' % date)
    try:
        rating_val = float(rating)
    except (TypeError, ValueError):
        rating_val = 0.0
    parts.append('[%.1f]' % rating_val)
    return '   '.join(parts)


def _meta_item(meta, ctype=None):
    meta = meta or {}
    mtype = meta.get('type') or ctype or 'movie'
    name = meta.get('name') or meta.get('id') or '?'
    year = _extract_year(meta.get('releaseInfo') or meta.get('released'))
    rating = meta.get('imdbRating')
    date = _date_only(meta.get('released')) or meta.get('releaseInfo')
    li = xbmcgui.ListItem(label=_decorate_label(name, date, rating))

    poster = meta.get('poster')
    logo = meta.get('logo')
    background = meta.get('background') or logo
    art = {'fanart': _row_fanart(background)}
    if poster:
        art.update({'poster': poster, 'thumb': poster, 'icon': poster})
    if logo:
        art['clearlogo'] = logo
    if meta.get('landscape'):
        art['landscape'] = meta.get('landscape')
    if meta.get('banner'):
        art['banner'] = meta.get('banner')
    li.setArt(art)

    info = {
        'title': name,
        'originaltitle': meta.get('name'),
        'plot': meta.get('description'),
        'genre': meta.get('genres') or [],
        'year': year,
        'mediatype': 'tvshow' if mtype == 'series' else 'movie',
        'duration': _parse_runtime_seconds(meta.get('runtime')),
    }
    if rating:
        info['rating'] = rating
    if meta.get('certification'):
        info['mpaa'] = meta.get('certification')
    if meta.get('country'):
        info['country'] = meta.get('country')
    if meta.get('director'):
        info['director'] = meta.get('director')
    if meta.get('writer'):
        info['writer'] = meta.get('writer')
    if meta.get('tagline'):
        info['plotoutline'] = meta.get('tagline')
    set_video_info(li, info)

    url = router.url_for('meta', type=mtype, id=meta.get('id'))
    return (url, li, True)


def _stream_item(info, stream, stype, sid, poster=None, title=None, logo=None):
    """Build a (url, ListItem, False) tuple for one parsed stream result.

    `poster`/`title` come from the calling meta/videos view (poster
    continuity); `logo` is that stream's addon manifest logo, used only
    when the caller passed no poster of its own.
    """
    stream = stream or {}
    label = streaminfo.format_label(info) or info.get('raw') or info.get('addon') or '?'
    # Defensive: format_label() never emits '\n', but never trust upstream
    # data enough to let a stray newline wrap a Kodi list row onto two lines.
    label = label.replace('\r', ' ').replace('\n', ' ')

    li = xbmcgui.ListItem(label=label)
    li.setProperty('IsPlayable', 'true')

    thumb = poster or logo or 'DefaultVideo.png'
    li.setArt({'icon': thumb, 'thumb': thumb, 'fanart': compat.addon_fanart()})

    set_video_info(li, {
        'title': title or info.get('title') or label,
        'plot': streaminfo.format_plot(info),
        'mediatype': 'episode' if stype == 'series' else 'movie',
    })
    behavior_hints = stream.get('behaviorHints') or {}
    if behavior_hints.get('videoSize'):
        li.setProperty('size', str(behavior_hints['videoSize']))
    elif info.get('size_bytes'):
        li.setProperty('size', str(info['size_bytes']))

    url = router.url_for('play', stream=router.encode_stream(stream), type=stype, id=sid)
    return (url, li, False)


def _fetch_meta(stype, sid):
    """Aggregate meta across every installed addon supporting it for
    (stype, sid); Stremio addons commonly disagree on coverage, so the
    first addon to return a usable object wins.

    Every eligible addon is queried concurrently rather than one at a
    time - each is a blocking HTTP call with its own 15s timeout, so a
    sequential loop over N addons could stall the UI for up to N x 15s.
    We return the instant a usable result is ready instead of waiting
    for every addon to answer.

    Preference order: the old sequential loop always returned the first
    addon (in store.get_addons() order) with a usable result, since it
    never even called later addons once one hit. With real concurrency
    every eligible addon is called up front, so that exact guarantee is
    no longer possible in general - but we still prefer the earliest
    addon among whichever have *already* answered by the time we check
    (a cheap, non-blocking snapshot), so if the winning addon is at
    least as fast as the others, the result is identical to before. If
    the earliest-preference addon happens to be the slow one, a faster
    later addon wins instead of blocking on it - strict list-order
    preference is sacrificed on purpose in that case, since waiting on
    the slowest addon ahead of one that already answered is exactly the
    freeze this function exists to avoid. Addons still in flight when we
    return are abandoned, not cancelled (Future.cancel() only works
    before a thread starts running) - they keep running to completion or
    their own 15s timeout in a background thread we no longer wait on.
    """
    store = _get_store()
    client = _get_client()
    targets = [
        descriptor for descriptor in store.get_addons()
        if addons_lib.addon_supports(descriptor.get('manifest') or {}, 'meta', stype, sid)
    ]
    if not targets:
        return None

    def _fetch_one(descriptor):
        transport_url = descriptor.get('transportUrl')
        try:
            return client.meta(transport_url, stype, sid)
        except AddonError as exc:
            log('views._fetch_meta: %s failed: %r' % (transport_url, exc), xbmc.LOGWARNING)
            return None

    if len(targets) == 1:
        return _fetch_one(targets[0])

    pool = ThreadPoolExecutor(max_workers=min(len(targets), _MAX_ADDON_WORKERS))
    futures = []
    try:
        futures = [pool.submit(_fetch_one, descriptor) for descriptor in targets]
        index_of = {future: index for index, future in enumerate(futures)}
        for future in as_completed(futures):
            if future.result() is None:
                continue
            winner = future
            # Only promotes to a future that has *already* finished (a
            # non-blocking .done() check) - never waits on one still running.
            for other in futures:
                if (index_of[other] < index_of[winner] and other.done()
                        and other.result() is not None):
                    winner = other
            return winner.result()
        return None
    finally:
        # Drop any addon call that never got a worker thread (only
        # possible when len(targets) > _MAX_ADDON_WORKERS); already-running
        # calls are left to finish in the background. wait=False so this
        # cleanup never blocks the caller on a straggler.
        for future in futures:
            future.cancel()
        pool.shutdown(wait=False)


def _ordered_seasons(videos):
    seasons = sorted({v.get('season') for v in videos if v.get('season') is not None})
    if 0 in seasons:
        seasons.remove(0)
        seasons.append(0)
    return seasons


# --------------------------------------------------------------------------
# Router actions
# --------------------------------------------------------------------------

@_safe_listing
def home():
    handle = router.ADDON_HANDLE
    store = _get_store()
    items = [
        _folder_item(L(30000), router.url_for('discover'), compat.addon_media_path('discover.png')),
        _folder_item(L(30001), router.url_for('search'), compat.addon_media_path('search.png')),
    ]
    if store.get_auth():
        items.append(_folder_item(L(30002), router.url_for('library'), compat.addon_media_path('library.png')))
    items.append(_folder_item(L(30003), router.url_for('addons'), compat.addon_media_path('addons.png')))
    items.append(_action_item(L(30004), router.url_for('settings'), compat.addon_media_path('settings.png')))
    xbmcplugin.addDirectoryItems(handle, items, len(items))
    xbmcplugin.setContent(handle, 'files')
    xbmcplugin.setPluginCategory(handle, compat.ADDON_NAME)
    xbmcplugin.endOfDirectory(handle)


def open_settings():
    compat.ADDON.openSettings()
    xbmcplugin.endOfDirectory(router.ADDON_HANDLE, succeeded=False, updateListing=False, cacheToDisc=False)


@_safe_listing
def discover():
    handle = router.ADDON_HANDLE
    store = _get_store()
    items = []
    for transport_url, manifest, catalog in addons_lib.iter_catalogs(store.get_addons()):
        addon_name = manifest.get('name', '?')
        catalog_name = catalog.get('name') or catalog.get('id')
        label = '%s: %s (%s)' % (addon_name, catalog_name, catalog.get('type'))
        li = xbmcgui.ListItem(label=label)
        logo = manifest.get('logo')
        art = {'fanart': _row_fanart(manifest.get('background'))}
        if logo:
            art.update({'icon': logo, 'thumb': logo})
        li.setArt(art)
        showcase_url = router.url_for(
            'showcase', transport=transport_url, type=catalog.get('type'), id=catalog.get('id')
        )
        li.addContextMenuItems([(L(30026), 'RunPlugin(%s)' % showcase_url)])
        url = router.url_for(
            'catalog', transport=transport_url, type=catalog.get('type'), id=catalog.get('id')
        )
        items.append((url, li, True))
    xbmcplugin.addDirectoryItems(handle, items, len(items))
    xbmcplugin.setContent(handle, 'files')
    xbmcplugin.endOfDirectory(handle)


def _fetch_catalog(transport, ctype, cid, extra=None):
    """Fetch one catalog's metas via the shared AddonClient - the exact
    call catalog() and showcase() both build their listing/overlay from.
    Raises AddonError on failure; callers decide how to surface it
    (catalog() ends the directory as failed, showcase() notifies)."""
    client = _get_client()
    return client.catalog(transport, ctype, cid, extra=extra)


@_safe_listing
def catalog(transport, ctype, cid, extra=None):
    handle = router.ADDON_HANDLE
    store = _get_store()
    try:
        metas = _fetch_catalog(transport, ctype, cid, extra)
    except AddonError as exc:
        log('views.catalog: %s %s/%s failed: %r' % (transport, ctype, cid, exc), xbmc.LOGERROR)
        notify(str(exc))
        xbmcplugin.endOfDirectory(handle, succeeded=False)
        return

    items = [_meta_item(meta, ctype) for meta in (metas or [])]

    if metas:
        manifest = _find_manifest(store, transport)
        if manifest and _catalog_declares_extra(manifest, ctype, cid, 'skip'):
            extra_pairs = _parse_extra(extra)
            current_skip = 0
            for name, value in extra_pairs:
                if name == 'skip':
                    try:
                        current_skip = int(value)
                    except ValueError:
                        current_skip = 0
            next_pairs = [(k, v) for k, v in extra_pairs if k != 'skip']
            next_pairs.append(('skip', str(current_skip + len(metas))))
            next_extra = addons_lib.encode_extra(next_pairs)
            next_url = router.url_for('catalog', transport=transport, type=ctype, id=cid, extra=next_extra)
            items.append(_folder_item(L(30040), next_url, 'DefaultFolder.png'))

    if not items:
        notify(L(30030))
    xbmcplugin.addDirectoryItems(handle, items, len(items))
    xbmcplugin.setContent(handle, _content_for_type(ctype))
    xbmcplugin.endOfDirectory(handle)


def showcase(transport, ctype, cid, extra=None):
    """RunPlugin action (Discover's "Showcase" context-menu item): open a
    fullscreen coverflow overlay (lib.ui.infowindow.ShowcaseWindow) over
    one catalog's metas - fetched exactly like catalog() - and, if the
    user picks a title, navigate the current directory there.

    A one-shot side effect, not a directory listing: unlike catalog() it
    never touches xbmcplugin, so it is deliberately not @_safe_listing.
    Any overlay/skin failure is logged and surfaced as a notification
    rather than vanishing silently.
    """
    try:
        metas = _fetch_catalog(transport, ctype, cid, extra)
    except AddonError as exc:
        log('views.showcase: %s %s/%s failed: %r' % (transport, ctype, cid, exc), xbmc.LOGERROR)
        notify(str(exc))
        return

    if not metas:
        notify(L(30030))
        return

    log('views.showcase: opening coverflow for %s/%s (%d items)' % (ctype, cid, len(metas)), xbmc.LOGINFO)
    try:
        from lib.ui.infowindow import open_showcase
        selected = open_showcase(metas)
    except Exception as exc:  # a skin/UI failure must surface, not vanish
        log('views.showcase: overlay failed to open: %r' % (exc,), xbmc.LOGERROR)
        notify(L(30032))
        return

    if selected:
        xbmc.executebuiltin('Container.Update(%s)' % router.url_for(
            'meta', type=selected.get('type') or ctype, id=selected.get('id')
        ))


@_safe_listing
def search():
    handle = router.ADDON_HANDLE
    query = xbmcgui.Dialog().input(L(30001))
    if not query:
        xbmcplugin.endOfDirectory(handle, succeeded=False, updateListing=False, cacheToDisc=False)
        return

    store = _get_store()
    store.add_search_query(query)
    client = _get_client()
    catalog_targets = list(addons_lib.iter_catalogs(store.get_addons(), extra_required='search'))

    def _fetch_catalog_result(target):
        transport_url, _manifest, cat = target
        try:
            return client.catalog(transport_url, cat.get('type'), cat.get('id'), extra=[('search', query)])
        except AddonError as exc:
            log('views.search: %s failed: %r' % (transport_url, exc), xbmc.LOGWARNING)
            return None

    metas = []
    fetched = _map_addons(_fetch_catalog_result, catalog_targets)
    for (_transport_url, _manifest, cat), results in zip(catalog_targets, fetched):
        for meta_obj in results or []:
            meta_obj['type'] = meta_obj.get('type') or cat.get('type')
            metas.append(meta_obj)

    if not metas:
        notify(L(30030))
        xbmcplugin.endOfDirectory(handle, succeeded=False, updateListing=False, cacheToDisc=False)
        return

    # Search results are presented directly in the coverflow showcase overlay
    # (the default search UX). Dismiss Kodi's GetDirectory busy dialog first so
    # the modal is interactable, mirroring the reference addon's prevent_busy().
    xbmc.executebuiltin('Dialog.Close(all, true)')
    try:
        from lib.ui.infowindow import open_showcase
        selected = open_showcase(metas)
    except Exception as exc:  # a skin/UI failure must surface, not vanish
        log('views.search: showcase overlay failed: %r' % (exc,), xbmc.LOGERROR)
        notify(L(30032))
        xbmcplugin.endOfDirectory(handle, succeeded=False, updateListing=False, cacheToDisc=False)
        return

    if not selected:
        xbmcplugin.endOfDirectory(handle, succeeded=False, updateListing=False, cacheToDisc=False)
        return

    # Picking a poster resolves this search directory into that title's content.
    meta(selected.get('type') or 'movie', selected.get('id'))


@_safe_listing
def meta(stype, sid):
    handle = router.ADDON_HANDLE
    meta_obj = _fetch_meta(stype, sid)
    if not meta_obj:
        notify(L(30030))
        xbmcplugin.endOfDirectory(handle, succeeded=False)
        return

    videos = meta_obj.get('videos') or []
    if not videos:
        # Movies (and channel/tv/anything without a video list) go straight
        # to the stream picker instead of an intermediate listing.
        return streams(stype, sid, poster=meta_obj.get('poster'), title=meta_obj.get('name'))

    seasons = _ordered_seasons(videos)
    poster = meta_obj.get('poster')
    background = meta_obj.get('background') or meta_obj.get('logo') or poster
    show_name = meta_obj.get('name')

    items = []
    for season in seasons:
        label = 'Specials' if season == 0 else 'Season %d' % season
        li = xbmcgui.ListItem(label=label)
        art = {'fanart': _row_fanart(background)}
        if poster:
            art.update({'poster': poster, 'thumb': poster})
        li.setArt(art)
        set_video_info(li, {
            'title': label, 'tvshowtitle': show_name, 'season': season, 'mediatype': 'season',
        })
        url = router.url_for('videos', type=stype, id=sid, season=str(season))
        items.append((url, li, True))

    xbmcplugin.addDirectoryItems(handle, items, len(items))
    xbmcplugin.setContent(handle, 'seasons')
    xbmcplugin.endOfDirectory(handle)


@_safe_listing
def videos(stype, sid, season):
    handle = router.ADDON_HANDLE
    meta_obj = _fetch_meta(stype, sid)
    if not meta_obj:
        notify(L(30030))
        xbmcplugin.endOfDirectory(handle, succeeded=False)
        return

    try:
        season_num = int(season)
    except (TypeError, ValueError):
        season_num = None

    show_name = meta_obj.get('name')
    fallback_thumb = meta_obj.get('poster')
    fallback_fanart = _row_fanart(meta_obj.get('background') or meta_obj.get('logo') or fallback_thumb)
    episodes = [v for v in (meta_obj.get('videos') or []) if v.get('season') == season_num]
    episodes.sort(key=lambda v: v.get('episode') or 0)

    items = []
    for video in episodes:
        title = video.get('title') or video.get('name') or video.get('id') or '?'
        label = '%dx%02d. %s' % (video.get('season') or 0, video.get('episode') or 0, title)
        li = xbmcgui.ListItem(label=label)
        thumb = video.get('thumbnail') or fallback_thumb
        art = {'fanart': fallback_fanart}
        if thumb:
            art.update({'thumb': thumb, 'icon': thumb})
        li.setArt(art)
        set_video_info(li, {
            'title': title,
            'tvshowtitle': show_name,
            'season': video.get('season'),
            'episode': video.get('episode'),
            'plot': video.get('overview'),
            'aired': _date_only(video.get('released')),
            'mediatype': 'episode',
        })
        url = router.url_for(
            'streams', type=stype, id=video.get('id') or sid, poster=thumb, title=label
        )
        items.append((url, li, True))

    xbmcplugin.addDirectoryItems(handle, items, len(items))
    xbmcplugin.setContent(handle, 'episodes')
    xbmcplugin.endOfDirectory(handle)


def _stream_query_extras():
    """Best-effort read of extra plugin-request query params (poster/title)
    that router.run()'s per-action dispatch table doesn't forward
    positionally to streams() -- see the `videos()` call site, which puts
    them on the URL instead of calling this view directly.
    """
    try:
        raw_qs = sys.argv[2]
    except IndexError:
        return {}
    if raw_qs.startswith('?'):
        raw_qs = raw_qs[1:]
    return dict(_parse_extra(raw_qs))


@_safe_listing
def streams(stype, sid, poster=None, title=None):
    handle = router.ADDON_HANDLE
    if poster is None or title is None:
        extra = _stream_query_extras()
        poster = poster or extra.get('poster')
        title = title or extra.get('title')

    store = _get_store()
    client = _get_client()
    targets = [
        descriptor for descriptor in store.get_addons()
        if addons_lib.addon_supports(descriptor.get('manifest') or {}, 'stream', stype, sid)
    ]

    def _fetch_streams(descriptor):
        transport_url = descriptor.get('transportUrl')
        try:
            return client.streams(transport_url, stype, sid)
        except AddonError as exc:
            log('views.streams: %s failed: %r' % (transport_url, exc), xbmc.LOGWARNING)
            return None

    pairs = []
    logos = {}
    for descriptor, results in zip(targets, _map_addons(_fetch_streams, targets)):
        if results is None:
            continue
        manifest = descriptor.get('manifest') or {}
        addon_name = manifest.get('name', '?')
        logos.setdefault(streaminfo.clean_text(addon_name), manifest.get('logo'))
        for stream in results or []:
            pairs.append((streaminfo.parse_stream(stream, addon_name=addon_name), stream))

    if not pairs:
        notify(L(30030))

    sort_key = compat.ADDON.getSetting('stream_sort') or 'quality'
    pairs = streaminfo.sort_streams(pairs, key=sort_key)

    items = [
        _stream_item(info, stream, stype, sid, poster=poster, title=title, logo=logos.get(info['addon']))
        for info, stream in pairs
    ]
    xbmcplugin.addDirectoryItems(handle, items, len(items))
    xbmcplugin.setContent(handle, 'videos')
    xbmcplugin.addSortMethod(handle, xbmcplugin.SORT_METHOD_NONE)
    xbmcplugin.endOfDirectory(handle)


@_safe_listing
def addons():
    handle = router.ADDON_HANDLE
    store = _get_store()
    items = []

    for descriptor in store.get_addons():
        manifest = descriptor.get('manifest') or {}
        flags = descriptor.get('flags') or {}
        transport_url = descriptor.get('transportUrl')
        label = '%s v%s' % (manifest.get('name', '?'), manifest.get('version', '?'))
        li = xbmcgui.ListItem(label=label)
        logo = manifest.get('logo')
        art = {'fanart': _row_fanart(manifest.get('background'))}
        if logo:
            art.update({'icon': logo, 'thumb': logo})
        li.setArt(art)
        set_video_info(li, {'title': label, 'plot': manifest.get('description', '')})
        if not flags.get('protected'):
            remove_url = router.url_for('addon_remove', transport=transport_url)
            li.addContextMenuItems([(L(30011), 'RunPlugin(%s)' % remove_url)])
            items.append((remove_url, li, False))
        else:
            items.append((router.url_for('discover'), li, True))

    items.append(_action_item(L(30010), router.url_for('addon_install'), 'DefaultAddonNone.png'))

    auth = store.get_auth()
    if auth:
        user = auth.get('user') or {}
        label = L(30022) % (user.get('email') or user.get('name') or '?')
        items.append(_action_item(label, router.url_for('logout'), 'DefaultAddonService.png'))
    else:
        items.append(_action_item(L(30020), router.url_for('login'), 'DefaultAddonService.png'))

    xbmcplugin.addDirectoryItems(handle, items, len(items))
    xbmcplugin.setContent(handle, 'files')
    xbmcplugin.endOfDirectory(handle)


def _sync_addons_if_logged_in(store, notify_success=False):
    """Best-effort push of the local addon collection back to Stremio's
    remote sync API when the user is logged in. A failed push is
    notified (not just logged) - previously silent, which made a real
    failure indistinguishable from "nothing to sync"/"working fine".
    Never blocks or fails the local install/remove/login that triggered
    it. Returns True on a successful push (or when there is nothing to
    do because the user isn't logged in and `notify_success` is False),
    False on failure.

    A failure whose `ApiError.is_auth_error` is true (401/403 - the
    authKey itself was invalidated server-side, not a transient blip)
    additionally clears the stored auth via `store.set_auth(None)`, since
    retrying the same dead key can never succeed; the next user-facing
    screen (Library, Addons, Settings > Account) then correctly shows
    "not logged in" instead of repeating this failure forever."""
    auth = store.get_auth()
    if not auth:
        if notify_success:
            notify(L(30020))
        return False
    try:
        StremioAPI().addon_collection_set(auth.get('authKey'), store.get_addons())
    except ApiError as exc:
        log('views._sync_addons_if_logged_in: %r' % (exc,), xbmc.LOGERROR)
        if exc.is_auth_error:
            # Clear the dead authKey so the next user-facing screen (Library,
            # Addons, Settings > Account) shows "not logged in" instead of
            # retrying the same bad token forever. Reuse the existing
            # generic failure notification below rather than library()'s
            # dedicated re-login prompt: this also runs from background
            # install/remove/login paths, where a "session expired" popup
            # would be out of context.
            store.set_auth(None)
        notify(L(30035))
        return False
    if notify_success:
        notify(L(30034))
    return True

def sync_addons_now():
    """RunPlugin action (Settings > Account > Sync addons now): force a
    push of the local addon collection, with explicit feedback either
    way - unlike the automatic post-install/remove/login push, a
    manually-triggered sync must confirm success too, not just surface
    failures. Also refreshes every installed addon's cached manifest
    from its own transportUrl first (see `_refresh_addon_manifests`), so
    a freshly-updated local manifest set - not a stale install-time
    snapshot - is what gets pushed to the account."""
    handle = router.ADDON_HANDLE
    store = _get_store()
    _refresh_addon_manifests(store, _get_client())
    _sync_addons_if_logged_in(store, notify_success=True)
    _finish_action(handle, refresh=False)


def _refresh_addon_manifests(store, client):
    """Best-effort refresh of every installed addon's cached manifest from
    its own transportUrl, so catalog/resource/logo/version changes the
    remote addon makes after install time eventually reach the local
    cache instead of staying stale forever - previously the only fix was
    to manually remove and reinstall the addon. Mirrors
    `_sync_addons_if_logged_in`'s best-effort philosophy: one addon being
    briefly unreachable (`AddonError`) or returning a manifest too
    malformed to use (no `id`) never aborts refreshing the others and
    never disturbs that addon's last-known-good cached manifest.
    Persisted via `Store.update_addons` (never a raw `get_addons()` +
    `set_addons()` pair) so the write stays safe against a concurrent
    `default.py` process changing addons.json at the same time.
    """
    descriptors = store.get_addons()
    if not descriptors:
        return

    def _fetch(descriptor):
        transport_url = descriptor.get('transportUrl')
        if not transport_url:
            return None
        try:
            return client.manifest(transport_url)
        except AddonError as exc:
            log('views._refresh_addon_manifests: %s failed: %r' % (transport_url, exc), xbmc.LOGWARNING)
            return None

    fetched = _map_addons(_fetch, descriptors)
    refreshed = {}
    for descriptor, manifest in zip(descriptors, fetched):
        if manifest and manifest.get('id') and manifest != descriptor.get('manifest'):
            refreshed[descriptor.get('transportUrl')] = manifest

    if not refreshed:
        return

    def _apply(addons):
        return [
            dict(addon, manifest=refreshed[addon.get('transportUrl')])
            if addon.get('transportUrl') in refreshed else addon
            for addon in addons
        ]

    store.update_addons(_apply)



def addon_install():
    handle = router.ADDON_HANDLE
    url = xbmcgui.Dialog().input(L(30010))
    if not url:
        _finish_action(handle, refresh=False)
        return

    try:
        manifest = _get_client().manifest(url)
    except AddonError as exc:
        log('views.addon_install: manifest fetch failed for %s: %r' % (url, exc), xbmc.LOGERROR)
        notify(L(30014))
        _finish_action(handle, refresh=False)
        return

    if not manifest or not manifest.get('id'):
        notify(L(30014))
        _finish_action(handle, refresh=False)
        return

    _get_store().install_addon(url, manifest)
    _sync_addons_if_logged_in(_get_store())
    notify(L(30012))
    _finish_action(handle)


def addon_remove(transport):
    handle = router.ADDON_HANDLE
    if not transport:
        _finish_action(handle, refresh=False)
        return

    if not xbmcgui.Dialog().yesno(L(30011), L(30011)):
        _finish_action(handle, refresh=False)
        return

    try:
        _get_store().remove_addon(transport)
        _sync_addons_if_logged_in(_get_store())
        notify(L(30013))
    except Exception as exc:  # noqa: BLE001 - e.g. protected-addon refusal
        log('views.addon_remove: %r' % (exc,), xbmc.LOGERROR)
        notify(str(exc))
    _finish_action(handle)


def login():
    handle = router.ADDON_HANDLE
    dialog = xbmcgui.Dialog()
    email = dialog.input(L(30024))
    if not email:
        _finish_action(handle, refresh=False)
        return
    password = dialog.input(L(30025), option=xbmcgui.ALPHANUM_HIDE_INPUT)
    if not password:
        _finish_action(handle, refresh=False)
        return

    api = StremioAPI()
    try:
        result = api.login(email, password)
    except ApiError as exc:
        log('views.login failed: %r' % (exc,), xbmc.LOGERROR)
        notify(L(30023))
        _finish_action(handle, refresh=False)
        return

    store = _get_store()
    store.set_auth(result)

    try:
        remote_addons = api.addon_collection_get(result.get('authKey'))
    except ApiError as exc:
        log('views.login: addon_collection_get failed: %r' % (exc,), xbmc.LOGERROR)
        remote_addons = None

    if remote_addons is not None:
        def _merge_with_remote(local_addons):
            # Union, not filter: EVERY local addon (protected or not) must
            # survive login. The previous version kept only protected ones,
            # silently dropping any community addon installed while logged
            # out - the store's local state must never regress on login.
            # Re-run against a freshly-read `local_addons` on every retry
            # (see Store.update_addons), so a concurrent install/remove
            # racing this login is merged rather than clobbered.
            seen = {a.get('transportUrl') for a in local_addons}
            merged = list(local_addons)
            for descriptor in remote_addons:
                if descriptor.get('transportUrl') not in seen:
                    merged.append(descriptor)
                    seen.add(descriptor.get('transportUrl'))
            return merged

        try:
            store.update_addons(_merge_with_remote)
        except ConcurrentUpdateError as exc:
            log('views.login: addon merge failed: %r' % (exc,), xbmc.LOGERROR)
        else:
            # Push the merged list right back up: closes the gap where an
            # addon installed before ever logging in would otherwise never
            # reach the account until its next unrelated install/remove.
            _sync_addons_if_logged_in(store)

    user = result.get('user') or {}
    notify(L(30022) % (user.get('email') or user.get('name') or ''))
    _finish_action(handle)


def logout():
    handle = router.ADDON_HANDLE
    store = _get_store()
    auth = store.get_auth()
    if not auth:
        _finish_action(handle, refresh=False)
        return
    if not xbmcgui.Dialog().yesno(L(30021), L(30021)):
        _finish_action(handle, refresh=False)
        return

    try:
        StremioAPI().logout(auth.get('authKey'))
    except ApiError as exc:
        log('views.logout: %r' % (exc,), xbmc.LOGERROR)

    store.set_auth(None)
    _finish_action(handle)


@_safe_listing
def library():
    handle = router.ADDON_HANDLE
    store = _get_store()
    items = []
    auth = store.get_auth()
    if auth:
        try:
            entries = StremioAPI().datastore_get(auth.get('authKey'), collection='libraryItem', all=True)
        except ApiError as exc:
            log('views.library: datastore_get failed: %r' % (exc,), xbmc.LOGERROR)
            if exc.is_auth_error:
                store.set_auth(None)
                notify(L(30085))
            entries = []
        for entry in entries or []:
            if entry.get('removed'):
                continue
            name = entry.get('name') or entry.get('_id')
            li = xbmcgui.ListItem(label=name)
            poster = entry.get('poster')
            art = {'fanart': _row_fanart(entry.get('background'))}
            if poster:
                art.update({'poster': poster, 'thumb': poster, 'icon': poster})
            li.setArt(art)
            entry_type = entry.get('type')
            set_video_info(li, {
                'title': name, 'mediatype': 'tvshow' if entry_type == 'series' else 'movie',
            })
            url = router.url_for('meta', type=entry_type, id=entry.get('_id'))
            items.append((url, li, True))

    xbmcplugin.addDirectoryItems(handle, items, len(items))
    xbmcplugin.setContent(handle, 'videos')
    xbmcplugin.endOfDirectory(handle)
