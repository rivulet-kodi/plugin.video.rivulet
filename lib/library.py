# SPDX-FileCopyrightText: 2026 M0Rf30
# SPDX-License-Identifier: MIT

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
``state.watched`` is a ``stremio-watched-bitfield``-encoded packed
per-episode bitfield, codec in :mod:`lib.stremio.watchedbitfield`.
:func:`merge_playback` NEVER computes or touches it -- it is carried over
byte-for-byte, or left ``None`` for a brand-new item. Only
:func:`mark_watched_payload` rewrites it, and only when the caller passes
the series' ``video_ids`` AND the codec can trust the existing string
(parseable, anchor still in the list); otherwise the original value is
kept untouched. Never synthesise or guess this field: a wrong value
silently corrupts the user's REAL Stremio library the next time any
official Stremio client reads it back.
"""
import datetime

from lib.stremio import watchedbitfield

#: Default `posterShape` for a freshly-built item. stremio-core's own
#: `PosterShape` enum defaults to `Poster` for anything else
#: (`#[default]` variant, src/types/resource/meta_item.rs:307-315).
DEFAULT_POSTER_SHAPE = 'poster'

#: Fraction of a video's `duration` that `state.timeWatched` must exceed
#: before this addon flags it "watched" (`flaggedWatched=1`, `timesWatched`
#: incremented once -- see `merge_playback`).
#:
#: This is stremio-core's own figure for the same check
#: (`WATCHED_THRESHOLD_COEF = 0.7`, src/constants.rs, consumed by the
#: `TimeChanged` arm of `src/models/player.rs`), and it has to stay equal
#: to it: the flag is what every other Stremio client reads back, so a
#: stricter value here leaves a movie stopped in its credits (70-90%)
#: watched on the official apps and unwatched everywhere Rivulet draws it
#: (issue #85). It is deliberately NOT the same number as the resume
#: prompt's `RESUME_MAX_PERCENT` (95%): that decides whether there is
#: anything left worth resuming into, this decides whether the title
#: counts as seen.
WATCHED_THRESHOLD_RATIO = 0.7

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
    hints = (meta or {}).get('behaviorHints')
    if not isinstance(hints, dict):
        # An addon's meta: a bool/string here is not worth an
        # `AttributeError` out of "Add to Library".
        hints = {}
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
    hints = item.get('behaviorHints')
    # `behaviorHints` is the account's JSON: a bool or string there must
    # fall through to `_id`, not raise `AttributeError` out of every push.
    default_video_id = hints.get('defaultVideoId') if isinstance(hints, dict) else None
    if default_video_id and isinstance(default_video_id, str):
        return default_video_id
    return item.get('_id')


def _whole(value):
    """A wire counter/time field as an ``int``, ``0`` when it is not one.

    ``LibraryItemState``'s fields are numbers on the wire, but the record
    is an account's JSON: a string, list or ``Infinity`` there (hand-edited,
    or written by a buggy third-party client) made ``int()`` raise
    ``TypeError``/``ValueError``/``OverflowError`` inside
    :func:`merge_playback` and :func:`mark_watched_payload`, and the caller
    then dropped that playback push (or the mark) on every attempt, for as
    long as the record stayed that way. A field that is not a number counts
    as the ``0`` a missing one does, which is what the writer then
    overwrites it with. Negative is not a number of milliseconds or of
    times watched either: a ``timesWatched`` of -1 made "Mark as watched"
    write 0, so the title never read as watched.
    """
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


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

    `state.watched` -- the per-episode `stremio-watched-bitfield` -- is
    copied over completely UNTOUCHED, see
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
    previous_offset = _whole(state.get('timeOffset'))

    state['lastWatched'] = now_iso

    if state.get('video_id') != resolved_video_id:
        state['overallTimeWatched'] = _whole(state.get('overallTimeWatched')) + _whole(state.get('timeWatched'))
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
        state['timeWatched'] = _whole(state.get('timeWatched')) + delta
        state['overallTimeWatched'] = _whole(state.get('overallTimeWatched')) + delta

    if position_ms > previous_offset:
        state['timeOffset'] = position_ms
        state['duration'] = duration_ms

    if not state.get('flaggedWatched') and duration_ms > 0 and state['timeWatched'] > duration_ms * WATCHED_THRESHOLD_RATIO:
        state['flaggedWatched'] = 1
        state['timesWatched'] = _whole(state.get('timesWatched')) + 1

    item = dict(item)
    item['state'] = state
    item['_mtime'] = now_iso
    return item


def library_add_payload(meta, now=None):
    """Build the LibraryItem an EXPLICIT "Add to Library" action pushes --
    the branch `build_library_item()`'s own docstring named but left
    "outside this module's scope" (library_item.rs:176-195's PhantomData
    constructor only ever produces the IMPLICIT `removed=True`/
    `temp=True` "recently watched, never added" record). Reuses
    `build_library_item()` for every other field (identity/type/name/
    poster/behaviorHints, a brand-new `state`) and flips just those two
    flags to `False` -- the wire signal that this record is a real,
    permanent library entry rather than a transient one.

    Deliberately built fresh from `meta` alone, never from any existing
    server-side record for this id: this addon caches no library items
    (`lib.ui.mystuff._read_library_entries()` re-fetches live every
    time for the same reason), and an explicit "Add" from a title the
    user has never played has no prior `state` to preserve anyway. A
    title that already has a `removed=True` implicit record (e.g. from
    `lib.service_runner`'s playback-progress push) simply gets that
    record's tombstone flag cleared by this same fresh payload landing
    on the SAME `_id` key server-side -- its `state` is not carried
    forward, matching the narrow "meta only" shape this function is
    given.
    """
    item = build_library_item(meta, now)
    item['removed'] = False
    item['temp'] = False
    return item


def library_add_from_existing(meta, existing=None, now=None):
    """The ``datastorePut`` body for an explicit Add to Library.

    ``existing`` is the account row pulled immediately before the put.
    Its ``state`` is kept, so the write does not replace another
    client's playback with a blank record. ``removed`` and ``temp``
    become false. ``_mtime`` is now. ``_ctime`` is now when the row is
    new or was a tombstone, which is the added time Library sorts on.
    An item that is already in the library keeps its original
    ``_ctime``. With no account row this is :func:`library_add_payload`.
    """
    meta = meta or {}
    if not isinstance(existing, dict) or not existing.get('_id'):
        return library_add_payload(meta, now)
    now_iso = iso8601_utc(now)
    result = dict(existing)
    state = result.get('state')
    result['state'] = dict(state) if isinstance(state, dict) else _default_state(now_iso)
    was_removed = bool(result.get('removed'))
    result['removed'] = False
    result['temp'] = False
    result['_mtime'] = now_iso
    if was_removed or not result.get('_ctime'):
        result['_ctime'] = now_iso
    if meta.get('name') and not result.get('name'):
        result['name'] = meta.get('name')
    if meta.get('type') and not result.get('type'):
        result['type'] = meta.get('type')
    if meta.get('poster') and not result.get('poster'):
        result['poster'] = meta.get('poster')
    return result


def library_remove_payload(item, now=None):
    """Return a NEW LibraryItem dict (`item` is never mutated) flagging
    `item` removed -- Stremio's library "delete" is a tombstone
    (`removed=True`), never an actual deletion, precisely so the removal
    itself syncs to every other device on the next `datastoreGet`
    instead of the record just vanishing locally and resurrecting the
    moment another device's own pull re-adds it.

    Every OTHER field of `item` -- including `temp`, `state`, and
    everything else `library_add_payload()`/`merge_playback()` may have
    already set -- is carried through completely unchanged; only
    `removed` and `_mtime` differ in the result, the same "only
    shallow-copy what actually changed" shape `merge_playback()` already
    uses above.
    """
    result = dict(item)
    result['removed'] = True
    result['_mtime'] = iso8601_utc(now)
    return result


def mark_watched_payload(item, watched, now=None, video_ids=None):
    """Return a NEW LibraryItem dict (`item` is never mutated) setting
    `item['state']['flaggedWatched']` to `watched` -- the explicit
    "Mark as watched"/"Mark as unwatched" action, using exactly the
    `flaggedWatched`/`timesWatched` pair `merge_playback()` already
    flips automatically at the 70% threshold (see
    `WATCHED_THRESHOLD_RATIO`); this function just lets the user set the
    same two fields by hand instead of waiting for enough playback.

    `timesWatched` increments by exactly one ONLY on the 0->1
    transition (`timesWatched` was 0 and `watched` is True), which is
    what makes this function idempotent: feeding its own output back in
    with the same `watched` value is a no-op past the first call, since
    the second call's `was_watched` reads back the count the first call
    just wrote.

    Marking unwatched resets `timesWatched` to 0, as stremio-core's own
    `LibraryItem::mark_as_watched(false)` does. That counter is not a
    private history here: the official clients decide "watched" from
    `timesWatched > 0` alone (`LibraryItem::watched()`), so an unmark that
    left it standing was undone on every other device (issue #85) and
    Rivulet's own badge (`title_verdicts`) could never clear.

    An `item` with no `state` at all yet (never played, never added)
    gets a fresh default one first, same as `merge_playback()`.

    `video_ids` (a series' video ids in `watchedbitfield.sorted_video_ids`
    order) additionally sets/clears every episode's bit in
    `state.watched`, so official clients show the same per-episode marks.
    If the existing string cannot be trusted the field is left as it was
    -- see the module docstring's CRITICAL note.
    """
    now_iso = iso8601_utc(now)
    original_state = item.get('state')
    state = dict(original_state) if isinstance(original_state, dict) else _default_state(now_iso)

    # `timesWatched`, not `flaggedWatched`: it is what "watched" means
    # upstream, and a record left at flag 1 / times 0 by an official unmark
    # must still count up when marked again.
    was_watched = _whole(state.get('timesWatched')) > 0
    state['flaggedWatched'] = 1 if watched else 0
    if watched and not was_watched:
        state['timesWatched'] = _whole(state.get('timesWatched')) + 1
    elif not watched:
        state['timesWatched'] = 0
    if video_ids:
        updated = watchedbitfield.set_all_watched(state.get('watched'), video_ids, watched)
        if updated is not None:
            state['watched'] = updated

    result = dict(item)
    result['state'] = state
    result['_mtime'] = now_iso
    return result


def _normalize_iso(value):
    """`value` -- a Stremio wire timestamp, which chrono emits WITH
    fractional seconds whenever they are non-zero (e.g.
    ``"2026-10-20T21:14:03.512Z"``, unlike the seconds-only form
    :func:`iso8601_utc` writes) -- re-rendered in :func:`iso8601_utc`'s
    exact seconds-precision form, or ``None`` when it is missing or not
    parseable. The local progress cache compares ``updated_at`` values
    as plain strings, which is only sound when every one of them shares
    this single fixed-width format."""
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith('Z'):
        text = text[:-1] + '+00:00'
    if '.' in text:
        head, _, tail = text.partition('.')
        offset = ''
        for sign in ('+', '-'):
            if sign in tail:
                offset = sign + tail.split(sign, 1)[1]
                break
        text = head + offset
    try:
        parsed = datetime.datetime.strptime(text.replace('+00:00', '+0000'), '%Y-%m-%dT%H:%M:%S%z')
    except ValueError:
        return None
    return iso8601_utc(parsed)


def progress_from_library_item(item):
    """Project one account `libraryItem` record onto the local progress
    cache's shape -- ``{'type', 'id', 'video_id', 'position_ms',
    'duration_ms', 'updated_at'}`` (the same keys
    ``lib.store.Store.get_progress_entries`` returns) -- or ``None`` when
    the record carries nothing worth resuming or listing as finished.

    The inverse of what :func:`merge_playback` pushes, so a title watched
    in an official Stremio client shows up in Rivulet's Continue
    Watching/Next Up/Recently Watched bands (issue #74):

    * ``video_id`` is the played episode for a series; a movie (or any
      record whose ``state.video_id`` is just its own ``_id``) maps to
      ``None``, the key ``lib.ui.player`` records movie progress under.
    * ``flaggedWatched`` maps to ``position_ms == duration_ms`` -- the
      local cache has no watched flag, it derives "finished" from the
      percentage (``lib.ui.mystuff``), so a finished title must read as
      100%. A flagged record with no known duration uses ``1/1`` for the
      same reason.
    * Skipped: a tombstoned record that is not also ``temp`` (stremio-
      core's own continue-watching filter is ``!removed || temp``), one
      never played (no ``timeOffset`` and not flagged watched), one with
      no usable ``lastWatched`` (nothing to order it by), and a series
      record without an episode id (nothing to resume into).
    """
    if not isinstance(item, dict):
        return None
    item_id = item.get('_id')
    content_type = item.get('type')
    state = item.get('state')
    # Strings only: the entry is keyed into `progress.json` by joining its
    # id and type, so a numeric/bool/list `_id` or `type` in a hostile
    # record would raise `TypeError` in `Store.merge_remote_progress` and
    # abort the whole pull, not just skip this record.
    if not isinstance(item_id, str) or not isinstance(content_type, str):
        return None
    if not item_id or not content_type or not isinstance(state, dict):
        return None
    if item.get('removed') and not item.get('temp'):
        return None
    updated_at = _normalize_iso(state.get('lastWatched'))
    if updated_at is None:
        return None
    try:
        offset = max(0, int(state.get('timeOffset') or 0))
        duration = max(0, int(state.get('duration') or 0))
    except (TypeError, ValueError, OverflowError):
        # OverflowError: Python's `json` accepts the literal `Infinity`, and
        # `int(float('inf'))` raises it -- one hostile record must not abort
        # the whole library pull.
        return None
    if state.get('flaggedWatched'):
        if duration <= 0:
            duration = 1
        offset = duration
    elif offset <= 0 or duration <= 0:
        return None
    video_id = state.get('video_id')
    if not isinstance(video_id, str) or not video_id or video_id == item_id:
        video_id = None
    if content_type == 'series' and video_id is None:
        return None
    if content_type != 'series':
        video_id = None
    return {
        'type': content_type,
        'id': item_id,
        'video_id': video_id,
        'position_ms': min(offset, duration),
        'duration_ms': duration,
        'updated_at': updated_at,
    }


def item_watched(item):
    """Whether one account `libraryItem` record counts as watched.

    ``state.timesWatched > 0`` is stremio-core's own answer
    (``LibraryItem::watched()``) and the only one used here:
    ``mark_as_watched(false)`` resets just ``timesWatched`` and leaves
    ``flaggedWatched`` as it was, so a record can carry a stale
    ``flaggedWatched = 1`` that every official client already reads as
    unwatched. Every writer that marks a title watched bumps
    ``timesWatched`` (an official mark, a playback past
    :data:`WATCHED_THRESHOLD_RATIO`, and this addon's explicit mark), so
    nothing is lost by ignoring the flag.

    The same rule for every type, deliberately: Stremio draws the check
    on a series poster once ``timesWatched`` is above zero, and for a
    series that counter goes up once per episode played past
    :data:`WATCHED_THRESHOLD_RATIO`, so "one episode seen" is what the
    official apps show and what this reproduces (issue #85). It is not
    "the whole show is finished".
    """
    state = item.get('state') if isinstance(item, dict) else None
    if not isinstance(state, dict):
        return False
    try:
        return int(state.get('timesWatched') or 0) > 0
    except (TypeError, ValueError, OverflowError):
        return False


def title_verdicts(entries):
    """``{title id: (watched, mtime)}`` for every typed record in
    `entries` (a ``libraryItem`` datastore read), movies and series alike.

    ``mtime`` is the record's ``_mtime`` in :func:`iso8601_utc`'s form
    (``''`` when absent/unparseable): it is what lets
    :func:`watched_title_ids` decide whether the account or a local
    playback sample is the more recent word. Tombstoned (``removed``)
    records are kept: removing a title from the library does not unwatch
    it, and a title watched without ever being added is a ``temp``,
    ``removed`` record by construction (:func:`build_library_item`).
    A record with no type is skipped, as everything downstream is keyed by
    a typed card.
    """
    verdicts = {}
    for entry in entries or []:
        if not isinstance(entry, dict) or not entry.get('type'):
            continue
        item_id = entry.get('_id')
        if not isinstance(item_id, str) or not item_id:
            continue
        verdicts[item_id] = (item_watched(entry), _normalize_iso(entry.get('_mtime')) or '')
    return verdicts


def progress_finished(entry):
    """Whether a local progress entry (`Store.get_progress_entries()`
    shape) has played past :data:`WATCHED_THRESHOLD_RATIO` of its
    duration. Strictly past, like :func:`merge_playback`'s own check.

    This reads where playback stopped, while the account flag counts time
    actually watched, so a viewer who skipped ahead can read as finished
    here before the account agrees -- a provisional answer
    :func:`watched_title_ids` hands back to the account the moment the
    account has a newer word.
    """
    try:
        position = int(entry.get('position_ms') or 0)
        duration = int(entry.get('duration_ms') or 0)
        # Inside the `try`: an integer past ~1.8e308 (a JSON number is
        # unbounded) converts fine above but raises OverflowError when the
        # ratio turns it into a float. Such a duration is garbage, and
        # garbage never counts as finished.
        return duration > 0 and position > duration * WATCHED_THRESHOLD_RATIO
    except (AttributeError, TypeError, ValueError, OverflowError):
        return False


def watched_title_ids(verdicts, progress_entries):
    """The frozenset of title ids (movies and series) that should wear the
    watched badge.

    `verdicts` is :func:`title_verdicts` of the account's library (empty
    when signed out or the library has not been read), `progress_entries`
    is `Store.get_progress_entries()`.

    * An account record that says watched wins outright.
    * A title with finished local progress (:func:`progress_finished`) is
      watched: a movie's own entry, or any one episode's entry for a
      series (Stremio's rule is "at least one episode", see
      :func:`item_watched`), even before the playback push has landed on the account,
      which lags by up to ``LIBRARY_PUSH_INTERVAL_SECONDS`` -- UNLESS the
      account has a record at least as new as that local sample. The
      record is then the later word, and for an explicit "Mark as
      unwatched" it is the whole point: the stale 100% sample would
      otherwise keep the badge on for good (local progress has no way to
      say "unwatched").

    Pure and cheap (one pass over each input), so callers compute it once
    per paint and look titles up in the result.
    """
    marks = {item_id for item_id, (watched, _mtime) in verdicts.items() if watched}
    for entry in progress_entries or []:
        if not isinstance(entry, dict) or not entry.get('type'):
            continue
        item_id = entry.get('id')
        if not isinstance(item_id, str) or item_id in marks or not progress_finished(entry):
            continue
        account = verdicts.get(item_id)
        if account is not None and account[1] >= str(entry.get('updated_at') or ''):
            continue
        marks.add(item_id)
    return frozenset(marks)


def newer_verdicts(older, newer):
    """Two :func:`title_verdicts` maps folded into one, keeping per title
    the verdict with the later ``mtime`` (``newer`` on a tie).

    Two reads of the same account can disagree only by age: the cached
    copy the background pull wrote (``Store.get_library_cache``) and a
    live read this process made since (Home's Continue/Library rows).
    Neither is newer as a whole -- the pull may have run after the live
    read, or a title may have changed on another device in between -- so
    the comparison is per record, on the record's own ``_mtime``.
    """
    merged = dict(older)
    for item_id, verdict in newer.items():
        current = merged.get(item_id)
        if current is None or verdict[1] >= current[1]:
            merged[item_id] = verdict
    return merged


def episode_ticked(percent):
    """Whether an episode row that displays `percent` (the rounded 0-100
    "N% WATCHED" figure the series page prints from local progress, or
    ``None`` when it prints nothing) wears the watched tick.

    The tick sits next to that printed figure, so it is decided on the
    same rounded number, at :data:`WATCHED_THRESHOLD_RATIO` -- the point
    the account counts the episode as watched -- rather than on
    :func:`progress_finished`'s strict ``>`` over raw milliseconds: a row
    reading "70% WATCHED" with no tick beside it would look like a bug.
    """
    return percent is not None and percent >= round(WATCHED_THRESHOLD_RATIO * 100)


#: The ``libraryItem`` fields ``Store.get_library_cache`` keeps per record:
#: exactly what :func:`title_verdicts` and the pending-write overlay
#: (``lib.ui.libraryaction.apply_library_writes``) read. A full record runs
#: to a kilobyte (name, poster, behaviour hints, the episode bitfield); a
#: slim one is under a hundred bytes, which is what lets the service
#: rewrite the file on every pull. A new reader of the cache that needs
#: another field adds it here.
_SLIM_ITEM_KEYS = ('_id', 'type', 'removed', '_mtime')
_SLIM_STATE_KEYS = ('timesWatched',)


def slim_library_item(item):
    """`item` (one account ``libraryItem`` record) cut down to the fields
    the watched verdict reads, still in wire shape (``state`` nested) so
    :func:`title_verdicts` accepts it unchanged; or ``None`` for a record
    with no string ``_id`` to key it by."""
    if not isinstance(item, dict):
        return None
    item_id = item.get('_id')
    if not isinstance(item_id, str) or not item_id:
        return None
    slim = {key: item.get(key) for key in _SLIM_ITEM_KEYS}
    state = item.get('state')
    if isinstance(state, dict):
        slim['state'] = {key: state.get(key) for key in _SLIM_STATE_KEYS}
    return slim
