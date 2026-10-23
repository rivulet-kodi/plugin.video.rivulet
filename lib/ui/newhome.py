# SPDX-FileCopyrightText: 2026 M0Rf30
# SPDX-License-Identifier: MIT

"""NewHomeWindow: Rivulet's Home, drawn by NewHomeWindow.xml.

Opened by `homewindow.open_home()`.

A full-bleed backdrop for the focused title, drawn whole rather than
cropped, fading to black toward the left and the bottom. The clearlogo
(or the name), one line of facts, three lines of plot and the scores sit
on the left (`newlook.paint_hero`). One row is visible, pinned to the
bottom of the screen. Down moves to the next row and Up moves back; the
hero follows the focused poster. The tabs (Home, My Stuff, Movies, Series, Anime) sit in a pill row under the clock, and Search, Add-ons and
Settings are icon buttons in a right-aligned grouplist so they stay on
the right edge at any resolution. The active tab resolves a saved layout into
one pinned landscape Continue slot and up to twelve poster slots. OK switches
on the chip bar; Left/Right only move focus. Back first resets the row, then
moves to the active chip, then Home, then closes. Native Up/Down never rebuild
rows. Snapshots retain each tab's loaded pages and positions for this window.

Picking a poster opens its page, except a Continue poster mid-playback, which
asks Resume or Episode list. Poster menus offer Library/Trailer, Remove from
Continue, and Browse full catalog for catalog rows. Pages append in place,
with two workers and a 2000-item safety ceiling per row.

Shared helpers this module calls and does not modify:
`lib.ui.mystuff` (`merge_entries`, `library_metas`, `_read_library_entries`,
`_enrich`, the resume percent constants), `lib.ui.views.iter_catalog_pages`
and `lib.ui.detailwindow.open_detail`.
"""
import json
import queue
import threading
import time
import weakref
from functools import partial

import xbmcgui

from lib.ui.dependencies import get_store
from lib.ui.uicommon import BaseWindow, gated_input

BACKGROUND = 30000
STATUS_LABEL = 30005
BROWSE_LIST = 30010  # permanent Home/My Stuff plus renderable type tabs
TOOLS_LIST = 30011  # right-aligned grouplist: Sign in, Search, Add-ons, Settings
HERO_TITLE = 30020
HERO_META = 30021  # textbox in the right quarter; setText(), not setLabel()
HERO_PLOT = 30022  # textbox; setText(), not setLabel()
HERO_LOGO = 30023
#: Full-bleed poster when a library title has no backdrop. The keep
#: image (``BACKGROUND``) letterboxes a poster; this one scales to cover.
HERO_FILL = 30028
CONTINUE_SHELF = 30040
LIBRARY_SHELF = 30041
BTN_SEARCH = 30070
BTN_ADDONS = 30071
BTN_SETTINGS = 30072
BTN_SIGNIN = 30073

#: How many Continue posters a row keeps. The Library row is the whole
#: account library: cutting it here stopped the focus ring at item 12
#: while the list itself went on. Catalog rows are separate: they show
#: `_ROW_INITIAL` from the first page and append the rest as the remote
#: approaches the end (`_maybe_extend_focused_shelf`).
SHELF_CAP = 12

#: First paint/append batch. Rows keep loading to the end, with a safety
#: ceiling for broken addons that keep producing distinct ids forever.
_ROW_INITIAL = 24
_ROW_CEILING = 2000
PREFETCH_THRESHOLD = 5
_ROW_RETRY_SECONDS = 30.0
_ROW_MAX_FAILURES = 3

#: Set when a hub poster changes the account library. Home has no handle
#: on that window, and its row cache would otherwise keep the old
#: Library row until the auth key changed.
_library_dirty = False

#: One landscape Continue slot is pinned first, followed by twelve poster
#: slots. New header ids use the 30160s to avoid the old counter ids.
#: The same slots are reused by each tab; its state lives in snapshots.
_SHELVES = tuple(
    ('slot%d' % index, 30041 + index, 30141 + index if index < 5 else 30160 + index - 5)
    for index in range(12)
)
_CLICKABLE = frozenset((BROWSE_LIST, CONTINUE_SHELF) + tuple(p for _, p, _ in _SHELVES))
_SHELF_IDS = (CONTINUE_SHELF,) + tuple(p for _, p, _ in _SHELVES)

_BUTTON_ACTIONS = {
    BTN_SEARCH: 'search',
    BTN_ADDONS: 'addons',
    BTN_SETTINGS: 'settings',
    BTN_SIGNIN: 'signin',
}

# Tabs have a fixed order. Eligibility is the same pure catalog rule used by
# the configurator; unsupported base types never create a Home row or chip.
_MENU = ((30495, 'home'), (30516, 'mystuff'), (30213, 'movies'),
         (30214, 'series'), (30215, 'anime'))
_SUBTITLES = {'mystuff': 30232, 'movies': 30216, 'series': 30217, 'anime': 30218}
L_FOR_ACTION = {action: string_id for string_id, action in _MENU}


def _menu_items(addons=None, layout=None, active='home', progress=None):
    from lib.homelayout import default_layout, installed_catalogs, matches_continue, resolve
    from lib.ui.compat import L, setting_bool

    catalogs = installed_catalogs(addons or [], setting_bool('home_hide_adult', True))
    layout = layout or default_layout()
    items = []
    for string_id, tab in _MENU:
        if tab == 'anime' and not setting_bool('home_show_anime', True):
            continue
        plan = resolve(layout, tab, catalogs)
        renderable = any(kind != 'continue' or any(matches_continue(meta, variant)
                         for meta in progress or []) for _, kind, _, variant, _ in plan)
        if tab not in ('home', 'mystuff') and not renderable:
            continue
        item = xbmcgui.ListItem(L(string_id))
        plot = L(_SUBTITLES[tab]) if tab in _SUBTITLES else ''
        item.setProperties({'action': tab, 'subtitle': plot, 'hero_title': L(string_id),
                            'hero_plot': plot, 'active': '1' if tab == active else ''})
        items.append(item)
    return items


def _truncate(text, limit):
    text = (text or '').strip()
    if len(text) <= limit:
        return text
    return text[:limit - 1].rstrip() + '\u2026'


def _runtime_text(meta):
    """A count-neutral runtime. Stremio sometimes already sends a string
    ("155 min"); a number is formatted with `#30429` so the unit is
    translated without picking a plural form."""
    from lib.ui.compat import L

    runtime = meta.get('runtime')
    if isinstance(runtime, str):
        return runtime.strip()
    if isinstance(runtime, bool) or not isinstance(runtime, (int, float)):
        return ''
    if runtime <= 0:
        return ''
    return L(30429) % int(runtime)


def _hero_meta_text(meta):
    """Ratings, year, runtime and genres, one per line.

    The skin draws this in the right quarter. Whatever the meta does
    not carry is left out, so a sparse title never shows a bare label.
    """
    from lib.ui.compat import L
    from lib.ui.playbackmeta import hero_panel_text

    return hero_panel_text(meta, runtime_text=_runtime_text(meta), now_word=L(30234))


