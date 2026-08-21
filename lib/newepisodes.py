"""New-episode detection for series the user follows.

Pure Python -- no ``xbmc*`` imports, unit-testable in isolation, same as
``lib.store``/``lib.library`` (see ``AGENTS.md``'s Kodi-independence rule).
Rivulet has no server-side notification channel (unlike Stremio's own
account-backed "what's new" feed), so this module answers the question
entirely from data the caller already has in hand: which series the user
has progress on, that series' full meta (with its ``videos`` array), and
which episodes have already been dismissed.

:func:`new_episodes` decides what counts as "new" for one render;
:func:`mark_seen` records that a candidate has been shown/acted on so it
never reappears. Both are pure functions of their arguments so
``tests/test_newepisodes.py`` exercises every branch with plain dicts --
no store, no addon client, no Kodi stubs.

Ordering mirrors ``lib.ui.binge.next_video()``'s season/episode/release
sort and its "never cross from a real episode into season-0 Specials"
guard (that module's own docstring traces both back to
``stremio-core``'s ``MetaItem::next_video()``), duplicated here rather
than imported: ``lib.ui.binge`` lives in ``lib/ui/`` and this module may
not depend on it, the same reason ``lib.stremio.subtitles`` duplicates a
constant instead of importing it (see that module's own comment).
"""

#: Hard cap on entries kept in the seen-episode set persisted via
#: ``lib.store.Store.get_seen_episodes``/``set_seen_episodes``. Mirrors
#: ``lib.store.MAX_PROGRESS_ENTRIES``'s reasoning (store.py's own docs):
#: a box that runs for years must not grow this file forever, one entry
#: per (series, episode) ever dismissed otherwise. Unlike
#: ``progress.json``, there is no separate age-based sweep here (see
#: :func:`mark_seen`'s docstring for why) -- the count cap alone is
#: what "never grow without bound" needs, since an episode already
#: below a series' last-watched pointer can never be offered again
#: regardless of whether it is still in this set.
MAX_SEEN_EPISODES = 500

#: ``datetime.strptime`` formats a Stremio meta video's ``released``
#: field is tried against, in order -- full RFC3339 with fractional
#: seconds (what Cinemeta actually sends), the same without a fraction,
#: and a bare date (what a hand-written/less careful addon sends).
_RELEASE_FORMATS = (
    "%Y-%m-%dT%H:%M:%S.%fZ",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%d",
)


def _int_or_zero(value):
    """Coerce a season/episode field to ``int``, treating anything that
    is not a real number (missing, ``None``, a string, a bool -- Stremio
    addons are third parties and regularly send malformed ``videos``
    entries) as ``0``, the same "no season/episode info" bucket
    :func:`_sort_key` already gives Specials."""
    if isinstance(value, bool):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _sort_key(video):
    """``(is_special, season, episode, released)`` ascending -- mirrors
    ``lib.ui.binge.next_video``'s ``_sort_key`` (season 0 or missing
    season/episode info sorts last, matching
    ``lib.ui.detailwindow._ordered_videos()``'s "Specials last"
    convention), duplicated per the module docstring. Sorting by this
    rather than trusting ``videos``' own array order is what makes
    out-of-order episode numbering from a sloppy addon harmless."""
    season = _int_or_zero(video.get("season"))
    episode = _int_or_zero(video.get("episode"))
    return (season == 0, season, episode, video.get("released") or "")


def _is_special(sort_key):
    """Whether a :func:`_sort_key` result is Specials/missing-info --
    its own first element, named for readability at call sites."""
    return sort_key[0]


def _parse_release(value):
    """Parse a meta video's ``released`` field into a naive UTC
    ``datetime``, tolerating anything :data:`_RELEASE_FORMATS` does not
    match -- missing, wrong type, or a shape no installed addon here
    actually sends -- by returning ``None``.

    A malformed date must never crash a home-screen render (this runs on
    every ``HomeWindow.onInit()``, against whatever third-party addons
    happen to be installed). :func:`new_episodes` treats ``None`` as
    "not confirmed aired" and excludes the episode -- the fail-closed
    choice: an unparseable date must never be able to make an unaired
    episode look available, only a wrongly-suppressed one look absent,
    which is the safe direction to be wrong in.
    """
    if not isinstance(value, str) or not value:
        return None
    import datetime

    for fmt in _RELEASE_FORMATS:
        try:
            return datetime.datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def _seen_key(content_type, content_id, video_id):
    """Composite key one (series, episode) pair is indexed by in the
    seen-set, or ``None`` when any part is missing/empty (nothing
    sensible to key). Joined with an ASCII unit separator like
    ``lib.store.Store._progress_key`` -- Stremio ids are themselves
    colon-delimited (e.g. ``"tt1234567:1:2"``), so reusing ``":"`` here
    could collide two different tuples. Duplicated rather than imported:
    this module stays a standalone leaf, and the join is one line."""
    if not content_type or not content_id or not video_id:
        return None
    return "\x1f".join((content_type, content_id, video_id))


