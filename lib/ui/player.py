"""Playback resolution: turn a Stremio Stream object into a Kodi-playable URL.

Kodi calls default.py -> router.run() -> here with the ADDON_HANDLE and the
base64url-decoded stream dict for action=play. This module owns the only
xbmc* calls involved in actually starting playback.
"""
import contextlib
import threading
import time
from urllib.parse import urlencode

import xbmc
import xbmcgui
import xbmcplugin

from lib.library import iso8601_utc
from lib.stremio.server import (
    UNKNOWN_FILE_IDX,
    ServerClient,
    UnsupportedStreamError,
    guess_file_idx,
    normalize_info_hash,
)
from lib.stremio.subtitles import collect_subtitles, filter_subtitles
from lib.ui.compat import (
    ADDON,
    L,
    log,
    notify,
    set_video_cast,
    set_video_info,
    setting_bool,
    setting_int,
)
from lib.ui.dependencies import get_client, get_store
from lib.ui.dialogs import RivuletProgress, confirm
from lib.ui.playbackmeta import (
    extract_file_name,
    filename_from_url,
    format_hms,
    human_size,
    mime_for,
    parse_duration_seconds,
    parse_rating,
    parse_year,
    resolve_art,
    sanitize_title,
)

#: Bounded (connect, read) timeouts for the pre-buffer network calls. The
#: SHORT read timeout is what makes the "Preparing stream" dialog
#: cancellable: on a stalled/dead-swarm read the socket unblocks within a
#: few seconds so the loop can recheck dialog.iscanceled(), instead of the
#: whole UI freezing for a 60s read (the original "can't cancel" bug -
#: kodi.log showed three back-to-back 60s freezes on a dead torrent). A
#: read that keeps receiving bytes resets its own clock, so this never
#: aborts a genuinely-progressing (even very slow) download.
_FRONT_TIMEOUT = (3.05, 5)
_METADATA_TIMEOUT = (3.05, 8)

#: Pause between retry attempts; also the abort-poll interval.
_ATTEMPT_PAUSE_SECONDS = 2.0

#: Retry-attempt budgets before giving up. Each attempt is bounded by the
#: short timeouts above (so cancel is always seen within a few seconds);
#: these caps are just the give-up backstop for a genuinely dead swarm.
_MAX_METADATA_ATTEMPTS = 60
_MAX_FRONT_ATTEMPTS = 60

#: Seconds to wait for a not-yet-reachable streaming server to come up
#: (e.g. one the background service is still launching) before giving up.
_SERVER_WAIT_ATTEMPTS = 5

#: Sleep between reachability probes while waiting for the streaming
#: server to come up (see `_wait_for_server`). Also doubles as the
#: abort-poll interval for that wait via `monitor.waitForAbort`.
_SERVER_POLL_INTERVAL_SECONDS = 1.0

#: Minimum bytes streamed from the file's FRONT (offset 0) before Kodi's
#: player can reliably probe the container header and start playback (see
#: ServerClient.iter_front's docstring in lib/stremio/server.py). Reaching
#: this floor means "safe to start", not "fully pre-buffered" - the
#: server's own readahead keeps filling ahead once playback begins, and
#: it is deliberately much smaller than the user's configured buffer_mb
#: target (a minimum of 5 MiB).
_HEADER_MIN_BYTES = 512 * 1024

#: Clearing `_HEADER_MIN_BYTES` means the container header is readable, not
#: that the swarm can KEEP UP. Starting there on a starved torrent is what
#: produced the live failure this gate exists for: a one-peer swarm at
#: ~174 KB/s feeding 720p HEVC started at 1.2 MB of a 20 MB target, starved
#: within seconds, and left Kodi's reader thread stuck in a curl reconnect.
#: So an early start now also requires the swarm to be fast enough to top
#: the REST of the buffer up almost immediately - if it can do that, it is
#: comfortably faster than playback drains it, which is the property that
#: actually matters. A slow swarm keeps filling toward the real target
#: instead, with the dialog showing live speed/peers so the user can cancel
#: and pick a healthier source.
_EARLY_START_ETA_SECONDS = 10

#: Upper bound on that extra filling, so a slow-but-alive swarm still starts
#: rather than sitting in the dialog forever. Once this much time has gone
#: into the buffering loop, whatever cleared the header floor is played.
_TARGET_WAIT_SECONDS = 45

#: Upper bound on how long the keep-alive pin (`_KeepAlivePin`) is allowed
#: to keep a background streaming request open on the torrent's URL after
#: pre-buffer decides to start - it exists to hold stremio-server-go's
#: piece priority on this file while Kodi's player is still opening/
#: probing the URL, not to babysit playback forever, so it always gives
#: up by this deadline even if Kodi never reports playback started.
_KEEPALIVE_MAX_SECONDS = 60.0

#: How often the pin re-checks `xbmc.Player().isPlayingVideo()` (and, when
#: a front read yields nothing to advance on, backs off) while held open.
_KEEPALIVE_POLL_SECONDS = 1.0

#: Bounded read rate for the pin's background GET: it exists to keep the
#: file's pieces prioritized server-side, not to race the real player for
#: bandwidth once handoff is imminent - so every chunk is followed by a
#: short sleep (interruptible by stop/abort) capping throughput well
#: below any real playback bitrate. Same 16 KiB chunk_size as the
#: pre-buffer loop, for the same IncompleteRead-loss reason (see
#: `ServerClient.iter_front`'s docstring).
_KEEPALIVE_CHUNK_SIZE = 16384
_KEEPALIVE_SLEEP_SECONDS = 0.25

#: How far past the pin's current offset each of its front reads asks
#: for - just needs to comfortably outlast what `_KEEPALIVE_MAX_SECONDS`
#: at the bounded rate above can consume, so the connection is never
#: starved for want_bytes headroom before the timer/abort/playback-
#: started check ends it.
_KEEPALIVE_WINDOW_BYTES = 8 * 1024 * 1024

#: Feasibility warning: if the swarm's measured download speed during
#: buffering falls below this fraction of the file's own average bitrate
#: (file size / runtime), the source is unlikely to keep up with
#: playback once it starts - see `_feasibility_warning_needed()`.
_FEASIBILITY_SPEED_FACTOR = 0.7

