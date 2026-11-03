# SPDX-FileCopyrightText: 2026 M0Rf30
# SPDX-License-Identifier: MIT
"""Settings > Refresh cached data: forget what the
screens have saved.

Catalog first pages (a day old at most), title details (30 minutes),
ratings, and the session's answers about which clearlogos exist. The
next screens ask the addons again.

Settings runs this action as its own plugin call, in a different Python
interpreter from the Home window behind Settings, so deleting the files
is not enough: Home also holds pages and logo answers in memory. The
action bumps a hidden setting (`cache_generation`); Home sees it change
when Settings closes, drops its memory copies and rebuilds its rows
(`home._after_child`).
"""

import os
import time

#: The files under the profile this clears. The account, addons, progress
#: and search history are data, not cache, and stay.
CACHE_FILES = ('catalog_pages.json', 'meta_cache.json', 'ratings.json')
GENERATION_SETTING = 'cache_generation'


def _cache_dirs():
    dirs = []
    try:
        from lib.ui.compat import addon_profile_dir

        dirs.append(addon_profile_dir())
    except Exception:
        pass
    try:
        from lib.ui.dependencies import get_store

        data_dir = getattr(get_store(), 'data_dir', None)
        if data_dir and data_dir not in dirs:
            dirs.append(data_dir)
    except Exception:
        pass
    return [d for d in dirs if d]


def clear_files():
    """Delete the cache files. The number removed."""
    removed = 0
    for folder in _cache_dirs():
        for name in CACHE_FILES:
            path = os.path.join(folder, name)
            try:
                os.remove(path)
                removed += 1
            except OSError:
                continue
    return removed


def clear_memory():
    """Drop this process's copies: page caches, logo answers, ratings."""
    try:
        from lib.ui import catalogpages

        catalogpages.stop()
        catalogpages.clear_later_pages()
        with catalogpages._caches_lock:
            catalogpages._caches.clear()
    except Exception:
        pass
    try:
        from lib.ui import logocheck

        logocheck.forget_all()
    except Exception:
        pass
    try:
        from lib.ui import ratingsched

        ratingsched.reset()
    except Exception:
        pass


def refresh_from_settings():
    """The Settings button: clear, bump the generation, say so."""
    from lib.ui.compat import ADDON, L, notify

    # Retire in-process writers before removing their cache files.
    clear_memory()
    clear_files()
    try:
        ADDON.setSetting(GENERATION_SETTING, str(int(time.time())))
    except Exception:
        pass
    notify(L(30501), sound=False)
