"""Tests for the torrent pre-buffer flow in lib.ui.player.

This is the first Kodi-layer test file in the suite (everything else under
tests/ exercises the pure lib.stremio.*/lib.store layer with no xbmc
dependency). lib.ui.player imports xbmc/xbmcgui/xbmcplugin directly (see its
module docstring: "This module owns the only xbmc* calls involved in
actually starting playback"), and lib.ui.compat - which player.py imports
ADDON/L/notify/log from - additionally imports xbmcaddon/xbmcvfs and binds
`ADDON = xbmcaddon.Addon()` at module scope. None of those five modules
exist in this environment, so the `kodi_stubs` fixture below injects fakes
into sys.modules and (re)imports lib.ui.compat/lib.ui.player under them,
restoring sys.modules exactly on teardown so no other test file ever sees
the stubs.

Reference: lib/ui/player.py `_prebuffer_torrent()` (the pre-buffer state
machine) and `play()` (the public entry point that drives it and surfaces
its outcome via xbmcplugin.setResolvedUrl). ServerClient is faked by
monkeypatching the `ServerClient` name player.py itself binds via
`from lib.stremio.server import ServerClient, ...` - that's the exact
symbol `_server_client()` calls to build the server object `play()` uses
throughout.
"""
import importlib
import sys
import types
from types import SimpleNamespace

import pytest

INFO_HASH = 'deadbeefdeadbeefdeadbeefdeadbeefdeadbeef'

_STUB_MODULES = ('xbmc', 'xbmcgui', 'xbmcplugin', 'xbmcaddon', 'xbmcvfs')
_RELOADED_MODULES = ('lib.ui.compat', 'lib.ui.player')


# --- fake xbmc* modules ------------------------------------------------


class _FakeAddon:
    """Stand-in for xbmcaddon.Addon(), configurable per test via .settings."""

    def __init__(self):
        self.settings = {
            'server_url': '',
            'buffer_enable': True,
            'buffer_mb': 1,
            'subs_enable': False,
            'subs_language': 'en',
        }
        self.info = {
            'id': 'plugin.video.rivulet',
            'name': 'Rivulet',
            'icon': '',
            'fanart': '',
        }
        # Only strings player.py applies `%` formatting to need real
        # placeholders (see _prebuffer_torrent's dialog.update text); every
        # other id gets a unique deterministic marker so notify()/dialog
        # calls can be asserted on without hardcoding real strings.po text.
        self._localized = {
            30081: 'buffered %s of %s',
            30082: 'speed %s, %s peers',
        }

    def getSetting(self, key):
        value = self.settings.get(key, '')
        return '' if value is None else str(value)

    def getSettingBool(self, key):
        return bool(self.settings.get(key, False))

    def getSettingInt(self, key):
        return int(self.settings.get(key, 0))

    def getLocalizedString(self, string_id):
        return self._localized.get(string_id, 'STR%d' % string_id)

    def getAddonInfo(self, key):
        return self.info.get(key, '')


class _Env:
    """Recorder shared by every fake xbmc* module for one test invocation."""

    def __init__(self):
        self.addon = _FakeAddon()
        self.dialog_created = []
        self.dialog_updates = []
        self.dialog_closed_count = 0
        self.cancel = False
        self.monitor_abort = False
        self.monitor_abort_calls = 0
        self.notifications = []
        self.resolved = []


def _make_fake_xbmc(env):
    module = types.ModuleType('xbmc')
    module.LOGDEBUG = 0
    module.LOGINFO = 1
    module.LOGWARNING = 2
    module.LOGERROR = 3
    module.log = lambda msg, level=0: None

    class Monitor:
        def waitForAbort(self, timeout=None):
            env.monitor_abort_calls += 1
            abort = env.monitor_abort
            return bool(abort(env.monitor_abort_calls)) if callable(abort) else bool(abort)

    module.Monitor = Monitor
    return module


