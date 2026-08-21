"""Tests for lib.ui.catalogpicker: CatalogPickerWindow, Rivulet's custom
replacement for the classical `discover()` directory, exercised against the
shared fake xbmc/xbmcgui stubs in tests/kodistubs (no real Kodi runtime, no
network).

lib.ui.catalogpicker imports xbmcgui and lib.ui.uicommon at module scope, and
CatalogPickerWindow._open_catalog() lazily `from lib.ui.views import
_fetch_catalog` / `from lib.ui.infowindow import open_showcase` at call time
- so load_catalogpicker reloads lib.ui.compat/lib.ui.uicommon/lib.ui.router/
lib.ui.views/lib.ui.infowindow/lib.ui.catalogpicker fresh together, the same
way tests/test_views.py reloads lib.ui.infowindow to get a handle
(`ctx.views`/`ctx.infowindow`) this file monkeypatches `_fetch_catalog`/
`open_showcase` on directly.

CatalogPickerWindow.onInit()/onClick()/onAction()/_open_catalog() are called
directly here, never through a real modal event loop, exactly like
tests/test_infowindow.py drives ShowcaseWindow: the fake
WindowXML.doModal() is a no-op counter, and getControl()/setFocusId()
are plain in-memory fakes. CatalogPickerWindow.xml's actual skin rendering is
Kodi-skin-engine-only and is NOT, and cannot be, exercised by this suite.
"""
import contextlib

import pytest

from lib.stremio.addons import AddonError
from tests.kodistubs import install_kodi_stubs

_RELOAD_MODULE_NAMES = (
    'lib.ui.compat', 'lib.ui.uicommon', 'lib.ui.router', 'lib.ui.dialogs',
    'lib.ui.views', 'lib.ui.infowindow', 'lib.ui.detailwindow', 'lib.ui.catalogpicker',
    'lib.ui.mystuff', 'lib.ui.gridwindow',
)


class _FakeStore:
    """Fake `lib.store.Store`: `get_enabled_addons()` is what
    `open_catalog_picker()` fans catalogs out over; `get_progress_entries()`/
    `get_seen_episodes()`/`set_seen_episodes()` back the pinned "New
    Episodes" row's `_followed_series()`/`_mark_episode_seen()` - the same
    shape as `tests/test_homewindow.py`'s fake, which this mirrors now
    that the row lives here."""

    def __init__(self, addons=None, progress_entries=None, seen_episodes=None):
        self._addons = addons or []
        self._progress_entries = [] if progress_entries is None else progress_entries
        self._seen_episodes = {} if seen_episodes is None else dict(seen_episodes)

    def get_addons(self):
        return self._addons

    def get_enabled_addons(self):
        return [a for a in self._addons if not (a.get('flags') or {}).get('disabled')]

    def get_progress_entries(self):
        return self._progress_entries

    def get_seen_episodes(self):
        return self._seen_episodes

    def set_seen_episodes(self, seen):
        self._seen_episodes = dict(seen)


@pytest.fixture
def load_catalogpicker():
    """Factory fixture: `load_catalogpicker(addon_info=None, dialog_inputs=None)`
    installs fresh stubs (via tests.kodistubs.install_kodi_stubs) reloading
    lib.ui.compat/lib.ui.uicommon/lib.ui.router/lib.ui.views/
    lib.ui.infowindow/lib.ui.catalogpicker, and returns a namespace with
    `.catalogpicker`, `.compat`, `.router`, `.views`, `.infowindow`, and
    `.env`. Every call is torn down automatically, in reverse order, at
    test end.
    """
    with contextlib.ExitStack() as stack:
        def _load(addon_info=None, dialog_inputs=None, settings=None, localized=None):
            return stack.enter_context(install_kodi_stubs(
                reload=_RELOAD_MODULE_NAMES,
                addon_info=addon_info,
                dialog_inputs=dialog_inputs,
                settings=settings,
                localized=localized,
            ))

        yield _load


def _make_window(catalogpicker_mod):
    return catalogpicker_mod.CatalogPickerWindow('CatalogPickerWindow.xml', '/addon/path', 'Default', '1080i')


def _stub_choose(monkeypatch, ctx, answer, capture=None):
    """Patches `lib.ui.dialogs.choose` directly (already exhaustively
    covered by tests/test_dialogs.py) rather than driving a real
    `doModal()` - this suite only needs to prove the genre-filter call
    sites pass the right heading/rows and react correctly to the index."""
    def _choose(heading, rows):
        if capture is not None:
            capture.append((heading, list(rows)))
        return answer

    monkeypatch.setattr(ctx.dialogs, 'choose', _choose)


# ---------------------------------------------------------------------------
# CatalogPickerWindow.onInit() - item building
# ---------------------------------------------------------------------------


def test_oninit_builds_one_item_per_catalog_with_label_and_position(load_catalogpicker):
    ctx = load_catalogpicker()
    picker = ctx.catalogpicker
    win = _make_window(picker)
    win.catalogs = [
        ('https://a.example/manifest.json', {'name': 'Addon A'}, {'name': 'Top', 'type': 'movie'}),
        ('https://b.example/manifest.json', {}, {'id': 'series-catalog', 'type': 'series'}),
    ]

    win.onInit()

    items = win.getControl(picker.LIST).items
    assert [item.getLabel() for item in items] == ['Top', 'series-catalog']
    assert [item.label2 for item in items] == [
        'Addon A \u00b7 movie',
        '? \u00b7 series',
    ]
    assert [item.getProperty('position') for item in items] == ['0', '1']
    assert win.getFocusId() == picker.LIST


# ---------------------------------------------------------------------------
# CatalogPickerWindow.onAction()
# ---------------------------------------------------------------------------


@pytest.mark.parametrize('action_id', [9, 10, 92], ids=['nav-back', 'previous-menu', 'backspace'])
def test_onaction_back_actions_close_the_window(load_catalogpicker, action_id):
    ctx = load_catalogpicker()
    import xbmcgui
    win = _make_window(ctx.catalogpicker)

    win.onAction(xbmcgui.Action(action_id))

    assert win.closed is True


def test_onaction_non_back_action_does_not_close(load_catalogpicker):
    ctx = load_catalogpicker()
    import xbmcgui
    win = _make_window(ctx.catalogpicker)

    win.onAction(xbmcgui.Action(1))

    assert win.closed is False


# ---------------------------------------------------------------------------
# CatalogPickerWindow.onClick() - selection-by-position
# ---------------------------------------------------------------------------


def test_onclick_ignores_control_ids_other_than_list(load_catalogpicker, monkeypatch):
    ctx = load_catalogpicker()
    win = _make_window(ctx.catalogpicker)
    calls = []
    monkeypatch.setattr(win, '_open_catalog', lambda *a: calls.append(a))

    win.onClick(9999)

    assert calls == []


def test_onclick_list_with_no_focused_item_does_not_crash(load_catalogpicker, monkeypatch):
    ctx = load_catalogpicker()
    win = _make_window(ctx.catalogpicker)
    calls = []
    monkeypatch.setattr(win, '_open_catalog', lambda *a: calls.append(a))

    win.onClick(ctx.catalogpicker.LIST)

    assert calls == []


