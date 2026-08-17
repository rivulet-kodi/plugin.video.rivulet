"""HomeWindow: Rivulet's custom entry-point screen. A vertical menu
over the addon's fanart; picking a row opens the next screen as a
nested modal (see `lib.ui.uicommon`'s module docstring for the
navigation model this and every other custom screen shares).

Discover/Search/Library each draw a nested modal over Home and only
close it once their own selection chain reaches playback (see
`_open_discover`/`_open_search`/`_open_mystuff`); Add-ons has no
playback path of its own, so Home always stays open behind it (see
`_open_addons`). Settings opens Kodi's own native settings dialog,
which is not worth replacing.
"""
import xbmcgui

from lib.ui.dependencies import get_store
from lib.ui.uicommon import BaseWindow, open_window

BACKGROUND = 30000
LIST = 30002
STATUS_LABEL = 30005  # plain text label; set at runtime via setLabel(), not a skin <label>

#: The content-type rows that replaced the single "Discover" entry, in
#: menu order: (action, {Stremio base catalog types} or None, setting id).
#: A catalog's type is reduced to its base - everything before the first
#: '.', lowercased, via `_base_type()` - before matching one of these
#: sets, so `anime.movie`/`anime.series` (the Stremio dotted-subtype
#: convention) join Anime and a stray `TV` joins Series the same as `tv`.
#:
#: `series` deliberately carries `tv` alongside it. An addon that
#: publishes live/linear channels types them "tv", and those are episodic
#: listings a viewer thinks of as television - grouping them under Series
#: keeps them reachable without a row of their own that would sit empty
#: for nearly everyone. "anime" is the one type common enough to earn its
#: own row: it is what the large anime catalog addons (and AIOStreams)
#: actually publish, and its audience wants it separated from film.
#:
#: `other` carries `None` instead of a fixed set: it is a catch-all for
#: whatever an addon publishes that the three curated rows do not claim
#: (Stremio lets an addon declare any string as a type), so its actual
#: type set cannot be a constant - it is computed live from installed
#: addons by `_remainder_types()`. Every consumer of `TYPE_ROW_TYPES`
#: must treat `None` as that sentinel, not as "unfiltered".
_TYPE_ROWS = (
    ('movies', frozenset({'movie'}), 'home_show_movies'),
    ('series', frozenset({'series', 'tv'}), 'home_show_series'),
    ('anime', frozenset({'anime'}), 'home_show_anime'),
    ('other', None, 'home_show_other'),
)

#: action -> the set of catalog types its picker is filtered to, or
#: `None` for `other`'s "compute the remainder" sentinel (see
#: `_TYPE_ROWS`).
TYPE_ROW_TYPES = {action: types for action, types, _ in _TYPE_ROWS}


def _remainder_types(available):
    """The `other` row's type set: every base type in `available` that no
    curated row (a non-`None` entry in `_TYPE_ROWS`) claims. Computed
    live rather than declared as a constant, because unlike Movies/
    Series/Anime its membership depends entirely on what is installed."""
    claimed = frozenset().union(*(types for _, types, _ in _TYPE_ROWS if types is not None))
    return available - claimed


#: (localized-string id, action) - the authoritative definition of Home's menu rows.
#:
#: `mystuff` replaces what used to be two separate rows, "Continue
#: watching" (30231) and "Library" (30002): both built a list of metas
#: and handed it to the same coverflow, and they overlap heavily in
#: practice (a title you are part-way through is usually also in your
#: library), so splitting them made the viewer guess which one held the
#: thing they wanted. See `lib.ui.mystuff` for the merge and its bands.
_MENU = (
    (30241, 'mystuff'),
    (30213, 'movies'),
    (30214, 'series'),
    (30215, 'anime'),
    (30227, 'other'),
    (30001, 'search'),
    (30003, 'addons'),
    (30004, 'settings'),
)


#: (action -> localized-string id) for HomeWindow.xml's dimmer second
#: label per row - localized via L(), not plain literals, so it follows
#: Kodi's language setting the same as every other row's main label.
_SUBTITLES = {
    'mystuff': 30232,
    'movies': 30216,
    'series': 30217,
    'anime': 30218,
    'other': 30228,
    'search': 30149,
    'addons': 30151,
    'settings': 30152,
}


