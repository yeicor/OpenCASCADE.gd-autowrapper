"""Extract method declarations from a class cursor."""

from __future__ import annotations

from clang.cindex import AccessSpecifier, Cursor, CursorKind

from model import MethodDecl, MethodKind, Parameter, OperatorType, OCCTType
from occast.type_utils import make_occt_type, is_handle_type
from occast.docs import extract_doc

# Operators that we know how to wrap
BINARY_OPS = {
    "+": OperatorType.PLUS, "-": OperatorType.MINUS,
    "*": OperatorType.MULTIPLY, "/": OperatorType.DIVIDE,
    "%": OperatorType.MODULO, "^": OperatorType.CROSS,
    "==": OperatorType.EQUALS, "!=": OperatorType.NOT_EQUALS,
    "<": OperatorType.LESS, ">": OperatorType.GREATER,
}
COMPOUND_OPS = {
    "+=": OperatorType.PLUS_ASSIGN, "-=": OperatorType.MINUS_ASSIGN,
    "*=": OperatorType.MULTIPLY_ASSIGN, "/=": OperatorType.DIVIDE_ASSIGN,
    "^=": OperatorType.CROSS_ASSIGN,
}
UNARY_OPS = {
    "-": OperatorType.UNARY_MINUS, "+": OperatorType.UNARY_PLUS,
    "*": OperatorType.DEREFERENCE,
}

# Names to skip entirely
SKIP_METHODS = {
    "operator new", "operator delete", "operator new[]", "operator delete[]",
    "operator=", "operator[]",  # subscript can't be named as method
    "ShallowCopy", "ShallowDump",
    "operator<<", "operator>>",  # stream I/O — not representable in GDScript
}

# Operators that can't be wrapped (not representable in GDScript)
UNWRAPPABLE_OPS = {
    "[]",  # subscript operator — can't be named as a method
    ",", "->", "->*", "new", "delete", "new[]", "delete[]",
    "<<", ">>",  # stream operators — not representable in GDScript
    "~",  # bitwise NOT — not representable
}


def classify_operator_name(name: str) -> tuple[OperatorType | None, str | None]:
    """Classify an operator name. Returns (OperatorType, clean_name) or (None, None)."""
    if not name.startswith("operator"):
        return None, None

    op = name[len("operator"):].strip()

    if not op:
        return None, None

    # Skip unwrappable operators
    if op in UNWRAPPABLE_OPS:
        return None, None

    if op in BINARY_OPS:
        return BINARY_OPS[op], op
    if op in COMPOUND_OPS:
        return COMPOUND_OPS[op], op
    if op in UNARY_OPS:
        return UNARY_OPS[op], f"unary_{op}"

    if op == "()":
        return OperatorType.CALL, "()"

    return None, None


def extract_methods(cursor: Cursor, known_transient: set[str]) -> tuple[
    list[MethodDecl],   # constructors
    list[MethodDecl],   # methods
    list[MethodDecl],   # operators
    list[MethodDecl],   # static methods
]:
    """Extract all methods from a class cursor."""
    constructors = []
    methods = []
    operators = []
    static_methods = []

    for child in cursor.get_children():
        if child.kind == CursorKind.CONSTRUCTOR:
            ctor = _extract_constructor(child, known_transient)
            if ctor:
                constructors.append(ctor)

        elif child.kind == CursorKind.DESTRUCTOR:
            pass  # Skip destructors

        elif child.kind == CursorKind.CONVERSION_FUNCTION:
            # Conversion operators (operator TopoDS_Shape(), operator int(), etc.)
            # can't be called like normal methods from GDScript — skip entirely.
            continue

        elif child.kind == CursorKind.CXX_METHOD:
            name = child.spelling
            if name in SKIP_METHODS:
                continue
            if child.access_specifier in (AccessSpecifier.PRIVATE, AccessSpecifier.PROTECTED):
                continue

            method = _extract_method(child, known_transient)
            if not method:
                continue

            # Check if it's an operator
            op_type, op_name = classify_operator_name(name)
            if op_type is not None:
                method.operator_type = op_type
                method.name = op_name
                operators.append(method)
            elif child.is_static_method():
                method.kind = MethodKind.STATIC_METHOD
                method.is_static = True
                static_methods.append(method)
            else:
                methods.append(method)

        elif child.kind == CursorKind.FIELD_DECL:
            pass  # Handled by fields.py

        elif child.kind in (CursorKind.CLASS_DECL, CursorKind.CLASS_TEMPLATE,
                            CursorKind.ENUM_DECL,
                            CursorKind.TYPEDEF_DECL, CursorKind.USING_DECLARATION,
                            CursorKind.CXX_BASE_SPECIFIER):
            pass  # Handled elsewhere

    return constructors, methods, operators, static_methods