def _make_fake_xbmcgui(env):
    module = types.ModuleType('xbmcgui')

    class ListItem:
        def __init__(self, label='', label2='', path='', offscreen=False):
            self.path = path
            self.label = label
            self.subtitles = None

        def setLabel(self, label):
            self.label = label

        def setSubtitles(self, urls):
            self.subtitles = urls

    class DialogProgress:
        def create(self, heading, message=''):
            env.dialog_created.append((heading, message))

        def iscanceled(self):
            cancel = env.cancel
            return bool(cancel()) if callable(cancel) else bool(cancel)

        def update(self, percent, message=''):
            env.dialog_updates.append((percent, message))

        def close(self):
            env.dialog_closed_count += 1

    class Dialog:
        def notification(self, heading, message, icon=None, time=0):
            env.notifications.append((heading, message, icon, time))

    module.ListItem = ListItem
    module.DialogProgress = DialogProgress
    module.Dialog = Dialog
    module.NOTIFICATION_INFO = 'info'
    return module


def _make_fake_xbmcplugin(env):
    module = types.ModuleType('xbmcplugin')

    def setResolvedUrl(handle, succeeded, list_item):
        env.resolved.append((handle, succeeded, list_item))

    module.setResolvedUrl = setResolvedUrl
    return module


def _make_fake_xbmcaddon(env):
    module = types.ModuleType('xbmcaddon')
    module.Addon = lambda *a, **k: env.addon
    return module


def _make_fake_xbmcvfs(env):
    module = types.ModuleType('xbmcvfs')
    module.translatePath = lambda path: path
    return module


# Sentinel distinguishing "the lib.ui package had no such attribute before"
# from "it had the attribute set to None" when snapshotting for restore.
_MISSING = object()


@pytest.fixture
def kodi_stubs():
    """Inject fake xbmc*/xbmcaddon/xbmcvfs and (re)import lib.ui.player.

    Snapshots the previous sys.modules entry for every name we touch and
    restores it verbatim in `finally`, so a failure mid-test still leaves
    other test files' import state untouched.

    `importlib.import_module('lib.ui.compat')` is a dotted import, so it
    always re-execs when the name is absent from sys.modules regardless of
    the `lib.ui` package's cached attribute - but the import protocol also
    sets `lib.ui.compat`/`lib.ui.player` as attributes on the `lib.ui`
    package object as a side effect, and merely popping sys.modules on
    teardown does NOT clear those attributes. A sibling module that does
    `from lib.ui import compat` (attribute-fromlist, e.g. lib/ui/views.py)
    would then silently reuse that stale, now-orphaned attribute via
    getattr - bypassing sys.modules entirely - instead of importing fresh.
    Snapshot and restore those two attributes too, so this fixture cannot
    leak a stubbed module into any other file regardless of which import
    style it uses.
    """
    env = _Env()
    fakes = {
        'xbmc': _make_fake_xbmc(env),
        'xbmcgui': _make_fake_xbmcgui(env),
        'xbmcplugin': _make_fake_xbmcplugin(env),
        'xbmcaddon': _make_fake_xbmcaddon(env),
        'xbmcvfs': _make_fake_xbmcvfs(env),
    }
    saved = {name: sys.modules.get(name) for name in _STUB_MODULES + _RELOADED_MODULES}
    leaves = [name.rsplit('.', 1)[-1] for name in _RELOADED_MODULES]
    lib_ui_pkg = sys.modules.get('lib.ui')
    saved_attrs = {leaf: getattr(lib_ui_pkg, leaf, _MISSING) for leaf in leaves} if lib_ui_pkg else {}
    try:
        sys.modules.update(fakes)
        for name in _RELOADED_MODULES:
            sys.modules.pop(name, None)

        importlib.import_module('lib.ui.compat')
        player = importlib.import_module('lib.ui.player')

        yield SimpleNamespace(env=env, player=player)
    finally:
        for name, original in saved.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original
        for leaf, original_attr in saved_attrs.items():
            if original_attr is _MISSING:
                if hasattr(lib_ui_pkg, leaf):
                    delattr(lib_ui_pkg, leaf)
            else:
                setattr(lib_ui_pkg, leaf, original_attr)


# --- fake ServerClient ---------------------------------------------------


