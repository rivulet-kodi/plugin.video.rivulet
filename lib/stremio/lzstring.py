"""Dependency-free LZ-String codec: `compress_to_encoded_uri_component` /
`decompress_from_encoded_uri_component`. Pure stdlib, no Kodi imports.

Why this exists: stremio-core builds a `GET {server}/{kind}/create?lz=
<payload>` URL for archive (rar/zip/7zip/tar/tgz), nzb and ftp(s) stream
sources - `Stream::convert()` in stremio-core's
src/types/resource/stream.rs (e.g. the Rar branch, stream.rs:251-270;
`ftp_url_handler`, stream.rs:186-214) - where `<payload>` is the
request-body JSON run through JS library `lz-string`'s
`compressToEncodedURIComponent()`. stremio-core gets this from the Rust
`lz-str` crate (this checkout's Cargo.lock pins `lz-str = "0.2"` ->
0.2.1, https://github.com/adumbidiot/lz-str-rs), itself a straight port
of the original `lz-string` JS library
(https://github.com/pieroxy/lz-string, (c) pieroxy, WTFPL).
stremio-server-go decodes the `lz` query param with the very same
algorithm server-side, so this port has to be BIT-FOR-BIT compatible
with the reference implementation - "round-trips against itself" is not
good enough on its own.

Verified directly against both upstream references while writing this
module (see tests/test_lzstring.py): `npm`-installing the real
`lz-string@1.5.0` package (the exact upstream pieroxy library) and
reading `adumbidiot/lz-str-rs`'s `src/constants.rs` (the crate
stremio-core actually depends on, confirmed via this checkout's
Cargo.lock). Both agree on a 64-character URI-safe alphabet ending in
`$` - NOT a second `-` or `=`, which the "URI-safe" name alone might
suggest:

    ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+-$

and `decompressFromEncodedURIComponent` tolerates a caller (or an
intermediate URL layer) that turned a literal `+` in the payload into a
space - the historical `application/x-www-form-urlencoded` convention -
by mapping every ` ` back to `+` BEFORE decoding token-by-token; it does
NOT treat `=` specially in any way. Four fixed inputs (empty string,
ASCII, an archive-create JSON body, and a non-ASCII BMP string) were
compressed with the real `lz-string@1.5.0` package and produce
byte-identical output to this module (see test_lzstring.py's
`test_matches_reference_lz_string_output`), which is the strongest
evidence available offline that stremio-server-go will decode a payload
built by this module correctly.

Algorithm: LZ78-family compression - a growing dictionary of
previously-seen substrings, each referenced by an index that grows in
bit-width as the dictionary grows - packed 6 bits per output character
(`_BITS_PER_CHAR`) and mapped through the alphabet above.
`_compress`/`_decompress_digits` below are a deliberately close
transliteration of lz-string.js's `_compress`/`_decompress` (variable
names kept close to the original) so a diff against upstream is easy to
audit; unlike the JS original (which returns `undefined`/throws on
malformed input in unspecified ways) or the Rust port (which threads
`Option` throughout), a malformed/truncated compressed payload here
cleanly returns None from `decompress_from_encoded_uri_component`
rather than raising - this module is never fed attacker-controlled
input in this addon (it only ever decompresses payloads it just
produced, in tests), but "return None on garbage" is a safer default
than an uncaught exception regardless.

Scope/limitation: operates on Python `str` code points directly. JS
strings are sequences of UTF-16 CODE UNITS, so a supplementary-plane
character (code point > 0xFFFF - most emoji) is stored as TWO UTF-16
code units there but ONE Python `str` element here. Round-tripping
through this module alone is always correct (`decompress_from_
encoded_uri_component(compress_to_encoded_uri_component(x)) == x` for
any `x`), but a payload compressed by this module would not byte-for-
byte match one produced by the JS/Rust reference for text containing
such characters. Every payload this module actually encodes for this
addon (URLs, filenames, small integers, JSON punctuation) is entirely
within the Basic Multilingual Plane, so this is a documented non-goal,
not a bug.
"""

