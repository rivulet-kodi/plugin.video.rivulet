"""lib/ui/home.py: how Home builds its rows.

The row helpers on their own, against the kodistubs fakes: what a row
shows when the account or an addon fails, how a row grows past its
first page, and which library records count as "in progress".
"""
import contextlib

import pytest

from tests.kodistubs import install_kodi_stubs
from tests.test_home import _RELOAD_MODULE_NAMES


@pytest.fixture
def home():
    with contextlib.ExitStack() as stack:
        ctx = stack.enter_context(install_kodi_stubs(reload=_RELOAD_MODULE_NAMES))
        ctx.home = ctx.home
        yield ctx


# --- text --------------------------------------------------------------------------


def test_truncate_keeps_short_text_and_ends_long_text_with_an_ellipsis(home):
    t = home.home._truncate
    assert t('  short  ', 10) == 'short'
    assert t(None, 10) == ''
    assert t('a long title here', 8) == 'a long…'


@pytest.mark.parametrize('runtime, text', [
    ('  155 min ', '155 min'), (98, '98 min'), (98.7, '98 min'), (0, ''), (-5, ''), (True, ''), (None, ''), ([], ''),
])
def test_runtime_text(home, runtime, text):
    assert home.home._runtime_text({'runtime': runtime}) == text


# --- a catalog's first page --------------------------------------------------------


def test_a_failing_addon_is_logged_and_raised_as_unavailable(home, monkeypatch):
    import lib.ui.catalogpages as catalogpages
    from lib.stremio.addons import AddonError

    def _addon_error(*args):
        raise AddonError('https://addon.example/catalog?token=SECRET failed')

    monkeypatch.setattr(catalogpages, 'cached_first_page', _addon_error)
    with pytest.raises(home.home.CatalogUnavailable) as caught:
        home.home._one_catalog_page('https://addon.example/manifest.json', {}, {'id': 'top'})
    assert str(caught.value) == 'top'

    def _bug(*args):
        raise KeyError('metas')

    monkeypatch.setattr(catalogpages, 'cached_first_page', _bug)
    with pytest.raises(home.home.CatalogUnavailable):
        home.home._one_catalog_page('https://addon.example/manifest.json', {}, 'not a catalog')
    logged = [msg for msg, _level in home.env.log_calls if 'home: catalog' in msg]
    assert len(logged) == 2
    # The addon's own detail is logged, never its URL's token.
    assert not any('SECRET' in msg for msg in logged)


def test_a_catalog_page_is_returned_whole(home, monkeypatch):
    import lib.ui.catalogpages as catalogpages

    page = [{'id': 'tt%d' % n} for n in range(40)]
    monkeypatch.setattr(catalogpages, 'cached_first_page', lambda *args: page)
    assert home.home._one_catalog_page('t', {}, {'id': 'top'}) is page


# --- growing a row -------------------------------------------------------------------






def test_leftovers_come_before_the_network(home, monkeypatch):
    def _never(state):
        raise AssertionError('no fetch while leftovers remain')

    monkeypatch.setattr(home.home, '_shelf_page_gen', _never)
    state = {'pending': [{'id': 'tt%d' % n} for n in range(5)]}
    assert [m['id'] for m in home.home._pull_more(state, 3)] == ['tt0', 'tt1', 'tt2']
    assert [m['id'] for m in state['pending']] == ['tt3', 'tt4']


@pytest.mark.parametrize('state, room', [(None, 5), ({}, 0), ({'exhausted': True, 'pending': [{'id': 'x'}]}, 5)])
def test_nothing_more_when_there_is_no_room_or_the_row_ended(home, state, room):
    assert home.home._pull_more(state, room) == []


def test_a_row_without_a_catalog_ends(home):
    state = {'pending': []}
    assert home.home._pull_more(state, 5) == []
    assert state['exhausted'] is True


def _with_pages(home, monkeypatch, pages):
    def _gen():
        for page in pages:
            if isinstance(page, BaseException):
                raise page
            yield page

    gen = _gen()
    monkeypatch.setattr(home.home, '_shelf_page_gen', lambda state: gen)


def test_the_next_page_is_fetched_once_the_leftovers_are_gone(home, monkeypatch):
    _with_pages(home, monkeypatch, [[{'id': 'tt5'}, 'junk', {'id': 'tt6'}, {'id': 'tt7'}]])
    state = {'pending': []}
    assert [m['id'] for m in home.home._pull_more(state, 2)] == ['tt5', 'tt6']
    assert [m['id'] for m in state['pending']] == ['tt7']
    assert not state.get('exhausted')