#: RivuletProgress percent bands for the staged "Preparing stream" dialog
#: `_resolve_playable_item` owns (created once, threaded through every
#: helper below). Order matches the real stage order so the whole
#: progression reads as monotonic forward motion: connect -> resolve ->
#: metadata -> engine warm -> buffer. Buffering gets the lion's share
#: (40-100%) since it is the only stage with a real, user-meaningful
#: ratio (bytes obtained so far / target); the others are coarse "still
#: working" ticks with no true fraction to report.
_CONNECT_PERCENT_MAX = 10
_RESOLVE_PERCENT = 15
_METADATA_PERCENT_BASE = 20
_METADATA_PERCENT_SPAN = 15  # 20-35%
_ENGINE_WARM_PERCENT = 38
_BUFFER_PERCENT_BASE = 40
_BUFFER_PERCENT_SPAN = 60  # 40-100%

# Stream source kinds that require the local streaming server to produce a
# playable URL at all (see stremio-protocol-spec.md gotcha #3).
_SERVER_DEPENDENT_KEYS = (
    'infoHash', 'ytId', 'rarUrls', 'zipUrls', '7zipUrls',
    'tarUrls', 'tgzUrls', 'nzbUrl', 'nzbUrls',
)


def _server_client():
    base_url = ADDON.getSetting('server_url') or 'http://127.0.0.1:11470'
    return ServerClient(base_url)


#: Resume-prompt progress band (percent of duration already watched):
#: below RESUME_MIN_PERCENT is "barely started, not worth asking"; at/
#: above RESUME_MAX_PERCENT is "basically finished, nothing meaningful
#: left to resume".
RESUME_MIN_PERCENT = 1.0
RESUME_MAX_PERCENT = 95.0


def _maybe_resume_offset_ms(store, stype, sid, video_id):
    """Return the cached position (milliseconds) to resume from if the
    user has local progress for `(stype, sid, video_id)` between
    `RESUME_MIN_PERCENT` and `RESUME_MAX_PERCENT` of duration, the
    'resume_ask' setting is on, and they answer yes to the
    `dialogs.confirm()` prompt below - else `None` (nothing
    cached, out of band, declined, or the setting is off). Never raises:
    a broken local progress cache must never block playback.
    """
    if not setting_bool('resume_ask', True):
        return None
    try:
        progress = store.get_progress(stype, sid, video_id)
    except Exception as exc:  # noqa: BLE001 - a corrupt local cache must never block playback
        log('player: get_progress failed for %s/%s: %r' % (stype, sid, exc), xbmc.LOGWARNING)
        return None
    if not progress:
        return None
    position_ms = progress.get('position_ms') or 0
    duration_ms = progress.get('duration_ms') or 0
    if duration_ms <= 0 or position_ms <= 0:
        return None
    percent = (position_ms / duration_ms) * 100.0
    if percent < RESUME_MIN_PERCENT or percent > RESUME_MAX_PERCENT:
        return None
    if not confirm(L(30172), _lfmt(30173, format_hms(position_ms / 1000.0)), L(30174), L(30175)):
        return None
    return position_ms


def _record_now_playing_and_maybe_resume(stype, sid, video_id, item_meta):
    """Best-effort: persist the "now playing" context
    (`lib.store.Store.set_now_playing`, consumed by
    `lib.service_runner`'s background progress tracker) and, when local
    progress exists for `(stype, content_id, video_id)` in a resumable
    band, prompt the user and queue a one-shot resume-seek offset the
    service's `onAVStarted` performs - `ListItem.setProperty(
    'StartOffset')` is unreliable for the direct `xbmc.Player().play()`
    path this addon's custom windows use, hence an explicit post-start
    seek rather than a resume property (see `lib.service_runner`).

    `content_id` is `item_meta['meta']['id']` when present, else `sid` -
    for a series this is the show's own library id (`sid` passed in is
    the season/stream-picker id), so local resume/progress and the
    stored now-playing context stay rooted at the show, keyed together
    with the exact episode `video_id`. A movie has no series meta id, so
    `content_id` falls back to `sid` exactly as before.

    Never raises: a broken store write must never block playback that
    has already been resolved.
    """
    try:
        store = get_store()
        meta = (item_meta or {}).get('meta') or {}
        content_id = meta.get('id') or sid
        resume_offset_ms = _maybe_resume_offset_ms(store, stype, content_id, video_id)
        art = (item_meta or {}).get('art') or {}
        store.set_now_playing({
            'type': stype,
            'id': content_id,
            'video_id': video_id,
            'name': (item_meta or {}).get('label') or meta.get('name') or '',
            'poster': art.get('poster') or meta.get('poster'),
            'started_at': iso8601_utc(),
        })
        store.set_resume_offset_ms(resume_offset_ms)
    except Exception as exc:  # noqa: BLE001 - a broken store write must never block playback
        log('player: recording now-playing context failed for %s/%s: %r' % (stype, sid, exc), xbmc.LOGWARNING)


def _attach_subtitles(list_item, behavior_hints, stype, sid):
    """Best-effort addon-subtitle lookup: never raises, never blocks
    playback - a broken subtitle addon just means a missing subtitle track.

    Only tracks in the user's `subs_language` are attached. Kodi reads an
    external subtitle's language from its filename, and addon subtitle
    URLs end in opaque numeric ids, so every attached track arrives with
    an empty language and Kodi's auto-selection picks arbitrarily among
    them (issue #6). A single-language list makes that pick correct;
    when nothing matches, nothing is attached and the file's own embedded
    tracks - which do carry language metadata - are left to Kodi.
    """
    if not setting_bool('subs_enable', True):
        return
    try:
        extra = []
        if 'videoSize' in behavior_hints:
            extra.append(('videoSize', str(behavior_hints['videoSize'])))
        if 'filename' in behavior_hints:
            extra.append(('filename', behavior_hints['filename']))
        subs = collect_subtitles(
            get_client(), get_store().get_enabled_addons(), stype, sid, extra=extra or None
        )
        subs = filter_subtitles(subs, ADDON.getSetting('subs_language') or 'en')
        urls = [sub['url'] for sub in subs[:20]]
        if urls:
            list_item.setSubtitles(urls)
    except Exception as exc:  # noqa: BLE001 - subtitles are a bonus, never fatal
        log('player: subtitle fetch failed for %s/%s: %r' % (stype, sid, exc), xbmc.LOGWARNING)


