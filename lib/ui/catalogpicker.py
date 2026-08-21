"""CatalogPickerWindow: a vertical list of every installed addon's
catalogs. Picking a row opens the coverflow (`lib.ui.infowindow`) over
that catalog's items; picking a TITLE from the coverflow opens
`lib.ui.detailwindow` for it.

Two things are pinned/sorted ahead of the plain addon-order list built
from `iter_catalogs()`:

- A search-only catalog (`_classify_catalog()` returns 'search') can
  only ever open a query prompt, never a browsable page, so
  `_sort_catalogs()` stably floats every one of those to the top -
  addon order (now user-controlled, addons are reorderable) is
  otherwise left untouched.
- The Series screen additionally pins a synthetic "New Episodes" row
  above even the search-only group, built from the same
  `_new_episode_items()` machinery `lib.ui.homewindow`'s "New
  Episodes" Home row used before it moved here - it is series-
  specific, so it belongs where shows live rather than as a permanent
  top-level row (see `open_catalog_picker()`).
"""
import datetime

import xbmcgui

from lib.stremio.addons import (
    AddonError,
    _base_type,
    addon_error_detail,
    catalog_extra_options,
    catalog_required_extra_names,
    safe_url_for_log,
)
from lib.ui.dependencies import get_store
from lib.ui.uicommon import BACK_ACTIONS, BaseWindow, busy_dialog, open_window

LIST = 30002
HEADING = 30006

#: ACTION_CONTEXT_MENU ("C"/menu button) - the secondary affordance for
#: browsing a catalog's declared genre/year options, mirroring
#: detailwindow's `_SEASON_NAV_ACTIONS`/infowindow's `_INFO_ACTION`.
_CONTEXT_MENU_ACTION = 117


def _classify_catalog(catalog):
    """How `catalog` must be opened, from its REQUIRED extras
    (`catalog_required_extra_names()`) minus `skip` (always satisfiable -
    it is paging, never a precondition to open):

    - 'search': requires `search` - prompt for a query on click.
    - 'genre': requires `genre` - the option select must open FIRST,
      with no unfiltered choice (a compliant client always supplies one -
      see Cinemeta's `movie/year`/`series/year`, display name "New").
    - 'open': no required extra the UI cannot supply - browses normally.
    - None: requires something else the UI cannot synthesise (e.g.
      Cinemeta's `lastVideosIds`/`calendarVideosIds`, or any unknown
      required name) - permanently unreachable, caller drops the row.

    `search` takes precedence over `genre` if a catalog somehow requires
    both."""
    required = catalog_required_extra_names(catalog) - {'skip'}
    if 'search' in required:
        return 'search'
    if 'genre' in required:
        return 'genre'
    if required:
        return None
    return 'open'


def _reachable_catalogs(catalogs):
    """Drop every catalog `_classify_catalog()` marks permanently
    unreachable (a required extra this UI cannot supply, e.g. Cinemeta's
    `lastVideosIds`/`calendarVideosIds`) or genre-required with no
    declared option values to choose from - logging each drop once at
    INFO so a missing row is diagnosable, not mistaken for a missing
    add-on."""
    import xbmc

    from lib.ui.compat import log

    kept = []
    for transport_url, manifest, catalog in catalogs:
        kind = _classify_catalog(catalog)
        if kind == 'genre' and not catalog_extra_options(catalog, 'genre'):
            kind = None
        if kind is None:
            name = catalog.get('name') or catalog.get('id')
            log(
                'catalogpicker: skipping %s/%s (%s) - requires extras this UI cannot supply' % (
                    safe_url_for_log(transport_url), catalog.get('type'), name,
                ),
                xbmc.LOGINFO,
            )
            continue
        kept.append((transport_url, manifest, catalog))
    return kept


def _sort_catalogs(catalogs):
    """Stably float every search-only catalog (`_classify_catalog()`
    returns 'search') ahead of the rest: a search-only catalog can only
    ever open a query prompt, never a browsable page, so leaving it at
    its `iter_catalogs()` position (i.e. addon install order) strands it
    mid-list where clicking it is a dead end unless the user already
    guessed what it does. `sorted()` is stable, so every other ordering
    stays untouched - both within the search-only group and within
    everything else - which matters now that addon order is user-
    controlled (addons are reorderable): this must reshuffle nothing
    beyond "search-only first"."""
    return sorted(catalogs, key=lambda entry: _classify_catalog(entry[2]) != 'search')


