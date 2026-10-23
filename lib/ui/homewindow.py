# SPDX-FileCopyrightText: 2026 M0Rf30
# SPDX-License-Identifier: MIT

"""Launch the Home session, with migration, update notice and heartbeat.

These run once per launch, outside onInit, which Kodi repeats when Home
reopens after playback. Keeping them here preserves their call timing.
"""
from lib.ui.dependencies import get_store
from lib.ui.uicommon import UiHeartbeat, open_window


def _migrate_mystuff_setting():
    """Carry a `home_show_continue` value over to `home_show_mystuff` once.

    The merged row replaced two rows and took a new setting id with it, so
    anyone who had deliberately turned the old "Continue watching" row OFF
    would silently get the new row switched back on. This copies that
    decision across the rename exactly once. The Home layout migration
    reads that result immediately afterwards, completing the old setting
    → merged setting → saved layout chain before retired toggles vanish.

    Only a `false` needs carrying: the new setting already defaults to
    True, so an unset or true old value is already correct. The old key is
    cleared afterwards, which is also what makes this idempotent - a
    second launch finds nothing to migrate and cannot re-apply the value
    over a choice the user has since changed.

    Best-effort: a settings read/write failing here must never stop Home
    from opening, so any exception is logged and swallowed.
    """
    import xbmc

    from lib.ui.compat import ADDON, log, setting_bool

    try:
        raw = (ADDON.getSetting('home_show_continue') or '').strip()
        if not raw:
            return
        if not setting_bool('home_show_continue', True):
            ADDON.setSetting('home_show_mystuff', 'false')
            log('homewindow: carried home_show_continue=false to home_show_mystuff', xbmc.LOGINFO)
        ADDON.setSetting('home_show_continue', '')
    except Exception as exc:
        log('homewindow: home_show_mystuff migration failed: %r' % (exc,), xbmc.LOGWARNING)


def _migrate_home_layout():
    """Finish the old toggle rename chain once, before Home reads layout.

    Only an absent file migrates; a user's saved arrangement wins over old
    settings forever. Personal Library splits survive disabled type rows.
    """
    import os

    from lib.homelayout import migrate
    from lib.stremio.addons import addon_error_detail
    from lib.ui.compat import log, setting_bool

    try:
        store = get_store()
        folder = getattr(store, 'data_dir', None)
        if not folder or os.path.exists(os.path.join(folder, 'home_layout.json')):
            return
        toggles = {key: setting_bool(key, True) for key in (
            'home_show_mystuff', 'home_show_movies', 'home_show_series',
        )}
        store.set_home_layout(migrate(toggles))
    except Exception as exc:
        log('homewindow: layout migration failed: %s' % addon_error_detail(exc))


def _notify_if_updated(store):
    """Toast the user once when the running addon version differs from the
    version `store` last recorded - the "tell them on next launch" approach
    (as opposed to a boot-time service notification, which is easy to miss
    behind Kodi's splash/home screen). A brand-new install has nothing
    recorded yet, so that first call only seeds the current version and
    stays silent; only version numbers that actually change are ever
    announced.

    Best-effort: `store` I/O failing here must never stop the home screen
    from opening, so any exception is logged and swallowed, same as other
    non-critical HomeWindow paths."""
    import xbmc

    from lib.ui.compat import log
    try:
        from lib.ui.compat import ADDON, L, notify

        current = ADDON.getAddonInfo('version')
        last_seen = store.get_last_seen_version()
        if last_seen == current:
            return
        store.set_last_seen_version(current)
        if last_seen is not None:
            notify(L(30184) % current)
    except Exception as exc:
        log('homewindow: update-notification check failed: %r' % (exc,), xbmc.LOGWARNING)




class HomeOpenError(RuntimeError):
    """The Home could not be constructed or enter Kodi's modal loop."""


def open_home():
    """Run the single Home under a heartbeat, closing it on every exit.

    On every exit it also releases the `ListItem`s Home built
    (`newhome.release_items`) before the window is dropped.

    Only construction/open failures reach the recovery directory. Home
    handles build/row failures itself, leaving failed rows empty.
    """
    import time

    import xbmc

    from lib.stremio.addons import addon_error_detail
    from lib.ui.compat import log

    started = time.monotonic()
    log('homewindow: opening HomeWindow', xbmc.LOGINFO)
    _migrate_mystuff_setting()
    _migrate_home_layout()
    with UiHeartbeat():
        # Once per launch, not onInit: reopening after playback must not
        # repeat the update notice. A cosmetic store failure is harmless.
        try:
            _notify_if_updated(get_store())
        except Exception as exc:
            log('homewindow: update-notification store unavailable: %s' %
                addon_error_detail(exc), xbmc.LOGWARNING)
        try:
            from lib.ui.newhome import NewHomeWindow

            win = open_window(NewHomeWindow, 'NewHomeWindow.xml')
        except Exception as exc:
            raise HomeOpenError(addon_error_detail(exc)) from exc
        def shown():
            log('homewindow: HomeWindow shown in %.3f s' % (time.monotonic() - started), xbmc.LOGINFO)

        win._home_on_shown = shown
        try:
            win.doModal()
        except Exception as exc:
            raise HomeOpenError(addon_error_detail(exc)) from exc
        finally:
            try:
                win.close()
            except Exception:
                pass
            # Still inside `open_home`, so the window and its controls
            # exist: free the Home `ListItem`s now, not at interpreter
            # teardown (the Kodi segfault `release_items` documents).
            try:
                from lib.ui.newhome import release_items

                release_items(win)
            except Exception as exc:
                log('homewindow: releasing Home items failed: %s' % addon_error_detail(exc),
                    xbmc.LOGWARNING)
    log('homewindow: HomeWindow closed', xbmc.LOGINFO)