#: The 64-character alphabet `compressToEncodedURIComponent` packs 6-bit
#: codes into (adumbidiot/lz-str-rs src/constants.rs `URI_KEY`; identical
#: to pieroxy/lz-string.js's `keyStrUriSafe`). Ends in '$', not '=' or a
#: second '-' - see the module docstring for why that matters here.
_URI_SAFE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+-$"
_URI_SAFE_REVERSE = {char: index for index, char in enumerate(_URI_SAFE_ALPHABET)}

#: Bits packed into each output character for the URI-safe variant
#: (`compressToEncodedURIComponent`/`decompressFromEncodedURIComponent`
#: always use 6; other lz-string variants like compressToBase64 also use
#: 6, only compress()/decompress() proper use 16).
_BITS_PER_CHAR = 6

#: Stream codes read/written at "new dictionary entry" boundaries -
#: mirrors adumbidiot/lz-str-rs's `U8_CODE`/`U16_CODE`/`CLOSE_CODE`
#: constants.rs. A code of 0 or 1 always means "the next 8 (or 16) bits
#: are a raw character code being added to the dictionary", 2 always
#: means "end of stream" - never a real dictionary index, so the first
#: three dictionary slots are permanently unused placeholders.
_MARKER_U8 = 0
_MARKER_U16 = 1
_MARKER_CLOSE = 2

#: Width, in bits, of the very first code in a compressed stream (before
#: the dictionary has grown at all) - lz-string.js's `_decompress` reads
#: this many bits via `Math.pow(2, 2)` before ever consulting `numBits`.
_START_CODE_BITS = 2


def _compress(uncompressed, bits_per_char):
    """Port of lz-string.js's `_compress` (LZString.js:109-324). Returns
    the list of `bits_per_char`-wide integer codes ready for alphabet
    mapping - the caller still has to look each one up in
    `_URI_SAFE_ALPHABET`.
    """
    context_dictionary = {}
    context_dictionary_to_create = set()
    context_w = ''
    context_enlarge_in = 2  # Compensate for the first entry, which should not count.
    context_dict_size = 3
    context_num_bits = 2
    context_data = []
    context_data_val = 0
    context_data_position = 0

    def write_bit(bit):
        nonlocal context_data_val, context_data_position
        context_data_val = (context_data_val << 1) | bit
        if context_data_position == bits_per_char - 1:
            context_data_position = 0
            context_data.append(context_data_val)
            context_data_val = 0
        else:
            context_data_position += 1

    def write_bits(count, value):
        # LSB-first: matches lz-string.js's `value & 1` / `value >>= 1` loops.
        for _ in range(count):
            write_bit(value & 1)
            value >>= 1

    def produce_w():
        nonlocal context_enlarge_in, context_num_bits
        if context_w in context_dictionary_to_create:
            first_char = ord(context_w[0])
            if first_char < 256:
                write_bits(context_num_bits, _MARKER_U8)
                write_bits(8, first_char)
            else:
                write_bits(context_num_bits, _MARKER_U16)
                write_bits(16, first_char)
            context_enlarge_in -= 1
            if context_enlarge_in == 0:
                context_enlarge_in = 1 << context_num_bits
                context_num_bits += 1
            context_dictionary_to_create.discard(context_w)
        else:
            write_bits(context_num_bits, context_dictionary[context_w])
        context_enlarge_in -= 1
        if context_enlarge_in == 0:
            context_enlarge_in = 1 << context_num_bits
            context_num_bits += 1

    for context_c in uncompressed:
        if context_c not in context_dictionary:
            context_dictionary[context_c] = context_dict_size
            context_dict_size += 1
            context_dictionary_to_create.add(context_c)

        context_wc = context_w + context_c
        if context_wc in context_dictionary:
            context_w = context_wc
        else:
            produce_w()
            context_dictionary[context_wc] = context_dict_size
            context_dict_size += 1
            context_w = context_c

    if context_w != '':
        produce_w()

    write_bits(context_num_bits, _MARKER_CLOSE)

    # Flush the last (possibly partial) output character. Deliberately a
    # do/while: lz-string.js always emits at least one more bit here even
    # when the stream already sits on a character boundary, producing a
    # trailing all-zero-bit character in that case - matching this exactly
    # is required for the output to be byte-identical to the reference.
    while True:
        write_bit(0)
        if context_data_position == 0:
            break

    return context_data


