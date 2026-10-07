# SPDX-FileCopyrightText: 2026 M0Rf30
# SPDX-License-Identifier: MIT

"""Back (Backspace=92, Esc=10, 9) during a blocking fetch under a spinner.

Two defects this file pins, both found in kodi.log (a 45 s first-page
catalog fetch whose spinner ignored every Back press):

1. Nothing was cancellable at all in most screens: the network call ran on
   the GUI thread under `busy_dialog()`, which never polled
   `iscanceled()`. `uicommon.run_cancellable()` now runs it on a worker and
   polls.
2. Even the loops that DID poll (`searchwindow._collect_answers()`, the
   streams fan-out) polled blind: Kodi does not run a script's window
   callbacks on arrival, it queues them and delivers them from
   `Monitor.waitForAbort()` (`MakePendingCalls()` every 100 ms slice,
   xbmc/interfaces/legacy/Monitor.cpp) - and a thread parked in
   `Queue.get()` never calls it, so the spinner's `onAction(Back)` simply
   did not run until something else called into Kodi.

To model (2) faithfully a Back press is delivered ONLY from inside a
`Monitor.waitForAbort()` call (`_press_back_on_pump`), never from a bare
`iscanceled()` read. Blocking fetches are modelled with `_Gate`, a
callable that parks until released, so "cancelled promptly" is a wall-clock
assertion rather than a mock call count.
"""
import contextlib
import queue
import threading
import time

import pytest

from tests.conftest import make_window, stub_choose, wire_store
from tests.kodistubs import install_kodi_stubs

_RELOAD_MODULE_NAMES = (
    'lib.ui.compat', 'lib.ui.router', 'lib.ui.uicommon', 'lib.ui.dialogs', 'lib.ui.dependencies',
    'lib.ui.views', 'lib.ui.streamswindow', 'lib.ui.searchwindow', 'lib.ui.infowindow',
    'lib.ui.detailwindow', 'lib.ui.gridwindow', 'lib.ui.mystuff', 'lib.ui.catalogpicker',
    'lib.ui.addonswindow', 'lib.ui.addoncatalogwindow',
)

#: Generous wall-clock bound for "returns promptly": the poll slice is 0.1 s,
#: so a healthy cancel lands in ~0.1-0.2 s; the 0.5 s promise leaves slack
#: for a loaded CI box without ever approaching a real network timeout.
_PROMPT_SECONDS = 1.0


@pytest.fixture
def load_ui():
    with contextlib.ExitStack() as stack:
        def _load(**kwargs):
            return stack.enter_context(install_kodi_stubs(reload=_RELOAD_MODULE_NAMES, **kwargs))

        yield _load


class _Gate:
    """A blocking call: parks the calling (worker) thread until `release()`.
    `value` is what the call returns once released; `started` is set the
    moment a worker is parked, `finished` once it has returned."""

    def __init__(self, value=None):
        self.value = value
        self.started = threading.Event()
        self.finished = threading.Event()
        self._release = threading.Event()

    def __call__(self, *args, **kwargs):
        self.started.set()
        self._release.wait(10)
        self.finished.set()
        return self.value

    def release(self):
        self._release.set()


@contextlib.contextmanager
def _gate(value=None):
    gate = _Gate(value)
    try:
        yield gate
    finally:
        gate.release()  # never leave a parked daemon worker behind


def _track_spinners(monkeypatch, ctx):
    """Every `RivuletBusy` created from now on, in creation order."""
    spinners = []
    real_create = ctx.dialogs.RivuletBusy.create

    def create(self, heading, message=''):
        real_create(self, heading, message)
        spinners.append(self)

    monkeypatch.setattr(ctx.dialogs.RivuletBusy, 'create', create)
    return spinners


def _press_back_on_pump(ctx, spinners, action_id=92, after=2):
    """Deliver a Back press to the newest spinner exactly the way Kodi does:
    from inside the `after`-th `Monitor.waitForAbort()` call, through the
    spinner window's real `onAction()`. The default 2 skips the one pump
    `run_cancellable()` makes right after the spinner appears (that one is
    the "Back queued before the spinner" case, tested separately), so the
    press lands while the call is genuinely in flight."""
    import xbmcgui

    def monitor_abort(call_count):
        if call_count == after and spinners:
            spinners[-1]._window.onAction(xbmcgui.Action(action_id))
        return False

    ctx.env.monitor_abort = monitor_abort


