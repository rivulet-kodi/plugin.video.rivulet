"""Tests for the torrent pre-buffer flow in lib.ui.player.

This is the first Kodi-layer test file in the suite (everything else under
tests/ exercises the pure lib.stremio.*/lib.store layer with no xbmc
dependency). lib.ui.player imports xbmc/xbmcgui/xbmcplugin directly (see its
module docstring: "This module owns the only xbmc* calls involved in
actually starting playback"), and lib.ui.compat - which player.py imports
ADDON/L/notify/log from - additionally imports xbmcaddon/xbmcvfs and binds
`ADDON = xbmcaddon.Addon()` at module scope. None of those five modules
exist in this environment, so the `kodi_stubs` fixture below (a thin
wrapper over tests.kodistubs.install_kodi_stubs()) injects fakes into
sys.modules and (re)imports lib.ui.compat/lib.ui.player under them,
restoring sys.modules exactly on teardown so no other test file ever sees
the stubs.

Reference: lib/ui/player.py `_prebuffer_torrent()` (the pre-buffer state
machine) and `play()` (the public entry point that drives it and surfaces
its outcome via xbmcplugin.setResolvedUrl). ServerClient is faked by
monkeypatching the `ServerClient` name player.py itself binds via
`from lib.stremio.server import ServerClient, ...` - that's the exact
symbol `_server_client()` calls to build the server object `play()` uses
throughout.

FRONT-PRIMING REWRITE (live bug fix): pre-buffer used to poll aggregate
file_stats()/buffered_bytes(), which can report megabytes "buffered" while
the file's FRONT (offset 0, where ffmpeg's container-header probe reads
from) was never actually downloaded - torrent pieces arrive out of order.
Verified live: a 1-peer torrent reported buffered=22.7MB by the aggregate
metric yet a Range read of the front returned 0 bytes, reproducing Kodi's
exact CURLE_PARTIAL_FILE(18)/"error probing input format" failure. Pre-
buffer now streams the FRONT directly via ServerClient.iter_front() and
only proceeds once _HEADER_MIN_BYTES (512 KiB) of front data is actually
obtained; a torrent that never yields usable front data now fails honestly
(string 30084, resolves False) instead of handing Kodi a doomed URL.

STAGED-DIALOG REWORK: the "Preparing stream" DialogProgress used to be
three independent, untruthful pieces - `_wait_for_server()` and
`_prebuffer_torrent()` each created/closed their own dialog (so a stream
that hit both could flash two in a row), the connect-wait dialog's
message was the FAILURE string (30031) shown WHILE still trying, and the
metadata-wait percent was a flat, meaningless 0%. `_resolve_playable_item`
now owns ONE dialog for the whole resolve, threaded through every helper,
ticking real, monotonic stage bands: connect 0-10%, resolve ~15%,
metadata 20-35%, engine warm ~38%, buffer 40-100% (the only stage with a
real bytes-obtained/target ratio). Buffering also gains a live,
best-effort speed/peers second line (same `/create` poll the metadata
stage already used) so a 2s retry pause is never silent.
"""
import contextlib

import pytest

from tests.kodistubs import install_kodi_stubs

INFO_HASH = 'deadbeefdeadbeefdeadbeefdeadbeefdeadbeef'

_RELOADED_MODULES = ('lib.ui.compat', 'lib.ui.player')


@pytest.fixture
def kodi_stubs():
    """Install fresh stubs (via tests.kodistubs.install_kodi_stubs),
    (re)importing lib.ui.compat/lib.ui.player fresh against them, and
    yield the namespace directly (`.env`, `.player`, `.compat`) - every
    test in this file configures its scenario by mutating
    `kodi_stubs.env.addon.settings[...]`/`env.cancel`/`env.monitor_abort`
    after setup rather than via fixture arguments. Restored exactly at
    teardown so no other test file ever sees the stubs.

    `localized` supplies a real `%d`/`%d` template for #30090 ("attempt
    %d of %d") - lib.ui.player formats it with `%`, and the default
    'STR30090' fallback (see tests/kodistubs/fakes.py's
    `_DEFAULT_LOCALIZED` docstring) has no placeholders to receive the
    args.
    """
    with install_kodi_stubs(reload=_RELOADED_MODULES, localized={30090: 'attempt %d of %d'}) as ctx:
        yield ctx


# --- fake ServerClient ---------------------------------------------------


