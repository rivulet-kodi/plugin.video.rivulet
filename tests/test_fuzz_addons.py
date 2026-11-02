"""Property tests for ``lib.stremio.addons``: manifests, catalogs and URL
building. A Stremio addon is arbitrary user-installed third-party code, and
the installed-addon list is also what the account sync and ``addons.json``
hand back, so every walk over it must survive any JSON and every URL built
from it must stay one well-formed request path.
"""
import re
from urllib.parse import unquote, urlsplit

from hypothesis import example, given
from hypothesis import strategies as st

from lib.stremio import addons
from tests.fuzz_strategies import (
    addon_lists,
    any_text,
    catalogs,
    hostile_json,
    json_values,
    manifests,
)

_names = st.sampled_from(['search', 'genre', 'skip', 'year', '']) | any_text


@given(addon_lists)
def test_iter_manifests_yields_only_dict_manifests(descriptors):
    for _transport_url, manifest in addons.iter_manifests(descriptors):
        assert isinstance(manifest, dict)


@given(json_values)
def test_iter_manifests_tolerates_any_json_collection(value):
    if isinstance(value, (list, tuple)) or value is None:
        list(addons.iter_manifests(value))


@given(addon_lists, st.none() | _names, st.none() | st.sets(any_text, max_size=3))
@example([{'manifest': {'catalogs': [{'type': 'movie', 'extra': [{'name': 'search'}]}]}}], 'search', {'movie'})
def test_iter_catalogs_yields_only_dict_catalogs_that_match_the_filters(descriptors, required, types):
    for _transport_url, manifest, catalog in addons.iter_catalogs(
        descriptors, extra_required=required, types=types
    ):
        assert isinstance(manifest, dict) and isinstance(catalog, dict)
        assert catalog in manifest['catalogs']
        if required:
            assert addons.catalog_supports_extra(catalog, required)
        if types is not None:
            assert catalog['type'] in types


@given(hostile_json(catalogs))
def test_catalog_extra_helpers_never_raise_and_agree_with_each_other(catalog):
    required = addons.catalog_required_extra_names(catalog)
    assert isinstance(required, set)
    assert all(isinstance(name, str) and name for name in required)
    # `catalog_supports_extra` documents "declared at all (required or not)".
    # The modern `extra` array takes precedence there while the required set
    # unions both forms (a documented difference), so the two only have to
    # agree when the catalog has no `extra` array.
    if not isinstance(catalog, dict) or not isinstance(catalog.get('extra'), list):
        for name in required:
            assert addons.catalog_supports_extra(catalog, name)
    assert addons.catalog_supports_extra(catalog, 'search') in (True, False)
    options = addons.catalog_extra_options(catalog, 'genre')
    assert isinstance(options, list)
    assert all(isinstance(option, str) and option for option in options)
    assert len(options) == len(set(options))


@given(hostile_json(manifests), st.sampled_from(['catalog', 'meta', 'stream', 'subtitles', 'x']),
       st.none() | any_text, st.none() | any_text)
@example({'resources': ['stream'], 'types': ['movie'], 'idPrefixes': [None, 3, 'tt']}, 'stream', 'movie', 'tt1')
@example({'resources': [{'name': 'stream', 'types': 'movie'}]}, 'stream', 'movie', None)
def test_addon_supports_never_raises_and_is_monotonic_in_its_filters(manifest, resource, rtype, rid):
    if not isinstance(manifest, dict):
        return
    loose = addons.addon_supports(manifest, resource, None)
    assert loose in (True, False)
    # Asking for more can only narrow the answer.
    if addons.addon_supports(manifest, resource, rtype, rid):
        assert loose
        assert addons.addon_supports(manifest, resource, rtype)


# --- URL building ------------------------------------------------------------

#: Everything `quote(..., safe="-_.!~*'()")` may leave in a path component.
_COMPONENT = re.compile(r"^(?:[A-Za-z0-9_.\-!~*'()]|%[0-9A-F]{2})*$")

_bases = st.sampled_from([
    'https://addon.example', 'https://addon.example/', 'https://addon.example/manifest.json',
    'http://127.0.0.1:7000/a/b', 'https://addon.example/x/manifest.json/',
])
_components = st.one_of(any_text, st.sampled_from(['tt0111161', 'a/b', 'a b', '?#', '%2F', '..', '']))
_extra_pairs = st.lists(st.tuples(_components, _components), max_size=4)


def _wire(text):
    """`text` as it survives the trip: UTF-8 cannot carry a lone surrogate."""
    return text.encode('utf-8', 'replace').decode('utf-8')


@given(_bases, _components, _components, _components, st.none() | _extra_pairs)
@example('https://a.example', 'catalog', 'movie', 'top', [('search', 'a&b=c'), ('genre', 'x/y')])
@example('https://a.example', 'stream', 'series', 'tt1:1:2', [('k', '\ud800')])
def test_build_resource_url_is_one_well_formed_path(base, resource, rtype, rid, extra):
    url = addons.build_resource_url(base, resource, rtype, rid, extra)
    trimmed = base[:-len(addons.MANIFEST_SUFFIX)] if base.endswith(addons.MANIFEST_SUFFIX) else base.rstrip('/')
    assert url.startswith(trimmed + '/') and url.endswith('.json')
    path = url[len(trimmed) + 1:-len('.json')]
    segments = path.split('/')
    # resource/type/id, plus the extra segment only when there is one:
    # a '/' or '?' inside a component never splits the path or starts a query.
    assert len(segments) == (4 if extra else 3)
    assert all(_COMPONENT.match(segment.replace('=', '').replace('&', '')) for segment in segments)
    assert [unquote(segment) for segment in segments[:3]] == [_wire(resource), _wire(rtype), _wire(rid)]
    assert urlsplit(url).query == '' and urlsplit(url).fragment == ''
    if extra:
        decoded = [tuple(unquote(part) for part in pair.split('=')) for pair in segments[3].split('&')]
        assert decoded == [(_wire(name), _wire(value)) for name, value in extra]


@given(_extra_pairs)
def test_encode_extra_matches_the_two_argument_shorthand(pairs):
    assert addons.encode_extra(pairs) == '&'.join(addons.encode_extra(name, value) for name, value in pairs)
    for name, value in pairs[:1]:
        assert addons.encode_extra(name, value) == addons.encode_extra([(name, value)])


@given(json_values)
def test_encode_extra_stringifies_any_json_value(value):
    encoded = addons.encode_extra([('k', value)])
    assert _COMPONENT.match(encoded.replace('=', '', 1))


# --- previews and error metas ------------------------------------------------


@given(hostile_json(st.fixed_dictionaries({}, optional={'id': json_values})) | json_values)
def test_is_error_meta_never_raises(meta):
    assert addons.is_error_meta(meta) in (True, False)
    category = addons.error_meta_category(meta)
    assert category.startswith('addon reported error')


@given(st.lists(hostile_json(st.fixed_dictionaries({}, optional={'type': json_values, 'id': json_values})),
                max_size=5),
       st.none() | any_text)
def test_default_meta_types_never_mutates_and_types_every_dict_preview(metas, ctype):
    snapshot = repr(metas)
    result = addons.default_meta_types(metas, ctype)
    assert repr(metas) == snapshot, 'default_meta_types mutated its input'
    if isinstance(ctype, str) and ctype:
        assert len(result) == len(metas)
        for meta in result:
            if isinstance(meta, dict):
                assert meta.get('type')
    else:
        assert result is metas
