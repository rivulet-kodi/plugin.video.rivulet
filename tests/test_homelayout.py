"""Home intent survives catalog churn
    resolution stays deterministic."""
import itertools
import json
import os

import pytest

from lib import homelayout as h
from lib.store import Store


def addon(catalogs, aid='a', name='Addon'):
    return {'transportUrl': aid, 'manifest': {'id': aid, 'name': name, 'catalogs': catalogs}}


def catalog(cid='top', ctype='movie', name=None, **kwargs):
    return dict({'id': cid, 'type': ctype, 'name': name or cid}, **kwargs)


def empty():
    return {'version': 1, 'tabs': {tab: [] for tab in h.TABS}}


def test_default_and_normalization():
    assert h.default_layout() == h.DEFAULT
    layout = empty()
    layout['tabs']['home'] = ['library', 'continue:movie', 'library', 'continue',
                              'unknown', None, 'cat:missing|movie|saved', 'cat:a|channel|x']
    assert h.normalize(layout)['tabs']['home'] == ['continue:movie', 'library', 'cat:missing|movie|saved']
    assert h.DEFAULT['tabs']['home'] == ['continue', 'new:movie', 'new:series']


@pytest.mark.parametrize('value', [None, [], {}, {'version': True, 'tabs': {}},
    {'version': 2, 'tabs': {}}, {'version': 1, 'tabs': {'oops': []}},
    {'version': 1, 'tabs': dict.fromkeys(h.TABS, 'bad')}])
def test_bad_envelopes_are_rejected(value):
    with pytest.raises(h.LayoutError):
        h.normalize(value)


def test_new_rule_whole_words_and_installed_order():
    cats = h.installed_catalogs([addon([catalog('renew'), catalog('latest films'), catalog('new')]),
                                 addon([catalog('fresh')], 'b')])
    plan = h.resolve(h.default_layout(), 'home', cats)
    assert plan[1][0] == 'cat:a|movie|latest films'
    assert plan[1][4] == (30529,)
    fallback = h.installed_catalogs([addon([catalog('renew'), catalog('archive')])])
    assert h.resolve(h.DEFAULT, 'home', fallback)[1][0] == 'cat:a|movie|renew'
    layout = empty()
    layout['tabs']['home'] = ['new:movie', 'rest:movie', 'cat:a|movie|new']
    ids = [row[0] for row in h.resolve(layout, 'home', cats)]
    assert ids == ['cat:a|movie|latest films', 'cat:a|movie|renew', 'cat:a|movie|new', 'cat:b|movie|fresh']


def test_catalog_eligibility_and_anime_split():
    cats = h.installed_catalogs([addon([
        catalog('channel', 'channel'), catalog('plain', 'anime'),
        catalog('films', 'anime.movie'), catalog('shows', 'anime.series'),
        catalog('search', extra=[{'name': 'search', 'isRequired': True}]),
        catalog('genre', extraRequired=['genre']), catalog('skip', extraRequired=['skip']),
        catalog('adult', adult=True),
    ])])
    assert [row['kind'] for row in cats] == ['anime-movie', 'anime-series', 'anime-movie', 'anime-series', 'movie']
    assert cats[0]['list_id'].endswith('|movie') and cats[1]['list_id'].endswith('|series')
    assert h.matches_catalog({'type': 'movie'}, 'movie')
    assert not h.matches_catalog({'type': 'anime.movie'}, 'movie')
    assert h.matches_catalog({'type': 'anime'}, 'series')
    assert h.matches_catalog({}, '')
    unhidden = h.installed_catalogs([addon([catalog('adult', adult=True)])], False)
    assert len(unhidden) == 1
    assert h.resolve({'version': 1, 'tabs': dict(empty()['tabs'], home=['new:movie'])}, 'home', unhidden) == []