class _ServerScript:
    """Configurable stand-in for lib.stremio.server.ServerClient.

    Installed by monkeypatching the `ServerClient` name in lib.ui.player -
    exactly the symbol `_server_client()` calls (`from lib.stremio.server
    import ServerClient, ...`) to build the server object `play()` uses
    throughout.

    `iter_front_attempts` scripts successive calls to `iter_front()` (one
    entry per outer pre-buffer retry): each entry is either a list of
    chunk-byte-counts to yield (mirrors a real front Range read streaming
    in pieces, ending normally once exhausted - real iter_front() never
    raises once it has yielded ANY bytes, per its own docstring) or an
    Exception instance to raise immediately with zero bytes yielded (the
    "this attempt got nothing" case). Exhausted lists repeat the last
    entry, matching this file's other *_results scripting conventions.
    """

    def __init__(self, *, available=True, available_results=None, resolve_url='http://server/x/0',
                 resolve_error=None,
                 create_engine_result=None, create_engine_results=None, create_engine_error=None,
                 iter_front_attempts=None,
                 torrent_url_result=None):
        self.available = available
        self.available_results = list(available_results or [])
        self.resolve_url = resolve_url
        self.resolve_error = resolve_error
        self.create_engine_result = {} if create_engine_result is None else create_engine_result
        self.create_engine_results = list(create_engine_results or [])
        self.create_engine_error = create_engine_error
        self.iter_front_attempts = list(iter_front_attempts or [])
        self.torrent_url_result = torrent_url_result
        self.is_available_calls = 0
        self.create_engine_calls = []
        self.iter_front_calls = []
        self.torrent_url_calls = []

    def build_class(self):
        script = self

        class FakeServerClient:
            def __init__(self, base_url):
                self.base_url = base_url

            def is_available(self):
                idx = script.is_available_calls
                script.is_available_calls += 1
                if script.available_results:
                    results = script.available_results
                    return results[idx] if idx < len(results) else results[-1]
                return script.available

            def resolve_stream(self, stream):
                if script.resolve_error is not None:
                    raise script.resolve_error
                return script.resolve_url

            def create_engine(self, info_hash, timeout=None):
                script.create_engine_calls.append(info_hash)
                if script.create_engine_error is not None:
                    raise script.create_engine_error
                results = script.create_engine_results
                if not results:
                    return script.create_engine_result
                idx = len(script.create_engine_calls) - 1
                return results[idx] if idx < len(results) else results[-1]

            def iter_front(self, info_hash, file_idx, want_bytes, chunk_size=1048576, timeout=60):
                script.iter_front_calls.append((info_hash, file_idx, want_bytes))
                idx = len(script.iter_front_calls) - 1
                attempts = script.iter_front_attempts
                if not attempts:
                    return
                attempt = attempts[idx] if idx < len(attempts) else attempts[-1]
                if isinstance(attempt, Exception):
                    raise attempt
                yield from attempt

            def torrent_url(self, info_hash, file_idx, announce=None):
                script.torrent_url_calls.append((info_hash, file_idx, tuple(announce or ())))
                if script.torrent_url_result is not None:
                    return script.torrent_url_result
                return '%s/%s/%s' % (self.base_url, info_hash, file_idx)

        return FakeServerClient

    def install(self, monkeypatch, player):
        monkeypatch.setattr(player, 'ServerClient', self.build_class())
        return self


def _torrent_stream(**overrides):
    stream = {
        'infoHash': INFO_HASH,
        'announce': ['udp://tracker.example:80'],
        'title': 'Example Movie',
    }
    stream.update(overrides)
    return stream


def _resolved_one(env):
    assert len(env.resolved) == 1
    return env.resolved[0]


# With the default _FakeAddon settings (buffer_mb=1, clamped up to the 5
# MiB floor by setting_int(minimum=5)), every test below that doesn't
# override buffer_mb targets this many bytes.
DEFAULT_TARGET_BYTES = 5 * 1024 * 1024


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
    assert [percent for percent, _ in env.dialog_updates if percent >= 40] == [70, 100]
    assert env.dialog_closed_count == 1
    handle, succeeded, list_item = _resolved_one(env)
    assert (handle, succeeded) == (2, True)
    assert list_item.path == 'http://server/x/0'


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
    metadata_updates = [percent for percent, message in env.dialog_updates if 'STR30088' in message]
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


# --- non-torrent / non-buffered streams: still get real, if brief, feedback


def test_non_torrent_stream_shows_resolve_feedback_then_closes_without_prebuffer(kodi_stubs, monkeypatch):
    """A fully direct stream (no `_SERVER_DEPENDENT_KEYS` entry at all, so
    not even the connect stage applies) still gets the shared dialog's
    Resolving stage - no longer the old zero-feedback path - and torrent
    pre-buffer still never engages.
    """
    env = kodi_stubs.env
    script = _ServerScript(resolve_url='https://example.com/a.mp4').install(monkeypatch, kodi_stubs.player)

    kodi_stubs.player.play(10, {'url': 'https://example.com/a.mp4'}, 'movie', 'tt10')

    assert script.is_available_calls == 0  # 'url' isn't a _SERVER_DEPENDENT_KEYS entry - no connect stage
    assert script.create_engine_calls == []
    assert script.iter_front_calls == []
    assert env.dialog_created == [('STR30080', '')]  # title falls back to '' - stream has no title/filename
    assert [percent for percent, _ in env.dialog_updates] == [15]  # resolve stage only
    assert env.dialog_closed_count == 1
    handle, succeeded, list_item = _resolved_one(env)
    assert (handle, succeeded) == (10, True)
    assert list_item.path == 'https://example.com/a.mp4'


def test_server_dependent_non_torrent_stream_shows_connect_and_resolve_stages(kodi_stubs, monkeypatch):
    """A server-dependent stream with no `infoHash` (e.g. a `ytId` stream)
    waits for the server (connect stage) and shows the resolve stage, but
    never engages torrent pre-buffering - `_prebuffer_torrent` is gated
    strictly on `infoHash`, unlike the connect wait above it.
    """
    env = kodi_stubs.env
    script = _ServerScript(
        available_results=[False, True],
        resolve_url='http://server/yt/xyz',
    ).install(monkeypatch, kodi_stubs.player)

    kodi_stubs.player.play(40, {'ytId': 'xyz', 'title': 'A YouTube Video'}, 'movie', 'tt40')

    assert script.is_available_calls == 2  # one miss, then up
    assert script.create_engine_calls == []
    assert script.iter_front_calls == []
    percents = [percent for percent, _ in env.dialog_updates]
    assert percents == [2, 15]  # one connect tick (min(10, 1*10//5)), then the fixed resolve tick
    assert env.dialog_closed_count == 1
    handle, succeeded, list_item = _resolved_one(env)
    assert (handle, succeeded) == (40, True)
    assert list_item.path == 'http://server/yt/xyz'


# --- UnsupportedStreamError: externalUrl/playerFrameUrl are a known -------
# --- limitation, not a fault - distinct notification + LOGINFO -----------


