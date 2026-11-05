# SPDX-FileCopyrightText: 2026 M0Rf30
# SPDX-License-Identifier: MIT

"""Kodi-independent live-TV rules, ported from stremio-core 0.66.0.

Rivulet follows Stremio's own live-TV model so that it works with any
Stremio live add-on and the engine's IPTV add-on works in the official
apps. Every rule below names the stremio-core source it copies; none of it
is engine-specific.

* A live item is `behaviorHints.isLive` or `type == "tv"`
  (`src/types/resource/meta_item.rs`, `MetaItemBehaviorHints::is_live`).
* A schedule is the meta's `videos`, each a programme carrying `startTime`
  and `endTime` (RFC 3339, `VideoEpgInfo`). `programs_at` is
  `MetaItem::programs_at`.
* A live meta page refreshes its schedule every 15 minutes, every minute
  once the schedule's coverage has passed or the last fetch failed
  (`src/models/meta_details.rs`, `live_schedule_refresh_update`).
* A guide catalog is a catalog declaring a `date` extra on an add-on whose
  manifest has `behaviorHints.epgProvider` (`src/types/addon/manifest.rs`
  `is_epg_guide`, `src/models/catalog_with_filters.rs`
  `is_epg_guide_request`). It is asked once per UTC date overlapping the
  user's local day (`src/models/live_tv_guide.rs`, `overlapping_utc_dates`)
  and refreshes every 15 minutes.
"""

import datetime

#: The refresh period of a live schedule and of a guide page
#: (`LIVE_SCHEDULE_REFRESH_INTERVAL` in stremio-core's meta_details.rs).
REFRESH_SECONDS = 15 * 60

#: The retry period once the schedule's coverage ended or a fetch failed.
RETRY_SECONDS = 60

#: How many recently watched channels the "Recent channels" row keeps
#: (`CATALOG_PREVIEW_SIZE` in stremio-core's constants.rs is 10).
RECENT_LIMIT = 10

#: The extra a guide catalog declares (`EPG_DATE_EXTRA_PROP`).
DATE_EXTRA = "date"

_EPOCH = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)


def is_live(meta):
    """Whether `meta` (a meta, a preview, or a library record's own fields)
    is a live channel: `behaviorHints.isLive` or the legacy `tv` type."""
    if not isinstance(meta, dict):
        return False
    hints = meta.get("behaviorHints")
    if isinstance(hints, dict) and hints.get("isLive") is True:
        return True
    return meta.get("type") == "tv"


