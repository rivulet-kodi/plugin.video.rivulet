# SPDX-FileCopyrightText: 2026 M0Rf30
# SPDX-License-Identifier: MIT

"""Tests for the clickable Genres/Cast/Directors/Writers chips
(`lib.ui.creditsrow`) on `DetailWindow` (a series) and `StreamsWindow`
(what a movie opens onto): what the row shows, what a click does, and
that the "Cast & crew" picker still reaches the same dispatch
(`lib.ui.infowindow.open_credit_link()`).

Windows are driven through `onInit()`/`onClick()` against the fake
xbmcgui, as every window test here is; a click is `onClick(CREDITS)` with
the chip selected via the fake list's `selected_index`.
"""
import contextlib
from urllib.parse import quote

import pytest

from lib.ui.creditsrow import CREDITS, credit_links
from tests.conftest import make_window, stub_choose
from tests.kodistubs import install_kodi_stubs

_RELOAD_MODULE_NAMES = (
    'lib.ui.compat', 'lib.ui.router', 'lib.ui.uicommon', 'lib.ui.dialogs',
    'lib.ui.views', 'lib.ui.dependencies', 'lib.ui.player', 'lib.ui.streamswindow',
    'lib.ui.searchwindow', 'lib.ui.infowindow', 'lib.ui.detailwindow',
)

_TRANSPORT = 'https://a.example/manifest.json'
_GENRE_URL = 'stremio:///discover/%s/movie/top?genre=Drama' % quote(_TRANSPORT, safe='')


@pytest.fixture
def ctx():
    with contextlib.ExitStack() as stack:
        yield stack.enter_context(install_kodi_stubs(reload=_RELOAD_MODULE_NAMES))


def _link(name, category, url):
    return {'name': name, 'category': category, 'url': url}


def _search(name):
    return 'stremio:///search?search=%s' % quote(name)


def _meta(*links, **extra):
    meta = {'id': 'tt1', 'name': 'Title', 'type': 'series', 'links': list(links)}
    meta.update(extra)
    return meta


class _Store:
    def __init__(self, addons=()):
        self._addons = list(addons)

    def get_enabled_addons(self):
        return list(self._addons)


def _wire(monkeypatch, ctx, addons=()):
    """Fakes the three seams a chip click crosses: the store/client the
    row resolves, the search, the catalog fetch and the results window.
    Returns the recorders."""
    rec = type('Rec', (), {'queries': [], 'fetches': [], 'opened': [], 'details': []})()
    store, client = _Store(addons), object()
    monkeypatch.setattr(ctx.dependencies, 'get_store', lambda: store)
    monkeypatch.setattr(ctx.dependencies, 'get_client', lambda: client)
    rec.store, rec.client = store, client

    def _run_query(passed_store, passed_client, query, report=None):
        rec.queries.append((passed_store, passed_client, query))
        return [{'id': 'tt2', 'name': 'Found', 'type': 'movie'}]

    def _fetch_catalog(transport_url, ctype, cid, extra=None):
        rec.fetches.append((transport_url, ctype, cid, extra))
        return [{'id': 'tt9', 'name': 'Drama', 'type': 'movie'}]

    monkeypatch.setattr(ctx.searchwindow, 'run_query', _run_query)
    monkeypatch.setattr(ctx.views, '_fetch_catalog', _fetch_catalog)
    monkeypatch.setattr(ctx.infowindow, 'open_showcase', lambda metas: rec.opened.append(metas) or None)
    # A person opens the Search screen on its results, the way Stremio's
    # search link does; a genre still opens the coverflow.
    _fake_search_window(monkeypatch, rec.opened)
    return rec


def _fake_search_window(monkeypatch, opened):
    import lib.ui.search as search

    class _Search:
        query = ''
        _metas = ()

        def start(self):
            opened.append(list(self._metas))
            return False

        def close(self):
            pass

    monkeypatch.setattr(search, 'open_window', lambda cls, xml: _Search())


def _detail_window(ctx, meta):
    win = ctx.detailwindow.DetailWindow('DetailWindow.xml', '/addon/path', 'Default', '1080i')
    win.meta = meta
    win.stype = 'series'
    win.onInit()
    return win


def _streams_window(ctx, meta):
    win = make_window(ctx.streamswindow.StreamsWindow)
    win.pairs = []
    win.meta = meta
    win.onInit()
    return win


def _chip_labels(win):
    return [(item.getLabel(), item.getProperty('category')) for item in win.getControl(CREDITS).items]


def _click_chip(win, position):
    win.getControl(CREDITS).selected_index = position
    win.onClick(CREDITS)


# ---------------------------------------------------------------------------
# Which chips, in what order
# ---------------------------------------------------------------------------


def test_chips_follow_stremios_group_order_whatever_the_addons_order():
    meta = _meta(
        _link('Writer One', 'Writers', _search('Writer One')),
        _link('Director One', 'Directors', _search('Director One')),
        _link('Actor One', 'Cast', _search('Actor One')),
        _link('Actor Two', 'Cast', _search('Actor Two')),
        _link('Drama', 'Genres', _GENRE_URL),
    )

    assert [(name, category) for name, category, _parsed in credit_links(meta)] == [
        ('Drama', 'Genres'), ('Actor One', 'Cast'), ('Actor Two', 'Cast'),
        ('Director One', 'Directors'), ('Writer One', 'Writers'),
    ]


