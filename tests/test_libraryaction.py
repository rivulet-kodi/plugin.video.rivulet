"""lib/ui/libraryaction.py: Add to / Remove from Library on a poster.

The account is a fake API: no network. What matters is that a failed or
cancelled lookup never writes, that the write starts from the account's
current record, and that a landed write shows on Home straight away.
"""
import pytest

from tests.kodistubs import install_kodi_stubs


@pytest.fixture
def action():
    with install_kodi_stubs(reload=('lib.ui.compat', 'lib.ui.dialogs', 'lib.ui.mystuff', 'lib.ui.libraryaction')) as ctx:
        ctx.mystuff.clear_library_writes()
        yield ctx
        ctx.mystuff.clear_library_writes()


class _Api:
    def __init__(self, record=None, get_error=None, put_error=None):
        self.record = record
        self.get_error = get_error
        self.put_error = put_error
        self.gets = []
        self.puts = []

    def get_library_item(self, auth_key, item_id):
        self.gets.append((auth_key, item_id))
        if self.get_error:
            raise self.get_error
        return self.record

    def put_library_item(self, auth_key, payload):
        if self.put_error:
            raise self.put_error
        self.puts.append((auth_key, payload))


class _Store:
    def __init__(self, auth):
        self._auth = auth

    def get_auth(self):
        return self._auth


_AUTH = {'authKey': 'key'}
_META = {'id': 'tt1', 'type': 'movie', 'name': 'Dune', 'poster': 'https://img/p.jpg'}


def _wire(monkeypatch, api, auth=_AUTH, pick=0, cancel_on=None):
    """`cancel_on`: the n-th spinner (0-based) that gets Back."""
    import lib.ui.dependencies as dependencies
    import lib.ui.dialogs as dialogs
    import lib.ui.newhome as newhome
    import lib.ui.uicommon as uicommon

    spinners = []
    shown = []

    def _run(func, heading, message=''):
        spinners.append(heading)
        if cancel_on is not None and len(spinners) - 1 == cancel_on:
            return uicommon.CANCELLED
        return func()

    monkeypatch.setattr(dependencies, 'get_api', lambda: api)
    monkeypatch.setattr(dependencies, 'get_store', lambda: _Store(auth))
    monkeypatch.setattr(uicommon, 'run_cancellable', _run)
    monkeypatch.setattr(dialogs, 'choose', lambda heading, rows: shown.append((heading, rows)) or pick)
    monkeypatch.setattr(newhome, '_library_dirty', False)
    return spinners, shown


def _api_error():
    from lib.stremio.api import ApiError

    return ApiError('server said no')


# --- the poster's meta ----------------------------------------------------------


class _Item:
    def __init__(self, label='', **props):
        self._label = label
        self._props = props

    def getProperty(self, key):
        return self._props.get(key, '')

    def getLabel(self):
        return self._label


def test_a_poster_gives_its_meta(action):
    item = _Item('Dune (label)', sid='tt1', stype='movie', hero_title='Dune', thumbnail='https://img/p.jpg')
    assert action.libraryaction.meta_from_item(item) == _META
    # Without a hero title the label names it.
    item = _Item('Dune (label)', sid='tt1')
    assert action.libraryaction.meta_from_item(item) == {'id': 'tt1', 'type': '', 'name': 'Dune (label)', 'poster': ''}


@pytest.mark.parametrize('item', [None, _Item('Movies', action='hub'), _Item('More', sid='tt1', action='more'), _Item('Empty')])
def test_chips_and_notices_have_no_meta(action, item):
    assert action.libraryaction.meta_from_item(item) is None


# --- reading the account ----------------------------------------------------------


def test_lookup_tells_no_record_from_a_failed_call(action, monkeypatch):
    api = _Api(record=None)
    _wire(monkeypatch, api)
    assert action.libraryaction._lookup(_AUTH, 'tt1') == (None, True)
    assert api.gets == [('key', 'tt1')]
    _wire(monkeypatch, _Api(get_error=_api_error()))
    assert action.libraryaction._lookup(_AUTH, 'tt1') == (None, False)


