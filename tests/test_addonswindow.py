"""Tests for lib.ui.addonswindow: AddonsWindow, Rivulet's add-on
manager, exercised against the shared fake xbmc/xbmcgui stubs in
tests/kodistubs (no real
Kodi runtime, no network).

lib.ui.addonswindow imports xbmcgui, lib.ui.uicommon, and `get_store`/
`get_client` (from lib.ui.dependencies) at module scope; every other
collaborator (`lib.store.ConcurrentUpdateError`, `lib.stremio.addons.AddonError`,
`lib.ui.compat.L`/`log`/`notify`, `lib.ui.dialogs.confirm`, `lib.ui.views._sync_addons_if_logged_in`) is imported lazily
inside the method that needs it - so this file fakes the shared
Store/AddonClient providers by assigning directly to
`addonswindow.get_store`/`addonswindow.get_client` (the same way
tests/test_views.py wires `views.get_store`/`views.get_client`), rather
than monkeypatching `lib.store`/`lib.stremio.addons`.

AddonsWindow.onInit()/onClick() are called directly here, never through a
real modal event loop, exactly like test_catalogpicker.py drives
CatalogPickerWindow: the fake WindowXML.doModal() is a no-op counter, and
getControl()/setFocusId() are plain in-memory fakes. AddonsWindow.xml's
actual skin rendering is Kodi-skin-engine-only and is NOT, and cannot be,
exercised by this suite.
"""
import contextlib

import pytest

from lib.store import ConcurrentUpdateError
from lib.stremio.addons import AddonError
from tests.kodistubs import install_kodi_stubs

_RELOAD_MODULE_NAMES = (
    'lib.ui.compat', 'lib.ui.dependencies', 'lib.ui.uicommon', 'lib.ui.router', 'lib.ui.views',
    'lib.ui.dialogs', 'lib.ui.addonswindow',
)


class _FakeStore:
    """Fake `lib.store.Store`: tracks `get_addons()`'s backing list plus
    every `install_addon`/`remove_addon`/`set_addon_disabled`/
    `move_addon` call, and reproduces `remove_addon`'s real
    protected-addon `ValueError` refusal and `move_addon`'s real
    not-installed `ValueError` refusal plus boundary clamping."""

    def __init__(self, addons=None):
        self.addons = list(addons or [])
        self.installed = []
        self.removed = []
        self.disable_calls = []
        self.move_calls = []

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

    def set_addon_disabled(self, transport_url, disabled):
        self.disable_calls.append((transport_url, disabled))
        target = next((a for a in self.addons if a.get('transportUrl') == transport_url), None)
        if target is None:
            return
        flags = dict(target.get('flags') or {})
        if disabled:
            flags['disabled'] = True
        else:
            flags.pop('disabled', None)
        target['flags'] = flags

    def move_addon(self, transport_url, delta):
        self.move_calls.append((transport_url, delta))
        index = next((i for i, a in enumerate(self.addons) if a.get('transportUrl') == transport_url), None)
        if index is None:
            raise ValueError('addon not installed: %s' % transport_url)
        target = max(0, min(len(self.addons) - 1, index + delta))
        if target == index:
            return
        self.addons.insert(target, self.addons.pop(index))

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
    return addonswindow_mod.AddonsWindow('AddonsWindow.xml', '/addon/path', 'Default', '1080i')


def _wire_store(addonswindow_mod, store):
    addonswindow_mod.get_store = lambda: store


def _wire_client(addonswindow_mod, client):
    addonswindow_mod.get_client = lambda: client


def _stub_confirm(monkeypatch, ctx, answer, capture=None):
    """Patches `lib.ui.dialogs.confirm` directly (already exhaustively
    covered by tests/test_dialogs.py) rather than driving a real
    `doModal()` - this suite only needs to prove `_remove()` passes the
    right heading/body/labels and reacts correctly to the result."""
    def _confirm(heading, body, yeslabel, nolabel):
        if capture is not None:
            capture.append((heading, body, yeslabel, nolabel))
        return answer

    monkeypatch.setattr(ctx.dialogs, 'confirm', _confirm)


def _stub_choose(monkeypatch, ctx, index, capture=None):
    """Patches `lib.ui.dialogs.choose` directly (already exhaustively
    covered by tests/test_dialogs.py) rather than driving a real
    `doModal()` - this suite only needs to prove `_open_actions()` passes
    the right heading/rows and reacts correctly to the picked index."""
    def _choose(heading, rows):
        if capture is not None:
            capture.append((heading, rows))
        return index

    monkeypatch.setattr(ctx.dialogs, 'choose', _choose)


# ---------------------------------------------------------------------------
# AddonsWindow.onInit() / _reload() - item building
# ---------------------------------------------------------------------------


