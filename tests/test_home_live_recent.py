# SPDX-FileCopyrightText: 2026 M0Rf30
# SPDX-License-Identifier: MIT

"""Home's Live tab: the Recent channels row, its removal, and now/next.

The row follows stremio-core's `LiveTvContinueWatching` (live records of
the account's library, newest `lastWatched` first) when the account syncs,
and this device's `recent_channels.json` otherwise. A live channel never
appears on Continue.
"""
import threading
import time

import pytest

from lib import homelayout
from lib.ui import livenow
from tests.test_home import load_homewindow as load_homewindow  # noqa: F401 - fixture
from tests.test_home_tabs import build

AUTH = {'authKey': 'k'}


def _live_addons(guide=False):
    catalogs = [{'type': 'tv', 'id': 'iptv0', 'name': 'Live TV', 'extra': [{'name': 'skip'}]}]
    manifest = {'name': 'IPTV', 'types': ['tv'], 'catalogs': catalogs}
    if guide:
        manifest['behaviorHints'] = {'epgProvider': True}
        catalogs.append({'type': 'tv', 'id': 'iptv0-guide', 'name': 'Guide', 'extra': [{'name': 'date'}]})
    return [{'transportUrl': 'https://live/manifest.json', 'manifest': manifest}]


def _stamp(offset):
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time() + offset))


def _record(cid, when, **extra):
    record = {'_id': cid, 'type': 'tv', 'name': 'Chan %s' % cid, 'poster': 'p', 'removed': True, 'temp': True,
              'state': {'lastWatched': when, 'timeOffset': 0}}
    record.update(extra)
    return record


def _local(cid, when='2026-05-04T10:00:00Z'):
    return {'id': cid, 'type': 'tv', 'name': 'Local %s' % cid, 'poster': 'p', 'last_watched': when}


@pytest.fixture
def live_feed(monkeypatch):
    """Home's now/next worker over injected fetches: `calls` records the
    guide pages and metas asked for, `gate` holds the answers back."""
    class Probe:
        def __init__(self):
            self.pages, self.metas, self.feeds, self.videos = [], [], [], []
            self.gate, self.woke = threading.Event(), threading.Event()

    probe = Probe()

    def fetch_page(source, dates):
        probe.pages.append(source)
        probe.gate.wait(5.0)
        return [{'id': 'tt0', 'type': 'tv', 'videos': probe.videos}]

    def fetch_meta(stype, sid, transport=None, widen=True):
        probe.metas.append((stype, sid))
        probe.gate.wait(5.0)
        return {'id': sid, 'videos': probe.videos}

    def new_feed(should_stop=None):
        feed = livenow.NowNextFeed(fetch_page, fetch_meta, probe.woke.set, should_stop)
        probe.feeds.append(feed)
        return feed

    probe.videos = [
        {'id': 'a', 'title': 'Evening News', 'overview': 'Headlines', 'startTime': _stamp(-600),
         'endTime': _stamp(1200)},
        {'id': 'b', 'title': 'Late Film', 'startTime': _stamp(1200), 'endTime': _stamp(4800)},
    ]
    monkeypatch.setattr(livenow, 'new_feed', new_feed)
    yield probe
    probe.gate.set()
    for feed in probe.feeds:
        feed.stop()
        if feed.thread:
            feed.thread.join(5.0)


def _live_home(ctx, monkeypatch, recent=None, auth=None, library=None, installed=None):
    win, store, calls = build(ctx, monkeypatch, installed=installed or _live_addons(), auth=auth,
                              library=library)
    store._recent_channels = list(recent or [])
    ctx.home._switch_tab(win, 'live')
    return win, store, calls


def _sids(win, panel=30041):
    return [item.getProperty('sid') for item in win.getControl(panel).items]


# --------------------------------------------------------------------- plan

