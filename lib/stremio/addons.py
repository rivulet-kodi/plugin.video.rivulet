# SPDX-FileCopyrightText: 2026 M0Rf30
# SPDX-License-Identifier: MIT

"""Stremio addon-protocol HTTP client (pure Python, no Kodi imports).

Implements the addon resource protocol exactly as stremio-core does it:
- URL shape & percent-encoding: src/addon_transport/http_transport/http_transport.rs,
  src/types/query_params_encode.rs, src/constants.rs (URI_COMPONENT_ENCODE_SET).
- Manifest resource/type/idPrefix matching: src/types/addon/manifest.rs
  (Manifest::is_resource_supported).
- Catalog declarations (modern `extra` array vs legacy extraSupported/
  extraRequired): src/types/addon/manifest.rs (ManifestCatalog, ManifestExtra).

`requests` is imported lazily (see `_ensure_requests()` below), not at
module scope, so this module - and all its pure helper functions
(encode_extra, build_resource_url, addon_supports, iter_catalogs,
validate_transport_url, safe_url_for_log) - stay importable, and cheap to
import, even where `requests` is missing; only constructing/using an
AddonClient actually needs it.
"""
import json
import threading
import weakref
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

#: `requests` costs ~35ms and ~200 transitive modules to import - resolved
#: lazily on first `AddonClient.__init__()` call via `_ensure_requests()`
#: rather than at module import time, so this module's pure helpers
#: (encode_extra, build_resource_url, addon_supports, iter_catalogs,
#: validate_transport_url, safe_url_for_log) - most of which run on every
#: UI navigation, HTTP or not - never pay that cost. `_UNSET` marks "not
#: resolved yet" (distinct from a real `None`, the cached "package not
#: installed" outcome).
_UNSET = object()
requests = _UNSET


def _ensure_requests():
    """Resolve and cache the `requests` module on first call, matching the
    previous eager `try: import requests / except ImportError: requests =
    None` at module scope. Safe to call repeatedly; only imports once."""
    global requests
    if requests is _UNSET:
        try:
            import requests as _requests
        except ImportError:  # pragma: no cover - exercised only without the dependency
            _requests = None
        requests = _requests
    return requests


#: stremio-core's URI_COMPONENT_ENCODE_SET (constants.rs): percent-encode
#: everything except alphanumerics and these characters.
EXTRA_SAFE_CHARS = "-_.!~*'()"

#: Addons are requested by replacing this manifest.json suffix with the
#: resource path (http_transport.rs); build_resource_url() strips it from a
#: transport_url so callers can pass either form as `base`.
MANIFEST_SUFFIX = '/manifest.json'

#: User-Agent every addon-protocol request carries. `requests`' default
#: ("python-requests/X.Y.Z") is refused outright by some addons' CDN:
#: Torrentio sits behind Cloudflare, which answers that UA with a 403 HTML
#: challenge page on both /manifest.json and /stream/ while serving the
#: very same request fine under any other UA. The 403 was only logged as
#: a per-addon failure, so the user just saw "no streams found" beside a
#: working Cinemeta catalog (issue #57). Same value `lib.serverbin` sends,
#: duplicated rather than imported to keep this module's import cheap.
USER_AGENT = 'plugin.video.rivulet'

#: Cap on any single addon-protocol HTTP response body. `_get_json()`
#: streams the response via `iter_content()` instead of buffering
#: `resp.json()` directly: a malicious/misbehaving third-party addon can
#: serve a catalog or manifest of unbounded size, and unpacking it whole
#: OOM-kills this addon's process on the ~1 GB-RAM ARM Kodi boxes (e.g. the
#: Xiaomi Mi Box) this addon targets. 8 MiB is far above any legitimate
#: manifest/catalog/meta/stream payload while staying well under what
#: starts pressuring a 1 GB device.
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024


