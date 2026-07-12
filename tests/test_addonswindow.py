"""Tests for lib.ui.addonswindow: AddonsWindow, Rivulet's custom
replacement for the classical `views.addons()` directory, exercised
against the shared fake xbmc/xbmcgui stubs in tests/kodistubs (no real
Kodi runtime, no network).

lib.ui.addonswindow imports xbmcgui and lib.ui.uicommon at module scope,
and every other collaborator (`lib.store.Store`, `lib.stremio.addons.
AddonClient`/`AddonError`, `lib.ui.compat.L`/`log`/`notify`/
`addon_profile_dir`, `lib.ui.views._sync_addons_if_logged_in`) is
imported lazily inside the method that needs it - so this file fakes
`lib.store.Store` and `lib.stremio.addons.AddonClient` by monkeypatching
those modules' attributes directly (the same way test_catalogpicker.py
patches `lib.store.Store`), rather than reloading lib.ui.addonswindow
itself.

AddonsWindow.onInit()/onClick() are called directly here, never through a
real modal event loop, exactly like test_catalogpicker.py drives
CatalogPickerWindow: the fake WindowXML.doModal() is a no-op counter, and
getControl()/setFocusId() are plain in-memory fakes. AddonsWindow.xml's
actual skin rendering is Kodi-skin-engine-only and is NOT, and cannot be,
exercised by this suite.
"""
import contextlib

import pytest

import lib.store as store_module
import lib.stremio.addons as addons_module
from lib.stremio.addons import AddonError
from tests.kodistubs import install_kodi_stubs

_RELOAD_MODULE_NAMES = (
    'lib.ui.compat', 'lib.ui.uicommon', 'lib.ui.router', 'lib.ui.views', 'lib.ui.addonswindow',
)


class _FakeStore:
    """Fake `lib.store.Store`: tracks `get_addons()`'s backing list plus
    every `install_addon`/`remove_addon` call, and reproduces
    `remove_addon`'s real protected-addon `ValueError` refusal."""

    def __init__(self, addons=None):
        self.addons = list(addons or [])
        self.installed = []
        self.removed = []

    def get_addons(self):
        return self.addons

    def install_addon(self, transport_url, manifest):
        self.installed.append((transport_url, manifest))
        self.addons.append({'transportUrl': transport_url, 'manifest': manifest, 'flags': {}})

    def remove_addon(self, transport_url):
        target = next((a for a in self.addons if a.get('transportUrl') == transport_url), None)
        if target is None:
            return
        if (target.get('flags') or {}).get('protected'):
            raise ValueError('cannot remove protected addon: %s' % transport_url)
        self.removed.append(transport_url)
        self.addons = [a for a in self.addons if a.get('transportUrl') != transport_url]

    def get_auth(self):
        return None


class _FakeAddonClient:
    """Fake `lib.stremio.addons.AddonClient`: `manifest(url)` returns
    `manifest_result` or raises `manifest_error`."""

    def __init__(self, manifest_result=None, manifest_error=None):
        self.manifest_result = manifest_result
        self.manifest_error = manifest_error
        self.manifest_calls = []

    def manifest(self, url):
        self.manifest_calls.append(url)
        if self.manifest_error is not None:
            raise self.manifest_error
        return self.manifest_result


@pytest.fixture
def load_addonswindow():
    """Factory fixture: `load_addonswindow(**kwargs)` installs fresh stubs
    (via tests.kodistubs.install_kodi_stubs) reloading lib.ui.compat/
    lib.ui.uicommon/lib.ui.router/lib.ui.views/lib.ui.addonswindow, and
    returns a namespace with `.addonswindow`, `.compat`, `.views`, and
    `.env`. Every call is torn down automatically, in reverse order, at
    test end."""
    with contextlib.ExitStack() as stack:
        def _load(**kwargs):
            return stack.enter_context(install_kodi_stubs(reload=_RELOAD_MODULE_NAMES, **kwargs))

        yield _load


def _make_window(addonswindow_mod):
    return addonswindow_mod.AddonsWindow('AddonsWindow.xml', '/addon/path', 'Default', '720p')


def _wire_store(monkeypatch, store):
    monkeypatch.setattr(store_module, 'Store', lambda *a, **k: store)


def _wire_client(monkeypatch, client):
    monkeypatch.setattr(addons_module, 'AddonClient', lambda *a, **k: client)


# ---------------------------------------------------------------------------
# AddonsWindow.onInit() / _reload() - item building
# ---------------------------------------------------------------------------


def test_oninit_builds_install_row_and_one_row_per_addon(load_addonswindow, monkeypatch):
    ctx = load_addonswindow()
    descriptor = {
        'transportUrl': 'https://a.example/manifest.json',
        'manifest': {'name': 'Addon A', 'version': '1.2.3', 'description': 'Line one\r\nLine two'},
        'flags': {},
    }
    _wire_store(monkeypatch, _FakeStore(addons=[descriptor]))
    win = _make_window(ctx.addonswindow)

    win.onInit()

    items = win.getControl(ctx.addonswindow.LIST).items
    assert len(items) == 2
    install_item, addon_item = items
    assert install_item.getLabel() == 'STR30010'
    assert install_item.getProperty('position') == 'install'
    assert addon_item.getLabel() == 'Addon A  \u00b7  v1.2.3'
    assert addon_item.label2 == 'Line one Line two'
    assert addon_item.getProperty('position') == '0'
    assert win.getFocusId() == ctx.addonswindow.LIST


