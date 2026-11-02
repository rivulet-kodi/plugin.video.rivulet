# SPDX-FileCopyrightText: 2026 M0Rf30
# SPDX-License-Identifier: MIT

"""Add to Library / Remove from Library (and, for a movie, Mark as watched /
unwatched) for a poster.

The title page already does this (`detailwindow._toggle_library`). A
poster on Home or a hub has no detail window of its own, so this calls
the same three pieces that menu uses: `get_library_item` to read the
live `removed` flag, `library_add_payload` / `library_remove_payload` to
build the record, and `put_library_item` to `datastore_put` it. Nothing
here changes the API client or the payload builders.

A failed lookup must not be treated as "no record": that used to
overwrite a real server-side item. Signed out, the menu tells the
viewer to sign in and writes nothing.
"""


def meta_from_item(item):
    """The meta `library_add_payload` needs, or None for a chip, a More
    card, or the empty-library notice (those have no title id)."""
    if item is None:
        return None
    sid = item.getProperty('sid') or ''
    if not sid or item.getProperty('action'):
        return None
    name = item.getProperty('hero_title') or item.getLabel() or ''
    return {
        'id': sid,
        'type': item.getProperty('stype') or '',
        'name': name,
        'poster': item.getProperty('thumbnail') or '',
    }


def _lookup(auth, item_id):
    """`(item, lookup_ok)`. `item` is None both when the account has no
    record and when the call failed; `lookup_ok` is what tells those
    apart. Back on the spinner returns `(CANCELLED, False)`."""
    from lib.stremio.addons import addon_error_detail
    from lib.stremio.api import ApiError
    from lib.ui.compat import L, log
    from lib.ui.dependencies import get_api
    from lib.ui.uicommon import CANCELLED, run_cancellable

    try:
        found = run_cancellable(
            lambda: get_api().get_library_item(auth.get('authKey'), item_id), L(30033),
        )
    except ApiError as exc:
        log('libraryaction: lookup failed: %s' % addon_error_detail(exc))
        return None, False
    if found is CANCELLED:
        return found, False
    return found, True


def _push(auth, payload):
    """True when `put_library_item` landed. Back or an `ApiError`
    notifies nothing on cancel and `#30301` on failure."""
    import xbmc

    from lib.stremio.addons import addon_error_detail
    from lib.stremio.api import ApiError
    from lib.ui.compat import L, log, notify
    from lib.ui.dependencies import get_api
    from lib.ui.uicommon import CANCELLED, run_cancellable

    try:
        if run_cancellable(
            lambda: get_api().put_library_item(auth.get('authKey'), payload), L(30033),
        ) is CANCELLED:
            log('libraryaction: write cancelled while pending', xbmc.LOGINFO)
            return False
    except ApiError as exc:
        log('libraryaction: write failed: %s' % addon_error_detail(exc), xbmc.LOGWARNING)
        notify(L(30301))
        return False
    return True


def offer(meta):
    """One-row menu: Add when the title is not in the library, Remove
    when it is. Returns `'added'`, `'removed'`, or None. The toast is
    the same copy the title page uses (`#30297` / `#30298`)."""
    from lib.ui import dialogs
    from lib.ui.compat import L, notify
    from lib.ui.dependencies import get_store
    from lib.ui.uicommon import CANCELLED

    auth = get_store().get_auth()
    if not auth:
        notify(L(30190))
        return None
    item, lookup_ok = _lookup(auth, meta.get('id'))
    if item is CANCELLED:
        return None
    if not lookup_ok:
        notify(L(30302))
        return None
    in_library = bool(item) and not item.get('removed')
    label = L(30294) if in_library else L(30293)
    picked = dialogs.choose(meta.get('name') or meta.get('id') or '', [label])
    if picked != 0:
        return None
    return _write(auth, meta, in_library)


