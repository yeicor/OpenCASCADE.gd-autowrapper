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


def is_failure_descendant(cursor: Cursor) -> bool:
    """Check if a class inherits from Standard_Failure anywhere in its hierarchy.

    Standard_Failure is the root of OCCT's exception hierarchy. In system OCCT it
    inherits from Standard_Transient, but in vcpkg it inherits from std::exception.
    Exception classes therefore can't be wrapped as REF_COUNTED (handle<T> doesn't
    compile), and they're not useful from GDScript anyway.
    """
    for base in get_all_bases(cursor):
        if base.spelling == "Standard_Failure":
            return True
    return False


def is_handle_type(type_spelling: str) -> bool:
    """Check if a type is opencascade::handle<T>.

    Only matches when the type itself is a handle, not when handles
    appear as template arguments of another type.
    """
    s = type_spelling.strip()
    # Strip leading qualifiers to reach the core type name
    for _ in range(4):
        s = s.removeprefix("const ").removeprefix("volatile ").strip()
    if s.startswith("opencascade::handle<") or s.startswith("Handle("):
        return True
    return False


def extract_handle_inner(type_spelling: str) -> str:
    """Extract T from opencascade::handle<T> or Handle(T)."""
    for prefix in ("opencascade::handle<", "Handle("):
        if prefix in type_spelling:
            start = type_spelling.index(prefix) + len(prefix)
            end = type_spelling.rindex(">")
            return type_spelling[start:end]
    return ""


def make_occt_type(cursor_type: Type, known_transient: set[str] | None = None) -> OCCTType:
    """Build an OCCTType from a libclang Type object using the Type API directly."""
    from clang.cindex import TypeKind

    T = cursor_type
    spelling = T.spelling
    canonical = T.get_canonical()

    # Use Type.kind to determine qualifiers (like pywrap does).
    # We start with the outermost type and peel layers:
    #   LVALUEREFERENCE → &  (ref)
    #   RVALUEREFERENCE → && (ref)
    #   POINTER         → *  (pointer)
    # Then check const on the pointee.
    is_ref = T.kind in (TypeKind.LVALUEREFERENCE, TypeKind.RVALUEREFERENCE)
    is_pointer = T.kind == TypeKind.POINTER

    if is_ref or is_pointer:
        pointee = T.get_pointee()
        pointee_spelling = pointee.spelling
    else:
        pointee = T
        pointee_spelling = spelling

    is_const = "const" in pointee_spelling or "const" in spelling

    # For typedef aliases to built-in C++ types, use canonical spelling
    _BUILTIN_KINDS = frozenset({
        TypeKind.BOOL,
        TypeKind.CHAR_S, TypeKind.CHAR_U,
        TypeKind.UCHAR, TypeKind.SHORT, TypeKind.USHORT,
        TypeKind.INT, TypeKind.UINT,
        TypeKind.LONG, TypeKind.ULONG,
        TypeKind.LONGLONG, TypeKind.ULONGLONG,
        TypeKind.FLOAT, TypeKind.DOUBLE, TypeKind.LONGDOUBLE,
        TypeKind.WCHAR, TypeKind.CHAR16, TypeKind.CHAR32,
    })
    # Detect handles: first from the original spelling, then from canonical
    # for handle typedefs (e.g. BOPAlgo_PPaveFiller → opencascade::handle<BOPAlgo_PaveFiller>)
    is_handle = is_handle_type(pointee_spelling)
    if not is_handle and T.kind == TypeKind.TYPEDEF:
        # Check if canonical resolves to a handle type
        canonical_is_handle = is_handle_type(canonical.spelling)
        if canonical_is_handle:
            is_handle = True
            # Use canonical spelling for pointee so handle_inner extraction works
            pointee_spelling_orig = pointee_spelling
            pointee_spelling = canonical.spelling

    if T.kind == TypeKind.TYPEDEF and not is_handle:
        if canonical.kind in _BUILTIN_KINDS:
            clean = canonical.spelling.replace("const ", "").strip()
        elif canonical.kind == TypeKind.ENUM:
            clean = canonical.spelling.replace("const ", "").strip()
        elif canonical.kind == TypeKind.RECORD:
            # For typedefs resolving to a class/struct, use canonical base name
            # (e.g. "Point" → "gp_XYZ", "Select3D_BndBox3d" → "BVH_Box<double, 3>")
            clean = canonical.spelling.replace("const ", "").strip()
        else:
            clean = pointee_spelling.replace("const ", "").strip()
    else:
        clean = pointee_spelling.replace("const ", "").strip()

    # Use canonical kind to detect enums (libclang may report just "D" for "gp_Dir::D")
    try:
        is_enum_type = canonical.kind == TypeKind.ENUM
    except Exception:
        is_enum_type = False
    if is_enum_type:
        clean = canonical.spelling.replace("const ", "").strip()

    handle_inner = extract_handle_inner(pointee_spelling) if is_handle else ""

    # For handle types, base_name should be the inner type (not "opencascade::handle<T>")
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
        canonical_spelling=canonical.spelling,
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
