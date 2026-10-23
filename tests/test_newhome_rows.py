"""lib/ui/newhome.py: how the New look Home builds its rows.

The row helpers on their own, against the kodistubs fakes: what a row
shows when the account or an addon fails, how a row grows past its
first page, and which library records count as "in progress".
"""
import contextlib

import pytest

from tests.kodistubs import install_kodi_stubs
from tests.test_newhome import _RELOAD_MODULE_NAMES


@pytest.fixture
def home():
    with contextlib.ExitStack() as stack:
        ctx = stack.enter_context(install_kodi_stubs(reload=_RELOAD_MODULE_NAMES))
        ctx.home = ctx.newhome
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


@pytest.mark.parametrize('entry, percent', [
    ({'state': {'timeOffset': 30, 'duration': 120}}, 25.0),
    ({'state': {'timeOffset': 0, 'duration': 120}}, 0.0),
    ({'state': {'timeOffset': 30, 'duration': 0}}, None),
    ({'state': {'timeOffset': 30, 'duration': True}}, None),
    ({'state': {'timeOffset': '30', 'duration': 120}}, None),
    ({'state': {'timeOffset': True, 'duration': 120}}, None),
    ({'state': {'timeOffset': 30, 'duration': '120'}}, None),
    ({'state': 'junk'}, None),
    ('junk', None),
])
def test_state_percent(home, entry, percent):
    assert home.home._state_percent(entry) == percent


def _entry(**overrides):
    entry = {
        '_id': 'tt1', 'name': 'One', 'type': 'movie', 'poster': 'p', 'background': 'b',
        'state': {'timeOffset': 600000, 'duration': 6000000, 'video_id': 'tt1'},
    }
    entry.update(overrides)
    return entry


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
    assert home.home._played_posters(store, [stale]) == []
    watched_again = _entry(_mtime='2099-01-01T00:00:00.000Z')
    assert len(home.home._played_posters(store, [watched_again])) == 1


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
    import lib.ui.mystuff as mystuff

    def _boom(store):
        raise RuntimeError('store gone')

    monkeypatch.setattr(mystuff, '_read_library_entries', _boom)
    assert home.home._fetch_library(object(), {'authKey': 'k'}) is None
    monkeypatch.setattr(mystuff, '_read_library_entries', lambda store: [])
    monkeypatch.setattr(mystuff, 'apply_library_writes', lambda rows: 'junk')
    assert home.home._fetch_library(object(), {'authKey': 'k'}) is None


def test_a_pending_write_shows_on_the_library_read(home, monkeypatch):
    import lib.ui.mystuff as mystuff

    mystuff.clear_library_writes()
    monkeypatch.setattr(mystuff, '_read_library_entries', lambda store: [{'_id': 'tt1', '_ctime': '2020'}])
    mystuff.note_library_write({'_id': 'tt9', 'removed': False, '_ctime': '2026', '_mtime': '2026'})
    try:
        rows = home.home._fetch_library(object(), {'authKey': 'k'})
    finally:
        mystuff.clear_library_writes()
    assert sorted(row['_id'] for row in rows) == ['tt1', 'tt9']








# --- the first screen of every row ------------------------------------------------------
