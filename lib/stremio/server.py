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
