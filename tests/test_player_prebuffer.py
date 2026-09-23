# ruff: noqa: F811
"""Tests for the torrent pre-buffer polling state machine in lib.ui.player.

Split from tests/test_player_buffer.py: this file owns `_prebuffer_torrent()`
itself - front-priming retry loop, cancellation (dialog/monitor), infoHash
normalization, the /create metadata-wait sub-loop (missing/None/-1 fileIdx,
guessedFileIdx rebuild, largest-file fallback), the `may_start_early` swarm-
speed gate, `_wait_for_server`, and the shared staged-dialog plumbing that
drives all of it. URL resolution / `_resolve_playable_item` / item metadata
live in test_player_buffer.py; playback callbacks/resume/now-playing live in
test_player_callbacks.py. Shared fixtures (`kodi_stubs`, `_ServerScript`,
`_torrent_stream`, `_resolved_one`, `_NullMonitor`, `INFO_HASH`,
`DEFAULT_TARGET_BYTES`) are defined once in test_player_buffer.py (the
largest/original file) and imported here to avoid duplicating them across
three files.

Reference: lib/ui/player.py `_prebuffer_torrent()`. See test_player_buffer.py's
module docstring for the FRONT-PRIMING REWRITE / STAGED-DIALOG REWORK
background these tests guard.
"""
import pytest

from tests.test_player_buffer import (  # noqa: F401 - re-exported fixtures/helpers
    DEFAULT_TARGET_BYTES,
    INFO_HASH,
    _NullMonitor,
    _resolved_one,
    _ServerScript,
    _torrent_stream,
    kodi_stubs,
)

# --- buffer_enable=False: pre-buffer entirely skipped ---------------------


def test_buffer_disabled_skips_engine_and_resolves_immediately(kodi_stubs, monkeypatch):
    env = kodi_stubs.env
    env.addon.settings['buffer_enable'] = False
    script = _ServerScript(resolve_url='http://server/x/0').install(monkeypatch, kodi_stubs.player)

    kodi_stubs.player.play(1, _torrent_stream(fileIdx=0), 'movie', 'tt1')

    assert script.create_engine_calls == []
    assert script.iter_front_calls == []
    # A dialog IS still created/closed for the connect+resolve stages -
    # only the torrent-specific engine warm/metadata/buffer stages are
    # skipped by buffer_enable=False.
    assert env.dialog_created == [('STR30080', 'Example Movie')]
    assert env.dialog_closed_count == 1
    handle, succeeded, list_item = _resolved_one(env)
    assert (handle, succeeded) == (1, True)
    assert list_item.path == 'http://server/x/0'


# --- happy path: front read crosses the header floor, resolves True -------


def test_happy_path_streams_front_to_target_then_resolves_true(kodi_stubs, monkeypatch):
    env = kodi_stubs.env
    env.addon.settings['buffer_mb'] = 1  # clamped up to the 5 MiB floor by setting_int(minimum=5)
    half = DEFAULT_TARGET_BYTES // 2
    script = _ServerScript(
        resolve_url='http://server/x/0',
        iter_front_attempts=[[half, half]],  # two chunks summing exactly to the target
    ).install(monkeypatch, kodi_stubs.player)

    kodi_stubs.player.play(2, _torrent_stream(fileIdx=0), 'movie', 'tt2')

    assert script.create_engine_calls == [INFO_HASH, INFO_HASH]  # engine warm + one buffering stats poll
    assert script.iter_front_calls == [(INFO_HASH, 0, DEFAULT_TARGET_BYTES)]
    assert env.dialog_created == [('STR30080', 'Example Movie')]
    # percent = 40 + got * 60 // target; pinned by the exact byte counts
    # above so a flipped clamp/off-by-one reddens this. Filtered to the
    # buffer band (>=40) since resolve/engine-warm ticks precede it.
    assert [percent for percent, _, _, _ in env.dialog_updates if percent >= 40] == [70, 100]
    assert env.dialog_closed_count == 1
    handle, succeeded, list_item = _resolved_one(env)
    assert (handle, succeeded) == (2, True)
    assert list_item.path == 'http://server/x/0'

def test_prebuffer_normalizes_uppercase_infohash_before_polling(kodi_stubs, monkeypatch):
    """player.py:403's pre-buffer polling (create_engine/iter_front) used
    to feed `stream['infoHash']` straight through, while the play-URL
    path already normalizes via stremio.server.normalize_info_hash
    (server.py:148). An uppercase/whitespace-padded infoHash - a form
    normalize_info_hash explicitly accepts - therefore polled a
    different (wrongly-cased) URL than the one actually played,
    breaking the /create and iter_front lookups. Both must now see the
    same 40-char lowercase hex hash normalize_info_hash produces.
    """
    env = kodi_stubs.env
    env.addon.settings['buffer_mb'] = 1
    half = DEFAULT_TARGET_BYTES // 2
    raw_hash = ' %s ' % INFO_HASH.upper()
    script = _ServerScript(
        resolve_url='http://server/x/0',
        iter_front_attempts=[[half, half]],
    ).install(monkeypatch, kodi_stubs.player)

    kodi_stubs.player.play(2, _torrent_stream(fileIdx=0, infoHash=raw_hash), 'movie', 'tt2')

    assert script.create_engine_calls == [INFO_HASH, INFO_HASH]
    assert script.iter_front_calls == [(INFO_HASH, 0, DEFAULT_TARGET_BYTES)]
    handle, succeeded, list_item = _resolved_one(env)
    assert (handle, succeeded) == (2, True)



def test_partial_front_above_header_floor_resolves_true_without_reaching_target(kodi_stubs, monkeypatch):
    """A single front-read attempt that gets enough for ffmpeg to probe
    (_HEADER_MIN_BYTES = 512 KiB) but falls well short of the configured
    buffer_mb target must still start playback immediately - the server's
    own readahead keeps filling ahead once playback begins; there is no
    reason to keep the user waiting once the header is obtainable.
    """
    env = kodi_stubs.env
    script = _ServerScript(
        resolve_url='http://server/x/0',
        iter_front_attempts=[[600_000]],  # > 512 KiB, well under the 5 MiB target
    ).install(monkeypatch, kodi_stubs.player)

    kodi_stubs.player.play(2, _torrent_stream(fileIdx=0), 'movie', 'tt2b')

    assert script.iter_front_calls == [(INFO_HASH, 0, DEFAULT_TARGET_BYTES)]  # one attempt was enough
    handle, succeeded, list_item = _resolved_one(env)
    assert (handle, succeeded) == (2, True)
    assert list_item.path == 'http://server/x/0'


