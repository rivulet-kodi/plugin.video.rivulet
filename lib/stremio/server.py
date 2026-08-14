"""stremio-server-go client (pure Python, no Kodi imports).

Turns Stream protocol objects (stremio-protocol-spec.md #3, stremio-core
src/types/resource/stream.rs StreamSource) into URLs playable against a
local stremio-server-go instance, mirroring stremio-core's Stream::convert
(stream.rs:234-604) for every source kind that endpoint recognizes:

- Torrent (infoHash/fileIdx/announce-or-sources) -> GET {server}/{infoHash}/{fileIdx}
  with repeated `tr=` tracker query params. Confirmed against server-go's
  internal/api/api.go (`trackers := q["tr"]`) and docs/swagger.yaml's
  `/{infoHash}/{fileIdx}` streaming endpoint. `infoHash` accepts either
  the 40-char hex `hex::encode` form or a magnet's 32-char RFC 4648
  Base32 form (upstream e200aff9); trackers are percent-decoded and
  deduped before becoming `tr=` params (`normalize_peer_search_sources`,
  streaming_server/request.rs:86-98, upstream 87b65391/bc5aa9d8) - see
  `normalize_info_hash`/`normalize_trackers` below.
- YouTube (ytId) -> GET {server}/yt/{ytId} (swagger `/yt/{id}`).
- Plain url: validated against `_DIRECT_URL_SCHEMES` and returned
  unchanged when allowed (see that constant for the exact scheme
  list); a `magnet:` url is parsed for `xt=urn:btih:` + `tr=` params
  and converted the same way as a Torrent source, since a bare magnet
  link needs the server to fetch it; a bare `ftp://`/`ftps://` url is
  proxied through `/ftp/{filename}` (see `_ftp_create_url` below).
  Anything else (a Kodi control/local scheme like `plugin:`/`script:`/
  `special:`/`file:`, an empty/relative/malformed url, or any scheme
  outside the allowlist) raises `UnsupportedStreamError` - a Stream
  dict is untrusted addon data, so its `url` field must be validated
  before it ever reaches Kodi's player.
- Archive sources (rar/zip/7zip/tgz/tar) and Nzb -> GET
  {server}/{kind}/create?lz=<payload>, and a nested ftp(s) member url ->
  GET {server}/ftp/{filename}?lz=<payload>, where <payload> is
  `lib.stremio.lzstring.compress_to_encoded_uri_component()` of the
  request JSON body - mirroring stream.rs's Rar/Zip/Zip7/Tgz/Tar/Nzb
  `StreamSource::convert()` branches (stream.rs:240-437) and
  `ftp_url_handler` (stream.rs:186-214). See `_archive_create_url`,
  `_resolve_nzb_stream` and `_ftp_create_url` below.
- `externalUrl`/`playerFrameUrl` (`StreamSource::External`/`PlayerFrame`,
  stream.rs:818-835) raise `UnsupportedStreamError`: stremio-core
  recognizes these, but neither is a URL Kodi's player can ever open (an
  external app deep-link and an embeddable iframe player, respectively)
  - distinct from returning None ("we don't understand this stream" /
  "missing data needed to build one"), so callers can show an honest,
  specific message instead of the generic failure one.

iter_front() is the client's readiness probe for torrents: it streams the
FIRST bytes of the file (offset 0, Range GET) rather than trusting
aggregate download progress. Verified live against stremio-server-go
v0.8.5: a torrent's *aggregate* per-file stats (buffered_bytes()) can
report megabytes downloaded while the front of the file - where ffmpeg's
container-header probe reads from - is still completely unavailable
(pieces download out of order), causing Kodi's player to fail with
CURLE_PARTIAL_FILE / "error probing input format" even though pre-buffer
"succeeded" by the old aggregate-byte-count metric. A front Range read
both DRIVES the server's front-prioritization (NewReader ->
primeBoundary/warmMoov, internal/engine/engine.go:766-813) and PROVES
playback will actually start cleanly.

resolve_stream() is a pure URL builder - it does not check whether the
server is actually reachable. Callers should call is_available() first
(e.g. to show string 30031 "Streaming server unavailable") before relying
on a torrent/YouTube/magnet/archive/nzb/ftp URL it returns.
"""
import base64
import binascii
import json
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse

#: `requests` costs ~35ms and ~200 transitive modules to import - resolved
#: lazily on first `ServerClient.__init__()` call via `_ensure_requests()`
#: rather than at module import time, so importing this module's pure
#: helpers (normalize_info_hash, normalize_trackers, buffered_bytes,
#: guess_file_idx, ...) never pays that cost. `_UNSET` marks "not resolved
#: yet" (distinct from a real `None`, the cached "package not installed"
#: outcome) so a test directly assigning `requests = None` is honoured
#: without triggering a redundant re-import.
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


#: `compress_to_encoded_uri_component` (a 334-line pure-Python codec) is
#: imported lazily inside `_lz_query_url()` below, the single call site
#: shared by `_ftp_create_url`/`_archive_create_url`/`_resolve_nzb_stream` -
#: it is only ever needed on the rare archive/nzb/ftp resolve path, never
#: for a torrent/magnet/direct-url/YouTube stream.

#: Same percent-encoding safe set stremio-core uses for the /yt/{id} path
#: segment (URI_COMPONENT_ENCODE_SET, constants.rs).
_YT_SAFE_CHARS = "-_.!~*'()"

#: fileIdx sentinel meaning "not specified" - tells stremio-server-go to
#: auto-select the largest file in the torrent (stream.rs:
#: `file_idx.map_or_else(|| "-1".to_string(), ...)`). NOT 0.
UNKNOWN_FILE_IDX = -1

#: Lengths, in characters, of the two info-hash encodings a magnet or a
#: Stream object's `infoHash` may carry - `normalize_info_hash()` accepts
#: only these two lengths.
_HEX_HASH_LENGTH = 40
_BASE32_HASH_LENGTH = 32
#: A BitTorrent info hash is always a 160-bit SHA-1 digest.
_INFO_HASH_BYTES = 20

#: Archive `StreamSource` variants stremio-core recognizes (stream.rs:
#: 738-792), keyed by the Stream protocol field carrying their URL list,
#: mapped to the `/{kind}/create` path segment stremio-server-go exposes
#: for each.
_ARCHIVE_KIND_BY_KEY = {
    'rarUrls': 'rar',
    'zipUrls': 'zip',
    '7zipUrls': '7zip',
    'tgzUrls': 'tgz',
    'tarUrls': 'tar',
}

#: Schemes `resolve_stream()` accepts for a Stream's own `url` field once
#: `magnet:`/`ftp(s):` have been special-cased into a different URL
#: (see the module docstring) - the current, intended network-media
#: families Kodi's player can open directly. `_validate_direct_url()`
#: rejects everything else, including Kodi control/local schemes
#: (`plugin`, `script`, `special`, `file`, ...) a rogue addon could
#: otherwise smuggle into an untrusted Stream dict.
_DIRECT_URL_SCHEMES = frozenset({
    'http', 'https',
    'ftp', 'ftps',
    'smb',
    'nfs',
    'rtmp', 'rtmps',
    'rtsp',
    'rtp',
    'udp',
})


def normalize_info_hash(value):
    """Normalize a torrent info hash to the 40-char lowercase hex string
    stremio-server-go's URL paths require (`hex::encode`, stream.rs:545).

    Accepts the 40-char hex form OR a magnet's 32-char RFC 4648 Base32
    form (BEP 0003 permits either encoding for a magnet's `xt=urn:btih:`
    value; stremio-core normalizes on ingest, upstream commit e200aff9,
    2025-11-18: "support base32 encoded info hash for magnet links").
    Anything else - wrong length, or characters outside the relevant
    alphabet - can't be turned into a hash the server understands, so
    this returns None rather than handing garbage to a URL builder.
    """
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if len(candidate) == _HEX_HASH_LENGTH:
        try:
            binascii.unhexlify(candidate)
        except (binascii.Error, ValueError):
            return None
        return candidate.lower()
    if len(candidate) == _BASE32_HASH_LENGTH:
        try:
            raw = base64.b32decode(candidate.upper())
        except (binascii.Error, ValueError):
            return None
        if len(raw) != _INFO_HASH_BYTES:
            return None
        return binascii.hexlify(raw).decode('ascii')
    return None


