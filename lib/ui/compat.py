"""Kodi version-compatibility helpers.

Centralizes the bits that differ between Kodi 19 (Matrix, Python 3 /
legacy ListItem.setInfo API) and Kodi >= 20 (Nexus+, InfoTagVideo
setter API). Everything else in the UI layer should go through here
instead of poking xbmc*/xbmcvfs/xbmcaddon directly for these concerns.
"""
import re

import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs

ADDON = xbmcaddon.Addon()
ADDON_ID = ADDON.getAddonInfo('id')
ADDON_NAME = ADDON.getAddonInfo('name')
ADDON_ICON = ADDON.getAddonInfo('icon')
ADDON_FANART = ADDON.getAddonInfo('fanart')

_LOG_PREFIX = '[%s] ' % ADDON_ID


def L(string_id):
    """Return the localized string for `string_id` from strings.po."""
    return ADDON.getLocalizedString(string_id)


def addon_profile_dir():
    """Return the addon's per-user data directory, creating it if needed."""
    path = xbmcvfs.translatePath('special://profile/addon_data/%s/' % ADDON_ID)
    if not xbmcvfs.exists(path):
        xbmcvfs.mkdirs(path)
    return path


def addon_media_path(name):
    """Return the special:// filesystem path to a bundled resources/media asset.

    Built from ADDON_ID at call time (never a hardcoded addon id) so this
    keeps working under a future rename/fork.
    """
    return xbmcvfs.translatePath('special://home/addons/%s/resources/media/%s' % (ADDON_ID, name))


def addon_fanart():
    """Return the addon's own bundled fanart image path, for rows/menus
    that have no more specific art of their own."""
    return ADDON_FANART


def log(msg, level=xbmc.LOGDEBUG):
    xbmc.log(_LOG_PREFIX + str(msg), level)


def notify(msg, heading=None, icon=None, time_ms=4000):
    xbmcgui.Dialog().notification(
        heading or ADDON_NAME, str(msg), icon or xbmcgui.NOTIFICATION_INFO, time_ms
    )


def setting_bool(key, default):
    """Read a boolean addon setting via the raw `getSetting()` string.

    `xbmcaddon.Addon.getSettingBool()` has been observed, live, to
    spuriously return False for a setting settings.xml genuinely has as
    "true" - specifically when read at torrent pre-buffer time, inside a
    setResolvedUrl-bound call, often right after an addon upgrade (see
    lib/ui/player.py's `_prebuffer_torrent()`). Parsing the raw setting
    string ourselves sidesteps whatever internal typing/caching quirk
    causes that. Any empty/missing/unreadable/unrecognized value falls
    back to `default` - this never raises and never silently goes False.
    """
    try:
        raw = ADDON.getSetting(key)
    except Exception:  # noqa: BLE001 - a broken setting read must never raise
        return default
    raw = (raw or '').strip().lower()
    if raw in ('true', '1', 'yes', 'on'):
        return True
    if raw in ('false', '0', 'no', 'off'):
        return False
    return default


def setting_int(key, default, minimum=None):
    """Read an int addon setting via the raw `getSetting()` string.

    Same rationale as `setting_bool()`: sidesteps `getSettingInt()`
    misbehaving the same way, and never raises. An empty/missing/
    unparseable value falls back to `default`; when `minimum` is given,
    the result is clamped up to it.
    """
    try:
        raw = ADDON.getSetting(key)
    except Exception:  # noqa: BLE001 - a broken setting read must never raise
        return default
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    if minimum is not None and value < minimum:
        return minimum
    return value


_KODI_MAJOR = None


def kodi_major_version():
    """Parse the leading major version out of System.BuildVersion, cached."""
    global _KODI_MAJOR
    if _KODI_MAJOR is None:
        build = xbmc.getInfoLabel('System.BuildVersion')
        match = re.match(r'\s*(\d+)', build or '')
        _KODI_MAJOR = int(match.group(1)) if match else 19
    return _KODI_MAJOR


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return [str(value)]


def _identity(value):
    return value


# info dict key -> (InfoTagVideo setter name, value transform)
# The same keys double as the legacy ListItem.setInfo('video', {...}) dict
# on Kodi 19, so callers only ever build one plain dict.
_VIDEO_INFO_SETTERS = {
    'title': ('setTitle', _identity),
    'originaltitle': ('setOriginalTitle', _identity),
    'tvshowtitle': ('setTvShowTitle', _identity),
    'plot': ('setPlot', _identity),
    'plotoutline': ('setPlotOutline', _identity),
    'genre': ('setGenres', _as_list),
    'year': ('setYear', int),
    'season': ('setSeason', int),
    'episode': ('setEpisode', int),
    'sortseason': ('setSortSeason', int),
    'sortepisode': ('setSortEpisode', int),
    'rating': ('setRating', float),
    'duration': ('setDuration', int),
    'mediatype': ('setMediaType', _identity),
    'premiered': ('setPremiered', _identity),
    'aired': ('setFirstAired', _identity),
    'imdbnumber': ('setIMDBNumber', _identity),
    'mpaa': ('setMpaa', _identity),
    'director': ('setDirectors', _as_list),
    'writer': ('setWriters', _as_list),
    'country': ('setCountries', _as_list),
    'studio': ('setStudios', _as_list),
    'trailer': ('setTrailer', _identity),
}


def set_video_info(list_item, info):
    """Apply a plain video-metadata dict to `list_item` on any Kodi version.

    `info` uses classic ListItem.setInfo('video', ...) key names (title,
    plot, genre, year, season, episode, mediatype, ...); values are plain
    str/int/float or lists. Falsy/empty values are skipped.
    """
    if not info:
        return
    if kodi_major_version() >= 20:
        tag = list_item.getVideoInfoTag()
        for key, value in info.items():
            if value in (None, ''):
                continue
            setter_info = _VIDEO_INFO_SETTERS.get(key)
            if not setter_info:
                continue
            setter_name, transform = setter_info
            setter = getattr(tag, setter_name, None)
            if setter is None:
                continue
            try:
                setter(transform(value))
            except (TypeError, ValueError):
                continue
    else:
        legacy = {
            key: value
            for key, value in info.items()
            if key in _VIDEO_INFO_SETTERS and value not in (None, '')
        }
        if legacy:
            list_item.setInfo('video', legacy)