def _encode_component(value):
    """Percent-encode one path/extra component, matching URI_COMPONENT_ENCODE_SET.

    ``errors='replace'``: an id or extra value comes from an addon's JSON, and
    a JSON string holding half of a surrogate pair decodes to a lone
    surrogate without complaint (``json.loads`` accepts it). ``quote()``
    then raised ``UnicodeEncodeError`` ("surrogates not allowed") from every
    request built for that title, an exception type no ``except
    AddonError`` site expects. The surrogate becomes ``%3F`` instead; no addon can serve
    an id that is not valid UTF-8 anyway, so nothing that worked is lost.
    """
    return quote(str(value), safe=EXTRA_SAFE_CHARS, errors='replace')


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
    """Raised when an addon HTTP request fails or returns malformed JSON.

    `category` is an optional, safe-by-construction detail alongside the
    (possibly unsafe) `message`: it MUST only ever be a value produced by
    `_request_error_category()`/`error_meta_category()` or a fixed
    literal such as 'invalid JSON' - never raw exception text, never a
    url - so log sites can surface it via `addon_error_detail()` without
    repeating the credential/path/query leakage `message` may carry
    (raise sites elsewhere in the codebase build `message` from
    arbitrary, unsanitized text).
    """

    def __init__(self, message, category=None):
        super().__init__(message)
        self.category = category



def validate_transport_url(url):
    """Validate an addon transport/resource URL before any HTTP request.

    HTTPS is accepted for any host. Plaintext HTTP is accepted only for
    "localhost" or a loopback/private/link-local IP literal (local addon
    development); every other HTTP host is rejected. Also rejects
    credentials, a missing host, a fragment, malformed ports, and any
    scheme other than http(s).

    Returns a normalized `scheme://host[:port]path[?query]` (lowercased
    scheme/host, no fragment) on success. Raises AddonError otherwise,
    with a message that never repeats the URL (safe to log verbatim).
    """
    try:
        parts = urlsplit(url)
        hostname = parts.hostname
        port = parts.port
    except ValueError:
        raise AddonError('invalid addon URL')

    scheme = (parts.scheme or '').lower()
    if scheme not in ('http', 'https'):
        raise AddonError('unsupported addon URL scheme')
    if parts.username or parts.password:
        raise AddonError('addon URL must not contain credentials')
    if parts.fragment:
        raise AddonError('addon URL must not contain a fragment')
    if not hostname:
        raise AddonError('addon URL is missing a host')

    hostname = hostname.lower()
    if scheme == 'http' and not _is_local_host(hostname):
        raise AddonError('plaintext HTTP is only allowed for local addon hosts')

    return urlunsplit((scheme, _format_netloc(hostname, port), parts.path, parts.query, ''))


def _is_local_host(hostname):
    """Whether (lowercased) `hostname` is safe for plaintext HTTP: the
    literal name "localhost", or an IP literal that's loopback, private
    (RFC 1918/4193), or link-local (RFC 3927/4291) - local addon
    development, never a plaintext request to a public host.

    `ipaddress` is imported here rather than at module scope: it costs
    ~0.8ms to import and this module is pulled in by every navigation
    for its pure helpers (`addon_supports`, `iter_catalogs`), while this
    function is only reached when validating a plaintext-`http` URL."""
    import ipaddress

    if hostname == 'localhost':
        return True
    try:
        addr = ipaddress.ip_address(hostname)
    except ValueError:
        return False
    return addr.is_loopback or addr.is_private or addr.is_link_local


def _format_netloc(hostname, port):
    """Rebuild a `netloc` from a bare (lowercased) hostname/port, re-adding
    the brackets `urlsplit().hostname` strips from IPv6 literals."""
    host = '[%s]' % hostname if ':' in hostname else hostname
    return host if port is None else '%s:%d' % (host, port)


def safe_url_for_log(url):
    """Return `scheme://hostname[:port]` for safely logging/displaying
    `url` - never userinfo, path, query, or fragment - or the sentinel
    `<invalid-url>` if it can't be parsed that far. Never raises."""
    try:
        parts = urlsplit(url)
        hostname = parts.hostname
        port = parts.port
    except ValueError:
        return '<invalid-url>'
    scheme = (parts.scheme or '').lower()
    if not scheme or not hostname:
        return '<invalid-url>'
    return '%s://%s' % (scheme, _format_netloc(hostname.lower(), port))


def _request_error_category(exc):
    """A short, safe-to-log category for a failed `requests` call - the
    HTTP status if the server responded, else the exception's class name
    (never `str(exc)`, which for `requests` often repeats the full URL,
    query string included)."""
    response = getattr(exc, 'response', None)
    status_code = getattr(response, 'status_code', None)
    if status_code is not None:
        return 'HTTP %s' % status_code
    return type(exc).__name__


