"""Static checks over `resources/skins/Default/1080i/*.xml`.

The skin layer has no compiler: Kodi loads whatever is on disk and, for
almost every mistake below, degrades silently rather than erroring - a
blank screen, a fallback glyph, an untranslated string - so a reviewer
sees nothing wrong and CI says nothing wrong. Every rule here names the
real defect it would have caught:

1. Well-formedness - a truncated/unclosed tag (a hand-edit slip) fails to
   parse; the root of every window XML must be exactly one `<window>`.
2. Fonts - addon skins cannot ship their own `Font.xml` or a TTF
   (`GUIFontManager::LoadFonts` only reads the ACTIVE skin's, normally
   skin.estuary's); a `<font>` name outside that ladder silently falls
   back to `font13`.
3. Textures - a `<texture>` filename with no file in
   `resources/skins/Default/media/` renders nothing at all (the mirror
   image of the `fade_bottom.png`/`fade_edge.png` cleanup, which deleted
   two files that had gone unreferenced).
4. `colordiffuse` - Kodi wants exactly 8 hex digits (AARRGGBB); a 6-digit
   value is silently misread. This repo shipped `FF3A3A3A` where
   `#0A0C0E` was meant and nobody saw it until the backdrop was measured.
5. Addon strings - `$ADDON[plugin.video.rivulet <id>]` with no matching
   `strings.po` entry renders blank; `$LOCALIZE[...]` reads KODI's own
   catalog, not the addon's, so it *always* renders blank in an addon
   skin. A handful of labels also shipped as bare, untranslated English
   literals in a 14-language addon.
6. Layout invariants - a `<list>`/`<fixedlist>` whose `<itemlayout>` and
   `<focusedlayout>` disagree on row height makes the focused row jump
   size; a list height that is not a whole multiple of its row height
   clips the last row (hit once in `DetailWindow.xml`).
"""
import glob
import os
import re
import xml.etree.ElementTree as ET

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SKIN_DIR = os.path.join(_REPO_ROOT, 'resources', 'skins', 'Default', '1080i')
_MEDIA_DIR = os.path.join(_REPO_ROOT, 'resources', 'skins', 'Default', 'media')
_ENGLISH_PO = os.path.join(_REPO_ROOT, 'resources', 'language', 'resource.language.en_gb', 'strings.po')

#: The real Estuary Font.xml, present only on a machine with Kodi
#: installed. Never required: every test below that needs it is
#: `skipif`-guarded so a missing system file cannot fail CI.
_ESTUARY_FONT_XML = '/usr/share/kodi/addons/skin.estuary/xml/Font.xml'


def _skin_files():
    return sorted(glob.glob(os.path.join(_SKIN_DIR, '*.xml')))


def _read(path):
    with open(path, encoding='utf-8') as handle:
        return handle.read()


# ---------------------------------------------------------------------------
# 1. well-formedness


def _root_tag(path):
    """The root element's tag name. Raises `ET.ParseError` on malformed XML -
    that failure alone is the well-formedness check for a truncated file."""
    return ET.parse(path).getroot().tag


def test_skin_file_discovery_finds_every_window():
    """Guards every other test in this file: a glob that silently matches
    nothing would make every rule below pass by scanning zero files."""
    paths = _skin_files()
    assert len(paths) == 14, 'expected 14 window XMLs in %s, found %d: %s' % (_SKIN_DIR, len(paths), paths)


@pytest.mark.parametrize('path', _skin_files(), ids=lambda p: os.path.basename(p))
def test_skin_xml_is_well_formed_single_window(path):
    """A hand-edit that leaves a tag unclosed fails to parse and Kodi shows
    a blank screen; the root of a window XML must be exactly one
    `<window>`, not e.g. a copy-pasted `<includes>` fragment."""
    assert _root_tag(path) == 'window', '%s: root element is <%s>, Kodi window XML must be <window>' % (path, _root_tag(path))


def test_malformed_skin_xml_fails_to_parse_regression(tmp_path):
    """Regression: an unclosed tag must not parse silently."""
    bad = tmp_path / 'Bad.xml'
    bad.write_text('<window><controls><control type="image"></window>', encoding='utf-8')
    with pytest.raises(ET.ParseError):
        _root_tag(str(bad))


