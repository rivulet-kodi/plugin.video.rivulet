# SPDX-FileCopyrightText: 2026 M0Rf30
# SPDX-License-Identifier: MIT
"""Search in the new look: a search bar, letters, and a row of results.

The classic search is a list (New search, phone, history) that opens the
keyboard and then a coverflow. This screen is one page, after the
reference the user sent: a white search bar, the alphabet under it, the
focused title's hero, and one row of posters. With nothing typed the row
is Discover (the first page of the first plain movie catalog); as
letters are picked it becomes the results.

Picking letters fills the bar; the Search pill (or OK from the phone)
runs the search. It runs the way the classic search does, on the screen's
own thread under the Rivulet spinner (`searchwindow.run_query`): Back on
the spinner cancels, and the results replace Discover with the focus on
them. rev18/rev19 searched on a worker as letters were picked and applied
the answer on a posted noop; on the TV that search never came back
("Searching for ..." forever), while the classic path works, so this
screen now uses that path. An empty answer, or one where every catalog
failed, is said on the line under the pills. OK on a poster opens the
title the way the coverflow does; the query then goes into the search
history.
"""

import threading

import xbmcgui

from lib.ui.uicommon import BaseWindow, escape_label, gated_input, open_window

QUERY = 30201
LETTERS = 30202
PHONE = 30203
#: The Search pill: runs what is in the bar now.
GO = 30204
#: One line under the pills: "Searching for ...", "No results for ...".
STATUS = 30205
ROW = 30215
ROW_HEADING = 30140
BACKGROUND = 30000
#: Letters, then space and delete. The skin draws " " as "_" and "<" as "<".
KEYS = tuple('ABCDEFGHIJKLMN') + tuple('OPQRSTUVWXYZ') + (' ', '<')
MAX_QUERY = 60
#: The hero here sits between the letters and the row: a shorter logo,
#: two lines of plot.
SEARCH_STACK = {'title_h': 100, 'plot_h': 82}
_BACK_ACTIONS = frozenset((9, 10, 92))


