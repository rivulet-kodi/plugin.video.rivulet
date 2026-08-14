"""Pure plugin:// URL construction and stream-token encoding.

No dispatch state, no Kodi imports: `url_for()` takes the caller's base
URL explicitly instead of reading a module global, so this module has no
mutable state and no import-cycle/module-reload concerns. The mutable
runtime base URL (`router.BASE_URL`) and Kodi dispatch itself stay in
lib.ui.router.
"""
import base64
import json
from urllib.parse import urlencode


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
    """Base64url-encode a stream dict for safe passage inside a plugin URL."""
    payload = json.dumps(stream or {}, separators=(',', ':')).encode('utf-8')
    return base64.urlsafe_b64encode(payload).decode('ascii')


def decode_stream(token):
    """Inverse of encode_stream(); returns {} for empty/invalid input."""
    if not token:
        return {}
    padded = token + '=' * (-len(token) % 4)
    try:
        payload = base64.urlsafe_b64decode(padded.encode('ascii'))
        return json.loads(payload.decode('utf-8'))
    except (ValueError, TypeError):
        return {}