def test_oninit_builds_add_row_and_one_row_per_addon(load_addonswindow, monkeypatch):
    ctx = load_addonswindow()
    descriptor = {
        'transportUrl': 'https://a.example/manifest.json',
        'manifest': {'name': 'Addon A', 'version': '1.2.3', 'description': 'Line one\r\nLine two'},
        'flags': {},
    }
    _wire_store(ctx.addonswindow, _FakeStore(addons=[descriptor]))
    win = _make_window(ctx.addonswindow)

    win.onInit()

    items = win.getControl(ctx.addonswindow.LIST).items
    assert len(items) == 2
    add_item, addon_item = items
    assert add_item.getLabel() == 'STR30350'
    assert add_item.label2 == 'STR30351'
    assert add_item.getProperty('position') == 'add'
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
    _wire_store(ctx.addonswindow, _FakeStore(addons=[descriptor]))
    win = _make_window(ctx.addonswindow)

    win.onInit()

    addon_item = win.getControl(ctx.addonswindow.LIST).items[1]
    assert len(addon_item.label2) <= 120
    assert addon_item.label2.endswith('...')
    assert '\n' not in addon_item.label2


def test_oninit_marks_disabled_addon_row_label(load_addonswindow, monkeypatch):
    ctx = load_addonswindow()
    descriptor = {
        'transportUrl': 'https://a.example/manifest.json',
        'manifest': {'name': 'Addon A', 'version': '1.0'},
        'flags': {'disabled': True},
    }
    _wire_store(ctx.addonswindow, _FakeStore(addons=[descriptor]))
    win = _make_window(ctx.addonswindow)

    win.onInit()

    addon_item = win.getControl(ctx.addonswindow.LIST).items[1]
    assert addon_item.getLabel() == 'Addon A  \u00b7  v1.0  \u00b7  STR30251'


# ---------------------------------------------------------------------------
# AddonsWindow.onClick() - dispatch
# ---------------------------------------------------------------------------


def test_onclick_ignores_control_ids_other_than_list(load_addonswindow, monkeypatch):
    ctx = load_addonswindow()
    _wire_store(ctx.addonswindow, _FakeStore())
    win = _make_window(ctx.addonswindow)
    win.onInit()
    calls = []
    monkeypatch.setattr(win, '_install', lambda: calls.append('install'))

    win.onClick(9999)

    assert calls == []


def test_onclick_list_with_no_focused_item_does_not_crash(load_addonswindow, monkeypatch):
    ctx = load_addonswindow()
    _wire_store(ctx.addonswindow, _FakeStore())
    win = _make_window(ctx.addonswindow)
    # No onInit() call -> the list control is never populated.

    win.onClick(ctx.addonswindow.LIST)  # must not raise


def test_onclick_addon_row_opens_action_menu_with_disable_and_remove_rows(load_addonswindow, monkeypatch):
    descriptor = _descriptor(name='Addon A')
    ctx = load_addonswindow()
    captured = []
    _stub_choose(monkeypatch, ctx, -1, capture=captured)
    _wire_store(ctx.addonswindow, _FakeStore(addons=[descriptor]))
    win = _make_window(ctx.addonswindow)
    win.onInit()
    win.getControl(ctx.addonswindow.LIST).selected_index = 1

    win.onClick(ctx.addonswindow.LIST)

    assert captured == [('Addon A', ['STR30248', 'STR30250'])]


def test_onclick_addon_row_opens_action_menu_with_enable_row_when_disabled(load_addonswindow, monkeypatch):
    descriptor = _descriptor(name='Addon A')
    descriptor['flags']['disabled'] = True
    ctx = load_addonswindow()
    captured = []
    _stub_choose(monkeypatch, ctx, -1, capture=captured)
    _wire_store(ctx.addonswindow, _FakeStore(addons=[descriptor]))
    win = _make_window(ctx.addonswindow)
    win.onInit()
    win.getControl(ctx.addonswindow.LIST).selected_index = 1

    win.onClick(ctx.addonswindow.LIST)

    assert captured == [('Addon A', ['STR30249', 'STR30250'])]


def test_onclick_first_addon_menu_omits_move_up(load_addonswindow, monkeypatch):
    """The first addon's action menu must not offer "Move up" - it would
    be an inert entry on a remote-control menu."""
    first = _descriptor(transport='https://a.example/manifest.json', name='Addon A')
    second = _descriptor(transport='https://b.example/manifest.json', name='Addon B')
    ctx = load_addonswindow()
    captured = []
    _stub_choose(monkeypatch, ctx, -1, capture=captured)
    _wire_store(ctx.addonswindow, _FakeStore(addons=[first, second]))
    win = _make_window(ctx.addonswindow)
    win.onInit()
    win.getControl(ctx.addonswindow.LIST).selected_index = 1  # first addon row

    win.onClick(ctx.addonswindow.LIST)

    assert captured == [('Addon A', ['STR30248', 'STR30250', 'STR30271'])]


