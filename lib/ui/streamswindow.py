"""StreamsWindow: the resolved-source picker for one title/episode -
Rivulet's custom replacement for the classical `streams()` directory.

Picking a row resolves and plays it DIRECTLY via
`lib.ui.player.play_direct` (no ADDON_HANDLE/`setResolvedUrl` - see that
function's docstring), so Kodi's player takes over the full screen.

Once playback actually starts and later stops - or never starts at all
within a short timeout, see `_wait_for_playback_end()` - `open_streams()`
reopens a fresh `StreamsWindow` over the SAME already-fetched
pairs/heading/art/poster (no addon re-fetch) so the user lands back on
the picker instead of falling all the way through to Kodi's main menu;
"Back" out of THAT reopened window is what finally returns control to
whatever opened `open_streams()` in the first place. Consequently
`open_streams()` now only ever returns False: once for the user backing
out of a window (the first one, or a reopened one) with no pick, and
once more for a fetch/window failure - it never returns True. Every
`if open_streams(...): self.close()` (or `return open_streams(...)`)
branch up the call chain (`DetailWindow`, `CatalogPickerWindow`,
`SearchWindow` via `open_detail`) is consequently dormant: those
callers stay open underneath for the round trip's "reopen" to sit on
top of, and simply resume (natural Back navigation) once
`open_streams()` finally returns.

`open_streams()`/`StreamsWindow.start()` also take optional `heading`/
`art` context kwargs (`heading='<title>'`, `art={'poster': ...,
'fanart': ...}`) - the pre-agreed cross-agent contract `DetailWindow`
(an episode's "<Show> - SxxExx <Title>" + the show's own art) and
`ShowcaseWindow`'s movie path (the movie's own title/art) both call into.
Both default to "nothing supplied" (`''`/`None`) so a bare `poster=`
kwarg, or no context at all, keeps every pre-existing call site working
unchanged: an empty heading falls back to a generic localized "Streams"
title, and no `art` simply means the side poster panel stays empty.

`meta` is a similar optional caller-context kwarg - the same Stremio
meta dict `DetailWindow`/`ShowcaseWindow`/`open_detail()` already have in
hand - rendered read-only into the side info panel (year/runtime/
rating/genres, plus a single-provider dedupe note); omitted or `None`
leaves that panel empty exactly like today.

`open_streams()`/`StreamsWindow.start()` also take an optional
`video_id=None` kwarg - the id of the episode whose streams are being
shown, threaded from `lib.ui.detailwindow`'s episode click (the only
caller that has one; a movie or any other context-free call keeps
defaulting to `None`, so nothing below ever runs for it). Once playback
of that episode ends NATURALLY (played through to completion, not
stopped by the user - see `_wait_for_playback_end()`), if it's a
series, the 'binge_enable' setting is on, `meta['videos']` has a next
episode (`lib.ui.binge.next_video()`), and that episode's own streams
can be fetched, `_try_binge_watch()` shows a cancellable countdown
offering to auto-play it - see that
function's docstring for exactly how it reuses `play_direct()`'s
`item_meta=`/`on_ready=close_windows_for_playback` contract and loops
for the episode after that. The user stopping the episode instead of
letting it end, cancelling the countdown, a monitor abort, no next
episode, or no fetchable stream for it all fall back to the
SAME "reopen the picker" round trip described above - `open_streams()`
still only ever returns False.
"""
import xbmcgui

from lib.stremio import streaminfo
from lib.ui.binge import next_video, pick_binge_stream
from lib.ui.dependencies import get_client, get_store
from lib.ui.uicommon import BaseWindow, busy_dialog, close_windows_for_playback, open_window

BACKGROUND = 30000
LIST = 30002
POSTER = 30004
HEADING = 30005
INFO_PANEL = 30008

#: Brief settle pause before reopening the picker after playback ends -
#: gives Kodi's player teardown a moment to finish before a fresh modal
#: window is drawn on top of it. Also reused as the settle pause after
#: each binge-watching auto-played episode, below.
_REOPEN_SETTLE_SECONDS = 0.5

