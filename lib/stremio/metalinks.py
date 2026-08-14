"""Parsing for Stremio Meta Link objects (pure Python, no Kodi imports).

Implements the addon SDK's Meta Link object (`docs/api/responses/meta.md`,
"Link" section) and the `stremio:///...` URI shapes it documents in
`docs/api/responses/meta.links.md`: `search`, `discover` and `detail`
links. A Meta object's `links` array is how the protocol expresses
cast/crew/genre navigation - there is no separate "person" or "genre"
resource, just these links pointing back into `search`/`discover`/`detail`.

`RESERVED_LINK_CATEGORIES` are the categories the spec reserves for
non-navigational URLs (external IMDb/share pages) and are never grouped
for display.
"""
from urllib.parse import parse_qsl, unquote, urlsplit

#: Reserved per docs/api/responses/meta.links.md - `imdb`/`share` point at
#: plain external https:// pages, `similar` is unused by real addons but
#: reserved all the same. Matched casefolded: real addon data capitalizes
#: category names inconsistently (e.g. Cinemeta's 'Cast', 'Directors').
RESERVED_LINK_CATEGORIES = frozenset(('imdb', 'share', 'similar'))


def parse_meta_link(url):
    """Parse a `stremio:///...` meta link into a dict describing it, or
    None when `url` is empty, not a string, a plain `http(s)://` URL (the
    imdb/share categories carry these), a `stremio:` link with an
    unrecognized first path segment, or malformed in a way that leaves any
    required field empty. Never raises.

        {'kind': 'search', 'query': str}
        {'kind': 'discover', 'transport_url': str, 'type': str,
                              'catalog_id': str, 'extra': [(name, value), ...]}
        {'kind': 'detail', 'type': str, 'id': str, 'video_id': str or None}

    `transport_url` and the discover/detail path segments are percent-
    decoded; `extra` pairs come from the query string (already decoded by
    parse_qsl) in the order the link declared them.
    """
    if not url or not isinstance(url, str):
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    # The documented shape is `stremio:///...` - three slashes, i.e. an
    # empty authority. A non-empty netloc means this isn't that shape.
    if parts.scheme != 'stremio' or parts.netloc:
        return None

    # Split the PATH into segments first, THEN percent-decode each one.
    # Decoding first would turn an encoded '%2F' inside a segment (e.g. the
    # discover transportUrl, which is itself a full URL containing real
    # slashes) into a literal '/', corrupting the split - the transportUrl
    # would be sliced apart as if it were extra path segments.
    raw_segments = [seg for seg in parts.path.split('/') if seg]
    if not raw_segments:
        return None
    kind = raw_segments[0]
    try:
        segments = [unquote(seg) for seg in raw_segments[1:]]
    except ValueError:
        return None

    if kind == 'search':
        query = next((v for k, v in parse_qsl(parts.query) if k == 'search'), None)
        if not query:
            return None
        return {'kind': 'search', 'query': query}

    if kind == 'discover':
        if len(segments) != 3:
            return None
        transport_url, ctype, catalog_id = segments
        if not transport_url or not ctype or not catalog_id:
            return None
        return {
            'kind': 'discover',
            'transport_url': transport_url,
            'type': ctype,
            'catalog_id': catalog_id,
            'extra': parse_qsl(parts.query),
        }

    if kind == 'detail':
        if len(segments) not in (2, 3):
            return None
        ctype, sid = segments[0], segments[1]
        if not ctype or not sid:
            return None
        return {
            'kind': 'detail',
            'type': ctype,
            'id': sid,
            'video_id': segments[2] if len(segments) == 3 else None,
        }

    return None


def iter_link_groups(meta):
    """Group a Meta object's `links` for display, skipping anything unusable.

    Returns `[(category, [(name, parsed_link), ...]), ...]`, categories and
    members in first-seen order, preserving the addon's own category
    spelling (e.g. 'Cast', 'Directors' from real Cinemeta data - not the
    lowercase singular the docs merely recommend). A link is skipped when
    its category casefolds into RESERVED_LINK_CATEGORIES, its name is
    empty, or parse_meta_link() rejects its url. Returns [] when `meta` has
    no usable links.
    """
    if not isinstance(meta, dict):
        return []
    links = meta.get('links')
    if not isinstance(links, list):
        return []

    order = []
    grouped = {}
    for link in links:
        if not isinstance(link, dict):
            continue
        category = link.get('category')
        name = link.get('name')
        if not isinstance(category, str) or not category:
            continue
        if category.casefold() in RESERVED_LINK_CATEGORIES:
            continue
        if not name:
            continue
        parsed = parse_meta_link(link.get('url'))
        if parsed is None:
            continue
        if category not in grouped:
            grouped[category] = []
            order.append(category)
        grouped[category].append((name, parsed))

    return [(category, grouped[category]) for category in order]
