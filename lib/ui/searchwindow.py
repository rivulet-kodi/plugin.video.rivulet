"""SearchWindow: a persistent search-history/new-query picker - Rivulet's
custom replacement for the classical `views.search()` directory. Unlike
the old bare `open_search()` function (which opened the coverflow
directly with no window underneath it, so Back from the results fell all
the way to Home), this window stays open under the coverflow the same
way `lib.ui.catalogpicker.CatalogPickerWindow` does for Discover - Back
from the results now correctly returns here.

Row 0 is always "New search…" (prompts a query, mirrors the old
behavior); every history row re-runs that past query (the closest thing
to autocompletion `xbmcgui.Dialog().input()` allows - see the module's
own history rows as the suggestion surface); a trailing "Clear search
history" row appears once there's history to clear. Picking a result
title opens `lib.ui.detailwindow` for it. Built/run via `open_search()`.
"""
import xbmc
import xbmcgui

from lib.ui.uicommon import BaseWindow, busy_dialog, open_window

LIST = 30002


class SearchWindow(BaseWindow):
    """See module docstring. Built/run via `open_search()`."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.store = None
        self.history = []
        self.should_close_caller = False

    def start(self):
        """doModal() and return True if the caller should also close (a
        classical-fallback navigation happened, e.g. after a movie/series
        playback round trip)."""
        self.should_close_caller = False
        self.doModal()
        return self.should_close_caller

    def onInit(self):
        self._reload()

    def _reload(self):
        from lib.store import Store
        from lib.ui.compat import addon_profile_dir

        self.store = self.store or Store(addon_profile_dir())
        self.history = self.store.get_search_history()

        control = self.getControl(LIST)
        control.reset()
        control.addItems(self._build_items(self.history))
        self.setFocusId(LIST)

    def _build_items(self, history):
        from lib.ui.compat import L

        new_item = xbmcgui.ListItem(label=L(30042), label2=L(30043))
        new_item.setProperty('position', 'new')
        items = [new_item]
        for index, query in enumerate(history):
            item = xbmcgui.ListItem(label=query, label2=L(30045))
            item.setProperty('position', str(index))
            items.append(item)
        if history:
            clear_item = xbmcgui.ListItem(label=L(30044))
            clear_item.setProperty('position', 'clear')
            items.append(clear_item)
        return items

    def onClick(self, control_id):
        if control_id != LIST:
            return
        focused = self.getControl(LIST).getSelectedItem()
        if focused is None:
            return
        position = focused.getProperty('position')
        if position == 'new':
            self._new_search()
            return
        if position == 'clear':
            self._clear_history()
            return
        self._run_search(self.history[int(position)])

    def _new_search(self):
        from lib.ui.compat import L

        query = xbmcgui.Dialog().input(L(30001))
        if not query:
            return
        self._run_search(query)

    def _clear_history(self):
        from lib.ui.compat import L

        if not xbmcgui.Dialog().yesno(L(30044), L(30046)):
            return
        self.store.clear_search_history()
        self._reload()

    def _run_search(self, query):
        from lib.stremio.addons import AddonClient, AddonError, iter_catalogs
        from lib.ui.compat import L, log, notify

        self.store.add_search_query(query)

        client = AddonClient()
        metas = []
        catalogs = list(iter_catalogs(self.store.get_addons(), extra_required='search'))
        total_catalogs = len(catalogs)
        with busy_dialog(L(30033), query) as dialog:
            for index, (transport_url, manifest, cat) in enumerate(catalogs):
                if dialog.iscanceled():
                    break
                percent = int(index * 100 / total_catalogs) if total_catalogs else 0
                dialog.update(percent, 'Searching %s...' % (manifest.get('name') or '?'))
                try:
                    results = client.catalog(transport_url, cat.get('type'), cat.get('id'), extra=[('search', query)])
                except AddonError as exc:
                    log('searchwindow: %s failed: %r' % (transport_url, exc), xbmc.LOGERROR)
                    continue
                for meta_obj in results or []:
                    meta_obj['type'] = meta_obj.get('type') or cat.get('type')
                    metas.append(meta_obj)

        self._reload()

        if not metas:
            notify(L(30030))
            return

        log('searchwindow: opening coverflow (%d results)' % len(metas), xbmc.LOGINFO)
        try:
            from lib.ui.infowindow import open_showcase
            selected = open_showcase(metas)
        except Exception as exc:  # a skin/UI failure must surface, not vanish
            log('searchwindow: coverflow failed to open: %r' % (exc,), xbmc.LOGERROR)
            notify(L(30032))
            return
        if not selected:
            return

        from lib.ui.detailwindow import open_detail
        if open_detail(selected.get('type') or 'movie', selected.get('id')):
            self.should_close_caller = True
            self.close()


def open_search():
    """Open the search history/new-query picker. Returns True if the
    caller should also close (see `SearchWindow.start`)."""
    from lib.ui.compat import L, log, notify

    log('searchwindow: opening SearchWindow', xbmc.LOGINFO)
    win = None
    try:
        win = open_window(SearchWindow, 'SearchWindow.xml')
        return win.start()
    except Exception as exc:  # a skin/UI failure must surface, not vanish
        log('searchwindow: window failed to open: %r' % (exc,), xbmc.LOGERROR)
        notify(L(30032))
        return False
    finally:
        # A normal return means SearchWindow already closed itself (its
        # own onAction/onClick calls self.close()) before .start()
        # returned - but an exception raised from WITHIN .start() (onInit(),
        # or a callback mid-doModal()) skips that self-close entirely.
        # Close unconditionally here so no exit path leaves a zombie modal
        # window behind; closing an already-closed window is a safe no-op.
        if win is not None:
            try:
                win.close()
            except Exception:
                pass