def test_onclick_last_addon_menu_omits_move_down(load_addonswindow, monkeypatch):
    """The last addon's action menu must not offer "Move down"."""
    first = _descriptor(transport='https://a.example/manifest.json', name='Addon A')
    second = _descriptor(transport='https://b.example/manifest.json', name='Addon B')
    ctx = load_addonswindow()
    captured = []
    _stub_choose(monkeypatch, ctx, -1, capture=captured)
    _wire_store(ctx.addonswindow, _FakeStore(addons=[first, second]))
    win = _make_window(ctx.addonswindow)
    win.onInit()
    win.getControl(ctx.addonswindow.LIST).selected_index = 2  # second addon row

    win.onClick(ctx.addonswindow.LIST)

    assert captured == [('Addon B', ['STR30248', 'STR30250', 'STR30270'])]


def test_onclick_middle_addon_menu_offers_both_move_directions(load_addonswindow, monkeypatch):
    first = _descriptor(transport='https://a.example/manifest.json', name='Addon A')
    middle = _descriptor(transport='https://b.example/manifest.json', name='Addon B')
    last = _descriptor(transport='https://c.example/manifest.json', name='Addon C')
    ctx = load_addonswindow()
    captured = []
    _stub_choose(monkeypatch, ctx, -1, capture=captured)
    _wire_store(ctx.addonswindow, _FakeStore(addons=[first, middle, last]))
    win = _make_window(ctx.addonswindow)
    win.onInit()
    win.getControl(ctx.addonswindow.LIST).selected_index = 2  # middle addon row

    win.onClick(ctx.addonswindow.LIST)

    assert captured == [('Addon B', ['STR30248', 'STR30250', 'STR30270', 'STR30271'])]


def test_onclick_dismissed_action_menu_mutates_nothing(load_addonswindow, monkeypatch):
    descriptor = _descriptor()
    ctx = load_addonswindow()
    _stub_choose(monkeypatch, ctx, -1)
    store = _FakeStore(addons=[descriptor])
    _wire_store(ctx.addonswindow, store)
    win = _make_window(ctx.addonswindow)
    win.onInit()
    win.getControl(ctx.addonswindow.LIST).selected_index = 1

    win.onClick(ctx.addonswindow.LIST)

    assert store.removed == []
    assert store.disable_calls == []
    assert ctx.env.notifications == []


def test_onclick_toggle_row_disables_addon_notifies_and_reloads(load_addonswindow, monkeypatch):
    descriptor = _descriptor()
    ctx = load_addonswindow()
    _stub_choose(monkeypatch, ctx, 0)
    store = _FakeStore(addons=[descriptor])
    _wire_store(ctx.addonswindow, store)
    win = _make_window(ctx.addonswindow)
    win.onInit()
    win.getControl(ctx.addonswindow.LIST).selected_index = 1

    win.onClick(ctx.addonswindow.LIST)

    assert store.disable_calls == [(descriptor['transportUrl'], True)]
    assert ctx.env.notifications == [('Rivulet', 'STR30251', 'info', 4000)]
    addon_item = win.getControl(ctx.addonswindow.LIST).items[1]
    assert addon_item.getLabel().endswith('STR30251')


def test_onclick_toggle_row_enables_addon_notifies_and_reloads(load_addonswindow, monkeypatch):
    descriptor = _descriptor()
    descriptor['flags']['disabled'] = True
    ctx = load_addonswindow()
    _stub_choose(monkeypatch, ctx, 0)
    store = _FakeStore(addons=[descriptor])
    _wire_store(ctx.addonswindow, store)
    win = _make_window(ctx.addonswindow)
    win.onInit()
    win.getControl(ctx.addonswindow.LIST).selected_index = 1

    win.onClick(ctx.addonswindow.LIST)

    assert store.disable_calls == [(descriptor['transportUrl'], False)]
    assert ctx.env.notifications == [('Rivulet', 'STR30252', 'info', 4000)]
    addon_item = win.getControl(ctx.addonswindow.LIST).items[1]
    assert addon_item.getLabel() == 'Addon A  \u00b7  v1.0'


def test_onclick_toggle_protected_addon_still_works(load_addonswindow, monkeypatch):
    """Unlike removal, disabling is reversible local state - there is no
    protected-addon refusal branch for it."""
    descriptor = _descriptor(protected=True)
    ctx = load_addonswindow()
    _stub_choose(monkeypatch, ctx, 0)
    store = _FakeStore(addons=[descriptor])
    _wire_store(ctx.addonswindow, store)
    win = _make_window(ctx.addonswindow)
    win.onInit()
    win.getControl(ctx.addonswindow.LIST).selected_index = 1

    win.onClick(ctx.addonswindow.LIST)

    assert store.disable_calls == [(descriptor['transportUrl'], True)]
    assert ctx.env.notifications == [('Rivulet', 'STR30251', 'info', 4000)]


