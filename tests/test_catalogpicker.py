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
WindowXMLDialog.doModal() is a no-op counter, and getControl()/setFocusId()
are plain in-memory fakes. CatalogPickerWindow.xml's actual skin rendering is
Kodi-skin-engine-only and is NOT, and cannot be, exercised by this suite.
"""
import contextlib

import pytest

import lib.store as store_module
from lib.stremio.addons import AddonError
from tests.kodistubs import install_kodi_stubs

_RELOAD_MODULE_NAMES = (
    'lib.ui.compat', 'lib.ui.uicommon', 'lib.ui.router',
    'lib.ui.views', 'lib.ui.infowindow', 'lib.ui.catalogpicker',
)


class _FakeStore:
    """Fake `lib.store.Store`: only `get_addons()` matters to
    open_catalog_picker()."""

    def __init__(self, addons=None):
        self._addons = addons or []

    def get_addons(self):
        return self._addons


@pytest.fixture
def load_catalogpicker():
    """Factory fixture: `load_catalogpicker(addon_info=None)` installs fresh
    stubs (via tests.kodistubs.install_kodi_stubs) reloading lib.ui.compat/
    lib.ui.uicommon/lib.ui.router/lib.ui.views/lib.ui.infowindow/
    lib.ui.catalogpicker, and returns a namespace with `.catalogpicker`,
    `.compat`, `.router`, `.views`, `.infowindow`, and `.env`. Every call is
    torn down automatically, in reverse order, at test end.
    """
    with contextlib.ExitStack() as stack:
        def _load(addon_info=None):
            return stack.enter_context(install_kodi_stubs(
                reload=_RELOAD_MODULE_NAMES,
                addon_info=addon_info,
            ))

        yield _load


def _make_window(catalogpicker_mod):
    return catalogpicker_mod.CatalogPickerWindow('CatalogPickerWindow.xml', '/addon/path', 'Default', '720p')


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
    assert [item.getLabel() for item in items] == [
        'Addon A: Top (movie)',
        '?: series-catalog (series)',
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
    monkeypatch.setattr(win, '_open_catalog', lambda transport, catalog: calls.append((transport, catalog)))

    win.onClick(picker.LIST)

    assert calls == [('https://b.example/manifest.json', {'id': 'new', 'type': 'series'})]


# ---------------------------------------------------------------------------
# CatalogPickerWindow._open_catalog()
# ---------------------------------------------------------------------------


def test_open_catalog_addon_error_is_logged_and_does_not_close(load_catalogpicker, monkeypatch):
    ctx = load_catalogpicker()
    win = _make_window(ctx.catalogpicker)

    def _raise(transport, ctype, cid):
        raise AddonError('upstream down')

    monkeypatch.setattr(ctx.views, '_fetch_catalog', _raise)

    win._open_catalog('https://a.example/manifest.json', {'type': 'movie', 'id': 'top'})

    assert win.should_close_caller is False
    assert win.closed is False
    assert ctx.env.executed_builtins == []


def test_open_catalog_empty_results_does_not_close_or_fallback(load_catalogpicker, monkeypatch):
    ctx = load_catalogpicker()
    win = _make_window(ctx.catalogpicker)
    monkeypatch.setattr(ctx.views, '_fetch_catalog', lambda transport, ctype, cid: [])

    win._open_catalog('https://a.example/manifest.json', {'type': 'movie', 'id': 'top'})

    assert win.should_close_caller is False
    assert win.closed is False
    assert ctx.env.executed_builtins == []


def test_open_catalog_no_selection_does_not_fallback_or_close(load_catalogpicker, monkeypatch):
    ctx = load_catalogpicker()
    win = _make_window(ctx.catalogpicker)
    metas = [{'id': 'tt1', 'name': 'One', 'type': 'movie'}]
    monkeypatch.setattr(ctx.views, '_fetch_catalog', lambda transport, ctype, cid: metas)
    monkeypatch.setattr(ctx.infowindow, 'open_showcase', lambda m: None)

    win._open_catalog('https://a.example/manifest.json', {'type': 'movie', 'id': 'top'})

    assert win.should_close_caller is False
    assert win.closed is False
    assert ctx.env.executed_builtins == []


def test_open_catalog_with_selection_falls_back_to_classical_meta_and_closes(load_catalogpicker, monkeypatch):
    ctx = load_catalogpicker()
    ctx.router.BASE_URL = 'plugin://plugin.video.rivulet/'
    win = _make_window(ctx.catalogpicker)
    metas = [{'id': 'tt1', 'name': 'One', 'type': 'series'}]
    monkeypatch.setattr(ctx.views, '_fetch_catalog', lambda transport, ctype, cid: metas)
    monkeypatch.setattr(ctx.infowindow, 'open_showcase', lambda m: m[0])

    win._open_catalog('https://a.example/manifest.json', {'type': 'movie', 'id': 'top'})

    assert win.should_close_caller is True
    assert win.closed is True
    assert ctx.env.executed_builtins == [
        'Container.Update(plugin://plugin.video.rivulet/?action=meta&type=series&id=tt1)'
    ]


def test_open_catalog_selected_meta_without_type_falls_back_to_the_catalogs_own_type(
    load_catalogpicker, monkeypatch,
):
    ctx = load_catalogpicker()
    ctx.router.BASE_URL = 'plugin://plugin.video.rivulet/'
    win = _make_window(ctx.catalogpicker)
    metas = [{'id': 'tt2', 'name': 'Two'}]  # no 'type' key on the selected meta
    monkeypatch.setattr(ctx.views, '_fetch_catalog', lambda transport, ctype, cid: metas)
    monkeypatch.setattr(ctx.infowindow, 'open_showcase', lambda m: m[0])

    win._open_catalog('https://a.example/manifest.json', {'type': 'movie', 'id': 'top'})

    assert ctx.env.executed_builtins == [
        'Container.Update(plugin://plugin.video.rivulet/?action=meta&type=movie&id=tt2)'
    ]


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
    ctx.router.BASE_URL = 'plugin://plugin.video.rivulet/'
    picker = ctx.catalogpicker
    win = _make_window(picker)
    catalogs = [('https://a.example/manifest.json', {'name': 'A'}, {'id': 'top', 'type': 'movie'})]
    metas = [{'id': 'tt1', 'name': 'One', 'type': 'movie'}]
    monkeypatch.setattr(ctx.views, '_fetch_catalog', lambda transport, ctype, cid: metas)
    monkeypatch.setattr(ctx.infowindow, 'open_showcase', lambda m: m[0])

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
    monkeypatch.setattr(store_module, 'Store', lambda *a, **k: _FakeStore(addons=[]))

    result = ctx.catalogpicker.open_catalog_picker()

    assert result is False
    assert ctx.env.notifications == [('Rivulet', 'STR30030', 'info', 4000)]


def test_open_catalog_picker_opens_window_with_discovered_catalogs(load_catalogpicker, monkeypatch):
    ctx = load_catalogpicker(addon_info={'path': '/addon/path'})
    descriptor = {
        'transportUrl': 'https://a.example/manifest.json',
        'manifest': {'name': 'Addon A', 'catalogs': [{'id': 'top', 'type': 'movie'}]},
    }
    monkeypatch.setattr(store_module, 'Store', lambda *a, **k: _FakeStore(addons=[descriptor]))
    captured = {}

    class RecordingWindow(ctx.catalogpicker.CatalogPickerWindow):
        def __init__(self, *args, **kwargs):
            captured['init_args'] = args
            super().__init__(*args, **kwargs)

        def start(self, catalogs):
            captured['catalogs'] = catalogs
            return True

    monkeypatch.setattr(ctx.catalogpicker, 'CatalogPickerWindow', RecordingWindow)

    result = ctx.catalogpicker.open_catalog_picker()

    assert result is True
    assert captured['init_args'] == ('CatalogPickerWindow.xml', '/addon/path', 'Default', '720p')
    assert captured['catalogs'] == [
        ('https://a.example/manifest.json', descriptor['manifest'], {'id': 'top', 'type': 'movie'}),
    ]