def test_buffering_loop_throttles_dialog_update_to_when_displayed_values_move(kodi_stubs, monkeypatch):
    """Per-chunk dialog.update() must only rebuild/send when human_size(got)
    or the integer percent - the two values the user actually sees - have
    moved since the previous chunk. A 20 MB pre-buffer at 10 KB chunks is
    ~2000 chunks; skipping the rebuild on the ones that would look
    identical is the whole point of the throttle (see _prebuffer_torrent's
    comment). The five middle 1 KB chunks below round to the same
    '1.9 MB'/62% the first chunk already showed and must not re-trigger
    update(); the final chunk (landing exactly on the 5 MB target) moves
    both and must.
    """
    env = kodi_stubs.env
    player = kodi_stubs.player
    chunks = [2_000_000, 1_000, 1_000, 1_000, 1_000, 1_000, 3_237_880]  # sums exactly to the 5 MB target
    script = _ServerScript(
        resolve_url='http://server/x/0', iter_front_attempts=[chunks],
    ).install(monkeypatch, player)
    dialog = player.RivuletProgress()

    proceed, _, _ = player._prebuffer_torrent(
        script.build_class()('http://server'), _torrent_stream(fileIdx=0),
        'http://server/x/0', dialog, _NullMonitor(),
    )

    assert proceed is True
    buffer_updates = [(percent, message) for percent, message, _, _ in env.dialog_updates if percent >= 40]
    assert buffer_updates == [
        (62, 'buffered 1.9 MB of 5.0 MB'),
        (100, 'buffered 5.0 MB of 5.0 MB'),
    ]


def test_buffering_loop_polls_iscanceled_on_every_chunk_even_when_update_is_skipped(kodi_stubs, monkeypatch):
    """Cancellation responsiveness must never regress: iscanceled() is
    polled on EVERY chunk regardless of whether the throttle above skipped
    that chunk's dialog.update() - the same seven-chunk script as the
    throttle test above, five of which are throttled.
    """
    env = kodi_stubs.env
    player = kodi_stubs.player
    chunks = [2_000_000, 1_000, 1_000, 1_000, 1_000, 1_000, 3_237_880]
    script = _ServerScript(
        resolve_url='http://server/x/0', iter_front_attempts=[chunks],
    ).install(monkeypatch, player)
    dialog = player.RivuletProgress()

    proceed, _, _ = player._prebuffer_torrent(
        script.build_class()('http://server'), _torrent_stream(fileIdx=0),
        'http://server/x/0', dialog, _NullMonitor(),
    )

    assert proceed is True
    assert env.dialog_iscanceled_calls >= len(chunks)


# --- cancellation: either trigger resolves False and closes the dialog ----


def test_cancel_via_dialog_iscanceled_resolves_false(kodi_stubs, monkeypatch):
    env = kodi_stubs.env
    env.cancel = True
    script = _ServerScript(
        iter_front_attempts=[[100]],
    ).install(monkeypatch, kodi_stubs.player)

    kodi_stubs.player.play(3, _torrent_stream(fileIdx=0), 'movie', 'tt3')

    assert script.create_engine_calls == []  # cancelled right after resolving, before any torrent network call
    assert script.iter_front_calls == []  # cancelled before the first front-read attempt
    assert env.dialog_closed_count == 1
    handle, succeeded, list_item = _resolved_one(env)
    assert (handle, succeeded) == (3, False)
    assert list_item.path == ''  # xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem())


def test_cancel_via_monitor_waitforabort_resolves_false(kodi_stubs, monkeypatch):
    env = kodi_stubs.env
    env.monitor_abort = True
    script = _ServerScript(
        iter_front_attempts=[[100]],  # well under the header floor, so the loop proceeds to wait/abort
    ).install(monkeypatch, kodi_stubs.player)

    kodi_stubs.player.play(4, _torrent_stream(fileIdx=0), 'movie', 'tt4')

    assert len(script.iter_front_calls) == 1  # one attempt happens before the abort
    assert env.monitor_abort_calls == 1
    assert env.dialog_closed_count == 1
    handle, succeeded, list_item = _resolved_one(env)
    assert (handle, succeeded) == (4, False)


# --- no usable front data ever: notifies 30084, resolves False honestly ---


def test_timeout_with_no_front_data_notifies_30084_and_resolves_false(kodi_stubs, monkeypatch):
    """The live production bug's dead-torrent case: every front-read
    attempt returns far too little to probe (a 1-peer swarm with no front
    pieces available). Rather than hand Kodi a doomed URL, pre-buffer must
    give up after the full budget and fail honestly.
    """
    env = kodi_stubs.env
    script = _ServerScript(
        resolve_url='http://server/x/0',
        iter_front_attempts=[[10]],  # far below the 512 KiB header floor, every attempt
    ).install(monkeypatch, kodi_stubs.player)

    kodi_stubs.player.play(5, _torrent_stream(fileIdx=0), 'movie', 'tt5')

    assert len(script.iter_front_calls) == 60  # _BUFFER_MAX_WAIT_SECONDS / 2s retry cadence
    assert env.monitor_abort_calls == 60
    assert [msg for _, msg, _, _ in env.notifications] == ['STR30084']
    assert env.dialog_closed_count == 1
    handle, succeeded, list_item = _resolved_one(env)
    assert (handle, succeeded) == (5, False)
    assert list_item.path == ''


# --- engine-warm failure (known fileIdx path) is non-fatal -----------------


def test_engine_warm_exception_is_nonfatal_front_streaming_still_proceeds(kodi_stubs, monkeypatch):
    """When the fileIdx is already known, create_engine() is only a best-
    effort warm - the front reads drive the engine regardless. A failing
    warm must be logged and swallowed, NOT abort pre-buffer, so front
    streaming still runs and succeeds on its own.
    """
    env = kodi_stubs.env
    script = _ServerScript(
        resolve_url='http://server/x/0',
        create_engine_error=RuntimeError('engine boom'),
        iter_front_attempts=[[600_000]],
    ).install(monkeypatch, kodi_stubs.player)

    kodi_stubs.player.play(6, _torrent_stream(fileIdx=0), 'movie', 'tt6')

    assert script.create_engine_calls == [INFO_HASH, INFO_HASH]  # warm attempted + one buffering stats poll (both fail)
    assert any(level == kodi_stubs.player.xbmc.LOGWARNING for _, level in env.log_calls)
    assert script.iter_front_calls == [(INFO_HASH, 0, DEFAULT_TARGET_BYTES)]  # AND front streaming proceeded
    assert env.dialog_closed_count == 1
    handle, succeeded, list_item = _resolved_one(env)
    assert (handle, succeeded) == (6, True)
    assert list_item.path == 'http://server/x/0'


# --- iter_front() exceptions are retried, not treated as fatal -------------


def test_iter_front_exception_every_attempt_times_out_notifies_30084(kodi_stubs, monkeypatch):
    """A front-read exception (e.g. a transient connection error) must be
    logged and RETRIED, not treated as an immediate "give up and play
    anyway" signal like the old aggregate-stats exception handling did -
    a single hiccup shouldn't hand Kodi a doomed URL any more than a
    single zero-byte attempt should. If every attempt keeps failing, the
    budget still exhausts to the same honest 30084 failure.
    """
    env = kodi_stubs.env
    script = _ServerScript(
        resolve_url='http://server/x/0',
        iter_front_attempts=[RuntimeError('front boom')],
    ).install(monkeypatch, kodi_stubs.player)

    kodi_stubs.player.play(7, _torrent_stream(fileIdx=0), 'movie', 'tt7')

    assert script.create_engine_calls == [INFO_HASH] * 61  # warm + one buffering stats poll per front attempt
    assert len(script.iter_front_calls) == 60
    assert any(level == kodi_stubs.player.xbmc.LOGWARNING for _, level in env.log_calls)
    assert [msg for _, msg, _, _ in env.notifications] == ['STR30084']
    assert env.dialog_closed_count == 1
    handle, succeeded, list_item = _resolved_one(env)
    assert (handle, succeeded) == (7, False)