def addon_error_detail(exc):
    """A safe, log-ready label for any exception raised while talking to
    an addon: the exception's TYPE plus, if present, the safe `category`
    an `AddonError` may carry - never `str(exc)`, which for third-party
    exceptions (or legacy/unsanitized `AddonError` raise sites) may embed
    credentials, a path, or a query string. Log sites MUST call this
    instead of `type(exc).__name__` so a bare "AddonError" becomes
    something actionable like "AddonError (HTTP 404)" whenever the
    category is available, while staying exactly as safe when it isn't."""
    category = getattr(exc, 'category', None)
    if category:
        return '%s (%s)' % (type(exc).__name__, category)
    return type(exc).__name__


#: Id prefix of the synthetic "meta" AIOStreams answers a failing
#: catalog/meta request with (`StremioTransformer.createErrorMeta` in
#: Viren070/AIOStreams: `id: 'aiostreamserror.' + encodeURIComponent(
#: JSON.stringify({errorTitle, errorDescription}))`, `name: errorTitle`,
#: `description: errorDescription`, `type: 'movie'`). It is returned
#: with HTTP 200 inside an otherwise ordinary `{"metas": [...]}`
#: envelope - alone when the upstream catalog failed outright, appended
#: after the real entries when only some sources did (unless the
#: instance owner set `hideErrors`) - so `raise_for_status()` never sees
#: it and, unrecognised, it is rendered as a title called "[X] AIOStreams".
#:
#: Measured against a public instance that had started throttling: every
#: search catalog answered `{"metas": [<this placeholder, "404 - Not
#: Found">]}`. All of them carry the SAME id, so `searchwindow._dedupe()`
#: folded the lot into a single entry and the coverflow opened on "1
#: results" with nothing in the log - no request had failed as far as the
#: client could tell. The catalog picker showed the same one-tile
#: coverflow for every catalog the instance could not serve.
ERROR_META_ID_PREFIX = 'aiostreamserror.'


def is_error_meta(meta):
    """Whether `meta` is an addon-synthesised error placeholder (see
    `ERROR_META_ID_PREFIX`) rather than a real title. Never raises: a
    non-dict or id-less entry is simply not one."""
    if not isinstance(meta, dict):
        return False
    meta_id = meta.get('id')
    return isinstance(meta_id, str) and meta_id.startswith(ERROR_META_ID_PREFIX)


def error_meta_category(meta):
    """A safe-by-construction `AddonError.category` for an error
    placeholder: the fixed literal `'addon reported error'`, plus the
    HTTP status the addon quotes at the start of its own description
    (`'404 - Not Found'`) when there is one. Only that three-digit
    number is ever copied out - the rest of the description is free text
    from a third party and may repeat a URL or credential, so it never
    reaches a log line (see `AddonError`'s `category` contract)."""
    text = str(meta.get('description') or '').strip() if isinstance(meta, dict) else ''
    code = text[:3]
    if code.isascii() and code.isdigit() and not text[3:4].isalnum():
        return 'addon reported error (HTTP %s)' % code
    return 'addon reported error'


_MAX_REDIRECTS = 5
_REDIRECT_STATUSES = (301, 302, 303, 307, 308)


def get_validated(session, url, timeout, stream=False):
    """`session.get(url)` that re-runs `validate_transport_url` on every
    redirect hop.

    `requests` follows redirects on its own, so the https-or-local-http
    rule would otherwise bind only the first URL: an https addon could
    302 to plaintext `http://public-host/...` and the body would be
    accepted. Raises AddonError for a rejected hop or a redirect loop;
    `requests` errors propagate for the caller's own handling.
    """
    url = validate_transport_url(url)
    for _ in range(_MAX_REDIRECTS + 1):
        resp = session.get(url, timeout=timeout, stream=stream, allow_redirects=False)
        if resp.status_code not in _REDIRECT_STATUSES:
            return resp
        location = resp.headers.get('Location')
        resp.close()
        if not location or not isinstance(location, str):
            raise AddonError('addon redirect without a target', category='bad redirect')
        try:
            url = validate_transport_url(urljoin(url, location))
        except AddonError:
            raise AddonError('addon redirected to a disallowed URL', category='bad redirect')
    raise AddonError('too many addon redirects', category='bad redirect')


