"""Shared Hypothesis strategies for the ``tests/test_fuzz_*.py`` properties.

Everything the Kodi-independent layer parses is JSON from a third party (a
Stremio addon, the account API, a hand-edited file in ``addon_data``), so
the one thing every property below starts from is :data:`json_values`:
arbitrary JSON as ``json.loads`` can hand it back. That is wider than
"well-formed protocol JSON" in the ways that have bitten this codebase
before: ``Infinity``/``NaN`` floats (Python's ``json`` accepts the
literals), integers far outside 64 bits, and lone surrogates (``"\\ud800"``
decodes without error, and then fails to encode as UTF-8 on the way back
out).

The record builders (:func:`library_items`, :func:`manifests`,
:func:`streams`, ...) start from a realistic shape and then let every field
be replaced by an arbitrary JSON value (:func:`noisy`). A uniformly random
dict almost never carries a ``type`` or a ``state`` the code under test
looks at, so it would exercise only its first guard clause; perturbing a
realistic record reaches the branches behind it.
"""
import datetime

from hypothesis import strategies as st

# --- generic JSON ----------------------------------------------------------

#: Text that may carry lone surrogates, which ``st.text()`` never produces
#: but ``json.loads('"\\ud800"')`` does.
any_text = st.one_of(
    st.text(max_size=24),
    st.text(alphabet=st.characters(exclude_categories=()), max_size=12),
)

#: Integers on both sides of every width a parser might silently assume.
_big_ints = st.one_of(
    st.integers(),
    st.sampled_from([2**31, 2**32, 2**53, 2**63, -(2**63), 2**64, 10**30, 2**1024, -(2**1024)]),
)

#: Every float JSON can carry via Python's ``json`` (``Infinity``, ``NaN``).
_floats = st.floats(allow_nan=True, allow_infinity=True)

_scalars = st.one_of(st.none(), st.booleans(), _big_ints, _floats, any_text)

#: Arbitrary JSON values. Small on purpose: breadth of *kinds* matters, not
#: size, and the recursive strategy is the slow part of every example.
json_values = st.recursive(
    _scalars,
    lambda children: st.one_of(
        st.lists(children, max_size=4),
        st.dictionaries(st.text(max_size=8), children, max_size=4),
    ),
    max_leaves=10,
)

json_objects = st.dictionaries(st.text(max_size=8), json_values, max_size=5)


def noisy(strategy):
    """`strategy`, or - one time in three - an arbitrary JSON value instead.

    Applied to every field of a realistic record, so one generated record
    has a handful of well-formed fields and a handful of wrong-typed ones.
    """
    return st.one_of(strategy, strategy, json_values)


def noisy_record(required=None, **optional):
    """A dict that may carry each of ``optional``'s keys (every value
    :func:`noisy`), plus ``required``'s keys always."""
    return st.fixed_dictionaries(
        {key: noisy(value) for key, value in (required or {}).items()},
        optional={key: noisy(value) for key, value in optional.items()},
    )


def hostile_json(strategy):
    """`strategy`, or an arbitrary JSON value (a record that is not even a
    dict)."""
    return st.one_of(strategy, strategy, strategy, json_values)


# --- timestamps -------------------------------------------------------------

_datetimes = st.datetimes(
    min_value=datetime.datetime(1971, 1, 1),
    max_value=datetime.datetime(2100, 1, 1),
)

#: ``lastWatched``/``_mtime`` as chrono emits them (fractional seconds,
#: ``Z``), as ``iso8601_utc`` writes them, with an explicit offset, and
#: as arbitrary text.
timestamps = st.one_of(
    _datetimes.map(lambda dt: dt.strftime('%Y-%m-%dT%H:%M:%S.%fZ')),
    _datetimes.map(lambda dt: dt.strftime('%Y-%m-%dT%H:%M:%SZ')),
    _datetimes.map(lambda dt: dt.strftime('%Y-%m-%dT%H:%M:%S+01:30')),
    _datetimes.map(lambda dt: dt.strftime('%Y-%m-%dT%H:%M:%S.123456-0500')),
    any_text,
)

# --- ids and types -----------------------------------------------------------

