"""Extract field declarations from a class cursor.

libclang's `access_specifier` is unreliable for OCCT fields (e.g. it reports
some `private:` members of `Bnd_Box2d` as public, while sibling fields are
correctly private).  We therefore determine access from the header source text:
scan the class body tracking brace nesting and the last `public:`/`private:`
access label, and trust libclang only when no label is visible in the body.
"""

from __future__ import annotations

import re as _re

from clang.cindex import AccessSpecifier, Cursor, CursorKind

from model import FieldDecl
from occast.type_utils import make_occt_type
from occast.docs import extract_doc

_ACCESS_LABELS = ("public", "protected", "private")


def _read_header(file_name: str) -> str:
    try:
        with open(file_name, "r", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def _class_body_access(source: str, start: int, end: int) -> dict[int, str] | None:
    """Map field declaration line numbers to their effective access label.

    Scans the class body between byte offsets [start, end), tracking brace
    depth.  Access labels (`public:`/`protected:`/`private:`) only apply at
    depth 1 (directly in the class body); labels inside nested structs, method
    bodies, comments and strings are ignored.  Returns {line: "public"|"protected"
    |"private"} for every line where the access changes, or None if the region
    can't be parsed.
    """
    open_brace = source.find("{", start, end)
    if open_brace < 0:
        return None
    access: str | None = None
    changes: dict[int, str] = {}
    depth = 0
    i = open_brace
    n = len(source)
    line = source.count("\n", 0, open_brace) + 1  # absolute 1-based line
    while i < n and i <= end:
        ch = source[i]
        if ch == "\n":
            line += 1
            i += 1
            continue
        if source.startswith("//", i):
            nl = source.find("\n", i, end)
            if nl < 0:
                break
            line += 1
            i = nl + 1
            continue
        if source.startswith("/*", i):
            close = source.find("*/", i + 2, end)
            if close < 0:
                break
            line += source.count("\n", i, close + 2)
            i = close + 2
            continue
        if source.startswith('"', i) or source.startswith("'", i):
            quote = source[i]
            j = i + 1
            while j < n and j <= end:
                if source[j] == "\\":
                    j += 2
                    continue
                if source[j] == quote:
                    break
                if source[j] == "\n":
                    line += 1
                j += 1
            line += source.count("\n", i, j + 1)
            i = j + 1
            continue
        if ch == "{":
            depth += 1
            i += 1
            continue
        if ch == "}":
            depth -= 1
            i += 1
            continue
        # Access label at class-body depth?
        if depth == 1:
            matched = None
            for label in _ACCESS_LABELS:
                if source.startswith(label + ":", i) and (
                    i == 0 or not (source[i - 1].isalnum() or source[i - 1] == "_")
                ):
                    matched = label
                    break
            if matched is not None:
                access = matched
                changes[line] = matched
                i += len(matched) + 1
                continue
        i += 1
    return changes


def _field_is_public(field: Cursor, changes: dict[int, str] | None) -> bool | None:
    """True/False when the source text is authoritative, None otherwise."""
    if changes is None:
        return None
    try:
        fline = field.location.line
    except Exception:
        return None
    # Effective access is the last recorded change at or before this line.
    best = None
    best_line = -1
    for line, label in changes.items():
        if line <= fline and line > best_line:
            best = label
            best_line = line
    if best is None:
        return None
    return best == "public"


_BUILTIN_BASES = {
    "bool", "char", "unsigned char", "signed char", "short", "unsigned short",
    "int", "unsigned int", "long", "unsigned long", "long long",
    "unsigned long long", "int16_t", "uint16_t", "int32_t", "uint32_t",
    "int64_t", "uint64_t", "float", "double", "long double", "void",
}


def _field_decl_text(source: str, field: Cursor) -> str:
    """Raw source text of the field declaration's type portion."""
    try:
        off = field.location.offset
    except Exception:
        return ""
    if off <= 0 or off > len(source):
        return ""
    # Walk back from the field name to the previous ';', '{' or '}'.
    start = off
    while start > 0:
        start -= 1
        c = source[start]
        if c in ";{}":
            start += 1
            break
    return source[start:off]


def _field_type_corrupt(source: str, field: Cursor, base_name: str) -> bool:
    """True when libclang mis-resolved the field type.

    libclang sometimes reports `occ::handle<T>` fields as plain builtins
    (e.g. `int` for `occ::handle<Graphic3d_TransformPers>`).  If the source
    declaration is a template/handle type but the resolved base is a scalar,
    the field cannot be exposed safely — skip it.
    """
    if base_name not in _BUILTIN_BASES:
        return False
    text = _field_decl_text(source, field)
    text = _re.sub(r"/\*.*?\*/", "", text, flags=_re.S)
    text = _re.sub(r"//.*", "", text)
    return "<" in text or "handle" in text.lower()


def extract_fields(cursor: Cursor, known_transient: set[str]) -> list[FieldDecl]:
    """Extract all field declarations from a class cursor."""
    changes: dict[int, str] | None = None
    source = ""
    try:
        file_name = cursor.location.file.name
        extent = cursor.extent
        source = _read_header(file_name)
        if source:
            changes = _class_body_access(
                source, extent.start.offset, extent.end.offset
            )
    except Exception:
        changes = None

    fields = []
    for child in cursor.get_children():
        if child.kind == CursorKind.FIELD_DECL:
            ftype = make_occt_type(child.type, known_transient)
            doc = extract_doc(child)
            src_public = _field_is_public(child, changes)
            if src_public is not None:
                is_public = src_public
            else:
                is_public = child.access_specifier == AccessSpecifier.PUBLIC
            if _field_type_corrupt(source, child, ftype.base_name):
                is_public = False
            fields.append(FieldDecl(
                name=child.spelling,
                type=ftype,
                doc=doc,
                is_public=is_public,
            ))
    return fields