def test_onclick_toggle_never_calls_sync_addons(load_addonswindow, monkeypatch):
    """The disabled flag is local presentation state, not part of
    Stremio's addon-collection schema - toggling must never push a sync,
    unlike install/remove."""
    descriptor = _descriptor()
    ctx = load_addonswindow()
    _stub_choose(monkeypatch, ctx, 0)
    store = _FakeStore(addons=[descriptor])
    _wire_store(ctx.addonswindow, store)
    sync_calls = []
    monkeypatch.setattr(ctx.views, '_sync_addons_if_logged_in', lambda s: sync_calls.append(s))
    win = _make_window(ctx.addonswindow)
    win.onInit()
    win.getControl(ctx.addonswindow.LIST).selected_index = 1

    win.onClick(ctx.addonswindow.LIST)

    assert sync_calls == []


def test_onclick_move_up_row_reorders_list_and_keeps_focus_on_moved_addon(load_addonswindow, monkeypatch):
    first = _descriptor(transport='https://a.example/manifest.json', name='Addon A')
    middle = _descriptor(transport='https://b.example/manifest.json', name='Addon B')
    last = _descriptor(transport='https://c.example/manifest.json', name='Addon C')
    ctx = load_addonswindow()
    _stub_choose(monkeypatch, ctx, 2)  # "Move up" - 3rd row of the middle addon's 4-row menu
    store = _FakeStore(addons=[first, middle, last])
    _wire_store(ctx.addonswindow, store)
    win = _make_window(ctx.addonswindow)
    win.onInit()
    win.getControl(ctx.addonswindow.LIST).selected_index = 2  # middle addon row (Addon B)

    win.onClick(ctx.addonswindow.LIST)

    assert store.move_calls == [('https://b.example/manifest.json', -1)]
    assert [a['transportUrl'] for a in store.addons] == [
        'https://b.example/manifest.json', 'https://a.example/manifest.json', 'https://c.example/manifest.json',
    ]
    control = win.getControl(ctx.addonswindow.LIST)
    assert control.items[1].getLabel().startswith('Addon B')  # Addon B is now first
    assert control.selected_index == 1  # focus follows Addon B to its new row


def test_reload_focus_offset_is_derived_from_build_items_not_hardcoded(load_addonswindow, monkeypatch):
    """Pins the seam between `_build_items()` and `_reload()`: the focus
    offset after a move must equal `len(_build_items()) - len(addons)`,
    recomputed here independently rather than hardcoded to today's single
    static row (add). If a second static row is ever prepended without
    `_reload()` picking up the new count, this test's independently-
    derived expectation and the control's actual `selected_index` diverge
    and the test fails loudly, instead of the user's focus silently
    landing one row off after a move."""
    first = _descriptor(transport='https://a.example/manifest.json', name='Addon A')
    second = _descriptor(transport='https://b.example/manifest.json', name='Addon B')
    ctx = load_addonswindow()
    store = _FakeStore(addons=[first, second])
    _wire_store(ctx.addonswindow, store)
    win = _make_window(ctx.addonswindow)
    win.onInit()
    static_row_count = len(win._build_items()) - len(store.addons)

    win._reload(focus_transport_url='https://b.example/manifest.json')

    control = win.getControl(ctx.addonswindow.LIST)
    assert control.selected_index == 1 + static_row_count  # Addon B is addons-list index 1


def test_onclick_move_down_row_reorders_list_and_pushes_sync(load_addonswindow, monkeypatch):
    """Unlike `_toggle`'s local-only `disabled` flag, list order is part
    of the synced addon-collection shape, so a move must push like
    install/remove."""
    first = _descriptor(transport='https://a.example/manifest.json', name='Addon A')
    second = _descriptor(transport='https://b.example/manifest.json', name='Addon B')
    ctx = load_addonswindow()
    _stub_choose(monkeypatch, ctx, 2)  # "Move down" - 3rd row of the first addon's 3-row menu
    store = _FakeStore(addons=[first, second])
    _wire_store(ctx.addonswindow, store)
    sync_calls = []
    monkeypatch.setattr(ctx.views, '_sync_addons_if_logged_in', lambda s: sync_calls.append(s))
    win = _make_window(ctx.addonswindow)
    win.onInit()
    win.getControl(ctx.addonswindow.LIST).selected_index = 1  # first addon row (Addon A)

    win.onClick(ctx.addonswindow.LIST)

    assert store.move_calls == [('https://a.example/manifest.json', 1)]
    assert sync_calls == [store]
    assert [a['transportUrl'] for a in store.addons] == [
        'https://b.example/manifest.json', 'https://a.example/manifest.json',
    ]
    control = win.getControl(ctx.addonswindow.LIST)
    assert control.selected_index == 2  # Addon A followed its move to the second row


