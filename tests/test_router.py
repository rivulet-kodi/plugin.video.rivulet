"""Tests for lib.ui.router: sys.argv -> action dispatch, exercised against
the shared fake xbmc/xbmcgui/xbmcplugin/xbmcaddon/xbmcvfs stubs in
tests/kodistubs (no real Kodi runtime, no network, no subprocess).

router.py only imports xbmc/xbmcgui/xbmcplugin (and lib.ui.player/views)
lazily inside run()/_download_server_binary()/_fail_gracefully() - never at
module scope - so a bare `import lib.ui.router` needs no stubs at all for
the pure helpers (_parse_params/url_for/encode_stream/decode_stream).
`load_router` still reloads it fresh per call (via
tests.kodistubs.install_kodi_stubs, reloading lib.ui.compat/lib.ui.router)
so every test starts from router's declared ADDON_HANDLE=-1/BASE_URL=''
module globals with no cross-test leakage.

For run() dispatch, router.run() resolves `from lib.ui import player,
views` via a plain getattr() on the already-imported `lib.ui` package
object (see tests/kodistubs/install.py's docstring) - if that attribute
already exists, Python's import machinery never touches sys.modules at
all. `load_router` exploits exactly that: it binds `lib.ui.views`/
`lib.ui.player` to a fresh `_Recorder` (see below) both in sys.modules and
as package attributes, so run() dispatches to *fakes* it can assert call
arguments against, without ever executing (or leaking an import of) the
real, heavier views.py/player.py. Both bindings are restored - popped if
originally absent - at teardown, same as the stub modules themselves.
"""
import base64
import contextlib
import os
import sys
from urllib.parse import parse_qsl, urlencode, urlparse

import pytest

import lib.advancedsettings as advancedsettings
import lib.serverbin as serverbin
from tests.kodistubs import FakeListItem, install_kodi_stubs

_RELOAD_MODULE_NAMES = ('lib.ui.compat', 'lib.ui.router')

_MISSING = object()


class _Recorder:
    """Stand-in for lib.ui.views / lib.ui.player: any attribute access not
    explicitly overridden resolves to a callable that appends
    `(name, args, kwargs)` to `.calls` and returns None. Assign a specific
    attribute (e.g. `.home = my_func`) to override one action for a single
    test (e.g. to make it raise).
    """

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def _record(*args, **kwargs):
            self.calls.append((name, args, kwargs))
        return _record


@pytest.fixture
def load_router():
    """Factory fixture: `load_router(**config)` installs fresh stubs (via
    tests.kodistubs.install_kodi_stubs) reloading lib.ui.compat/
    lib.ui.router, additionally binding lib.ui.views/lib.ui.player to fresh
    `_Recorder`s, and returns a namespace with `.router`, `.compat`,
    `.env`, `.views`, `.player`. Every call is torn down automatically, in
    reverse order, at test end - including restoring the `lib.ui`
    package's `views`/`player` attributes and sys.modules entries exactly
    - so no other test file ever observes the fakes.
    """
    import lib.ui as ui_pkg

    with contextlib.ExitStack() as stack:
        def _load(addon_info=None, settings=None, localized=None, dialog_inputs=None,
                   cancel=False, monitor_abort=False):
            ctx = stack.enter_context(install_kodi_stubs(
                reload=_RELOAD_MODULE_NAMES,
                addon_info=addon_info,
                settings=settings,
                localized=localized,
                dialog_inputs=dialog_inputs,
                cancel=cancel,
                monitor_abort=monitor_abort,
            ))

            saved_views_mod = sys.modules.get('lib.ui.views', _MISSING)
            saved_player_mod = sys.modules.get('lib.ui.player', _MISSING)
            saved_views_attr = getattr(ui_pkg, 'views', _MISSING)
            saved_player_attr = getattr(ui_pkg, 'player', _MISSING)

            def _restore():
                for name, saved in (('lib.ui.views', saved_views_mod), ('lib.ui.player', saved_player_mod)):
                    if saved is _MISSING:
                        sys.modules.pop(name, None)
                    else:
                        sys.modules[name] = saved
                for attr, saved in (('views', saved_views_attr), ('player', saved_player_attr)):
                    if saved is _MISSING:
                        if hasattr(ui_pkg, attr):
                            delattr(ui_pkg, attr)
                    else:
                        setattr(ui_pkg, attr, saved)

            stack.callback(_restore)

            fake_views = _Recorder()
            fake_player = _Recorder()
            sys.modules['lib.ui.views'] = fake_views
            sys.modules['lib.ui.player'] = fake_player
            ui_pkg.views = fake_views
            ui_pkg.player = fake_player

            ctx.views = fake_views
            ctx.player = fake_player
            return ctx

        yield _load