def test_onclick_dispatches_to_open_catalog_with_the_focused_row(load_catalogpicker, monkeypatch):
    ctx = load_catalogpicker()
    picker = ctx.catalogpicker
    win = _make_window(picker)
    win.catalogs = [
        ('https://a.example/manifest.json', {'name': 'A'}, {'id': 'top', 'type': 'movie'}),
        ('https://b.example/manifest.json', {'name': 'B'}, {'id': 'new', 'type': 'series'}),
    ]
    win.onInit()
    win.getControl(picker.LIST).selected_index = 1  # simulate scrolling to the 2nd row
    calls = []
    monkeypatch.setattr(
        win, '_open_catalog',
        lambda transport, manifest, catalog: calls.append((transport, manifest, catalog)),
    )

    win.onClick(picker.LIST)

    assert calls == [('https://b.example/manifest.json', {'name': 'B'}, {'id': 'new', 'type': 'series'})]


# ---------------------------------------------------------------------------
# CatalogPickerWindow._open_catalog()
# ---------------------------------------------------------------------------


def test_open_catalog_addon_error_is_logged_and_does_not_close(load_catalogpicker, monkeypatch):
    ctx = load_catalogpicker()
    win = _make_window(ctx.catalogpicker)

    def _raise(transport, ctype, cid, extra=None):
        raise AddonError('upstream down')

    monkeypatch.setattr(ctx.views, '_fetch_catalog', _raise)

    win._open_catalog('https://a.example/manifest.json', {'name': 'Addon A'}, {'type': 'movie', 'id': 'top'})

    assert win.should_close_caller is False
    assert win.closed is False
    assert ctx.env.executed_builtins == []
    assert ctx.env.notifications == [('Rivulet', 'STR30032', 'info', 4000)]


def test_open_catalog_addon_error_log_never_leaks_credentials_path_or_query(load_catalogpicker, monkeypatch):
    ctx = load_catalogpicker()
    import xbmc
    win = _make_window(ctx.catalogpicker)
    secret_transport = 'https://user:hunter2@evil.example:8443/private/path/manifest.json?token=abc123'

    def _raise(transport, ctype, cid, extra=None):
        raise AddonError('GET %s failed: bad request' % transport)

    monkeypatch.setattr(ctx.views, '_fetch_catalog', _raise)

    win._open_catalog(secret_transport, {'name': 'Addon A'}, {'type': 'movie', 'id': 'top'})

    all_messages = ' '.join(msg for msg, _level in ctx.env.log_calls)
    assert 'hunter2' not in all_messages
    assert 'token=abc123' not in all_messages
    assert '/private/path' not in all_messages
    assert 'bad request' not in all_messages
    error_msgs = [msg for msg, lvl in ctx.env.log_calls if lvl == xbmc.LOGERROR]
    assert any('evil.example:8443' in msg and 'AddonError' in msg for msg in error_msgs)


def test_open_catalog_empty_results_does_not_close_or_fallback(load_catalogpicker, monkeypatch):
    ctx = load_catalogpicker()
    win = _make_window(ctx.catalogpicker)
    monkeypatch.setattr(ctx.views, '_fetch_catalog', lambda transport, ctype, cid, extra=None: [])

    win._open_catalog('https://a.example/manifest.json', {'name': 'Addon A'}, {'type': 'movie', 'id': 'top'})

    assert win.should_close_caller is False
    assert win.closed is False
    assert ctx.env.executed_builtins == []
    assert ctx.env.notifications == [('Rivulet', 'STR30030', 'info', 4000)]


def test_open_catalog_no_selection_does_not_fallback_or_close(load_catalogpicker, monkeypatch):
    ctx = load_catalogpicker()
    win = _make_window(ctx.catalogpicker)
    metas = [{'id': 'tt1', 'name': 'One', 'type': 'movie'}]
    monkeypatch.setattr(ctx.views, '_fetch_catalog', lambda transport, ctype, cid, extra=None: metas)
    monkeypatch.setattr(ctx.infowindow, 'open_showcase', lambda m, catalog_title=None, more_pages=None: None)

    win._open_catalog('https://a.example/manifest.json', {'name': 'Addon A'}, {'type': 'movie', 'id': 'top'})

    assert win.should_close_caller is False
    assert win.closed is False
    assert ctx.env.executed_builtins == []


# ---------------------------------------------------------------------------
# CatalogPickerWindow._fetch_and_show() - `skip` paging
# ---------------------------------------------------------------------------


def test_fetch_and_show_does_not_page_a_catalog_without_skip(load_catalogpicker, monkeypatch):
    ctx = load_catalogpicker()
    win = _make_window(ctx.catalogpicker)
    calls = []

    def fake_fetch(transport, ctype, cid, extra=None):
        calls.append(extra)
        return [{'id': 'tt%d' % n, 'name': 'M%d' % n, 'type': 'movie'} for n in range(20)]

    monkeypatch.setattr(ctx.views, '_fetch_catalog', fake_fetch)
    monkeypatch.setattr(ctx.infowindow, 'open_showcase', lambda m, catalog_title=None, more_pages=None: None)

    win._open_catalog(
        'https://a.example/manifest.json', {'name': 'Addon A'}, {'id': 'top', 'type': 'movie'},
    )

    assert calls == [None]


def test_fetch_and_show_opens_on_the_first_page_without_fetching_the_rest(load_catalogpicker, monkeypatch):
    """The point of the lazy walk: the coverflow must open after ONE
    request. Waiting for all 20 pages of a 400-title AIOLists list took a
    minute on a real device, against ~6s for the first page - so anything
    that fetches page two before `open_showcase()` is a regression, even
    though the end result would look the same."""
    ctx = load_catalogpicker()
    win = _make_window(ctx.catalogpicker)
    fetched = []

    def fake_fetch(transport, ctype, cid, extra=None):
        skip = next((value for name, value in extra or [] if name == 'skip'), 0)
        fetched.append(skip)
        return [{'id': 'tt%d' % n, 'name': 'M%d' % n, 'type': 'movie'} for n in range(skip, skip + 20)]

    monkeypatch.setattr(ctx.views, '_fetch_catalog', fake_fetch)
    opened = {}

    def fake_showcase(metas, catalog_title=None, more_pages=None):
        opened['metas'] = metas
        opened['fetched_by_now'] = list(fetched)
        opened['more_pages'] = more_pages
        return None

    monkeypatch.setattr(ctx.infowindow, 'open_showcase', fake_showcase)

    win._open_catalog(
        'https://a.example/manifest.json', {'name': 'AIOLists'},
        {'id': 'list', 'type': 'movie', 'name': 'Criterion', 'extra': [{'name': 'skip'}]},
    )

    assert opened['fetched_by_now'] == [0]  # exactly one request before the window opened
    assert len(opened['metas']) == 20
    assert opened['more_pages'] is not None


def test_fetch_and_show_hands_the_remaining_pages_to_the_coverflow(load_catalogpicker, monkeypatch):
    """The generator handed over must actually continue the walk from
    page two - proving the picker passes the SAME iterator it already
    took the first page from, not a fresh one that would re-serve page
    one."""
    ctx = load_catalogpicker()
    win = _make_window(ctx.catalogpicker)

    def fake_fetch(transport, ctype, cid, extra=None):
        skip = next((value for name, value in extra or [] if name == 'skip'), 0)
        if skip >= 40:
            return []
        return [{'id': 'tt%d' % n, 'name': 'M%d' % n, 'type': 'movie'} for n in range(skip, skip + 20)]

    monkeypatch.setattr(ctx.views, '_fetch_catalog', fake_fetch)
    captured = {}
    monkeypatch.setattr(
        ctx.infowindow, 'open_showcase',
        lambda metas, catalog_title=None, more_pages=None: captured.update(
            metas=metas, rest=list(more_pages or []),
        ) and None,
    )

    win._open_catalog(
        'https://a.example/manifest.json', {'name': 'AIOLists'},
        {'id': 'list', 'type': 'movie', 'name': 'Criterion', 'extra': [{'name': 'skip'}]},
    )

    assert [m['id'] for m in captured['metas']] == ['tt%d' % n for n in range(20)]
    assert [m['id'] for page in captured['rest'] for m in page] == ['tt%d' % n for n in range(20, 40)]


