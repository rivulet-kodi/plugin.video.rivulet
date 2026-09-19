"""Tests for lib.ui.streamswindow's addon-fetch side of open_streams():
_supported_stream_addons()/_query_addon_streams() aggregation, the
safe-category failure-reason contract (_safe_failure_reason()) that
fixed issue #34's log-noise bug, _summarize_addon_failures()'s cap on
named addons in the single aggregate WARNING line, busy_dialog progress
reporting/cancellation, _fetch_stream_pairs(), and
_start_stream_fetch_workers()'s bounded raw-daemon-thread fan-out. See
test_streamswindow.py for onInit()/rendering coverage and
test_streamswindow_playback.py for onClick()/binge/reopen coverage;
fixtures/fakes are duplicated from that file.
"""
import contextlib
import types

import pytest

from lib.stremio import streaminfo
from lib.stremio.addons import AddonError
from tests.conftest import make_window
from tests.kodistubs import install_kodi_stubs

_RELOAD_MODULE_NAMES = (
    'lib.ui.compat', 'lib.ui.dependencies', 'lib.ui.uicommon', 'lib.ui.dialogs', 'lib.ui.player',
    'lib.ui.streamswindow',
)


class _FakeStore:
    """Fake `lib.store.Store`: only `get_enabled_addons()` matters to open_streams()."""

    def __init__(self, addons=None):
        self._addons = addons or []

    def get_addons(self):
        return self._addons

    def get_enabled_addons(self):
        return [a for a in self._addons if not (a.get('flags') or {}).get('disabled')]


class _FakeAddonClient:
    """Fake `lib.stremio.addons.AddonClient`. `stream_results` maps
    transport_url -> a list of Stream objects, or an Exception instance to
    raise instead (standing in for an addon-request failure). `.calls`
    records every `streams(transport, stype, sid)` invocation."""

    def __init__(self, stream_results):
        self._stream_results = stream_results
        self.calls = []

    def streams(self, transport, stype, sid):
        self.calls.append((transport, stype, sid))
        result = self._stream_results[transport]
        if isinstance(result, Exception):
            raise result
        return result


class _SpoofedCategoryError(Exception):
    """A non-`AddonError` exception that happens to carry a `category`
    attribute of its own - standing in for some future/third-party
    exception type outside `AddonError`'s safe-by-construction contract
    (see `_safe_failure_reason()`'s docstring in lib/ui/streamswindow.py).
    Used below to prove that contract is never extended to an arbitrary
    exception via `hasattr(exc, 'category')` duck-typing."""

    def __init__(self, message, category):
        super().__init__(message)
        self.category = category


@pytest.fixture
def load_streamswindow():
    """Factory fixture: `load_streamswindow(addon_info=None)` installs fresh
    stubs (via tests.kodistubs.install_kodi_stubs) reloading lib.ui.compat/
    lib.ui.uicommon/lib.ui.player/lib.ui.streamswindow, and returns a
    namespace with `.streamswindow`, `.compat`, `.player`, and `.env`. Every
    call is torn down automatically, in reverse order, at test end.
    """
    with contextlib.ExitStack() as stack:
        def _load(addon_info=None):
            return stack.enter_context(install_kodi_stubs(
                reload=_RELOAD_MODULE_NAMES,
                addon_info=addon_info,
            ))

        yield _load


def _wire_data_layer(streamswindow_mod, store, client):
    streamswindow_mod.get_store = lambda: store
    streamswindow_mod.get_client = lambda: client


def _make_window(streamswindow_mod):
    return make_window(streamswindow_mod.StreamsWindow)


# ---------------------------------------------------------------------------
# open_streams()
# ---------------------------------------------------------------------------


def test_open_streams_filters_unsupported_addons_and_forwards_aggregate_to_the_window(
    load_streamswindow, monkeypatch,
):
    ctx = load_streamswindow()
    sw = ctx.streamswindow
    supported = {
        'transportUrl': 't-supported',
        'manifest': {'name': 'Supported', 'resources': ['stream'], 'types': ['movie']},
    }
    unsupported = {
        'transportUrl': 't-unsupported',
        # declares no 'stream' resource at all -> addon_supports() excludes
        # it before any HTTP request is made.
        'manifest': {'name': 'Unsupported', 'resources': ['catalog'], 'types': ['movie']},
    }
    stream = {'url': 'https://a.example/a.mp4'}
    client = _FakeAddonClient({'t-supported': [stream]})
    _wire_data_layer(sw, _FakeStore(addons=[supported, unsupported]), client)
    captured = {}

    class RecordingWindow(sw.StreamsWindow):
        def start(self, pairs, stype, sid, poster=None, heading='', art=None, meta=None, video_id=None):
            captured['args'] = (pairs, stype, sid, poster)
            return True

    monkeypatch.setattr(sw, 'StreamsWindow', RecordingWindow)
    # open_streams() now round-trips after a played start() - stub the wait
    # helper to "no reopen" (as if the user backed out immediately) so this
    # stays a single-iteration test of aggregate forwarding.
    monkeypatch.setattr(sw, '_wait_for_playback_end', lambda *a, **k: (False, False))

    result = sw.open_streams('movie', 'tt1', poster='https://x/poster.jpg')

    assert [call[0] for call in client.calls] == ['t-supported']
    pairs, stype, sid, poster = captured['args']
    assert (stype, sid, poster) == ('movie', 'tt1', 'https://x/poster.jpg')
    assert [s for _info, s in pairs] == [stream]
    assert result is False