def test_onclick_move_concurrent_update_notifies_and_reloads_instead_of_raising(load_addonswindow, monkeypatch):
    """Same guard as toggle/remove - a losing CAS race on `move_addon()`
    must notify + reload rather than raise out of `_move()`."""
    first = _descriptor(transport='https://a.example/manifest.json', name='Addon A')
    second = _descriptor(transport='https://b.example/manifest.json', name='Addon B')
    ctx = load_addonswindow()
    _stub_choose(monkeypatch, ctx, 2)  # "Move down" on the first addon
    store = _FakeStore(addons=[first, second])

    def _raise(transport_url, delta):
        raise ConcurrentUpdateError('addons.json changed underneath us')

    monkeypatch.setattr(store, 'move_addon', _raise)
    _wire_store(ctx.addonswindow, store)
    win = _make_window(ctx.addonswindow)
    win.onInit()
    win.getControl(ctx.addonswindow.LIST).selected_index = 1
    reload_calls = []
    original_reload = win._reload

    def _counting_reload(*args, **kwargs):
        reload_calls.append(True)
        original_reload(*args, **kwargs)

    monkeypatch.setattr(win, '_reload', _counting_reload)

    win.onClick(ctx.addonswindow.LIST)  # must not raise ConcurrentUpdateError

    assert ctx.env.notifications == [('Rivulet', 'STR30032', 'info', 4000)]
    assert reload_calls == [True]


def test_onclick_move_store_raises_valueerror_notifies_without_reload(load_addonswindow, monkeypatch):
    """`_guard_mutation()` only catches `ConcurrentUpdateError` - if a
    concurrent `default.py` process removes the addon while
    `dialogs.choose()`'s action menu is still open (it blocks), `Store.
    move_addon()`'s own not-installed `ValueError` refusal must still
    propagate to `_move()`'s own handler, notifying the refusal string
    WITHOUT the extra `_reload()` a concurrent-update failure would
    trigger - same shape as `_remove()`'s protected-addon `ValueError`
    guard."""
    first = _descriptor(transport='https://a.example/manifest.json', name='Addon A')
    second = _descriptor(transport='https://b.example/manifest.json', name='Addon B')
    ctx = load_addonswindow()
    _stub_choose(monkeypatch, ctx, 2)  # "Move down" on the first addon
    store = _FakeStore(addons=[first, second])

    def _raise(transport_url, delta):
        raise ValueError('addon not installed: %s' % transport_url)

    monkeypatch.setattr(store, 'move_addon', _raise)
    _wire_store(ctx.addonswindow, store)
    win = _make_window(ctx.addonswindow)
    win.onInit()
    win.getControl(ctx.addonswindow.LIST).selected_index = 1
    reload_calls = []
    original_reload = win._reload

    def _counting_reload(*args, **kwargs):
        reload_calls.append(True)
        original_reload(*args, **kwargs)

    monkeypatch.setattr(win, '_reload', _counting_reload)

    win.onClick(ctx.addonswindow.LIST)  # must not raise ValueError

    assert ctx.env.notifications == [('Rivulet', 'STR30272', 'info', 4000)]
    assert reload_calls == []


# ---------------------------------------------------------------------------
# AddonsWindow._add_addons() - the single static row's chooser
# ---------------------------------------------------------------------------


def test_onclick_add_row_first_entry_opens_addon_catalog(load_addonswindow, monkeypatch):
    """The add row's chooser first entry ("Browse addon catalogs") must
    dispatch to `_open_addon_catalog()`, not `_install()`."""
    ctx = load_addonswindow()
    _stub_choose(monkeypatch, ctx, 0)
    _wire_store(ctx.addonswindow, _FakeStore())
    calls = []
    monkeypatch.setattr(ctx.addonswindow.AddonsWindow, '_open_addon_catalog', lambda self: calls.append('catalog'))
    monkeypatch.setattr(ctx.addonswindow.AddonsWindow, '_install', lambda self: calls.append('install'))
    win = _make_window(ctx.addonswindow)
    win.onInit()

    win.onClick(ctx.addonswindow.LIST)  # focused item is the add row

    assert calls == ['catalog']


def test_onclick_add_row_second_entry_opens_install_from_url(load_addonswindow, monkeypatch):
    """The add row's chooser second entry ("Install from URL") must
    dispatch to `_install()`, not `_open_addon_catalog()`."""
    ctx = load_addonswindow()
    _stub_choose(monkeypatch, ctx, 1)
    _wire_store(ctx.addonswindow, _FakeStore())
    calls = []
    monkeypatch.setattr(ctx.addonswindow.AddonsWindow, '_open_addon_catalog', lambda self: calls.append('catalog'))
    monkeypatch.setattr(ctx.addonswindow.AddonsWindow, '_install', lambda self: calls.append('install'))
    win = _make_window(ctx.addonswindow)
    win.onInit()

    win.onClick(ctx.addonswindow.LIST)  # focused item is the add row

    assert calls == ['install']