def _assert_prompt(started):
    assert time.monotonic() - started < _PROMPT_SECONDS


# ---------------------------------------------------------------------------
# uicommon.run_cancellable() / wait_cancellable() / deliver_queued_input()
# ---------------------------------------------------------------------------


def test_run_cancellable_returns_the_workers_value_and_closes_the_spinner(load_ui, monkeypatch):
    ctx = load_ui()
    spinners = _track_spinners(monkeypatch, ctx)

    result = ctx.uicommon.run_cancellable(lambda: [1, 2, 3], 'Loading')

    assert result == [1, 2, 3]
    assert len(spinners) == 1
    assert spinners[0]._window is None  # closed


def test_run_cancellable_reraises_what_the_call_raised(load_ui, monkeypatch):
    from lib.stremio.addons import AddonError

    ctx = load_ui()
    spinners = _track_spinners(monkeypatch, ctx)

    def boom():
        raise AddonError('upstream down')

    with pytest.raises(AddonError):
        ctx.uicommon.run_cancellable(boom, 'Loading')
    assert spinners[0]._window is None  # the spinner never outlives the call


@pytest.mark.parametrize('action_id', [92, 10, 9])
def test_back_pressed_during_a_blocking_call_cancels_promptly(load_ui, monkeypatch, action_id):
    """All three back ids, delivered via the monitor like on a device: the
    caller gets CANCELLED within the poll slice, with the spinner closed,
    while the worker is STILL parked (a socket read cannot be interrupted)."""
    ctx = load_ui()
    spinners = _track_spinners(monkeypatch, ctx)
    _press_back_on_pump(ctx, spinners, action_id=action_id)

    with _gate('late') as gate:
        started = time.monotonic()
        result = ctx.uicommon.run_cancellable(gate, 'Loading')

        assert result is ctx.uicommon.CANCELLED
        _assert_prompt(started)
        assert gate.started.is_set() and not gate.finished.is_set()
        assert spinners[0]._window is None
    # Every Monitor call used a positive timeout: waitForAbort(0) means
    # "wait forever" in Kodi.
    assert ctx.env.wait_calls
    assert all(timeout and timeout > 0 for timeout in ctx.env.wait_calls)


def test_deliver_queued_input_calls_the_monitor_with_a_positive_timeout_and_reports_shutdown(load_ui):
    """It is the monitor call that makes Kodi run queued callbacks, and a
    zero timeout would mean "wait forever" there."""
    ctx = load_ui()

    assert ctx.uicommon.deliver_queued_input() is False
    ctx.env.monitor_abort = True
    assert ctx.uicommon.deliver_queued_input() is True
    assert ctx.env.wait_calls and all(timeout > 0 for timeout in ctx.env.wait_calls)


def test_late_result_of_a_cancelled_call_is_discarded(load_ui, monkeypatch):
    """The orphaned worker finishes after the caller left: nothing is
    raised in the caller and its value is never delivered anywhere."""
    ctx = load_ui()
    spinners = _track_spinners(monkeypatch, ctx)
    _press_back_on_pump(ctx, spinners)
    delivered = []

    with _gate('late') as gate:
        result = ctx.uicommon.run_cancellable(lambda: delivered.append(gate()), 'Loading')
        assert result is ctx.uicommon.CANCELLED
        gate.release()
        assert gate.finished.wait(5)
    time.sleep(0.05)
    assert result is ctx.uicommon.CANCELLED  # the late value never replaces it
    assert spinners[0]._window is None  # and nothing re-opened the spinner


def test_kodi_shutdown_cancels_the_wait(load_ui, monkeypatch):
    ctx = load_ui()
    _track_spinners(monkeypatch, ctx)
    ctx.env.monitor_abort = True

    with _gate() as gate:
        started = time.monotonic()
        assert ctx.uicommon.run_cancellable(gate, 'Loading') is ctx.uicommon.CANCELLED
        _assert_prompt(started)


def test_back_landing_as_the_call_finishes_wins_over_the_result(load_ui, monkeypatch):
    ctx = load_ui()
    _track_spinners(monkeypatch, ctx)
    monkeypatch.setattr(ctx.dialogs.RivuletBusy, 'iscanceled', lambda self: True)

    assert ctx.uicommon.run_cancellable(lambda: 'result', 'Loading') is ctx.uicommon.CANCELLED