def test_supported_stream_addons_skips_disabled_addon(load_streamswindow):
    """A disabled addon stays installed but must never be dispatched for
    streams - `_supported_stream_addons()` fans out over
    `get_enabled_addons()`, not every installed descriptor."""
    ctx = load_streamswindow()
    sw = ctx.streamswindow
    enabled = {
        'transportUrl': 't-enabled',
        'manifest': {'name': 'Enabled', 'resources': ['stream'], 'types': ['movie']},
    }
    disabled = {
        'transportUrl': 't-disabled',
        'manifest': {'name': 'Disabled', 'resources': ['stream'], 'types': ['movie']},
        'flags': {'disabled': True},
    }
    _wire_data_layer(sw, _FakeStore(addons=[enabled, disabled]), _FakeAddonClient({}))

    addons = sw._supported_stream_addons('movie', 'tt1')

    assert [descriptor['transportUrl'] for descriptor, _manifest in addons] == ['t-enabled']


def test_open_streams_forwards_heading_art_and_meta_to_the_window(load_streamswindow, monkeypatch):
    ctx = load_streamswindow()
    sw = ctx.streamswindow
    supported = {
        'transportUrl': 't-supported',
        'manifest': {'name': 'Supported', 'resources': ['stream'], 'types': ['movie']},
    }
    stream = {'url': 'https://a.example/a.mp4'}
    client = _FakeAddonClient({'t-supported': [stream]})
    _wire_data_layer(sw, _FakeStore(addons=[supported]), client)
    captured = {}

    class RecordingWindow(sw.StreamsWindow):
        def start(self, pairs, stype, sid, poster=None, heading='', art=None, meta=None, video_id=None):
            captured['heading'] = heading
            captured['art'] = art
            captured['meta'] = meta
            return True

    monkeypatch.setattr(sw, 'StreamsWindow', RecordingWindow)
    # See test_open_streams_filters_unsupported_addons_and_forwards_aggregate_to_the_window
    # - stub the round-trip wait so a played start() ends the call here.
    monkeypatch.setattr(sw, '_wait_for_playback_end', lambda *a, **k: (False, False))

    result = sw.open_streams(
        'movie', 'tt1', heading='Some Movie',
        art={'poster': 'https://x/p.jpg', 'fanart': 'https://x/f.jpg'},
        meta={'name': 'Some Movie', 'runtime': '90 min'},
    )

    assert result is False
    assert captured['heading'] == 'Some Movie'
    assert captured['art'] == {'poster': 'https://x/p.jpg', 'fanart': 'https://x/f.jpg'}
    assert captured['meta'] == {'name': 'Some Movie', 'runtime': '90 min'}


def test_open_streams_window_is_closed_exactly_once_when_start_raises(load_streamswindow, monkeypatch):
    ctx = load_streamswindow()
    sw = ctx.streamswindow
    supported = {
        'transportUrl': 't-supported',
        'manifest': {'name': 'Supported', 'resources': ['stream'], 'types': ['movie']},
    }
    stream = {'url': 'https://a.example/a.mp4'}
    client = _FakeAddonClient({'t-supported': [stream]})
    _wire_data_layer(sw, _FakeStore(addons=[supported]), client)
    captured = {}

    class ExplodingWindow(sw.StreamsWindow):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.close_calls = 0
            captured['window'] = self

        def close(self):
            self.close_calls += 1
            super().close()

        def start(self, pairs, stype, sid, poster=None, heading='', art=None, meta=None, video_id=None):
            # Stands in for a crash inside onInit()/onAction() while the
            # modal loop is running - self.close() (the window's own,
            # normal-path close) never gets a chance to run.
            raise RuntimeError('onInit blew up')

    monkeypatch.setattr(sw, 'StreamsWindow', ExplodingWindow)

    result = sw.open_streams('movie', 'tt1')

    assert result is False
    win = captured['window']
    assert win.close_calls == 1
    assert win.closed is True
    assert ctx.env.notifications == [('Rivulet', 'STR30032', 'info', 4000)]