def new_episodes(series_items, metas, seen, now):
    """Return the new-episode candidates across every followed series.

    ``series_items`` -- ``[{'type', 'id', 'video_id'}, ...]``, one entry
    per followed series with ``video_id`` the last-watched episode's id
    (``None``/absent if the series has never been watched at all). The
    caller decides what "followed" means and reduces to one entry per
    series; this function trusts that shape and does not re-dedupe it.

    ``metas`` -- ``{series_id: meta}`` where ``meta`` is a Stremio meta
    dict (``name``, ``poster``, ``background``, ``videos``). A series
    missing from ``metas``, or whose meta has no ``videos`` list at all
    (a movie-shaped meta, or a fetch that failed upstream), contributes
    nothing -- never raises.

    ``seen`` -- the seen-set :func:`mark_seen` produces; an episode
    already in it is never returned again regardless of date/order.

    ``now`` -- a naive UTC ``datetime``, the single source of "current
    time" so this stays a pure, deterministic function of its inputs
    (never ``datetime.utcnow()`` internally).

    An episode qualifies only when ALL of:

    * its ``released`` date parses (:func:`_parse_release`) and is not
      after ``now`` -- an episode with a missing/malformed/future date
      is never advertised as available (fail-closed; see that
      function's docstring for why unparseable specifically means
      "not confirmed aired" rather than "assume aired").
    * it sorts (:func:`_sort_key`) strictly after the series'
      last-watched episode, so a rewatch or a gap in what was actually
      watched never re-surfaces older episodes as "new". A series with
      no last-watched pointer at all has no lower bound -- everything
      aired and unseen qualifies.
    * it is not Specials/season-0 (or missing season+episode info
      entirely, the same "unfit to judge" bucket :func:`_sort_key`
      gives it) UNLESS the last-watched episode was ALSO in that
      bucket -- mirrors ``lib.ui.binge.next_video``'s guard: a viewer
      partway through the real seasons should not get a "new episode"
      nudge for a behind-the-scenes special, but a viewer already
      watching specials should.
    * it is not already in ``seen``.

    Malformed ``videos`` entries (not a dict, or missing ``id``) are
    dropped up front rather than raised on.
    """
    seen = seen or {}
    results = []
    for item in series_items or []:
        if not isinstance(item, dict):
            continue
        content_type = item.get("type")
        content_id = item.get("id")
        if not content_type or not content_id:
            continue
        meta = (metas or {}).get(content_id)
        if not isinstance(meta, dict):
            continue
        videos = meta.get("videos")
        if not isinstance(videos, list):
            continue
        candidates = [v for v in videos if isinstance(v, dict) and v.get("id")]
        if not candidates:
            continue
        ordered = sorted(candidates, key=_sort_key)

        last_watched_key = None
        last_watched_id = item.get("video_id")
        if last_watched_id:
            for video in ordered:
                if video.get("id") == last_watched_id:
                    last_watched_key = _sort_key(video)
                    break
        watching_specials = last_watched_key is not None and _is_special(last_watched_key)

        for video in ordered:
            video_id = video.get("id")
            key = _seen_key(content_type, content_id, video_id)
            if key is None or key in seen:
                continue
            sort_key = _sort_key(video)
            if last_watched_key is not None and sort_key <= last_watched_key:
                continue
            if _is_special(sort_key) and not watching_specials:
                continue
            released = _parse_release(video.get("released"))
            if released is None or released > now:
                continue
            results.append({
                "type": content_type,
                "id": content_id,
                "video_id": video_id,
                "season": video.get("season"),
                "episode": video.get("episode"),
                "released": video.get("released"),
                "name": meta.get("name"),
                "poster": meta.get("poster"),
                "background": meta.get("background"),
            })
    return results


def mark_seen(seen, episodes):
    """Return an updated seen-set with every episode in ``episodes``
    (as :func:`new_episodes` returns them, or any dict carrying the same
    ``type``/``id``/``video_id``) added, keyed exactly like
    :func:`new_episodes` looks entries up.

    ``seen`` is an ordered mapping, not a plain ``set``: capping it
    below needs to know which entries are OLDEST, the same problem
    ``lib.store._prune_progress`` solves by parsing each entry's
    ``updated_at``. There is nothing to sort by here -- this file only
    ever needs to answer "have we shown this already", never "when" --
    so insertion order stands in for age instead: a plain ``dict``
    already preserves it (Python 3.7+), which is exactly why this is a
    dict of markers rather than a ``set`` (whose iteration order is not
    a usable proxy for insertion order).

    A key already present is re-inserted at the end, so marking the same
    episode seen twice (harmless -- it would not have been offered
    again by :func:`new_episodes` regardless) refreshes its position
    instead of leaving a stale duplicate "age" behind.

    Capped at :data:`MAX_SEEN_EPISODES`, oldest first, exactly like
    :data:`lib.store.MAX_PROGRESS_ENTRIES` bounds ``progress.json`` --
    simplified relative to that cap in one way: ``lib.store._prune_progress``
    also runs a separate periodic sweep that drops entries past a max
    AGE, which does not apply here (:func:`mark_seen` takes no ``now``)
    because an old *seen* marker is never harmful the way an old
    *progress* sample would be stale -- the episode it names has
    already fallen below its series' last-watched pointer by the time
    it would otherwise re-qualify, so it can never be offered again
    whether or not this set still remembers it. The count cap alone is
    what bounds this file's growth over a years-long install.
    """
    updated = dict(seen or {})
    for episode in episodes or []:
        if not isinstance(episode, dict):
            continue
        key = _seen_key(episode.get("type"), episode.get("id"), episode.get("video_id"))
        if key is None:
            continue
        updated.pop(key, None)
        updated[key] = True
    if len(updated) > MAX_SEEN_EPISODES:
        overflow = len(updated) - MAX_SEEN_EPISODES
        for key in list(updated)[:overflow]:
            del updated[key]
    return updated
