"""Heuristic adult-content detection for Stremio metas and catalogs.

The Stremio addon protocol has NO official "this is adult content" flag.
`MetaPreview`/`MetaItem` define an optional `adult: boolean`, but it is
advisory and addon-authored - nothing in stremio-core validates or
requires it, and community catalogs that do carry adult material
routinely omit it. That absence is exactly why this module exists, and
why it is heuristic rather than authoritative: a future reader who spots
a manifest with no `adult` field anywhere should not conclude we missed
it - there is nothing to read.

Detection therefore falls back to matching a small set of adult-content
marker words against free-text fields (genre, catalog id/name) on WORD
BOUNDARIES, never as a bare substring - "Analyze That" or "Milford
Sound" must not trip on "anal"/"milf" embedded inside a longer word.
This is a real tradeoff, not a solved problem: word-boundary matching
still lets a title genuinely named after a marker word slip through as
a false positive (a franchise called "xXx" would match `\\bxxx\\b`
verbatim), and conversely a catalog that never says any marker word at
all is a false negative this module cannot catch. Callers accept both
risks in exchange for not shipping an allow/deny list of every adult
addon in existence.
"""
import re

#: Adult-content marker words, matched case-insensitively on word
#: boundaries against genre/catalog id/catalog name text. Deliberately
#: short: each addition is a new false-positive surface (see the
#: module docstring's "xXx" example), so this stays limited to words
#: that are adult-content markers in virtually every real usage.
_ADULT_MARKERS = frozenset({
    'adult', 'xxx', 'porn', 'porno', 'pornographic', 'hentai',
    'erotic', 'erotica', 'nsfw', 'anal', 'milf', 'fetish',
})

_ADULT_MARKER_RE = re.compile(
    r'\b(?:%s)\b' % '|'.join(re.escape(marker) for marker in _ADULT_MARKERS),
    re.IGNORECASE,
)

#: Stremio content `type` values that are adult by definition, checked
#: as an exact (case-insensitive) match rather than the marker regex -
#: `type` is a short addon-authored token, not free text, so a plain
#: membership test is both sufficient and cheaper.
_ADULT_TYPES = frozenset({'xxx', 'adult'})


def _contains_marker(text):
    """Whether `text` contains an adult marker word on a word boundary."""
    return bool(text) and _ADULT_MARKER_RE.search(str(text)) is not None


def _type_is_adult(type_value):
    """Whether a Stremio content-`type` string counts as adult - exact
    membership in `_ADULT_TYPES`, or (belt and suspenders) a marker word
    if an addon invents its own type like "hentai" instead of "xxx"."""
    if not type_value:
        return False
    normalized = str(type_value).strip().lower()
    return normalized in _ADULT_TYPES or _contains_marker(normalized)


def _coerce_bool(value):
    """Best-effort bool coercion of an addon-supplied `adult` flag.

    The protocol's own `adult` field is typed as a real boolean, but
    addons are not obliged to send anything typed - some send "true"/
    "false" strings, 0/1, or nothing parseable at all. Returns None
    (not a default) when `value` cannot be read as a bool at all, so
    callers can fall back to the genre/type signals instead of trusting
    a garbage value.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ('true', '1', 'yes'):
            return True
        if normalized in ('false', '0', 'no', ''):
            return False
    return None


def _genre_strings(obj):
    """Every genre string on `obj`, from either the modern `genres`
    array or the deprecated `genre` field (string or string array) -
    Stremio addons in the wild still send either."""
    values = []
    for field in ('genres', 'genre'):
        value = obj.get(field)
        if not value:
            continue
        values.extend(value) if isinstance(value, list) else values.append(value)
    return [str(value) for value in values if value]


def is_adult_meta(meta):
    """Whether `meta` (a Stremio MetaPreview/MetaItem dict) looks adult.

    Signals are checked in order, and the first that gives a definite
    answer wins:

    1. An explicit `adult` field, if the addon supplies one and it
       parses as a bool - this is authoritative even when it disagrees
       with signal 2/3 (an addon marking `adult: false` on something
       whose genre happens to contain a marker word is trusted, not
       second-guessed).
    2. `genres`/`genre` containing an adult marker word.
    3. `type` being an adult type (`xxx`/`adult`, or a marker word).

    Returns False for anything that isn't a dict (no signal to read).
    """
    if not isinstance(meta, dict):
        return False
    if 'adult' in meta and meta['adult'] is not None:
        explicit = _coerce_bool(meta['adult'])
        if explicit is not None:
            return explicit
    if any(_contains_marker(genre) for genre in _genre_strings(meta)):
        return True
    return _type_is_adult(meta.get('type'))


def is_adult_catalog(catalog, manifest=None):
    """Whether `catalog` (a Stremio catalog descriptor dict, as declared
    in `manifest['catalogs']`) looks adult - the same marker-word/type
    treatment `is_adult_meta()` gives one item, applied to the catalog's
    own `id`/`name`/`type` so a whole adult catalog can be skipped
    before any network request is spent fetching it.

    `manifest`, if given, is also checked: an addon whose own
    `manifest['types']` names an adult type marks every catalog it
    serves as adult, even one with an innocuous id/name (e.g. a single
    "Popular" catalog on an addon that only serves `xxx`).
    """
    if not isinstance(catalog, dict):
        return False
    if _contains_marker(catalog.get('id')) or _contains_marker(catalog.get('name')):
        return True
    if _type_is_adult(catalog.get('type')):
        return True
    if manifest:
        types = manifest.get('types') or []
        if isinstance(types, str):
            types = [types]
        if any(_type_is_adult(t) for t in types):
            return True
    return False


def filter_metas(metas):
    """The subset of `metas` for which `is_adult_meta()` is False, order
    preserved. Always returns a new list - never mutates `metas`."""
    return [meta for meta in metas if not is_adult_meta(meta)]
