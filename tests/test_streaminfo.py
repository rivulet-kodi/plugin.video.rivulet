"""Tests for lib.stremio.streaminfo (pure text/label formatting helpers).

No network access; these functions are pure string/dict transforms over
Stream protocol objects (stremio-protocol-spec.md #3) and AIOStreams-style
addon output (multi-line, emoji-decorated name/title/description).
"""
import re
import time

import pytest

from lib.stremio.streaminfo import (
    _LANGUAGE_SEGMENT_RES,
    _LETTER_RE,
    _MAX_TEXT_LEN,
    _MULTI_GLOBE_MARKER,
    _decode_flag_pair,
    _language_patterns,
    _language_tag_patterns,
    _scan_languages,
    _segment_after,
    clean_text,
    filter_streams,
    filter_summary,
    format_details,
    format_label,
    format_plot,
    parse_stream,
    sort_streams,
    stream_fields,
)
from lib.ui import playbackmeta

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
    "binge_group",
    "raw",
    "audio",
    "channels",
    "languages",
    "bitrate",
    "group",
    "tracker",
    "service",
    "cached",
    "release",
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
        "binge_group": None,
        "raw": "",
        "audio": [],
        "channels": "",
        "languages": [],
        "bitrate": "",
        "group": "",
        "tracker": "",
        "service": "",
        "cached": None,
        "release": [],
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


def test_parse_stream_binge_group_from_behaviorhints():
    stream = {
        "name": "Movie.Title.2024.2160p.WEB-DL.HEVC-GROUP", "title": "", "description": "",
        "behaviorHints": {"bingeGroup": "rivulet|2160p|HEVC-GROUP"},
    }
    info = parse_stream(stream)
    assert info["binge_group"] == "rivulet|2160p|HEVC-GROUP"


def test_parse_stream_binge_group_absent_is_none():
    info = parse_stream(AIOSTREAMS_STREAM, addon_name="AIOStreams")
    assert info["binge_group"] is None


def test_parse_stream_binge_group_empty_string_is_none():
    stream = {"name": "x", "title": "", "description": "", "behaviorHints": {"bingeGroup": ""}}
    info = parse_stream(stream)
    assert info["binge_group"] is None


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


# --- parse_stream: audio / channels / bitrate / release -------------------


def test_parse_stream_audio_dts_hd_ma_does_not_also_yield_dts():
    stream = {"name": "Movie.2024.2160p.DTS-HD.MA.5.1", "title": "", "description": "", "behaviorHints": {}}
    info = parse_stream(stream)
    assert info["audio"] == ["DTS-HD MA"]


def test_parse_stream_audio_e_ac_3_yields_ddplus_not_dd():
    stream = {"name": "Movie.2024.2160p.E-AC-3.5.1", "title": "", "description": "", "behaviorHints": {}}
    info = parse_stream(stream)
    assert info["audio"] == ["DD+"]


def test_parse_stream_audio_and_channels_from_text():
    stream = {"name": "Movie.2024.2160p.TrueHD.Atmos.7.1", "title": "", "description": "", "behaviorHints": {}}
    info = parse_stream(stream)
    assert info["audio"] == ["TrueHD", "Atmos"]
    assert info["channels"] == "7.1"


def test_parse_stream_bitrate_from_text():
    stream = {
        "name": "x", "title": "", "behaviorHints": {},
        "description": "\U0001F4E6 22.42 GB | \u303D\uFE0F 25.5 Mbps",
    }
    info = parse_stream(stream)
    assert info["bitrate"] == "25.5 Mbps"


def test_parse_stream_dv_profile_marker_only_when_dv_present():
    # Issue #4's own case: a DV row whose file is really a P8 remap. The
    # bare 'DVT' group suffix is NOT a Dolby Vision hint - the standalone
    # 'DV' tag is.
    stream = {
        "name": "Deadpool 2160p Remux DV Hybrid.P8.by.DVT",
        "title": "", "description": "", "behaviorHints": {},
    }
    info = parse_stream(stream)
    assert info["release"] == ["Hybrid", "P8"]


def test_parse_stream_dv_profile_marker_absent_when_only_dvd_in_text():
    stream = {"name": "Movie.2019.DVDRip.P8.mkv", "title": "", "description": "", "behaviorHints": {}}
    info = parse_stream(stream)
    assert info["release"] == []