def test_unsupported_stream_error_notifies_30160_and_logs_loginfo_not_error(kodi_stubs, monkeypatch):
    """resolve_stream() raising UnsupportedStreamError (externalUrl/
    playerFrameUrl - see lib.stremio.server) must be handled distinctly
    from a generic broken-response failure: notify() shows the specific
    "only playable in the Stremio app" string (30160), not the generic
    "no playable stream" one (30030), and the failure is logged at
    LOGINFO (a known limitation) rather than LOGERROR (a fault)."""
    from lib.stremio.server import UnsupportedStreamError

    env = kodi_stubs.env
    _ServerScript(
        resolve_error=UnsupportedStreamError('externalUrl'),
    ).install(monkeypatch, kodi_stubs.player)

    kodi_stubs.player.play(50, {'externalUrl': 'https://example.com/watch'}, 'movie', 'tt50')

    assert [msg for _, msg, _, _ in env.notifications] == ['STR30160']
    loginfo = kodi_stubs.player.xbmc.LOGINFO
    logerror = kodi_stubs.player.xbmc.LOGERROR
    assert any(level == loginfo for _, level in env.log_calls)
    assert not any(level == logerror for _, level in env.log_calls)
    handle, succeeded, list_item = _resolved_one(env)
    assert (handle, succeeded) == (50, False)
    assert list_item.path == ''


# --- ListItem hardening: setContentLookup/setMimeType/video-info (seek-exit fix) -


def test_play_disables_content_lookup_and_sets_mimetype_for_known_extension(kodi_stubs, monkeypatch):
    """The primary seek-exits-playback fix: `setContentLookup(False)` stops
    Kodi's own content-type HEAD probe, which races/aborts against the
    torrent engine re-priming a range on (re)open and seek. A known
    container extension additionally gets an explicit `setMimeType` so
    Kodi never needs that probe in the first place.
    """
    env = kodi_stubs.env
    env.addon.settings['buffer_enable'] = False
    _ServerScript(resolve_url='http://server/x/0').install(monkeypatch, kodi_stubs.player)

    stream = _torrent_stream(fileIdx=0, behaviorHints={'filename': 'My.Movie.2020.mkv'})
    kodi_stubs.player.play(30, stream, 'movie', 'tt30')

    handle, succeeded, list_item = _resolved_one(env)
    assert (handle, succeeded) == (30, True)
    assert list_item.content_lookup is False
    assert list_item.mimetype == 'video/x-matroska'


@pytest.mark.parametrize('behavior_hints', [
    None,                              # no behaviorHints key at all
    {},                                # behaviorHints present, no filename
    {'filename': 'readme.txt'},        # filename present, unrecognized extension
])
def test_play_leaves_mimetype_unset_for_unknown_or_absent_filename(kodi_stubs, monkeypatch, behavior_hints):
    env = kodi_stubs.env
    env.addon.settings['buffer_enable'] = False
    _ServerScript(resolve_url='http://server/x/0').install(monkeypatch, kodi_stubs.player)

    overrides = {'fileIdx': 0}
    if behavior_hints is not None:
        overrides['behaviorHints'] = behavior_hints
    kodi_stubs.player.play(31, _torrent_stream(**overrides), 'movie', 'tt31')

    handle, succeeded, list_item = _resolved_one(env)
    assert (handle, succeeded) == (31, True)
    assert list_item.mimetype is None
    # Kodi's own content-type probe must stay disabled regardless of
    # whether a MIME type could be derived.
    assert list_item.content_lookup is False


def test_play_sets_title_and_mediatype_infolabels_for_movie(kodi_stubs, monkeypatch):
    env = kodi_stubs.env
    env.addon.settings['buffer_enable'] = False
    _ServerScript(resolve_url='http://server/x/0').install(monkeypatch, kodi_stubs.player)

    stream = _torrent_stream(fileIdx=0, behaviorHints={'filename': 'My.Movie.2020.mkv'})
    kodi_stubs.player.play(32, stream, 'movie', 'tt32')

    handle, succeeded, list_item = _resolved_one(env)
    assert (handle, succeeded) == (32, True)
    # This file's kodi_stubs fixture leaves System.BuildVersion unset, so
    # lib.ui.compat.set_video_info() takes the Kodi-19 legacy
    # ListItem.setInfo('video', {...}) path, recorded as legacy_info.
    assert list_item.legacy_info.get('title') == 'My.Movie.2020.mkv'
    assert list_item.legacy_info.get('mediatype') == 'movie'


def test_play_sets_episode_mediatype_for_series_stream(kodi_stubs, monkeypatch):
    env = kodi_stubs.env
    env.addon.settings['buffer_enable'] = False
    _ServerScript(resolve_url='http://server/x/0').install(monkeypatch, kodi_stubs.player)

    stream = _torrent_stream(fileIdx=0, behaviorHints={'filename': 'Show.S01E01.mkv'})
    kodi_stubs.player.play(33, stream, 'series', 'tt33:1:1')

    handle, succeeded, list_item = _resolved_one(env)
    assert (handle, succeeded) == (33, True)
    assert list_item.legacy_info.get('title') == 'Show.S01E01.mkv'
    assert list_item.legacy_info.get('mediatype') == 'episode'


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


