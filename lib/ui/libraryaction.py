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

Every account library write in the addon goes through `push_write` here,
which also keeps the pending-write ledger (`note_library_write` /
`apply_library_writes`) Home and the watched check paint from. Nothing at
module level imports Kodi, so `lib.ui.watchmarks` (and the test suite's
conftest) can import this module bare.
"""
import threading
from typing import Dict


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


def push_write(auth, payload):
    """Put `payload` on the account and, once it landed, tell Home. True
    when it landed.

    The one library write path: the poster menu here and the series page
    (`detailwindow._toggle_library`/`_toggle_watched`) both come through
    it. A landed write is remembered (`note_library_write`) because the
    account's next read can still predate it, and Home is marked to
    re-read its library (`home.mark_library_dirty`). A caller that put
    the record but skipped those two left Home showing the old library
    membership and check (the PR #87 review), which is why they are not
    the caller's job.

    Back on the spinner notifies nothing: the request cannot be recalled
    and may still land, but the viewer is neither blocked nor told it
    did. An `ApiError` notifies `#30301`. Either way nothing is
    remembered and Home stays clean. The success toast is the caller's,
    as only it knows which action landed.
    """
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
    from lib.ui.home import mark_library_dirty

    note_library_write(payload)
    mark_library_dirty()
    return True


# Writes that have been pushed and must show before the account's next
# read echoes them. Keyed by `_id`. A later read drops a write once the
# account row is at least as new and agrees on `removed`. Kept here, next
# to the one path that records them, rather than in a Kodi-importing
# module: `lib.ui.watchmarks` overlays them on the cached library and must
# stay importable without Kodi.
_library_writes: Dict[str, dict] = {}
_library_write_lock = threading.Lock()


def dismiss_live(auth, channel_id):
    """Take a live channel off the account's Recent channels row. True when
    nothing is left to dismiss or the write landed.

    stremio-core's `RewindLibraryItem` on a live item
    (`library.rewind_live`): `lastWatched` goes null and the offset 0, on
    the account's CURRENT record (read fresh, as `_write` does, so another
    client's state is not replaced), through `push_write`. The record stays
    (it may be in the library). An account with no record has nothing to
    dismiss; a failed lookup tells the viewer (#30302) and returns False.
    """
    from lib import library
    from lib.ui.compat import L, notify
    from lib.ui.uicommon import CANCELLED

    fresh, fresh_ok = _lookup(auth, channel_id)
    if fresh is CANCELLED:
        return False
    if not fresh_ok:
        notify(L(30302))
        return False
    if not isinstance(fresh, dict):
        return True
    return push_write(auth, library.rewind_live(fresh))


def clear_library_writes():
    """Test hook. Production never calls this."""
    with _library_write_lock:
        _library_writes.clear()


def note_library_write(item):
    """Remember a libraryItem this process just put.

    Painting from this copy is what makes Add and Remove show up on Home's
    Library row, and a mark on the watched check, without waiting for the
    account to echo the change.
    """
    if not isinstance(item, dict):
        return
    item_id = item.get('_id')
    if not item_id:
        return
    with _library_write_lock:
        _library_writes[item_id] = item


def apply_library_writes(entries):
    """`entries` (a libraryItem read: live, or `Store.get_library_cache`)
    with this process's pending writes laid over it. An account row at
    least as new as the write, and agreeing on `removed`, wins and retires
    the write."""
    with _library_write_lock:
        pending = dict(_library_writes)
    if not pending:
        return list(entries or [])
    out = []
    seen = set()
    for entry in entries or []:
        if not isinstance(entry, dict):
            out.append(entry)
            continue
        item_id = entry.get('_id')
        overlay = pending.pop(item_id, None) if item_id else None
        if overlay is None:
            out.append(entry)
            if item_id:
                seen.add(item_id)
            continue
        remote_m = entry.get('_mtime') or ''
        local_m = overlay.get('_mtime') or ''
        if remote_m >= local_m and bool(entry.get('removed')) == bool(overlay.get('removed')):
            with _library_write_lock:
                current = _library_writes.get(item_id)
                if (
                    isinstance(current, dict)
                    and (current.get('_mtime') or '') <= remote_m
                    and bool(current.get('removed')) == bool(entry.get('removed'))
                ):
                    _library_writes.pop(item_id, None)
            out.append(entry)
        else:
            out.append(overlay)
        if item_id:
            seen.add(item_id)
    for item_id, item in pending.items():
        if item_id not in seen:
            out.append(item)
    return out


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
    the trailer. The library row is Add when the title is not in the
    library and Remove when it is; its toast is the same copy the title
    page uses (`#30297` / `#30298`). Returns `'added'`, `'removed'`,
    `'watched'`, `'unwatched'` or None (a trailer, an extra row, or
    nothing picked).
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
    if not push_write(auth, payload):
        return None
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
    if not push_write(auth, payload):
        return None
    notify(L(30298) if in_library else L(30297))
    return 'removed' if in_library else 'added'
