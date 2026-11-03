"""The stock catalog-opening flow shared by the picker and Home rows."""
import pytest

from lib.stremio.addons import AddonError
from tests.test_home import load_homewindow as load_homewindow
from tests.test_home_tabs import build


def test_split_anime_filters_first_and_later_pages_under_the_existing_spinner(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    from lib.ui import infowindow
    picker = ctx.catalogpicker
    pages = [[{'id': 's0', 'type': 'series'}],
             [{'id': 'm1', 'type': 'movie'}, {'id': 's1', 'type': 'anime'}],
             [{'id': 's2', 'type': 'series'}, {'id': 'm2', 'type': 'movie'}]]
    calls, shown, details, spinner = [], [], [], []
    monkeypatch.setattr(ctx.views, 'iter_catalog_pages', lambda *args, **kw: calls.append((args, kw)) or iter(pages))
    monkeypatch.setattr(picker, 'run_cancellable', lambda fn, heading: spinner.append(heading) or fn())

    def showcase(metas, catalog_title='', more_pages=None):
        shown.append((metas, catalog_title, list(more_pages)))
        return metas[0]

    monkeypatch.setattr(infowindow, 'open_showcase', showcase)
    monkeypatch.setattr(ctx.detailwindow, 'open_detail', lambda *args: details.append(args) or True)
    manifest = {'name': 'Addon'}
    catalog = {'type': 'anime', 'id': 'top', 'name': 'Top'}
    assert picker.open_single_catalog('t', manifest, catalog, extra=[('genre', 'Drama')], meta_filter='movie') is True
    assert spinner == ['STR30033']
    assert calls == [(('t', 'anime', 'top'), {'extra': [('genre', 'Drama')], 'catalog': catalog, 'manifest': manifest})]
    assert shown == [([{'id': 'm1', 'type': 'movie'}], 'Addon · Top', [[{'id': 'm2', 'type': 'movie'}]])]
    assert details == [('movie', 'm1')]
    assert list(picker._filtered_catalog_pages(iter(pages), 'series')) == [pages[0], [pages[1][1]], [pages[2][0]]]


@pytest.mark.parametrize('outcome', ['cancel', 'empty', 'error', 'back'])
def test_home_keeps_open_with_existing_catalog_cancel_empty_and_error_behavior(load_homewindow, monkeypatch, outcome):
    ctx = load_homewindow()
    from lib.ui import infowindow
    win, _, _ = build(ctx, monkeypatch)
    picker = ctx.catalogpicker

    def pages(*args, **kwargs):
        if outcome == 'error':
            raise AddonError('https://u:SECRET@host/path?key=TOKEN')
        if outcome == 'back':
            yield [{'id': 'm1', 'type': 'movie'}]

    monkeypatch.setattr(ctx.views, 'iter_catalog_pages', pages)
    monkeypatch.setattr(picker, 'run_cancellable', lambda fn, heading: picker.CANCELLED if outcome == 'cancel' else fn())
    shown = []
    monkeypatch.setattr(infowindow, 'open_showcase', lambda *args, **kwargs: shown.append(True) or None)
    ctx.home._browse_catalog(win, win._rows[30041])
    assert not win.closed
    messages = [message for _, message, _, _ in ctx.env.notifications]
    if outcome in ('empty', 'error'):
        assert ('STR30030' if outcome == 'empty' else 'STR30032') in messages
    else:
        assert messages == []
    assert shown == ([True] if outcome == 'back' else [])
    assert all('SECRET' not in message and 'TOKEN' not in message for message, _ in ctx.env.log_calls)
