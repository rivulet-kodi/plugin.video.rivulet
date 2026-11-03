# SPDX-FileCopyrightText: 2026 M0Rf30
# SPDX-License-Identifier: MIT

"""Once-per-launch Home migration, notice, construction and heartbeat."""
import contextlib
import threading

import pytest

from tests.kodistubs import install_kodi_stubs

_RELOAD_MODULE_NAMES = (
    'lib.ui.compat', 'lib.ui.uicommon', 'lib.ui.router', 'lib.ui.homewindow', 'lib.ui.home',
)


class _FakeVersionStore:
    """Fake `lib.store.Store`: only `get_last_seen_version()`/
    `set_last_seen_version()` matter to `_notify_if_updated()`.
    `raise_on_get` stands in for a Store I/O failure (e.g. a corrupt or
    unwritable `last_version.json`), which `_notify_if_updated()` must
    swallow rather than let take down the home screen."""

    def __init__(self, last_seen=None, raise_on_get=False):
        self._last_seen = last_seen
        self._raise_on_get = raise_on_get
        self.set_calls = []

    def get_last_seen_version(self):
        if self._raise_on_get:
            raise RuntimeError('store unavailable')
        return self._last_seen

    def set_last_seen_version(self, version):
        self.set_calls.append(version)
        self._last_seen = version



@pytest.fixture
def load_homewindow():
    """Reload the launcher and single Home against fresh Kodi stubs.

    Each test gets isolated settings, heartbeat state and notification
    recorders; teardown restores the imported modules and their parents.
    """
    with contextlib.ExitStack() as stack:
        def _load(addon_info=None, localized=None, settings=None):
            return stack.enter_context(install_kodi_stubs(
                reload=_RELOAD_MODULE_NAMES,
                addon_info=addon_info,
                localized=localized,
                settings=settings,
            ))

        yield _load



def test_notify_if_updated_first_run_records_version_and_notifies_nothing(load_homewindow):
    ctx = load_homewindow(addon_info={'version': '1.0.0'})
    store = _FakeVersionStore(last_seen=None)

    ctx.homewindow._notify_if_updated(store)

    assert store.set_calls == ['1.0.0']
    assert ctx.env.notifications == []



def test_notify_if_updated_changed_version_notifies_once_and_records(load_homewindow):
    ctx = load_homewindow(addon_info={'version': '1.1.0'}, localized={30184: 'Updated to version %s'})
    store = _FakeVersionStore(last_seen='1.0.0')

    ctx.homewindow._notify_if_updated(store)

    assert store.set_calls == ['1.1.0']
    assert len(ctx.env.notifications) == 1
    heading, message, icon, time_ms = ctx.env.notifications[0]
    assert message == 'Updated to version 1.1.0'



def test_notify_if_updated_unchanged_version_notifies_nothing(load_homewindow):
    ctx = load_homewindow(addon_info={'version': '1.0.0'})
    store = _FakeVersionStore(last_seen='1.0.0')

    ctx.homewindow._notify_if_updated(store)

    assert store.set_calls == []
    assert ctx.env.notifications == []



def test_notify_if_updated_swallows_store_exception(load_homewindow):
    ctx = load_homewindow(addon_info={'version': '1.0.0'})
    store = _FakeVersionStore(last_seen=None, raise_on_get=True)

    ctx.homewindow._notify_if_updated(store)  # must not raise

    assert ctx.env.notifications == []



def test_open_home_builds_the_window_against_the_skin_path_and_blocks_on_domodal(
    load_homewindow, monkeypatch,
):
    ctx = load_homewindow(addon_info={'path': '/addon/path'})
    captured = {}

    class RecordingWindow(ctx.home.HomeWindow):
        def __init__(self, *args, **kwargs):
            captured['init_args'] = args
            super().__init__(*args, **kwargs)
            captured['instance'] = self

    monkeypatch.setattr(ctx.home, 'HomeWindow', RecordingWindow)

    ctx.homewindow.open_home()

    assert captured['init_args'] == ('HomeWindow.xml', '/addon/path', 'Default', '1080i')
    assert captured['instance'].modal_calls == 1