def compress_to_encoded_uri_component(text):
    """`lz-string`'s `compressToEncodedURIComponent(text)`: compress
    `text` into a string safe to drop straight into a URL query value
    (no further percent-encoding needed - the whole point of this
    variant). `None` compresses to `''`, matching upstream.
    """
    if text is None:
        return ''
    digits = _compress(text, _BITS_PER_CHAR)
    return ''.join(_URI_SAFE_ALPHABET[digit] for digit in digits)


def _decompress_digits(digits, bits_per_char):
    """Port of lz-string.js's `_decompress` (LZString.js:332-492), taking
    already alphabet-decoded integer codes. Returns a list of code
    points, or None for a malformed/truncated stream (JS's `_decompress`
    returns "" or an unspecified falsy value in the equivalent cases;
    the Rust `lz-str` port models this as `Option::None` - reproduced
    here for the same reason: a caller must never mistake "decode
    failed" for a legitimately-empty result).
    """
    if not digits:
        return None

    reset_value = 1 << (bits_per_char - 1)
    index = 1
    data_val = digits[0]
    data_position = reset_value

    def read_bit():
        nonlocal data_val, data_position, index
        result_bit = data_val & data_position
        data_position >>= 1
        if data_position == 0:
            data_position = reset_value
            if index >= len(digits):
                return None
            data_val = digits[index]
            index += 1
        return 1 if result_bit else 0

    def read_bits(count):
        bits = 0
        power = 1
        max_power = 1 << count
        while power != max_power:
            bit = read_bit()
            if bit is None:
                return None
            if bit:
                bits |= power
            power <<= 1
        return bits

    code = read_bits(_START_CODE_BITS)
    if code == _MARKER_U8:
        first_entry = read_bits(8)
    elif code == _MARKER_U16:
        first_entry = read_bits(16)
    elif code == _MARKER_CLOSE:
        return []
    else:
        return None
    if first_entry is None:
        return None

    # Slots 0-2 are the permanently-unused U8/U16/CLOSE marker placeholders
    # (see _MARKER_* docstring) - never reached via the `code < len(dictionary)`
    # lookup below, since those code values are always intercepted above.
    dictionary = [[], [], [], [first_entry]]
    word = [first_entry]
    result = [first_entry]
    num_bits = 3
    enlarge_in = 4

    while True:
        if index > len(digits):
            return None
        code = read_bits(num_bits)
        if code is None:
            return None

        if code == _MARKER_U8:
            bits = read_bits(8)
            if bits is None:
                return None
            dictionary.append([bits])
            code = len(dictionary) - 1
            enlarge_in -= 1
        elif code == _MARKER_U16:
            bits = read_bits(16)
            if bits is None:
                return None
            dictionary.append([bits])
            code = len(dictionary) - 1
            enlarge_in -= 1
        elif code == _MARKER_CLOSE:
            return result

        if enlarge_in == 0:
            enlarge_in = 1 << num_bits
            num_bits += 1

        if code < len(dictionary):
            entry = dictionary[code]
        elif code == len(dictionary):
            entry = word + [word[0]]
        else:
            return None

        result.extend(entry)
        dictionary.append(word + [entry[0]])
        enlarge_in -= 1
        word = entry

        if enlarge_in == 0:
            enlarge_in = 1 << num_bits
            num_bits += 1


def decompress_from_encoded_uri_component(data):
    """`lz-string`'s `decompressFromEncodedURIComponent(data)`, the
    inverse of `compress_to_encoded_uri_component`. `None` decompresses
    to `''`; `''` decompresses to `None` (matching upstream's `null`) -
    upstream draws this distinction because compressing `None` (JS
    `null`/`undefined`) legitimately produces `''`, so `''` alone can't
    mean "empty input" on the way back in. A malformed payload (stray
    characters outside the URI-safe alphabet, or a truncated bitstream)
    also returns None.
    """
    if data is None:
        return ''
    if data == '':
        return None
    # A URL layer between compression and this call may have turned a
    # literal '+' into a space (application/x-www-form-urlencoded), so
    # undo that before alphabet lookup - matches upstream exactly.
    data = data.replace(' ', '+')
    try:
        digits = [_URI_SAFE_REVERSE[char] for char in data]
    except KeyError:
        return None
    code_points = _decompress_digits(digits, _BITS_PER_CHAR)
    if code_points is None:
        return None
    return ''.join(chr(code_point) for code_point in code_points)