def test_the_live_tab_leads_with_the_recent_row_and_no_other_tab_does(load_homewindow, monkeypatch, live_feed):
    ctx = load_homewindow()
    win, _store, _calls = _live_home(ctx, monkeypatch, recent=[_local('c1'), _local('c2')])
    assert [state['kind'] for state in win._rows.values()] == ['recent', 'catalog']
    assert _sids(win) == ['c1', 'c2']
    assert win.getControl(30041).items[0].getProperty('live') == '1'
    assert _sids(win, 30042)[:2] == ['tt0', 'tt1']
    ctx.home._switch_tab(win, 'home')
    assert 'recent' not in [state['kind'] for state in win._rows.values()]


def test_the_recent_row_is_hidden_when_empty(load_homewindow, monkeypatch, live_feed):
    ctx = load_homewindow()
    win, _store, _calls = _live_home(ctx, monkeypatch, recent=[])
    assert win._rows[30041]['kind'] == 'recent' and win.getControl(30041).size() == 0
    assert not win.getProperty('shelf_slot0')
    assert win.getProperty('shelf_slot1') == '1'


def test_a_saved_layout_gets_the_recent_row_and_the_slots_still_fit(load_homewindow, monkeypatch, live_feed):
    ctx = load_homewindow()
    layout = homelayout.default_layout()
    installed = _live_addons()
    installed[0]['manifest']['catalogs'] = [
        {'type': 'tv', 'id': 'iptv%d' % i, 'name': 'Live %d' % i} for i in range(20)]
    layout['tabs']['live'] = ['rest:live']
    win, _, _ = build(ctx, monkeypatch, layout=layout, installed=installed)
    win._layout = layout
    ctx.home._switch_tab(win, 'live')
    kinds = [state['kind'] for state in win._rows.values()]
    assert len(kinds) == 12 and kinds[0] == 'recent' and kinds.count('catalog') == 11


def test_the_recent_row_follows_the_account_newest_watched_first(load_homewindow, monkeypatch, live_feed):
    ctx = load_homewindow()
    library = [
        _record('old', '2026-05-01T10:00:00Z'),
        _record('new', '2026-05-04T10:00:00Z'),
        _record('rewound', None),  # dismissed: lastWatched null
        _record('gone', '2026-05-05T10:00:00Z', temp=False),  # removed from the library for good
        {'_id': 'tt9', 'type': 'movie', 'name': 'Film', 'removed': True, 'temp': True,
         'state': {'lastWatched': '2026-05-06T10:00:00Z'}},
    ]
    win, _store, _ = _live_home(ctx, monkeypatch, auth=AUTH, library=library, recent=[_local('device')])
    assert _sids(win) == ['new', 'old']  # the account's, not this device's file


def test_with_progress_sync_off_the_row_is_this_devices_list(load_homewindow, monkeypatch, live_feed):
    ctx = load_homewindow()
    ctx.compat.ADDON.setSetting('sync_progress', 'false')
    library = [_record('acct', '2026-05-04T10:00:00Z')]
    win, _store, _ = _live_home(ctx, monkeypatch, auth=AUTH, library=library, recent=[_local('device')])
    assert _sids(win) == ['device']


def test_a_failed_library_read_falls_back_to_the_devices_list(load_homewindow, monkeypatch, live_feed):
    ctx = load_homewindow()
    win, store, _ = build(ctx, monkeypatch, installed=_live_addons(), auth=AUTH)
    monkeypatch.setattr(ctx.home, '_fetch_library', lambda *_: None)
    store._recent_channels = [_local('device')]
    win._library_entries = None  # Home's own load had no records to keep
    ctx.home._switch_tab(win, 'live')
    assert _sids(win) == ['device']


def test_a_signed_out_home_reads_no_library_for_the_recent_row(load_homewindow, monkeypatch, live_feed):
    ctx = load_homewindow()
    win, store, _ = build(ctx, monkeypatch, installed=_live_addons())
    reads = []
    monkeypatch.setattr(ctx.home, '_fetch_library', lambda *_: reads.append(1) or [])
    store._recent_channels = [_local('c1')]
    win._library_entries = None
    ctx.home._switch_tab(win, 'live')
    assert reads == [] and _sids(win) == ['c1']