def test_onclick_add_row_dismissed_chooser_is_a_noop(load_addonswindow, monkeypatch):
    """Dismissing the add row's chooser (`dialogs.choose()` returns -1)
    must run neither flow."""
    ctx = load_addonswindow()
    _stub_choose(monkeypatch, ctx, -1)
    _wire_store(ctx.addonswindow, _FakeStore())
    calls = []
    monkeypatch.setattr(ctx.addonswindow.AddonsWindow, '_open_addon_catalog', lambda self: calls.append('catalog'))
    monkeypatch.setattr(ctx.addonswindow.AddonsWindow, '_install', lambda self: calls.append('install'))
    win = _make_window(ctx.addonswindow)
    win.onInit()

    win.onClick(ctx.addonswindow.LIST)  # focused item is the add row

    assert calls == []


# ---------------------------------------------------------------------------
# AddonsWindow._install() - install-from-URL row
# ---------------------------------------------------------------------------


def test_install_empty_url_is_a_noop(load_addonswindow, monkeypatch):
    ctx = load_addonswindow()  # no dialog_inputs -> Dialog.input() returns ''
    store = _FakeStore()
    _wire_store(ctx.addonswindow, store)
    _wire_client(ctx.addonswindow, _FakeAddonClient())
    _stub_choose(monkeypatch, ctx, 1)  # "Install from URL" from the add-row chooser
    win = _make_window(ctx.addonswindow)
    win.onInit()

    win.onClick(ctx.addonswindow.LIST)  # focused item is the add row

    assert store.installed == []
    assert ctx.env.notifications == []


def test_install_addon_error_notifies_and_does_not_install(load_addonswindow, monkeypatch):
    ctx = load_addonswindow(dialog_inputs=['https://bad.example/manifest.json'])
    store = _FakeStore()
    _wire_store(ctx.addonswindow, store)
    _wire_client(ctx.addonswindow, _FakeAddonClient(manifest_error=AddonError('404')))
    _stub_choose(monkeypatch, ctx, 1)  # "Install from URL" from the add-row chooser
    win = _make_window(ctx.addonswindow)
    win.onInit()

    win.onClick(ctx.addonswindow.LIST)

    assert store.installed == []
    assert ctx.env.notifications == [('Rivulet', 'STR30014', 'info', 4000)]


def test_install_manifest_missing_id_notifies_and_does_not_install(load_addonswindow, monkeypatch):
    ctx = load_addonswindow(dialog_inputs=['https://bad.example/manifest.json'])
    store = _FakeStore()
    _wire_store(ctx.addonswindow, store)
    _wire_client(ctx.addonswindow, _FakeAddonClient(manifest_result={'name': 'No Id Here'}))
    _stub_choose(monkeypatch, ctx, 1)  # "Install from URL" from the add-row chooser
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
    _wire_store(ctx.addonswindow, store)
    _wire_client(ctx.addonswindow, _FakeAddonClient(manifest_result=manifest))
    _stub_choose(monkeypatch, ctx, 1)  # "Install from URL" from the add-row chooser
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
    ctx = load_addonswindow()
    _stub_choose(monkeypatch, ctx, 1)
    captured = []
    _stub_confirm(monkeypatch, ctx, True, capture=captured)
    store = _FakeStore(addons=[descriptor])
    _wire_store(ctx.addonswindow, store)
    win = _make_window(ctx.addonswindow)
    win.onInit()
    win.getControl(ctx.addonswindow.LIST).selected_index = 1  # the addon row

    win.onClick(ctx.addonswindow.LIST)

    assert store.removed == [descriptor['transportUrl']]
    assert ctx.env.notifications == [('Rivulet', 'STR30013', 'info', 4000)]
    assert captured == [('STR30011', 'Addon A', 'Yes', 'No')]
    items_after = win.getControl(ctx.addonswindow.LIST).items
    assert len(items_after) == 1
    assert items_after[0].getProperty('position') == 'add'


def test_remove_declined_leaves_addon_untouched(load_addonswindow, monkeypatch):
    descriptor = _descriptor()
    ctx = load_addonswindow()
    _stub_choose(monkeypatch, ctx, 1)
    _stub_confirm(monkeypatch, ctx, False)
    store = _FakeStore(addons=[descriptor])
    _wire_store(ctx.addonswindow, store)
    win = _make_window(ctx.addonswindow)
    win.onInit()
    win.getControl(ctx.addonswindow.LIST).selected_index = 1

    win.onClick(ctx.addonswindow.LIST)

    assert store.removed == []
    assert ctx.env.notifications == []