# ---------------------------------------------------------------------------
# _parse_params
# ---------------------------------------------------------------------------


@pytest.mark.parametrize('raw_qs, expected', [
    ('', {}),
    ('?', {}),
    ('action=home', {'action': 'home'}),
    ('?action=home&id=tt123', {'action': 'home', 'id': 'tt123'}),
    ('a=1&a=2', {'a': '1'}),
    ('title=Foo%20Bar%20%26%20Baz', {'title': 'Foo Bar & Baz'}),
], ids=[
    'empty-string',
    'bare-question-mark',
    'no-leading-question-mark',
    'leading-question-mark-is-stripped',
    'repeated-key-collapses-to-first',
    'percent-encoded-value-is-decoded',
])
def test_parse_params(load_router, raw_qs, expected):
    ctx = load_router()
    assert ctx.router._parse_params(raw_qs) == expected


# ---------------------------------------------------------------------------
# url_for
# ---------------------------------------------------------------------------


def test_url_for_builds_plugin_url_with_action_and_params(load_router):
    ctx = load_router()
    ctx.router.BASE_URL = 'plugin://plugin.video.rivulet/'

    url = ctx.router.url_for('meta', type='movie', id='tt123')

    assert url.startswith('plugin://plugin.video.rivulet/?')
    assert parse_qsl(urlparse(url).query) == [('action', 'meta'), ('type', 'movie'), ('id', 'tt123')]


def test_url_for_drops_none_and_empty_string_params(load_router):
    ctx = load_router()
    ctx.router.BASE_URL = 'plugin://plugin.video.rivulet/'

    url = ctx.router.url_for('search', q=None, extra='')

    assert parse_qsl(urlparse(url).query) == [('action', 'search')]


def test_url_for_preserves_kwarg_call_order_after_action(load_router):
    ctx = load_router()
    ctx.router.BASE_URL = 'plugin://plugin.video.rivulet/'

    url = ctx.router.url_for('catalog', extra='skip=2', id='tt1', type='movie', transport='http://x/m.json')

    assert parse_qsl(urlparse(url).query) == [
        ('action', 'catalog'),
        ('extra', 'skip=2'),
        ('id', 'tt1'),
        ('type', 'movie'),
        ('transport', 'http://x/m.json'),
    ]


def test_url_for_round_trips_special_characters_through_parse_params(load_router):
    ctx = load_router()
    ctx.router.BASE_URL = 'plugin://plugin.video.rivulet/'
    tricky_id = 'Bad & Ugly: 2024 \u00dcn\u00efcode?/#'

    url = ctx.router.url_for('meta', type='movie', id=tricky_id)
    query_string = urlparse(url).query

    assert ctx.router._parse_params(query_string) == {'action': 'meta', 'type': 'movie', 'id': tricky_id}


# ---------------------------------------------------------------------------
# encode_stream / decode_stream
# ---------------------------------------------------------------------------

STREAM_SAMPLE = {
    'name': 'Stream4Me \U0001f3ac',
    'title': 'Movie.2024.2160p.HDR.x265-GROUP \U0001f525',
    'url': 'https://cdn.example.com/path?tok=abc123==&x=1',
    'infoHash': 'deadbeefdeadbeefdeadbeefdeadbeefdeadbeef',
    'behaviorHints': {'bingeGroup': 'rivulet|2160p|HDR', 'filename': 'movie (2024).mkv'},
}