def test_open_streams_addonerror_is_logged_and_skipped_not_fatal(load_streamswindow, monkeypatch):
    ctx = load_streamswindow()
    import xbmc
    sw = ctx.streamswindow
    failing_transport = 'https://fail.example/manifest.json'
    ok_transport = 'https://ok.example/manifest.json'
    failing = {
        'transportUrl': failing_transport,
        'manifest': {'name': 'Failing', 'resources': ['stream'], 'types': ['movie']},
    }
    working = {
        'transportUrl': ok_transport,
        'manifest': {'name': 'Working', 'resources': ['stream'], 'types': ['movie']},
    }
    ok_stream = {'url': 'https://a.example/a.mp4'}
    client = _FakeAddonClient({failing_transport: AddonError('upstream down'), ok_transport: [ok_stream]})
    _wire_data_layer(sw, _FakeStore(addons=[failing, working]), client)
    captured = {}

    class RecordingWindow(sw.StreamsWindow):
        def start(self, pairs, stype, sid, poster=None, heading='', art=None, meta=None, video_id=None):
            captured['pairs'] = pairs
            return True

    monkeypatch.setattr(sw, 'StreamsWindow', RecordingWindow)
    # Not testing the round-trip here - stub it away (see
    # test_open_streams_filters_unsupported_addons_and_forwards_aggregate_to_the_window).
    monkeypatch.setattr(sw, '_wait_for_playback_end', lambda *a, **k: (False, False))

    result = sw.open_streams('movie', 'tt1')

    assert result is False
    assert [call[0] for call in client.calls] == [failing_transport, ok_transport]
    assert [s for _info, s in captured['pairs']] == [ok_stream]
    # The failing addon must never hit ERROR (that was the noisy old
    # behavior) - one DEBUG line naming its safe scheme+host, plus exactly
    # one aggregate WARNING summarizing the fetch, and nothing else at
    # WARNING/ERROR. The exception's own text ('upstream down') and the
    # transport's path (manifest.json) are never logged - only
    # safe_url_for_log()'s scheme+host and the exception's class name.
    assert not [lvl for _msg, lvl in ctx.env.log_calls if lvl == xbmc.LOGERROR]
    debug_msgs = [msg for msg, lvl in ctx.env.log_calls if lvl == xbmc.LOGDEBUG]
    assert any('fail.example' in msg and 'AddonError' in msg for msg in debug_msgs)
    assert not any('upstream down' in msg or 'manifest.json' in msg for msg in debug_msgs)
    warnings = [msg for msg, lvl in ctx.env.log_calls if lvl == xbmc.LOGWARNING]
    assert len(warnings) == 1
    assert warnings[0] == '[plugin.video.rivulet] streamswindow: 1 addon(s) failed: Failing (AddonError)'


def test_query_addon_streams_logs_addon_error_category_at_debug(load_streamswindow):
    """The reported bug (issue #34): a stale debrid key surfaces as HTTP
    401 from `_get_json`, but the old log line collapsed every distinct
    failure to the bare literal "AddonError". `addon_error_detail()`
    carries the safe category through, so the DEBUG line now
    distinguishes 401 from 404/ConnectionError/etc. without touching
    `str(exc)` (still never logged - see the credential-leak guard
    above)."""
    ctx = load_streamswindow()
    import xbmc
    sw = ctx.streamswindow
    transport = 'https://stremio.torbox.app/manifest.json'
    client = _FakeAddonClient({
        transport: AddonError('GET %s failed: HTTP 401' % transport, category='HTTP 401'),
    })

    pairs, failed, reason = sw._query_addon_streams(client, transport, 'TorBox', 'movie', 'tt1')

    assert failed is True
    assert reason == 'HTTP 401'
    assert pairs == []
    debug_msgs = [msg for msg, lvl in ctx.env.log_calls if lvl == xbmc.LOGDEBUG]
    assert any('TorBox' in msg and 'stremio.torbox.app' in msg and 'HTTP 401' in msg for msg in debug_msgs)
    assert not any('manifest.json' in msg for msg in debug_msgs)


def test_query_addon_streams_never_trusts_a_category_attribute_off_a_non_addonerror_exception(
    load_streamswindow,
):
    """The safe-category contract documented on `_safe_failure_reason()`
    holds ONLY for `AddonError.category` - it is populated exclusively
    by `_request_error_category()`/fixed literals at `AddonError` raise
    sites, never by arbitrary code. A `category` attribute on any OTHER
    exception type carries no such guarantee: nothing stops it from
    being a live credential. `_query_addon_streams()` must fall back to
    the exception's bare type name for such an exception instead of
    duck-typing on `hasattr(exc, 'category')`, and that fallback must
    never touch the log at any level."""
    ctx = load_streamswindow()
    sw = ctx.streamswindow
    transport = 'https://evil.example/manifest.json'
    secret = 'sk_live_51H8xJ2eZvKYlo2C'
    client = _FakeAddonClient({
        transport: _SpoofedCategoryError('boom', category=secret),
    })

    pairs, failed, reason = sw._query_addon_streams(client, transport, 'Spoofed', 'movie', 'tt1')

    assert failed is True
    assert pairs == []
    assert reason == '_SpoofedCategoryError'
    all_messages = ' '.join(msg for msg, _level in ctx.env.log_calls)
    assert secret not in all_messages


def test_open_streams_aggregate_warning_names_a_spoofed_category_addon_by_type_not_by_secret(
    load_streamswindow, monkeypatch,
):
    """End-to-end companion to the unit test above: the aggregate
    WARNING - the ONLY failure detail a default-log-level user ever
    sees - must name a failing addon by its exception's bare type name
    when that exception is not an `AddonError`, never by a `category`
    attribute duck-typed off it, even when that attribute holds a
    credential-shaped secret."""
    ctx = load_streamswindow()
    import xbmc
    sw = ctx.streamswindow
    transport = 'https://evil.example/manifest.json'
    secret = 'sk_live_51H8xJ2eZvKYlo2C'
    addon = {
        'transportUrl': transport,
        'manifest': {'name': 'Spoofed', 'resources': ['stream'], 'types': ['movie']},
    }
    client = _FakeAddonClient({transport: _SpoofedCategoryError('boom', category=secret)})
    _wire_data_layer(sw, _FakeStore(addons=[addon]), client)
    monkeypatch.setattr(sw, '_wait_for_playback_end', lambda *a, **k: (False, False))

    result = sw.open_streams('movie', 'tt1')

    assert result is False  # the only addon failed -> no streams at all
    all_messages = ' '.join(msg for msg, _level in ctx.env.log_calls)
    assert secret not in all_messages
    warnings = [msg for msg, lvl in ctx.env.log_calls if lvl == xbmc.LOGWARNING]
    assert warnings == [
        '[plugin.video.rivulet] streamswindow: 1 addon(s) failed: Spoofed (_SpoofedCategoryError)',
    ]