class _ServerScript:
    """Configurable stand-in for lib.stremio.server.ServerClient.

    Installed by monkeypatching the `ServerClient` name in lib.ui.player -
    exactly the symbol `_server_client()` calls (`from lib.stremio.server
    import ServerClient, ...`) to build the server object `play()` uses.
    """

    def __init__(self, *, available=True, resolve_url='http://server/x/0',
                 create_engine_result=None, create_engine_results=None, create_engine_error=None,
                 file_stats_results=None, file_stats_error=None,
                 torrent_url_result=None):
        self.available = available
        self.resolve_url = resolve_url
        self.create_engine_result = {} if create_engine_result is None else create_engine_result
        self.create_engine_results = list(create_engine_results or [])
        self.create_engine_error = create_engine_error
        self.file_stats_results = list(file_stats_results or [])
        self.file_stats_error = file_stats_error
        self.torrent_url_result = torrent_url_result
        self.is_available_calls = 0
        self.create_engine_calls = []
        self.file_stats_calls = []
        self.torrent_url_calls = []

    def build_class(self):
        script = self

        class FakeServerClient:
            def __init__(self, base_url):
                self.base_url = base_url

            def is_available(self):
                script.is_available_calls += 1
                return script.available

            def resolve_stream(self, stream):
                return script.resolve_url

            def create_engine(self, info_hash):
                script.create_engine_calls.append(info_hash)
                if script.create_engine_error is not None:
                    raise script.create_engine_error
                results = script.create_engine_results
                if not results:
                    return script.create_engine_result
                idx = len(script.create_engine_calls) - 1
                return results[idx] if idx < len(results) else results[-1]

            def file_stats(self, info_hash, file_idx):
                script.file_stats_calls.append((info_hash, file_idx))
                if script.file_stats_error is not None:
                    raise script.file_stats_error
                results = script.file_stats_results
                if not results:
                    return {}
                idx = len(script.file_stats_calls) - 1
                return results[idx] if idx < len(results) else results[-1]

            def torrent_url(self, info_hash, file_idx, announce=None):
                script.torrent_url_calls.append((info_hash, file_idx, tuple(announce or ())))
                if script.torrent_url_result is not None:
                    return script.torrent_url_result
                return '%s/%s/%s' % (self.base_url, info_hash, file_idx)

        return FakeServerClient

    def install(self, monkeypatch, player):
        monkeypatch.setattr(player, 'ServerClient', self.build_class())
        return self


def _torrent_stream(**overrides):
    stream = {
        'infoHash': INFO_HASH,
        'announce': ['udp://tracker.example:80'],
        'title': 'Example Movie',
    }
    stream.update(overrides)
    return stream


def _resolved_one(env):
    assert len(env.resolved) == 1
    return env.resolved[0]


# --- buffer_enable=False: pre-buffer entirely skipped ---------------------


def test_buffer_disabled_skips_engine_and_resolves_immediately(kodi_stubs, monkeypatch):
    env = kodi_stubs.env
    env.addon.settings['buffer_enable'] = False
    script = _ServerScript(resolve_url='http://server/x/0').install(monkeypatch, kodi_stubs.player)

    kodi_stubs.player.play(1, _torrent_stream(fileIdx=0), 'movie', 'tt1')

    assert script.create_engine_calls == []
    assert script.file_stats_calls == []
    assert env.dialog_created == []
    assert env.dialog_closed_count == 0
    handle, succeeded, list_item = _resolved_one(env)
    assert (handle, succeeded) == (1, True)
    assert list_item.path == 'http://server/x/0'


# --- happy path: polls until target reached, then resolves True -----------


def test_happy_path_polls_until_target_then_resolves_true(kodi_stubs, monkeypatch):
    env = kodi_stubs.env
    env.addon.settings['buffer_mb'] = 1  # configured_target = 1 MiB = 1048576 bytes
    below_target = {'streamProgress': 0.5, 'streamLen': 1048576, 'downloadSpeed': 500000, 'peers': 3}
    at_target = {'streamProgress': 1.0, 'streamLen': 1048576, 'downloadSpeed': 500000, 'peers': 5}
    script = _ServerScript(
        resolve_url='http://server/x/0',
        file_stats_results=[below_target, at_target],
    ).install(monkeypatch, kodi_stubs.player)

    kodi_stubs.player.play(2, _torrent_stream(fileIdx=0), 'movie', 'tt2')

    assert script.create_engine_calls == [INFO_HASH]
    assert script.file_stats_calls == [(INFO_HASH, 0), (INFO_HASH, 0)]
    assert env.dialog_created == [('STR30080', 'Example Movie')]
    # percent = min(100, buffered * 100 // target); pinned by the exact
    # byte counts above so a flipped clamp/off-by-one reddens this.
    assert [percent for percent, _ in env.dialog_updates] == [50, 100]
    assert env.dialog_closed_count == 1
    handle, succeeded, list_item = _resolved_one(env)
    assert (handle, succeeded) == (2, True)
    assert list_item.path == 'http://server/x/0'


