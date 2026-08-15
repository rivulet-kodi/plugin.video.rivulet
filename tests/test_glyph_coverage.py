"""Guard against tofu boxes: text Kodi renders with a font that has no
glyph for it.

Kodi draws a missing glyph as an empty rectangle. Nothing fails, nothing
logs, and a unit test that asserts on the *string* still passes - the
defect only exists on screen. Two shipped this way before this file
existed:

* ``\u25b2`` prefixing the seeder column, in a ``Mono26`` label.
* ``\u2605`` in the streams info panel, after that panel was restyled
  from a NotoSans font to ``Mono26``. The string had been fine for
  releases; changing the *font* broke it.

That second one is the shape to keep in mind: the text and the font are
edited in different files, so neither diff looks wrong on its own.

Coverage facts below are measured from the fonts Kodi's default skin
actually ships, and :func:`test_hardcoded_coverage_still_matches_the_real_fonts`
re-derives them from those files whenever the machine has them, so the
tables cannot quietly drift. They are hardcoded because CI has no Kodi
install to read.

Scope: strings *we* author - skin XML, our Python, our translations.
Text a Stremio addon hands us is a separate problem already solved at
runtime by ``lib.stremio.streaminfo.clean_text()``, which strips emoji
and other unrenderable ranges out of third-party titles before they ever
reach a label.
"""
import glob
import os
import re
import struct
import unicodedata

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SKIN_DIR = os.path.join(_REPO_ROOT, 'resources', 'skins', 'Default', '1080i')
_LANG_GLOB = os.path.join(_REPO_ROOT, 'resources', 'language', '*', 'strings.po')

#: Kodi's default skin maps every font name except ``Mono26`` to
#: NotoSans-Regular.ttf, and ``Mono26`` alone to NotoMono-Regular.ttf.
_MONO_FONT = 'Mono26'

#: Non-ASCII characters present in BOTH faces - safe in any label.
_SAFE_ANYWHERE = set('\u00b0\u00b7\u00d7\u2013\u2014\u2022\u2026')

#: Present in NotoSans, ABSENT from NotoMono. Safe only in a label whose
#: font is not `_MONO_FONT`; in a ``Mono26`` label these are tofu.
_SANS_ONLY = set('\u2191\u2192\u25b2\u25cf\u2605')

_RENDERABLE = _SAFE_ANYWHERE | _SANS_ONLY

#: Unicode blocks that are decorative symbols rather than script text:
#: arrows, math, geometric shapes, dingbats, emoji, variation selectors.
#: Letters and script-specific punctuation (Arabic, CJK, fullwidth) are
#: deliberately NOT in here - NotoSans covers them, and every translation
#: legitimately uses them.
_SYMBOL_RANGES = ((0x2190, 0x2BFF), (0xFE00, 0xFE0F), (0x1F000, 0x1FAFF))

#: Kodi label markup and skin substitutions, stripped before inspecting
#: the literal text an XML label actually renders.
_SUBSTITUTION_RE = re.compile(r'\$(?:INFO|ADDON|LOCALIZE|VAR|ESCINFO)\[[^\]]*\]')
_BBCODE_RE = re.compile(r'\[/?(?:B|I|COLOR|UPPERCASE|LOWERCASE|CAPITALIZE|CR)[^\]]*\]')

#: `Mono26` controls that Python writes into. Kodi resolves the font in
#: the XML and the text in the Python, so neither file alone shows the
#: pairing; anything listed here has been checked by hand and must stay
#: checked. Adding a `Mono26` control that Python populates without
#: listing it fails `test_python_populated_mono_controls_are_declared`.
_DECLARED_MONO_CONTROLS = {
    30100: 'streamswindow SOURCES_COUNT - "%d SOURCES", digits and ASCII only',
    30101: 'streamswindow ADDONS_COUNT - "%d ADDONS", digits and ASCII only',
    30102: 'streamswindow CACHED_COUNT - "%d CACHED", digits and ASCII only',
}


def _is_symbol(ch):
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _SYMBOL_RANGES)


def _describe(ch):
    return 'U+%04X %s' % (ord(ch), unicodedata.name(ch, 'unnamed'))


def _read(path):
    with open(path, encoding='utf-8') as handle:
        return handle.read()


def _skin_files():
    return sorted(glob.glob(os.path.join(_SKIN_DIR, '*.xml')))


