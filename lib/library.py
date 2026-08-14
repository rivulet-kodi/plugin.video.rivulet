"""Pure-Python LibraryItem construction and playback-progress merging --
Rivulet's half of what stremio-core calls the "library" ctx layer (no
``xbmc*`` imports anywhere in this module, so it is safe to unit test
with a plain ``python3`` interpreter).

Wire shape verified against ``~/M0Rf30/stremio-core`` (read-only checkout,
HEAD 7f69095a):

* ``src/types/library/library_item.rs:17-42`` (``LibraryItem`` field names
  and casing -- ``_id``/``_ctime``/``_mtime`` keep their underscore
  prefix, every other field is ``camelCase``) and ``:215-261``
  (``LibraryItemState`` -- ``timeWatched``/``timeOffset``/
  ``overallTimeWatched``/``duration`` are all MILLISECONDS; ``video_id``
  is the one field that keeps its snake_case wire name -- an explicit
  ``#[serde(rename = "video_id")]`` on an otherwise
  ``rename_all = "camelCase"`` struct).
* ``src/types/library/library_item.rs:176-195`` (``From<(&MetaItemPreview,
  PhantomData<E>)>`` -- the exact constructor stremio-core's own ctx
  layer uses the first time a title is watched without ever having been
  explicitly "Added to Library": ``removed``/``temp`` both start
  ``true``. See :func:`build_library_item`.
* ``src/types/library/library_item.rs:244-250`` (``state.video_id``'s doc
  comment) and ``src/models/player.rs:523-577``
  (``ActionPlayer::TimeChanged``'s handler -- the merge logic
  :func:`merge_playback` below mirrors step for step).
* ``src/unit_tests/ctx/add_to_library.rs:35-37`` pins the exact
  ``datastorePut`` wire body (key names, order, and ``null``/``0``/
  ``false`` defaults) that ``lib.stremio.api.StremioAPI.datastore_put``
  posts -- every dict this module builds uses that exact key order so a
  caller can hand one straight to ``datastore_put`` unmodified.

CRITICAL -- read this before touching ``state.watched`` anywhere:
``state.watched`` is a ``stremio-watched-bitfield``-encoded (a packed
per-episode bitfield; see the ``stremio-watched-bitfield`` Rust crate)
value this addon does not decode or encode. :func:`merge_playback` NEVER
computes or touches it -- it is carried over from the existing item
byte-for-byte, or left ``None`` for a brand-new item. Synthesising or
guessing this field would silently corrupt the user's REAL Stremio
library the next moment any official Stremio client reads it back.
"""
import datetime

#: Default `posterShape` for a freshly-built item. stremio-core's own
#: `PosterShape` enum defaults to `Poster` for anything else
#: (`#[default]` variant, src/types/resource/meta_item.rs:307-315).
DEFAULT_POSTER_SHAPE = 'poster'

#: Fraction of a video's `duration` that `state.timeWatched` must exceed
#: before this addon flags it "watched" (`flaggedWatched=1`, `timesWatched`
#: incremented once -- see `merge_playback`).
#:
#: NOTE: stremio-core's own player model uses a DIFFERENT, lower
#: coefficient for this exact check (`WATCHED_THRESHOLD_COEF = 0.7`,
#: src/constants.rs:43, consumed by `ActionPlayer::TimeChanged` at
#: src/models/player.rs:571-573) -- this addon deliberately requires a
#: stricter 90% instead (project decision for this feature), so the real
#: upstream figure is documented here rather than left for a future
#: reader to assume this constant is upstream-verified.
WATCHED_THRESHOLD_RATIO = 0.9

#: Every `LibraryItemState` time field is milliseconds on the wire
#: (library_item.rs:228-243); a playback position sampled off Kodi's own
#: `xbmc.Player.getTime()`/`getTotalTime()` is seconds (float). Callers
#: doing that conversion (see `lib.service_runner`) multiply by this
#: rather than a bare magic `1000`.
MS_PER_SECOND = 1000