# ---------------------------------------------------------------------------
# CatalogPickerWindow._fetch_and_show() - the "RIVULET / <TITLE>" breadcrumb
# ---------------------------------------------------------------------------


def test_fetch_and_show_passes_addon_and_catalog_name_as_breadcrumb_title(load_catalogpicker, monkeypatch):
    ctx = load_catalogpicker()
    win = _make_window(ctx.catalogpicker)
    metas = [{'id': 'tt1', 'name': 'One', 'type': 'movie'}]
    monkeypatch.setattr(ctx.views, '_fetch_catalog', lambda transport, ctype, cid, extra=None: metas)
    captured = {}
    monkeypatch.setattr(
        ctx.infowindow, 'open_showcase',
        lambda m, catalog_title=None, more_pages=None: captured.setdefault('catalog_title', catalog_title) and None,
    )

    win._open_catalog(
        'https://a.example/manifest.json', {'name': 'Cinemeta'}, {'name': 'Popular Movies', 'type': 'movie'},
    )

    assert captured['catalog_title'] == 'Cinemeta \u00b7 Popular Movies'


def test_fetch_and_show_falls_back_to_the_catalog_name_alone_without_an_addon_name(
    load_catalogpicker, monkeypatch,
):
    ctx = load_catalogpicker()
    win = _make_window(ctx.catalogpicker)
    metas = [{'id': 'tt1', 'name': 'One', 'type': 'movie'}]
    monkeypatch.setattr(ctx.views, '_fetch_catalog', lambda transport, ctype, cid, extra=None: metas)
    captured = {}
    monkeypatch.setattr(
        ctx.infowindow, 'open_showcase',
        lambda m, catalog_title=None, more_pages=None: captured.setdefault('catalog_title', catalog_title) and None,
    )

    win._open_catalog(
        'https://a.example/manifest.json', {}, {'name': 'Popular Movies', 'type': 'movie'},
    )

    assert captured['catalog_title'] == 'Popular Movies'


def test_open_catalog_with_selection_opens_detail_and_closes_when_playback_started(
    load_catalogpicker, monkeypatch,
):
    ctx = load_catalogpicker()
    win = _make_window(ctx.catalogpicker)
    metas = [{'id': 'tt1', 'name': 'One', 'type': 'series'}]
    monkeypatch.setattr(ctx.views, '_fetch_catalog', lambda transport, ctype, cid, extra=None: metas)
    monkeypatch.setattr(ctx.infowindow, 'open_showcase', lambda m, catalog_title=None, more_pages=None: m[0])
    captured = {}

    def fake_open_detail(stype, sid):
        captured['args'] = (stype, sid)
        return True

    monkeypatch.setattr(ctx.detailwindow, 'open_detail', fake_open_detail)

    win._open_catalog('https://a.example/manifest.json', {'name': 'Addon A'}, {'type': 'movie', 'id': 'top'})

    assert captured['args'] == ('series', 'tt1')
    assert win.should_close_caller is True
    assert win.closed is True


def test_open_catalog_with_selection_does_not_close_when_detail_returns_false(
    load_catalogpicker, monkeypatch,
):
    ctx = load_catalogpicker()
    win = _make_window(ctx.catalogpicker)
    metas = [{'id': 'tt1', 'name': 'One', 'type': 'series'}]
    monkeypatch.setattr(ctx.views, '_fetch_catalog', lambda transport, ctype, cid, extra=None: metas)
    monkeypatch.setattr(ctx.infowindow, 'open_showcase', lambda m, catalog_title=None, more_pages=None: m[0])
    monkeypatch.setattr(ctx.detailwindow, 'open_detail', lambda stype, sid: False)

    win._open_catalog('https://a.example/manifest.json', {'name': 'Addon A'}, {'type': 'movie', 'id': 'top'})

    assert win.should_close_caller is False
    assert win.closed is False


def test_open_catalog_selected_meta_without_type_falls_back_to_the_catalogs_own_type(
    load_catalogpicker, monkeypatch,
):
    ctx = load_catalogpicker()
    win = _make_window(ctx.catalogpicker)
    metas = [{'id': 'tt2', 'name': 'Two'}]  # no 'type' key on the selected meta
    monkeypatch.setattr(ctx.views, '_fetch_catalog', lambda transport, ctype, cid, extra=None: metas)
    monkeypatch.setattr(ctx.infowindow, 'open_showcase', lambda m, catalog_title=None, more_pages=None: m[0])
    captured = {}

    def fake_open_detail(stype, sid):
        captured['args'] = (stype, sid)
        return True

    monkeypatch.setattr(ctx.detailwindow, 'open_detail', fake_open_detail)

    win._open_catalog('https://a.example/manifest.json', {'name': 'Addon A'}, {'type': 'movie', 'id': 'top'})

    assert captured['args'] == ('movie', 'tt2')


def _capture_busy_windows(monkeypatch, ctx):
    """Records each `RivuletBusy._window` as it's created, the same way
    tests/test_uicommon.py's busy_dialog tests grab `dialog._window` -
    `_fetch_and_show()` never binds `as dialog` itself, so this hooks
    `create()` instead to get the window reference before a later
    `close()` clears it off the instance."""
    windows = []
    real_create = ctx.dialogs.RivuletBusy.create

    def _create(self, heading, message=''):
        real_create(self, heading, message)
        windows.append(self._window)

    monkeypatch.setattr(ctx.dialogs.RivuletBusy, 'create', _create)
    return windows


def test_open_catalog_fetch_shows_and_closes_the_busy_dialog(load_catalogpicker, monkeypatch):
    ctx = load_catalogpicker()
    win = _make_window(ctx.catalogpicker)
    metas = [{'id': 'tt1', 'name': 'One', 'type': 'series'}]
    monkeypatch.setattr(ctx.views, '_fetch_catalog', lambda transport, ctype, cid, extra=None: metas)
    monkeypatch.setattr(ctx.infowindow, 'open_showcase', lambda m, catalog_title=None, more_pages=None: None)
    windows = _capture_busy_windows(monkeypatch, ctx)

    win._open_catalog('https://a.example/manifest.json', {'name': 'Addon A'}, {'type': 'movie', 'id': 'top'})

    assert len(windows) == 1
    assert windows[0].getControl(ctx.dialogs.BUSY_HEADING).label == 'STR30033'
    assert windows[0].closed is True