def test_live_catalogs_keep_their_type_and_tv_shows_stay_series():
    cats = h.installed_catalogs([addon([
        catalog('football', 'sport', 'Football'), catalog('now', 'sports', 'Live Now'),
        catalog('ppv', 'events'), catalog('channels', 'tv', 'Live Channels'),
        catalog('top', 'tv', 'Popular'), catalog('uploads', 'channel'),
        catalog('today', 'sport', extraRequired=['genre']),
    ])])
    assert [(row['catalog']['id'], row['kind']) for row in cats] == [
        ('football', 'live'), ('now', 'live'), ('ppv', 'live'), ('channels', 'live'), ('top', 'series')]
    assert cats[0]['list_id'] == 'cat:a|sport|football'
    live = [row[0] for row in h.resolve(h.DEFAULT, 'live', cats)]
    assert live == ['cat:a|sport|football', 'cat:a|sports|now', 'cat:a|events|ppv', 'cat:a|tv|channels']
    assert 'cat:a|tv|top' in [row[0] for row in h.resolve(h.DEFAULT, 'series', cats)]
    assert not h.valid_id('new:live') and h.valid_id('rest:live')
    # A user can still place any eligible catalog on Live by hand.
    layout = empty()
    layout['tabs']['live'] = ['cat:a|tv|top']
    assert [row[0] for row in h.resolve(layout, 'live', cats)] == ['cat:a|tv|top']


@pytest.mark.parametrize('ctype,sid,variant', [('movie','tt1','movie'), ('tv','tt2','series'),
    ('series','tt3','series'), ('anime.movie','a','anime'), ('movie','kitsu:1','anime')])
def test_continue_anime_precedence(ctype, sid, variant):
    meta = {'type': ctype, 'id': sid}
    assert h.matches_continue(meta)
    assert [v for v in ('movie', 'series', 'anime') if h.matches_continue(meta, v)] == [variant]


@pytest.mark.parametrize('ctype,variant', [('movie','movie'), ('anime.movie','movie'),
    ('tv','series'), ('series','series'), ('anime','series'), ('anime.series','series')])
def test_library_split(ctype, variant):
    assert h.matches_library({'type': ctype})
    assert h.matches_library({'type': ctype}, variant)
    assert not h.matches_library({'type': ctype}, 'movie' if variant == 'series' else 'series')


@pytest.mark.parametrize('flags', list(itertools.product((False, True), repeat=4)))
def test_all_toggle_migrations(flags):
    keys = ('home_show_mystuff', 'home_show_movies', 'home_show_series', 'home_show_anime')
    layout = h.migrate(dict(zip(keys, flags)))
    rows = sum(layout['tabs'].values(), [])
    assert ('continue' in rows) is flags[0]
    assert ('new:movie' in rows) is flags[1]
    assert ('new:series' in rows) is flags[2]
    assert 'new:anime-movie' in rows
    assert 'rest:anime-series' in rows
    assert ('continue:movie' in rows) is (flags[0] and flags[1])
    assert ('continue:series' in rows) is (flags[0] and flags[2])
    assert ('continue:anime' in rows) is flags[0]
    assert layout['tabs']['mystuff'] == ['library:movie', 'library:series']


def test_editing_materializes_and_preserves_missing_addons():
    cats = h.installed_catalogs([addon([catalog('top'), catalog('latest'), catalog('third')])])
    layout = empty()
    layout['tabs']['home'] = ['continue', 'new:movie', 'rest:movie', 'cat:missing|movie|saved']
    layout = h.materialize(layout, 'home', cats)
    assert layout['tabs']['home'] == ['continue', 'cat:a|movie|latest', 'cat:a|movie|top', 'cat:a|movie|third', 'cat:missing|movie|saved']
    assert len(h.resolve(layout, 'home', cats)) == 4
    assert h.move(layout, 'home', 'continue', 1) == layout
    assert h.move(layout, 'home', 'cat:a|movie|latest', -1) == layout
    moved = h.move(layout, 'home', 'cat:a|movie|latest', 1)
    assert moved['tabs']['home'][1:3] == ['cat:a|movie|top', 'cat:a|movie|latest']
    assert h.move(moved, 'home', 'cat:a|movie|latest', -1) == layout
    moved = h.remove(moved, 'home', 'cat:a|movie|latest')
    assert 'cat:a|movie|latest' not in moved['tabs']['home']
    multi, changed = h.add(moved, 'mystuff', 'cat:a|movie|top')
    assert changed and 'cat:a|movie|top' in multi['tabs']['home'] and 'cat:a|movie|top' in multi['tabs']['mystuff']
    assert not h.add(multi, 'mystuff', 'cat:a|movie|top')[1]
    assert not h.add(multi, 'home', 'continue:anime')[1]
    assert h.add(multi, 'home', 'continue:anime', True)[0]['tabs']['home'][0] == 'continue:anime'
    assert h.reset() == h.DEFAULT


