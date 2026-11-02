"""Property tests for ``lib.homelayout``: the saved layout is a hand-editable
JSON file and the catalogs it resolves against are third-party manifests, so
``normalize`` must turn any input into a valid layout (or refuse it with
``LayoutError``, never anything else) and every editing step must keep it
valid.
"""
import copy

from hypothesis import example, given
from hypothesis import strategies as st

from lib import homelayout
from tests.fuzz_strategies import addon_lists, home_layouts, json_objects, json_values, list_ids

_tabs = st.sampled_from(homelayout.TABS)


def _normalized(layout):
    try:
        return homelayout.normalize(layout)
    except homelayout.LayoutError:
        return None


def assert_valid(layout):
    assert set(layout) == {'version', 'tabs'} and layout['version'] == 1
    assert list(layout['tabs']) == list(homelayout.TABS)
    for rows in layout['tabs'].values():
        assert isinstance(rows, list)
        assert all(homelayout.valid_id(value) for value in rows)
        assert len(rows) == len(set(rows)), 'duplicate row'
        continues = [value for value in rows if value in homelayout.CONTINUE_IDS]
        # At most one Continue row, and it leads the tab.
        assert len(continues) <= 1
        assert rows[:len(continues)] == continues


@given(home_layouts)
@example({'version': 1, 'tabs': {'home': 'continue'}})
@example({'version': True, 'tabs': {'home': []}})
@example({'version': 1, 'tabs': {'home': [['unhashable']]}})
@example({'version': 1, 'tabs': {'home': [{'a': 1}, 'continue', 'continue:movie']}})
def test_normalize_yields_a_valid_layout_or_layout_error(layout):
    before = repr(layout)
    result = _normalized(layout)
    assert repr(layout) == before, 'normalize mutated its input'
    if result is None:
        return
    assert_valid(result)
    # Idempotent, and the result shares nothing with the input.
    assert homelayout.normalize(result) == result
    result['tabs']['home'].append('x')
    assert repr(layout) == before


@given(home_layouts, _tabs, list_ids, st.booleans())
def test_add_remove_move_keep_the_layout_valid(layout, tab, list_id, replace_continue):
    base = _normalized(layout)
    if base is None:
        return
    added, changed = homelayout.add(base, tab, list_id, replace_continue)
    assert_valid(added)
    assert changed in (True, False)
    if changed:
        assert list_id in added['tabs'][tab]
    removed = homelayout.remove(added, tab, list_id)
    assert_valid(removed)
    assert list_id not in removed['tabs'][tab]
    for direction in (-1, 1):
        moved = homelayout.move(added, tab, list_id, direction)
        assert_valid(moved)
        assert sorted(moved['tabs'][tab]) == sorted(added['tabs'][tab])


@given(st.lists(list_ids, max_size=20), _tabs)
def test_add_never_exceeds_the_poster_row_cap(ids, tab):
    layout = homelayout.default_layout()
    layout['tabs'][tab] = []
    for list_id in ids:
        layout, _changed = homelayout.add(layout, tab, list_id)
    rows = layout['tabs'][tab]
    assert sum(value not in homelayout.CONTINUE_IDS for value in rows) <= homelayout.MAX_POSTER_ROWS


@given(json_values)
def test_valid_id_and_parse_catalog_id_never_raise(value):
    parts = homelayout.parse_catalog_id(value)
    assert homelayout.valid_id(value) in (True, False)
    if parts is not None:
        assert len(parts) in (3, 4) and all(parts)


@given(json_objects)
def test_migrate_always_yields_a_valid_layout(toggles):
    assert_valid(homelayout.migrate(toggles))


@given(addon_lists, st.booleans())
def test_installed_catalogs_never_raises_and_yields_parseable_ids(addons, hide_adult):
    entries = homelayout.installed_catalogs(addons, hide_adult=hide_adult)
    for entry in entries:
        assert homelayout.parse_catalog_id(entry['list_id']) is not None
        assert homelayout.valid_id(entry['list_id'])
        assert entry['kind'] in homelayout.KINDS
        assert isinstance(entry['manifest'], dict) and isinstance(entry['catalog'], dict)
        assert isinstance(entry['addon_id'], str)


@given(addon_lists, home_layouts, _tabs, st.booleans())
def test_resolve_and_materialize_never_raise_on_hostile_manifests(addons, layout, tab, hide_adult):
    base = _normalized(layout)
    if base is None:
        return
    catalogs = homelayout.installed_catalogs(addons, hide_adult=hide_adult)
    plan = homelayout.resolve(base, tab, catalogs, hide_adult)
    posters = [entry for entry in plan if entry[1] != 'continue']
    assert len(posters) <= homelayout.MAX_POSTER_ROWS
    assert len({entry[0] for entry in plan}) == len(plan), 'a row is planned twice'
    for list_id, kind, _catalog, _variant, heading in plan:
        assert kind in ('continue', 'library', 'catalog')
        assert heading and all(isinstance(part, (int, str)) for part in heading)
        assert homelayout.valid_id(list_id)
        homelayout.heading_text(heading, str)
    assert_valid(homelayout.materialize(base, tab, catalogs, hide_adult))


@given(json_objects, st.sampled_from(['', 'movie', 'series', 'anime']))
def test_row_matchers_never_raise(meta, variant):
    for matcher in (homelayout.matches_continue, homelayout.matches_library, homelayout.matches_catalog):
        assert matcher(meta, variant) in (True, False)
    assert copy.deepcopy(meta) is not None