def test_open_catalog_closes_busy_dialog_before_opening_coverflow(load_catalogpicker, monkeypatch):
    ctx = load_catalogpicker()
    win = _make_window(ctx.catalogpicker)
    metas = [{'id': 'tt1', 'name': 'One', 'type': 'series'}]
    monkeypatch.setattr(ctx.views, '_fetch_catalog', lambda transport, ctype, cid, extra=None: metas)
    windows = _capture_busy_windows(monkeypatch, ctx)
    captured = {}

    def fake_open_showcase(m, catalog_title=None, more_pages=None):
        captured['closed'] = windows[0].closed
        return None

    monkeypatch.setattr(ctx.infowindow, 'open_showcase', fake_open_showcase)

    win._open_catalog('https://a.example/manifest.json', {'name': 'Addon A'}, {'type': 'movie', 'id': 'top'})

    assert captured['closed'] is True


def test_open_catalog_addon_error_still_closes_the_busy_dialog(load_catalogpicker, monkeypatch):
    ctx = load_catalogpicker()
    win = _make_window(ctx.catalogpicker)
    windows = _capture_busy_windows(monkeypatch, ctx)

    def _raise(transport, ctype, cid, extra=None):
        raise AddonError('upstream down')

    monkeypatch.setattr(ctx.views, '_fetch_catalog', _raise)

    win._open_catalog('https://a.example/manifest.json', {'name': 'Addon A'}, {'type': 'movie', 'id': 'top'})

    assert len(windows) == 1
    assert windows[0].getControl(ctx.dialogs.BUSY_HEADING).label == 'STR30033'
    assert windows[0].closed is True


# ---------------------------------------------------------------------------
# CatalogPickerWindow.start() - the doModal()/empty-catalogs contract
# ---------------------------------------------------------------------------


def test_start_with_empty_catalogs_returns_false_without_domodal(load_catalogpicker):
    ctx = load_catalogpicker()
    win = _make_window(ctx.catalogpicker)

    result = win.start([])

    assert result is False
    assert win.modal_calls == 0


def test_start_resets_should_close_caller_on_each_call(load_catalogpicker):
    ctx = load_catalogpicker()
    win = _make_window(ctx.catalogpicker)
    win.should_close_caller = True  # leftover from a previous run

    result = win.start([])

    assert result is False
    assert win.should_close_caller is False


def test_start_with_catalogs_calls_domodal_and_returns_should_close_caller(load_catalogpicker, monkeypatch):
    ctx = load_catalogpicker()
    picker = ctx.catalogpicker
    win = _make_window(picker)
    catalogs = [('https://a.example/manifest.json', {'name': 'A'}, {'id': 'top', 'type': 'movie'})]
    metas = [{'id': 'tt1', 'name': 'One', 'type': 'movie'}]
    monkeypatch.setattr(ctx.views, '_fetch_catalog', lambda transport, ctype, cid, extra=None: metas)
    monkeypatch.setattr(ctx.infowindow, 'open_showcase', lambda m, catalog_title=None, more_pages=None: m[0])
    monkeypatch.setattr(ctx.detailwindow, 'open_detail', lambda stype, sid: True)

    # The fake doModal() is a no-op counter; simulate what a real modal event
    # loop would drive around it (onInit(), the user picking the only row),
    # exactly as Kodi calls back into the window.
    real_domodal = win.doModal

    def fake_domodal():
        real_domodal()
        win.onInit()
        win.getControl(picker.LIST).selected_index = 0
        win.onClick(picker.LIST)

    win.doModal = fake_domodal

    result = win.start(catalogs)

    assert result is True
    assert win.modal_calls == 1


# ---------------------------------------------------------------------------
# open_catalog_picker()
# ---------------------------------------------------------------------------


def test_open_catalog_picker_with_no_catalogs_notifies_and_returns_false(load_catalogpicker, monkeypatch):
    ctx = load_catalogpicker()
    monkeypatch.setattr(ctx.catalogpicker, 'get_store', lambda: _FakeStore(addons=[]))

    result = ctx.catalogpicker.open_catalog_picker()

    assert result is False
    assert ctx.env.notifications == [('Rivulet', 'STR30030', 'info', 4000)]


def test_open_catalog_picker_opens_window_with_discovered_catalogs(load_catalogpicker, monkeypatch):
    ctx = load_catalogpicker(addon_info={'path': '/addon/path'})
    descriptor = {
        'transportUrl': 'https://a.example/manifest.json',
        'manifest': {'name': 'Addon A', 'catalogs': [{'id': 'top', 'type': 'movie'}]},
    }
    monkeypatch.setattr(ctx.catalogpicker, 'get_store', lambda: _FakeStore(addons=[descriptor]))
    captured = {}

    class RecordingWindow(ctx.catalogpicker.CatalogPickerWindow):
        def __init__(self, *args, **kwargs):
            captured['init_args'] = args
            super().__init__(*args, **kwargs)

        def start(self, catalogs, heading='', new_episode_items=None):
            captured['catalogs'] = catalogs
            captured['heading'] = heading
            return True

    monkeypatch.setattr(ctx.catalogpicker, 'CatalogPickerWindow', RecordingWindow)

    result = ctx.catalogpicker.open_catalog_picker()

    assert result is True
    assert captured['init_args'] == ('CatalogPickerWindow.xml', '/addon/path', 'Default', '1080i')
    assert captured['catalogs'] == [
        ('https://a.example/manifest.json', descriptor['manifest'], {'id': 'top', 'type': 'movie'}),
    ]


def test_open_catalog_picker_excludes_disabled_addons_catalogs(load_catalogpicker, monkeypatch):
    """A disabled addon stays installed but must never surface a catalog
    row - open_catalog_picker() fans out over get_enabled_addons(), not
    every installed descriptor."""
    ctx = load_catalogpicker(addon_info={'path': '/addon/path'})
    enabled = {
        'transportUrl': 'https://a.example/manifest.json',
        'manifest': {'name': 'Addon A', 'catalogs': [{'id': 'top', 'type': 'movie'}]},
    }
    disabled = {
        'transportUrl': 'https://b.example/manifest.json',
        'manifest': {'name': 'Addon B', 'catalogs': [{'id': 'other', 'type': 'movie'}]},
        'flags': {'disabled': True},
    }
    monkeypatch.setattr(ctx.catalogpicker, 'get_store', lambda: _FakeStore(addons=[enabled, disabled]))
    captured = {}

    class RecordingWindow(ctx.catalogpicker.CatalogPickerWindow):
        def start(self, catalogs, heading='', new_episode_items=None):
            captured['catalogs'] = catalogs
            return True

    monkeypatch.setattr(ctx.catalogpicker, 'CatalogPickerWindow', RecordingWindow)

    result = ctx.catalogpicker.open_catalog_picker()

    assert result is True
    assert captured['catalogs'] == [
        ('https://a.example/manifest.json', enabled['manifest'], {'id': 'top', 'type': 'movie'}),
    ]


# ---------------------------------------------------------------------------
# open_catalog_picker() - types= filtering via base-type matching
# ---------------------------------------------------------------------------


def test_base_type_reduces_dotted_subtype_and_lowercases(load_catalogpicker):
    ctx = load_catalogpicker()

    assert ctx.catalogpicker._base_type('anime.movie') == 'anime'
    assert ctx.catalogpicker._base_type('anime.series') == 'anime'
    assert ctx.catalogpicker._base_type('TV') == 'tv'
    assert ctx.catalogpicker._base_type('movie') == 'movie'
    assert ctx.catalogpicker._base_type(None) == ''