def test_wait_cancellable_works_with_any_dialog_exposing_iscanceled(load_ui):
    ctx = load_ui()
    flags = {'cancel': False}

    class _Dialog:
        def iscanceled(self):
            return flags['cancel']

    def monitor_abort(call_count):
        flags['cancel'] = True
        return False

    ctx.env.monitor_abort = monitor_abort
    with _gate() as gate:
        assert ctx.uicommon.wait_cancellable(gate, _Dialog()) is ctx.uicommon.CANCELLED


def test_cancelled_sentinel_is_falsy_but_distinct_from_empty_results(load_ui):
    ctx = load_ui()
    cancelled = ctx.uicommon.CANCELLED
    assert not cancelled
    assert cancelled is not None and cancelled != [] and repr(cancelled) == 'CANCELLED'


def _manifest_client(gate):
    """A client whose `manifest()` parks on `gate`."""
    class _Client:
        def manifest(self, url):
            return gate()

    return _Client()


def test_fetch_and_validate_addon_reports_a_cancel_not_an_error_id(load_ui, monkeypatch):
    ctx = load_ui()
    spinners = _track_spinners(monkeypatch, ctx)
    _press_back_on_pump(ctx, spinners)

    with _gate() as gate:
        result = ctx.uicommon.fetch_and_validate_addon(
            _manifest_client(gate), 'https://a.example/manifest.json',
        )

    assert result == (None, None, ctx.uicommon.CANCELLED)


# ---------------------------------------------------------------------------
# Every screen that waits behind a spinner
# ---------------------------------------------------------------------------


def test_back_during_a_slow_catalog_first_page_returns_to_the_picker_and_never_opens_it_later(
    load_ui, monkeypatch,
):
    """The 45 s case from kodi.log: Back on the spinner returns to the
    picker (no notification, no coverflow); when the orphaned fetch finally
    answers, nothing opens."""
    ctx = load_ui()
    spinners = _track_spinners(monkeypatch, ctx)
    _press_back_on_pump(ctx, spinners)
    opened = []
    monkeypatch.setattr(ctx.infowindow, 'open_showcase', lambda *a, **k: opened.append(a) or None)
    win = make_window(ctx.catalogpicker.CatalogPickerWindow)
    metas = [{'id': 'tt1', 'name': 'One', 'type': 'movie'}]

    with _gate(metas) as gate:
        monkeypatch.setattr(ctx.views, '_fetch_catalog', lambda *a, **k: gate())
        started = time.monotonic()
        win._open_catalog('https://a.example/manifest.json', {'name': 'A'}, {'type': 'movie', 'id': 'top'})

        _assert_prompt(started)
        assert not gate.finished.is_set()
        gate.release()
        assert gate.finished.wait(5)
        time.sleep(0.1)

    assert opened == []
    assert ctx.env.notifications == []
    assert win.closed is False and win.should_close_caller is False
    assert spinners[0]._window is None


def test_back_during_open_detail_meta_fetch_returns_false_and_opens_nothing(load_ui, monkeypatch):
    ctx = load_ui()
    spinners = _track_spinners(monkeypatch, ctx)
    _press_back_on_pump(ctx, spinners)
    monkeypatch.setattr(ctx.streamswindow, 'open_streams', lambda *a, **k: pytest.fail('streams opened'))

    with _gate({'id': 'tt1', 'name': 'X'}) as gate:
        monkeypatch.setattr(ctx.views, '_fetch_meta', lambda stype, sid: gate())
        started = time.monotonic()
        result = ctx.detailwindow.open_detail('movie', 'tt1')

        _assert_prompt(started)
    assert result is False
    assert ctx.env.notifications == []  # Back is "go back", not "not found"


class _AuthStore:
    def get_auth(self):
        return {'authKey': 'tok'}


class _SlowApi:
    def __init__(self, gate):
        self._gate = gate
        self.put_calls = []

    def get_library_item(self, auth_key, item_id):
        return self._gate()

    def put_library_item(self, auth_key, payload):
        self.put_calls.append(payload)
        return self._gate()


def test_back_during_the_library_lookup_opens_no_context_menu(load_ui, monkeypatch):
    ctx = load_ui()
    spinners = _track_spinners(monkeypatch, ctx)
    _press_back_on_pump(ctx, spinners)
    win = make_window(ctx.detailwindow.DetailWindow)
    win.meta = {'id': 'tt1', 'name': 'Show'}
    choose_calls = []
    stub_choose(monkeypatch, ctx, -1, capture=choose_calls)
    monkeypatch.setattr(ctx.dependencies, 'get_store', lambda: _AuthStore())

    with _gate() as gate:
        monkeypatch.setattr(ctx.dependencies, 'get_api', lambda: _SlowApi(gate))
        started = time.monotonic()
        win._open_context_menu()

        _assert_prompt(started)
    assert choose_calls == []
    assert ctx.env.notifications == []