def test_prebuffer_target_and_completion_logged_at_loginfo(kodi_stubs, monkeypatch):
    env = kodi_stubs.env
    _ServerScript(
        resolve_url='http://server/x/0', iter_front_attempts=[[600_000]],
    ).install(monkeypatch, kodi_stubs.player)

    kodi_stubs.player.play(17, _torrent_stream(fileIdx=0), 'movie', 'tt17')

    loginfo = kodi_stubs.player.xbmc.LOGINFO
    entries = [msg for msg, level in env.log_calls if level == loginfo]
    assert any('buffer_enable=True' in msg and 'fileIdx=0' in msg for msg in entries), entries
    assert any('buffer_mb=' in msg and 'target_bytes=' in msg for msg in entries), entries
    assert any('pre-buffer complete' in msg for msg in entries), entries


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
    assert not any('STR30086' in message for _, message in env.dialog_updates)  # connect stage never ticks
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

    percents = [percent for percent, _ in env.dialog_updates]
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


# --- compat.setting_bool()/setting_int(): raw-string parsing, never raises -


@pytest.mark.parametrize('raw,expected', [
    ('true', True), ('True', True), ('1', True), ('yes', True), ('on', True),
    ('false', False), ('False', False), ('0', False), ('no', False), ('off', False),
])
def test_setting_bool_parses_recognized_strings(kodi_stubs, raw, expected):
    kodi_stubs.env.addon.settings['buffer_enable'] = raw
    assert kodi_stubs.compat.setting_bool('buffer_enable', not expected) is expected


@pytest.mark.parametrize('raw', ['', 'maybe', 'null', '  '])
def test_setting_bool_falls_back_to_default_on_unreadable(kodi_stubs, raw):
    kodi_stubs.env.addon.settings['buffer_enable'] = raw
    assert kodi_stubs.compat.setting_bool('buffer_enable', True) is True
    assert kodi_stubs.compat.setting_bool('buffer_enable', False) is False


def test_setting_bool_missing_key_falls_back_to_default(kodi_stubs):
    del kodi_stubs.env.addon.settings['buffer_enable']
    assert kodi_stubs.compat.setting_bool('buffer_enable', True) is True
    assert kodi_stubs.compat.setting_bool('buffer_enable', False) is False


def test_setting_bool_never_raises_when_getsetting_raises(kodi_stubs, monkeypatch):
    def boom(key):
        raise RuntimeError('kodi settings db locked')

    monkeypatch.setattr(kodi_stubs.env.addon, 'getSetting', boom)
    assert kodi_stubs.compat.setting_bool('buffer_enable', True) is True


def test_setting_int_parses_and_falls_back_to_default(kodi_stubs):
    kodi_stubs.env.addon.settings['buffer_mb'] = '42'
    assert kodi_stubs.compat.setting_int('buffer_mb', 20) == 42
    kodi_stubs.env.addon.settings['buffer_mb'] = ''
    assert kodi_stubs.compat.setting_int('buffer_mb', 20) == 20
    kodi_stubs.env.addon.settings['buffer_mb'] = 'not-a-number'
    assert kodi_stubs.compat.setting_int('buffer_mb', 20) == 20


def test_setting_int_clamps_to_minimum(kodi_stubs):
    kodi_stubs.env.addon.settings['buffer_mb'] = '1'
    assert kodi_stubs.compat.setting_int('buffer_mb', 20, minimum=5) == 5
    kodi_stubs.env.addon.settings['buffer_mb'] = '10'
    assert kodi_stubs.compat.setting_int('buffer_mb', 20, minimum=5) == 10


def test_setting_int_never_raises_when_getsetting_raises(kodi_stubs, monkeypatch):
    def boom(key):
        raise RuntimeError('kodi settings db locked')

    monkeypatch.setattr(kodi_stubs.env.addon, 'getSetting', boom)
    assert kodi_stubs.compat.setting_int('buffer_mb', 20) == 20


# --- play_direct(): the custom-window direct-play path (lib.ui.streamswindow),
# --- sharing _resolve_playable_item() with play() above - only the final
# --- disposition differs (xbmc.Player().play() vs xbmcplugin.setResolvedUrl())


def test_play_direct_successful_resolution_starts_player_and_returns_true(kodi_stubs, monkeypatch):
    env = kodi_stubs.env
    _ServerScript(resolve_url='https://example.com/a.mp4').install(monkeypatch, kodi_stubs.player)

    result = kodi_stubs.player.play_direct({'url': 'https://example.com/a.mp4'}, 'movie', 'tt50')

    assert result is True
    assert len(env.player_play_calls) == 1
    url, list_item = env.player_play_calls[0]
    assert url == 'https://example.com/a.mp4'
    assert list_item.path == 'https://example.com/a.mp4'
    assert env.resolved == []  # play_direct never touches xbmcplugin.setResolvedUrl()


def test_play_direct_failed_resolution_returns_false_without_starting_player(kodi_stubs, monkeypatch):
    env = kodi_stubs.env
    # An empty resolve_stream() result is a resolution failure ("no url"),
    # the same honest-failure path play()'s own tests exercise.
    _ServerScript(resolve_url=None).install(monkeypatch, kodi_stubs.player)

    result = kodi_stubs.player.play_direct({'url': 'https://example.com/a.mp4'}, 'movie', 'tt51')

    assert result is False
    assert env.player_play_calls == []
    assert env.notifications == [('Rivulet', 'STR30030', 'info', 4000)]


# --- item_meta: OSD title/art/info forwarding (Defect A: "Not available" +
# --- placeholder art), and the improved torrent filename derivation that
# --- feeds both the title fallback and setMimeType -------------------------


