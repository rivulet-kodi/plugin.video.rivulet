# SPDX-FileCopyrightText: 2026 M0Rf30
# SPDX-License-Identifier: MIT

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
import queue
import threading
import time

import xbmc
import xbmcgui

from lib.ui.dependencies import get_client, get_store
from lib.ui.uicommon import BaseWindow, busy_dialog, escape_label, open_window

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
        from lib.ui.compat import L, notify

        # Nothing enabled can search (e.g. Cinemeta switched off in the
        # addon manager): say so BEFORE the keyboard opens, rather than
        # after the user has typed a query that can never be answered.
        if not _search_catalogs(self.store or get_store()):
            notify(L(30381), time_ms=_NOTICE_MS)
            return
        # Stripped before anything else sees it: the history row
        # (`Store.add_search_query()` strips) must name the query that was
        # actually sent, a trailing space from a virtual keyboard is a
        # different (uncached, `%20`-suffixed) request to an addon, and a
        # blank entry is a cancel, not a search for "".
        query = (xbmcgui.Dialog().input(L(30001)) or '').strip()
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
        report = SearchReport()
        metas = run_query(self.store, client, query, report=report)

        self._reload()

        notice = _search_notice(report)
        if not metas:
            if report.canceled:
                # The user backed out of the busy dialog: neither a result
                # list nor a verdict ("No results found" would be a lie
                # about a search they abandoned) is wanted.
                return
            # An outage is not "no results": say which it was.
            if notice:
                notify(notice, time_ms=_NOTICE_MS)
            else:
                notify(L(30380))
            return

        if notice:
            # Some catalogs answered, some did not - say so BEFORE the
            # coverflow opens (the toast outlives the transition), so a
            # short list is not mistaken for the whole answer.
            notify(notice, time_ms=_NOTICE_MS)

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


#: Cap on concurrent catalog requests `run_query()` opens at once - its
#: own local constant, like `lib.ui.views._MAX_ADDON_WORKERS` and
#: `lib.ui.streamswindow._MAX_STREAM_ADDON_WORKERS`: every fan-out point
#: in this addon bounds its OWN pool because it also runs on low-power
#: ARM boxes. Each `AddonClient` call still carries its own 15s timeout;
#: this only lets those timeouts run side by side instead of end to end.
#: The loop it replaces was serial, and one aggregator alone publishes
#: eight search catalogs: ~0.5s each on a good day, but on a bad one
#: (measured against a public AIOStreams instance) four of them timed
#: out back to back - a minute behind one spinner.
_MAX_SEARCH_WORKERS = 8

#: How long `run_query()`'s collector blocks on its result queue before
#: it re-checks `dialog.iscanceled()` - short enough that Back is
#: honoured while requests are still in flight (the serial loop only
#: looked between requests, so a cancel pressed during a 15s timeout was
#: not seen for up to 15s), long enough not to spin the CPU.
_SEARCH_POLL_SECONDS = 0.2

#: How long, once every catalog has answered, `run_query()` still waits
#: for the ranking feed (`lib.ui.searchfeed`, ~3.7MB, refreshed daily)
#: it started loading before the fan-out. The feed only reorders ties
#: (`_rank_by_title()`), so a cold fetch that is still running is
#: abandoned for THIS search - it keeps going on its daemon thread and
#: lands in the on-disk cache for the next one - instead of freezing the
#: UI behind a closed spinner for its own 30s timeout, which is what
#: fetching it AFTER the busy dialog had closed used to do.
_FEED_WAIT_SECONDS = 3.0

#: How many failing addons the "search incomplete" notification names;
#: the rest are folded into a trailing `+N`.
_MAX_NAMED_FAILURES = 3

#: How many failed catalogs the one INFO summary line spells out, so a
#: broken install with dozens of dead catalogs cannot grow it unboundedly.
_MAX_LOGGED_FAILURES = 8

#: How long (ms) the longer search notices stay up - the default 4s is
#: too short to read a sentence that ends in an instruction.
_NOTICE_MS = 6000