def test_iter_front_exception_then_recovers_on_retry(kodi_stubs, monkeypatch):
    env = kodi_stubs.env
    script = _ServerScript(
        resolve_url='http://server/x/0',
        iter_front_attempts=[RuntimeError('transient'), [600_000]],
    ).install(monkeypatch, kodi_stubs.player)

    kodi_stubs.player.play(7, _torrent_stream(fileIdx=0), 'movie', 'tt7b')

    assert len(script.iter_front_calls) == 2  # first attempt failed, second succeeded
    assert env.monitor_abort_calls == 1  # one wait between the failed attempt and the retry
    handle, succeeded, list_item = _resolved_one(env)
    assert (handle, succeeded) == (7, True)
    assert list_item.path == 'http://server/x/0'


# --- missing/None/-1 fileIdx: rebuild via guessedFileIdx + torrent_url ----


@pytest.mark.parametrize(
    'file_idx_override',
    [{}, {'fileIdx': None}, {'fileIdx': -1}],
    ids=['missing', 'none', 'negative_one'],
)
def test_missing_file_idx_rebuilds_url_and_streams_guessed_index(kodi_stubs, monkeypatch, file_idx_override):
    env = kodi_stubs.env
    stream = _torrent_stream(**file_idx_override)
    script = _ServerScript(
        resolve_url='http://server/x/-1',
        create_engine_result={'guessedFileIdx': 4},
        iter_front_attempts=[[600_000]],
        torrent_url_result='http://server/x/4',
    ).install(monkeypatch, kodi_stubs.player)

    kodi_stubs.player.play(8, stream, 'movie', 'tt8')

    assert script.torrent_url_calls == [(INFO_HASH, 4, tuple(stream['announce']))]
    assert script.iter_front_calls == [(INFO_HASH, 4, DEFAULT_TARGET_BYTES)]  # streams the guessed index, not -1
    handle, succeeded, list_item = _resolved_one(env)
    assert (handle, succeeded) == (8, True)
    assert list_item.path == 'http://server/x/4'  # resolved to the rebuilt url, not the original


# --- v0.8.5 gap: /create never gains guessedFileIdx; files[] appears once
# --- metadata resolves, and /create must be re-polled to see it ----------


@pytest.mark.parametrize(
    'create_engine_result',
    [{}, {'guessedFileIdx': -1}, {'files': []}],
    ids=['absent', 'negative', 'empty_files'],
)
def test_metadata_never_resolves_exhausts_budget_and_proceeds(kodi_stubs, monkeypatch, create_engine_result):
    """Every /create poll comes back with nothing guess_file_idx() can use
    (contract: 'stats never yields files/idx -> budget exhausted ->
    proceed'). This replaces the old immediate-skip expectation: v0.8.5's
    /create response never grows a guessedFileIdx later, so the only sane
    behaviour left is to keep polling for the full budget, then fall back
    to unbuffered playback exactly like a genuine metadata timeout would.
    This is a DIFFERENT failure mode from "we resolved an index but its
    front data never arrived" (30084): here we never even got metadata to
    check, so trying anyway (30083) is the only option left.
    """
    env = kodi_stubs.env
    script = _ServerScript(
        resolve_url='http://server/x/-1',
        create_engine_result=create_engine_result,
    ).install(monkeypatch, kodi_stubs.player)

    kodi_stubs.player.play(9, _torrent_stream(), 'movie', 'tt9')  # fileIdx missing -> UNKNOWN_FILE_IDX

    assert len(script.create_engine_calls) == 60  # _MAX_METADATA_ATTEMPTS; never resolves an index
    assert env.monitor_abort_calls == 60
    assert script.torrent_url_calls == []
    assert script.iter_front_calls == []  # never reached per-file front streaming
    assert [msg for _, msg, _, _ in env.notifications] == ['STR30083']
    assert env.dialog_closed_count == 1
    handle, succeeded, list_item = _resolved_one(env)
    assert (handle, succeeded) == (9, True)
    assert list_item.path == 'http://server/x/-1'  # original url, never rebuilt


def test_files_array_without_guessed_idx_picks_largest_file_and_streams_it(kodi_stubs, monkeypatch):
    """v0.8.5 shape confirmed live: /create's response carries `files`
    ([{name, path, length, offset}, ...]) but no `guessedFileIdx` at all -
    guess_file_idx() must pick the largest file itself, and front streaming
    must engage against that index (not stall like the old
    guessedFileIdx-only code path did).
    """
    env = kodi_stubs.env
    stream = _torrent_stream()  # fileIdx missing -> UNKNOWN_FILE_IDX
    files = [
        {'name': 'sample.mkv', 'length': 1024},
        {'name': 'Sintel.mkv', 'length': 129241752},
        {'name': 'subs.srt', 'length': 2048},
    ]
    script = _ServerScript(
        resolve_url='http://server/x/-1',
        create_engine_result={'files': files},
        iter_front_attempts=[[600_000]],
        torrent_url_result='http://server/x/1',
    ).install(monkeypatch, kodi_stubs.player)

    kodi_stubs.player.play(11, stream, 'movie', 'tt11')

    assert script.create_engine_calls == [INFO_HASH, INFO_HASH]  # resolved on the very first /create poll + one buffering stats poll
    assert script.torrent_url_calls == [(INFO_HASH, 1, tuple(stream['announce']))]
    assert script.iter_front_calls == [(INFO_HASH, 1, DEFAULT_TARGET_BYTES)]  # streams the largest file's index
    handle, succeeded, list_item = _resolved_one(env)
    assert (handle, succeeded) == (11, True)
    assert list_item.path == 'http://server/x/1'


def test_metadata_arrives_on_third_create_poll(kodi_stubs, monkeypatch):
    """The metadata-wait loop must keep re-polling /create (not just call
    it once) and, once resolved, spend only the REMAINING shared budget on
    front streaming - not a fresh 120s.
    """
    env = kodi_stubs.env
    stream = _torrent_stream()  # fileIdx missing -> UNKNOWN_FILE_IDX
    no_metadata_yet = {'peers': 2}
    still_no_metadata = {'peers': 5}
    resolved = {'files': [{'length': 100}, {'length': 900}]}
    script = _ServerScript(
        resolve_url='http://server/x/-1',
        create_engine_results=[no_metadata_yet, still_no_metadata, resolved],
        iter_front_attempts=[[600_000]],
        torrent_url_result='http://server/x/1',
    ).install(monkeypatch, kodi_stubs.player)

    kodi_stubs.player.play(12, stream, 'movie', 'tt12')

    assert len(script.create_engine_calls) == 4  # 3 metadata polls + 1 buffering stats poll
    assert env.monitor_abort_calls == 2  # one wait after each of the first two unresolved polls
    # metadata-wait phase now ticks 20-35% with the live attempt count
    # (was an indeterminate 0%); filter by the stage-line marker so the
    # new resolve-stage tick ahead of it doesn't shift indices.
    metadata_updates = [percent for percent, message, _, _ in env.dialog_updates if 'STR30088' in message]
    assert metadata_updates == [20, 21]
    assert script.torrent_url_calls == [(INFO_HASH, 1, tuple(stream['announce']))]
    assert script.iter_front_calls == [(INFO_HASH, 1, DEFAULT_TARGET_BYTES)]  # continues with the shared, not reset, budget
    handle, succeeded, list_item = _resolved_one(env)
    assert (handle, succeeded) == (12, True)
    assert list_item.path == 'http://server/x/1'


