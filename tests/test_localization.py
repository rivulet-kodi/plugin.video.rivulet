"""Stdlib-only validation for `resources/language/*/strings.po` catalogs and
for production references to those catalogs' numeric string ids.

English (`resource.language.en_gb`) is the source of truth: every catalog's
ids must be numeric, unique, and known to English (translated catalogs may
*omit* ids - Kodi falls back to English at runtime - but must never invent
one), and every statically-resolvable `L(<int>)`, `_lfmt(<int>, ...)`
(`lib.ui.player`'s `L(...) % args` wrapper), and
`<addon>.getLocalizedString(<int>)` call site in `lib/`, `default.py`, and
`service.py` must reference an id English actually defines.

"Statically-resolvable" covers: an int literal argument; a same-module
top-level `NAME = <int>` constant; a same-module top-level dict literal
subscripted by a runtime key (`L(_DICT[key])` - every value `_DICT` could
produce is checked, since the key isn't known statically); and a
`for a, b in _TUPLE:` loop variable bound from a same-module top-level
tuple/list of tuples (`_TUPLE`'s first element per row is checked).
Anything else (an argument traced through another module, a bare function
parameter with no enclosing literal/loop/constant, etc.) is silently
skipped - a gap in this scan's reach, not in the underlying ids' validity.
"""
import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
LANGUAGE_DIR = REPO_ROOT / "resources" / "language"
ENGLISH_PO = LANGUAGE_DIR / "resource.language.en_gb" / "strings.po"

PRODUCTION_GLOBS = ("lib/**/*.py", "default.py", "service.py")


# --- strings.po parsing ------------------------------------------------------
#
# Kodi catalogs are flat: comments (`#...`), blank lines, one header
# paragraph (plain `msgid ""` / `msgstr ""` holding continuation-line
# metadata, no `msgctxt`), then one `msgctxt "#<digits>"` / `msgid "..."` /
# `msgstr "..."` triple per localized string, each on its own single line.
# No dependency (e.g. polib) provides this for free within the stdlib.

_MSGCTXT_RE = re.compile(r'^msgctxt\s+"(?P<value>.*)"\s*$')
_MSGID_RE = re.compile(r'^msgid\s+"(?P<value>(?:[^"\\]|\\.)*)"\s*$')
_MSGSTR_RE = re.compile(r'^msgstr\s+"(?P<value>(?:[^"\\]|\\.)*)"\s*$')
_NUMERIC_CTXT_RE = re.compile(r"^#(\d+)$")
_ESCAPE_RE = re.compile(r"\\(.)")
_ESCAPES = {'"': '"', "\\": "\\", "n": "\n", "t": "\t"}


class PoError(ValueError):
    """Raised for a structurally invalid Kodi `strings.po` catalog."""


def _unescape(value):
    return _ESCAPE_RE.sub(lambda m: _ESCAPES.get(m.group(1), m.group(1)), value)


def parse_po(text):
    """Parse a Kodi `strings.po` catalog's numeric-id entries.

    Returns `{int string_id: str msgid}`. Raises `PoError` for a `msgctxt`
    that isn't `#<digits>`, one not immediately followed by `msgid` then
    `msgstr`, or a duplicate id within the same catalog.
    """
    entries = {}
    lines = text.splitlines()
    i, n = 0, len(lines)
    while i < n:
        line = lines[i].strip()
        if not line or line.startswith("#"):
            i += 1
            continue
        ctxt_match = _MSGCTXT_RE.match(line)
        if ctxt_match is None:
            if _MSGID_RE.match(line) is not None:
                # Header paragraph: msgid ""/msgstr "" with continuation
                # metadata lines and no msgctxt. Skip to the next blank line.
                i += 1
                while i < n and lines[i].strip():
                    i += 1
                continue
            raise PoError("line %d: expected msgctxt or header msgid, got %r" % (i + 1, lines[i]))
        ctxt_num_match = _NUMERIC_CTXT_RE.match(ctxt_match.group("value"))
        if ctxt_num_match is None:
            raise PoError("line %d: msgctxt %r is not '#<digits>'" % (i + 1, ctxt_match.group("value")))
        string_id = int(ctxt_num_match.group(1))
        if i + 2 >= n:
            raise PoError("line %d: msgctxt #%d has no msgid/msgstr pair following it" % (i + 1, string_id))
        msgid_match = _MSGID_RE.match(lines[i + 1].strip())
        msgstr_match = _MSGSTR_RE.match(lines[i + 2].strip())
        if msgid_match is None or msgstr_match is None:
            raise PoError("line %d: msgctxt #%d not immediately followed by msgid then msgstr" % (i + 1, string_id))
        if string_id in entries:
            raise PoError("line %d: duplicate msgctxt id #%d" % (i + 1, string_id))
        entries[string_id] = _unescape(msgid_match.group("value"))
        i += 3
    return entries


def _catalog_paths():
    return sorted(LANGUAGE_DIR.glob("*/strings.po"))


