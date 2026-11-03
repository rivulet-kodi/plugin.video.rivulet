"""Only a completed tab may become a reusable Home snapshot."""
import pytest

from lib import homelayout
from tests.test_home import load_homewindow as load_homewindow
from tests.test_home_tabs import build


@pytest.mark.parametrize('result', ['cancelled', 'failed'])
def test_cancelled_anime_toggle_home_is_never_cached_and_tab_return_reloads(load_homewindow, monkeypatch, result):
    ctx = load_homewindow()
    hw = ctx.home
    win, _, calls = build(ctx, monkeypatch)
    hw._switch_tab(win, 'anime')
    calls.clear()
    ctx.compat.ADDON.setSetting('home_show_anime', 'false')
    with monkeypatch.context() as patch:
        patch.setattr(ctx.uicommon, 'run_cancellable', lambda *_: ctx.uicommon.CANCELLED if result == 'cancelled' else None)
        hw._after_child(win)
    assert win._active_tab == 'home'
    assert win._rows == {} and win._tab_loaded['home'] is False
    assert calls == []
    hw._switch_tab(win, 'movies')
    assert 'home' not in win._tab_snapshots
    assert win._rows and win._tab_loaded['movies']
    calls.clear()
    hw._switch_tab(win, 'home')
    assert sorted(calls) == ['movies', 'shows']
    assert [row['kind'] for row in win._rows.values()] == ['continue', 'catalog', 'catalog']
    assert win._tab_loaded['home'] is True
    assert any('home: tab home load '+result in line for line, _ in ctx.env.log_calls)
    win.close()


def test_ok_on_cancelled_active_home_reloads_instead_of_only_refocusing(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    hw = ctx.home
    win, _, calls = build(ctx, monkeypatch)
    with monkeypatch.context() as patch:
        patch.setattr(ctx.uicommon, 'run_cancellable', lambda *_: ctx.uicommon.CANCELLED)
        win._build()
    calls.clear()
    win.getControl(hw.BROWSE_LIST).selectItem(0)
    win.onClick(hw.BROWSE_LIST)
    assert sorted(calls) == ['movies', 'shows']
    assert win._tab_loaded['home'] is True
    assert len(win._rows) == 3
    win.close()


def test_empty_complete_home_restores_without_refetch(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    hw = ctx.home
    layout = homelayout.default_layout()
    layout['tabs']['home'] = []
    win, _, _ = build(ctx, monkeypatch, layout=layout)
    assert win._rows == {} and win._tab_loaded['home'] is True
    loads = []
    load = hw._load_rows

    def record(*args):
        loads.append(win._active_tab)
        return load(*args)

    monkeypatch.setattr(hw, '_load_rows', record)
    hw._switch_tab(win, 'movies')
    assert win._tab_snapshots['home'] == ([], None)
    hw._switch_tab(win, 'home')
    hw._switch_tab(win, 'home')
    assert loads == ['movies']
    assert win._rows == {} and win._tab_loaded['home'] is True
    win.close()


def test_build_exception_marks_tab_not_loaded_and_keeps_error_and_info_logs(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    hw = ctx.home
    win, _, _ = build(ctx, monkeypatch)

    def fail(*args):
        raise RuntimeError('broken build')

    monkeypatch.setattr(win, '_build_rows', fail)
    win._build()
    assert win._rows == {} and win._tab_loaded['home'] is False
    hw._snapshot_tab(win)
    assert 'home' not in win._tab_snapshots
    import xbmc
    assert any('home: build failed:' in line and level == xbmc.LOGERROR for line, level in ctx.env.log_calls)
    assert any('home: tab home load failed' in line and level == xbmc.LOGINFO for line, level in ctx.env.log_calls)
    win.close()


def test_stopped_tab_load_is_incomplete_and_logs_stopped(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    win, store, calls = build(ctx, monkeypatch)
    ctx.home._home_stop(win).set()
    calls.clear()
    assert ctx.home._load_rows(win, store, None) == []
    assert win._tab_loaded['home'] is False and calls == []
    assert any('home: tab home load stopped' in line for line, _ in ctx.env.log_calls)
    win.close()
