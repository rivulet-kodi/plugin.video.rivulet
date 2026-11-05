"""Property tests for the stream/subtitle/meta-link parsers
(``lib.stremio.streaminfo``, ``subtitles``, ``metalinks``): their input is
whatever a third-party addon answered, and one addon's malformed entry must
never take the rest of the list down.
"""
import re

from hypothesis import example, given
from hypothesis import strategies as st

from lib.stremio import metalinks, streaminfo, subtitles
from tests.fuzz_strategies import any_text, hostile_json, json_values, streams, subtitle_objects

_INFO_KEYS = {
    'addon', 'title', 'resolution', 'source', 'codec', 'hdr', 'size_bytes', 'size_text', 'seeders',
    'is_torrent', 'filename', 'binge_group', 'raw', 'audio', 'channels', 'languages', 'bitrate',
    'group', 'tracker', 'service', 'cached', 'release', 'label',
}
_LABEL_MARKUP = re.compile(r'\[/?(?:B|I|COLOR[^\]]*|CR|UPPERCASE|LOWERCASE|CAPITALIZE|LIGHT|TABS)\]', re.I)

_infos = st.builds(streaminfo.parse_stream, hostile_json(streams), any_text)


# --- streaminfo ---------------------------------------------------------------


@given(hostile_json(streams), json_values)
@example({'behaviorHints': 'x', 'name': ['a']}, None)
@example({'behaviorHints': {'videoSize': 10**400}, 'title': '1.5 GB'}, None)
def test_parse_stream_never_raises_and_returns_the_full_shape(stream, addon_name):
    info = streaminfo.parse_stream(stream, addon_name if isinstance(addon_name, str) else '')
    assert set(info) == _INFO_KEYS
    for key in ('addon', 'title', 'raw', 'filename', 'size_text', 'bitrate', 'channels', 'source', 'codec'):
        assert isinstance(info[key], str), key
    assert info['size_bytes'] is None or (isinstance(info['size_bytes'], int) and info['size_bytes'] >= 0)
    assert info['seeders'] is None or (isinstance(info['seeders'], int) and info['seeders'] >= 0)
    assert info['cached'] in (True, False, None)
    assert info['is_torrent'] in (True, False)
    assert info['resolution'] in ('', '2160p', '1080p', '720p', '480p')
    for key in ('hdr', 'audio', 'languages', 'release'):
        assert isinstance(info[key], list) and all(isinstance(item, str) for item in info[key]), key
    # `raw` is what the filters and the label read: clean_text() output, so
    # one line, no emoji/symbol junk, bounded.
    assert '\n' not in info['raw'] and '\r' not in info['raw']
    assert len(info['raw']) <= streaminfo._MAX_TEXT_LEN
    assert streaminfo._JUNK_RE.search(info['raw']) is None


@given(_infos, st.booleans())
def test_every_formatter_accepts_parse_stream_output(info, include_addon):
    label = streaminfo.format_label(info, include_addon=include_addon)
    assert isinstance(label, str) and '\n' not in label
    assert isinstance(streaminfo.format_details(info), str)
    assert '\n' not in streaminfo.format_details(info)
    assert isinstance(streaminfo.format_plot(info), str)
    fields = streaminfo.stream_fields(info)
    assert all(isinstance(value, str) for value in fields.values())
    assert fields['cache_state'] != ''
    # The addon name is the one untrusted string interpolated into the
    # label's own markup, so it is escaped when parsed: no tag of the
    # label vocabulary or `$INFO[` survives inside it.
    assert _LABEL_MARKUP.search(info['addon']) is None
    assert streaminfo._LABEL_INFO_RE.search(info['addon']) is None


@given(json_values)
@example('[[B]B]')
@example('$$INFO[x]')
def test_escape_label_never_raises_and_is_idempotent(value):
    cleaned = streaminfo.escape_label(value)
    assert isinstance(cleaned, str)
    assert streaminfo.escape_label(cleaned) == cleaned
    assert _LABEL_MARKUP.search(cleaned) is None