def test_open_catalog_picker_types_filter_matches_dotted_subtype_base(load_catalogpicker, monkeypatch):
    ctx = load_catalogpicker(addon_info={'path': '/addon/path'})
    descriptor = {
        'transportUrl': 'https://a.example/manifest.json',
        'manifest': {'name': 'Addon A', 'catalogs': [
            {'id': 'am', 'type': 'anime.movie'}, {'id': 'top', 'type': 'movie'},
        ]},
    }
    monkeypatch.setattr(ctx.catalogpicker, 'get_store', lambda: _FakeStore(addons=[descriptor]))
    captured = {}

    class RecordingWindow(ctx.catalogpicker.CatalogPickerWindow):
        def start(self, catalogs, heading='', new_episode_items=None):
            captured['catalogs'] = catalogs
            return True

    monkeypatch.setattr(ctx.catalogpicker, 'CatalogPickerWindow', RecordingWindow)

    result = ctx.catalogpicker.open_catalog_picker(types={'anime'})

    assert result is True
    assert [cat.get('id') for _t, _m, cat in captured['catalogs']] == ['am']


def test_open_catalog_picker_types_filter_is_case_insensitive(load_catalogpicker, monkeypatch):
    ctx = load_catalogpicker(addon_info={'path': '/addon/path'})
    descriptor = {
        'transportUrl': 'https://a.example/manifest.json',
        'manifest': {'name': 'Addon A', 'catalogs': [{'id': 'ch', 'type': 'TV'}]},
    }
    monkeypatch.setattr(ctx.catalogpicker, 'get_store', lambda: _FakeStore(addons=[descriptor]))
    captured = {}

    class RecordingWindow(ctx.catalogpicker.CatalogPickerWindow):
        def start(self, catalogs, heading='', new_episode_items=None):
            captured['catalogs'] = catalogs
            return True

    monkeypatch.setattr(ctx.catalogpicker, 'CatalogPickerWindow', RecordingWindow)

    result = ctx.catalogpicker.open_catalog_picker(types={'series', 'tv'})

    assert result is True
    assert [cat.get('id') for _t, _m, cat in captured['catalogs']] == ['ch']


def test_open_catalog_picker_types_filter_to_remainder_excludes_curated_types(load_catalogpicker, monkeypatch):
    # The 'other' row's picker: filtered to exactly {'porn'} must not also
    # surface the movie/series/anime catalogs the same addon publishes.
    ctx = load_catalogpicker(addon_info={'path': '/addon/path'})
    descriptor = {
        'transportUrl': 'https://a.example/manifest.json',
        'manifest': {'name': 'TPB 4K Porn', 'catalogs': [
            {'id': 'm', 'type': 'movie'}, {'id': 's', 'type': 'series'},
            {'id': 'a', 'type': 'anime'}, {'id': 'p', 'type': 'Porn'},
        ]},
    }
    monkeypatch.setattr(ctx.catalogpicker, 'get_store', lambda: _FakeStore(addons=[descriptor]))
    captured = {}

    class RecordingWindow(ctx.catalogpicker.CatalogPickerWindow):
        def start(self, catalogs, heading='', new_episode_items=None):
            captured['catalogs'] = catalogs
            return True

    monkeypatch.setattr(ctx.catalogpicker, 'CatalogPickerWindow', RecordingWindow)

    result = ctx.catalogpicker.open_catalog_picker(types={'porn'})

    assert result is True
    assert [cat.get('id') for _t, _m, cat in captured['catalogs']] == ['p']


def test_open_catalog_picker_window_is_closed_exactly_once_when_start_raises(
    load_catalogpicker, monkeypatch,
):
    ctx = load_catalogpicker(addon_info={'path': '/addon/path'})
    descriptor = {
        'transportUrl': 'https://a.example/manifest.json',
        'manifest': {'name': 'Addon A', 'catalogs': [{'id': 'top', 'type': 'movie'}]},
    }
    monkeypatch.setattr(ctx.catalogpicker, 'get_store', lambda: _FakeStore(addons=[descriptor]))
    captured = {}

    class ExplodingWindow(ctx.catalogpicker.CatalogPickerWindow):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.close_calls = 0
            captured['window'] = self

        def close(self):
            self.close_calls += 1
            super().close()

        def start(self, catalogs, heading='', new_episode_items=None):
            # Stands in for a crash inside onInit()/onAction() while the
            # modal loop is running - self.close() (the window's own,
            # normal-path close) never gets a chance to run.
            raise RuntimeError('onInit blew up')

    monkeypatch.setattr(ctx.catalogpicker, 'CatalogPickerWindow', ExplodingWindow)

    result = ctx.catalogpicker.open_catalog_picker()

    assert result is False
    win = captured['window']
    assert win.close_calls == 1
    assert win.closed is True
    assert ctx.env.notifications == [('Rivulet', 'STR30032', 'info', 4000)]


# ---------------------------------------------------------------------------
# Required-extra classification: search-only / genre-required / unreachable
# ---------------------------------------------------------------------------


_SEARCH_CATALOG = {
    'id': 'tmdb.search', 'type': 'movie', 'name': 'Search',
    'extra': [{'name': 'search', 'isRequired': True}],
}


def _search_only_catalog(catalog_id):
    """A minimal search-only catalog dict (see `_SEARCH_CATALOG` above)
    for the `_sort_catalogs()` fixtures, distinguished only by `id`."""
    return {'id': catalog_id, 'type': 'movie', 'extra': [{'name': 'search', 'isRequired': True}]}


_GENRE_REQUIRED_CATALOG = {
    'id': 'year', 'type': 'movie', 'name': 'New',
    'extra': [{'name': 'genre', 'isRequired': True, 'options': ['2026', '2025']}],
}
_GENRE_OPTIONAL_CATALOG = {
    'id': 'top', 'type': 'movie', 'name': 'Popular',
    'extra': [{'name': 'genre', 'options': ['Action', 'Comedy']}],
}
_UNSUPPORTED_CATALOG = {
    'id': 'last-videos', 'type': 'series', 'name': 'Continue Watching',
    'extraRequired': ['lastVideosIds'],
}


def test_oninit_marks_search_only_catalog_rows_in_label2(load_catalogpicker):
    ctx = load_catalogpicker()
    picker = ctx.catalogpicker
    win = _make_window(picker)
    win.catalogs = [
        ('https://a.example/manifest.json', {'name': 'Addon A'}, _SEARCH_CATALOG),
        ('https://b.example/manifest.json', {'name': 'Addon B'}, _GENRE_OPTIONAL_CATALOG),
    ]

    win.onInit()

    items = win.getControl(picker.LIST).items
    assert items[0].label2 == 'Addon A \u00b7 movie \u00b7 STR30199'
    assert items[1].label2 == 'Addon B \u00b7 movie'


def test_oninit_heading_names_the_row_the_user_came_in_through(load_catalogpicker):
    ctx = load_catalogpicker()
    picker = ctx.catalogpicker
    win = _make_window(picker)
    win.catalogs = [('https://a.example/manifest.json', {'name': 'Addon A'}, _GENRE_OPTIONAL_CATALOG)]
    win.heading = 'Movies'

    win.onInit()

    # The skin's header is "RIVULET / <SECTION>"; the section is the row.
    assert win.getControl(picker.HEADING).label == 'RIVULET / MOVIES'


def test_oninit_heading_falls_back_to_the_generic_title_when_unfiltered(load_catalogpicker):
    ctx = load_catalogpicker()
    picker = ctx.catalogpicker
    win = _make_window(picker)
    win.catalogs = [('https://a.example/manifest.json', {'name': 'Addon A'}, _GENRE_OPTIONAL_CATALOG)]

    win.onInit()

    # No heading -> the label the skin used to hardcode, not "RIVULET / ".
    assert win.getControl(picker.HEADING).label == 'RIVULET / STR30000'


