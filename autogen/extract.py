"""AST extraction: classes, enums, typedefs, methods, fields from a TU.

Clean extraction driven by the libclang Type API; no source-text
mis-resolution heuristics.  The only source-text readers are (a) field access
recovery (libclang's access_specifier is unreliable for OCCT class bodies) and
(b) token-extent default-argument recovery (libclang does not expose defaults).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from clang.cindex import (AccessSpecifier, Cursor, CursorKind, Diagnostic,
                          TranslationUnit, Type)

from .model import (ClassDecl, DocBlock, EnumDecl, EnumValue, FieldDecl,
                    MethodDecl, MethodKind, OperatorType, Parameter)
from .types import make_type

# ---------------------------------------------------------------------------
# Small, data-driven skip tables (nothing type-resolution related)
# ---------------------------------------------------------------------------

# Operators that cannot be represented as a named GDScript method.
UNWRAPPABLE_OPERATORS = {"[]", ",", "->", "->*", "new", "delete", "new[]",
                         "delete[]", "<<", ">>", "~"}

# Methods that are meaningless from GDScript.
SKIP_METHOD_NAMES = {
    "operator new", "operator delete", "operator new[]", "operator delete[]",
    "operator=", "operator[]", "operator<<", "operator>>",
    "ShallowCopy", "ShallowDump",  # stream/IO helpers
    "ObjectIterator",              # returns a typedef libclang cannot follow
}

# Methods/constructors declared Standard_EXPORT in a header but with NO
# definition in the installed OCCT static libs (header/library drift).
# Wrapping them makes the extension .so reference symbols dlopen cannot
# resolve ("undefined symbol" at runtime). Keyed by OCCT class name, value is
# a set of (method name, parameter count); a constructor's name is its class
# name (matching the IR). Verified against OCCT 8.0.1 via
# `nm -D --undefined-only` on the built extension.
SKIP_METHODS_BY_CLASS: dict[str, set[tuple[str, int]]] = {
    "AppDef_MultiLine": {("SetParameter", 2)},
    "BRepFeat_MakeLinearForm": {("TransformShapeFU", 1)},
    "BRepOffsetAPI_FindContigousEdges": {("NbEdges", 0)},
    "BRepOffset_MakeOffset": {("GetAnalyse", 0)},
    "GeomFill_SweepSectionGenerator": {("GeomFill_SweepSectionGenerator", 4),
                                      ("Init", 4)},
    "IntTools_PntOnFace": {("IsValid", 0)},
    "ShapeFix_WireSegment": {("ShapeFix_WireSegment", 2)},
    "TCollection_AsciiString": {("IsEqual", 2)},
}

# Operators we wrap, mapped to stable names.
BINARY_OPERATOR_TYPES = {
    "+": OperatorType.PLUS, "-": OperatorType.MINUS,
    "*": OperatorType.MULTIPLY, "/": OperatorType.DIVIDE,
    "%": OperatorType.MODULO, "^": OperatorType.CROSS,
    "==": OperatorType.EQUALS, "!=": OperatorType.NOT_EQUALS,
    "<": OperatorType.LESS, ">": OperatorType.GREATER,
}
COMPOUND_OPERATOR_TYPES = {
    "+=": OperatorType.PLUS_ASSIGN, "-=": OperatorType.MINUS_ASSIGN,
    "*=": OperatorType.MULTIPLY_ASSIGN, "/=": OperatorType.DIVIDE_ASSIGN,
    "^=": OperatorType.CROSS_ASSIGN,
}
UNARY_OPERATOR_TYPES = {
    "-": OperatorType.UNARY_MINUS, "+": OperatorType.UNARY_PLUS,
    "*": OperatorType.DEREFERENCE,
}


def classify_operator(name: str) -> tuple[OperatorType | None, str]:
    """Map an operator spelling to (OperatorType, wrapper name) or (None, "")."""
    if not name.startswith("operator"):
        return None, ""
    op = name[len("operator"):].strip()
    if not op or op in UNWRAPPABLE_OPERATORS:
        return None, ""
    if op in BINARY_OPERATOR_TYPES:
        return BINARY_OPERATOR_TYPES[op], op
    if op in COMPOUND_OPERATOR_TYPES:
        return COMPOUND_OPERATOR_TYPES[op], op
    if op in UNARY_OPERATOR_TYPES:
        return UNARY_OPERATOR_TYPES[op], f"unary_{op}"
    if op == "()":
        return OperatorType.CALL, "()"
    return None, ""


# ---------------------------------------------------------------------------
# Default arguments (token-extent recovery)
# ---------------------------------------------------------------------------

def _param_default(cursor: Cursor) -> str | None:
    """Recover the source text of a parameter default from its token extent."""
    try:
        tokens = list(cursor.get_tokens())
    except Exception:
        return None
    eq_idx = -1
    for i, t in enumerate(tokens):
        if t.spelling == "=":
            eq_idx = i
            break
    if eq_idx < 0:
        return None
    start = tokens[eq_idx].extent.end
    end = cursor.extent.end
    try:
        if start.file is None or end.file is None or start.file.name != end.file.name:
            return None
        if start.offset is None or end.offset is None:
            return None
        with open(start.file.name) as f:
            text = f.read()[start.offset:end.offset]
    except (OSError, IndexError, AttributeError):
        return None
    text = text.strip()
    if not text:
        return None
    while text.endswith((",", ")")):
        if text.endswith(")"):
            if text.count("(") >= text.count(")"):
                break
            text = text[:-1].rstrip()
        else:
            text = text[:-1].rstrip()
    return text or None


def _is_deleted(cursor: Cursor) -> bool:
    """True when the declaration is `= delete` (libclang does not expose it)."""
    try:
        toks = [t.spelling for t in cursor.get_tokens()]
    except Exception:
        return False
    for i, t in enumerate(toks[:-1]):
        if t == "=" and i + 1 < len(toks) and toks[i + 1] == "delete":
            return True
    return False


def _is_copy_ctor(cursor: Cursor, class_name: str) -> bool:
    """Structurally detect copy constructors (single const-ref / handle<Self>)."""
    params = [c for c in cursor.get_children() if c.kind == CursorKind.PARM_DECL]
    if len(params) != 1:
        return False
    t = make_type(params[0].type)
    return (t.is_handle and t.handle_inner == class_name) or (
        t.base_name == class_name and t.is_ref and t.is_const)


def _has_explicit_noncopyable(cursor: Cursor) -> bool:
    """True if the class explicitly deletes its copy ctor or copy/move assignment."""
    for child in cursor.get_children():
        try:
            if child.kind == CursorKind.CXX_METHOD:
                if (child.spelling == "operator=" and child.is_deleted_method()
                        and (child.is_copy_assignment_operator_method()
                             or child.is_move_assignment_operator_method())):
                    return True
            elif (child.kind == CursorKind.CONSTRUCTOR and child.is_copy_constructor()
                  and child.is_deleted_method()):
                return True
        except Exception:
            pass
    return False


def _params(cursor: Cursor) -> list[Parameter]:
    out: list[Parameter] = []
    for child in cursor.get_children():
        if child.kind == CursorKind.PARM_DECL:
            name = child.spelling or f"arg{len(out)}"
            out.append(Parameter(type=make_type(child.type), name=name,
                                 default_value=_param_default(child)))
    return out


def _doc(cursor: Cursor) -> DocBlock:
    try:
        brief = cursor.brief_comment or ""
    except Exception:
        brief = ""
    try:
        raw = cursor.raw_comment or ""
    except Exception:
        raw = ""
    return DocBlock(brief=brief, raw=raw)


def _extract_method(cursor: Cursor, class_name: str) -> MethodDecl | None:
    name = cursor.spelling
    if name in SKIP_METHOD_NAMES:
        return None
    params = _params(cursor)
    if (name, len(params)) in SKIP_METHODS_BY_CLASS.get(class_name, ()):
        return None
    if _is_deleted(cursor):
        return None
    if cursor.access_specifier != AccessSpecifier.PUBLIC:
        return None

    return_type = make_type(cursor.result_type)
    op_type, op_name = classify_operator(name)

    is_static = bool(cursor.is_static_method())
    return MethodDecl(
        name=op_name if op_type else name,
        return_type=return_type,
        parameters=params,
        kind=(MethodKind.STATIC_METHOD if is_static and not op_type
              else MethodKind.OPERATOR if op_type else MethodKind.METHOD),
        is_const=bool(cursor.is_const_method()),
        is_virtual=bool(cursor.is_virtual_method()),
        is_static=is_static,
        is_default=bool(cursor.is_default_method()),
        is_pure_virtual=bool(cursor.is_pure_virtual_method()),
        is_variadic=bool(cursor.type.is_function_variadic()),
        operator_type=op_type,
        doc=_doc(cursor),
    )


def _extract_constructor(cursor: Cursor, class_name: str) -> MethodDecl | None:
    if cursor.access_specifier in (AccessSpecifier.PRIVATE, AccessSpecifier.PROTECTED):
        return None
    if _is_deleted(cursor):
        return None
    if _is_copy_ctor(cursor, class_name):
        return None
    params = _params(cursor)
    if (class_name, len(params)) in SKIP_METHODS_BY_CLASS.get(class_name, ()):
        return None
    return MethodDecl(
        name=class_name,
        parameters=params,
        kind=MethodKind.CONSTRUCTOR,
        is_default=bool(cursor.is_default_method()),
        doc=_doc(cursor),
    )


# ---------------------------------------------------------------------------
# Field access recovery (libclang's access_specifier is unreliable for OCCT)
# ---------------------------------------------------------------------------

_ACCESS_LABELS = ("public", "protected", "private")


def _read_header(file_name: str) -> str:
    try:
        with open(file_name, "r", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def _class_body_access(source: str, start: int, end: int) -> dict[int, str] | None:
    open_brace = source.find("{", start, end)
    if open_brace < 0:
        return None
    access: str | None = None
    changes: dict[int, str] = {}
    depth = 0
    i = open_brace
    n = len(source)
    line = source.count("\n", 0, open_brace) + 1
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
        if depth == 1:
            for label in _ACCESS_LABELS:
                if source.startswith(label + ":", i) and (
                        i == 0 or not (source[i - 1].isalnum() or source[i - 1] == "_")):
                    access = label
                    changes[line] = label
                    i += len(label) + 1
                    break
            else:
                i += 1
            continue
        i += 1
    return changes


def _field_is_public(field: Cursor, changes: dict[int, str] | None) -> bool | None:
    if changes is None:
        return None
    try:
        fline = field.location.line
    except Exception:
        return None
    best = None
    best_line = -1
    for line, label in changes.items():
        if line <= fline and line > best_line:
            best = label
            best_line = line
    if best is None:
        return None
    return best == "public"


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def _find_type_def(root: Cursor, name: str,
                   defs: dict[str, Cursor | None] | None = None) -> Cursor | None:
    """Find the definition cursor of the type named `name` in the TU.

    Memoized: the scan runs once per parsed header and the TU is large, so a
    repeated search for the same root name must not re-walk the whole tree.
    """
    if defs is None:
        defs = {}
    if name in defs:
        return defs[name]

    def search(cursor: Cursor) -> Cursor | None:
        if cursor.spelling == name and cursor.kind in (
                CursorKind.CLASS_DECL, CursorKind.CLASS_TEMPLATE,
                CursorKind.STRUCT_DECL,
                CursorKind.CLASS_TEMPLATE_PARTIAL_SPECIALIZATION):
            if cursor.is_definition():
                return cursor
        for c in cursor.get_children():
            r = search(c)
            if r:
                return r
        return None

    defs[name] = search(root)
    return defs[name]


def _is_transient(cursor: Cursor, defs: dict[str, Cursor | None] | None = None,
                  root: Cursor | None = None) -> bool:
    """True when the class (or any base) derives from Standard_Transient.

    OCCT marks Transient descendants with DEFINE_STANDARD_RTTIEXT.  The base
    chain can cross typedefs (``BVH_PrimitiveSet3d`` is a typedef of the
    template ``BVH_PrimitiveSet<double, 3>``) and class templates whose base
    specifiers are dependent types.  libclang's ``get_definition`` on a
    typedef base yields the TYPEDEF_DECL (no bases to follow) and on a
    dependent base yields the template definition; a type that cannot be
    followed structurally is resolved by root name in the translation unit.
    """
    if cursor is None or not cursor.is_definition():
        return False
    name = cursor.spelling or ""
    if name == "Standard_Transient":
        return True
    if cursor.kind == CursorKind.TYPEDEF_DECL:
        t = cursor.underlying_typedef_type
        if t is None or root is None:
            return False
        root_name = re.sub(r"<.*", "", t.get_canonical().spelling).strip().split("::")[-1]
        found = _find_type_def(root, root_name, defs)
        return found is not None and _is_transient(found, defs, root)
    for child in cursor.get_children():
        if child.kind != CursorKind.CXX_BASE_SPECIFIER:
            continue
        base = child.get_definition()
        if base is None or not base.is_definition():
            # Dependent base (e.g. BVH_Object<T, N> inside a class template):
            # resolve the root template name in the TU and follow its own base.
            if root is None:
                continue
            root_name = re.sub(r"<.*", "", child.type.spelling).strip().split("::")[-1]
            if not root_name or root_name == name:
                continue
            found = _find_type_def(root, root_name, defs)
            if found is not None and _is_transient(found, defs, root):
                return True
            continue
        if _is_transient(base, defs, root):
            return True
    return False


def _base_names(cursor: Cursor) -> list[str]:
    return [c.type.spelling for c in cursor.get_children()
            if c.kind == CursorKind.CXX_BASE_SPECIFIER]


def _extract_enum(cursor: Cursor, header: str, parent: str = "") -> EnumDecl | None:
    name = cursor.spelling or ""
    if not name or name.startswith("("):
        return None  # anonymous enum: no stable name to expose across modules
    values: list[EnumValue] = []
    for c in cursor.get_children():
        if c.kind == CursorKind.ENUM_CONSTANT_DECL:
            try:
                val = c.enum_value
            except Exception:
                val = None
            values.append(EnumValue(name=c.spelling, value=val))
    try:
        # Nested enums carry a real access specifier; file-scope enums report
        # INVALID but are always public.
        is_public = (not parent) or cursor.access_specifier == AccessSpecifier.PUBLIC
    except Exception:
        is_public = True
    return EnumDecl(
        name=name, values=values,
        is_scoped=cursor.kind == CursorKind.ENUM_DECL and "scoped" in str(cursor.type.spelling),
        is_nested=bool(parent), parent_class=parent,
        is_public=is_public,
        header_file=header, doc=_doc(cursor),
    )


def _extract_class(cursor: Cursor, header: str,
                   tu_root: Cursor | None = None,
                   defs: dict[str, Cursor | None] | None = None) -> ClassDecl:
    name = cursor.spelling
    cls = ClassDecl(
        name=name, base_classes=_base_names(cursor),
        is_transient_descendant=_is_transient(cursor, defs, tu_root),
        is_template=cursor.kind == CursorKind.CLASS_TEMPLATE,
        header_file=header, doc=_doc(cursor),
    )
    source = ""
    changes: dict[int, str] | None = None
    try:
        fname = cursor.location.file.name
        source = _read_header(fname)
        if source:
            changes = _class_body_access(source, cursor.extent.start.offset,
                                         cursor.extent.end.offset)
    except Exception:
        changes = None

    for child in cursor.get_children():
        kind = child.kind
        if kind == CursorKind.DESTRUCTOR:
            if child.access_specifier != AccessSpecifier.PUBLIC:
                cls.has_protected_dtor = True
        if kind == CursorKind.CONSTRUCTOR:
            cls.has_any_ctor = True
            if child.access_specifier != AccessSpecifier.PUBLIC:
                cls.has_any_nonpublic_ctor = True
            else:
                cls.has_any_public_ctor = True
            ctor = _extract_constructor(child, name)
            if ctor:
                cls.constructors.append(ctor)
                if (len(ctor.parameters) == 0
                        or all(p.default_value is not None
                               for p in ctor.parameters)):
                    cls.has_public_default_ctor = True
        elif kind == CursorKind.CXX_METHOD:
            if child.spelling in ("operator new", "operator delete",
                                  "operator new[]", "operator delete[]"):
                cls.has_operator_new_delete = True
            method = _extract_method(child, name)
            if method:
                if method.kind == MethodKind.STATIC_METHOD:
                    cls.static_methods.append(method)
                elif method.kind == MethodKind.OPERATOR:
                    cls.operators.append(method)
                else:
                    cls.methods.append(method)
        elif kind == CursorKind.FIELD_DECL:
            is_public = _field_is_public(child, changes)
            if is_public is None:
                is_public = child.access_specifier == AccessSpecifier.PUBLIC
            cls.fields.append(FieldDecl(name=child.spelling,
                                        type=make_type(child.type),
                                        doc=_doc(child), is_public=is_public,
                                        is_const=bool(child.type.is_const_qualified())))
        elif kind == CursorKind.VAR_DECL:
            # A VarDecl directly under a class definition is a static data member.
            cls.static_constants.append(child.spelling)
        elif kind == CursorKind.ENUM_DECL and child.is_definition():
            enum = _extract_enum(child, header, parent=name)
            if enum is not None:
                cls.nested_enums.append(enum)
    cls.has_pure_virtual = any(m.is_pure_virtual for m in cls.methods)
    cls.has_copy_assignment = not (
        _has_explicit_noncopyable(cursor)
        or any("unique_ptr" in f.type.base_name for f in cls.fields))
    try:
        cls.is_abstract = bool(cursor.is_abstract_record())
    except Exception:
        pass
    return cls


@dataclass
class HeaderResult:
    header: str
    classes: list[ClassDecl] = field(default_factory=list)
    enums: list[EnumDecl] = field(default_factory=list)
    typedefs: list[tuple[str, str]] = field(default_factory=list)
    # OCCT headers that had to be pre-included for this header to parse at all
    # (closure + retry fixes); wrappers of its classes must include them too.
    extra_includes: list[str] = field(default_factory=list)


def extract_header(header: Path, tu: TranslationUnit) -> HeaderResult:
    """Extract declarations DEFINED in the given header file."""
    header = str(header)
    result = HeaderResult(header=header)

    def is_occt_name(name: str) -> bool:
        # OCCT classes begin with a module prefix (`gp_`, `math_`) or a capital.
        # Lowercase names (e.g. `hash`, `tuple`) are C++ std/library helpers.
        return not (name.islower() or name.isdigit() or not name)

    def walk(cursor: Cursor, namespace: tuple[str, ...] = ()):
        for child in cursor.get_children():
            if child.kind == CursorKind.NAMESPACE:
                walk(child, namespace + (child.spelling,))
                continue
            try:
                if child.location.file is None or child.location.file.name != header:
                    continue
            except Exception:
                continue
            if child.kind in (CursorKind.CLASS_DECL, CursorKind.STRUCT_DECL):
                if child.is_definition() and not namespace \
                        and is_occt_name(child.spelling):
                    try:
                        is_specialization = child.specialized_template is not None
                    except Exception:
                        is_specialization = False
                    if is_specialization:
                        continue  # class template specialization
                    result.classes.append(
                        _extract_class(child, header, tu.cursor, defs))
            elif child.kind == CursorKind.ENUM_DECL and child.is_definition() \
                    and not namespace:
                enum = _extract_enum(child, header)
                if enum is not None:
                    result.enums.append(enum)
            elif child.kind == CursorKind.TYPEDEF_DECL:
                t = make_type(child.underlying_typedef_type)
                if t.is_enum or t.is_handle or t.is_collection or (
                        t.base_name and t.base_name[0].isupper()):
                    result.typedefs.append((child.spelling, t.base_name))

    defs: dict[str, Cursor | None] = {}
    walk(tu.cursor)
    return result