def _lfmt(string_id, *args):
    """Format localized string `string_id` with `args`, degrading to the
    bare space-joined args when the translation is stale/empty or has
    mismatched placeholders (e.g. a hot-deployed strings.po Kodi hasn't
    reloaded yet: `'' % (1, 60)` raises TypeError). Dialog cosmetics
    must never abort stream preparation.
    """
    try:
        return L(string_id) % args
    except (TypeError, ValueError):
        return ' '.join(str(arg) for arg in args)


def _stats_line(stats):
    """Best-effort 'speed - N peers' line from a `/create` stats snapshot
    (the same shape `_await_file_idx` and the buffering loop below poll),
    or '' once there is nothing worth showing yet - a still-warming
    engine with no `peers` key, or a poll that failed and passed `None`
    through. Never raises.
    """
    peers = (stats or {}).get('peers')
    if peers is None:
        return ''
    speed = human_size((stats or {}).get('downloadSpeed') or 0)
    return _lfmt(30082, speed, peers)


def _poll_stats_best_effort(server, info_hash):
    """Live stats snapshot for the buffering dialog's second line - the
    SAME `/create` poll `_await_file_idx` uses for its own speed/peers
    line, reused here so a torrent already past metadata resolution still
    shows live numbers while its front is being primed. Throttled to one
    call per outer front-priming attempt (never per chunk): a fast swarm
    can yield many chunks in one attempt and this must not turn into a
    stats-server hammering loop. A failure here is purely cosmetic - the
    dialog just shows no stats line for that attempt - and must never
    break the front-priming loop itself.
    """
    try:
        return server.create_engine(info_hash, timeout=_METADATA_TIMEOUT)
    except Exception as exc:  # noqa: BLE001 - stats are a bonus, never fatal to buffering
        log('player: buffer stats poll failed for %s: %r' % (info_hash, exc), xbmc.LOGWARNING)
        return None


def _may_start_early(stats, got, target, waited, stalled=False, progressed=False):
    """Whether a front read that cleared the header floor but not `target`
    may start playback anyway.

    Withholds the early start ONLY on positive evidence that the swarm is
    too slow to sustain playback - a measured speed that could not deliver
    the REMAINING buffer inside `_EARLY_START_ETA_SECONDS`. A swarm that
    can top the buffer up that fast is comfortably outrunning playback,
    which is the property that matters and needs no guess at the file's
    bitrate.

    Absent stats mean "unknown", NOT "slow": the stats poll is best-effort
    and fails on exactly the struggling servers this runs against, so an
    unknown speed keeps the previous fast-start behaviour rather than
    inventing a 45s wait from missing evidence. Only a speed the server
    actually reported can hold playback back.

    `stalled` (default False, every pre-existing caller/test unaffected)
    is True when the front-read attempt that just ran made NO progress at
    all (a timeout/zero-byte attempt) - a stronger warning sign than a
    merely-slow `downloadSpeed` figure, since that figure may just be
    stale data from before the stall. In that specific case an early
    start additionally requires `progressed` - genuine evidence (the
    aggregate downloaded-bytes counter grew since the previous attempt)
    that the swarm is still making progress somewhere, even if not on
    this file's front. Absent that evidence, a stalled attempt must not
    talk itself into starting on stale pre-stall speed data; it keeps
    buffering instead, still bounded by `_TARGET_WAIT_SECONDS` below.

    Also yes once `_TARGET_WAIT_SECONDS` of filling has gone by, so a
    slow-but-alive swarm eventually plays instead of buffering forever.
    """
    if waited >= _TARGET_WAIT_SECONDS:
        return True
    if stalled and not progressed:
        return False
    if not isinstance(stats, dict) or 'downloadSpeed' not in stats:
        return True
    try:
        speed = float(stats.get('downloadSpeed') or 0)
    except (TypeError, ValueError):
        return True
    if speed <= 0:
        return False
    return (target - got) / speed <= _EARLY_START_ETA_SECONDS