def test_open_catalog_search_only_prompts_and_fetches_with_search_extra(load_catalogpicker, monkeypatch):
    ctx = load_catalogpicker(dialog_inputs=['batman'])
    win = _make_window(ctx.catalogpicker)
    captured = {}

    def fake_fetch(transport, ctype, cid, extra=None):
        captured['extra'] = extra
        return []

    monkeypatch.setattr(ctx.views, '_fetch_catalog', fake_fetch)

    win._open_catalog('https://a.example/manifest.json', {'name': 'Addon A'}, _SEARCH_CATALOG)

    assert ctx.env.dialog_input_prompts == ['STR30001']
    assert captured['extra'] == [('search', 'batman')]


def test_open_catalog_search_only_cancelled_prompt_fetches_nothing(load_catalogpicker, monkeypatch):
    ctx = load_catalogpicker(dialog_inputs=[''])
    win = _make_window(ctx.catalogpicker)
    calls = []
    monkeypatch.setattr(ctx.views, '_fetch_catalog', lambda *a, **k: calls.append((a, k)))

    win._open_catalog('https://a.example/manifest.json', {'name': 'Addon A'}, _SEARCH_CATALOG)

    assert calls == []
    assert ctx.env.notifications == []


def test_open_catalog_normal_catalog_click_does_not_prompt(load_catalogpicker, monkeypatch):
    ctx = load_catalogpicker()
    win = _make_window(ctx.catalogpicker)
    captured = {}

    def fake_fetch(transport, ctype, cid, extra=None):
        captured['extra'] = extra
        return []

    monkeypatch.setattr(ctx.views, '_fetch_catalog', fake_fetch)

    win._open_catalog('https://a.example/manifest.json', {'name': 'Addon A'}, _GENRE_OPTIONAL_CATALOG)

    assert ctx.env.dialog_input_prompts == []
    assert captured['extra'] is None


def test_open_catalog_genre_required_opens_select_with_no_all_entry(load_catalogpicker, monkeypatch):
    ctx = load_catalogpicker()
    win = _make_window(ctx.catalogpicker)
    captured = []
    _stub_choose(monkeypatch, ctx, 1, capture=captured)

    def fake_fetch(transport, ctype, cid, extra=None):
        captured.append(('extra', extra))
        return []

    monkeypatch.setattr(ctx.views, '_fetch_catalog', fake_fetch)

    win._open_catalog('https://a.example/manifest.json', {'name': 'Addon A'}, _GENRE_REQUIRED_CATALOG)

    assert captured[0] == ('STR30194', ['2026', '2025'])
    assert captured[1] == ('extra', [('genre', '2025')])


def test_open_catalog_genre_required_cancelled_select_fetches_nothing(load_catalogpicker, monkeypatch):
    ctx = load_catalogpicker()
    win = _make_window(ctx.catalogpicker)
    _stub_choose(monkeypatch, ctx, -1)
    calls = []
    monkeypatch.setattr(ctx.views, '_fetch_catalog', lambda *a, **k: calls.append((a, k)))

    win._open_catalog('https://a.example/manifest.json', {'name': 'Addon A'}, _GENRE_REQUIRED_CATALOG)

    assert calls == []


# ---------------------------------------------------------------------------
# ACTION_CONTEXT_MENU (117) - the year/genre filter row
# ---------------------------------------------------------------------------


def test_onaction_context_menu_opens_select_with_all_first_then_declared_options(load_catalogpicker, monkeypatch):
    ctx = load_catalogpicker()
    import xbmcgui
    picker = ctx.catalogpicker
    win = _make_window(picker)
    win.catalogs = [('https://a.example/manifest.json', {'name': 'A'}, _GENRE_OPTIONAL_CATALOG)]
    win.onInit()
    win.getControl(picker.LIST).selected_index = 0
    win.setFocusId(picker.LIST)
    captured = []
    _stub_choose(monkeypatch, ctx, -1, capture=captured)

    win.onAction(xbmcgui.Action(picker._CONTEXT_MENU_ACTION))

    assert captured[0] == ('STR30194', ['STR30198', 'Action', 'Comedy'])


def test_onaction_context_menu_all_fetches_with_no_extra(load_catalogpicker, monkeypatch):
    ctx = load_catalogpicker()
    import xbmcgui
    picker = ctx.catalogpicker
    win = _make_window(picker)
    win.catalogs = [('https://a.example/manifest.json', {'name': 'A'}, _GENRE_OPTIONAL_CATALOG)]
    win.onInit()
    win.getControl(picker.LIST).selected_index = 0
    win.setFocusId(picker.LIST)
    _stub_choose(monkeypatch, ctx, 0)
    captured = {}
    monkeypatch.setattr(ctx.views, '_fetch_catalog', lambda t, c, i, extra=None: captured.setdefault('extra', extra) or [])

    win.onAction(xbmcgui.Action(picker._CONTEXT_MENU_ACTION))

    assert captured['extra'] is None


def test_onaction_context_menu_option_fetches_with_genre_extra(load_catalogpicker, monkeypatch):
    ctx = load_catalogpicker()
    import xbmcgui
    picker = ctx.catalogpicker
    win = _make_window(picker)
    win.catalogs = [('https://a.example/manifest.json', {'name': 'A'}, _GENRE_OPTIONAL_CATALOG)]
    win.onInit()
    win.getControl(picker.LIST).selected_index = 0
    win.setFocusId(picker.LIST)
    _stub_choose(monkeypatch, ctx, 2)  # index 2 -> options[1] 'Comedy'
    captured = {}
    monkeypatch.setattr(ctx.views, '_fetch_catalog', lambda t, c, i, extra=None: captured.setdefault('extra', extra) or [])

    win.onAction(xbmcgui.Action(picker._CONTEXT_MENU_ACTION))

    assert captured['extra'] == [('genre', 'Comedy')]


def test_onaction_context_menu_cancelled_select_fetches_nothing(load_catalogpicker, monkeypatch):
    ctx = load_catalogpicker()
    import xbmcgui
    picker = ctx.catalogpicker
    win = _make_window(picker)
    win.catalogs = [('https://a.example/manifest.json', {'name': 'A'}, _GENRE_OPTIONAL_CATALOG)]
    win.onInit()
    win.getControl(picker.LIST).selected_index = 0
    win.setFocusId(picker.LIST)
    _stub_choose(monkeypatch, ctx, -1)
    calls = []
    monkeypatch.setattr(ctx.views, '_fetch_catalog', lambda *a, **k: calls.append((a, k)))

    win.onAction(xbmcgui.Action(picker._CONTEXT_MENU_ACTION))

    assert calls == []


def test_onaction_context_menu_notifies_when_catalog_has_no_genre_options(load_catalogpicker):
    ctx = load_catalogpicker()
    import xbmcgui
    picker = ctx.catalogpicker
    win = _make_window(picker)
    win.catalogs = [('https://a.example/manifest.json', {'name': 'A'}, {'id': 'top', 'type': 'movie'})]
    win.onInit()
    win.getControl(picker.LIST).selected_index = 0
    win.setFocusId(picker.LIST)

    win.onAction(xbmcgui.Action(picker._CONTEXT_MENU_ACTION))

    assert ctx.env.notifications == [('Rivulet', 'STR30030', 'info', 4000)]