def test_oninit_truncates_long_descriptions_to_one_line(load_addonswindow, monkeypatch):
    ctx = load_addonswindow()
    descriptor = {
        'transportUrl': 'https://a.example/manifest.json',
        'manifest': {'name': 'Addon A', 'version': '1.0', 'description': 'x' * 200},
        'flags': {},
    }
    _wire_store(monkeypatch, _FakeStore(addons=[descriptor]))
    win = _make_window(ctx.addonswindow)

    win.onInit()

    addon_item = win.getControl(ctx.addonswindow.LIST).items[1]
    assert len(addon_item.label2) <= 120
    assert addon_item.label2.endswith('...')
    assert '\n' not in addon_item.label2


# ---------------------------------------------------------------------------
# AddonsWindow.onClick() - dispatch
# ---------------------------------------------------------------------------


def test_onclick_ignores_control_ids_other_than_list(load_addonswindow, monkeypatch):
    ctx = load_addonswindow()
    _wire_store(monkeypatch, _FakeStore())
    win = _make_window(ctx.addonswindow)
    win.onInit()
    calls = []
    monkeypatch.setattr(win, '_install', lambda: calls.append('install'))

    win.onClick(9999)

    assert calls == []


def test_onclick_list_with_no_focused_item_does_not_crash(load_addonswindow, monkeypatch):
    ctx = load_addonswindow()
    _wire_store(monkeypatch, _FakeStore())
    win = _make_window(ctx.addonswindow)
    # No onInit() call -> the list control is never populated.

    win.onClick(ctx.addonswindow.LIST)  # must not raise


# ---------------------------------------------------------------------------
# AddonsWindow._install() - install-from-URL row
# ---------------------------------------------------------------------------


def test_install_empty_url_is_a_noop(load_addonswindow, monkeypatch):
    ctx = load_addonswindow()  # no dialog_inputs -> Dialog.input() returns ''
    store = _FakeStore()
    _wire_store(monkeypatch, store)
    _wire_client(monkeypatch, _FakeAddonClient())
    win = _make_window(ctx.addonswindow)
    win.onInit()

    win.onClick(ctx.addonswindow.LIST)  # focused item is the install row

    assert store.installed == []
    assert ctx.env.notifications == []


def test_install_addon_error_notifies_and_does_not_install(load_addonswindow, monkeypatch):
    ctx = load_addonswindow(dialog_inputs=['https://bad.example/manifest.json'])
    store = _FakeStore()
    _wire_store(monkeypatch, store)
    _wire_client(monkeypatch, _FakeAddonClient(manifest_error=AddonError('404')))
    win = _make_window(ctx.addonswindow)
    win.onInit()

    win.onClick(ctx.addonswindow.LIST)

    assert store.installed == []
    assert ctx.env.notifications == [('Rivulet', 'STR30014', 'info', 4000)]


def test_install_manifest_missing_id_notifies_and_does_not_install(load_addonswindow, monkeypatch):
    ctx = load_addonswindow(dialog_inputs=['https://bad.example/manifest.json'])
    store = _FakeStore()
    _wire_store(monkeypatch, store)
    _wire_client(monkeypatch, _FakeAddonClient(manifest_result={'name': 'No Id Here'}))
    win = _make_window(ctx.addonswindow)
    win.onInit()

    win.onClick(ctx.addonswindow.LIST)

    assert store.installed == []
    assert ctx.env.notifications == [('Rivulet', 'STR30014', 'info', 4000)]


def test_install_success_persists_notifies_and_reloads(load_addonswindow, monkeypatch):
    url = 'https://new.example/manifest.json'
    ctx = load_addonswindow(dialog_inputs=[url])
    store = _FakeStore()
    manifest = {'id': 'org.new', 'name': 'New Addon', 'version': '1.0'}
    _wire_store(monkeypatch, store)
    _wire_client(monkeypatch, _FakeAddonClient(manifest_result=manifest))
    win = _make_window(ctx.addonswindow)
    win.onInit()

    win.onClick(ctx.addonswindow.LIST)

    assert store.installed == [(url, manifest)]
    assert ctx.env.notifications == [('Rivulet', 'STR30012', 'info', 4000)]
    # _reload() re-populated the list with the freshly-installed addon.
    items = win.getControl(ctx.addonswindow.LIST).items
    assert len(items) == 2
    assert items[1].getLabel() == 'New Addon  \u00b7  v1.0'


# ---------------------------------------------------------------------------
# AddonsWindow._remove() - addon rows
# ---------------------------------------------------------------------------


def _descriptor(transport='https://a.example/manifest.json', name='Addon A', protected=False):
    return {
        'transportUrl': transport,
        'manifest': {'name': name, 'version': '1.0'},
        'flags': {'protected': protected},
    }