def test_skin_xml_with_wrong_root_is_rejected_regression(tmp_path):
    """Regression: a well-formed file whose root is not `<window>` (e.g. a
    copy-pasted `<includes>` fragment) must fail the root check."""
    bad = tmp_path / 'Bad.xml'
    bad.write_text('<includes><window /></includes>', encoding='utf-8')
    assert _root_tag(str(bad)) != 'window'


# ---------------------------------------------------------------------------
# 2. fonts


#: Kodi resolves a `<font>` value against the active skin's fontset;
#: `GUIFontManager::LoadFonts` silently falls back to `font13` for any name
#: it does not find - no error, no log line a reviewer would notice.
#: Addon skins cannot bundle a `Font.xml` or TTF of their own, so this is
#: the complete, fixed universe this skin can draw from: nine names, each
#: tied to exactly one point size (26 - `Mono26` - is the only monospace
#: face). Hardcoded so the check runs without Kodi installed;
#: `test_hardcoded_font_sizes_match_estuary` below keeps it honest against
#: the real Font.xml when one is available.
_ESTUARY_FONT_SIZES = {
    'font10': 23,
    'font12': 25,
    'Mono26': 26,
    'font27': 27,
    'font13': 30,
    'font32': 32,
    'font37': 37,
    'font45': 45,
    'font60': 60,
}
_ESTUARY_FONT_NAMES = frozenset(_ESTUARY_FONT_SIZES)


def _font_values(path):
    """`<font>` text values in one skin XML file, in document order."""
    return [el.text.strip() for el in ET.parse(path).iter('font') if el.text and el.text.strip()]


def test_skin_fonts_exist_in_estuary_ladder():
    """The `font13` fallback bug: a `<font>` name Estuary does not define
    renders with the wrong size on a device, unnoticed in review."""
    offenders = []
    total = 0
    for path in _skin_files():
        for name in _font_values(path):
            total += 1
            if name not in _ESTUARY_FONT_NAMES:
                offenders.append('%s: <font>%s</font> is not in the Estuary ladder %s - it silently falls back to font13'
                                  % (path, name, sorted(_ESTUARY_FONT_NAMES)))
    assert total, 'no <font> tags found across %s - the check is not scanning anything' % _SKIN_DIR
    assert not offenders, 'unresolvable skin fonts:\n  ' + '\n  '.join(offenders)


def _real_estuary_font_sizes():
    """`{name: size}` for the "Default" fontset of the real Font.xml."""
    root = ET.parse(_ESTUARY_FONT_XML).getroot()
    fontset = root.find("./fontset[@id='Default']")
    sizes = {}
    for font in fontset.findall('font'):
        name = font.findtext('name')
        size = font.findtext('size')
        if name is not None and size is not None:
            sizes[name.strip()] = int(size.strip())
    return sizes


@pytest.mark.skipif(not os.path.exists(_ESTUARY_FONT_XML), reason='skin.estuary is not installed on this machine; a missing system file must not fail CI')
def test_hardcoded_font_sizes_match_estuary():
    """Keeps `_ESTUARY_FONT_SIZES` honest against the real Font.xml on any
    machine that has Kodi, so drift (a renamed or resized font) is caught
    even though the main check above never touches the system file."""
    real = _real_estuary_font_sizes()
    for name, size in _ESTUARY_FONT_SIZES.items():
        assert real.get(name) == size, '%s is %s in skin.estuary, hardcoded as %s' % (name, real.get(name), size)


def test_unknown_font_name_is_rejected_regression(tmp_path):
    """Regression: a font name Estuary does not define - the 30340
    dropdown's own defect class, one layer down - must be flagged."""
    bad = tmp_path / 'Bad.xml'
    bad.write_text('<window><controls><control type="label"><font>font99</font></control></controls></window>', encoding='utf-8')
    names = _font_values(str(bad))
    assert names == ['font99']
    assert not set(names) & _ESTUARY_FONT_NAMES


# ---------------------------------------------------------------------------
# 3. textures


def _media_files():
    return {os.path.basename(p) for p in glob.glob(os.path.join(_MEDIA_DIR, '*')) if os.path.isfile(p)}