class NewSearchWindow(BaseWindow):
    """See module docstring. Built/run via `open_search()`."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.query = ''
        self.should_close_caller = False
        self._metas = []
        self._discover = []
        self._results_query = ''
        self._searching = False
        self._lock = threading.Lock()
        self._ready = []
        self._hero_painted = None

    def start(self):
        self.should_close_caller = False
        self.doModal()
        return self.should_close_caller

    # --- screen ------------------------------------------------------------

    def onInit(self):
        letters = self.getControl(LETTERS)
        letters.reset()
        items = []
        for key in KEYS:
            item = xbmcgui.ListItem('_' if key == ' ' else key)
            item.setProperty('key', key)
            items.append(item)
        letters.addItems(items)
        self._paint_query()
        if self._metas:
            self._show(self._metas, results=True)
        elif self._discover:
            self._show(self._discover, results=False)
        else:
            self._welcome()
            self._load_discover()
        self.setFocusId(LETTERS)

    def _paint_query(self):
        text = escape_label(self.query)
        self.getControl(QUERY).setLabel('%s[COLOR 8007090B]|[/COLOR]' % text)

    def _welcome(self):
        from lib.ui import newhome, newlook
        from lib.ui.compat import L

        self.setProperty('HeroLogo', '')
        self.setProperty('HeroFill', '')
        self.setProperty('SearchHasRow', '')
        call = newlook._hero_call
        call(self, newhome.HERO_LOGO, 'setImage', '')
        call(self, newhome.HERO_TITLE, 'setLabel', '[B]%s[/B]' % L(30508))
        call(self, newhome.HERO_META, 'setText', '')
        call(self, newhome.HERO_PLOT, 'setText', L(30509))
        call(self, BACKGROUND, 'setImage', newlook.fanart())
        newhome._set_fill_image(self, '')
        newlook.paint_hero(self, L(30509), '', '', SEARCH_STACK)
        self._hero_painted = None

    def _show(self, metas, results):
        from lib.ui.compat import L
        from lib.ui.newhome import _poster_item

        row = self.getControl(ROW)
        row.reset()
        items = []
        for index, meta in enumerate(metas):
            item = _poster_item(meta)
            item.setProperty('position', str(index))
            items.append(item)
        row.addItems(items)
        self.getControl(ROW_HEADING).setLabel('[B]%s[/B]' % L(30507 if results else 30506))
        self.setProperty('SearchHasRow', '1' if items else '')
        if items:
            _paint_hero(self, items[0])
        else:
            self._welcome()

    # --- input ---------------------------------------------------------------

    @gated_input
    def onAction(self, action):
        action_id = action.getId()
        if action_id in _BACK_ACTIONS:
            self.close()
            return
        if action_id == 117:
            if self.getFocusId() == ROW:
                from lib.ui.libraryaction import meta_from_item, poster_menu

                meta = meta_from_item(self.getControl(ROW).getSelectedItem())
                if meta is not None:
                    poster_menu(meta)
            return
        if action_id == 4 and self.getFocusId() == LETTERS and not self.getProperty('SearchHasRow'):
            # Kodi must not walk toward a hidden results row.
            return
        self._apply_ready()
        if self.getFocusId() == ROW:
            _paint_hero(self, self.getControl(ROW).getSelectedItem())

    @gated_input
    def onClick(self, control_id):
        if self._searching:
            # `run_cancellable` delivers queued clicks while its spinner is
            # up. Acting on them would edit or start a search under the one
            # still running, whose results would then be painted for a
            # query the bar no longer shows.
            return
        if control_id == LETTERS:
            item = self.getControl(LETTERS).getSelectedItem()
            if item is not None:
                self._key(item.getProperty('key'))
            return
        if control_id == PHONE:
            self._phone()
            return
        if control_id == GO:
            self._go()
            return
        if control_id == ROW:
            self._open_focused()

    def _key(self, key):
        if key == '<':
            self.query = self.query[:-1]
        elif key == ' ':
            if self.query and not self.query.endswith(' ') and len(self.query) < MAX_QUERY:
                self.query += ' '
        elif key and len(self.query) < MAX_QUERY:
            self.query += key.lower() if self.query and not self.query.endswith(' ') else key
        self._paint_query()
        if not self.query.strip():
            # An empty bar is Discover again.
            self._metas = []
            self._status('')
            self._show(self._discover, results=False)

    def _go(self):
        """The Search pill: search what is in the bar, under the spinner,
        then move to the results."""
        query = self.query.strip()
        if not query:
            return
        from lib.ui.compat import L
        from lib.ui.dependencies import get_client, get_store
        from lib.ui.searchwindow import SearchReport, _search_notice, run_query

        report = SearchReport()
        self._searching = True
        try:
            metas = run_query(get_store(), get_client(), query, report=report)
        except Exception as exc:
            import xbmc

            from lib.ui.compat import log

            log('newsearch: search failed: %s' % type(exc).__name__, xbmc.LOGWARNING)
            metas = []
        finally:
            self._searching = False
        if report.canceled:
            return
        self._metas = list(metas or [])
        self._results_query = query
        if self._metas:
            self._show(self._metas, results=True)
            self.setFocusId(ROW)
            notice = _search_notice(report)
            self._status(notice or '')
            return
        self._status(_search_notice(report) or _with_query(L(30513), query))

    def _status(self, text):
        try:
            self.getControl(STATUS).setLabel(text)
        except Exception:
            pass

    def _phone(self):
        from lib.ui.pairwindow import phone_search

        query = (phone_search() or '').strip()
        if not query:
            return
        self.query = query[:MAX_QUERY]
        self._paint_query()
        self._go()

    # --- searching -----------------------------------------------------------

    def _load_discover(self):
        def _work():
            metas = discover_metas()
            self._post(('discover', '', metas))

        threading.Thread(target=_work, name='rivulet-search-discover', daemon=True).start()

    def _post(self, answer):
        with self._lock:
            self._ready.append(answer)
        try:
            import xbmc

            xbmc.executebuiltin('Action(noop)')
        except Exception:
            pass

    def _apply_ready(self):
        """Discover's first page, from its worker, on the UI thread."""
        with self._lock:
            ready, self._ready = self._ready, []
        for kind, _query, metas in ready:
            if kind == 'discover':
                self._discover = list(metas or [])
                if not self._metas and self._discover:
                    self._show(self._discover, results=False)

    def _open_focused(self):
        item = self.getControl(ROW).getSelectedItem()
        if item is None:
            return
        try:
            index = int(item.getProperty('position'))
        except ValueError:
            return
        metas = self._metas if self._metas else self._discover
        if index < 0 or index >= len(metas):
            return
        meta = metas[index]
        if self._metas and self._results_query:
            from lib.ui.dependencies import get_store

            try:
                get_store().add_search_query(self._results_query)
            except Exception:
                pass
        from lib.ui.detailwindow import open_detail

        if open_detail(meta.get('type') or 'movie', meta.get('id')):
            self.should_close_caller = True
            self.close()


