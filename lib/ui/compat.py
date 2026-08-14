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

from lib import settings as _settings

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
    lib/ui/player.py's `_prebuffer_torrent()`). Delegates to
    `lib.settings.setting_bool()`, shared with `lib.service_runner`, so
    the UI and the background service can never disagree about the same
    setting's value.
    """
    return _settings.setting_bool(ADDON, key, default)


def setting_int(key, default, minimum=None):
    """Read an int addon setting via the raw `getSetting()` string.

    Same rationale as `setting_bool()`: delegates to
    `lib.settings.setting_int()` to sidestep `getSettingInt()`
    misbehaving the same way, shared with `lib.service_runner`.
    """
    return _settings.setting_int(ADDON, key, default, minimum=minimum)


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
    'votes': ('setVotes', str),
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


def set_video_cast(list_item, cast):
    """Apply a Stremio meta's `cast` array to `list_item` on any Kodi
    version.

    `cast` is a Stremio meta's plain list of actor names (no roles, no
    thumbnails) - e.g. `["Marlon Brando", "Al Pacino"]`. Non-list/None
    input, and lists that reduce to no usable names once None/empty
    entries are dropped, are no-ops: `setCast()` is never called with an
    empty list. Non-string scalars are stringified.

    Kodi >= 20 takes `ListItem.getVideoInfoTag().setCast()`, a list of
    `xbmc.Actor` objects - a class only added in Kodi 20, so it is
    resolved defensively via `getattr(xbmc, 'Actor', None)`; if it (or
    `getVideoInfoTag`/`setCast`) is missing, this falls back to the
    legacy path below instead of raising. Kodi 19 (Matrix) has no such
    InfoTagVideo API at all and takes the legacy `ListItem.setCast()`, a
    list of plain `{'name', 'role', 'order', 'thumbnail'}` dicts.

    Either way, since Stremio supplies names only, `role`/`thumbnail`
    are always empty by necessity, and `order` is 1-based, matching
    Kodi's own convention. A `setCast()` call that raises is swallowed,
    the same defensive posture as `set_video_info()`'s per-setter guard
    - one hostile manifest must never break a whole directory listing.
    """
    if not isinstance(cast, (list, tuple)):
        return
    names = []
    for name in cast:
        if name is None:
            continue
        text = name if isinstance(name, str) else str(name)
        if text:
            names.append(text)
    if not names:
        return

    if kodi_major_version() >= 20:
        actor_cls = getattr(xbmc, 'Actor', None)
        tag = list_item.getVideoInfoTag() if actor_cls is not None else None
        setter = getattr(tag, 'setCast', None) if tag is not None else None
        if setter is not None:
            actors = [actor_cls(name, '', order) for order, name in enumerate(names, start=1)]
            try:
                setter(actors)
            except (TypeError, ValueError):
                pass
            return

    setter = getattr(list_item, 'setCast', None)
    if setter is None:
        return
    try:
        setter([
            {'name': name, 'role': '', 'order': order, 'thumbnail': ''}
            for order, name in enumerate(names, start=1)
        ])
    except (TypeError, ValueError):
        pass
