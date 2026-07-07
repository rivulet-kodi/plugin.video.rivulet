"""Tests for lib.stremio.streaminfo (pure text/label formatting helpers).

No network access; these functions are pure string/dict transforms over
Stream protocol objects (stremio-protocol-spec.md #3) and AIOStreams-style
addon output (multi-line, emoji-decorated name/title/description).
"""
import re
import time

import pytest

from lib.stremio.streaminfo import (
    _MAX_TEXT_LEN,
    clean_text,
    format_label,
    format_plot,
    parse_stream,
    sort_streams,
)

# --- fixtures ----------------------------------------------------------

#: Deliberately different from the "4.39 GB" mentioned in the description
#: text below, so tests can assert behaviorHints.videoSize wins.
AIOSTREAMS_VIDEO_SIZE = 5368709120  # 5 GiB

AIOSTREAMS_FILENAME = "Movie.Title.2024.2160p.WEB-DL.HEVC-GROUP.mkv"

AIOSTREAMS_STREAM = {
    "name": "[AIOStreams Stable] 4K (p2p)",
    "title": "Movie.Title.2024.2160p.WEB-DL.HEVC-GROUP",
    "description": (
        "\U0001F3AC Movie.Title.2024.2160p.WEB-DL.HEVC-GROUP\n"
        "\U0001F4BE 4.39 GB   DV \u00b7 HDR10+\n"
        "\U0001F331 Seeds: 50   \u26a1 3.3 Mbps"
    ),
    "infoHash": "ab" * 20,
    "fileIdx": 0,
    "behaviorHints": {
        "videoSize": AIOSTREAMS_VIDEO_SIZE,
        "filename": AIOSTREAMS_FILENAME,
    },
}

EXPECTED_INFO_KEYS = {
    "addon",
    "title",
    "resolution",
    "source",
    "codec",
    "hdr",
    "size_bytes",
    "size_text",
    "seeders",
    "is_torrent",
    "filename",
    "raw",
}


def _info(resolution="", seeders=None, size_bytes=None, **extra):
    base = {
        "addon": "test",
        "title": "t",
        "resolution": resolution,
        "source": "",
        "codec": "",
        "hdr": [],
        "size_bytes": size_bytes,
        "size_text": "",
        "seeders": seeders,
        "is_torrent": True,
        "filename": "",
        "raw": "",
    }
    base.update(extra)
    return base


# --- clean_text ----------------------------------------------------------


def test_clean_text_strips_emoji():
    assert "\U0001F3AC" not in clean_text("\U0001F3AC Movie Title")


def test_clean_text_strips_non_bmp_symbols():
    # Mathematical alphanumeric symbol, U+1D400 (outside the BMP)
    s = clean_text("Hello \U0001D400 World")
    assert "\U0001D400" not in s


def test_clean_text_strips_zero_width_chars():
    s = clean_text("Hello\u200bWorld\u200c\u200dFoo")
    assert "\u200b" not in s
    assert "\u200c" not in s
    assert "\u200d" not in s


def test_clean_text_collapses_newlines_to_single_space():
    s = clean_text("Line1\n\nLine2")
    assert "\n" not in s
    assert "Line1" in s and "Line2" in s


def test_clean_text_collapses_whitespace_runs():
    s = clean_text("A   B\t\tC")
    assert "  " not in s
    assert s == s.strip()


def test_clean_text_keeps_accented_latin():
    s = clean_text("Caf\u00e9 M\u00fcnster")
    assert "Caf\u00e9" in s
    assert "M\u00fcnster" in s


def test_clean_text_keeps_digits_and_common_punctuation():
    s = clean_text("S:12 - 4.39GB!")
    assert "S:12" in s
    assert "4.39GB" in s
    assert "!" in s


def test_clean_text_keeps_cjk():
    s = clean_text("\u5f71\u7247 2024")
    assert "\u5f71\u7247" in s


def test_clean_text_handles_empty_and_none_like_input():
    assert clean_text("") == ""


