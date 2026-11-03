"""Settings > Refresh cached data (lib/ui/cacherefresh.py)."""
import os

from tests.kodistubs import install_kodi_stubs


def test_refresh_deletes_the_cache_files_and_keeps_the_rest(tmp_path, monkeypatch):
    with install_kodi_stubs(reload=('lib.ui.compat', 'lib.ui.cacherefresh')) as ctx:
        import lib.ui.cacherefresh as cacherefresh
        for name in cacherefresh.CACHE_FILES + ('auth.json', 'progress.json'):
            (tmp_path / name).write_text('{}')
        monkeypatch.setattr(cacherefresh, '_cache_dirs', lambda: [str(tmp_path)])
        import lib.ui.logocheck as logocheck
        logocheck.remember('https://img/a.png', True)

        cacherefresh.refresh_from_settings()

        left = sorted(os.listdir(str(tmp_path)))
        assert left == ['auth.json', 'progress.json']
        assert logocheck.state('https://img/a.png') is None
        assert ctx.env.addon.getSetting('cache_generation') != ''
        assert ctx.env.notifications[-1][1] == 'STR30501'


def test_home_drops_its_memory_when_the_generation_changes(monkeypatch):
    from tests.test_home import _RELOAD_MODULE_NAMES

    with install_kodi_stubs(reload=_RELOAD_MODULE_NAMES + ('lib.ui.cacherefresh',)) as ctx:
        hw = ctx.home
        cleared = []
        import lib.ui.cacherefresh as cacherefresh
        monkeypatch.setattr(cacherefresh, 'clear_memory', lambda: cleared.append(True))
        win = hw.HomeWindow('HomeWindow.xml', '/addon/path', 'Default', '1080i')

        hw._drop_memory_if_refreshed(win)          # first build: just records
        assert cleared == []
        ctx.env.addon.setSetting('cache_generation', '123')
        hw._drop_memory_if_refreshed(win)
        assert cleared == [True]
        assert 'cache_generation' in hw._ROW_SETTINGS   # so the rows rebuild too


# --- where the files are, what is kept, and what memory is dropped -------------

import pytest  # noqa: E402


@pytest.fixture
def refresh():
    with install_kodi_stubs(reload=('lib.ui.compat', 'lib.ui.cacherefresh')) as ctx:
        ctx.module = ctx.cacherefresh
        yield ctx


class _Store:
    def __init__(self, data_dir):
        self.data_dir = data_dir


def test_the_profile_and_the_store_folder_are_both_cleared(refresh, monkeypatch, tmp_path):
    import lib.ui.compat as compat
    import lib.ui.dependencies as dependencies

    profile = tmp_path / 'profile'
    store = tmp_path / 'store'
    monkeypatch.setattr(compat, 'addon_profile_dir', lambda: str(profile))
    monkeypatch.setattr(dependencies, 'get_store', lambda: _Store(str(store)))
    assert refresh.module._cache_dirs() == [str(profile), str(store)]
    # The same folder twice is listed once.
    monkeypatch.setattr(dependencies, 'get_store', lambda: _Store(str(profile)))
    assert refresh.module._cache_dirs() == [str(profile)]
    # A store without a folder adds nothing.
    monkeypatch.setattr(dependencies, 'get_store', lambda: _Store(None))
    assert refresh.module._cache_dirs() == [str(profile)]


def test_folders_that_cannot_be_found_are_skipped(refresh, monkeypatch):
    import lib.ui.compat as compat
    import lib.ui.dependencies as dependencies

    def _boom():
        raise RuntimeError('no profile')

    monkeypatch.setattr(compat, 'addon_profile_dir', _boom)
    monkeypatch.setattr(dependencies, 'get_store', _boom)
    assert refresh.module._cache_dirs() == []
    assert refresh.module.clear_files() == 0
    monkeypatch.setattr(compat, 'addon_profile_dir', lambda: '')
    monkeypatch.setattr(dependencies, 'get_store', lambda: _Store(''))
    assert refresh.module._cache_dirs() == []


