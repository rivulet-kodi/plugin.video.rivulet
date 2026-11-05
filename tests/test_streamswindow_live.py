# SPDX-FileCopyrightText: 2026 M0Rf30
# SPDX-License-Identifier: MIT

"""One-click open of a live channel (`streamswindow.open_live`).

stremio-core opens a live item on its stream list and the official apps
play at once when the add-ons offer a single source. The decision is the
pure `live_play_choice`; `open_live` gathers every add-on's answer first,
so a second, slower source is never silently skipped.
"""
import contextlib

import pytest

from tests.kodistubs import install_kodi_stubs

_RELOAD_MODULE_NAMES = (
    'lib.ui.compat', 'lib.ui.dependencies', 'lib.ui.uicommon', 'lib.ui.dialogs', 'lib.ui.player',
    'lib.ui.streamswindow',
)


@pytest.fixture
def load_streamswindow():
    with contextlib.ExitStack() as stack:
        def _load():
            return stack.enter_context(install_kodi_stubs(reload=_RELOAD_MODULE_NAMES))

        yield _load


def _pairs(count):
    return [({'name': 'src%d' % index}, {'url': 'https://s/%d.m3u8' % index}) for index in range(count)]


@pytest.mark.parametrize('count, expected', [(0, 'none'), (1, 'play'), (2, 'pick'), (7, 'pick')])
def test_live_play_choice(load_streamswindow, count, expected):
    sw = load_streamswindow().streamswindow
    assert sw.live_play_choice(_pairs(count)) == expected
    assert sw.live_play_choice(None if count == 0 else _pairs(count)) == expected


def _wire(sw, monkeypatch, ctx, pairs, play_ok=True):
    log = []
    monkeypatch.setattr(sw, '_gather_stream_pairs',
                        lambda stype, sid, meta=None: log.append(('fetch', stype, sid)) or (pairs, False))
    monkeypatch.setattr(sw, 'new_playback_end_watcher', lambda: log.append('watcher') or 'W')

    def play_direct(stream, stype, sid, item_meta=None, on_ready=None, video_id=None):
        log.append(('play', stream, stype, sid, item_meta, on_ready, video_id))
        return play_ok

    monkeypatch.setattr(ctx.player, 'play_direct', play_direct)
    monkeypatch.setattr(sw, '_wait_for_playback_end', lambda player=None: log.append(('wait', player)) or (True, False))
    monkeypatch.setattr(sw, 'end_player_phase', lambda: log.append('end'))
    monkeypatch.setattr(sw, 'open_streams', lambda *a, **k: log.append(('open_streams', a, k)) or False)
    return log


def test_a_single_stream_plays_at_once_and_the_screens_return_after(load_streamswindow, monkeypatch):
    ctx = load_streamswindow()
    sw = ctx.streamswindow
    pairs = _pairs(1)
    log = _wire(sw, monkeypatch, ctx, pairs)
    meta = {'id': 'ch1', 'name': 'One', 'type': 'tv'}

    assert sw.open_live('tv', 'ch1', poster='https://x/p.png', heading='One', meta=meta) is False

    kinds = [entry if isinstance(entry, str) else entry[0] for entry in log]
    assert kinds == ['fetch', 'watcher', 'play', 'wait', 'end']  # the watcher exists before play
    play = log[2]
    assert play[1] is pairs[0][1] and play[2:4] == ('tv', 'ch1')
    assert play[5] is sw.close_windows_for_playback and play[6] == 'ch1'
    assert play[4]['label'] == 'One' and play[4]['art'] == {'poster': 'https://x/p.png'} and play[4]['meta'] is meta
    assert log[3] == ('wait', 'W')


def test_several_streams_open_the_sources_page_over_the_same_answers(load_streamswindow, monkeypatch):
    ctx = load_streamswindow()
    sw = ctx.streamswindow
    pairs = _pairs(3)
    log = _wire(sw, monkeypatch, ctx, pairs)

    sw.open_live('tv', 'ch1', poster='p', heading='One', art={'poster': 'p'}, meta={'id': 'ch1'})

    assert [entry[0] if isinstance(entry, tuple) else entry for entry in log] == ['fetch', 'open_streams']
    _name, args, kwargs = log[1]
    assert args == ('tv', 'ch1')
    assert kwargs['prefetched'] is pairs and kwargs['video_id'] == 'ch1' and kwargs['heading'] == 'One'


