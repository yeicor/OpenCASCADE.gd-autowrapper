"""Type resolution utilities for libclang cursors.

Provides base chain walking, handle detection, and type classification.
"""

from __future__ import annotations

from clang.cindex import Cursor, CursorKind, Type

from model import OCCTType


def get_all_bases(cursor: Cursor) -> list[Cursor]:
    """Walk the full inheritance chain of a class cursor.

    Returns all base class definition cursors (deepest first).
    """
    bases = []
    for child in cursor.get_children():
        if child.kind == CursorKind.CXX_BASE_SPECIFIER:
            base_def = child.get_definition()
            if base_def and base_def.is_definition():
                bases.append(base_def)
                bases.extend(get_all_bases(base_def))
    return bases


def get_direct_bases(cursor: Cursor) -> list[str]:
    """Get direct base class names (spelling from the base specifier)."""
    names = []
    for child in cursor.get_children():
        if child.kind == CursorKind.CXX_BASE_SPECIFIER:
            names.append(child.type.spelling)
    return names


def is_transient_descendant(cursor: Cursor) -> bool:
    """Check if a class inherits from Standard_Transient anywhere in its hierarchy."""
    for base in get_all_bases(cursor):
        if base.spelling == "Standard_Transient":
            return True
    return False


def is_handle_type(type_spelling: str) -> bool:
    """Check if a type is opencascade::handle<T>."""
    return "opencascade::handle<" in type_spelling or "Handle(" in type_spelling


def extract_handle_inner(type_spelling: str) -> str:
    """Extract T from opencascade::handle<T> or Handle(T)."""
    for prefix in ("opencascade::handle<", "Handle("):
        if prefix in type_spelling:
            start = type_spelling.index(prefix) + len(prefix)
            end = type_spelling.rindex(">")
            return type_spelling[start:end]
    return ""


def make_occt_type(cursor_type: Type, known_transient: set[str] | None = None) -> OCCTType:
    """Build an OCCTType from a libclang Type object."""
    spelling = cursor_type.spelling
    canonical = cursor_type.get_canonical()

    # Strip const and ref qualifiers for base name analysis
    clean = spelling.replace("const ", "").replace("&", "").replace("*", "").strip()

    is_const = "const" in spelling
    is_ref = "&" in spelling
    is_pointer = "*" in spelling and "handle<" not in spelling.lower()

    # Use canonical kind to detect enums (libclang may report just "D" for "gp_Dir::D")
    try:
        from clang.cindex import TypeKind
        is_enum_type = canonical.kind == TypeKind.ENUM
    except Exception:
        is_enum_type = False
    if is_enum_type:
        # Use canonical spelling to get the full enum name
        clean = canonical.spelling.replace("const ", "").replace("&", "").replace("*", "").strip()

    is_handle = is_handle_type(spelling)
    handle_inner = extract_handle_inner(spelling) if is_handle else ""

    # For handle types, base_name should be the inner type (not "opencascade::handle<T>")
    # so that _is_enum and wrapper lookups work correctly.
    if is_handle and handle_inner:
        clean = handle_inner

    # Determine if transient descendant
    is_transient = False
    if clean in (known_transient or set()):
        is_transient = True
    elif is_handle:
        is_transient = True  # handles are always transient

    return OCCTType(
        spelling=spelling,
        base_name=clean,
        is_const=is_const,
        is_ref=is_ref,
        is_pointer=is_pointer,
        is_handle=is_handle,
        handle_inner=handle_inner,
        is_transient_descendant=is_transient,
    )


def is_wrapper_type(type_spelling: str, known_wrappers: set[str]) -> bool:
    """Check if a type has a generated wrapper."""
    clean = type_spelling.replace("const ", "").replace("&", "").replace("*", "").strip()
    return clean in known_wrappers
