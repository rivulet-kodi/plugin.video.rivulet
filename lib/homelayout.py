# SPDX-FileCopyrightText: 2026 M0Rf30
# SPDX-License-Identifier: MIT
"""Pure Home-list layout, classification and editing rules.

The persisted ids describe intent, not current installation state. Missing
addons must not erase a user's arrangement: resolution skips them, while
normalisation and editing retain their ids for a later reinstall.
"""
import copy
import re

from lib.stremio.addons import _base_type, catalog_required_extra_names, iter_catalogs
from lib.stremio.contentrating import is_adult_catalog

TABS = ('home', 'mystuff', 'movies', 'series', 'anime', 'live')
TAB_LABELS = dict(zip(TABS, (30495, 30516, 30213, 30214, 30215, 30552)))
KINDS = ('movie', 'series', 'anime-movie', 'anime-series', 'live')
#: Kinds a ``new:`` placeholder can name. Live has no "newest" catalog: its
#: rows are time-sensitive already, so only ``rest:live`` exists.
NEW_KINDS = KINDS[:4]
#: Base types Home can show. ``sport``/``sports``/``events`` are what live
#: addons declare (Sports Streams, Maximum Sports); ``channel`` stays out,
#: Stremio uses it for YouTube-style uploaders, not broadcasts.
LIVE_TYPES = ('sport', 'sports', 'event', 'events', 'live')
HOME_TYPES = ('movie', 'series', 'tv', 'anime') + LIVE_TYPES
#: Whole words that make a ``tv`` catalog live television. ``tv`` alone is
#: not enough: plenty of addons publish TV *shows* under it.
LIVE_WORDS = {'live', 'channel', 'channels', 'iptv', 'sport', 'sports', 'broadcast', 'broadcasts'}
CONTINUE_IDS = ('continue', 'continue:movie', 'continue:series', 'continue:anime')
LIBRARY_IDS = ('library', 'library:movie', 'library:series')
MAX_POSTER_ROWS = 12
NEW_TOKENS = ('new', 'latest', 'recent', 'recently', 'release', 'releases',
              'premiere', 'premieres', 'fresh', 'airing', 'ongoing',
              'theaters', 'theatres', 'now playing')
ANIME_SOURCES = {'crunchyroll', 'hidive', 'funimation', 'kitsu', 'anilist', 'myanimelist', 'anidb'}
_NEW_RE = re.compile(r'\b(?:%s)\b' % '|'.join(re.escape(token) for token in NEW_TOKENS))
_NEW_LABELS = dict(zip(NEW_KINDS, (30529, 30530, 30531, 30532)))
DEFAULT = {'version': 1, 'tabs': {
    'home': ['continue', 'new:movie', 'new:series'],
    'mystuff': ['library:movie', 'library:series'],
    'movies': ['continue:movie', 'new:movie', 'rest:movie'],
    'series': ['continue:series', 'new:series', 'rest:series'],
    'anime': ['continue:anime', 'new:anime-movie', 'new:anime-series',
              'rest:anime-movie', 'rest:anime-series'],
    'live': ['rest:live'],
}}


class LayoutError(ValueError):
    """The saved envelope cannot describe this version of Home."""


def default_layout():
    return copy.deepcopy(DEFAULT)


def parse_catalog_id(value):
    if not isinstance(value, str) or not value.startswith('cat:'):
        return None
    parts = value[4:].split('|')
    if len(parts) not in (3, 4) or not all(parts):
        return None
    if _base_type(parts[1]) not in HOME_TYPES:
        return None
    if len(parts) == 4 and (parts[1].lower() != 'anime' or parts[3] not in ('movie', 'series')):
        return None
    return parts


def valid_id(value):
    if not isinstance(value, str):
        return False
    if value in CONTINUE_IDS or value in LIBRARY_IDS:
        return True
    if value in tuple('new:%s' % kind for kind in NEW_KINDS) + tuple('rest:%s' % kind for kind in KINDS):
        return True
    return parse_catalog_id(value) is not None


def normalize(layout):
    """Return a validated copy of ``layout``.

    A tab the saved layout lacks takes its default rows and an unknown tab
    is dropped, so one missing or extra tab (a layout saved by another
    build) costs only that tab. Rejecting the whole file for it, as this
    once did, made the next Settings edit overwrite the user's entire
    arrangement with the defaults. An envelope naming no known tab at all
    is still refused: nothing in it is recognisable.
    """
    if (not isinstance(layout, dict) or type(layout.get('version')) is not int
            or layout.get('version') != 1 or not isinstance(layout.get('tabs'), dict)
            or not set(layout['tabs']) & set(TABS)):
        raise LayoutError('invalid Home layout envelope')
    result = {'version': 1, 'tabs': {}}
    for tab in TABS:
        source = layout['tabs'].get(tab, DEFAULT['tabs'][tab])
        if not isinstance(source, list):
            raise LayoutError('invalid Home tab lists')
        continued, posters = [], []
        for value in source:
            if not valid_id(value):
                continue
            if value in CONTINUE_IDS:
                if not continued:
                    continued.append(value)
            elif value not in posters:
                posters.append(value)
        result['tabs'][tab] = continued + posters
    return result