# --- cancellation: either trigger resolves False and closes the dialog ----


def test_cancel_via_dialog_iscanceled_resolves_false(kodi_stubs, monkeypatch):
    env = kodi_stubs.env
    env.cancel = True
    script = _ServerScript(
        file_stats_results=[{'streamProgress': 0.1, 'streamLen': 1000, 'downloadSpeed': 0, 'peers': 0}],
    ).install(monkeypatch, kodi_stubs.player)

    kodi_stubs.player.play(3, _torrent_stream(fileIdx=0), 'movie', 'tt3')

    assert script.create_engine_calls == [INFO_HASH]
    assert script.file_stats_calls == []  # cancelled before the first poll
    assert env.dialog_closed_count == 1
    handle, succeeded, list_item = _resolved_one(env)
    assert (handle, succeeded) == (3, False)
    assert list_item.path == ''  # xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem())


def test_cancel_via_monitor_waitforabort_resolves_false(kodi_stubs, monkeypatch):
    env = kodi_stubs.env
    env.monitor_abort = True
    script = _ServerScript(
        file_stats_results=[{'streamProgress': 0.1, 'streamLen': 1000, 'downloadSpeed': 0, 'peers': 0}],
    ).install(monkeypatch, kodi_stubs.player)

    kodi_stubs.player.play(4, _torrent_stream(fileIdx=0), 'movie', 'tt4')

    assert len(script.file_stats_calls) == 1  # one poll happens before the abort check
    assert env.monitor_abort_calls == 1
    assert env.dialog_closed_count == 1
    handle, succeeded, list_item = _resolved_one(env)
    assert (handle, succeeded) == (4, False)


# --- ~120 polls without reaching target: notifies 30083, resolves True ----


def test_timeout_after_max_polls_notifies_and_resolves_true(kodi_stubs, monkeypatch):
    env = kodi_stubs.env
    script = _ServerScript(
        resolve_url='http://server/x/0',
        file_stats_results=[{'streamProgress': 0.01, 'streamLen': 1000, 'downloadSpeed': 100, 'peers': 1}],
    ).install(monkeypatch, kodi_stubs.player)

    kodi_stubs.player.play(5, _torrent_stream(fileIdx=0), 'movie', 'tt5')

    assert len(script.file_stats_calls) == 120  # _BUFFER_MAX_WAIT_SECONDS
    assert env.monitor_abort_calls == 120
    assert [msg for _, msg, _, _ in env.notifications] == ['STR30083']
    assert env.dialog_closed_count == 1
    handle, succeeded, list_item = _resolved_one(env)
    assert (handle, succeeded) == (5, True)
    assert list_item.path == 'http://server/x/0'


# --- create_engine()/file_stats() exceptions degrade to immediate play ----


def test_create_engine_exception_degrades_to_resolve_true(kodi_stubs, monkeypatch):
    env = kodi_stubs.env
    script = _ServerScript(
        resolve_url='http://server/x/0',
        create_engine_error=RuntimeError('engine boom'),
    ).install(monkeypatch, kodi_stubs.player)

    kodi_stubs.player.play(6, _torrent_stream(fileIdx=0), 'movie', 'tt6')

    assert script.create_engine_calls == [INFO_HASH]
    assert script.file_stats_calls == []
    assert env.dialog_closed_count == 1
    handle, succeeded, list_item = _resolved_one(env)
    assert (handle, succeeded) == (6, True)
    assert list_item.path == 'http://server/x/0'


def test_file_stats_exception_degrades_to_resolve_true(kodi_stubs, monkeypatch):
    env = kodi_stubs.env
    script = _ServerScript(
        resolve_url='http://server/x/0',
        file_stats_error=RuntimeError('stats boom'),
    ).install(monkeypatch, kodi_stubs.player)

    kodi_stubs.player.play(7, _torrent_stream(fileIdx=0), 'movie', 'tt7')

    assert script.create_engine_calls == [INFO_HASH]
    assert len(script.file_stats_calls) == 1
    assert env.dialog_closed_count == 1
    handle, succeeded, list_item = _resolved_one(env)
    assert (handle, succeeded) == (7, True)
    assert list_item.path == 'http://server/x/0'