def test_coming_back_from_a_channel_refreshes_the_row(load_homewindow, monkeypatch, live_feed):
    ctx = load_homewindow()
    win, store, _ = _live_home(ctx, monkeypatch, recent=[_local('c1')])
    store._recent_channels = [_local('c2'), _local('c1')]
    ctx.home._refresh_saved_rows(win, store)
    assert _sids(win) == ['c2', 'c1']
    # A tab left behind and revisited shows the fresh row too.
    ctx.home._switch_tab(win, 'home')
    store._recent_channels = [_local('c3')]
    ctx.home._refresh_saved_rows(win, store)
    ctx.home._switch_tab(win, 'live')
    assert _sids(win) == ['c3']


# ------------------------------------------------------------------ removal

def _menu(ctx, monkeypatch, win, item, panel):
    from lib.ui import libraryaction

    menus = []
    monkeypatch.setattr(libraryaction, 'poster_menu', lambda meta, extra: menus.append(extra))
    ctx.home._offer_library(win, item, panel)
    return menus[-1]


def test_only_the_recent_row_offers_the_removal(load_homewindow, monkeypatch, live_feed):
    ctx = load_homewindow()
    win, _store, _ = _live_home(ctx, monkeypatch, recent=[_local('c1')])
    assert [label for label, _ in _menu(ctx, monkeypatch, win, win.getControl(30041).items[0], 30041)] == ['STR30671']
    assert 'STR30671' not in [label for label, _ in _menu(ctx, monkeypatch, win, win.getControl(30042).items[0], 30042)]


def test_removing_a_recent_channel_signed_out_forgets_it_locally(load_homewindow, monkeypatch, live_feed):
    ctx = load_homewindow()
    win, store, _ = _live_home(ctx, monkeypatch, recent=[_local('c1'), _local('c2'), _local('c3')])
    control = win.getControl(30041)
    control.selectItem(1)
    win.setFocusId(30041)
    item = control.getSelectedItem()
    _menu(ctx, monkeypatch, win, item, 30041)[0][1]()
    assert [c['id'] for c in store.get_recent_channels()] == ['c1', 'c3']
    assert _sids(win) == ['c1', 'c3']
    assert [meta['id'] for meta in win._rows[30041]['metas']] == ['c1', 'c3']
    assert control.getSelectedPosition() == 1 and win.getFocusId() == 30041


def test_removing_the_last_recent_channel_hides_the_row_and_moves_focus(load_homewindow, monkeypatch, live_feed):
    ctx = load_homewindow()
    win, _store, _ = _live_home(ctx, monkeypatch, recent=[_local('c1')])
    win.setFocusId(30041)
    ctx.home._remove_recent(win, win.getControl(30041).items[0], 30041)
    assert win.getControl(30041).size() == 0 and not win.getProperty('shelf_slot0')
    assert win.getFocusId() == ctx.home.BROWSE_LIST


def test_removal_reaches_the_row_of_a_saved_tab_snapshot(load_homewindow, monkeypatch, live_feed):
    ctx = load_homewindow()
    win, store, _ = _live_home(ctx, monkeypatch, recent=[_local('c1'), _local('c2')])
    ctx.home._switch_tab(win, 'home')
    saved = [state for state in ctx.home._snapshot_only_states(win) if state['kind'] == 'recent']
    assert saved
    # The snapshot's own copy: removing from the live row must keep it in step.
    ctx.home._switch_tab(win, 'live')
    ctx.home._remove_recent(win, win.getControl(30041).items[0], 30041)
    ctx.home._switch_tab(win, 'home')
    for state in ctx.home._snapshot_only_states(win):
        if state['kind'] == 'recent':
            assert [it.getProperty('sid') for it in state['items']] == ['c2']