def _texture_refs(path):
    """`(tag, filename)` for every element whose tag names a texture slot
    (`texture`, `texturefocus`, `midtexture`, `texturesliderbar`, ...) and
    carries a static filename. Empty content (Python fills it at runtime,
    e.g. a poster placeholder) and `$INFO[...]` bindings are not static
    and cannot be checked here."""
    refs = []
    for el in ET.parse(path).iter():
        if 'texture' not in el.tag.lower():
            continue
        value = (el.text or '').strip()
        if value and not value.startswith('$'):
            refs.append((el.tag, value))
    return refs


def test_skin_textures_resolve_to_real_media_files():
    """The inverse of the `fade_bottom.png`/`fade_edge.png` cleanup: a
    texture reference with no file behind it renders nothing at all."""
    media = _media_files()
    offenders = []
    total = 0
    for path in _skin_files():
        for tag, value in _texture_refs(path):
            total += 1
            if value not in media:
                offenders.append('%s: <%s>%s</%s> has no file in %s - it renders nothing'
                                  % (path, tag, value, tag, _MEDIA_DIR))
    assert total, 'no static texture references found across %s - the check is not scanning anything' % _SKIN_DIR
    assert not offenders, 'unresolvable skin textures:\n  ' + '\n  '.join(offenders)


def test_missing_texture_file_is_rejected_regression(tmp_path):
    """Regression: a texture filename with no file behind it must be
    flagged, not silently treated as fine."""
    bad = tmp_path / 'Bad.xml'
    bad.write_text('<window><controls><control type="image"><texture>ghost.png</texture></control></controls></window>', encoding='utf-8')
    refs = _texture_refs(str(bad))
    assert refs == [('texture', 'ghost.png')]
    assert 'ghost.png' not in _media_files()


# ---------------------------------------------------------------------------
# 4. colordiffuse


_HEX8_RE = re.compile(r'^[0-9A-Fa-f]{8}$')


def _colordiffuse_refs(path):
    """`(tag, value)` for every `colordiffuse` attribute in one file."""
    return [(el.tag, el.get('colordiffuse')) for el in ET.parse(path).iter() if el.get('colordiffuse') is not None]


def test_skin_colordiffuse_is_eight_hex_digits():
    """The `FF3A3A3A`-for-`#0A0C0E` bug: a `colordiffuse` value with the
    wrong digit count is silently misread rather than rejected."""
    offenders = []
    total = 0
    for path in _skin_files():
        for tag, value in _colordiffuse_refs(path):
            if value.startswith('$'):
                continue  # $INFO[...] binding, resolved at runtime
            total += 1
            if not _HEX8_RE.match(value):
                offenders.append('%s: <%s colordiffuse="%s"> is %d hex digits, not AARRGGBB (8)'
                                  % (path, tag, value, len(value)))
    assert total, 'no colordiffuse attributes found across %s - the check is not scanning anything' % _SKIN_DIR
    assert not offenders, 'malformed skin colordiffuse values:\n  ' + '\n  '.join(offenders)


def test_six_hex_colordiffuse_is_rejected_regression(tmp_path):
    """Regression: a 6-digit value (the alpha channel dropped) must be
    flagged - this is exactly how FF3A3A3A-for-#0A0C0E shipped."""
    bad = tmp_path / 'Bad.xml'
    bad.write_text('<window><controls><control type="image"><texture colordiffuse="3A3A3A">white.png</texture></control></controls></window>', encoding='utf-8')
    ((_tag, value),) = _colordiffuse_refs(str(bad))
    assert value == '3A3A3A'
    assert not _HEX8_RE.match(value)


# ---------------------------------------------------------------------------
# 5. addon strings: $ADDON ids, $LOCALIZE, bare English literals


_ADDON_STRING_RE = re.compile(r'\$ADDON\[plugin\.video\.rivulet (\d+)\]')
_PO_MSGCTXT_RE = re.compile(r'^msgctxt\s+"#(\d+)"', re.MULTILINE)


def _addon_string_ids(path):
    return [int(m.group(1)) for m in _ADDON_STRING_RE.finditer(_read(path))]


def _po_ids(path):
    return {int(m.group(1)) for m in _PO_MSGCTXT_RE.finditer(_read(path))}


def _has_localize(path):
    return '$LOCALIZE[' in _read(path)