class SearchReport:
    """How a `run_query()` fan-out went, beyond the metas it returned -
    pass one in to be told, and to own the user-facing message.

    A search can come back short or empty for reasons the metas alone do
    not show, and the old code reported all of them the same way (a
    plain "no results", or a coverflow that quietly lacked a source):

    - `total_catalogs`: search-capable catalogs the ENABLED addons
      declare. 0 means nothing enabled can search at all (e.g. Cinemeta
      switched off in the addon manager), which is not the same thing as
      having searched and found nothing.
    - `queried`: how many of those were actually requested (adult
      catalogs are skipped, unrequested, while home_hide_adult is on).
    - `failures`: `[(addon_name, reason), ...]` in catalog order, one per
      catalog that failed. `reason` is safe to log (an `AddonError`'s
      `category`, or a bare exception type name) - never `str(exc)`.
    - `unanswered`: addon names still in flight when the user backed out.
    - `canceled`: the user backed out of the busy dialog before every
      catalog answered. What had arrived by then is still returned.
    """

    def __init__(self):
        self.total_catalogs = 0
        self.queried = 0
        self.failures = []
        self.unanswered = []
        self.canceled = False

    @property
    def all_failed(self):
        """Every catalog that was asked failed - so an empty result is
        an outage, not a verdict on the query."""
        return self.queried > 0 and len(self.failures) == self.queried

    def failed_addon_names(self):
        """Names of the addons with at least one failed catalog, in
        catalog order, each once (one aggregator failing eight search
        catalogs is one name, not eight)."""
        names = []
        for name, _reason in self.failures:
            if name not in names:
                names.append(name)
        return names


def _search_catalogs(store):
    """Every search-capable catalog the ENABLED addons declare, as
    `iter_catalogs()` yields them: `(transport_url, manifest, catalog)`."""
    from lib.stremio.addons import iter_catalogs

    return list(iter_catalogs(store.get_enabled_addons(), extra_required='search'))


def _search_notice(report):
    """The notification text that explains a search's problems, or None
    when it had none (or the user cancelled it, which explains itself).

    Precedence matches what is most useful to act on: nothing enabled can
    search (enable something), then everything asked failed (an outage;
    retry or add another source), then some failed (the list is shorter
    than it should be). Both of the first two name Cinemeta because it is
    the stock search addon - the one a user is most likely to have
    switched off in the addon manager, which leaves a Cinemeta-less
    install searching through whatever single third-party aggregator
    remains."""
    from lib.ui.compat import L

    if report.canceled:
        return None
    if report.total_catalogs == 0:
        return L(30381)
    if report.all_failed:
        return L(30382)
    names = report.failed_addon_names()
    if not names:
        return None
    shown = names[:_MAX_NAMED_FAILURES]
    label = ', '.join(escape_label(name) for name in shown)
    if len(names) > len(shown):
        label = '%s, +%d' % (label, len(names) - len(shown))
    return L(30383) % label


def _query_catalog(client, transport_url, cat, query):
    """One catalog's search - the unit of work `run_query()` runs
    concurrently. Returns `(metas, failure)`: the metas with `type`
    defaulted from the catalog and `failure` None, or `([], reason)` when
    the request failed.

    This IS a worker-thread body, so it must never raise: an exception
    escaping it would kill its thread before it queued an answer, and the
    collector would wait forever for a result that cannot arrive. An
    `AddonError` is the expected failure and is logged with its safe
    category; anything else (a bug, a third-party library surprise) is
    logged by type name only and still costs just this one catalog."""
    import xbmc

    from lib.stremio.addons import AddonError, addon_error_detail, safe_url_for_log
    from lib.ui.compat import log

    try:
        results = client.catalog(transport_url, cat.get('type'), cat.get('id'), extra=[('search', query)])
    except AddonError as exc:
        log('searchwindow: %s failed: %s' % (safe_url_for_log(transport_url), addon_error_detail(exc)), xbmc.LOGERROR)
        return [], exc.category or type(exc).__name__
    except Exception as exc:  # noqa: BLE001 - see docstring: a worker thread that dies here wedges the collector
        log('searchwindow: %s raised %s' % (safe_url_for_log(transport_url), type(exc).__name__), xbmc.LOGERROR)
        return [], type(exc).__name__
    metas = []
    for meta_obj in results or []:
        if not isinstance(meta_obj, dict):
            continue
        meta_obj['type'] = meta_obj.get('type') or cat.get('type')
        metas.append(meta_obj)
    return metas, None


