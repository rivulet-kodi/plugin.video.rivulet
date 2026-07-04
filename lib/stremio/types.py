"""Small shared helpers for Stremio protocol data (pure Python, no Kodi imports)."""


def video_sort_key(video):
    """Sort key for a meta item's `videos` array.

    Per stremio-core (src/types/resource/meta_item.rs:301-330 and the
    "Meta Types Behavior" protocol gotcha), series videos are ordered by
    (season, episode) ascending, except season 0 ("Specials") always sorts
    last regardless of its numeric value being the smallest. A missing
    season/episode is treated as 0.

    Usage: `sorted(meta['videos'], key=video_sort_key)`.
    """
    season = video.get('season') or 0
    episode = video.get('episode') or 0
    return (season == 0, season, episode)
