# SPDX-FileCopyrightText: 2026 M0Rf30
# SPDX-License-Identifier: MIT

"""Pure plugin:// URL construction and stream-token decoding.

No dispatch state, no Kodi imports: `url_for()` takes the caller's base
URL explicitly instead of reading a module global, so this module has no
mutable state and no import-cycle/module-reload concerns. The mutable
runtime base URL (`router.BASE_URL`) and Kodi dispatch itself stay in
lib.ui.router.
"""
import base64
import json
import re
import zlib
from urllib.parse import urlencode

#: Hard cap on the inflated size `decode_stream()` will ever produce.
#: `decode_stream()` runs on a `plugin://` token that arrives via
#: `sys.argv` before any auth/origin check - a favourite, a .strm file,
#: or a skin widget can hand Kodi an attacker-crafted URL. Plain
#: `zlib.decompress(payload)` has no output bound, so a hand-built
#: ~1KB deflate "zip bomb" (highly repetitive bytes compress ~1000:1)
#: would inflate to a huge in-memory buffer and json.loads() it,
#: turning one clicked/loaded URL into a memory-exhaustion DoS on the
#: box running Kodi. 1 MiB is far larger than any real stream dict
#: (~1KB) ever needs.
_MAX_TOKEN_BYTES = 1024 * 1024


def url_for(base_url, action, **params):
    """Build a plugin:// URL for `action` against `base_url` with the given
    string params."""
    query = {'action': action}
    for key, value in params.items():
        if value is None or value == '':
            continue
        query[key] = value
    return '%s?%s' % (base_url, urlencode(query))


def decode_stream(token):
    """Decode a `stream=` token (base64url of zlib-deflated JSON) into the
    stream dict; returns {} for empty/invalid input.

    Nothing writes these tokens any more: the classical (pre-0.8.0)
    directory listings built them for their play URLs, and 675407b
    ("views: delete the classical directory listings") deleted that
    encoder's last caller. A saved favourite/.strm/skin-widget URL
    encoded before 0.8.0 can still reach `router.do_play()` and must keep
    decoding - see the addon.xml 0.8.0 note ("Playing a saved stream link
    still works").

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
            decompressor = zlib.decompressobj()
            payload = decompressor.decompress(payload, _MAX_TOKEN_BYTES)
            if decompressor.unconsumed_tail or not decompressor.eof:
                return {}
        return json.loads(payload.decode('utf-8'))
    except (ValueError, TypeError, zlib.error):
        return {}


#: TMDB serves every size of an image from one path: ``/t/p/<size>/<file>``.
#: Catalogs built on TMDB often hand out ``original``, a multi-megabyte
#: file, for art the skin draws a fraction of that size. Kodi downloads
#: the whole file before it can scale it down.
_TMDB_IMAGE = re.compile(r'^(https?://image\.tmdb\.org/t/p/)([A-Za-z0-9_]+)(/.+)$', re.IGNORECASE)

#: Widest size each use needs. A row poster is 200px wide (w342 covers
#: the 340px detail poster too); a backdrop is the 1920px hero, which the
#: fade and the bar cover below y=420 (w1280); a poster that fills the
#: hero for want of a backdrop is scaled to the screen (w780).
_TMDB_WIDTH = {'poster': 342, 'backdrop': 1280, 'fill': 780}


def sized_image(url, kind):
    """`url` at the TMDB size `kind` needs, or `url` unchanged.

    Only ``image.tmdb.org`` paths are rewritten, and only downward: an
    ``original`` or a ``w`` size wider than the use is replaced, a
    smaller one is kept (asking for a larger file would not sharpen it).
    `kind` is ``poster``, ``backdrop`` or ``fill``. Anything that is not
    a string comes back as ``''``.
    """
    if not isinstance(url, str):
        return ''
    width = _TMDB_WIDTH.get(kind)
    match = _TMDB_IMAGE.match(url) if width else None
    if match is None:
        return url
    size = match.group(2).lower()
    if size == 'original':
        wanted = True
    elif size.startswith('w') and size[1:].isdigit():
        wanted = int(size[1:]) > width
    else:
        # h632 and friends are height-bound sizes; leave them alone.
        wanted = False
    if not wanted:
        return url
    return '%sw%d%s' % (match.group(1), width, match.group(3))