def test_chip_categories_match_regardless_of_case_and_number_and_unknown_ones_come_last():
    meta = _meta(
        _link('Somebody', 'Collection', _search('Somebody')),
        _link('Writer One', 'writer', _search('Writer One')),
        _link('Drama', 'GENRES', _GENRE_URL),
    )

    assert [name for name, _category, _parsed in credit_links(meta)] == ['Drama', 'Writer One', 'Somebody']


def test_chips_skip_reserved_and_unusable_links():
    meta = _meta(
        _link('IMDb', 'imdb', 'https://www.imdb.com/title/tt1'),
        _link('', 'Cast', _search('x')),
        _link('Broken', 'Cast', 'not a link'),
        _link('Actor One', 'Cast', _search('Actor One')),
    )

    assert [name for name, _category, _parsed in credit_links(meta)] == ['Actor One']


# ---------------------------------------------------------------------------
# DetailWindow
# ---------------------------------------------------------------------------


def test_detail_window_lists_the_chips_with_their_category_and_shows_the_row(ctx):
    win = _detail_window(ctx, _meta(
        _link('Actor One', 'Cast', _search('Actor One')),
        _link('Drama', 'Genres', _GENRE_URL),
    ))

    assert _chip_labels(win) == [('Drama', 'Genres'), ('Actor One', 'Cast')]
    assert win.getControl(CREDITS).visible is True
    assert win.getFocusId() == ctx.detailwindow.LIST  # the episodes keep the initial focus


def test_detail_window_without_links_hides_the_row(ctx):
    win = _detail_window(ctx, _meta())

    assert win.getControl(CREDITS).visible is False
    assert win.getControl(CREDITS).items == []


def test_detail_window_clicking_a_person_chip_searches_that_name(ctx, monkeypatch):
    rec = _wire(monkeypatch, ctx)
    win = _detail_window(ctx, _meta(
        _link('Actor One', 'Cast', _search('Actor One')),
        _link('Drama', 'Genres', _GENRE_URL),
    ))

    _click_chip(win, 1)  # Genres first, then 'Actor One'

    assert rec.queries == [(rec.store, rec.client, 'Actor One')]
    assert rec.opened == [[{'id': 'tt2', 'name': 'Found', 'type': 'movie'}]]


def test_detail_window_clicking_a_genre_chip_fetches_that_catalog_for_an_installed_addon(ctx, monkeypatch):
    rec = _wire(monkeypatch, ctx, addons=[{'transportUrl': _TRANSPORT}])
    win = _detail_window(ctx, _meta(_link('Drama', 'Genres', _GENRE_URL)))

    _click_chip(win, 0)

    assert rec.fetches == [(_TRANSPORT, 'movie', 'top', [('genre', 'Drama')])]
    assert rec.opened == [[{'id': 'tt9', 'name': 'Drama', 'type': 'movie'}]]


def test_detail_window_clicking_a_genre_chip_of_an_uninstalled_addon_fetches_nothing(ctx, monkeypatch):
    rec = _wire(monkeypatch, ctx, addons=[{'transportUrl': 'https://other.example/manifest.json'}])
    win = _detail_window(ctx, _meta(_link('Drama', 'Genres', _GENRE_URL)))

    _click_chip(win, 0)

    assert rec.fetches == []
    assert rec.opened == []


def test_detail_window_click_on_an_empty_row_does_nothing(ctx, monkeypatch):
    rec = _wire(monkeypatch, ctx)
    win = _detail_window(ctx, _meta())

    win.onClick(CREDITS)

    assert rec.queries == []


# ---------------------------------------------------------------------------
# StreamsWindow (a movie)
# ---------------------------------------------------------------------------


def test_streams_window_shows_the_chips_for_a_movie(ctx):
    win = _streams_window(ctx, _meta(
        _link('Director One', 'Directors', _search('Director One')),
        _link('Drama', 'Genres', _GENRE_URL),
        type='movie',
    ))

    assert _chip_labels(win) == [('Drama', 'Genres'), ('Director One', 'Director')]
    assert win.getControl(CREDITS).visible is True
    assert win.getFocusId() == ctx.streamswindow.LIST


def test_a_caption_heads_its_group_only_and_unknown_categories_keep_the_addons_word(ctx):
    win = _streams_window(ctx, _meta(
        _link('Drama', 'Genres', _GENRE_URL),
        _link('Actor One', 'Cast', _search('Actor One')),
        _link('Actor Two', 'cast', _search('Actor Two')),
        _link('Saga', 'Collection', _search('Saga')),
        _link('Saga Two', 'Collection', _search('Saga Two')),
        type='movie',
    ))

    assert _chip_labels(win) == [
        ('Drama', 'Genres'), ('Actor One', 'Cast'), ('Actor Two', ''),
        ('Saga', 'Collection'), ('Saga Two', ''),
    ]


