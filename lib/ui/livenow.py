# SPDX-FileCopyrightText: 2026 M0Rf30
# SPDX-License-Identifier: MIT

"""Now/next for the focused live channel, and the "Recent channels" row.

What the hero of a live item shows (Home's Live tab, the coverflow, the
Search row): the CURRENT programme with its time span and progress, its
overview as the plot, and the NEXT programme. The rules are
stremio-core 0.66.0's, through `lib.livetv`: `programs_at`,
`LIVE_SCHEDULE_REFRESH_INTERVAL` (15 min, 1 min once the schedule's
coverage passed or a fetch failed) in `models/meta_details.rs`, and the
guide in `models/live_tv_guide.rs`.

Where the schedule comes from, in order:

1. The row's add-on guide catalog (`lib.stremio.addons.guide_catalog_for`:
   a `date` extra on an `epgProvider` add-on). One request per UTC date
   overlapping the local day (`livetv.guide_dates`) answers the programmes
   of a WHOLE page of channels (`metasDetailed`), merged per channel with
   `livetv.guide_videos`; every other channel of that page reuses it, so
   scrolling a row costs one request per page, not one per channel. The
   guide page asked for is the channel catalog's own page (same `skip`):
   the engine's guide catalog lists channels "in catalog order, page size
   as the channel catalog".
2. The channel's own meta `videos` (`fetch_live_meta`): one request, asked
   of the epgProvider add-ons first and then the others
   (`ordered_meta_addons`; stremio-core prefers the add-on that served the
   last stream, which Rivulet does not track, then an epgProvider).
   Also the fallback when the guide page lacks the channel or failed.

`NowNextFeed` is the single worker thread a window starts for this. It
holds plain data only (no window, no `ListItem`): the window asks for the
focused channel (`want`), the worker fetches and refreshes on the
15 minute / 1 minute schedule and wakes the window (`wake`, an
`Action(noop)`), and the GUI thread reads what is cached (`snapshot`) and
writes it onto the item (`apply_to_item`). That is the same split the
coverflow's enrich worker and Home's library-meta worker use, and what
`home.release_items` relies on: a worker never owns a native item.

Live metas are never persisted by `lib.ui.metacache` (a 30 minute disk
TTL would outlive the 15 minute refresh and show yesterday's programme);
this module keeps schedules in memory for the life of the feed.

Everything above `NowNextFeed` is pure (no Kodi), so the formatting and
the ordering rules are tested without stubs.
"""
import math
import threading
import time

from lib import livetv

#: The longest a programme line's title runs before it is cut: the hero's
#: facts line is one non-wrapping label (HomeWindow.xml, 1200 px at font13).
TITLE_LIMIT = 40

#: Seconds the worker sleeps with nothing to do or between progress ticks.
#: Progress is a whole percent, so a minute is the finest change worth a
#: repaint.
TICK_SECONDS = 60.0


def _clip(text, limit=TITLE_LIMIT):
    text = ' '.join(str(text or '').split())
    if len(text) <= limit:
        return text
    return text[:limit - 1].rstrip() + '\u2026'


def _lines(info, fmt, now_template, next_template, clean):
    """``(now line, next line)`` of `info`, '' where there is no such
    programme: `playbackmeta.live_programme_lines`, the wording the player's
    OSD already uses (``Now: 20:00 - 21:00 Title`` / ``Next: 21:00
    Title``), with titles cut to `TITLE_LIMIT` and escaped by `clean`."""
    from lib.ui.playbackmeta import live_programme_lines

    def wrap(template):
        return lambda text: template % clean(text)

    def clipped(entry):
        return dict(entry, title=_clip(entry.get('title'))) if entry else None

    current, upcoming = clipped(info.get('current')), clipped(info.get('next'))
    now_lines = live_programme_lines({'current': current}, wrap(now_template), wrap(next_template), fmt)
    next_lines = live_programme_lines({'next': upcoming}, wrap(now_template), wrap(next_template), fmt)
    return (now_lines[0] if now_lines else '', next_lines[0] if next_lines else '')