# Deliberately chosen so its STANDARD (non-urlsafe) base64 encoding contains
# both '+' and '/' (verified out of band) - a regression guard for
# encode_stream() using urlsafe_b64encode (required for the token to
# round-trip unmangled through url_for()'s urlencode() inside a plugin://
# querystring) rather than plain b64encode.
STREAM_WITH_PLUS_AND_SLASH_BYTES = {'infoHash': 'o/b>K1JtP($ 7?A#> t)Zmj(UMX51h', 'n': 3}


def test_encode_decode_stream_round_trips_unicode_and_nested_dict(load_router):
    ctx = load_router()
    token = ctx.router.encode_stream(STREAM_SAMPLE)
    assert ctx.router.decode_stream(token) == STREAM_SAMPLE


def test_encode_stream_none_and_empty_dict_are_equivalent(load_router):
    ctx = load_router()
    assert ctx.router.decode_stream(ctx.router.encode_stream(None)) == {}
    assert ctx.router.decode_stream(ctx.router.encode_stream({})) == {}


def test_decode_stream_empty_or_missing_token_returns_empty_dict(load_router):
    ctx = load_router()
    assert ctx.router.decode_stream('') == {}
    assert ctx.router.decode_stream(None) == {}


def test_decode_stream_garbage_base64_returns_empty_dict(load_router):
    ctx = load_router()
    assert ctx.router.decode_stream('!!!not-valid-base64!!!') == {}


def test_decode_stream_valid_base64_invalid_utf8_returns_empty_dict(load_router):
    ctx = load_router()
    token = base64.urlsafe_b64encode(b'\xff\xfe\xfd').decode('ascii')
    assert ctx.router.decode_stream(token) == {}


def test_decode_stream_valid_base64_non_json_returns_empty_dict(load_router):
    ctx = load_router()
    token = base64.urlsafe_b64encode(b'not json at all').decode('ascii')
    assert ctx.router.decode_stream(token) == {}


def test_encode_stream_uses_urlsafe_alphabet_for_plus_slash_producing_payload(load_router):
    ctx = load_router()

    token = ctx.router.encode_stream(STREAM_WITH_PLUS_AND_SLASH_BYTES)

    assert '+' not in token
    assert '/' not in token
    assert '-' in token
    assert '_' in token
    assert ctx.router.decode_stream(token) == STREAM_WITH_PLUS_AND_SLASH_BYTES


# ---------------------------------------------------------------------------
# run() dispatch: directory-listing / no-arg actions
# ---------------------------------------------------------------------------

DISPATCH_CASES = [
    pytest.param({'action': 'home'}, ('home', (), {}), id='home'),
    pytest.param({'action': 'discover'}, ('discover', (), {}), id='discover'),
    pytest.param(
        {'action': 'catalog', 'transport': 'http://x/manifest.json', 'type': 'movie', 'id': 'tt1', 'extra': 'skip=2'},
        ('catalog', ('http://x/manifest.json', 'movie', 'tt1', 'skip=2'), {}),
        id='catalog',
    ),
    pytest.param(
        {'action': 'showcase', 'transport': 'http://x/manifest.json', 'type': 'movie', 'id': 'tt1', 'extra': 'skip=2'},
        ('showcase', ('http://x/manifest.json', 'movie', 'tt1', 'skip=2'), {}),
        id='showcase',
    ),
    pytest.param({'action': 'search'}, ('search', (), {}), id='search'),
    pytest.param(
        {'action': 'meta', 'type': 'series', 'id': 'tt2'},
        ('meta', ('series', 'tt2'), {}),
        id='meta',
    ),
    pytest.param(
        {'action': 'videos', 'type': 'series', 'id': 'tt2', 'season': '1'},
        ('videos', ('series', 'tt2', '1'), {}),
        id='videos',
    ),
    pytest.param(
        {'action': 'streams', 'type': 'movie', 'id': 'tt1'},
        ('streams', ('movie', 'tt1'), {}),
        id='streams',
    ),
    pytest.param({'action': 'addons'}, ('addons', (), {}), id='addons'),
    pytest.param({'action': 'addon_install'}, ('addon_install', (), {}), id='addon_install'),
    pytest.param(
        {'action': 'addon_remove', 'transport': 'http://x/manifest.json'},
        ('addon_remove', ('http://x/manifest.json',), {}),
        id='addon_remove',
    ),
    pytest.param({'action': 'login'}, ('login', (), {}), id='login'),
    pytest.param({'action': 'logout'}, ('logout', (), {}), id='logout'),
    pytest.param({'action': 'library'}, ('library', (), {}), id='library'),
    # Note: the URL action string is literally 'settings' (not
    # 'open_settings') - only the view function it dispatches to is named
    # open_settings(). See final report re: the assignment's naming.
    pytest.param({'action': 'settings'}, ('open_settings', (), {}), id='settings-calls-open_settings'),
    pytest.param({'action': 'sync_addons_now'}, ('sync_addons_now', (), {}), id='sync_addons_now'),
]


