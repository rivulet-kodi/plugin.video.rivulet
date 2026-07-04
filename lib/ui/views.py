"""Directory-listing views for plugin.video.rivulet.

Each public function here backs one router action. Functions that build a
Kodi directory call addDirectoryItems()/endOfDirectory(); the handful that
are one-shot side effects (login/logout/addon install/remove/settings),
invoked via RunPlugin from inside another listing, finish with
_finish_action() instead.
"""
import re
import sys
from functools import wraps
from urllib.parse import parse_qsl

import xbmc
import xbmcgui
import xbmcplugin

from lib.stremio import addons as addons_lib
from lib.stremio import streaminfo
from lib.stremio.addons import AddonClient, AddonError
from lib.stremio.api import ApiError, StremioAPI
from lib.store import Store
from lib.ui import compat, router
from lib.ui.compat import L, log, notify, set_video_info

_YEAR_RE = re.compile(r'(\d{4})')
_RUNTIME_RE = re.compile(r'(\d+)')

_STORE = None
_CLIENT = None


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


def _meta_item(meta, ctype=None):
    meta = meta or {}
    mtype = meta.get('type') or ctype or 'movie'
    name = meta.get('name') or meta.get('id') or '?'
    li = xbmcgui.ListItem(label=name)

    poster = meta.get('poster')
    background = meta.get('background') or meta.get('logo')
    art = {'fanart': _row_fanart(background)}
    if poster:
        art.update({'poster': poster, 'thumb': poster, 'icon': poster})
    li.setArt(art)

    info = {
        'title': name,
        'plot': meta.get('description'),
        'genre': meta.get('genres') or [],
        'year': _extract_year(meta.get('releaseInfo') or meta.get('released')),
        'mediatype': 'tvshow' if mtype == 'series' else 'movie',
        'duration': _parse_runtime_seconds(meta.get('runtime')),
    }
    if meta.get('imdbRating'):
        info['rating'] = meta.get('imdbRating')
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
    first addon to return a usable object wins."""
    store = _get_store()
    client = _get_client()
    for descriptor in store.get_addons():
        manifest = descriptor.get('manifest') or {}
        if not addons_lib.addon_supports(manifest, 'meta', stype, sid):
            continue
        try:
            result = client.meta(descriptor.get('transportUrl'), stype, sid)
        except AddonError as exc:
            log('views._fetch_meta: %s failed: %r' % (descriptor.get('transportUrl'), exc), xbmc.LOGERROR)
            continue
        if result:
            return result
    return None


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
        url = router.url_for(
            'catalog', transport=transport_url, type=catalog.get('type'), id=catalog.get('id')
        )
        items.append((url, li, True))
    xbmcplugin.addDirectoryItems(handle, items, len(items))
    xbmcplugin.setContent(handle, 'files')
    xbmcplugin.endOfDirectory(handle)


@_safe_listing
def catalog(transport, ctype, cid, extra=None):
    handle = router.ADDON_HANDLE
    store = _get_store()
    client = _get_client()
    try:
        metas = client.catalog(transport, ctype, cid, extra=extra)
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


@_safe_listing
def search():
    handle = router.ADDON_HANDLE
    query = xbmcgui.Dialog().input(L(30001))
    if not query:
        xbmcplugin.endOfDirectory(handle, succeeded=False, updateListing=False, cacheToDisc=False)
        return

    store = _get_store()
    client = _get_client()
    items = []
    for transport_url, manifest, cat in addons_lib.iter_catalogs(store.get_addons(), extra_required='search'):
        try:
            metas = client.catalog(transport_url, cat.get('type'), cat.get('id'), extra=[('search', query)])
        except AddonError as exc:
            log('views.search: %s failed: %r' % (transport_url, exc), xbmc.LOGERROR)
            continue
        if not metas:
            continue
        addon_name = manifest.get('name', '?')
        for meta in metas:
            url, li, is_folder = _meta_item(meta, cat.get('type'))
            li.setLabel('[%s] %s' % (addon_name, li.getLabel()))
            items.append((url, li, is_folder))

    if not items:
        notify(L(30030))
    xbmcplugin.addDirectoryItems(handle, items, len(items))
    xbmcplugin.setContent(handle, 'videos')
    xbmcplugin.endOfDirectory(handle)


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
        label = 'S%02dE%02d - %s' % (video.get('season') or 0, video.get('episode') or 0, title)
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
    pairs = []
    logos = {}
    for descriptor in store.get_addons():
        manifest = descriptor.get('manifest') or {}
        transport_url = descriptor.get('transportUrl')
        if not addons_lib.addon_supports(manifest, 'stream', stype, sid):
            continue
        try:
            results = client.streams(transport_url, stype, sid)
        except AddonError as exc:
            log('views.streams: %s failed: %r' % (transport_url, exc), xbmc.LOGERROR)
            continue
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
            items.append((router.url_for('discover'), li, False))

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
        notify(L(30013))
    except Exception as exc:  # noqa: BLE001 - e.g. protected-addon refusal
        log('views.addon_remove: %r' % (exc,), xbmc.LOGERROR)
        notify(str(exc))
    _finish_action(handle)


def login():
    handle = router.ADDON_HANDLE
    dialog = xbmcgui.Dialog()
    email = dialog.input(L(30020))
    if not email:
        _finish_action(handle, refresh=False)
        return
    password = dialog.input(L(30020), option=xbmcgui.ALPHANUM_HIDE_INPUT)
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
        protected = [a for a in store.get_addons() if (a.get('flags') or {}).get('protected')]
        seen = {a.get('transportUrl') for a in protected}
        merged = list(protected)
        for descriptor in remote_addons:
            if descriptor.get('transportUrl') not in seen:
                merged.append(descriptor)
                seen.add(descriptor.get('transportUrl'))
        store.set_addons(merged)

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