def poster_menu(meta, extra=()):
    """The menu key on a Home or hub poster: `extra` rows first (``(label,
    callable)`` pairs: Go to series on a New Episodes card), then the
    library row, a Mark as watched/unwatched row for a movie, then Trailer.

    A movie has no title page to carry the watched action
    (`detailwindow._toggle_watched` is the series/episode path), and its
    poster now shows the watched check (issue #85), so the check needs a
    way to be set and cleared from where it is drawn. The row's wording
    follows the check (`watchmarks.is_watched`), not the raw account
    record, so the menu never offers the action the badge already shows.

    Signed out, or when the library lookup fails, the menu still offers
    the trailer. Returns what `offer` does, plus `'watched'` and
    `'unwatched'`: `'added'`, `'removed'` or None (a trailer, an extra
    row, or nothing picked).
    """
    from lib.ui import dialogs, watchmarks
    from lib.ui.compat import L
    from lib.ui.dependencies import get_store
    from lib.ui.uicommon import CANCELLED

    auth = get_store().get_auth()
    extra = [(label, action) for label, action in extra or () if label and callable(action)]
    rows = [label for label, _action in extra]
    in_library = None
    library_row = mark_row = None
    watched = watchmarks.is_watched(meta)
    if auth:
        item, lookup_ok = _lookup(auth, meta.get('id'))
        if item is CANCELLED:
            return None
        if lookup_ok:
            in_library = bool(item) and not item.get('removed')
            library_row = len(rows)
            rows.append(L(30294) if in_library else L(30293))
            if meta.get('type') == 'movie':
                mark_row = len(rows)
                rows.append(L(30296) if watched else L(30295))
    trailer_row = len(rows)
    rows.append(L(30499))
    picked = dialogs.choose(meta.get('name') or meta.get('id') or '', rows)
    if picked is None or picked < 0 or picked >= len(rows):
        return None
    if picked < len(extra):
        extra[picked][1]()
        return None
    if picked == trailer_row:
        from lib.ui.trailer import play_trailer

        play_trailer(meta, meta.get('type') or '', meta.get('id') or '')
        return None
    if picked == mark_row:
        return _mark(auth, meta, not watched)
    if picked == library_row:
        return _write(auth, meta, in_library)
    return None


def _mark(auth, meta, watched):
    """Mark `meta` watched or unwatched on the account, with the toast.

    Same fresh-read-then-write discipline as `_write`, and the same
    `detailwindow._toggle_watched` shape: a title the account has no record
    of yet gets the implicit `build_library_item` one, which is only right
    when the lookup truly found nothing (`_lookup`'s `lookup_ok`).
    """
    from lib import library
    from lib.ui.compat import L, notify
    from lib.ui.uicommon import CANCELLED

    fresh, fresh_ok = _lookup(auth, meta.get('id'))
    if fresh is CANCELLED:
        return None
    if not fresh_ok:
        notify(L(30302))
        return None
    base = fresh if isinstance(fresh, dict) else library.build_library_item(meta)
    payload = library.mark_watched_payload(base, watched)
    if not _push(auth, payload):
        return None
    from lib.ui.mystuff import note_library_write
    from lib.ui.newhome import mark_library_dirty

    note_library_write(payload)
    mark_library_dirty()
    notify(L(30299) if watched else L(30300))
    return 'watched' if watched else 'unwatched'


def _write(auth, meta, in_library):
    """Add or remove `meta` from the account's library, with the toast."""
    from lib import library
    from lib.ui.compat import L, notify
    from lib.ui.uicommon import CANCELLED

    # Pull again at the moment of the write. The menu lookup can be
    # seconds old, and a fresh `library_add_payload` would replace
    # another client's `state` with a blank one.
    fresh, fresh_ok = _lookup(auth, meta.get('id'))
    if fresh is CANCELLED:
        return None
    if not fresh_ok:
        notify(L(30302))
        return None
    if in_library:
        if not isinstance(fresh, dict):
            notify(L(30302))
            return None
        payload = library.library_remove_payload(fresh)
    else:
        payload = library.library_add_from_existing(
            meta, fresh if isinstance(fresh, dict) else None,
        )
    if not _push(auth, payload):
        return None
    from lib.ui.mystuff import note_library_write
    from lib.ui.newhome import mark_library_dirty

    note_library_write(payload)
    mark_library_dirty()
    notify(L(30298) if in_library else L(30297))
    return 'removed' if in_library else 'added'