@pytest.mark.parametrize('query_params, expected_call', DISPATCH_CASES)
def test_run_dispatches_to_matching_view(load_router, monkeypatch, query_params, expected_call):
    ctx = load_router()
    monkeypatch.setattr(sys, 'argv', ['plugin://plugin.video.rivulet/', '7', '?' + urlencode(query_params)])

    ctx.router.run()

    assert ctx.views.calls == [expected_call]
    assert ctx.player.calls == []


def test_run_missing_action_defaults_to_home(load_router, monkeypatch):
    ctx = load_router()
    monkeypatch.setattr(sys, 'argv', ['plugin://plugin.video.rivulet/', '7', ''])

    ctx.router.run()

    assert ctx.views.calls == [('home', (), {})]


def test_run_unknown_action_falls_back_to_home(load_router, monkeypatch):
    ctx = load_router()
    monkeypatch.setattr(sys, 'argv', ['plugin://plugin.video.rivulet/', '7', '?action=some_future_action_v2'])

    ctx.router.run()

    assert ctx.views.calls == [('home', (), {})]


def test_run_sets_base_url_and_handle_from_argv(load_router, monkeypatch):
    ctx = load_router()
    monkeypatch.setattr(sys, 'argv', ['plugin://plugin.video.rivulet/', '42', '?action=home'])

    ctx.router.run()

    assert ctx.router.BASE_URL == 'plugin://plugin.video.rivulet/'
    assert ctx.router.ADDON_HANDLE == 42


def test_run_with_empty_argv_falls_back_to_defaults(load_router, monkeypatch):
    ctx = load_router()
    monkeypatch.setattr(sys, 'argv', [])

    ctx.router.run()

    assert ctx.router.BASE_URL == 'plugin://plugin.video.rivulet/'
    assert ctx.router.ADDON_HANDLE == -1
    assert ctx.views.calls == [('home', (), {})]


def test_run_with_non_numeric_handle_falls_back_to_negative_one(load_router, monkeypatch):
    ctx = load_router()
    monkeypatch.setattr(sys, 'argv', ['plugin://x/', 'not-a-number', '?action=home'])

    ctx.router.run()

    assert ctx.router.ADDON_HANDLE == -1
    assert ctx.router.BASE_URL == 'plugin://x/'


# ---------------------------------------------------------------------------
# run() dispatch: play (decodes the stream token first)
# ---------------------------------------------------------------------------


def test_run_play_decodes_stream_and_calls_player(load_router, monkeypatch):
    ctx = load_router()
    stream = {
        'name': 'Stream4Me',
        'title': '2160p HDR',
        'infoHash': 'deadbeef' * 5,
        'behaviorHints': {'bingeGroup': 'x'},
    }
    token = ctx.router.encode_stream(stream)
    query = urlencode({'action': 'play', 'stream': token, 'type': 'movie', 'id': 'tt1'})
    monkeypatch.setattr(sys, 'argv', ['plugin://plugin.video.rivulet/', '9', '?' + query])

    ctx.router.run()

    assert ctx.player.calls == [('play', (9, stream, 'movie', 'tt1'), {})]
    assert ctx.views.calls == []


