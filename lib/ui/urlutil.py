"""Pure plugin:// URL construction and stream-token encoding.

No dispatch state, no Kodi imports: `url_for()` takes the caller's base
URL explicitly instead of reading a module global, so this module has no
mutable state and no import-cycle/module-reload concerns. The mutable
runtime base URL (`router.BASE_URL`) and Kodi dispatch itself stay in
lib.ui.router.
"""
import base64
import json
import zlib
from urllib.parse import urlencode

#: zlib level for `encode_stream`. A stream dict is mostly repeated
#: tracker URLs and long release names, so it compresses ~45%; level 1
#: captures essentially all of that (level 9 buys a further 1%) for the
#: least CPU, which matters when a popular title returns several hundred
#: streams and every one gets a token built for its row.
_COMPRESS_LEVEL = 1


def url_for(base_url, action, **params):
    """Build a plugin:// URL for `action` against `base_url` with the given
    string params."""
    query = {'action': action}
    for key, value in params.items():
        if value is None or value == '':
            continue
        query[key] = value
    return '%s?%s' % (base_url, urlencode(query))


def encode_stream(stream):
    """Compress and base64url-encode a stream dict for safe passage inside
    a plugin URL.

    The whole dict is round-tripped through the URL rather than an index
    into addon-side state, so a row stays playable straight out of Kodi's
    own directory cache with no addon state to go stale. That makes the
    token the largest thing in a streams listing - deflating it first
    takes a Torrentio-shaped stream from ~1035 to ~574 characters, which
    is ~230KB less held in memory AND less written to the directory cache
    on flash for a 500-stream title.

    No production code calls this any more: it built the `stream=`
    querystring param for the classical (pre-0.8.0) directory listings'
    play URL, and 675407b ("views: delete the classical directory
    listings") deleted that last call site along with the rest of the
    dual UI. The current picker (streamswindow.py) hands a stream straight
    to player.play_direct() in-process and never round-trips it through a
    plugin:// URL. It stays here anyway as the token generator
    decode_stream()'s own tests (and the do_play() dispatch tests in
    tests/test_router.py) build fixtures with, since a saved favourite/
    .strm/skin-widget URL encoded before 0.8.0 can still reach
    router.do_play() and must keep decoding - see decode_stream() and the
    addon.xml 0.8.0 note ("Playing a saved stream link still works").
    Hand-rolling this compress+encode step again inside the test file
    would just duplicate it under a different name, so it is kept as the
    real implementation instead.
    """
    payload = json.dumps(stream or {}, separators=(',', ':')).encode('utf-8')
    return base64.urlsafe_b64encode(
        zlib.compress(payload, _COMPRESS_LEVEL)
    ).decode('ascii')


def decode_stream(token):
    """Inverse of encode_stream(); returns {} for empty/invalid input.

    Also accepts the uncompressed tokens written before compression was
    introduced: those URLs can still be sitting in Kodi's directory cache
    after an upgrade, and clicking one must not fail. json.dumps() never
    emits leading whitespace, so a decoded payload starting with '{' is
    unambiguously a legacy plain-JSON token, and anything else is
    deflated.
    """
    if not token:
        return {}
    padded = token + '=' * (-len(token) % 4)
    try:
        payload = base64.urlsafe_b64decode(padded.encode('ascii'))
        if not payload.startswith(b'{'):
            payload = zlib.decompress(payload)
        return json.loads(payload.decode('utf-8'))
    except (ValueError, TypeError, zlib.error):
        return {}