def test_back_during_a_library_write_is_not_reported_as_success_or_failure(load_ui, monkeypatch):
    ctx = load_ui()
    spinners = _track_spinners(monkeypatch, ctx)
    _press_back_on_pump(ctx, spinners)
    win = make_window(ctx.detailwindow.DetailWindow)

    with _gate() as gate:
        monkeypatch.setattr(ctx.dependencies, 'get_api', lambda: _SlowApi(gate))
        assert win._push_library_item({'authKey': 'tok'}, {'_id': 'tt1'}) is False
    assert ctx.env.notifications == []


def test_back_during_the_showcase_credits_meta_fetch_opens_no_picker(load_ui, monkeypatch):
    ctx = load_ui()
    spinners = _track_spinners(monkeypatch, ctx)
    _press_back_on_pump(ctx, spinners)
    monkeypatch.setattr(ctx.dependencies, 'get_store', lambda: object())
    monkeypatch.setattr(ctx.dependencies, 'get_client', lambda: object())
    monkeypatch.setattr(ctx.infowindow, 'open_credits_picker', lambda *a, **k: pytest.fail('picker opened'))
    win = ctx.infowindow.ShowcaseWindow('ShowcaseWindow.xml', '/addon/path', 'Default', '1080i')
    win.metas = [{'id': 'tt1', 'name': 'One', 'type': 'movie'}]
    win.onInit()
    win.getControl(ctx.infowindow.SELECT).selected_index = 0

    with _gate({'id': 'tt1'}) as gate:
        monkeypatch.setattr(ctx.views, '_fetch_meta', lambda stype, sid: gate())
        started = time.monotonic()
        win._open_credits()

        _assert_prompt(started)


def test_back_during_a_discover_link_fetch_opens_no_results(load_ui, monkeypatch):
    from urllib.parse import quote

    ctx = load_ui()
    spinners = _track_spinners(monkeypatch, ctx)
    _press_back_on_pump(ctx, spinners)
    transport = 'https://a.example/manifest.json'
    url = 'stremio:///discover/%s/movie/top?genre=Drama' % quote(transport, safe='')
    meta = {'id': 'tt1', 'type': 'movie', 'links': [{'name': 'Drama', 'category': 'Genres', 'url': url}]}

    class _Store:
        def get_enabled_addons(self):
            return [{'transportUrl': transport}]

    stub_choose(monkeypatch, ctx, 0)
    monkeypatch.setattr(ctx.infowindow, 'open_showcase', lambda *a, **k: pytest.fail('coverflow opened'))

    with _gate([{'id': 'tt2', 'name': 'Two'}]) as gate:
        monkeypatch.setattr(ctx.views, '_fetch_catalog', lambda *a, **k: gate())
        started = time.monotonic()
        ctx.infowindow.open_credits_picker(_Store(), object(), meta)

        _assert_prompt(started)
    assert ctx.env.notifications == []


def test_back_during_my_stuff_load_opens_no_grid(load_ui, monkeypatch):
    ctx = load_ui()
    spinners = _track_spinners(monkeypatch, ctx)
    _press_back_on_pump(ctx, spinners)

    class _Store:
        def get_progress_entries(self):
            return []

    monkeypatch.setattr(ctx.mystuff, 'get_store', lambda: _Store())
    monkeypatch.setattr(ctx.gridwindow, 'open_grid', lambda *a, **k: pytest.fail('grid opened'))

    with _gate([]) as gate:
        monkeypatch.setattr(ctx.mystuff, '_fetch_library_entries', lambda store: gate())
        started = time.monotonic()
        assert ctx.mystuff.open_my_stuff() is False

        _assert_prompt(started)
    assert ctx.env.notifications == []  # not even "nothing here yet"


