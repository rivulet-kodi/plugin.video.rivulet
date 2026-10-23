# SPDX-FileCopyrightText: 2026 M0Rf30
# SPDX-License-Identifier: MIT

"""Poster texture warm: posters only, four at a time, abort stops it."""
import contextlib

import pytest

from tests.kodistubs import install_kodi_stubs

_RELOAD = ('lib.ui.compat', 'lib.ui.posterwarm')


@pytest.fixture
def load_warm():
    with contextlib.ExitStack() as stack:
        def _load(**kwargs):
            return stack.enter_context(install_kodi_stubs(reload=_RELOAD, **kwargs))

        yield _load


def test_collect_keeps_posters_and_ignores_backgrounds():
    from lib.ui.posterwarm import collect_poster_urls

    groups = [
        [{'poster': 'p%d' % index, 'background': 'b%d' % index, 'logo': 'l%d' % index} for index in range(40)],
        [],
        [{'poster': 's%d' % index, 'background': 'fan'} for index in range(2)],
        [{'poster': 'third', 'background': 'only-fan'}],
        [{'poster': 'too-far'}],
    ]
    urls = collect_poster_urls(groups, limit_rows=3, limit_titles=30)
    assert urls[0] == 'p0'
    assert len([url for url in urls if url.startswith('p')]) == 30
    assert 's0' in urls and 's1' in urls and 'third' in urls
    assert all(not url.startswith('b') and not url.startswith('l') and url != 'fan' for url in urls)
    assert 'too-far' not in urls
    assert 'only-fan' not in urls


class _Slot:
    def __init__(self):
        self.images = []

    @property
    def image(self):
        return self.images[-1] if self.images else None

    def setImage(self, url):
        self.images.append(url)


class _Screen:
    """A Home or hub window: the eight warm slots its XML declares."""

    def __init__(self, missing=False):
        from lib.ui.posterwarm import WARM_SLOTS

        self.missing = missing
        self.slots = {control_id: _Slot() for control_id in WARM_SLOTS}

    def getControl(self, control_id):
        if self.missing:
            raise RuntimeError('Non-Existent Control %d' % control_id)
        return self.slots[control_id]


def test_the_gui_side_paints_a_batch_and_acknowledges_it(load_warm):
    """The worker hands batches over; only the GUI thread touches slots."""
    load_warm()

    from lib.ui import posterwarm

    posterwarm.reset_warmer()
    screen = _Screen()
    urls = ['https://img.example/%d.jpg' % index for index in range(5)]

    ticket = posterwarm._post_batch(screen, urls)
    assert not ticket['done'].is_set()
    assert all(slot.image is None for slot in screen.slots.values())  # nothing painted off the GUI thread

    posterwarm.apply_pending(screen)

    painted = [screen.slots[control_id].image for control_id in posterwarm.WARM_SLOTS]
    assert painted == urls + [''] * (len(posterwarm.WARM_SLOTS) - len(urls))
    assert ticket['done'].is_set() and ticket['ok'] is True


def test_a_closing_screen_paints_nothing_and_releases_the_worker(load_warm):
    import threading

    load_warm()

    from lib.ui import posterwarm

    posterwarm.reset_warmer()
    screen = _Screen()
    screen._home_stop = threading.Event()
    pending = posterwarm._post_batch(screen, ['u1'])
    screen._home_stop.set()

    posterwarm.apply_pending(screen)

    assert pending['done'].is_set() and pending['ok'] is False
    assert all(slot.image is None for slot in screen.slots.values())

    # Closing with a batch still queued must not strand the worker either.
    queued = posterwarm._post_batch(screen, ['u2'])
    posterwarm.stop(screen)
    assert queued['done'].is_set()
    assert all(slot.image == '' for slot in screen.slots.values())


def test_a_screen_without_slots_acknowledges_without_painting(load_warm):
    """A skin with no warm slots, or a window that has closed: the worker
    is released and told the batch did not land."""
    load_warm()

    from lib.ui import posterwarm

    posterwarm.reset_warmer()
    screen = _Screen(missing=True)
    ticket = posterwarm._post_batch(screen, ['u1'])
    posterwarm.apply_pending(screen)
    assert ticket['done'].is_set() and ticket['ok'] is False


def test_new_home_declares_the_warm_slots():
    import os
    import xml.etree.ElementTree as ET

    from lib.ui.posterwarm import WARM_SLOTS

    skin = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'resources', 'skins', 'Default', '1080i',
    )
    for name in ('NewHomeWindow.xml',):
        root = ET.parse(os.path.join(skin, name)).getroot()
        for control_id in WARM_SLOTS:
            control = root.find(".//control[@id='%d']" % control_id)
            assert control is not None, (name, control_id)
            assert control.get('type') == 'image', name
            assert control.findtext('width') == '1' and control.findtext('height') == '1', name
            assert control.find('visible') is None, name


def test_seen_posters_are_capped_oldest_first(load_warm, monkeypatch):
    ctx = load_warm()
    warmer = ctx.posterwarm
    # A worker already owns the slots: exercise queueing without a thread.
    monkeypatch.setattr(warmer, '_running', True)
    urls = ['poster%d' % i for i in range(warmer._MAX_SEEN + 5)]
    warmer.request_warm(None, urls)
    assert len(warmer._seen) == warmer._MAX_SEEN
    assert urls[0] not in warmer._seen and urls[-1] in warmer._seen
    warmer.reset_warmer()


def test_the_queued_target_does_not_keep_a_closed_window_alive(load_warm, monkeypatch):
    """A module global owning the window would keep Home, and every
    `ListItem` it holds, alive until interpreter teardown."""
    import gc
    import weakref

    ctx = load_warm()
    warmer = ctx.posterwarm
    monkeypatch.setattr(warmer, '_running', True)  # a worker owns the slots: no thread here
    screen = _Screen()
    gone = weakref.ref(screen)
    warmer.request_warm(screen, ['https://img.example/1.jpg'])
    del screen
    gc.collect()
    assert gone() is None
    warmer.reset_warmer()


def test_join_waits_only_for_the_worker_serving_that_window(load_warm):
    import gc
    import threading
    import time
    import weakref

    ctx = load_warm()
    warmer = ctx.posterwarm
    warmer.reset_warmer()
    screen, other = _Screen(), _Screen()
    screen._home_stop = threading.Event()
    gone = weakref.ref(screen)
    before = set(threading.enumerate())
    warmer.request_warm(screen, ['https://img.example/1.jpg'])
    deadline = time.monotonic() + 5
    while not getattr(screen, '_poster_warm_ready', None) and time.monotonic() < deadline:
        time.sleep(0.01)  # the worker is holding its first batch for the GUI thread
    assert screen._poster_warm_ready
    worker, = [t for t in threading.enumerate()
               if t not in before and t.name == 'rivulet-poster-warm']

    started = time.monotonic()
    warmer.join(other, 5)  # holds nothing of `other`: must not wait for `screen`'s worker
    assert time.monotonic() - started < 1 and worker.is_alive()

    screen._home_stop.set()
    warmer.stop(screen)
    warmer.join(screen, 5)

    assert not worker.is_alive()
    del screen
    gc.collect()
    assert gone() is None
    warmer.reset_warmer()