def test_cancel_during_metadata_wait_resolves_false(kodi_stubs, monkeypatch):
    env = kodi_stubs.env
    env.cancel = True
    script = _ServerScript(
        create_engine_result={},
    ).install(monkeypatch, kodi_stubs.player)

    kodi_stubs.player.play(13, _torrent_stream(), 'movie', 'tt13')  # fileIdx missing -> UNKNOWN_FILE_IDX

    assert script.create_engine_calls == []  # cancelled before the first /create poll
    assert script.iter_front_calls == []
    assert env.dialog_closed_count == 1
    handle, succeeded, list_item = _resolved_one(env)
    assert (handle, succeeded) == (13, False)
    assert list_item.path == ''


def test_cancel_partway_through_metadata_wait_resolves_false(kodi_stubs, monkeypatch):
    """A cancel that arrives mid-metadata-wait (not from the very start)
    must still be honored by `_await_file_idx`'s OWN loop check, not just
    caught earlier by the resolve-stage/prebuffer-entry guards above it.
    Keyed off a stable observable (polls so far) rather than a raw
    iscanceled() call count, so it stays correct regardless of exactly
    how many other cancel checks run before the metadata loop.
    """
    env = kodi_stubs.env
    script = _ServerScript(create_engine_result={}).install(monkeypatch, kodi_stubs.player)
    env.cancel = lambda: len(script.create_engine_calls) >= 2

    kodi_stubs.player.play(13, _torrent_stream(), 'movie', 'tt13b')  # fileIdx missing -> UNKNOWN_FILE_IDX

    assert len(script.create_engine_calls) == 2  # polled twice, cancelled before a third
    assert script.iter_front_calls == []
    assert env.dialog_closed_count == 1
    handle, succeeded, list_item = _resolved_one(env)
    assert (handle, succeeded) == (13, False)
    assert list_item.path == ''


# --- buffer_enable read via raw getSetting() string (resolve-time fix) ----


def test_buffer_enable_missing_key_defaults_on_and_streams_front(kodi_stubs, monkeypatch):
    """Production bug repro: settings.xml has buffer_enable=true, but at
    resolve-time `ADDON.getSettingBool()` has been observed to flake and
    return False - see lib/ui/compat.py's `setting_bool()` docstring.
    Simulate that as `getSetting('buffer_enable')` coming back '' (as it
    would for a genuinely missing/unreadable key): pre-buffer must still
    default ON and stream the front, not silently vanish before ever
    logging or creating the dialog.
    """
    env = kodi_stubs.env
    env.addon.settings['buffer_enable'] = ''  # raw getSetting() for a missing/unreadable key
    script = _ServerScript(
        resolve_url='http://server/x/0',
        iter_front_attempts=[[600_000]],
    ).install(monkeypatch, kodi_stubs.player)

    kodi_stubs.player.play(14, _torrent_stream(fileIdx=0), 'movie', 'tt14')

    assert script.create_engine_calls == [INFO_HASH, INFO_HASH]  # engine warm + one buffering stats poll: pre-buffer ran
    assert script.iter_front_calls == [(INFO_HASH, 0, DEFAULT_TARGET_BYTES)]  # AND streamed - not skipped
    assert env.dialog_created == [('STR30080', 'Example Movie')]
    assert env.dialog_closed_count == 1
    handle, succeeded, list_item = _resolved_one(env)
    assert (handle, succeeded) == (14, True)
    assert list_item.path == 'http://server/x/0'


def test_buffer_enable_raw_false_string_still_skips(kodi_stubs, monkeypatch):
    """An explicit user "off" (settings.xml -> raw getSetting() == 'false')
    must still disable pre-buffering - only a missing/unreadable value
    defaults ON, never an explicit off. The shared dialog still shows its
    connect/resolve feedback (created once, by `_resolve_playable_item`)
    even though the torrent-specific buffering stage never runs.
    """
    env = kodi_stubs.env
    env.addon.settings['buffer_enable'] = 'false'
    script = _ServerScript(resolve_url='http://server/x/0').install(monkeypatch, kodi_stubs.player)

    kodi_stubs.player.play(15, _torrent_stream(fileIdx=0), 'movie', 'tt15')

    assert script.create_engine_calls == []
    assert script.iter_front_calls == []
    assert env.dialog_created == [('STR30080', 'Example Movie')]
    assert env.dialog_closed_count == 1
    handle, succeeded, list_item = _resolved_one(env)
    assert (handle, succeeded) == (15, True)
    assert list_item.path == 'http://server/x/0'


# --- LOGINFO traceability: kodi.log must show which branch ran ------------


def test_prebuffer_entry_always_logs_enable_and_file_idx_at_loginfo(kodi_stubs, monkeypatch):
    """The exact fix for the live bug: entry into `_prebuffer_torrent` now
    logs unconditionally, BEFORE the buffer_enable check short-circuits -
    so a future kodi.log always shows which branch ran, even when
    pre-buffering ends up skipped.
    """
    env = kodi_stubs.env
    env.addon.settings['buffer_enable'] = False
    _ServerScript(resolve_url='http://server/x/0').install(monkeypatch, kodi_stubs.player)

    kodi_stubs.player.play(16, _torrent_stream(fileIdx=26), 'movie', 'tt16')

    loginfo = kodi_stubs.player.xbmc.LOGINFO
    entries = [msg for msg, level in env.log_calls if level == loginfo]
    assert any('buffer_enable=False' in msg and 'fileIdx=26' in msg for msg in entries), entries


def test_prebuffer_logs_an_early_start_as_such_not_as_complete(kodi_stubs, monkeypatch):
    """600KB clears the header floor but is nowhere near the target, so
    playback starts on the server's readahead. Saying "complete" there made
    a real log read as though the buffer was full moments before the stream
    starved - the message must distinguish the two."""
    env = kodi_stubs.env
    _ServerScript(
        resolve_url='http://server/x/0', iter_front_attempts=[[600_000]],
    ).install(monkeypatch, kodi_stubs.player)

    kodi_stubs.player.play(17, _torrent_stream(fileIdx=0), 'movie', 'tt17')

    loginfo = kodi_stubs.player.xbmc.LOGINFO
    entries = [msg for msg, level in env.log_calls if level == loginfo]
    assert any('buffer_enable=True' in msg and 'fileIdx=0' in msg for msg in entries), entries
    assert any('buffer_mb=' in msg and 'target_bytes=' in msg for msg in entries), entries
    assert any('header floor reached, starting early' in msg for msg in entries), entries
    assert not any('pre-buffer complete' in msg for msg in entries), entries


def test_prebuffer_logs_complete_only_when_the_target_is_reached(kodi_stubs, monkeypatch):
    """The other branch: a front read that actually reaches the configured
    target is the only thing allowed to call itself complete."""
    env = kodi_stubs.env
    _ServerScript(
        resolve_url='http://server/x/0', iter_front_attempts=[[5 * 1024 * 1024]],
    ).install(monkeypatch, kodi_stubs.player)

    kodi_stubs.player.play(17, _torrent_stream(fileIdx=0), 'movie', 'tt17')

    loginfo = kodi_stubs.player.xbmc.LOGINFO
    entries = [msg for msg, level in env.log_calls if level == loginfo]
    assert any('pre-buffer complete' in msg for msg in entries), entries
    assert not any('starting early' in msg for msg in entries), entries


