# SPDX-FileCopyrightText: 2026 M0Rf30
# SPDX-License-Identifier: MIT

"""The live hero's now/next: composition, the item write, and the worker.

`lib.ui.livenow` is Kodi-free at import, so the pure parts run on plain
dicts and a fake item; the worker runs with injected fetches and a fixed
clock. Rules come from stremio-core 0.66.0 (`MetaItem::programs_at`,
`live_schedule_refresh_update` in meta_details.rs).
"""
import threading
import time

import pytest

from lib import livetv
from lib.ui import livenow

NOW = 1_700_000_000.0
FMT = '%H:%M'


def _stamp(epoch):
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(epoch))


def _programme(pid, start, end, title, overview=''):
    return {'id': pid, 'title': title, 'overview': overview,
            'startTime': _stamp(start), 'endTime': _stamp(end)}


def _schedule():
    return [_programme('a', NOW - 600, NOW + 1200, 'News', 'Headlines of the day'),
            _programme('b', NOW + 1200, NOW + 3000, 'Film')]


def _info():
    return livetv.now_next(_schedule(), NOW)


class FakeItem:
    def __init__(self, **props):
        self.props = dict(props)

    def getProperty(self, key):
        return self.props.get(key, '')

    def setProperty(self, key, value):
        self.props[key] = value


def _local(epoch):
    return time.strftime(FMT, time.localtime(epoch))


# ---------------------------------------------------------------- compose

def test_compose_lines_progress_and_runtime():
    out = livenow.compose(_info(), FMT, 'Now: %s', 'Next: %s', now=NOW)
    assert out['hero_now'] == 'Now: %s - %s News' % (_local(NOW - 600), _local(NOW + 1200))
    assert out['hero_next'] == 'Next: %s Film' % _local(NOW + 1200)
    assert out['hero_progress'] == '33'  # 10 of 30 minutes
    assert out['hero_runtime'] == '20'
    # The facts panel: the programme line and the progress; the next line
    # leads the plot, then the programme's own overview.
    assert out['hero_meta'] == out['hero_now'] + '\n33%'
    assert out['hero_plot'] == out['hero_next'] + '\nHeadlines of the day'


def test_compose_without_a_current_programme_shows_the_next_one():
    info = livetv.now_next([_programme('b', NOW + 600, NOW + 1800, 'Film')], NOW)
    out = livenow.compose(info, FMT, 'Now: %s', 'Next: %s', now=NOW)
    assert out['hero_now'] == '' and out['hero_progress'] == '' and out['hero_runtime'] == ''
    assert out['hero_meta'] == out['hero_next'] == 'Next: %s Film' % _local(NOW + 600)
    assert out['hero_plot'] == ''


def test_compose_nothing_is_empty():
    assert set(livenow.compose(None, FMT, 'Now: %s', 'Next: %s').values()) == {''}


def test_compose_cuts_and_escapes_the_add_on_text():
    long = _programme('a', NOW - 60, NOW + 600, 'T' * 80, 'x[B]y')
    info = livetv.now_next([long], NOW)
    out = livenow.compose(info, FMT, 'Now: %s', 'Next: %s', clean=lambda text: text.replace('[', '('), now=NOW)
    assert out['hero_now'].count('T') == livenow.TITLE_LIMIT - 1
    assert out['hero_now'].endswith('\u2026')
    assert out['hero_plot'] == 'x(B]y'


# ------------------------------------------------------------ apply_to_item

def _apply(item, info, now=NOW):
    return livenow.apply_to_item(item, info, FMT, 'Now: %s', 'Next: %s', now=now)


def test_apply_writes_the_hero_properties_and_keeps_the_originals():
    item = FakeItem(hero_meta='2024 • Drama', hero_plot='Channel blurb', hero_facts='8.1', hero_runtime='')
    assert _apply(item, _info()) is True
    assert item.getProperty('hero_now').startswith('Now: ')
    assert item.getProperty('hero_progress') == '33'
    assert item.getProperty('hero_runtime') == '20'
    assert item.getProperty('hero_facts') == ''  # the ratings overlay must not replace the line
    assert item.getProperty('hero_meta') == item.getProperty('hero_now') + '\n33%'
    assert item.getProperty('live_base_hero_plot') == 'Channel blurb'


