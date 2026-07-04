"""stremio-server-go client (pure Python, no Kodi imports).

Turns Stream protocol objects (stremio-protocol-spec.md #3, stremio-core
src/types/resource/stream.rs StreamSource) into URLs playable against a
local stremio-server-go instance, mirroring stremio-core's Stream::convert
(stream.rs:234-604) for the sources this client supports:

- Torrent (infoHash/fileIdx/announce) -> GET {server}/{infoHash}/{fileIdx}
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

resolve_stream() is a pure URL builder - it does not check whether the
server is actually reachable. Callers should call is_available() first
(e.g. to show string 30031 "Streaming server unavailable") before relying
on a torrent/YouTube/magnet URL it returns.
"""
from urllib.parse import parse_qs, quote, urlencode, urlparse

try:
    import requests
except ImportError:  # pragma: no cover - exercised only without the dependency
    requests = None

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
    """Talks to a local stremio-server-go instance (default http://127.0.0.1:11470)."""

    def __init__(self, base_url):
        self.base_url = (base_url or '').rstrip('/')

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
                resp = requests.get(self.base_url + path, timeout=2)
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

    def create_engine(self, info_hash):
        """GET `{base}/{infoHash}/create` - start/attach the torrent engine.

        Per stremio-server-go's handleCreate (internal/api/api.go:697-750,
        docs/swagger.yaml `/{infoHash}/create`), the server calls
        EnsureEngine + Ready() with a 90s timeout blocking until torrent
        metadata is available, then returns `types.Stats` including
        `guessedFileIdx` (set when the server picked a best file). Uses a
        100s client timeout to stay above the server's 90s Ready() budget.
        """
        if requests is None:
            raise ServerError('the "requests" package is required for ServerClient')
        url = '%s/%s/create' % (self.base_url, str(info_hash).lower())
        try:
            resp = requests.get(url, timeout=100)
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
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise ServerError('GET %s failed: %s' % (url, exc))
        try:
            return resp.json()
        except ValueError as exc:
            raise ServerError('GET %s returned invalid JSON: %s' % (url, exc))

    def resolve_stream(self, stream):
        """Resolve a Stream protocol dict to a playable URL, or None.

        - `url`: http(s)/other schemes are returned as-is; a `magnet:` url
          is converted via _magnet_to_torrent_url() when it carries a
          parseable info hash, else None (playing a bare magnet needs a
          torrent client, which the addon doesn't embed).
        - `infoHash` (+ `fileIdx`, `announce`): -> torrent_url(). Missing
          `fileIdx` defaults to UNKNOWN_FILE_IDX (-1), matching
          stremio-core, NOT 0.
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
            announce = stream.get('announce') or []
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