def test_row_cap_and_disambiguated_headings():
    cats = h.installed_catalogs([addon([catalog(str(i), name='Same') for i in range(14)]),
                                 addon([catalog('anime', 'anime', 'Same')], 'b', 'Second')])
    layout = empty()
    layout['tabs']['home'] = ['continue'] + [c['list_id'] for c in cats]
    plan = h.resolve(layout, 'home', cats)
    assert len(plan) == 13
    assert plan[1][4] == ('Same', 'Addon')
    assert not h.add(layout, 'home', 'library')[1]
    layout['tabs']['home'] = [cats[-1]['list_id']]
    parts = h.resolve(layout, 'home', cats)[0][4]
    assert parts == ('Same', 30536)
    assert h.heading_text(parts, lambda n: str(n)) == 'Same · 30536'
    assert h.move(layout, 'home', 'missing', -1) == layout
    assert not h.add(layout, 'home', 'invalid')[1]


@pytest.mark.parametrize('payload', ['{bad', '[]', '{}', '{"version":2,"tabs":{}}',
    json.dumps({'version': 1, 'tabs': dict.fromkeys(h.TABS, False)})])
def test_corrupt_files_reset_and_log_safely(tmp_path, payload, caplog):
    store = Store(str(tmp_path))
    (tmp_path / 'home_layout.json').write_text(payload)
    assert store.get_home_layout() == h.DEFAULT
    assert 'LayoutError' in caplog.text


def test_persistence_bypasses_cached_reads_and_same_size_rewrites(tmp_path, monkeypatch, caplog):
    first, second = Store(str(tmp_path)), Store(str(tmp_path))
    assert first.get_home_layout() == h.DEFAULT
    layout = empty()
    layout['tabs']['home'] = ['cat:a|movie|one']
    first.set_home_layout(layout)
    path = tmp_path / 'home_layout.json'
    stamp = path.stat().st_mtime_ns
    assert first.get_home_layout() == layout
    layout['tabs']['home'] = ['cat:a|movie|two']
    before_size = path.stat().st_size
    second.set_home_layout(layout)
    assert path.stat().st_size == before_size
    os.utime(str(path), ns=(stamp, stamp))
    monkeypatch.setattr(first, '_cached_read', lambda *args: pytest.fail('layout must read fresh'))
    assert first.get_home_layout() == layout
    assert first.set_home_layout({'bad': 'https://user:secret@host'}) == h.DEFAULT
    assert 'secret' not in caplog.text


@pytest.mark.parametrize('manifest,catalog,expected', [
    ({}, {'type': 'anime', 'id': 'x'}, True),
    ({}, {'type': 'anime.movie', 'id': 'x'}, True),
    ({}, {'type': 'movie', 'name': 'Top Anime', 'id': 'top'}, True),
    ({}, {'type': 'series', 'name': 'CRUNCHYROLL – Popular', 'id': 'top'}, True),
    ({}, {'type': 'tv', 'name': 'HiDive Picks', 'id': 'top'}, True),
    ({}, {'type': 'series', 'name': 'Popular', 'id': 'funimation_top'}, True),
    ({'id': 'org.kitsu'}, {'type': 'movie', 'id': 'top'}, True),
    ({'name': 'AniList Catalogs'}, {'type': 'series', 'id': 'top'}, True),
    ({'id': 'org.myanimelist'}, {'type': 'series', 'id': 'top'}, True),
    ({'name': 'AniDB'}, {'type': 'tv', 'id': 'top'}, True),
    ({'id': 'pw.ers.netflix-catalog'}, {'type': 'movie', 'id': 'cru'}, True),
    ({'id': 'pw.ers.netflix-catalog'}, {'type': 'series', 'id': 'cru'}, True),
    ({'id': 'pw.ers.netflix-catalog'}, {'type': 'movie', 'id': 'nfx', 'name': 'Netflix'}, False),
    ({}, {'type': 'movie', 'name': 'Animation'}, False),
    ({}, {'type': 'movie', 'name': 'Animated Movies'}, False),
    ({}, {'type': 'series', 'name': 'Animal Planet'}, False),
    ({}, {'type': 'series', 'name': 'Malcolm'}, False),
    ({}, {'type': 'series', 'name': 'Normal People'}, False),
    ({'id': 'org.mal', 'name': 'MAL Catalog'}, {'type': 'series', 'id': 'top'}, False),
    ({}, {'type': 'movie', 'id': 'anime', 'name': 'Top'}, False),
    ({'name': 'Anime sources'}, {'type': 'movie', 'id': 'top'}, False),
    ({'name': 'Crunchyroll'}, {'type': 'channel', 'id': 'top'}, False),
    ({}, {'type': 'movie', 'name': 'NotCrunchyroll Movies'}, False),
])
def test_anime_source_classifier_uses_only_the_approved_words(manifest, catalog, expected):
    assert h.is_anime_source(manifest, catalog) is expected


