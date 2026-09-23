"""Tests for URL resolution / `_resolve_playable_item()` / item metadata in
lib.ui.player.

Split from the original tests/test_player_buffer.py: this file keeps that
name (and the shared fixtures below, imported by its two siblings) as the
largest/original group. `_prebuffer_torrent()` polling/cancel/infoHash now
live in test_player_prebuffer.py; resume/now-playing/playback-callback
behavior lives in test_player_callbacks.py. This file covers `play()`/
`play_direct()`'s URL resolution (including non-torrent/server-dependent
streams and UnsupportedStreamError handling), ListItem metadata (title/
mimetype/mediatype, `item_meta` label/art/info/cast forwarding), subtitle
attachment, and the pure lib.ui.playbackmeta helpers.

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

Reference: lib/ui/player.py `_resolve_playable_item()` (shared by `play()`
and `play_direct()`). ServerClient is faked by monkeypatching the
`ServerClient` name player.py itself binds via `from lib.stremio.server
import ServerClient, ...` - that's the exact symbol `_server_client()`
calls to build the server object `play()` uses throughout.

STAGED-DIALOG REWORK: the "Preparing stream" DialogProgress used to be
three independent, untruthful pieces - `_wait_for_server()` and
`_prebuffer_torrent()` each created/closed their own dialog (so a stream
that hit both could flash two in a row), the connect-wait dialog's
message was the FAILURE string (30031) shown WHILE still trying, and the
metadata-wait percent was a flat, meaningless 0%. `_resolve_playable_item`
now owns ONE dialog for the whole resolve, threaded through every helper,
ticking real, monotonic stage bands: connect 0-10%, resolve ~15%,
metadata 20-35%, engine warm ~38%, buffer 40-100% (the only stage with a
real bytes-obtained/target ratio; see test_player_prebuffer.py). Buffering
also gains a live, best-effort speed/peers second line (same `/create`
poll the metadata stage already used) so a 2s retry pause is never silent.
"""
import contextlib

import pytest

from lib.ui import playbackmeta
from tests.kodistubs import install_kodi_stubs

INFO_HASH = 'deadbeefdeadbeefdeadbeefdeadbeefdeadbeef'

_RELOADED_MODULES = ('lib.ui.compat', 'lib.ui.player')


class _FakeProgressDialog:
    """Local stand-in for lib.ui.dialogs.RivuletProgress: mirrors the
    create/update/iscanceled/close surface lib.ui.player drives, recording
    into `env` the same way the old fake xbmcgui.DialogProgress did
    (dialog_created/dialog_updates/dialog_closed_count/
    dialog_iscanceled_calls), widened to the real four-argument
    update(percent, message, attempt, stats) shape. RivuletProgress's own
    window/panel wiring is exhaustively covered by tests/test_dialogs.py -
    this only needs to prove what lib.ui.player PASSES it.
    """

    def __init__(self, env):
        self._env = env

    def create(self, heading, message=''):
        self._env.dialog_created.append((heading, message))

    def update(self, percent, message='', attempt='', stats=''):
        self._env.dialog_updates.append((percent, message, attempt, stats))

    def iscanceled(self):
        self._env.dialog_iscanceled_calls += 1
        cancel = self._env.cancel
        return bool(cancel()) if callable(cancel) else bool(cancel)

    def close(self):
        self._env.dialog_closed_count += 1


def _make_fake_confirm(env, answers):
    """Local stand-in for lib.ui.dialogs.confirm(): records into
    `env.dialog_yesno_prompts` exactly like the old fake
    `xbmcgui.Dialog().yesno()` did, so existing count/emptiness
    assertions keep working unchanged."""
    queued = list(answers or [])

    def confirm(heading, body, yeslabel, nolabel):
        env.dialog_yesno_prompts.append((heading, body))
        return queued.pop(0) if queued else False

    return confirm