# --- missing/None/-1 fileIdx: rebuild via guessedFileIdx + torrent_url ----


@pytest.mark.parametrize(
    'file_idx_override',
    [{}, {'fileIdx': None}, {'fileIdx': -1}],
    ids=['missing', 'none', 'negative_one'],
)
def test_missing_file_idx_rebuilds_url_and_polls_guessed_index(kodi_stubs, monkeypatch, file_idx_override):
    env = kodi_stubs.env
    stream = _torrent_stream(**file_idx_override)
    at_target = {'streamProgress': 1.0, 'streamLen': 1048576, 'downloadSpeed': 1, 'peers': 1}
    script = _ServerScript(
        resolve_url='http://server/x/-1',
        create_engine_result={'guessedFileIdx': 4},
        file_stats_results=[at_target],
        torrent_url_result='http://server/x/4',
    ).install(monkeypatch, kodi_stubs.player)

    kodi_stubs.player.play(8, stream, 'movie', 'tt8')

    assert script.torrent_url_calls == [(INFO_HASH, 4, tuple(stream['announce']))]
    assert script.file_stats_calls == [(INFO_HASH, 4)]  # polls the guessed index, not -1
    handle, succeeded, list_item = _resolved_one(env)
    assert (handle, succeeded) == (8, True)
    assert list_item.path == 'http://server/x/4'  # resolved to the rebuilt url, not the original


# --- v0.8.5 gap: /create never gains guessedFileIdx; files[] appears once
# --- metadata resolves, and /create must be re-polled to see it ----------


@pytest.mark.parametrize(
    'create_engine_result',
    [{}, {'guessedFileIdx': -1}, {'files': []}],
    ids=['absent', 'negative', 'empty_files'],
)
def test_metadata_never_resolves_exhausts_budget_and_proceeds(kodi_stubs, monkeypatch, create_engine_result):
    """Every /create poll comes back with nothing guess_file_idx() can use
    (contract: 'stats never yields files/idx -> budget exhausted ->
    proceed'). This replaces the old immediate-skip expectation: v0.8.5's
    /create response never grows a guessedFileIdx later, so the only sane
    behaviour left is to keep polling for the full budget, then fall back
    to unbuffered playback exactly like a genuine metadata timeout would.
    """
    env = kodi_stubs.env
    script = _ServerScript(
        resolve_url='http://server/x/-1',
        create_engine_result=create_engine_result,
    ).install(monkeypatch, kodi_stubs.player)

    kodi_stubs.player.play(9, _torrent_stream(), 'movie', 'tt9')  # fileIdx missing -> UNKNOWN_FILE_IDX

    assert len(script.create_engine_calls) == 120  # _BUFFER_MAX_WAIT_SECONDS; never resolves an index
    assert env.monitor_abort_calls == 120
    assert script.torrent_url_calls == []
    assert script.file_stats_calls == []  # never reached per-file polling
    assert [msg for _, msg, _, _ in env.notifications] == ['STR30083']
    assert env.dialog_closed_count == 1
    handle, succeeded, list_item = _resolved_one(env)
    assert (handle, succeeded) == (9, True)
    assert list_item.path == 'http://server/x/-1'  # original url, never rebuilt