def test_skin_addon_string_ids_exist_in_english_catalog():
    """A `$ADDON[plugin.video.rivulet <id>]` pointing at an id nobody added
    to `strings.po` has nothing to substitute and renders blank."""
    valid_ids = _po_ids(_ENGLISH_PO)
    offenders = []
    total = 0
    for path in _skin_files():
        for string_id in _addon_string_ids(path):
            total += 1
            if string_id not in valid_ids:
                offenders.append('%s: $ADDON[plugin.video.rivulet %d] has no #%d entry in %s' % (path, string_id, string_id, _ENGLISH_PO))
    assert total, 'no $ADDON[...] substitutions found across %s - the check is not scanning anything' % _SKIN_DIR
    assert not offenders, 'orphaned skin addon-string ids:\n  ' + '\n  '.join(offenders)


def test_skin_never_uses_localize_substitution():
    """`$LOCALIZE` reads KODI's own string catalog, not this addon's - the
    id namespaces do not overlap, so it always renders blank in an addon
    skin, confirmed as a real failure mode by probe."""
    offenders = [path for path in _skin_files() if _has_localize(path)]
    assert not offenders, 'skin files using $LOCALIZE (reads Kodi\'s catalog, not the addon\'s):\n  ' + '\n  '.join(offenders)


def test_unknown_addon_string_id_is_rejected_regression(tmp_path):
    """Regression: an id absent from strings.po must be flagged."""
    bad = tmp_path / 'Bad.xml'
    bad.write_text('<window><controls><control type="label"><label>$ADDON[plugin.video.rivulet 99999]</label></control></controls></window>', encoding='utf-8')
    assert _addon_string_ids(str(bad)) == [99999]
    assert 99999 not in _po_ids(_ENGLISH_PO)


def test_localize_substitution_is_rejected_regression(tmp_path):
    """Regression: any `$LOCALIZE[...]` in a skin file must be detected."""
    bad = tmp_path / 'Bad.xml'
    bad.write_text('<window><controls><control type="label"><label>$LOCALIZE[31000]</label></control></controls></window>', encoding='utf-8')
    assert _has_localize(str(bad))


#: `[B]`/`[COLOR xxxxxxxx]`/... BBCode and `$INFO[...]`/`$ADDON[...]`/
#: `$LOCALIZE[...]`/`$VAR[...]`/`$ESCINFO[...]` runtime substitutions,
#: stripped before inspecting the literal text a `<label>` actually ships.
_SUBSTITUTION_RE = re.compile(r'\$(?:INFO|ADDON|LOCALIZE|VAR|ESCINFO)\[[^\]]*\]')
_BBCODE_RE = re.compile(r'\[/?[A-Za-z][^\]]*\]')
_WORD_RE = re.compile(r'[A-Za-z]+')

#: Literal English words this skin may ship un-substituted. `RIVULET` is
#: the brand wordmark, never translated in any locale. `OK`/`BACK` label
#: the remote-control keys themselves (as in "[OK] select  [BACK] back"),
#: not sentence content - the action verb beside each is already routed
#: through `$ADDON[...]` (ids 30209/30210/30223/30224/30225/...); the key
#: caption is left as-is the way a keyboard's own key legends are.
_ALLOWED_LITERAL_WORDS = frozenset({'RIVULET', 'OK', 'BACK'})


def _literal_words(label_text):
    """Words a `<label>` renders as static text, after stripping BBCode and
    runtime substitutions - anything left here is untranslated English."""
    stripped = _BBCODE_RE.sub('', _SUBSTITUTION_RE.sub('', label_text))
    return _WORD_RE.findall(stripped)


def _label_texts(path):
    return [el.text for el in ET.parse(path).iter('label') if el.text and el.text.strip()]


def test_skin_labels_have_no_untranslated_english_literal():
    """The `Sources`/breadcrumb bug: a hardcoded English word in a
    14-language addon ships untranslated for every locale but English."""
    offenders = []
    total = 0
    for path in _skin_files():
        for text in _label_texts(path):
            total += 1
            bad_words = sorted({w for w in _literal_words(text) if w not in _ALLOWED_LITERAL_WORDS})
            if bad_words:
                offenders.append('%s: <label>%s</label> has untranslated English %s - route it through $ADDON[...] or extend _ALLOWED_LITERAL_WORDS with a reason'
                                  % (path, text, bad_words))
    assert total, 'no non-empty <label> text found across %s - the check is not scanning anything' % _SKIN_DIR
    assert not offenders, 'untranslated skin label literals:\n  ' + '\n  '.join(offenders)