def _wire_player_dialogs(ctx, yesno_answers=None):
    """lib.ui.player imports RivuletProgress/confirm from lib.ui.dialogs at
    module scope (`from lib.ui.dialogs import RivuletProgress, confirm`);
    point those two names at the local fakes above instead of reloading
    lib.ui.uicommon/lib.ui.dialogs and driving a real WindowXMLDialog
    event loop from this file.

    Also stubs `_start_keepalive_pin` to a no-op: production spawns a real
    background thread there once pre-buffer decides to start (see
    `_KeepAlivePin` in lib.ui.player), which would otherwise run
    concurrently with - and non-deterministically race - the exact-call
    assertions every pre-buffer test in this suite makes against the fake
    ServerClient's `iter_front_calls`. Tests that want to exercise the pin
    itself re-point this name back at the real
    `lib.ui.player._start_keepalive_pin` (or drive `_KeepAlivePin`
    directly) instead of relying on this fixture's default.
    """
    ctx.env.dialog_iscanceled_calls = 0
    ctx.player.RivuletProgress = lambda: _FakeProgressDialog(ctx.env)
    ctx.player.confirm = _make_fake_confirm(ctx.env, yesno_answers)
    ctx.player._start_keepalive_pin = lambda *a, **k: None
    return ctx


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
        yield _wire_player_dialogs(ctx)


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
        self.iter_front_start_bytes = []
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

            def iter_front(self, info_hash, file_idx, want_bytes, chunk_size=1048576, timeout=60, start_byte=0):
                script.iter_front_calls.append((info_hash, file_idx, want_bytes))
                script.iter_front_start_bytes.append(start_byte)
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


class _NullMonitor:
    """Minimal xbmc.Monitor stand-in for calling _prebuffer_torrent()
    directly, below the full play()/kodi_stubs stack: waitForAbort()
    never fires, so a single scripted attempt runs start to finish."""

    def waitForAbort(self, timeout=None):
        return False


# With the default _FakeAddon settings (buffer_mb=1, clamped up to the 5
# MiB floor by setting_int(minimum=5)), every test below that doesn't
# override buffer_mb targets this many bytes.
DEFAULT_TARGET_BYTES = 5 * 1024 * 1024




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
    assert [percent for percent, _, _, _ in env.dialog_updates] == [15]  # resolve stage only
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
    percents = [percent for percent, _, _, _ in env.dialog_updates]
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



def test_unsupported_stream_error_play_direct_no_player_play_sink(kodi_stubs, monkeypatch):
    """The same UnsupportedStreamError rejection (e.g. a direct `url`
    field rejected by lib.stremio.server._DIRECT_URL_SCHEMES - see
    lib.stremio.server.resolve_stream) must short-circuit play_direct()
    (the custom-window path) BEFORE any ListItem/metadata construction
    and before xbmc.Player().play() is ever called - only a failure
    notification, no player sink."""
    from lib.stremio.server import UnsupportedStreamError

    env = kodi_stubs.env
    _ServerScript(
        resolve_error=UnsupportedStreamError("Stream url scheme 'plugin' is not an allowed direct-playback scheme"),
    ).install(monkeypatch, kodi_stubs.player)

    result = kodi_stubs.player.play_direct({'url': 'plugin://evil'}, 'movie', 'tt51')

    assert result is False
    assert env.player_play_calls == []
    assert env.resolved == []  # play_direct never touches xbmcplugin.setResolvedUrl()
    assert [msg for _, msg, _, _ in env.notifications] == ['STR30160']