def test_no_stream_tells_the_viewer(load_streamswindow, monkeypatch):
    ctx = load_streamswindow()
    sw = ctx.streamswindow
    log = _wire(sw, monkeypatch, ctx, [])

    assert sw.open_live('tv', 'ch1') is False

    assert ctx.env.notifications == [('Rivulet', 'STR30030', 'info', 4000)]
    assert log == [('fetch', 'tv', 'ch1')]


def test_a_failed_resolution_waits_for_nothing(load_streamswindow, monkeypatch):
    ctx = load_streamswindow()
    sw = ctx.streamswindow
    log = _wire(sw, monkeypatch, ctx, _pairs(1), play_ok=False)

    assert sw.open_live('tv', 'ch1') is False

    assert [entry if isinstance(entry, str) else entry[0] for entry in log] == ['fetch', 'watcher', 'play']


def test_open_streams_on_prefetched_pairs_does_no_second_lookup(load_streamswindow, monkeypatch):
    ctx = load_streamswindow()
    sw = ctx.streamswindow

    def forbidden(*_a, **_k):
        raise AssertionError('the add-ons were already asked')

    monkeypatch.setattr(sw, '_stream_targets', forbidden)
    monkeypatch.setattr(sw, '_start_stream_fetch_workers', forbidden)
    monkeypatch.setattr(sw, '_meta_with_imdb', forbidden)
    seen = {}

    class Recording(sw.StreamsWindow):
        def start(self, pairs, stype, sid, poster=None, heading='', art=None, meta=None, video_id=None):
            seen.update(pairs=list(pairs), sid=sid, video_id=video_id, loading=self._loading_flag())
            return False

        def _loading_flag(self):
            return self.getProperty('loading')

    monkeypatch.setattr(sw, 'StreamsWindow', Recording)
    pairs = _pairs(2)

    assert sw.open_streams('tv', 'ch1', video_id='ch1', prefetched=pairs) is False

    assert sorted(info['name'] for info, _stream in seen['pairs']) == ['src0', 'src1']
    assert seen['video_id'] == 'ch1'
    assert seen['loading'] in ('', None)  # nothing is still streaming in


def test_backing_out_of_the_spinner_plays_nothing_and_says_nothing(load_streamswindow, monkeypatch):
    """Back on the spinner leaves only the early answers in `pairs`: one of
    them must not play, and an empty set must not claim "no streams"."""
    ctx = load_streamswindow()
    sw = ctx.streamswindow
    state = {'cancelled': False}

    class Dialog:
        def iscanceled(self):
            return state['cancelled']

        def update(self, *args):
            pass

    @contextlib.contextmanager
    def busy(*args, **kwargs):
        yield Dialog()

    class Consumer:
        consumed = 1

        def __init__(self, results, total, pairs, failures):
            self.pairs = pairs

        def __call__(self, cancelled):
            self.pairs.extend(_pairs(1))  # one add-on answered ...
            state['cancelled'] = True  # ... then the viewer pressed Back
            return ('src0', None)

    played = []
    monkeypatch.setattr(sw, '_stream_targets', lambda stype, sid, meta: ['addon'])
    monkeypatch.setattr(sw, '_start_stream_fetch_workers', lambda stype, sid, addons: None)
    monkeypatch.setattr(sw, '_StreamAnswerConsumer', Consumer)
    monkeypatch.setattr(sw, 'busy_dialog', busy)
    monkeypatch.setattr(ctx.player, 'play_direct', lambda *a, **k: played.append(a) or True)
    monkeypatch.setattr(sw, 'open_streams', lambda *a, **k: played.append('picker'))

    assert sw.open_live('tv', 'ch1') is False

    assert played == [] and ctx.env.notifications == []
