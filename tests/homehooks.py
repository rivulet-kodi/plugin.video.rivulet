# SPDX-FileCopyrightText: 2026 M0Rf30
# SPDX-License-Identifier: MIT

"""Wait for HomeWindow's background workers in tests.

Production never waits for them: each worker posts Action(noop) and the
window applies its result there. A test has no Kodi event loop to deliver
that noop, so it joins the worker and then applies the result itself.
Kept here rather than in lib.ui.home because only tests call them.
"""


def join_shelf_extends(window):
    """Join every shelf-extend worker `window` started (2s each)."""
    for thread in list(getattr(window, '_shelf_threads', [])):
        thread.join(timeout=2)


def join_library_meta(window):
    """Wait (2s at most) until `window`'s library-meta worker is idle."""
    idle = getattr(window, '_library_meta_idle', None)
    if idle is not None:
        idle.wait(timeout=2)