def normalize_trackers(values):
    """Percent-decode, drop empty entries, and dedupe (order-preserving)
    a raw `announce`/`sources` tracker list before it becomes one or
    more `tr=` query params (mirrors `normalize_peer_search_sources`,
    stremio-core src/types/streaming_server/request.rs:86-98, upstream
    87b65391/bc5aa9d8): some addons ship a tracker/source entry that is
    itself percent-encoded (e.g. a `tracker:<percent-encoded-url>`
    wrapper), which must be decoded before stremio-server-go sees it.
    """
    seen = set()
    normalized = []
    for value in values or []:
        if not isinstance(value, str):
            continue
        decoded = unquote(value)
        if not decoded or decoded in seen:
            continue
        seen.add(decoded)
        normalized.append(decoded)
    return normalized


def _is_ftp_url(value):
    """True if `value` is a string with an `ftp://`/`ftps://` scheme -
    stremio-core proxies both the same way (`Stream::ftp_url_handler`
    matches scheme "ftp" or "ftps", stream.rs:187-188).
    """
    return isinstance(value, str) and (value.startswith('ftp://') or value.startswith('ftps://'))


def _ftp_filename(url):
    """Last non-empty path segment of `url`, or None if it has none -
    mirrors `Stream::ftp_filename` (stream.rs:169-176), which upstream
    uses as the `/ftp/{filename}` path segment. Upstream raises an
    `EnvError` when there's no path segment to use; this addon just
    treats that url as unusable.
    """
    segments = [segment for segment in urlparse(url).path.split('/') if segment]
    return segments[-1] if segments else None


def _validate_direct_url(url):
    """Return `url` unchanged if it is an absolute network-media URL
    whose scheme is in `_DIRECT_URL_SCHEMES`, else raise
    `UnsupportedStreamError`.

    Never rewrites `url` - only `_ftp_create_url()`/`_magnet_to_torrent_url()`
    (checked before this is ever called, see `resolve_stream()`) turn a
    Stream's own `url` field into a different URL. Rejects: non-strings,
    empty strings, relative urls (no scheme and/or no host - `urlparse`
    reports `netloc` empty for both), and any scheme outside the
    allowlist - in particular Kodi's own control/local schemes
    (`plugin:`, `script:`, `special:`, `file:`) that resolve to addon
    invocations or local-filesystem paths, not media, if handed to
    `xbmc.Player()`/`ListItem` unchecked.

    The raised message never embeds `url` itself (or any userinfo/path/
    query/fragment out of it) - a Stream dict is untrusted addon data,
    and this error's text reaches kodi.log (`lib.ui.player` logs `%r`
    of the exception) and a user-visible notification, neither of
    which should ever leak a credential/token/path a malicious or
    misconfigured addon embedded in a rejected url. Only the bare
    scheme (never sensitive on its own) is included when that's what
    failed; anything else (missing/malformed) gets a generic message.
    """
    parsed = urlparse(url) if isinstance(url, str) and url else None
    if parsed is None or not parsed.netloc:
        raise UnsupportedStreamError('Stream url is empty, relative, or malformed')
    scheme = parsed.scheme.lower()
    if scheme not in _DIRECT_URL_SCHEMES:
        raise UnsupportedStreamError('Stream url scheme %r is not an allowed direct-playback scheme' % (scheme,))
    return url


class ServerError(Exception):
    """Raised when a stremio-server-go engine/stats request fails.

    Mirrors AddonError/ApiError in this package: network failures and
    malformed JSON both surface as this one type so callers (e.g. the
    playback pre-buffer poller) can catch a single exception and fall
    back to "proceed without buffering" rather than bricking playback.
    """