def test_clean_text_truncates_huge_input_and_returns_promptly():
    # A malicious/broken addon is semi-trusted, arbitrary user-installed
    # code and can hand us an unbounded title/description. clean_text()
    # must not scale its work (junk-char scan + whitespace regex) with
    # input size -- truncate first, then clean the truncated result.
    huge = "Stream Title " * 400_000  # ~5.2 MB
    start = time.monotonic()
    result = clean_text(huge)
    elapsed = time.monotonic() - start
    assert len(result) <= _MAX_TEXT_LEN
    # Generous ceiling: a regex/scan blowup regression would take orders
    # of magnitude longer than a bounded ~4000-char clean ever could.
    assert elapsed < 2.0


def test_clean_text_truncation_mid_emoji_sequence_does_not_raise():
    # Multi-codepoint ZWJ emoji sequence repeated well past the cap. Python
    # string slicing operates on code points, so truncating mid-sequence
    # can never raise or produce an invalid str -- confirm that holds and
    # that the junk-stripping pass still runs cleanly on whatever partial
    # sequence is left dangling at the cut point.
    family_emoji = "\U0001F468\u200D\U0001F469\u200D\U0001F467\u200D\U0001F466"
    huge = ("Movie " + family_emoji + " ") * 2000
    result = clean_text(huge)  # must not raise
    assert len(result) <= _MAX_TEXT_LEN
    assert "Movie" in result


# --- parse_stream ----------------------------------------------------------


def test_parse_stream_returns_expected_keys():
    info = parse_stream(AIOSTREAMS_STREAM, addon_name="AIOStreams")
    assert set(info.keys()) == EXPECTED_INFO_KEYS


def test_parse_stream_resolution_from_name_4k_maps_to_2160p():
    info = parse_stream(AIOSTREAMS_STREAM, addon_name="AIOStreams")
    assert info["resolution"] == "2160p"


def test_parse_stream_codec_hevc():
    info = parse_stream(AIOSTREAMS_STREAM, addon_name="AIOStreams")
    assert info["codec"] == "HEVC"


def test_parse_stream_hdr_list_contains_dv_and_hdr10plus():
    info = parse_stream(AIOSTREAMS_STREAM, addon_name="AIOStreams")
    assert set(info["hdr"]) == {"DV", "HDR10+"}


def test_parse_stream_prefers_behaviorhints_videosize_over_text():
    info = parse_stream(AIOSTREAMS_STREAM, addon_name="AIOStreams")
    assert info["size_bytes"] == AIOSTREAMS_VIDEO_SIZE


def test_parse_stream_seeders_from_text():
    info = parse_stream(AIOSTREAMS_STREAM, addon_name="AIOStreams")
    assert info["seeders"] == 50


def test_parse_stream_is_torrent_true_for_info_hash():
    info = parse_stream(AIOSTREAMS_STREAM, addon_name="AIOStreams")
    assert info["is_torrent"] is True


def test_parse_stream_is_torrent_false_without_info_hash():
    stream = {"name": "Direct", "title": "", "description": "", "url": "https://example.com/x.mp4"}
    info = parse_stream(stream)
    assert info["is_torrent"] is False


def test_parse_stream_addon_field_set_from_argument():
    info = parse_stream(AIOSTREAMS_STREAM, addon_name="AIOStreams")
    assert "AIOStreams" in info["addon"]


def test_parse_stream_filename_from_behaviorhints():
    info = parse_stream(AIOSTREAMS_STREAM, addon_name="AIOStreams")
    assert info["filename"] == AIOSTREAMS_FILENAME


def test_parse_stream_raw_is_single_line_and_cleaned():
    info = parse_stream(AIOSTREAMS_STREAM, addon_name="AIOStreams")
    assert "\n" not in info["raw"]
    assert "\U0001F3AC" not in info["raw"]
    assert "\U0001F331" not in info["raw"]
    assert "\u26a1" not in info["raw"]


@pytest.mark.parametrize(
    "text,expected",
    [
        ("2160p", "2160p"),
        ("4K", "2160p"),
        ("1080p", "1080p"),
        ("720p", "720p"),
        ("480p", "480p"),
    ],
)
def test_parse_stream_resolution_variants(text, expected):
    stream = {"name": text, "title": "", "description": "", "behaviorHints": {}}
    info = parse_stream(stream)
    assert info["resolution"] == expected