def _start_search_workers(client, query, jobs):
    """Fan `_query_catalog()` out across a small, genuinely BOUNDED pool of
    raw daemon threads fed by a `queue.Queue`, and return the results
    `Queue` they fill with `(index, (metas, failure))` pairs in COMPLETION
    order - `index` is the catalog's position in `jobs`, so the caller can
    put the answers back in catalog order (addon order is user-controlled
    priority: it decides which copy of a duplicate title wins `_dedupe()`).

    Deliberately raw `threading.Thread(daemon=True)`, not
    `concurrent.futures.ThreadPoolExecutor`: its atexit hook joins every
    worker at interpreter shutdown whatever the daemon flag, so a request
    still inside its 15s timeout would block plugin-process exit - see
    `lib.ui.streamswindow._start_stream_fetch_workers()` for the
    measurement. A raw daemon thread is simply abandoned, which is also
    what lets `run_query()` return before a straggler answers."""
    work = queue.Queue()
    for index, (transport_url, _manifest, cat) in enumerate(jobs):
        work.put((index, transport_url, cat))
    results = queue.Queue()

    def _worker():
        while True:
            try:
                index, transport_url, cat = work.get_nowait()
            except queue.Empty:
                return
            results.put((index, _query_catalog(client, transport_url, cat, query)))

    for _ in range(min(len(jobs), _MAX_SEARCH_WORKERS)):
        threading.Thread(target=_worker, daemon=True).start()
    return results


def _collect_answers(dialog, results, jobs, report):
    """Block on `results` until every job in `jobs` has answered or the
    user cancels, returning `{index: (metas, failure)}` for what arrived.

    Polls in `_SEARCH_POLL_SECONDS` slices so `dialog.iscanceled()` is
    honoured while requests are still in flight. The busy dialog names
    the first catalog still outstanding and shows how many have answered;
    a cancel sets `report.canceled` and leaves the stragglers out - the
    caller still gets whatever had landed, the same "stop waiting" meaning
    Back has in the streams fan-out."""
    from lib.ui.compat import L

    total = len(jobs)
    answered = {}
    dialog.update(0, L(30186) % (jobs[0][1].get('name') or '?'))
    while len(answered) < total:
        if dialog.iscanceled():
            report.canceled = True
            break
        try:
            index, answer = results.get(timeout=_SEARCH_POLL_SECONDS)
        except queue.Empty:
            continue
        answered[index] = answer
        outstanding = next((i for i in range(total) if i not in answered), None)
        if outstanding is not None:
            dialog.update(
                int(len(answered) * 100 / total),
                L(30186) % (jobs[outstanding][1].get('name') or '?'),
            )
    return answered


class _FeedIndexLoader:
    """`_feed_index()` computed on a daemon thread that `run_query()`
    starts BEFORE its fan-out, so a cold ranking-feed fetch (~3.7MB)
    overlaps the catalog requests instead of following them.

    It used to run after `run_query()`'s busy dialog had closed: the one
    search a day that found the cache stale paid for the whole download
    with the spinner already gone (kodi.log: "searchfeed: fetched 19975
    feed records" lands at 21:31:46.1, 0.2s before that search's coverflow
    opens - i.e. after the fan-out, outside the dialog) - up to the
    fetch's own 30s timeout on a bad link.

    Only started when the store has a `data_dir` and the client a
    `session` (the real ones - see `_feed_index()`), so unit tests with
    fakes never spawn it."""

    def __init__(self, store, client):
        self.index = None
        self._store = store
        self._client = client
        self._done = threading.Event()
        if getattr(store, 'data_dir', None) is None or getattr(client, 'session', None) is None:
            self._done.set()
            return
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        import xbmc

        from lib.ui.compat import log

        try:
            self.index = _feed_index(self._store, self._client)
        except Exception as exc:  # noqa: BLE001 - ranking is an enhancement and must never break search
            log('searchwindow: ranking feed failed: %s' % type(exc).__name__, xbmc.LOGWARNING)
        finally:
            self._done.set()

    def wait(self, dialog, timeout=_FEED_WAIT_SECONDS):
        """The feed index if it is ready within `timeout` (cancel-aware),
        else None - `_rank_by_title()`'s feedless path."""
        deadline = time.monotonic() + timeout
        while not self._done.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0 or dialog.iscanceled():
                break
            self._done.wait(min(_SEARCH_POLL_SECONDS, remaining))
        return self.index if self._done.is_set() else None


