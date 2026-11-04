# SPDX-FileCopyrightText: 2026 M0Rf30
# SPDX-License-Identifier: MIT
"""Settings' Home-list editor, using the existing choose/confirm dialogs.

Materialising a tab once on entry makes its ordering concrete. Each edit is
saved before returning to its screen; another plugin interpreter can then
signal the Home behind Settings through home_layout_generation.
"""
import time

from lib import homelayout
from lib.ui import dialogs
from lib.ui.compat import ADDON, L, notify, setting_bool
from lib.ui.dependencies import get_store


def _save(store, layout):
    store.set_home_layout(layout)
    # Nanoseconds as text avoid two edits in one second sharing a generation.
    ADDON.setSetting('home_layout_generation', str(time.time_ns()))


def _label(list_id, catalogs):
    if list_id in homelayout.CONTINUE_IDS:
        suffix = {'continue:movie': 30213, 'continue:series': 30214, 'continue:anime': 30215}
        return L(30241) + (' · ' + L(suffix[list_id]) if list_id in suffix else '')
    layout = homelayout.default_layout()
    layout['tabs']['home'] = [list_id]
    plan = homelayout.resolve(layout, 'home', catalogs)
    if plan:
        return homelayout.heading_text(plan[0][4], L)
    # An uninstalled catalog retains its addon-authored id until reinstall.
    parts = homelayout.parse_catalog_id(list_id)
    return parts[2] if parts else list_id


def _replace_continue():
    """Replacing a list leaves watch progress untouched; confirm that edit."""
    import xbmc

    return dialogs.confirm(L(30241), L(30538), xbmc.getLocalizedString(107), xbmc.getLocalizedString(106))


def _append(store, layout, tab, list_id):
    rows = layout['tabs'][tab]
    if list_id in rows:
        return layout
    replace = list_id in homelayout.CONTINUE_IDS and any(row in homelayout.CONTINUE_IDS for row in rows)
    if replace and not _replace_continue():
        return layout
    changed, ok = homelayout.add(layout, tab, list_id, replace_continue=replace)
    if not ok:
        notify(L(30527))
        return layout
    _save(store, changed)
    return changed


def _add_list(store, layout, tab, catalogs):
    ids = list(homelayout.CONTINUE_IDS) + ['library:movie', 'library:series', 'library']
    ids += [catalog['list_id'] for kind in homelayout.KINDS for catalog in catalogs if catalog['kind'] == kind]
    ids = [list_id for list_id in ids if list_id not in layout['tabs'][tab]]
    by_id = {catalog['list_id']: catalog for catalog in catalogs}
    rows = []
    for list_id in ids:
        catalog = by_id.get(list_id)
        subtitle = ''
        if catalog:
            type_id = {'movie': 30213, 'series': 30214, 'anime-movie': 30535, 'anime-series': 30536,
                       'live': 30552}[catalog['kind']]
            subtitle = '%s · %s' % (L(type_id), catalog['manifest'].get('name') or catalog['manifest'].get('id') or '')
        rows.append((_label(list_id, catalogs), subtitle))
    picked = dialogs.choose(L(30522), rows)
    if 0 <= picked < len(ids):
        return _append(store, layout, tab, ids[picked])
    return layout


def _list_actions(store, layout, tab, list_id, catalogs):
    rows = layout['tabs'][tab]
    index = rows.index(list_id)
    actions = []
    if list_id not in homelayout.CONTINUE_IDS:
        if index > 0 and rows[index - 1] not in homelayout.CONTINUE_IDS:
            actions.append((30523, -1))
        if index + 1 < len(rows):
            actions.append((30524, 1))
    actions.extend(((30525, 'remove'), (30526, 'also')))
    picked = dialogs.choose(_label(list_id, catalogs), [L(label) for label, _ in actions])
    if not 0 <= picked < len(actions):
        return layout
    action = actions[picked][1]
    if action == 'also':
        tabs = [target for target in homelayout.TABS if target != tab]
        chosen = dialogs.choose(L(30526), [L(homelayout.TAB_LABELS[target]) for target in tabs])
        if 0 <= chosen < len(tabs):
            target = tabs[chosen]
            materialized = homelayout.materialize(layout, target, catalogs)
            if materialized != layout:
                _save(store, materialized)
            return _append(store, materialized, target, list_id)
        return layout
    if action == 'remove':
        changed = homelayout.remove(layout, tab, list_id)
    else:
        changed = homelayout.move(layout, tab, list_id, action)
    _save(store, changed)
    return changed


def _edit_tab(store, layout, tab, catalogs):
    materialized = homelayout.materialize(layout, tab, catalogs)
    if materialized != layout:
        _save(store, materialized)
    layout = materialized
    while True:
        ids = layout['tabs'][tab]
        rows = [_label(list_id, catalogs) for list_id in ids] + [L(30522)]
        picked = dialogs.choose(L(homelayout.TAB_LABELS[tab]), rows)
        if picked < 0:
            return layout
        if picked == len(ids):
            layout = _add_list(store, layout, tab, catalogs)
        elif picked < len(ids):
            layout = _list_actions(store, layout, tab, ids[picked], catalogs)


def open_configurator():
    """Choose a tab, arrange its lists, or reset to the pure defaults."""
    import xbmc

    from lib.ui.homewindow import _migrate_home_layout, _migrate_mystuff_setting

    _migrate_mystuff_setting()
    _migrate_home_layout()
    store = get_store()
    catalogs = homelayout.installed_catalogs(store.get_enabled_addons(), setting_bool('home_hide_adult', True))
    layout = store.get_home_layout()
    while True:
        rows = [(L(homelayout.TAB_LABELS[tab]), L(30520) % len(homelayout.resolve(layout, tab, catalogs)))
                for tab in homelayout.TABS]
        rows.append((L(30521), ''))
        picked = dialogs.choose(L(30518), rows)
        if picked < 0:
            return
        if picked == len(homelayout.TABS):
            if dialogs.confirm(L(30521), L(30528), xbmc.getLocalizedString(107), xbmc.getLocalizedString(106)):
                layout = homelayout.reset()
                _save(store, layout)
        elif picked < len(homelayout.TABS):
            layout = _edit_tab(store, layout, homelayout.TABS[picked], catalogs)