def test_back_during_the_first_addon_catalog_load_leaves_the_screen(load_ui, monkeypatch):
    ctx = load_ui()
    spinners = _track_spinners(monkeypatch, ctx)
    _press_back_on_pump(ctx, spinners)

    class _Store:
        def get_addons(self):
            return []

    wire_store(ctx.addoncatalogwindow, _Store())
    win = make_window(ctx.addoncatalogwindow.AddonCatalogWindow)
    rendered = []
    win._render = lambda: rendered.append(True)

    with _gate([]) as gate:
        win._fetch_entries = lambda installed: gate()
        started = time.monotonic()
        win.onInit()

        _assert_prompt(started)
    assert win.closed is True  # nothing to show: the one Back leaves the screen
    assert rendered == []


def test_back_during_an_addon_catalog_reload_keeps_the_listed_entries(load_ui, monkeypatch):
    ctx = load_ui()
    spinners = _track_spinners(monkeypatch, ctx)
    _press_back_on_pump(ctx, spinners)

    class _Store:
        def get_addons(self):
            return []

    wire_store(ctx.addoncatalogwindow, _Store())
    win = make_window(ctx.addoncatalogwindow.AddonCatalogWindow)
    existing = [{'transportUrl': 'https://x.example/manifest.json', 'manifest': {'id': 'x', 'name': 'X'}}]
    win.entries = list(existing)
    rendered = []
    win._render = lambda: rendered.append(True)

    with _gate([]) as gate:
        win._fetch_entries = lambda installed: gate()
        win._reload()

    assert win.entries == existing
    assert win.closed is False
    assert rendered == [True]


def test_back_during_the_addon_manifest_fetch_installs_nothing(load_ui, monkeypatch):
    ctx = load_ui(dialog_inputs=['https://a.example/manifest.json'])
    spinners = _track_spinners(monkeypatch, ctx)
    _press_back_on_pump(ctx, spinners)
    installs = []

    class _Store:
        def install_addon(self, *args):
            installs.append(args)

    win = make_window(ctx.addonswindow.AddonsWindow)
    win.store = _Store()

    with _gate({'id': 'x'}) as gate:
        monkeypatch.setattr(ctx.addonswindow, 'get_client', lambda: _manifest_client(gate))
        started = time.monotonic()
        win._install()

        _assert_prompt(started)
    assert installs == []
    assert ctx.env.notifications == []  # no "invalid manifest" toast for a user cancel


def test_search_collect_answers_sees_back_while_no_answer_arrives(load_ui, monkeypatch):
    """`_collect_answers()` blocked in `Queue.get(timeout=...)` never called
    into Kodi, so its `dialog.iscanceled()` stayed False for as long as the
    slowest catalog took; it now pumps on every empty slice."""
    ctx = load_ui()
    spinners = _track_spinners(monkeypatch, ctx)
    _press_back_on_pump(ctx, spinners, after=2)
    monkeypatch.setattr(ctx.searchwindow, '_SEARCH_POLL_SECONDS', 0.01)
    report = ctx.searchwindow.SearchReport()
    jobs = [(0, {'name': 'Slow catalog'})]
    results = queue.Queue()
    # Safety net: a regression (no pump) would otherwise spin forever, so
    # let the "slow catalog" answer after 3 s and fail the assertions below.
    timer = threading.Timer(3, lambda: results.put((0, ([], None))))
    timer.daemon = True
    timer.start()

    started = time.monotonic()
    try:
        with ctx.uicommon.busy_dialog('Searching') as dialog:
            answered = ctx.searchwindow._collect_answers(dialog, results, jobs, report)
    finally:
        timer.cancel()

    _assert_prompt(started)
    assert answered == {}
    assert report.canceled is True


def test_back_while_the_series_screen_computes_new_episodes_returns_without_opening_it(load_ui, monkeypatch):
    """The Series row's New Episodes meta fan-out ran with no spinner and no
    way out; it now sits behind a cancellable spinner and Back returns to
    the caller (Home) without ever constructing the picker."""
    ctx = load_ui()
    spinners = _track_spinners(monkeypatch, ctx)
    _press_back_on_pump(ctx, spinners)
    addon = {
        'transportUrl': 'https://a.example/manifest.json',
        'manifest': {
            'id': 'a', 'name': 'A', 'resources': ['catalog'], 'types': ['series'],
            'catalogs': [{'type': 'series', 'id': 'top'}],
        },
    }

    class _Store:
        def get_enabled_addons(self):
            return [addon]

    wire_store(ctx.catalogpicker, _Store())
    monkeypatch.setattr(
        ctx.catalogpicker.CatalogPickerWindow, 'start',
        lambda *a, **k: pytest.fail('picker opened'),
    )

    with _gate([]) as gate:
        monkeypatch.setattr(ctx.catalogpicker, '_new_episode_items', lambda store: gate())
        started = time.monotonic()
        result = ctx.catalogpicker.open_catalog_picker(types=['series', 'tv'], heading='Series')

        _assert_prompt(started)
    assert result is False
    assert ctx.env.notifications == []