def test_parse_stream_dv_profile_marker_absent_without_dv_hint():
    stream = {"name": "Movie.P8.mkv", "title": "", "description": "", "behaviorHints": {}}
    info = parse_stream(stream)
    assert "P8" not in info["release"]


# --- parse_stream: debrid service / cache state ----------------------------


def test_parse_stream_torrentio_cached_bracket_plus():
    stream = {"name": "[RD+] Deadpool 2160p", "title": "", "description": "", "behaviorHints": {}}
    info = parse_stream(stream)
    assert info["service"] == "RD"
    assert info["cached"] is True


def test_parse_stream_torrentio_uncached_bracket_download():
    stream = {"name": "[RD download] Deadpool 2160p", "title": "", "description": "", "behaviorHints": {}}
    info = parse_stream(stream)
    assert info["service"] == "RD"
    assert info["cached"] is False


def test_parse_stream_comet_kodi_cached_bracket_c():
    stream = {
        "name": "[RD C] 1080p | 3.65 GB | S:20 | FraMeSToR", "title": "", "description": "", "behaviorHints": {},
    }
    info = parse_stream(stream)
    assert info["service"] == "RD"
    assert info["cached"] is True


def test_parse_stream_comet_kodi_uncached_bracket_u():
    stream = {
        "name": "[RD U] 1080p | 3.65 GB | S:20 | FraMeSToR", "title": "", "description": "", "behaviorHints": {},
    }
    info = parse_stream(stream)
    assert info["service"] == "RD"
    assert info["cached"] is False


def test_parse_stream_stremthru_cached_lightning_bolt():
    stream = {"name": "x", "title": "", "description": "\u26a1\ufe0f [RD] Deadpool 2160p", "behaviorHints": {}}
    info = parse_stream(stream)
    assert info["service"] == "RD"
    assert info["cached"] is True


def test_parse_stream_stremthru_uncached_bare_bracket():
    # StremThru's uncached form is a bare code in `name` (its cached form
    # carries the bolt) - see the module's marker matrix.
    stream = {"name": "[RD]\nStremThru\n1080p", "title": "", "description": "", "behaviorHints": {}}
    info = parse_stream(stream)
    assert info["service"] == "RD"
    assert info["cached"] is False


def test_parse_stream_bare_service_code_outside_name_is_not_a_verdict():
    # '[EN]' is Easynews' code but also an ordinary language tag: read as
    # a debrid verdict it would claim 'not cached' on a stream nobody said
    # anything about. Only `name` carries the bare-code form.
    stream = {
        "name": "Deadpool 2160p", "title": "Deadpool.2016.2160p.WEB-DL [EN] [RD]",
        "description": "", "behaviorHints": {},
    }
    info = parse_stream(stream)
    assert info["service"] == ""
    assert info["cached"] is None


def test_parse_stream_aiostreams_prism_ready_is_cached():
    stream = {"name": "\u26a1Ready (RD) Deadpool 2160p", "title": "", "description": "", "behaviorHints": {}}
    info = parse_stream(stream)
    assert info["service"] == "RD"
    assert info["cached"] is True


def test_parse_stream_aiostreams_prism_not_ready_is_uncached():
    stream = {"name": "\u274c Not Ready (RD) Deadpool 2160p", "title": "", "description": "", "behaviorHints": {}}
    info = parse_stream(stream)
    assert info["service"] == "RD"
    assert info["cached"] is False


def test_parse_stream_aiostreams_torbox_instant_is_cached():
    stream = {"name": "Deadpool 2160p (Instant RD)", "title": "", "description": "", "behaviorHints": {}}
    info = parse_stream(stream)
    assert info["service"] == "RD"
    assert info["cached"] is True


def test_parse_stream_aiostreams_torbox_bare_paren_is_uncached():
    stream = {"name": "Deadpool 2160p (RD)", "title": "", "description": "", "behaviorHints": {}}
    info = parse_stream(stream)
    assert info["service"] == "RD"
    assert info["cached"] is False


@pytest.mark.parametrize("bracket", ["[SEV]", "[P2P]", "[AIOStreams Stable]"])
def test_parse_stream_non_service_bracket_yields_no_service(bracket):
    stream = {"name": "%s Deadpool 2160p" % bracket, "title": "", "description": "", "behaviorHints": {}}
    info = parse_stream(stream)
    assert info["service"] == ""
    assert info["cached"] is None