def test_unsupported_stream_error_log_and_notification_omit_embedded_secrets(kodi_stubs, monkeypatch):
    """A rejected direct url may embed a credential/token a malicious
    addon put there - lib.ui.player logs `%r` of the raised
    UnsupportedStreamError (see the `except UnsupportedStreamError`
    block in _resolve_playable_item) and shows a fixed notification
    string; neither may ever contain the secret. Uses the REAL
    lib.stremio.server._validate_direct_url() to produce the actual
    exception message a live rejection would carry."""
    from lib.stremio.server import UnsupportedStreamError, _validate_direct_url

    secret_url = 'plugin://user:SUPERSECRETPASS@evil.example.com/steal?token=SECRETTOKEN#frag'
    try:
        _validate_direct_url(secret_url)
        raise AssertionError('expected UnsupportedStreamError')
    except UnsupportedStreamError as exc:
        real_error = exc

    env = kodi_stubs.env
    _ServerScript(resolve_error=real_error).install(monkeypatch, kodi_stubs.player)

    kodi_stubs.player.play(61, {'url': secret_url}, 'movie', 'tt61')

    logged_text = ' '.join(msg for msg, _level in env.log_calls)
    notified_text = ' '.join(msg for _, msg, _, _ in env.notifications)
    for secret in ('SUPERSECRETPASS', 'SECRETTOKEN', 'steal', 'frag', 'evil.example.com'):
        assert secret not in logged_text
        assert secret not in notified_text
    handle, succeeded, list_item = _resolved_one(env)
    assert (handle, succeeded) == (61, False)

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
    its own, so `playbackmeta.mime_for` could never derive a MIME type
    from it before. `playbackmeta.extract_file_name` recovers the real
    filename from the
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


def test_apply_item_metadata_applies_cast_from_meta_with_one_based_order(kodi_stubs, monkeypatch):
    """`item_meta['meta']['cast']` (a Stremio meta's plain actor-name
    array) must reach the playback ListItem via `compat.set_video_cast`,
    alongside the existing title/art/plot metadata - the OSD/info cast
    display Kodi's fullscreen player offers during playback, on both the
    `play()` and `play_direct()` paths (both funnel through
    `_apply_item_metadata`). The default stub build has no
    `System.BuildVersion`, so `compat.kodi_major_version()` falls back to
    19's legacy `ListItem.setCast()` path (`legacy_cast`).
    """
    env = kodi_stubs.env
    env.addon.settings['buffer_enable'] = False
    _ServerScript(resolve_url='http://server/x/0').install(monkeypatch, kodi_stubs.player)

    item_meta = {
        'label': 'Dune',
        'meta': {'description': 'A desert planet', 'cast': ['Timothee Chalamet', 'Zendaya']},
    }
    kodi_stubs.player.play(77, _torrent_stream(fileIdx=0), 'movie', 'tt77', item_meta=item_meta)

    _handle, _succeeded, list_item = _resolved_one(env)
    # title/art/plot still populated alongside the new cast wiring
    assert list_item.getLabel() == 'Dune'
    assert list_item.legacy_info.get('plot') == 'A desert planet'
    assert list_item.legacy_cast == [
        {'name': 'Timothee Chalamet', 'role': '', 'order': 1, 'thumbnail': ''},
        {'name': 'Zendaya', 'role': '', 'order': 2, 'thumbnail': ''},
    ]


def test_apply_item_metadata_no_cast_call_when_meta_has_no_cast(kodi_stubs, monkeypatch):
    """A `meta` dict present but with no `cast` key must never call
    `setCast()` at all - `compat.set_video_cast` is a no-op on absent
    input, but this guards the caller side too."""
    env = kodi_stubs.env
    env.addon.settings['buffer_enable'] = False
    _ServerScript(resolve_url='http://server/x/0').install(monkeypatch, kodi_stubs.player)

    item_meta = {'label': 'Dune', 'meta': {'description': 'A desert planet'}}
    kodi_stubs.player.play(78, _torrent_stream(fileIdx=0), 'movie', 'tt78', item_meta=item_meta)

    _handle, _succeeded, list_item = _resolved_one(env)
    assert list_item.legacy_cast is None
    assert list_item.info_tag.calls == {}


def test_apply_item_metadata_no_cast_call_when_item_meta_has_no_meta_key(kodi_stubs, monkeypatch):
    """No `item_meta['meta']` at all (or no `item_meta`) must also never
    call `setCast()`."""
    env = kodi_stubs.env
    env.addon.settings['buffer_enable'] = False
    _ServerScript(resolve_url='http://server/x/0').install(monkeypatch, kodi_stubs.player)

    kodi_stubs.player.play(79, _torrent_stream(fileIdx=0), 'movie', 'tt79', item_meta={'label': 'Dune'})

    _handle, _succeeded, list_item = _resolved_one(env)
    assert list_item.legacy_cast is None
    assert list_item.info_tag.calls == {}


