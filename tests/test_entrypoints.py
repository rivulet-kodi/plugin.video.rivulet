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
    def _load(argv, open_home=None):
        with install_kodi_stubs(reload=_RELOAD_MODULE_NAMES) as ctx:
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
# action == 'home': HomeWindow failure falls back to the recovery directory
# ---------------------------------------------------------------------------


def test_home_action_falls_back_to_container_update_on_homewindow_error(load_default):
    def _raise():
        raise RuntimeError('HomeWindow.onInit blew up')

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
# service.py: hands off to lib.service_runner.main() exactly once
# ---------------------------------------------------------------------------


def test_service_calls_service_runner_main_exactly_once(monkeypatch):
    calls = []
    monkeypatch.setattr(service_runner, 'main', lambda: calls.append(True))
    runpy.run_path(SERVICE_PY, run_name='__main__')
    assert calls == [True]