def test_removing_a_recent_channel_signed_in_rewinds_the_account_record(load_homewindow, monkeypatch, live_feed):
    ctx = load_homewindow()
    from lib.ui import libraryaction

    library = [_record('c1', '2026-05-04T10:00:00Z'), _record('c2', '2026-05-03T10:00:00Z')]
    win, store, _ = _live_home(ctx, monkeypatch, auth=AUTH, library=library, recent=[_local('c1')])
    pushed = []
    monkeypatch.setattr(libraryaction, '_lookup', lambda auth, cid: (dict(library[0]), True))
    monkeypatch.setattr(libraryaction, 'push_write', lambda auth, payload: pushed.append(payload) or True)
    ctx.home._remove_recent(win, win.getControl(30041).items[0], 30041)
    (record,) = pushed
    assert record['_id'] == 'c1' and record['state']['lastWatched'] is None and record['state']['timeOffset'] == 0
    assert _sids(win) == ['c2']
    assert store.get_recent_channels() == []  # the device's own entry goes too


def test_a_failed_account_write_keeps_the_channel(load_homewindow, monkeypatch, live_feed):
    ctx = load_homewindow()
    from lib.ui import libraryaction

    library = [_record('c1', '2026-05-04T10:00:00Z')]
    win, store, _ = _live_home(ctx, monkeypatch, auth=AUTH, library=library, recent=[_local('c1')])
    monkeypatch.setattr(libraryaction, '_lookup', lambda auth, cid: (dict(library[0]), True))
    monkeypatch.setattr(libraryaction, 'push_write', lambda auth, payload: False)
    ctx.home._remove_recent(win, win.getControl(30041).items[0], 30041)
    assert _sids(win) == ['c1'] and [c['id'] for c in store.get_recent_channels()] == ['c1']


def test_dismiss_live_with_no_account_record_has_nothing_to_do(load_homewindow, monkeypatch):
    load_homewindow()
    from lib.ui import libraryaction

    monkeypatch.setattr(libraryaction, '_lookup', lambda auth, cid: (None, True))
    monkeypatch.setattr(libraryaction, 'push_write', lambda *a: pytest.fail('nothing to push'))
    assert libraryaction.dismiss_live(AUTH, 'c1') is True