def _extract_constructor(cursor: Cursor, known_transient: set[str]) -> MethodDecl | None:
    """Extract a constructor declaration."""
    if not cursor.is_definition():
        # Forward declaration only — still useful for default constructors
        pass

    params = _extract_params(cursor, known_transient)
    doc = extract_doc(cursor)

    # Detect template resolution failures in parameters
    all_resolved = [p.type.spelling for p in params]
    if not any("<" in r for r in all_resolved):
        try:
            src_file = cursor.location.file.name
            if src_file:
                start_line = cursor.extent.start.line - 1
                end_line = cursor.extent.end.line
                src_lines = open(src_file).read().split('\n')
                src_text = '\n'.join(src_lines[start_line:end_line])
                if '<' in src_text:
                    return None
        except (OSError, IndexError):
            pass

    return MethodDecl(
        name=cursor.spelling,
        parameters=params,
        kind=MethodKind.CONSTRUCTOR,
        is_default=cursor.is_default_method(),
        doc=doc,
    )


def _extract_method(cursor: Cursor, known_transient: set[str]) -> MethodDecl | None:
    """Extract a method declaration."""
    params = _extract_params(cursor, known_transient)
    return_type = make_occt_type(cursor.result_type, known_transient)
    doc = extract_doc(cursor)

    # Detect libclang template resolution failures: the source declaration may contain
    # template types (e.g. NCollection_IndexedMap<...>) or qualified typedefs
    # (e.g. XSAlgo_ShapeProcessor::ParameterMap) that libclang resolves to simple
    # primitives like 'const int &'. Compare the source line with the resolved type.
    # Check both return type AND parameters.
    if "<" not in return_type.spelling:
        all_resolved = [return_type.spelling] + [p.type.spelling for p in params]
        if not any("<" in r for r in all_resolved):
            try:
                src_file = cursor.location.file.name
                if src_file:
                    # Read the full method declaration (may span multiple lines)
                    start_line = cursor.extent.start.line - 1
                    end_line = cursor.extent.end.line
                    src_lines = open(src_file).read().split('\n')
                    src_text = '\n'.join(src_lines[start_line:end_line])
                    if '<' in src_text:
                        # Template brackets in source but not in resolved types — failure
                        return None
                    # Also detect qualified typedefs (e.g. X::Y) resolving to primitives.
                    # If source has a :: in a return/param position but resolved type is
                    # int/bool/double, it's a failed resolution.
                    primitive_resolved = {"int", "const int &", "int &", "int &&",
                                          "bool", "const bool &", "double", "const double &"}
                    if any(r in primitive_resolved for r in all_resolved):
                        # Check if source has :: qualified names in type positions
                        if "::" in src_text:
                            # Extract type portion before each param name
                            import re
                            # Look for TypeName::Something patterns before variable names
                            if re.search(r'\w+::\w+', src_text):
                                return None
            except (OSError, IndexError):
                pass

    return MethodDecl(
        name=cursor.spelling,
        return_type=return_type,
        parameters=params,
        kind=MethodKind.METHOD,
        is_const=cursor.is_const_method(),
        is_virtual=cursor.is_virtual_method(),
        is_static=cursor.is_static_method(),
        is_default=cursor.is_default_method(),
        is_pure_virtual=cursor.is_pure_virtual_method(),
        doc=doc,
    )


def _extract_params(cursor: Cursor, known_transient: set[str]) -> list[Parameter]:
    """Extract parameters from a method/constructor cursor."""
    params = []
    for child in cursor.get_children():
        if child.kind == CursorKind.PARM_DECL:
            ptype = make_occt_type(child.type, known_transient)
            params.append(Parameter(
                type=ptype,
                name=child.spelling or "arg{}".format(len(params)),
            ))
    return params