def test_open_streams_multiple_addon_failures_still_log_a_single_aggregate_warning(
    load_streamswindow, monkeypatch,
):
    ctx = load_streamswindow()
    import xbmc
    sw = ctx.streamswindow
    fail_a_transport = 'https://fail-a.example/manifest.json'
    fail_b_transport = 'https://fail-b.example/manifest.json'
    ok_transport = 'https://ok.example/manifest.json'
    fail_a = {
        'transportUrl': fail_a_transport,
        'manifest': {'name': 'FailA', 'resources': ['stream'], 'types': ['movie']},
    }
    fail_b = {
        'transportUrl': fail_b_transport,
        'manifest': {'name': 'FailB', 'resources': ['stream'], 'types': ['movie']},
    }
    working = {
        'transportUrl': ok_transport,
        'manifest': {'name': 'Working', 'resources': ['stream'], 'types': ['movie']},
    }
    ok_stream = {'url': 'https://a.example/a.mp4'}
    client = _FakeAddonClient({
        fail_a_transport: AddonError('boom a'), fail_b_transport: AddonError('boom b'), ok_transport: [ok_stream],
    })
    _wire_data_layer(sw, _FakeStore(addons=[fail_a, fail_b, working]), client)

    class RecordingWindow(sw.StreamsWindow):
        def start(self, pairs, stype, sid, poster=None, heading='', art=None, meta=None, video_id=None):
            return True

    monkeypatch.setattr(sw, 'StreamsWindow', RecordingWindow)
    monkeypatch.setattr(sw, '_wait_for_playback_end', lambda *a, **k: (False, False))

    result = sw.open_streams('movie', 'tt1')

    assert result is False
    debug_msgs = [msg for msg, lvl in ctx.env.log_calls if lvl == xbmc.LOGDEBUG]
    assert sum(1 for msg in debug_msgs if 'fail-a.example' in msg) == 1
    assert sum(1 for msg in debug_msgs if 'fail-b.example' in msg) == 1
    assert not any('boom a' in msg or 'boom b' in msg for msg in debug_msgs)
    warnings = [msg for msg, lvl in ctx.env.log_calls if lvl == xbmc.LOGWARNING]
    assert len(warnings) == 1
    warning = warnings[0]
    assert warning.startswith('[plugin.video.rivulet] streamswindow: 2 addon(s) failed: ')
    assert 'FailA (AddonError)' in warning
    assert 'FailB (AddonError)' in warning
    assert not [lvl for _msg, lvl in ctx.env.log_calls if lvl == xbmc.LOGERROR]


def test_open_streams_survives_an_addon_whose_payload_breaks_parsing(
    load_streamswindow, monkeypatch,
):
    """A stream resource is arbitrary third-party JSON: a body that is a
    bare list (`.get` -> AttributeError) or an item hostile enough to
    break `parse_stream` must be reported as that ONE addon's failure,
    never escape `_query_addon_streams` - it runs as a worker-thread
    body, and an exception there kills the thread before it queues any
    result, wedging the consumer on an answer that never comes (the
    silent-drop shape behind issue #32). The healthy addon's own streams
    must still reach the window."""
    ctx = load_streamswindow()
    import xbmc
    sw = ctx.streamswindow
    bad_transport = 'https://bad.example/manifest.json'
    ok_transport = 'https://ok.example/manifest.json'
    bad = {
        'transportUrl': bad_transport,
        'manifest': {'name': 'Bad', 'resources': ['stream'], 'types': ['movie']},
    }
    working = {
        'transportUrl': ok_transport,
        'manifest': {'name': 'Working', 'resources': ['stream'], 'types': ['movie']},
    }
    ok_stream = {'url': 'https://a.example/a.mp4'}
    captured = {}

    def exploding_parse(stream, addon_name=None):
        if addon_name == 'Bad':
            raise TypeError('hostile payload')
        return {'addon': addon_name, 'title': 'ok'}

    client = _FakeAddonClient({bad_transport: [{'infoHash': 'x'}], ok_transport: [ok_stream]})
    _wire_data_layer(sw, _FakeStore(addons=[bad, working]), client)
    monkeypatch.setattr(sw.streaminfo, 'parse_stream', exploding_parse)

    class RecordingWindow(sw.StreamsWindow):
        def start(self, pairs, stype, sid, poster=None, heading='', art=None, meta=None, video_id=None):
            captured['pairs'] = pairs
            return True

    monkeypatch.setattr(sw, 'StreamsWindow', RecordingWindow)
    monkeypatch.setattr(sw, '_wait_for_playback_end', lambda *a, **k: (False, False))

    assert sw.open_streams('movie', 'tt1') is False
    assert [s for _info, s in captured['pairs']] == [ok_stream]
    errors = [msg for msg, lvl in ctx.env.log_calls if lvl == xbmc.LOGERROR]
    assert any('Bad' in msg and 'bad.example' in msg and 'TypeError' in msg for msg in errors)
    assert not any('hostile payload' in msg or 'manifest.json' in msg for msg in errors)
    warnings = [msg for msg, lvl in ctx.env.log_calls if lvl == xbmc.LOGWARNING]
    assert len(warnings) == 1 and warnings[0] == '[plugin.video.rivulet] streamswindow: 1 addon(s) failed: Bad (TypeError)'


