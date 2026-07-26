"""Extract class declarations from a translation unit."""

from __future__ import annotations

from pathlib import Path

from clang.cindex import Cursor, CursorKind, AccessSpecifier

from model import ClassDecl, ClassKind, OCCTType
from occast.type_utils import get_direct_bases, is_transient_descendant, make_occt_type
from occast.methods import extract_methods
from occast.fields import extract_fields
from occast.enums import extract_nested_enums
from occast.docs import extract_doc


def extract_classes(tu_cursor: Cursor, module_name: str, known_transient: set[str],
                    header_prefix: str = "") -> list[ClassDecl]:
    """Extract class/struct definitions from a translation unit.

    Only processes classes whose definition is in a header matching header_prefix.
    This prevents picking up classes from included headers (e.g. Standard_Transient).
    """
    classes = []
    seen = set()

    for child in tu_cursor.get_children():
        if child.kind == CursorKind.CLASS_DECL:
            if not child.is_definition():
                continue
            if child.spelling in seen:
                continue

            # Filter by header path: only include classes defined in headers
            # that match the module prefix
            if header_prefix and child.location and child.location.file:
                loc_file = str(child.location.file)
                basename = Path(loc_file).name
                if not basename.startswith(header_prefix):
                    continue

            # Also filter by class name prefix (template specializations may appear
            # in module headers but belong to a different module)
            if header_prefix and not child.spelling.startswith(header_prefix.rstrip("_")):
                continue

            seen.add(child.spelling)

            cls = _extract_class(child, module_name, known_transient)
            if cls:
                classes.append(cls)

    return classes


def _extract_class(cursor: Cursor, module_name: str, known_transient: set[str]) -> ClassDecl | None:
    """Extract a single class declaration."""
    name = cursor.spelling
    if not name:
        return None

    # Skip internal implementation classes
    if name.startswith("_"):
        return None

    # Skip deprecated typedef-like classes (HArray1Of*, HArray2Of*, HSequence*, etc.)
    if "HArray" in name or "HSequence" in name:
        return None

    # Check for protected/private destructor or constructors — can't wrap
    has_protected_dtor = False
    has_protected_ctor = False
    has_pure_virtual = False
    has_public_default_ctor = False
    for child in cursor.get_children():
        if child.kind == CursorKind.DESTRUCTOR:
            if child.access_specifier in (AccessSpecifier.PRIVATE, AccessSpecifier.PROTECTED):
                has_protected_dtor = True
        if child.kind == CursorKind.CONSTRUCTOR:
            if child.access_specifier in (AccessSpecifier.PRIVATE, AccessSpecifier.PROTECTED):
                has_protected_ctor = True
            elif child.access_specifier in (AccessSpecifier.PUBLIC, AccessSpecifier.INVALID):
                if len([p for p in child.get_children() if p.kind == CursorKind.PARM_DECL]) == 0:
                    has_public_default_ctor = True
        if child.kind == CursorKind.CXX_METHOD and child.is_pure_virtual_method():
            has_pure_virtual = True

    # Also check base classes for pure virtual methods (not overridden in this class)
    if not has_pure_virtual:
        from occast.type_utils import get_all_bases
        own_virtuals = set()
        for child in cursor.get_children():
            if child.kind == CursorKind.CXX_METHOD:
                own_virtuals.add(child.spelling)
        for base_def in get_all_bases(cursor):
            for child in base_def.get_children():
                if child.kind == CursorKind.CXX_METHOD and child.is_pure_virtual_method():
                    if child.spelling not in own_virtuals:
                        has_pure_virtual = True
                        break
            if has_pure_virtual:
                break

    # Skip classes with protected destructors (can't be subclassed as RefCounted)
    if has_protected_dtor:
        return None

    # Skip classes where all constructors are protected/private (can't instantiate)
    if has_protected_ctor and not has_public_default_ctor:
        return None

    # Skip abstract classes (can't instantiate _native)
    if has_pure_virtual:
        return None

    # Skip abstract classes (can't instantiate _native)
    if has_pure_virtual:
        return None

    # Skip exception-type classes (Standard_Failure descendants) — in vcpkg OCCT,
    # Standard_Failure inherits from std::exception, not Standard_Transient, so
    # handle<> doesn't work for them. The system OCCT headers differ.
    # Check the full base chain for any Standard_* exception/error type.
    direct_bases = get_direct_bases(cursor)
    for base in direct_bases:
        if base.startswith("Standard_") and any(
            kw in base for kw in ("Failure", "Error", "OutOfRange",
                                   "OutOfMemory", "DomainError", "RangeError")
        ):
            return None

    # Skip internal TShape implementation classes (BRep_TVertex, BRep_TFace, etc.)
    # — they use NCollection templates with incomplete TopoDS types causing
    # template instantiation errors
    for base in direct_bases:
        if base.startswith("TopoDS_T") and base != "TopoDS_TShape":
            return None

    # Get base classes
    direct_bases = get_direct_bases(cursor)

    # Check if this class is a Standard_Transient descendant
    transient = is_transient_descendant(cursor)

    # Track transient status for type resolution
    if transient:
        known_transient.add(name)

    # Extract methods
    constructors, methods, operators, static_methods = extract_methods(cursor, known_transient)

    # Extract fields
    fields = extract_fields(cursor, known_transient)

    # Extract nested enums
    nested_enums = extract_nested_enums(cursor)

    # Extract documentation
    doc = extract_doc(cursor)

    # Get header file location
    header_file = ""
    if cursor.location and cursor.location.file:
        header_file = str(cursor.location.file)

    cls = ClassDecl(
        name=name,
        module_name=module_name,
        base_classes=direct_bases,
        is_transient_descendant=transient,
        constructors=constructors,
        methods=methods,
        operators=operators,
        static_methods=static_methods,
        fields=fields,
        nested_enums=nested_enums,
        header_file=header_file,
        doc=doc,
        has_public_default_ctor=has_public_default_ctor,
    )

    return cls