def compose(info, fmt, now_template, next_template, clean=None, now=None):
    """The hero strings for `info` (`livetv.now_next`), as a dict:

    * `hero_now` -- ``Now: 21:00 - 22:00 Title`` or '';
    * `hero_next` -- ``Next: 22:00 Title`` or '';
    * `hero_progress` -- ``'45'`` (0-100) or '';
    * `hero_meta` -- the facts panel: the now line and ``45%``; with no
      current programme, the next line instead;
    * `hero_plot` -- the next line, then the current overview (the next
      line leads so it stays on screen: the plot box is three lines and
      scrolls). '' when there is neither; the caller keeps the channel's
      own description in that case (`apply_to_item`);
    * `hero_runtime` -- whole minutes left in the current programme, which
      the hero already turns into "Ends at HH:MM" (`look.ends_at`).

    `fmt` is the clock format (``look._clock_format()``); `clean` escapes
    add-on authored text for a label (`escape_label`); `now` is epoch
    seconds (default: the clock).
    """
    clean = clean or (lambda text: text)
    now = time.time() if now is None else now
    out = {'hero_now': '', 'hero_next': '', 'hero_progress': '', 'hero_meta': '',
           'hero_plot': '', 'hero_runtime': ''}
    if not info:
        return out
    current = info.get('current')
    out['hero_now'], out['hero_next'] = _lines(info, fmt, now_template, next_template, clean)
    if current:
        if current.get('progress') is not None:
            out['hero_progress'] = str(int(current['progress']))
        remaining = math.ceil((current['end'] - now) / 60.0)
        out['hero_runtime'] = str(remaining) if remaining > 0 else ''
    facts = [out['hero_now'] if current else out['hero_next']]
    if out['hero_progress']:
        facts.append('%s%%' % out['hero_progress'])
    out['hero_meta'] = '\n'.join(line for line in facts if line)
    overview = clean(current['overview']) if current and current.get('overview') else ''
    out['hero_plot'] = '\n'.join(line for line in (out['hero_next'] if current else '', overview) if line)
    return out


#: Item properties `apply_to_item` takes over, and the copy of each the
#: item had before, so a channel whose programme ended (no schedule
#: any more) gets its catalog description back and a refresh never stacks
#: the next line onto an already composed plot.
_COMPOSED = ('hero_meta', 'hero_plot', 'hero_runtime', 'hero_facts')
_FEED_PROPS = ('hero_now', 'hero_next', 'hero_progress')


def signature(info):
    """What of `info` a paint depends on, comparable. None for no schedule."""
    if not info:
        return None
    current, upcoming = info.get('current'), info.get('next')
    return (
        (current['title'], current['start'], current['end'], current['progress'], current['overview'])
        if current else None,
        (upcoming['title'], upcoming['start'], upcoming['end']) if upcoming else None,
    )


def apply_to_item(item, info, fmt, now_template, next_template, clean=None, now=None):
    """Write `info` onto `item` (GUI thread). True when anything changed.

    The hero reads `hero_meta`/`hero_plot`/`hero_runtime` (Home's
    `paint_hero`, `look.paint_poster_hero`, the coverflow's hero all go
    through them), so composing into those is what makes every hero show
    now/next; `hero_now`/`hero_next`/`hero_progress` carry the raw lines
    for a skin that wants to place them. `hero_facts` is cleared while the
    programme line owns the facts panel: the ratings overlay would rebuild
    the panel from cached scores and drop it (as `apply_episode_hero`
    does for an episode). With no schedule the item is restored.
    """
    sig = repr(signature(info))
    seen = item.getProperty('live_sig')
    if seen == sig:
        return False
    if not seen:
        # First write on this item (or the first after `reset_item`): the
        # catalog's own values are the base to restore.
        for key in _COMPOSED:
            item.setProperty('live_base_' + key, item.getProperty(key) or '')
    composed = compose(info, fmt, now_template, next_template, clean, now)
    for key in _FEED_PROPS:
        item.setProperty(key, composed[key])
    for key in _COMPOSED:
        base = item.getProperty('live_base_' + key)
        if key == 'hero_facts':
            value = '' if info else base
        elif key == 'hero_runtime':
            value = composed['hero_runtime'] if info else base
        else:
            value = composed[key] or base
        item.setProperty(key, value)
    item.setProperty('live_sig', sig)
    return True


def reset_item(item):
    """Forget what `apply_to_item` composed. Called after something else
    rewrote the item's hero properties (the coverflow's meta enrichment,
    Home's library-meta merge): the next `apply_to_item` then takes THOSE
    values as the base and composes again, instead of believing its own
    now/next is still on the item."""
    item.setProperty('live_sig', '')


