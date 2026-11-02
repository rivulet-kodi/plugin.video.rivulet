# SPDX-FileCopyrightText: 2026 M0Rf30
# SPDX-License-Identifier: MIT
"""What a catalog said about a title, for when its id cannot be opened.

Provider lists (Netflix, Disney+, Hulu, HBO...) can key their titles by
an id no installed meta addon resolves: ``tmdb:...`` or an aggregator's
own prefix. A series opened from one could not be loaded, while the same
title found by search (a Cinemeta ``tt`` id) opened and played. The
coverflow remembers each catalog preview it draws here, by id, so the
detail page can fall back to an IMDb id: the one the preview names, or
the matching title from a search.
"""

import collections
import re
import threading

_MAX = 3000
_LOCK = threading.Lock()
_PREVIEWS = collections.OrderedDict()  # type: collections.OrderedDict


def remember(meta):
    """Keep `meta` (a catalog preview) under its id. Never raises."""
    if not isinstance(meta, dict):
        return
    sid = meta.get('id')
    if not isinstance(sid, str) or not sid:
        return
    keep = {key: meta.get(key) for key in (
        'id', 'type', 'name', 'year', 'releaseInfo', 'released', 'dvdRelease',
        'imdb_id', 'imdbId', 'behaviorHints', 'poster', 'background', 'logo', 'description',
    ) if meta.get(key) is not None}
    with _LOCK:
        _PREVIEWS[sid] = keep
        _PREVIEWS.move_to_end(sid)
        while len(_PREVIEWS) > _MAX:
            _PREVIEWS.popitem(last=False)


def lookup(sid):
    with _LOCK:
        found = _PREVIEWS.get(sid)
        return dict(found) if found else None


def forget_all():
    with _LOCK:
        _PREVIEWS.clear()


def year_of(meta):
    """The first four-digit year in `year`/`releaseInfo`/`released`, or ``''``."""
    if not isinstance(meta, dict):
        return ''
    for key in ('year', 'releaseInfo', 'released'):
        match = re.search(r'(19|20)\d\d', str(meta.get(key) or ''))
        if match:
            return match.group(0)
    return ''


def _norm(name):
    return re.sub(r'[^0-9a-z]+', '', str(name or '').lower())


def best_match(preview, candidates, stype):
    """The ``tt`` id among search `candidates` that is this title: same
    type, same name (ignoring case and punctuation), and the same year
    when both name one. ``''`` when nothing matches. Kodi-free."""
    from lib.stremio.addons import _base_type

    want_name = _norm((preview or {}).get('name'))
    if not want_name:
        return ''
    want_year = year_of(preview)
    want_type = _base_type(stype or (preview or {}).get('type') or '')
    for meta in candidates or []:
        if not isinstance(meta, dict):
            continue
        sid = str(meta.get('id') or '')
        if not sid.startswith('tt'):
            continue
        if want_type and _base_type(meta.get('type') or '') not in ('', want_type):
            continue
        if _norm(meta.get('name')) != want_name:
            continue
        year = year_of(meta)
        if want_year and year and year != want_year:
            continue
        return sid
    return ''