def test_open_streams_addon_failure_log_never_leaks_credentials_path_or_query(load_streamswindow):
    ctx = load_streamswindow()
    import xbmc
    sw = ctx.streamswindow
    secret_transport = 'https://user:hunter2@evil.example:8443/private/path/manifest.json?token=abc123'
    failing = {
        'transportUrl': secret_transport,
        'manifest': {'name': 'Failing', 'resources': ['stream'], 'types': ['movie']},
    }
    client = _FakeAddonClient({
        secret_transport: AddonError('GET %s failed: bad request' % secret_transport),
    })
    _wire_data_layer(sw, _FakeStore(addons=[failing]), client)

    result = sw.open_streams('movie', 'tt1')

    assert result is False  # the only addon failed -> no streams at all
    all_messages = ' '.join(msg for msg, _level in ctx.env.log_calls)
    assert 'hunter2' not in all_messages
    assert 'token=abc123' not in all_messages
    assert '/private/path' not in all_messages
    warnings = [msg for msg, lvl in ctx.env.log_calls if lvl == xbmc.LOGWARNING]
    assert warnings == ['[plugin.video.rivulet] streamswindow: 1 addon(s) failed: Failing (AddonError)']
    debug_msgs = [msg for msg, lvl in ctx.env.log_calls if lvl == xbmc.LOGDEBUG]
    assert any('evil.example:8443' in msg for msg in debug_msgs)
    assert all('\n' not in msg and '\r' not in msg for msg in debug_msgs)


def test_open_streams_aggregate_warning_names_each_failing_addon_with_its_safe_category(
    load_streamswindow, monkeypatch,
):
    """The visibility fix for issue #34: a user on Kodi's default log
    level never sees the per-addon DEBUG lines at all (their own log
    showed "Disabled debug logging due to GUI setting"), so this
    single WARNING line is the ONLY failure detail such a user can
    ever hand a maintainer - it must name each failing addon plus its
    safe category/exception type, not just a bare count."""
    ctx = load_streamswindow()
    import xbmc
    sw = ctx.streamswindow
    torbox_transport = 'https://torbox.example/manifest.json'
    torrentio_transport = 'https://torrentio.example/manifest.json'
    local_transport = 'https://local.example/manifest.json'
    torbox = {
        'transportUrl': torbox_transport,
        'manifest': {'name': 'TorBox', 'resources': ['stream'], 'types': ['movie']},
    }
    torrentio = {
        'transportUrl': torrentio_transport,
        'manifest': {'name': 'Torrentio PM', 'resources': ['stream'], 'types': ['movie']},
    }
    local = {
        'transportUrl': local_transport,
        'manifest': {'name': 'Local Files', 'resources': ['stream'], 'types': ['movie']},
    }
    client = _FakeAddonClient({
        torbox_transport: AddonError(
            'GET %s failed: HTTP 401' % torbox_transport, category='HTTP 401'),
        torrentio_transport: AddonError(
            'GET %s failed: HTTP 403' % torrentio_transport, category='HTTP 403'),
        local_transport: ConnectionError('refused'),
    })
    _wire_data_layer(sw, _FakeStore(addons=[torbox, torrentio, local]), client)
    monkeypatch.setattr(sw, '_wait_for_playback_end', lambda *a, **k: (False, False))

    result = sw.open_streams('movie', 'tt1')

    assert result is False  # every addon failed -> no streams at all
    warnings = [msg for msg, lvl in ctx.env.log_calls if lvl == xbmc.LOGWARNING]
    assert len(warnings) == 1
    warning = warnings[0]
    assert warning.startswith('[plugin.video.rivulet] streamswindow: 3 addon(s) failed: ')
    assert 'TorBox (HTTP 401)' in warning
    assert 'Torrentio PM (HTTP 403)' in warning
    assert 'Local Files (ConnectionError)' in warning


def test_summarize_addon_failures_caps_named_addons_and_folds_the_remainder(load_streamswindow):
    """More failing addons than `_MAX_NAMED_ADDON_FAILURES` must not
    grow the single aggregate WARNING line without bound (a user with
    a genuinely broken install could have dozens of dead addons) - the
    remainder past the cap is folded into one trailing '+N more'."""
    ctx = load_streamswindow()
    sw = ctx.streamswindow
    cap = sw._MAX_NAMED_ADDON_FAILURES
    failures = [('Addon%02d' % i, 'HTTP 500') for i in range(cap + 2)]

    summary = sw._summarize_addon_failures(failures)

    assert summary.startswith('%d addon(s) failed: ' % len(failures))
    assert summary.endswith(', +2 more')
    assert summary.count('(HTTP 500)') == cap  # only the cap's worth are named individually