#: Localized templates ``Now: %s`` / ``Next: %s``, the same strings the
#: player's OSD info panel uses for a playing channel (`lib.ui.player`).
NOW_STRING_ID = 30653
NEXT_STRING_ID = 30654


def track_item(feed, item, guide=None):
    """GUI thread, once per hero paint: ask `feed` for the focused item's
    schedule and write what it has so far onto the item BEFORE the hero
    reads it. True when the item changed. A non-live item (or none) tells
    the feed nothing is wanted, which idles its worker.

    The one place Home, the coverflow and Search all go through, so a
    live card behaves the same wherever it is focused.
    """
    if item is None or item.getProperty('live') != '1' or not item.getProperty('sid'):
        feed.want()
        return False
    sid = item.getProperty('sid')
    feed.want(item.getProperty('stype') or 'tv', sid, guide)
    known, info = feed.snapshot(sid)
    if not known:
        return False
    from lib.ui import look
    from lib.ui.compat import L
    from lib.ui.uicommon import escape_label

    return apply_to_item(item, info, look._clock_format(), L(NOW_STRING_ID), L(NEXT_STRING_ID), escape_label)


# ---------------------------------------------------------------- sources

def ordered_meta_addons(addons, stype, sid):
    """``[(transport_url, manifest)]`` of the add-ons that serve a meta for
    `stype`/`sid`: the `epgProvider` ones first (the schedule is what they
    exist for), the rest after, each group in the user's add-on order."""
    from lib.stremio.addons import addon_supports, iter_manifests

    providers, others = [], []
    for transport, manifest in iter_manifests(addons):
        if not isinstance(transport, str) or not transport:
            continue
        if not addon_supports(manifest, 'meta', stype, sid):
            continue
        (providers if livetv.is_epg_provider(manifest) else others).append((transport, manifest))
    return providers + others


def page_skip(extra):
    """The `skip` of a catalog request's `extra` (``[(name, value), ...]``),
    0 when it has none or an unusable one."""
    for name, value in extra if isinstance(extra, (list, tuple)) else ():
        if name == 'skip':
            try:
                return max(int(value), 0)
            except (TypeError, ValueError):
                return 0
    return 0


def guide_extra(date, skip=0):
    """The extra of a guide request: ``date=YYYY-MM-DD`` (and ``skip``)."""
    extra = [(livetv.DATE_EXTRA, date)]
    if skip:
        extra.append(('skip', int(skip)))
    return extra


def fetch_guide_page(source, dates):
    """The metas of one guide page, one request per UTC date in `dates`.

    `source` is ``(transport_url, type, catalog id, skip)``. The caller
    runs this off the GUI thread; a failing date raises (`AddonError`), so
    the page counts as failed and is retried in a minute. A guide that
    answers plain `metas` rather than `metasDetailed` yields no programmes
    (stremio-core lifts it into metas without videos): `catalog_detailed`
    reads `metasDetailed` only and answers [] for it, never an error.
    """
    from lib.ui.dependencies import get_client

    transport, ctype, cid, skip = source
    client = get_client()
    metas = []
    for date in dates:
        metas.extend(client.catalog_detailed(transport, ctype, cid, guide_extra(date, skip)))
    return metas


def fetch_live_meta(stype, sid):
    """The meta of a live channel with its schedule, or None. The first
    add-on (`ordered_meta_addons`) whose meta carries `videos` wins; the
    first usable meta without them is the fallback."""
    from lib.stremio.addons import AddonError
    from lib.ui.dependencies import get_client, get_store

    client = get_client()
    best = None
    for transport, _manifest in ordered_meta_addons(get_store().get_enabled_addons(), stype, sid):
        try:
            meta = client.meta(transport, stype, sid)
        except AddonError:
            continue
        if not isinstance(meta, dict):
            continue
        if meta.get('videos'):
            return meta
        best = best or meta
    return best


def _local_offset(now):
    """Seconds the local zone is east of UTC at `now` (DST included)."""
    try:
        return time.localtime(now).tm_gmtoff or 0
    except (OverflowError, OSError, ValueError, AttributeError):
        return -time.timezone


