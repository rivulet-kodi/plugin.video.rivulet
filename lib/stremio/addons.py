"""Stremio addon-protocol HTTP client (pure Python, no Kodi imports).

Implements the addon resource protocol exactly as stremio-core does it:
- URL shape & percent-encoding: src/addon_transport/http_transport/http_transport.rs,
  src/types/query_params_encode.rs, src/constants.rs (URI_COMPONENT_ENCODE_SET).
- Manifest resource/type/idPrefix matching: src/types/addon/manifest.rs
  (Manifest::is_resource_supported).
- Catalog declarations (modern `extra` array vs legacy extraSupported/
  extraRequired): src/types/addon/manifest.rs (ManifestCatalog, ManifestExtra).

`requests` is imported at module scope but guarded so this module - and all
its pure helper functions (encode_extra, build_resource_url, addon_supports,
iter_catalogs) - stay importable even where `requests` is missing; only
constructing/using an AddonClient actually needs it.
"""
from urllib.parse import quote

try:
    import requests
except ImportError:  # pragma: no cover - exercised only without the dependency
    requests = None

#: stremio-core's URI_COMPONENT_ENCODE_SET (constants.rs): percent-encode
#: everything except alphanumerics and these characters.
EXTRA_SAFE_CHARS = "-_.!~*'()"

#: Addons are requested by replacing this manifest.json suffix with the
#: resource path (http_transport.rs); build_resource_url() strips it from a
#: transport_url so callers can pass either form as `base`.
MANIFEST_SUFFIX = '/manifest.json'


def _encode_component(value):
    """Percent-encode one path/extra component, matching URI_COMPONENT_ENCODE_SET."""
    return quote(str(value), safe=EXTRA_SAFE_CHARS)


def encode_extra(props, *value):
    """Encode addon "extra" properties into a `name=value&name2=value2` segment.

    This is NOT a `?query` string - it becomes one more `/`-separated path
    segment before `.json` (see build_resource_url). Percent-encoding keeps
    stremio-core's safe set `-_.!~*'()` (constants.rs); everything else,
    including space, is escaped as %XX (space -> %20, not `+`).

    Canonical form takes a list/tuple of (name, value) pairs, preserving
    order, e.g.::

        encode_extra([("search", "breaking bad"), ("skip", "100")])
        # -> "search=breaking%20bad&skip=100"

    A `(name, value)` two-argument shorthand is also accepted for the
    common single-prop case::

        encode_extra("search", "breaking bad")
        # -> "search=breaking%20bad"
    """
    if value:
        pairs = [(props, value[0])]
    else:
        pairs = list(props or [])
    return '&'.join(
        '%s=%s' % (_encode_component(name), _encode_component(val))
        for name, val in pairs
    )


def build_resource_url(base, resource, rtype, rid, extra=None):
    """Build `{base}/{resource}/{type}/{id}[/{extra}].json`.

    `base` may be a transport_url with or without the trailing
    "/manifest.json" - either is accepted, the suffix is stripped if
    present (http_transport.rs replaces ADDON_MANIFEST_PATH with the
    resource path on the raw transport_url string). `resource`/`type`/`id`
    are percent-encoded individually with the same safe set as extra
    values. `extra` may be a list/tuple of (name, value) pairs (encoded via
    encode_extra) or an already-encoded "name=value&..." string (used
    verbatim, e.g. when round-tripped through a plugin:// URL parameter);
    when falsy, no extra path segment is appended.
    """
    if base.endswith(MANIFEST_SUFFIX):
        base = base[:-len(MANIFEST_SUFFIX)]
    else:
        base = base.rstrip('/')

    segments = [
        _encode_component(resource),
        _encode_component(rtype),
        _encode_component(rid),
    ]

    if extra:
        extra_segment = extra if isinstance(extra, str) else encode_extra(extra)
        if extra_segment:
            segments.append(extra_segment)

    return '%s/%s.json' % (base, '/'.join(segments))


class AddonError(Exception):
    """Raised when an addon HTTP request fails or returns malformed JSON."""