def test_open_streams_aggregate_warning_caps_named_addons_when_more_fail_than_the_cap(
    load_streamswindow, monkeypatch,
):
    """Same cap, exercised end to end through `open_streams()`'s own
    fan-out rather than calling the summarizer directly."""
    ctx = load_streamswindow()
    import xbmc
    sw = ctx.streamswindow
    cap = sw._MAX_NAMED_ADDON_FAILURES
    addon_count = cap + 2
    addons = []
    stream_results = {}
    for i in range(addon_count):
        transport = 'https://fail%02d.example/manifest.json' % i
        addons.append({
            'transportUrl': transport,
            'manifest': {'name': 'Addon%02d' % i, 'resources': ['stream'], 'types': ['movie']},
        })
        stream_results[transport] = AddonError('boom', category='HTTP 500')
    client = _FakeAddonClient(stream_results)
    _wire_data_layer(sw, _FakeStore(addons=addons), client)
    monkeypatch.setattr(sw, '_wait_for_playback_end', lambda *a, **k: (False, False))

    result = sw.open_streams('movie', 'tt1')

    assert result is False
    warnings = [msg for msg, lvl in ctx.env.log_calls if lvl == xbmc.LOGWARNING]
    assert len(warnings) == 1
    warning = warnings[0]
    assert warning.startswith('[plugin.video.rivulet] streamswindow: %d addon(s) failed: ' % addon_count)
    assert warning.endswith(', +2 more')
    assert warning.count('(HTTP 500)') == cap


def test_open_streams_no_results_notifies_and_returns_false_without_building_a_window(
    load_streamswindow, monkeypatch,
):
    ctx = load_streamswindow()
    sw = ctx.streamswindow
    descriptor = {
        'transportUrl': 't1',
        'manifest': {'resources': ['stream'], 'types': ['movie']},
    }
    _wire_data_layer(sw, _FakeStore(addons=[descriptor]), _FakeAddonClient({'t1': []}))

    def _unexpected(*a, **k):
        raise AssertionError('StreamsWindow must never be constructed on an empty aggregate')

    monkeypatch.setattr(sw, 'StreamsWindow', _unexpected)

    result = sw.open_streams('movie', 'tt1')

    assert result is False
    assert ctx.env.notifications == [('Rivulet', 'STR30030', 'info', 4000)]


def test_open_streams_reads_stream_sort_setting_and_applies_it_to_final_order(load_streamswindow, monkeypatch):
    ctx = load_streamswindow()
    sw = ctx.streamswindow
    hi_res_low_seeds = {'id': 'hi-res'}
    lo_res_hi_seeds = {'id': 'lo-res'}
    descriptor = {
        'transportUrl': 't1',
        'manifest': {'resources': ['stream'], 'types': ['movie']},
    }
    _wire_data_layer(
        sw, _FakeStore(addons=[descriptor]),
        _FakeAddonClient({'t1': [hi_res_low_seeds, lo_res_hi_seeds]}),
    )

    def fake_parse_stream(stream, addon_name=''):
        if stream is hi_res_low_seeds:
            return {'resolution': '2160p', 'seeders': 1, 'size_bytes': 100}
        return {'resolution': '480p', 'seeders': 999, 'size_bytes': 100}

    monkeypatch.setattr(streaminfo, 'parse_stream', fake_parse_stream)
    captured = {}

    class RecordingWindow(sw.StreamsWindow):
        def start(self, pairs, stype, sid, poster=None, heading='', art=None, meta=None, video_id=None):
            captured['pairs'] = pairs
            return True

    monkeypatch.setattr(sw, 'StreamsWindow', RecordingWindow)
    # A played start() would otherwise round-trip forever (RecordingWindow
    # always returns True) - stub it away; this test only cares about sort
    # order, not the round-trip loop.
    monkeypatch.setattr(sw, '_wait_for_playback_end', lambda *a, **k: (False, False))

    # Default setting ('' -> 'quality'): resolution tier wins over seeders.
    sw.open_streams('movie', 'tt1')
    assert [s for _info, s in captured['pairs']] == [hi_res_low_seeds, lo_res_hi_seeds]

    # An explicit 'seeders' setting must flip the order for the SAME inputs.
    ctx.env.addon.settings['stream_sort'] = 'seeders'
    sw.open_streams('movie', 'tt1')
    assert [s for _info, s in captured['pairs']] == [lo_res_hi_seeds, hi_res_low_seeds]


# ---------------------------------------------------------------------------
# open_streams() - busy_dialog progress reporting/cancellation
# ---------------------------------------------------------------------------


def _cancel_after(n):
    """Builds a zero-arg closure for a scripted `iscanceled()` check that
    reports cancelled (True) starting from its (n+1)th call onward -
    same no-arg call convention `RivuletBusy.iscanceled()` itself uses
    (unlike `Monitor.waitForAbort()`'s 1-based-count-arg convention)."""
    state = {'calls': 0}

    def _check():
        state['calls'] += 1
        return state['calls'] > n
    return _check


def _record_busy_calls(monkeypatch, dialogs_mod):
    """Monkeypatches `lib.ui.dialogs.RivuletBusy`'s create()/update()/
    close() to record calls in the same (heading, message)/(percent,
    message)/count shape the old `xbmcgui.DialogProgress` fake exposed
    as `env.dialog_created`/`dialog_updates`/`dialog_closed_count`,
    while still delegating to the real implementation so the fetch
    loop's dialog is genuinely created/updated/closed against the fake
    window/controls too. Mirrors test_router.py's `_record_progress_calls`."""
    calls = types.SimpleNamespace(created=[], updated=[], closed=0)
    orig_create = dialogs_mod.RivuletBusy.create
    orig_update = dialogs_mod.RivuletBusy.update
    orig_close = dialogs_mod.RivuletBusy.close

    def create(self, heading, message=''):
        calls.created.append((heading, message))
        return orig_create(self, heading, message)

    def update(self, percent, message='', attempt='', stats=''):
        calls.updated.append((percent, message))
        return orig_update(self, percent, message, attempt, stats)

    def close(self):
        calls.closed += 1
        return orig_close(self)

    monkeypatch.setattr(dialogs_mod.RivuletBusy, 'create', create)
    monkeypatch.setattr(dialogs_mod.RivuletBusy, 'update', update)
    monkeypatch.setattr(dialogs_mod.RivuletBusy, 'close', close)
    return calls