@pytest.mark.parametrize('pages', [[], [[]], [['junk', None]]])
def test_an_empty_page_ends_the_row(home, monkeypatch, pages):
    _with_pages(home, monkeypatch, pages)
    state = {'pending': []}
    assert home.home._pull_more(state, 5) == []
    assert state['exhausted'] is True




# --- Continue from the account --------------------------------------------------------


def _entry(**overrides):
    entry = {
        '_id': 'tt1', 'name': 'One', 'type': 'movie', 'poster': 'p', 'background': 'b',
        'state': {'timeOffset': 600000, 'duration': 6000000, 'video_id': 'tt1'},
    }
    entry.update(overrides)
    return entry


def test_live_channels_with_an_offset_on_the_account_are_never_continued(home):
    metas = home.home._library_continue_metas([
        _entry(_id='ch1', type='tv'),
        _entry(_id='ch2', behaviorHints={'isLive': True}),
        _entry(_id='tt1'),
    ])
    assert [meta['id'] for meta in metas] == ['tt1']


def test_titles_part_way_through_on_the_account_are_continued(home):
    metas = home.home._library_continue_metas([
        _entry(),
        _entry(_id='tt2', removed=True, temp=True),            # played, not added: still Continue
        _entry(_id='tt3', removed=True),                       # removed by the viewer
        _entry(_id='tt4', state={'timeOffset': 1, 'duration': 6000000}),       # barely started
        _entry(_id='tt5', state={'timeOffset': 5900000, 'duration': 6000000}),  # finished
        _entry(_id='', name='No id'),
        _entry(_id='tt6', name='', poster=''),
        'junk',
    ])
    assert [meta['id'] for meta in metas] == ['tt1', 'tt2']
    first = metas[0]
    assert first['band'] == 'resume'
    assert first['position_ms'] == 600000 and first['duration_ms'] == 6000000
    assert first['video_id'] == 'tt1'
    assert home.home._library_continue_metas(None) == []


def test_series_with_a_next_episode_pointer_on_the_account_are_next_up(home):
    """#99: finishing an episode in a Stremio app leaves `timeOffset` 1
    on the next episode. A 95%+ record names the episode just finished
    (possibly a finale), so it is not a pointer."""
    def series(sid, **state):
        return _entry(_id=sid, type='series', state=dict({'duration': 3600000}, **state))

    metas = home.home._library_continue_metas([
        series('tt1', timeOffset=1, video_id='tt1:1:3'),              # advanced by a Stremio app
        series('tt2', timeOffset=3590000, video_id='tt2:1:2'),        # finished, maybe a finale
        series('tt3', timeOffset=1, video_id='tt3:2:1', duration=0),  # pointer with no duration
        series('tt4', timeOffset=0, video_id='tt4:1:8'),              # nothing left to play
        series('tt5', timeOffset=1),                                  # no episode to point at
        series('tt6', timeOffset=600000, video_id='tt6:1:1'),         # mid-episode
    ])
    assert [(meta['id'], meta['band'], meta['video_id']) for meta in metas] == [
        ('tt6', 'resume', 'tt6:1:1'),
        ('tt1', 'next_up', 'tt1:1:3'),
        ('tt3', 'next_up', 'tt3:2:1'),
    ]


@pytest.mark.parametrize('stype', ['movie', 'series'])
@pytest.mark.parametrize('state', [
    {'timeOffset': '30', 'duration': 120},
    {'timeOffset': True, 'duration': 120},
    {'timeOffset': 30, 'duration': '120'},
    {'timeOffset': 30, 'duration': True},
    {'timeOffset': float('inf'), 'duration': 120},
    {'timeOffset': 1, 'duration': float('inf')},
    {'timeOffset': float('nan'), 'duration': 120},
    'junk',
])
def test_malformed_account_state_is_not_continued(home, stype, state):
    """A Continue card int()s these fields: Infinity would raise there."""
    entry = _entry(type=stype, state=state)
    if isinstance(state, dict):
        state['video_id'] = 'tt1:1:1'
    assert home.home._library_continue_metas([entry]) == []


def test_account_resume_records_outrank_next_up_under_the_cap(home):
    def at(sid, offset, when, stype='series'):
        return _entry(_id=sid, type=stype, state={
            'timeOffset': offset, 'duration': 3600000, 'video_id': sid + ':1:1', 'lastWatched': when})

    metas = home.home._library_continue_metas([
        at('tt1', 1, '2026-11-03T10:00:00Z'),
        at('tt2', 600000, '2026-11-01T10:00:00Z'),
        at('tt3', 1, '2026-11-02T10:00:00Z'),
        at('tt4', 600000, '2026-11-02T10:00:00Z', 'movie'),
    ])
    assert [meta['id'] for meta in metas] == ['tt4', 'tt2', 'tt1', 'tt3']


