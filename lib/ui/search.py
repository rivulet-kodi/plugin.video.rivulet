# SPDX-FileCopyrightText: 2026 M0Rf30
# SPDX-License-Identifier: MIT
"""Search: a search bar, letters, and a row of results.

The classic search is a list (New search, phone, history) that opens the
keyboard and then a coverflow. This screen is one page, after the
reference the user sent: a white search bar, the alphabet under it, the
focused title's hero, and one row of posters. With nothing typed the row
is Discover (the first page of the first plain movie catalog); as
letters are picked it becomes the results.

Picking letters fills the bar; the Search pill (or OK from the phone)
runs the search. The bar itself is an edit control and has the focus
when the screen opens: Kodi hands raw key presses only to a focused edit
control, and everywhere else runs them through the keymap, where most
letters are commands ("s" opens the shutdown menu, "x" stops, "c" opens
the context menu). With a label for a bar, a laptop keyboard could not
type at all. Each change in the bar reaches `onClick(QUERY)`.

Typing in the bar searches by itself: `_TYPE_PAUSE_SECONDS` after the last
key, if the focus is still in the bar, the search runs, still on the screen's own thread, with no spinner - its
progress goes to the status line and the focus stays in the bar, so the
user can keep typing; a key pressed mid-search cancels it and the pause
starts again. Enter is not needed, which matters because Kodi's keymap
turns Enter into Select on an edit control, and Select always opens Kodi's
own keyboard dialog (`CGUIEditControl` -> `CGUIButtonControl::OnAction`
-> `OnClick`) before an add-on sees the action: the add-on cannot stop
that dialog, only make it unnecessary. OK on the bar with a remote still
opens it, which is how a remote types; letters picked from the alphabet
wait for the pill, as a remote user picks them slower than the pause; confirming it, the Search pill or
OK from the phone run the search the classic way, under the Rivulet
spinner (`searchwindow.run_query`), with the focus moved to the results.

Only the pause timer runs on a worker; the search itself never does.
rev18/rev19 searched on a worker as letters were picked and applied the
answer on a posted noop; on the TV that search never came back
("Searching for ..." forever). The timer posts the same noop, so on a
device where it is lost typing simply does not search by itself and Enter
or the pill still do. An empty answer, or one where every catalog
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
SEARCH_STACK = {'title_h': 100, 'plot_h': 81}
_BACK_ACTIONS = frozenset((9, 10, 92))
#: ACTION_SELECT_ITEM and ACTION_MOUSE_LEFT_CLICK: on the bar, Kodi opens
#: its keyboard for these before Python sees them.
_BAR_OPEN_ACTIONS = frozenset((7, 100))
#: Quiet time after the last typed key before the bar searches by itself:
#: long enough not to search every half-word at normal typing speed, short
#: enough to feel live.
_TYPE_PAUSE_SECONDS = 0.7

#: Shortest bar text a pause searches. One letter matches nearly every
#: title in every catalog: a full fan-out (and its posters) for a list
#: nobody wants. The pill and Enter still search any length.
_TYPE_MIN_CHARS = 2


class SearchWindow(BaseWindow):
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
        #: now/next worker for a focused live channel (`lib.ui.livenow`),
        #: started on the first live card focused and stopped on `close()`.
        self._now_feed = None
        #: monotonic time of the last edit typed into the bar, and the
        #: pending pause timer (`_schedule_typed_search()`).
        # When the pending pause started; None when no typed search is
        # pending (none typed yet, already run, or overtaken by the pill).
        self._typed_at = None
        self._closing = False
        self._type_timer = None
        #: A search started by typing is running (no spinner, bar focused).
        self._typing_search = False
        #: The bar reported a change since the last action. Kodi queues
        #: the bar's click before the action that caused it, so a Select
        #: on the bar that arrives with this set followed a confirmed
        #: keyboard dialog, and one without it a cancelled dialog.
        self._bar_changed = False

    def close(self):
        # A typed search has no spinner to carry Back: `_BarProgress` stops
        # it on this flag. A pause timer left running would post into a
        # closed window (and, under test, into restored modules).
        self._closing = True
        self._typed_at = None
        timer, self._type_timer = self._type_timer, None
        if timer is not None:
            timer.cancel()
        if self._now_feed is not None:
            from lib.ui import livenow

            livenow.stop_feed(self._now_feed)
        super().close()

    def start(self):
        self.should_close_caller = False
        self.doModal()
        return self.should_close_caller

    # --- screen ------------------------------------------------------------

    def onInit(self):
        from lib.ui.compat import L

        # Heads Kodi's keyboard, opened by OK/Enter on the bar; without it
        # Kodi titles it "Enter value".
        self.getControl(QUERY).setType(xbmcgui.INPUT_TYPE_TEXT, L(30001))
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
        self.setFocusId(QUERY)

    def _paint_query(self):
        # Edit text, not a label: Kodi draws the cursor while it has focus.
        self.getControl(QUERY).setText(self.query)

    def _welcome(self):
        from lib.ui import home, look
        from lib.ui.compat import L

        self.setProperty('HeroLogo', '')
        self.setProperty('HeroFill', '')
        self.setProperty('SearchHasRow', '')
        call = look._hero_call
        call(self, home.HERO_LOGO, 'setImage', '')
        call(self, home.HERO_TITLE, 'setLabel', '[B]%s[/B]' % L(30508))
        call(self, home.HERO_META, 'setText', '')
        call(self, home.HERO_PLOT, 'setText', L(30509))
        call(self, BACKGROUND, 'setImage', look.fanart())
        home._set_fill_image(self, '')
        look.paint_hero(self, L(30509), '', '', SEARCH_STACK)
        self._hero_painted = None

    def _show(self, metas, results):
        from lib.ui.compat import L
        from lib.ui.home import _poster_item

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
            self._paint_hero(items[0])
        else:
            self._welcome()

    # --- input ---------------------------------------------------------------

    @gated_input
    def onAction(self, action):
        action_id = action.getId()
        if action_id in _BACK_ACTIONS:
            self.close()
            return
        bar_changed, self._bar_changed = self._bar_changed, False
        if action_id in _BAR_OPEN_ACTIONS and self.getFocusId() == QUERY:
            if bar_changed and not self._searching:
                self._go()
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
            self._paint_hero(self.getControl(ROW).getSelectedItem())

    def _paint_hero(self, item):
        """The shared poster hero for `item`, after a live channel's
        now/next is put on it (`livenow.track_item`). The feed's wake is an
        `Action(noop)`, which lands in `onAction` and so repaints here when
        the schedule arrives or a programme changes."""
        from lib.ui import livenow, look

        if item is not None:
            self._now_feed = livenow.ensure_feed(self._now_feed)
            livenow.track_item(self._now_feed, item)
        look.paint_poster_hero(self, item, SEARCH_STACK)

    @gated_input
    def onClick(self, control_id):
        if control_id == QUERY and self._typing_search:
            # Typing on during a search started by typing: take the new
            # text; the running search sees the bar change and stops.
            self._bar_edited()
            return
        if self._searching:
            # `run_cancellable` delivers queued clicks while its spinner is
            # up. Acting on them would edit or start a search under the one
            # still running, whose results would then be painted for a
            # query the bar no longer shows.
            return
        if control_id == QUERY:
            self._bar_edited()
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
        self._query_changed()

    def _bar_edited(self):
        """Typed (or confirmed in Kodi's keyboard) into the bar: take its
        text as the query, cut to `MAX_QUERY` like picked letters."""
        bar = self.getControl(QUERY)
        text = bar.getText() or ''
        if len(text) > MAX_QUERY:
            text = text[:MAX_QUERY]
            bar.setText(text)
        self.query = text
        self._bar_changed = True
        self._query_changed()
        self._schedule_typed_search()

    def _schedule_typed_search(self):
        """(Re)start the pause timer. Its worker only posts a wake-up; the
        search runs on this window's thread in `_apply_ready()`."""
        import time

        self._cancel_typed_search()
        if not self.query.strip():
            return
        self._typed_at = time.monotonic()
        self._type_timer = _start_pause_timer(_TYPE_PAUSE_SECONDS, lambda: self._post(('typed', '', None)))

    def _cancel_typed_search(self):
        """Forget the pending pause: its timer, and (via `_typed_at`) any
        wake-up it already posted."""
        self._typed_at = None
        timer, self._type_timer = self._type_timer, None
        if timer is not None:
            timer.cancel()

    def _typed_search_due(self):
        """Only from the bar: the user who has moved to the letters or the
        row must not have it reset under the cursor."""
        import time

        query = self.query.strip()
        return (
            self._typed_at is not None
            and len(query) >= _TYPE_MIN_CHARS
            and query != self._results_query
            and not self._searching
            and self.getFocusId() == QUERY
            and time.monotonic() - self._typed_at >= _TYPE_PAUSE_SECONDS * 0.9
        )

    def _search_typed(self):
        """Search what was typed without leaving the bar: no spinner, the
        progress on the status line, cancelled as soon as the bar changes
        (`_BarProgress`). Results replace the row; the focus stays put."""
        from lib.ui.compat import L
        from lib.ui.dependencies import get_client, get_store
        from lib.ui.searchwindow import SearchReport, _search_notice, run_query

        query = self.query.strip()
        report = SearchReport()
        self._typed_at = None
        self._searching = self._typing_search = True
        self._status(L(30033))
        try:
            metas = run_query(get_store(), get_client(), query, report=report,
                              progress=_BarProgress(self, query))
        except Exception as exc:
            import xbmc

            from lib.ui.compat import log

            log('search: search failed: %s' % type(exc).__name__, xbmc.LOGWARNING)
            metas = []
        finally:
            self._searching = self._typing_search = False
        if report.canceled or self.query.strip() != query:
            # Superseded by more typing: that edit already restarted the
            # pause, and its search will paint.
            return
        self._metas = list(metas or [])
        self._results_query = query
        if self._metas:
            self._show(self._metas, results=True)
            self._status(_search_notice(report) or '')
            return
        self._show_no_results(report, query)

    def _show_no_results(self, report, query):
        """Nothing found: take the previous results off the row (OK on one
        would open the Discover title at its index) and say so."""
        from lib.ui.compat import L
        from lib.ui.searchwindow import _search_notice

        self._show([], results=True)
        self._status(_search_notice(report) or _with_query(L(30513), query))

    def _query_changed(self):
        if not self.query.strip():
            # An empty bar is Discover again; typing the last query anew
            # must search it again.
            self._metas = []
            self._results_query = ''
            self._status('')
            self._show(self._discover, results=False)

    def _go(self):
        """The Search pill: search what is in the bar, under the spinner,
        then move to the results."""
        query = self.query.strip()
        if not query:
            return
        from lib.ui.dependencies import get_client, get_store
        from lib.ui.searchwindow import SearchReport, _search_notice, run_query

        report = SearchReport()
        # The pill runs this search; a pause that ends under its spinner
        # must not start it again once the user cancels it.
        self._cancel_typed_search()
        self._searching = True
        try:
            metas = run_query(get_store(), get_client(), query, report=report)
        except Exception as exc:
            import xbmc

            from lib.ui.compat import log

            log('search: search failed: %s' % type(exc).__name__, xbmc.LOGWARNING)
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
        self._show_no_results(report, query)

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
        typed = False
        for kind, _query, metas in ready:
            if kind == 'typed':
                typed = True
            elif kind == 'discover':
                self._discover = list(metas or [])
                if not self._metas and self._discover:
                    self._show(self._discover, results=False)
        if typed and self._typed_search_due():
            self._search_typed()

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


def _with_query(text, query):
    """`text` (a string with one ``%s``) around the query."""
    query = escape_label(query)
    return text % query if text.count('%s') == 1 else '%s %s' % (text, query)


def discover_metas():
    """The first page of the first plain movie catalog (else series)."""
    from lib.stremio.addons import _base_type, iter_catalogs
    from lib.ui.catalogpicker import _classify_catalog, _reachable_catalogs
    from lib.ui.dependencies import get_store
    from lib.ui.home import CatalogUnavailable, _one_catalog_page

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
    """Open the search. True when playback started (close the caller).

    A person's credits no longer come here: they open the coverflow like
    every other list of titles (`lib.ui.infowindow.open_credit_link`)."""
    import xbmc

    from lib.ui.compat import L, log, notify

    log('search: opening', xbmc.LOGINFO)
    window = None
    try:
        window = open_window(SearchWindow, 'SearchWindow.xml')
        return window.start()
    except Exception as exc:  # a skin failure must surface, not vanish
        from lib.stremio.addons import addon_error_detail

        log('search: window failed: %s' % addon_error_detail(exc), xbmc.LOGERROR)
        notify(L(30032))
        return False
    finally:
        if window is not None:
            try:
                window.close()
            except Exception:
                pass


def _start_pause_timer(delay, callback):
    """A started daemon `threading.Timer`; its own function so tests can
    fire the pause without waiting for it."""
    timer = threading.Timer(delay, callback)
    timer.daemon = True
    timer.start()
    return timer


class _BarProgress:
    """`run_query()`'s progress sink for a search started by typing: the
    messages go to the status line, and it reports cancelled once the bar
    no longer holds the query (the user typed on) or Kodi is stopping."""

    def __init__(self, window, query):
        self._window = window
        self._query = query

    def iscanceled(self):
        import xbmc

        if self._window._closing or xbmc.Monitor().abortRequested():
            return True
        try:
            text = self._window.getControl(QUERY).getText() or ''
        except Exception:
            text = self._window.query
        return text.strip()[:MAX_QUERY] != self._query

    def update(self, _percent, message=''):
        if message:
            self._window._status(message)
