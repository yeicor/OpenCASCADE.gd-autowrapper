"""Extract enum declarations from class and translation unit cursors."""

from __future__ import annotations

from clang.cindex import Cursor, CursorKind, AccessSpecifier

from model import EnumDecl, EnumValue
from occast.docs import extract_doc


def extract_nested_enums(cursor: Cursor) -> list[EnumDecl]:
    """Extract enum declarations nested inside a class."""
    enums = []
    for child in cursor.get_children():
        if child.kind == CursorKind.ENUM_DECL:
            # Skip private/protected enums
            try:
                if child.access_specifier not in (AccessSpecifier.PUBLIC, AccessSpecifier.INVALID):
                    continue
            except Exception:
                pass
            # Skip unnamed enums — they produce invalid C++ names
            name = child.spelling
            if not name or name.startswith("(unnamed"):
                continue
            enum = _extract_enum(child, is_nested=True, parent_class=cursor.spelling)
            if enum:
                enums.append(enum)
    return enums


def extract_tu_enums(cursor: Cursor) -> list[EnumDecl]:
    """Extract top-level enum declarations from a translation unit."""
    enums = []
    for child in cursor.get_children():
        if child.kind == CursorKind.ENUM_DECL:
            # Skip nested enums (they're part of a class)
            if not _is_nested_in_class(child):
                enum = _extract_enum(child, is_nested=False, parent_class="")
                if enum:
                    enums.append(enum)
    return enums


def _extract_enum(cursor: Cursor, is_nested: bool, parent_class: str) -> EnumDecl | None:
    """Extract a single enum declaration."""
    name = cursor.spelling
    if not name:
        return None

    is_scoped = cursor.is_scoped() if hasattr(cursor, 'is_scoped') else False
    doc = extract_doc(cursor)

    values = []
    for child in cursor.get_children():
        if child.kind == CursorKind.ENUM_CONSTANT_DECL:
            val = EnumValue(
                name=child.spelling,
                value=child.enum_value,
            )
            values.append(val)

    if not values:
        return None

    # Get header file location
    header_file = ""
    if cursor.location and cursor.location.file:
        header_file = str(cursor.location.file)

    return EnumDecl(
        name=name,
        values=values,
        is_scoped=is_scoped,
        is_nested=is_nested,
        parent_class=parent_class,
        header_file=header_file,
        doc=doc,
    )


def _is_nested_in_class(cursor: Cursor) -> bool:
    """Check if an enum is nested inside a class."""
    parent = cursor.semantic_parent
    if parent and parent.kind in (CursorKind.CLASS_DECL, CursorKind.CLASS_TEMPLATE):
        return True
    return False
