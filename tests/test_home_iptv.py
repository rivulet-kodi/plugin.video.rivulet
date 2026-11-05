# SPDX-FileCopyrightText: 2026 M0Rf30
# SPDX-License-Identifier: MIT

"""Changing the IPTV playlists in Settings reloads Home's Live rows.

The engine numbers playlist catalogs by position (`iptv0`), so a new URL in
slot 0 is the same catalog id at the same transport. Home's row signature
therefore carries the playlist stamp `lib.iptv` puts on the published
manifest, and, because the entry is republished only once the restarted
engine answers (after Settings has closed), a bounded daemon waits for it
and wakes the GUI thread to rebuild.
"""
import time

from lib import iptv
from tests.test_home import _FakeStore
from tests.test_home import load_homewindow as load_homewindow  # noqa: F401 - fixture
from tests.test_home_tabs import addons, build

OLD = 'http://old/list.m3u'
NEW = 'http://new/list.m3u'
ENGINE = 'http://127.0.0.1:11470/iptv/manifest.json'


def _entry(playlists):
    manifest = {'id': iptv.MANIFEST_ID, 'name': 'IPTV', 'types': ['tv'],
                'catalogs': [{'type': 'tv', 'id': 'iptv0', 'name': 'Live'}]}
    if playlists:
        manifest[iptv.STAMP_FIELD] = iptv.playlists_stamp(playlists)
    return {'transportUrl': ENGINE, 'manifest': manifest,
            'flags': {'builtin': iptv.BUILTIN_ID, 'protected': True}}


def _home(ctx, monkeypatch, published):
    installed = addons() + ([_entry(published)] if published else [])
    win, store, _calls = build(ctx, monkeypatch, installed=installed)
    monkeypatch.setattr(ctx.home, '_IPTV_POLL_SECONDS', 0.01)
    return win, store


def _wait_until(predicate, limit=5.0):
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


def _publish(store, playlists):
    store._addons = addons() + [_entry(playlists)]


def test_signature_changes_with_the_playlist_stamp_only(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    store = _FakeStore(addons=addons() + [_entry(OLD)])
    before = ctx.home._home_signature(store)
    assert ctx.home._home_signature(store) == before
    store._addons = addons() + [_entry(NEW)]
    assert ctx.home._home_signature(store) != before
    store._addons = addons()
    assert ctx.home._home_signature(store) != before


def test_the_signature_of_an_unstamped_addon_is_its_transport(load_homewindow):
    ctx = load_homewindow()
    signature = ctx.home._home_signature(_FakeStore(addons=addons()))
    assert signature[1] == ('https://addon/manifest.json',)


def test_a_late_republish_rebuilds_home_keeping_the_place(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    win, store = _home(ctx, monkeypatch, OLD)
    ctx.compat.ADDON.setSetting('iptv_playlists', NEW)
    builds = []
    real_build = win._build
    monkeypatch.setattr(win, '_build', lambda place=None: (builds.append(place), real_build(place=place)))

    ctx.home._watch_iptv_playlists(win)
    thread = win._iptv_thread
    assert thread.is_alive() and not win._iptv_ready
    _publish(store, NEW)
    thread.join(5.0)

    assert win._iptv_ready
    assert builds == []  # nothing happens off the GUI thread
    import xbmcgui

    win.onAction(xbmcgui.Action(4))
    assert len(builds) == 1 and builds[0] is not None
    assert not win._iptv_ready
    win.onAction(xbmcgui.Action(4))
    assert len(builds) == 1


def test_a_guide_change_alone_is_waited_for(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    win, store = _home(ctx, monkeypatch, NEW)  # published with the guide-less stamp
    ctx.compat.ADDON.setSetting('iptv_playlists', NEW)
    ctx.compat.ADDON.setSetting('iptv_epg', 'http://guide/xmltv.xml')
    ctx.home._watch_iptv_playlists(win)
    assert win._iptv_thread.is_alive()
    win._iptv_stop.set()
    win._iptv_thread.join(5.0)


def test_no_wait_when_the_entry_already_matches_or_the_setting_is_off(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    win, store = _home(ctx, monkeypatch, NEW)
    ctx.compat.ADDON.setSetting('iptv_playlists', NEW)
    ctx.home._watch_iptv_playlists(win)
    assert getattr(win, '_iptv_thread', None) is None
    ctx.compat.ADDON.setSetting('iptv_playlists', '')
    ctx.home._watch_iptv_playlists(win)
    assert getattr(win, '_iptv_thread', None) is None


def test_a_missing_entry_is_waited_for(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    win, store = _home(ctx, monkeypatch, None)
    ctx.compat.ADDON.setSetting('iptv_playlists', NEW)
    ctx.home._watch_iptv_playlists(win)
    thread = win._iptv_thread
    assert thread.is_alive()
    _publish(store, NEW)
    thread.join(5.0)
    assert win._iptv_ready


def test_the_wait_gives_up_after_its_bound(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    win, _store = _home(ctx, monkeypatch, OLD)
    monkeypatch.setattr(ctx.home, '_IPTV_WAIT_SECONDS', 0.05)
    ctx.compat.ADDON.setSetting('iptv_playlists', NEW)
    ctx.home._watch_iptv_playlists(win)
    thread = win._iptv_thread
    thread.join(5.0)
    assert not thread.is_alive() and not win._iptv_ready


def test_closing_home_stops_the_wait(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    win, store = _home(ctx, monkeypatch, OLD)
    ctx.compat.ADDON.setSetting('iptv_playlists', NEW)
    ctx.home._watch_iptv_playlists(win)
    thread = win._iptv_thread
    win.close()
    thread.join(5.0)
    assert not thread.is_alive()
    _publish(store, NEW)
    assert not win._iptv_ready


def test_release_items_joins_the_wait(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    win, _store = _home(ctx, monkeypatch, OLD)
    ctx.compat.ADDON.setSetting('iptv_playlists', NEW)
    ctx.home._watch_iptv_playlists(win)
    thread = win._iptv_thread
    ctx.home.release_items(win)
    assert not thread.is_alive() and win._iptv_thread is None


def test_a_second_settings_visit_replaces_the_wait(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    win, _store = _home(ctx, monkeypatch, OLD)
    ctx.compat.ADDON.setSetting('iptv_playlists', NEW)
    ctx.home._watch_iptv_playlists(win)
    first = win._iptv_thread
    ctx.home._watch_iptv_playlists(win)
    second = win._iptv_thread
    first.join(5.0)
    assert not first.is_alive() and second is not first and second.is_alive()
    ctx.home._stop_workers(win)
    second.join(5.0)


def test_the_wait_survives_a_store_error_and_a_gone_window(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    win, store = _home(ctx, monkeypatch, OLD)
    ctx.compat.ADDON.setSetting('iptv_playlists', NEW)
    real = ctx.home.get_store
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) == 2:
            raise OSError('busy')
        return real()

    monkeypatch.setattr(ctx.home, 'get_store', flaky)
    ctx.home._watch_iptv_playlists(win)
    thread = win._iptv_thread
    assert _wait_until(lambda: len(calls) >= 3)
    _publish(store, NEW)
    thread.join(5.0)
    assert win._iptv_ready


def test_settings_visit_starts_the_wait(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    win, store = _home(ctx, monkeypatch, OLD)
    ctx.compat.ADDON.setSetting('iptv_playlists', NEW)
    ctx.home._open_settings(win)
    thread = win._iptv_thread
    assert thread.is_alive()
    ctx.home._stop_workers(win)
    thread.join(5.0)