def fetch_json(session, timeout, url):
    """GET `url` and JSON-decode the body, capped at `_MAX_RESPONSE_BYTES`.

    Streams via `iter_content()` and accumulates chunks itself rather
    than calling `resp.json()`/`resp.content` directly: those buffer
    the entire body in memory regardless of size, and a hostile or
    just-huge third-party addon response used to OOM-kill this
    addon's process on ~1 GB-RAM ARM Kodi boxes before any JSON
    parsing even started. The `Content-Length` header is checked
    first as a cheap short-circuit, but the chunk loop enforces the
    cap unconditionally since a chunked/absent-length response can
    still grow unbounded.
    """
    url = validate_transport_url(url)
    safe = safe_url_for_log(url)
    resp = None
    try:
        resp = get_validated(session, url, timeout, stream=True)
        resp.raise_for_status()
    except requests.RequestException as exc:
        category = _request_error_category(exc)
        if resp is not None:
            resp.close()
        raise AddonError('GET %s failed: %s' % (safe, category), category=category)
    try:
        content_length = resp.headers.get('Content-Length')
        if content_length is not None:
            try:
                too_big = int(content_length) > _MAX_RESPONSE_BYTES
            except (TypeError, ValueError):
                too_big = False
            if too_big:
                raise AddonError(
                    'GET %s response exceeds the %d byte limit' % (safe, _MAX_RESPONSE_BYTES),
                    category='response too large',
                )
        body = bytearray()
        for chunk in resp.iter_content(chunk_size=65536):
            if not chunk:
                continue
            body += chunk
            if len(body) > _MAX_RESPONSE_BYTES:
                raise AddonError(
                    'GET %s response exceeds the %d byte limit' % (safe, _MAX_RESPONSE_BYTES),
                    category='response too large',
                )
        try:
            return json.loads(bytes(body))
        except (ValueError, RecursionError):
            # RecursionError: a few MiB of '[' stays under the size cap
            # yet overflows the JSON parser's stack.
            raise AddonError('GET %s returned invalid JSON' % safe, category='invalid JSON')
    except requests.RequestException as exc:
        # Raised by `iter_content()` while the body is still arriving
        # (a read timeout after the headers landed, a reset, a
        # truncated chunked body). `requests` only wraps those for
        # the `get()`/`raise_for_status()` pair above, so without
        # this branch a stalled-mid-body addon escaped as a bare
        # `requests.ConnectionError`: no `except AddonError` site
        # (search, the catalog picker, `views._fetch_meta()`'s worker
        # thread) caught it, and the first uncaught one aborted the
        # whole search with a traceback instead of costing one addon.
        category = _request_error_category(exc)
        raise AddonError('GET %s failed mid-stream: %s' % (safe, category), category=category)
    finally:
        resp.close()