def _paint_hero(window, item):
    """Home's poster properties and cached ratings in the search-sized stack.

    The Home painter also schedules library metadata and addresses Home
    controls. Keep this window's painting local while reusing its existing
    logo, fill and upcoming-poster helpers and the shared hero renderer.
    """
    if item is None:
        return
    from lib.ui import newhome, newlook

    title = item.getProperty('hero_title') or item.getLabel() or ''
    if not title and not item.getProperty('sid') and not item.getProperty('hero_logo'):
        return
    meta = item.getProperty('hero_meta') or ''
    facts_raw = item.getProperty('hero_facts') or ''
    if facts_raw and item.getProperty('facts_done') != '1':
        meta = newhome._overlay_hero_facts(window, item, meta, facts_raw)
    plot = item.getProperty('hero_plot') or item.getProperty('subtitle') or ''
    fanart = item.getProperty('fanart') or ''
    logo = newhome._hero_logo(window, item)
    fill = item.getProperty('hero_fill') == '1' and bool(fanart)
    runtime = item.getProperty('hero_runtime') or ''
    painted = (title, meta, plot, fanart, fill, logo, runtime)
    if painted == getattr(window, '_hero_painted', None):
        return
    window._hero_painted = painted
    window.setProperty('HeroLogo', logo)
    window.setProperty('HeroFill', '1' if fill else '')
    call = newlook._hero_call
    call(window, newhome.HERO_LOGO, 'setImage', logo)
    call(window, newhome.HERO_TITLE, 'setLabel', newhome._fit_hero_title(title))
    call(window, newhome.HERO_META, 'setText', meta)
    call(window, newhome.HERO_PLOT, 'setText', plot)
    call(window, BACKGROUND, 'setImage', fanart or newlook.fanart())
    newhome._set_fill_image(window, fanart if fill else '')
    newlook.paint_hero(window, plot, meta, runtime, SEARCH_STACK)


def _with_query(text, query):
    """`text` (a string with one ``%s``) around the query."""
    query = escape_label(query)
    return text % query if text.count('%s') == 1 else '%s %s' % (text, query)


def discover_metas():
    """The first page of the first plain movie catalog (else series)."""
    from lib.stremio.addons import _base_type, iter_catalogs
    from lib.ui.catalogpicker import _classify_catalog, _reachable_catalogs
    from lib.ui.dependencies import get_store
    from lib.ui.newhome import CatalogUnavailable, _one_catalog_page

    try:
        addons = get_store().get_enabled_addons() or []
        catalogs = _reachable_catalogs(list(iter_catalogs(addons)))
    except Exception:
        return []
    for wanted in ('movie', 'series'):
        for transport, manifest, catalog in catalogs:
            if _base_type(catalog.get('type')) != wanted or _classify_catalog(catalog) != 'open':
                continue
            try:
                metas = _one_catalog_page(transport, manifest, catalog)
            except CatalogUnavailable:
                continue
            except Exception:
                continue
            metas = [meta for meta in (metas or []) if isinstance(meta, dict)]
            if metas:
                return metas[:40]
    return []


def open_search():
    """Open the new-look search. True when playback started (close the caller)."""
    import xbmc

    from lib.ui.compat import L, log, notify

    log('newsearch: opening', xbmc.LOGINFO)
    window = None
    try:
        window = open_window(NewSearchWindow, 'NewSearchWindow.xml')
        return window.start()
    except Exception as exc:  # a skin failure must surface, not vanish
        from lib.stremio.addons import addon_error_detail

        log('newsearch: window failed: %s' % addon_error_detail(exc), xbmc.LOGERROR)
        notify(L(30032))
        return False
    finally:
        if window is not None:
            try:
                window.close()
            except Exception:
                pass
