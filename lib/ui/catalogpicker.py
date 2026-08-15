"""CatalogPickerWindow: a vertical list of every installed addon's
catalogs. Picking a row opens the coverflow (`lib.ui.infowindow`) over
that catalog's items; picking a TITLE from the coverflow opens
`lib.ui.detailwindow` for it.
"""
import xbmcgui

from lib.stremio.addons import (
    AddonError,
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


def _base_type(catalog_type):
    """Reduce a Stremio catalog type to the key `lib.ui.homewindow`'s type
    rows match on: everything before the first '.', lowercased. Addons
    follow the convention of a dotted subtype (e.g. `anime.movie`/
    `anime.series`) to specialize a base type, and type strings are
    otherwise free-form and mixed-case - this is what lets `anime.movie`
    join the Anime row and a stray `TV` join Series alongside `tv`."""
    return (catalog_type or '').split('.', 1)[0].lower()


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


class CatalogPickerWindow(BaseWindow):
    """See module docstring. Built/run via `open_catalog_picker()`."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.catalogs = []
        self.heading = ''
        self.should_close_caller = False

    def start(self, catalogs, heading=''):
        """doModal() with `catalogs` (a list of `(transport_url, manifest,
        catalog)` tuples, as `lib.stremio.addons.iter_catalogs` yields)
        loaded as the picker's rows, under `heading` (the Home row the
        user came in through - "MOVIES", "SERIES", ...). Returns True if
        playback started somewhere down the chain and the caller should
        also close."""
        self.catalogs = list(catalogs or [])
        self.heading = heading or ''
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

    def onInit(self):
        items = [
            self._make_item(index, manifest, catalog)
            for index, (_transport_url, manifest, catalog) in enumerate(self.catalogs)
        ]
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
        current selection, or None if nothing is focused - the
        bounds-safe lookup `onClick()`/the context-menu handling share."""
        focused = self.getControl(LIST).getSelectedItem()
        if focused is None:
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
            transport_url, ctype, catalog.get('id'), extra=extra, catalog=catalog,
        )
        try:
            with busy_dialog(L(30033)):
                metas = next(pages, [])
        except AddonError as exc:
            log('catalogpicker: %s failed: %s' % (safe_url_for_log(transport_url), type(exc).__name__), xbmc.LOGERROR)
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


def open_catalog_picker(types=None, heading=''):
    """List the installed addons' catalogs and open the coverflow for the
    one picked. `types` restricts the list to those Stremio content types
    (see `lib.ui.homewindow`'s type rows); `heading` names the screen for
    the row the user came in through. Returns True if the caller should
    also close (see the module docstring)."""
    import xbmc

    from lib.stremio.addons import iter_catalogs
    from lib.ui.compat import L, log, notify

    catalogs = list(iter_catalogs(get_store().get_addons()))
    if types is not None:
        wanted = {_base_type(t) for t in types}
        catalogs = [c for c in catalogs if _base_type(c[2].get('type')) in wanted]
    catalogs = _reachable_catalogs(catalogs)
    if not catalogs:
        notify(L(30030))
        return False

    log('catalogpicker: opening CatalogPickerWindow (%d catalogs)' % len(catalogs), xbmc.LOGINFO)
    win = None
    try:
        win = open_window(CatalogPickerWindow, 'CatalogPickerWindow.xml')
        return win.start(catalogs, heading)
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