@given(json_values)
def test_clean_text_never_raises_and_is_idempotent(value):
    # Only str/falsy input is the contract (callers pass `_text()` output), but
    # a stray number must not raise either.
    cleaned = streaminfo.clean_text(value)
    assert isinstance(cleaned, str)
    assert len(cleaned) <= streaminfo._MAX_TEXT_LEN
    assert streaminfo.clean_text(cleaned) == cleaned


@given(st.lists(st.tuples(_infos, hostile_json(streams)), max_size=6),
       st.sampled_from(['quality', 'size', 'seeders', 'bogus']))
def test_sort_streams_is_a_stable_permutation(pairs, key):
    ordered = streaminfo.sort_streams(pairs, key=key)
    assert len(ordered) == len(pairs)
    assert sorted(map(id, ordered)) == sorted(map(id, pairs))
    if key == 'size':
        sizes = [pair[0]['size_bytes'] for pair in ordered if pair[0]['size_bytes'] is not None]
        assert sizes == sorted(sizes, reverse=True)
    elif key == 'seeders':
        seeds = [pair[0]['seeders'] for pair in ordered if pair[0]['seeders'] is not None]
        assert seeds == sorted(seeds, reverse=True)
        # Unknown seeders sort after every known one.
        flags = [pair[0]['seeders'] is None for pair in ordered]
        assert flags == sorted(flags)
    else:
        tiers = [streaminfo._RESOLUTION_TIER[pair[0]['resolution']] for pair in ordered]
        assert tiers == sorted(tiers, reverse=True)


@given(
    st.lists(st.tuples(_infos, st.none()), max_size=6),
    st.integers(min_value=-10, max_value=3000),
    st.integers(min_value=-10, max_value=2**40),
    st.booleans(),
    st.booleans(),
)
@example([(streaminfo.parse_stream({'behaviorHints': {'videoSize': 1}}, ''), None)], 0, -1, False, False)
def test_filter_streams_keeps_an_ordered_subset_that_satisfies_every_active_filter(
    entries, min_height, max_size, exclude_cam, cached_only,
):
    kept = streaminfo.filter_streams(
        entries, min_height=min_height, max_size_bytes=max_size,
        exclude_cam=exclude_cam, cached_only=cached_only,
    )
    ids = [id(pair) for pair in entries]
    kept_ids = [id(pair) for pair in kept]
    assert kept_ids == [i for i in ids if i in set(kept_ids)], 'order or identity lost'
    for info, _stream in kept:
        if min_height > 0:
            height = streaminfo._RESOLUTION_HEIGHT.get(info['resolution'])
            assert height is None or height >= min_height
        if max_size > 0 and info['size_bytes'] is not None:
            assert info['size_bytes'] <= max_size
        if exclude_cam:
            assert streaminfo._CAM_FILTER_RE.search(info['raw']) is None
        if cached_only:
            assert info['cached'] is True
    if not (min_height > 0 or max_size > 0 or exclude_cam or cached_only):
        assert kept == list(entries)
    total, hidden = streaminfo.filter_summary(len(entries), len(kept))
    assert total == len(entries) and hidden == len(entries) - len(kept)


# --- subtitles ----------------------------------------------------------------


class _Client:
    """Stands in for AddonClient: answers every subtitles call with `answer`
    (or raises it, when it is an exception)."""

    def __init__(self, answer):
        self.answer = answer

    def subtitles(self, transport_url, rtype, rid, extra=None):
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


#: A single subtitle-capable addon: one request, so no worker threads.
_SUBTITLE_ADDON = [{
    'transportUrl': 'https://subs.example/manifest.json',
    'manifest': {'resources': ['subtitles'], 'types': ['movie'], 'idPrefixes': ['tt']},
}]


@given(st.lists(hostile_json(subtitle_objects), max_size=6) | json_values)
@example([{'url': 'https://a.example/1.srt', 'label': ['x'], 'lang': {'a': 1}, 'id': None}])
def test_collect_subtitles_keeps_only_usable_unique_http_urls(answer):
    subs = subtitles.collect_subtitles(_Client(answer), _SUBTITLE_ADDON, 'movie', 'tt1')
    urls = [sub['url'] for sub in subs]
    assert len(urls) == len(set(urls))
    for sub in subs:
        assert sub['url'].split(':', 1)[0].lower() in ('http', 'https')
        assert isinstance(sub['lang'], str)
        assert sub['id']
        assert set(sub) <= {'id', 'lang', 'url', 'label'}
        if 'label' in sub:
            assert sub['label'] and sub['label'] == sub['label'].strip()
            assert '\n' not in sub['label'] and '\r' not in sub['label']