def test_lookup_cancelled_is_not_ok(action, monkeypatch):
    import lib.ui.uicommon as uicommon

    _wire(monkeypatch, _Api(record={'_id': 'tt1'}), cancel_on=0)
    found, ok = action.libraryaction._lookup(_AUTH, 'tt1')
    assert found is uicommon.CANCELLED and ok is False


def test_push_lands_fails_or_is_cancelled(action, monkeypatch):
    api = _Api()
    _wire(monkeypatch, api)
    assert action.libraryaction._push(_AUTH, {'_id': 'tt1'}) is True
    assert api.puts == [('key', {'_id': 'tt1'})]

    _wire(monkeypatch, _Api(put_error=_api_error()))
    assert action.libraryaction._push(_AUTH, {'_id': 'tt1'}) is False
    assert action.env.notifications[-1][1] == 'STR30301'

    count = len(action.env.notifications)
    api = _Api()
    _wire(monkeypatch, api, cancel_on=0)
    assert action.libraryaction._push(_AUTH, {'_id': 'tt1'}) is False
    # Back says nothing and writes nothing.
    assert len(action.env.notifications) == count
    assert api.puts == []


# --- the one-row menu -------------------------------------------------------------


def test_offer_signed_out_asks_to_sign_in(action, monkeypatch):
    api = _Api()
    spinners, shown = _wire(monkeypatch, api, auth=None)
    assert action.libraryaction.offer(_META) is None
    assert action.env.notifications[-1][1] == 'STR30190'
    assert shown == [] and api.gets == []


def test_offer_adds_a_title_not_in_the_library(action, monkeypatch):
    import lib.ui.newhome as newhome

    api = _Api(record=None)
    _, shown = _wire(monkeypatch, api)
    assert action.libraryaction.offer(_META) == 'added'
    assert shown == [('Dune', ['STR30293'])]
    payload = api.puts[0][1]
    assert payload['_id'] == 'tt1' and payload['removed'] is False
    assert action.env.notifications[-1][1] == 'STR30297'
    # Home re-reads its library and shows the add before the account echoes it.
    assert newhome._library_dirty is True
    assert [row['_id'] for row in action.mystuff.apply_library_writes([])] == ['tt1']


def test_offer_removes_a_title_in_the_library(action, monkeypatch):
    from lib import library

    record = library.library_add_payload(_META)
    api = _Api(record=record)
    _, shown = _wire(monkeypatch, api)
    assert action.libraryaction.offer(_META) == 'removed'
    assert shown == [('Dune', ['STR30294'])]
    payload = api.puts[0][1]
    assert payload['removed'] is True
    assert action.env.notifications[-1][1] == 'STR30298'


def test_a_removed_record_is_offered_as_an_add(action, monkeypatch):
    from lib import library

    record = dict(library.library_add_payload(_META), removed=True)
    _, shown = _wire(monkeypatch, _Api(record=record), pick=-1)
    assert action.libraryaction.offer(_META) is None
    assert shown == [('Dune', ['STR30293'])]


def test_offer_with_a_failed_lookup_writes_nothing(action, monkeypatch):
    api = _Api(get_error=_api_error())
    _, shown = _wire(monkeypatch, api)
    assert action.libraryaction.offer(_META) is None
    assert action.env.notifications[-1][1] == 'STR30302'
    assert shown == [] and api.puts == []


def test_offer_back_on_the_lookup_spinner_writes_nothing(action, monkeypatch):
    api = _Api()
    _, shown = _wire(monkeypatch, api, cancel_on=0)
    assert action.libraryaction.offer(_META) is None
    assert shown == [] and api.puts == []
    assert action.env.notifications == []


def test_offer_backed_out_of_the_menu_writes_nothing(action, monkeypatch):
    api = _Api()
    _, shown = _wire(monkeypatch, api, pick=None)
    assert action.libraryaction.offer({'id': 'tt1'}) is None
    # No name: the id heads the menu.
    assert shown == [('tt1', ['STR30293'])]
    assert api.puts == []


# --- the write ------------------------------------------------------------------