def test_caught_up_series_with_a_new_episode_are_next_up_on_it(home, tmp_path):
    """#99: Stremio's notifications. The service keeps the latest episodes
    of library series; one out since the last watch puts the series on
    Continue on that episode, until it is watched or removed from Continue."""
    from lib.store import Store

    store = Store(str(tmp_path))
    store.set_last_videos({
        'tt1': [['tt1:1:1', '2026-09-01T00:00:00Z'], ['tt1:1:2', '2026-10-05T00:00:00Z'],
                ['tt1:1:3', '2026-10-12T00:00:00Z']],
        'tt2': [['tt2:1:1', '2026-09-01T00:00:00Z']],
    })

    def series(sid, last):
        return _entry(_id=sid, type='series', temp=False, removed=False,
                      state={'timeOffset': 0, 'duration': 0, 'lastWatched': last})

    entries = [series('tt1', '2026-10-01T00:00:00Z'), series('tt2', '2026-10-01T00:00:00Z')]
    metas = home.home._new_episode_metas(store, entries, now='2026-11-03T00:00:00Z')
    assert [(m['id'], m['band'], m['video_id'], m['watched_at']) for m in metas] == [
        ('tt1', 'next_up', 'tt1:1:2', '2026-10-12T00:00:00Z'),
    ]
    assert [m['id'] for m in home.home._played_metas(store, entries)] == ['tt1']

    store.remove_progress_title('series', 'tt1')
    assert home.home._played_metas(store, entries) == []


def test_a_continue_row_without_a_video_id_resumes_the_title(home):
    entry = _entry()
    del entry['state']['video_id']
    assert home.home._library_continue_metas([entry])[0]['video_id'] is None


def test_an_account_title_removed_from_continue_stays_off_until_watched_again(home, tmp_path):
    """The account still holds the in-progress state of a title removed
    from Continue; the row must honour the removal, not just local progress."""
    from lib.store import Store

    store = Store(str(tmp_path))
    store.remove_progress_title('movie', 'tt1')
    stale = _entry(_mtime='2000-01-01T00:00:00.000Z')
    assert home.home._played_metas(store, [stale]) == []
    watched_again = _entry(_mtime='2099-01-01T00:00:00.000Z')
    assert len(home.home._played_metas(store, [watched_again])) == 1


def test_library_is_read_newest_first(home):
    rows = home.home._newest_first([
        {'_id': 'a', '_ctime': '2026-01-01'},
        'junk',
        {'_id': 'b', '_mtime': '2026-03-01'},
        {'_id': 'c'},
        {'_id': 'd', '_ctime': '2026-02-01', '_mtime': '2026-09-01'},
    ])
    assert [row['_id'] for row in rows] == ['b', 'd', 'a', 'c']
    assert home.home._newest_first(None) == []


# --- the account read ------------------------------------------------------------------


def test_signed_out_reads_no_library(home, monkeypatch):
    import lib.ui.mystuff as mystuff

    monkeypatch.setattr(mystuff, '_read_library_entries', lambda store: pytest.fail('not asked'))
    assert home.home._fetch_library(object(), None) == []


def test_a_library_read_that_breaks_is_not_cached_as_empty(home, monkeypatch):
    import lib.ui.libraryaction as libraryaction
    import lib.ui.mystuff as mystuff

    def _boom(store):
        raise RuntimeError('store gone')

    monkeypatch.setattr(mystuff, '_read_library_entries', _boom)
    assert home.home._fetch_library(object(), {'authKey': 'k'}) is None
    monkeypatch.setattr(mystuff, '_read_library_entries', lambda store: [])
    monkeypatch.setattr(libraryaction, 'apply_library_writes', lambda rows: 'junk')
    assert home.home._fetch_library(object(), {'authKey': 'k'}) is None


def test_a_pending_write_shows_on_the_library_read(home, monkeypatch):
    import lib.ui.libraryaction as libraryaction
    import lib.ui.mystuff as mystuff

    monkeypatch.setattr(mystuff, '_read_library_entries', lambda store: [{'_id': 'tt1', '_ctime': '2020'}])
    libraryaction.note_library_write({'_id': 'tt9', 'removed': False, '_ctime': '2026', '_mtime': '2026'})
    rows = home.home._fetch_library(object(), {'authKey': 'k'})
    assert sorted(row['_id'] for row in rows) == ['tt1', 'tt9']








# --- the first screen of every row ------------------------------------------------------
