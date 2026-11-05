# SPDX-FileCopyrightText: 2026 M0Rf30
# SPDX-License-Identifier: MIT

"""Search engine behind the one-page search (`lib.ui.search`) and the
coverflow's person/discover links: `run_query()` fans a query across every
searchable catalog on daemon threads, merges the answers in catalog order,
ranks them (`_rank_by_title`, `_rank_by_credit`) and reports failures through
`SearchReport`. No window lives here any more; the screen is `SearchWindow`.
"""
import os
import queue
import threading
import time
from contextlib import nullcontext

import xbmc

from lib.ui.uicommon import busy_dialog, deliver_queued_input, escape_label


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

#: Once a catalog has answered WITH titles, how long `run_query()` still
#: waits for the rest. One search catalog that answers slowly (or retries
#: three 15s timeouts) held every result that had already landed: the
#: spinner stayed up until Back, and Back showed the results. The
#: stragglers are left out of this search, as Back leaves them out.
_SEARCH_GRACE_SECONDS = 8.0

#: The most `run_query()` waits for the catalogs at all, results or not.
_SEARCH_DEADLINE_SECONDS = 25.0

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
    - `unanswered`: addon names still in flight when the user backed out,
      or when the wait ended (`_SEARCH_GRACE_SECONDS` after the first
      titles, or `_SEARCH_DEADLINE_SECONDS`); one entry per catalog.
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
    remains. Last, with nothing failed, catalogs that never answered in
    time (a slow but good catalog would otherwise be silently missing)."""
    from lib.ui.compat import L

    if report.canceled:
        return None
    if report.total_catalogs == 0:
        return L(30381)
    if report.all_failed:
        return L(30382)
    names = report.failed_addon_names()
    if not names:
        if report.unanswered:
            # Catalogs too slow for the grace period or the deadline:
            # their titles are missing, so say how many were left out.
            return L(30416) % len(report.unanswered)
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


#: First try plus two silent retries when a catalog errors. An empty
#: success is the catalog's answer. Local to this fan-out, same reason
#: `_MAX_SEARCH_WORKERS` is not shared with the streams pool.
_SEARCH_ATTEMPTS = 3
_SEARCH_RETRY_BACKOFF_SECONDS = (0.4, 0.8)


def _pause_before_search_retry(seconds):
    """See `streamswindow._pause_before_stream_retry`. Returns True when
    Kodi is aborting and the worker should stop."""
    if seconds <= 0:
        return False
    return xbmc.Monitor().waitForAbort(seconds)


def _query_catalog_retrying(client, transport_url, cat, query, stop=None):
    """`_query_catalog()` with silent retries on failure only. The
    collector still sees one answer per catalog, so the spinner stays
    up until the retries are done or the user backs out (the collector
    polls cancellation on its own thread)."""
    metas, failure = [], 'failed'
    for attempt in range(_SEARCH_ATTEMPTS):
        metas, failure = _query_catalog(client, transport_url, cat, query)
        if failure is None or attempt + 1 >= _SEARCH_ATTEMPTS:
            return metas, failure
        if stop is not None and stop.is_set():
            return metas, failure
        if _pause_before_search_retry(_SEARCH_RETRY_BACKOFF_SECONDS[attempt]):
            return metas, failure
    return metas, failure


def _start_search_workers(client, query, jobs, stop=None):
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
    what lets `run_query()` return before a straggler answers.

    `stop` (a `threading.Event`) is set by `run_query()` once it stops
    collecting - cancelled, the grace period or the deadline. An abandoned
    worker used to keep draining `work`: every catalog still queued was
    asked anyway, with its retries, for a search nobody would see. Search
    as you type made that the normal case - each pause abandons the last
    search - and on a box with eight search catalogs a few words typed
    with pauses stacked several full fan-outs. Now a worker finishes the
    request it is in (a `requests` call cannot be interrupted) and takes
    no other."""
    work = queue.Queue()
    for index, (transport_url, _manifest, cat) in enumerate(jobs):
        work.put((index, transport_url, cat))
    results = queue.Queue()

    def _worker():
        try:
            while True:
                if stop is not None and stop.is_set():
                    return
                try:
                    index, transport_url, cat = work.get_nowait()
                except queue.Empty:
                    return
                results.put((index, _query_catalog_retrying(client, transport_url, cat, query, stop)))
        finally:
            release = getattr(client, '_release_worker_session', None)
            if callable(release):
                release()

    for _ in range(min(len(jobs), _MAX_SEARCH_WORKERS)):
        threading.Thread(target=_worker, daemon=True).start()
    return results