def test_the_write_starts_from_the_record_as_it_is_now(action, monkeypatch):
    """An add over a tombstone keeps the account's resume point."""
    from lib import library

    tomb = library.merge_playback(library.library_add_payload(_META), 5000, 90000, video_id='tt1')
    tomb = dict(tomb, removed=True)
    api = _Api(record=tomb)
    _wire(monkeypatch, api)
    assert action.libraryaction._write(_AUTH, _META, False) == 'added'
    payload = api.puts[0][1]
    assert payload['removed'] is False
    assert payload['state']['timeOffset'] == 5000


def test_a_remove_whose_record_vanished_writes_nothing(action, monkeypatch):
    api = _Api(record=None)
    _wire(monkeypatch, api)
    assert action.libraryaction._write(_AUTH, _META, True) is None
    assert action.env.notifications[-1][1] == 'STR30302'
    assert api.puts == []


def test_a_write_whose_fresh_lookup_fails_writes_nothing(action, monkeypatch):
    import lib.ui.newhome as newhome

    api = _Api(get_error=_api_error())
    _wire(monkeypatch, api)
    assert action.libraryaction._write(_AUTH, _META, False) is None
    assert action.env.notifications[-1][1] == 'STR30302'
    assert newhome._library_dirty is False


def test_a_write_cancelled_at_either_spinner_changes_nothing(action, monkeypatch):
    import lib.ui.newhome as newhome

    for spinner in (0, 1):
        api = _Api(record=None)
        _wire(monkeypatch, api, cancel_on=spinner)
        assert action.libraryaction._write(_AUTH, _META, False) is None
        assert api.puts == []
        assert newhome._library_dirty is False
    assert action.mystuff.apply_library_writes([]) == []
    assert action.env.notifications == []


def test_a_write_the_server_refuses_is_not_shown_on_home(action, monkeypatch):
    import lib.ui.newhome as newhome

    _wire(monkeypatch, _Api(record=None, put_error=_api_error()))
    assert action.libraryaction._write(_AUTH, _META, False) is None
    assert action.env.notifications[-1][1] == 'STR30301'
    assert newhome._library_dirty is False
    assert action.mystuff.apply_library_writes([]) == []


# --- the poster menu -------------------------------------------------------------


def test_extra_rows_come_first_and_run(action, monkeypatch):
    ran = []
    _, shown = _wire(monkeypatch, _Api(record=None), pick=0)
    extra = [('Go to series', lambda: ran.append('series')), ('', lambda: ran.append('blank')), ('Broken', 'not callable')]
    assert action.libraryaction.poster_menu(_META, extra) is None
    # Rows without a label or an action are dropped.
    assert shown == [('Dune', ['Go to series', 'STR30293', 'STR30295', 'STR30499'])]
    assert ran == ['series']


def test_the_library_row_after_extras_still_writes(action, monkeypatch):
    api = _Api(record=None)
    _wire(monkeypatch, api, pick=1)
    assert action.libraryaction.poster_menu(_META, [('Go to series', lambda: None)]) == 'added'
    assert len(api.puts) == 1


def test_a_failed_lookup_leaves_only_the_trailer(action, monkeypatch):
    import lib.ui.trailer as trailer

    played = []
    monkeypatch.setattr(trailer, 'play_trailer', lambda meta, stype='', sid='': played.append((stype, sid)))
    _, shown = _wire(monkeypatch, _Api(get_error=_api_error()), pick=0)
    assert action.libraryaction.poster_menu(_META) is None
    assert shown == [('Dune', ['STR30499'])]
    assert played == [('movie', 'tt1')]


def test_back_on_the_menu_lookup_opens_no_menu(action, monkeypatch):
    _, shown = _wire(monkeypatch, _Api(), cancel_on=0)
    assert action.libraryaction.poster_menu(_META) is None
    assert shown == []


@pytest.mark.parametrize('pick', [None, -1, 3, 7])
def test_a_pick_outside_the_menu_does_nothing(action, monkeypatch, pick):
    import lib.ui.trailer as trailer

    played = []
    monkeypatch.setattr(trailer, 'play_trailer', lambda *args: played.append(args))
    api = _Api(record=None)
    _wire(monkeypatch, api, pick=pick)
    assert action.libraryaction.poster_menu(_META) is None
    assert played == [] and api.puts == []