def test_parse_stream_resolution_absent_is_empty_string():
    stream = {"name": "Untagged Stream", "title": "", "description": "", "behaviorHints": {}}
    info = parse_stream(stream)
    assert info["resolution"] == ""


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Movie.2024.BluRay.1080p", "BluRay"),
        ("Movie.2024.REMUX.2160p", "Remux"),
        ("Movie.2024.WEB-DL.1080p", "WEB-DL"),
        ("Movie.2024.WEBRip.720p", "WEB"),
        ("Movie.2024.HDTV.480p", "HDTV"),
        ("Movie.2024.CAM", "CAM"),
    ],
)
def test_parse_stream_source_variants(text, expected):
    stream = {"name": text, "title": "", "description": "", "behaviorHints": {}}
    info = parse_stream(stream)
    assert info["source"] == expected


def test_parse_stream_size_from_text_when_no_behaviorhints():
    stream = {
        "name": "Some Stream",
        "title": "",
        "description": "\U0001F4BE 4.39 GB",
        "behaviorHints": {},
    }
    info = parse_stream(stream)
    assert isinstance(info["size_bytes"], int)
    # Accept either a decimal (GB=1e9) or binary (GiB=2^30) interpretation.
    assert 4_000_000_000 <= info["size_bytes"] <= 5_000_000_000
    assert info["size_text"]


def test_parse_stream_seeders_standalone_s_colon_format():
    stream = {
        "name": "x",
        "title": "",
        "description": "Quality info S:12 extra",
        "behaviorHints": {},
    }
    info = parse_stream(stream)
    assert info["seeders"] == 12


def test_parse_stream_seeders_absent_is_none():
    stream = {"name": "x", "title": "", "description": "no seed info here", "behaviorHints": {}}
    info = parse_stream(stream)
    assert info["seeders"] is None


def test_parse_stream_seeders_after_unit_absurdly_large_is_none():
    # Crafted/broken addon example from the confirmed gap: a huge digit run
    # right after a size unit must not propagate as a literal giant int
    # that would sort/display as nonsense.
    stream = {
        "name": "x",
        "title": "",
        "description": "GB 99999999999999999999 seeders",
        "behaviorHints": {},
    }
    info = parse_stream(stream)
    assert info["seeders"] is None


def test_parse_stream_seeders_labeled_absurdly_large_is_none():
    # Same sanity ceiling applies to the more-reliable labeled-pattern path.
    stream = {
        "name": "x",
        "title": "",
        "description": "Seeds: 99999999999999999999",
        "behaviorHints": {},
    }
    info = parse_stream(stream)
    assert info["seeders"] is None


# --- format_label ------------------------------------------------------


@pytest.mark.parametrize(
    "resolution,color",
    [
        ("2160p", "gold"),
        ("1080p", "lime"),
        ("720p", "cyan"),
        ("480p", "white"),
    ],
)
def test_format_label_resolution_color(resolution, color):
    info = _info(resolution=resolution)
    label = format_label(info)
    assert "[COLOR %s]" % color in label
    assert "[/COLOR]" in label


def test_format_label_empty_resolution_omits_color_tag_for_it():
    # An empty resolution is an empty segment - it must be omitted entirely,
    # not rendered as a dangling "[COLOR white][/COLOR]".
    info = _info(resolution="")
    label = format_label(info)
    assert "[COLOR white]" not in label


def test_format_label_is_single_line():
    info = parse_stream(AIOSTREAMS_STREAM, addon_name="AIOStreams")
    label = format_label(info)
    assert "\n" not in label


def test_format_label_includes_addon_in_gray():
    info = _info(resolution="1080p", seeders=10)
    info["addon"] = "AIOStreams"
    label = format_label(info)
    assert "[COLOR gray]" in label
    assert "AIOStreams" in label


def test_format_label_omits_empty_segments_without_dangling_separators():
    info = _info(resolution="")
    label = format_label(info)
    stripped = label.strip()
    assert not stripped.startswith("\u00b7")
    assert not stripped.endswith("\u00b7")
    assert "\u00b7  \u00b7" not in label
    assert "\u00b7\u00b7" not in label
    # No orphaned seeders marker when seeders is None.
    assert not re.search(r"(?<![A-Za-z])S(?![A-Za-z])", label)