def migrate(toggles):
    layout = default_layout()
    for tab, rows in layout['tabs'].items():
        kept = []
        for list_id in rows:
            if list_id in CONTINUE_IDS and not toggles.get('home_show_mystuff', True):
                continue
            disabled = False
            for kind, setting in (('movie', 'home_show_movies'), ('series', 'home_show_series')):
                if toggles.get(setting, True):
                    continue
                if list_id in ('new:%s' % kind, 'rest:%s' % kind):
                    disabled = True
                if list_id == 'continue:%s' % kind:
                    disabled = True
            if not disabled:
                kept.append(list_id)
        layout['tabs'][tab] = kept
    return layout


def matches_continue(meta, variant=''):
    stype = _base_type(meta.get('type') or meta.get('stype'))
    anime = stype == 'anime' or str(meta.get('id') or '').startswith('kitsu:')
    if not variant:
        return True
    if variant == 'anime':
        return anime
    if anime:
        return False
    return stype == 'movie' if variant == 'movie' else stype in ('series', 'tv')


def matches_library(meta, variant=''):
    stype = str(meta.get('type') or '').strip().lower()
    base = _base_type(stype)
    if not variant:
        return True
    movie = base == 'movie' or stype == 'anime.movie'
    return movie if variant == 'movie' else base in ('series', 'tv', 'anime') and not movie


def matches_catalog(meta, variant=''):
    if not variant:
        return True
    movie = _base_type(meta.get('type')) == 'movie'
    return movie if variant == 'movie' else not movie


def is_anime_source(manifest, catalog):
    """Whole source words, not substrings such as Animation or Malcolm.

    Streaming Catalogs' opaque cru id needs its exact manifest/id pair.
    Classification changes placeholder defaults only; explicit ids stay put.
    """
    base = _base_type(catalog.get('type'))
    if base == 'anime':
        return True
    if base not in ('movie', 'series', 'tv'):
        return False

    if _words(catalog.get('name')) & (ANIME_SOURCES | {'anime'}):
        return True
    if any(_words(value) & ANIME_SOURCES for value in (
            catalog.get('id'), manifest.get('id'), manifest.get('name'))):
        return True
    return manifest.get('id') == 'pw.ers.netflix-catalog' and catalog.get('id') == 'cru'


def _words(value):
    return set(re.findall(r'[^\W_]+', str(value or '').lower()))


def is_live_catalog(catalog):
    """A sport/event type, or a ``tv`` catalog whose own name or id says
    live/channels. Only the catalog is read, never the addon: an addon may
    serve live channels next to ordinary TV-show catalogs."""
    base = _base_type(catalog.get('type'))
    if base in LIVE_TYPES:
        return True
    return base == 'tv' and bool((_words(catalog.get('name')) | _words(catalog.get('id'))) & LIVE_WORDS)


def installed_catalogs(addons, hide_adult=True):
    """Reuse the protocol's union of modern/legacy required extras.

    Like the stock picker, skip is always satisfiable: it is pagination,
    not a prerequisite input. Home cannot prompt for search/genre values,
    so every other required extra excludes a preview row.
    """
    result = []
    for transport, manifest, catalog in iter_catalogs(addons):
        if not isinstance(catalog, dict):
            continue
        ctype = str(catalog.get('type') or '').strip().lower()
        base = _base_type(ctype)
        if base not in HOME_TYPES:
            continue
        if catalog_required_extra_names(catalog) - {'skip'}:
            continue
        if hide_adult and is_adult_catalog(catalog, manifest):
            continue
        addon_id = transport or manifest.get('id')
        cid = catalog.get('id')
        if not isinstance(addon_id, str) or not isinstance(cid, str) or not cid:
            continue
        kind = 'movie' if base == 'movie' else 'series'
        if is_live_catalog(catalog):
            kind = 'live'
        elif base != 'anime' and is_anime_source(manifest, catalog):
            kind = 'anime-%s' % kind
        views = [(kind, '')]
        if base == 'anime':
            if ctype in ('anime.movie', 'anime.series'):
                views = [('anime-%s' % ctype.split('.')[1], '')]
            else:
                views = [('anime-movie', 'movie'), ('anime-series', 'series')]
        for kind, variant in views:
            list_id = 'cat:%s|%s|%s' % (addon_id, catalog['type'], cid)
            if variant:
                list_id += '|' + variant
            if parse_catalog_id(list_id) is None:
                continue
            result.append({'list_id': list_id, 'kind': kind, 'filter': variant,
                           'transport': transport or addon_id, 'manifest': manifest,
                           'catalog': catalog, 'addon_id': addon_id})
    return result