@pytest.mark.parametrize('action_id', [9, 10, 92], ids=['nav-back', 'previous-menu', 'backspace'])
def test_onaction_back_actions_still_close_through_the_new_onaction(load_catalogpicker, action_id):
    ctx = load_catalogpicker()
    import xbmcgui
    picker = ctx.catalogpicker
    win = _make_window(picker)
    win.catalogs = [('https://a.example/manifest.json', {'name': 'A'}, _GENRE_OPTIONAL_CATALOG)]
    win.onInit()

    win.onAction(xbmcgui.Action(action_id))

    assert win.closed is True


# ---------------------------------------------------------------------------
# open_catalog_picker() - dropping permanently-unreachable catalogs
# ---------------------------------------------------------------------------


def test_open_catalog_picker_omits_and_logs_catalogs_requiring_unsupportable_extras(
    load_catalogpicker, monkeypatch,
):
    ctx = load_catalogpicker(addon_info={'path': '/addon/path'})
    import xbmc
    descriptor = {
        'transportUrl': 'https://a.example/manifest.json',
        'manifest': {'name': 'Addon A', 'catalogs': [_UNSUPPORTED_CATALOG, {'id': 'top', 'type': 'movie'}]},
    }
    monkeypatch.setattr(ctx.catalogpicker, 'get_store', lambda: _FakeStore(addons=[descriptor]))
    captured = {}

    class RecordingWindow(ctx.catalogpicker.CatalogPickerWindow):
        def start(self, catalogs, heading='', new_episode_items=None):
            captured['catalogs'] = catalogs
            return True

    monkeypatch.setattr(ctx.catalogpicker, 'CatalogPickerWindow', RecordingWindow)

    result = ctx.catalogpicker.open_catalog_picker()

    assert result is True
    assert [cat.get('id') for _t, _m, cat in captured['catalogs']] == ['top']
    info_msgs = [msg for msg, lvl in ctx.env.log_calls if lvl == xbmc.LOGINFO]
    assert any('last-videos' in msg or 'Continue Watching' in msg for msg in info_msgs)


def test_open_catalog_picker_omits_genre_required_catalog_with_no_declared_options(
    load_catalogpicker, monkeypatch,
):
    ctx = load_catalogpicker(addon_info={'path': '/addon/path'})
    no_options_genre_catalog = {'id': 'year', 'type': 'movie', 'name': 'New', 'extra': [{'name': 'genre', 'isRequired': True}]}
    descriptor = {
        'transportUrl': 'https://a.example/manifest.json',
        'manifest': {'name': 'Addon A', 'catalogs': [no_options_genre_catalog, {'id': 'top', 'type': 'movie'}]},
    }
    monkeypatch.setattr(ctx.catalogpicker, 'get_store', lambda: _FakeStore(addons=[descriptor]))
    captured = {}

    class RecordingWindow(ctx.catalogpicker.CatalogPickerWindow):
        def start(self, catalogs, heading='', new_episode_items=None):
            captured['catalogs'] = catalogs
            return True

    monkeypatch.setattr(ctx.catalogpicker, 'CatalogPickerWindow', RecordingWindow)

    result = ctx.catalogpicker.open_catalog_picker()

    assert result is True
    assert [cat.get('id') for _t, _m, cat in captured['catalogs']] == ['top']


# ---------------------------------------------------------------------------
# _sort_catalogs() - search-only catalogs float to the top, stably
# ---------------------------------------------------------------------------


def test_sort_catalogs_floats_search_only_catalogs_above_browsable_preserving_relative_order(
    load_catalogpicker,
):
    ctx = load_catalogpicker()
    browsable_a = ('u', {}, {'id': 'a', 'type': 'movie'})
    search_b = ('u', {}, _search_only_catalog('b'))
    browsable_c = ('u', {}, {'id': 'c', 'type': 'movie'})
    search_d = ('u', {}, _search_only_catalog('d'))
    catalogs = [browsable_a, search_b, browsable_c, search_d]

    sorted_catalogs = ctx.catalogpicker._sort_catalogs(catalogs)

    # Search-only first (b, d), addon order preserved within each group.
    assert [cat['id'] for _t, _m, cat in sorted_catalogs] == ['b', 'd', 'a', 'c']


def test_open_catalog_picker_sorts_search_only_catalogs_first(load_catalogpicker, monkeypatch):
    ctx = load_catalogpicker(addon_info={'path': '/addon/path'})
    descriptor = {
        'transportUrl': 'https://a.example/manifest.json',
        'manifest': {'name': 'Addon A', 'catalogs': [
            {'id': 'a', 'type': 'movie'}, _search_only_catalog('b'),
            {'id': 'c', 'type': 'movie'}, _search_only_catalog('d'),
        ]},
    }
    monkeypatch.setattr(ctx.catalogpicker, 'get_store', lambda: _FakeStore(addons=[descriptor]))
    captured = {}

    class RecordingWindow(ctx.catalogpicker.CatalogPickerWindow):
        def start(self, catalogs, heading='', new_episode_items=None):
            captured['catalogs'] = catalogs
            return True

    monkeypatch.setattr(ctx.catalogpicker, 'CatalogPickerWindow', RecordingWindow)

    result = ctx.catalogpicker.open_catalog_picker()

    assert result is True
    assert [cat.get('id') for _t, _m, cat in captured['catalogs']] == ['b', 'd', 'a', 'c']


# ---------------------------------------------------------------------------
# open_catalog_picker() - pinned "New Episodes" row on the Series screen
# ---------------------------------------------------------------------------


_SERIES_DESCRIPTOR = {
    'transportUrl': 'https://a.example/manifest.json',
    'manifest': {'name': 'Addon A', 'catalogs': [{'id': 's', 'type': 'series'}]},
}


def _recording_window(catalogpicker_mod, captured):
    class RecordingWindow(catalogpicker_mod.CatalogPickerWindow):
        def start(self, catalogs, heading='', new_episode_items=None):
            captured['catalogs'] = catalogs
            captured['new_episode_items'] = new_episode_items
            return True
    return RecordingWindow


def test_open_catalog_picker_pins_new_episodes_for_the_series_screen_with_a_count(
    load_catalogpicker, monkeypatch,
):
    ctx = load_catalogpicker(addon_info={'path': '/addon/path'})
    monkeypatch.setattr(ctx.catalogpicker, 'get_store', lambda: _FakeStore(addons=[_SERIES_DESCRIPTOR]))
    items = [{'type': 'series', 'id': 'tt1', 'video_id': 's1e2'}]
    monkeypatch.setattr(ctx.catalogpicker, '_new_episode_items', lambda store: items)
    captured = {}
    monkeypatch.setattr(ctx.catalogpicker, 'CatalogPickerWindow', _recording_window(ctx.catalogpicker, captured))

    result = ctx.catalogpicker.open_catalog_picker(types={'series', 'tv'})

    assert result is True
    assert captured['new_episode_items'] == items


