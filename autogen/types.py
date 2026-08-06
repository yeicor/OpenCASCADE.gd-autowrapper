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

_HANDLE_PREFIX_RE = re.compile(r"^(?:(?:occ|opencascade)::handle)\s*<")


def _handle_inner(s: str) -> str | None:
    """Return the inner type of a ``occ::handle<...>`` spelling, or None.

    Handles may wrap template specializations (``occ::handle<HArray1<double>>``
    or nested ``occ::handle<HArray2<occ::handle<Geom_Surface>>>``), so the
    closing ``>`` is found by angle-depth counting rather than a flat regex.
    """
    if _HANDLE_PREFIX_RE.match(s) is None:
        return None
    start = _HANDLE_PREFIX_RE.match(s).end()
    depth = 0
    for i in range(start, len(s)):
        ch = s[i]
        if ch == "<":
            depth += 1
        elif ch == ">":
            if depth == 0:
                inner = s[start:i].strip()
                return inner or None
            depth -= 1
    return None

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
    """Build an OCCTType using ONLY canonical libclang types.

    Every typedef is collapsed through ``get_canonical()`` so that OCCT's
    pervasive aliases (`Standard_Real`, `Standard_CString`, `occ::handle<T>`,
    `Standard_OStream`, ...) are indistinguishable from their underlying
    builtin / class / template types.  The generator therefore never has to
    know OCCT's alias names: a `Standard_Integer` parameter *is* an `int`.
    """
    T = cursor_type
    try:
        canonical = T.get_canonical()
    except Exception:
        canonical = T

    spelling = T.spelling                      # as written in the header
    can_spelling = canonical.spelling

    is_ref = canonical.kind in (TypeKind.LVALUEREFERENCE, TypeKind.RVALUEREFERENCE)
    is_rvalue_ref = canonical.kind == TypeKind.RVALUEREFERENCE
    is_pointer = canonical.kind == TypeKind.POINTER
    if is_ref or is_pointer:
        try:
            pointee = canonical.get_pointee()
        except Exception:
            pointee = canonical
    else:
        pointee = canonical

    try:
        pointee_is_const = bool(pointee.is_const_qualified())
    except Exception:
        pointee_is_const = "const" in can_spelling

    core = _strip_qualifiers(pointee.spelling)
    if is_pointer:
        core = core.rstrip("*").rstrip()
    if is_ref:
        core = core.rstrip("&").rstrip()

    # Handle: top-level canonical spelling `occ::handle<T>` / `opencascade::handle<T>`.
    inner = _handle_inner(core)
    is_handle = inner is not None
    handle_inner = inner if inner else ""

    if is_handle:
        base_name = handle_inner
        targs: list[str] = []
    else:
        base_name = core
        targs = _template_args(pointee)

    # Enum detection from the canonical kind of the (pointee) type.
    is_enum = False
    try:
        is_enum = pointee.kind == TypeKind.ENUM
    except Exception:
        pass

    return OCCTType(
        spelling=spelling,
        base_name=base_name,
        canonical_spelling=can_spelling,
        is_const=pointee_is_const if (is_ref or is_pointer) else "const" in can_spelling,
        is_ref=is_ref,
        is_rvalue_ref=is_rvalue_ref,
        is_pointer=is_pointer,
        is_handle=is_handle,
        handle_inner=handle_inner,
        is_transient_descendant=is_handle,  # handles always wrap transient objects
        pointee_is_const=pointee_is_const,
        is_enum=is_enum,
        template_args=targs,
    )
