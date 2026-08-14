"""Tests for lib.library: pure-Python LibraryItem construction and
playback-progress merging (no `xbmc*` imports, no network).

Reference: ~/M0Rf30/stremio-core (read-only checkout) --
src/types/library/library_item.rs (LibraryItem/LibraryItemState field
shapes and the PhantomData meta-preview constructor), src/models/player.rs
(ActionPlayer::TimeChanged's merge semantics :func:`lib.library.
merge_playback` mirrors), src/unit_tests/ctx/add_to_library.rs (the
pinned datastorePut wire fixture).
"""
import datetime

import lib.library as library

NOW = datetime.datetime(2020, 1, 1, 0, 0, 0, tzinfo=datetime.timezone.utc)
LATER = datetime.datetime(2020, 1, 1, 0, 5, 0, tzinfo=datetime.timezone.utc)
NOW_ISO = "2020-01-01T00:00:00Z"
LATER_ISO = "2020-01-01T00:05:00Z"


def _meta(**overrides):
    meta = {"id": "tt1", "name": "A Movie", "type": "movie"}
    meta.update(overrides)
    return meta


# --- iso8601_utc -------------------------------------------------------


def test_iso8601_utc_formats_seconds_precision_with_z_suffix():
    assert library.iso8601_utc(NOW) == NOW_ISO


def test_iso8601_utc_converts_non_utc_timezone():
    tz = datetime.timezone(datetime.timedelta(hours=2))
    dt = datetime.datetime(2020, 1, 1, 2, 0, 0, tzinfo=tz)
    assert library.iso8601_utc(dt) == NOW_ISO


def test_iso8601_utc_treats_naive_datetime_as_utc():
    dt = datetime.datetime(2020, 1, 1, 0, 0, 0)
    assert library.iso8601_utc(dt) == NOW_ISO


def test_iso8601_utc_defaults_to_now_when_omitted():
    result = library.iso8601_utc()
    assert result.endswith("Z")
    assert len(result) == len(NOW_ISO)


# --- build_library_item -------------------------------------------------


def test_build_library_item_matches_pinned_upstream_shape():
    """Mirrors library_item.rs:176-195's PhantomData constructor: a
    brand-new item starts removed=True, temp=True (see the module
    docstring for why -- this is the "recently watched, not explicitly
    added to library" record, not a permanent library entry)."""
    item = library.build_library_item(_meta(), now=NOW)

    assert item == {
        "_id": "tt1",
        "name": "A Movie",
        "type": "movie",
        "poster": None,
        "posterShape": "poster",
        "removed": True,
        "temp": True,
        "_ctime": NOW_ISO,
        "_mtime": NOW_ISO,
        "state": {
            "lastWatched": NOW_ISO,
            "timeWatched": 0,
            "timeOffset": 0,
            "overallTimeWatched": 0,
            "timesWatched": 0,
            "flaggedWatched": 0,
            "duration": 0,
            "video_id": None,
            "watched": None,
            "noNotif": False,
        },
        "behaviorHints": {
            "defaultVideoId": None,
            "featuredVideoId": None,
            "hasScheduledVideos": False,
        },
    }


def test_build_library_item_carries_poster_and_poster_shape():
    item = library.build_library_item(
        _meta(poster="https://example.com/p.jpg", posterShape="landscape")
    )
    assert item["poster"] == "https://example.com/p.jpg"
    assert item["posterShape"] == "landscape"


def test_build_library_item_carries_behavior_hints():
    item = library.build_library_item(_meta(behaviorHints={
        "defaultVideoId": "tt1:1:1", "featuredVideoId": "tt1:1:2", "hasScheduledVideos": True,
    }))
    assert item["behaviorHints"] == {
        "defaultVideoId": "tt1:1:1", "featuredVideoId": "tt1:1:2", "hasScheduledVideos": True,
    }


def test_build_library_item_defaults_name_and_type_when_absent():
    item = library.build_library_item({"id": "tt1"})
    assert item["name"] == ""
    assert item["type"] == ""