@given(st.sampled_from([RuntimeError('boom'), ValueError('x'), None, 3, 'text', {}]))
def test_collect_subtitles_survives_a_broken_addon(answer):
    assert subtitles.collect_subtitles(_Client(answer), _SUBTITLE_ADDON, 'movie', 'tt1') == []


@given(st.lists(hostile_json(subtitle_objects), max_size=3), st.lists(hostile_json(st.fixed_dictionaries({}, optional={
    'transportUrl': json_values, 'manifest': json_values})), max_size=3))
def test_collect_subtitles_tolerates_arbitrary_installed_addons(answer, addon_list):
    # Whatever the descriptors are, the fan-out never raises; a list of
    # junk entries yields no queries at all.
    subtitles.collect_subtitles(_Client(answer), addon_list, 'movie', 'tt1')


_sub_entries = st.lists(st.fixed_dictionaries({
    'id': any_text, 'url': any_text, 'lang': st.sampled_from(['en', 'eng', 'it', 'ita', 'fr', '', ' EN ', 'pob', 'pt-BR', 'en_US', 'ell']) | any_text,
}), max_size=6)


@given(_sub_entries, json_values)
@example([{'id': 'a', 'url': 'u', 'lang': 'en'}], 5)
@example([{'id': 'a', 'url': 'u', 'lang': 'pob'}, {'id': 'b', 'url': 'u', 'lang': 'pt-BR'}], 'pt')
def test_filter_subtitles_returns_an_ordered_subset(subs, preferred):
    kept = subtitles.filter_subtitles(subs, preferred if isinstance(preferred, str) or preferred is None else '')
    positions = [next(i for i, sub in enumerate(subs) if sub is entry) for entry in kept]
    assert positions == sorted(positions)
    if preferred is None or preferred == '' or not isinstance(preferred, str) or not preferred.strip():
        assert kept == subs


# --- meta links ---------------------------------------------------------------


_link_urls = st.one_of(
    st.sampled_from([
        'stremio:///search?search=a%20b', 'stremio:///discover/https%3A%2F%2Fa.example%2Fmanifest.json/movie/top?genre=x',
        'stremio:///detail/series/tt1/tt1%3A1%3A2', 'stremio:///detail/movie/tt1', 'https://imdb.com/x',
        'stremio://host/search?search=a', 'stremio:///[', 'stremio:///discover/%ff/movie/top',
    ]),
    any_text,
    any_text.map(lambda text: 'stremio:///' + text),
)


@given(json_values | _link_urls)
def test_parse_meta_link_never_raises_and_fills_every_required_field(url):
    parsed = metalinks.parse_meta_link(url)
    if parsed is None:
        return
    kind = parsed['kind']
    if kind == 'search':
        assert parsed['query']
    elif kind == 'discover':
        assert parsed['transport_url'] and parsed['type'] and parsed['catalog_id']
        assert all(isinstance(name, str) and isinstance(value, str) for name, value in parsed['extra'])
    else:
        assert kind == 'detail'
        assert parsed['type'] and parsed['id']
        assert parsed['video_id'] is None or isinstance(parsed['video_id'], str)


_links = st.fixed_dictionaries({}, optional={
    'category': st.sampled_from(['Cast', 'Directors', 'imdb', 'Share', 'Genres', '']) | json_values,
    'name': any_text | json_values,
    'url': _link_urls | json_values,
})


@given(hostile_json(st.fixed_dictionaries({}, optional={'links': st.lists(hostile_json(_links), max_size=6) | json_values})))
def test_iter_link_groups_never_raises_and_groups_by_first_seen_category(meta):
    groups = metalinks.iter_link_groups(meta)
    categories = [category for category, _members in groups]
    assert len(categories) == len(set(categories))
    for category, members in groups:
        assert isinstance(category, str) and category
        assert category.casefold() not in metalinks.RESERVED_LINK_CATEGORIES
        assert members
        for name, parsed in members:
            assert name and parsed['kind'] in ('search', 'discover', 'detail')