class AddonClient:
    """Thin HTTP client for the addon manifest/catalog/meta/stream/subtitles
    resources. One `requests.Session()` per client instance (stored as
    `.session`, not private, so callers/tests can substitute it directly).

    `requests.Session` is not thread-safe. Fan-out workers used to share
    this one session, and a response body read on one thread could be
    observed as an empty result on another - the intermittent "No streams
    found" that a second attempt then filled in. The constructing thread
    keeps `.session` (a test that replaces it still records every call
    made on that object). Every other thread gets its own session, created
    on first use and closed when that thread drops it.
    """

    def __init__(self, timeout=15):
        if _ensure_requests() is None:
            raise AddonError('the "requests" package is required for AddonClient')
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers['User-Agent'] = USER_AGENT
        # Identity of the session we created. A caller that assigns a
        # different object to `.session` (every test fake does) wants that
        # object used from every thread; only the untouched production
        # session is unsafe to share.
        self._owned_session = self.session
        self._owner_ident = threading.get_ident()
        self._worker_sessions = threading.local()

    def _active_session(self):
        """The `requests.Session` the current thread may use.

        The owner thread, and any thread when `.session` has been
        substituted, uses `.session`. A worker thread against the session
        this client constructed gets a private one, copying the headers
        (the User-Agent) already set on the owned session.
        """
        if threading.get_ident() == self._owner_ident or self.session is not self._owned_session:
            return self.session
        session = getattr(self._worker_sessions, 'session', None)
        if session is None:
            session = requests.Session()
            session.headers.update(self._owned_session.headers)
            # Pool threads and abandoned daemon workers never call
            # `_release_worker_session()`. Closing on collection is what
            # keeps an unclosed socket from becoming a `ResourceWarning`
            # (this suite treats those as errors).
            weakref.finalize(session, session.close)
            self._worker_sessions.session = session
        return session

    def _release_worker_session(self):
        """Close this thread's private session, if it made one. The owner
        thread's `.session` is left open - the client owns that for the
        process. A no-op when this thread never called `_active_session()`
        or when it is using a substituted `.session`."""
        session = getattr(self._worker_sessions, 'session', None)
        if session is None:
            return
        self._worker_sessions.session = None
        try:
            session.close()
        except Exception:  # noqa: BLE001 - a close failure must not kill a worker that is already done
            pass

    def _get_json(self, url):
        """GET `url` and JSON-decode it; see `fetch_json()`."""
        return fetch_json(self._active_session(), self.timeout, url)

    def _get_field(self, url, field, kind):
        """GET `url`, then extract `field` from the JSON envelope, enforcing
        the shape catalog/meta/streams/subtitles callers rely on.

        Mirrors `fetch_addon_catalog()`'s envelope check in
        addoncatalogs.py: a non-`dict` envelope, or a field whose type
        doesn't match `kind` ('list' or 'meta'), used to raise
        `AttributeError`/silently cache a truthy non-dict `meta` instead
        of the documented `AddonError` contract, because `.get()` was
        called directly on whatever `_get_json()` returned. `kind='list'`
        treats a missing/`None` field as `[]`; `kind='meta'` treats a
        missing/`None` field as `None`.
        """
        safe = safe_url_for_log(url)
        envelope = self._get_json(url)
        if not isinstance(envelope, dict):
            raise AddonError(
                'GET %s returned a malformed %s envelope' % (safe, field), category='invalid JSON'
            )
        value = envelope.get(field)
        if kind == 'list':
            if value is None:
                return []
            if not isinstance(value, list):
                raise AddonError(
                    'GET %s returned a malformed %s envelope' % (safe, field),
                    category='invalid JSON',
                )
            return value
        if value is not None and not isinstance(value, dict):
            raise AddonError(
                'GET %s returned a malformed %s envelope' % (safe, field), category='invalid JSON'
            )
        return value

    def manifest(self, transport_url):
        """GET the addon manifest (transport_url normally ends in /manifest.json).

        Requires a dict with a string `id`: a persisted manifest of any
        other shape would crash every later browse, so it is rejected
        here, at the one place third-party JSON enters the store.
        """
        data = self._get_json(transport_url)
        if not isinstance(data, dict) or not isinstance(data.get('id'), str):
            raise AddonError(
                'GET %s returned a malformed manifest' % safe_url_for_log(transport_url),
                category='invalid JSON',
            )
        return data

    def catalog(self, base, rtype, cid, extra=None):
        """GET a catalog resource -> list of meta preview objects (resp['metas']).

        Addon-synthesised error placeholders (`is_error_meta()`) never
        reach the caller as titles. When a catalog ALSO carries real
        entries they are simply dropped; when the placeholder is ALL
        the addon sent, the request is a failure like any other and
        raises `AddonError` (category from `error_meta_category()`), so
        every `except AddonError` site - search, the catalog picker,
        discover links - reports it instead of showing "[X] AIOStreams"
        as the one result of a 1-title coverflow."""
        url = build_resource_url(base, 'catalog', rtype, cid, extra)
        metas = self._get_field(url, 'metas', 'list')
        real = [meta for meta in metas if not is_error_meta(meta)]
        if len(real) == len(metas):
            return default_meta_types(metas, rtype)
        if real:
            return default_meta_types(real, rtype)
        raise AddonError(
            'GET %s answered with an error placeholder instead of a catalog' % safe_url_for_log(url),
            category=error_meta_category(metas[0]),
        )

    def meta(self, base, rtype, mid):
        """GET a meta resource -> the meta object (resp['meta']).

        An addon-synthesised error placeholder (`is_error_meta()`) raises
        `AddonError` rather than being returned as the title:
        `views._fetch_meta()` takes the first usable answer across addons,
        so a placeholder from a throttled aggregator would otherwise
        outrank a perfectly good meta from a later addon and open the
        detail screen on "[X] AIOStreams"."""
        url = build_resource_url(base, 'meta', rtype, mid)
        meta = self._get_field(url, 'meta', 'meta')
        if is_error_meta(meta):
            raise AddonError(
                'GET %s answered with an error placeholder instead of a meta' % safe_url_for_log(url),
                category=error_meta_category(meta),
            )
        return meta

    def streams(self, base, rtype, sid):
        """GET a stream resource -> list of Stream objects (resp['streams'])."""
        url = build_resource_url(base, 'stream', rtype, sid)
        return self._get_field(url, 'streams', 'list')

    def subtitles(self, base, rtype, sid, extra=None):
        """GET a subtitles resource -> list of subtitle objects (resp['subtitles'])."""
        url = build_resource_url(base, 'subtitles', rtype, sid, extra)
        return self._get_field(url, 'subtitles', 'list')


