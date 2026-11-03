# SPDX-FileCopyrightText: 2026 M0Rf30
# SPDX-License-Identifier: MIT

"""Episode helpers for Home's Continue row: the metahub still for an
episode and the ``S1E3 · Title`` label.

Pure Python -- no ``xbmc*`` imports, unit-testable in isolation, same as
``lib.store``/``lib.library`` (see ``AGENTS.md``'s Kodi-independence rule).
"""


def _int_or_zero(value):
    """Coerce a season/episode field to ``int``, treating anything that
    is not a real number (missing, ``None``, a string, a bool -- Stremio
    addons are third parties and regularly send malformed ``videos``
    entries) as ``0`` ("no season/episode info")."""
    if isinstance(value, bool):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        # OverflowError: json accepts the literal ``Infinity``, and
        # ``int(float("inf"))`` raises it.
        return 0


def _text(value):
    """A stripped string, or ``""`` for anything an addon stuffed into a
    text field that is not one (``None``, a number, a dict)."""
    if not isinstance(value, str):
        return ""
    return value.strip()


def _is_imdb_id(value):
    """Cinemeta episode stills are keyed by an imdb id (``tt`` plus
    digits). Other id schemes have no metahub image to point at."""
    if not isinstance(value, str) or not value.startswith("tt") or len(value) < 3:
        return False
    return value[2:].isdigit()


def _episode_still(video, series_id):
    """URL of this episode's picture.

    ``thumbnail`` is what Cinemeta and the addon SDK put on a video.
    ``thumb`` is the same thing from a less careful addon. When both
    are empty and the series id is an imdb id, use the metahub still
    Cinemeta itself writes into ``thumbnail`` -- the path is
    ``https://episodes.metahub.space/{id}/{season}/{episode}/w780.jpg``.
    Anything else returns ``""`` so the caller can fall back to the
    series poster, which is the only art left.
    """
    for key in ("thumbnail", "thumb"):
        url = _text(video.get(key))
        if url:
            return url
    season = _int_or_zero(video.get("season"))
    episode = _int_or_zero(video.get("episode"))
    if _is_imdb_id(series_id) and season >= 1 and episode >= 1:
        return "https://episodes.metahub.space/%s/%d/%d/w780.jpg" % (
            series_id, season, episode,
        )
    return ""


def background_url(imdb_id):
    """The metahub backdrop Cinemeta writes into a meta's ``background``
    for an imdb id, or ``""`` for any other id scheme."""
    if not _is_imdb_id(imdb_id):
        return ""
    return "https://images.metahub.space/background/medium/%s/img" % imdb_id


def episode_still_url(series_id, season, episode):
    """The metahub still for one Cinemeta episode, or ``""``.

    Public face of `_episode_still` for a caller that has the numbers but
    not the video record (Home's Continue card, from a ``tt..:s:e`` id).
    """
    return _episode_still({'season': season, 'episode': episode}, series_id)


def _episode_label(season, episode, title):
    """``S1E3 · Title`` for the hero. Unpadded on purpose: that is the
    label the row is supposed to read. A video with no season and no
    episode number contributes the title alone."""
    season_n = _int_or_zero(season)
    episode_n = _int_or_zero(episode)
    if season_n or episode_n:
        code = "S%dE%d" % (season_n, episode_n)
        return "%s \u00b7 %s" % (code, title) if title else code
    return title