def test_format_label_full_fixture_contains_expected_pieces():
    info = parse_stream(AIOSTREAMS_STREAM, addon_name="AIOStreams")
    label = format_label(info)
    assert "2160p" in label
    assert "HEVC" in label
    assert "S50" in label


# --- format_plot ---------------------------------------------------------


def test_format_plot_is_multiline():
    info = parse_stream(AIOSTREAMS_STREAM, addon_name="AIOStreams")
    plot = format_plot(info)
    assert plot.count("\n") >= 1


def test_format_plot_contains_filename_or_title():
    info = parse_stream(AIOSTREAMS_STREAM, addon_name="AIOStreams")
    plot = format_plot(info)
    assert info["filename"] in plot or info["title"] in plot


def test_format_plot_contains_size_and_seeders():
    info = parse_stream(AIOSTREAMS_STREAM, addon_name="AIOStreams")
    plot = format_plot(info)
    assert info["size_text"] in plot or str(info["size_bytes"]) in plot
    assert "50" in plot


def test_format_plot_contains_cleaned_addon_line():
    info = parse_stream(AIOSTREAMS_STREAM, addon_name="AIOStreams")
    plot = format_plot(info)
    assert "AIOStreams" in plot
    assert "\U0001F3AC" not in plot


# --- sort_streams ------------------------------------------------------


def test_sort_streams_quality_tier_ordering():
    pairs = [
        (_info("480p", seeders=100), "a"),
        (_info("2160p", seeders=1), "b"),
        (_info("720p", seeders=1), "c"),
        (_info("1080p", seeders=1), "d"),
        (_info("", seeders=999), "e"),
    ]
    result = sort_streams(pairs, key="quality")
    assert [p[1] for p in result] == ["b", "d", "c", "a", "e"]


def test_sort_streams_quality_seeders_tiebreak_none_last():
    pairs = [
        (_info("1080p", seeders=None), "x"),
        (_info("1080p", seeders=50), "y"),
        (_info("1080p", seeders=10), "z"),
    ]
    result = sort_streams(pairs, key="quality")
    assert [p[1] for p in result] == ["y", "z", "x"]


def test_sort_streams_quality_size_tiebreak_after_seeders():
    pairs = [
        (_info("1080p", seeders=10, size_bytes=100), "small"),
        (_info("1080p", seeders=10, size_bytes=500), "big"),
    ]
    result = sort_streams(pairs, key="quality")
    assert [p[1] for p in result] == ["big", "small"]


def test_sort_streams_is_stable_for_equal_keys():
    pairs = [
        (_info("1080p", seeders=10, size_bytes=100), "first"),
        (_info("1080p", seeders=10, size_bytes=100), "second"),
        (_info("1080p", seeders=10, size_bytes=100), "third"),
    ]
    result = sort_streams(pairs, key="quality")
    assert [p[1] for p in result] == ["first", "second", "third"]


def test_sort_streams_returns_copy_does_not_mutate_input():
    pairs = [
        (_info("480p"), "a"),
        (_info("2160p"), "b"),
    ]
    original_order = [p[1] for p in pairs]
    result = sort_streams(pairs, key="quality")
    assert [p[1] for p in pairs] == original_order
    assert result is not pairs


def test_sort_streams_size_key_descending_none_last():
    pairs = [
        (_info(size_bytes=100), "small"),
        (_info(size_bytes=500), "big"),
        (_info(size_bytes=None), "unknown"),
    ]
    result = sort_streams(pairs, key="size")
    assert [p[1] for p in result] == ["big", "small", "unknown"]


def test_sort_streams_seeders_key_descending_none_last():
    pairs = [
        (_info(seeders=5), "low"),
        (_info(seeders=50), "high"),
        (_info(seeders=None), "none"),
    ]
    result = sort_streams(pairs, key="seeders")
    assert [p[1] for p in result] == ["high", "low", "none"]


def test_sort_streams_empty_list():
    assert sort_streams([], key="quality") == []
