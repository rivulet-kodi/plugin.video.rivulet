# SPDX-FileCopyrightText: 2026 M0Rf30
# SPDX-License-Identifier: MIT

"""Warm Kodi's texture cache with poster URLs only.

Home carries eight 1x1 image controls (``WARM_SLOTS``)
under their backdrop plate. The GUI hands those slots the posters of
the first rows, eight at a time, so Kodi downloads and caches them before
the remote reaches them. A worker queues each batch and the GUI applies it
on Action(noop), before the per-batch hold begins. Fanart and logos are not warmed: a background
decodes toward fanartres, and ninety of those plus the video cache is how
a 1 GB box gets tight. Posters are not downloaded into ``addon_data`` and
``Textures13.db`` is not written from Python. Kodi fills its own cache
when a visible control is given the URL.

This used to open a separate ``xbmcgui.Window`` for the slots. Kodi
refuses to activate a window while a modal dialog is up ("Activate of
window ... refused because there are active modal dialogs"), and every
Rivulet screen is one, so the slots never rendered and nothing was
cached. Closing that window also sent Kodi's previous-window message to
whatever was underneath. The slots now live in the screen that asked.

Each batch is held for ``_HOLD_SECONDS``. Kodi cancels a queued texture
job when its control lets go of the URL, so a batch swapped out too fast
is a batch that never loaded. The thread waits on ``Monitor.waitForAbort``
so a Kodi shutdown is not stuck behind the queue. One warmer runs at a
time; further requests add to that queue and Home's slots carry the rest.
"""

import threading

#: Image controls in NewHomeWindow.xml. 1x1, visible,
#: drawn under the opaque backdrop plate.
WARM_SLOTS = tuple(range(30090, 30098))

_HOLD_SECONDS = 1.0
_TITLES = 30
_ROWS = 3

_lock = threading.Lock()
_seen = {}  # type: dict
_MAX_SEEN = 2000
_queue = []
_target = {'window': None}
_running = False


def collect_poster_urls(groups, limit_rows=_ROWS, limit_titles=_TITLES):
    """Poster URLs from the first ``limit_rows`` non-empty groups.

    Each group is a list of metas or of objects with a ``poster``
    string. ``background`` and ``logo`` are ignored even when present.
    """
    urls = []
    rows = 0
    for group in groups or []:
        found = []
        for meta in group or []:
            if len(found) >= limit_titles:
                break
            poster = None
            if isinstance(meta, dict):
                poster = meta.get('poster')
            elif hasattr(meta, 'getProperty'):
                poster = meta.getProperty('thumbnail') or meta.getProperty('poster')
            if isinstance(poster, str) and poster:
                found.append(poster)
        if not found:
            continue
        urls.extend(found)
        rows += 1
        if rows >= limit_rows:
            break
    return urls


def _slots(window):
    """The window's warm controls, or [] when it has none (another skin,
    or a window that has already closed)."""
    if window is None:
        return []
    found = []
    for control_id in WARM_SLOTS:
        try:
            found.append(window.getControl(control_id))
        except Exception:
            return []
    return found


def _clear(slots):
    for slot in slots:
        try:
            slot.setImage('')
        except Exception:
            pass


def request_warm(window, urls):
    """Queue poster URLs for ``window``'s slots. A URL already queued
    this process is skipped. The worker is a daemon and stops with
    ``waitForAbort``.
    """
    global _running
    fresh = []
    with _lock:
        for url in urls or []:
            if not isinstance(url, str) or not url or url in _seen:
                continue
            _seen[url] = None
            while len(_seen) > _MAX_SEEN:
                del _seen[next(iter(_seen))]
            fresh.append(url)
        if not fresh:
            return
        _queue.extend(fresh)
        if window is not None:
            _target['window'] = window
        if _running:
            return
        _running = True
    threading.Thread(target=_drain, name='rivulet-poster-warm', daemon=True).start()


def _post_batch(window, urls):
    """A ticket is acknowledged only after the GUI has applied the batch."""
    ticket = {'urls': list(urls), 'done': threading.Event(), 'ok': False}
    with _lock:
        ready = getattr(window, '_poster_warm_ready', [])
        window._poster_warm_ready = ready
        ready.append(ticket)
    try:
        import xbmc
        xbmc.executebuiltin('Action(noop)')
    except Exception:
        pass
    return ticket


def apply_pending(window):
    """GUI thread only: resolve native controls, paint batches and acknowledge."""
    with _lock:
        ready = getattr(window, '_poster_warm_ready', [])
        window._poster_warm_ready = []
    if not ready:
        return
    slots = _slots(window)
    stop = getattr(window, '_home_stop', None)
    for ticket in ready:
        try:
            if slots and not (stop is not None and stop.is_set()):
                for index, slot in enumerate(slots):
                    urls = ticket['urls']
                    slot.setImage(urls[index] if index < len(urls) else '')
                ticket['ok'] = True
        except Exception:
            ticket['ok'] = False
        finally:
            ticket['done'].set()


def stop(window):
    """Called on Home's GUI thread; no queued or held image survives close."""
    with _lock:
        ready = getattr(window, '_poster_warm_ready', [])
        window._poster_warm_ready = []
        if _target['window'] is window:
            _queue.clear()
            _target['window'] = None
    for ticket in ready:
        ticket['done'].set()
    _clear(_slots(window))


def _queued_warm(window, urls):
    from lib.ui.background import monitor as shared_monitor

    if window is None:
        return
    monitor = shared_monitor()
    stop = getattr(window, '_home_stop', None)
    def stopped():
        return (stop is not None and stop.is_set()) or monitor.abortRequested()
    try:
        for start in range(0, len(urls), len(WARM_SLOTS)):
            if stopped():
                return
            ticket = _post_batch(window, urls[start:start + len(WARM_SLOTS)])
            while not ticket['done'].wait(1.0):
                if stopped():
                    return
            if stopped() or not ticket['ok']:
                return
            if stop is not None:
                if stop.wait(_HOLD_SECONDS) or monitor.abortRequested():
                    return
            elif monitor.waitForAbort(_HOLD_SECONDS):
                return
    finally:
        if stop is None or not stop.is_set():
            _post_batch(window, [])


def _drain():
    global _running
    while True:
        with _lock:
            batch = list(_queue)
            del _queue[:]
            window = _target['window']
            stop = getattr(window, '_home_stop', None)
            if stop is not None and stop.is_set():
                del _queue[:]
                _target['window'] = None
                _running = False
                return
            if not batch:
                _running = False
                return
        try:
            _queued_warm(window, batch)
        except Exception as exc:
            import xbmc

            from lib.ui.compat import log

            log('posterwarm: %s' % type(exc).__name__, xbmc.LOGWARNING)


def reset_warmer():
    """Test hook. Drops the queued URL set."""
    global _running
    with _lock:
        _seen.clear()
        del _queue[:]
        _target['window'] = None
        _running = False