def test_open_home_closes_the_window_exactly_once_and_reraises_when_domodal_raises(
    load_homewindow, monkeypatch,
):
    ctx = load_homewindow(addon_info={'path': '/addon/path'})
    captured = {}

    class ExplodingWindow(ctx.home.HomeWindow):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.close_calls = 0
            captured['window'] = self

        def close(self):
            self.close_calls += 1
            super().close()

        def doModal(self):
            # Kodi could not enter the modal loop. Build/data failures
            # are handled inside HomeWindow and tested separately.
            raise RuntimeError('onInit blew up')

    monkeypatch.setattr(ctx.home, 'HomeWindow', ExplodingWindow)

    # An open failure is wrapped for default.py's recovery path. The
    # window still closes and the heartbeat clears before it propagates.
    with pytest.raises(ctx.homewindow.HomeOpenError, match='RuntimeError'):
        ctx.homewindow.open_home()

    win = captured['window']
    assert win.close_calls == 1
    assert win.closed is True



def test_open_home_calls_notify_if_updated_with_the_store(load_homewindow, monkeypatch):
    ctx = load_homewindow(addon_info={'path': '/addon/path'})
    store = _FakeVersionStore(last_seen='1.0.0')
    monkeypatch.setattr(ctx.homewindow, 'get_store', lambda: store)
    calls = []
    monkeypatch.setattr(ctx.homewindow, '_notify_if_updated', calls.append)

    ctx.homewindow.open_home()

    assert calls == [store]



def test_open_home_still_opens_the_window_when_the_store_raises(load_homewindow, monkeypatch):
    ctx = load_homewindow(addon_info={'path': '/addon/path'})
    monkeypatch.setattr(ctx.homewindow, 'get_store', lambda: _FakeVersionStore(raise_on_get=True))
    captured = {}

    class RecordingWindow(ctx.home.HomeWindow):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            captured['instance'] = self

    monkeypatch.setattr(ctx.home, 'HomeWindow', RecordingWindow)

    ctx.homewindow.open_home()

    assert captured['instance'].modal_calls == 1
    assert ctx.env.notifications == []



def test_open_home_holds_a_ui_heartbeat_for_the_whole_session_and_clears_it_after(
    load_homewindow, monkeypatch,
):
    ctx = load_homewindow(addon_info={'path': '/addon/path'})
    seen = {}

    class RecordingWindow(ctx.home.HomeWindow):
        def doModal(self):
            # While Home is up a second interpreter must see a live owner.
            seen['alive'] = ctx.uicommon.ui_owner_alive()
            seen['threads'] = [t.name for t in threading.enumerate()]
            super().doModal()

    monkeypatch.setattr(ctx.home, 'HomeWindow', RecordingWindow)

    ctx.homewindow.open_home()

    assert seen['alive'] is True
    assert 'rivulet-ui-heartbeat' in seen['threads']
    # Cleared on exit, thread joined: a later launch is a genuine one.
    assert ctx.uicommon.ui_owner_alive() is False
    assert 'rivulet-ui-heartbeat' not in [t.name for t in threading.enumerate()]
    assert ctx.env.window_properties[10000].get('rivulet.ui.heartbeat') is None



def test_open_home_clears_the_heartbeat_when_the_window_raises(load_homewindow, monkeypatch):
    ctx = load_homewindow(addon_info={'path': '/addon/path'})

    class ExplodingWindow(ctx.home.HomeWindow):
        def doModal(self):
            raise RuntimeError('onInit blew up')

    monkeypatch.setattr(ctx.home, 'HomeWindow', ExplodingWindow)

    with pytest.raises(ctx.homewindow.HomeOpenError, match='RuntimeError'):
        ctx.homewindow.open_home()

    assert ctx.uicommon.ui_owner_alive() is False
    assert 'rivulet-ui-heartbeat' not in [t.name for t in threading.enumerate()]