# ---------------------------------------------------------------------------
# Phases: Back / clicks across the 1 (addon) -> 2 (player) -> 3 (addon)
# transitions. Phase state lives in uicommon (`input_suppressed()`); every
# `ModalStackWindow` screen is gated in one place by `__init_subclass__`.
# ---------------------------------------------------------------------------


def _screens(ctx):
    """One instance of every Back-closable Rivulet screen, built the way each
    one's own tests build it."""
    showcase = ctx.infowindow.ShowcaseWindow('ShowcaseWindow.xml', '/addon/path', 'Default', '1080i')
    showcase.metas = [{'id': 'tt1', 'name': 'One', 'type': 'movie'}]
    showcase.onInit()
    screens = {
        'catalogpicker': make_window(ctx.catalogpicker.CatalogPickerWindow),
        'detail': make_window(ctx.detailwindow.DetailWindow),
        'showcase': showcase,
        'grid': make_window(ctx.gridwindow.GridWindow),
        'addons': make_window(ctx.addonswindow.AddonsWindow),
        'addoncatalog': make_window(ctx.addoncatalogwindow.AddonCatalogWindow),
        'search': make_window(ctx.searchwindow.SearchWindow),
        'streams': make_window(ctx.streamswindow.StreamsWindow),
    }
    return screens


@pytest.mark.parametrize('action_id', [92, 10, 9])
def test_every_screen_closes_on_all_three_back_ids_in_the_addon_phase(load_ui, action_id):
    ctx = load_ui()
    import xbmcgui
    for name, win in _screens(ctx).items():
        win.onAction(xbmcgui.Action(action_id))
        assert win.closed is True, name


def test_no_screen_reacts_to_back_while_the_player_owns_the_input_and_all_do_again_afterwards(load_ui):
    """Phase 2: a Back queued before the handoff and delivered while Kodi's
    player owns the input must NOT close/rewind a screen (it belongs to the
    OSD); the same press works again as soon as phase 3 begins."""
    ctx = load_ui()
    import xbmcgui
    screens = _screens(ctx)

    ctx.uicommon.begin_player_phase()
    for name, win in screens.items():
        win.onAction(xbmcgui.Action(92))
        assert win.closed is False, name

    ctx.uicommon.end_player_phase()
    for name, win in screens.items():
        win.onAction(xbmcgui.Action(92))
        assert win.closed is True, name


def test_a_click_queued_during_the_player_phase_opens_nothing(load_ui, monkeypatch):
    """Replayed picks were the other half of the stale-input bug: a click
    delivered in phase 2 must not start another fetch/screen."""
    ctx = load_ui()
    picker = make_window(ctx.catalogpicker.CatalogPickerWindow)
    monkeypatch.setattr(
        picker, '_open_catalog', lambda *a, **k: pytest.fail('a queued click opened a catalog'),
    )
    picker.catalogs = [('https://a.example/manifest.json', {'name': 'A'}, {'type': 'movie', 'id': 'top'})]
    picker.onInit()
    picker.getControl(ctx.catalogpicker.LIST).selected_index = 0

    ctx.uicommon.begin_player_phase()
    picker.onClick(ctx.catalogpicker.LIST)

    ctx.uicommon.end_player_phase()


def test_a_spinner_cancel_is_not_honoured_in_the_player_phase_but_works_before_and_after(load_ui, monkeypatch):
    """Phase 1 -> 2 -> 3 on the transient dialogs: Back cancels in phase 1,
    is ignored by a spinner still on screen during phase 2, and cancels again
    in phase 3 (a fresh spinner)."""
    ctx = load_ui()
    import xbmcgui
    first = ctx.dialogs.RivuletBusy()
    first.create('Loading')
    first._window.onAction(xbmcgui.Action(92))
    assert first.iscanceled() is True  # phase 1
    first.close()

    ctx.uicommon.begin_player_phase()
    second = ctx.dialogs.RivuletBusy()
    second.create('Loading')
    second._window.onAction(xbmcgui.Action(92))
    assert second.iscanceled() is False  # phase 2
    second.close()

    ctx.uicommon.end_player_phase()
    third = ctx.dialogs.RivuletBusy()
    third.create('Loading')
    third._window.onAction(xbmcgui.Action(92))
    assert third.iscanceled() is True  # phase 3
    third.close()