class UnsupportedStreamError(ServerError):
    """Raised by `resolve_stream()` for a Stream source kind stremio-core
    itself recognizes but that can never be handed to Kodi's player:
    `externalUrl` (`StreamSource::External`, stream.rs:826-835 - meant
    to be opened by a native app or deep link, e.g. a torrent client or
    a platform store listing) and `playerFrameUrl`
    (`StreamSource::PlayerFrame`, stream.rs:818-821 - an embeddable
    IFRAME player, not a media URL at all); and, since a Stream dict is
    untrusted addon data, an empty/relative/malformed `url` field or one
    outside `_DIRECT_URL_SCHEMES` (a Kodi control/local scheme such as
    `plugin:`/`script:`/`special:`/`file:` would let a rogue addon run
    addon code or read local files via `xbmc.Player()`/`ListItem`
    instead of merely failing to play a video). Distinct from
    `resolve_stream()` returning None (which means "unrecognized
    source" or "missing data needed to build a URL") so a caller can
    show a precise, honest message instead of the generic "no playable
    stream found" one.
    """


class ServerClient:
    """Talks to a local stremio-server-go instance (default http://127.0.0.1:11470).

    One `requests.Session()` per client instance (stored as `.session`, not
    private, so callers/tests can substitute it directly - mirrors
    AddonClient in addons.py). Session creation is gated behind the same
    `requests is None` check the rest of this module uses for the optional
    dependency, so constructing a client without `requests` installed still
    doesn't crash; `.session` just stays `None`, which is_available() (and
    every other method below) already treats as "unavailable"/raises on.
    """

    def __init__(self, base_url):
        self.base_url = (base_url or '').rstrip('/')
        self.session = requests.Session() if _ensure_requests() is not None else None

    def is_available(self):
        """Probe the server: GET /settings, falling back to /stats.json.

        Both endpoints exist per docs/swagger.yaml. Uses a short timeout
        since this may run on every playback attempt; returns False on
        ANY error (connection refused, timeout, non-2xx, missing
        `requests`) rather than raising - unavailability is a normal,
        expected state (server disabled or still starting up).
        """
        if requests is None:
            return False
        for path in ('/settings', '/stats.json'):
            try:
                resp = self.session.get(self.base_url + path, timeout=2)
                if resp.ok:
                    return True
            except requests.RequestException:
                continue
        return False

    def torrent_url(self, info_hash, file_idx, announce=None):
        """Build `{base}/{infoHash}/{fileIdx}[?tr=...&tr=...]`, or None if
        `info_hash` can't be normalized to a usable hash.

        `info_hash` is normalized via `normalize_info_hash()` - accepting
        either the 40-char hex form or a magnet's 32-char Base32 form,
        always lowercased hex on output (stremio-core's `hex::encode` is
        always lowercase). `announce` is normalized via
        `normalize_trackers()` (percent-decoded, deduped) before being
        encoded the way Rust's `url::Url::query_pairs_mut()` encodes
        query params - application/x-www-form-urlencoded (space -> '+'),
        which is NOT the same percent-encoding scheme
        addons.encode_extra() uses for extra props.
        """
        normalized_hash = normalize_info_hash(info_hash)
        if normalized_hash is None:
            return None
        url = '%s/%s/%s' % (self.base_url, normalized_hash, file_idx)
        trackers = normalize_trackers(announce)
        if trackers:
            url += '?' + urlencode([('tr', tracker) for tracker in trackers])
        return url

    def _magnet_to_torrent_url(self, magnet):
        """Parse `magnet:?dn=...&xt=urn:btih:<hash>&tr=...` (the exact shape
        stremio-core's build_magnet_uri produces, stream.rs:1036-1071) back
        into a torrent_url(), or None if it has no usable btih info hash.
        """
        query = parse_qs(urlparse(magnet).query)
        info_hash = next(
            (xt.split(':', 2)[2] for xt in query.get('xt', []) if xt.lower().startswith('urn:btih:')),
            None,
        )
        if not info_hash:
            return None
        return self.torrent_url(info_hash, UNKNOWN_FILE_IDX, query.get('tr', []))

    def create_engine(self, info_hash, timeout=100):
        """GET `{base}/{infoHash}/create` - start/attach the torrent engine.

        Per stremio-server-go's handleCreate (internal/api/api.go:697-750,
        docs/swagger.yaml `/{infoHash}/create`), the server calls
        EnsureEngine + Ready() with a 90s timeout blocking until torrent
        metadata is available, then returns `types.Stats` including
        `guessedFileIdx` (set when the server picked a best file).
        Defaults to a 100s client timeout (above the server's 90s Ready()
        budget); callers polling in a cancellable UI loop pass a short
        timeout instead so a slow /create can't freeze the loop between
        cancel checks (each timeout just re-polls the same warming engine).
        """
        if requests is None:
            raise ServerError('the "requests" package is required for ServerClient')
        url = '%s/%s/create' % (self.base_url, str(info_hash).lower())
        try:
            resp = self.session.get(url, timeout=timeout)
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise ServerError('GET %s failed: %s' % (url, exc))
        try:
            return resp.json()
        except ValueError as exc:
            raise ServerError('GET %s returned invalid JSON: %s' % (url, exc))

    def file_stats(self, info_hash, file_idx):
        """GET `{base}/{infoHash}/{fileIdx}/stats.json` - per-file buffer stats.

        Per docs/swagger.yaml `/{infoHash}/{fileIdx}/stats.json` and
        writeStats (internal/api/api.go:818-825), returns `types.Stats`
        with the per-file extras (`streamProgress`, `streamLen`,
        `streamName`) populated in addition to the torrent-level fields
        (`downloadSpeed`, `peers`, ...). Requesting per-file stats also
        triggers ensureDownloading(idx) server-side, prioritizing this
        file's pieces. Uses a short 10s timeout since this is polled
        repeatedly during pre-buffer.
        """
        if requests is None:
            raise ServerError('the "requests" package is required for ServerClient')
        url = '%s/%s/%s/stats.json' % (self.base_url, str(info_hash).lower(), file_idx)
        try:
            resp = self.session.get(url, timeout=10)
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise ServerError('GET %s failed: %s' % (url, exc))
        try:
            return resp.json()
        except ValueError as exc:
            raise ServerError('GET %s returned invalid JSON: %s' % (url, exc))

    def iter_front(self, info_hash, file_idx, want_bytes, chunk_size=16384, timeout=60):
        """Stream the FRONT (offset 0) of a torrent file, yielding each
        chunk's length as it arrives - the pre-buffer readiness probe.

        Issues `GET {base}/{infoHash}/{fileIdx}` with a `Range:
        bytes=0-(want_bytes-1)` header (the same request shape Kodi's own
        player makes), streamed rather than buffered whole. A connection
        that closes after delivering SOME bytes (IncompleteRead /
        ChunkedEncodingError - the normal shape of a live, poorly-seeded
        Range read) is treated as a non-fatal end of this attempt, since
        partial front data is still meaningful; the caller re-issues a
        fresh request to keep trying. Only a request that fails with NO
        bytes received raises `ServerError`, matching this file's other
        methods.

        `chunk_size` defaults small (16 KiB), NOT large: `requests`'
        `iter_content()` does one `raw.read(chunk_size)` per chunk, and if
        the connection closes before a FULL chunk_size of bytes has
        arrived, `http.client` raises `IncompleteRead` for that WHOLE
        chunk before yielding anything - losing every byte read so far in
        it. Live-verified against a real mid-stream close: a 1 MiB
        chunk_size lost an entire ~1 MB of genuinely-received front data
        (reported as 0 bytes obtained) when the connection closed 8 KB
        short of that chunk boundary; shrinking to 16 KiB reduces a worst-
        case loss to a single small tail fragment instead of the whole
        read.
        """
        if requests is None:
            raise ServerError('the "requests" package is required for ServerClient')
        url = '%s/%s/%s' % (self.base_url, str(info_hash).lower(), file_idx)
        headers = {'Range': 'bytes=0-%d' % (want_bytes - 1)}
        try:
            resp = self.session.get(url, headers=headers, stream=True, timeout=timeout)
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise ServerError('GET %s failed: %s' % (url, exc))
        got = 0
        try:
            for chunk in resp.iter_content(chunk_size=chunk_size):
                if not chunk:
                    continue
                got += len(chunk)
                yield len(chunk)
                if got >= want_bytes:
                    break
        except requests.RequestException as exc:
            if got == 0:
                raise ServerError('GET %s failed mid-stream: %s' % (url, exc))
            return
        finally:
            try:
                resp.close()
            except Exception:  # noqa: BLE001 - closing must never mask the real outcome
                pass

    def _lz_query_url(self, path, body):
        """GET `{base}/{path}?lz=<payload>` - the shape shared by every
        stremio-server-go endpoint that takes a JSON body via a query
        string instead of a POST body: archive/nzb `.../create` and the
        ftp(s) proxy `/ftp/{filename}`. `<payload>` is
        `compress_to_encoded_uri_component()` of the compact-JSON
        `body`, exactly how stream.rs builds every one of these
        (`serde_json::to_string(&payload)` then `lz_str::
        compress_to_encoded_uri_component(&stream_data)`).
        """
        from lib.stremio.lzstring import compress_to_encoded_uri_component
        compressed = compress_to_encoded_uri_component(json.dumps(body, separators=(',', ':')))
        return '%s/%s?%s' % (self.base_url, path, urlencode({'lz': compressed}))

    def _ftp_create_url(self, url):
        """Rewrite an `ftp://`/`ftps://` url into the proxied
        `{base}/ftp/{filename}?lz=<payload>` form stremio-server-go
        actually fetches - Kodi (like most HTTP clients) can't open an
        `ftp://` URL directly (`Stream::ftp_url_handler`, stream.rs:
        186-214). `filename` is the url's last path segment
        (`Stream::ftp_filename`, stream.rs:169-176); returns None if
        there isn't one to use (mirrors upstream's
        `EnvError::Other("Ftp(s) filepath is missing in the url")`).
        """
        filename = _ftp_filename(url)
        if not filename:
            return None
        return self._lz_query_url('ftp/%s' % quote(filename, safe=''), {'ftpUrl': url})

    def _archive_create_url(self, kind, raw_urls, file_idx=None, file_must_include=None):
        """Build `{base}/{kind}/create?lz=<payload>` for an archive
        stream source (`kind` one of 'rar'/'zip'/'7zip'/'tgz'/'tar' -
        stream.rs's Rar/Zip/Zip7/Tgz/Tar `StreamSource::convert()`
        branches, stream.rs:240-403, identical but for the path
        segment).

        `raw_urls` is the Stream protocol's own wire shape for e.g.
        `rarUrls` - a list of `[url]`/`[url, bytes]` pairs
        (`ArchiveUrlShort`, stream.rs:885-899), passed straight through
        rather than converted to an `{"url":..., "bytes":...}` object:
        stream.rs's own doctest for `ArchiveUrl`'s `Serialize` impl
        (stream.rs:838-852) proves the OUTGOING `/…/create` request
        re-serializes each entry back to that same array form, since
        `ArchiveStreamBody.urls: Vec<ArchiveUrl>` and `ArchiveUrl`
        always (de)serializes via `ArchiveUrlShort` in both directions
        (`#[serde(from = ..., into = ...)]`).

        A nested `ftp://`/`ftps://` member url is itself rewritten
        through `/ftp/{filename}` first (`archive_urls_with_ftp_proxy`,
        stream.rs:216-230) - its `bytes` size (if any) is kept as given,
        only the url changes. Returns None if there are no usable
        entries left (mirrors upstream's `if urls.is_empty() { return
        Err(...) }`) or a nested ftp url can't be proxied (no filename
        to build `/ftp/{filename}` from).
        """
        entries = []
        for raw in raw_urls or []:
            if not raw or not isinstance(raw, (list, tuple)) or not raw[0]:
                continue
            url = raw[0]
            num_bytes = raw[1] if len(raw) > 1 else None
            if _is_ftp_url(url):
                url = self._ftp_create_url(url)
                if url is None:
                    return None
            entries.append([url] if num_bytes is None else [url, num_bytes])
        if not entries:
            return None

        body = {'urls': entries}
        if file_idx is not None:
            body['fileIdx'] = file_idx
        if file_must_include:
            body['fileMustInclude'] = list(file_must_include)
        return self._lz_query_url('%s/create' % kind, body)

    def _resolve_nzb_stream(self, stream):
        """Build `{base}/nzb/create?lz=<payload>` for an Nzb stream
        source (`StreamSource::Nzb`, stream.rs:793-804 - `convert()`
        branch stream.rs:404-437). Supports both the single-url
        `nzbUrl` field and the newer multi-url `nzbUrls` array (upstream
        cb2d5d0c, 2026-03-10); either or both may be present. `servers`
        (Usenet server URLs) are required - an Nzb stream with none can
        never be fetched, mirroring upstream's `if servers.is_empty() {
        return Err(...) }` - and, like every url here, are individually
        proxied through `/ftp/{filename}` first if they carry an
        `ftp://`/`ftps://` scheme.
        """
        servers = []
        for server_url in stream.get('servers') or []:
            if not isinstance(server_url, str) or not server_url:
                continue
            if _is_ftp_url(server_url):
                server_url = self._ftp_create_url(server_url)
                if server_url is None:
                    return None
            servers.append(server_url)
        if not servers:
            return None

        body = {'servers': servers}

        single_url = stream.get('nzbUrl')
        if single_url:
            if _is_ftp_url(single_url):
                single_url = self._ftp_create_url(single_url)
                if single_url is None:
                    return None
            body['nzbUrl'] = single_url

        multi_urls = []
        for nzb_url in stream.get('nzbUrls') or []:
            if not isinstance(nzb_url, str) or not nzb_url:
                continue
            if _is_ftp_url(nzb_url):
                nzb_url = self._ftp_create_url(nzb_url)
                if nzb_url is None:
                    return None
            multi_urls.append(nzb_url)
        if multi_urls:
            body['nzbUrls'] = multi_urls

        return self._lz_query_url('nzb/create', body)

    def resolve_stream(self, stream):
        """Resolve a Stream protocol dict to a playable URL, None, or
        raise `UnsupportedStreamError`.

        - `url`: validated by `_validate_direct_url()` (`_DIRECT_URL_SCHEMES`)
          and returned as-is when allowed, else raises
          `UnsupportedStreamError`; a `magnet:` url is converted via
          _magnet_to_torrent_url() when it carries a parseable info hash,
          else None (playing a bare magnet needs a torrent client, which
          the addon doesn't embed); a bare `ftp://`/`ftps://` url is
          proxied via `_ftp_create_url()`.
        - `infoHash` (+ `fileIdx`, `announce`/`sources`): -> torrent_url(),
          which normalizes `infoHash` (see `normalize_info_hash`) and
          returns None for one that can't be. Missing `fileIdx` defaults
          to UNKNOWN_FILE_IDX (-1), matching stremio-core, NOT 0.
          Trackers come from `announce`, falling back to `sources` when
          absent - stremio-core deserializes torrent trackers with
          `#[serde(alias = "sources")]` (stream.rs:812), and
          Torrentio/AIOStreams-style addons ship them under `sources`
          (e.g. "tracker:udp://host:port/announce", "dht:<hash>").
          stremio-server-go strips the "tracker:" prefix and ignores
          "dht:" entries itself (engine.go mergeTrackers), so forwarding
          raw sources entries as `tr=` is correct as-is.
        - `ytId`: -> `{base}/yt/{ytId}`.
        - `rarUrls`/`zipUrls`/`7zipUrls`/`tgzUrls`/`tarUrls` (+
          `fileIdx`/`fileMustInclude`): -> `_archive_create_url()`.
        - `nzbUrl`/`nzbUrls` (+ `servers`): -> `_resolve_nzb_stream()`.
        - `externalUrl`/`playerFrameUrl`: raises `UnsupportedStreamError`
          - stremio-core recognizes these StreamSource kinds, but
          neither is a URL Kodi's player can ever open.
        - anything else (no recognized key at all): None, unrecognized.
        """
        stream = stream or {}

        url = stream.get('url')
        if url:
            if isinstance(url, str) and url.startswith('magnet:'):
                return self._magnet_to_torrent_url(url)
            if _is_ftp_url(url):
                return self._ftp_create_url(url)
            return _validate_direct_url(url)

        info_hash = stream.get('infoHash')
        if info_hash:
            file_idx = stream.get('fileIdx')
            if file_idx is None:
                file_idx = UNKNOWN_FILE_IDX
            announce = stream.get('announce') or stream.get('sources') or []
            return self.torrent_url(info_hash, file_idx, announce)

        yt_id = stream.get('ytId')
        if yt_id:
            return '%s/yt/%s' % (self.base_url, quote(str(yt_id), safe=_YT_SAFE_CHARS))

        for key, kind in _ARCHIVE_KIND_BY_KEY.items():
            if stream.get(key):
                return self._archive_create_url(
                    kind, stream[key], stream.get('fileIdx'), stream.get('fileMustInclude')
                )

        if stream.get('nzbUrl') or stream.get('nzbUrls'):
            return self._resolve_nzb_stream(stream)

        if stream.get('externalUrl') or stream.get('playerFrameUrl'):
            unsupported_key = 'externalUrl' if stream.get('externalUrl') else 'playerFrameUrl'
            raise UnsupportedStreamError(
                'Stremio stream source %r can only be opened in the Stremio app' % unsupported_key
            )

        return None