def test_dismiss_live_reports_a_failed_lookup(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    from lib.ui import libraryaction

    monkeypatch.setattr(libraryaction, '_lookup', lambda auth, cid: (None, False))
    monkeypatch.setattr(libraryaction, 'push_write', lambda *a: pytest.fail('lookup failed'))
    assert libraryaction.dismiss_live(AUTH, 'c1') is False
    assert ctx.env.notifications and ctx.env.notifications[-1][1] == 'STR30302'


# ----------------------------------------------------------- Continue filters

def test_a_live_record_never_reaches_continue(load_homewindow):
    ctx = load_homewindow()
    entries = [
        {'_id': 'ch1', 'type': 'tv', 'name': 'One', 'poster': 'p', 'removed': True, 'temp': True,
         'state': {'timeOffset': 5000, 'duration': 10000, 'lastWatched': '2026-05-04T10:00:00Z'}},
        {'_id': 'ev1', 'type': 'events', 'name': 'Match', 'poster': 'p', 'behaviorHints': {'isLive': True},
         'removed': True, 'temp': True,
         'state': {'timeOffset': 5000, 'duration': 10000, 'lastWatched': '2026-05-04T10:00:00Z'}},
        {'_id': 'tt1', 'type': 'movie', 'name': 'Film', 'poster': 'p', 'removed': True, 'temp': True,
         'state': {'timeOffset': 5000, 'duration': 10000, 'lastWatched': '2026-05-04T10:00:00Z'}},
    ]
    assert [meta['id'] for meta in ctx.home._library_continue_metas(entries)] == ['tt1']


def test_a_live_progress_row_never_reaches_continue(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    progress = [
        {'type': 'tv', 'id': 'ch1', 'name': 'One', 'poster': 'p', 'position_ms': 20000, 'duration_ms': 100000},
        {'type': 'movie', 'id': 'tt1', 'name': 'Film', 'poster': 'p', 'position_ms': 20000, 'duration_ms': 100000},
    ]
    win, store, _ = build(ctx, monkeypatch, progress=progress)
    played = ctx.home._played_metas(store, [])
    assert [meta['id'] for meta in played] == ['tt1']


# ------------------------------------------------------------------- hero

def _act(win):
    """One action, as Kodi delivers the feed's `Action(noop)` wake."""
    import xbmcgui

    win.onAction(xbmcgui.Action(0))


def _focus_recent(win):
    win.setFocusId(30041)
    _act(win)


def test_a_focused_recent_channel_shows_now_and_next(load_homewindow, monkeypatch, live_feed):
    ctx = load_homewindow()
    win, _store, _ = _live_home(ctx, monkeypatch, recent=[_local('c1')])
    _focus_recent(win)
    assert 'Next: ' not in win.getControl(ctx.home.HERO_PLOT).text  # the schedule has not arrived
    live_feed.gate.set()
    assert live_feed.woke.wait(5.0)
    _act(win)
    assert live_feed.metas == [('tv', 'c1')] and live_feed.pages == []
    plot = win.getControl(ctx.home.HERO_PLOT).text
    meta = win.getControl(ctx.home.HERO_META).text
    assert plot.startswith('Next: ') and 'Late Film' in plot and plot.endswith('Headlines')
    assert meta.startswith('Now: ') and 'Evening News' in meta and meta.endswith('%')


def test_a_catalog_row_asks_its_guide_catalog_with_the_rows_page_skip(load_homewindow, monkeypatch, live_feed):
    ctx = load_homewindow()
    win, _store, _ = _live_home(ctx, monkeypatch, installed=_live_addons(guide=True))
    win.setFocusId(30042)
    _act(win)
    live_feed.gate.set()
    assert live_feed.woke.wait(5.0)
    # The guide page of the channel's own catalog; no per-channel meta.
    assert live_feed.pages == [('https://live/manifest.json', 'tv', 'iptv0-guide', 0)]
    assert live_feed.metas == []
    _act(win)
    assert 'Late Film' in win.getControl(ctx.home.HERO_PLOT).text


def test_focusing_a_non_live_item_asks_for_nothing(load_homewindow, monkeypatch, live_feed):
    ctx = load_homewindow()
    win, _, _ = build(ctx, monkeypatch)
    win.setFocusId(30041)
    _act(win)
    assert live_feed.feeds == [] or all(feed.thread is None for feed in live_feed.feeds)


def test_closing_home_stops_the_now_next_worker(load_homewindow, monkeypatch, live_feed):
    ctx = load_homewindow()
    win, _store, _ = _live_home(ctx, monkeypatch, recent=[_local('c1')])
    _focus_recent(win)
    feed = live_feed.feeds[0]
    live_feed.gate.set()
    ctx.home._stop_workers(win)
    ctx.home._join_workers(win)
    assert feed.stopped and not feed.thread.is_alive()
    ctx.home.release_items(win)
    assert win._now_feed is None


def test_a_library_meta_merge_lets_the_programme_be_composed_again(load_homewindow, monkeypatch, live_feed):
    """`_write_meta_onto` rewrites the hero properties; without
    `livenow.reset_item` the item still claims its now/next is on it, and
    the programme never comes back over the catalog text."""
    ctx = load_homewindow()
    item = ctx.home._poster_item({'id': 'c1', 'type': 'tv', 'name': 'One', 'description': 'Channel blurb'})
    from lib import livetv

    now = time.time()
    videos = [{'id': 'a', 'title': 'Evening News', 'startTime': _stamp(-600), 'endTime': _stamp(1200)}]
    current = livetv.now_next(videos, now)
    args = ('%H:%M', 'Now: %s', 'Next: %s')
    assert livenow.apply_to_item(item, current, *args, now=now)
    ctx.home._write_meta_onto(item, {'id': 'c1', 'type': 'tv', 'name': 'One', 'description': 'Fresh blurb'})
    assert item.getProperty('hero_plot') == 'Fresh blurb'  # the merge overwrote the composed plot
    assert livenow.apply_to_item(item, current, *args, now=now)  # ... so it is composed again
    assert 'Evening News' in item.getProperty('hero_meta')
