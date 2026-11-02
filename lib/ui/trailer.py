# SPDX-FileCopyrightText: 2026 M0Rf30
# SPDX-License-Identifier: MIT
"""Play a title's trailer.

Stremio metas carry trailers as YouTube ids: Cinemeta's
``trailerStreams`` (``[{"title", "ytId"}]``) and the older ``trailers``
(``[{"source": <id>, "type": "Trailer"}]``). The streaming server turns
a ``ytId`` into a playable stream at ``/yt/<id>`` (`ServerClient.
resolve_stream`), which is how Stremio itself plays them: no YouTube API
key. It is used when a probe shows it answers with video; Kodi's YouTube
add-on is next, if it is installed.

A trailer is not a title being watched: it records no progress, makes
no Continue entry and starts no Up next. The screens close for it the
way they do for any playback (a dialog left on top would take the
remote from the player) and reopen where they were when it stops.
"""

import re

#: A YouTube video id. Anything else in a trailer field is ignored.
_YT_ID = re.compile(r'^[A-Za-z0-9_-]{6,20}$')
_YOUTUBE_ADDON = 'plugin.video.youtube'
#: Seconds to wait for the streaming server's first bytes of a trailer.
PROBE_SECONDS = 6.0
#: A route whose clock has not moved by then is stopped and the next one
#: tried. The YouTube add-on through inputstream.adaptive can take a while.
START_TIMEOUT_SECONDS = 20.0


def trailer_id(meta):
    """The first trailer's YouTube id in `meta`, or ``''``. Kodi-free."""
    if not isinstance(meta, dict):
        return ''
    for entry in meta.get('trailerStreams') or []:
        if isinstance(entry, dict):
            yt = str(entry.get('ytId') or '').strip()
            if _YT_ID.match(yt):
                return yt
    candidates = []
    for entry in meta.get('trailers') or []:
        if not isinstance(entry, dict):
            continue
        source = str(entry.get('source') or '').strip()
        if not _YT_ID.match(source):
            continue
        kind = str(entry.get('type') or '').lower()
        candidates.append((0 if kind == 'trailer' else 1, source))
    if candidates:
        candidates.sort(key=lambda pair: pair[0])
        return candidates[0][1]
    return ''


def _server_url(yt):
    """``<server>/yt/<id>`` when the streaming server answers it with
    video, else ``''``. Probed first: not every server build has the
    ``/yt`` route, and handing Kodi a URL that fails closed the screens
    and showed "One or more items failed to play"."""
    from lib.ui.player import _server_client

    try:
        server = _server_client()
        if not server.is_available():
            return ''
        url = server.resolve_stream({'ytId': yt}) or ''
    except Exception:
        return ''
    return url if url and _plays(url) else ''


def _plays(url):
    """True when `url` starts answering with something that is not a page."""
    try:
        import requests
    except Exception:
        return False
    try:
        response = requests.get(url, stream=True, timeout=PROBE_SECONDS, headers={'Range': 'bytes=0-1023'})
    except Exception:
        return False
    try:
        if response.status_code not in (200, 206):
            return False
        kind = str(response.headers.get('Content-Type') or '').lower()
        return not kind.startswith(('text/', 'application/json'))
    finally:
        try:
            response.close()
        except Exception:
            pass


def _youtube_addon_url(yt):
    import xbmc

    if not xbmc.getCondVisibility('System.HasAddon(%s)' % _YOUTUBE_ADDON):
        return ''
    return 'plugin://%s/play/?video_id=%s' % (_YOUTUBE_ADDON, yt)


def trailer_urls(yt, server_url=None):
    """Where to play `yt` from, best first: the streaming server's ``/yt``
    route when a probe shows it answers with video (a plain stream Kodi
    plays without help), then Kodi's YouTube add-on when it is installed.
    `server_url` is a probe already made (``''``: it failed)."""
    url = _server_url(yt) if server_url is None else server_url
    if url:
        yield url
    url = _youtube_addon_url(yt)
    if url:
        yield url


def _full_meta(meta, stype, sid):
    """`meta`, or the full meta when `meta` has no trailer field (a
    catalog preview drops them)."""
    if trailer_id(meta) or not stype or not sid:
        return meta
    from lib.ui.compat import L
    from lib.ui.uicommon import CANCELLED, run_cancellable
    from lib.ui.views import _fetch_meta

    fetched = run_cancellable(lambda: _fetch_meta(stype, sid), L(30033))
    if fetched is CANCELLED:
        return CANCELLED
    if not isinstance(fetched, dict):
        return meta
    return fetched


