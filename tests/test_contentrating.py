"""Tests for lib.stremio.contentrating (pure heuristic adult-content
detection - no Kodi imports, no network).

The Stremio protocol has no official "this is adult content" flag, so
these tests exercise the heuristic signals `is_adult_meta()`/
`is_adult_catalog()` fall back to, in the documented priority order,
and the word-boundary matching that keeps a title merely containing an
adult marker word as a substring (e.g. "Analyze That") from tripping a
false positive.
"""
from lib.stremio.contentrating import (
    filter_metas,
    is_adult_catalog,
    is_adult_manifest,
    is_adult_meta,
)

# ---------------------------------------------------------------------------
# is_adult_meta() - each signal independently
# ---------------------------------------------------------------------------


def test_explicit_adult_true_flags_meta_with_no_other_signal():
    assert is_adult_meta({'id': 'tt1', 'name': 'Ordinary Title', 'adult': True}) is True


def test_explicit_adult_false_is_not_adult():
    assert is_adult_meta({'id': 'tt1', 'name': 'Ordinary Title', 'adult': False}) is False


def test_explicit_adult_string_true_is_coerced():
    assert is_adult_meta({'id': 'tt1', 'adult': 'true'}) is True


def test_explicit_adult_string_false_is_coerced():
    assert is_adult_meta({'id': 'tt1', 'adult': 'false'}) is False


def test_genres_list_containing_marker_flags_meta():
    assert is_adult_meta({'id': 'tt1', 'name': 'Late Night Show', 'genres': ['Erotic']}) is True


def test_genre_singular_string_containing_marker_flags_meta():
    assert is_adult_meta({'id': 'tt1', 'genre': 'Hentai'}) is True


def test_ordinary_genre_does_not_flag_meta():
    assert is_adult_meta({'id': 'tt1', 'name': 'A Thriller', 'genres': ['Thriller']}) is False


def test_erotic_thriller_genre_flags_but_plain_thriller_does_not():
    """The exact genre distinction the marker regex exists for: "Erotic
    Thriller" carries the marker word, "Thriller" alone does not."""
    assert is_adult_meta({'id': 'tt1', 'genres': ['Erotic Thriller']}) is True
    assert is_adult_meta({'id': 'tt2', 'genres': ['Thriller']}) is False


def test_adult_type_flags_meta_with_no_other_signal():
    assert is_adult_meta({'id': 'tt1', 'name': 'Untitled', 'type': 'xxx'}) is True


def test_ordinary_type_does_not_flag_meta():
    assert is_adult_meta({'id': 'tt1', 'name': 'Untitled', 'type': 'movie'}) is False


def test_meta_id_containing_marker_flags_meta():
    assert is_adult_meta({'id': 'xxx-tt1', 'name': 'Untitled'}) is True


def test_meta_name_containing_marker_flags_meta():
    assert is_adult_meta({'id': 'tt1', 'name': 'Hentai Collection'}) is True


# ---------------------------------------------------------------------------
# Word-boundary safety - adversarial non-adult strings containing a marker
# as a bare substring, not a whole word.
# ---------------------------------------------------------------------------


def test_analyze_that_is_not_flagged_by_the_anal_marker():
    """'anal' is a marker word, but "Analyze" continues past the boundary -
    a substring match would wrongly flag this real movie title."""
    assert is_adult_meta({'id': 'tt1', 'name': 'Analyze That', 'genres': ['Comedy']}) is False


def test_milford_sound_is_not_flagged_by_the_milf_marker():
    """'milf' is a marker word, but "Milford" continues past the boundary -
    a substring match would wrongly flag this real place name."""
    assert is_adult_meta({'id': 'tt1', 'name': 'Milford Sound: New Zealand Fjords',
                           'genres': ['Documentary']}) is False


def test_adversarial_substring_in_catalog_name_is_not_flagged():
    assert is_adult_catalog({'id': 'analog-clocks', 'name': 'Analog Clocks', 'type': 'movie'}) is False


# ---------------------------------------------------------------------------
# Explicit `adult` field wins over a coincidental genre match
# ---------------------------------------------------------------------------


def test_explicit_adult_false_overrides_a_coincidental_adult_genre():
    meta = {'id': 'tt1', 'name': 'Erotic Thriller Anthology', 'genres': ['Erotic'], 'adult': False}
    assert is_adult_meta(meta) is False


def test_explicit_adult_true_overrides_an_ordinary_genre():
    meta = {'id': 'tt1', 'genres': ['Comedy'], 'adult': True}
    assert is_adult_meta(meta) is True


# ---------------------------------------------------------------------------
# Defensive input handling
# ---------------------------------------------------------------------------


def test_non_dict_meta_is_not_adult():
    assert is_adult_meta(None) is False
    assert is_adult_meta('not a dict') is False


def test_meta_with_no_signals_at_all_is_not_adult():
    assert is_adult_meta({'id': 'tt1', 'name': 'Plain Title'}) is False


def test_unparseable_adult_value_falls_back_to_other_signals():
    """An `adult` field that is neither a bool nor a recognisable string/
    number can't be trusted either way - fall back to genre/type."""
    meta = {'id': 'tt1', 'adult': {'weird': 'object'}, 'genres': ['XXX']}
    assert is_adult_meta(meta) is True


# ---------------------------------------------------------------------------
# is_adult_catalog()
# ---------------------------------------------------------------------------


def test_catalog_id_containing_marker_is_adult():
    assert is_adult_catalog({'id': 'xxx-movies', 'name': 'Movies', 'type': 'movie'}) is True


def test_catalog_name_containing_marker_is_adult():
    assert is_adult_catalog({'id': 'cat1', 'name': 'Hentai Collection', 'type': 'movie'}) is True