def test_resolve_with_no_item_meta_uses_sanitized_stream_title_as_label_and_info_title(kodi_stubs, monkeypatch):
    """Defect A repro with no `item_meta` at all: a stream with nothing but
    a `title` - the common shape for a torrent with no
    `behaviorHints.filename` - must still reach Kodi's OSD with a real,
    sanitized title instead of the empty label/title that caused the
    live "Not available" bug. Addon-supplied titles routinely bake in
    CR/LF (see `lib.ui.streamswindow.onInit`'s identical sanitization).
    """
    env = kodi_stubs.env
    _ServerScript(resolve_url='https://example.com/a.mp4').install(monkeypatch, kodi_stubs.player)

    stream = {'url': 'https://example.com/a.mp4', 'title': 'Some\r\nTitle'}
    kodi_stubs.player.play(70, stream, 'movie', 'tt70')

    handle, succeeded, list_item = _resolved_one(env)
    assert (handle, succeeded) == (70, True)
    assert list_item.getLabel() == 'Some  Title'
    assert list_item.legacy_info.get('title') == 'Some  Title'


def test_resolve_with_full_item_meta_populates_label_art_and_info(kodi_stubs, monkeypatch):
    """The full `item_meta` contract: label/title come from
    `item_meta['label']` (not the stream's own `title`), art carries
    poster+thumb+fanart, and info carries
    plot/year/rating/genre/duration/mediatype/tvshowtitle - the actual
    fix for Defect A: `lib.ui.streamswindow` already knows all of this
    and now forwards it instead of letting the OSD show "Not available"
    and the default camera placeholder.
    """
    env = kodi_stubs.env
    env.addon.settings['buffer_enable'] = False
    _ServerScript(resolve_url='http://server/x/0').install(monkeypatch, kodi_stubs.player)

    item_meta = {
        'label': 'The Mandalorian - S01E02 Chapter 2',
        'art': {'poster': 'http://img/poster.jpg', 'fanart': 'http://img/fanart.jpg'},
        'meta': {
            'name': 'The Mandalorian',
            'description': 'A lone gunfighter...',
            'releaseInfo': '2019-2023',
            'imdbRating': '8.7',
            'genres': ['Action', 'Sci-Fi'],
            'runtime': '40 min',
        },
    }
    stream = _torrent_stream(fileIdx=0, title='ignored raw title')

    kodi_stubs.player.play(71, stream, 'series', 'tt71:1:2', item_meta=item_meta)

    handle, succeeded, list_item = _resolved_one(env)
    assert (handle, succeeded) == (71, True)
    assert list_item.getLabel() == 'The Mandalorian - S01E02 Chapter 2'
    assert list_item.art.get('poster') == 'http://img/poster.jpg'
    assert list_item.art.get('thumb') == 'http://img/poster.jpg'
    assert list_item.art.get('fanart') == 'http://img/fanart.jpg'
    info = list_item.legacy_info
    assert info.get('title') == 'The Mandalorian - S01E02 Chapter 2'
    assert info.get('mediatype') == 'episode'
    assert info.get('tvshowtitle') == 'The Mandalorian'
    assert info.get('plot') == 'A lone gunfighter...'
    assert info.get('year') == 2019
    assert info.get('rating') == 8.7
    assert info.get('genre') == ['Action', 'Sci-Fi']
    assert info.get('duration') == 40 * 60

def test_plot_falls_back_to_the_streams_own_description_when_meta_has_none(kodi_stubs, monkeypatch):
    """Kodi's OSD info panel renders an empty plot as the literal "Not
    available" (Estuary's DialogSeekBar.xml binds
    `$INFO[VideoPlayer.Plot]` with `fallback="10005"`), and a catalog
    preview routinely carries no `description` at all - so a picked
    stream with a title/poster but no plot still looked broken on a real
    device. The stream's own parsed description (release name, size,
    seeders, provider) is the fallback.
    """
    env = kodi_stubs.env
    env.addon.settings['buffer_enable'] = False
    _ServerScript(resolve_url='http://server/x/0').install(monkeypatch, kodi_stubs.player)

    item_meta = {'label': 'Dune', 'meta': {'name': 'Dune'}}  # no description
    stream = _torrent_stream(fileIdx=0, title='Dune.2021.2160p.WEB-DL\nSeeds: 42')

    kodi_stubs.player.play(72, stream, 'movie', 'tt72', item_meta=item_meta)

    _handle, _succeeded, list_item = _resolved_one(env)
    plot = list_item.legacy_info.get('plot')
    assert plot  # never empty -> the OSD never shows "Not available"
    assert 'Dune.2021.2160p.WEB-DL' in plot
    assert '42 seeders' in plot


def test_explicit_item_meta_plot_wins_over_description_and_stream_fallback(kodi_stubs, monkeypatch):
    """A caller that knows the episode's own overview (DetailWindow) can
    pass it directly; it outranks both the show-level `description` and
    the stream-derived fallback."""
    env = kodi_stubs.env
    env.addon.settings['buffer_enable'] = False
    _ServerScript(resolve_url='http://server/x/0').install(monkeypatch, kodi_stubs.player)

    item_meta = {
        'label': 'Chapter 2',
        'plot': 'The Mandalorian returns the Child.',
        'meta': {'description': 'show-level blurb', 'tagline': 'This is the Way'},
    }

    kodi_stubs.player.play(73, _torrent_stream(fileIdx=0), 'series', 'tt73:1:2', item_meta=item_meta)

    _handle, _succeeded, list_item = _resolved_one(env)
    info = list_item.legacy_info
    assert info.get('plot') == 'The Mandalorian returns the Child.'
    assert info.get('plotoutline') == 'This is the Way'