def test_parse_stream_comet_kodi_meta_overrides_text_guesses():
    stream = {
        "name": "[RD C] 1080p | TrueHD | 7.1 | English",
        "title": "", "description": "",
        "behaviorHints": {
            "cometKodiMetaV1": {"audio": "DTS", "channels": "2.0", "languages": ["French"]},
        },
    }
    info = parse_stream(stream)
    assert info["audio"] == ["DTS"]
    assert info["channels"] == "2.0"
    assert info["languages"] == ["FR"]


# --- parse_stream: languages ------------------------------------------


def test_parse_stream_languages_flag_emoji_survive_clean_text():
    stream = {
        "name": "Deadpool 2160p", "title": "",
        "description": "Dual Audio / \U0001F1EC\U0001F1E7 / \U0001F1EE\U0001F1F3",
        "behaviorHints": {},
    }
    info = parse_stream(stream)
    assert info["languages"] == ["DUAL", "EN", "HI"]
    assert "\U0001F1EC" not in info["raw"]


def test_parse_stream_languages_full_names_from_labelled_segment():
    # AIOStreams' torbox format labels the segment: 'Languages: <names>'.
    stream = {
        "name": "x", "title": "", "behaviorHints": {},
        "description": "Size: 22.42 GB\nLanguages: English, Russian",
    }
    info = parse_stream(stream)
    assert info["languages"] == ["EN", "RU"]


def test_parse_stream_language_name_in_a_title_is_not_a_language():
    # No marker, so 'Italian' here is what it looks like: part of the
    # film's name, not an audio track.
    stream = {
        "name": "x", "title": "The Italian Job 2160p Remux",
        "description": "", "behaviorHints": {},
    }
    info = parse_stream(stream)
    assert info["languages"] == []


def test_parse_stream_languages_bare_abbreviations_are_not_full_names():
    stream = {"name": "x", "title": "", "description": "en / fr", "behaviorHints": {}}
    info = parse_stream(stream)
    assert info["languages"] == []


# --- _scan_languages: finding 9b letter-gate ------------------------------


def _ungated_scan_languages(pre_clean):
    """Reference copy of `_scan_languages` as it stood before finding 9b:
    always runs the full `_language_patterns()` sweep against a matched
    segment, with no letter pre-check. Used to pin that the gated version
    is byte-identical, never just "close enough".
    """
    matches = []
    for marker in _LANGUAGE_SEGMENT_RES:
        offset, segment = _segment_after(pre_clean, marker)
        if not segment:
            continue
        for code, pattern in _language_patterns():
            match = pattern.search(segment)
            if match:
                matches.append((offset + match.start(), code))
        if marker is _MULTI_GLOBE_MARKER and not _LETTER_RE.search(segment):
            matches.append((offset, "MULTI"))
    for match in re.finditer("[\U0001F1E6-\U0001F1FF]{2}", pre_clean):
        matches.append((match.start(), _decode_flag_pair(match.group(0))))
    for code, pattern in _language_tag_patterns():
        match = pattern.search(pre_clean)
        if match:
            matches.append((match.start(), code))
    matches.sort(key=lambda item: item[0])
    seen = set()
    result = []
    for _, code in matches:
        if code in seen:
            continue
        seen.add(code)
        result.append(code)
        if len(result) >= 6:
            break
    return result


def _synthetic_language_corpus(n=400):
    """400 rows mixing the documented segment-marker shapes (see
    `_LANGUAGE_SEGMENT_RES`'s docstring) with real language names, plus
    plenty of rows with no language content at all -- the same shapes
    finding 9b's audit was measured against.
    """
    name_sets = [
        ["English", "Russian"], ["Italian", "German", "French"],
        ["Spanish", "Portuguese"], ["Japanese", "Korean"], ["Turkish"],
    ]
    markers = ["\U0001F30E", "\U0001F310", "\U0001F5E3\uFE0F", "\U0001F399\uFE0F", "Languages:"]
    rows = []
    for i in range(n):
        bucket = i % 20
        names = ", ".join(name_sets[i % len(name_sets)])
        if bucket < 8:  # no language content at all
            rows.append("Movie.Title.%d.2160p.WEB-DL.HEVC-GROUP" % i)
        elif bucket < 9:  # documented letterless "Multi" edge case
            rows.append("\U0001F30E / \U0001F1EC\U0001F1E7 / \U0001F1EE\U0001F1F3")
        elif bucket < 10:  # flags only, no segment marker at all
            rows.append("Dual Audio / \U0001F1EC\U0001F1E7 / \U0001F1EE\U0001F1F3")
        else:  # a real marker followed by real names, per each addon format
            rows.append("%s %s" % (markers[i % len(markers)], names))
    return rows