def test_open_streams_busy_dialog_reports_progress_and_skips_unsupported_addons(
    load_streamswindow, monkeypatch,
):
    ctx = load_streamswindow()
    sw = ctx.streamswindow
    alpha = {
        'transportUrl': 't-alpha',
        'manifest': {'name': 'Alpha', 'resources': ['stream'], 'types': ['movie']},
    }
    unsupported = {
        'transportUrl': 't-unsupported',
        # no 'stream' resource -> excluded before total_addons is even computed.
        'manifest': {'name': 'Unsupported', 'resources': ['catalog'], 'types': ['movie']},
    }
    alpha_stream = {'url': 'https://a.example/a.mp4'}
    client = _FakeAddonClient({'t-alpha': [alpha_stream]})
    _wire_data_layer(sw, _FakeStore(addons=[alpha, unsupported]), client)
    captured = {}

    class RecordingWindow(sw.StreamsWindow):
        def start(self, pairs, stype, sid, poster=None, heading='', art=None, meta=None, video_id=None):
            captured['pairs'] = pairs
            return True

    monkeypatch.setattr(sw, 'StreamsWindow', RecordingWindow)
    monkeypatch.setattr(sw, '_wait_for_playback_end', lambda *a, **k: (False, False))
    busy = _record_busy_calls(monkeypatch, ctx.dialogs)

    result = sw.open_streams('movie', 'tt1')

    assert result is False
    assert [call[0] for call in client.calls] == ['t-alpha']  # the unsupported addon is never even queried
    assert [s for _info, s in captured['pairs']] == [alpha_stream]
    assert busy.created == [('STR30033', '')]
    # total_addons is 1 (the unsupported addon never counts toward the
    # denominator) - one 'Checking Alpha...' update at 100%, on top of
    # busy_dialog's own initial update(0, message) on entry.
    assert busy.updated == [
        (0, ''),
        (100, 'Checking Alpha...'),
    ]
    assert busy.closed == 1


def test_open_streams_cancelled_while_still_waiting_for_a_non_empty_result_falls_back_to_no_results(
    load_streamswindow, monkeypatch,
):
    """Every addon queried concurrently now fires its own HTTP call
    immediately regardless of cancellation (unlike the old serial loop,
    which could skip an addon it never reached) - so cancellation can no
    longer stop an addon from being QUERIED, only stop open_streams()
    from continuing to WAIT for a non-empty result. Two addons that both
    answer empty force the wait loop to actually check
    `dialog.iscanceled()` more than once before giving up."""
    ctx = load_streamswindow()
    sw = ctx.streamswindow
    empty_a = {
        'transportUrl': 't-empty-a',
        'manifest': {'name': 'A', 'resources': ['stream'], 'types': ['movie']},
    }
    empty_b = {
        'transportUrl': 't-empty-b',
        'manifest': {'name': 'B', 'resources': ['stream'], 'types': ['movie']},
    }
    client = _FakeAddonClient({'t-empty-a': [], 't-empty-b': []})
    _wire_data_layer(sw, _FakeStore(addons=[empty_a, empty_b]), client)
    # RivuletBusy.iscanceled() is checked by the REAL wait loop inside
    # _fetch_stream_pairs()/open_streams() (not mocked here) - it has no
    # BACK_ACTIONS onAction() to drive since the dialog is created
    # internally, so the scripted check is patched onto the class
    # itself instead, same as test_router.py's mid-download cancel test.
    _scripted_cancel = _cancel_after(1)
    monkeypatch.setattr(ctx.dialogs.RivuletBusy, 'iscanceled', lambda self: _scripted_cancel())
    busy = _record_busy_calls(monkeypatch, ctx.dialogs)

    def _unexpected(*a, **k):
        raise AssertionError('StreamsWindow must never be constructed when the wait is cancelled with nothing found')

    monkeypatch.setattr(sw, 'StreamsWindow', _unexpected)

    result = sw.open_streams('movie', 'tt1')

    assert result is False
    assert ctx.env.notifications == [('Rivulet', 'STR30030', 'info', 4000)]
    assert busy.closed == 1


def test_open_streams_cancelled_before_first_addon_falls_back_to_no_results(
    load_streamswindow, monkeypatch,
):
    ctx = load_streamswindow()
    sw = ctx.streamswindow
    descriptor = {
        'transportUrl': 't1',
        'manifest': {'name': 'Alpha', 'resources': ['stream'], 'types': ['movie']},
    }
    client = _FakeAddonClient({'t1': [{'url': 'https://a.example/a.mp4'}]})
    _wire_data_layer(sw, _FakeStore(addons=[descriptor]), client)
    # Already cancelled before the wait ever starts - see the comment on
    # the scripted RivuletBusy.iscanceled() patch above.
    monkeypatch.setattr(ctx.dialogs.RivuletBusy, 'iscanceled', lambda self: True)
    busy = _record_busy_calls(monkeypatch, ctx.dialogs)

    def _unexpected(*a, **k):
        raise AssertionError('StreamsWindow must never be constructed on an empty aggregate')

    monkeypatch.setattr(sw, 'StreamsWindow', _unexpected)

    result = sw.open_streams('movie', 'tt1')

    # Every addon is now submitted to the fan-out CONCURRENTLY, before
    # open_streams() ever checks cancellation - unlike the old serial
    # loop, which gated each addon's own HTTP call behind that same
    # check, cancelling before the first addon can no longer prevent it
    # from being queried. What it DOES still guarantee is the same
    # user-visible outcome: no window, and the "no results" notification.
    assert result is False
    assert ctx.env.notifications == [('Rivulet', 'STR30030', 'info', 4000)]
    assert busy.closed == 1