def _resource_entry(manifest, resource):
    """Return manifest['resources'] entry (short string or long-form dict)
    named `resource`, or None if the addon doesn't declare it at all."""
    resources = manifest.get('resources')
    for entry in resources if isinstance(resources, list) else ():
        if isinstance(entry, str):
            name = entry
        elif isinstance(entry, dict):
            name = entry.get('name')
        else:
            continue
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

    if not isinstance(types, list):
        types = []
    if not isinstance(id_prefixes, list):
        id_prefixes = []
    if rtype is not None and rtype not in types:
        return False
    if (
        rid is not None
        and id_prefixes
        and not any(isinstance(p, str) and rid.startswith(p) for p in id_prefixes)
    ):
        return False
    return True


def _catalog_extra_names(catalog):
    """Extra prop names a catalog declares, from either the modern
    `extra: [{name, isRequired, ...}]` array or the legacy
    `extraSupported`/`extraRequired` string-array form (both are valid
    ManifestExtra encodings per manifest.rs)."""
    extra = catalog.get('extra')
    if isinstance(extra, list):
        return {p['name'] for p in extra if isinstance(p, dict) and isinstance(p.get('name'), str) and p['name']}
    names = set()
    for key in ('extraSupported', 'extraRequired'):
        values = catalog.get(key)
        if isinstance(values, list):
            names.update(n for n in values if isinstance(n, str))
    return names


def catalog_supports_extra(catalog, name):
    """Whether `catalog` declares extra prop `name` at all (required or
    not), across both the modern `extra` array and the legacy
    `extraSupported`/`extraRequired` forms - the same plain existence
    check `iter_catalogs(extra_required=...)` applies, exposed for
    callers that hold a single catalog rather than iterating.

    Used to decide whether a catalog can be paged with `skip` (see
    `lib.ui.views.iter_catalog_pages`). Never raises: a
    missing/non-dict catalog, or a non-list `extra`, returns False."""
    if not isinstance(catalog, dict):
        return False
    return name in _catalog_extra_names(catalog)


def catalog_required_extra_names(catalog):
    """Extra prop names a catalog marks REQUIRED, from either the modern
    `extra: [{name, isRequired, ...}]` array (ManifestExtra.is_required in
    manifest.rs) or the legacy `extraRequired` string-array form. Unlike
    `_catalog_extra_names()`, both forms are unioned rather than one
    taking precedence - a catalog may declare `isRequired` entries in
    `extra` and still carry a legacy `extraRequired` list, and either one
    marking a prop required is enough to make it required.

    Never raises: returns an empty set for a missing/non-dict catalog, a
    non-list `extra`/`extraRequired`, non-dict entries, or empty/None
    names."""
    if not isinstance(catalog, dict):
        return set()
    names = set()
    extra = catalog.get('extra')
    if isinstance(extra, list):
        names.update(
            p['name']
            for p in extra
            if isinstance(p, dict) and p.get('isRequired') and isinstance(p.get('name'), str) and p['name']
        )
    extra_required = catalog.get('extraRequired')
    if isinstance(extra_required, list):
        names.update(n for n in extra_required if isinstance(n, str) and n)
    return names


