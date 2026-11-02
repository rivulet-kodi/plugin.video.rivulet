from lib.stremio import lastvideos
from lib.stremio.addons import AddonError

CINEMETA = {
    'transportUrl': 'https://v3-cinemeta.strem.io/manifest.json',
    'manifest': {
        'id': 'com.linvo.cinemeta', 'types': ['movie', 'series'], 'idPrefixes': ['tt'],
        'resources': ['catalog', 'meta'],
        'catalogs': [
            {'type': 'series', 'id': 'top'},
            {'type': 'series', 'id': 'last-videos',
             'extra': [{'name': 'lastVideosIds', 'isRequired': True, 'optionsLimit': 2}]},
        ],
    },
}


def _series(sid, last='2026-10-01T00:00:00.000Z', mtime='2026-10-01T00:00:00Z', **extra):
    entry = {'_id': sid, 'type': 'series', 'removed': False, 'temp': False, '_mtime': mtime,
             'state': {'lastWatched': last}}
    entry.update(extra)
    return entry


def test_only_library_series_with_notifications_on_are_pulled_newest_first():
    entries = [
        _series('tt1', mtime='2026-10-01T00:00:00Z'),
        _series('tt2', mtime='2026-10-03T00:00:00Z'),
        _series('tt3', removed=True),
        _series('tt4', temp=True),
        _series('tt5', state={'noNotif': True}),
        _series('tt6', behaviorHints={'defaultVideoId': 'tt6'}),
        dict(_series('tt7'), type='movie'),
        dict(_series('tt8'), type='other'),
        'junk',
    ]
    assert lastvideos.pull_ids(entries) == [('tt2', 'series'), ('tt1', 'series')]


def test_requests_keep_the_newest_ids_within_the_catalog_limit_sorted():
    ids = [('tt3', 'series'), ('tt1', 'series'), ('tt2', 'series'), ('kitsu:1', 'series'), ('tt9', 'movie')]
    assert lastvideos.plan_requests([CINEMETA, 'junk'], ids) == [(
        CINEMETA['transportUrl'], 'series', 'last-videos', [('lastVideosIds', 'tt1,tt3')],
    )]


def test_answers_reduce_to_released_videos_and_the_first_catalog_wins():
    second = {'transportUrl': 'https://other.example/manifest.json', 'manifest': CINEMETA['manifest']}
    calls = []

    class Client:
        def catalog_detailed(self, base, rtype, cid, extra):
            calls.append(base)
            if base == second['transportUrl']:
                return [{'id': 'tt1', 'videos': [{'id': 'tt1:9:9', 'released': '2026-10-09T00:00:00Z'}]}]
            return [{'id': 'tt1', 'videos': [
                {'id': 'tt1:1:2', 'released': '2026-10-05T00:00:00.000Z'},
                {'id': 'tt1:1:1', 'released': '2026-09-28T00:00:00.000Z'},
                {'id': 'tt1:1:3'},
                {'id': '', 'released': '2026-10-06T00:00:00Z'},
                'junk',
            ]}, {'id': 'tt2', 'videos': []}, 'junk']

    got = lastvideos.fetch(Client(), [CINEMETA, second], [('tt1', 'series')])
    assert calls == [CINEMETA['transportUrl'], second['transportUrl']]
    assert got == {'tt1': [['tt1:1:1', '2026-09-28T00:00:00Z'], ['tt1:1:2', '2026-10-05T00:00:00Z']]}


def test_a_failing_catalog_is_skipped_and_reported():
    class Client:
        def catalog_detailed(self, *args):
            raise AddonError('down')

    logs = []
    assert lastvideos.fetch(Client(), [CINEMETA], [('tt1', 'series')], log=logs.append) == {}
    assert len(logs) == 1


def test_new_videos_are_released_after_the_last_watch_and_not_in_the_future():
    pairs = [['tt1:1:1', '2026-09-28T00:00:00Z'], ['tt1:1:2', '2026-10-05T00:00:00Z'],
             ['tt1:1:3', '2026-10-12T00:00:00Z'], ['tt1:1:4', '2026-11-20T00:00:00Z'], 'junk']
    now = '2026-11-03T00:00:00Z'
    assert lastvideos.new_videos(_series('tt1'), pairs, now) == pairs[1:3]
    # Never watched: Stremio raises no notification.
    assert lastvideos.new_videos(_series('tt1', state={}), pairs, now) == []
    assert lastvideos.new_videos(_series('tt1', removed=True), pairs, now) == []
    assert lastvideos.new_videos(_series('tt1'), None, now) == []