def parse_time(value):
    """An RFC 3339 timestamp as epoch seconds (float), or ``None``.

    The offset (`Z` or `+hh:mm`) is honoured; a timestamp without one is
    read as UTC. Fractional seconds are kept. Anything that is not a
    timestamp (a number, ``None``, junk) is ``None``, so one hostile
    programme never raises out of a paint.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text[-1] in "zZ":
        text = text[:-1] + "+00:00"
    # `fromisoformat` is 3.7+ but only takes 3-or-6 fraction digits until
    # 3.11; normalise the fraction to six digits first.
    head, sep, tail = text.partition(".")
    if sep:
        digits = ""
        for char in tail:
            if not char.isdigit():
                break
            digits += char
        rest = tail[len(digits):]
        text = head + "." + (digits + "000000")[:6] + rest
    try:
        parsed = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    try:
        return (parsed - _EPOCH).total_seconds()
    except OverflowError:
        return None


def _span(video):
    """``(start, end)`` epoch seconds of a programme, or ``None`` when it
    carries no usable `startTime`/`endTime` (a plain episode video)."""
    if not isinstance(video, dict):
        return None
    start = parse_time(video.get("startTime"))
    end = parse_time(video.get("endTime"))
    if start is None or end is None:
        return None
    return start, end


def programs_at(videos, now):
    """``(current, next)`` programme dicts of a schedule at `now` (epoch
    seconds), either ``None``. `MetaItem::programs_at`:

    * current: of the programmes with ``start <= now < end``, the one with
      the latest start (the first of equals, as Rust's `max_by_key`
      returns the LAST maximum -- ties go to the later entry, mirrored
      here);
    * next: of those with ``start < end``, ``start > now`` and, when there
      is a current one, ``start >= current.end``, the earliest start (the
      first of equals, `min_by_key`'s rule).
    """
    spans = []
    for video in videos if isinstance(videos, (list, tuple)) else ():
        span = _span(video)
        if span is not None:
            spans.append((video, span))
    current = None
    for video, (start, end) in spans:
        if start <= now < end and (current is None or start >= current[1][0]):
            current = (video, (start, end))
    upcoming = None
    for video, (start, end) in spans:
        if not (start < end and start > now):
            continue
        if current is not None and start < current[1][1]:
            continue
        if upcoming is None or start < upcoming[1][0]:
            upcoming = (video, (start, end))
    return (current[0] if current else None, upcoming[0] if upcoming else None)


def coverage_end(videos):
    """The latest `endTime` of the schedule (epoch seconds), ``None`` when
    no programme has one."""
    ends = [span[1] for span in map(_span, videos if isinstance(videos, (list, tuple)) else ())
            if span is not None]
    return max(ends) if ends else None


def refresh_interval(videos, now, failed=False):
    """Seconds until a live schedule is fetched again:
    :data:`REFRESH_SECONDS`, or :data:`RETRY_SECONDS` once the schedule's
    coverage (its max `endTime`) has passed or the last fetch `failed`.
    An empty schedule counts as covering nothing: retried every minute, as
    a channel whose guide has not loaded yet should appear soon."""
    if failed:
        return RETRY_SECONDS
    end = coverage_end(videos)
    if end is None or end <= now:
        return RETRY_SECONDS
    return REFRESH_SECONDS


def is_epg_provider(manifest):
    """Whether the manifest's `behaviorHints.epgProvider` is true."""
    hints = manifest.get("behaviorHints") if isinstance(manifest, dict) else None
    return isinstance(hints, dict) and hints.get("epgProvider") is True


def _extra_names(catalog):
    names = []
    extras = catalog.get("extra") if isinstance(catalog, dict) else None
    if isinstance(extras, list):
        for extra in extras:
            if isinstance(extra, dict) and isinstance(extra.get("name"), str):
                names.append(extra["name"])
    extra_supported = catalog.get("extraSupported") if isinstance(catalog, dict) else None
    if isinstance(extra_supported, list):
        names.extend(name for name in extra_supported if isinstance(name, str))
    return names


def is_guide_catalog(manifest, catalog):
    """Whether `catalog` of `manifest` is a guide catalog: it declares the
    `date` extra and the add-on is an `epgProvider`. A `date` extra on any
    other add-on is an ordinary catalog filter and stays browsable."""
    return is_epg_provider(manifest) and DATE_EXTRA in _extra_names(catalog)


def guide_dates(now, utc_offset_seconds=0):
    """The UTC dates (``YYYY-MM-DD``) overlapping the local day that
    contains `now` (epoch seconds); `utc_offset_seconds` is the local zone
    east of UTC. One date when local midnight is UTC midnight, else two
    (`overlapping_utc_dates`)."""
    local = datetime.timedelta(seconds=now + utc_offset_seconds)
    day_start_local = _EPOCH + datetime.timedelta(days=local.days)
    start = day_start_local - datetime.timedelta(seconds=utc_offset_seconds)
    end = start + datetime.timedelta(days=1) - datetime.timedelta(seconds=1)
    dates = []
    day = start.date()
    while day <= end.date():
        dates.append(day.isoformat())
        day += datetime.timedelta(days=1)
    return dates


def now_next(videos, now):
    """What a hero shows for a channel at `now`: ``None`` with no
    programme at all, else a dict with

    * `current` -- ``{'title', 'start', 'end', 'overview', 'progress'}``
      (`progress` 0-100, ``None`` without a current programme), or ``None``;
    * `next` -- ``{'title', 'start', 'end'}`` or ``None``.
    """
    current, upcoming = programs_at(videos, now)
    if current is None and upcoming is None:
        return None

    def facts(video):
        # `programs_at` only returns programmes whose span parsed.
        start, end = _span(video) or (0.0, 0.0)
        return {
            "title": str(video.get("title") or video.get("name") or "").strip(),
            "start": start,
            "end": end,
        }

    result = {"current": None, "next": None}
    if current is not None:
        info = facts(current)
        info["overview"] = str(current.get("overview") or current.get("description") or "").strip()
        length = info["end"] - info["start"]
        info["progress"] = max(0, min(100, int((now - info["start"]) * 100 // length))) if length > 0 else 0
        result["current"] = info
    if upcoming is not None:
        result["next"] = facts(upcoming)
    return result


def guide_videos(guide_metas, channel_id):
    """The programmes of `channel_id` across the metas of the guide pages
    asked for (one per UTC date): every `videos` entry of the channel's
    metas, merged, de-duplicated by id, sorted by start."""
    seen = set()
    merged = []
    for meta in guide_metas if isinstance(guide_metas, (list, tuple)) else ():
        if not isinstance(meta, dict) or meta.get("id") != channel_id:
            continue
        for video in meta.get("videos") or ():
            if not isinstance(video, dict):
                continue
            key = video.get("id")
            if key is not None:
                if key in seen:
                    continue
                seen.add(key)
            merged.append(video)
    merged.sort(key=lambda video: parse_time(video.get("startTime")) or 0.0)
    return merged