# --- _attach_subtitles: subs_language filtering (issue #6) ----------------
# Kodi reads an external subtitle's language from its filename, and addon
# subtitle URLs end in opaque numeric ids, so every attached track used to
# arrive with an empty language - Kodi's auto-selection then picked
# arbitrarily among a mixed-language batch. _attach_subtitles now narrows
# collect_subtitles()'s result to subs_language via filter_subtitles()
# (the real, unpatched function) before ever calling setSubtitles().


class _StubAddonStore:
    """No-op lib.store.Store stand-in: only get_enabled_addons() is reached, and
    only to build the (unused, since collect_subtitles is monkeypatched
    below) addon list collect_subtitles() would otherwise query."""

    def get_addons(self):
        return []

    def get_enabled_addons(self):
        return []


class _FakeSubtitleStore:
    """lib.store.Store stand-in carrying real descriptors (unlike
    _StubAddonStore above): the disabled-addon dispatch test below needs
    get_enabled_addons() to actually filter, since collect_subtitles()
    runs unpatched there."""

    def __init__(self, addons):
        self._addons = addons

    def get_addons(self):
        return self._addons

    def get_enabled_addons(self):
        return [a for a in self._addons if not (a.get('flags') or {}).get('disabled')]


class _FakeSubtitleClient:
    """Fake AddonClient.subtitles(): records every transport_url queried,
    so a test can assert a disabled addon received no request."""

    def __init__(self):
        self.calls = []

    def subtitles(self, base, rtype, sid, extra=None):
        self.calls.append(base)
        return [{'lang': 'en', 'url': '%s/sub.srt' % base}]


def _install_collect_subtitles(monkeypatch, player_module, subs):
    monkeypatch.setattr(player_module, 'collect_subtitles', lambda *a, **k: subs)
    monkeypatch.setattr(player_module, 'get_client', lambda: None)
    monkeypatch.setattr(player_module, 'get_store', lambda: _StubAddonStore())


def test_attach_subtitles_filters_to_preferred_language_in_collect_order(kodi_stubs, monkeypatch):
    env = kodi_stubs.env
    env.addon.settings['subs_enable'] = True
    env.addon.settings['subs_language'] = 'en'
    subs = [
        {'lang': 'es', 'url': 'https://x/es.srt'},
        {'lang': 'en', 'url': 'https://x/en1.srt'},
        {'lang': 'fr', 'url': 'https://x/fr.srt'},
        {'lang': 'en', 'url': 'https://x/en2.srt'},
    ]
    _install_collect_subtitles(monkeypatch, kodi_stubs.player, subs)
    list_item = kodi_stubs.player.xbmcgui.ListItem()

    kodi_stubs.player._attach_subtitles(list_item, {}, 'movie', 'tt1')

    assert list_item.subtitles == ['https://x/en1.srt', 'https://x/en2.srt']


def test_attach_subtitles_no_matching_language_never_calls_setsubtitles(kodi_stubs, monkeypatch):
    env = kodi_stubs.env
    env.addon.settings['subs_enable'] = True
    env.addon.settings['subs_language'] = 'en'
    subs = [{'lang': 'es', 'url': 'https://x/es.srt'}, {'lang': 'fr', 'url': 'https://x/fr.srt'}]
    _install_collect_subtitles(monkeypatch, kodi_stubs.player, subs)
    list_item = kodi_stubs.player.xbmcgui.ListItem()

    kodi_stubs.player._attach_subtitles(list_item, {}, 'movie', 'tt1')

    assert list_item.subtitles is None