def test_run_play_without_stream_param_decodes_to_empty_dict(load_router, monkeypatch):
    ctx = load_router()
    query = urlencode({'action': 'play', 'type': 'movie', 'id': 'tt1'})
    monkeypatch.setattr(sys, 'argv', ['plugin://plugin.video.rivulet/', '9', '?' + query])

    ctx.router.run()

    assert ctx.player.calls == [('play', (9, {}, 'movie', 'tt1'), {})]


# ---------------------------------------------------------------------------
# run(): a handler exception never escapes - it logs and calls
# _fail_gracefully() instead (the router's last-resort guard).
# ---------------------------------------------------------------------------


def test_run_handler_exception_ends_directory_as_failed_and_logs_error(load_router, monkeypatch):
    ctx = load_router()

    def _raise():
        raise RuntimeError('boom')

    ctx.views.home = _raise
    monkeypatch.setattr(sys, 'argv', ['plugin://x/', '5', '?action=home'])

    ctx.router.run()  # must never propagate - that is the whole point of the guard

    xbmc_mod = sys.modules['xbmc']
    assert ctx.env.end_of_directory == [
        {'handle': 5, 'succeeded': False, 'updateListing': False, 'cacheToDisc': True}
    ]
    assert ctx.env.resolved == []
    assert any(
        level == xbmc_mod.LOGERROR and 'action "home" failed' in msg
        for msg, level in ctx.env.log_calls
    )


def test_run_handler_exception_on_play_resolves_url_as_failed(load_router, monkeypatch):
    ctx = load_router()

    def _raise(handle, stream, type_, id_):
        raise RuntimeError('boom')

    ctx.player.play = _raise
    token = ctx.router.encode_stream({})
    query = urlencode({'action': 'play', 'stream': token, 'type': 'movie', 'id': 'tt1'})
    monkeypatch.setattr(sys, 'argv', ['plugin://x/', '5', '?' + query])

    ctx.router.run()

    assert ctx.env.end_of_directory == []
    assert len(ctx.env.resolved) == 1
    handle, succeeded, list_item = ctx.env.resolved[0]
    assert (handle, succeeded) == (5, False)
    assert isinstance(list_item, FakeListItem)


# ---------------------------------------------------------------------------
# _fail_gracefully() called directly
# ---------------------------------------------------------------------------


def test_fail_gracefully_ends_directory_for_non_play_action(load_router):
    ctx = load_router()
    ctx.router.ADDON_HANDLE = 11

    ctx.router._fail_gracefully('catalog')

    assert ctx.env.end_of_directory == [
        {'handle': 11, 'succeeded': False, 'updateListing': False, 'cacheToDisc': True}
    ]
    assert ctx.env.resolved == []
    assert [m for _, m, _, _ in ctx.env.notifications] == ['STR30032']


def test_fail_gracefully_resolves_url_false_for_play_action(load_router):
    ctx = load_router()
    ctx.router.ADDON_HANDLE = 11

    ctx.router._fail_gracefully('play')

    assert ctx.env.end_of_directory == []
    assert len(ctx.env.resolved) == 1
    handle, succeeded, list_item = ctx.env.resolved[0]
    assert (handle, succeeded) == (11, False)
    assert isinstance(list_item, FakeListItem)
    assert [m for _, m, _, _ in ctx.env.notifications] == ['STR30032']


# ---------------------------------------------------------------------------
# _download_server_binary() (action 'server_download')
# ---------------------------------------------------------------------------


def test_server_download_success_notifies_with_path_and_closes_dialog(load_router, monkeypatch):
    ctx = load_router(localized={30062: 'installed to %s'})
    monkeypatch.setattr(sys, 'argv', ['plugin://x/', '7', '?action=server_download'])

    calls = []

    def fake_install_binary(dest_dir, progress_cb=None):
        calls.append(dest_dir)
        progress_cb(0, 0)      # total falsy -> percent forced to 0
        progress_cb(150, 100)  # over 100% -> clamped via min(percent, 100)
        return os.path.join(dest_dir, 'stremio-server')

    monkeypatch.setattr(serverbin, 'install_binary', fake_install_binary)

    ctx.router.run()

    assert os.path.basename(calls[0]) == 'bin'
    expected_path = os.path.join(calls[0], 'stremio-server')
    assert ctx.env.dialog_created == [('STR30061', '')]
    assert ctx.env.dialog_updates == [(0, 'STR30061'), (100, 'STR30061')]
    assert ctx.env.dialog_closed_count == 1
    assert ctx.env.notifications == [('Rivulet', 'installed to %s' % expected_path, 'info', 4000)]