def test_library_transport_errors_never_log_credentials(action, monkeypatch):
    from lib.stremio.api import ApiError

    error = ApiError('https://user:secret@server.test/private?token=hidden')
    _wire(monkeypatch, _Api(get_error=error, put_error=error))
    assert action.libraryaction._lookup(_AUTH, 'tt1') == (None, False)
    assert action.libraryaction._push(_AUTH, {}) is False
    messages = [msg for msg, _ in action.env.log_calls if 'libraryaction:' in msg]
    assert len(messages) == 2
    assert all('ApiError' in msg and 'secret' not in msg and 'hidden' not in msg
               and 'user:' not in msg for msg in messages)


# --- Mark as watched / unwatched on a movie poster (issue #85) ------------------


def _watched_record(watched=True, mtime='2020-01-01T00:00:00Z'):
    return {'_id': 'tt1', 'type': 'movie', 'removed': False, '_mtime': mtime,
            'state': {'flaggedWatched': int(watched), 'timesWatched': int(watched)}}


def test_a_movie_menu_offers_mark_watched_between_library_and_trailer(action, monkeypatch):
    _, shown = _wire(monkeypatch, _Api(record=None), pick=None)
    action.libraryaction.poster_menu(_META)
    assert shown == [('Dune', ['STR30293', 'STR30295', 'STR30499'])]


def test_a_series_menu_has_no_mark_row(action, monkeypatch):
    _, shown = _wire(monkeypatch, _Api(record=None), pick=None)
    action.libraryaction.poster_menu(dict(_META, type='series'))
    assert shown == [('Dune', ['STR30293', 'STR30499'])]


def test_the_mark_row_follows_the_check_and_unmarks(action, monkeypatch):
    from lib.ui import watchmarks

    class _Marked:
        def get_auth(self):
            return _AUTH

        def get_progress_entries(self):
            return []

    api = _Api(record=_watched_record())
    _, shown = _wire(monkeypatch, api, pick=1)
    watchmarks.refresh(_Marked(), [_watched_record()])
    assert action.libraryaction.poster_menu(_META) == 'unwatched'
    assert shown == [('Dune', ['STR30294', 'STR30296', 'STR30499'])]
    payload = api.puts[0][1]
    assert payload['state']['flaggedWatched'] == 0 and payload['state']['timesWatched'] == 0
    assert action.env.notifications[-1][1] == 'STR30300'


def test_marking_a_new_title_watched_writes_the_implicit_record(action, monkeypatch):
    api = _Api(record=None)
    _wire(monkeypatch, api, pick=1)
    assert action.libraryaction.poster_menu(_META) == 'watched'
    key, payload = api.puts[0]
    assert key == 'key' and payload['_id'] == 'tt1' and payload['type'] == 'movie'
    assert payload['state']['flaggedWatched'] == 1 and payload['state']['timesWatched'] == 1
    assert payload['removed'] is True and payload['temp'] is True
    assert action.env.notifications[-1][1] == 'STR30299'


def test_a_landed_mark_shows_on_home_and_dirties_it(action, monkeypatch):
    import lib.ui.newhome as newhome

    _wire(monkeypatch, _Api(record=None), pick=1)
    action.libraryaction.poster_menu(_META)
    assert newhome._library_dirty is True
    assert any(entry.get('_id') == 'tt1' for entry in action.mystuff.apply_library_writes([]))


def test_a_failed_lookup_at_write_time_never_marks(action, monkeypatch):
    class _Flaky(_Api):
        def get_library_item(self, auth_key, item_id):
            self.gets.append((auth_key, item_id))
            if len(self.gets) > 1:
                raise _api_error()
            return None

    api = _Flaky()
    _wire(monkeypatch, api, pick=1)
    assert action.libraryaction.poster_menu(_META) is None
    assert api.puts == []
    assert action.env.notifications[-1][1] == 'STR30302'


def test_a_failed_mark_write_says_so_and_returns_nothing(action, monkeypatch):
    api = _Api(record=None, put_error=_api_error())
    _wire(monkeypatch, api, pick=1)
    assert action.libraryaction.poster_menu(_META) is None
    assert action.env.notifications[-1][1] == 'STR30301'