def test_attach_subtitles_more_than_twenty_matches_attaches_only_first_twenty(kodi_stubs, monkeypatch):
    env = kodi_stubs.env
    env.addon.settings['subs_enable'] = True
    env.addon.settings['subs_language'] = 'en'
    subs = [{'lang': 'en', 'url': 'https://x/%d.srt' % i} for i in range(25)]
    _install_collect_subtitles(monkeypatch, kodi_stubs.player, subs)
    list_item = kodi_stubs.player.xbmcgui.ListItem()

    kodi_stubs.player._attach_subtitles(list_item, {}, 'movie', 'tt1')

    assert list_item.subtitles == ['https://x/%d.srt' % i for i in range(20)]


def test_attach_subtitles_disabled_setting_skips_collect_subtitles_entirely(kodi_stubs, monkeypatch):
    env = kodi_stubs.env
    env.addon.settings['subs_enable'] = False
    calls = []
    monkeypatch.setattr(kodi_stubs.player, 'collect_subtitles', lambda *a, **k: calls.append(1) or [])
    list_item = kodi_stubs.player.xbmcgui.ListItem()

    kodi_stubs.player._attach_subtitles(list_item, {}, 'movie', 'tt1')

    assert calls == []
    assert list_item.subtitles is None


def test_attach_subtitles_never_dispatches_to_a_disabled_addon(kodi_stubs, monkeypatch):
    """collect_subtitles() must fan out through get_enabled_addons(), not
    get_addons(): a disabled subtitle-capable addon must receive no
    request, even though it would otherwise return a match."""
    env = kodi_stubs.env
    env.addon.settings['subs_enable'] = True
    env.addon.settings['subs_language'] = 'en'
    manifest = {'resources': ['subtitles'], 'types': ['movie']}
    client = _FakeSubtitleClient()
    store = _FakeSubtitleStore([
        {'transportUrl': 'https://enabled.example/manifest.json', 'manifest': manifest},
        {'transportUrl': 'https://disabled.example/manifest.json', 'manifest': manifest,
         'flags': {'disabled': True}},
    ])
    monkeypatch.setattr(kodi_stubs.player, 'get_client', lambda: client)
    monkeypatch.setattr(kodi_stubs.player, 'get_store', lambda: store)
    list_item = kodi_stubs.player.xbmcgui.ListItem()

    kodi_stubs.player._attach_subtitles(list_item, {}, 'movie', 'tt1')

    assert client.calls == ['https://enabled.example/manifest.json']
    assert list_item.subtitles == ['https://enabled.example/manifest.json/sub.srt']


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
    `dialogs.confirm()` answers queued up front -- that fixture has no
    such parameter (no other test in this file needs one)."""
    with install_kodi_stubs(reload=_RELOADED_MODULES, localized={30090: 'attempt %d of %d'}) as ctx:
        yield _wire_player_dialogs(ctx, yesno_answers)




# --- lib.ui.playbackmeta: pure filename/MIME helpers, tested directly ------
# (no kodi_stubs needed - playbackmeta.py has no xbmc dependency)


def test_mime_for_known_extension_returns_mimetype():
    assert playbackmeta.mime_for('Movie.2020.mkv') == 'video/x-matroska'


def test_mime_for_unknown_or_absent_extension_returns_none():
    assert playbackmeta.mime_for('Movie.2020.xyz') is None
    assert playbackmeta.mime_for('') is None
    assert playbackmeta.mime_for(None) is None


def test_filename_from_url_strips_headers_and_query_string():
    url = 'http://server/x/0/Movie.mkv?token=1|User-Agent=test'
    assert playbackmeta.filename_from_url(url) == 'Movie.mkv'


def test_extract_file_name_returns_name_at_index():
    stats = {'files': [{'name': 'a.mkv'}, {'name': 'b.mkv'}]}
    assert playbackmeta.extract_file_name(stats, 1) == 'b.mkv'


def test_extract_file_name_out_of_range_or_malformed_returns_none():
    assert playbackmeta.extract_file_name({'files': [{'name': 'a.mkv'}]}, 5) is None
    assert playbackmeta.extract_file_name({'files': 'not-a-list'}, 0) is None
    assert playbackmeta.extract_file_name({}, 0) is None
    assert playbackmeta.extract_file_name(None, 0) is None
