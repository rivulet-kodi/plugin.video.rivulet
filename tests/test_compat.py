"""Tests for lib.ui.compat's Kodi 19 (Matrix, legacy ListItem API) vs
Kodi >= 20 (Nexus+, InfoTagVideo API) version split.

This file exists because that split is a real shipped contract - addon.xml
declares an `xbmc.python` dependency of 3.0.0, i.e. Kodi 19 is supported -
yet the Kodi 19 branch of `lib.ui.compat` is otherwise nearly untested: it
is only exercised incidentally, through whatever fields consumer tests
(tests/test_views.py) happen to set. `set_video_cast()` in particular adds
a second, independent version fork (Kodi 20's `xbmc.Actor` may itself be
absent) that has no other test coverage at all.
"""
import contextlib
import sys

import pytest

from tests.kodistubs import FakeListItem, install_kodi_stubs

_KODI21_LABELS = {'System.BuildVersion': '21.0 Git:abcdef'}
_KODI20_LABELS = {'System.BuildVersion': '20.0 Git:abcdef'}


@pytest.fixture
def load_compat():
    """Factory fixture: `load_compat(info_labels=None)` installs fresh
    Kodi stubs (via tests.kodistubs.install_kodi_stubs) reloading
    lib.ui.compat, and returns the reload namespace (`.compat`, `.env`).
    Every call is torn down automatically, in reverse order, at test end.
    Omitting `info_labels` (or leaving out 'System.BuildVersion') is the
    Kodi 19 fixture: `xbmc.getInfoLabel()` then returns '', which
    `kodi_major_version()` falls back to major version 19 for.
    """
    with contextlib.ExitStack() as stack:
        def _load(info_labels=None):
            return stack.enter_context(install_kodi_stubs(
                reload=('lib.ui.compat',),
                info_labels=info_labels,
            ))

        yield _load


# ---------------------------------------------------------------------------
# set_video_cast()
# ---------------------------------------------------------------------------


def test_set_video_cast_kodi21_uses_infotagvideo_with_actor_objects(load_compat):
    ctx = load_compat(info_labels=_KODI21_LABELS)
    li = FakeListItem()

    ctx.compat.set_video_cast(li, ['Marlon Brando', 'Al Pacino', 'James Caan'])

    actors = li.info_tag.calls['setCast']
    assert [a.getName() for a in actors] == ['Marlon Brando', 'Al Pacino', 'James Caan']
    assert [a.getOrder() for a in actors] == [1, 2, 3]
    assert all(a.getRole() == '' for a in actors)
    assert li.legacy_cast is None


def test_set_video_cast_kodi19_uses_legacy_listitem_setcast(load_compat):
    ctx = load_compat()  # no System.BuildVersion -> kodi_major_version() falls back to 19
    assert ctx.compat.kodi_major_version() < 20
    li = FakeListItem()

    ctx.compat.set_video_cast(li, ['Marlon Brando', 'Al Pacino'])

    assert li.legacy_cast == [
        {'name': 'Marlon Brando', 'role': '', 'order': 1, 'thumbnail': ''},
        {'name': 'Al Pacino', 'role': '', 'order': 2, 'thumbnail': ''},
    ]
    assert li.info_tag.calls == {}


def test_set_video_cast_kodi20_falls_back_when_actor_class_missing(load_compat, monkeypatch):
    """A Kodi >=20 build whose `xbmc` shim lacks `Actor` (defensive edge
    case) must fall back to the legacy ListItem path, not raise."""
    ctx = load_compat(info_labels=_KODI20_LABELS)
    monkeypatch.delattr(sys.modules['xbmc'], 'Actor')
    li = FakeListItem()

    ctx.compat.set_video_cast(li, ['Robert De Niro'])

    assert li.legacy_cast == [{'name': 'Robert De Niro', 'role': '', 'order': 1, 'thumbnail': ''}]
    assert li.info_tag.calls == {}


@pytest.mark.parametrize('cast', [
    None,
    [],
    (),
    'Marlon Brando',        # a plain string is not a list/tuple - ignored, not iterated char-by-char
    {'name': 'Marlon'},     # a dict is not a list/tuple - ignored
    ['', None, ''],         # list, but every entry is empty/None once stringified
])
def test_set_video_cast_never_calls_setcast_for_unusable_input(load_compat, cast):
    ctx = load_compat(info_labels=_KODI21_LABELS)
    li = FakeListItem()

    ctx.compat.set_video_cast(li, cast)

    assert li.info_tag.calls == {}
    assert li.legacy_cast is None


def test_set_video_cast_stringifies_non_string_scalars(load_compat):
    ctx = load_compat(info_labels=_KODI21_LABELS)
    li = FakeListItem()

    ctx.compat.set_video_cast(li, ['Real Name', 42])

    actors = li.info_tag.calls['setCast']
    assert [a.getName() for a in actors] == ['Real Name', '42']