def next_boundary(videos, now):
    """The next epoch second at which `livetv.now_next(videos, now)` changes
    by itself (a programme starts or ends), or None."""
    edges = []
    for video in videos or ():
        if not isinstance(video, dict):
            continue
        start = livetv.parse_time(video.get('startTime'))
        end = livetv.parse_time(video.get('endTime'))
        if start is None or end is None:
            continue
        edges.extend(edge for edge in (start, end) if edge > now)
    return min(edges) if edges else None


class NowNextFeed:
    """The worker behind a window's live hero. See the module docstring.

    `fetch_page(source, dates)` -> metas and `fetch_meta(stype, sid)` ->
    meta are the two network calls (injected so tests need none); `wake()`
    nudges the GUI thread to repaint; `should_stop()` is the window's
    cooperative stop (its session event and Kodi's abort). Plain data
    only: never give this object a window or an item.
    """

    def __init__(self, fetch_page, fetch_meta, wake, should_stop=None, on_exit=None,
                 clock=time.time, mono=time.monotonic, utc_offset=_local_offset):
        self._fetch_page = fetch_page
        self._fetch_meta = fetch_meta
        self._wake_ui = wake
        self._should_stop = should_stop or (lambda: False)
        self._on_exit = on_exit
        self._clock = clock
        self._mono = mono
        self._utc_offset = utc_offset
        self._lock = threading.Lock()
        self._nudge = threading.Event()
        self._stop = threading.Event()
        self.thread = None
        self._wanted = None
        self._schedules = {}
        self._pages = {}
        self._announced = {}

    @property
    def stopped(self):
        """Over for good: `stop()` was called or the owner's stop fired.
        A window builds a fresh feed when it finds this set."""
        return self._stopped()

    def want(self, stype=None, sid=None, guide=None):
        """The channel whose schedule the window shows now (None: no live
        item focused). `guide` is ``(transport, type, catalog id, skip)``
        of the guide catalog serving the channel's row, or None."""
        wanted = (stype, sid, guide) if sid else None
        with self._lock:
            if wanted == self._wanted:
                return
            self._wanted = wanted
            self._announced.pop(sid, None)
        if wanted is None:
            return
        self._ensure_started()
        self._nudge.set()

    def snapshot(self, sid, now=None):
        """``(known, info)``: whether a schedule for `sid` has been fetched
        and `livetv.now_next` of it at `now` (None: nothing to show)."""
        with self._lock:
            entry = self._schedules.get(sid)
            videos = entry['videos'] if entry else None
        if entry is None:
            return False, None
        return True, livetv.now_next(videos, self._clock() if now is None else now)

    def stop(self):
        self._stop.set()
        self._nudge.set()

    def _ensure_started(self):
        with self._lock:
            if self.thread is not None or self._stop.is_set():
                return
            self.thread = threading.Thread(target=self._run, name='rivulet-live-now', daemon=True)
            thread = self.thread
        thread.start()

    def _stopped(self):
        return self._stop.is_set() or bool(self._should_stop())

    def _run(self):
        try:
            while not self._stopped():
                with self._lock:
                    wanted = self._wanted
                delay = TICK_SECONDS
                if wanted is not None:
                    sid = wanted[1]
                    if self._due_in(sid) <= 0:
                        self._refresh(wanted)
                        if self._stopped():
                            break  # the window is gone: nothing to wake
                    self._announce(sid)
                    delay = self._sleep_for(sid)
                self._nudge.wait(delay)
                self._nudge.clear()
        finally:
            if self._on_exit is not None:
                self._on_exit()

    def _due_in(self, sid):
        with self._lock:
            entry = self._schedules.get(sid)
        return 0.0 if entry is None else entry['next'] - self._mono()

    def _sleep_for(self, sid):
        with self._lock:
            entry = self._schedules.get(sid)
        delay = TICK_SECONDS
        if entry is not None:
            delay = min(delay, entry['next'] - self._mono())
            edge = next_boundary(entry['videos'], self._clock())
            if edge is not None:
                delay = min(delay, edge - self._clock() + 0.5)
        return max(delay, 0.5)

    def _announce(self, sid):
        """Wake the GUI when what the hero shows for `sid` changed."""
        known, info = self.snapshot(sid)
        if not known:
            return
        sig = signature(info)
        with self._lock:
            # No schedule on record is what the window already shows (the
            # catalog's own description), so it is the starting point.
            if self._announced.get(sid) == sig:
                return
            self._announced[sid] = sig
        try:
            self._wake_ui()
        except Exception:  # noqa: BLE001 - a repaint nudge must never end the worker
            pass

    def _refresh(self, wanted):
        stype, sid, guide = wanted
        now = self._clock()
        videos, failed = [], False
        if guide is not None:
            videos = self._guide_programmes(guide, sid, now)
        if not videos:
            try:
                meta = self._fetch_meta(stype, sid)
            except Exception:  # noqa: BLE001 - any failure is "try again in a minute"
                meta = None
            listed = meta.get('videos') if isinstance(meta, dict) else None
            videos = [video for video in listed if isinstance(video, dict)] if isinstance(listed, list) else []
            failed = meta is None
        with self._lock:
            previous = self._schedules.get(sid)
            if failed and previous is not None:
                videos = previous['videos']
            self._schedules[sid] = {
                'videos': videos,
                'next': self._mono() + livetv.refresh_interval(videos, now, failed),
            }

    def _guide_programmes(self, source, sid, now):
        """`sid`'s programmes from the guide page `source`, [] when the page
        failed or does not list the channel. A page is fetched again only
        once its own refresh time passed, so the channels of a row share it."""
        with self._lock:
            page = self._pages.get(source)
        if page is None or page['next'] <= self._mono():
            try:
                metas = self._fetch_page(source, livetv.guide_dates(now, self._utc_offset(now)))
            except Exception:  # noqa: BLE001
                metas = None
            if metas is None:
                page = {'by_id': page['by_id'] if page else {}, 'next': self._mono() + livetv.RETRY_SECONDS}
            else:
                ids = {meta.get('id') for meta in metas if isinstance(meta, dict) and meta.get('id')}
                by_id = {cid: livetv.guide_videos(metas, cid) for cid in ids}
                every = [video for videos in by_id.values() for video in videos]
                page = {'by_id': by_id, 'next': self._mono() + livetv.refresh_interval(every, now)}
            with self._lock:
                self._pages[source] = page
        return page['by_id'].get(sid) or []


