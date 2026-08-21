"""Stremio `addon_catalog` protocol client (pure Python, no Kodi imports).

An addon can catalog OTHER addons for a user to browse and install,
exactly the way it catalogs movies/series: it advertises one or more
`{type, id, name}` descriptors in `manifest['addonCatalogs']` - a key
distinct from `manifest['catalogs']` (meta catalogs, `iter_catalogs()` in
`lib.stremio.addons`) - and serves each one at
`{transportUrl-base}/addon_catalog/{type}/{id}.json`, returning
`{"addons": [{transportUrl, transportName, manifest}, ...]}`. Verified
live against Cinemeta's own "Community addons" catalog (95 entries):
each entry already carries a FULL manifest, so installing one never
needs a follow-up manifest fetch the way `AddonClient.catalog()`'s meta
previews do.

Cinemeta ships this seeded into `lib.store.DEFAULT_ADDONS` with its
`addonCatalogs` array already committed, so `lib.ui.addoncatalogwindow` works
offline-seeded on first run with no new dependency - but nothing here is
Cinemeta-specific: `iter_addon_catalogs()` reads the field off WHATEVER
addons happen to be installed, so any addon publishing `addonCatalogs`
contributes.

Roughly half of a community catalog's entries set
`manifest.behaviorHints.configurationRequired` (46 of Cinemeta's 95, per
that same live census) - installing one of those as-is yields an addon
that looks installed but silently 404s/errors on every resource until
configured through a web page Kodi cannot render. `descriptor_state()`
is what stops `lib.ui.addoncatalogwindow` from offering a plain "install" on
those.

A live census against Cinemeta found 11 declared `(type, id)` pairs -
`all|movie|series|channel` under id `official`, plus
`all|movie|series|channel|tv|Podcasts|other` under id `community` -
fetched as 11 separate GETs that pull 297 rows for only 102 unique
addons (7 official + 95 community), because each id's `all` variant
already returns everything its narrower type variants repeat.
`iter_unique_addon_catalogs()` collapses an addon's declared catalogs
to one entry per unique id (preferring the `all` type when declared),
turning Cinemeta's 11 pairs into 2 requests for the same 102 addons.
About 16% of a community catalog's transportUrls are dead in practice,
so `fetch_addon_catalogs()` fans several sources out concurrently,
bounded like every other fan-out site in this codebase (see
`lib.ui.views._MAX_ADDON_WORKERS`), and keeps one dead source from
losing every other source's rows. `fetch_addon_catalog_cached()` adds
a short TTL in front of `fetch_addon_catalog()` so re-opening the same
catalog inside one already-running process costs zero HTTP requests;
see its own docstring for why that cache is a plain in-process dict.

Reuses `lib.stremio.addons`' HTTP/error/timeout/logging conventions
(`AddonError`, `validate_transport_url`, `safe_url_for_log`,
`_request_error_category`, `_ensure_requests`) rather than a parallel
hierarchy, so every call site that already catches `AddonError` from
`AddonClient` catches this module's errors too.
"""
import threading
import time

from lib.stremio import addons as _addons
from lib.stremio.addons import (
    AddonError,
    build_resource_url,
    safe_url_for_log,
    validate_transport_url,
)

#: `descriptor_state()` outcomes - see its docstring for what each means.
STATE_INSTALLED = 'installed'
STATE_UPDATE_AVAILABLE = 'update-available'
STATE_INSTALLABLE = 'installable'
STATE_NEEDS_CONFIGURATION = 'needs-configuration'