def _english_ids():
    return parse_po(ENGLISH_PO.read_text(encoding="utf-8"))


# --- catalog tests ------------------------------------------------------


@pytest.mark.parametrize("po_path", _catalog_paths(), ids=lambda p: p.parent.name)
def test_catalog_parses_and_has_numeric_unique_ids(po_path):
    entries = parse_po(po_path.read_text(encoding="utf-8"))
    assert entries, "%s defines no localized strings" % po_path
    assert all(isinstance(sid, int) for sid in entries)
    assert all(msgid for msgid in entries.values()), "%s has an entry with an empty msgid" % po_path


def test_translated_catalog_ids_are_known_to_english():
    english_ids = set(_english_ids())
    assert english_ids, "English catalog defines no ids"
    for po_path in _catalog_paths():
        if po_path == ENGLISH_PO:
            continue
        catalog_ids = set(parse_po(po_path.read_text(encoding="utf-8")))
        unknown = sorted(catalog_ids - english_ids)
        assert not unknown, "%s has ids unknown to English: %s" % (po_path, unknown)


# --- regression fixtures: prove the .po validator actually catches bad data -


def test_parse_po_rejects_duplicate_msgctxt():
    text = 'msgctxt "#30000"\nmsgid "Discover"\nmsgstr ""\n' * 2
    with pytest.raises(PoError, match="duplicate"):
        parse_po(text)


def test_parse_po_rejects_non_numeric_msgctxt():
    text = 'msgctxt "greeting"\nmsgid "Hi"\nmsgstr ""\n'
    with pytest.raises(PoError, match="digits"):
        parse_po(text)


def test_parse_po_rejects_entry_missing_msgstr():
    text = 'msgctxt "#30000"\nmsgid "Discover"\n'
    with pytest.raises(PoError):
        parse_po(text)


def test_unknown_translated_id_is_rejected_against_english_fixture():
    """Regression: a translated catalog with an id absent from English must
    fail the same set-difference check `test_translated_catalog_ids_are_known_to_english`
    runs against the real catalogs."""
    english_ids = {30000: "Discover"}
    translated = parse_po('msgctxt "#99999"\nmsgid "Ghost"\nmsgstr "Fantasma"\n')
    unknown = set(translated) - set(english_ids)
    assert unknown == {99999}


# --- production L()/getLocalizedString() reference scan ---------------------


def _production_files():
    files = []
    for pattern in PRODUCTION_GLOBS:
        files.extend(REPO_ROOT.glob(pattern))
    return sorted(set(files))


def _module_level_int_constants(tree):
    """`{name: value}` for every top-level `NAME = <int literal>` assignment."""
    consts = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            value = node.value
            if isinstance(value, ast.Constant) and isinstance(value.value, int) and not isinstance(value.value, bool):
                consts[node.targets[0].id] = value.value
    return consts


def _module_level_containers(tree):
    """`{name: ast node}` for every top-level `NAME = <dict/tuple/list literal>`
    assignment (e.g. `homewindow._SUBTITLES`/`homewindow._MENU`)."""
    containers = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            if isinstance(node.value, (ast.Dict, ast.Tuple, ast.List)):
                containers[node.targets[0].id] = node.value
    return containers


def _int_constant(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, int) and not isinstance(node.value, bool):
        return node.value
    return None


def _dict_int_values(dict_node):
    """Every int-literal value in a `Dict` literal (e.g. `_SUBTITLES`'s
    action->string_id map) - the runtime key isn't known statically, so
    every value the dict could produce is checked."""
    return [v for v in (_int_constant(v) for v in dict_node.values) if v is not None]


def _tuple_row_ids(container_node):
    """First element of each row in a tuple/list of tuples/lists (e.g.
    `_MENU`'s (string_id, action) rows), when that element is an int
    literal."""
    ids = []
    for elt in getattr(container_node, "elts", []):
        if isinstance(elt, (ast.Tuple, ast.List)) and elt.elts:
            row_id = _int_constant(elt.elts[0])
            if row_id is not None:
                ids.append(row_id)
    return ids


def _for_loop_row_containers(tree, containers):
    """`{loop_var_name: container_node}` for every `for a, ... in NAME:` loop
    whose `NAME` is a module-level tuple/list-of-tuples container - binds
    `a` back to that container so `L(a)` inside the loop body resolves
    (e.g. `homewindow._menu_items`'s `for string_id, action in _MENU:`)."""
    bindings = {}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.For)
            and isinstance(node.target, (ast.Tuple, ast.List))
            and node.target.elts
            and isinstance(node.target.elts[0], ast.Name)
            and isinstance(node.iter, ast.Name)
            and node.iter.id in containers
        ):
            bindings[node.target.elts[0].id] = containers[node.iter.id]
    return bindings