# ---------------------------------------------------------------------------
# Addon install: the Stremio account push runs under a cancellable spinner
# ---------------------------------------------------------------------------


class _SyncStore:
    def __init__(self, auth=None):
        self.auth = auth if auth is not None else {'authKey': 'k'}
        self.auth_set_calls = []

    def get_auth(self):
        return self.auth

    def get_addons(self):
        return [{'transportUrl': 'https://a.example/manifest.json', 'manifest': {'id': 'a'}}]

    def set_auth(self, value):
        self.auth_set_calls.append(value)


def _fake_api(ctx, monkeypatch, push):
    class _Api:
        def addon_collection_set(self, auth_key, payload):
            return push(auth_key, payload)

    monkeypatch.setattr(ctx.views, 'StremioAPI', _Api)


def test_back_during_the_account_push_returns_promptly_and_reports_nothing(load_ui, monkeypatch):
    """The push after an install used to block the screen with no spinner and
    no way out. Cancelled it is neither a success nor a failure: no toast, the
    auth is kept, the caller gets False."""
    ctx = load_ui()
    spinners = _track_spinners(monkeypatch, ctx)
    _press_back_on_pump(ctx, spinners)
    store = _SyncStore()

    with _gate() as gate:
        _fake_api(ctx, monkeypatch, lambda auth_key, payload: gate())
        started = time.monotonic()
        result = ctx.views._sync_addons_if_logged_in(store, notify_success=True, cancellable=True)

        _assert_prompt(started)
        assert gate.started.is_set() and not gate.finished.is_set()
    assert result is False
    assert len(spinners) == 1 and spinners[0]._window is None
    assert ctx.env.notifications == []
    assert store.auth_set_calls == []


def test_cancellable_account_push_reports_success_and_failure_on_the_calling_thread(load_ui, monkeypatch):
    from lib.stremio.api import ApiError

    ctx = load_ui()
    spinners = _track_spinners(monkeypatch, ctx)
    pushed = []
    _fake_api(ctx, monkeypatch, lambda auth_key, payload: pushed.append((auth_key, payload)))

    assert ctx.views._sync_addons_if_logged_in(_SyncStore(), notify_success=True, cancellable=True) is True
    assert pushed and pushed[0][0] == 'k'
    assert [msg for _, msg, _, _ in ctx.env.notifications] == ['STR30034']
    assert len(spinners) == 1  # it really ran under the spinner

    ctx.env.notifications.clear()

    def fail(auth_key, payload):
        raise ApiError('nope')

    _fake_api(ctx, monkeypatch, fail)
    assert ctx.views._sync_addons_if_logged_in(_SyncStore(), cancellable=True) is False
    assert [msg for _, msg, _, _ in ctx.env.notifications] == ['STR30035']


def test_account_push_without_cancellable_flag_shows_no_spinner(load_ui, monkeypatch):
    """RunPlugin/login paths keep their old inline behaviour."""
    ctx = load_ui()
    spinners = _track_spinners(monkeypatch, ctx)
    _fake_api(ctx, monkeypatch, lambda auth_key, payload: None)

    assert ctx.views._sync_addons_if_logged_in(_SyncStore()) is True
    assert spinners == []


def test_syncing_string_exists_in_every_locale():
    import glob
    import os

    root = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'resources', 'language')
    files = glob.glob(os.path.join(root, '*', 'strings.po'))
    assert len(files) == 14
    for path in files:
        with open(path, encoding='utf-8') as handle:
            assert 'msgctxt "#30410"' in handle.read(), path


# ---------------------------------------------------------------------------
# A Back queued just before a spinner appears is redirected to the spinner
# ---------------------------------------------------------------------------


def _count_closes(win):
    calls = []
    real_close = win.close

    def close():
        calls.append(True)
        real_close()

    win.close = close
    return calls