def _collect_answers(dialog, results, jobs, report):
    """Block on `results` until every job in `jobs` has answered or the
    user cancels, returning `{index: (metas, failure)}` for what arrived.

    Polls in `_SEARCH_POLL_SECONDS` slices so `dialog.iscanceled()` is
    honoured while requests are still in flight. It also stops waiting
    `_SEARCH_GRACE_SECONDS` after the first catalog that answered with
    titles, and after `_SEARCH_DEADLINE_SECONDS` in any case: one slow
    catalog no longer holds results that are already in. The busy dialog names
    the first catalog still outstanding and shows how many have answered;
    a cancel sets `report.canceled` and leaves the stragglers out - the
    caller still gets whatever had landed, the same "stop waiting" meaning
    Back has in the streams fan-out."""
    from lib.ui.compat import L

    total = len(jobs)
    answered = {}
    dialog.update(0, L(30186) % (jobs[0][1].get('name') or '?'))
    monitor = xbmc.Monitor()
    started = time.monotonic()
    first_hit = None
    while len(answered) < total:
        if dialog.iscanceled():
            report.canceled = True
            break
        now = time.monotonic()
        if (first_hit is not None and now - first_hit >= _SEARCH_GRACE_SECONDS) or \
                now - started >= _SEARCH_DEADLINE_SECONDS:
            # Titles are in hand (or the wait is over): show them. The
            # catalogs still out are reported as unanswered.
            break
        try:
            index, answer = results.get(timeout=_SEARCH_POLL_SECONDS)
        except queue.Empty:
            # A blocked queue read never reaches Kodi, which only hands a
            # script its queued Back press from inside a Monitor call -
            # without this the dialog's iscanceled() above stayed False
            # until the next answer happened to land (see
            # uicommon.deliver_queued_input()).
            if deliver_queued_input(monitor):
                report.canceled = True  # Kodi is shutting down
                break
            continue
        answered[index] = answer
        if first_hit is None and answer and answer[0]:
            first_hit = time.monotonic()
        outstanding = next((i for i in range(total) if i not in answered), None)
        if outstanding is not None:
            dialog.update(
                int(len(answered) * 100 / total),
                L(30186) % (jobs[outstanding][1].get('name') or '?'),
            )
    return answered


def _feed_wait_budget(started):
    """How long `run_query()` may still wait for the ranking feed: at most
    `_FEED_WAIT_SECONDS`, and never past `_SEARCH_DEADLINE_SECONDS` counted
    from `started` (the fan-out's start), so a search that already ran into
    the deadline does not keep the spinner up for the feed on top of it."""
    remaining = started + _SEARCH_DEADLINE_SECONDS - time.monotonic()
    return max(0.0, min(_FEED_WAIT_SECONDS, remaining))

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
        monitor = xbmc.Monitor()
        while not self._done.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0 or dialog.iscanceled():
                break
            self._done.wait(min(_SEARCH_POLL_SECONDS, remaining))
            deliver_queued_input(monitor)
        return self.index if self._done.is_set() else None