def test_torrent_resolved_filename_from_create_stats_sets_correct_mimetype(kodi_stubs, monkeypatch):
    """Defect A/mime fix: a torrent's resolved playback URL
    (`http://host/<infoHash>/<fileIdx>`) carries no file extension of
    its own, so `_mime_for` could never derive a MIME type from it
    before. `_extract_file_name` recovers the real filename from the
    `/create` stats dict the metadata-wait loop already fetched (no
    extra HTTP round-trip), letting a torrent stream get a correct
    `setMimeType` (and a real title) exactly like a
    `behaviorHints.filename` stream always could.
    """
    env = kodi_stubs.env
    stream = _torrent_stream()  # fileIdx missing -> UNKNOWN_FILE_IDX
    files = [{'name': 'Some.Movie.2020.mkv', 'length': 500}]
    _ServerScript(
        resolve_url='http://server/x/-1',
        create_engine_result={'files': files},
        iter_front_attempts=[[600_000]],
        torrent_url_result='http://server/x/0',
    ).install(monkeypatch, kodi_stubs.player)

    kodi_stubs.player.play(72, stream, 'movie', 'tt72')

    handle, succeeded, list_item = _resolved_one(env)
    assert (handle, succeeded) == (72, True)
    assert list_item.mimetype == 'video/x-matroska'
    assert list_item.getLabel() == 'Some.Movie.2020.mkv'


def test_apply_item_metadata_skips_malformed_meta_fields_without_poisoning_others(kodi_stubs, monkeypatch):
    """Malformed Stremio meta values must be tolerated field-by-field:
    an unparseable `imdbRating`/`runtime` is skipped, `releaseInfo`'s
    open-ended '2019-' shape is still parsed to a year, and none of that
    prevents the OTHER metadata (label, plot, genre) from coming
    through intact.
    """
    env = kodi_stubs.env
    env.addon.settings['buffer_enable'] = False
    _ServerScript(resolve_url='http://server/x/0').install(monkeypatch, kodi_stubs.player)

    item_meta = {
        'label': 'Dune',
        'meta': {
            'description': 'A desert planet',
            'imdbRating': 'n/a',
            'runtime': '?',
            'releaseInfo': '2019-',
            'genres': ['Sci-Fi'],
        },
    }
    kodi_stubs.player.play(76, _torrent_stream(fileIdx=0), 'movie', 'tt76', item_meta=item_meta)

    handle, succeeded, list_item = _resolved_one(env)
    assert (handle, succeeded) == (76, True)
    assert list_item.getLabel() == 'Dune'
    info = list_item.legacy_info
    assert info.get('plot') == 'A desert planet'
    assert info.get('year') == 2019  # tolerates the open-ended '2019-' shape
    assert 'rating' not in info  # 'n/a' is unparseable -> skipped, not raised
    assert 'duration' not in info  # '?' is unparseable -> skipped, not raised
    assert info.get('genre') == ['Sci-Fi']  # other fields unaffected


# --- play_direct(on_ready=...): fires immediately before xbmc.Player().play(),
# --- only on successful resolution, and never blocks playback on its own -


def test_play_direct_on_ready_invoked_once_immediately_before_player_play(kodi_stubs, monkeypatch):
    env = kodi_stubs.env
    _ServerScript(resolve_url='https://example.com/a.mp4').install(monkeypatch, kodi_stubs.player)

    calls = []

    def on_ready():
        calls.append(len(env.player_play_calls))  # must run BEFORE Player().play() is recorded

    result = kodi_stubs.player.play_direct(
        {'url': 'https://example.com/a.mp4'}, 'movie', 'tt73', on_ready=on_ready,
    )

    assert result is True
    assert calls == [0]  # exactly one call, and it ran before any play() was recorded
    assert len(env.player_play_calls) == 1


def test_play_direct_on_ready_not_called_when_resolution_fails(kodi_stubs, monkeypatch):
    env = kodi_stubs.env
    _ServerScript(resolve_url=None).install(monkeypatch, kodi_stubs.player)
    calls = []

    result = kodi_stubs.player.play_direct(
        {'url': 'https://example.com/a.mp4'}, 'movie', 'tt74', on_ready=lambda: calls.append(1),
    )

    assert result is False
    assert calls == []
    assert env.player_play_calls == []


def test_play_direct_on_ready_exception_is_logged_and_swallowed_but_playback_still_starts(kodi_stubs, monkeypatch):
    """A broken `on_ready` hook must never prevent playback that has
    already been resolved - it is logged at LOGWARNING and swallowed,
    and `xbmc.Player().play()` still runs.
    """
    env = kodi_stubs.env
    _ServerScript(resolve_url='https://example.com/a.mp4').install(monkeypatch, kodi_stubs.player)

    def boom():
        raise RuntimeError('hook boom')

    result = kodi_stubs.player.play_direct(
        {'url': 'https://example.com/a.mp4'}, 'movie', 'tt75', on_ready=boom,
    )

    assert result is True
    assert len(env.player_play_calls) == 1
    assert any(level == kodi_stubs.player.xbmc.LOGWARNING for _, level in env.log_calls)


@contextlib.contextmanager
def _kodi_stubs_with_yesno(yesno_answers):
    """Like the `kodi_stubs` fixture above, but with scripted
    `xbmcgui.Dialog().yesno()` answers queued up front -- that fixture
    has no such parameter (no other test in this file needs one)."""
    with install_kodi_stubs(
        reload=_RELOADED_MODULES, localized={30090: 'attempt %d of %d'},
        dialog_yesno=yesno_answers,
    ) as ctx:
        yield ctx


class _FakeProgressStore:
    """Fake `lib.store.Store` surface `lib.ui.player`'s resume/now-
    playing code needs (`get_progress`/`set_now_playing`/
    `set_resume_offset_ms`) -- injected via `monkeypatch.setattr(
    kodi_stubs.player, 'Store', ...)` so these tests never touch a real
    filesystem or `lib.store.Store` directly."""

    def __init__(self, progress=None):
        self._progress = progress
        self.now_playing = None
        self.resume_offset_ms = 'UNSET'  # distinguishes "never called" from "cleared to None"
        self.get_progress_calls = []

    def get_progress(self, content_type, content_id, video_id=None):
        self.get_progress_calls.append((content_type, content_id, video_id))
        return self._progress

    def set_now_playing(self, context):
        self.now_playing = context

    def set_resume_offset_ms(self, offset_ms):
        self.resume_offset_ms = offset_ms