def _controls(xml_text):
    """Yield ``(font, label_text)`` for every control that declares both.

    Deliberately regex-based rather than a real XML walk: a Kodi control
    nests its ``<font>`` and ``<label>`` as direct children, and the
    layouts repeat the same tag names, so a flat scan over each control
    block is both sufficient and easier to read than tree surgery.
    """
    for block in re.findall(r'<control\b.*?</control>', xml_text, re.S):
        font = re.search(r'<font>([^<]+)</font>', block)
        if not font:
            continue
        for label in re.findall(r'<label>(.*?)</label>', block, re.S):
            yield font.group(1).strip(), label


def _literal_text(label):
    """The characters an XML label renders, minus markup and anything
    substituted at runtime (whose content this file cannot see)."""
    return _BBCODE_RE.sub('', _SUBSTITUTION_RE.sub('', label))


def test_skin_labels_only_use_glyphs_their_font_has():
    """The `\u25b2` bug: a static label in a `Mono26` control."""
    offenders = []
    for path in _skin_files():
        for font, label in _controls(_read(path)):
            allowed = _SAFE_ANYWHERE if font == _MONO_FONT else _RENDERABLE
            for ch in _literal_text(label):
                if ord(ch) > 127 and ch not in allowed:
                    offenders.append(
                        '%s: <font>%s</font> label %r contains %s'
                        % (os.path.basename(path), font, label.strip()[:60], _describe(ch))
                    )
    assert not offenders, 'unrenderable glyphs in skin labels:\n  ' + '\n  '.join(offenders)


def _python_sources():
    for pattern in ('lib/**/*.py', 'default.py', 'service.py'):
        yield from glob.glob(os.path.join(_REPO_ROOT, pattern), recursive=True)


def _display_strings(source):
    """String literals excluding those built into a compiled regex.

    ``lib.stremio.streaminfo`` matches Torrentio/AIOStreams field markers
    by their emoji, so its patterns legitimately contain characters no
    font can draw. Those are parsed, never rendered - `clean_text()`
    strips them from anything that reaches a label.
    """
    without_patterns = re.sub(r're\.compile\((?:[^()]|\([^()]*\))*\)', '', source, flags=re.S)
    without_comments = re.sub(r'(?m)#.*$', '', without_patterns)
    for match in re.finditer(r"'([^'\n]*)'|\"([^\"\n]*)\"", without_comments):
        yield match.group(1) if match.group(1) is not None else match.group(2)


def _decoded(literal):
    """Characters a literal contributes, resolving ``\\uXXXX`` escapes -
    every glyph this project ships is written escaped rather than raw."""
    for match in re.finditer(r'\\u([0-9a-fA-F]{4})', literal):
        yield chr(int(match.group(1), 16))
    yield from re.sub(r'\\u[0-9a-fA-F]{4}', '', literal)


def test_python_display_strings_use_renderable_glyphs():
    """Catches a glyph NEITHER face has - a check mark, an emoji - being
    put into a label, whatever font that label ends up using."""
    offenders = []
    for path in _python_sources():
        source = _read(path)
        for literal in _display_strings(source):
            for ch in _decoded(literal):
                if ord(ch) > 127 and ch not in _RENDERABLE:
                    offenders.append(
                        '%s: %r contains %s'
                        % (os.path.relpath(path, _REPO_ROOT), literal[:60], _describe(ch))
                    )
    assert not offenders, 'unrenderable glyphs in display strings:\n  ' + '\n  '.join(offenders)


def test_python_populated_mono_controls_are_declared():
    """The `\u2605` bug: restyling a control to `Mono26` while Python keeps
    feeding it text that only NotoSans can draw.

    Static analysis cannot follow a string from `setLabel()` back to a
    font, so this makes the pairing explicit instead: every `Mono26`
    control Python writes to has to be declared above, which forces
    whoever adds one to look at what it is being fed.
    """
    mono_ids = set()
    for path in _skin_files():
        for block in re.findall(r'<control\b.*?</control>', _read(path), re.S):
            control_id = re.search(r'<control\b[^>]*\bid="(\d+)"', block)
            font = re.search(r'<font>([^<]+)</font>', block)
            if control_id and font and font.group(1).strip() == _MONO_FONT:
                mono_ids.add(int(control_id.group(1)))

    written = set()
    for path in _python_sources():
        source = _read(path)
        constants = dict(re.findall(r'(?m)^([A-Z][A-Z0-9_]*)\s*=\s*(\d{5})\s*(?:#.*)?$', source))
        for name, value in constants.items():
            if re.search(r'getControl\(\s*%s\s*\)\s*\.\s*set(?:Label|Text)\(' % name, source):
                written.add(int(value))

    undeclared = sorted((mono_ids & written) - set(_DECLARED_MONO_CONTROLS))
    assert not undeclared, (
        'Mono26 controls written from Python but not declared in '
        '_DECLARED_MONO_CONTROLS: %s. Check what text each is fed - Mono26 '
        'is NotoMono and cannot draw %s - then add it with a note.'
        % (undeclared, ''.join(sorted(_SANS_ONLY)))
    )

    stale = sorted(set(_DECLARED_MONO_CONTROLS) - mono_ids)
    assert not stale, (
        'declared Mono26 controls that are no longer Mono26 (or no longer '
        'exist): %s - drop them from _DECLARED_MONO_CONTROLS' % stale
    )


