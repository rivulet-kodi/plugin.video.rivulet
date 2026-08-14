"""Tests for lib.stremio.metalinks (Stremio Meta Link parsing).

Pure Python (no Kodi imports), same rationale as tests/test_lzstring.py /
tests/test_advancedsettings.py: `lib.stremio.metalinks` never touches
`xbmc*`, so these tests run under plain python3/pytest with no
`kodistubs` fixture and no network access.
"""
from lib.stremio.metalinks import (
    RESERVED_LINK_CATEGORIES,
    iter_link_groups,
    parse_meta_link,
)

# --- real, verified Cinemeta payload shapes (tt0068646, The Godfather) -----

CAST_LINK_URL = 'stremio:///search?search=Marlon%20Brando'
GENRE_LINK_URL = (
    'stremio:///discover/https%3A%2F%2Fv3-cinemeta.strem.io%2Fmanifest.json'
    '/movie/top?genre=Crime'
)
IMDB_LINK_URL = 'https://imdb.com/title/tt0068646'
SHARE_LINK_URL = 'https://www.strem.io/s/movie/the-godfather-0068646'

# The full real `links` array shape for tt0068646: imdb, share, Genres x2,
# Cast x3, Writers x2, Directors x1 - deliberately interleaved rather than
# pre-grouped, matching how Cinemeta actually orders them.
GODFATHER_LINKS = [
    {'name': 'tt0068646', 'category': 'imdb', 'url': IMDB_LINK_URL},
    {'name': 'Share', 'category': 'share', 'url': SHARE_LINK_URL},
    {
        'name': 'Crime',
        'category': 'Genres',
        'url': (
            'stremio:///discover/https%3A%2F%2Fv3-cinemeta.strem.io%2Fmanifest.json'
            '/movie/top?genre=Crime'
        ),
    },
    {'name': 'Marlon Brando', 'category': 'Cast', 'url': CAST_LINK_URL},
    {
        'name': 'Drama',
        'category': 'Genres',
        'url': (
            'stremio:///discover/https%3A%2F%2Fv3-cinemeta.strem.io%2Fmanifest.json'
            '/movie/top?genre=Drama'
        ),
    },
    {'name': 'Al Pacino', 'category': 'Cast', 'url': 'stremio:///search?search=Al%20Pacino'},
    {
        'name': 'Mario Puzo',
        'category': 'Writers',
        'url': 'stremio:///search?search=Mario%20Puzo',
    },
    {'name': 'James Caan', 'category': 'Cast', 'url': 'stremio:///search?search=James%20Caan'},
    {
        'name': 'Francis Ford Coppola',
        'category': 'Writers',
        'url': 'stremio:///search?search=Francis%20Ford%20Coppola',
    },
    {
        'name': 'Francis Ford Coppola',
        'category': 'Directors',
        'url': 'stremio:///search?search=Francis%20Ford%20Coppola',
    },
]


# ---------------------------------------------------------------------------
# parse_meta_link(): search links
# ---------------------------------------------------------------------------


def test_parse_search_link_decodes_percent_encoded_space_in_query():
    assert parse_meta_link(CAST_LINK_URL) == {'kind': 'search', 'query': 'Marlon Brando'}


def test_parse_search_link_with_empty_query_is_none():
    assert parse_meta_link('stremio:///search?search=') is None


def test_parse_search_link_with_missing_query_is_none():
    assert parse_meta_link('stremio:///search') is None


# ---------------------------------------------------------------------------
# parse_meta_link(): discover links
# ---------------------------------------------------------------------------


def test_parse_discover_link_matches_real_cinemeta_genre_link():
    assert parse_meta_link(GENRE_LINK_URL) == {
        'kind': 'discover',
        'transport_url': 'https://v3-cinemeta.strem.io/manifest.json',
        'type': 'movie',
        'catalog_id': 'top',
        'extra': [('genre', 'Crime')],
    }


def test_parse_discover_link_with_no_query_has_empty_extra():
    url = 'stremio:///discover/https%3A%2F%2Fv3-cinemeta.strem.io%2Fmanifest.json/movie/top'
    parsed = parse_meta_link(url)
    assert parsed['extra'] == []


def test_parse_discover_link_missing_catalog_id_is_none():
    url = 'stremio:///discover/https%3A%2F%2Fv3-cinemeta.strem.io%2Fmanifest.json/movie'
    assert parse_meta_link(url) is None


def test_parse_discover_link_encoded_slash_inside_transport_url_does_not_corrupt_split():
    """The decode-order trap: the transportUrl segment is itself a full URL
    with several real path segments, all folded into one percent-encoded
    path component. Splitting the raw (still-encoded) path on '/' first
    keeps it as a single segment; decoding the whole path before splitting
    would instead see the decoded '%2F's as real separators and shred the
    transportUrl into extra bogus path segments, misaligning type/catalog_id.
    """
    transport = 'https://example.com/addons/community/v2/manifest.json'
    from urllib.parse import quote

    url = 'stremio:///discover/%s/series/newest?genre=Horror' % quote(transport, safe='')
    parsed = parse_meta_link(url)
    assert parsed == {
        'kind': 'discover',
        'transport_url': transport,
        'type': 'series',
        'catalog_id': 'newest',
        'extra': [('genre', 'Horror')],
    }


# ---------------------------------------------------------------------------
# parse_meta_link(): detail links
# ---------------------------------------------------------------------------