# --- merge_playback: ms passthrough / basic accumulation ------------------


def test_merge_playback_sets_time_offset_and_duration_from_first_sample():
    item = library.build_library_item(_meta(), now=NOW)
    merged = library.merge_playback(item, 5000, 90000, video_id="tt1", now=LATER)
    assert merged["state"]["timeOffset"] == 5000
    assert merged["state"]["duration"] == 90000


def test_merge_playback_never_mutates_the_input_item():
    item = library.build_library_item(_meta(), now=NOW)
    snapshot = dict(item)
    library.merge_playback(item, 5000, 90000, video_id="tt1", now=LATER)
    assert item == snapshot


def test_merge_playback_updates_mtime_and_last_watched():
    item = library.build_library_item(_meta(), now=NOW)
    merged = library.merge_playback(item, 1000, 90000, video_id="tt1", now=LATER)
    assert merged["_mtime"] == LATER_ISO
    assert merged["state"]["lastWatched"] == LATER_ISO


def test_merge_playback_accumulates_time_watched_across_calls_for_the_same_video():
    """A fresh item's `state.video_id` starts `None`, so its VERY FIRST
    merge always takes the "video changed" branch (matching upstream:
    selecting ANY video, even for the first time, resets `timeWatched`
    to 0) -- real accumulation only shows up from the SECOND sample of
    the same video onward."""
    item = library.build_library_item(_meta(), now=NOW)
    c1 = library.merge_playback(item, 1000, 90000, video_id="tt1", now=NOW)
    assert c1["state"]["timeWatched"] == 0
    assert c1["state"]["timeOffset"] == 1000

    c2 = library.merge_playback(c1, 4000, 90000, video_id="tt1", now=LATER)
    assert c2["state"]["timeWatched"] == 3000  # delta since c1's timeOffset (1000)
    assert c2["state"]["overallTimeWatched"] == 3000
    assert c2["state"]["timeOffset"] == 4000


def test_merge_playback_backward_seek_does_not_advance_time_offset_or_duration():
    item = library.build_library_item(_meta(), now=NOW)
    item["state"]["video_id"] = "tt1"  # already-known video: no first-call reset noise
    forward = library.merge_playback(item, 50000, 90000, video_id="tt1", now=NOW)
    assert forward["state"]["timeOffset"] == 50000

    backward = library.merge_playback(forward, 10000, 90000, video_id="tt1", now=LATER)
    assert backward["state"]["timeOffset"] == 50000  # unchanged, not moved back to 10000
    assert backward["state"]["duration"] == 90000


def test_merge_playback_backward_seek_does_not_subtract_watched_time():
    item = library.build_library_item(_meta(), now=NOW)
    item["state"]["video_id"] = "tt1"
    forward = library.merge_playback(item, 50000, 90000, video_id="tt1", now=NOW)
    time_watched_after_forward = forward["state"]["timeWatched"]
    assert time_watched_after_forward == 50000

    backward = library.merge_playback(forward, 10000, 90000, video_id="tt1", now=LATER)
    assert backward["state"]["timeWatched"] == time_watched_after_forward  # delta clamped to 0


def test_merge_playback_clamps_negative_position_and_duration_to_zero():
    item = library.build_library_item(_meta(), now=NOW)
    merged = library.merge_playback(item, -500, -10, video_id="tt1", now=NOW)
    assert merged["state"]["timeOffset"] == 0
    assert merged["state"]["duration"] == 0


# --- merge_playback: video_id resolution/fallback chain ------------------


def test_merge_playback_uses_explicit_video_id_when_given():
    item = library.build_library_item(_meta(type="series"), now=NOW)
    merged = library.merge_playback(item, 1000, 90000, video_id="tt1:1:2", now=NOW)
    assert merged["state"]["video_id"] == "tt1:1:2"