def test_files_array_without_guessed_idx_picks_largest_file_and_polls_it(kodi_stubs, monkeypatch):
    """v0.8.5 shape confirmed live: /create's response carries `files`
    ([{name, path, length, offset}, ...]) but no `guessedFileIdx` at all -
    guess_file_idx() must pick the largest file itself, and per-file
    polling must engage against that index (not stall like the old
    guessedFileIdx-only code path did).
    """
    env = kodi_stubs.env
    stream = _torrent_stream()  # fileIdx missing -> UNKNOWN_FILE_IDX
    files = [
        {'name': 'sample.mkv', 'length': 1024},
        {'name': 'Sintel.mkv', 'length': 129241752},
        {'name': 'subs.srt', 'length': 2048},
    ]
    at_target = {'streamProgress': 1.0, 'streamLen': 129241752, 'downloadSpeed': 1, 'peers': 1}
    script = _ServerScript(
        resolve_url='http://server/x/-1',
        create_engine_result={'files': files},
        file_stats_results=[at_target],
        torrent_url_result='http://server/x/1',
    ).install(monkeypatch, kodi_stubs.player)

    kodi_stubs.player.play(11, stream, 'movie', 'tt11')

    assert script.create_engine_calls == [INFO_HASH]  # resolved on the very first /create poll
    assert script.torrent_url_calls == [(INFO_HASH, 1, tuple(stream['announce']))]
    assert script.file_stats_calls == [(INFO_HASH, 1)]  # polls the largest file's index, not -1
    handle, succeeded, list_item = _resolved_one(env)
    assert (handle, succeeded) == (11, True)
    assert list_item.path == 'http://server/x/1'


def test_metadata_arrives_on_third_create_poll(kodi_stubs, monkeypatch):
    """The metadata-wait loop must keep re-polling /create (not just call
    it once) and, once resolved, spend only the REMAINING shared budget on
    per-file byte polling - not a fresh 120s.
    """
    env = kodi_stubs.env
    stream = _torrent_stream()  # fileIdx missing -> UNKNOWN_FILE_IDX
    no_metadata_yet = {'peers': 2}
    still_no_metadata = {'peers': 5}
    resolved = {'files': [{'length': 100}, {'length': 900}]}
    at_target = {'streamProgress': 1.0, 'streamLen': 900, 'downloadSpeed': 1, 'peers': 5}
    script = _ServerScript(
        resolve_url='http://server/x/-1',
        create_engine_results=[no_metadata_yet, still_no_metadata, resolved],
        file_stats_results=[at_target],
        torrent_url_result='http://server/x/1',
    ).install(monkeypatch, kodi_stubs.player)

    kodi_stubs.player.play(12, stream, 'movie', 'tt12')

    assert len(script.create_engine_calls) == 3
    assert env.monitor_abort_calls == 2  # one wait after each of the first two unresolved polls
    # metadata-wait phase shows an indeterminate 0% while no file is picked yet
    assert [percent for percent, _ in env.dialog_updates[:2]] == [0, 0]
    assert script.torrent_url_calls == [(INFO_HASH, 1, tuple(stream['announce']))]
    assert script.file_stats_calls == [(INFO_HASH, 1)]  # continues with the shared, not reset, budget
    handle, succeeded, list_item = _resolved_one(env)
    assert (handle, succeeded) == (12, True)
    assert list_item.path == 'http://server/x/1'


def test_cancel_during_metadata_wait_resolves_false(kodi_stubs, monkeypatch):
    env = kodi_stubs.env
    env.cancel = True
    script = _ServerScript(
        create_engine_result={},
    ).install(monkeypatch, kodi_stubs.player)

    kodi_stubs.player.play(13, _torrent_stream(), 'movie', 'tt13')  # fileIdx missing -> UNKNOWN_FILE_IDX

    assert script.create_engine_calls == []  # cancelled before the first /create poll
    assert script.file_stats_calls == []
    assert env.dialog_closed_count == 1
    handle, succeeded, list_item = _resolved_one(env)
    assert (handle, succeeded) == (13, False)
    assert list_item.path == ''


# --- non-torrent streams never engage pre-buffer ---------------------------


def test_non_torrent_stream_never_engages_prebuffer(kodi_stubs, monkeypatch):
    env = kodi_stubs.env
    script = _ServerScript(resolve_url='https://example.com/a.mp4').install(monkeypatch, kodi_stubs.player)

    kodi_stubs.player.play(10, {'url': 'https://example.com/a.mp4'}, 'movie', 'tt10')

    assert script.is_available_calls == 0  # 'url' isn't a _SERVER_DEPENDENT_KEYS entry
    assert script.create_engine_calls == []
    assert script.file_stats_calls == []
    assert env.dialog_created == []
    assert env.dialog_closed_count == 0
    handle, succeeded, list_item = _resolved_one(env)
    assert (handle, succeeded) == (10, True)
    assert list_item.path == 'https://example.com/a.mp4'