def test_scan_languages_matches_reference_sweep_on_synthetic_corpus():
    for pre_clean in _synthetic_language_corpus():
        assert _scan_languages(pre_clean) == _ungated_scan_languages(pre_clean)


def test_scan_languages_skips_pattern_sweep_for_letterless_segment():
    # The documented Multi case: a globe marker whose segment is only
    # flag emoji carries no Latin letter, so none of the 48 patterns
    # could ever match it -- the gate must skip the sweep entirely.
    calls = []
    real_patterns = _language_patterns()

    class _CountingPattern:
        def __init__(self, pattern):
            self._pattern = pattern

        def search(self, text):
            calls.append(text)
            return self._pattern.search(text)

    import lib.stremio.streaminfo as streaminfo_module
    patched = [(code, _CountingPattern(pattern)) for code, pattern in real_patterns]
    original_cache = streaminfo_module._LANGUAGE_PATTERNS_CACHE
    streaminfo_module._LANGUAGE_PATTERNS_CACHE = patched
    try:
        result = _scan_languages("\U0001F30E / \U0001F1EC\U0001F1E7 / \U0001F1EE\U0001F1F3")
    finally:
        streaminfo_module._LANGUAGE_PATTERNS_CACHE = original_cache
    # The flag pairs still decode via the separate, unconditional
    # flag-pair loop -- only the 48-pattern name sweep is gated.
    assert result == ["MULTI", "EN", "HI"]
    assert calls == []


# --- parse_stream: tracker / release group ---------------------------------


def test_parse_stream_tracker_from_gear_marker_torrentio():
    title = (
        "Deadpool [2016] 2160p BDRip x265 [SEV]\n"
        "\U0001F464 16 \U0001F4BE 22.42 GB \u2699\uFE0F 1337x"
    )
    stream = {"name": "Torrentio", "title": title, "description": "", "behaviorHints": {}}
    info = parse_stream(stream)
    assert info["tracker"] == "1337x"


def test_parse_stream_stremthru_tracker_and_group_precedence():
    description = (
        "\U0001F4BF BluRay | \U0001F39E\uFE0F HEVC\n"
        "\U0001F3A7 TrueHD | 7.1\n"
        "\U0001F4E6 22.42 GB | \u303D\uFE0F 25.5 Mbps\n"
        "\u2699\uFE0F FraMeSToR\n"
        "\U0001F50D 1337x\n"
        "\U0001F4C4 Deadpool.2016.2160p.mkv"
    )
    stream = {
        "name": "StremThru", "title": "", "description": description,
        "behaviorHints": {"filename": "Deadpool.2016.2160p.mkv"},
    }
    info = parse_stream(stream)
    assert info["tracker"] == "1337x"
    assert info["group"] == "FraMeSToR"


def test_parse_stream_tracker_skips_the_addon_name_aiostreams_prism():
    # prism puts the ADDON behind the magnifier and its indexer behind the
    # satellite marker, the opposite of gdrive/StremThru: the row must end
    # up with the tracker, not a second copy of the addon name.
    description = (
        "\U0001F4E6 97.43 GB \U0001F4CA 130 Mbps \U0001F331 85\n"
        "\U0001F3F7\uFE0F BTM \U0001F4E1 Rutor\n"
        "\u274C Not Ready (RD) \U0001F50DAIOStreams"
    )
    stream = {"name": "4K UHD", "title": "", "description": description, "behaviorHints": {}}
    info = parse_stream(stream, addon_name="AIOStreams")
    assert info["tracker"] == "Rutor"
    assert info["group"] == "BTM"


def test_parse_stream_group_falls_back_to_filename_release_group():
    info = parse_stream(AIOSTREAMS_STREAM, addon_name="AIOStreams")
    assert info["group"] == "GROUP"


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
    assert "\u25b2" not in label


def test_format_label_full_fixture_contains_expected_pieces():
    info = parse_stream(AIOSTREAMS_STREAM, addon_name="AIOStreams")
    label = format_label(info)
    assert "2160p" in label
    assert "HEVC" in label
    assert "\u25b250" in label