def test_merge_playback_falls_back_to_default_video_id_when_none_given():
    item = library.build_library_item(
        _meta(behaviorHints={"defaultVideoId": "tt1:default"}), now=NOW
    )
    merged = library.merge_playback(item, 1000, 90000, video_id=None, now=NOW)
    assert merged["state"]["video_id"] == "tt1:default"


def test_merge_playback_falls_back_to_meta_id_when_no_video_id_or_default():
    item = library.build_library_item(_meta(), now=NOW)  # no behaviorHints.defaultVideoId
    merged = library.merge_playback(item, 1000, 90000, video_id=None, now=NOW)
    assert merged["state"]["video_id"] == "tt1"  # the item's own _id


def test_merge_playback_video_id_change_resets_time_watched_and_folds_into_overall():
    item = library.build_library_item(_meta(type="series"), now=NOW)
    item["state"]["video_id"] = "tt1:1:1"  # already watching this episode
    watched_ep1 = library.merge_playback(item, 60000, 90000, video_id="tt1:1:1", now=NOW)
    assert watched_ep1["state"]["timeWatched"] == 60000

    switched = library.merge_playback(watched_ep1, 5000, 100000, video_id="tt1:1:2", now=LATER)
    assert switched["state"]["video_id"] == "tt1:1:2"
    assert switched["state"]["timeWatched"] == 0
    assert switched["state"]["flaggedWatched"] == 0
    # overallTimeWatched had already accumulated 60000 while ep1 played (the
    # "same video" branch adds to it too, matching player.rs:550-556), then
    # gains ep1's final timeWatched (60000) again on the switch itself
    # (player.rs:541-546) -- ported byte-for-byte from upstream.
    assert switched["state"]["overallTimeWatched"] == 120000


def test_merge_playback_video_id_change_resets_stale_time_offset_even_at_position_zero():
    """Without an explicit reset, a call landing at EXACTLY position=0
    right when the video switches would otherwise leave the OLD video's
    timeOffset in place (the forward-only `position_ms > previous_offset`
    guard is a strict `>`), silently discarding the new video's early
    watched time on every subsequent sample. Regression coverage for
    that."""
    item = library.build_library_item(_meta(type="series"), now=NOW)
    item["state"]["video_id"] = "tt1:1:1"
    watched_ep1 = library.merge_playback(item, 91000, 100000, video_id="tt1:1:1", now=NOW)
    assert watched_ep1["state"]["flaggedWatched"] == 1

    switched = library.merge_playback(watched_ep1, 0, 100000, video_id="tt1:1:2", now=LATER)
    assert switched["state"]["timeOffset"] == 0  # not left at ep1's stale 91000

    resumed = library.merge_playback(switched, 91000, 100000, video_id="tt1:1:2", now=LATER)
    assert resumed["state"]["timeWatched"] == 91000  # accumulated from the reset baseline


# --- merge_playback: watched threshold (90%) ------------------------------


def test_merge_playback_below_threshold_does_not_flag_watched():
    item = library.build_library_item(_meta(), now=NOW)
    item["state"]["video_id"] = "tt1"
    merged = library.merge_playback(item, 89000, 100000, video_id="tt1", now=NOW)
    assert merged["state"]["flaggedWatched"] == 0
    assert merged["state"]["timesWatched"] == 0


def test_merge_playback_crossing_threshold_flags_watched_and_increments_times_watched():
    item = library.build_library_item(_meta(), now=NOW)
    item["state"]["video_id"] = "tt1"
    merged = library.merge_playback(item, 91000, 100000, video_id="tt1", now=NOW)
    assert merged["state"]["flaggedWatched"] == 1
    assert merged["state"]["timesWatched"] == 1


def test_merge_playback_exactly_at_threshold_boundary_not_yet_flagged():
    """`WATCHED_THRESHOLD_RATIO` is a strict `>`, not `>=` -- exactly 90%
    watched time has not yet crossed it."""
    item = library.build_library_item(_meta(), now=NOW)
    item["state"]["video_id"] = "tt1"
    duration_ms = 100000
    exact_threshold = int(duration_ms * library.WATCHED_THRESHOLD_RATIO)
    merged = library.merge_playback(item, exact_threshold, duration_ms, video_id="tt1", now=NOW)
    assert merged["state"]["flaggedWatched"] == 0
    assert merged["state"]["timesWatched"] == 0


