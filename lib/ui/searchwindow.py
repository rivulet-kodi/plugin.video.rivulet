"""open_search(): prompt a query, then show the aggregated results
directly in the coverflow overlay (`lib.ui.infowindow`) - Rivulet's
custom replacement for the classical `search()` directory action.

Picking a title has no custom detail screen yet, so it falls back to
the classical `meta` directory (see `lib.ui.catalogpicker`'s module
docstring for why this pattern exists) and signals its caller to close
too.
"""
from lib.store import Store
from lib.stremio.addons import AddonClient, AddonError, iter_catalogs
from lib.ui.uicommon import fallback_to_classical


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

    fallback_to_classical('meta', type=selected.get('type') or 'movie', id=selected.get('id'))
    return True
