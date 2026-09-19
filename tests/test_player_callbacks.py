# ruff: noqa: F811
"""Tests for lib.ui.player's resume/now-playing/playback-callback behavior.

Split from tests/test_player_buffer.py: this file owns LibrarySync "now
playing" context recording, the resume prompt (1%-95% band, `resume_ask`
setting, yes/no), and the degrade-gracefully guarantees (a logged-out user,
a broken store, or a broken `get_progress()` must never block playback).
URL resolution / `_resolve_playable_item` / item metadata live in
test_player_buffer.py; `_prebuffer_torrent()` polling/cancel/infoHash live
in test_player_prebuffer.py. Shared fixtures (`kodi_stubs`, `_ServerScript`,
`_resolved_one`, `_kodi_stubs_with_yesno`) are defined once in
test_player_buffer.py (the largest/original file) and imported here to
avoid duplicating them across three files.

Reference: lib/ui/player.py `_resolve_playable_item()`'s now-playing/resume
wiring and `lib.store.Store.get_progress`/`set_now_playing`/
`set_resume_offset_ms`.
"""
from tests.test_player_buffer import (  # noqa: F401 - re-exported fixtures/helpers
    _kodi_stubs_with_yesno,
    _resolved_one,
    _ServerScript,
    kodi_stubs,
)


class _FakeProgressStore:
    """Fake `lib.store.Store` surface `lib.ui.player`'s resume/now-
    playing code needs (`get_progress`/`set_now_playing`/
    `set_resume_offset_ms`) -- injected via `monkeypatch.setattr(
    kodi_stubs.player, 'get_store', ...)` so these tests never touch a real
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
    monkeypatch.setattr(player_module, 'get_store', lambda: store)


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


def test_series_now_playing_and_progress_are_rooted_at_the_shows_meta_id_not_the_episode_sid(
    kodi_stubs, monkeypatch,
):
    """For a series, `sid` passed to `play_direct()` is the episode/stream
    id, but local resume/progress and the stored now-playing context must
    stay rooted at the SHOW's own library id (`item_meta['meta']['id']`),
    keyed together with the exact episode `video_id`."""
    store = _FakeProgressStore()
    _install_progress_store(monkeypatch, kodi_stubs.player, store)
    _ServerScript(resolve_url='https://example.com/a.mp4').install(monkeypatch, kodi_stubs.player)

    item_meta = {'meta': {'id': 'tt-show', 'name': 'Show'}}
    kodi_stubs.player.play_direct(
        {'url': 'https://example.com/a.mp4'}, 'series', 's1e2', item_meta=item_meta, video_id='s1e2',
    )

    assert store.get_progress_calls == [('series', 'tt-show', 's1e2')]
    assert store.now_playing['id'] == 'tt-show'
    assert store.now_playing['video_id'] == 's1e2'


def test_series_content_id_falls_back_to_sid_when_meta_has_no_id(kodi_stubs, monkeypatch):
    """A series `item_meta` with no (or falsy) `meta.id` must fall back to
    `sid` exactly like a movie does - never crash, never key on `None`."""
    store = _FakeProgressStore()
    _install_progress_store(monkeypatch, kodi_stubs.player, store)
    _ServerScript(resolve_url='https://example.com/a.mp4').install(monkeypatch, kodi_stubs.player)

    item_meta = {'meta': {'name': 'Show'}}
    kodi_stubs.player.play_direct(
        {'url': 'https://example.com/a.mp4'}, 'series', 's1e2', item_meta=item_meta, video_id='s1e2',
    )

    assert store.get_progress_calls == [('series', 's1e2', 's1e2')]
    assert store.now_playing['id'] == 's1e2'


def test_movie_content_id_matches_sid_when_meta_id_mirrors_it(kodi_stubs, monkeypatch):
    """A movie's own `item_meta['meta']['id']` (when a caller supplies
    one, e.g. `lib.ui.infowindow`) is the SAME id as `sid` - the movie's
    library id never has a separate show-vs-episode split, so the shared
    `(meta.id or sid)` derivation is a no-op for movies: `id` stays
    `sid`, `video_id` stays `None`, exactly as before this change."""
    store = _FakeProgressStore()
    _install_progress_store(monkeypatch, kodi_stubs.player, store)
    _ServerScript(resolve_url='https://example.com/a.mp4').install(monkeypatch, kodi_stubs.player)

    item_meta = {'meta': {'id': 'tt-movie', 'name': 'Movie'}}
    kodi_stubs.player.play_direct(
        {'url': 'https://example.com/a.mp4'}, 'movie', 'tt-movie', item_meta=item_meta,
    )

    assert store.get_progress_calls == [('movie', 'tt-movie', None)]
    assert store.now_playing['id'] == 'tt-movie'
    assert store.now_playing['video_id'] is None


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

    monkeypatch.setattr(kodi_stubs.player, 'get_store', _raise)
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