def test_format_label_cached_service_shows_lime_segment():
    info = _info(service="RD", cached=True)
    label = format_label(info)
    assert "[COLOR lime]RD[/COLOR]" in label


def test_format_label_uncached_service_shows_orange_dl_segment():
    info = _info(service="RD", cached=False)
    label = format_label(info)
    assert "[COLOR orange]RD DL[/COLOR]" in label


def test_format_label_unknown_cache_state_shows_bare_service_no_color():
    info = _info(service="RD", cached=None)
    label = format_label(info)
    assert "RD" in label
    assert "[COLOR lime]RD" not in label
    assert "[COLOR orange]RD" not in label


def test_format_label_no_service_omits_cache_segment_entirely():
    info = _info(service="", cached=True)
    label = format_label(info)
    assert "RD" not in label
    assert "[COLOR lime]" not in label
    assert "[COLOR orange]" not in label


# --- stream_fields -----------------------------------------------------


@pytest.mark.parametrize("resolution,color", [
    ("2160p", "FFFFD700"),
    ("1080p", "FF4ADE80"),
    ("720p", "C7EEF3F6"),
    ("480p", "C7EEF3F6"),
    ("", "C7EEF3F6"),
], ids=["2160p-gold", "1080p-green", "720p-dim", "480p-dim", "unknown-dim"])
def test_stream_fields_quality_color_matches_the_palette(resolution, color):
    fields = stream_fields(_info(resolution=resolution))
    assert fields["quality"] == resolution
    assert fields["quality_color"] == color


def test_stream_fields_release_is_the_source_field_not_the_release_tag_list():
    # info['release'] (Extended/Hybrid/P8/...) is a DIFFERENT concept from
    # the design's bold row label, which is info['source'] (Remux/BluRay/
    # WEB-DL/...) - stream_fields() must not conflate the two.
    info = _info(source="BluRay", release=["Hybrid", "P8"])
    fields = stream_fields(info)
    assert fields["release"] == "BluRay"


def test_stream_fields_flags_joins_codec_hdr_and_audio_in_order():
    info = _info(codec="HEVC", hdr=["DV", "HDR10"], audio=["Atmos"])
    fields = stream_fields(info)
    assert fields["flags"] == "HEVC \u00b7 DV \u00b7 HDR10 \u00b7 Atmos"


def test_stream_fields_flags_drops_empty_pieces_without_dangling_separators():
    info = _info(codec="", hdr=["DV", "HDR"], audio=[])
    fields = stream_fields(info)
    assert fields["flags"] == "DV \u00b7 HDR"


def test_stream_fields_flags_empty_string_when_nothing_detected():
    fields = stream_fields(_info())
    assert fields["flags"] == ""


def test_stream_fields_provider_is_the_addon_name():
    info = _info(addon="AIOStreams")
    assert stream_fields(info)["provider"] == "AIOStreams"


def test_stream_fields_cached_true_is_green_cached_state():
    info = _info(service="RD", cached=True)
    fields = stream_fields(info)
    assert fields["cache_state"] == "CACHED"
    assert fields["cache_color"] == "FF4ADE80"


def test_stream_fields_cached_false_is_orange_dl_state():
    info = _info(service="RD", cached=False)
    fields = stream_fields(info)
    assert fields["cache_state"] == "DL"
    assert fields["cache_color"] == "FFFB923C"


def test_stream_fields_no_cache_info_is_dim_em_dash_not_fabricated():
    # Design line 280: a stream with no cache verdict draws a dim em-dash
    # rather than a blank cell, so the column stays visually aligned
    # instead of leaving a hole - never a fabricated CACHED/DL guess.
    info = _info(service="", cached=None)
    fields = stream_fields(info)
    assert fields["cache_state"] == "\u2014"
    assert fields["cache_color"] == "4DEEF3F6"


def test_stream_fields_missing_size_and_seeders_are_empty_strings():
    # A stream with nothing parseable for size/seeders must report ''
    # for each - never a fabricated placeholder - so the skin's
    # !String.IsEmpty(...) guard can collapse those columns cleanly.
    # cache_state is the one exception - see
    # test_stream_fields_no_cache_info_is_dim_em_dash_not_fabricated.
    info = _info(size_text="", seeders=None, service="", cached=None)
    fields = stream_fields(info)
    assert fields["size"] == ""
    assert fields["seeders"] == ""
    assert fields["cache_state"] == "\u2014"


