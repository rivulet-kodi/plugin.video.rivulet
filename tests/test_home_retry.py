"""Transient Home paging failures are retryable; stock paging stays eager-stop."""
import pytest

from lib.stremio.addons import AddonError
from tests.homehooks import join_shelf_extends
from tests.test_home import load_homewindow as load_homewindow
from tests.test_home_tabs import build, metas


def row(ctx, monkeypatch):
    win, _, _ = build(ctx, monkeypatch)
    state = win._rows[30041]
    state.update(pending=[], served=24, seen_ids={m['id'] for m in metas(0, 24)})
    win.getControl(30041).selectItem(19)
    win.setFocusId(30041)
    from lib.ui import catalogpages
    catalogpages.clear_later_pages()
    return win, state


def prefetch(hw, win):
    hw._maybe_extend_focused_shelf(win)
    join_shelf_extends(win)
    hw._apply_shelf_extends(win)


def test_retry_same_skip_after_backoff_continues_without_duplicates(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    from lib.ui import views
    hw = ctx.home
    win, state = row(ctx, monkeypatch)
    state['list_id'] = 'cat:https://u:SECRET@addon/private?key=TOKEN|movie|movies'
    attempts = []

    def fetch(*args, extra=None):
        skip = dict(extra)['skip']
        attempts.append(skip)
        if len(attempts) == 1:
            raise AddonError('SECRET TOKEN timeout', category='HTTP 504')
        return metas(20, 16) if skip == 24 else []

    monkeypatch.setattr(views, '_fetch_catalog', fetch)
    prefetch(hw, win)
    assert state['failed']['count'] == 1 and not state['exhausted']
    assert state['served'] == 24
    assert win.getControl(30041).size() == 24
    prefetch(hw, win)
    assert attempts == [24]  # No network before the thirty-second backoff.
    assert hw._ROW_RETRY_SECONDS >= 30
    state['failed']['at'] -= hw._ROW_RETRY_SECONDS + 1
    from lib.ui import catalogpages
    for failed in catalogpages._later_failures.values():
        failed['at'] -= hw._ROW_RETRY_SECONDS + 1
    prefetch(hw, win)
    assert attempts == [24, 24, 40]
    items = win.getControl(30041).items
    assert len(items) == len({item.getProperty('sid') for item in items}) == 36
    assert win.getControl(30041).getSelectedPosition() == 19
    assert state['failed'] is None and state['exhausted']
    hw._log_row_end(state)
    ended = [(message, level) for message, level in ctx.env.log_calls if 'home: row ' in message and ' ended ' in message]
    assert len(ended) == 1
    assert '(empty page) at 36 items' in ended[0][0]
    import xbmc
    assert ended[0][1] == xbmc.LOGINFO
    assert all('SECRET' not in message and 'TOKEN' not in message for message, _ in ctx.env.log_calls)


def test_three_consecutive_failures_end_the_row_once(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    from lib.ui import views
    hw = ctx.home
    win, state = row(ctx, monkeypatch)
    attempts = []

    def fail(*args, extra=None):
        attempts.append(dict(extra)['skip'])
        raise AddonError('temporary outage')

    monkeypatch.setattr(views, '_fetch_catalog', fail)
    for count in range(1, 4):
        if state.get('failed'):
            state['failed']['at'] -= 31
        prefetch(hw, win)
        assert state['failed']['count'] == count
        assert state['exhausted'] is (count == 3)
    prefetch(hw, win)
    assert attempts == [24, 24, 24]
    ended = [m for m, _ in ctx.env.log_calls if ' ended ' in m]
    assert len(ended) == 1
    assert '(3 consecutive page failures) at 24 items' in ended[0]


@pytest.mark.parametrize('reason', ['empty page', 'added nothing new', 'no skip', 'fixed skip'])
def test_true_ends_are_not_retried(load_homewindow, monkeypatch, reason):
    ctx = load_homewindow()
    from lib.ui import views
    hw = ctx.home
    win, state = row(ctx, monkeypatch)
    calls = []
    monkeypatch.setattr(views, '_fetch_catalog', lambda *args, **kwargs: calls.append(True) or (metas(0, 24) if reason == 'added nothing new' else []))
    if reason == 'no skip':
        state['catalog'] = {'type': 'movie', 'id': 'top'}
    if reason == 'fixed skip':
        state['extra'] = [('skip', 24)]
    prefetch(hw, win)
    assert state['exhausted'] and state['end_reason'] == reason
    count = len(calls)
    prefetch(hw, win)
    assert len(calls) == count
    ended = [m for m, _ in ctx.env.log_calls if ' ended ' in m]
    assert len(ended) == 1


def test_successful_page_resets_the_consecutive_failure_count(load_homewindow, monkeypatch):
    ctx = load_homewindow()
    from lib.ui import views
    hw = ctx.home
    _win, state = row(ctx, monkeypatch)
    state['failed'] = {'at': -1000, 'count': 2}

    def fetch(*args, extra=None):
        if dict(extra)['skip'] == 24:
            return metas(24, 1)
        raise AddonError('next page timed out')

    monkeypatch.setattr(views, '_fetch_catalog', fetch)
    assert hw._pull_more(state, 24) == metas(24, 1)
    assert state['failed']['count'] == 1
    assert not state['exhausted']