def _available_types(addons):
    """Return the set of base catalog types `addons` actually publish,
    reduced via `lib.stremio.addons._base_type()` - the same rule the
    picker's own `types=` filter applies - so a dotted subtype like
    `anime.movie` is counted under `anime` and a stray `TV` under `tv`.

    Drives the auto-hide half of a type row's visibility: a row whose
    types nothing installed publishes would open an empty picker, so it
    is left out entirely rather than offered and then disappointing.
    """
    from lib.stremio.addons import _base_type, iter_catalogs

    return {_base_type(catalog.get('type')) for _, _, catalog in iter_catalogs(addons)}


def _type_row_enabled(action, available):
    """Whether the type row `action` should appear: its setting must be
    on (all four default True) AND something installed must publish a
    catalog of one of its types - for `other`, "its types" is whatever
    `_remainder_types()` computes, not a fixed set."""
    from lib.ui.compat import setting_bool

    types = TYPE_ROW_TYPES.get(action)
    if types is None:
        types = _remainder_types(available)
    if not types & available:
        return False
    setting_id = next(sid for act, _, sid in _TYPE_ROWS if act == action)
    return setting_bool(setting_id, True)


def _menu_items(show_mystuff, addons=None):
    from lib.ui.compat import L, addon_media_path

    available = _available_types(addons or [])
    items = []
    for string_id, action in _MENU:
        if action == 'mystuff' and not show_mystuff:
            continue
        if action in TYPE_ROW_TYPES and not _type_row_enabled(action, available):
            continue
        item = xbmcgui.ListItem(L(string_id))
        # One setProperties() call instead of two setProperty() calls
        # (action, subtitle) - setArt() stays separate, a distinct Kodi
        # API that setProperties() cannot batch into.
        item.setProperties({'action': action, 'subtitle': L(_SUBTITLES[action])})
        item.setArt({'icon': addon_media_path('%s.png' % action)})
        items.append(item)
    return items


def _status_text(auth):
    """Render HomeWindow's top status line from the same `get_auth()`
    result onInit() already fetched for `show_library`: uses the shared
    "Logged in as <email/name/?>" string (30022), the same one
    `lib.ui.views.login()` shows as a post-login notification; the
    logged-out case uses its own string id (30190). Both states are
    prefixed with a `[COLOR]`-tinted `●` indicator dot per the design's
    status pill (font10 is NotoSans and covers U+25CF; Mono26 does not)."""
    from lib.ui.compat import L

    if not auth:
        return '[COLOR 57EEF3F6]\u25cf[/COLOR] %s' % L(30190)
    user = auth.get('user') or {}
    text = L(30022) % (user.get('email') or user.get('name') or '?')
    return '[COLOR FF38BDF8]\u25cf[/COLOR] %s' % text


class HomeWindow(BaseWindow):
    """See module docstring. Built/run via `open_home()`."""

    def onInit(self):
        from lib.ui.compat import addon_fanart, setting_bool
        from lib.ui.mystuff import has_content

        store = get_store()
        auth = store.get_auth()
        # Recomputed every onInit(): onInit() re-runs on every resume from
        # a nested modal (see reset() below), so the row appears/
        # disappears the moment the first title is played or the user logs
        # in, without restarting the addon.
        show_mystuff = setting_bool('home_show_mystuff', True) and has_content(store)
        self.getControl(BACKGROUND).setImage(addon_fanart())
        control = self.getControl(LIST)
        # reset() before addItems(): onInit() runs again when
        # uicommon.ModalStackWindow reopens a screen force-closed for
        # playback, and re-adding onto a retained list would double every
        # item. Rebuilding also re-reads the type rows' settings and the
        # installed catalogs, so toggling a row in Settings (or installing
        # an addon that publishes a new type) is reflected on the way back
        # here without restarting the addon.
        control.reset()
        control.addItems(_menu_items(show_mystuff, store.get_addons()))
        self.getControl(STATUS_LABEL).setLabel(_status_text(auth))
        self.setFocusId(LIST)

    def onClick(self, control_id):
        if control_id != LIST:
            return
        focused = self.getControl(LIST).getSelectedItem()
        if focused is None:
            return
        handler = _ACTIONS.get(focused.getProperty('action'))
        if handler:
            handler(self)