def test_may_start_early_blocks_a_measurably_starved_swarm(kodi_stubs):
    """The live failure: a one-peer swarm at ~174 KB/s cleared the header
    floor at 1.2MB of a 20MB target and started, then starved within
    seconds. Topping up the remaining ~19MB at that speed takes ~113s, far
    past the early-start budget, so it must NOT start early."""
    player = kodi_stubs.player
    target = 20 * 1024 * 1024

    assert player._may_start_early({'downloadSpeed': 174687.6}, 1245184, target, 0.0) is False


def test_may_start_early_allows_a_swarm_that_outruns_playback(kodi_stubs):
    """A swarm that can top the rest of the buffer up within the budget is
    comfortably faster than playback drains it - keep the fast start."""
    player = kodi_stubs.player
    target = 20 * 1024 * 1024

    assert player._may_start_early({'downloadSpeed': 8 * 1024 * 1024}, 1245184, target, 0.0) is True


def test_may_start_early_treats_absent_stats_as_unknown_not_slow(kodi_stubs):
    """The stats poll is best-effort and fails on exactly the struggling
    servers this runs against. Missing evidence must not be read as slow,
    or a flaky stats endpoint would invent a long wait on a healthy swarm."""
    player = kodi_stubs.player
    target = 20 * 1024 * 1024

    assert player._may_start_early(None, 1245184, target, 0.0) is True
    assert player._may_start_early({}, 1245184, target, 0.0) is True
    assert player._may_start_early({'downloadSpeed': 'nonsense'}, 1245184, target, 0.0) is True


def test_may_start_early_blocks_a_reported_dead_stall(kodi_stubs):
    """A speed the server actually reported as zero is evidence, not
    absence of it."""
    player = kodi_stubs.player

    assert player._may_start_early({'downloadSpeed': 0}, 1245184, 20 * 1024 * 1024, 0.0) is False


def test_may_start_early_gives_up_waiting_after_the_budget(kodi_stubs):
    """A slow-but-alive swarm must eventually play rather than buffer
    forever behind the gate."""
    player = kodi_stubs.player
    target = 20 * 1024 * 1024
    slow = {'downloadSpeed': 174687.6}

    assert player._may_start_early(slow, 1245184, target, player._TARGET_WAIT_SECONDS) is True


def test_prebuffer_starts_late_rather_than_failing_when_budget_is_spent(kodi_stubs, monkeypatch):
    """With the gate holding a starved swarm back, the retry budget can now
    run out while the header floor HAS been cleared. Before the gate that
    case played immediately, so it must still play - refusing a merely-slow
    stream would be a regression."""
    env = kodi_stubs.env
    _ServerScript(
        resolve_url='http://server/x/0',
        # First attempt clears the floor; every retry after that gets
        # nothing further (resume means a repeated attempt now means
        # "more bytes arrived", so an empty list is what a genuinely
        # stalled swarm looks like under start_byte resume), while stats
        # stay measurably slow with no downloaded-bytes progress evidence.
        iter_front_attempts=[[600_000], []],
        create_engine_result={'downloadSpeed': 1000, 'peers': 1},
    ).install(monkeypatch, kodi_stubs.player)

    kodi_stubs.player.play(19, _torrent_stream(fileIdx=0), 'movie', 'tt19')

    entries = [msg for msg, level in env.log_calls]
    assert any('budget spent' in msg for msg in entries), entries
    assert not any('pre-buffer timed out' in msg for msg in entries), entries


def test_prebuffer_timeout_logged_at_loginfo(kodi_stubs, monkeypatch):
    env = kodi_stubs.env
    _ServerScript(
        resolve_url='http://server/x/0',
        iter_front_attempts=[[10]],  # far below the header floor, every attempt
    ).install(monkeypatch, kodi_stubs.player)

    kodi_stubs.player.play(18, _torrent_stream(fileIdx=0), 'movie', 'tt18')

    loginfo = kodi_stubs.player.xbmc.LOGINFO
    entries = [msg for msg, level in env.log_calls if level == loginfo]
    assert any('pre-buffer timed out' in msg for msg in entries), entries


# --- _wait_for_server: brief cancellable wait for the streaming server ----


def test_server_available_immediately_no_wait_dialog(kodi_stubs, monkeypatch):
    env = kodi_stubs.env
    script = _ServerScript(
        available=True, resolve_url='http://server/x/0', iter_front_attempts=[[600_000]],
    ).install(monkeypatch, kodi_stubs.player)

    kodi_stubs.player.play(20, _torrent_stream(fileIdx=0), 'movie', 'tt20')

    assert script.is_available_calls == 1  # single probe, no wait loop
    assert not any('STR30086' in message for _, message, _, _ in env.dialog_updates)  # connect stage never ticks
    handle, succeeded, _ = _resolved_one(env)
    assert (handle, succeeded) == (20, True)


def test_server_comes_up_during_wait_then_proceeds(kodi_stubs, monkeypatch):
    """A server the background service is still launching should be waited
    for briefly rather than failing on the first probe."""
    env = kodi_stubs.env
    script = _ServerScript(
        available_results=[False, False, True],  # up on the third probe
        resolve_url='http://server/x/0',
        iter_front_attempts=[[600_000]],
    ).install(monkeypatch, kodi_stubs.player)

    kodi_stubs.player.play(21, _torrent_stream(fileIdx=0), 'movie', 'tt21')

    assert script.is_available_calls == 3  # kept probing until it came up
    handle, succeeded, list_item = _resolved_one(env)
    assert (handle, succeeded) == (21, True)
    assert list_item.path == 'http://server/x/0'


def test_server_never_comes_up_notifies_unavailable_and_resolves_false(kodi_stubs, monkeypatch):
    env = kodi_stubs.env
    script = _ServerScript(available=False).install(monkeypatch, kodi_stubs.player)

    kodi_stubs.player.play(22, _torrent_stream(fileIdx=0), 'movie', 'tt22')

    assert script.create_engine_calls == []  # never entered pre-buffer
    assert [msg for _, msg, _, _ in env.notifications] == ['STR30031']
    handle, succeeded, list_item = _resolved_one(env)
    assert (handle, succeeded) == (22, False)
    assert list_item.path == ''


def test_server_wait_cancelled_resolves_false(kodi_stubs, monkeypatch):
    env = kodi_stubs.env
    env.cancel = True
    script = _ServerScript(available=False).install(monkeypatch, kodi_stubs.player)

    kodi_stubs.player.play(23, _torrent_stream(fileIdx=0), 'movie', 'tt23')

    assert script.create_engine_calls == []
    handle, succeeded, _ = _resolved_one(env)
    assert (handle, succeeded) == (23, False)