def run_query(store, client, query, report=None):
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

    The catalogs are queried CONCURRENTLY (`_MAX_SEARCH_WORKERS`), not one
    after another, and answers are put back in catalog order before
    anything is merged, so which copy of a duplicate title wins does not
    depend on which request happened to be fastest. Back is polled while
    requests are in flight (`_collect_answers()`); a cancelled search
    still returns what had arrived.

    Pass a `SearchReport` as `report` to learn how the fan-out went
    (nothing enabled can search / every catalog failed / some failed /
    cancelled) and to own the user-facing message - `SearchWindow.
    _run_search()` does, so it can say "search failed" where it would
    otherwise say "no results". Without one, `run_query()` shows that
    notification itself (`_search_notice()`), so a caller that never
    heard of reports (`open_credits_picker()`) still stops presenting an
    outage as an empty search. One INFO line per call records how many
    catalogs were asked, answered, failed and unanswered: a short or
    empty result used to leave nothing in the log at all.

    An addon's own error placeholder is not a result: `AddonClient.
    catalog()` turns a lone one into an `AddonError`, so it lands in
    `report.failures` like any other failed catalog. (Left as a title,
    eight search catalogs answering the same AIOStreams "404 - Not Found"
    placeholder collapsed in `_dedupe()` to a coverflow of "1 results"
    that was the error itself.)

    When resources/settings.xml's home_hide_adult setting is on (the
    default, same toggle `lib.ui.views.iter_catalog_pages()` reads): a
    catalog that itself looks adult (`lib.stremio.contentrating.
    is_adult_catalog()`) is skipped before spending a request on it, and
    every adult-flagged meta is dropped from the aggregate afterwards
    (`filter_metas()`) - the same two-layer policy `iter_catalog_pages()`
    applies, reused rather than reinvented. No separate "results
    exhausted by filtering" path is needed: an all-adult query already
    falls out as an empty `metas` list, which `_run_search()`'s existing
    no-results branch treats as an ordinary no-results search.

    The collected metas are deduplicated (`_dedupe()`) and then ordered
    by how well each title matches the query (`_rank_by_title()`), after
    the existing credit ranking. Both callers want that: a person
    dispatch from `open_credits_picker()` fans out the same way and gets
    the same duplicates back."""
    import xbmc

    from lib.stremio.contentrating import filter_metas, is_adult_catalog
    from lib.ui.compat import L, log, notify, setting_bool

    own_report = report is None
    if own_report:
        report = SearchReport()
    report.failures = []
    report.unanswered = []
    report.canceled = False
    hide_adult = setting_bool('home_hide_adult', True)
    catalogs = _search_catalogs(store)
    report.total_catalogs = len(catalogs)
    jobs = [entry for entry in catalogs if not (hide_adult and is_adult_catalog(entry[2], entry[1]))]
    report.queried = len(jobs)

    metas = []
    feed_index = None
    answered = {}
    if jobs:
        with busy_dialog(L(30033), query) as dialog:
            loader = _FeedIndexLoader(store, client)
            results = _start_search_workers(client, query, jobs)
            answered = _collect_answers(dialog, results, jobs, report)
            if not report.canceled:
                feed_index = loader.wait(dialog)
        for index, (_transport_url, manifest, _cat) in enumerate(jobs):
            name = manifest.get('name') or '?'
            answer = answered.get(index)
            if answer is None:
                report.unanswered.append(name)
                continue
            found, failure = answer
            if failure is not None:
                report.failures.append((name, failure))
            metas.extend(found)
    if hide_adult:
        metas = filter_metas(metas)
    ranked = _rank_by_title(_dedupe(_rank_by_credit(metas, query)), query, feed_index)

    log(
        'searchwindow: %d catalog(s) declared, %d asked, %d answered, %d failed, %d unanswered%s -> %d result(s)' % (
            report.total_catalogs, report.queried, len(answered), len(report.failures), len(report.unanswered),
            ' (canceled)' if report.canceled else '', len(ranked),
        ),
        xbmc.LOGINFO,
    )
    if report.failures:
        log('searchwindow: failed catalogs: %s' % ', '.join(
            '%s (%s)' % (name, reason) for name, reason in report.failures[:_MAX_LOGGED_FAILURES]
        ), xbmc.LOGINFO)
    if own_report:
        notice = _search_notice(report)
        if notice:
            notify(notice, time_ms=_NOTICE_MS)
    return ranked


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

    Called from `_FeedIndexLoader`'s thread, not inline: see there.
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