def test_bare_english_label_is_rejected_regression(tmp_path):
    """Regression: a hardcoded English word outside the allow-list (the
    `Sources` bug) must be flagged."""
    words = _literal_words('Sources')
    assert words == ['Sources']
    assert 'Sources' not in _ALLOWED_LITERAL_WORDS


def test_allowed_literal_words_survive_substitution_stripping_regression(tmp_path):
    """Regression: the allow-listed OK/BACK hint strip - literal key
    captions mixed with a real $ADDON substitution - must NOT be flagged,
    or the rule would be too strict to ship."""
    text = '[COLOR FF38BDF8]OK[/COLOR] $ADDON[plugin.video.rivulet 30209] [COLOR FF38BDF8]BACK[/COLOR] $ADDON[plugin.video.rivulet 30210]'
    assert set(_literal_words(text)) <= _ALLOWED_LITERAL_WORDS


# ---------------------------------------------------------------------------
# 6. layout invariants: itemlayout/focusedlayout row height


def _list_layout_rows(path):
    """`(control_id, own_height_text, item_height, focused_height)` for
    every `list`/`fixedlist` control that declares both an `itemlayout`
    and a `focusedlayout` with a `height` attribute."""
    rows = []
    for control in ET.parse(path).iter('control'):
        if control.get('type') not in ('list', 'fixedlist'):
            continue
        item_layout = control.find('itemlayout')
        focused_layout = control.find('focusedlayout')
        if item_layout is None or focused_layout is None:
            continue
        item_height = item_layout.get('height')
        focused_height = focused_layout.get('height')
        if item_height is None or focused_height is None:
            continue
        own_height_el = control.find('height')
        own_height = own_height_el.text.strip() if own_height_el is not None and own_height_el.text else None
        rows.append((control.get('id'), own_height, item_height, focused_height))
    return rows


def test_skin_list_focused_layout_matches_and_divides_row_height():
    """Two invariants a `<list>`/`<fixedlist>` must hold: `itemlayout` and
    `focusedlayout` must agree on row height (else the focused row jumps
    size), and the control's own height must be a whole multiple of that
    row height (else the last row clips - the `DetailWindow.xml` bug)."""
    offenders = []
    total = 0
    for path in _skin_files():
        for control_id, own_height, item_height, focused_height in _list_layout_rows(path):
            total += 1
            if item_height != focused_height:
                offenders.append('%s: control %s itemlayout height=%s != focusedlayout height=%s'
                                  % (path, control_id, item_height, focused_height))
                continue
            if own_height is None:
                continue
            row_height = int(item_height)
            if row_height and int(own_height) % row_height != 0:
                offenders.append('%s: control %s height=%s is not a whole multiple of its %spx row - the last row clips'
                                  % (path, control_id, own_height, row_height))
    assert total, 'no list/fixedlist with both itemlayout and focusedlayout found across %s - the check is not scanning anything' % _SKIN_DIR
    assert not offenders, 'skin list layout invariant violations:\n  ' + '\n  '.join(offenders)


def test_mismatched_focused_layout_height_is_rejected_regression(tmp_path):
    """Regression: itemlayout/focusedlayout disagreeing on row height must
    be flagged - the focused row would jump size on selection."""
    bad = tmp_path / 'Bad.xml'
    bad.write_text(
        '<window><controls>'
        '<control type="list" id="1"><height>600</height>'
        '<itemlayout height="100"></itemlayout>'
        '<focusedlayout height="120"></focusedlayout>'
        '</control></controls></window>',
        encoding='utf-8',
    )
    rows = _list_layout_rows(str(bad))
    assert rows == [('1', '600', '100', '120')]


def test_fractional_last_row_height_is_rejected_regression(tmp_path):
    """Regression: the `DetailWindow.xml` clipped-row bug - a list height
    that is not a whole multiple of its row height truncates the last
    visible row."""
    bad = tmp_path / 'Bad.xml'
    bad.write_text(
        '<window><controls>'
        '<control type="list" id="1"><height>1290</height>'
        '<itemlayout height="132"></itemlayout>'
        '<focusedlayout height="132"></focusedlayout>'
        '</control></controls></window>',
        encoding='utf-8',
    )
    ((_control_id, own_height, item_height, _focused_height),) = _list_layout_rows(str(bad))
    assert int(own_height) % int(item_height) != 0