_ids = st.one_of(
    st.sampled_from(['tt0111161', 'tt0944947:1:2', 'kitsu:1234', 'tmdb:55', 'aiostreamserror.x']),
    any_text,
)
_content_types = st.one_of(
    st.sampled_from(['movie', 'series', 'tv', 'anime', 'anime.movie', 'anime.series', 'channel']),
    any_text,
)
ms_values = st.one_of(st.integers(min_value=0, max_value=10**8), _big_ints)

# --- library records ---------------------------------------------------------

#: ``LibraryItemState`` (library_item.rs:228-243) with each field perturbed.
library_states = noisy_record(
    lastWatched=timestamps,
    timeWatched=ms_values,
    timeOffset=ms_values,
    overallTimeWatched=ms_values,
    timesWatched=st.integers(min_value=0, max_value=20),
    flaggedWatched=st.sampled_from([0, 1]),
    duration=ms_values,
    video_id=st.one_of(st.none(), _ids),
    watched=st.one_of(st.none(), st.text(max_size=40)),
    noNotif=st.booleans(),
)

#: One account ``libraryItem`` record.
library_items = noisy_record(
    _id=_ids,
    type=_content_types,
    name=any_text,
    poster=st.one_of(st.none(), any_text),
    removed=st.booleans(),
    temp=st.booleans(),
    _ctime=timestamps,
    _mtime=timestamps,
    state=library_states,
    behaviorHints=noisy_record(defaultVideoId=_ids, hasScheduledVideos=st.booleans()),
)

#: A Stremio meta preview as an addon's catalog lists it: ``id`` is the one
#: key the protocol requires, so it stays a string; every other field is
#: perturbed.
metas = st.fixed_dictionaries(
    {'id': _ids},
    optional={
        'type': noisy(_content_types),
        'name': noisy(any_text),
        'poster': noisy(any_text),
        'posterShape': noisy(st.sampled_from(['poster', 'landscape', 'square'])),
        'behaviorHints': noisy_record(
            defaultVideoId=_ids, featuredVideoId=_ids, hasScheduledVideos=st.booleans(),
        ),
    },
)

#: A ``libraryItem`` datastore read: a list that may hold non-records.
library_item_lists = st.lists(hostile_json(library_items), max_size=6)

#: One ``Store.get_progress_entries()`` entry.
progress_entries = noisy_record(
    type=_content_types,
    id=_ids,
    video_id=st.one_of(st.none(), _ids),
    position_ms=ms_values,
    duration_ms=ms_values,
    updated_at=timestamps,
)

# --- addon manifests ---------------------------------------------------------

_resource_names = st.sampled_from(['catalog', 'meta', 'stream', 'subtitles', 'addon_catalog'])

extra_props = noisy_record(
    name=st.sampled_from(['search', 'genre', 'skip', 'year', '']) | any_text,
    isRequired=st.booleans(),
    options=st.lists(noisy(any_text), max_size=4),
)

catalogs = noisy_record(
    id=_ids,
    type=_content_types,
    name=any_text,
    extra=st.lists(hostile_json(extra_props), max_size=4),
    extraRequired=st.lists(noisy(any_text), max_size=3),
    extraSupported=st.lists(noisy(any_text), max_size=3),
    genres=st.lists(noisy(any_text), max_size=3),
)

_long_resources = noisy_record(
    name=_resource_names,
    types=st.lists(noisy(_content_types), max_size=3),
    idPrefixes=st.lists(noisy(st.sampled_from(['tt', 'kitsu:', ''])), max_size=3),
)

manifests = noisy_record(
    id=_ids,
    name=any_text,
    version=any_text,
    types=st.lists(noisy(_content_types), max_size=4),
    idPrefixes=st.lists(noisy(st.sampled_from(['tt', 'kitsu:', ''])), max_size=3),
    resources=st.lists(hostile_json(st.one_of(_resource_names, _long_resources)), max_size=4),
    catalogs=st.lists(hostile_json(catalogs), max_size=4),
)

_transport_urls = st.one_of(
    st.sampled_from([
        'https://addon.example/manifest.json',
        'https://addon.example/',
        'http://[::1]:7000/manifest.json',
    ]),
    any_text,
)

#: One ``Store.get_addons()`` descriptor.
addon_descriptors = noisy_record(
    transportUrl=_transport_urls,
    manifest=manifests,
    flags=noisy_record(disabled=st.booleans(), offline=st.booleans()),
)