def test_translations_avoid_decorative_symbols():
    """A translator reaching for a check mark or an arrow would render a
    box in every language that used it. Script text is untouched: this
    only rejects the symbol blocks."""
    offenders = []
    for path in sorted(glob.glob(_LANG_GLOB)):
        locale = os.path.basename(os.path.dirname(path))
        for match in re.finditer(r'msg(?:id|str) "([^"]*)"', _read(path)):
            for ch in match.group(1):
                if _is_symbol(ch) and ch not in _RENDERABLE:
                    offenders.append('%s: %r contains %s' % (locale, match.group(1)[:60], _describe(ch)))
    assert not offenders, 'unrenderable symbols in translations:\n  ' + '\n  '.join(offenders)


# ---------------------------------------------------------------------------
# drift guard


_ESTUARY_FONTS = '/usr/share/kodi/addons/skin.estuary/fonts'


def _cmap(path):
    """Code points in a TrueType font's format-4 cmap subtable."""
    with open(path, 'rb') as handle:
        data = handle.read()
    table_offset = None
    for index in range(struct.unpack('>H', data[4:6])[0]):
        record = 12 + index * 16
        if data[record:record + 4] == b'cmap':
            table_offset = struct.unpack('>I', data[record + 8:record + 12])[0]
            break
    subtable = None
    for index in range(struct.unpack('>H', data[table_offset + 2:table_offset + 4])[0]):
        record = table_offset + 4 + index * 8
        candidate = table_offset + struct.unpack('>HHI', data[record:record + 8])[2]
        if struct.unpack('>H', data[candidate:candidate + 2])[0] == 4:
            subtable = candidate
    seg_bytes = struct.unpack('>H', data[subtable + 6:subtable + 8])[0]
    ends = subtable + 14
    starts = ends + seg_bytes + 2
    covered = set()
    for seg in range(seg_bytes // 2):
        end = struct.unpack('>H', data[ends + seg * 2:ends + seg * 2 + 2])[0]
        start = struct.unpack('>H', data[starts + seg * 2:starts + seg * 2 + 2])[0]
        if start != 0xFFFF:
            covered.update(range(start, min(end, 0xFFFF) + 1))
    return covered


@pytest.mark.skipif(
    not os.path.isdir(_ESTUARY_FONTS),
    reason='needs a local Kodi install to read the real skin fonts',
)
def test_hardcoded_coverage_still_matches_the_real_fonts():
    """Keeps the tables above honest on any machine that has Kodi. The
    tables are what CI enforces, so they must not drift from the fonts
    they claim to describe."""
    sans = _cmap(os.path.join(_ESTUARY_FONTS, 'NotoSans-Regular.ttf'))
    mono = _cmap(os.path.join(_ESTUARY_FONTS, 'NotoMono-Regular.ttf'))

    for ch in sorted(_SAFE_ANYWHERE):
        assert ord(ch) in sans, '%s is in _SAFE_ANYWHERE but NotoSans lacks it' % _describe(ch)
        assert ord(ch) in mono, '%s is in _SAFE_ANYWHERE but NotoMono lacks it' % _describe(ch)
    for ch in sorted(_SANS_ONLY):
        assert ord(ch) in sans, '%s is in _SANS_ONLY but NotoSans lacks it' % _describe(ch)
        assert ord(ch) not in mono, (
            '%s is in _SANS_ONLY but NotoMono HAS it - move it to _SAFE_ANYWHERE' % _describe(ch)
        )