def test_apply_is_a_no_op_for_an_unchanged_schedule_and_never_stacks():
    item = FakeItem(hero_plot='Channel blurb')
    assert _apply(item, _info()) is True
    written = dict(item.props)
    assert _apply(item, _info()) is False
    assert item.props == written
    # A later minute changes the progress: composed again from the base,
    # not from the plot already composed.
    later = livetv.now_next(_schedule(), NOW + 600)
    assert _apply(item, later, now=NOW + 600) is True
    assert item.getProperty('hero_plot').count('Next: ') == 1


def test_apply_without_a_schedule_restores_the_catalog_values():
    item = FakeItem(hero_meta='2024 • Drama', hero_plot='Channel blurb', hero_facts='8.1', hero_runtime='90')
    _apply(item, _info())
    assert _apply(item, None) is True
    assert (item.getProperty('hero_meta'), item.getProperty('hero_plot')) == ('2024 • Drama', 'Channel blurb')
    assert (item.getProperty('hero_facts'), item.getProperty('hero_runtime')) == ('8.1', '90')
    assert item.getProperty('hero_now') == '' and item.getProperty('hero_progress') == ''


def test_reset_makes_the_next_apply_take_rewritten_values_as_the_base():
    item = FakeItem(hero_plot='Channel blurb')
    _apply(item, _info())
    # The coverflow's enrichment rewrites the hero properties.
    item.setProperty('hero_plot', 'Fresh description')
    livenow.reset_item(item)
    assert _apply(item, _info()) is True
    assert item.getProperty('live_base_hero_plot') == 'Fresh description'
    assert item.getProperty('hero_plot').endswith('Headlines of the day')
    assert _apply(item, None) is True
    assert item.getProperty('hero_plot') == 'Fresh description'


# ------------------------------------------------------------ small helpers

@pytest.mark.parametrize('extra, expected', [
    ([('genre', 'x'), ('skip', '40')], 40), ([('skip', 'bad')], 0), ([('skip', -5)], 0), ([], 0), (None, 0),
])
def test_page_skip(extra, expected):
    assert livenow.page_skip(extra) == expected


def test_guide_extra_carries_the_date_and_only_a_real_skip():
    assert livenow.guide_extra('2026-05-04') == [('date', '2026-05-04')]
    assert livenow.guide_extra('2026-05-04', 100) == [('date', '2026-05-04'), ('skip', 100)]


def test_next_boundary_is_the_soonest_future_edge():
    assert livenow.next_boundary(_schedule(), NOW) == NOW + 1200
    assert livenow.next_boundary(_schedule(), NOW + 1200) == NOW + 3000
    assert livenow.next_boundary(_schedule(), NOW + 3000) is None
    assert livenow.next_boundary([{'title': 'no times'}, 'junk', None], NOW) is None


def test_ordered_meta_addons_puts_epg_providers_first():
    def entry(name, provider):
        manifest = {'id': name, 'resources': ['meta'], 'types': ['tv']}
        if provider:
            manifest['behaviorHints'] = {'epgProvider': True}
        return {'transportUrl': 'https://%s/manifest.json' % name, 'manifest': manifest}

    skipped = {'transportUrl': 'https://movies/manifest.json',
               'manifest': {'id': 'movies', 'resources': ['meta'], 'types': ['movie']}}
    addons = [entry('plain1', False), skipped, entry('guide', True), entry('plain2', False)]
    ordered = livenow.ordered_meta_addons(addons, 'tv', 'ch1')
    assert [transport for transport, _manifest in ordered] == [
        'https://guide/manifest.json', 'https://plain1/manifest.json', 'https://plain2/manifest.json']


# ----------------------------------------------------------------- recent row

def test_metas_from_records_force_the_live_hint_and_keep_the_shape():
    metas = livenow.metas_from_records([
        {'_id': 'ch1', 'type': 'tv', 'name': 'One', 'poster': 'p', 'posterShape': 'square'},
        {'_id': 'ch2', 'type': 'events', 'name': 'Two', 'behaviorHints': {'isLive': False, 'x': 1}},
        {'name': 'no id'}, 'junk',
    ])
    assert [meta['id'] for meta in metas] == ['ch1', 'ch2']
    assert metas[0]['posterShape'] == 'square' and metas[0]['behaviorHints'] == {'isLive': True}
    assert metas[1]['behaviorHints'] == {'isLive': True, 'x': 1}
    assert all(livetv.is_live(meta) for meta in metas)