def _open_mystuff(window):
    from lib.ui.mystuff import open_my_stuff
    if open_my_stuff():
        window.close()


def _open_type_row(window, action):
    # Nested modal: the picker draws over Home, so Home stays open -
    # backing all the way out returns here rather than exiting the addon.
    from lib.ui.catalogpicker import open_catalog_picker
    from lib.ui.compat import L
    types = TYPE_ROW_TYPES[action]
    if types is None:  # 'other': not a fixed set - compute the remainder live
        types = _remainder_types(_available_types(get_store().get_addons()))
    if open_catalog_picker(types=types, heading=L(L_FOR_ACTION[action])):
        window.close()


def _open_search(window):
    from lib.ui.searchwindow import open_search
    if open_search():
        window.close()


def _open_addons(window):
    from lib.ui.addonswindow import open_addons
    open_addons()


def _open_settings(window):
    from lib.ui.compat import ADDON
    ADDON.openSettings()


#: action -> the localized-string id its picker uses as a heading, so the
#: filtered screen names the row the user came in through ("MOVIES")
#: rather than the generic catalog-picker title.
L_FOR_ACTION = {action: string_id for string_id, action in _MENU}


_ACTIONS = {
    'mystuff': _open_mystuff,
    'movies': lambda window: _open_type_row(window, 'movies'),
    'series': lambda window: _open_type_row(window, 'series'),
    'anime': lambda window: _open_type_row(window, 'anime'),
    'other': lambda window: _open_type_row(window, 'other'),
    'search': _open_search,
    'addons': _open_addons,
    'settings': _open_settings,
}


def _notify_if_updated(store):
    """Toast the user once when the running addon version differs from the
    version `store` last recorded - the "tell them on next launch" approach
    (as opposed to a boot-time service notification, which is easy to miss
    behind Kodi's splash/home screen). A brand-new install has nothing
    recorded yet, so that first call only seeds the current version and
    stays silent; only version numbers that actually change are ever
    announced.

    Best-effort: `store` I/O failing here must never stop the home screen
    from opening, so any exception is logged and swallowed, same as other
    non-critical HomeWindow paths."""
    import xbmc

    from lib.ui.compat import log
    try:
        from lib.ui.compat import ADDON, L, notify

        current = ADDON.getAddonInfo('version')
        last_seen = store.get_last_seen_version()
        if last_seen == current:
            return
        store.set_last_seen_version(current)
        if last_seen is not None:
            notify(L(30184) % current)
    except Exception as exc:
        log('homewindow: update-notification check failed: %r' % (exc,), xbmc.LOGWARNING)


def open_home():
    """Build and run the HomeWindow modal; blocks until the user exits.

    default.py wraps this call in its own try/except and falls back to
    the minimal recovery directory (Settings row) on ANY exception, so
    an exception raised here must keep propagating unchanged - this
    only logs it for diagnostics and
    guarantees the window is closed (it may not have had a chance to
    self-close, e.g. if onInit() or doModal() itself raised) before
    re-raising."""
    import xbmc

    from lib.ui.compat import log

    log('homewindow: opening HomeWindow', xbmc.LOGINFO)
    # Runs here, not in HomeWindow.onInit(): onInit() re-fires every time
    # Home is resumed after a nested modal (Discover/Search/...) closes,
    # which would re-show the toast on every back-navigation. open_home()
    # runs exactly once per addon launch. get_store() itself is guarded
    # here too (not just inside _notify_if_updated): a cosmetic toast must
    # never stop Home from opening, even if Store construction itself
    # fails (e.g. an unwritable profile directory).
    try:
        _notify_if_updated(get_store())
    except Exception as exc:
        log('homewindow: update-notification store unavailable: %r' % (exc,), xbmc.LOGWARNING)
    win = open_window(HomeWindow, 'HomeWindow.xml')
    try:
        win.doModal()
    except Exception as exc:  # default.py's caller falls back to the recovery directory
        log('homewindow: HomeWindow failed: %r' % (exc,), xbmc.LOGERROR)
        raise
    finally:
        # A normal return means HomeWindow already closed itself; close()
        # again here is a safe no-op. Only a raised exception makes this
        # the window's one chance to close.
        try:
            win.close()
        except Exception:
            pass
    log('homewindow: HomeWindow closed', xbmc.LOGINFO)
