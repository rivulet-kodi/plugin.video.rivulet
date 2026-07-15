"""stremio-server-go client (pure Python, no Kodi imports).

Turns Stream protocol objects (stremio-protocol-spec.md #3, stremio-core
src/types/resource/stream.rs StreamSource) into URLs playable against a
local stremio-server-go instance, mirroring stremio-core's Stream::convert
(stream.rs:234-604) for the sources this client supports:

- Torrent (infoHash/fileIdx/announce-or-sources) -> GET {server}/{infoHash}/{fileIdx}
  with repeated `tr=` tracker query params. Confirmed against server-go's
  internal/api/api.go (`trackers := q["tr"]`) and docs/swagger.yaml's
  `/{infoHash}/{fileIdx}` streaming endpoint.
- YouTube (ytId) -> GET {server}/yt/{ytId} (swagger `/yt/{id}`).
- Plain url (http/https) is returned unchanged; a `magnet:` url is parsed
  for `xt=urn:btih:` + `tr=` params and converted the same way as a
  Torrent source, since a bare magnet link needs the server to fetch it.

Archives (rar/zip/7zip/tar/tgz), Nzb and ftp(s) sources need stremio-core's
lz-string `/…/create` payload conversion, which is out of scope here;
callers see those as unresolved (None) rather than a guessed URL.

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
on a torrent/YouTube/magnet URL it returns.
"""
from urllib.parse import parse_qs, quote, urlencode, urlparse

try:
    import requests
except ImportError:  # pragma: no cover - exercised only without the dependency
    requests = None  # type: ignore[assignment]

#: Same percent-encoding safe set stremio-core uses for the /yt/{id} path
#: segment (URI_COMPONENT_ENCODE_SET, constants.rs).
_YT_SAFE_CHARS = "-_.!~*'()"

#: fileIdx sentinel meaning "not specified" - tells stremio-server-go to
#: auto-select the largest file in the torrent (stream.rs:
#: `file_idx.map_or_else(|| "-1".to_string(), ...)`). NOT 0.
UNKNOWN_FILE_IDX = -1


class ServerError(Exception):
    """Raised when a stremio-server-go engine/stats request fails.

    Mirrors AddonError/ApiError in this package: network failures and
    malformed JSON both surface as this one type so callers (e.g. the
    playback pre-buffer poller) can catch a single exception and fall
    back to "proceed without buffering" rather than bricking playback.
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
        self.session = requests.Session() if requests is not None else None

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
        """Build `{base}/{infoHash}/{fileIdx}[?tr=...&tr=...]`.

        `info_hash` is lower-cased (stremio-core's `hex::encode` is always
        lowercase). Tracker query params are encoded the way Rust's
        `url::Url::query_pairs_mut()` encodes them - application/x-www-
        form-urlencoded (space -> '+'), which is NOT the same
        percent-encoding scheme addons.encode_extra() uses for extra props.
        """
        url = '%s/%s/%s' % (self.base_url, str(info_hash).lower(), file_idx)
        if announce:
            url += '?' + urlencode([('tr', tracker) for tracker in announce])
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

    def resolve_stream(self, stream):
        """Resolve a Stream protocol dict to a playable URL, or None.

        - `url`: http(s)/other schemes are returned as-is; a `magnet:` url
          is converted via _magnet_to_torrent_url() when it carries a
          parseable info hash, else None (playing a bare magnet needs a
          torrent client, which the addon doesn't embed).
        - `infoHash` (+ `fileIdx`, `announce`/`sources`): -> torrent_url().
          Missing `fileIdx` defaults to UNKNOWN_FILE_IDX (-1), matching
          stremio-core, NOT 0. Trackers come from `announce`, falling back
          to `sources` when absent - stremio-core deserializes torrent
          trackers with `#[serde(alias = "sources")]` (stream.rs:812), and
          Torrentio/AIOStreams-style addons ship them under `sources`
          (e.g. "tracker:udp://host:port/announce", "dht:<hash>").
          stremio-server-go strips the "tracker:" prefix and ignores
          "dht:" entries itself (engine.go mergeTrackers), so forwarding
          raw sources entries as `tr=` is correct as-is.
        - `ytId`: -> `{base}/yt/{ytId}`.
        - `externalUrl`/`playerFrameUrl`/anything else (archives, nzb,
          ftp): -> None, unsupported without full stremio-core conversion.
        """
        stream = stream or {}

        url = stream.get('url')
        if url:
            if url.startswith('magnet:'):
                return self._magnet_to_torrent_url(url)
            return url

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
