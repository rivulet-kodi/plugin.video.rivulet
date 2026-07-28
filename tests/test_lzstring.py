"""Tests for lib.stremio.lzstring (LZ-String codec).

No network access - the reference vectors in
`test_matches_reference_lz_string_output` were captured OFFLINE, once,
by running `npm install lz-string@1.5.0` (the real upstream pieroxy/
lz-string package - the exact library stremio-core's `lz-str` Rust crate
ports) in a scratch directory and calling
`LZString.compressToEncodedURIComponent()` on each input below. They are
hardcoded here, not fetched at test time.
"""
import json

from lib.stremio.lzstring import (
    compress_to_encoded_uri_component,
    decompress_from_encoded_uri_component,
)

# --- round-trip coverage -----------------------------------------------


def test_round_trip_empty_string():
    assert decompress_from_encoded_uri_component(compress_to_encoded_uri_component('')) == ''


def test_round_trip_ascii():
    text = 'hello world, this is a stream URL payload'
    assert decompress_from_encoded_uri_component(compress_to_encoded_uri_component(text)) == text


def test_round_trip_unicode_bmp():
    text = 'héllo wörld 日本語 Ω café naïve'
    assert decompress_from_encoded_uri_component(compress_to_encoded_uri_component(text)) == text


def test_round_trip_multi_kilobyte_payload():
    """A few KB of semi-repetitive text - big enough to exercise the
    dictionary's bit-width growth (num_bits climbing past its initial
    3-bit start) several times over, the way a real archive/nzb `/create`
    JSON body with several urls/trackers would.
    """
    text = 'The quick brown fox jumps over the lazy dog. ' * 100
    assert len(text) > 2000
    compressed = compress_to_encoded_uri_component(text)
    assert decompress_from_encoded_uri_component(compressed) == text


def test_round_trip_json_archive_payload():
    payload = json.dumps(
        {
            'urls': [{'url': 'https://example.com/file.rar', 'bytes': 10000}],
            'fileIdx': 1,
            'fileMustInclude': [],
        },
        separators=(',', ':'),
    )
    compressed = compress_to_encoded_uri_component(payload)
    assert decompress_from_encoded_uri_component(compressed) == payload


# --- cross-check against the real reference implementation -------------


def test_matches_reference_lz_string_output():
    """Byte-for-byte against real `lz-string@1.5.0` (npm), captured
    offline - see module docstring. This is the strongest evidence
    available offline that stremio-server-go (which decodes with the
    same algorithm) will accept a payload this module produces.
    """
    vectors = {
        '': 'Q',
        'hello world': 'BYUwNmD2AEDukCcwBMg',
        'héllo wörld 日本語 Ω': 'BYS4NmD2AEDuBvAnMATahT00DTmg8qOoSuAg',
        'abcabcabcabcabcabcabcabcabcabcabcabcabcabcabc': 'IYIwxqHpPXUNo+Q',
        json.dumps(
            {
                'urls': [{'url': 'https://example.com/file.rar', 'bytes': 10000}],
                'fileIdx': 1,
                'fileMustInclude': [],
            },
            separators=(',', ':'),
        ): (
            'N4IgrgTgNgziBcBtUkoJACwC5YA43gHpCBTADwEMBbXKEgOgGMB7KwgMwEs76IKIQAGhAAjAJ5'
            'YSceAEYADArkBfALrCudAJIATMghnruJALJgYWTQDtGUMNpIJEKpUA'
        ),
    }
    for text, expected in vectors.items():
        assert compress_to_encoded_uri_component(text) == expected


# --- decompress edge cases ------------------------------------------------


def test_decompress_none_input_returns_empty_string():
    assert decompress_from_encoded_uri_component(None) == ''


def test_compress_none_input_returns_empty_string():
    assert compress_to_encoded_uri_component(None) == ''


def test_decompress_empty_string_returns_none():
    """Upstream distinguishes "no input" (None -> '') from "empty
    compressed string" (which can never be a valid compressed stream,
    since even compressing '' itself yields a 1-character payload)."""
    assert decompress_from_encoded_uri_component('') is None


def test_decompress_garbage_characters_returns_none():
    assert decompress_from_encoded_uri_component('not valid lz!!!') is None


def test_decompress_truncated_stream_returns_none():
    compressed = compress_to_encoded_uri_component('some reasonably long input string here')
    assert decompress_from_encoded_uri_component(compressed[:2]) is None


def test_decompress_tolerates_space_for_plus_substitution():
    """A URL layer between compression and decompression may turn a
    literal '+' into a space (application/x-www-form-urlencoded
    convention) - upstream's decompressFromEncodedURIComponent undoes
    this before alphabet lookup, so this module must too."""
    text = 'x' * 200  # long enough to be likely to contain a '+' digit
    compressed = compress_to_encoded_uri_component(text)
    if '+' not in compressed:
        return  # nothing to prove for this particular input
    mangled = compressed.replace('+', ' ')
    assert decompress_from_encoded_uri_component(mangled) == text
