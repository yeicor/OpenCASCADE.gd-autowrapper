"""Extract field declarations from a class cursor."""

from __future__ import annotations

from clang.cindex import Cursor, CursorKind

from model import FieldDecl
from occast.type_utils import make_occt_type
from occast.docs import extract_doc


def extract_fields(cursor: Cursor, known_transient: set[str]) -> list[FieldDecl]:
    """Extract all field declarations from a class cursor."""
    fields = []
    for child in cursor.get_children():
        if child.kind == CursorKind.FIELD_DECL:
            ftype = make_occt_type(child.type, known_transient)
            doc = extract_doc(child)
            fields.append(FieldDecl(
                name=child.spelling,
                type=ftype,
                doc=doc,
            ))
    return fields