def test_server_download_no_asset_error_notifies_and_logs_warning(load_router, monkeypatch):
    ctx = load_router()
    monkeypatch.setattr(sys, 'argv', ['plugin://x/', '7', '?action=server_download'])

    def fake_install_binary(dest_dir, progress_cb=None):
        raise serverbin.NoAssetError('no release asset for Linux/arm64')

    monkeypatch.setattr(serverbin, 'install_binary', fake_install_binary)
    xbmc_mod = sys.modules['xbmc']

    ctx.router.run()

    assert [msg for _, msg, _, _ in ctx.env.notifications] == ['STR30064']
    assert any(
        level == xbmc_mod.LOGWARNING and 'router: server_download:' in msg
        for msg, level in ctx.env.log_calls
    )
    assert ctx.env.dialog_closed_count == 1
    assert ctx.env.end_of_directory == []
    assert ctx.env.resolved == []


def test_server_download_download_error_notifies_and_logs_error(load_router, monkeypatch):
    ctx = load_router()
    monkeypatch.setattr(sys, 'argv', ['plugin://x/', '7', '?action=server_download'])

    def fake_install_binary(dest_dir, progress_cb=None):
        progress_cb(10, 100)
        raise serverbin.DownloadError('checksum mismatch for stremio-server_Linux_x86_64.tar.gz')

    monkeypatch.setattr(serverbin, 'install_binary', fake_install_binary)
    xbmc_mod = sys.modules['xbmc']

    ctx.router.run()

    assert [msg for _, msg, _, _ in ctx.env.notifications] == ['STR30063']
    assert any(
        level == xbmc_mod.LOGERROR and 'router: server_download failed:' in msg
        for msg, level in ctx.env.log_calls
    )
    assert ctx.env.dialog_updates == [(10, 'STR30061')]
    assert ctx.env.dialog_closed_count == 1


def test_server_download_user_cancel_mid_download_notifies_download_error(load_router, monkeypatch):
    """DialogProgress.iscanceled() is checked by the REAL progress_cb defined
    inside _download_server_binary() (not mocked here) - only
    install_binary() itself is faked, and it just drives that real
    callback, exactly like a real download loop would."""
    ctx = load_router(cancel=True)
    monkeypatch.setattr(sys, 'argv', ['plugin://x/', '7', '?action=server_download'])

    def fake_install_binary(dest_dir, progress_cb=None):
        progress_cb(10, 100)  # real progress_cb sees iscanceled()=True and raises
        return os.path.join(dest_dir, 'stremio-server')  # never reached

    monkeypatch.setattr(serverbin, 'install_binary', fake_install_binary)

    ctx.router.run()

    assert [msg for _, msg, _, _ in ctx.env.notifications] == ['STR30063']
    assert ctx.env.dialog_closed_count == 1


# ---------------------------------------------------------------------------
# _install_advancedsettings() (action 'advancedsettings_install')
# ---------------------------------------------------------------------------


def test_advancedsettings_install_success_notifies_restart_message(load_router, monkeypatch):
    ctx = load_router(localized={30066: 'restart Kodi to apply'})
    monkeypatch.setattr(sys, 'argv', ['plugin://x/', '7', '?action=advancedsettings_install'])

    calls = []

    def fake_install(source_path, dest_path):
        calls.append((source_path, dest_path))
        return 'installed'

    monkeypatch.setattr(advancedsettings, 'install', fake_install)

    ctx.router.run()

    assert calls == [(
        '/fake-kodi-home/home/addons/plugin.video.rivulet/resources/advancedsettings.xml',
        '/fake-kodi-home/masterprofile/advancedsettings.xml',
    )]
    assert ctx.env.notifications == [('Rivulet', 'restart Kodi to apply', 'info', 4000)]
    assert ctx.env.end_of_directory == []
    assert ctx.env.resolved == []
    assert ctx.views.calls == []
    assert ctx.player.calls == []