def test_fetch_stream_pairs_aggregates_every_addon_and_logs_a_single_warning_on_failure(
    load_streamswindow,
):
    ctx = load_streamswindow()
    import xbmc
    sw = ctx.streamswindow
    failing_transport = 'https://fail.example/manifest.json'
    ok_transport = 'https://ok.example/manifest.json'
    failing = {
        'transportUrl': failing_transport,
        'manifest': {'name': 'Failing', 'resources': ['stream'], 'types': ['movie']},
    }
    working = {
        'transportUrl': ok_transport,
        'manifest': {'name': 'Working', 'resources': ['stream'], 'types': ['movie']},
    }
    ok_stream = {'url': 'https://a.example/a.mp4'}
    client = _FakeAddonClient({failing_transport: AddonError('upstream down'), ok_transport: [ok_stream]})
    _wire_data_layer(sw, _FakeStore(addons=[failing, working]), client)

    pairs = sw._fetch_stream_pairs('movie', 'tt1')

    assert [s for _info, s in pairs] == [ok_stream]
    assert sorted(call[0] for call in client.calls) == [failing_transport, ok_transport]
    warnings = [msg for msg, lvl in ctx.env.log_calls if lvl == xbmc.LOGWARNING]
    assert warnings == ['[plugin.video.rivulet] streamswindow: 1 addon(s) failed: Failing (AddonError)']


# ---------------------------------------------------------------------------
# _start_stream_fetch_workers() - the bounded raw-daemon-thread fan-out
# both _fetch_stream_pairs() and open_streams() feed from. Regression
# coverage for the defect this whole helper replaces
# ThreadPoolExecutor to fix: concurrent.futures.thread's atexit hook
# JOINS every worker at interpreter shutdown regardless of daemon flag,
# so a still-running addon fetch blocked process exit for 6.0s on both
# Python 3.8 and 3.13 even with pool.shutdown(wait=False). Raw daemon
# threads have no such hook - which only holds if every thread this
# helper starts is ACTUALLY daemon, and there are never more of them
# than _MAX_STREAM_ADDON_WORKERS regardless of how many addons are fed
# in - both asserted directly here rather than through a real interpreter
# exit (which this suite has no way to observe).
# ---------------------------------------------------------------------------


def _spy_on_threads(monkeypatch, streamswindow_mod):
    """Wraps `streamswindow_mod.threading.Thread` so every instance it
    constructs (not just `.start()`ed ones) is recorded, and returns the
    list those instances land in - the deterministic "threads the helper
    creates" this file's daemon-thread/bounded-pool tests inspect,
    instead of the process-wide (and thus test-order-sensitive)
    `threading.enumerate()`."""
    created = []
    real_thread = streamswindow_mod.threading.Thread

    def _make_thread(*args, **kwargs):
        thread = real_thread(*args, **kwargs)
        created.append(thread)
        return thread

    monkeypatch.setattr(streamswindow_mod.threading, 'Thread', _make_thread)
    return created


def test_start_stream_fetch_workers_starts_only_daemon_threads(load_streamswindow, monkeypatch):
    ctx = load_streamswindow()
    sw = ctx.streamswindow
    addons = [
        ({'transportUrl': 't0'}, {'name': 'A0'}),
        ({'transportUrl': 't1'}, {'name': 'A1'}),
        ({'transportUrl': 't2'}, {'name': 'A2'}),
    ]
    client = _FakeAddonClient({'t0': [], 't1': [], 't2': []})
    sw.get_client = lambda: client
    created = _spy_on_threads(monkeypatch, sw)

    results = sw._start_stream_fetch_workers('movie', 'tt1', addons)

    for _ in addons:
        results.get(timeout=2)  # drain every answer so no worker outlives the test
    for thread in created:
        thread.join(2)

    assert len(created) == len(addons)
    assert all(thread.daemon for thread in created)


def test_start_stream_fetch_workers_never_starts_more_threads_than_the_worker_cap(
    load_streamswindow, monkeypatch,
):
    ctx = load_streamswindow()
    sw = ctx.streamswindow
    addon_count = sw._MAX_STREAM_ADDON_WORKERS + 5
    stream_results = {'t%d' % i: [] for i in range(addon_count)}
    addons = [({'transportUrl': 't%d' % i}, {'name': 'A%d' % i}) for i in range(addon_count)]
    client = _FakeAddonClient(stream_results)
    sw.get_client = lambda: client
    created = _spy_on_threads(monkeypatch, sw)

    results = sw._start_stream_fetch_workers('movie', 'tt1', addons)

    for _ in addons:
        results.get(timeout=2)  # drain every answer so no worker outlives the test
    for thread in created:
        thread.join(2)

    assert len(created) == sw._MAX_STREAM_ADDON_WORKERS
    assert all(thread.daemon for thread in created)
    assert len(client.calls) == addon_count  # every addon still got queried, just via a bounded pool


