# SPDX-FileCopyrightText: 2026 M0Rf30
# SPDX-License-Identifier: MIT

"""Behavioral tests for the two real Kodi entry-point scripts, `default.py`
and `service.py`: Kodi invokes each of these as a standalone script (never
imports them as a module), so they're exercised the same way here, via
`runpy.run_path(..., run_name='__main__')` against the shared fake
xbmc*/lib.ui stubs in tests/kodistubs (no real Kodi runtime, no network,
no subprocess).

`default.py` only imports `xbmc`/`xbmcplugin`/`lib.ui.compat`/
`lib.ui.homewindow`/`lib.ui.uicommon` inside its `action == 'home'` branch,
so `load_default` reloads exactly those four `lib.ui.*` modules (plus
`lib.ui.router`, which it always touches) fresh against fresh stubs -
mirroring tests/test_router.py's and tests/test_homewindow.py's own
reload lists. `lib.ui.router.run()` (the non-home dispatch target) and
`lib.ui.homewindow.open_home()` are replaced with call-recording fakes so
this file only asserts on default.py's own argv/query parsing and
home-branch control flow, never on router.run()'s or HomeWindow's
internals (already covered by tests/test_router.py and
tests/test_homewindow.py respectively).

`install_kodi_stubs()` and pytest's `monkeypatch` both restore every
module/attribute/`sys.argv` they touch automatically at teardown, so no
manual cleanup is needed here.
"""
import runpy
import sys
import time
from pathlib import Path

import pytest

import lib.service_runner as service_runner
from lib.ui import urlutil
from tests.kodistubs import install_kodi_stubs

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PY = str(REPO_ROOT / 'default.py')
SERVICE_PY = str(REPO_ROOT / 'service.py')

_RELOAD_MODULE_NAMES = ('lib.ui.compat', 'lib.ui.uicommon', 'lib.ui.router', 'lib.ui.homewindow')


@pytest.fixture
def load_default(monkeypatch):
    """Factory: `load_default(argv, open_home=None)` executes default.py
    fresh via `runpy.run_path` with `sys.argv` set to `argv`, against
    `lib.ui.compat`/`lib.ui.uicommon`/`lib.ui.router`/`lib.ui.homewindow`
    freshly reloaded onto fresh xbmc*/xbmcplugin stubs.

    `lib.ui.router.run()` is replaced with a recorder (`ctx.run_calls`, a
    list of `True` per call) so the non-home dispatch path never touches
    the real `lib.ui.views`/`lib.ui.player`. `lib.ui.homewindow.open_home`
    defaults to a recording no-op (`ctx.open_home_calls`); pass
    `open_home` (e.g. a function that raises) to override it.

    Returns `(namespace, ctx)`: `namespace` is default.py's executed
    global dict (exposing its module-private `_action`/`_raw_qs`), `ctx`
    is `tests.kodistubs`' namespace (`.env`, `.router`, `.homewindow`, ...)
    plus `.run_calls`/`.open_home_calls`.
    """
    def _load(argv, open_home=None, before=None, **stubs):
        with install_kodi_stubs(reload=_RELOAD_MODULE_NAMES, **stubs) as ctx:
            if before is not None:
                before(ctx)
            ctx.run_calls = []
            monkeypatch.setattr(ctx.router, 'run', lambda: ctx.run_calls.append(True))

            ctx.open_home_calls = []

            def _default_open_home():
                ctx.open_home_calls.append(True)

            monkeypatch.setattr(ctx.homewindow, 'open_home', open_home or _default_open_home)

            monkeypatch.setattr(sys, 'argv', argv)
            namespace = runpy.run_path(DEFAULT_PY, run_name='__main__')
            return namespace, ctx
    return _load


# ---------------------------------------------------------------------------
# argv[1] -> router.ADDON_HANDLE (argv[0] is not testable through runpy: it
# always overwrites sys.argv[0] with the executed file's own path, exactly
# like a real `python default.py` invocation would)
# ---------------------------------------------------------------------------


def test_argv_handle_parsed_as_int(load_default):
    _ns, ctx = load_default(['plugin://plugin.video.rivulet/', '7', '?action=meta&id=tt1'])
    assert ctx.router.ADDON_HANDLE == 7
    assert ctx.run_calls == [True]  # non-home action dispatched to router.run()


def test_argv_missing_handle_falls_back_to_negative_one(load_default):
    _ns, ctx = load_default(['plugin://plugin.video.rivulet/'])
    assert ctx.router.ADDON_HANDLE == -1


def test_argv_non_numeric_handle_falls_back_to_negative_one(load_default):
    _ns, ctx = load_default(['plugin://plugin.video.rivulet/', 'not-a-number', '?action=meta'])
    assert ctx.router.ADDON_HANDLE == -1