def test_stream_fields_seeders_is_a_plain_decimal_string_no_marker():
    # The seeders MARKER (\u25b2) is the skin's job (a static glyph next
    # to $INFO[ListItem.Property(seeders)]), not this helper's - keeping
    # it out of the property means the skin can hide the marker too via
    # the same visibility guard when the property is empty.
    fields = stream_fields(_info(seeders=72))
    assert fields["seeders"] == "72"
    assert "\u25b2" not in fields["seeders"]


def test_stream_fields_none_info_cache_state_is_dim_em_dash():
    # Every other field is empty for a None info, but cache_state is
    # never '' - see stream_fields()'s own docstring.
    fields = stream_fields(None)
    assert fields == {
        "quality": "", "quality_color": "C7EEF3F6", "release": "", "flags": "",
        "provider": "", "size": "", "seeders": "", "cache_state": "\u2014", "cache_color": "4DEEF3F6",
    }


def test_stream_fields_against_real_aiostreams_fixture():
    info = parse_stream(AIOSTREAMS_STREAM, addon_name="AIOStreams")
    fields = stream_fields(info)
    assert fields["quality"] == "2160p"
    assert fields["quality_color"] == "FFFFD700"
    assert fields["release"] == "WEB-DL"
    assert "HEVC" in fields["flags"]
    assert fields["provider"] == "AIOStreams"
    assert fields["seeders"] == "50"


# --- format_details ---------------------------------------------------------


def test_format_details_empty_info_is_empty_string():
    assert format_details(_info()) == ""


def test_format_details_omits_empty_segments_without_dangling_separators():
    info = _info(audio=["TrueHD"], tracker="1337x")
    details = format_details(info)
    assert details == "TrueHD \u00b7 1337x"
    assert "\u00b7  \u00b7" not in details
    assert "\u00b7\u00b7" not in details


def test_format_details_composes_all_segments_in_order():
    info = _info(
        audio=["TrueHD", "Atmos"], channels="7.1", languages=["EN", "RU"],
        bitrate="25.5 Mbps", release=["Hybrid", "P8"], group="FraMeSToR", tracker="1337x",
    )
    details = format_details(info)
    assert details == "TrueHD Atmos 7.1 \u00b7 EN / RU \u00b7 25.5 Mbps \u00b7 Hybrid P8 \u00b7 FraMeSToR \u00b7 1337x"


def test_format_details_never_contains_bbcode_or_newlines():
    info = parse_stream(AIOSTREAMS_STREAM, addon_name="AIOStreams")
    details = format_details(info)
    assert "[COLOR" not in details
    assert "[B]" not in details
    assert "\n" not in details
    assert "\r" not in details


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


def test_format_plot_contains_details_line_when_non_empty():
    info = parse_stream(AIOSTREAMS_STREAM, addon_name="AIOStreams")
    plot = format_plot(info)
    details = format_details(info)
    assert details
    assert details in plot


def test_format_plot_omits_details_line_when_empty():
    info = _info()
    plot = format_plot(info)
    assert format_details(info) == ""
    assert "\n\n" not in plot


def test_format_plot_cached_line_variants():
    assert "Cached (RD)" in format_plot(_info(service="RD", cached=True))
    assert "Not cached (RD)" in format_plot(_info(service="RD", cached=False))
    plot_unknown = format_plot(_info(service="RD", cached=None))
    assert "Cached (RD)" not in plot_unknown
    assert "Not cached (RD)" not in plot_unknown
    assert "RD" in plot_unknown


def test_format_plot_no_service_omits_cache_line():
    plot = format_plot(_info(service="", cached=True))
    assert "Cached" not in plot
    assert "RD" not in plot


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


# --- filter_streams / filter_summary ------------------------------------


def test_filter_streams_min_height_drops_below_threshold():
    pairs = [(_info("480p"), "low"), (_info("1080p"), "high")]
    result = filter_streams(pairs, min_height=720)
    assert [p[1] for p in result] == ["high"]


def test_filter_streams_min_height_keeps_unknown_resolution():
    # An entry parse_stream() could not read a resolution out of is a
    # parser gap, not necessarily a low-quality stream - hiding it would
    # turn "we don't know" into "we hid it", the exact bug min_height's
    # contract rules out.
    pairs = [(_info(""), "unknown"), (_info("480p"), "low")]
    result = filter_streams(pairs, min_height=1080)
    assert [p[1] for p in result] == ["unknown"]