def test_catalog_adult_type_is_adult():
    assert is_adult_catalog({'id': 'cat1', 'name': 'Popular', 'type': 'xxx'}) is True


def test_ordinary_catalog_is_not_adult():
    assert is_adult_catalog({'id': 'top', 'name': 'Popular Movies', 'type': 'movie'}) is False


def test_catalog_flagged_by_owning_manifest_types():
    """An addon whose own manifest['types'] names an adult type marks
    every catalog it serves as adult, even an innocuous-looking one."""
    catalog = {'id': 'top', 'name': 'Popular', 'type': 'movie'}
    manifest = {'types': ['xxx']}
    assert is_adult_catalog(catalog, manifest=manifest) is True


def test_catalog_not_flagged_when_manifest_types_are_ordinary():
    catalog = {'id': 'top', 'name': 'Popular', 'type': 'movie'}
    manifest = {'types': ['movie', 'series']}
    assert is_adult_catalog(catalog, manifest=manifest) is False


def test_non_dict_catalog_is_not_adult():
    assert is_adult_catalog(None) is False


# ---------------------------------------------------------------------------
# filter_metas()
# ---------------------------------------------------------------------------


def test_filter_metas_preserves_order_and_drops_only_adult_entries():
    metas = [
        {'id': 'tt1', 'name': 'A'},
        {'id': 'tt2', 'name': 'B', 'adult': True},
        {'id': 'tt3', 'name': 'C'},
        {'id': 'tt4', 'name': 'D', 'genres': ['Hentai']},
        {'id': 'tt5', 'name': 'E'},
    ]
    result = filter_metas(metas)
    assert [meta['id'] for meta in result] == ['tt1', 'tt3', 'tt5']


def test_filter_metas_returns_a_new_list_without_mutating_the_input():
    metas = [{'id': 'tt1'}, {'id': 'tt2', 'adult': True}]
    original = list(metas)
    result = filter_metas(metas)
    assert result is not metas
    assert metas == original


def test_filter_metas_of_empty_list_is_empty_list():
    assert filter_metas([]) == []


# ---------------------------------------------------------------------------
# is_adult_manifest()
# ---------------------------------------------------------------------------


def test_manifest_explicit_behavior_hints_adult_true_flags_manifest():
    manifest = {'id': 'org.plain', 'name': 'Plain Addon', 'behaviorHints': {'adult': True}}
    assert is_adult_manifest(manifest) is True


def test_manifest_explicit_behavior_hints_adult_false_overrides_adult_type():
    manifest = {'id': 'org.plain', 'name': 'Plain Addon', 'types': ['xxx'], 'behaviorHints': {'adult': False}}
    assert is_adult_manifest(manifest) is False


def test_manifest_types_naming_an_adult_type_flags_manifest():
    manifest = {'id': 'org.plain', 'name': 'Untitled Provider', 'types': ['movie', 'xxx']}
    assert is_adult_manifest(manifest) is True


def test_manifest_types_as_a_bare_string_is_accepted():
    manifest = {'id': 'org.plain', 'name': 'Untitled Provider', 'types': 'hentai'}
    assert is_adult_manifest(manifest) is True


def test_manifest_ordinary_types_do_not_flag_manifest():
    manifest = {'id': 'org.plain', 'name': 'Plain Addon', 'types': ['movie', 'series']}
    assert is_adult_manifest(manifest) is False


def test_manifest_name_containing_marker_flags_manifest():
    manifest = {'id': 'org.plain', 'name': 'MindGeek Adult Video HD'}
    assert is_adult_manifest(manifest) is True


def test_manifest_description_containing_marker_flags_manifest():
    manifest = {'id': 'org.plain', 'name': 'Untitled', 'description': 'The best hentai streams around'}
    assert is_adult_manifest(manifest) is True


def test_manifest_catalog_looking_adult_flags_manifest():
    manifest = {
        'id': 'org.plain', 'name': 'Mixed Addon',
        'catalogs': [{'id': 'top', 'name': 'Popular', 'type': 'movie'},
                     {'id': 'xxx-top', 'name': 'Popular', 'type': 'movie'}],
    }
    assert is_adult_manifest(manifest) is True


def test_manifest_ordinary_catalogs_do_not_flag_manifest():
    manifest = {
        'id': 'org.plain', 'name': 'Ordinary Addon',
        'catalogs': [{'id': 'top', 'name': 'Popular', 'type': 'movie'}],
    }
    assert is_adult_manifest(manifest) is False


def test_manifest_with_no_signals_at_all_is_not_adult():
    manifest = {'id': 'org.plain', 'name': 'Plain Addon', 'description': 'A perfectly normal addon'}
    assert is_adult_manifest(manifest) is False


def test_manifest_analog_clocks_name_is_not_flagged_by_the_anal_marker():
    """Same word-boundary false-positive guard `is_adult_meta()`/
    `is_adult_catalog()` already prove, applied at the manifest level -
    a real addon called "Analog Clocks" must not trip on "anal"."""
    manifest = {'id': 'org.analog', 'name': 'Analog Clocks', 'description': 'Wall clock wallpapers'}
    assert is_adult_manifest(manifest) is False


def test_manifest_milford_sound_name_is_not_flagged_by_the_milf_marker():
    manifest = {'id': 'org.milford', 'name': 'Milford Sound Documentaries'}
    assert is_adult_manifest(manifest) is False


def test_manifest_non_dict_behavior_hints_is_ignored():
    manifest = {'id': 'org.plain', 'name': 'Plain Addon', 'behaviorHints': 'not-a-dict'}
    assert is_adult_manifest(manifest) is False


def test_non_dict_manifest_is_not_adult():
    assert is_adult_manifest(None) is False
    assert is_adult_manifest('not a dict') is False
