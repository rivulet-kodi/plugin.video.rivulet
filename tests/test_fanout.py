# SPDX-FileCopyrightText: 2026 M0Rf30
# SPDX-License-Identifier: MIT

"""Tests for lib.stremio.fanout.map_bounded (daemon-thread ordered map)."""
import threading

import pytest

from lib.stremio.fanout import map_bounded


def test_map_bounded_empty_input_spawns_nothing():
    assert map_bounded(lambda x: x, [], 4) == []


def test_map_bounded_returns_input_order_when_workers_finish_out_of_order():
    """The first item blocks until the last one has answered, so completion
    order is the reverse of input order; the result must still be input
    order (collect_subtitles()'s first-occurrence-wins dedup and
    fetch_addon_catalogs()'s entry order rely on it)."""
    last_done = threading.Event()

    def fn(item):
        if item == 0:
            assert last_done.wait(5.0)
        elif item == 2:
            last_done.set()
        return item * 10

    assert map_bounded(fn, [0, 1, 2], 3) == [0, 10, 20]


def test_map_bounded_never_runs_more_than_max_workers_at_once():
    lock = threading.Lock()
    state = {'now': 0, 'peak': 0}

    def fn(item):
        with lock:
            state['now'] += 1
            state['peak'] = max(state['peak'], state['now'])
        threading.Event().wait(0.02)
        with lock:
            state['now'] -= 1
        return item

    assert map_bounded(fn, list(range(10)), 2) == list(range(10))
    assert state['peak'] <= 2


def test_map_bounded_workers_are_daemon_threads():
    """A non-daemon worker would hold interpreter shutdown for as long as a
    hung addon request - the reason ThreadPoolExecutor is forbidden."""
    seen = []

    def fn(_item):
        seen.append(threading.current_thread().daemon)

    map_bounded(fn, [1, 2, 3], 2)
    assert seen and all(seen)


def test_map_bounded_reraises_first_failure_in_input_order():
    def fn(item):
        if item in (1, 2):
            raise ValueError('boom %d' % item)
        return item

    with pytest.raises(ValueError, match='boom 1'):
        map_bounded(fn, [0, 1, 2], 3)


def test_map_bounded_returns_when_a_worker_raises_a_base_exception():
    def fn(item):
        if item == 1:
            raise SystemExit('stop')
        return item

    with pytest.raises(SystemExit):
        map_bounded(fn, [0, 1, 2], 2)