def test_set_video_cast_swallows_typeerror_from_infotagvideo_setcast(load_compat):
    ctx = load_compat(info_labels=_KODI21_LABELS)
    li = FakeListItem()

    def raising_setter(actors):
        raise TypeError('boom')

    li.info_tag.setCast = raising_setter

    ctx.compat.set_video_cast(li, ['Marlon Brando'])  # must not raise


def test_set_video_cast_swallows_typeerror_from_legacy_setcast(load_compat, monkeypatch):
    ctx = load_compat()  # Kodi 19 legacy path
    li = FakeListItem()

    def raising_setter(actors):
        raise TypeError('boom')

    monkeypatch.setattr(li, 'setCast', raising_setter)

    ctx.compat.set_video_cast(li, ['Marlon Brando'])  # must not raise


# ---------------------------------------------------------------------------
# set_video_info() - Kodi 19 legacy branch
# ---------------------------------------------------------------------------


def test_set_video_info_kodi19_legacy_dict_only_known_nonempty_keys(load_compat):
    ctx = load_compat()  # Kodi 19 legacy path
    li = FakeListItem()
    info = {
        'title': 'The Godfather',
        'year': 1972,
        'plot': '',          # falsy -> excluded
        'genre': None,       # falsy -> excluded
        'unknownkey': 'x',   # not a recognized video-info key -> excluded
    }

    ctx.compat.set_video_info(li, info)

    assert li.legacy_info == {'title': 'The Godfather', 'year': 1972}
    assert li.info_tag.calls == {}


# ---------------------------------------------------------------------------
# setting_bool() / setting_int() - raw-string parsing of an addon setting,
# never raises regardless of what's stored or what getSetting() does. The
# single canonical home for this coverage: it used to be duplicated across
# tests/test_uicommon.py, tests/test_player_buffer.py and
# tests/test_service_runner.py (each delegating through a different
# caller), which let the three copies drift out of sync.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize('raw,expected', [
    ('true', True), ('True', True), ('TRUE', True), ('1', True), ('yes', True), ('YES', True), ('on', True), ('On', True),
    ('false', False), ('False', False), ('FALSE', False), ('0', False), ('no', False), ('No', False), ('off', False), ('OFF', False),
])
def test_setting_bool_parses_recognized_strings(load_compat, raw, expected):
    ctx = load_compat()
    ctx.env.addon.settings['flag'] = raw
    assert ctx.compat.setting_bool('flag', not expected) is expected


@pytest.mark.parametrize('raw', ['', 'maybe', 'null', '  ', 'not-a-bool'])
def test_setting_bool_falls_back_to_default_on_unreadable(load_compat, raw):
    ctx = load_compat()
    ctx.env.addon.settings['flag'] = raw
    assert ctx.compat.setting_bool('flag', True) is True
    assert ctx.compat.setting_bool('flag', False) is False


def test_setting_bool_missing_key_falls_back_to_default(load_compat):
    ctx = load_compat()
    assert ctx.compat.setting_bool('missing', True) is True
    assert ctx.compat.setting_bool('missing', False) is False


def test_setting_bool_never_raises_when_getsetting_raises(load_compat, monkeypatch):
    ctx = load_compat()

    def boom(key):
        raise RuntimeError('kodi settings db locked')

    monkeypatch.setattr(ctx.env.addon, 'getSetting', boom)
    assert ctx.compat.setting_bool('flag', True) is True


def test_setting_int_parses_and_falls_back_to_default(load_compat):
    ctx = load_compat()
    ctx.env.addon.settings['n'] = '42'
    assert ctx.compat.setting_int('n', 20) == 42
    ctx.env.addon.settings['n'] = ''
    assert ctx.compat.setting_int('n', 20) == 20
    ctx.env.addon.settings['n'] = 'not-a-number'
    assert ctx.compat.setting_int('n', 20) == 20


def test_setting_int_zero_is_not_treated_as_missing(load_compat):
    ctx = load_compat()
    ctx.env.addon.settings['n'] = '0'
    assert ctx.compat.setting_int('n', 99) == 0


def test_setting_int_clamps_to_minimum(load_compat):
    ctx = load_compat()
    ctx.env.addon.settings['n'] = '-5'
    assert ctx.compat.setting_int('n', 0, minimum=1) == 1
    ctx.env.addon.settings['n'] = '1'
    assert ctx.compat.setting_int('n', 20, minimum=5) == 5
    ctx.env.addon.settings['n'] = '10'
    assert ctx.compat.setting_int('n', 20, minimum=5) == 10


def test_setting_int_never_raises_when_getsetting_raises(load_compat, monkeypatch):
    ctx = load_compat()

    def boom(key):
        raise RuntimeError('kodi settings db locked')

    monkeypatch.setattr(ctx.env.addon, 'getSetting', boom)
    assert ctx.compat.setting_int('n', 20) == 20
