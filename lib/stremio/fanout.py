# SPDX-FileCopyrightText: 2026 M0Rf30
# SPDX-License-Identifier: MIT

"""Bounded, ordered fan-out over raw daemon threads (pure Python, no
``xbmc*`` imports).

`lib.ui.views._fan_out()` is the same daemon-thread + `queue.Queue`
pattern, but it lives in `lib.ui` and `lib.stremio` must not import
upward (see AGENTS.md "Kodi-independence rule"), so the protocol layer
carries this small copy. Each call site still passes its OWN worker cap:
every fan-out point bounds itself because this runs on low-power ARM
boxes - do not fold those caps into one constant.
"""
import queue
import threading


def map_bounded(fn, items, max_workers):
    """`[fn(item) for item in items]`, run over up to `max_workers` raw
    daemon `threading.Thread` workers pulling from a `queue.Queue`, with
    the results in INPUT order whatever order the workers finish in.

    Deliberately NOT `concurrent.futures.ThreadPoolExecutor`:
    `concurrent.futures.thread` registers an atexit hook that JOINS every
    worker at interpreter shutdown regardless of the daemon flag, so one
    addon request still inside its 15s timeout when Kodi quits would hold
    the Python interpreter (and Kodi's own shutdown) hostage. A daemon
    worker is simply dropped at exit.

    Blocks until every item has answered, exactly like `Executor.map()`
    did. An exception raised by `fn` is re-raised here for the FIRST
    failing item in input order (also like `Executor.map()`), after the
    rest have finished - callers whose `fn` must not sink its siblings
    catch inside `fn`.
    """
    items = list(items)
    if not items:
        return []
    work = queue.Queue()
    for index, item in enumerate(items):
        work.put((index, item))
    done = queue.Queue()

    def _worker():
        while True:
            try:
                index, item = work.get_nowait()
            except queue.Empty:
                return
            try:
                done.put((index, True, fn(item)))
            # BaseException, not Exception: a SystemExit/GeneratorExit in
            # one worker would otherwise post no outcome, and the collector
            # below would wait for it forever.
            except BaseException as exc:  # noqa: B036 - handed to the caller below
                done.put((index, False, exc))

    for _ in range(min(len(items), max_workers)):
        threading.Thread(target=_worker, daemon=True).start()

    outcomes = [None] * len(items)
    for _ in items:
        index, ok, value = done.get()
        outcomes[index] = (ok, value)
    results = []
    for ok, value in outcomes:
        if not ok:
            raise value
        results.append(value)
    return results