def iter_addon_catalogs(addons):
    """Yield `(transport_url, manifest, addon_catalog)` for every
    addon-catalog descriptor declared by `addons` - a list of installed
    addon descriptors shaped like `{"transportUrl": ..., "manifest": {...}}`
    (`lib.store.Store.get_addons()`).

    Reads `manifest['addonCatalogs']`, which is a DISTINCT manifest key
    from `manifest['catalogs']` (`lib.stremio.addons.iter_catalogs()`
    reads that one). Mirrors that function's shape exactly, deliberately
    with no per-addon special-casing: ANY installed addon publishing
    `addonCatalogs` contributes here, not only whichever ships as
    Rivulet's seeded default (currently Cinemeta, in
    `lib.store.DEFAULT_ADDONS`) - a catalog browser that only worked for one
    hardcoded addon would stop working the moment that addon is removed
    or replaced by the user.
    """
    for descriptor in addons or []:
        manifest = descriptor.get('manifest') or {}
        transport_url = descriptor.get('transportUrl')
        for addon_catalog in manifest.get('addonCatalogs') or []:
            yield transport_url, manifest, addon_catalog


def fetch_addon_catalog(client_or_session, transport_url, type_, id_):
    """GET the `addon_catalog` resource -
    `{transport_url-base}/addon_catalog/{type_}/{id_}.json` - and return
    its `addons` list.

    `type_`/`id_` come from one entry of an installed addon's own
    `manifest['addonCatalogs']` (see `iter_addon_catalogs`); `transport_url`
    is that DECLARING addon's own transportUrl, never the transportUrl of
    an addon being listed.

    The envelope is `{"addons": [{transportUrl, transportName,
    manifest}, ...]}`, verified live against Cinemeta's own community
    catalog - each entry already carries a FULL manifest, so no
    follow-up manifest fetch is ever needed before installing one.

    `client_or_session` accepts either an `AddonClient` (its `.session`/
    `.timeout` are used, exactly like `AddonClient._get_json`) or a bare
    `requests.Session`-alike (falls back to a 15s default timeout) - a
    caller that already built an `AddonClient` for other resources
    doesn't need a second one just for this call.

    Raises `AddonError` on a network failure, a non-2xx response,
    invalid JSON, or an envelope missing a list `addons` key - the last
    of which `AddonClient._get_json()` has no equivalent for, since none
    of its own resources are validated this strictly today. Never logs
    or embeds the raw `transport_url`/query in the raised message -
    only `safe_url_for_log()`'s `scheme://host[:port]`, same as every
    `AddonClient` error.
    """
    requests_mod = _addons._ensure_requests()
    if requests_mod is None:
        raise AddonError('the "requests" package is required to fetch an addon catalog')

    session = getattr(client_or_session, 'session', client_or_session)
    timeout = getattr(client_or_session, 'timeout', 15)

    url = validate_transport_url(build_resource_url(transport_url, 'addon_catalog', type_, id_))
    safe = safe_url_for_log(url)
    try:
        resp = session.get(url, timeout=timeout)
        resp.raise_for_status()
    except requests_mod.RequestException as exc:
        category = _addons._request_error_category(exc)
        raise AddonError('GET %s failed: %s' % (safe, category), category=category)

    try:
        data = resp.json()
    except ValueError:
        raise AddonError('GET %s returned invalid JSON' % safe, category='invalid JSON')

    addons_list = data.get('addons') if isinstance(data, dict) else None
    if not isinstance(addons_list, list):
        raise AddonError(
            'GET %s returned a malformed addon_catalog envelope' % safe, category='invalid JSON'
        )
    return addons_list


