"""open_search(): prompt a query, then show the aggregated results
directly in the coverflow overlay (`lib.ui.infowindow`) - Rivulet's
custom replacement for the classical `search()` directory action.
Picking a title opens `lib.ui.detailwindow` for it.
"""
from lib.store import Store
from lib.stremio.addons import AddonClient, AddonError, iter_catalogs


def open_search():
    """Prompt for a search query and show the aggregated results in the
    coverflow. Returns True if the caller should also close (a
    classical-fallback navigation happened)."""
    import xbmc
    import xbmcgui

    from lib.ui.compat import L, addon_profile_dir, log, notify

    query = xbmcgui.Dialog().input(L(30001))
    if not query:
        return False

    store = Store(addon_profile_dir())
    client = AddonClient()
    metas = []
    for transport_url, _manifest, cat in iter_catalogs(store.get_addons(), extra_required='search'):
        try:
            results = client.catalog(transport_url, cat.get('type'), cat.get('id'), extra=[('search', query)])
        except AddonError as exc:
            log('searchwindow: %s failed: %r' % (transport_url, exc), xbmc.LOGERROR)
            continue
        for meta_obj in results or []:
            meta_obj['type'] = meta_obj.get('type') or cat.get('type')
            metas.append(meta_obj)

    if not metas:
        notify(L(30030))
        return False

    from lib.ui.infowindow import open_showcase
    selected = open_showcase(metas)
    if not selected:
        return False

    from lib.ui.detailwindow import open_detail
    return open_detail(selected.get('type') or 'movie', selected.get('id'))