def ensure_feed(feed):
    """`feed` while it still runs, else a fresh production feed: what a
    window keeps in its `_now_feed`, so a window reopened after playback
    (its feed stopped on close) gets a live one again."""
    return feed if feed is not None and not feed.stopped else new_feed()


def new_feed(should_stop=None):
    """A production feed: real fetches, and an `Action(noop)` as the wake
    (it reaches the active window's `onAction` on the GUI thread, as the
    other workers' do). `should_stop` defaults to Kodi's abort request."""
    from lib.ui import background
    from lib.ui.dependencies import get_client

    should_stop = should_stop or (lambda: background.monitor().abortRequested())

    def wake():
        import xbmc

        xbmc.executebuiltin('Action(noop)')

    def release():
        # Drop this thread's private requests session.
        release_session = getattr(get_client(), '_release_worker_session', None)
        if callable(release_session):
            release_session()

    return NowNextFeed(fetch_guide_page, fetch_live_meta, wake, should_stop, on_exit=release)


# ------------------------------------------------------------ recent row

def metas_from_records(records):
    """Card metas for the account's live library records
    (`library.recent_live_records`): id, type, name, poster, shape, and the
    live hint, so a card is square and one-click even when the record did
    not keep `behaviorHints`."""
    metas = []
    for record in records or ():
        if not isinstance(record, dict) or not record.get('_id'):
            continue
        metas.append(_recent_meta(record.get('_id'), record))
    return metas


def metas_from_local(channels):
    """Card metas for `Store.get_recent_channels()` entries."""
    return [_recent_meta(entry.get('id'), entry) for entry in channels or ()
            if isinstance(entry, dict) and entry.get('id')]


def _recent_meta(channel_id, source):
    hints = source.get('behaviorHints') if isinstance(source.get('behaviorHints'), dict) else {}
    meta = {'id': channel_id, 'type': source.get('type') or 'tv', 'name': source.get('name') or '',
            'poster': source.get('poster') or '', 'behaviorHints': dict(hints, isLive=True)}
    if source.get('posterShape'):
        meta['posterShape'] = source['posterShape']
    return meta