def test_crunchyroll_defaults_to_anime_but_explicit_placements_win():
    installed = [{'transportUrl': 'streaming', 'manifest': {'id': 'pw.ers.netflix-catalog', 'catalogs': [
        catalog('nfx', 'movie', 'Netflix'), catalog('hbm', 'series', 'HBO Max'),
        catalog('cru', 'movie', 'Crunchyroll'), catalog('cru', 'series', 'Crunchyroll'),
    ]}}]
    cats = h.installed_catalogs(installed)
    anime = [entry for entry in cats if entry['catalog']['id'] == 'cru']
    assert [entry['kind'] for entry in anime] == ['anime-movie', 'anime-series']
    assert all(entry['filter'] == '' for entry in anime)
    assert [entry['list_id'] for entry in anime] == ['cat:streaming|movie|cru', 'cat:streaming|series|cru']
    layout = h.default_layout()
    for tab in ('home', 'movies', 'series'):
        assert not any(entry[2] and entry[2]['catalog']['id'] == 'cru' for entry in h.resolve(layout, tab, cats))
    assert [entry[0] for entry in h.resolve(layout, 'anime', cats) if entry[1] == 'catalog'] == [entry['list_id'] for entry in anime]
    layout['tabs']['series'].append('cat:streaming|series|cru')
    assert 'cat:streaming|series|cru' in [entry[0] for entry in h.resolve(layout, 'series', cats)]


def test_missing_and_extra_tabs_cost_only_those_tabs():
    layout = empty()
    layout['tabs']['home'] = ['library']
    del layout['tabs']['anime']
    layout['tabs']['future'] = ['library']
    result = h.normalize(layout)
    assert result['tabs']['home'] == ['library']
    assert result['tabs']['anime'] == h.DEFAULT['tabs']['anime']
    assert 'future' not in result['tabs']


@pytest.mark.parametrize('payload', ['{bad', '{"version":2,"tabs":{}}'])
def test_rejected_layout_is_moved_aside_once_and_not_relogged(tmp_path, payload, caplog):
    store = Store(str(tmp_path))
    path = tmp_path / 'home_layout.json'
    path.write_text(payload)
    assert store.get_home_layout() == h.DEFAULT
    assert (tmp_path / 'home_layout.json.bak').read_text() == payload
    assert not path.exists()
    caplog.clear()
    assert store.get_home_layout() == h.DEFAULT
    assert caplog.text == ''


def test_unreadable_layout_is_left_untouched(tmp_path, monkeypatch):
    import builtins
    store = Store(str(tmp_path))
    layout = empty()
    layout['tabs']['home'] = ['cat:a|movie|one']
    store.set_home_layout(layout)
    real_open = builtins.open

    def flaky(file, *args, **kwargs):
        if str(file).endswith('home_layout.json'):
            raise PermissionError(13, 'in use')
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, 'open', flaky)
    assert store.get_home_layout() == h.DEFAULT
    monkeypatch.undo()
    assert store.get_home_layout() == layout
    assert not (tmp_path / 'home_layout.json.bak').exists()