# ---------------------------------------------------------------------------
# query-string parsing: leading '?' stripped, action extracted/defaulted
# ---------------------------------------------------------------------------


def test_leading_question_mark_is_stripped_and_action_dispatched(load_default):
    ns, ctx = load_default(['plugin://plugin.video.rivulet/', '3', '?action=meta&type=movie&id=tt1'])
    assert ns['_raw_qs'] == 'action=meta&type=movie&id=tt1'
    assert ns['_action'] == 'meta'
    assert ctx.run_calls == [True]
    assert ctx.env.end_of_directory == []  # non-home action never touches the home branch


def test_query_without_leading_question_mark_still_parses_action(load_default):
    ns, ctx = load_default(['plugin://plugin.video.rivulet/', '3', 'action=play'])
    assert ns['_action'] == 'play'
    assert ctx.run_calls == [True]


def test_missing_query_defaults_action_to_home(load_default):
    ns, ctx = load_default(['plugin://plugin.video.rivulet/', '9', ''])
    assert ns['_action'] == 'home'
    assert ctx.run_calls == []
    assert len(ctx.env.end_of_directory) == 1


# ---------------------------------------------------------------------------
# action == 'home': endOfDirectory satisfied exactly once
# ---------------------------------------------------------------------------


def test_home_action_ends_directory_exactly_once_before_the_window_opens(load_default):
    _ns, ctx = load_default(['plugin://plugin.video.rivulet/', '5', '?action=home'])
    assert ctx.env.end_of_directory == [
        {'handle': 5, 'succeeded': True, 'updateListing': False, 'cacheToDisc': False},
    ]


# ---------------------------------------------------------------------------
# action == 'home': HomeWindow success path
# ---------------------------------------------------------------------------


def test_home_action_opens_homewindow_on_success(load_default):
    _ns, ctx = load_default(['plugin://plugin.video.rivulet/', '5', '?action=home'])
    assert ctx.open_home_calls == [True]
    assert not any('Container.Update' in cmd for cmd in ctx.env.executed_builtins)


# ---------------------------------------------------------------------------
# action == 'home': leaving Home also leaves the launch directory (#103)
# ---------------------------------------------------------------------------
#
# Kodi opens the launch in its Videos window on the plugin root; default.py
# answers it empty and Home opens over it. Without a Back of its own, the
# user's Back out of Home landed in that empty window.

_ON_ROOT = {'Window.IsActive(videos)': True}


@pytest.mark.parametrize('folder', [
    'plugin://plugin.video.rivulet/', 'plugin://plugin.video.rivulet', 'plugin://plugin.video.rivulet/?action=home',
])
def test_backing_out_of_home_also_leaves_the_empty_launch_directory(load_default, folder):
    _ns, ctx = load_default(
        ['plugin://plugin.video.rivulet/', '5', ''],
        info_labels={'Container.FolderPath': folder}, cond_visibility=_ON_ROOT,
    )
    assert ctx.open_home_calls == [True]
    assert ctx.env.executed_builtins[-1] == 'Action(Back)'


@pytest.mark.parametrize('folder, conditions', [
    # Launched without a Videos window (RunPlugin, a script): nothing to leave.
    ('plugin://plugin.video.rivulet/', {}),
    # Home jumped into one of the addon's directories: the user is browsing it.
    ('plugin://plugin.video.rivulet/?action=home_classical', _ON_ROOT),
    ('plugin://plugin.video.other/', _ON_ROOT),
    # A dialog is up: Back would close it instead.
    ('plugin://plugin.video.rivulet/', dict(_ON_ROOT, **{'System.HasActiveModalDialog': True})),
])
def test_home_exit_stays_put_anywhere_but_the_bare_launch_directory(load_default, folder, conditions):
    _ns, ctx = load_default(
        ['plugin://plugin.video.rivulet/', '5', ''],
        info_labels={'Container.FolderPath': folder}, cond_visibility=conditions,
    )
    assert ctx.open_home_calls == [True]
    assert 'Action(Back)' not in ctx.env.executed_builtins


def test_a_kodi_shutdown_during_home_sends_no_back(load_default):
    _ns, ctx = load_default(
        ['plugin://plugin.video.rivulet/', '5', ''], monitor_abort_requested=True,
        info_labels={'Container.FolderPath': 'plugin://plugin.video.rivulet/'}, cond_visibility=_ON_ROOT,
    )
    assert 'Action(Back)' not in ctx.env.executed_builtins


# ---------------------------------------------------------------------------
# action == 'home': HomeWindow failure falls back to the recovery directory
# ---------------------------------------------------------------------------