def play_trailer(meta, stype='', sid=''):
    """Play the trailer of `meta`, blocking until it stops. True when one played.

    The screens stay up, with a spinner, until the trailer is actually
    running: the player's clock has to move. Only then do they close and
    the video goes full screen. A route that does not get going within
    `START_TIMEOUT_SECONDS` (the YouTube add-on stuck on its loading
    circle, a black screen) is stopped and the next one is tried; when
    none plays the user is told and stays where they were. rev18 closed
    the screens first, so a stalled trailer left Kodi's Videos window
    with a black player behind it.
    """
    import xbmc
    import xbmcgui

    from lib.stremio.addons import safe_url_for_log
    from lib.ui.compat import L, log, notify
    from lib.ui.uicommon import CANCELLED, escape_label, run_cancellable

    full = _full_meta(meta if isinstance(meta, dict) else {}, stype, sid)
    if full is CANCELLED:
        return False
    yt = trailer_id(full)
    if not yt:
        notify(L(30497))
        return False
    # Addon-supplied: the label reaches the player OSD and the spinner.
    name = (full or {}).get('name')
    name = escape_label(name) if isinstance(name, str) else ''
    label = ('%s – %s' % (name, L(30499))) if name else L(30499)
    server = run_cancellable(lambda: _server_url(yt), label)
    if server is CANCELLED:
        return False
    tried = 0
    for url in trailer_urls(yt, server or ''):
        tried += 1
        item = xbmcgui.ListItem(label=label, path=url)
        log('trailer: trying %s via %s' % (yt, safe_url_for_log(url)), xbmc.LOGINFO)
        outcome = _start(url, item, label)
        if outcome == 'rolling':
            _watch()
            return True
        if outcome in ('cancelled', 'abort'):
            return False
        log('trailer: %s did not start via %s' % (yt, safe_url_for_log(url)), xbmc.LOGWARNING)
    if not tried:
        _offer_youtube_addon(L, notify)
    else:
        notify(L(30510))
    return False


def _start(url, item, heading):
    """Start `url` and wait, under a spinner, for its clock to move.

    ``'rolling'``, ``'stalled'`` (stopped again), ``'cancelled'`` (Back
    on the spinner; stopped) or ``'abort'`` (Kodi is quitting)."""
    import xbmc

    from lib.ui.uicommon import CANCELLED, busy_dialog, wait_cancellable

    player = xbmc.Player()
    monitor = xbmc.Monitor()
    try:
        player.play(url, item)
    except Exception:
        return 'stalled'
    with busy_dialog(heading) as dialog:
        result = wait_cancellable(lambda: _wait_rolling(player, monitor), dialog, monitor)
    if result == 'rolling':
        return 'rolling'
    _stop(player, monitor)
    if result is CANCELLED:
        return 'abort' if monitor.abortRequested() else 'cancelled'
    return result if result == 'abort' else 'stalled'


def _wait_rolling(player, monitor, timeout=None, tick=0.25):
    """``'rolling'`` once the player's clock moves, ``'stalled'`` after
    `timeout` seconds without that, ``'abort'`` on Kodi shutdown."""
    import time

    # Monotonic: a wall-clock change (NTP, a time zone) must not end or
    # stretch the wait.
    deadline = time.monotonic() + (START_TIMEOUT_SECONDS if timeout is None else timeout)
    first = None
    while time.monotonic() < deadline:
        if monitor.waitForAbort(tick):
            return 'abort'
        try:
            if not player.isPlayingVideo():
                continue
            now = player.getTime()
        except Exception:
            continue
        if first is None:
            first = now
        elif now > first + 0.2:
            return 'rolling'
    return 'stalled'


def _stop(player, monitor, wait=5.0):
    """Stop a trailer that never got going, and let the player close."""
    try:
        if player.isPlaying():
            player.stop()
    except Exception:
        return
    waited = 0.0
    while waited < wait:
        try:
            if not player.isPlaying():
                return
        except Exception:
            return
        if monitor.waitForAbort(0.25):
            return
        waited += 0.25


def _watch():
    """The trailer is running: close the screens, go full screen, and
    wait for it to stop. The screens reopen where they were."""
    import xbmc

    from lib.ui.uicommon import close_windows_for_playback, end_player_phase

    close_windows_for_playback()
    try:
        # Kodi refuses to switch to the video while a dialog is up, so the
        # switch it tried at start was turned down. Ask again now.
        if not xbmc.getCondVisibility('Window.IsActive(fullscreenvideo)'):
            xbmc.executebuiltin('ActivateWindow(fullscreenvideo)')
        from lib.ui.streamswindow import _wait_for_playback_end

        _wait_for_playback_end(start_timeout=START_TIMEOUT_SECONDS)
    finally:
        end_player_phase()


def _offer_youtube_addon(L, notify):
    """Nothing can play it: offer Kodi's YouTube add-on, which Kodi then
    installs from its own repository (it asks once more itself)."""
    import xbmc

    from lib.ui import dialogs

    if xbmc.getCondVisibility('System.HasAddon(%s)' % _YOUTUBE_ADDON):
        notify(L(30498))
        return
    try:
        yes = dialogs.confirm(L(30499), L(30504), xbmc.getLocalizedString(107), xbmc.getLocalizedString(106))
    except Exception:
        yes = False
    if yes:
        xbmc.executebuiltin('InstallAddon(%s)' % _YOUTUBE_ADDON)