class CatalogPickerWindow(BaseWindow):
    """See module docstring. Built/run via `open_catalog_picker()`."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.catalogs = []
        self.heading = ''
        self.should_close_caller = False
        self.new_episode_items = []

    def start(self, catalogs, heading='', new_episode_items=None):
        """doModal() with `catalogs` (a list of `(transport_url, manifest,
        catalog)` tuples, as `lib.stremio.addons.iter_catalogs` yields)
        loaded as the picker's rows, under `heading` (the Home row the
        user came in through - "MOVIES", "SERIES", ...). `new_episode_items`
        pins a synthetic "New Episodes" row above every catalog row (see
        `open_catalog_picker()`) - empty/omitted on every screen but the
        Series one. Returns True if playback started somewhere down the
        chain and the caller should also close."""
        self.catalogs = list(catalogs or [])
        self.heading = heading or ''
        self.new_episode_items = list(new_episode_items or [])
        self.should_close_caller = False
        if not self.catalogs:
            return False
        self.doModal()
        return self.should_close_caller

    def _make_item(self, index, manifest, catalog):
        from lib.ui.compat import L

        addon_name = manifest.get('name', '?')
        catalog_name = catalog.get('name') or catalog.get('id')
        catalog_type = catalog.get('type')
        # The type is already named by the heading on a filtered screen,
        # so repeating it per row just crowds out the addon name.
        label2 = addon_name if self.heading else '%s \u00b7 %s' % (addon_name, catalog_type)
        if _classify_catalog(catalog) == 'search':
            label2 = '%s \u00b7 %s' % (label2, L(30199))
        item = xbmcgui.ListItem(label=catalog_name, label2=label2)
        item.setProperty('position', str(index))
        return item

    def _make_new_episodes_item(self, count):
        """The pinned "New Episodes" row (see `open_catalog_picker()`):
        same label as `lib.ui.homewindow`'s old Home row (30313), with a
        count-neutral subtitle (30360, "New episodes: %d") rather than
        the singular/plural pair that row used - these `.po` files have
        no plural-form machinery, and a genuine two-id plural pick reads
        wrong once translated into a language with more than two plural
        forms (Polish, Russian, Arabic, Turkish among them)."""
        from lib.ui.compat import L
        item = xbmcgui.ListItem(label=L(30313), label2=L(30360) % count)
        item.setProperty('kind', 'new_episodes')
        return item

    def onInit(self):
        items = []
        if self.new_episode_items:
            items.append(self._make_new_episodes_item(len(self.new_episode_items)))
        items.extend(
            self._make_item(index, manifest, catalog)
            for index, (_transport_url, manifest, catalog) in enumerate(self.catalogs)
        )
        control = self.getControl(LIST)
        # reset() before addItems(): onInit() runs again when
        # uicommon.ModalStackWindow reopens a screen force-closed for
        # playback, and re-adding onto a retained list would double every
        # item.
        control.reset()
        control.addItems(items)
        self.getControl(HEADING).setLabel(self._heading_text())
        self.setFocusId(LIST)

    def _heading_text(self):
        """The header line, in the skin's "RIVULET / <SECTION>" form.
        Falls back to the generic catalog-picker title when the screen is
        not filtered to a type, which is what the skin used to hardcode."""
        from lib.ui.compat import L

        return 'RIVULET / %s' % ((self.heading or L(30000)).upper(),)

    def _focused_catalog(self):
        """The `(transport_url, manifest, catalog)` tuple under the LIST's
        current selection, or None if nothing is focused OR the
        selection is the synthetic "New Episodes" row
        (`_make_new_episodes_item()`) - that row carries no `position`
        property because it indexes nothing in `self.catalogs`, so
        `int(focused.getProperty('position'))` on it raises ValueError
        on an empty string. Rejecting it here, rather than in each
        caller, is what makes this the bounds-safe lookup onClick()'s
        catalog-row fallback and the context-menu's genre lookup can
        both share without re-deriving the same guard."""
        focused = self.getControl(LIST).getSelectedItem()
        if focused is None or focused.getProperty('kind') == 'new_episodes':
            return None
        return self.catalogs[int(focused.getProperty('position'))]

    def onAction(self, action):
        action_id = action.getId()
        if action_id == _CONTEXT_MENU_ACTION and self.getFocusId() == LIST:
            self._open_genre_filter()
            return
        if action_id in BACK_ACTIONS:
            self.close()

    def onClick(self, control_id):
        if control_id != LIST:
            return
        selected = self.getControl(LIST).getSelectedItem()
        if selected is not None and selected.getProperty('kind') == 'new_episodes':
            self._open_new_episodes()
            return
        focused = self._focused_catalog()
        if focused is None:
            return
        transport_url, manifest, catalog = focused
        self._open_catalog(transport_url, manifest, catalog)

    def _open_genre_filter(self):
        """Context-menu (117) affordance: browse an optional-genre
        catalog (e.g. Popular) filtered, or unfiltered via the "All"
        entry. A catalog with no declared genre options has nothing to
        filter by."""
        from lib.ui import dialogs
        from lib.ui.compat import L, notify

        focused = self._focused_catalog()
        if focused is None:
            return
        transport_url, manifest, catalog = focused
        options = catalog_extra_options(catalog, 'genre')
        if not options:
            notify(L(30030))
            return
        choice = dialogs.choose(L(30194), [L(30198)] + options)
        if choice < 0:
            return
        if choice == 0:
            self._fetch_and_show(transport_url, manifest, catalog)
            return
        self._fetch_and_show(transport_url, manifest, catalog, extra=[('genre', options[choice - 1])])

    def _open_catalog(self, transport_url, manifest, catalog):
        kind = _classify_catalog(catalog)
        if kind == 'search':
            from lib.ui.compat import L
            query = xbmcgui.Dialog().input(L(30001))
            if not query:
                return
            self._fetch_and_show(transport_url, manifest, catalog, extra=[('search', query)])
            return
        if kind == 'genre':
            # Required genre (e.g. Cinemeta's "New"/year catalog): no
            # unfiltered choice - the select opens immediately, with no
            # "All" entry, and a cancel just does nothing.
            from lib.ui import dialogs
            from lib.ui.compat import L
            options = catalog_extra_options(catalog, 'genre')
            choice = dialogs.choose(L(30194), options)
            if choice < 0:
                return
            self._fetch_and_show(transport_url, manifest, catalog, extra=[('genre', options[choice])])
            return
        self._fetch_and_show(transport_url, manifest, catalog)

    def _fetch_and_show(self, transport_url, manifest, catalog, extra=None):
        import xbmc

        from lib.ui.compat import L, log, notify
        from lib.ui.views import iter_catalog_pages

        ctype = catalog.get('type')
        catalog_name = catalog.get('name') or catalog.get('id')
        addon_name = manifest.get('name')
        # "ADDON · CATALOG" when both are on hand, matching the design's
        # breadcrumb (e.g. "CINEMETA · POPULAR MOVIES") - otherwise just
        # the catalog name, which is always present.
        catalog_title = '%s \u00b7 %s' % (addon_name, catalog_name) if addon_name else catalog_name

        # A catalog that declares `skip` is walked page by page rather
        # than one page deep, so a list longer than the addon's page size
        # is reachable in full. Only the FIRST page is waited on here -
        # the rest are handed to the coverflow and appended as they land
        # (see ShowcaseWindow.start), because the pages are serial and an
        # addon serving 20 at a time would otherwise hold a 400-title
        # list behind a spinner for a minute before showing anything.
        pages = iter_catalog_pages(
            transport_url, ctype, catalog.get('id'), extra=extra, catalog=catalog, manifest=manifest,
        )
        try:
            with busy_dialog(L(30033)):
                metas = next(pages, [])
        except AddonError as exc:
            log('catalogpicker: %s failed: %s' % (safe_url_for_log(transport_url), addon_error_detail(exc)), xbmc.LOGERROR)
            notify(L(30032))
            return
        if not metas:
            log('catalogpicker: %s (%s) returned no results' % (catalog_name, ctype), xbmc.LOGINFO)
            notify(L(30030))
            return

        log('catalogpicker: opening coverflow (%d results, paging in background)' % len(metas), xbmc.LOGINFO)
        try:
            from lib.ui.infowindow import open_showcase
            selected = open_showcase(metas, catalog_title=catalog_title, more_pages=pages)
        except Exception as exc:  # a skin/UI failure must surface, not vanish
            log('catalogpicker: coverflow failed to open: %r' % (exc,), xbmc.LOGERROR)
            notify(L(30032))
            return
        if not selected:
            return

        from lib.ui.detailwindow import open_detail
        if open_detail(selected.get('type') or ctype, selected.get('id')):
            self.should_close_caller = True
            self.close()

    def _open_new_episodes(self):
        """Open the borrowed-band grid (`gridwindow.NEW_EPISODES_BAND`)
        over the items `open_catalog_picker()` already computed and
        stashed on this window via `start()`. Marks the picked episode
        seen BEFORE opening its detail screen - "acted on", not
        "rendered", is the seen-set's own rule (see
        `lib.newepisodes.mark_seen`'s docstring) - then hands off to
        `lib.ui.detailwindow.open_detail()`, the same path every other
        row's selection chain already ends at. Moved here from
        `lib.ui.homewindow` along with the row itself: New Episodes is
        series-specific, so it now lives pinned atop the Series picker
        instead of as a permanent Home row."""
        from lib.ui.compat import L, log, notify

        items = list(self.new_episode_items or [])
        if not items:
            return
        try:
            from lib.ui.gridwindow import NEW_EPISODES_BAND, open_grid
            selected = open_grid(
                [(NEW_EPISODES_BAND, items)], heading=L(30313), labels={NEW_EPISODES_BAND: 30317},
            )
        except Exception as exc:  # a skin/UI failure must surface, not vanish
            import xbmc
            log('catalogpicker: new-episodes grid failed to open: %r' % (exc,), xbmc.LOGERROR)
            notify(L(30032))
            return
        if not selected:
            return
        _mark_episode_seen(selected)
        from lib.ui.detailwindow import open_detail
        if open_detail(selected.get('type'), selected.get('id')):
            self.should_close_caller = True
            self.close()


#: Hard cap on how many followed series `_new_episode_items()` will fetch
#: a meta for on a single picker render - see `_followed_series()`'s
#: docstring for the render-blocking risk this guards against. Moved
#: here from `lib.ui.homewindow` along with the New Episodes row itself.
MAX_NEW_EPISODE_SERIES = 24


def _followed_series(store):
    """Series the New Episodes row treats as "followed": every series
    with at least one local playback-progress entry, reduced to the one
    entry `lib.ui.mystuff.latest_by_title()` keeps per series (most
    recently updated) - the same signal `lib.ui.mystuff`'s own NEXT_UP
    band already treats as "the user cares about this show". That
    reduction conveniently doubles as the last-watched-episode pointer
    `lib.newepisodes.new_episodes()` needs.

    Deliberately local-progress-only, not the Stremio account library: a
    library entry carries no last-watched pointer of its own (nothing to
    compare a candidate episode against), and this way the row works
    fully offline and logged out too, like every other locally-sourced
    signal this addon uses.

    Capped at `MAX_NEW_EPISODE_SERIES`: `latest_by_title()` already
    returns most-recently-updated first, so the slice below keeps
    exactly the shows a viewer is most likely still watching.
    """
    from lib.ui.mystuff import latest_by_title

    series = [
        {'type': entry['type'], 'id': entry['id'], 'video_id': entry.get('video_id')}
        for entry in latest_by_title(store.get_progress_entries())
        if entry.get('type') == 'series'
    ]
    return series[:MAX_NEW_EPISODE_SERIES]


def _fetch_series_metas(series_items):
    """One full meta (with `videos`) per followed series, fetched the
    same bounded-fan-out way `lib.ui.mystuff._enrich()` fetches next-up
    metas: `views._fetch_meta()` sits behind `lib.ui.metacache`'s
    short-TTL disk cache, so a render that already computed this
    recently costs a handful of disk reads, not a fresh addon
    round-trip per followed series."""
    from lib.ui import views

    if not series_items:
        return {}
    fetched = views._map_addons(
        lambda item: views._fetch_meta(item['type'], item['id']), series_items,
    )
    return {item['id']: meta for item, meta in zip(series_items, fetched) if meta}


def _new_episode_items(store):
    """New-episode candidates across every followed series (see
    `_followed_series()`), computed fresh on this picker render - the
    same render-path-not-a-poll-loop design `lib.ui.mystuff`'s "My
    Stuff" row uses. Any failure - a store I/O error, or anything an
    addon fan-out raised that `views._fetch_meta()`'s own per-addon
    guard did not already catch - degrades to an empty list rather than
    taking down the picker's render.
    """
    import xbmc

    from lib.newepisodes import new_episodes
    from lib.ui.compat import log

    try:
        series_items = _followed_series(store)
        if not series_items:
            return []
        metas = _fetch_series_metas(series_items)
        if not metas:
            return []
        seen = store.get_seen_episodes()
        return new_episodes(series_items, metas, seen, datetime.datetime.utcnow())
    except Exception as exc:
        log('catalogpicker: new-episodes computation failed: %r' % (exc,), xbmc.LOGWARNING)
        return []


def _mark_episode_seen(episode):
    """Persist that `episode` (one of `_new_episode_items()`'s dicts) has
    been acted on, so it never reappears in the row - see
    `lib.newepisodes.mark_seen`'s "acted on, not rendered" rule.

    Best-effort: a store I/O failure here must not stop the user reaching
    the detail screen they just picked; it only means the same episode
    might resurface once more."""
    import xbmc

    from lib.newepisodes import mark_seen
    from lib.ui.compat import log

    store = get_store()
    try:
        store.set_seen_episodes(mark_seen(store.get_seen_episodes(), [episode]))
    except Exception as exc:
        log('catalogpicker: failed to mark episode seen: %r' % (exc,), xbmc.LOGWARNING)


#: The exact `types=` set `lib.ui.homewindow`'s Series row passes
#: (`_TYPE_ROWS`'s `frozenset({'series', 'tv'})` - `tv` is the stray
#: declared type that row's own comment folds into Series). Pinning New
#: Episodes checks the reduced `wanted` set against this by equality,
#: not mere membership: `'series' in wanted` is also true for a
#: hypothetical mixed filter like `{'series', 'movie'}`, which is not
#: the Series screen. Today `home` is the only caller and always passes
#: exactly this set, so this equality check is hardening against a
#: hypothetical future picker rather than a fix for an observed bug.
_SERIES_SCREEN_TYPES = frozenset({'series', 'tv'})


def open_catalog_picker(types=None, heading=''):
    """List the installed addons' catalogs and open the coverflow for the
    one picked. `types` restricts the list to those Stremio content types
    (see `lib.ui.homewindow`'s type rows); `heading` names the screen for
    the row the user came in through. Catalogs sort search-only first
    (`_sort_catalogs()`), and when `types` names the Series row
    specifically, a pinned "New Episodes" row (see
    `CatalogPickerWindow._open_new_episodes()`) is added ahead of even
    that group - series-specific, so it belongs here rather than as a
    permanent Home row. Returns True if the caller should also close
    (see the module docstring)."""
    import xbmc

    from lib.stremio.addons import iter_catalogs
    from lib.ui.compat import L, log, notify, setting_bool

    store = get_store()
    catalogs = list(iter_catalogs(store.get_enabled_addons()))
    wanted = None
    if types is not None:
        wanted = {_base_type(t) for t in types}
        catalogs = [c for c in catalogs if _base_type(c[2].get('type')) in wanted]
    catalogs = _reachable_catalogs(catalogs)
    if not catalogs:
        notify(L(30030))
        return False
    catalogs = _sort_catalogs(catalogs)

    # Pinned only on the Series screen specifically - `wanted` must
    # equal `_SERIES_SCREEN_TYPES` exactly, not merely contain 'series',
    # so a picker filtered to a superset (e.g. a hypothetical
    # {'series', 'movie'}) does not also pick this up (never on
    # Movies/Anime/Other either). New Episodes is series-specific, so it
    # only ever belongs where shows live. Gated on the same setting the
    # old Home row used - it stays off entirely rather than paying for
    # the addon-fetching computation below when the user has disabled
    # it.
    new_episode_items = []
    if wanted == _SERIES_SCREEN_TYPES and setting_bool('home_show_new_episodes', True):
        new_episode_items = _new_episode_items(store)

    log('catalogpicker: opening CatalogPickerWindow (%d catalogs)' % len(catalogs), xbmc.LOGINFO)
    win = None
    try:
        win = open_window(CatalogPickerWindow, 'CatalogPickerWindow.xml')
        return win.start(catalogs, heading, new_episode_items=new_episode_items)
    except Exception as exc:  # a skin/UI failure must surface, not vanish
        log('catalogpicker: window failed to open: %r' % (exc,), xbmc.LOGERROR)
        notify(L(30032))
        return False
    finally:
        # A normal return means CatalogPickerWindow already closed itself
        # (its own onAction/onClick calls self.close()) before .start()
        # returned - but an exception raised from WITHIN .start() (onInit(),
        # or a callback mid-doModal()) skips that self-close entirely.
        # Close unconditionally here so no exit path leaves a zombie modal
        # window behind; closing an already-closed window is a safe no-op.
        if win is not None:
            try:
                win.close()
            except Exception:
                pass