#: resources/settings.xml keys for the two binge-watching controls.
_BINGE_ENABLE_SETTING = 'binge_enable'
_BINGE_COUNTDOWN_SETTING = 'binge_countdown'

#: Fallback/floor for the 'binge_countdown' setting - mirrors its own
#: <default>/<constraints><minimum> in resources/settings.xml.
_BINGE_COUNTDOWN_DEFAULT_SECONDS = 10
_BINGE_COUNTDOWN_MIN_SECONDS = 3

#: One countdown tick per second: fine enough for a "N s" label, and the
#: same `monitor.waitForAbort(tick)` idiom `_wait_for_playback_end()`
#: already uses for its own poll loop below.
_BINGE_COUNTDOWN_TICK_SECONDS = 1.0

#: strings.po ids for the countdown DialogProgress (heading, then the
#: '%s'/'%d'-parameterized "Playing <label> in <N> s" message).
_BINGE_DIALOG_HEADING_STRING_ID = 30182
_BINGE_DIALOG_MESSAGE_STRING_ID = 30183


class StreamsWindow(BaseWindow):
    """See module docstring. Built/run via `open_streams()`."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.pairs = []
        self.stype = 'movie'
        self.sid = None
        self.poster = None
        self.heading = ''
        self.art = None
        self.meta = None
        self.video_id = None
        self.played = False
        self.played_pair = None

    def start(self, pairs, stype, sid, poster=None, heading='', art=None, meta=None, video_id=None):
        """doModal() showing `pairs` (a list of `(info, stream)` as
        `lib.stremio.streaminfo.parse_stream`/`sort_streams` produce).
        `heading`/`art`/`meta` are the optional caller-context kwargs
        described in the module docstring; `video_id` is the episode
        `pairs` belongs to (`None` for a movie or a context-free call -
        see the module docstring's binge-watching paragraph). Returns
        True if playback started (the caller should also close)."""
        self.pairs = list(pairs or [])
        self.stype = stype
        self.sid = sid
        self.poster = poster
        self.heading = heading or ''
        self.art = art
        self.meta = meta
        self.video_id = video_id
        self.played = False
        self.played_pair = None
        if not self.pairs:
            return False
        self.doModal()
        return self.played

    def onInit(self):
        from lib.ui.compat import L, addon_fanart

        art = self.art or {}
        background = art.get('fanart') or art.get('poster') or self.poster or addon_fanart()
        self.getControl(BACKGROUND).setImage(background)
        self.getControl(POSTER).setImage(art.get('poster') or self.poster or '')
        self.getControl(HEADING).setLabel((self.heading or L(30041)).upper())

        providers = {info.get('addon') for info, _stream in self.pairs if info.get('addon')}
        single_provider = next(iter(providers)) if len(providers) == 1 else None

        items = []
        for index, (info, _stream) in enumerate(self.pairs):
            # Multi-provider rows show the addon as a gray tail segment on
            # line 1 (format_label's own include_addon=True rendering);
            # once every pair is the SAME addon it's redundant there and
            # surfaces once instead as the info panel's 'via <addon>' line
            # below, so line 1 drops it (include_addon=False).
            line1 = streaminfo.format_label(info, include_addon=not single_provider) or info.get('raw') or '?'
            line1 = line1.replace('\r', ' ').replace('\n', ' ')
            # Line 2 is the re-derived detail line (audio/channels,
            # languages, bitrate, release tags, group, tracker) - see
            # streaminfo.format_details() - never the provider name.
            line2 = streaminfo.format_details(info).replace('\r', ' ').replace('\n', ' ')
            item = xbmcgui.ListItem(line1, label2=line2)
            item.setProperty('position', str(index))
            items.append(item)
        control = self.getControl(LIST)
        # reset() before addItems(): onInit() runs again when
        # uicommon.ModalStackWindow reopens a screen force-closed for
        # playback, and re-adding onto a retained list would double every
        # item.
        control.reset()
        control.addItems(items)
        self.setFocusId(LIST)

        meta = self.meta or {}
        year = str(meta.get('releaseInfo') or meta.get('year') or '').rstrip('-')
        runtime = meta.get('runtime') or ''
        top_line = ' \u00b7 '.join(part for part in (year, runtime) if part)
        lines = [top_line] if top_line else []
        rating = meta.get('imdbRating')
        if rating:
            lines.append('\u2605 %s' % rating)
        genres = (meta.get('genres') or [])[:3]
        if genres:
            lines.append(' / '.join(genres))
        if single_provider:
            lines.append('via %s' % single_provider)
        self.getControl(INFO_PANEL).setText('\n'.join(lines))

    def onClick(self, control_id):
        if control_id != LIST:
            return
        focused = self.getControl(LIST).getSelectedItem()
        if focused is None:
            return
        info, stream = self.pairs[int(focused.getProperty('position'))]

        # item_meta carries the content title/art/meta this window
        # already has in hand (see the module docstring's heading/art/
        # meta kwargs) into the player's OSD - only the keys actually
        # known are included, so a bare "no context" open still behaves
        # exactly like item_meta=None to play_direct(). on_ready fires
        # right before Kodi actually starts playing (play_direct()'s own
        # docstring), and _close_for_player_handoff() below is what
        # tears down every OTHER live screen AND this picker itself at
        # that exact instant, so no Rivulet modal - not even this one -
        # is still live when xbmc.Player().play() runs. This window is
        # closed WITHOUT _closed_for_playback, unlike its ancestors, so
        # ModalStackWindow.doModal() does not immediately reopen it;
        # open_streams()'s own reopen loop is what brings the picker
        # back once playback actually ends.
        item_meta = {}
        label = self.heading or (self.meta or {}).get('name') or ''
        if label:
            item_meta['label'] = label
        art = self.art or ({'poster': self.poster} if self.poster else None)
        if art:
            item_meta['art'] = art
        if self.meta:
            item_meta['meta'] = self.meta

        from lib.ui.player import play_direct
        if play_direct(
            stream, self.stype, self.sid, item_meta=item_meta,
            on_ready=lambda: _close_for_player_handoff(self),
            video_id=self.video_id,
        ):
            self.played = True
            self.played_pair = (info, stream)
            self.close()  # no-op: on_ready above already closed this window


def _close_for_player_handoff(picker):
    """`play_direct()`'s `on_ready` hook for `StreamsWindow.onClick()`:
    fires immediately before `xbmc.Player().play()` (see that
    function's docstring), so this is the ONE moment every live Rivulet
    modal - including `picker` itself - must already be gone, or Kodi
    keeps routing play/pause and the OSD to whichever WindowXMLDialog
    is still topmost rather than to the player (see uicommon's module
    docstring).

    `close_windows_for_playback(exclude=picker)` tears down every OTHER
    live screen, marking each ancestor `_closed_for_playback` so
    `ModalStackWindow.doModal()` restores it once `open_streams()`'s own
    post-playback reopen loop brings a fresh picker back. `picker`
    itself is then closed the same plain way `onAction()`'s Back
    handling does - deliberately WITHOUT `_closed_for_playback`, since
    that flag would also trip `ModalStackWindow.doModal()`'s own reopen
    loop and pop a second, premature picker up immediately behind the
    player; bringing the picker back afterwards is `open_streams()`'s
    reopen loop's job alone.
    """
    close_windows_for_playback(exclude=picker)
    picker.close()


def _wait_for_playback_end(player=None, monitor=None, start_timeout=20.0, tick=0.5):
    """Block until playback `open_streams()` just started has both begun
    and ended, so it can safely reopen the streams picker underneath
    Kodi's player instead of unwinding the whole custom-window stack.

    `play_direct()`/`xbmc.Player().play()` is fire-and-forget - there is
    a short real-world gap before `xbmc.Player().isPlaying()` actually
    reports True - so this first polls up to `start_timeout` seconds (in
    `tick`-second steps) waiting for playback to begin. If it never does
    (resolution failed past the point `play_direct()` still returned
    True, or Kodi itself couldn't play the url), there is nothing left
    to wait out: the user already saw `play_direct()`'s own failure
    notification, so this returns `(True, False)` (safe to reopen,
    nothing played so nothing to binge into) once the budget runs out.
    Once playback DOES begin, it polls again until `isPlaying()` goes
    back to False (stopped/finished).

    Returns a `(proceed, ended_naturally)` tuple rather than a bare
    bool, since `isPlaying()` alone cannot tell a natural end apart from
    the user pressing stop. `proceed` keeps the meaning described above
    (and below): False only on a monitor abort or an unexpected
    exception, True otherwise. `ended_naturally` is True only when the
    polled player actually reported Kodi's `onPlayBackEnded()` (played
    through to completion) rather than `onPlayBackStopped()`/
    `onPlayBackError()` (user stop / playback failure); callers use it
    to gate auto-play-next (binge-watching) - it is always False
    whenever `proceed` is False or playback never started.

    Every poll tick is a `monitor.waitForAbort(tick)` call, exactly like
    every other cancellable wait loop in `lib.ui.player` - Kodi shutting
    down mid-wait must be seen within one tick, at either stage, and
    returns `(False, False)` immediately (the caller must NOT reopen
    into a shutting-down Kodi). Any unexpected exception anywhere in
    here (a broken Player/Monitor) degrades to that same `(False,
    False)` - this helper must never raise into
    `StreamsWindow.onClick()`'s caller.

    `player`/`monitor` are injectable (unit tests pass tiny fakes);
    an injected `player` is asked for its own `ended_naturally`
    attribute (`getattr(player, 'ended_naturally', False)`) once it
    stops. Production callers omit `player` and get a
    `_PlaybackEndWatcher` - a tiny `xbmc.Player` subclass, defined here
    rather than at module scope since this module only ever imports
    `xbmc` lazily inside the functions that need it - whose
    `onPlayBackEnded()`/`onPlayBackStopped()`/`onPlayBackError()`
    overrides record which one Kodi actually called.
    """
    import xbmc

    from lib.ui.compat import log

    class _PlaybackEndWatcher(xbmc.Player):
        """Distinguishes a natural end from a user stop/failure, which
        `isPlaying()` alone cannot - it reports False as soon as
        playback stops for ANY reason."""

        def __init__(self):
            super().__init__()
            self.ended_naturally = False

        def onPlayBackEnded(self):
            self.ended_naturally = True

        def onPlayBackStopped(self):
            self.ended_naturally = False

        def onPlayBackError(self):
            self.ended_naturally = False

    try:
        if player is None:
            player = _PlaybackEndWatcher()
        if monitor is None:
            monitor = xbmc.Monitor()

        attempts = int(start_timeout / tick)
        for _attempt in range(attempts):
            if player.isPlaying():
                break
            if monitor.waitForAbort(tick):
                return False, False
        else:
            # Never started within the budget - play_direct()'s own
            # failure notification already told the user; just reopen.
            # Nothing played, so nothing to binge into.
            return True, False

        while player.isPlaying():
            if monitor.waitForAbort(tick):
                return False, False
        return True, getattr(player, 'ended_naturally', False)
    except Exception as exc:  # noqa: BLE001 - a wait hiccup must never crash onClick()
        log('streamswindow: wait-for-playback-end failed: %r (treating as stop)' % (exc,), xbmc.LOGWARNING)
        return False, False


def _fetch_stream_pairs(stype, sid):
    """Fetch+parse (not sort) every installed addon's streams for
    (stype, sid) - the exact aggregate pipeline `open_streams()` has
    always run for its own initial fetch, factored out so
    `_try_binge_watch()` can silently re-run it for a next episode's OWN
    id without duplicating the busy_dialog/per-addon-failure bookkeeping.
    Sorting and the "no results" notify()/return-False handling stay the
    CALLER's job - `open_streams()`'s own caller wants a user-visible
    notify(); the binge round trip wants a silent fall-back to reopening
    the picker instead (see `_try_binge_watch()`)."""
    import xbmc

    from lib.stremio.addons import AddonError, addon_supports, safe_url_for_log
    from lib.ui.compat import L, log

    store = get_store()
    client = get_client()
    pairs = []
    addons = []
    for descriptor in store.get_addons():
        manifest = descriptor.get('manifest') or {}
        if addon_supports(manifest, 'stream', stype, sid):
            addons.append((descriptor, manifest))
    total_addons = len(addons)
    failed_addons = 0
    with busy_dialog(L(30033)) as dialog:
        for index, (descriptor, manifest) in enumerate(addons):
            if dialog.iscanceled():
                break
            transport_url = descriptor.get('transportUrl')
            addon_name = manifest.get('name', '?')
            percent = int(index * 100 / total_addons) if total_addons else 0
            dialog.update(percent, 'Checking %s...' % addon_name)
            try:
                results = client.streams(transport_url, stype, sid)
            except AddonError as exc:
                # One addon failing (offline, misconfigured, slow) is
                # routine, not exceptional - logging each at ERROR with a
                # full exception repr drowned real problems in noise on
                # every single fetch. DEBUG + only the safe scheme/host
                # (never the raw transport_url, which may carry
                # path/query/credentials, nor the exception text, which
                # may embed the raw URL too) here; one aggregate WARNING
                # below covers "something's wrong" without spamming
                # per-addon detail into the normal log.
                log('streamswindow: %s failed: %s' % (
                    safe_url_for_log(transport_url), type(exc).__name__), xbmc.LOGDEBUG)
                failed_addons += 1
                continue
            for stream in results or []:
                pairs.append((streaminfo.parse_stream(stream, addon_name=addon_name), stream))

    if failed_addons:
        log('streamswindow: %d addon(s) failed' % failed_addons, xbmc.LOGWARNING)
    return pairs


def _binge_label(video):
    """'S02E01 \u00b7 Title' - the same shape
    `lib.ui.detailwindow._episode_label()` already renders for the
    episode list, reimplemented here (not imported) so this Kodi-facing
    module never reaches into another window module's private helpers -
    there is no shared, lower-layer module beneath either of them to
    hold one copy instead."""
    title = video.get('title') or video.get('name') or video.get('id') or ''
    return 'S%02dE%02d \u00b7 %s' % (video.get('season') or 0, video.get('episode') or 0, title)


def _binge_heading(show_name, video):
    """'<Show> \u2013 S02E01 \u00b7 Title' - the OSD label for an
    auto-played binge-watching episode, the same shape
    `lib.ui.detailwindow._episode_heading()` builds for a manually
    picked one."""
    code_and_title = _binge_label(video)
    return '%s \u2013 %s' % (show_name, code_and_title) if show_name else code_and_title


def _binge_item_meta(show_name, video, art, poster, meta):
    """`item_meta` for the next episode's auto-played `play_direct()`
    call - the exact same shape `StreamsWindow.onClick()` builds for a
    user-picked stream (see that method's own comment), just keyed off
    `video`'s own heading instead of the picker's static one, since
    every auto-played episode after the first needs its own OSD title."""
    item_meta = {}
    label = _binge_heading(show_name, video)
    if label:
        item_meta['label'] = label
    effective_art = art or ({'poster': poster} if poster else None)
    if effective_art:
        item_meta['art'] = effective_art
    if meta:
        item_meta['meta'] = meta
    return item_meta


def _binge_countdown(label, seconds):
    """Cancellable `xbmcgui.DialogProgress` counting `seconds` down to 0,
    one `_BINGE_COUNTDOWN_TICK_SECONDS` tick at a time, showing
    ``L(_BINGE_DIALOG_MESSAGE_STRING_ID) % (label, remaining)``. Mirrors
    `_wait_for_playback_end()`'s own tri-state contract: True once the
    countdown runs out uninterrupted (caller should auto-play), None if
    the user cancelled via the dialog's own Cancel control (fall back to
    reopening the picker - a deliberate "not now", not a Back action),
    or False on a `monitor.waitForAbort()` abort (Kodi shutting down -
    the caller must return False immediately, reopening nothing, same as
    every other abort check in this module)."""
    import xbmc

    from lib.ui.compat import L

    dialog = xbmcgui.DialogProgress()
    dialog.create(L(_BINGE_DIALOG_HEADING_STRING_ID))
    monitor = xbmc.Monitor()
    try:
        for remaining in range(seconds, 0, -1):
            if dialog.iscanceled():
                return None
            percent = int((seconds - remaining) * 100 / seconds)
            dialog.update(percent, L(_BINGE_DIALOG_MESSAGE_STRING_ID) % (label, remaining))
            if monitor.waitForAbort(_BINGE_COUNTDOWN_TICK_SECONDS):
                return False
        return True
    finally:
        dialog.close()


def _try_binge_watch(stype, meta, poster, art, video_id, played_info):
    """After an episode has played through to its NATURAL end (the caller
    only reaches here when `_wait_for_playback_end()` reported
    `ended_naturally`; a user stop never gets this far), try
    to keep going straight into the next one(s) - see the module
    docstring's binge-watching paragraph for the user-facing shape.

    Loops internally rather than telling its caller "keep going": each
    successfully auto-played episode immediately tries the ONE after it
    too, matching the "loop for the episode after that" requirement.
    Returns `None` the moment there is nothing left to binge into (not a
    series, no `video_id`/`meta`, the 'binge_enable' setting is off,
    `lib.ui.binge.next_video()` found no next episode, its streams could
    not be fetched, the countdown was cancelled, the auto-played episode
    was stopped rather than played through to its own natural end, or
    `play_direct()` itself failed) - the caller must fall back to its own
    normal "reopen the picker" step, exactly as if this function had
    never run. Returns
    `False` the moment a `monitor.waitForAbort()` fires anywhere in the
    chain (Kodi shutting down) - the caller must return False
    immediately instead, reopening nothing, same as every other abort
    check in this module."""
    if stype != 'series' or video_id is None or not meta:
        return None

    import xbmc

    from lib.ui.compat import ADDON, log, setting_bool, setting_int
    from lib.ui.player import play_direct

    if not setting_bool(_BINGE_ENABLE_SETTING, True):
        return None

    show_name = meta.get('name') or meta.get('id') or ''
    current_video_id = video_id
    binge_group = (played_info or {}).get('binge_group')
    countdown_seconds = setting_int(
        _BINGE_COUNTDOWN_SETTING, _BINGE_COUNTDOWN_DEFAULT_SECONDS, minimum=_BINGE_COUNTDOWN_MIN_SECONDS,
    )

    try:
        while True:
            candidate = next_video(meta, current_video_id)
            if candidate is None:
                return None

            next_pairs = _fetch_stream_pairs(stype, candidate.get('id'))
            if not next_pairs:
                return None
            sort_key = ADDON.getSetting('stream_sort') or 'quality'
            next_pairs = streaminfo.sort_streams(next_pairs, key=sort_key)
            picked_info, picked_stream = pick_binge_stream(next_pairs, binge_group)

            countdown = _binge_countdown(_binge_label(candidate), countdown_seconds)
            if countdown is None:
                return None
            if countdown is False:
                return False

            item_meta = _binge_item_meta(show_name, candidate, art, poster, meta)
            if not play_direct(
                picked_stream, stype, candidate.get('id'), item_meta=item_meta,
                on_ready=close_windows_for_playback,
                video_id=candidate.get('id'),
            ):
                return None
            log('streamswindow: binge-watching auto-played %r' % candidate.get('id'), xbmc.LOGINFO)

            proceed, ended_naturally = _wait_for_playback_end()
            if not proceed:
                return False
            if not ended_naturally:
                # The user stopped this auto-played episode instead of
                # letting it end - stop the chain here, exactly like any
                # other "nothing left to binge into" case above.
                return None
            if xbmc.Monitor().waitForAbort(_REOPEN_SETTLE_SECONDS):
                return False

            current_video_id = candidate.get('id')
            binge_group = picked_info.get('binge_group')
    except Exception as exc:  # noqa: BLE001 - a binge-chain hiccup must fall back to the picker, never crash open_streams()
        log('streamswindow: binge-watching failed: %r (falling back to the picker)' % (exc,), xbmc.LOGWARNING)
        return None


def open_streams(stype, sid, poster=None, heading='', art=None, meta=None, video_id=None):
    """Fetch+sort every installed addon's streams for (stype, sid) and
    show them; a pick resolves+plays directly. `heading`/`art`/`meta` are
    forwarded to `StreamsWindow.start()` unchanged, and `video_id` (the
    id of the episode `pairs` is for, `None` for a movie or a
    context-free call) drives the binge-watching round trip below - see
    the module docstring for both.

    Once a pick plays, this reopens a fresh `StreamsWindow` over the
    SAME `pairs`/`heading`/`art`/`meta`/`poster` once playback ends (see
    `_wait_for_playback_end()`) rather than returning - see the module
    docstring for why this means the function now only ever returns
    False."""
    import xbmc

    from lib.ui.compat import ADDON, L, log, notify

    pairs = _fetch_stream_pairs(stype, sid)

    if not pairs:
        notify(L(30030))
        return False

    sort_key = ADDON.getSetting('stream_sort') or 'quality'
    pairs = streaminfo.sort_streams(pairs, key=sort_key)

    log('streamswindow: opening StreamsWindow (%d streams)' % len(pairs), xbmc.LOGINFO)
    while True:
        win = None
        try:
            win = open_window(StreamsWindow, 'StreamsWindow.xml')
            played = win.start(
                pairs, stype, sid, poster=poster, heading=heading, art=art, meta=meta, video_id=video_id,
            )
        except Exception as exc:  # a skin/UI failure must surface, not vanish
            log('streamswindow: window failed to open: %r' % (exc,), xbmc.LOGERROR)
            notify(L(30032))
            return False
        finally:
            # A normal return means StreamsWindow already closed itself (its
            # own onAction/onClick calls self.close()) before .start() returned
            # - but an exception raised from WITHIN .start() (onInit(), or a
            # callback mid-doModal()) skips that self-close entirely. Close
            # unconditionally here so no exit path leaves a zombie modal
            # window behind; closing an already-closed window is a safe no-op.
            if win is not None:
                try:
                    win.close()
                except Exception:
                    pass

        if not played:
            return False

        # Playback started: wait it out, then reopen the SAME picker
        # underneath the player that just closed instead of unwinding the
        # whole custom-window stack (see the module docstring). A monitor
        # abort (Kodi shutting down) at any point below returns False
        # immediately, reopening nothing.
        proceed, ended_naturally = _wait_for_playback_end()
        if not proceed:
            return False
        if xbmc.Monitor().waitForAbort(_REOPEN_SETTLE_SECONDS):  # brief settle pause before reopening
            return False

        # Binge-watching: try to auto-play straight through as many
        # consecutive "next episodes" as apply (see _try_binge_watch()'s
        # own docstring) before falling back to reopening THIS picker -
        # but only when the just-played episode ran through to its own
        # natural end; the user pressing stop must fall straight through
        # to reopening the picker, never auto-play the next episode.
        # None means the normal fall-back; False means a monitor abort
        # fired mid-chain, which must return False immediately instead,
        # reopening nothing, same as every other abort check above.
        if ended_naturally:
            played_info = win.played_pair[0] if win.played_pair else None
            if _try_binge_watch(stype, meta, poster, art, video_id, played_info) is False:
                return False

        log('streamswindow: reopening StreamsWindow after playback (%d streams)' % len(pairs), xbmc.LOGINFO)
