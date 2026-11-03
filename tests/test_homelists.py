"""Script the existing choose dialogs through the list configurator."""
import copy

import pytest

from lib import homelayout as h
from tests.test_home import load_homewindow as load_homewindow
from tests.test_home_tabs import addons


class Store:
    def __init__(self, layout=None):
        self.layout = layout or h.default_layout()
        self.saved = []

    def get_home_layout(self):
        return copy.deepcopy(self.layout)

    def set_home_layout(self, layout):
        self.layout = copy.deepcopy(layout)
        self.saved.append(copy.deepcopy(layout))

    def get_enabled_addons(self):
        return addons()


def setup(ctx, monkeypatch, choices, layout=None, confirms=()):
    from lib.ui import homewindow
    mod = ctx.homelists
    store = Store(layout)
    screens = []
    answers = iter(choices)
    confirmations = iter(confirms)

    def choose(heading, rows):
        screens.append((heading, rows))
        return next(answers)

    monkeypatch.setattr(mod, 'get_store', lambda: store)
    monkeypatch.setattr(mod.dialogs, 'choose', choose)
    monkeypatch.setattr(mod.dialogs, 'confirm', lambda *args: next(confirmations))
    monkeypatch.setattr(homewindow, '_migrate_mystuff_setting', lambda: None)
    monkeypatch.setattr(homewindow, '_migrate_home_layout', lambda: None)
    return mod, store, screens