def test_metas_from_local_entries():
    metas = livenow.metas_from_local([
        {'id': 'ch1', 'type': 'tv', 'name': 'One', 'poster': 'p', 'posterShape': 'square',
         'last_watched': '2026-05-04T10:00:00Z'},
        {'type': 'tv'}, None,
    ])
    assert len(metas) == 1
    assert metas[0]['name'] == 'One' and metas[0]['posterShape'] == 'square'
    assert livetv.is_live(metas[0])


# ---------------------------------------------------------------------- feed

class Harness:
    """A feed over injected fetches and a fixed clock; `woken` counts the
    GUI nudges."""

    def __init__(self, pages=None, metas=None, **kwargs):
        self.page_calls, self.meta_calls = [], []
        self.pages, self.metas = pages or {}, metas or {}
        self.woken = threading.Semaphore(0)
        self.wakes = 0
        self.feed = livenow.NowNextFeed(self.fetch_page, self.fetch_meta, self.wake,
                                        clock=lambda: NOW, utc_offset=lambda _now: 0, **kwargs)

    def wake(self):
        self.wakes += 1
        self.woken.release()

    def fetch_page(self, source, dates):
        self.page_calls.append((source, list(dates)))
        answer = self.pages.get(source)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def fetch_meta(self, stype, sid):
        self.meta_calls.append((stype, sid))
        answer = self.metas.get(sid)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def wait_for_wake(self):
        assert self.woken.acquire(timeout=5.0), 'the feed never woke the window'


GUIDE = ('https://guide/manifest.json', 'tv', 'iptv0', 0)


def _guide_metas(ids):
    return [{'id': cid, 'type': 'tv', 'videos': _schedule()} for cid in ids]


@pytest.fixture
def harness():
    made = []

    def make(**kwargs):
        built = Harness(**kwargs)
        made.append(built)
        return built

    yield make
    for built in made:
        built.feed.stop()
        if built.feed.thread:
            built.feed.thread.join(5.0)


def test_the_guide_page_serves_the_channel_and_is_shared_across_a_row(harness):
    h = harness(pages={GUIDE: _guide_metas(['ch1', 'ch2'])})
    h.feed.want('tv', 'ch1', GUIDE)
    h.wait_for_wake()
    known, info = h.feed.snapshot('ch1')
    assert known and info['current']['title'] == 'News' and info['next']['title'] == 'Film'
    assert h.meta_calls == []
    # One request per UTC date of the page; the second channel reuses it.
    assert len(h.page_calls) == 1 and h.page_calls[0][0] == GUIDE
    assert h.page_calls[0][1] == livetv.guide_dates(NOW, 0)
    h.feed.want('tv', 'ch2', GUIDE)
    h.wait_for_wake()
    assert h.feed.snapshot('ch2')[1]['current']['title'] == 'News'
    assert len(h.page_calls) == 1


def test_a_channel_the_guide_lacks_falls_back_to_its_own_meta(harness):
    h = harness(pages={GUIDE: _guide_metas(['other'])},
                metas={'ch1': {'id': 'ch1', 'videos': _schedule()}})
    h.feed.want('tv', 'ch1', GUIDE)
    h.wait_for_wake()
    assert h.meta_calls == [('tv', 'ch1')]
    assert h.feed.snapshot('ch1')[1]['current']['title'] == 'News'


def test_without_a_guide_source_only_the_meta_is_asked(harness):
    h = harness(metas={'ch1': {'id': 'ch1', 'videos': _schedule()}})
    h.feed.want('tv', 'ch1')
    h.wait_for_wake()
    assert h.page_calls == [] and h.meta_calls == [('tv', 'ch1')]


def test_a_channel_with_no_schedule_is_known_but_empty(harness):
    h = harness(metas={'ch1': {'id': 'ch1'}})
    h.feed.want('tv', 'ch1')
    deadline = time.monotonic() + 5.0
    while not h.feed.snapshot('ch1')[0] and time.monotonic() < deadline:
        time.sleep(0.01)
    assert h.feed.snapshot('ch1') == (True, None)
    assert h.wakes == 0  # nothing changed for the window to repaint