def test_remove_protected_addon_notifies_and_never_calls_remove(load_addonswindow, monkeypatch):
    descriptor = _descriptor(protected=True)
    ctx = load_addonswindow()
    _stub_choose(monkeypatch, ctx, 1)
    captured = []
    _stub_confirm(monkeypatch, ctx, True, capture=captured)  # scripted answer must never even be consulted
    store = _FakeStore(addons=[descriptor])
    _wire_store(ctx.addonswindow, store)
    win = _make_window(ctx.addonswindow)
    win.onInit()
    win.getControl(ctx.addonswindow.LIST).selected_index = 1

    win.onClick(ctx.addonswindow.LIST)

    assert store.removed == []
    assert captured == []
    assert ctx.env.notifications == [
        ('Rivulet', 'STR30191', 'info', 4000),
    ]


def test_remove_store_raises_valueerror_notifies_protected(load_addonswindow, monkeypatch):
    """Belt-and-suspenders: even if the descriptor's own `flags.protected`
    check were ever bypassed, `Store.remove_addon`'s own `ValueError`
    refusal is still caught and notified the same way."""
    descriptor = _descriptor()
    ctx = load_addonswindow()
    _stub_choose(monkeypatch, ctx, 1)
    _stub_confirm(monkeypatch, ctx, True)
    store = _FakeStore(addons=[descriptor])

    def _raise(transport_url):
        raise ValueError('cannot remove protected addon: %s' % transport_url)

    monkeypatch.setattr(store, 'remove_addon', _raise)
    _wire_store(ctx.addonswindow, store)
    win = _make_window(ctx.addonswindow)
    win.onInit()
    win.getControl(ctx.addonswindow.LIST).selected_index = 1

    win.onClick(ctx.addonswindow.LIST)

    assert ctx.env.notifications == [
        ('Rivulet', 'STR30191', 'info', 4000),
    ]


# ---------------------------------------------------------------------------
# AddonsWindow._guard_mutation() - ConcurrentUpdateError handling
# ---------------------------------------------------------------------------


def test_onclick_toggle_concurrent_update_notifies_and_reloads_instead_of_raising(load_addonswindow, monkeypatch):
    """`set_addon_disabled()` goes through the same CAS `update_addons()`
    path as install/remove - Kodi running `default.py` as concurrent OS
    processes can make it lose the race, and `_guard_mutation()` must
    notify + reload instead of letting `ConcurrentUpdateError` escape the
    click handler."""
    descriptor = _descriptor()
    ctx = load_addonswindow()
    _stub_choose(monkeypatch, ctx, 0)
    store = _FakeStore(addons=[descriptor])

    def _raise(transport_url, disabled):
        raise ConcurrentUpdateError('addons.json changed underneath us')

    monkeypatch.setattr(store, 'set_addon_disabled', _raise)
    _wire_store(ctx.addonswindow, store)
    win = _make_window(ctx.addonswindow)
    win.onInit()
    win.getControl(ctx.addonswindow.LIST).selected_index = 1
    reload_calls = []
    original_reload = win._reload

    def _counting_reload():
        reload_calls.append(True)
        original_reload()

    monkeypatch.setattr(win, '_reload', _counting_reload)

    win.onClick(ctx.addonswindow.LIST)  # must not raise ConcurrentUpdateError

    assert ctx.env.notifications == [('Rivulet', 'STR30032', 'info', 4000)]
    assert reload_calls == [True]


def test_remove_confirmed_concurrent_update_notifies_and_reloads_instead_of_raising(load_addonswindow, monkeypatch):
    """Same guard as the toggle case, for `remove_addon()` - a losing CAS
    race must notify + reload rather than raise out of `_remove()`."""
    descriptor = _descriptor()
    ctx = load_addonswindow()
    _stub_choose(monkeypatch, ctx, 1)
    _stub_confirm(monkeypatch, ctx, True)
    store = _FakeStore(addons=[descriptor])

    def _raise(transport_url):
        raise ConcurrentUpdateError('addons.json changed underneath us')

    monkeypatch.setattr(store, 'remove_addon', _raise)
    _wire_store(ctx.addonswindow, store)
    win = _make_window(ctx.addonswindow)
    win.onInit()
    win.getControl(ctx.addonswindow.LIST).selected_index = 1
    reload_calls = []
    original_reload = win._reload

    def _counting_reload():
        reload_calls.append(True)
        original_reload()

    monkeypatch.setattr(win, '_reload', _counting_reload)

    win.onClick(ctx.addonswindow.LIST)  # must not raise ConcurrentUpdateError

    assert ctx.env.notifications == [('Rivulet', 'STR30032', 'info', 4000)]
    assert reload_calls == [True]