def test_materialize_then_move_down_and_up_persists_and_bumps_generation(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    # Home, first poster, down, second poster, up, Back twice.
    mod, store, screens = setup(ctx, monkeypatch, [0, 1, 0, 2, 0, -1, -1])
    mod.open_configurator()
    assert len(store.saved) == 3
    assert store.saved[0]['tabs']['home'] == ['continue', 'cat:https://addon/manifest.json|movie|movies',
                                            'cat:https://addon/manifest.json|series|shows']
    assert store.saved[1]['tabs']['home'][1:] == store.saved[0]['tabs']['home'][1:][::-1]
    assert store.layout == store.saved[0]
    assert ctx.compat.ADDON.getSetting('home_layout_generation')
    assert screens[0][0] == 'STR30518'
    assert screens[0][1][0] == ('STR30495', 'Lists: 3')
    assert screens[2][1] == ['STR30524', 'STR30525', 'STR30526']


def test_remove_hides_list_and_continue_has_no_move_actions(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    mod, store, screens = setup(ctx, monkeypatch, [0, 0, 0, -1, -1])
    mod.open_configurator()
    assert 'continue' not in store.layout['tabs']['home']
    assert screens[2][1] == ['STR30525', 'STR30526']


def test_also_show_materializes_target_and_appends_without_removing_source(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    mod, store, _ = setup(ctx, monkeypatch, [0, 1, 2, 0, -1, -1])
    mod.open_configurator()
    cid = 'cat:https://addon/manifest.json|movie|movies'
    assert cid in store.layout['tabs']['home']
    assert store.layout['tabs']['mystuff'] == ['library:movie', 'library:series', cid]


def test_add_library_and_continue_replacement_rule(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    mod, store, _ = setup(ctx, monkeypatch, [0, 3, 3, -1, -1], confirms=[False, True])
    mod.open_configurator()
    assert store.layout['tabs']['home'][-1] == 'library:movie'
    before = len(store.saved)
    # No leaves both the saved layout and generation alone.
    generation = ctx.compat.ADDON.getSetting('home_layout_generation')
    assert mod._append(store, store.layout, 'home', 'continue:movie') == store.layout
    assert len(store.saved) == before
    assert ctx.compat.ADDON.getSetting('home_layout_generation') == generation
    changed = mod._append(store, store.layout, 'home', 'continue:movie')
    assert changed['tabs']['home'][0] == 'continue:movie'
    assert 'continue' not in changed['tabs']['home']
    assert mod._append(store, changed, 'home', 'continue:movie') == changed


def test_full_tab_refuses_new_poster_with_notification(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    layout = h.default_layout()
    layout['tabs']['home'] = ['cat:missing|movie|%d' % n for n in range(12)]
    mod, store, _ = setup(ctx, monkeypatch, [], layout)
    notified = []
    monkeypatch.setattr(mod, 'notify', notified.append)
    assert mod._append(store, layout, 'home', 'library') == layout
    assert notified == ['STR30527']
    assert not store.saved


def test_add_catalogs_are_grouped_and_exclude_adult_and_unsupported_types(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    mod, store, screens = setup(ctx, monkeypatch, [-1])
    installed = addons()
    installed[0]['manifest']['catalogs'] += [{'id': 'adult', 'type': 'movie', 'adult': True}]
    cats = h.installed_catalogs(installed)
    layout = h.default_layout()
    layout['tabs']['home'] = []
    assert mod._add_list(store, layout, 'home', cats) == layout
    rows = screens[0][1]
    assert len(rows) == 11
    assert [label for label, _ in rows[:4]] == ['STR30241', 'STR30241 · STR30213',
                                               'STR30241 · STR30214', 'STR30241 · STR30215']
    assert [subtitle for _, subtitle in rows[7:]] == ['STR30213 · Addon', 'STR30214 · Addon',
                                                    'STR30535 · Addon', 'STR30536 · Addon']
    assert not any('adult' in label.lower() or 'channel' in label.lower() for label, _ in rows)


@pytest.mark.parametrize('answers', [[-1], [0, -1, -1], [0, 1, -1, -1, -1],
                                    [0, 3, -1, -1, -1], [0, 1, 2, -1, -1, -1]])
def test_back_at_every_dialog_level(load_homewindow, monkeypatch, answers):
    ctx = load_homewindow()
    mod, store, _ = setup(ctx, monkeypatch, answers)
    mod.open_configurator()
    assert len(store.saved) <= 1  # Only materialisation on entering Home.


@pytest.mark.parametrize('confirm', [True, False])
def test_reset_uses_pure_defaults_and_existing_confirmation(load_homewindow, monkeypatch, confirm):
    ctx = load_homewindow(settings={'home_show_mystuff': 'false'})
    layout = h.default_layout()
    layout['tabs']['home'] = []
    mod, store, _ = setup(ctx, monkeypatch, [5, -1], layout, [confirm])
    mod.open_configurator()
    assert store.layout == (h.default_layout() if confirm else layout)
    assert len(store.saved) == int(confirm)


def test_missing_catalog_label_survives_reinstall_and_router_is_lazy(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    mod, _store, _ = setup(ctx, monkeypatch, [])
    assert mod._label('cat:missing|movie|saved', []) == 'saved'
    opened = []
    monkeypatch.setattr(mod, 'open_configurator', lambda: opened.append(True))
    ctx.router._home_lists()
    assert opened == [True]


def test_continue_replacement_confirms_the_localized_prompt_and_persists(load_homewindow, monkeypatch):
    ctx = load_homewindow(localized={30538: 'Replace this tab’s Continue list?'})
    mod, store, _ = setup(ctx, monkeypatch, [0, 3, 0, -1, -1])
    prompted = []
    monkeypatch.setattr(mod.dialogs, 'confirm', lambda *args: prompted.append(args) or True)
    mod.open_configurator()
    assert prompted[0][:2] == ('STR30241', 'Replace this tab’s Continue list?')
    assert store.layout['tabs']['home'][0] == 'continue:movie'
    assert sum(value in h.CONTINUE_IDS for value in store.layout['tabs']['home']) == 1
    assert len(store.saved) == 2
    assert ctx.compat.ADDON.getSetting('home_layout_generation')


def test_continue_replacement_back_keeps_the_saved_layout(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    mod, store, _ = setup(ctx, monkeypatch, [0, 3, -1, -1, -1])
    confirmations = []
    monkeypatch.setattr(mod.dialogs, 'confirm', lambda *args: confirmations.append(args) or False)
    mod.open_configurator()
    assert store.layout['tabs']['home'][0] == 'continue'
    assert len(store.saved) == 1  # Materialisation only, before opening Add.
    assert not confirmations
