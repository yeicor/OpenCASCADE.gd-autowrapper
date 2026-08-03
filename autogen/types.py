"""Clean type resolution from libclang Type objects.

Only the Type API is used (kind, pointee, canonical, template arguments).
No source-text inspection.  With a correct `-resource-dir` this is sufficient:
libclang natively reports `occ::handle<T>`, `opencascade::handle<T>`, and nested
collection templates like `NCollection_List<occ::handle<T>>` correctly.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from clang.cindex import TypeKind

from .model import OCCTType

if TYPE_CHECKING:
    from clang.cindex import Type

_HANDLE_RE = re.compile(
    r"^(?:(?:occ|opencascade)::handle)\s*<\s*([A-Za-z_]\w*(?:::[A-Za-z_]\w*)*)\s*>"
)

_BUILTIN_KINDS = frozenset({
    TypeKind.BOOL, TypeKind.CHAR_S, TypeKind.CHAR_U, TypeKind.UCHAR,
    TypeKind.SCHAR, TypeKind.SHORT, TypeKind.USHORT, TypeKind.INT, TypeKind.UINT,
    TypeKind.LONG, TypeKind.ULONG, TypeKind.LONGLONG, TypeKind.ULONGLONG,
    TypeKind.FLOAT, TypeKind.DOUBLE, TypeKind.LONGDOUBLE,
    TypeKind.WCHAR, TypeKind.CHAR16, TypeKind.CHAR32,
    TypeKind.UNEXPOSED,
})


def _strip_qualifiers(s: str) -> str:
    for _ in range(4):
        s = s.removeprefix("const ").removeprefix("volatile ").strip()
    return s


def _template_args(t: "Type") -> list[str]:
    """Top-level template argument spellings, best-effort."""
    try:
        n = t.get_num_template_arguments()
        if n <= 0:
            return []
        out = []
        for i in range(n):
            out.append(t.get_template_argument_type(i).spelling)
        return out
    except Exception:
        pass
    # Fallback: split the outermost <...> by top-level commas.
    m = re.match(r"^[A-Za-z_]\w*(?:::)?[A-Za-z_0-9<>:,\s]*?<(.*)>$", t.spelling, re.S)
    if not m:
        return []
    inner = m.group(1)
    parts: list[str] = []
    depth = 0
    cur = ""
    for ch in inner:
        if ch == "<":
            depth += 1
            cur += ch
        elif ch == ">":
            depth -= 1
            cur += ch
        elif ch == "," and depth == 0:
            parts.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        parts.append(cur.strip())
    return parts


def make_type(cursor_type: "Type") -> OCCTType:
    """Build an OCCTType from a libclang Type using only the Type API."""
    T = cursor_type
    spelling = T.spelling
    try:
        canonical = T.get_canonical()
    except Exception:
        canonical = T

    is_ref = T.kind in (TypeKind.LVALUEREFERENCE, TypeKind.RVALUEREFERENCE)
    is_pointer = T.kind == TypeKind.POINTER

    if is_ref or is_pointer:
        try:
            pointee = T.get_pointee()
        except Exception:
            pointee = T
    else:
        pointee = T

    is_const = "const" in spelling
    try:
        pointee_is_const = pointee.is_const_qualified() if is_pointer else is_const
    except Exception:
        pointee_is_const = is_const

    core = _strip_qualifiers(pointee.spelling if (is_ref or is_pointer) else spelling)
    if is_pointer:
        core = core.rstrip("*").rstrip()
    if is_ref:
        core = core.rstrip("&").rstrip()

    # Top-level handle detection.
    m = _HANDLE_RE.match(core)
    is_handle = m is not None
    handle_inner = m.group(1) if m else ""

    if is_handle:
        base_name = handle_inner
        targs: list[str] = []
    else:
        base_name = core
        targs = _template_args(T if not (is_ref or is_pointer) else pointee)

    # Enum detection from canonical kind.
    is_enum = False
    try:
        is_enum = canonical.kind == TypeKind.ENUM
    except Exception:
        pass

    # Primitive desugaring: libclang's spelling already collapses typedefs of
    # builtins (Standard_Real -> double).  For non-builtin typedefs (e.g. an
    # enum typedef) keep the spelled name.
    if T.kind == TypeKind.TYPEDEF and not is_handle:
        if canonical.kind in _BUILTIN_KINDS:
            base_name = _strip_qualifiers(canonical.spelling)

    return OCCTType(
        spelling=spelling,
        base_name=base_name,
        canonical_spelling=canonical.spelling,
        is_const=is_const,
        is_ref=is_ref,
        is_pointer=is_pointer,
        is_handle=is_handle,
        handle_inner=handle_inner,
        is_transient_descendant=is_handle,  # handles always wrap transient objects
        pointee_is_const=pointee_is_const,
        is_enum=is_enum,
        template_args=targs,
    )