def _new_catalog(catalogs, kind):
    eligible = [catalog for catalog in catalogs if catalog['kind'] == kind]
    for entry in eligible:
        catalog = entry['catalog']
        if any(_NEW_RE.search(str(catalog.get(field) or '').lower()) for field in ('id', 'name')):
            return entry
    return eligible[0] if eligible else None


def _expand(rows, catalogs):
    by_id = {catalog['list_id']: catalog for catalog in catalogs}
    seen = set()
    for list_id in rows:
        source = [list_id]
        heading = None
        if list_id.startswith(('new:', 'rest:')):
            prefix, kind = list_id.split(':', 1)
            # rest: leaves out what new: would show; a kind without new: keeps all.
            chosen = _new_catalog(catalogs, kind) if kind in NEW_KINDS else None
            if prefix == 'new':
                source = [chosen['list_id']] if chosen else []
                heading = (_NEW_LABELS[kind],)
            else:
                source = [entry['list_id'] for entry in catalogs if entry['kind'] == kind
                          and (chosen is None or entry['list_id'] != chosen['list_id'])]
        for concrete in source:
            if concrete in seen:
                continue
            seen.add(concrete)
            yield concrete, by_id.get(concrete), heading


def _heading(list_id, catalog, heading):
    if heading is not None:
        return heading
    if list_id in CONTINUE_IDS:
        return (30241,)
    if list_id in LIBRARY_IDS:
        return ({'library': 30422, 'library:movie': 30533, 'library:series': 30534}[list_id],)
    return (str(catalog['catalog'].get('name') or catalog['catalog']['id']),)


def resolve(layout, tab, catalogs, hide_adult=True):
    """Ordered (id, kind, descriptor, filter, heading-parts) render plan.

    Heading parts carry localized ids or addon-authored names; UI callers
    pass L into heading_text. This module stays independent of Kodi.
    """
    layout = normalize(layout)
    catalogs = [c for c in catalogs if not hide_adult
                or not is_adult_catalog(c['catalog'], c['manifest'])]
    plan = []
    posters = 0
    for list_id, catalog, heading in _expand(layout['tabs'][tab], catalogs):
        if list_id.startswith('cat:') and catalog is None:
            continue
        kind = 'continue' if list_id in CONTINUE_IDS else 'library' if list_id in LIBRARY_IDS else 'catalog'
        if kind != 'continue':
            if posters >= MAX_POSTER_ROWS:
                continue
            posters += 1
        variant = list_id.split(':', 1)[1] if kind != 'catalog' and ':' in list_id else ''
        if kind == 'catalog':
            variant = catalog['filter']
        plan.append((list_id, kind, catalog, variant, _heading(list_id, catalog, heading)))
    names = [entry[4][0] for entry in plan if entry[1] == 'catalog']
    result = []
    for list_id, kind, catalog, variant, heading in plan:
        if kind == 'catalog':
            name = str(catalog['catalog'].get('name') or catalog['catalog']['id'])
            if names.count(name) > 1:
                heading += (str(catalog['manifest'].get('name') or catalog['manifest'].get('id') or ''),)
            if variant and isinstance(heading[0], str):
                heading += (30535 if catalog['kind'] == 'anime-movie' else 30536,)
        result.append((list_id, kind, catalog, variant, heading))
    return result


def heading_text(parts, localize):
    return ' · '.join(localize(part) if isinstance(part, int) else part for part in parts if part)


def materialize(layout, tab, catalogs, hide_adult=True):
    result = normalize(layout)
    resolved = resolve(result, tab, catalogs, hide_adult)
    known = {entry['list_id'] for entry in catalogs}
    # Preserve explicit missing-addon ids. Reinstalling still restores them.
    missing = [value for value in result['tabs'][tab] if value.startswith('cat:') and value not in known]
    result['tabs'][tab] = [entry[0] for entry in resolved] + missing
    return normalize(result)


def add(layout, tab, list_id, replace_continue=False):
    result = normalize(layout)
    rows = result['tabs'][tab]
    if not valid_id(list_id) or list_id in rows:
        return result, False
    if list_id in CONTINUE_IDS:
        existing = [value for value in rows if value in CONTINUE_IDS]
        if existing and not replace_continue:
            return result, False
        rows[:] = [value for value in rows if value not in CONTINUE_IDS]
        rows.insert(0, list_id)
    else:
        if sum(value not in CONTINUE_IDS for value in rows) >= MAX_POSTER_ROWS:
            return result, False
        rows.append(list_id)
    return result, True


def remove(layout, tab, list_id):
    result = normalize(layout)
    result['tabs'][tab] = [value for value in result['tabs'][tab] if value != list_id]
    return result


def move(layout, tab, list_id, direction):
    result = normalize(layout)
    rows = result['tabs'][tab]
    if list_id not in rows or list_id in CONTINUE_IDS:
        return result
    index = rows.index(list_id)
    target = index + direction
    if 0 <= target < len(rows) and rows[target] not in CONTINUE_IDS:
        rows[index], rows[target] = rows[target], rows[index]
    return result


def reset():
    return default_layout()
