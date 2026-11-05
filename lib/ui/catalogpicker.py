# SPDX-FileCopyrightText: 2026 M0Rf30
# SPDX-License-Identifier: MIT

"""Catalog helpers behind Home's catalog rows and the one-page search:
`_classify_catalog()` says how a catalog must be opened, `_reachable_catalogs()`
drops the ones this UI can never open, and `open_single_catalog()` opens one
in the coverflow (`lib.ui.infowindow`), whose title picks go on to
`lib.ui.detailwindow`.
"""
from lib.stremio.addons import (
    AddonError,
    addon_error_detail,
    catalog_extra_options,
    catalog_required_extra_names,
    safe_url_for_log,
)
from lib.ui.uicommon import CANCELLED, run_cancellable


def _classify_catalog(catalog):
    """How `catalog` must be opened, from its REQUIRED extras
    (`catalog_required_extra_names()`) minus `skip` (always satisfiable -
    it is paging, never a precondition to open):

    - 'search': requires `search` - prompt for a query on click.
    - 'genre': requires `genre` - the option select must open FIRST,
      with no unfiltered choice (a compliant client always supplies one -
      see Cinemeta's `movie/year`/`series/year`, display name "New").
    - 'open': no required extra the UI cannot supply - browses normally.
    - None: requires something else the UI cannot synthesise (e.g.
      Cinemeta's `lastVideosIds`/`calendarVideosIds`, or any unknown
      required name) - permanently unreachable, caller drops the row.

    `search` takes precedence over `genre` if a catalog somehow requires
    both."""
    required = catalog_required_extra_names(catalog) - {'skip'}
    if 'search' in required:
        return 'search'
    if 'genre' in required:
        return 'genre'
    if required:
        return None
    return 'open'


def _reachable_catalogs(catalogs):
    """Drop every catalog `_classify_catalog()` marks permanently
    unreachable (a required extra this UI cannot supply, e.g. Cinemeta's
    `lastVideosIds`/`calendarVideosIds`) or genre-required with no
    declared option values to choose from - logging each drop once at
    INFO so a missing row is diagnosable, not mistaken for a missing
    add-on."""
    import xbmc

    from lib.ui.compat import log

    kept = []
    for transport_url, manifest, catalog in catalogs:
        kind = _classify_catalog(catalog)
        if kind == 'genre' and not catalog_extra_options(catalog, 'genre'):
            kind = None
        if kind is None:
            name = catalog.get('name') or catalog.get('id')
            log(
                'catalogpicker: skipping %s/%s (%s) - requires extras this UI cannot supply' % (
                    safe_url_for_log(transport_url), catalog.get('type'), name,
                ),
                xbmc.LOGINFO,
            )
            continue
        kept.append((transport_url, manifest, catalog))
    return kept


def _filtered_catalog_pages(pages, meta_filter):
    from lib.homelayout import matches_catalog

    for page in pages:
        metas = [meta for meta in page if isinstance(meta, dict) and matches_catalog(meta, meta_filter)]
        if metas:
            yield metas


def open_single_catalog(transport_url, manifest, catalog, extra=None, meta_filter=''):
    """Open one catalog in the stock coverflow, optionally as an anime view."""
    import xbmc

    from lib.ui.compat import L, log, notify
    from lib.ui.uicommon import escape_label
    from lib.ui.views import iter_catalog_pages

    ctype = catalog.get('type')
    catalog_name = escape_label(catalog.get('name') or catalog.get('id'))
    addon_name = escape_label(manifest.get('name'))
    # "Addon · Catalog" when both are on hand, the coverflow's page
    # title - otherwise just the catalog name, which is always present.
    catalog_title = '%s \u00b7 %s' % (addon_name, catalog_name) if addon_name else catalog_name

    # A catalog that declares `skip` is walked page by page rather
    # than one page deep, so a list longer than the addon's page size
    # is reachable in full. Only the FIRST page is waited on here -
    # the rest are handed to the coverflow and appended as they land
    # (see ShowcaseWindow.start), because the pages are serial and an
    # addon serving 20 at a time would otherwise hold a 400-title
    # list behind a spinner for a minute before showing anything.
    pages = iter_catalog_pages(
        transport_url, ctype, catalog.get('id'), extra=extra, catalog=catalog, manifest=manifest,
    )
    if meta_filter:
        pages = _filtered_catalog_pages(pages, meta_filter)
    # Run on a worker under a cancellable spinner: this first page is
    # the one place the user waits on a remote addon with nothing to
    # look at (kodi.log: a 45 s wait during which every Back press was
    # ignored). Back returns to this picker within ~0.2 s; the orphaned
    # worker's page is discarded, so no coverflow can open for it later.
    try:
        metas = run_cancellable(lambda: next(pages, []), L(30033))
    except AddonError as exc:
        log('catalogpicker: %s failed: %s' % (safe_url_for_log(transport_url), addon_error_detail(exc)), xbmc.LOGERROR)
        notify(L(30032))
        return False
    if metas is CANCELLED:
        log('catalogpicker: %s (%s) load cancelled by the user' % (catalog_name, ctype), xbmc.LOGINFO)
        return False
    if not metas:
        log('catalogpicker: %s (%s) returned no results' % (catalog_name, ctype), xbmc.LOGINFO)
        # "No results for <catalog>", as a genre or a person with no titles
        # says - not the stream picker's "No streams found".
        from lib.ui.search import _with_query
        notify(_with_query(L(30513), catalog.get('name') or catalog.get('id') or ''))
        return False

    log('catalogpicker: opening coverflow (%d results, paging in background)' % len(metas), xbmc.LOGINFO)
    try:
        from lib.ui.infowindow import open_showcase
        selected = open_showcase(metas, catalog_title=catalog_title, more_pages=pages)
    except Exception as exc:  # a skin/UI failure must surface, not vanish
        log('catalogpicker: coverflow failed to open: %s' % addon_error_detail(exc), xbmc.LOGERROR)
        notify(L(30032))
        return False
    if not selected:
        return False

    from lib.ui.detailwindow import open_detail
    return bool(open_detail(selected.get('type') or ctype, selected.get('id')))