addon_lists = st.lists(hostile_json(addon_descriptors), max_size=4)

# --- streams and subtitles ---------------------------------------------------

#: Fragments real addon titles are made of (AIOStreams, Torrentio, debrid
#: wrappers), so the regex branches in ``parse_stream`` actually fire.
_STREAM_TOKENS = [
    '2160p', '4K', '1080p', '720p', '480p', 'WEB-DL', 'BluRay', 'Remux', 'HDTV', 'CAM',
    'HDTS', 'HEVC', 'x265', 'x264', 'AV1', 'HDR10+', 'DV', 'Atmos', 'DDP5.1', 'DTS-HD',
    '7.1', '5.1', '12 Mbps', '3.3 Mbps', '\U0001f464 120', '\U0001f4be 2.3 GB', '1.5GB', '700 MB',
    'Seeders: 5', 'S:12', '\u26a1', '[RD+]', '[TB download]', '[AD]', 'MULTi', '\U0001f1ec\U0001f1e7',
    '\U0001f1ee\U0001f1f9', '-GROUP', '\u2699\ufe0f Torrentio', 'Cached', '\u23f3', 'Extended',
    'Hybrid', 'P8', '[B]x[/B]', '$INFO[Player.Title]', '\n', '\r\n', '\u200b',
]

_stream_text = st.lists(
    st.one_of(st.sampled_from(_STREAM_TOKENS), any_text), max_size=8,
).map(' '.join)

_behavior_hints = noisy_record(
    videoSize=_big_ints,
    filename=_stream_text,
    bingeGroup=any_text,
    notWebReady=st.booleans(),
    countryWhitelist=st.lists(noisy(any_text), max_size=3),
)

#: One Stream object (stremio-core ``stream.rs``).
streams = noisy_record(
    name=_stream_text,
    title=_stream_text,
    description=_stream_text,
    url=any_text,
    infoHash=st.text(alphabet='0123456789abcdef', max_size=40),
    fileIdx=st.integers(min_value=-1, max_value=50),
    behaviorHints=_behavior_hints,
)

#: One subtitle object an addon's ``/subtitles`` answer lists.
subtitle_objects = noisy_record(
    id=_ids,
    url=st.one_of(
        st.sampled_from([
            'https://subs.example/1.srt', 'http://subs.example/2.srt', 'ftp://subs.example/3.srt',
            'file:///etc/passwd', 'http://[x/a', 'https://subs.example/1.srt',
        ]),
        any_text,
    ),
    lang=st.sampled_from(['en', 'eng', 'it', 'ita', 'fr', 'fre', 'fra', '', 'EN ']) | any_text,
    label=any_text,
)

# --- Home layouts ------------------------------------------------------------

_VALID_LIST_IDS = [
    'continue', 'continue:movie', 'continue:series', 'continue:anime',
    'library', 'library:movie', 'library:series',
    'new:movie', 'new:series', 'new:anime-movie', 'new:anime-series',
    'rest:movie', 'rest:series', 'rest:anime-movie', 'rest:anime-series',
    'cat:https://a.example/manifest.json|movie|top',
    'cat:https://a.example/manifest.json|anime|airing|movie',
]
_INVALID_LIST_IDS = [
    'cat:', 'cat:a|b', 'cat:a|b|c', 'cat:a|anime|c|show', 'cat:a|movie|c|movie', 'new:tv', '',
    'continue:', 'rest', 'library:anime',
]

list_ids = st.one_of(
    st.sampled_from(_VALID_LIST_IDS),
    st.sampled_from(_INVALID_LIST_IDS),
    json_values,
)

_TABS = ['home', 'mystuff', 'movies', 'series', 'anime']

#: A saved Home layout, or something the file might hold instead.
home_layouts = hostile_json(st.fixed_dictionaries({
    'version': st.one_of(st.just(1), st.just(1), st.just(1), st.just(True), st.just(1.0), json_values),
    'tabs': st.one_of(
        st.dictionaries(
            st.sampled_from(_TABS + ['extra']),
            st.one_of(st.lists(list_ids, max_size=8), st.lists(list_ids, max_size=8), json_values),
            max_size=6,
        ),
        json_values,
    ),
}))
