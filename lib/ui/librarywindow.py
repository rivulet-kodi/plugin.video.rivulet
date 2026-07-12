"""open_library(): fetch the logged-in user's Stremio library and show it
directly in the coverflow overlay (`lib.ui.infowindow`) - Rivulet's custom
replacement for the classical `library()` directory action. Picking a
title opens `lib.ui.detailwindow` for it.
"""


def open_library():
    """Fetch the logged-in user's library datastore and show it in the
    coverflow. Returns True if the caller (HomeWindow) should also close
    (playback started somewhere down the open_detail() chain)."""
    import xbmc

    from lib.store import Store
    from lib.stremio.api import ApiError, StremioAPI
    from lib.ui.compat import L, addon_profile_dir, log, notify
    from lib.ui.uicommon import busy_dialog

    auth = Store(addon_profile_dir()).get_auth()
    if not auth:
        notify(L(30020))
        return False

    try:
        with busy_dialog(L(30033)):
            entries = StremioAPI().datastore_get(auth.get('authKey'), collection='libraryItem', all=True)
    except ApiError as exc:
        log('librarywindow: datastore_get failed: %r' % (exc,), xbmc.LOGERROR)
        notify(L(30032))
        return False

    metas = [
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
    if not metas:
        notify(L(30030))
        return False

    log('librarywindow: opening library showcase (%d items)' % len(metas), xbmc.LOGINFO)
    try:
        from lib.ui.infowindow import open_showcase
        selected = open_showcase(metas)
    except Exception as exc:  # a skin/UI failure must surface, not vanish
        log('librarywindow: coverflow failed to open: %r' % (exc,), xbmc.LOGERROR)
        notify(L(30032))
        return False
    if not selected:
        return False

    from lib.ui.detailwindow import open_detail
    return open_detail(selected.get('type'), selected.get('id'))