def test_install_concurrent_update_notifies_and_does_not_report_success(load_addonswindow, monkeypatch):
    """`install_addon()` shares the same CAS path, and its guarded call
    sits before the Stremio collection push - a losing race must notify
    30032 and stop, never follow up with the "Addon installed" 30012 that
    would claim a descriptor was persisted."""
    url = 'https://new.example/manifest.json'
    ctx = load_addonswindow(dialog_inputs=[url])
    store = _FakeStore()

    def _raise(transport_url, manifest):
        raise ConcurrentUpdateError('addons.json changed underneath us')

    monkeypatch.setattr(store, 'install_addon', _raise)
    _wire_store(ctx.addonswindow, store)
    _wire_client(ctx.addonswindow, _FakeAddonClient(
        manifest_result={'id': 'org.new', 'name': 'New Addon', 'version': '1.0'}))
    _stub_choose(monkeypatch, ctx, 1)  # "Install from URL" from the add-row chooser
    win = _make_window(ctx.addonswindow)
    win.onInit()

    win.onClick(ctx.addonswindow.LIST)  # must not raise ConcurrentUpdateError

    assert store.installed == []
    assert ctx.env.notifications == [('Rivulet', 'STR30032', 'info', 4000)]


def test_remove_store_raises_valueerror_is_not_caught_by_guard_mutation(load_addonswindow, monkeypatch):
    """`_guard_mutation()` only catches `ConcurrentUpdateError` - the
    protected-addon `ValueError` refusal must still propagate to
    `_remove()`'s own handler unchanged, notifying the refusal string
    WITHOUT the extra `_reload()` a concurrent-update failure would
    trigger."""
    descriptor = _descriptor()
    ctx = load_addonswindow()
    _stub_choose(monkeypatch, ctx, 1)
    _stub_confirm(monkeypatch, ctx, True)
    store = _FakeStore(addons=[descriptor])

    def _raise(transport_url):
        raise ValueError('cannot remove protected addon: %s' % transport_url)

    monkeypatch.setattr(store, 'remove_addon', _raise)
    _wire_store(ctx.addonswindow, store)
    win = _make_window(ctx.addonswindow)
    win.onInit()
    win.getControl(ctx.addonswindow.LIST).selected_index = 1
    reload_calls = []
    original_reload = win._reload

    def _counting_reload():
        reload_calls.append(True)
        original_reload()

    monkeypatch.setattr(win, '_reload', _counting_reload)

    win.onClick(ctx.addonswindow.LIST)  # must not raise ValueError

    assert ctx.env.notifications == [('Rivulet', 'STR30191', 'info', 4000)]
    assert reload_calls == []


# ---------------------------------------------------------------------------
# open_addons()
# ---------------------------------------------------------------------------


def test_open_addons_opens_window_and_runs_modal(load_addonswindow, monkeypatch):
    ctx = load_addonswindow(addon_info={'path': '/addon/path'})
    descriptor = _descriptor()
    _wire_store(ctx.addonswindow, _FakeStore(addons=[descriptor]))
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
    assert captured['init_args'] == ('AddonsWindow.xml', '/addon/path', 'Default', '1080i')
    assert captured['modal_called'] is True


def test_open_addons_window_is_closed_exactly_once_when_domodal_raises(load_addonswindow, monkeypatch):
    ctx = load_addonswindow(addon_info={'path': '/addon/path'})
    _wire_store(ctx.addonswindow, _FakeStore())
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


# ---------------------------------------------------------------------------
# Shared process-wide Store/AddonClient (lib.ui.dependencies)
# ---------------------------------------------------------------------------


def test_reopening_addonswindow_reuses_the_shared_store(load_addonswindow, monkeypatch):
    """`_reload()` re-runs every time the window reopens (`onInit()` fires
    again) - it must always fetch the SAME `get_store()` singleton rather
    than constructing a fresh `Store` in place."""
    ctx = load_addonswindow()

    class _CountingStore:
        instances = 0

        def __init__(self, *args):
            type(self).instances += 1

        def get_addons(self):
            return []

    monkeypatch.setattr(ctx.dependencies, 'Store', _CountingStore)
    win = _make_window(ctx.addonswindow)

    win.onInit()
    first_store = win.store
    win.onInit()  # simulates the window reopening

    assert win.store is first_store
    assert _CountingStore.instances == 1


def test_install_reuses_the_shared_client_across_calls(load_addonswindow, monkeypatch):
    """Two separate `_install()` runs must reuse the SAME `get_client()`
    singleton rather than each constructing its own `AddonClient`."""
    ctx = load_addonswindow(dialog_inputs=['https://a.example/manifest.json', 'https://b.example/manifest.json'])
    _wire_store(ctx.addonswindow, _FakeStore())

    class _CountingClient:
        instances = 0

        def __init__(self):
            type(self).instances += 1

        def manifest(self, url):
            return None  # falsy manifest -> _install() notifies and returns early

    monkeypatch.setattr(ctx.dependencies, 'AddonClient', _CountingClient)
    win = _make_window(ctx.addonswindow)
    win.onInit()

    win._install()
    win._install()

    assert _CountingClient.instances == 1