def test_a_disabled_continue_row_stays_disabled_after_the_rename(load_homewindow):
    """The merged row took a new setting id, so without this anyone who had
    deliberately turned the old row OFF would silently get the new one back
    on."""
    ctx = load_homewindow(settings={'home_show_continue': 'false'})

    ctx.homewindow._migrate_mystuff_setting()

    assert ctx.compat.ADDON.getSetting('home_show_mystuff') == 'false'
    assert ctx.compat.ADDON.getSetting('home_show_continue') == ''



def test_an_enabled_continue_row_does_not_overwrite_the_new_setting(load_homewindow):
    """A true old value carries nothing across - and must not stomp on a
    newer choice.

    The pre-existing `home_show_mystuff` here is what gives the test teeth:
    without it, a migration that wrongly wrote `'true'` would look
    identical to one that correctly did nothing.
    """
    ctx = load_homewindow(settings={
        'home_show_continue': 'true',      # the old row was left on
        'home_show_mystuff': 'false',      # ...but the new one has since been turned off
    })

    ctx.homewindow._migrate_mystuff_setting()

    assert ctx.compat.ADDON.getSetting('home_show_mystuff') == 'false'
    assert ctx.compat.ADDON.getSetting('home_show_continue') == ''



def test_migration_is_idempotent(load_homewindow):
    """Clearing the old key is what makes a second launch a no-op - it must
    not re-apply an old value over a choice the user has since changed."""
    ctx = load_homewindow(settings={'home_show_continue': 'false'})

    ctx.homewindow._migrate_mystuff_setting()
    ctx.compat.ADDON.setSetting('home_show_mystuff', 'true')   # user turns it back on
    ctx.homewindow._migrate_mystuff_setting()

    assert ctx.compat.ADDON.getSetting('home_show_mystuff') == 'true'



def test_migration_survives_a_broken_settings_write(load_homewindow, monkeypatch):
    """A cosmetic migration must never stop Home from opening."""
    ctx = load_homewindow(settings={'home_show_continue': 'false'})

    def _boom(*_args, **_kwargs):
        raise RuntimeError('settings unwritable')

    monkeypatch.setattr(ctx.compat.ADDON, 'setSetting', _boom)

    ctx.homewindow._migrate_mystuff_setting()   # must not raise


def test_layout_migration_runs_after_the_old_continue_rename(load_homewindow, monkeypatch, tmp_path):
    from lib.store import Store

    ctx = load_homewindow(settings={'home_show_continue': 'false', 'home_show_movies': 'false'})
    store = Store(str(tmp_path))
    monkeypatch.setattr(ctx.homewindow, 'get_store', lambda: store)
    ctx.homewindow.open_home()
    layout = store.get_home_layout()
    assert all(not value.startswith('continue') for rows in layout['tabs'].values() for value in rows)
    assert 'new:movie' not in layout['tabs']['home']
    assert layout['tabs']['mystuff'] == ['library:movie', 'library:series']
    ctx.env.addon.settings.update(home_show_mystuff='true', home_show_movies='true')
    ctx.homewindow._migrate_home_layout()
    assert store.get_home_layout() == layout


def test_anime_toggle_never_rewrites_saved_layout_and_absent_keeps_placeholders(load_homewindow, monkeypatch, tmp_path):
    from lib.store import Store

    ctx = load_homewindow(settings={'home_show_anime': 'false'})
    store = Store(str(tmp_path))
    monkeypatch.setattr(ctx.homewindow, 'get_store', lambda: store)
    path = tmp_path / 'home_layout.json'
    saved = b'{"version": 1, "tabs": {"anime": ["cat:t|series|cru"]}}\n'
    path.write_bytes(saved)
    ctx.homewindow._migrate_home_layout()
    assert path.read_bytes() == saved
    path.unlink()
    ctx.homewindow._migrate_home_layout()
    assert 'rest:anime-movie' in store.get_home_layout()['tabs']['anime']
    assert 'rest:anime-series' in store.get_home_layout()['tabs']['anime']
    assert 'continue:anime' in store.get_home_layout()['tabs']['anime']