def test_advancedsettings_install_resolves_source_path_from_addon_id_not_hardcoded(load_router, monkeypatch):
    """Regression guard: the source path must be built from the addon's own
    ADDON_ID (so a fork/rename keeps working), never a literal
    'plugin.video.rivulet' string."""
    ctx = load_router(addon_info={'id': 'plugin.video.otherfork'})
    monkeypatch.setattr(sys, 'argv', ['plugin://x/', '7', '?action=advancedsettings_install'])

    calls = []

    def fake_install(source_path, dest_path):
        calls.append((source_path, dest_path))
        return 'installed'

    monkeypatch.setattr(advancedsettings, 'install', fake_install)

    ctx.router.run()

    assert calls == [(
        '/fake-kodi-home/home/addons/plugin.video.otherfork/resources/advancedsettings.xml',
        '/fake-kodi-home/masterprofile/advancedsettings.xml',
    )]


def test_advancedsettings_install_exists_notifies_merge_manually_message(load_router, monkeypatch):
    ctx = load_router(localized={30067: 'already exists - merge manually'})
    monkeypatch.setattr(sys, 'argv', ['plugin://x/', '7', '?action=advancedsettings_install'])

    monkeypatch.setattr(advancedsettings, 'install', lambda source_path, dest_path: 'exists')

    ctx.router.run()

    assert ctx.env.notifications == [('Rivulet', 'already exists - merge manually', 'info', 4000)]
    assert ctx.env.end_of_directory == []
    assert ctx.env.resolved == []


def test_advancedsettings_install_error_notifies_failure_and_logs_error(load_router, monkeypatch):
    ctx = load_router(localized={30068: 'Failed to install advancedsettings.xml'})
    monkeypatch.setattr(sys, 'argv', ['plugin://x/', '7', '?action=advancedsettings_install'])

    def fake_install(source_path, dest_path):
        raise advancedsettings.AdvancedSettingsError('disk full')

    monkeypatch.setattr(advancedsettings, 'install', fake_install)
    xbmc_mod = sys.modules['xbmc']

    ctx.router.run()

    assert ctx.env.notifications == [('Rivulet', 'Failed to install advancedsettings.xml', 'info', 4000)]
    assert any(
        level == xbmc_mod.LOGERROR and 'router: advancedsettings_install failed:' in msg
        for msg, level in ctx.env.log_calls
    )
    assert ctx.env.end_of_directory == []
    assert ctx.env.resolved == []


def test_advancedsettings_install_unwrapped_oserror_is_not_swallowed(load_router, monkeypatch):
    """_install_advancedsettings() only catches
    advancedsettings.AdvancedSettingsError (lib.advancedsettings.install()'s
    documented contract) - a plain OSError escaping install() by mistake
    must still surface via router.run()'s top-level guard instead of
    vanishing silently."""
    ctx = load_router()
    monkeypatch.setattr(sys, 'argv', ['plugin://x/', '7', '?action=advancedsettings_install'])

    def fake_install(source_path, dest_path):
        raise OSError('unexpected raw OSError, not wrapped')

    monkeypatch.setattr(advancedsettings, 'install', fake_install)
    xbmc_mod = sys.modules['xbmc']

    ctx.router.run()

    assert ctx.env.end_of_directory == [
        {'handle': 7, 'succeeded': False, 'updateListing': False, 'cacheToDisc': True}
    ]
    assert ctx.env.resolved == []
    assert [msg for _, msg, _, _ in ctx.env.notifications] == ['STR30032']
    assert any(
        level == xbmc_mod.LOGERROR and 'action "advancedsettings_install" failed' in msg
        for msg, level in ctx.env.log_calls
    )