def test_single_dialog_spans_connect_wait_and_prebuffer(kodi_stubs, monkeypatch):
    """Guards the core rework: ONE DialogProgress must be created/closed
    for the whole resolve, even when both the connect-wait AND the
    torrent pre-buffer stages run in the same flow (previously each
    helper created and closed its own dialog, so this combination could
    show two in a row).
    """
    env = kodi_stubs.env
    _ServerScript(
        available_results=[False, True],
        resolve_url='http://server/x/0',
        iter_front_attempts=[[600_000]],
    ).install(monkeypatch, kodi_stubs.player)

    kodi_stubs.player.play(41, _torrent_stream(fileIdx=0), 'movie', 'tt41')

    assert env.dialog_created == [('STR30080', 'Example Movie')]  # created exactly once
    assert env.dialog_closed_count == 1  # closed exactly once, not once per helper
    handle, succeeded, list_item = _resolved_one(env)
    assert (handle, succeeded) == (41, True)
    assert list_item.path == 'http://server/x/0'


def test_stage_percents_progress_monotonically_connect_to_buffer(kodi_stubs, monkeypatch):
    """The whole staged dialog must read as real forward progress: connect
    (0-10%) -> resolve (15%) -> metadata (20-35%) -> buffer (40-100%),
    never regressing.
    """
    env = kodi_stubs.env
    _ServerScript(
        available_results=[False, True],
        resolve_url='http://server/x/-1',
        create_engine_results=[{'peers': 1}, {'files': [{'length': 900}]}],
        iter_front_attempts=[[DEFAULT_TARGET_BYTES // 2, DEFAULT_TARGET_BYTES // 2]],
        torrent_url_result='http://server/x/1',
    ).install(monkeypatch, kodi_stubs.player)

    kodi_stubs.player.play(42, _torrent_stream(), 'movie', 'tt42')  # fileIdx missing -> metadata wait engages

    percents = [percent for percent, _, _, _ in env.dialog_updates]
    assert percents == sorted(percents)  # never regresses
    assert percents[0] <= 10  # starts in the connect band
    assert 40 <= percents[-1] <= 100  # ends in the buffer band
    handle, succeeded, list_item = _resolved_one(env)
    assert (handle, succeeded) == (42, True)
    assert list_item.path == 'http://server/x/1'


def test_cancel_partway_through_buffering_loop_resolves_false(kodi_stubs, monkeypatch):
    """A cancel that arrives mid-buffering (after some attempts already
    ran) must be honored by the front-priming loop's OWN check, not just
    caught earlier by the resolve-stage/prebuffer-entry guards above it.
    Keyed off a stable observable (attempts so far) rather than a raw
    iscanceled() call count.
    """
    env = kodi_stubs.env
    script = _ServerScript(
        iter_front_attempts=[[100]],  # always short of the header floor -> always retries
    ).install(monkeypatch, kodi_stubs.player)
    env.cancel = lambda: len(script.iter_front_calls) >= 2

    kodi_stubs.player.play(3, _torrent_stream(fileIdx=0), 'movie', 'tt3c')

    assert len(script.iter_front_calls) == 2  # two attempts ran, cancelled before a third
    assert env.dialog_closed_count == 1
    handle, succeeded, list_item = _resolved_one(env)
    assert (handle, succeeded) == (3, False)


def test_cancel_after_resolve_for_non_torrent_stream_resolves_false(kodi_stubs, monkeypatch):
    """A cancel that lands right after `resolve_stream()` returns (before
    any torrent-specific work would even apply) must still be honored -
    the Resolving stage is cancellable for every stream, torrent or not.
    """
    env = kodi_stubs.env
    env.cancel = True
    _ServerScript(resolve_url='https://example.com/a.mp4').install(monkeypatch, kodi_stubs.player)

    result = kodi_stubs.player.play_direct({'url': 'https://example.com/a.mp4'}, 'movie', 'tt60')

    assert result is False
    assert env.player_play_calls == []
    assert env.dialog_closed_count == 1


def test_buffer_stats_poll_exception_is_best_effort_and_does_not_abort(kodi_stubs, monkeypatch):
    """The live stats poll powering the buffering dialog's second line
    must be pure best-effort: a failure there is cosmetic only and must
    never break the front-priming loop itself.
    """
    env = kodi_stubs.env
    script = _ServerScript(
        resolve_url='http://server/x/0',
        create_engine_error=RuntimeError('stats boom'),
        iter_front_attempts=[[600_000]],
    ).install(monkeypatch, kodi_stubs.player)

    kodi_stubs.player.play(43, _torrent_stream(fileIdx=0), 'movie', 'tt43')

    assert any('buffer stats poll failed' in msg for msg, _ in env.log_calls)
    assert script.iter_front_calls == [(INFO_HASH, 0, DEFAULT_TARGET_BYTES)]  # front streaming still ran
    handle, succeeded, list_item = _resolved_one(env)
    assert (handle, succeeded) == (43, True)
    assert list_item.path == 'http://server/x/0'

# --- resume-from-offset: a retry must not restart the front read at 0 -----


def test_front_read_retry_resumes_from_total_bytes_not_from_zero(kodi_stubs, monkeypatch):
    """A retry after a too-small attempt must ask iter_front() to resume
    from the highest byte already obtained (Range: bytes=<got>-<want-1>
    via ServerClient.iter_front's `start_byte`), not restart the front
    read from offset 0 - and the cumulative total (not just the latest
    attempt's own bytes) must be what the loop measures against the
    header floor/target and shows in the dialog.
    """
    env = kodi_stubs.env
    script = _ServerScript(
        resolve_url='http://server/x/0',
        # First attempt gets a small amount (under the header floor, so
        # the loop retries); the second tops it up past the floor.
        iter_front_attempts=[[100_000], [600_000]],
    ).install(monkeypatch, kodi_stubs.player)

    kodi_stubs.player.play(50, _torrent_stream(fileIdx=0), 'movie', 'tt50')

    assert script.iter_front_start_bytes == [0, 100_000]  # second attempt resumes, never restarts
    handle, succeeded, list_item = _resolved_one(env)
    assert (handle, succeeded) == (50, True)
    assert list_item.path == 'http://server/x/0'
    # The final buffer-stage update must show the CUMULATIVE 700 KB, not
    # just the second attempt's own 600 KB - proving no bytes were lost.
    buffer_updates = [(percent, message) for percent, message, _, _ in env.dialog_updates if percent >= 40]
    assert buffer_updates[-1] == (48, 'buffered 683.6 KB of 5.0 MB')


def test_front_read_retry_after_exception_also_resumes(kodi_stubs, monkeypatch):
    """The resume offset must reflect bytes actually received, not the
    attempt count - an attempt that raised with SOME bytes already
    yielded (via a prior chunk) still contributes to the resume offset;
    an attempt that raised with NOTHING yielded resumes from 0 again."""
    env = kodi_stubs.env
    script = _ServerScript(
        resolve_url='http://server/x/0',
        iter_front_attempts=[RuntimeError('boom'), [600_000]],
    ).install(monkeypatch, kodi_stubs.player)

    kodi_stubs.player.play(50, _torrent_stream(fileIdx=0), 'movie', 'tt50b')

    assert script.iter_front_start_bytes == [0, 0]  # nothing was obtained before the retry
    handle, succeeded, list_item = _resolved_one(env)
    assert (handle, succeeded) == (50, True)


# --- early-start stall guard: a stalled attempt needs progress evidence ---


def test_may_start_early_stalled_without_progress_blocks_even_with_fast_prior_speed(kodi_stubs):
    """A stalled attempt (no new front bytes at all this attempt) must not
    start on stats left over from before the stall, even a fast one -
    only genuine progress evidence may override a stall boundary."""
    player = kodi_stubs.player
    target = 20 * 1024 * 1024
    fast_stats = {'downloadSpeed': 8 * 1024 * 1024}
    assert player._may_start_early(fast_stats, 1245184, target, 0.0, stalled=True, progressed=False) is False


def test_may_start_early_stalled_with_progress_falls_back_to_normal_logic(kodi_stubs):
    """Once progress evidence exists, a stalled attempt is judged exactly
    like a non-stalled one - the guard only removes the "absent/leftover
    speed still means start" shortcut, it never blocks outright."""
    player = kodi_stubs.player
    target = 20 * 1024 * 1024
    fast_stats = {'downloadSpeed': 8 * 1024 * 1024}
    assert player._may_start_early(fast_stats, 1245184, target, 0.0, stalled=True, progressed=True) is True


def test_may_start_early_stalled_default_false_leaves_prior_behaviour_unchanged(kodi_stubs):
    """`stalled` defaults False, so every pre-existing 4-positional-arg
    caller (see the tests above this section) is unaffected."""
    player = kodi_stubs.player
    target = 20 * 1024 * 1024
    assert player._may_start_early(None, 1245184, target, 0.0) is True  # unchanged absent-stats fast path


def test_stall_guard_blocks_absent_stats_then_progress_evidence_allows_start(kodi_stubs, monkeypatch):
    """Integration proof: a slow swarm blocks normally on attempt 1
    (measured speed), a stalled attempt 2 with NO stats at all must NOT
    fall back to the old "absent means fast, start anyway" behaviour it
    would have hit pre-guard, and only once attempt 3's stats show real
    progress (downloaded increasing) does playback start.
    """
    env = kodi_stubs.env
    script = _ServerScript(
        resolve_url='http://server/x/0',
        create_engine_results=[
            {},  # engine warm
            {'downloadSpeed': 174687.6, 'downloaded': 1_000_000, 'peers': 1},  # attempt 1: slow, not stalled
            {},  # attempt 2: absent stats entirely, front stalled
            {'downloaded': 2_000_000},  # attempt 3: no speed, but real progress since attempt 1
        ],
        iter_front_attempts=[[600_000], [], []],
    ).install(monkeypatch, kodi_stubs.player)

    kodi_stubs.player.play(51, _torrent_stream(fileIdx=0), 'movie', 'tt51')

    assert len(script.iter_front_calls) == 3  # blocked on attempts 1 and 2, started on 3
    handle, succeeded, list_item = _resolved_one(env)
    assert (handle, succeeded) == (51, True)
    assert list_item.path == 'http://server/x/0'


# --- feasibility warning: measured speed vs. the file's own bitrate -------


def test_required_bytes_per_second_computes_average_bitrate(kodi_stubs):
    player = kodi_stubs.player
    # 1200s runtime, 1200 MiB file -> 1 MiB/s required.
    assert player._required_bytes_per_second(1200, 1200 * 1024 * 1024) == 1024 * 1024


@pytest.mark.parametrize(
    'runtime_seconds, file_length_bytes',
    [(None, 1000), (1200, None), (0, 1000), (1200, 0), ('nonsense', 1000)],
)
def test_required_bytes_per_second_none_when_inputs_missing_or_invalid(kodi_stubs, runtime_seconds, file_length_bytes):
    player = kodi_stubs.player
    assert player._required_bytes_per_second(runtime_seconds, file_length_bytes) is None


def test_feasibility_warning_needed_true_when_speed_well_below_required(kodi_stubs):
    player = kodi_stubs.player
    file_length = 1200 * 1024 * 1024
    required = player._required_bytes_per_second(1200, file_length)
    assert player._feasibility_warning_needed(1200, file_length, required * 0.5) is True


def test_feasibility_warning_needed_false_when_speed_comfortably_above_required(kodi_stubs):
    player = kodi_stubs.player
    file_length = 1200 * 1024 * 1024
    required = player._required_bytes_per_second(1200, file_length)
    assert player._feasibility_warning_needed(1200, file_length, required * 2) is False


def test_feasibility_warning_needed_false_right_at_the_factor_boundary(kodi_stubs):
    player = kodi_stubs.player
    file_length = 1200 * 1024 * 1024
    required = player._required_bytes_per_second(1200, file_length)
    boundary_speed = required * player._FEASIBILITY_SPEED_FACTOR
    assert player._feasibility_warning_needed(1200, file_length, boundary_speed) is False


def test_feasibility_warning_needed_false_when_runtime_or_size_unknown(kodi_stubs):
    player = kodi_stubs.player
    assert player._feasibility_warning_needed(None, 1000, 1) is False
    assert player._feasibility_warning_needed(1200, None, 1) is False


def test_feasibility_warning_needed_false_when_speed_missing_or_non_positive(kodi_stubs):
    player = kodi_stubs.player
    file_length = 1200 * 1024 * 1024
    assert player._feasibility_warning_needed(1200, file_length, None) is False
    assert player._feasibility_warning_needed(1200, file_length, 0) is False
    assert player._feasibility_warning_needed(1200, file_length, 'nonsense') is False


def test_feasibility_warning_notifies_and_logs_when_speed_too_slow_for_runtime(kodi_stubs, monkeypatch):
    env = kodi_stubs.env
    file_length = 1200 * 1024 * 1024  # 1.2 GiB
    _ServerScript(
        resolve_url='http://server/x/0',
        create_engine_results=[
            {'files': [{'name': 'Movie.mkv', 'length': file_length}]},  # engine warm
            {'downloadSpeed': 100_000, 'peers': 1},  # far too slow for the ~1 MiB/s runtime requires
        ],
        iter_front_attempts=[[DEFAULT_TARGET_BYTES]],
    ).install(monkeypatch, kodi_stubs.player)

    kodi_stubs.player.play(
        52, _torrent_stream(fileIdx=0), 'movie', 'tt52', item_meta={'meta': {'runtime': '20 min'}},
    )

    assert any('feasibility warning' in msg for msg, _ in env.log_calls)
    assert [msg for _, msg, _, _ in env.notifications] == ['STR30361']
    handle, succeeded, list_item = _resolved_one(env)
    assert (handle, succeeded) == (52, True)


def test_feasibility_warning_not_triggered_when_speed_is_adequate(kodi_stubs, monkeypatch):
    env = kodi_stubs.env
    file_length = 1200 * 1024 * 1024
    _ServerScript(
        resolve_url='http://server/x/0',
        create_engine_results=[
            {'files': [{'name': 'Movie.mkv', 'length': file_length}]},
            {'downloadSpeed': 2 * 1024 * 1024, 'peers': 5},  # comfortably above the ~1 MiB/s requirement
        ],
        iter_front_attempts=[[DEFAULT_TARGET_BYTES]],
    ).install(monkeypatch, kodi_stubs.player)

    kodi_stubs.player.play(
        53, _torrent_stream(fileIdx=0), 'movie', 'tt53', item_meta={'meta': {'runtime': '20 min'}},
    )

    assert not any('feasibility warning' in msg for msg, _ in env.log_calls)
    assert env.notifications == []
    handle, succeeded, list_item = _resolved_one(env)
    assert (handle, succeeded) == (53, True)


def test_feasibility_warning_skipped_without_runtime_metadata(kodi_stubs, monkeypatch):
    """No item_meta (or no meta.runtime) at all must never warn - the
    check needs both a runtime and a file size, exactly like before this
    parameter existed for every other caller of `_prebuffer_torrent`."""
    env = kodi_stubs.env
    file_length = 1200 * 1024 * 1024
    _ServerScript(
        resolve_url='http://server/x/0',
        create_engine_results=[
            {'files': [{'name': 'Movie.mkv', 'length': file_length}]},
            {'downloadSpeed': 100_000, 'peers': 1},
        ],
        iter_front_attempts=[[DEFAULT_TARGET_BYTES]],
    ).install(monkeypatch, kodi_stubs.player)

    kodi_stubs.player.play(54, _torrent_stream(fileIdx=0), 'movie', 'tt54')  # no item_meta at all

    assert not any('feasibility warning' in msg for msg, _ in env.log_calls)
    assert env.notifications == []
    handle, succeeded, list_item = _resolved_one(env)
    assert (handle, succeeded) == (54, True)


# --- _KeepAlivePin: driven directly via _run(), never via start()/a real -
# --- thread, so these stay synchronous and deterministic ------------------


def test_keepalive_pin_run_stops_immediately_once_is_playing_reports_true(kodi_stubs):
    """`_run()` must check `is_playing()` BEFORE issuing any front read and
    return immediately once it reports True - a pin started after Kodi's
    player has already begun must not open a redundant connection."""
    player = kodi_stubs.player
    calls = []

    class _FakeServer:
        def iter_front(self, *a, **k):
            calls.append((a, k))
            yield 16384  # pragma: no cover - never reached

    pin = player._KeepAlivePin(_FakeServer(), INFO_HASH, 0, 100, lambda: True)
    pin._run()

    assert calls == []  # never reads: is_playing() was already True


def test_keepalive_pin_run_resumes_from_start_byte_and_stops_once_playing(kodi_stubs):
    """The pin's first front read must resume from `start_byte` (not 0),
    and it must stop as soon as `is_playing()` flips True mid-read -
    without ever pulling a second chunk from the generator."""
    player = kodi_stubs.player
    play_states = iter([False, True])  # not playing yet, then playing after the first chunk

    class _FakeServer:
        def __init__(self):
            self.calls = []

        def iter_front(self, info_hash, file_idx, want_bytes, chunk_size=None, timeout=None, start_byte=0):
            self.calls.append((info_hash, file_idx, want_bytes, start_byte))
            yield 16384
            yield 16384  # pragma: no cover - the pin must never reach this second chunk

    server = _FakeServer()
    pin = player._KeepAlivePin(server, INFO_HASH, 0, 500_000, lambda: next(play_states))

    pin._run()

    assert len(server.calls) == 1
    info_hash, file_idx, _want_bytes, start_byte = server.calls[0]
    assert (info_hash, file_idx, start_byte) == (INFO_HASH, 0, 500_000)


def test_keepalive_pin_stop_before_run_exits_without_any_network_call(kodi_stubs):
    """`stop()` called before `_run()` starts must make the very first
    loop check exit the pin without ever calling `iter_front()`."""
    player = kodi_stubs.player
    calls = []

    class _FakeServer:
        def iter_front(self, *a, **k):
            calls.append((a, k))
            yield 16384  # pragma: no cover - never reached

    pin = player._KeepAlivePin(_FakeServer(), INFO_HASH, 0, 0, lambda: False)
    pin.stop()
    pin._run()

    assert calls == []


def test_keepalive_pin_exits_without_network_call_once_kodi_requests_abort(kodi_stubs):
    """Kodi waits on a plugin's live threads at shutdown, so an abort
    request must end the pin at its first check - no front read at all."""
    player = kodi_stubs.player
    calls = []

    class _FakeServer:
        def iter_front(self, *a, **k):
            calls.append((a, k))
            yield 16384  # pragma: no cover - never reached

    pin = player._KeepAlivePin(_FakeServer(), INFO_HASH, 0, 0, lambda: False, is_aborted=lambda: True)
    pin._run()

    assert calls == []


def test_keepalive_pin_stops_mid_read_once_abort_flips_true(kodi_stubs):
    """An abort arriving while the pin is streaming must stop it after the
    current chunk, never pulling a second one from the generator."""
    player = kodi_stubs.player
    aborted = iter([False, True])
    pulled = []

    class _FakeServer:
        def iter_front(self, *a, **k):
            for _ in range(5):
                pulled.append(16384)
                yield 16384

    pin = player._KeepAlivePin(_FakeServer(), INFO_HASH, 0, 0, lambda: False, is_aborted=lambda: next(aborted))
    pin._run()

    assert pulled == [16384]


def test_keepalive_pin_broken_abort_probe_is_treated_as_not_aborted(kodi_stubs):
    """A raising abort probe must not crash the pin; it falls back to the
    other stop conditions (here: playback already started)."""
    player = kodi_stubs.player

    def boom():
        raise RuntimeError('monitor gone')

    pin = player._KeepAlivePin(object(), INFO_HASH, 0, 0, lambda: True, is_aborted=boom)
    pin._run()  # must not raise

    assert pin._should_stop() is False


def test_keepalive_pin_read_exception_is_logged_and_swallowed(kodi_stubs):
    """A front-read hiccup inside the pin must never raise out of
    `_run()` - it is a bonus feature, never allowed to crash playback."""
    env = kodi_stubs.env
    player = kodi_stubs.player
    pin_holder = {}

    class _FakeServer:
        def iter_front(self, *a, **k):
            raise RuntimeError('boom')
            yield  # pragma: no cover - unreachable, keeps this a generator function

    def is_playing():
        # Stop right after this check so the loop exits on its NEXT
        # condition test - once, after the read exception is handled -
        # without ever sleeping for real.
        pin_holder['pin'].stop()
        return False

    pin = player._KeepAlivePin(_FakeServer(), INFO_HASH, 0, 0, is_playing)
    pin_holder['pin'] = pin

    pin._run()  # must not raise despite the iter_front() exception

    assert any('keep-alive pin read failed' in msg for msg, _ in env.log_calls)


def test_keepalive_pin_start_spawns_a_real_background_thread(kodi_stubs):
    """`start()` really does spawn a background `threading.Thread` and
    returns immediately without blocking the caller - exercised via
    `_KeepAlivePin` directly (not `_start_keepalive_pin`, which every
    other test's `kodi_stubs` fixture stubs to a no-op precisely to keep
    the rest of this suite deterministic). `is_playing` is True from the
    start, so the thread's own body does no network I/O and finishes
    almost immediately.
    """
    player = kodi_stubs.player

    class _FakeServer:
        def iter_front(self, *a, **k):
            raise AssertionError('must not be called: is_playing() is already True')

    pin = player._KeepAlivePin(_FakeServer(), INFO_HASH, 0, 0, lambda: True)
    pin.start()

    assert pin._thread is not None
    assert pin._thread.name == 'RivuletKeepAlivePin'
    assert pin._thread.daemon is True
    pin._thread.join(timeout=5)
    assert not pin._thread.is_alive()