def test_filter_streams_max_size_drops_above_threshold():
    pairs = [
        (_info(size_bytes=1_000_000_000), "small"),
        (_info(size_bytes=5_000_000_000), "big"),
    ]
    result = filter_streams(pairs, max_size_bytes=2_000_000_000)
    assert [p[1] for p in result] == ["small"]


def test_filter_streams_max_size_keeps_unknown_size():
    pairs = [(_info(size_bytes=None), "unknown"), (_info(size_bytes=5_000_000_000), "big")]
    result = filter_streams(pairs, max_size_bytes=1_000_000_000)
    assert [p[1] for p in result] == ["unknown"]


def test_filter_streams_exclude_cam_drops_camrip_word_boundary():
    pairs = [(_info(raw="Movie.2020.CAMRip.x264"), "cam")]
    assert filter_streams(pairs, exclude_cam=True) == []


def test_filter_streams_exclude_cam_keeps_camelot_not_a_cam_source():
    # "Camelot" contains "cam" but is not word-bounded as a release tag -
    # must survive the filter untouched.
    pairs = [(_info(raw="Camelot.2017.1080p.BluRay"), "camelot")]
    result = filter_streams(pairs, exclude_cam=True)
    assert [p[1] for p in result] == ["camelot"]


def test_filter_streams_cached_only_keeps_true_drops_unknown_and_false():
    pairs = [
        (_info(cached=True), "cached"),
        (_info(cached=False), "uncached"),
        (_info(cached=None), "unknown"),
    ]
    result = filter_streams(pairs, cached_only=True)
    assert [p[1] for p in result] == ["cached"]


def test_filter_streams_all_zero_args_returns_input_unchanged_order_preserved():
    pairs = [(_info("480p"), "a"), (_info("2160p"), "b"), (_info(""), "c")]
    result = filter_streams(pairs)
    assert [p[1] for p in result] == ["a", "b", "c"]


def test_filter_streams_combined_axes():
    pairs = [
        (_info("2160p", size_bytes=1_000_000_000, cached=True, raw="Great.Movie.2160p"), "keep"),
        (_info("480p", size_bytes=1_000_000_000, cached=True, raw="Low.Res.480p"), "low_res"),
        (_info("2160p", size_bytes=9_000_000_000, cached=True, raw="Huge.File.2160p"), "too_big"),
        (_info("2160p", size_bytes=1_000_000_000, cached=False, raw="Not.Cached.2160p"), "not_cached"),
        (_info("2160p", size_bytes=1_000_000_000, cached=True, raw="Movie.2160p.CAMRip"), "cam"),
    ]
    result = filter_streams(
        pairs, min_height=1080, max_size_bytes=2_000_000_000, exclude_cam=True, cached_only=True,
    )
    assert [p[1] for p in result] == ["keep"]


def test_filter_summary_reports_total_and_hidden_count():
    assert filter_summary(10, 7) == (10, 3)


def test_filter_summary_hidden_never_negative():
    # kept can never exceed total in real use, but the helper still
    # shouldn't hand a UI a negative "hidden" count if it ever did.
    assert filter_summary(5, 8) == (5, 0)


def test_filter_streams_then_sort_streams_excluded_stream_does_not_disturb_order():
    # filter_streams() must be a clean per-entry drop with no side effect
    # on the RELATIVE order sort_streams() gives the survivors -
    # streamswindow.py relies on filtering before sorting without the
    # two steps fighting over row order. "too_big" outranks both
    # survivors on seeders alone, so if it were not excluded before the
    # sort it would land first.
    pairs = [
        (_info("1080p", seeders=5, size_bytes=1_000_000_000), "keep_a"),
        (_info("1080p", seeders=50, size_bytes=9_000_000_000), "too_big"),
        (_info("1080p", seeders=20, size_bytes=1_000_000_000), "keep_b"),
    ]
    filtered = filter_streams(pairs, max_size_bytes=2_000_000_000)
    result = sort_streams(filtered, key="quality")
    assert [p[1] for p in result] == ["keep_b", "keep_a"]


# --- lib.ui.playbackmeta: pure OSD metadata formatting/parsing helpers -----
# (Kodi-independent, so tested directly here alongside this module's own
# pure text/label formatting helpers, with no xbmc stub required.)


