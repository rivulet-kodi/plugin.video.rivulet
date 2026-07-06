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
"""
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
    """
    with install_kodi_stubs(reload=_RELOADED_MODULES) as ctx:
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
                 create_engine_result=None, create_engine_results=None, create_engine_error=None,
                 iter_front_attempts=None,
                 torrent_url_result=None):
        self.available = available
        self.available_results = list(available_results or [])
        self.resolve_url = resolve_url
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
    assert env.dialog_created == []
    assert env.dialog_closed_count == 0
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

    assert script.create_engine_calls == [INFO_HASH]
    assert script.iter_front_calls == [(INFO_HASH, 0, DEFAULT_TARGET_BYTES)]
    assert env.dialog_created == [('STR30080', 'Example Movie')]
    # percent = min(100, got * 100 // target); pinned by the exact byte
    # counts above so a flipped clamp/off-by-one reddens this.
    assert [percent for percent, _ in env.dialog_updates] == [50, 100]
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

    assert script.create_engine_calls == [INFO_HASH]
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

    assert script.create_engine_calls == [INFO_HASH]  # warm was attempted
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

    assert script.create_engine_calls == [INFO_HASH]
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

    assert script.create_engine_calls == [INFO_HASH]  # resolved on the very first /create poll
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

    assert len(script.create_engine_calls) == 3
    assert env.monitor_abort_calls == 2  # one wait after each of the first two unresolved polls
    # metadata-wait phase shows an indeterminate 0% while no file is picked yet
    assert [percent for percent, _ in env.dialog_updates[:2]] == [0, 0]
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


# --- non-torrent streams never engage pre-buffer ---------------------------


def test_non_torrent_stream_never_engages_prebuffer(kodi_stubs, monkeypatch):
    env = kodi_stubs.env
    script = _ServerScript(resolve_url='https://example.com/a.mp4').install(monkeypatch, kodi_stubs.player)

    kodi_stubs.player.play(10, {'url': 'https://example.com/a.mp4'}, 'movie', 'tt10')

    assert script.is_available_calls == 0  # 'url' isn't a _SERVER_DEPENDENT_KEYS entry
    assert script.create_engine_calls == []
    assert script.iter_front_calls == []
    assert env.dialog_created == []
    assert env.dialog_closed_count == 0
    handle, succeeded, list_item = _resolved_one(env)
    assert (handle, succeeded) == (10, True)
    assert list_item.path == 'https://example.com/a.mp4'


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

    assert script.create_engine_calls == [INFO_HASH]  # engine WAS warmed: pre-buffer ran
    assert script.iter_front_calls == [(INFO_HASH, 0, DEFAULT_TARGET_BYTES)]  # AND streamed - not skipped
    assert env.dialog_created == [('STR30080', 'Example Movie')]
    assert env.dialog_closed_count == 1
    handle, succeeded, list_item = _resolved_one(env)
    assert (handle, succeeded) == (14, True)
    assert list_item.path == 'http://server/x/0'


def test_buffer_enable_raw_false_string_still_skips(kodi_stubs, monkeypatch):
    """An explicit user "off" (settings.xml -> raw getSetting() == 'false')
    must still disable pre-buffering - only a missing/unreadable value
    defaults ON, never an explicit off.
    """
    env = kodi_stubs.env
    env.addon.settings['buffer_enable'] = 'false'
    script = _ServerScript(resolve_url='http://server/x/0').install(monkeypatch, kodi_stubs.player)

    kodi_stubs.player.play(15, _torrent_stream(fileIdx=0), 'movie', 'tt15')

    assert script.create_engine_calls == []
    assert script.iter_front_calls == []
    assert env.dialog_created == []
    assert env.dialog_closed_count == 0
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