def test_clear_files_counts_what_it_removed(refresh, monkeypatch, tmp_path):
    first = tmp_path / 'a'
    second = tmp_path / 'b'
    first.mkdir()
    second.mkdir()
    (first / 'catalog_pages.json').write_text('{}')
    (second / 'ratings.json').write_text('{}')
    (second / 'meta_cache.json').write_text('{}')
    (second / 'library.json').write_text('{}')
    monkeypatch.setattr(refresh.module, '_cache_dirs', lambda: [str(first), str(second)])
    assert refresh.module.clear_files() == 3
    assert sorted(p.name for p in second.iterdir()) == ['library.json']
    # Nothing left to remove the second time.
    assert refresh.module.clear_files() == 0


def test_clear_memory_drops_every_copy(refresh):
    from tests.test_home import _RELOAD_MODULE_NAMES

    with install_kodi_stubs(reload=_RELOAD_MODULE_NAMES + ('lib.ui.catalogpages', 'lib.ui.ratingsched',
                                                           'lib.ui.cacherefresh')) as ctx:
        import lib.ui.catalogpages as catalogpages
        import lib.ui.logocheck as logocheck
        import lib.ui.ratingsched as ratingsched

        catalogpages._caches['/data'] = object()
        logocheck.remember('https://img/a.png', True)
        logocheck.remember_texture('https://img/a.png', True, 10)
        ratingsched._cache = object()

        ctx.cacherefresh.clear_memory()

        assert catalogpages._caches == {}
        assert logocheck.state('https://img/a.png') is None
        assert logocheck._LOGO_STATE == {}
        assert ratingsched._cache is None


def test_clear_memory_carries_on_past_a_module_that_fails(refresh, monkeypatch):
    import lib.ui.catalogpages as catalogpages
    import lib.ui.logocheck as logocheck
    import lib.ui.ratingsched as ratingsched

    class _BadLock:
        def __enter__(self):
            raise RuntimeError('lock gone')

        def __exit__(self, *exc):
            return False

    import lib.ui

    monkeypatch.setattr(catalogpages, '_caches_lock', _BadLock())
    # Refreshing logo answers must not need the Home module.
    monkeypatch.setattr(lib.ui, 'home', object(), raising=False)
    logocheck.remember('https://img/a.png', True)
    ratingsched._cache = object()
    refresh.module.clear_memory()
    # The modules after each failure were still cleared.
    assert logocheck.state('https://img/a.png') is None
    assert ratingsched._cache is None


def test_a_setting_that_will_not_save_still_says_done(refresh, monkeypatch):
    import lib.ui.compat as compat

    done = []
    monkeypatch.setattr(refresh.module, 'clear_files', lambda: done.append('files') or 0)
    monkeypatch.setattr(refresh.module, 'clear_memory', lambda: done.append('memory'))

    def _readonly(key, value):
        raise RuntimeError('read only')

    monkeypatch.setattr(compat.ADDON, 'setSetting', _readonly)
    refresh.module.refresh_from_settings()
    assert done == ['memory', 'files']  # Stop old writers before deleting their files.
    assert refresh.env.notifications[-1][1] == 'STR30501'


def test_each_refresh_bumps_the_generation(refresh, monkeypatch):
    import time

    monkeypatch.setattr(refresh.module, 'clear_files', lambda: 0)
    monkeypatch.setattr(refresh.module, 'clear_memory', lambda: None)
    monkeypatch.setattr(time, 'time', lambda: 1000.9)
    refresh.module.refresh_from_settings()
    assert refresh.env.addon.getSetting('cache_generation') == '1000'
    monkeypatch.setattr(time, 'time', lambda: 2000.0)
    refresh.module.refresh_from_settings()
    assert refresh.env.addon.getSetting('cache_generation') == '2000'


def test_cache_refresh_never_imports_home(refresh, monkeypatch):
    import builtins

    original = builtins.__import__
    imports = []

    def tracked(name, globals=None, locals=None, fromlist=(), level=0):
        if name == 'lib.ui.home' or (name == 'lib.ui' and 'home' in (fromlist or ())):
            imports.append(name)
        return original(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, '__import__', tracked)
    refresh.module.clear_memory()
    assert imports == []