def iter_unique_addon_catalogs(addons):
    """Like `iter_addon_catalogs()`, but yields at most one
    `(transport_url, manifest, addon_catalog)` per unique catalog `id`
    declared by each addon, instead of one per declared `(type, id)`
    pair - see the module docstring for the measured 11-pairs/2-ids
    Cinemeta numbers this collapsing is built from.

    Picks, per id: the descriptor declaring type `all` when one
    exists, regardless of where it falls in declaration order;
    otherwise the first-declared descriptor for that id, keeping ITS
    type - an addon publishing a catalog only under narrower types (no
    `all` variant) is still fetched exactly once, under whichever type
    it happened to declare first, rather than dropped or fetched once
    per type.

    `iter_addon_catalogs()` itself is left untouched: `lib.ui.
    addoncatalogwindow` still calls it directly today, so changing its
    yield shape out from under that caller would silently break it.
    This is the collapsing helper callers can opt into instead.
    """
    for descriptor in addons or []:
        manifest = descriptor.get('manifest') or {}
        transport_url = descriptor.get('transportUrl')
        chosen = {}
        order = []
        for addon_catalog in manifest.get('addonCatalogs') or []:
            catalog_id = addon_catalog.get('id')
            if catalog_id is None:
                continue
            if catalog_id not in chosen:
                order.append(catalog_id)
                chosen[catalog_id] = addon_catalog
            elif addon_catalog.get('type') == 'all' and chosen[catalog_id].get('type') != 'all':
                chosen[catalog_id] = addon_catalog
        for catalog_id in order:
            yield transport_url, manifest, chosen[catalog_id]


#: Cap on concurrent addon_catalog HTTP calls per `fetch_addon_catalogs()`
#: fan-out. Deliberately this module's OWN constant, never a shared import
#: of `lib.ui.views._MAX_ADDON_WORKERS` - AGENTS.md is explicit that fan-out
#: is bounded per call site, not DRY'd into one shared constant, because
#: this runs on low-power ARM boxes; this module also has no dependency on
#: `lib.ui` today and must not grow one just to reuse a number.
_MAX_CATALOG_WORKERS = 8

#: How long a cached `fetch_addon_catalog()` response is served before
#: being treated as stale. Mirrors `lib.ui.metacache.TTL_SECONDS`'s
#: reasoning - short on purpose, just long enough to cover one browsing
#: session (open the catalog window, back out, reopen it, re-filter) without
#: risking a long-lived stale answer once a source addon's own catalog
#: changes.
_CATALOG_CACHE_TTL_SECONDS = 300

#: In-process cache of `(transport_url, type_, id_) -> (fetched_at,
#: addons_list)`. A plain module-level dict, NOT `lib.ui.metacache`'s
#: on-disk JSON file: this module has zero Kodi imports by design (module
#: docstring) and must stay that way, so it has no `xbmcvfs`-found data
#: directory to persist a cache file into. The accepted tradeoff: a fresh
#: `default.py` invocation always starts cold, because Kodi runs every
#: `plugin://` invocation as its OWN OS process (`lib.store`'s module
#: docstring documents the same constraint) - one process's cache cannot
#: outlive it. That is fine here: this cache only exists to stop the SAME
#: already-open window from re-fetching the same catalog several times in
#: its own lifetime, never to survive across separate plugin invocations.
_catalog_cache = {}  # type: dict
_catalog_cache_lock = threading.Lock()


def fetch_addon_catalog_cached(client_or_session, transport_url, type_, id_):
    """`fetch_addon_catalog()`, served from `_catalog_cache` when a prior
    call for the same `(transport_url, type_, id_)` completed less than
    `_CATALOG_CACHE_TTL_SECONDS` ago - see that constant's docstring for
    why this is an in-process dict rather than a disk cache. A cache hit
    issues zero HTTP requests.
    """
    key = (transport_url, type_, id_)
    now = time.monotonic()
    with _catalog_cache_lock:
        cached = _catalog_cache.get(key)
    if cached is not None and now - cached[0] < _CATALOG_CACHE_TTL_SECONDS:
        return cached[1]
    addons_list = fetch_addon_catalog(client_or_session, transport_url, type_, id_)
    with _catalog_cache_lock:
        _catalog_cache[key] = (now, addons_list)
    return addons_list