def _resolve_int_args(node, consts, containers, loop_containers):
    """Every id `node` could statically evaluate to (see module docstring's
    "statically-resolvable" definition). Usually zero or one; a
    dict-subscript or loop-tuple argument yields every value its container
    could produce."""
    literal = _int_constant(node)
    if literal is not None:
        return [literal]
    if isinstance(node, ast.Name):
        if node.id in consts:
            return [consts[node.id]]
        if node.id in loop_containers:
            return _tuple_row_ids(loop_containers[node.id])
        return []
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id in containers:
        container = containers[node.value.id]
        if isinstance(container, ast.Dict):
            return _dict_int_values(container)
    return []


def iter_l_call_ids(tree):
    """Yield `(lineno, string_id)` for every statically-resolvable `L(<int>)`,
    `_lfmt(<int>, ...)` (`lib.ui.player`'s `L(...) % args` wrapper), or
    `<expr>.getLocalizedString(<int>)` call in `tree` (see module docstring
    for exactly what "statically-resolvable" covers)."""
    consts = _module_level_int_constants(tree)
    containers = _module_level_containers(tree)
    loop_containers = _for_loop_row_containers(tree, containers)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in ("L", "_lfmt"):
            if not node.args:
                continue
            arg = node.args[0]
        elif isinstance(func, ast.Attribute) and func.attr == "getLocalizedString":
            if len(node.args) != 1 or node.keywords:
                continue
            arg = node.args[0]
        else:
            continue
        for string_id in _resolve_int_args(arg, consts, containers, loop_containers):
            yield node.lineno, string_id


def test_production_localized_string_ids_exist_in_english():
    english_ids = set(_english_ids())
    missing = []
    resolved_count = 0
    for path in _production_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for lineno, string_id in iter_l_call_ids(tree):
            resolved_count += 1
            if string_id not in english_ids:
                missing.append("%s:%d -> %d" % (path.relative_to(REPO_ROOT), lineno, string_id))
    # Guards against a broken scan (e.g. a typo'd AST pattern) silently
    # matching nothing and passing vacuously.
    assert resolved_count > 60, "expected many statically-resolvable localized-string call sites, found %d" % resolved_count
    assert not missing, "production code references ids missing from English:\n" + "\n".join(missing)


def test_l_call_scan_regression_fixture_flags_id_missing_from_english():
    """Regression: production code referencing an id absent from English
    must be flagged by `iter_l_call_ids` + the English id set, exactly as
    `test_production_localized_string_ids_exist_in_english` asserts."""
    tree = ast.parse("from lib.ui.compat import L\n\n\ndef f():\n    return L(999999)\n")
    ids = [string_id for _lineno, string_id in iter_l_call_ids(tree)]
    assert ids == [999999]
    assert not set(ids) <= set(_english_ids())


def test_l_call_scan_resolves_module_level_constant_argument():
    """Regression: an `L(_SOME_ID)` call where `_SOME_ID` is a top-level int
    constant (as `lib.ui.streamswindow` does for its binge-countdown dialog
    strings) must resolve to that constant's value, not be skipped."""
    tree = ast.parse("_ID = 30182\n\n\ndef f():\n    return L(_ID)\n")
    ids = [string_id for _lineno, string_id in iter_l_call_ids(tree)]
    assert ids == [30182]


def test_l_call_scan_recognizes_lfmt_wrapper():
    """Regression: `_lfmt(<int>, ...)` (`lib.ui.player`'s `L(...) % args`
    wrapper) is a localized-string sink too, and an unknown id passed
    through it must be flagged."""
    tree = ast.parse("from lib.ui.player import _lfmt\n\n\ndef f():\n    return _lfmt(999999, 'x')\n")
    ids = [string_id for _lineno, string_id in iter_l_call_ids(tree)]
    assert ids == [999999]
    assert not set(ids) <= set(_english_ids())


def test_l_call_scan_resolves_dict_subscript_argument():
    """Regression: `L(_DICT[key])` where `_DICT` is a top-level dict literal
    (as `lib.ui.homewindow._SUBTITLES` is) must resolve to every value
    `_DICT` could produce, since `key` isn't known statically."""
    tree = ast.parse("_D = {'a': 30148, 'b': 30149}\n\n\ndef f(key):\n    return L(_D[key])\n")
    ids = sorted(string_id for _lineno, string_id in iter_l_call_ids(tree))
    assert ids == [30148, 30149]


def test_l_call_scan_resolves_for_loop_tuple_target():
    """Regression: `for string_id, action in _MENU: ... L(string_id)` (as
    `lib.ui.homewindow._menu_items` does) must resolve `string_id` to every
    first element `_MENU`'s rows hold."""
    tree = ast.parse(
        "_MENU = (\n    (30000, 'discover'),\n    (30001, 'search'),\n)\n\n\n"
        "def f():\n    for string_id, action in _MENU:\n        L(string_id)\n"
    )
    ids = sorted(string_id for _lineno, string_id in iter_l_call_ids(tree))
    assert ids == [30000, 30001]