def iso8601_utc(dt=None):
    """Format `dt` (default: now) as upstream's `_ctime`/`_mtime`/
    `state.lastWatched` wire format -- seconds-precision ISO 8601 UTC
    with a literal 'Z' suffix, matching chrono's `DateTime<Utc>`
    Serialize impl for a timestamp with no fractional seconds (see the
    pinned `"2020-01-01T00:00:00Z"` values in
    src/unit_tests/ctx/add_to_library.rs:37,65-68). A naive `dt` (no
    tzinfo) is treated as already being UTC.
    """
    dt = dt or datetime.datetime.now(datetime.timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _default_state(now_iso):
    """A brand-new `LibraryItemState` dict, key order matching the
    pinned `datastorePut` fixture exactly (`lastWatched` first, `watched`/
    `noNotif` last) -- see the module docstring."""
    return {
        'lastWatched': now_iso,
        'timeWatched': 0,
        'timeOffset': 0,
        'overallTimeWatched': 0,
        'timesWatched': 0,
        'flaggedWatched': 0,
        'duration': 0,
        'video_id': None,
        'watched': None,
        'noNotif': False,
    }


def _behavior_hints(meta):
    """`MetaItemBehaviorHints`'s 3 named wire fields (src/types/resource/
    meta_item.rs:422-431), read off `meta['behaviorHints']` with
    upstream's own per-field defaults. Any OTHER key that dict carries
    (upstream flattens unknown keys into a separate `.other` map) is
    dropped -- a `LibraryItem.behaviorHints` on the wire only ever has
    these three."""
    hints = (meta or {}).get('behaviorHints') or {}
    return {
        'defaultVideoId': hints.get('defaultVideoId'),
        'featuredVideoId': hints.get('featuredVideoId'),
        'hasScheduledVideos': bool(hints.get('hasScheduledVideos', False)),
    }


def build_library_item(meta, now=None):
    """Build a brand-new LibraryItem dict from a Stremio meta dict
    (`{'id', 'type', 'name', 'poster', 'posterShape', 'behaviorHints'}` --
    every key but `id` optional).

    Mirrors stremio-core's `From<(&MetaItemPreview, PhantomData<E>)>`
    (library_item.rs:176-195) field for field: `removed`/`temp` both
    start `true`. This is the SAME constructor stremio-core's own ctx
    layer uses the very first time a title is watched without ever
    having been explicitly "Added to Library" (see
    models/ctx/update_library.rs:159-163's `MetaItemMarkAsWatched`
    branch) -- it produces exactly the transient "recently watched, not
    in your library" record every official Stremio client also creates,
    never a permanent library entry (an explicit "Add to Library" action
    is what would flip `removed`/`temp` to `false` -- outside this
    module's scope).
    """
    now_iso = iso8601_utc(now)
    return {
        '_id': meta['id'],
        'name': meta.get('name', ''),
        'type': meta.get('type', ''),
        'poster': meta.get('poster'),
        'posterShape': meta.get('posterShape') or DEFAULT_POSTER_SHAPE,
        'removed': True,
        'temp': True,
        '_ctime': now_iso,
        '_mtime': now_iso,
        'state': _default_state(now_iso),
        'behaviorHints': _behavior_hints(meta),
    }


def _resolve_video_id(item, video_id):
    """The fallback chain library_item.rs:244-250 documents for
    `state.video_id`: the actually-played episode id when given, else
    `behaviorHints.defaultVideoId`, else the item's own `_id` (a movie,
    or any meta with no separate episodes, has no other video to name)."""
    if video_id:
        return video_id
    default_video_id = (item.get('behaviorHints') or {}).get('defaultVideoId')
    if default_video_id:
        return default_video_id
    return item.get('_id')


def merge_playback(item, position_ms, duration_ms, video_id=None, now=None):
    """Return a NEW LibraryItem dict (`item` is never mutated) merging
    one playback sample into `item['state']` -- mirrors stremio-core's
    `ActionPlayer::TimeChanged` handler (src/models/player.rs:523-577)
    step for step:

    1. `state.lastWatched` = now.
    2. Resolve the played video id (see `_resolve_video_id`). If it
       differs from the item's CURRENT `state.video_id` (a fresh item,
       or the user moved to a different episode): fold the old video's
       `timeWatched` into `overallTimeWatched` and restart
       `timeWatched`/`flaggedWatched` at 0 for the new video.
    3. Otherwise (same video as last sample): the delta since the
       item's last known `timeOffset` -- never negative, so a backward
       seek cannot *subtract* watched time -- is added to both
       `timeWatched` and `overallTimeWatched`.
    4. `timeOffset`/`duration` only advance when `position_ms` is
       actually AHEAD of the stored `timeOffset` -- guards against a
       backward seek corrupting the resume position (upstream's own
       comment: "if we seek forward, time will be < time_offset [...]
       for both backward and forward seeking we expect the apps to send
       the right actions").
    5. The first time (and only the first time -- gated by
       `flaggedWatched == 0`) `timeWatched` exceeds
       `duration_ms * WATCHED_THRESHOLD_RATIO`, `flaggedWatched` flips
       to 1 and `timesWatched` increments by exactly one.

    `state.watched` -- the per-episode `stremio-watched-bitfield` this
    addon does not implement -- is copied over completely UNTOUCHED, see
    the module docstring's CRITICAL note. Every other top-level field
    (`removed`/`temp`/`name`/`poster`/`behaviorHints`/...) is copied
    unchanged; only `_mtime` and `state` differ in the result.

    `position_ms`/`duration_ms` are clamped to `>= 0` (a negative sample
    -- e.g. a bogus `getTime()` reading -- must never corrupt stored
    progress); an item with no prior `state` at all gets a fresh default
    one first.

    Only shallow-copies what it actually mutates -- the top-level dict
    and `state` -- rather than `copy.deepcopy`-ing the whole item (which
    can run 8-12 KB, sampled on every playback-progress tick). Every
    field this function never writes (`poster`, `behaviorHints`, and
    `state.watched` itself) is carried into the result BY REFERENCE,
    shared with `item`'s own copy of it -- safe precisely because
    nothing below ever mutates one of those shared objects in place.
    """
    now_iso = iso8601_utc(now)
    original_state = item.get('state')
    if isinstance(original_state, dict):
        state = dict(original_state)
    else:
        state = _default_state(now_iso)

    position_ms = max(0, int(position_ms))
    duration_ms = max(0, int(duration_ms))
    resolved_video_id = _resolve_video_id(item, video_id)
    previous_offset = int(state.get('timeOffset') or 0)

    state['lastWatched'] = now_iso

    if state.get('video_id') != resolved_video_id:
        state['overallTimeWatched'] = int(state.get('overallTimeWatched') or 0) + int(state.get('timeWatched') or 0)
        state['timeWatched'] = 0
        state['flaggedWatched'] = 0
        state['video_id'] = resolved_video_id
        # A NEW video starts its own timeline: the previous video's
        # timeOffset/duration are meaningless for it (e.g. episode 1
        # finished at 100000ms, episode 2 starts at position~0) --
        # written directly (not just the local `previous_offset` below)
        # so a call landing at EXACTLY position_ms=0 for the new video
        # still clears the stale value (the `position_ms >
        # previous_offset` guard a few lines down is a strict `>`, so a
        # merely-reset local variable would leave `state['timeOffset']`
        # itself untouched for that one boundary sample). Upstream
        # achieves the same reset via a SEPARATE explicit
        # `advance_to_video`/next-video action fired before its own
        # TimeChanged; this addon has no such separate step, so the
        # reset lives here instead.
        state['timeOffset'] = 0
        state['duration'] = 0
        previous_offset = 0
    else:
        delta = max(0, position_ms - previous_offset)
        state['timeWatched'] = int(state.get('timeWatched') or 0) + delta
        state['overallTimeWatched'] = int(state.get('overallTimeWatched') or 0) + delta

    if position_ms > previous_offset:
        state['timeOffset'] = position_ms
        state['duration'] = duration_ms

    if not state.get('flaggedWatched') and duration_ms > 0 and state['timeWatched'] > duration_ms * WATCHED_THRESHOLD_RATIO:
        state['flaggedWatched'] = 1
        state['timesWatched'] = int(state.get('timesWatched') or 0) + 1

    item = dict(item)
    item['state'] = state
    item['_mtime'] = now_iso
    return item