def fetch_addon_catalogs(client_or_session, sources):
    """Fetch several `(transport_url, type_, id_)` catalog sources
    concurrently through `fetch_addon_catalog_cached()`, bounded by
    `_MAX_CATALOG_WORKERS`.

    Returns `(entries, failures)`: `entries` is every fetched addon
    envelope entry from every SUCCESSFUL source, concatenated in source
    order; `failures` is a list of `(transport_url, type_, id_, error)`
    for every source whose fetch raised `AddonError`. Measured live,
    about 16% of a community catalog's transportUrls are dead - one dead
    source must never cost the other sources their rows, so each fetch
    runs inside its own try/except in the worker thread, exactly like
    `lib.ui.views._map_addons()`'s callers are expected to guard their
    own per-item call.
    """
    sources = list(sources or [])
    if not sources:
        return [], []

    def _fetch_one(source):
        transport_url, type_, id_ = source
        try:
            return fetch_addon_catalog_cached(client_or_session, transport_url, type_, id_), None
        except AddonError as exc:
            return None, (transport_url, type_, id_, exc)

    if len(sources) == 1:
        results = [_fetch_one(sources[0])]
    else:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(len(sources), _MAX_CATALOG_WORKERS)) as pool:
            results = list(pool.map(_fetch_one, sources))

    entries = []
    failures = []
    for addons_list, failure in results:
        if failure is not None:
            failures.append(failure)
        else:
            entries.extend(addons_list)
    return entries, failures


def _version_key(version):
    """Best-effort sortable key for a dotted version string like
    "1.2.3", tolerant of anything an addon manifest might actually put
    there. Each dot-separated segment sorts as its integer value when it
    is one, else as its raw string - so "1.2.3" > "1.2.0" holds without
    ever raising on a non-numeric segment like "1.2.0-rc1". Only ever
    used to decide whether to badge an entry "update available", never
    to reject an addon outright, so an unusual scheme merely stops
    comparing usefully rather than breaking anything."""
    parts = []
    for segment in str(version or '').split('.'):
        parts.append((0, int(segment)) if segment.isdigit() else (1, segment))
    return parts


def _is_newer(catalog_version, installed_version):
    """Whether `catalog_version` sorts strictly after `installed_version`
    (see `_version_key`). False - never "unknown"/raises - when either
    side is missing, so a manifest without a `version` field is simply
    never offered as an update."""
    if not catalog_version or not installed_version:
        return False
    return _version_key(catalog_version) > _version_key(installed_version)


def descriptor_state(descriptor, installed_addons):
    """Classify one `addon_catalog` entry against the locally installed
    addon list, for `lib.ui.addoncatalogwindow` to badge/gate each row:

    - `STATE_INSTALLED`: already installed, and the catalog's own
      manifest declares no newer `version`.
    - `STATE_UPDATE_AVAILABLE`: already installed, but the catalog's
      manifest (a FULL manifest - see the module docstring) declares a
      newer `version` than the installed copy - a plain string compare,
      no extra fetch needed.
    - `STATE_NEEDS_CONFIGURATION`: NOT installed, and
      `manifest.behaviorHints.configurationRequired` is set - installing
      one of these as-is silently yields a broken addon (see the module
      docstring). Checked only when not installed: an addon the user
      already runs is presumed already configured, whatever its catalog
      listing says now.
    - `STATE_INSTALLABLE`: not installed, no configuration required.

    Matching is by `transportUrl` alone - the addon protocol's own
    identity key for "the same addon" - never by manifest `id`, which
    two unrelated addons are free to reuse.
    """
    manifest = descriptor.get('manifest') or {}
    transport_url = descriptor.get('transportUrl')

    installed = None
    for addon in installed_addons or []:
        if addon.get('transportUrl') == transport_url:
            installed = addon
            break

    if installed is not None:
        installed_version = (installed.get('manifest') or {}).get('version')
        if _is_newer(manifest.get('version'), installed_version):
            return STATE_UPDATE_AVAILABLE
        return STATE_INSTALLED

    if (manifest.get('behaviorHints') or {}).get('configurationRequired'):
        return STATE_NEEDS_CONFIGURATION
    return STATE_INSTALLABLE