def test_back_reaching_a_screen_while_a_spinner_is_up_cancels_the_spinner_not_the_screen(load_ui):
    ctx = load_ui()
    import xbmcgui

    for name, win in _screens(ctx).items():
        closes = _count_closes(win)
        spinner = ctx.dialogs.RivuletBusy()
        spinner.create('Loading')
        assert ctx.uicommon.transient_dialog_active() is True

        win.onAction(xbmcgui.Action(92))

        assert spinner.iscanceled() is True, name
        assert closes == [] and win.closed is False, name
        spinner.close()
        assert ctx.uicommon.transient_dialog_active() is False

        win.onAction(xbmcgui.Action(92))  # normal Back once the spinner is gone
        assert closes == [True] and win.closed is True, name


def test_a_progress_dialog_redirects_back_the_same_way(load_ui):
    ctx = load_ui()
    import xbmcgui

    win = make_window(ctx.streamswindow.StreamsWindow)
    progress = ctx.dialogs.RivuletProgress()
    progress.create('Preparing')

    win.onAction(xbmcgui.Action(10))

    assert progress.iscanceled() is True
    assert win.closed is False
    progress.close()
    win.onAction(xbmcgui.Action(10))
    assert win.closed is True


def test_non_back_actions_still_reach_the_screen_while_a_spinner_is_up(load_ui):
    ctx = load_ui()
    import xbmcgui

    seen = []

    class _Screen(ctx.uicommon.BaseWindow):
        def onAction(self, action):
            seen.append(action.getId())

    win = make_window(_Screen)
    spinner = ctx.dialogs.RivuletBusy()
    spinner.create('Loading')
    win.onAction(xbmcgui.Action(1))  # move left
    spinner.close()

    assert seen == [1]
    assert spinner.iscanceled() is False


def test_back_queued_before_the_spinner_cancels_it_and_the_fetch_never_starts(load_ui, monkeypatch):
    """The reported bug: Back pressed an instant before the spinner appeared
    is delivered by the first pump - to the SCREEN underneath. It must cancel
    the spinner (func never runs) and leave the screen open; the next Back,
    after the spinner is gone, closes it exactly once."""
    ctx = load_ui()
    import xbmcgui

    spinners = _track_spinners(monkeypatch, ctx)
    win = make_window(ctx.streamswindow.StreamsWindow)
    closes = _count_closes(win)

    def monitor_abort(call_count):
        if call_count == 1:
            win.onAction(xbmcgui.Action(92))  # stale Back, addressed to the screen
        return False

    ctx.env.monitor_abort = monitor_abort
    ran = []

    result = ctx.uicommon.run_cancellable(lambda: ran.append(True), 'Loading')

    assert result is ctx.uicommon.CANCELLED
    assert ran == []
    assert closes == [] and win.closed is False
    assert spinners[0]._window is None

    win.onAction(xbmcgui.Action(92))
    assert closes == [True]


def test_second_stale_back_after_a_cancel_is_drained_and_does_not_close_the_screen(load_ui, monkeypatch):
    ctx = load_ui()
    import xbmcgui

    spinners = _track_spinners(monkeypatch, ctx)
    win = make_window(ctx.streamswindow.StreamsWindow)
    closes = _count_closes(win)

    def monitor_abort(call_count):
        if call_count == 2:  # in-flight: Back reaches the spinner
            spinners[-1]._window.onAction(xbmcgui.Action(92))
        elif call_count == 3:  # drain pump: a second queued Back, for the screen
            win.onAction(xbmcgui.Action(92))
        return False

    ctx.env.monitor_abort = monitor_abort

    with _gate() as gate:
        result = ctx.uicommon.run_cancellable(gate, 'Loading')

    assert result is ctx.uicommon.CANCELLED
    assert ctx.env.monitor_abort_calls >= 3  # the drain really pumped
    assert closes == [] and win.closed is False
    assert all(timeout and timeout > 0 for timeout in ctx.env.wait_calls)

    win.onAction(xbmcgui.Action(92))  # the user's next, real Back
    assert closes == [True]


def test_unregister_is_by_identity_and_tolerates_unknown_dialogs(load_ui):
    ctx = load_ui()

    class _D:
        def cancel(self):
            pass

    first, second = _D(), _D()
    ctx.uicommon.register_transient_dialog(first)
    ctx.uicommon.register_transient_dialog(first)  # idempotent
    ctx.uicommon.register_transient_dialog(second)
    ctx.uicommon.unregister_transient_dialog(_D())  # unknown: no-op
    ctx.uicommon.unregister_transient_dialog(first)
    assert ctx.uicommon.transient_dialog_active() is True
    ctx.uicommon.unregister_transient_dialog(second)
    assert ctx.uicommon.transient_dialog_active() is False