def test_parse_detail_link_without_video_id():
    assert parse_meta_link('stremio:///detail/movie/tt0068646') == {
        'kind': 'detail',
        'type': 'movie',
        'id': 'tt0068646',
        'video_id': None,
    }


def test_parse_detail_link_with_video_id():
    assert parse_meta_link('stremio:///detail/series/tt1234567/tt1234567:1:1') == {
        'kind': 'detail',
        'type': 'series',
        'id': 'tt1234567',
        'video_id': 'tt1234567:1:1',
    }


def test_parse_detail_link_missing_id_is_none():
    assert parse_meta_link('stremio:///detail/movie') is None


# ---------------------------------------------------------------------------
# parse_meta_link(): rejected / malformed input, never raises
# ---------------------------------------------------------------------------


def test_parse_meta_link_rejects_plain_imdb_and_share_urls():
    assert parse_meta_link(IMDB_LINK_URL) is None
    assert parse_meta_link(SHARE_LINK_URL) is None


def test_parse_meta_link_rejects_unknown_stremio_kind():
    assert parse_meta_link('stremio:///bogus/thing') is None


def test_parse_meta_link_rejects_non_empty_authority():
    # Two slashes, not three: 'foo' parses as a netloc, not the documented
    # empty-authority shape.
    assert parse_meta_link('stremio://foo/search?search=x') is None


def test_parse_meta_link_none_and_empty_and_non_string_inputs():
    assert parse_meta_link(None) is None
    assert parse_meta_link('') is None
    assert parse_meta_link(123) is None
    assert parse_meta_link(['stremio:///search?search=x']) is None


def test_parse_meta_link_bare_scheme_with_no_path_is_none():
    assert parse_meta_link('stremio:///') is None


# ---------------------------------------------------------------------------
# RESERVED_LINK_CATEGORIES
# ---------------------------------------------------------------------------


def test_reserved_link_categories_matched_casefolded():
    assert 'imdb' in RESERVED_LINK_CATEGORIES
    assert 'share' in RESERVED_LINK_CATEGORIES
    assert 'similar' in RESERVED_LINK_CATEGORIES
    assert 'IMDb'.casefold() in RESERVED_LINK_CATEGORIES


# ---------------------------------------------------------------------------
# iter_link_groups()
# ---------------------------------------------------------------------------


def test_iter_link_groups_excludes_imdb_and_share_links():
    meta = {'links': [
        {'name': 'tt0068646', 'category': 'imdb', 'url': IMDB_LINK_URL},
        {'name': 'Share', 'category': 'share', 'url': SHARE_LINK_URL},
    ]}
    assert iter_link_groups(meta) == []


def test_iter_link_groups_full_godfather_payload_groups_in_first_seen_order():
    groups = iter_link_groups({'links': GODFATHER_LINKS})
    categories = [category for category, _members in groups]
    assert categories == ['Genres', 'Cast', 'Writers', 'Directors']

    by_category = dict(groups)
    assert len(by_category['Genres']) == 2
    assert len(by_category['Cast']) == 3
    assert len(by_category['Writers']) == 2
    assert len(by_category['Directors']) == 1

    assert by_category['Genres'][0][0] == 'Crime'
    assert by_category['Genres'][1][0] == 'Drama'
    assert by_category['Cast'][0] == ('Marlon Brando', {
        'kind': 'search', 'query': 'Marlon Brando',
    })


def test_iter_link_groups_merges_links_sharing_one_category_into_one_group():
    meta = {'links': [
        {'name': 'Crime', 'category': 'Genres', 'url': GENRE_LINK_URL},
        {'name': 'Marlon Brando', 'category': 'Cast', 'url': CAST_LINK_URL},
        {'name': 'Drama', 'category': 'Genres',
         'url': 'stremio:///discover/https%3A%2F%2Fv3-cinemeta.strem.io%2Fmanifest.json'
                '/movie/top?genre=Drama'},
    ]}
    groups = iter_link_groups(meta)
    assert [c for c, _ in groups] == ['Genres', 'Cast']
    assert [name for name, _link in dict(groups)['Genres']] == ['Crime', 'Drama']


def test_iter_link_groups_skips_link_with_empty_name():
    meta = {'links': [{'name': '', 'category': 'Cast', 'url': CAST_LINK_URL}]}
    assert iter_link_groups(meta) == []


def test_iter_link_groups_skips_link_whose_url_does_not_parse():
    meta = {'links': [{'name': 'Marlon Brando', 'category': 'Cast', 'url': IMDB_LINK_URL}]}
    assert iter_link_groups(meta) == []


def test_iter_link_groups_tolerates_missing_or_none_links():
    assert iter_link_groups({}) == []
    assert iter_link_groups({'links': None}) == []


def test_iter_link_groups_tolerates_non_list_links():
    assert iter_link_groups({'links': 'not-a-list'}) == []


def test_iter_link_groups_tolerates_non_dict_meta():
    assert iter_link_groups(None) == []
    assert iter_link_groups('not-a-dict') == []


def test_iter_link_groups_tolerates_non_dict_entries_in_links():
    meta = {'links': [None, 'garbage', 42, {'name': 'Marlon Brando', 'category': 'Cast',
                                             'url': CAST_LINK_URL}]}
    groups = iter_link_groups(meta)
    assert groups == [('Cast', [('Marlon Brando', {'kind': 'search', 'query': 'Marlon Brando'})])]