def _install_progress_store(monkeypatch, player_module, store):
    monkeypatch.setattr(player_module, 'Store', lambda *a, **k: store)


# --- "now playing" context recording (LibrarySync) --------------------------


def test_now_playing_context_recorded_on_successful_resolve(kodi_stubs, monkeypatch):
    store = _FakeProgressStore()
    _install_progress_store(monkeypatch, kodi_stubs.player, store)
    _ServerScript(resolve_url='https://example.com/a.mp4').install(monkeypatch, kodi_stubs.player)

    item_meta = {'label': 'Some Title', 'art': {'poster': 'https://x/poster.jpg'},
                 'meta': {'name': 'Meta Name', 'poster': 'https://x/meta-poster.jpg'}}
    kodi_stubs.player.play_direct(
        {'url': 'https://example.com/a.mp4'}, 'series', 'tt1', item_meta=item_meta, video_id='tt1:1:2',
    )

    assert store.now_playing['type'] == 'series'
    assert store.now_playing['id'] == 'tt1'
    assert store.now_playing['video_id'] == 'tt1:1:2'
    assert store.now_playing['name'] == 'Some Title'  # item_meta['label'] wins over meta.name
    assert store.now_playing['poster'] == 'https://x/poster.jpg'  # item_meta['art'] wins over meta.poster
    assert store.now_playing['started_at'].endswith('Z')


def test_now_playing_falls_back_to_meta_name_and_poster_with_no_label_or_art(kodi_stubs, monkeypatch):
    store = _FakeProgressStore()
    _install_progress_store(monkeypatch, kodi_stubs.player, store)
    _ServerScript(resolve_url='https://example.com/a.mp4').install(monkeypatch, kodi_stubs.player)

    item_meta = {'meta': {'name': 'Meta Name', 'poster': 'https://x/meta-poster.jpg'}}
    kodi_stubs.player.play_direct(
        {'url': 'https://example.com/a.mp4'}, 'movie', 'tt2', item_meta=item_meta,
    )

    assert store.now_playing['name'] == 'Meta Name'
    assert store.now_playing['poster'] == 'https://x/meta-poster.jpg'
    assert store.now_playing['video_id'] is None  # no video_id passed -> None, unchanged


def test_now_playing_defaults_empty_name_and_none_poster_with_no_item_meta(kodi_stubs, monkeypatch):
    store = _FakeProgressStore()
    _install_progress_store(monkeypatch, kodi_stubs.player, store)
    _ServerScript(resolve_url='https://example.com/a.mp4').install(monkeypatch, kodi_stubs.player)

    kodi_stubs.player.play_direct({'url': 'https://example.com/a.mp4'}, 'movie', 'tt3')

    assert store.now_playing['name'] == ''
    assert store.now_playing['poster'] is None


def test_resolve_failure_does_not_record_now_playing(kodi_stubs, monkeypatch):
    store = _FakeProgressStore()
    _install_progress_store(monkeypatch, kodi_stubs.player, store)
    _ServerScript(resolve_url=None).install(monkeypatch, kodi_stubs.player)

    result = kodi_stubs.player.play_direct({'url': 'https://example.com/a.mp4'}, 'movie', 'tt4')

    assert result is False
    assert store.now_playing is None


def test_play_classical_path_also_records_now_playing(kodi_stubs, monkeypatch):
    """The classical GetDirectory `play()` path shares
    `_resolve_playable_item()` with `play_direct()` -- both the
    setResolvedUrl path and the custom-window direct path must resume."""
    env = kodi_stubs.env
    store = _FakeProgressStore()
    _install_progress_store(monkeypatch, kodi_stubs.player, store)
    _ServerScript(resolve_url='https://example.com/a.mp4').install(monkeypatch, kodi_stubs.player)

    kodi_stubs.player.play(50, {'url': 'https://example.com/a.mp4'}, 'movie', 'tt5')

    handle, succeeded, _list_item = _resolved_one(env)
    assert (handle, succeeded) == (50, True)
    assert store.now_playing['id'] == 'tt5'


def test_video_id_threaded_to_progress_lookup_and_now_playing_context(kodi_stubs, monkeypatch):
    store = _FakeProgressStore()
    _install_progress_store(monkeypatch, kodi_stubs.player, store)
    _ServerScript(resolve_url='https://example.com/a.mp4').install(monkeypatch, kodi_stubs.player)

    kodi_stubs.player.play_direct(
        {'url': 'https://example.com/a.mp4'}, 'series', 'tt6', video_id='tt6:1:3',
    )

    assert store.get_progress_calls == [('series', 'tt6', 'tt6:1:3')]
    assert store.now_playing['video_id'] == 'tt6:1:3'


# --- resume prompt: 1%-95% band, resume_ask setting, yes/no -----------------


def test_resume_prompt_skipped_below_one_percent(kodi_stubs, monkeypatch):
    store = _FakeProgressStore(progress={'position_ms': 500, 'duration_ms': 1000000})
    _install_progress_store(monkeypatch, kodi_stubs.player, store)
    _ServerScript(resolve_url='https://example.com/a.mp4').install(monkeypatch, kodi_stubs.player)

    kodi_stubs.player.play_direct({'url': 'https://example.com/a.mp4'}, 'movie', 'tt7')

    assert kodi_stubs.env.dialog_yesno_prompts == []
    assert store.resume_offset_ms is None