def run_query(store, client, query, report=None, progress=None):
    """Fan `query` across every search-capable catalog
    (`iter_catalogs(..., extra_required='search')`) and return the
    collected metas, each with `type` defaulted from its catalog.
    The fan-out (progress dialog via `busy_dialog`/L(30186), per-addon
    `AddonError` isolation and logging included) lives here so every
    window can run a search without opening one - callers are
    `search.SearchWindow` and the person branch of
    `lib.ui.infowindow.open_credit_link()`.
    Writes no history, opens no coverflow - callers own
    both.

    `progress` replaces the modal spinner: any object with
    `iscanceled()` and `update(percent, message)`. The Search screen's
    search-as-you-type passes one that reports on its status line and
    cancels once the bar no longer holds `query`, because a modal spinner
    would take the keyboard away from the bar mid-word (and every letter
    then runs through Kodi's keymap, where "s" opens the shutdown menu).

    The catalogs are queried CONCURRENTLY (`_MAX_SEARCH_WORKERS`), not one
    after another, and answers are put back in catalog order before
    anything is merged, so which copy of a duplicate title wins does not
    depend on which request happened to be fastest. Back is polled while
    requests are in flight (`_collect_answers()`); a cancelled search
    still returns what had arrived.

    Pass a `SearchReport` as `report` to learn how the fan-out went
    (nothing enabled can search / every catalog failed / some failed /
    cancelled) and to own the user-facing message - `SearchWindow`
    does, so it can say "search failed" where it would
    otherwise say "no results". Without one, `run_query()` shows that
    notification itself (`_search_notice()`), so a caller that never
    heard of reports still stops presenting an
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
    falls out as an empty `metas` list, which each caller's existing
    no-results branch treats as an ordinary no-results search.

    The collected metas are deduplicated (`_dedupe()`) and then ordered
    by how well each title matches the query (`_rank_by_title()`), after
    the existing credit ranking. Both callers want that: a person
    link (`infowindow.open_credit_link()`) fans out the same way and gets
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
        with (nullcontext(progress) if progress is not None else busy_dialog(L(30033), query)) as dialog:
            started = time.monotonic()
            loader = _FeedIndexLoader(store, client)
            stop = threading.Event()
            results = _start_search_workers(client, query, jobs, stop)
            try:
                answered = _collect_answers(dialog, results, jobs, report)
            finally:
                stop.set()  # whatever is still queued is not wanted any more
            if not report.canceled:
                feed_index = loader.wait(dialog, timeout=_feed_wait_budget(started))
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
    from lib.ui import searchfeed

    # One loader at a time: a second search waits for the first one's
    # load (or fetch) and then takes its memo instead of repeating it.
    with _feed_lock:
        stamp = _feed_file_stamp(searchfeed._path(data_dir))
        if stamp is not None and _feed_memo.get('stamp') == stamp and \
                time.time() - stamp[0] <= searchfeed.TTL_SECONDS:
            return _feed_memo.get('index')
        if stamp is None and time.monotonic() - _feed_memo.get('failed_at', -_FEED_RETRY_SECONDS) < _FEED_RETRY_SECONDS:
            # The last fetch failed (offline, blocked host): ranking does
            # without the feed rather than every typed pause queueing
            # another 30s download attempt behind this lock.
            return None
        records = searchfeed.load_records(data_dir, session)
        index = searchfeed.build_index(records) if records else None
        _feed_memo.clear()
        _feed_memo.update(stamp=_feed_file_stamp(searchfeed._path(data_dir)), index=index)
        if index is None:
            _feed_memo['failed_at'] = time.monotonic()
        return index


#: The built feed index, kept for the life of the plugin process and
#: keyed on the cache file's `(mtime, size)`. Reading the ~3.7MB file
#: back and indexing its ~20k records took a measurable bite out of
#: every search; with search as you type that is every pause in the
#: typing, so it is done once per file instead. Reset by tests through
#: `tests/conftest.py`'s `_fresh_process_state`.
_feed_memo = {}  # type: dict

#: After a feed load that came back empty, how long searches skip it.
_FEED_RETRY_SECONDS = 600.0
_feed_lock = threading.Lock()


def _feed_file_stamp(path):
    try:
        info = os.stat(path)
    except OSError:
        return None
    return (info.st_mtime, info.st_size)