def test_format_hms_formats_hours_minutes_seconds():
    assert playbackmeta.format_hms(5410) == "1:30:10"


def test_format_hms_clamps_negative_to_zero():
    assert playbackmeta.format_hms(-5) == "0:00:00"


def test_human_size_formats_bytes_kb_mb_gb():
    assert playbackmeta.human_size(500) == "500.0 B"
    assert playbackmeta.human_size(1536) == "1.5 KB"
    assert playbackmeta.human_size(5 * 1024 * 1024) == "5.0 MB"
    assert playbackmeta.human_size(2 * 1024 * 1024 * 1024) == "2.0 GB"


def test_human_size_none_or_falsy_is_zero_bytes():
    assert playbackmeta.human_size(None) == "0.0 B"


def test_sanitize_title_strips_crlf_and_trims():
    assert playbackmeta.sanitize_title("Some\r\nTitle\n") == "Some  Title"


def test_sanitize_title_empty_or_none_is_empty_string():
    assert playbackmeta.sanitize_title("") == ""
    assert playbackmeta.sanitize_title(None) == ""


@pytest.mark.parametrize("value,expected", [
    ("2019", 2019),
    ("2019-", 2019),
    ("2019-2023", 2019),
    (None, None),
    ("n/a", None),
])
def test_parse_year_handles_open_ended_ranges_and_unparseable(value, expected):
    assert playbackmeta.parse_year(value) == expected


# ---------------------------------------------------------------------------
# year_range() - the display form of a `releaseInfo` range, shared by all
# three screens that print one (ShowcaseWindow's hero, DetailWindow's left
# column, StreamsWindow's info panel).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value,expected", [
    # open-ended: still running, so the dangling separator becomes a word
    ("2022-", "2022-now"),
    ("2022–", "2022–now"),   # EN DASH: what Cinemeta actually sends
    ("2022—", "2022—now"),   # EM DASH, for completeness
    ("2022 -", "2022-now"),            # stray space before the separator
    # closed ranges and bare years are untouched
    ("2011–2019", "2011–2019"),
    ("2010-2017", "2010-2017"),
    ("1999", "1999"),
    # the ISO date callers fall back to: hyphens are separators, not a range
    ("2021-07-04", "2021-07-04"),
    # degenerate input must not produce a bare "now"
    ("", ""),
    (None, ""),
    ("-", "-"),
    ("--", "--"),
])
def test_year_range_closes_only_open_ended_ranges(value, expected):
    """A still-running series arrives open-ended ('2022-'). Printing the
    dash reads as truncated text, and stripping it loses the fact that
    the show has not ended - so it is closed with a word instead. The
    separator already in the data is reused, so the range stays
    internally consistent.

    Cinemeta terminates every range with an EN DASH, not the ASCII
    hyphen - so the `.rstrip('-')` this replaced never fired on a real
    series at all.
    """
    assert playbackmeta.year_range(value, "now") == expected


def test_year_range_word_is_supplied_by_the_caller():
    """The module is Kodi-independent: callers pass the localized word
    in rather than this reaching for the addon's string catalog."""
    assert playbackmeta.year_range("2022–", "heute") == "2022–heute"


@pytest.mark.parametrize("value,expected", [
    ("7.8", 7.8),
    (None, None),
    ("n/a", None),
])
def test_parse_rating_handles_unparseable_values(value, expected):
    assert playbackmeta.parse_rating(value) == expected


@pytest.mark.parametrize("value,expected", [
    ("132 min", 132 * 60),
    (None, None),
    ("?", None),
])
def test_parse_duration_seconds_converts_minutes_to_seconds(value, expected):
    assert playbackmeta.parse_duration_seconds(value) == expected


def test_resolve_art_poster_becomes_thumb_and_icon_when_no_explicit_thumb():
    result = playbackmeta.resolve_art({"poster": "p.jpg"}, {})
    assert result == {"poster": "p.jpg", "icon": "p.jpg", "thumb": "p.jpg"}


def test_resolve_art_falls_back_to_meta_fields_when_art_empty():
    result = playbackmeta.resolve_art(None, {"poster": "mp.jpg", "background": "bg.jpg"})
    assert result == {"poster": "mp.jpg", "icon": "mp.jpg", "thumb": "mp.jpg", "fanart": "bg.jpg"}


def test_resolve_art_missing_fields_are_omitted_entirely():
    assert playbackmeta.resolve_art(None, None) == {}