def buffered_bytes(stats):
    """How many bytes of the current stream file are buffered so far.

    `stats` is a `types.Stats` dict as returned by `file_stats()`:
    `streamProgress` (float, 0-1, BytesCompleted/Length per
    internal/engine/engine.go:1124-1278) times `streamLen` (int, file
    size in bytes, swagger.yaml types.Stats.streamLen). Tolerates a
    missing/None/non-numeric `stats`, or missing/None fields, returning
    0 rather than raising - this feeds a UI progress poll that must
    never crash playback over a transient/incomplete stats payload.
    """
    if not isinstance(stats, dict):
        return 0
    progress = stats.get('streamProgress')
    length = stats.get('streamLen')
    if progress is None or length is None:
        return 0
    try:
        value = int(round(float(progress) * float(length)))
    except (TypeError, ValueError):
        return 0
    return max(0, value)


def guess_file_idx(stats):
    """Pick the torrent file index to pre-buffer/poll from a `/create`
    response, tolerating a server-version gap confirmed live against
    stremio-server-go v0.8.5 (Sintel torrent 08ada5a7a618...): that
    build's `/create` response NEVER carries `guessedFileIdx` (only a
    per-file `/{infoHash}/{fileIdx}/stats.json` response does, once a
    concrete index is already being polled) but DOES carry a `files`
    array once metadata resolves (`[{name, path, length, offset}, ...]`).

    Prefers an explicit non-negative int `guessedFileIdx` when present -
    server builds that still emit it up front win outright. Otherwise,
    when `files` is a non-empty list, picks the index of the entry with
    the largest `length` (ties keep the first/lowest index), the same
    "biggest file is the movie" heuristic the server used to apply
    itself. Returns None when neither is usable.

    Tolerates garbage input throughout rather than raising - this feeds
    a playback pre-buffer poll that must never crash on an unexpected
    server response shape: a non-dict `stats`, a missing/non-list
    `files`, and entries that are not dicts or have a missing/non-numeric
    `length` (treated as length 0) all fall through safely.
    """
    if not isinstance(stats, dict):
        return None

    guessed = stats.get('guessedFileIdx')
    if isinstance(guessed, int) and not isinstance(guessed, bool) and guessed >= 0:
        return guessed

    files = stats.get('files')
    if not isinstance(files, list) or not files:
        return None

    best_idx, best_length = None, -1
    for idx, entry in enumerate(files):
        length = entry.get('length') if isinstance(entry, dict) else None
        try:
            length = float(length)
        except (TypeError, ValueError):
            length = 0
        if length > best_length:
            best_idx, best_length = idx, length
    return best_idx