def test_remove_confirmed_removes_notifies_and_reloads(load_addonswindow, monkeypatch):
    descriptor = _descriptor()
    ctx = load_addonswindow(dialog_yesno=[True])
    store = _FakeStore(addons=[descriptor])
    _wire_store(monkeypatch, store)
    win = _make_window(ctx.addonswindow)
    win.onInit()
    win.getControl(ctx.addonswindow.LIST).selected_index = 1  # the addon row

    win.onClick(ctx.addonswindow.LIST)

    assert store.removed == [descriptor['transportUrl']]
    assert ctx.env.notifications == [('Rivulet', 'STR30013', 'info', 4000)]
    assert ctx.env.dialog_yesno_prompts == [('STR30011', 'Addon A')]
    items_after = win.getControl(ctx.addonswindow.LIST).items
    assert len(items_after) == 1
    assert items_after[0].getProperty('position') == 'install'


def test_remove_declined_leaves_addon_untouched(load_addonswindow, monkeypatch):
    descriptor = _descriptor()
    ctx = load_addonswindow(dialog_yesno=[False])
    store = _FakeStore(addons=[descriptor])
    _wire_store(monkeypatch, store)
    win = _make_window(ctx.addonswindow)
    win.onInit()
    win.getControl(ctx.addonswindow.LIST).selected_index = 1

    win.onClick(ctx.addonswindow.LIST)

    assert store.removed == []
    assert ctx.env.notifications == []


def test_remove_protected_addon_notifies_and_never_calls_remove(load_addonswindow, monkeypatch):
    descriptor = _descriptor(protected=True)
    ctx = load_addonswindow(dialog_yesno=[True])  # scripted answer must never even be consulted
    store = _FakeStore(addons=[descriptor])
    _wire_store(monkeypatch, store)
    win = _make_window(ctx.addonswindow)
    win.onInit()
    win.getControl(ctx.addonswindow.LIST).selected_index = 1

    win.onClick(ctx.addonswindow.LIST)

    assert store.removed == []
    assert ctx.env.dialog_yesno_prompts == []
    assert ctx.env.notifications == [
        ('Rivulet', 'This addon is protected and cannot be removed', 'info', 4000),
    ]


def test_remove_store_raises_valueerror_notifies_protected(load_addonswindow, monkeypatch):
    """Belt-and-suspenders: even if the descriptor's own `flags.protected`
    check were ever bypassed, `Store.remove_addon`'s own `ValueError`
    refusal is still caught and notified the same way."""
    descriptor = _descriptor()
    ctx = load_addonswindow(dialog_yesno=[True])
    store = _FakeStore(addons=[descriptor])

    def _raise(transport_url):
        raise ValueError('cannot remove protected addon: %s' % transport_url)

    monkeypatch.setattr(store, 'remove_addon', _raise)
    _wire_store(monkeypatch, store)
    win = _make_window(ctx.addonswindow)
    win.onInit()
    win.getControl(ctx.addonswindow.LIST).selected_index = 1

    win.onClick(ctx.addonswindow.LIST)

    assert ctx.env.notifications == [
        ('Rivulet', 'This addon is protected and cannot be removed', 'info', 4000),
    ]


# ---------------------------------------------------------------------------
# open_addons()
# ---------------------------------------------------------------------------


def test_open_addons_opens_window_and_runs_modal(load_addonswindow, monkeypatch):
    ctx = load_addonswindow(addon_info={'path': '/addon/path'})
    descriptor = _descriptor()
    _wire_store(monkeypatch, _FakeStore(addons=[descriptor]))
    captured = {}

    class RecordingWindow(ctx.addonswindow.AddonsWindow):
        def __init__(self, *args, **kwargs):
            captured['init_args'] = args
            super().__init__(*args, **kwargs)

        def doModal(self):
            captured['modal_called'] = True

    monkeypatch.setattr(ctx.addonswindow, 'AddonsWindow', RecordingWindow)

    result = ctx.addonswindow.open_addons()

    assert result is None
    assert captured['init_args'] == ('AddonsWindow.xml', '/addon/path', 'Default', '720p')
    assert captured['modal_called'] is True


def test_open_addons_window_is_closed_exactly_once_when_domodal_raises(load_addonswindow, monkeypatch):
    ctx = load_addonswindow(addon_info={'path': '/addon/path'})
    _wire_store(monkeypatch, _FakeStore())
    captured = {}

    class ExplodingWindow(ctx.addonswindow.AddonsWindow):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.close_calls = 0
            captured['window'] = self

        def close(self):
            self.close_calls += 1
            super().close()

        def doModal(self):
            raise RuntimeError('onInit blew up')

    monkeypatch.setattr(ctx.addonswindow, 'AddonsWindow', ExplodingWindow)

    result = ctx.addonswindow.open_addons()

    assert result is None
    win = captured['window']
    assert win.close_calls == 1
    assert win.closed is True
    assert ctx.env.notifications == [('Rivulet', 'STR30032', 'info', 4000)]