def test_resume_prompt_skipped_above_ninety_five_percent(kodi_stubs, monkeypatch):
    store = _FakeProgressStore(progress={'position_ms': 96000, 'duration_ms': 100000})
    _install_progress_store(monkeypatch, kodi_stubs.player, store)
    _ServerScript(resolve_url='https://example.com/a.mp4').install(monkeypatch, kodi_stubs.player)

    kodi_stubs.player.play_direct({'url': 'https://example.com/a.mp4'}, 'movie', 'tt8')

    assert kodi_stubs.env.dialog_yesno_prompts == []
    assert store.resume_offset_ms is None


def test_resume_prompt_shown_between_one_and_ninety_five_percent_yes_queues_offset(monkeypatch):
    with _kodi_stubs_with_yesno([True]) as ctx:
        store = _FakeProgressStore(progress={'position_ms': 50000, 'duration_ms': 100000})
        _install_progress_store(monkeypatch, ctx.player, store)
        _ServerScript(resolve_url='https://example.com/a.mp4').install(monkeypatch, ctx.player)

        ctx.player.play_direct({'url': 'https://example.com/a.mp4'}, 'movie', 'tt9')

        assert len(ctx.env.dialog_yesno_prompts) == 1
        assert store.resume_offset_ms == 50000


def test_resume_prompt_declined_does_not_queue_offset(monkeypatch):
    with _kodi_stubs_with_yesno([False]) as ctx:
        store = _FakeProgressStore(progress={'position_ms': 50000, 'duration_ms': 100000})
        _install_progress_store(monkeypatch, ctx.player, store)
        _ServerScript(resolve_url='https://example.com/a.mp4').install(monkeypatch, ctx.player)

        ctx.player.play_direct({'url': 'https://example.com/a.mp4'}, 'movie', 'tt10')

        assert len(ctx.env.dialog_yesno_prompts) == 1
        assert store.resume_offset_ms is None


def test_resume_ask_setting_off_skips_prompt_entirely(kodi_stubs, monkeypatch):
    env = kodi_stubs.env
    env.addon.settings['resume_ask'] = False
    store = _FakeProgressStore(progress={'position_ms': 50000, 'duration_ms': 100000})
    _install_progress_store(monkeypatch, kodi_stubs.player, store)
    _ServerScript(resolve_url='https://example.com/a.mp4').install(monkeypatch, kodi_stubs.player)

    kodi_stubs.player.play_direct({'url': 'https://example.com/a.mp4'}, 'movie', 'tt11')

    assert env.dialog_yesno_prompts == []
    assert store.resume_offset_ms is None


def test_no_cached_progress_skips_resume_prompt_and_still_records_now_playing(kodi_stubs, monkeypatch):
    store = _FakeProgressStore(progress=None)
    _install_progress_store(monkeypatch, kodi_stubs.player, store)
    _ServerScript(resolve_url='https://example.com/a.mp4').install(monkeypatch, kodi_stubs.player)

    kodi_stubs.player.play_direct({'url': 'https://example.com/a.mp4'}, 'movie', 'tt12')

    assert kodi_stubs.env.dialog_yesno_prompts == []
    assert store.now_playing['id'] == 'tt12'


# --- degrade-gracefully guarantees: never block playback --------------------


def test_logged_out_user_gets_local_resume_with_zero_extra_calls_and_no_swallowed_bug(monkeypatch):
    """A logged-out user must still get local progress/resume: the fake
    store below deliberately has NO `get_auth()` method at all -- if
    `lib.ui.player`'s resume/now-playing code ever called it, this
    would raise AttributeError, which the broad `except Exception`
    guards would silently swallow and log, masking a real bug. Asserts
    no such warning appears."""
    with _kodi_stubs_with_yesno([True]) as ctx:
        store = _FakeProgressStore(progress={'position_ms': 50000, 'duration_ms': 100000})
        _install_progress_store(monkeypatch, ctx.player, store)
        _ServerScript(resolve_url='https://example.com/a.mp4').install(monkeypatch, ctx.player)

        ctx.player.play_direct({'url': 'https://example.com/a.mp4'}, 'movie', 'tt13')

        assert store.resume_offset_ms == 50000
        assert not any('failed' in msg for msg, _level in ctx.env.log_calls)


def test_store_construction_failure_is_logged_and_never_blocks_playback(kodi_stubs, monkeypatch):
    def _raise(*_a, **_k):
        raise OSError('disk full')

    monkeypatch.setattr(kodi_stubs.player, 'Store', _raise)
    _ServerScript(resolve_url='https://example.com/a.mp4').install(monkeypatch, kodi_stubs.player)

    result = kodi_stubs.player.play_direct({'url': 'https://example.com/a.mp4'}, 'movie', 'tt14')

    assert result is True  # playback still starts
    assert len(kodi_stubs.env.player_play_calls) == 1
    assert any(
        'recording now-playing context failed' in msg and level == kodi_stubs.player.xbmc.LOGWARNING
        for msg, level in kodi_stubs.env.log_calls
    )


def test_get_progress_exception_is_logged_and_resume_skipped_without_blocking_playback(kodi_stubs, monkeypatch):
    class _BrokenStore(_FakeProgressStore):
        def get_progress(self, content_type, content_id, video_id=None):
            raise RuntimeError('corrupt cache')

    store = _BrokenStore()
    _install_progress_store(monkeypatch, kodi_stubs.player, store)
    _ServerScript(resolve_url='https://example.com/a.mp4').install(monkeypatch, kodi_stubs.player)

    result = kodi_stubs.player.play_direct({'url': 'https://example.com/a.mp4'}, 'movie', 'tt15')

    assert result is True
    assert kodi_stubs.env.dialog_yesno_prompts == []
    assert store.now_playing['id'] == 'tt15'  # now-playing recording still succeeds