def test_a_failed_fetch_is_retried_in_a_minute_and_keeps_the_last_schedule(harness):
    h = harness(metas={'ch1': {'id': 'ch1', 'videos': _schedule()}})
    h.feed.want('tv', 'ch1')
    h.wait_for_wake()
    first_due = h.feed._schedules['ch1']['next']
    assert first_due - time.monotonic() > livetv.RETRY_SECONDS  # a healthy schedule: the long interval
    # Make it due, then fail.
    h.metas['ch1'] = RuntimeError('boom')
    with h.feed._lock:
        h.feed._schedules['ch1']['next'] = time.monotonic() - 1
    h.feed._nudge.set()
    deadline = time.monotonic() + 5.0
    while len(h.meta_calls) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(h.meta_calls) >= 2
    time.sleep(0.05)
    entry = h.feed._schedules['ch1']
    assert [video['id'] for video in entry['videos']] == ['a', 'b']
    assert 0 < entry['next'] - time.monotonic() <= livetv.RETRY_SECONDS


def test_a_failing_guide_page_is_retried_in_a_minute_through_the_meta(harness):
    h = harness(pages={GUIDE: RuntimeError('down')}, metas={'ch1': {'id': 'ch1', 'videos': _schedule()}})
    h.feed.want('tv', 'ch1', GUIDE)
    h.wait_for_wake()
    page = h.feed._pages[GUIDE]
    assert 0 < page['next'] - time.monotonic() <= livetv.RETRY_SECONDS
    assert h.feed.snapshot('ch1')[1]['current']['title'] == 'News'


def test_it_wakes_the_window_only_when_what_the_hero_shows_changed(harness):
    h = harness(metas={'ch1': {'id': 'ch1', 'videos': _schedule()}})
    h.feed.want('tv', 'ch1')
    h.wait_for_wake()
    h.feed._announce('ch1')
    h.feed._announce('ch1')
    assert h.wakes == 1


def test_stop_ends_the_worker_and_runs_the_exit_hook(harness):
    exited = threading.Event()
    h = harness(metas={'ch1': {'id': 'ch1', 'videos': _schedule()}}, on_exit=exited.set)
    h.feed.want('tv', 'ch1')
    h.wait_for_wake()
    assert not h.feed.stopped
    h.feed.stop()
    h.feed.thread.join(5.0)
    assert h.feed.stopped and exited.is_set() and not h.feed.thread.is_alive()


def test_the_owners_stop_flag_ends_the_worker_too(harness):
    flag = []
    h = harness(metas={'ch1': {'id': 'ch1', 'videos': _schedule()}}, should_stop=lambda: bool(flag))
    h.feed.want('tv', 'ch1')
    h.wait_for_wake()
    flag.append(1)
    h.feed.want('tv', 'ch2')
    h.feed.thread.join(5.0)
    assert not h.feed.thread.is_alive() and h.feed.stopped


def test_no_wanted_channel_never_starts_a_thread(harness):
    h = harness()
    h.feed.want()
    h.feed.want(None, None, GUIDE)
    assert h.feed.thread is None and h.page_calls == [] and h.meta_calls == []


def test_the_worker_sleeps_until_the_next_programme_edge(harness):
    h = harness(metas={'ch1': {'id': 'ch1', 'videos': _schedule()}})
    h.feed.want('tv', 'ch1')
    h.wait_for_wake()
    # The news ends 1200 s from the fixed clock: sooner than the minute tick? No: the
    # tick wins; with an edge 10 s away the edge wins.
    assert h.feed._sleep_for('ch1') == livenow.TICK_SECONDS
    soon = [_programme('a', NOW - 600, NOW + 10, 'News')]
    with h.feed._lock:
        h.feed._schedules['ch1']['videos'] = soon
    assert h.feed._sleep_for('ch1') == pytest.approx(10.5)


def test_ensure_feed_keeps_a_running_feed_and_replaces_a_stopped_one(monkeypatch):
    made = []
    monkeypatch.setattr(livenow, 'new_feed', lambda: made.append(object()) or made[-1])
    feed = livenow.NowNextFeed(lambda *_: None, lambda *_: None, lambda: None)
    assert livenow.ensure_feed(feed) is feed
    feed.stop()
    assert livenow.ensure_feed(feed) is made[0]
    assert livenow.ensure_feed(None) is made[1]
