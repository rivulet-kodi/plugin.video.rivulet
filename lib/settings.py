"""Kodi-independent addon-setting parsing, shared by the UI process
(`lib.ui.compat`) and the background service (`lib.service_runner`).

`xbmcaddon.Addon.getSettingBool()`/`getSettingInt()` have been observed,
live, to spuriously return the wrong typed value for a setting
settings.xml genuinely has set correctly -- specifically when read at
torrent pre-buffer time, inside a setResolvedUrl-bound call, often
right after an addon upgrade (see `lib.ui.player._prebuffer_torrent()`).
Parsing the raw `getSetting()` string ourselves sidesteps whatever
internal typing/caching quirk causes that. Centralizing that parsing
here, rather than duplicating it per process, guarantees the UI and the
service can never disagree about the same setting's value.

This module takes no Kodi imports: `addon` is any object exposing a
`getSetting(key) -> str` method (a real `xbmcaddon.Addon` in
production, `tests.kodistubs.fakes.FakeAddon` in tests).
"""


def setting_bool(addon, key, default):
    """Parse a boolean addon setting from `addon.getSetting(key)`.

    Never raises; any empty/missing/unreadable/unrecognized value falls
    back to `default`.
    """
    try:
        raw = addon.getSetting(key)
    except Exception:  # noqa: BLE001 - a broken setting read must never raise
        return default
    raw = (raw or '').strip().lower()
    if raw in ('true', '1', 'yes', 'on'):
        return True
    if raw in ('false', '0', 'no', 'off'):
        return False
    return default


def setting_int(addon, key, default, minimum=None):
    """Parse an int addon setting from `addon.getSetting(key)`.

    Never raises; an empty/missing/unparseable value falls back to
    `default`; when `minimum` is given, the result is clamped up to it.
    """
    try:
        raw = addon.getSetting(key)
    except Exception:  # noqa: BLE001 - a broken setting read must never raise
        return default
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    if minimum is not None and value < minimum:
        return minimum
    return value
