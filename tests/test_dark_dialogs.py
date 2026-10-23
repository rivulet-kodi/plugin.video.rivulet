"""Dark dialogs are the single dialog style."""
import contextlib
import os
import re

import pytest

from tests.kodistubs import install_kodi_stubs

_SKIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'resources', 'skins', 'Default', '1080i')


@pytest.fixture
def load():
    with contextlib.ExitStack() as stack:
        def _load(settings=None):
            return stack.enter_context(install_kodi_stubs(
                reload=('lib.ui.compat', 'lib.ui.uicommon', 'lib.ui.dialogs'), settings=settings))
        yield _load


def _read(name):
    with open(os.path.join(_SKIN, name), encoding='utf-8') as handle:
        return handle.read()


@pytest.mark.parametrize('name, required', [
    ('Busy', {30320, 30321, 30322}),
    ('Progress', {30300, 30301, 30302, 30303, 30304, 30305, 30306}),
    ('Countdown', {30310, 30311, 30312, 30313}),
    ('Confirm', {30330, 30331, 30332, 30333}),
    ('OptionList', {30340, 30341, 30342, 30343, 30344, 30345, 30346}),
])
def test_dark_dialog_controls_and_style(name, required):
    dark = _read('Dark' + name + 'Dialog.xml')
    assert required <= {int(cid) for cid in re.findall(r'<control type="\w+" id="(\d+)"', dark)}
    assert 'dlg_panel.png' in dark and 'dlg_stroke.png' in dark
    assert '38BDF8' not in dark and 'FF12161A' not in dark


def test_the_menu_fits_its_rows(load):
    ctx = load()
    dialogs = ctx.dialogs
    win = ctx.uicommon.open_window(dialogs._OptionListWindow, 'DarkOptionListDialog.xml')
    win.heading = 'Title'
    win.rows = ['One', 'Two', 'Three']
    win.onInit()
    top, height, list_h = dialogs.option_list_geometry(3)
    panel = win.getControl(dialogs.OPTIONLIST_PANEL)
    assert (panel.y, panel.height) == (top, height)
    assert win.getControl(dialogs.OPTIONLIST_LIST).height == list_h
    # The scrollbar rides inside a wrapper group: Kodi's Python API cannot
    # wrap a `scrollbar`, so the group is what gets moved with the list.
    scroll = win.getControl(dialogs.OPTIONLIST_SCROLL_GROUP)
    assert scroll.y == top + dialogs._OPTION_HEAD_H


def test_the_option_list_never_asks_python_for_the_scrollbar(load):
    """Asking for the `scrollbar` raised "Unknown control type for python"
    and was logged as a warning on every open of the menu."""
    ctx = load()
    dialogs = ctx.dialogs
    win = ctx.uicommon.open_window(dialogs._OptionListWindow, 'DarkOptionListDialog.xml')
    win.heading = 'Title'
    win.rows = ['One'] * 9
    win.onInit()
    failures = [msg for msg, _level in ctx.env.log_calls if 'getControl' in msg and 'failed' in msg]
    assert failures == []