class AddonClient:
    """Thin HTTP client for the addon manifest/catalog/meta/stream/subtitles
    resources. One `requests.Session()` per client instance (stored as
    `.session`, not private, so callers/tests can substitute it directly).
    """

    def __init__(self, timeout=15):
        if requests is None:
            raise AddonError('the "requests" package is required for AddonClient')
        self.timeout = timeout
        self.session = requests.Session()

    def _get_json(self, url):
        try:
            resp = self.session.get(url, timeout=self.timeout)
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise AddonError('GET %s failed: %s' % (url, exc))
        try:
            return resp.json()
        except ValueError as exc:
            raise AddonError('GET %s returned invalid JSON: %s' % (url, exc))

    def manifest(self, transport_url):
        """GET the addon manifest (transport_url normally ends in /manifest.json)."""
        return self._get_json(transport_url)

    def catalog(self, base, rtype, cid, extra=None):
        """GET a catalog resource -> list of meta preview objects (resp['metas'])."""
        url = build_resource_url(base, 'catalog', rtype, cid, extra)
        return self._get_json(url).get('metas') or []

    def meta(self, base, rtype, mid):
        """GET a meta resource -> the meta object (resp['meta'])."""
        url = build_resource_url(base, 'meta', rtype, mid)
        return self._get_json(url).get('meta')

    def streams(self, base, rtype, sid):
        """GET a stream resource -> list of Stream objects (resp['streams'])."""
        url = build_resource_url(base, 'stream', rtype, sid)
        return self._get_json(url).get('streams') or []

    def subtitles(self, base, rtype, sid, extra=None):
        """GET a subtitles resource -> list of subtitle objects (resp['subtitles'])."""
        url = build_resource_url(base, 'subtitles', rtype, sid, extra)
        return self._get_json(url).get('subtitles') or []


def _resource_entry(manifest, resource):
    """Return manifest['resources'] entry (short string or long-form dict)
    named `resource`, or None if the addon doesn't declare it at all."""
    for entry in manifest.get('resources') or []:
        name = entry if isinstance(entry, str) else entry.get('name')
        if name == resource:
            return entry
    return None


def addon_supports(manifest, resource, rtype, rid=None):
    """Whether `manifest` serves `resource` for `rtype` (and `rid`, if given).

    Mirrors stremio-core's Manifest::is_resource_supported (manifest.rs),
    with one deliberate relaxation for a friendlier Kodi client: a
    long-form resource entry that OMITS `types`/`idPrefixes` falls back to
    the addon's global `types`/`idPrefixes` fields, same as the short
    string form does (stremio-core only falls back for the short form).
    An explicit list is always honoured literally: empty `idPrefixes`
    matches every id (protocol convention - "no restriction"), empty
    `types` matches no type (there is nothing to match against).
    """
    entry = _resource_entry(manifest, resource)
    if entry is None:
        return False

    if isinstance(entry, str):
        types, id_prefixes = None, None
    else:
        types, id_prefixes = entry.get('types'), entry.get('idPrefixes')

    if types is None:
        types = manifest.get('types') or []
    if id_prefixes is None:
        id_prefixes = manifest.get('idPrefixes') or []

    if rtype is not None and rtype not in types:
        return False
    if rid is not None and id_prefixes and not any(rid.startswith(p) for p in id_prefixes):
        return False
    return True


def _catalog_extra_names(catalog):
    """Extra prop names a catalog declares, from either the modern
    `extra: [{name, isRequired, ...}]` array or the legacy
    `extraSupported`/`extraRequired` string-array form (both are valid
    ManifestExtra encodings per manifest.rs)."""
    extra = catalog.get('extra')
    if isinstance(extra, list):
        return {p.get('name') for p in extra if isinstance(p, dict) and p.get('name')}
    names = set(catalog.get('extraSupported') or [])
    names.update(catalog.get('extraRequired') or [])
    return names


def iter_catalogs(addons, extra_required=None):
    """Yield `(transport_url, manifest, catalog)` for every catalog declared
    by `addons` - a list of descriptor dicts shaped like
    `{"transportUrl": ..., "manifest": {...}, ...}`
    (lib.store.Store.get_addons() / StremioAPI.addon_collection_get()).

    Catalog support is determined purely by presence in
    `manifest['catalogs']`, independent of the manifest 'resources' list
    (that's how stremio-core treats it too - see
    Manifest::is_resource_supported's "catalog"/"addon_catalog" arms,
    which check self.catalogs directly rather than self.resources).

    When `extra_required` is given (e.g. "search"), only catalogs that
    declare that extra prop name are yielded - this is a plain existence
    check on the prop name, not a full is_extra_supported() validation of
    every other required prop.
    """
    for descriptor in addons or []:
        manifest = descriptor.get('manifest') or {}
        transport_url = descriptor.get('transportUrl')
        for catalog in manifest.get('catalogs') or []:
            if extra_required and extra_required not in _catalog_extra_names(catalog):
                continue
            yield transport_url, manifest, catalog