def catalog_extra_options(catalog, name):
    """Declared option values for extra prop `name` on `catalog`, from the
    modern `extra: [{name, options, ...}]` array (ManifestExtra.options in
    manifest.rs), falling back to the legacy sibling array Cinemeta also
    ships for `genre` (a top-level `catalog['genres']` list carrying the
    same values `extra[].options` would). Only that one `name + 's'`
    lookup is supported - it is a documented Cinemeta quirk, not a general
    pluralization rule.

    Returns [] when `catalog` is missing/not a dict, the extra isn't
    declared, it has no options, or `options` isn't a list. Order is
    preserved (year options arrive newest-first) with duplicates and
    empty/None entries dropped; non-string scalars are stringified.
    """
    if not isinstance(catalog, dict):
        return []

    options = None
    extra = catalog.get('extra')
    if isinstance(extra, list):
        for prop in extra:
            if isinstance(prop, dict) and prop.get('name') == name:
                candidate = prop.get('options')
                if isinstance(candidate, list):
                    options = candidate
                break

    if options is None and name == 'genre':
        candidate = catalog.get('genres')
        if isinstance(candidate, list):
            options = candidate

    if not options:
        return []

    seen = set()
    result = []
    for value in options:
        if value is None:
            continue
        text = value if isinstance(value, str) else str(value)
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def iter_manifests(addons):
    """Yield `(transport_url, manifest)` for every installed-addon
    descriptor (`lib.store.Store.get_addons()` shape:
    `{"transportUrl": ..., "manifest": {...}}`) that is a dict with a dict
    manifest, skipping anything else. `addons` may be None.

    The store file and account sync can both hand back malformed entries,
    and every walk over the installed addons (catalogs here, addon
    catalogs, subtitles) must survive them the same way. `transport_url`
    is passed through unchecked; callers that need one test it."""
    for descriptor in addons or []:
        if not isinstance(descriptor, dict):
            continue
        manifest = descriptor.get('manifest')
        if isinstance(manifest, dict):
            yield descriptor.get('transportUrl'), manifest


def iter_catalogs(addons, extra_required=None, types=None):
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

    When `types` is given (a set/sequence of Stremio content types, e.g.
    `{"movie"}`), only catalogs whose own `type` is in it are yielded.
    The type strings are addon-authored and unconstrained by the
    protocol, so this is a plain membership test - see
    `lib.ui.homewindow`'s row definitions for how the ones Rivulet
    surfaces are grouped.
    """
    wanted = set(types) if types is not None else None
    for transport_url, manifest in iter_manifests(addons):
        catalogs = manifest.get('catalogs')
        for catalog in catalogs if isinstance(catalogs, list) else ():
            if not isinstance(catalog, dict):
                continue
            if extra_required and extra_required not in _catalog_extra_names(catalog):
                continue
            if wanted is not None:
                ctype = catalog.get('type')
                if not isinstance(ctype, str) or ctype not in wanted:
                    continue
            yield transport_url, manifest, catalog


def default_meta_types(metas, ctype):
    """Give each preview without a ``type`` the catalog's own type.

    Provider lists (Netflix, Disney+, Hulu, HBO...) can leave ``type``
    off their previews. The catalog browser already opens a pick as
    ``selected.get('type') or ctype``; filling it in where the previews
    arrive gives every other reader the same answer (the coverflow's
    direct-to-streams path for a movie checks ``type == 'movie'``).

    Returns a new list; a preview that gets a type is a shallow copy, so
    the dicts passed in are never changed. `AddonClient.catalog()` only
    ever hands it freshly decoded JSON that nothing else holds (the
    client keeps no response cache), so this is defensive: a later
    caller that passes shared or cached dicts stays safe too.
    """
    if not isinstance(ctype, str) or not ctype:
        return metas
    return [
        dict(meta, type=ctype) if isinstance(meta, dict) and not meta.get('type') else meta
        for meta in metas or ()
    ]


def _base_type(catalog_type):
    """Reduce a Stremio catalog type to the key `lib.ui.homewindow`'s type
    rows match on: everything before the first '.', lowercased. Addons
    follow the convention of a dotted subtype (e.g. `anime.movie`/
    `anime.series`) to specialize a base type, and type strings are
    otherwise free-form and mixed-case - this is what lets `anime.movie`
    join the Anime row and a stray `TV` join Series alongside `tv`."""
    if not isinstance(catalog_type, str):
        return ''
    return catalog_type.split('.', 1)[0].lower()
