"""SearchWindow: a persistent search-history/new-query picker. Unlike
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

from lib.ui.dependencies import get_client, get_store
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
        """doModal() and return True if the caller should also close
        (playback started somewhere down the chain, e.g. after a
        movie/series round trip)."""
        self.should_close_caller = False
        self.doModal()
        return self.should_close_caller

    def onInit(self):
        self._reload()

    def _reload(self):
        self.store = self.store or get_store()
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
        from lib.ui import dialogs
        from lib.ui.compat import L

        if not dialogs.confirm(L(30044), L(30046), xbmc.getLocalizedString(107), xbmc.getLocalizedString(106)):
            return
        self.store.clear_search_history()
        self._reload()

    def _run_search(self, query):
        from lib.ui.compat import L, log, notify

        self.store.add_search_query(query)

        client = get_client()
        metas = run_query(self.store, client, query)

        self._reload()

        if not metas:
            notify(L(30030))
            return

        log('searchwindow: opening coverflow (%d results)' % len(metas), xbmc.LOGINFO)
        try:
            from lib.ui.infowindow import open_showcase
            selected = open_showcase(metas, catalog_title='%s \u00b7 %s' % (L(30001), query))
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


def _rank_by_credit(metas, query):
    """Stable-sort `metas` so any meta crediting `query` in its
    `cast`/`director`/`writer` list (case-insensitive, exact match
    against a list entry) is ranked ahead of the rest, preserving each
    group's original relative order otherwise.

    The protocol has no field-scoped query - `search=` is always plain
    full-text, so a query that came from a Cast/Directors/Writers meta
    link (see lib.stremio.metalinks) genuinely returns both the
    person's credited titles and unrelated title matches (Cinemeta's
    own "Marlon Brando" search returns both One-Eyed Jacks, where he is
    cast+director, and "Listen to Me Marlon", a title-only match). We
    RANK rather than filter: filtering would hide results the addon
    actually returned, and would silently break for addons whose
    search previews omit cast/director/writer entirely.
    """
    needle = query.casefold()

    def _credit_rank(meta_obj):
        for field in ('cast', 'director', 'writer'):
            for entry in meta_obj.get(field) or []:
                if isinstance(entry, str) and entry.casefold() == needle:
                    return 0
        return 1

    return sorted(metas, key=_credit_rank)


def _dedupe(metas):
    """Collapse metas sharing a `(type, id)` to a single entry, keeping
    each title's first-seen position.

    The fan-out asks every search-capable catalog the same question, so
    the same title genuinely comes back many times over - a title in
    both Cinemeta's `top` catalog and an aggregator's own search catalog
    is two copies before any addon is even duplicated. Measured against
    a real install (Cinemeta plus AIOLists' four search catalogs),
    "alien" returned 96 metas covering 57 distinct titles: 41% of what
    the coverflow showed was a repeat of something already in it.

    Fields are merged rather than dropped with the losing copy. Search
    previews are trimmed per-addon and each addon trims differently, so
    the union across duplicates carries strictly more metadata than any
    single copy - on that same install Inception came back three times
    and only the third copy carried `imdbRating`. A field is filled in
    only where the winner has nothing, so the first-seen copy stays
    authoritative wherever it actually has a value.

    Metas with no `id` are dropped: `open_detail()` is keyed by id, so
    an id-less meta is a dead entry in the coverflow.
    """
    winners = {}
    order = []
    for meta_obj in metas:
        content_id = meta_obj.get('id')
        if not content_id:
            continue
        key = (meta_obj.get('type'), content_id)
        winner = winners.get(key)
        if winner is None:
            winners[key] = dict(meta_obj)
            order.append(key)
            continue
        for field, value in meta_obj.items():
            if winner.get(field) in (None, '', []) and value not in (None, '', []):
                winner[field] = value
    return [winners[key] for key in order]


#: `_match_tier()`'s return values, best first. Only their ORDER
#: matters - they are ranks handed to `sorted()`, never arithmetic.
_TIER_EXACT, _TIER_PREFIX, _TIER_WORD, _TIER_SUBSTRING, _TIER_OTHER = range(5)


def _match_tier(name, needle):
    """Rank how well `name` matches `needle` - both already casefolded
    and stripped by `_rank_by_title()`.

    Tiers rather than a similarity score: `search=` is plain full-text
    and every addon implements it differently, so the only signal that
    generalises across them is how the returned title relates to what
    was typed. Exact beats "starts with" beats "contains the query as a
    whole word" beats "contains it anywhere".

    The whole-word tier is what separates "Alien Nation" from "My
    Stepmother Is an Alien" - both merely contain the query, but only
    one leads with it. Trailing punctuation is stripped per word so a
    mid-title "Alien:" still counts as the word "alien".
    """
    if not name:
        return _TIER_OTHER
    if name == needle:
        return _TIER_EXACT
    if name.startswith(needle):
        return _TIER_PREFIX
    if any(word.strip(':,.-!?\'"') == needle for word in name.split()):
        return _TIER_WORD
    if needle in name:
        return _TIER_SUBSTRING
    return _TIER_OTHER


def _rank_by_title(metas, query, feed_index=None):
    """Stable-sort `metas` by `_match_tier()`, best tier first, breaking
    ties within a tier by `lib.ui.searchfeed`'s popularity/rating boost
    when `feed_index` is given.

    Stable, so titles the feed says nothing about keep the order they
    already had - this only ever moves a title relative to titles in a
    DIFFERENT tier, or above one the feed scores lower in the SAME tier,
    and never invents an ordering where neither the tier nor the feed
    expressed one. Runs after `_rank_by_credit()` and so reorders its
    output: a credited title that does not also match by name is still a
    name miss, and the coverflow should lead with what the user typed.

    The tier always outranks the boost - popularity breaks ties, it does
    not cross tiers. `stremio-core` folds its boost into the text-match
    score instead, letting a popular title outrank a better textual
    match; here the query is typed in full and submitted rather than
    completed keystroke by keystroke, so a title the user typed exactly
    must not be displaced by a more popular near-match.
    """
    needle = (query or '').casefold().strip()
    if not needle:
        return metas
    if feed_index is None:
        return sorted(metas, key=lambda meta_obj: _match_tier((meta_obj.get('name') or '').casefold().strip(), needle))

    from lib.ui.searchfeed import boost

    index, max_rating, max_popularity = feed_index

    def _rank(meta_obj):
        tier = _match_tier((meta_obj.get('name') or '').casefold().strip(), needle)
        return (tier, -boost(meta_obj, index, max_rating, max_popularity))

    return sorted(metas, key=_rank)


def run_query(store, client, query):
    """Fan `query` across every search-capable catalog
    (`iter_catalogs(..., extra_required='search')`) and return the
    collected metas, each with `type` defaulted from its catalog.
    Extracted verbatim from `SearchWindow._run_search()`'s own fan-out
    (progress dialog via `busy_dialog`/L(30186), per-addon `AddonError`
    isolation and logging included) so other windows can run a search
    without going through `SearchWindow` -
    `lib.ui.infowindow.open_credits_picker()`'s "person" dispatch is the
    second caller. Writes no history, opens no coverflow - callers own
    both.

    The collected metas are deduplicated (`_dedupe()`) and then ordered
    by how well each title matches the query (`_rank_by_title()`), after
    the existing credit ranking. Both callers want that: a person
    dispatch from `open_credits_picker()` fans out the same way and gets
    the same duplicates back."""
    from lib.stremio.addons import AddonError, iter_catalogs, safe_url_for_log
    from lib.ui.compat import L, log

    metas = []
    catalogs = list(iter_catalogs(store.get_enabled_addons(), extra_required='search'))
    total_catalogs = len(catalogs)
    with busy_dialog(L(30033), query) as dialog:
        for index, (transport_url, manifest, cat) in enumerate(catalogs):
            if dialog.iscanceled():
                break
            percent = int(index * 100 / total_catalogs) if total_catalogs else 0
            dialog.update(percent, L(30186) % (manifest.get('name') or '?'))
            try:
                results = client.catalog(transport_url, cat.get('type'), cat.get('id'), extra=[('search', query)])
            except AddonError as exc:
                log('searchwindow: %s failed: %s' % (safe_url_for_log(transport_url), type(exc).__name__), xbmc.LOGERROR)
                continue
            for meta_obj in results or []:
                meta_obj['type'] = meta_obj.get('type') or cat.get('type')
                metas.append(meta_obj)
    return _rank_by_title(_dedupe(_rank_by_credit(metas, query)), query, _feed_index(store, client))


def _feed_index(store, client):
    """The `lib.ui.searchfeed` index for `_rank_by_title()`, or None when
    the feed is unavailable - a cold fetch that fails, a store with no
    `data_dir`, or a client with no session to borrow.

    Reuses the `AddonClient`'s own `requests.Session()` rather than
    opening a second one: the feed is an ordinary HTTPS GET and the
    session already carries the addon's connection pooling.

    None (rather than an empty index) is deliberate - it routes
    `_rank_by_title()` down its feedless path, which is exactly the
    behaviour this module had before the feed existed.
    """
    data_dir = getattr(store, 'data_dir', None)
    session = getattr(client, 'session', None)
    if data_dir is None or session is None:
        return None
    from lib.ui.searchfeed import build_index, load_records

    records = load_records(data_dir, session)
    if not records:
        return None
    return build_index(records)


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