def test_home_action_falls_back_to_container_update_on_homewindow_error(load_default):
    def _raise():
        from lib.ui.homewindow import HomeOpenError
        raise HomeOpenError('construction failed')

    _ns, ctx = load_default(
        ['plugin://plugin.video.rivulet/', '5', '?action=home'], open_home=_raise,
    )

    # endOfDirectory is still satisfied exactly once despite the failure -
    # the handle was already closed before open_home() ever raised.
    assert len(ctx.env.end_of_directory) == 1
    assert any('HomeWindow failed' in msg for msg, _level in ctx.env.log_calls)
    expected_url = urlutil.url_for(ctx.router.BASE_URL, 'home_classical')
    assert ctx.env.executed_builtins[-1] == 'Container.Update(%s)' % expected_url


# ---------------------------------------------------------------------------
# action == 'home': Kodi re-running the plugin root while a UI is alive
# ---------------------------------------------------------------------------
#
# After playback stops Kodi re-activates the Videos window the addon was
# launched from, and CGUIMediaWindow::OnInitWindow() re-fetches a plugin path
# (kodi.log 01:07:59.987 -> 01:08:00.189): a second interpreter running
# default.py with the bare root. It must satisfy the directory handle and
# exit - no HomeWindow, and no Dialog.Close(all), which would close the
# picker that just came back.

_HEARTBEAT = 'rivulet.ui.heartbeat'
_PHASE = 'rivulet.ui.phase'


def _owner(ctx, age=0.0, phase=None):
    props = ctx.env.window_properties.setdefault(10000, {})
    props[_HEARTBEAT] = 'tok|%.3f' % (time.time() - age)
    if phase is not None:
        props[_PHASE] = phase


def _assert_launch_ignored(ctx):
    assert ctx.open_home_calls == []
    assert 'Dialog.Close(all, true)' not in ctx.env.executed_builtins
    assert len(ctx.env.end_of_directory) == 1  # the handle is still satisfied, once
    assert not any('Container.Update' in cmd for cmd in ctx.env.executed_builtins)


def test_bare_launch_after_playback_stopped_in_the_player_phase_is_a_noop(load_default):
    # Phase 2, mid CloseFile: the post-stop refresh. The running UI reopens
    # by itself; neither a second UI nor a show request.
    _ns, ctx = load_default(
        ['plugin://plugin.video.rivulet/', '5', '?action=home'],
        before=lambda ctx: _owner(ctx, phase='player'),
    )
    _assert_launch_ignored(ctx)
    assert not ctx.env.window_properties[10000].get('rivulet.ui.show')


def test_bare_launch_while_the_video_plays_asks_the_running_ui_back(load_default):
    # Issue #73: the user left the fullscreen video and opened Rivulet. The
    # running UI is blocked in its playback wait - wake it, never open a
    # second Home over it.
    def before(ctx):
        _owner(ctx, phase='player')
        ctx.env.player_is_playing = True

    _ns, ctx = load_default(['plugin://plugin.video.rivulet/', '5', '?action=home'], before=before)
    _assert_launch_ignored(ctx)
    assert ctx.env.window_properties[10000].get('rivulet.ui.show')


def test_bare_launch_while_a_ui_is_alive_and_nothing_plays_is_a_noop(load_default):
    # Phase 3 just started: the old UI ended the phase and reopened the
    # picker, then Kodi's delayed refresh arrives.
    _ns, ctx = load_default(
        ['plugin://plugin.video.rivulet/', '5', '?action=home'],
        before=lambda ctx: _owner(ctx, phase='addon'),
    )
    _assert_launch_ignored(ctx)
    assert any('already running' in msg for msg, _level in ctx.env.log_calls)


def test_bare_launch_with_a_live_ui_but_background_playback_still_opens_home(load_default):
    # Kodi IS playing and the earlier session is in its addon phase: a
    # deliberate launch from Kodi's menus, honoured.
    def before(ctx):
        _owner(ctx, phase='addon')
        ctx.env.player_is_playing = True

    _ns, ctx = load_default(['plugin://plugin.video.rivulet/', '5', '?action=home'], before=before)
    assert ctx.open_home_calls == [True]


def test_a_stale_ui_marker_never_locks_the_user_out(load_default):
    # The interpreter was killed: its stamp stays on Window(10000) forever,
    # in whichever phase it died. Past the age limit it counts for nothing.
    _ns, ctx = load_default(
        ['plugin://plugin.video.rivulet/', '5', '?action=home'],
        before=lambda ctx: _owner(ctx, age=60.0, phase='player'),
    )
    assert ctx.open_home_calls == [True]