def test_merge_playback_threshold_flips_exactly_once_across_repeated_calls():
    """Repeated samples past the threshold for the SAME video must not
    keep incrementing timesWatched."""
    item = library.build_library_item(_meta(), now=NOW)
    item["state"]["video_id"] = "tt1"
    merged = library.merge_playback(item, 91000, 100000, video_id="tt1", now=NOW)
    assert merged["state"]["timesWatched"] == 1

    merged = library.merge_playback(merged, 95000, 100000, video_id="tt1", now=LATER)
    assert merged["state"]["timesWatched"] == 1
    assert merged["state"]["flaggedWatched"] == 1


def test_merge_playback_next_episode_can_flag_watched_again():
    """flaggedWatched resets to 0 on a video_id change, so a series item
    can flag "watched" once PER EPISODE -- not just once ever."""
    item = library.build_library_item(_meta(type="series"), now=NOW)
    item["state"]["video_id"] = "tt1:1:1"
    ep1 = library.merge_playback(item, 91000, 100000, video_id="tt1:1:1", now=NOW)
    assert ep1["state"]["timesWatched"] == 1

    ep2_start = library.merge_playback(ep1, 0, 100000, video_id="tt1:1:2", now=LATER)
    assert ep2_start["state"]["flaggedWatched"] == 0

    ep2_watched = library.merge_playback(ep2_start, 91000, 100000, video_id="tt1:1:2", now=LATER)
    assert ep2_watched["state"]["flaggedWatched"] == 1
    assert ep2_watched["state"]["timesWatched"] == 2


# --- merge_playback: state.watched bitfield NEVER synthesised -------------


def test_merge_playback_carries_over_watched_bitfield_untouched():
    """CRITICAL invariant: this addon does not implement the
    stremio-watched-bitfield encoding, so an existing item's real
    `state.watched` value must survive a merge byte-for-byte."""
    item = library.build_library_item(_meta(type="series"), now=NOW)
    item["state"]["watched"] = "eJwDAAAAAAE="  # opaque real bitfield, must never be touched
    item["state"]["video_id"] = "tt1:1:1"
    merged = library.merge_playback(item, 91000, 100000, video_id="tt1:1:1", now=LATER)
    assert merged["state"]["watched"] == "eJwDAAAAAAE="


def test_merge_playback_leaves_watched_as_none_for_a_fresh_item():
    item = library.build_library_item(_meta(), now=NOW)
    assert item["state"]["watched"] is None
    merged = library.merge_playback(item, 1000, 90000, video_id="tt1", now=NOW)
    assert merged["state"]["watched"] is None


def test_merge_playback_preserves_other_top_level_fields_unchanged():
    item = library.build_library_item(_meta(poster="https://example.com/p.jpg"), now=NOW)
    merged = library.merge_playback(item, 1000, 90000, video_id="tt1", now=LATER)
    assert merged["_id"] == item["_id"]
    assert merged["name"] == item["name"]
    assert merged["type"] == item["type"]
    assert merged["poster"] == item["poster"]
    assert merged["removed"] == item["removed"]
    assert merged["temp"] == item["temp"]
    assert merged["_ctime"] == item["_ctime"]
    assert merged["behaviorHints"] == item["behaviorHints"]


def test_merge_playback_tolerates_item_with_no_state_key_at_all():
    """A caller-supplied dict missing `state` entirely (e.g. a
    hand-built minimal fixture) must still merge cleanly, not raise."""
    item = {"_id": "tt1", "name": "n", "type": "movie", "behaviorHints": {}}
    merged = library.merge_playback(item, 1000, 90000, video_id="tt1", now=NOW)
    assert merged["state"]["timeOffset"] == 1000
    assert merged["state"]["duration"] == 90000