@pytest.mark.parametrize('meta', [None, {}, _meta()], ids=['no-meta', 'empty-meta', 'no-links'])
def test_streams_window_without_links_hides_the_row(ctx, meta):
    win = _streams_window(ctx, meta)

    assert win.getControl(CREDITS).visible is False


def test_streams_window_clicking_a_person_chip_searches_that_name(ctx, monkeypatch):
    rec = _wire(monkeypatch, ctx)
    win = _streams_window(ctx, _meta(_link('Director One', 'Directors', _search('Director One')), type='movie'))

    _click_chip(win, 0)

    assert rec.queries == [(rec.store, rec.client, 'Director One')]
    assert rec.opened == [[{'id': 'tt2', 'name': 'Found', 'type': 'movie'}]]


def test_streams_window_clicking_a_genre_chip_of_an_uninstalled_addon_fetches_nothing(ctx, monkeypatch):
    rec = _wire(monkeypatch, ctx)
    win = _streams_window(ctx, _meta(_link('Drama', 'Genres', _GENRE_URL), type='movie'))

    _click_chip(win, 0)

    assert rec.fetches == []
    assert rec.opened == []


def test_streams_window_ignores_a_chip_click_queued_during_another_pick(ctx, monkeypatch):
    """Kodi replays queued clicks; a second one must not open a second
    results window over the first."""
    rec = _wire(monkeypatch, ctx)
    win = _streams_window(ctx, _meta(_link('Director One', 'Directors', _search('Director One')), type='movie'))
    win._pick_in_progress = True

    _click_chip(win, 0)

    assert rec.queries == []


def test_streams_window_chip_click_releases_the_pick_guard(ctx, monkeypatch):
    _wire(monkeypatch, ctx)
    win = _streams_window(ctx, _meta(_link('Director One', 'Directors', _search('Director One')), type='movie'))

    _click_chip(win, 0)

    assert win._pick_in_progress is False


# ---------------------------------------------------------------------------
# The picker shares the dispatch
# ---------------------------------------------------------------------------


def test_picker_routes_the_chosen_link_through_open_credit_link(ctx, monkeypatch):
    dispatched = []
    monkeypatch.setattr(ctx.infowindow, 'open_credit_link', lambda store, client, parsed: dispatched.append((store, client, parsed)))
    stub_choose(monkeypatch, ctx, [1])
    store, client = object(), object()

    ctx.infowindow.open_credits_picker(store, client, _meta(
        _link('Actor One', 'Cast', _search('Actor One')),
        _link('Actor Two', 'Cast', _search('Actor Two')),
    ))

    assert dispatched == [(store, client, {'kind': 'search', 'query': 'Actor Two'})]


def test_picker_search_pick_still_searches_and_opens_results(ctx, monkeypatch):
    rec = _wire(monkeypatch, ctx)
    stub_choose(monkeypatch, ctx, [0])

    ctx.infowindow.open_credits_picker(rec.store, rec.client, _meta(_link('Actor One', 'Cast', _search('Actor One'))))

    assert rec.queries == [(rec.store, rec.client, 'Actor One')]
    assert len(rec.opened) == 1


@pytest.mark.parametrize('make_window_for', [_detail_window, _streams_window], ids=['detail', 'streams'])
def test_focus_returns_to_the_chip_after_the_results_window_closes(ctx, monkeypatch, make_window_for):
    """The result windows are modals over this one; on a real device the
    window beneath came back with nothing focused."""
    _wire(monkeypatch, ctx)
    win = make_window_for(ctx, _meta(_link('Actor One', 'Cast', _search('Actor One'))))

    _click_chip(win, 0)

    assert win.getFocusId() == CREDITS


# ---------------------------------------------------------------------------
# Skin navigation: the row is wired into both windows without trapping focus
# ---------------------------------------------------------------------------


def _nav(xml_name):
    import xml.etree.ElementTree as ET
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / 'resources/skins/Default/1080i' / xml_name
    return {
        int(c.get('id')): {d: int(c.findtext(d)) for d in ('onup', 'ondown', 'onleft', 'onright') if c.findtext(d)}
        for c in ET.parse(path).iter('control') if c.get('id')
    }


def test_series_page_row_sits_between_the_buttons_and_the_season_bar():
    """Under the title, above the season bar: the bar stays the episodes'
    own header."""
    nav = _nav('DetailWindow.xml')

    assert nav[30002]['onup'] == 30007 and nav[30007]['ondown'] == 30002
    assert nav[30007]['onup'] == CREDITS and nav[CREDITS]['ondown'] == 30007
    assert nav[CREDITS]['onup'] == 30009
    assert nav[30009]['ondown'] == nav[30012]['ondown'] == CREDITS
    assert nav[CREDITS]['onleft'] == nav[CREDITS]['onright'] == CREDITS  # scrolls, never escapes sideways


def test_movie_page_row_is_one_step_above_the_sources():
    nav = _nav('StreamsWindow.xml')

    assert nav[30002]['onup'] == CREDITS
    assert nav[CREDITS]['ondown'] == 30002 and nav[CREDITS]['onup'] == CREDITS