def test_a_first_launch_with_no_marker_opens_home(load_default):
    _ns, ctx = load_default(['plugin://plugin.video.rivulet/', '5', '?action=home'])
    assert ctx.open_home_calls == [True]
    assert 'Dialog.Close(all, true)' in ctx.env.executed_builtins


def test_a_garbled_marker_is_ignored(load_default):
    def before(ctx):
        ctx.env.window_properties[10000] = {_HEARTBEAT: 'garbage', _PHASE: 'player'}

    _ns, ctx = load_default(['plugin://plugin.video.rivulet/', '5', '?action=home'], before=before)
    assert ctx.open_home_calls == [True]


# ---------------------------------------------------------------------------
# service.py: hands off to lib.service_runner.main() exactly once
# ---------------------------------------------------------------------------


def test_service_calls_service_runner_main_exactly_once(monkeypatch):
    calls = []
    monkeypatch.setattr(service_runner, 'main', lambda: calls.append(True))
    runpy.run_path(SERVICE_PY, run_name='__main__')
    assert calls == [True]


@pytest.mark.parametrize('failure', ['construct', 'build', 'rows'])
def test_home_recovery_is_limited_to_window_open_failures(monkeypatch, failure):
    names = _RELOAD_MODULE_NAMES + ('lib.ui.home', 'lib.ui.look', 'lib.ui.dependencies')
    with install_kodi_stubs(reload=names) as ctx:
        opened = []
        original = ctx.homewindow.open_window

        class Store:
            def get_auth(self):
                if failure == 'build':
                    raise RuntimeError('https://user:secret@example.test/secret')
                return None

            def get_enabled_addons(self):
                return []

        class Window(ctx.home.HomeWindow):
            def doModal(self):
                self.onInit()
                opened.append(self)
                assert not self.closed
                assert all(not self.getProperty(prop) for _lid, prop, _counter in
                           ctx.home._ROW_COUNTERS)

        def construct(cls, xml):
            if failure == 'construct':
                raise RuntimeError('https://user:secret@example.test/secret')
            return original(cls, xml)

        def broken_rows(*args):
            raise RuntimeError('https://user:secret@example.test/secret')

        monkeypatch.setattr(ctx.home, 'HomeWindow', Window)
        monkeypatch.setattr(ctx.home, 'get_store', Store)
        monkeypatch.setattr(ctx.home, '_fill_shelves', broken_rows)
        monkeypatch.setattr(ctx.homewindow, 'open_window', construct)
        monkeypatch.setattr(sys, 'argv', ['plugin://plugin.video.rivulet/', '5', ''])
        runpy.run_path(DEFAULT_PY, run_name='__main__')
        recovered = any('Container.Update' in cmd for cmd in ctx.env.executed_builtins)
        assert recovered is (failure == 'construct')
        assert bool(opened) is (failure != 'construct')
        assert any('failed' in msg for msg, _level in ctx.env.log_calls)
        assert not any('secret' in msg or 'user:' in msg for msg, _level in ctx.env.log_calls)


@pytest.mark.parametrize('startup', ['launch_action', 'dismiss_busy_dialog'])
def test_startup_probe_and_busy_cleanup_errors_recover_safely(load_default, monkeypatch, startup):
    def prepare(ctx):
        def fail():
            raise RuntimeError('https://user:secret@host/private?token=hidden')
        monkeypatch.setattr(ctx.uicommon, startup, fail)

    _ns, ctx = load_default(['plugin://plugin.video.rivulet/', '5', ''], before=prepare)
    assert ctx.open_home_calls == []
    assert any('Container.Update' in cmd for cmd in ctx.env.executed_builtins)
    messages = [msg for msg, _ in ctx.env.log_calls if 'failed to open' in msg]
    assert len(messages) == 1 and 'RuntimeError' in messages[0]
    assert 'secret' not in messages[0] and 'hidden' not in messages[0]


def test_a_failing_show_request_recovers_safely(load_default, monkeypatch):
    # Issue #73 path (alive UI in the player phase while a video plays): if
    # posting the show request itself fails, recovery runs once and no second
    # Home opens over the running one.
    def before(ctx):
        _owner(ctx, phase='player')
        ctx.env.player_is_playing = True

        def fail():
            raise RuntimeError('https://user:secret@host/private?token=hidden')
        monkeypatch.setattr(ctx.uicommon, 'request_ui_show', fail)

    _ns, ctx = load_default(['plugin://plugin.video.rivulet/', '5', ''], before=before)
    assert ctx.open_home_calls == []
    assert any('Container.Update' in cmd for cmd in ctx.env.executed_builtins)
    messages = [msg for msg, _ in ctx.env.log_calls if 'failed to open' in msg]
    assert len(messages) == 1 and 'RuntimeError' in messages[0]
    assert 'secret' not in messages[0] and 'hidden' not in messages[0]