def test_open_catalog_picker_omits_new_episodes_row_when_count_is_zero(load_catalogpicker, monkeypatch):
    ctx = load_catalogpicker(addon_info={'path': '/addon/path'})
    # No progress entries - _followed_series()/_new_episode_items() run for
    # real and genuinely find nothing, unlike the setting-off test below.
    monkeypatch.setattr(ctx.catalogpicker, 'get_store', lambda: _FakeStore(addons=[_SERIES_DESCRIPTOR]))
    captured = {}
    monkeypatch.setattr(ctx.catalogpicker, 'CatalogPickerWindow', _recording_window(ctx.catalogpicker, captured))

    result = ctx.catalogpicker.open_catalog_picker(types={'series', 'tv'})

    assert result is True
    assert captured['new_episode_items'] == []


def test_open_catalog_picker_omits_new_episodes_row_when_the_setting_is_off(
    load_catalogpicker, monkeypatch,
):
    ctx = load_catalogpicker(addon_info={'path': '/addon/path'}, settings={'home_show_new_episodes': 'false'})
    monkeypatch.setattr(ctx.catalogpicker, 'get_store', lambda: _FakeStore(addons=[_SERIES_DESCRIPTOR]))
    calls = []
    monkeypatch.setattr(
        ctx.catalogpicker, '_new_episode_items',
        lambda store: calls.append(1) or [{'type': 'series', 'id': 'tt1', 'video_id': 's1e2'}],
    )
    captured = {}
    monkeypatch.setattr(ctx.catalogpicker, 'CatalogPickerWindow', _recording_window(ctx.catalogpicker, captured))

    result = ctx.catalogpicker.open_catalog_picker(types={'series', 'tv'})

    assert result is True
    assert captured['new_episode_items'] == []
    assert calls == []  # gated before the addon-fetching computation ever runs


@pytest.mark.parametrize('types', [{'movie'}, {'anime'}, {'porn'}, None], ids=[
    'movies', 'anime', 'other', 'unfiltered',
])
def test_open_catalog_picker_never_pins_new_episodes_outside_the_series_screen(
    load_catalogpicker, monkeypatch, types,
):
    ctx = load_catalogpicker(addon_info={'path': '/addon/path'})
    descriptor = {
        'transportUrl': 'https://a.example/manifest.json',
        'manifest': {'name': 'Addon A', 'catalogs': [
            {'id': 'm', 'type': 'movie'}, {'id': 's', 'type': 'series'},
            {'id': 'a', 'type': 'anime'}, {'id': 'p', 'type': 'Porn'},
        ]},
    }
    monkeypatch.setattr(ctx.catalogpicker, 'get_store', lambda: _FakeStore(addons=[descriptor]))
    items = [{'type': 'series', 'id': 'tt1', 'video_id': 's1e2'}]
    monkeypatch.setattr(ctx.catalogpicker, '_new_episode_items', lambda store: items)
    captured = {}
    monkeypatch.setattr(ctx.catalogpicker, 'CatalogPickerWindow', _recording_window(ctx.catalogpicker, captured))

    result = ctx.catalogpicker.open_catalog_picker(types=types)

    assert result is True
    assert captured['new_episode_items'] == []


def test_oninit_pins_the_new_episodes_row_first_with_a_count_neutral_label(load_catalogpicker):
    ctx = load_catalogpicker(localized={30360: 'New episodes: %d'})
    picker = ctx.catalogpicker
    win = _make_window(picker)
    win.catalogs = [('https://a.example/manifest.json', {'name': 'A'}, {'id': 'x', 'type': 'series'})]
    win.new_episode_items = [
        {'type': 'series', 'id': 'tt1', 'video_id': 's1e2'},
        {'type': 'series', 'id': 'tt2', 'video_id': 's1e3'},
    ]

    win.onInit()

    items = win.getControl(picker.LIST).items
    assert items[0].getLabel() == 'STR30313'
    assert items[0].label2 == 'New episodes: 2'
    assert items[0].getProperty('kind') == 'new_episodes'
    assert [item.getLabel() for item in items[1:]] == ['x']


def test_onclick_new_episodes_row_marks_seen_and_opens_detail(load_catalogpicker, monkeypatch):
    ctx = load_catalogpicker()
    picker = ctx.catalogpicker
    import xbmcgui
    win = _make_window(picker)
    episode = {'type': 'series', 'id': 'tt1', 'video_id': 's1e2'}
    win.new_episode_items = [episode]
    item = xbmcgui.ListItem('label')
    item.setProperty('kind', 'new_episodes')
    win.getControl(picker.LIST).addItems([item])
    fake_store = _FakeStore()
    monkeypatch.setattr(picker, 'get_store', lambda: fake_store)
    grid_calls = []

    def _fake_open_grid(bands, heading='', labels=None):
        grid_calls.append((bands, heading, labels))
        return episode

    monkeypatch.setattr(ctx.gridwindow, 'open_grid', _fake_open_grid)
    detail_calls = []
    monkeypatch.setattr(ctx.detailwindow, 'open_detail', lambda stype, sid: detail_calls.append((stype, sid)) or True)

    win.onClick(picker.LIST)

    assert win.closed is True
    assert win.should_close_caller is True
    assert detail_calls == [('series', 'tt1')]
    assert grid_calls[0][0] == [(ctx.gridwindow.NEW_EPISODES_BAND, [episode])]
    # Acted on (selected), so it must be persisted seen - the row's own
    # "mark seen on action, not on render" rule.
    assert fake_store.get_seen_episodes() == {'series\x1ftt1\x1fs1e2': True}


def test_onclick_new_episodes_row_stays_open_and_marks_nothing_when_grid_returns_none(
    load_catalogpicker, monkeypatch,
):
    ctx = load_catalogpicker()
    picker = ctx.catalogpicker
    import xbmcgui
    win = _make_window(picker)
    win.new_episode_items = [{'type': 'series', 'id': 'tt1', 'video_id': 's1e2'}]
    item = xbmcgui.ListItem('label')
    item.setProperty('kind', 'new_episodes')
    win.getControl(picker.LIST).addItems([item])
    fake_store = _FakeStore()
    monkeypatch.setattr(picker, 'get_store', lambda: fake_store)
    monkeypatch.setattr(ctx.gridwindow, 'open_grid', lambda bands, heading='', labels=None: None)

    win.onClick(picker.LIST)

    assert win.closed is False
    assert fake_store.get_seen_episodes() == {}


# ---------------------------------------------------------------------------
# _followed_series() - moved here from lib.ui.homewindow with the row itself
# ---------------------------------------------------------------------------


def test_followed_series_caps_the_number_of_candidates_fetched(load_catalogpicker):
    """Bounds `_fetch_series_metas()`'s fan-out to `MAX_NEW_EPISODE_SERIES`
    series per render, regardless of how many series `progress.json` has
    accumulated - see `MAX_NEW_EPISODE_SERIES`'s docstring for the cold-
    metacache render-blocking risk this guards against."""
    ctx = load_catalogpicker()
    cap = ctx.catalogpicker.MAX_NEW_EPISODE_SERIES
    total = cap + 10
    entries = [
        {
            'type': 'series', 'id': 'tt%d' % i, 'video_id': None,
            'position_ms': 500, 'duration_ms': 1000,
            'updated_at': '2026-08-%02dT00:00:00Z' % (i + 1),
        }
        for i in range(total)
    ]
    store = _FakeStore(progress_entries=entries)

    series = ctx.catalogpicker._followed_series(store)

    assert len(series) == cap
    # latest_by_title() already sorts most-recently-updated first - the
    # slice must keep that end of the list, not an arbitrary cap.
    assert {s['id'] for s in series} == {'tt%d' % i for i in range(10, total)}