def _coerce_float(value):
    """`float(value)`, or None for anything that isn't cleanly numeric -
    shared by the feasibility/progress checks below, which must never
    raise on a malformed or missing stats field."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _stats_file_length(stats, file_idx):
    """Best-effort file size in bytes for `file_idx` out of a `/create`
    stats dict's `files` array (`[{'name', 'path', 'length', 'offset'},
    ...]` - see `guess_file_idx()`'s docstring in lib.stremio.server for
    the full response shape, and `lib.ui.playbackmeta.extract_file_name`
    for the sibling filename lookup this mirrors). None when `stats`/
    `files`/the entry at `file_idx`/its `length` is missing, malformed,
    or not positive - feeds the feasibility warning below, which must
    degrade to "can't judge" rather than raise or invent a size.
    """
    try:
        files = stats.get('files')
    except AttributeError:
        return None
    if not isinstance(files, list) or file_idx is None or not (0 <= file_idx < len(files)):
        return None
    entry = files[file_idx]
    length = _coerce_float(entry.get('length') if isinstance(entry, dict) else None)
    return length if length and length > 0 else None


def _required_bytes_per_second(runtime_seconds, file_length_bytes):
    """Bytes/s the swarm must sustain to deliver `file_length_bytes` over
    `runtime_seconds` of playback - the file's own average bitrate. None
    when either input is missing/non-positive: feasibility cannot be
    judged without both a runtime and a file size.
    """
    runtime_seconds = _coerce_float(runtime_seconds)
    file_length_bytes = _coerce_float(file_length_bytes)
    if not runtime_seconds or runtime_seconds <= 0 or not file_length_bytes or file_length_bytes <= 0:
        return None
    return file_length_bytes / runtime_seconds


def _feasibility_warning_needed(runtime_seconds, file_length_bytes, download_speed):
    """Whether the measured `download_speed` (bytes/s) over the buffering
    window is too slow to sustain the file's own average bitrate (see
    `_required_bytes_per_second`) by more than `_FEASIBILITY_SPEED_FACTOR`.

    Absent/non-positive/malformed inputs never warn - this is a best-
    effort heads-up, not a hard gate, and must never invent a warning
    from missing evidence (mirrors `_may_start_early`'s "absent means
    unknown, not slow" philosophy for this same swarm-speed class of
    check).
    """
    required = _required_bytes_per_second(runtime_seconds, file_length_bytes)
    if required is None:
        return False
    speed = _coerce_float(download_speed)
    if not speed or speed <= 0:
        return False
    return speed < required * _FEASIBILITY_SPEED_FACTOR


class _KeepAlivePin:
    """Background thread that keeps a streaming GET open on the torrent's
    URL after pre-buffer decides to hand off to Kodi's player, so
    stremio-server-go keeps this file's pieces prioritized while the
    player itself is still spinning up (opening the URL, probing the
    container, filling its own read-ahead) - the exact window a cold
    swarm can lose piece priority in and stall Kodi's very first read.

    Reads and discards at a bounded rate (`_KEEPALIVE_SLEEP_SECONDS`
    between `_KEEPALIVE_CHUNK_SIZE` chunks) starting from `start_byte`
    (the offset pre-buffer already reached) rather than racing the real
    player for bandwidth. Stops itself as soon as
    `is_playing()` reports True, `is_aborted()` reports True (Kodi is
    shutting down: Kodi waits on a plugin's live threads, so an unchecked
    pin would hold shutdown for up to `_KEEPALIVE_MAX_SECONDS`), after
    `_KEEPALIVE_MAX_SECONDS`, or once `stop()` is called - whichever comes
    first. `start()` never blocks the caller: it spawns a daemon thread and
    returns immediately.
    """

    def __init__(self, server, info_hash, file_idx, start_byte, is_playing, is_aborted=None):
        self._server = server
        self._info_hash = info_hash
        self._file_idx = file_idx
        self._start_byte = max(0, start_byte or 0)
        self._is_playing = is_playing
        self._is_aborted = is_aborted
        self._stop_event = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, name='RivuletKeepAlivePin', daemon=True)
        self._thread.start()
        return self

    def stop(self):
        """Signal the background thread to stop at its next check point -
        best-effort cleanup on abort/playback-ended; never blocks/joins."""
        self._stop_event.set()

    def _playing_now(self):
        try:
            return bool(self._is_playing())
        except Exception:  # noqa: BLE001 - a broken probe must never wedge the pin open
            return False

    def _should_stop(self):
        """True once `stop()` was called or Kodi requested abort; a broken
        abort probe counts as "not aborted" (the deadline still bounds it)."""
        if self._stop_event.is_set():
            return True
        if self._is_aborted is None:
            return False
        try:
            return bool(self._is_aborted())
        except Exception:  # noqa: BLE001 - a broken probe must never crash the pin
            return False

    def _run(self):
        deadline = time.monotonic() + _KEEPALIVE_MAX_SECONDS
        offset = self._start_byte
        try:
            while time.monotonic() < deadline and not self._should_stop():
                if self._playing_now():
                    return
                advanced = False
                try:
                    want_bytes = offset + _KEEPALIVE_WINDOW_BYTES
                    for chunk_len in self._server.iter_front(
                        self._info_hash, self._file_idx, want_bytes,
                        chunk_size=_KEEPALIVE_CHUNK_SIZE, timeout=_FRONT_TIMEOUT, start_byte=offset,
                    ):
                        offset += chunk_len
                        advanced = True
                        if self._should_stop() or time.monotonic() >= deadline or self._playing_now():
                            return
                        if self._stop_event.wait(_KEEPALIVE_SLEEP_SECONDS):
                            return
                except Exception as exc:  # noqa: BLE001 - a pin read hiccup must never crash playback
                    log('player: keep-alive pin read failed for %s: %r' % (self._info_hash, exc), xbmc.LOGDEBUG)
                if not advanced and self._stop_event.wait(_KEEPALIVE_POLL_SECONDS):
                    return
        except Exception as exc:  # noqa: BLE001 - the pin is a bonus, never fatal to playback
            log('player: keep-alive pin failed for %s: %r' % (self._info_hash, exc), xbmc.LOGWARNING)


def _start_keepalive_pin(server, info_hash, file_idx, start_byte):
    """Spawn a `_KeepAlivePin` for the torrent pre-buffer just decided to
    start on, and return immediately - see `_KeepAlivePin`'s docstring.
    A module-level seam (like `RivuletProgress`/`confirm` used elsewhere
    in this module) so tests can monkeypatch this to a no-op instead of
    spawning a real background thread.
    """
    try:
        monitor = xbmc.Monitor()
        _KeepAlivePin(
            server, info_hash, file_idx, start_byte,
            lambda: xbmc.Player().isPlayingVideo(),
            is_aborted=monitor.abortRequested,
        ).start()
    except Exception as exc:  # noqa: BLE001 - the pin is a bonus, never fatal to playback
        log('player: keep-alive pin failed to start for %s: %r' % (info_hash, exc), xbmc.LOGWARNING)


def _await_file_idx(server, stream, info_hash, url, dialog, monitor):
    """Poll `GET /create` until stremio-server-go resolves a file index for
    streams with no fileIdx of their own, sharing the caller's `dialog`/
    `monitor` (owned by `_resolve_playable_item` for the whole resolve -
    see that function) so the flow stays cancellable and ticks the
    "Fetching torrent metadata…" stage (20-35%, with a live attempt
    counter and, once available, a speed/peers line).

    Live-verified gap this closes: against stremio-server-go v0.8.5,
    `/create` returns BEFORE metadata resolves and its response never
    gains `guessedFileIdx` later - only a `files` array once metadata
    lands (see `guess_file_idx()`). Older/other server builds that DO
    emit `guessedFileIdx` up front resolve on the very first iteration.

    Each poll uses a SHORT client timeout (`_METADATA_TIMEOUT`) so a
    still-warming `/create` cannot freeze the loop between cancel checks -
    a timed-out poll just re-hits the same warming engine next iteration.

    Returns `(file_idx, url, proceed, stats)`. `proceed` is False only on
    cancellation (caller must resolve False; `stats` is then irrelevant
    and always None). When the budget runs out with no usable metadata,
    `file_idx` is UNKNOWN_FILE_IDX, `proceed` is True and `stats` is
    None - the caller then falls back to "proceed without polling".
    Otherwise `stats` is the exact `/create` response `file_idx` was
    guessed from, threaded back out so the caller can recover a real
    filename (`extract_file_name`) without an extra `/create` round-trip.
    """
    for attempt in range(_MAX_METADATA_ATTEMPTS):
        if dialog.iscanceled():
            return UNKNOWN_FILE_IDX, url, False, None

        try:
            stats = server.create_engine(info_hash, timeout=_METADATA_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 - a slow/failed poll just means "try again"
            log('player: metadata poll failed for %s: %r' % (info_hash, exc), xbmc.LOGWARNING)
            stats = None

        idx = guess_file_idx(stats)
        if idx is not None:
            trackers = stream.get('announce') or stream.get('sources') or []
            rebuilt = server.torrent_url(stream['infoHash'], idx, trackers)
            return idx, rebuilt, True, stats

        percent = min(_METADATA_PERCENT_BASE + _METADATA_PERCENT_SPAN, _METADATA_PERCENT_BASE + attempt)
        dialog.update(
            percent, L(30088),
            attempt=_lfmt(30090, attempt + 1, _MAX_METADATA_ATTEMPTS),
            stats=_stats_line(stats),
        )

        if monitor.waitForAbort(1.0):
            return UNKNOWN_FILE_IDX, url, False, None

    return UNKNOWN_FILE_IDX, url, True, None


def _prebuffer_torrent(server, stream, url, dialog, monitor, item_meta=None):
    """Warm the torrent engine and show cancellable, truthful progress
    before playback, ticking the shared `dialog` (owned/closed by
    `_resolve_playable_item` for the whole resolve, not here - see that
    function) through its engine-warm/metadata (20-38%) and buffering
    (40-100%) stages.

    Only called for torrent streams (`infoHash` present) once the server
    is already known available and the stream itself has been resolved.
    Returns `(proceed, url, filename)`: `proceed` is False when the user
    cancelled OR no usable front data could be obtained (caller must
    resolve False; `url`/`filename` are then not meaningful); `url` is
    the original url, or the rebuilt one when the server had to guess
    the file index; `filename` is the resolved torrent file's own name
    (from a `/create` stats dict this function already fetched for
    another reason - see `extract_file_name`) when one could be
    recovered, else None. ANY unexpected error degrades to `(True, url,
    None)` - a broken pre-buffer must never block playback.

    `item_meta` (optional, forwarded unchanged from `_resolve_playable_item`)
    supplies `meta.runtime` for the feasibility warning below when
    present; `None`/no `runtime` simply skips that check, exactly as
    before this parameter existed.

    Every successful return spawns a `_KeepAlivePin` (via
    `_start_keepalive_pin`, a monkeypatchable seam) on the exact bytes
    already obtained, so stremio-server-go keeps this file prioritized
    while Kodi's player is still opening/probing the URL this function
    hands back.
    """
    buffer_enable = setting_bool('buffer_enable', True)
    log(
        'player: pre-buffer entry: buffer_enable=%s fileIdx=%r' % (buffer_enable, stream.get('fileIdx')),
        xbmc.LOGINFO,
    )
    if not buffer_enable:
        return True, url, None

    # Normalize here too: the play-URL path already normalizes via
    # normalize_info_hash (server.py:148), but this pre-buffer polling
    # (create_engine/file_stats/etc.) used the raw stream value directly,
    # so a base32 or whitespace-padded infoHash (both explicitly accepted
    # by normalize_info_hash) polled the wrong URL and playback was
    # refused even though the actual play URL was fine.
    info_hash = normalize_info_hash(stream['infoHash']) or stream['infoHash']
    try:
        if dialog.iscanceled():
            return False, url, None

        file_idx = stream.get('fileIdx')
        if file_idx is None:
            file_idx = UNKNOWN_FILE_IDX
        filename = None
        file_length_bytes = None
        if file_idx == UNKNOWN_FILE_IDX:
            file_idx, url, proceed, stats = _await_file_idx(server, stream, info_hash, url, dialog, monitor)
            if not proceed:
                return False, url, None
            if file_idx == UNKNOWN_FILE_IDX:
                # Metadata never arrived within budget; nothing to stream
                # the front of, so just start playback.
                notify(L(30083))
                return True, url, None
            filename = extract_file_name(stats, file_idx)
            file_length_bytes = _stats_file_length(stats, file_idx)
        else:
            # Warm the engine, but bounded: a cold /create would otherwise
            # block for its full timeout with no cancel check. The front
            # reads below drive the engine anyway, so a failed/slow warm is
            # non-fatal. Its response also doubles as the source of a real
            # filename (see `extract_file_name`) - no extra request needed.
            dialog.update(_ENGINE_WARM_PERCENT, L(30089))
            try:
                warm_stats = server.create_engine(info_hash, timeout=_METADATA_TIMEOUT)
                filename = extract_file_name(warm_stats, file_idx)
                file_length_bytes = _stats_file_length(warm_stats, file_idx)
            except Exception as exc:  # noqa: BLE001 - front reads drive the engine regardless
                log('player: engine warm failed for %s: %r (continuing)' % (info_hash, exc), xbmc.LOGWARNING)

        # Best-effort runtime for the feasibility warning below - a missing/
        # unparseable meta.runtime just means that check never fires,
        # exactly like a missing file_length_bytes above.
        runtime_seconds = parse_duration_seconds(((item_meta or {}).get('meta') or {}).get('runtime'))
        feasibility_warned = False

        buffer_mb = setting_int('buffer_mb', 20, minimum=5)
        target = buffer_mb * 1024 * 1024
        # Formatted once: target never changes for the rest of this call, so
        # every per-chunk update below reuses this instead of paying
        # human_size() again per chunk (see the throttle comment in the
        # buffering loop below).
        target_size = human_size(target)
        log(
            'player: pre-buffer target: buffer_mb=%d target_bytes=%d' % (buffer_mb, target),
            xbmc.LOGINFO,
        )

        # Front-priming readiness loop. Streams the file FRONT directly
        # rather than trusting aggregate download stats, which can report
        # megabytes "buffered" from out-of-order pieces while the front is
        # still missing (the live CURLE_PARTIAL_FILE / "error probing
        # input format" bug). Short per-read timeout keeps the dialog
        # cancellable; a genuinely dead swarm fails honestly (30084) after
        # the budget rather than hanging or handing Kodi a doomed URL.
        # Wall clock for the whole buffering loop, used by the early-start
        # gate below to bound how long a slow swarm is allowed to keep
        # filling before playback starts on what it has.
        loop_started = time.monotonic()

        def waited():
            return time.monotonic() - loop_started

        #: Cumulative bytes obtained across every attempt so far. Each
        #: retry resumes from here via iter_front's `start_byte` (Range:
        #: bytes=<total_got>-<target-1>) instead of restarting the front
        #: read from offset 0, so a stall/retry never re-downloads (or
        #: re-waits on) data already received. Monotonically
        #: non-decreasing: a chunk received in any attempt is never lost.
        total_got = 0
        #: Aggregate downloaded-bytes counter from the previous attempt's
        #: stats poll, best-effort - used only by the stalled-attempt
        #: guard in `_may_start_early` to tell "swarm truly dead" apart
        #: from "still making progress elsewhere, just not on this file's
        #: front yet". None until a poll actually reports one.
        prev_downloaded = None

        for attempt in range(_MAX_FRONT_ATTEMPTS):
            if dialog.iscanceled():
                return False, url, None

            # Best-effort live speed/peers for this attempt's updates,
            # throttled to one poll per attempt (see
            # `_poll_stats_best_effort`'s docstring). Re-checked right
            # after so a slow poll never delays the next cancel check
            # past the front-read call that follows it.
            stats = _poll_stats_best_effort(server, info_hash)
            stats_line = _stats_line(stats)
            if dialog.iscanceled():
                return False, url, None

            if not feasibility_warned and file_length_bytes and runtime_seconds:
                measured_speed = (stats or {}).get('downloadSpeed')
                if _feasibility_warning_needed(runtime_seconds, file_length_bytes, measured_speed):
                    feasibility_warned = True
                    log(
                        'player: feasibility warning for %s: runtime=%.0fs size=%d speed=%r'
                        % (info_hash, runtime_seconds, file_length_bytes, measured_speed),
                        xbmc.LOGWARNING,
                    )
                    notify(L(30361))

            got_before_attempt = total_got
            # A 20 MB pre-buffer at 16 KB chunks is ~1300 iterations of this
            # loop. RivuletProgress already dedupes identical writes into
            # Kodi itself (measured: 324 calls instead of 20,480 for that
            # run), but building the message here - two human_size() calls
            # plus an _lfmt() lookup/format - still ran every time even when
            # the user would see no difference. Only rebuild/send when the
            # two values actually shown (the human-readable size and the
            # integer percent) have moved; iscanceled() is still polled on
            # EVERY chunk regardless, so cancel responsiveness never
            # regresses.
            last_size = None
            last_percent = None
            try:
                for chunk_len in server.iter_front(
                    info_hash, file_idx, target, timeout=_FRONT_TIMEOUT, start_byte=total_got,
                ):
                    total_got += chunk_len
                    percent = min(100, _BUFFER_PERCENT_BASE + total_got * _BUFFER_PERCENT_SPAN // target) if target else 100
                    size = human_size(total_got)
                    if size != last_size or percent != last_percent:
                        last_size, last_percent = size, percent
                        dialog.update(percent, _lfmt(30081, size, target_size), stats=stats_line)
                    if dialog.iscanceled():
                        return False, url, None
                    if total_got >= target:
                        break
            except Exception as exc:  # noqa: BLE001 - a front-read hiccup must not brick playback
                log('player: front read failed for %s: %r' % (info_hash, exc), xbmc.LOGWARNING)

            # This attempt's own stall boundary: no new bytes at all,
            # whether from a timeout, an exception, or a zero-chunk
            # response - see `_may_start_early`'s `stalled` parameter.
            stalled = total_got == got_before_attempt
            downloaded_now = _coerce_float((stats or {}).get('downloaded'))
            progressed = (
                downloaded_now is not None
                and prev_downloaded is not None
                and downloaded_now > prev_downloaded
            )
            if downloaded_now is not None:
                prev_downloaded = downloaded_now

            if total_got >= target:
                log(
                    'player: pre-buffer complete for %s: buffered=%d target=%d'
                    % (info_hash, total_got, target),
                    xbmc.LOGINFO,
                )
                _start_keepalive_pin(server, info_hash, file_idx, total_got)
                return True, url, filename

            if total_got >= _HEADER_MIN_BYTES and _may_start_early(
                stats, total_got, target, waited(), stalled=stalled, progressed=progressed
            ):
                log(
                    'player: pre-buffer header floor reached, starting early for %s: '
                    'buffered=%d target=%d waited=%.1fs' % (info_hash, total_got, target, waited()),
                    xbmc.LOGINFO,
                )
                _start_keepalive_pin(server, info_hash, file_idx, total_got)
                return True, url, filename

            # About to sleep _ATTEMPT_PAUSE_SECONDS before retrying - show a
            # retrying hint so that silent pause isn't a dead-looking dialog.
            percent = min(100, _BUFFER_PERCENT_BASE + total_got * _BUFFER_PERCENT_SPAN // target) if target else 100
            dialog.update(
                percent, _lfmt(30081, human_size(total_got), target_size),
                attempt=_lfmt(30090, attempt + 1, _MAX_FRONT_ATTEMPTS),
                stats=stats_line,
            )

            if monitor.waitForAbort(_ATTEMPT_PAUSE_SECONDS):
                return False, url, None

        if total_got >= _HEADER_MIN_BYTES:
            # The retry budget ran out while still under target, but the
            # header is readable. Failing here would be a regression: before
            # the early-start gate this case started playback immediately, so
            # play it rather than refusing a stream that is merely slow.
            log(
                'player: pre-buffer budget spent for %s, starting on %d of %d bytes'
                % (info_hash, total_got, target),
                xbmc.LOGINFO,
            )
            _start_keepalive_pin(server, info_hash, file_idx, total_got)
            return True, url, filename

        log(
            'player: pre-buffer timed out for %s after %d attempts with no usable front data'
            % (info_hash, _MAX_FRONT_ATTEMPTS),
            xbmc.LOGINFO,
        )
        notify(L(30084))
        return False, url, None
    except Exception as exc:  # noqa: BLE001 - pre-buffer is a bonus, never fatal
        log('player: pre-buffer failed for %s: %r' % (stream.get('infoHash'), exc), xbmc.LOGWARNING)
        return True, url, None


def _wait_for_server(server, dialog, monitor):
    """Return True as soon as the streaming server answers, waiting briefly
    for a not-yet-reachable instance (e.g. one the background service is
    still launching) to come up rather than failing instantly on the first
    probe. Cancellable via the shared `dialog`/`monitor`
    `_resolve_playable_item` owns for the whole resolve (created/closed
    once there, not here); ticks the "Connecting to streaming server…"
    stage (0-10%) while it waits.
    """
    if server.is_available():
        return True
    for attempt in range(_SERVER_WAIT_ATTEMPTS):
        if dialog.iscanceled():
            return False
        percent = min(_CONNECT_PERCENT_MAX, (attempt + 1) * _CONNECT_PERCENT_MAX // _SERVER_WAIT_ATTEMPTS)
        dialog.update(percent, L(30086))
        if monitor.waitForAbort(_SERVER_POLL_INTERVAL_SECONDS):
            return False
        if server.is_available():
            return True
    return False


def _stream_plot(stream):
    """Last-resort plot text for the OSD: the stream's own parsed
    description (release name, size, seeders, provider), or '' when the
    stream yields nothing worth showing.

    Parsing here rather than accepting a pre-parsed `info` from the
    caller keeps `item_meta` a pure content-metadata contract and means
    the classical `action=play` path (which never had an `info` dict)
    benefits too. `parse_stream`/`format_plot` are pure text helpers and
    cannot raise on odd input, but a malformed stream must never cost us
    playback, so this still degrades to ''.
    """
    try:
        from lib.stremio.streaminfo import format_plot, parse_stream
        return format_plot(parse_stream(stream or {}))
    except Exception as exc:  # noqa: BLE001 - a plot is cosmetic, playback is not
        log('player: stream plot fallback failed: %r' % (exc,), xbmc.LOGWARNING)
        return ''


def _apply_item_metadata(list_item, stream, stype, item_meta, filename):
    """Populate `list_item`'s label and video-info metadata (title, art,
    plot, year, rating, genre, duration, mediatype/tvshowtitle, cast) so
    Kodi's fullscreen OSD shows a real title and artwork instead of "Not
    available" plus the default camera placeholder - the live Defect A:
    a stream with no `behaviorHints.filename` (the common case for a
    torrent resolved to `http://host/<infoHash>/<fileIdx>`, which has no
    filename of its own) used to reach Kodi with an empty label/title
    and no art at all, even though the caller (`lib.ui.streamswindow`)
    already knew the content's real title/poster/fanart/meta - `item_meta`
    (see `_resolve_playable_item`'s docstring for its shape) is how that
    caller now forwards them. Every field is best-effort: a missing or
    malformed value is silently skipped, never raised - a metadata
    hiccup must never prevent playback.

    `cast` comes from `item_meta['meta']['cast']`, a Stremio meta's plain
    list of actor names only (Stremio supplies no roles) - see
    `compat.set_video_cast` for how it is applied across Kodi versions.

    `filename` is the release/torrent filename `_resolve_playable_item`
    already derived (`behaviorHints.filename`, or the resolved torrent
    file's own name - see `extract_file_name`): the title fallback when
    `item_meta` has no `label`, and preserved as `originaltitle` when a
    more specific `item_meta['label']` title is chosen instead, so the
    user can still see which exact release is playing.
    """
    item_meta = item_meta or {}
    meta = item_meta.get('meta') or {}

    title = sanitize_title(
        item_meta.get('label') or filename or stream.get('title') or stream.get('name') or ''
    )
    if title:
        list_item.setLabel(title)

    art = resolve_art(item_meta.get('art'), meta)
    if art:
        list_item.setArt(art)

    info = {'mediatype': 'episode' if stype == 'series' else 'movie'}
    if title:
        info['title'] = title

    originaltitle = sanitize_title(filename or '')
    if originaltitle and originaltitle != title:
        info['originaltitle'] = originaltitle

    # Kodi's fullscreen OSD info panel (Estuary's DialogSeekBar.xml renders
    # `$INFO[VideoPlayer.Tagline][CR]$INFO[VideoPlayer.Plot]` with
    # `fallback="10005"`, and Kodi string 10005 IS the literal "Not
    # available") shows that fallback whenever the playing item has no
    # plot - live-confirmed on a real device even after the title/art fix
    # above landed. Catalog previews frequently carry no `description` at
    # all, so rather than leave the panel reading "Not available", fall
    # back to the stream's own parsed description (release name, size,
    # seeders, provider) - genuinely the most useful thing to show about
    # the file actually playing.
    plot = item_meta.get('plot') or meta.get('description') or _stream_plot(stream)
    if plot:
        info['plot'] = plot

    if meta.get('tagline'):
        info['plotoutline'] = meta['tagline']

    year = parse_year(meta.get('releaseInfo') or meta.get('year'))
    if year is not None:
        info['year'] = year

    rating = parse_rating(meta.get('imdbRating'))
    if rating is not None:
        info['rating'] = rating

    genres = meta.get('genres')
    if genres:
        info['genre'] = genres

    duration = parse_duration_seconds(meta.get('runtime'))
    if duration is not None:
        info['duration'] = duration

    if stype == 'series' and meta.get('name'):
        info['tvshowtitle'] = meta['name']

    set_video_info(list_item, info)
    set_video_cast(list_item, meta.get('cast'))


def _resolve_playable_item(stream, stype, sid, item_meta=None, video_id=None):
    """Resolve `stream` (Stremio Stream object for content `stype`/`sid`)
    to a `(url, list_item)` pair ready to hand to Kodi's player, or
    `(None, None)` on failure - a notification has already been shown
    (either here or inside `_prebuffer_torrent`) by the time this
    returns `None`.

    Owns the single "Preparing stream" `RivuletProgress` for the WHOLE
    resolve - created once here, threaded through `_wait_for_server`, the
    `resolve_stream()` call, and `_prebuffer_torrent` (which used to each
    create/close their own dialog, so a torrent stream that also had to
    wait for the server could briefly show two in a row). Every stage
    below updates this same instance and only this function ever creates
    or closes it (see the `finally` below), so a cancel raised anywhere
    inside always surfaces here as `(None, None)`.

    Shared by `play()` (the classical GetDirectory path -
    `xbmcplugin.setResolvedUrl`) and `play_direct()` (the custom-window
    path - `xbmc.Player().play()`): neither `xbmcplugin` nor an
    `ADDON_HANDLE` is touched here, only stream resolution.

    `item_meta` is the caller's own already-known content metadata: an
    optional `{'label': str, 'art': dict, 'meta': dict}`, every key
    optional (see `_apply_item_metadata`'s docstring for exactly how
    each is used). It exists to fix a live bug: `stream` alone routinely
    carries nothing usable for Kodi's fullscreen OSD - a torrent with no
    `behaviorHints.filename` resolves to a bare
    `http://host/<infoHash>/<fileIdx>` URL - so without it the OSD
    showed the title as "Not available" and the artwork as the default
    camera placeholder, even though the caller (`lib.ui.streamswindow`)
    already knew the real title/poster/fanart/meta all along. `None`/
    `{}` (the default) behaves exactly as before this parameter existed.

    `video_id` (optional) is the specific episode id actually being
    played, when the caller has one - threaded into the "now playing"
    context (`lib.store.Store.set_now_playing`) and the local progress-
    cache lookup/write key (see `_record_now_playing_and_maybe_resume`).
    `None` (a movie, or a caller with no episode id of its own) behaves
    exactly as before this parameter existed.
    """
    stream = stream or {}
    behavior_hints = stream.get('behaviorHints') or {}
    title = behavior_hints.get('filename') or stream.get('title') or stream.get('name') or ''

    server = _server_client()
    dialog = RivuletProgress()
    dialog.create(L(30080), title)
    resolved_filename = None
    try:
        monitor = xbmc.Monitor()

        if any(key in stream for key in _SERVER_DEPENDENT_KEYS) and not _wait_for_server(server, dialog, monitor):
            notify(L(30031))
            return None, None

        dialog.update(_RESOLVE_PERCENT, L(30087))
        try:
            url = server.resolve_stream(stream)
        except UnsupportedStreamError as exc:
            # A known limitation (externalUrl/playerFrameUrl streams can
            # only be opened by the Stremio app itself) OR a rejected
            # direct-url scheme (lib.stremio.server._DIRECT_URL_SCHEMES -
            # e.g. a malicious addon smuggling a plugin:/script:/special:/
            # file: url) - either way not a fault. LOGINFO, and a message
            # telling the user WHY instead of the generic "no playable
            # stream" one below - raised, and this early return happens,
            # strictly before any metadata/ListItem construction below.
            log('player: unsupported stream for %s/%s: %r' % (stype, sid, exc), xbmc.LOGINFO)
            notify(L(30160))
            return None, None
        except Exception as exc:  # noqa: BLE001 - a broken server response must not crash Kodi
            log('player: resolve_stream failed for %s/%s: %r' % (stype, sid, exc), xbmc.LOGERROR)
            url = None

        if not url:
            notify(L(30030))
            return None, None

        if dialog.iscanceled():
            return None, None

        if stream.get('infoHash'):
            proceed, url, resolved_filename = _prebuffer_torrent(server, stream, url, dialog, monitor, item_meta=item_meta)
            if not proceed:
                return None, None
    finally:
        # A raising close() must never replace an exception already
        # unwinding through this try (e.g. a cancel/notify path above) -
        # best-effort cleanup only.
        with contextlib.suppress(Exception):
            dialog.close()

    request_headers = (behavior_hints.get('proxyHeaders') or {}).get('request') or {}
    if request_headers:
        # Kodi convention: "|urlencoded=headers" appended to the path makes
        # the player send these headers with every request for that URL.
        url = '%s|%s' % (url, urlencode(request_headers))

    filename = behavior_hints.get('filename') or resolved_filename

    list_item = xbmcgui.ListItem(path=url)
    # Disable Kodi's content-type HEAD probe: it races/aborts against a
    # torrent engine that is still (re)priming a range on open/seek, which
    # is the primary cause of seek-exits-playback. setMimeType (when the
    # extension is known) gives Kodi the same information up front so the
    # probe was never needed.
    list_item.setContentLookup(False)
    mime = mime_for(filename or filename_from_url(url))
    if mime:
        list_item.setMimeType(mime)

    _apply_item_metadata(list_item, stream, stype, item_meta, filename)

    _attach_subtitles(list_item, behavior_hints, stype, sid)

    _record_now_playing_and_maybe_resume(stype, sid, video_id, item_meta)

    return url, list_item


def play(handle, stream, stype, sid, item_meta=None, video_id=None):
    """Resolve `stream` and hand it to Kodi via `setResolvedUrl` - the
    classical GetDirectory play path (action=play).

    `item_meta` (optional `{'label', 'art', 'meta'}`, every key optional
    - see `_resolve_playable_item`'s docstring) forwards the content
    title/artwork/meta the caller already resolved, fixing the live OSD
    bug where a stream with no filename of its own left Kodi showing
    "Not available" and the default camera placeholder. `None` (the
    default) behaves exactly as before this parameter existed.

    `video_id` (optional) is forwarded to `_resolve_playable_item()`
    unchanged - see that function's docstring.
    """
    _url, list_item = _resolve_playable_item(stream, stype, sid, item_meta=item_meta, video_id=video_id)
    if list_item is None:
        xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem())
        return
    xbmcplugin.setResolvedUrl(handle, True, list_item)


def play_direct(stream, stype, sid, item_meta=None, on_ready=None, video_id=None):
    """Resolve `stream` and hand it DIRECTLY to `xbmc.Player()` - the
    custom-window path (`lib.ui.streamswindow`), where there is no
    `ADDON_HANDLE`/GetDirectory call to satisfy. Returns True if
    playback was started, False on a resolution failure (already
    notified by `_resolve_playable_item`).

    `item_meta` is forwarded to `_resolve_playable_item()` unchanged -
    see `play()`'s docstring for what it fixes and why. `on_ready`, when
    given, is called with no arguments immediately before
    `xbmc.Player().play()` and ONLY once resolution actually succeeded,
    so a caller (e.g. `lib.ui.streamswindow`) can act at the exact
    moment playback is handed off rather than guessing earlier. Any
    exception it raises is logged at LOGWARNING and swallowed - a broken
    hook must never prevent playback that has already been resolved.

    `video_id` (optional) is forwarded to `_resolve_playable_item()`
    unchanged - see that function's docstring.
    """
    url, list_item = _resolve_playable_item(stream, stype, sid, item_meta=item_meta, video_id=video_id)
    if list_item is None:
        return False
    if on_ready is not None:
        try:
            on_ready()
        except Exception as exc:  # noqa: BLE001 - a hook failure must never block playback
            log('player: on_ready hook failed: %r' % (exc,), xbmc.LOGWARNING)
    xbmc.Player().play(url, list_item)
    return True