def _runtime_minutes(meta):
    """The meta's runtime in whole minutes as a string, or ``''``."""
    from lib.ui.playbackmeta import parse_duration_seconds

    seconds = parse_duration_seconds(meta.get('runtime')) if isinstance(meta, dict) else None
    return str(int(seconds // 60)) if seconds else ''


def _poster_item(meta, action='', sparse=False):
    """One shelf cell. `action` set means the click opens that menu
    action (Sign in) instead of the title's page.

    `sparse` is a library record: a name and a poster, and sometimes a
    backdrop, with no description. The hero for that cell is filled in
    on focus (`_schedule_library_meta`). Until that answer arrives, a
    poster with no backdrop is drawn full-bleed (`hero_fill`) instead
    of letterboxed on the black plate.
    """
    from lib.stremio.ratings import panel_facts
    from lib.ui.uicommon import escape_label
    from lib.ui.urlutil import sized_image

    name = meta.get('name') or ''
    background = meta.get('background') or ''
    poster = meta.get('poster') or ''
    from lib.ui import previews

    previews.remember(meta)
    item = xbmcgui.ListItem(escape_label(name))
    item.setProperties({
        # TMDB `original` art is megabytes for a 200px card (#18).
        'thumbnail': sized_image(poster, 'poster'),
        'fanart': sized_image(background, 'backdrop') if background else sized_image(poster, 'fill'),
        'hero_logo': meta.get('logo') or '',
        'hero_title': escape_label(name),
        'hero_meta': escape_label(_hero_meta_text(meta)),
        'hero_plot': escape_label(_truncate(meta.get('description') or '', 280)),
        'hero_facts': json.dumps(panel_facts(meta), ensure_ascii=False, separators=(',', ':')),
        # Minutes, for the hero's "Ends at" (lib/ui/newlook.py).
        'hero_runtime': _runtime_minutes(meta),
        # No backdrop: the poster fills the hero (scaled to cover) instead
        # of standing as a narrow pillarbox in the middle (#21). Library
        # records used to be the only ones; any catalog title can lack one.
        'hero_fill': '1' if not background and poster else '',
        'needs_meta': '1' if sparse and meta.get('id') and meta.get('type') and not action else '',
        'action': action,
        'stype': str(meta.get('type') or ''),
        'sid': str(meta.get('id') or ''),
    })
    return item


class CatalogUnavailable(Exception):
    """A catalog's first page failed (timeout, HTTP error, addon error).

    Not the same as an empty catalog: a hub row keeps a Try again card
    for it instead of disappearing (#16). Already logged when raised.
    """


def _one_catalog_page(transport, manifest, catalog, extra=None):
    """The first page of one catalog, uncut.

    Callers keep `_ROW_INITIAL` for the first paint and hold the rest
    back. The generator is not walked here: the following pages are
    fetched only when the remote nears the end of what is on screen
    (`_pull_more`), so opening Home does not pull a whole catalog.

    A failure is logged with the addon's own detail and raised as
    `CatalogUnavailable`; it used to be swallowed into an empty list,
    with no log line, so a broken addon's row just vanished (#16).
    """
    import xbmc

    from lib.stremio.addons import AddonError, addon_error_detail, safe_url_for_log
    from lib.ui.catalogpages import cached_first_page
    from lib.ui.compat import log

    name = (catalog or {}).get('id') if isinstance(catalog, dict) else ''
    try:
        return cached_first_page(transport, manifest, catalog, extra)
    except AddonError as exc:
        log('home: catalog %s failed: %s (%s)' % (
            name, addon_error_detail(exc), safe_url_for_log(transport or ''),
        ), xbmc.LOGWARNING)
        raise CatalogUnavailable(name)
    except Exception as exc:
        log('home: catalog %s failed: %s' % (name, addon_error_detail(exc)), xbmc.LOGWARNING)
        raise CatalogUnavailable(name)


class _RememberedPageFailure(Exception):
    """Bypass the upstream AddonError warning for a cached failure."""

    def __init__(self, failure):
        self.failed = failure.failed


def _shelf_page_gen(state):
    """Resume after raw served metas, sharing Home's later-page LRU.

    A new iterator per step makes paging state snapshotable. No generator
    runs against a snapshot that another tab can restore underneath it.
    """
    from lib.ui.catalogpages import LaterPageFailure, cached_later_page
    from lib.ui.views import iter_catalog_pages_after

    if not state.get('catalog'):
        return iter(())

    def fetch(transport, ctype, cid, extra=None):
        try:
            page = cached_later_page(transport, ctype, cid, extra)
        except LaterPageFailure as exc:
            if exc.remembered:
                raise _RememberedPageFailure(exc) from exc
            raise
        state['failed'] = None
        state['served'] += len(page)
        state['seen_ids'].update(meta.get('id') for meta in page if isinstance(meta, dict))
        state['page_number'] = state.get('page_number', 1) + 1
        return page

    catalog = state['catalog']
    return iter_catalog_pages_after(
        state['transport'], catalog.get('type'), catalog.get('id'),
        state['served'], state['seen_ids'], extra=state.get('extra'),
        catalog=catalog, manifest=state['manifest'], page_limit=None,
        fetch_page=fetch, stop_short=False, raise_on_error=True,
        on_end=lambda reason: state.update(end_reason=reason),
    )


def _row_waiting(state):
    import time

    failed = state.get('failed')
    return bool(failed and time.monotonic() - failed['at'] < _ROW_RETRY_SECONDS)


def _log_row_end(state):
    """One terminal diagnostic per row, with only the addon's safe origin."""
    if not state.get('exhausted') or state.get('end_logged'):
        return
    import xbmc

    from lib.homelayout import parse_catalog_id
    from lib.stremio.addons import safe_url_for_log
    from lib.ui.compat import log

    list_id = state.get('list_id') or '?'
    parts = parse_catalog_id(list_id)
    if parts:
        list_id = 'cat:%s|%s' % (safe_url_for_log(parts[0]), '|'.join(parts[1:]))
    log('home: row %s ended (%s) at %d items' % (
        list_id, state.get('end_reason') or 'catalog end', len(state.get('metas') or [])), xbmc.LOGINFO)
    state['end_logged'] = True


def _safe_list_id(list_id):
    from lib.homelayout import parse_catalog_id
    from lib.stremio.addons import safe_url_for_log

    parts = parse_catalog_id(list_id)
    return 'cat:%s|%s' % (safe_url_for_log(parts[0]), '|'.join(parts[1:])) if parts else list_id


def _log_skipped_page(state):
    import xbmc

    from lib.ui.compat import log

    failed = state.get('failed')
    if failed:
        log('home: row %s page %d skipped (failed %.1f s ago)' % (
            _safe_list_id(state.get('list_id') or '?'), state.get('page_number', 1) + 1,
            max(0.0, time.monotonic() - failed['at'])), xbmc.LOGINFO)


def _remembered_row_failure(state):
    from lib.ui.catalogpages import later_page_failure

    catalog = state['catalog']
    failed = later_page_failure(state['transport'], catalog.get('type'), catalog.get('id'),
                               list(state.get('extra') or []) + [('skip', state['served'])],
                               _ROW_RETRY_SECONDS)
    if failed:
        state['failed'] = {key: failed[key] for key in ('at', 'count')}
        if failed['count'] >= _ROW_MAX_FAILURES:
            state.update(exhausted=True, end_reason='3 consecutive page failures')
        _log_skipped_page(state)


def _stopping(stop=None):
    from lib.ui.background import monitor

    return (stop is not None and stop.is_set()) or monitor().abortRequested()


def _home_stop(window):
    stop = getattr(window, '_home_stop', None)
    if stop is None:
        stop = threading.Event()
        window._home_stop = stop
    return stop


def _pull_more(state, room, max_pages=5):
    """Fill at most 24 matching posters, bounded to five raw pages.

    Empty filtered anime pages are not catalog exhaustion. Raw ids and
    offsets advance even when that view shows none of the fetched page.
    """
    from lib.homelayout import matches_catalog
    from lib.stremio.addons import addon_error_detail
    from lib.ui.compat import log

    if not state or _stopping(state.get('_stop')) or room <= 0 or state.get('exhausted') or _row_waiting(state):
        return []
    result = []
    pending = state.get('pending') or []
    result.extend(pending[:room])
    state['pending'] = pending[room:]
    if len(result) >= room:
        return result
    gen = _shelf_page_gen(state)
    for _ in range(max_pages):
        if _stopping(state.get('_stop')):
            break
        if len(result) >= room:
            break
        try:
            page = next(gen)
        except StopIteration:
            state['exhausted'] = True
            state.setdefault('end_reason', 'catalog end')
            state['failed'] = None
            break
        except Exception as exc:
            if not isinstance(exc, _RememberedPageFailure):
                log('home: next catalog page failed: %s' % addon_error_detail(exc))
            import time

            from lib.ui.catalogpages import LaterPageFailure

            count = (state.get('failed') or {}).get('count', 0) + 1
            state['failed'] = {'at': time.monotonic(), 'count': count}
            if isinstance(exc, (LaterPageFailure, _RememberedPageFailure)):
                state['failed'] = dict(exc.failed)
                count = exc.failed['count']
                _log_skipped_page(state)
            if count >= _ROW_MAX_FAILURES:
                state['exhausted'] = True
                state['end_reason'] = '3 consecutive page failures'
            break
        batch = [meta for meta in page if isinstance(meta, dict)
                 and matches_catalog(meta, state.get('filter', ''))]
        needed = room - len(result)
        result.extend(batch[:needed])
        state['pending'] = batch[needed:]
    return result


def _open_shelf(descriptor):
    """Page one only: a short cached page must never wait for page two."""
    from lib.homelayout import matches_catalog
    from lib.ui.views import _dedupe_page

    state = dict(descriptor)
    state.update({'pending': [], 'served': 0, 'seen_ids': set(), 'exhausted': False, 'page_number': 1})
    if _stopping(state.get('_stop')):
        state['metas'] = []
        return state
    try:
        first = _one_catalog_page(state['transport'], state['manifest'], state['catalog'])
    except CatalogUnavailable:
        state['exhausted'] = True
        state['end_reason'] = 'first page failed'
        first = []
    state['served'] = len(first)
    _fresh, first = _dedupe_page(first, state['seen_ids'], False)
    state['pending'] = [meta for meta in first if isinstance(meta, dict)
                        and matches_catalog(meta, state.get('filter', ''))]
    if not first:
        state['exhausted'] = True
        state['end_reason'] = 'first page failed' if state.get('end_reason') == 'first page failed' else 'empty page'
    state['metas'] = state['pending'][:_ROW_INITIAL]
    state['pending'] = state['pending'][_ROW_INITIAL:]
    _remembered_row_failure(state)
    _log_row_end(state)
    return state


def _set_shelf(window, list_id, label_id, prop, heading, items):
    control = window.getControl(list_id)
    control.reset()
    if items:
        control.addItems(items)
    # Bold and brighter than before (#11): with one row on screen the
    # heading is the only wayfinding, and font12 at 60% white read faint.
    window.getControl(label_id).setLabel('[B]%s[/B]' % heading if items and heading else '')
    window.setProperty(prop, '1' if items else '')


#: Row panel -> its "row / rows" label at the right end of the heading
#: (NewHomeWindow.xml). Only one row is on screen at a time, so the count is
#: the only cue to how far down the list you are (#20).
_ROW_COUNTERS = ((CONTINUE_SHELF, 'shelf_continue', 30150),) + tuple(
    (panel, 'shelf_%s' % slot, 30151 + index if index < 5 else 30170 + index - 5)
    for index, (slot, panel, _header) in enumerate(_SHELVES)
)


def _number_rows(window):
    """Write "row / rows" on every visible row, in screen order.

    Run after any shelf is filled or emptied: Continue appearing after a
    first playback shifts every count below it. Count-neutral digits, no
    word to translate. A window without the labels skips.
    """
    visible = [(prop, counter) for _list_id, prop, counter in _ROW_COUNTERS if window.getProperty(prop)]
    total = len(visible)
    shown = {counter: index + 1 for index, (_prop, counter) in enumerate(visible)}
    for _list_id, _prop, counter in _ROW_COUNTERS:
        try:
            label = window.getControl(counter)
        except Exception:
            continue
        index = shown.get(counter)
        label.setLabel('%d / %d' % (index, total) if index and total > 1 else '')


def _state_percent(entry):
    """Percent of a libraryItem's own `state`, or None when the record
    has no usable duration. Same fields `lib.library.merge_playback()`
    writes (`timeOffset`, `duration`); this only reads them."""
    if not isinstance(entry, dict):
        return None
    state = entry.get('state')
    if not isinstance(state, dict):
        return None
    duration = state.get('duration') or 0
    offset = state.get('timeOffset') or 0
    if isinstance(duration, bool) or not isinstance(duration, (int, float)):
        return None
    if isinstance(offset, bool) or not isinstance(offset, (int, float)):
        return None
    if duration <= 0:
        return None
    return (float(offset) / float(duration)) * 100.0


def _library_continue_metas(entries):
    """In-progress titles recorded on the account.

    Local `progress.json` only knows what this device played. A title
    watched in the Stremio app is a libraryItem whose `state` still sits
    in the resume window. An explicit removal is `removed` without
    `temp` (`library_remove_payload`); those stay off Continue. An
    implicit watch record is both flags true, which is how Stremio
    stores "played, not added", and those do belong here.
    """
    from lib.ui.mystuff import BAND_RESUME, RESUME_MAX_PERCENT, RESUME_MIN_PERCENT

    metas = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        if entry.get('removed') and not entry.get('temp'):
            continue
        percent = _state_percent(entry)
        if percent is None or percent < RESUME_MIN_PERCENT or percent >= RESUME_MAX_PERCENT:
            continue
        if not entry.get('_id'):
            continue
        if not (entry.get('name') or entry.get('poster')):
            continue
        state = entry.get('state') if isinstance(entry.get('state'), dict) else {}
        metas.append({
            'id': entry.get('_id'),
            'name': entry.get('name'),
            'type': entry.get('type'),
            'poster': entry.get('poster'),
            'background': entry.get('background'),
            'band': BAND_RESUME,
            'video_id': state.get('video_id') or None,
            'position_ms': state.get('timeOffset'),
            'duration_ms': state.get('duration'),
        })
    return metas


def _merge_played(progress_entries, library_entries):
    """`mystuff.merge_entries()` plus each played row's position and
    duration, which a Continue poster's Resume needs
    (`_continue_offset_ms`). The rows are the merge's own; the two
    fields are copied from the same latest-per-title progress entry the
    merge banded."""
    from lib.ui.mystuff import latest_by_title, merge_entries

    merged = merge_entries(progress_entries, library_entries)
    latest = {
        (entry.get('type'), entry.get('id')): entry
        for entry in latest_by_title(progress_entries)
    }
    for entry in merged:
        source = latest.get((entry.get('type'), entry.get('id')))
        if source is None:
            continue
        for key in ('position_ms', 'duration_ms'):
            if entry.get(key) is None:
                entry[key] = source.get(key)
    return merged


def _newest_first(entries):
    """Library records newest `_ctime` (the added time) first. A row with
    no `_ctime` falls back to `_mtime`, then stays in the order it
    arrived. The Library row reads in this order."""
    kept = [entry for entry in entries or [] if isinstance(entry, dict)]
    return sorted(kept, key=lambda entry: entry.get('_ctime') or entry.get('_mtime') or '', reverse=True)


_CONTINUE_ENRICH_SECONDS = 2.0
_continue_meta_misses = set()
_continue_miss_lock = threading.Lock()


def _enrich_for_home(items, deadline=None, stop=None):
    from lib.ui.mystuff import _enrich
    from lib.ui.views import _fetch_meta_for_home

    deadline = deadline if deadline is not None else time.monotonic() + _CONTINUE_ENRICH_SECONDS

    def stopped():
        return time.monotonic() >= deadline or _stopping(stop)

    def fetch_meta(stype, sid, **kw):
        def not_found():
            with _continue_miss_lock:
                _continue_meta_misses.add((stype, sid))
        return _fetch_meta_for_home(stype, sid, stopped, stop_event=stop,
                                    on_not_found=not_found, **kw)

    def map_items(fetch, wanted):
        return _map_continue_meta(fetch, wanted, deadline, stop)

    return _enrich(items, fetch_meta=fetch_meta, map_items=map_items)


def _map_continue_meta(fetch, wanted, deadline, stop):
    """Only completed results enrich this load; deadlines retry next load."""
    def stopped():
        return time.monotonic() >= deadline or _stopping(stop)

    with _continue_miss_lock:
        jobs = [(index, partial(fetch, item), None) for index, item in enumerate(wanted)
                if (item.get('type'), item.get('id')) not in _continue_meta_misses]
    timing = {'jobs': {}, 'lock': threading.Lock()}
    fetched = _load_home_jobs(jobs, stopped, timing, deadline, max_workers=8)
    return [fetched.get(index) for index in range(len(wanted))]


def _home_personal_jobs(window, store, auth, plan, stopped, cancelled, timing):
    """Account read and bounded Continue work share the row pool in parallel."""
    cached = getattr(window, '_library_entries', None)
    entries = {'value': cached}
    ready = threading.Event()
    jobs = []
    needs_library = any(kind in ('continue', 'library') for _, kind, _, _, _ in plan)
    if needs_library and (cached is None or getattr(window, '_library_failed', False)):
        def library():
            try:
                entries['value'] = _fetch_library(store, auth)
                return entries['value']
            finally:
                ready.set()
        jobs.append(('library', library, None))
    else:
        ready.set()
    continued = [(list_id, variant) for list_id, kind, _, variant, _ in plan if kind == 'continue']
    if continued:
        def continue_rows():
            while not ready.is_set() and not stopped():
                ready.wait(1.0)
            if stopped():
                return {}
            started = time.monotonic()
            deadline = started + _CONTINUE_ENRICH_SECONDS
            result = {list_id: _played_posters(store, entries['value'], variant,
                       getattr(window, '_continue_suppressed', None), deadline, cancelled)
                      for list_id, variant in continued}
            timing['continue'] = time.monotonic() - started
            return result
        jobs.append(('continue', continue_rows, {}))
    return jobs, needs_library, cached


def _played_posters(store, library_entries, variant='', suppressed=None, deadline=None, stop=None):
    """Resume, next-up and recently finished. Never the library band.

    `merge_entries()` is what copies a saved title's name and poster onto
    a local progress row. `_enrich()` runs only for rows that still have
    neither, so a home paint does not fan out a meta fetch for every
    series just to learn the next episode code.
    """
    from lib.ui.mystuff import BAND_LIBRARY

    try:
        merged = _merge_played(store.get_progress_entries(), library_entries)
    except Exception:
        merged = []
    from lib.homelayout import matches_continue

    suppressed = suppressed or set()
    played = [entry for entry in merged if entry.get('band') != BAND_LIBRARY
              and entry.get('id') not in suppressed and matches_continue(entry, variant)]
    missing = [entry for entry in played if not (entry.get('name') or entry.get('poster'))]
    if missing:
        try:
            _enrich_for_home(missing, deadline, stop)
        except Exception:
            pass
    posters = []
    seen = set()
    for entry in played:
        if not (entry.get('name') or entry.get('poster')):
            continue
        key = (entry.get('type'), entry.get('id'))
        seen.add(key)
        posters.append(_continue_item(entry))
        if len(posters) >= SHELF_CAP:
            return posters
    for meta in _library_continue_metas(library_entries):
        if meta.get('id') in suppressed or not matches_continue(meta, variant):
            continue
        key = (meta.get('type'), meta.get('id'))
        if key in seen:
            continue
        seen.add(key)
        posters.append(_continue_item(meta))
        if len(posters) >= SHELF_CAP:
            break
    return posters


def _fetch_library(store, auth):
    """The account library, [] when signed out, or None on a failed read."""
    if not auth:
        return []
    from lib.ui.mystuff import _read_library_entries, apply_library_writes

    try:
        # A write this session already pushed (`libraryaction`) shows
        # before the account echoes it.
        raw = _read_library_entries(store)
        fetched = apply_library_writes(raw) if isinstance(raw, list) else None
    except Exception as exc:
        from lib.stremio.addons import addon_error_detail
        from lib.ui.compat import log

        log('home: library read failed: %s' % addon_error_detail(exc))
        return None
    if not isinstance(fetched, list):
        return None
    return fetched


# Each first-load fan-out has its own four-worker bound; later paging has
# a separate two-worker bound because it runs while the remote is moving.
_MAX_HOME_WORKERS = 4


def _make_row_states(store, auth, entries, plan, fetched, suppressed=None, continued=None):
    from lib.homelayout import heading_text, matches_library
    from lib.ui.compat import L
    from lib.ui.mystuff import library_metas

    rows = []
    signed_out_card = False
    for list_id, kind, descriptor, variant, heading in plan:
        state = {'list_id': list_id, 'kind': kind, 'filter': variant,
                 'heading': heading_text(heading, L).upper(), 'descriptor': descriptor,
                 'pending': [], 'exhausted': True, 'metas': [], 'items': []}
        if kind == 'catalog':
            state.update(fetched.get(list_id, {}))
            state.pop('_stop', None)
            state['kind'] = 'catalog'
            state['items'] = [_poster_item(meta) for meta in state['metas']]
        elif kind == 'continue':
            state['items'] = continued.get(list_id, []) if continued is not None else _played_posters(store, entries, variant, suppressed)
            if entries is None and auth and not state['items']:
                state['items'] = [_poster_item({'name': L(30032)})]
        elif entries is None and auth:
            state['items'] = [_poster_item({'name': L(30032)})]
        elif not auth:
            if not signed_out_card:
                state['items'] = [_poster_item({'name': L(30427), 'description': L(30428)}, action='signin')]
                signed_out_card = True
        else:
            metas = [meta for meta in library_metas(_newest_first(entries))
                     if matches_library(meta, variant)]
            state['metas'] = metas[:_ROW_INITIAL]
            state['pending'] = metas[_ROW_INITIAL:]
            state['exhausted'] = not state['pending']
            state['items'] = [_poster_item(meta, sparse=True) for meta in state['metas']]
            if not state['items']:
                state['items'] = [_poster_item({'name': L(30426), 'description': L(30423)})]
        rows.append(state)
    return rows


def _collect_home_rows(results, count, threads, stopped, deadline=None):
    """Cancellation and a dead worker both end the first-paint collector."""
    fetched = {}
    while len(fetched) < count and not stopped():
        if results.empty() and not any(thread.is_alive() for thread in threads):
            break
        timeout = min(0.75, deadline - time.monotonic()) if deadline is not None else 0.75
        if timeout <= 0:
            break
        try:
            key, value = results.get(timeout=timeout)
            fetched[key] = value
        except queue.Empty:
            # A worker dying outside Exception handling must not strand us.
            if not any(thread.is_alive() for thread in threads):
                break
    return fetched


def _home_load_worker(work, results, stopped, timing):
    while not stopped():
        try:
            key, fetch, fallback = work.get_nowait()
        except queue.Empty:
            return
        started = time.monotonic()
        try:
            value = fetch()
        except Exception as exc:
            from lib.stremio.addons import addon_error_detail
            from lib.ui.compat import log

            log('home: row load failed: %s' % addon_error_detail(exc))
            value = fallback
        with timing['lock']:
            timing['jobs'][key] = time.monotonic() - started
        if not stopped():
            results.put((key, value))


def _load_home_jobs(jobs, stopped, timing, deadline=None, max_workers=_MAX_HOME_WORKERS):
    from lib.ui.catalogpages import deferred_saves

    work, results = queue.Queue(), queue.Queue()
    for job in jobs:
        work.put(job)
    with deferred_saves():
        threads = []
        for _ in range(min(len(jobs), max_workers)):
            thread = threading.Thread(target=_home_load_worker, args=(work, results, stopped, timing),
                                      name='rivulet-home-load', daemon=True)
            threads.append(thread)
            thread.start()
        return _collect_home_rows(results, len(jobs), threads, stopped, deadline)


def _log_tab_paint(window, count):
    import xbmc

    from lib.ui.compat import log

    timing = getattr(window, '_tab_load_timing', None)
    if timing is None:
        return
    with timing['lock']:
        jobs = dict(timing['jobs'])
    catalogs = [(key, value) for key, value in jobs.items() if key.startswith('cat:')]
    slowest, seconds = max(catalogs, key=lambda entry: entry[1]) if catalogs else ('-', 0.0)
    log('home: tab %s painted %d rows in %.3f s (library %.3f s, continue enrich %.3f s, '
        'slowest row %s %.3f s, cancelled=%s)' % (
            timing['tab'], count, time.monotonic() - timing['start'], jobs.get('library', 0.0),
            timing.get('continue', 0.0), _safe_list_id(slowest), seconds, timing['cancelled']), xbmc.LOGINFO)


def _set_tab_loaded(window, loaded):
    flags = getattr(window, '_tab_loaded', {})
    window._tab_loaded = flags
    flags[window._active_tab] = loaded
    if not loaded:
        getattr(window, '_tab_snapshots', {}).pop(window._active_tab, None)


def _tab_load_incomplete(window, reason):
    import xbmc

    from lib.ui.compat import log

    _set_tab_loaded(window, False)
    log('home: tab %s load %s' % (window._active_tab, reason), xbmc.LOGINFO)


def _load_rows(window, store, addons, auth):
    """Fetch only this tab's plan. One account read feeds every personal row."""
    from lib.homelayout import resolve
    from lib.ui.compat import L, setting_bool
    from lib.ui.uicommon import CANCELLED, run_cancellable

    tab = window._active_tab
    _set_tab_loaded(window, False)
    if tab == 'anime' and not setting_bool('home_show_anime', True):
        return []
    plan = resolve(window._layout, tab, window._catalogs)
    session_stop = _home_stop(window)
    cancelled = threading.Event()
    load_stops = getattr(window, '_home_load_stops', [])
    window._home_load_stops = load_stops
    load_stops.append(cancelled)

    def stopped():
        return cancelled.is_set() or _stopping(session_stop)

    timing = {'tab': tab, 'start': time.monotonic(), 'jobs': {}, 'lock': threading.Lock(),
              'cancelled': False, 'continue': 0.0}
    window._tab_load_timing = timing

    def load():
        jobs, needs_library, cached = _home_personal_jobs(
            window, store, auth, plan, stopped, cancelled, timing)
        for list_id, kind, descriptor, _variant, _heading in plan:
            if kind == 'catalog':
                jobs.append((list_id, partial(_open_shelf, dict(descriptor, _stop=cancelled)),
                             {'metas': [], 'exhausted': True}))
        # Library must be able to complete without a Continue waiter
        # occupying one of the first four row slots.
        jobs.sort(key=lambda job: job[0] == 'continue')
        fetched = _load_home_jobs(jobs, stopped, timing)
        if stopped():
            return None
        if len(fetched) != len(jobs):
            return None
        entries = fetched.pop('library', cached)
        continued = fetched.pop('continue', {})
        rows = _make_row_states(store, auth, cached if entries is None and cached is not None else entries, plan, fetched,
                                getattr(window, '_continue_suppressed', None), continued)
        return entries if needs_library or cached is not None else None, rows

    try:
        loaded = run_cancellable(load, L(30033))
    finally:
        cancelled.set()
        load_stops.remove(cancelled)
    if loaded is CANCELLED or not isinstance(loaded, tuple) or _stopping(session_stop):
        timing['cancelled'] = True
        reason = 'stopped' if _stopping(session_stop) else 'cancelled' if loaded is CANCELLED else 'failed'
        _tab_load_incomplete(window, reason)
        return []
    entries, rows = loaded
    needs_library = any(kind in ('continue', 'library') for _, kind, _, _, _ in plan)
    if entries is not None:
        window._library_entries = entries
        window._library_failed = False
    elif needs_library and auth:
        window._library_failed = True
    _set_tab_loaded(window, not (needs_library and getattr(window, '_library_failed', False)))
    return rows


def _warm_shelf_posters(window, pages):
    from lib.ui.posterwarm import collect_poster_urls, request_warm

    request_warm(window, collect_poster_urls(list(pages.values())))


def _paint_rows(window, states):
    """Map this tab onto one landscape and twelve unchanged poster slots."""
    for list_id, prop, _counter in _ROW_COUNTERS:
        label = 30140 if list_id == CONTINUE_SHELF else next(
            header for _slot, panel, header in _SHELVES if panel == list_id)
        _set_shelf(window, list_id, label, prop, '', [])
    window._rows = {}
    slots = iter(_SHELVES)
    for state in states:
        if state['kind'] == 'continue':
            list_id, header, prop = CONTINUE_SHELF, 30140, 'shelf_continue'
        else:
            slot, list_id, header = next(slots)
            prop = 'shelf_%s' % slot
        window._rows[list_id] = state
        _set_shelf(window, list_id, header, prop, state['heading'], state['items'])
        if state['items']:
            window.getControl(list_id).selectItem(min(state.get('position', 0), len(state['items']) - 1))
    window._shelf_more = window._rows
    window._continue_items = next((s['items'] for s in states if s['kind'] == 'continue'), [])
    window._library_items = next((s['items'] for s in states if s['kind'] == 'library'), [])
    window._catalog_pages = {s['list_id']: s['metas'] for s in states if s['kind'] == 'catalog'}
    _number_rows(window)
    _warm_shelf_posters(window, {s['list_id']: s['metas'] for s in states})


def _fill_shelves(window, store, addons, auth, show_continue=True):
    states = _load_rows(window, store, addons, auth)
    _paint_rows(window, states)
    _log_tab_paint(window, len(states))
    _top_up_shelves(window)


def _top_up_shelves(window):
    """Only after paint; use the normal generation-checked append workers."""
    for panel, state in window._rows.items():
        if state['kind'] != 'catalog' or state.get('exhausted'):
            continue
        room = _ROW_INITIAL - len(state['metas'])
        if room > 0:
            if _row_waiting(state):
                _log_skipped_page(state)
            else:
                _schedule_shelf_extend(window, panel, room)


def mark_library_dirty():
    """The next Home paint re-reads the account library."""
    global _library_dirty
    _library_dirty = True


def refresh_library_row(window):
    if not hasattr(window, '_rows'):
        mark_library_dirty()
        return
    _refresh_saved_rows(window)


def _refresh_saved_rows(window, store=None):
    """Refresh personal rows only, retaining their loaded slice and cursor."""
    global _library_dirty
    from lib.ui.compat import L
    from lib.ui.uicommon import CANCELLED, run_cancellable

    store = store or get_store()
    auth = store.get_auth()
    entries = run_cancellable(lambda: _fetch_library(store, auth), L(30033))
    if entries is CANCELLED or entries is None:
        if entries is None:
            window._library_failed = True
            _set_tab_loaded(window, False)
        _library_dirty = True
        return
    _cancel_shelf_extends(window)
    window._library_entries = entries
    window._library_failed = False
    # Cached tabs may contain personal rows too. Rebuild them when revisited.
    place = _remember_place(window)
    _refresh_snapshot_personal(list(getattr(window, '_rows', {}).values()), store, entries, auth,
                               getattr(window, '_continue_suppressed', None))
    for panel, state in getattr(window, '_rows', {}).items():
        if state['kind'] == 'catalog':
            continue
        label = 30140 if panel == CONTINUE_SHELF else next(h for _, p, h in _SHELVES if p == panel)
        prop = next(prop for p, prop, _ in _ROW_COUNTERS if p == panel)
        _set_shelf(window, panel, label, prop, state['heading'], state['items'])
    for states, _focus in getattr(window, '_tab_snapshots', {}).values():
        _refresh_snapshot_personal(states, store, entries, auth,
                                   getattr(window, '_continue_suppressed', None))
    _number_rows(window)
    _library_dirty = False
    _restore_place(window, place)


def _refresh_snapshot_personal(states, store, entries, auth, suppressed=None):
    """Personal writes invalidate personal data, never a tab's loaded catalogs."""
    from lib.homelayout import matches_library
    from lib.ui.mystuff import library_metas

    signed_out_card = False
    for state in states:
        if state['kind'] == 'continue':
            state['items'][:] = _played_posters(store, entries, state['filter'], suppressed)
        elif state['kind'] == 'library':
            if not auth and signed_out_card:
                state['items'][:] = []
                continue
            signed_out_card = True
            metas = [meta for meta in library_metas(_newest_first(entries))
                     if matches_library(meta, state['filter'])]
            count = max(_ROW_INITIAL, len(state['items']))
            state['metas'] = metas[:count]
            state['pending'] = metas[count:]
            state['exhausted'] = not state['pending']
            state['items'][:] = [_poster_item(meta, sparse=True) for meta in state['metas']]
            if not state['items']:
                from lib.ui.compat import L

                state['items'][:] = [_poster_item({'name': L(30426 if auth else 30427),
                                                  'description': L(30423 if auth else 30428)},
                                                 action='' if auth else 'signin')]


def _snapshot_tab(window):
    """Keep loaded rows (up to 2000 each), native positions and last row."""
    if not getattr(window, '_tab_loaded', {}).get(window._active_tab, False):
        window._tab_snapshots.pop(window._active_tab, None)
        return
    rows = list(getattr(window, '_rows', {}).values())
    for panel, state in getattr(window, '_rows', {}).items():
        state['position'] = window.getControl(panel).getSelectedPosition()
    focus = window.getFocusId()
    if focus in getattr(window, '_rows', {}):
        window._tab_last_row = focus
    window._tab_snapshots[window._active_tab] = (rows, getattr(window, '_tab_last_row', None))


def _select_active_chip(window):
    control = window.getControl(BROWSE_LIST)
    for index in range(control.size()):
        item = control.getListItem(index)
        active = item.getProperty('action') == window._active_tab
        item.setProperty('active', '1' if active else '')
        if active:
            control.selectItem(index)


def _switch_tab(window, tab):
    """Switch on OK only. Discard old queued/ready loads before slot reuse."""
    from lib.ui.compat import setting_bool

    if tab == 'anime' and not setting_bool('home_show_anime', True):
        return
    complete = getattr(window, '_tab_loaded', {})
    if tab == window._active_tab and complete.get(tab, False):
        _select_active_chip(window)
        window.setFocusId(BROWSE_LIST)
        return
    if tab != window._active_tab:
        _snapshot_tab(window)
    _cancel_shelf_extends(window)
    window._active_tab = tab
    snapshot = window._tab_snapshots.get(tab)
    if snapshot is not None and complete.get(tab, False):
        rows, window._tab_last_row = snapshot
        _paint_rows(window, rows)
        _top_up_shelves(window)
    else:
        store = get_store()
        _fill_shelves(window, store, store.get_enabled_addons(), store.get_auth())
        window._tab_last_row = None
    _select_active_chip(window)
    window.setFocusId(BROWSE_LIST)
    paint_hero(window)
    _note_focus(window)


#: Settings a Home row is built from. A Settings visit that changes none
#: of them (and no sign-in, no addon change) leaves the rows, and the
#: remote's place on them, alone.
_ROW_SETTINGS = ('home_hide_adult', 'cache_generation', 'home_layout_generation', 'home_show_anime')


def _home_signature(store):
    """What the rows were built from. Equal signature, same rows."""
    from lib.ui.compat import ADDON

    auth = store.get_auth() or {}
    addons = store.get_enabled_addons() or []
    ids = []
    for descriptor in addons:
        if isinstance(descriptor, dict):
            manifest = descriptor.get('manifest') or {}
            ids.append(str(descriptor.get('transportUrl') or manifest.get('id') or ''))
    settings = []
    for key in _ROW_SETTINGS:
        try:
            settings.append(ADDON.getSetting(key))
        except Exception:
            settings.append('')
    # Settings can overwrite another interpreter's generation setting on
    # close. The saved arrangement is authoritative across interpreters.
    layout = json.dumps(store.get_home_layout(), sort_keys=True)
    return (auth.get('authKey') if isinstance(auth, dict) else None, tuple(ids), tuple(settings), layout)


#: Lists whose selection survives a refresh. The chips list is matched
#: by its `action`, a poster by its `sid`.
def _place_lists():
    return (BROWSE_LIST,) + _SHELF_IDS

def _item_key(item):
    if item is None:
        return ''
    return item.getProperty('sid') or item.getProperty('action') or item.getLabel() or ''


def _remember_place(window):
    """The focused control and, for every list, the title it is on."""
    try:
        focus = window.getFocusId()
    except Exception:
        focus = 0
    rows = {}
    for list_id in _place_lists():
        try:
            control = window.getControl(list_id)
            rows[list_id] = (_item_key(control.getSelectedItem()), control.getSelectedPosition())
        except Exception:
            continue
    return focus, rows


def _focusable(window, control_id):
    if not control_id:
        return False
    if control_id == BROWSE_LIST:
        try:
            return window.getControl(BROWSE_LIST).size() > 0
        except Exception:
            return False
    if control_id == BTN_SIGNIN:
        return bool(window.getProperty('HomeSignedOut'))
    if control_id in _BUTTON_ACTIONS or control_id == TOOLS_LIST:
        return True
    for prop, list_id in (('shelf_continue', CONTINUE_SHELF),):
        if control_id == list_id:
            return bool(window.getProperty(prop))
    for action, list_id, _label in _SHELVES:
        if control_id == list_id:
            return bool(window.getProperty('shelf_%s' % action))
    return False


def _restore_place(window, place):
    """Put every list back on the title it was on, then the focus."""
    focus, rows = place
    for list_id, (key, position) in rows.items():
        try:
            control = window.getControl(list_id)
            size = control.size()
        except Exception:
            continue
        if size <= 0:
            continue
        target = None
        if key:
            for index in range(size):
                if _item_key(control.getListItem(index)) == key:
                    target = index
                    break
        if target is None:
            target = max(0, min(position if isinstance(position, int) else 0, size - 1))
        control.selectItem(target)
    if _focusable(window, focus):
        window.setFocusId(focus)
        _note_focus(window)
    else:
        _focus_first(window)


def _drop_memory_if_refreshed(window):
    """Notice generation changes or deleted cache files after Settings closes."""
    import os

    from lib.ui import ratingsched
    from lib.ui.cacherefresh import CACHE_FILES, _cache_dirs, clear_memory
    from lib.ui.compat import ADDON

    try:
        generation = ADDON.getSetting('cache_generation') or ''
    except Exception:
        generation = getattr(window, '_cache_generation', '')
    seen = getattr(window, '_cache_generation', None)
    window._cache_generation = generation
    present = {os.path.join(folder, name): os.path.isfile(os.path.join(folder, name))
               for folder in _cache_dirs() for name in CACHE_FILES}
    previous = getattr(window, '_cache_files_present', {})
    window._cache_files_present = present
    held = bool(getattr(window, '_catalog_pages', None)) or ratingsched._cache is not None
    removed = held and any(existed and not present.get(path, False)
                           for path, existed in previous.items())
    changed = seen is not None and generation != seen
    if changed or removed:
        clear_memory()
        return True
    return False


def _after_child(window, played=False):
    """Bring Home up to date once a screen on top of it has closed.

    Kodi re-runs a dialog's onInit only when the dialog is opened again
    (here, the reopen after playback), not when a dialog above it closes.
    Home used to rely on onInit for both, so a library change or a new
    addon waited for playback, and a reopen rebuilt every row and dropped
    the remote on the first Continue poster. Now: a changed sign-in,
    addon list, or row setting rebuilds the rows and puts the remote back
    where it was; a library change, or coming back from playback,
    re-reads Continue and Library in place; anything else just repaints.
    """
    window._child_refreshed = True
    refreshed = _drop_memory_if_refreshed(window)
    try:
        store = get_store()
        signature = _home_signature(store)
    except Exception:
        return
    if refreshed or signature != getattr(window, '_signature', None) or getattr(window, '_catalog_pages', None) is None:
        place = _remember_place(window)
        window._tab_snapshots = {}
        window._library_entries = None
        window._catalog_pages = None
        window._build(place=place)
        return
    if played or _library_dirty or getattr(window, '_library_failed', False):
        _refresh_saved_rows(window, store)
    paint_hero(window)


def _open_child(window, opener):
    """Run a screen over Home. True when Home closed for playback.

    A child that returns True started playback; Home closes with it. A
    plain Back refreshes Home (`_after_child`), unless Home was closed
    for playback underneath, in which case its reopen does that.
    """
    window._child_refreshed = False
    if opener():
        window.close()
        return True
    if getattr(window, '_closed_for_playback', False) or getattr(window, '_child_refreshed', False):
        return False
    _after_child(window)
    return False


def _back(window):
    """Exact four-rung Back ladder; only its first rung resets a row."""
    focus = window.getFocusId()
    if focus in _SHELF_IDS:
        control = window.getControl(focus)
        if control.getSelectedPosition() > 0:
            control.selectItem(0)
            state = getattr(window, '_rows', {}).get(focus)
            if state:
                state['position'] = 0
        else:
            _select_active_chip(window)
            window.setFocusId(BROWSE_LIST)
    elif focus == BROWSE_LIST and getattr(window, '_active_tab', 'home') != 'home':
        _switch_tab(window, 'home')
    else:
        window.close()
    paint_hero(window)
    _note_focus(window)


def _remove_continue(window, item):
    import xbmc

    from lib.ui import dialogs
    from lib.ui.compat import L, log

    if not dialogs.confirm(L(30517), L(30415), xbmc.getLocalizedString(107), xbmc.getLocalizedString(106)):
        return
    store = get_store()
    try:
        store.remove_progress_title(item.getProperty('stype'), item.getProperty('sid'))
    except Exception as exc:
        from lib.stremio.addons import addon_error_detail

        log('home: progress removal failed: %s' % addon_error_detail(exc))
        return
    # A library playback state can recreate local progress on refresh. Hide
    # the id for this Home instance only, across every Continue variant.
    suppressed = getattr(window, '_continue_suppressed', set())
    suppressed.add(item.getProperty('sid'))
    window._continue_suppressed = suppressed
    state = window._rows[CONTINUE_SHELF]
    control = window.getControl(CONTINUE_SHELF)
    position = control.getSelectedPosition()
    state['items'][:] = _played_posters(store, getattr(window, '_library_entries', []), state['filter'], suppressed)
    _set_shelf(window, CONTINUE_SHELF, 30140, 'shelf_continue', state['heading'], state['items'])
    if state['items']:
        control.selectItem(min(position, len(state['items']) - 1))
        window.setFocusId(CONTINUE_SHELF)
    else:
        _select_active_chip(window)
        window.setFocusId(BROWSE_LIST)
    for states, _focus in getattr(window, '_tab_snapshots', {}).values():
        for saved in states:
            if saved['kind'] == 'continue':
                saved['items'][:] = _played_posters(store, getattr(window, '_library_entries', []), saved['filter'], suppressed)
    _number_rows(window)
    paint_hero(window)


def _browse_catalog(window, state):
    from lib.ui.catalogpicker import open_single_catalog

    descriptor = state['descriptor']

    def open_catalog():
        open_single_catalog(descriptor['transport'], descriptor['manifest'], descriptor['catalog'],
                            extra=state.get('extra'), meta_filter=state['filter'])
        # Like Add-ons, this child does not itself close Home. Playback's
        # existing modal handoff remains responsible for its own windows.
        return False

    _open_child(window, open_catalog)


def _offer_library(window, item, control_id=None):
    """Personal actions precede Library and Trailer in the existing menu."""
    from lib.ui.compat import L
    from lib.ui.libraryaction import meta_from_item, poster_menu

    meta = meta_from_item(item)
    if meta is None:
        return
    extra = []
    if control_id == CONTINUE_SHELF:
        if item.getProperty('stype') == 'series':
            extra.append((L(30514), lambda: _open_child(window, lambda: _open_detail_for(item))))
        extra.append((L(30517), lambda: _remove_continue(window, item)))
    state = getattr(window, '_rows', {}).get(control_id)
    if state and state['kind'] == 'catalog':
        extra.append((L(30537), lambda: _browse_catalog(window, state)))
    if poster_menu(meta, extra):
        refresh_library_row(window)


def _note_focus(window):
    """Remember where the remote is, so the next Up can tell a dead end
    from a move (`HomeWindow.onAction`), and prefetch the focused row's
    next page. Paging is best-effort: its failure must not cost the focus
    bookkeeping the dead-end Up logic relies on."""
    try:
        _maybe_extend_focused_shelf(window)
    except Exception as exc:  # noqa: BLE001 - see docstring
        import xbmc

        from lib.ui.compat import log

        log('home: paging check failed: %s' % type(exc).__name__, xbmc.LOGDEBUG)
    try:
        window._last_focus = window.getFocusId()
        if window._last_focus in getattr(window, '_rows', {}):
            window._tab_last_row = window._last_focus
    except Exception:
        window._last_focus = None


def _focus_first(window):
    """First visible row, preserving its native selected item."""
    for panel, prop, _counter in _ROW_COUNTERS:
        if window.getProperty(prop):
            window.setFocusId(panel)
            _note_focus(window)
            return
    window.setFocusId(BROWSE_LIST)
    _note_focus(window)

def _status_text(auth):
    """Home's top status line. Both states are prefixed with a
    `[COLOR]`-tinted dot (font12 is NotoSans and covers U+25CF).

    Signed in reads "Signed in", not the account's address: Home is on a
    shared TV, often with guests on the sofa (#19). The address stays in
    Settings > Account (`account_user`, kept current by `_build`).
    """
    from lib.ui.compat import L

    if not auth:
        return '[COLOR 57EEF3F6]\u25cf[/COLOR] %s' % L(30190)
    return '[COLOR FFD6D6D6]\u25cf[/COLOR] %s' % L(30488)


def _fit_hero_title(text):
    """One line for the title band. The label does not wrap, and a long
    name used to run through the metadata under it."""
    text = text or ''
    if len(text) <= 48:
        return text
    return text[:47].rstrip() + '\u2026'


def _append_to_shelf(window, list_id, metas, sparse=False):
    """Append only; restoring Kodi's cursor never rebuilds existing cells."""
    if not metas:
        return
    control = window.getControl(list_id)
    pos = max(0, control.getSelectedPosition())
    items = [_poster_item(meta, sparse=sparse) for meta in metas]
    control.addItems(items)
    control.selectItem(pos)
    state = getattr(window, '_rows', {}).get(list_id)
    if state is not None:
        state['items'].extend(items)
        state['metas'].extend(metas)
    elif list_id == LIBRARY_SHELF:
        window._library_items.extend(items)
    _warm_shelf_posters(window, {'appended': metas})


def _shelf_lock(window):
    lock = getattr(window, '_shelf_lock', None)
    if lock is None:
        lock = threading.Lock()
        window._shelf_lock = lock
    return lock


def _cancel_shelf_extends(window):
    with _shelf_lock(window):
        window._tab_generation = getattr(window, '_tab_generation', 0) + 1
        window._shelf_queue = []
        window._shelf_ready = []
        window._shelf_inflight = {}


def _current_shelf(window, generation, panel, state):
    return (not _stopping(_home_stop(window))
            and generation == getattr(window, '_tab_generation', 0)
            and getattr(window, '_rows', {}).get(panel) is state)


def _schedule_shelf_extend(window, panel, room):
    """One job per row, two daemon workers, newest queued request first.

    Workers mutate private paging copies. Only a current result can commit
    its offsets/pending ids, so leaving a tab during HTTP loses no posters
    from that tab's saved snapshot. Running HTTP is never interrupted.
    """
    state = getattr(window, '_rows', {}).get(panel)
    if not state or _stopping(_home_stop(window)):
        return
    with _shelf_lock(window):
        inflight = getattr(window, '_shelf_inflight', {})
        window._shelf_inflight = inflight
        if panel in inflight:
            return
        generation = getattr(window, '_tab_generation', 0)
        inflight[panel] = (generation, state)
        jobs = getattr(window, '_shelf_queue', [])
        window._shelf_queue = jobs
        jobs.append((generation, panel, state, room))
        running = getattr(window, '_shelf_running', 0)
        threads = getattr(window, '_shelf_threads', [])
        window._shelf_threads = [thread for thread in threads if thread.is_alive()]
        if running < 2:
            window._shelf_running = running + 1
            thread = threading.Thread(target=_shelf_worker, args=(window,),
                                      name='rivulet-home-page', daemon=True)
            window._shelf_threads.append(thread)
            thread.start()


def _shelf_worker(window):
    retired = False
    try:
        while True:
            with _shelf_lock(window):
                if _stopping(_home_stop(window)) or not window._shelf_queue:
                    window._shelf_running -= 1
                    retired = True
                    return
                generation, panel, state, room = window._shelf_queue.pop()
            queued = False
            try:
                if not _current_shelf(window, generation, panel, state):
                    continue
                private = dict(state)
                private['_stop'] = _home_stop(window)
                private['pending'] = list(state.get('pending', []))
                private['seen_ids'] = set(state.get('seen_ids', set()))
                metas = _pull_more(private, room)
                with _shelf_lock(window):
                    if not _current_shelf(window, generation, panel, state):
                        continue
                    ready = getattr(window, '_shelf_ready', [])
                    window._shelf_ready = ready
                    ready.append((generation, panel, state, private, metas))
                    queued = True
                import xbmc
                xbmc.executebuiltin('Action(noop)')
            except Exception as exc:
                from lib.stremio.addons import addon_error_detail
                from lib.ui.compat import log

                log('home: shelf load failed: %s' % addon_error_detail(exc))
            finally:
                if not queued:
                    with _shelf_lock(window):
                        if window._shelf_inflight.get(panel) == (generation, state):
                            window._shelf_inflight.pop(panel, None)
    finally:
        if not retired:
            with _shelf_lock(window):
                window._shelf_running -= 1


def _join_shelf_extends(window):
    """Test hook; production applies results on Action(noop)."""
    for thread in list(getattr(window, '_shelf_threads', [])):
        thread.join(timeout=2)


def _apply_shelf_extends(window):
    with _shelf_lock(window):
        ready = list(getattr(window, '_shelf_ready', []))
        window._shelf_ready = []
    for generation, panel, state, private, metas in ready:
        if not _current_shelf(window, generation, panel, state) or not _focusable(window, panel):
            window._shelf_inflight.pop(panel, None)
            continue
        for key in ('pending', 'seen_ids', 'served', 'exhausted', 'failed', 'end_reason', 'page_number'):
            if key in private:
                state[key] = private[key]
        _append_to_shelf(window, panel, metas, sparse=(state['kind'] == 'library'))
        window._shelf_inflight.pop(panel, None)
        if len(state['metas']) >= _ROW_CEILING:
            state['exhausted'] = True
            state['end_reason'] = '2000-item safety ceiling'
        _log_row_end(state)


def _maybe_extend_focused_shelf(window):
    """Prefetch within five cells; the GUI never waits on a page."""
    panel = window.getFocusId()
    state = getattr(window, '_rows', {}).get(panel)
    if not state or state.get('exhausted') or state['kind'] == 'continue' or _row_waiting(state):
        return
    control = window.getControl(panel)
    if control.getSelectedPosition() < max(0, control.size() - PREFETCH_THRESHOLD):
        return
    room = min(_ROW_INITIAL, _ROW_CEILING - len(state['metas']))
    if room > 0:
        _schedule_shelf_extend(window, panel, room)


def _fetch_library_meta(stype, sid, stop=None):
    """Backdrop, description, and ratings for one library title.

    A library record is a name and a poster. The other rows already
    show whatever installed addon answers ``meta`` (Cinemeta, when it
    is installed). The same call fills this hero. A miss leaves the
    poster, drawn full-bleed.
    """
    from lib.ui.views import _fetch_meta_for_home

    return _fetch_meta_for_home(stype, sid, should_stop=lambda: _stopping(stop), stop_event=stop)


#: Seconds before a failed library/Continue meta fetch is tried again.
_META_RETRY_SECONDS = 20.0


def _library_meta_lock(window):
    lock = getattr(window, '_library_meta_lock', None)
    if lock is None:
        lock = threading.Lock()
        window._library_meta_lock = lock
    return lock


def _schedule_library_meta(window, item):
    """Fetch the focused library title off the GUI thread.

    The remote must not wait on a catalog. The poster (full-bleed, when
    there is no backdrop) stays up until the answer is applied on the
    next action. A title already fetched, or one in flight, is not
    asked again.
    """
    if _stopping(_home_stop(window)):
        return
    if item is None or item.getProperty('needs_meta') != '1':
        with _library_meta_lock(window):
            previous = getattr(window, '_library_meta_latest', None)
            if previous is not None:
                previous[0].setProperty('meta_state', '')
            window._library_meta_latest = None
            window._library_meta_pending = None
            window._library_meta_ready = []
        return
    state = item.getProperty('meta_state')
    if state in ('loading', 'done'):
        return
    if state.startswith('failed:'):
        # A fetch that failed (timeout, addon down) is asked again after
        # a pause, not left blank for the session.
        import time
        try:
            failed_at = float(state.split(':', 1)[1])
        except ValueError:
            failed_at = 0.0
        if time.time() - failed_at < _META_RETRY_SECONDS:
            return
    stype = item.getProperty('stype')
    sid = item.getProperty('sid')
    if not stype or not sid:
        return
    request = (item, stype, sid)
    with _library_meta_lock(window):
        previous = getattr(window, '_library_meta_latest', None)
        if previous is not None and previous[0] is not item:
            previous[0].setProperty('meta_state', '')
        item.setProperty('meta_state', 'loading')
        window._library_meta_latest = request
        window._library_meta_pending = request
        window._library_meta_ready = []
        if getattr(window, '_library_meta_running', False):
            window._library_meta_idle.clear()
            window._library_meta_wake.set()
            return
        _start_library_meta(window)


def _start_library_meta(window):
    """Caller holds the slot lock, including a playback reopen handoff."""
    window._library_meta_running = True
    wake = threading.Event()
    idle = threading.Event()
    window._library_meta_wake = wake
    window._library_meta_idle = idle
    wake.set()

    # Reuse one worker while Home is open; close wakes it explicitly even
    # while Kodi (or a caller) still owns the window object.
    window_ref = weakref.ref(window, lambda ref: wake.set())
    thread = threading.Thread(target=_library_meta_worker, args=(window_ref, wake, idle, _home_stop(window)),
                              name='rivulet-library-meta', daemon=True)
    window._library_meta_thread = thread
    thread.start()


def _library_meta_worker(window_ref, wake, idle, stop):
    """Serve one Home session; neither GC nor another key press ends it."""
    while True:
        wake.wait(timeout=1.0)
        wake.clear()
        window = window_ref()
        if window is None:
            return
        with _library_meta_lock(window):
            if _stopping(stop):
                window._library_meta_running = False
                idle.set()
                if window._library_meta_pending is not None and not _stopping(_home_stop(window)):
                    _start_library_meta(window)
                return
        _drain_library_meta(window, idle, stop)
        with _library_meta_lock(window):
            if not stop.is_set() and window._library_meta_pending is None:
                window._library_meta_running = False
                idle.set()
                return
        del window


def _drain_library_meta(window, idle, stop):
    while True:
        with _library_meta_lock(window):
            if _stopping(stop):
                idle.set()
                return
            wanted = window._library_meta_pending
            window._library_meta_pending = None
            if wanted is None:
                idle.set()
                return
        _item, wanted_type, wanted_id = wanted
        try:
            meta = _fetch_library_meta(wanted_type, wanted_id, stop)
        except Exception:
            meta = None
        with _library_meta_lock(window):
            if _stopping(stop) or window._library_meta_latest is not wanted:
                continue
            window._library_meta_ready = [(wanted_type, wanted_id, meta)]
        try:
            import xbmc
            xbmc.executebuiltin('Action(noop)')
        except Exception:
            pass


def _join_library_meta(window):
    """Test hook. Production applies the answer on the worker's noop."""
    idle = getattr(window, '_library_meta_idle', None)
    if idle is not None:
        idle.wait(timeout=2)


_HERO_PROPS = (
    'thumbnail', 'fanart', 'hero_logo', 'hero_title', 'hero_meta',
    'hero_plot', 'hero_facts', 'hero_fill', 'hero_runtime', 'needs_meta', 'stype', 'sid',
)


def _write_meta_onto(item, meta, keep_label=False):
    """Copy a fetched meta onto the poster that is already on the row.

    Rebuilding the list would send the cursor home. The card keeps its
    place; the hero reads the new properties on the next paint. No
    backdrop still means the poster, full-bleed.
    """
    merged = dict(meta)
    if not merged.get('poster'):
        merged['poster'] = item.getProperty('thumbnail')
    if not merged.get('name'):
        merged['name'] = item.getLabel()
    if not merged.get('id'):
        merged['id'] = item.getProperty('sid')
    if not merged.get('type'):
        merged['type'] = item.getProperty('stype')
    fresh = _poster_item(merged)
    background = merged.get('background') or ''
    poster = merged.get('poster') or item.getProperty('thumbnail') or ''
    if not background and poster:
        from lib.ui.urlutil import sized_image

        fresh.setProperty('hero_fill', '1')
        fresh.setProperty('fanart', sized_image(poster, 'fill'))
    fresh.setProperty('needs_meta', '')
    for key in _HERO_PROPS:
        item.setProperty(key, fresh.getProperty(key))
    label = fresh.getLabel()
    # A Continue card keeps its own label (the show it was started as).
    if label and not keep_label:
        item.setLabel(label)
    item.setProperty('meta_state', 'done')


def episode_label_for(meta, video_id):
    """``(label, overview)`` for `video_id` (``tt..:1:3``): ``S1E3 ·
    Title``, the title and overview from the series meta's videos when it
    has them. ``('', '')`` for anything that is not an episode id."""
    from lib.newepisodes import _episode_label

    if not isinstance(video_id, str) or not video_id:
        return '', ''
    show, season, episode = (video_id.rsplit(':', 2) + ['', ''])[:3]
    if not (show and season.isdigit() and episode.isdigit()):
        return '', ''
    title = overview = ''
    videos = meta.get('videos') if isinstance(meta, dict) else None
    for video in videos or []:
        if isinstance(video, dict) and video.get('id') == video_id:
            title = str(video.get('title') or video.get('name') or '').strip()
            overview = str(video.get('overview') or video.get('description') or '').strip()
            break
    return _episode_label(int(season), int(episode), title), overview


def apply_episode_hero(item, show, label, overview=''):
    """The hero for an episode card: the show's own hero, with the
    episode on top of the facts.

    The card keeps the episode still; the hero does not blow it up. The
    backdrop, clearlogo and title are the show's (its poster full-bleed
    when it has no backdrop), the first facts line is ``S1E3 · Title``,
    then the show's year, runtime and genres, and the plot is the
    episode's overview when there is one.
    """
    from lib.ui.uicommon import escape_label
    from lib.ui.urlutil import sized_image

    show = show if isinstance(show, dict) else {}
    background = show.get('background') or ''
    poster = show.get('poster') or ''
    if isinstance(background, str) and background:
        item.setProperty('fanart', sized_image(background, 'backdrop'))
        item.setProperty('hero_fill', '')
    elif isinstance(poster, str) and poster:
        item.setProperty('fanart', sized_image(poster, 'fill'))
        item.setProperty('hero_fill', '1')
    item.setProperty('hero_logo', show.get('logo') or '')
    name = show.get('name') or ''
    if name:
        item.setProperty('hero_title', escape_label(name))
    lines = [label] if label else []
    panel = _hero_meta_text(show)
    if panel:
        lines.append(panel)
    item.setProperty('hero_meta', escape_label('\n'.join(lines)))
    # The panel is written whole here; the ratings overlay would rebuild
    # it from the show's scores and drop the episode line.
    item.setProperty('hero_facts', '')
    item.setProperty('hero_plot', escape_label(_truncate(overview or show.get('description') or '', 280)))
    item.setProperty('hero_runtime', _runtime_minutes(show))


def _apply_library_meta(window):
    """Write fetched library metas onto their posters. UI thread only."""
    with _library_meta_lock(window):
        ready = list(getattr(window, '_library_meta_ready', None) or [])
        window._library_meta_ready = []
    if getattr(window, '_library_meta_latest', None) is not None:
        try:
            focused = window.getControl(window.getFocusId()).getSelectedItem()
        except Exception:
            focused = None
        latest = window._library_meta_latest
        if (focused is None or focused.getProperty('stype') != latest[1]
                or focused.getProperty('sid') != latest[2]):
            with _library_meta_lock(window):
                latest[0].setProperty('meta_state', '')
                window._library_meta_latest = None
                window._library_meta_pending = None
            ready = []
    if not ready:
        return
    import time

    items = []
    # Library records and Continue entries both arrive without a
    # description; both rows get the same hero once the meta lands.
    rows = getattr(window, '_rows', None)
    shelves = [panel for panel, state in rows.items() if state['kind'] in ('library', 'continue')] if rows is not None else (LIBRARY_SHELF, CONTINUE_SHELF)
    for shelf in shelves:
        try:
            control = window.getControl(shelf)
            items.extend((shelf, control.getListItem(index)) for index in range(control.size()))
        except Exception:
            continue
    for stype, sid, meta in ready:
        for shelf, item in items:
            if item.getProperty('stype') != stype or item.getProperty('sid') != sid:
                continue
            if meta is None:
                item.setProperty('meta_state', 'failed:%d' % int(time.time()))
                continue
            useful = isinstance(meta, dict) and (
                meta.get('name') or meta.get('background')
                or meta.get('description') or meta.get('poster')
            )
            if useful:
                _write_meta_onto(item, meta, keep_label=(shelf == CONTINUE_SHELF))
                if shelf == CONTINUE_SHELF:
                    _fill_continue_card(item, meta)
                    label, overview = episode_label_for(meta, item.getProperty('video_id'))
                    if label:
                        apply_episode_hero(item, meta, label, overview)
            else:
                item.setProperty('needs_meta', '')
                item.setProperty('meta_state', 'done')


def _fill_continue_card(item, meta):
    """A Continue card with no wide picture takes the fetched backdrop."""
    if item.getProperty('land'):
        return
    background = meta.get('background') if isinstance(meta, dict) else ''
    if isinstance(background, str) and background:
        from lib.ui.urlutil import sized_image

        item.setProperty('land', sized_image(background, 'backdrop'))


def _upcoming_facts(window, count=2):
    """`hero_facts` of the next posters on the focused row, so their
    ratings ride along once the focus rests. [] off a container."""
    facts = []
    try:
        control = window.getControl(window.getFocusId())
        position = control.getSelectedPosition()
        size = control.size()
    except Exception:
        return facts
    if not isinstance(position, int) or position < 0:
        return facts
    for index in range(position + 1, min(size, position + 1 + count)):
        try:
            raw = control.getListItem(index).getProperty('hero_facts') or ''
            parsed = json.loads(raw) if raw else None
        except Exception:
            parsed = None
        if isinstance(parsed, dict):
            facts.append(parsed)
    return facts


#: Two 1x1 image slots under the backdrop plate (Home and the hubs) that
#: load a clearlogo before the hero is allowed to show it.
LOGO_WARM_SLOTS = (30098, 30099)

#: How long a logo found missing from Kodi's texture cache is trusted to
#: still be missing before the cache is asked again. A logo that loaded
#: is known good for the rest of the session.
_LOGO_RECHECK_SECONDS = 3.0


def _texture_cached(url):
    """True when Kodi's texture cache holds `url`, i.e. it loaded before.

    A clearlogo that 404s never gets a row in Textures13.db; one that
    loaded once does, in this session or an earlier one. One indexed
    lookup over JSON-RPC (``Textures.GetTextures``). Any failure answers
    False, which only means the text title is shown.
    """
    from lib.ui.logocheck import texture_cached

    return texture_cached(url)


def _logo_known_good(url):
    """Whether the hero may swap the text title for this clearlogo.

    The skin hides the title whenever a logo is set, so a logo URL that
    404s left the title area blank (#10). Only a logo Kodi has already
    loaded replaces the text; until then the title shows and the logo is
    warmed in a slot.
    """
    import time

    from lib.ui import logocheck

    if not url:
        return False
    # Answered over HTTP (lib/ui/logocheck.py): an image, or not there.
    answered = logocheck.state(url)
    if answered is not None:
        return answered
    now = time.monotonic()
    state = logocheck.texture_state(url)
    if state is not None and (state[0] or now - state[1] < _LOGO_RECHECK_SECONDS):
        return state[0]
    ok = _texture_cached(url)
    logocheck.remember_texture(url, ok, now)
    if ok:
        logocheck.remember(url, True)
    else:
        # Kodi has not drawn it yet: ask its host. The worker repaints
        # the hero when the logo turns out to be real.
        logocheck.ask([url])
    return ok


def _upcoming_logos(window, count=3):
    """Clearlogos of the next posters on the focused row."""
    logos = []
    try:
        control = window.getControl(window.getFocusId())
        position = control.getSelectedPosition()
        size = control.size()
    except Exception:
        return logos
    if not isinstance(position, int) or position < 0:
        return logos
    for index in range(position + 1, min(size, position + 1 + count)):
        try:
            logo = control.getListItem(index).getProperty('hero_logo') or ''
        except Exception:
            logo = ''
        if logo:
            logos.append(logo)
    return logos


def _warm_logos(window, logos):
    """Load `logos` in the 1x1 slots so Kodi caches them. A window
    without the slots warms nothing."""
    from lib.ui import logocheck

    pending = [logo for logo in logos if logo and not (logocheck.texture_state(logo) or (False, 0))[0]]
    for slot, logo in zip(LOGO_WARM_SLOTS, pending):
        try:
            window.getControl(slot).setImage(logo)
        except Exception:
            return


def _set_fill_image(window, image):
    """The scale plate. Hubs have no library row, so the control is absent there."""
    try:
        window.getControl(HERO_FILL).setImage(image)
    except Exception:
        pass


def paint_hero(window, item=None):
    """Paint the backdrop from the focused poster.

    A button has no selected item. Leaving the labels alone in that case
    keeps the last title on screen while the remote is on the top bar,
    which is the reading order of this layout. A poster with no fanart
    falls back to the addon fanart so the hero never goes blank.

    A container that has scrolled past the items it actually holds hands
    back a blank list item. Writing that blank is what cleared the title
    and the plot and left the row looking dead. Keep the previous hero.
    """
    from lib.ui.newlook import fanart as addon_fanart

    if item is None:
        try:
            focus_id = window.getFocusId()
            if focus_id:
                item = window.getControl(focus_id).getSelectedItem()
        except Exception:
            item = None
    if item is None:
        return
    # A library poster has no plot until Cinemeta answers. Ask now, and
    # keep the poster on screen while that request is in flight.
    _schedule_library_meta(window, item)
    title = item.getProperty('hero_title') or ''
    label = item.getLabel() or ''
    if not title and not label and not item.getProperty('sid') and not item.getProperty('hero_logo'):
        return
    meta = item.getProperty('hero_meta') or ''
    facts_raw = item.getProperty('hero_facts') or ''
    from lib.ui.ratingsched import needs_refresh

    if facts_raw and (item.getProperty('facts_done') != '1' or needs_refresh(facts_raw)):
        # A ratings key rebuilds this from the cached scores. No key, or
        # a cache miss, leaves the catalog line alone and returns at once.
        from lib.ui.ratingsched import overlay_text

        try:
            facts = json.loads(facts_raw)
        except ValueError:
            facts = None
        if isinstance(facts, dict):
            overlaid = overlay_text(facts, meta, _upcoming_facts(window))
            if overlaid != meta:
                # Scores applied: keep the result on the card, so the next
                # paint of this title skips the decode and the merge (#17).
                item.setProperty('hero_meta', overlaid)
                item.setProperty('facts_done', '1')
            meta = overlaid
    plot = item.getProperty('hero_plot') or item.getProperty('subtitle') or ''
    fanart = item.getProperty('fanart') or ''
    wanted_logo = item.getProperty('hero_logo') or ''
    # The text title stays up until Kodi has loaded the logo once (#10).
    logo = wanted_logo if _logo_known_good(wanted_logo) else ''
    upcoming_logos = _upcoming_logos(window)
    _warm_logos(window, ([wanted_logo] if wanted_logo and not logo else []) + upcoming_logos)
    # The next titles' logos are checked ahead, so they are ready on arrival.
    from lib.ui import logocheck

    logocheck.ask(upcoming_logos)
    fill = item.getProperty('hero_fill') == '1' and bool(fanart)
    runtime = item.getProperty('hero_runtime') or ''
    # Every action repaints (worker noops, mouse moves, a key that did not
    # move). An unchanged hero is a dozen boundary crossings and a bar
    # re-measure for nothing (#17).
    painted = (title or label, meta, plot, fanart, fill, logo, runtime)
    if painted == getattr(window, '_hero_painted', None):
        return
    window._hero_painted = painted
    try:
        window.setProperty('HeroLogo', logo)
        window.getControl(HERO_LOGO).setImage(logo)
        fitted = _fit_hero_title(title or label)
        window.getControl(HERO_TITLE).setLabel(fitted)
        window.getControl(HERO_META).setText(meta)
        window.getControl(HERO_PLOT).setText(plot)
        image = fanart or addon_fanart()
        # HeroFill shows the scale plate and hides the letterboxed one.
        # A real backdrop clears it, so the picture is shown whole.
        window.setProperty('HeroFill', '1' if fill else '')
        window.getControl(BACKGROUND).setImage(image)
        _set_fill_image(window, image if fill else '')
        from lib.ui import newlook

        # No bar: the facts and scores lines, stacked up from the row.
        newlook.paint_hero(window, plot, meta, runtime)
    except Exception as exc:
        if not getattr(window, '_hero_failure_logged', False):
            import xbmc

            from lib.stremio.addons import addon_error_detail
            from lib.ui.compat import log

            window._hero_failure_logged = True
            log('home: hero paint failed: %s' % addon_error_detail(exc), xbmc.LOGDEBUG)


class NewHomeWindow(BaseWindow):
    """See module docstring. Built/run by `homewindow.open_home()`."""

    def onInit(self):
        self._hero_failure_logged = False
        # Playback reopens this same SWIG window. A fresh event must never
        # revive workers carrying the previous session's cancellation token.
        if _home_stop(self).is_set():
            self._home_stop = threading.Event()
        # Kodi re-runs onInit when this dialog is opened again, which is
        # the reopen after playback. That refreshes Continue in place and
        # keeps the remote where it was. The first open builds the rows.
        if getattr(self, '_built', False):
            _after_child(self, played=True)
            return
        self._build()
        self._built = True
        shown = getattr(self, '_home_on_shown', None)
        if shown is not None:
            self._home_on_shown = None
            shown()

    def close(self):
        _home_stop(self).set()
        for stop in getattr(self, '_home_load_stops', []):
            stop.set()
        _cancel_shelf_extends(self)
        with _library_meta_lock(self):
            previous = getattr(self, '_library_meta_latest', None)
            if previous is not None:
                previous[0].setProperty('meta_state', '')
            self._library_meta_latest = None
            self._library_meta_pending = None
            self._library_meta_ready = []
            wake = getattr(self, '_library_meta_wake', None)
            if wake is not None:
                wake.set()
        from lib.ui import catalogpages, logocheck, posterwarm, ratingsched

        catalogpages.stop()
        posterwarm.stop(self)
        logocheck.stop()
        ratingsched.stop()
        super().close()

    def _build(self, place=None):
        """Keep a failed build on Home, with empty/hidden poster rows.

        A data failure must not send the whole addon to recovery. This
        uses the same hidden-row state as a failed catalog fetch and
        adds no error controls to the pinned skin.
        """
        _cancel_shelf_extends(self)
        self._active_tab = getattr(self, '_active_tab', 'home')
        _set_tab_loaded(self, False)
        self._tab_snapshots = getattr(self, '_tab_snapshots', {})
        try:
            self._build_rows(place)
        except Exception as exc:
            import xbmc

            from lib.stremio.addons import addon_error_detail
            from lib.ui.compat import log

            log('home: build failed: %s' % addon_error_detail(exc), xbmc.LOGERROR)
            _tab_load_incomplete(self, 'failed')
            _cancel_shelf_extends(self)
            self._rows = {}
            for list_id, prop, _counter in _ROW_COUNTERS:
                self.setProperty(prop, '')
                self.getControl(list_id).reset()
            _number_rows(self)
            _focus_first(self)

    def _build_rows(self, place=None):
        from lib.homelayout import default_layout, installed_catalogs
        from lib.ui.compat import setting_bool
        from lib.ui.newlook import fanart as addon_fanart

        store = get_store()
        auth = store.get_auth()
        self._hero_painted = None
        if place is None:
            self.getControl(BACKGROUND).setImage(addon_fanart())
        addons = store.get_enabled_addons()
        self._layout = store.get_home_layout() if hasattr(store, 'get_home_layout') else default_layout()
        self._catalogs = installed_catalogs(addons, setting_bool('home_hide_adult', True))
        browse = _menu_items(addons, self._layout, self._active_tab, store.get_progress_entries())
        if self._active_tab not in [item.getProperty('action') for item in browse]:
            self._active_tab = 'home'
        chips = self.getControl(BROWSE_LIST)
        chips.reset()
        chips.addItems(browse)
        self.setProperty('HomeBrowse', '1' if browse else '')
        self.setProperty('HomeSignedOut', '' if auth else '1')
        self.getControl(STATUS_LABEL).setLabel(_status_text(auth))
        _fill_shelves(self, store, addons, auth)
        _select_active_chip(self)
        if place is None:
            _focus_first(self)
        else:
            _restore_place(self, place)
        paint_hero(self)
        try:
            self._signature = _home_signature(store)
        except Exception:
            self._signature = None
        # The refresh generation these rows were built under.
        _drop_memory_if_refreshed(self)

    @gated_input
    def onAction(self, action):
        # The library fetch finished on a worker and woke a noop. The
        # poster properties have to be current before the hero paints.
        from lib.ui.posterwarm import apply_pending
        from lib.ui.uicommon import BACK_ACTIONS

        apply_pending(self)
        _apply_library_meta(self)
        _apply_shelf_extends(self)
        action_id = action.getId()
        if action_id in BACK_ACTIONS:
            _back(self)
            return
        if action_id == 117:
            focus = self.getFocusId()
            if focus in _CLICKABLE and focus != BROWSE_LIST:
                _offer_library(self, self.getControl(focus).getSelectedItem(), focus)
            return
        focus = self.getFocusId()
        if action_id == 4 and getattr(self, '_last_focus', None) == BROWSE_LIST:
            _select_active_chip(self)
            _focus_first(self)
        elif focus != BROWSE_LIST and getattr(self, '_last_focus', None) == BROWSE_LIST:
            _select_active_chip(self)
        paint_hero(self)
        _note_focus(self)

    @gated_input
    def onClick(self, control_id):
        _note_focus(self)
        if control_id in _BUTTON_ACTIONS:
            handler = _ACTIONS.get(_BUTTON_ACTIONS[control_id])
            if handler:
                handler(self)
            return
        if control_id not in _CLICKABLE:
            return
        focused = self.getControl(control_id).getSelectedItem()
        _activate(self, focused, control_id)


def _open_signin(window):
    """The existing sign-in dialog, then a refresh of this window.

    `views.sign_in()` writes auth.json in this addon's profile. The new
    auth key changes Home's signature, so `_after_child` rebuilds the
    rows for it and keeps the remote where it was. Not `views.login()`:
    that ends a plugin handle, and Home has none to end (#22).
    """
    from lib.ui.views import sign_in

    sign_in()
    _after_child(window)


def _continue_card(entry):
    """``(wide picture, "S:1 E:2")`` for a Continue card (#9).

    The card is 16:9; the poster scaled into it was a slice through the
    middle of the poster. An episode with a Cinemeta id uses that
    episode's still (the picture the New Episodes cards use), anything
    else its backdrop. ``""`` when there is neither: the skin then shows
    the poster whole on the plate.
    """
    from lib.newepisodes import episode_still_url
    from lib.ui.urlutil import sized_image

    land = ''
    code = ''
    video_id = entry.get('video_id')
    if isinstance(video_id, str) and video_id:
        show, season, episode = (video_id.rsplit(':', 2) + ['', ''])[:3]
        if show and season.isdigit() and episode.isdigit():
            code = 'S:%d E:%d' % (int(season), int(episode))
            land = episode_still_url(show, int(season), int(episode))
    if not land:
        background = entry.get('background') or ''
        land = sized_image(background, 'backdrop') if isinstance(background, str) else ''
    return land, code


def _continue_item(entry):
    """A Continue poster. Carries the resume fields the dialog reads.

    Catalog and Library posters stay on `_poster_item` alone: those
    clicks still open the page, and the skin ignores unknown properties.
    The card itself is wide art with the show and the episode written
    bottom-left, the way the New Episodes cards read (#9).
    """
    from lib.ui.uicommon import escape_label

    item = _poster_item(entry)
    land, code = _continue_card(entry)
    props = {
        'land': land,
        'card_show': escape_label(entry.get('name') or ''),
        'card_code': code,
    }
    # Progress records carry a name and a poster, not a description:
    # fetch the meta on focus, the way Library does, so the hero reads
    # the same on every row. An episode always asks: its hero is the
    # show's (backdrop, logo) with the episode line on top.
    episode = bool(code)
    if (episode or not entry.get('description')) and entry.get('id') and entry.get('type'):
        props['needs_meta'] = '1'
    if episode and not entry.get('background'):
        # Until the show's meta lands, no picture beats the record's
        # poster blown up to the hero: that can be the episode still.
        props['fanart'] = ''
        props['hero_fill'] = ''
    band = entry.get('band')
    if band:
        props['band'] = str(band)
    video_id = entry.get('video_id')
    if video_id:
        props['video_id'] = str(video_id)
    for key in ('position_ms', 'duration_ms'):
        value = entry.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            props[key] = str(int(value))
    item.setProperties(props)
    return item


def _property_int(item, key):
    raw = item.getProperty(key) or ''
    if not raw:
        return 0
    try:
        return int(raw)
    except ValueError:
        return 0


def _continue_offset_ms(item):
    """Milliseconds to offer Resume for, or 0 when this poster is not one.

    Next-up and recently finished share the Continue row and have no
    mid-title position. A duration-0 account pointer (timeOffset 1) is
    how Stremio marks "play the next episode", not a place to seek.
    The band check uses the same window the row was built with.
    """
    from lib.ui.mystuff import BAND_RESUME, RESUME_MAX_PERCENT, RESUME_MIN_PERCENT

    if item.getProperty('band') != BAND_RESUME:
        return 0
    position = _property_int(item, 'position_ms')
    duration = _property_int(item, 'duration_ms')
    if position <= 0 or duration <= 0:
        return 0
    percent = (float(position) / float(duration)) * 100.0
    if percent < RESUME_MIN_PERCENT or percent >= RESUME_MAX_PERCENT:
        return 0
    return position


def _continue_art(item):
    art = {}
    poster = item.getProperty('thumbnail') or ''
    fanart = item.getProperty('fanart') or ''
    if poster:
        art['poster'] = poster
    if fanart:
        art['fanart'] = fanart
    return art or None


def _continue_item_meta(item, show_id):
    """OSD / now-playing context for a Continue resume.

    `meta['id']` is the show (or movie) id. The play call's own sid is
    the episode id when there is one, which is what the picker passes,
    so subtitles and the progress key stay on the episode while sync
    stays on the show.
    """
    label = item.getProperty('hero_title') or item.getLabel() or ''
    item_meta = {}
    if label:
        item_meta['label'] = label
    art = _continue_art(item)
    if art:
        item_meta['art'] = art
    meta = {'id': show_id, 'type': item.getProperty('stype') or ''}
    if label:
        meta['name'] = label
    poster = item.getProperty('thumbnail') or ''
    if poster:
        meta['poster'] = poster
    item_meta['meta'] = meta
    return item_meta


def _continue_choice(item, offset_ms):
    """0 resume, 1 the existing page, -1 Back. Back closes the dialog."""
    from lib.ui.compat import L
    from lib.ui.dialogs import choose
    from lib.ui.playbackmeta import episode_label_from_id, format_resume_clock

    clock = format_resume_clock(offset_ms / 1000.0)
    code = episode_label_from_id(item.getProperty('video_id'))
    if code:
        resume = L(30438) % (code, clock)
    else:
        resume = L(30439) % (clock,)
    second = L(30440) if item.getProperty('stype') == 'series' else L(30441)
    heading = item.getProperty('hero_title') or item.getLabel() or ''
    return choose(heading, [resume, second])


def _meta_for_resume(stype, show_id, item_meta):
    """Fill `videos` before play, or report that the user cancelled.

    Continue's poster does not carry the episode list. The stop push
    still records a position without it. A finish does not: with no
    list, `finish_playback` leaves the watched bits alone and, with no
    next episode id, zeroes the offset instead of pointing Continue at
    the next episode. This is the same meta fetch the episode list
    already ran. Back on the spinner cancels the resume. A miss plays
    anyway, because the position sync does not need the list.
    """
    import xbmc

    from lib.ui.compat import L, log
    from lib.ui.uicommon import CANCELLED, run_cancellable
    from lib.ui.views import _fetch_meta

    fetched = run_cancellable(lambda: _fetch_meta(stype, show_id), L(30033))
    if fetched is CANCELLED:
        log('home: resume cancelled while loading the title', xbmc.LOGINFO)
        return item_meta, True
    if isinstance(fetched, dict) and (fetched.get('id') or fetched.get('videos')):
        meta = dict(fetched)
        meta['id'] = fetched.get('id') or show_id
        item_meta = dict(item_meta)
        item_meta['meta'] = meta
        if not item_meta.get('label') and meta.get('name'):
            item_meta['label'] = meta.get('name')
    return item_meta, False


def _resume_continue(item, offset_ms):
    """The stream picker for the episode (or movie) the card is on.

    Picking a stream plays it the stock way: the player finds the same
    saved position in the local progress cache and offers to resume
    there (`player._maybe_resume_offset_ms`). `offset_ms` is what the
    card offered; the player re-reads it, so it is not passed on.
    """
    from lib.ui.streamswindow import open_streams

    stype = item.getProperty('stype')
    show_id = item.getProperty('sid')
    video_id = item.getProperty('video_id') or None
    play_id = video_id or show_id
    item_meta, cancelled = _meta_for_resume(stype, show_id, _continue_item_meta(item, show_id))
    if cancelled:
        return False
    return open_streams(
        stype, play_id,
        poster=item.getProperty('thumbnail') or None,
        heading=item.getProperty('hero_title') or item.getLabel() or '',
        art=_continue_art(item),
        meta=item_meta.get('meta'),
        video_id=video_id,
    )


def _handle_continue(window, item):
    """True when this click was a resumable Continue poster.

    Back does nothing else. The second row is the page this click used
    to open (episode list, or details for a movie).
    """
    offset_ms = _continue_offset_ms(item)
    if not offset_ms:
        return False
    choice = _continue_choice(item, offset_ms)
    if choice < 0:
        return True
    if choice == 0:
        _open_child(window, lambda: _resume_continue(item, offset_ms))
        return True
    land_on = item.getProperty('video_id') or None
    _open_child(window, lambda: _open_detail_for(item, land_on=land_on))
    return True


def _activate(window, item, control_id=None):
    """A shelf poster with an id opens its page. Anything else is a
    menu action.

    Continue is the exception, and only for a poster that is actually
    part-way through: that click asks first. Next-up, recently finished,
    Library and the catalog rows still open the page directly.
    """
    if item is None:
        return
    action = item.getProperty('action')
    if control_id == BROWSE_LIST:
        if action not in L_FOR_ACTION:
            return
        _switch_tab(window, action)
        return
    sid = item.getProperty('sid')
    if sid and not action:
        if control_id == CONTINUE_SHELF and _handle_continue(window, item):
            return
        land_on = None
        if control_id == CONTINUE_SHELF:
            # Next up and recently finished carry the episode too. The
            # page should open there, not on season 1.
            land_on = item.getProperty('video_id') or None
        _open_child(window, lambda: _open_detail_for(item, land_on=land_on))
        return
    handler = _ACTIONS.get(action)
    if handler:
        handler(window)


def _open_detail_for(item, land_on=None):
    """`open_detail` for a poster: the stock title page. `land_on` is the
    episode a Continue card is on; the stock page does not take one yet,
    so it opens where it always does."""
    from lib.ui.detailwindow import open_detail

    return open_detail(item.getProperty('stype'), item.getProperty('sid'))


def _open_search(window):
    from lib.ui.searchwindow import open_search
    _open_child(window, open_search)


def _open_addons(window):
    """Add-ons never closes Home. A newly installed catalog shows up on
    the way back (`_after_child` sees the addon list change)."""
    from lib.ui.addonswindow import open_addons

    def _run():
        open_addons()
        return False

    _open_child(window, _run)


def _open_settings(window):
    from lib.ui.compat import ADDON

    ADDON.openSettings()
    # Account > Login returns here without re-running onInit. A sign-in,
    # a row switched on or off, or a new exclusion changes the signature
    # and rebuilds the rows; a look at Settings that changed nothing
    # leaves them, and the remote's place, alone.
    try:
        _after_child(window)
    except Exception:
        pass


_ACTIONS = {
    'signin': _open_signin,
    'search': _open_search,
    'addons': _open_addons,
    'settings': _open_settings,
}
