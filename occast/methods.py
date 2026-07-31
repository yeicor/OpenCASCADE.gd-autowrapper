"""Extract method declarations from a class cursor."""

from __future__ import annotations

import re
from pathlib import Path

from clang.cindex import AccessSpecifier, Cursor, CursorKind

from model import MethodDecl, MethodKind, Parameter, OperatorType, OCCTType
from occast.type_utils import make_occt_type, is_handle_type
from classify.skippable import UNWRAPPABLE_TYPES
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
    "ObjectIterator",  # returns Iterator typedef that libclang resolves to int
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
            # Skip protected/private constructors
            if child.access_specifier in (AccessSpecifier.PRIVATE, AccessSpecifier.PROTECTED):
                continue
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
            if child.access_specifier != AccessSpecifier.PUBLIC:
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

    try:
        src_file = cursor.location.file.name
        if not src_file:
            raise OSError
        start_line = cursor.extent.start.line - 1
        end_line = cursor.extent.end.line
        src_lines = open(src_file).read().split('\n')
        src_text = '\n'.join(src_lines[start_line:end_line])
    except (OSError, IndexError):
        src_text = ""

    # Detect template resolution failures in parameters
    all_resolved = [p.type.spelling for p in params]
    if not any("<" in r for r in all_resolved):
        if '<' in src_text:
            return None

    # Fix handle types misresolved by libclang
    void_type = OCCTType(base_name='void', spelling='void', is_ref=False, is_handle=False, is_const=False, is_pointer=False)
    handle_fixed = _fix_handle_types_from_source(
        src_text, void_type, params, cursor.spelling, UNWRAPPABLE_TYPES)
    if handle_fixed:
        _, params = handle_fixed
    void_type, params = _fix_typedef_handle_types(void_type, params, UNWRAPPABLE_TYPES)

    # After fixing, check for remaining misresolved int params that are
    # actually collection types (NCollection_, etc.) — skip the constructor.
    # Use a count-based heuristic: if the number of collection-template mentions
    # in the source param area exceeds the number of correctly-resolved template
    # params (those whose libclang spelling contains '<'), then some templates
    # are still misresolved and we must skip.
    # Only count non-handle template types (handles are fixed separately)
    count_correctly_resolved = sum(
        1 for r in all_resolved
        if '<' in r and '::handle<' not in r
    )
    if handle_fixed or any("<" not in r and r in {"int", "const int", "const int &", "int &", "int &&",
                                                  "bool", "const bool &", "double", "const double &",
                                                  "int32_t", "int64_t", "uint32_t", "uint64_t"}
                           for r in all_resolved):
        still_bad = [p for p in params if p.type.base_name in {"int", "const int", "int &", "const int &", "int &&",
                                                                "int32_t", "int64_t", "uint32_t", "uint64_t",
                                                                "short", "long", "signed", "unsigned"}]
        if still_bad:
            # Check the entire param area for collection templates
            func_name = cursor.spelling
            func_pos = -1
            _pos = len(src_text)
            while _pos > 0:
                _idx = src_text.rfind(func_name, 0, _pos)
                if _idx < 0:
                    break
                _after = _idx + len(func_name)
                _ws = 0
                while _after + _ws < len(src_text) and src_text[_after + _ws] in ' \t':
                    _ws += 1
                if _after + _ws < len(src_text) and src_text[_after + _ws] == '(':
                    _before = _idx - 1
                    while _before >= 0 and src_text[_before] in ' \t':
                        _before -= 1
                    if not (_before >= 1 and src_text[_before-1:_before+1] == '->') and \
                       not (_before >= 0 and src_text[_before] == '.'):
                        func_pos = _idx
                _pos = _idx
            if func_pos >= 0:
                open_paren = src_text.find('(', func_pos)
                if open_paren >= 0:
                    close_paren = src_text.find(')', open_paren)
                    if close_paren >= 0:
                        param_area = src_text[open_paren:close_paren]
                        count_collection_src = len(__import__('re').findall(
                            r'NCollection_|TColStd_|TColgp_', param_area))
                        if count_collection_src > count_correctly_resolved:
                            return None
            # Check for handle inner types in UNWRAPPABLE_TYPES
            for m in __import__('re').finditer(r'(?:occ|opencascade)::handle\s*<\s*(\w+)', src_text):
                if m.group(1) in UNWRAPPABLE_TYPES:
                    return None

    # Skip deleted constructors (e.g. copy/move `= delete`)
    try:
        if not src_text:
            raise OSError
        src_text_one = src_text.replace(' ', '').replace('\n', '')
        if '=delete' in src_text_one:
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
    # template types (e.g. NCollection_IndexedMap<...>) that libclang resolves to simple
    # primitives like 'int'. Compare the source line with the resolved type.
    # Check both return type AND parameters.
    primitive_resolved = {"int", "unsigned int",
                          "const int", "const int &", "int &", "int &&",
                          "bool", "const bool &", "double", "const double &",
                          "int32_t", "int64_t", "uint32_t", "uint64_t"}
    try:
        src_file = cursor.location.file.name
        if not src_file:
            raise OSError
        start_line = cursor.extent.start.line - 1
        end_line = cursor.extent.end.line
        src_lines = open(src_file).read().split('\n')
        src_text = '\n'.join(src_lines[start_line:end_line])
    except (OSError, IndexError):
        src_text = ""

    all_resolved = [return_type.spelling] + [p.type.spelling for p in params]
    # Check for handle patterns first (most important — libclang always misresolves
    # occ::handle<T> to int/void* regardless of other template types in the signature)
    handle_fixed = _fix_handle_types_from_source(
        src_text, return_type, params, cursor.spelling, UNWRAPPABLE_TYPES)
    if handle_fixed:
        return_type, params = handle_fixed
    return_type, params = _fix_typedef_handle_types(return_type, params, UNWRAPPABLE_TYPES)

    # After fixing handles, check if any remaining misresolved-int params or return type
    # have collection-template types (NCollection_List<handle<T>> etc.) in the source.
    # These are unfixable — skip the whole method.
    # Use a count-based heuristic: if the number of collection-template mentions
    # in the source param area exceeds the number of correctly-resolved template
    # params (those whose libclang spelling contains '<'), then some templates
    # are still misresolved and we must skip.
    # Only count PARAMETER types here (return type is outside the param area).
    # Only count non-handle template types (handles are fixed separately)
    count_correctly_resolved_params = sum(
        1 for p in params
        if '<' in p.type.spelling and '::handle<' not in p.type.spelling
    )
    should_skip = False
    if handle_fixed or any("<" not in r and r in primitive_resolved for r in all_resolved):
        still_bad = [p for p in params if p.type.base_name in primitive_resolved]
        # Also check return type
        if return_type.base_name in primitive_resolved:
            still_bad.append(None)  # flag for return type check
        if still_bad:
            # Check the entire parameter declaration area (from ( to )) for any
            # collection-template types — if present, the method is unwrappable.
            func_name = cursor.spelling
            func_pos = -1
            _pos = len(src_text)
            while _pos > 0:
                _idx = src_text.rfind(func_name, 0, _pos)
                if _idx < 0:
                    break
                _after = _idx + len(func_name)
                _ws = 0
                while _after + _ws < len(src_text) and src_text[_after + _ws] in ' \t':
                    _ws += 1
                if _after + _ws < len(src_text) and src_text[_after + _ws] == '(':
                    # Skip if inside a string literal
                    if src_text[:_idx].count('"') % 2 == 1:
                        _pos = _idx
                        continue
                    _before = _idx - 1
                    while _before >= 0 and src_text[_before] in ' \t':
                        _before -= 1
                    if not (_before >= 1 and src_text[_before-1:_before+1] == '->') and \
                       not (_before >= 0 and src_text[_before] == '.'):
                        func_pos = _idx
                _pos = _idx
            if func_pos >= 0:
                open_paren = src_text.find('(', func_pos)
                if open_paren >= 0:
                    close_paren = src_text.find(')', open_paren)
                    if close_paren >= 0:
                        param_area = src_text[open_paren:close_paren]
                        count_collection_src = len(__import__('re').findall(
                            r'NCollection_|TColStd_|TColgp_', param_area))
                        if count_collection_src > count_correctly_resolved_params:
                            return None
            # Check for handle inner types in UNWRAPPABLE_TYPES
            for m in __import__('re').finditer(r'(?:occ|opencascade)::handle\s*<\s*(\w+)', src_text):
                if m.group(1) in UNWRAPPABLE_TYPES:
                    return None
            
            # Also check return type area (before function name) for collection types
            for p in still_bad:
                if p is None:
                    if func_pos >= 0:
                        ret_area = src_text[:func_pos]
                        if any(p in ret_area for p in ('NCollection_', 'TColStd_', 'TColgp_')):
                            return None
            for p in still_bad:
                if p is None:
                    if func_pos >= 0:
                        ret_area = src_text[:func_pos]
                        if any(p in ret_area for p in ('NCollection_', 'TColStd_', 'TColgp_')):
                            return None
            # Check each param individually — if any was converted to handle but
            # should not have been (inside collection template), skip.
            remaining_primitive = [x for x in still_bad if x is not None]
            if remaining_primitive:
                for p in remaining_primitive:
                    if not p.name or p.name.startswith('arg'):
                        continue
                    try:
                        ppos = src_text.index(p.name)
                    except ValueError:
                        continue
                    if func_pos < 0:
                        continue
                    open_paren = src_text.find('(', func_pos)
                    if open_paren < 0:
                        continue
                    decl_seg = src_text[open_paren:ppos]
                    if any(p in decl_seg for p in ('NCollection_', 'TColStd_', 'TColgp_')):
                        # Only skip if the NCollection_ template is still OPEN
                        # (unmatched '<') — meaning the param is inside the template
                        # angle brackets.  If the template is closed with '>' before
                        # this param, it's a separate legitimate primitive.
                        nc = any(
                            decl_seg.rfind(p, 0) >= 0 and
                            decl_seg[decl_seg.rfind(p):ppos].count('<')
                            > decl_seg[decl_seg.rfind(p):ppos].count('>')
                            for p in ('NCollection_', 'TColStd_', 'TColgp_')
                        )
                        if nc:
                            return None

    if not handle_fixed and "<" not in return_type.spelling:
        if not any("<" in r for r in all_resolved):
            # Check canonical type: if it resolves to a primitive but the
            # original spelling is different, libclang may have failed.
            canon_resolved = return_type.canonical_spelling in primitive_resolved
            spell_resolved = any(r in primitive_resolved for r in all_resolved)
            if canon_resolved or spell_resolved:
                if '<' in src_text or '::' in src_text:
                    return None
                # Macro-defined methods (e.g. DEFINE_DERIVED_ATTRIBUTE):
                # libclang can't resolve their return types from macro expansions.
                # Normal declarations may start with modern C++ specifiers that the
                # macro heuristic must not mistake for a macro (constexpr accessors
                # like gp_Pnt::X(), [[nodiscard]] attributes, etc.).
                decl_head = re.sub(r'^(\[\s*\[.*?\]\s*\])+', '', src_text.strip(), flags=re.DOTALL)
                if not decl_head.startswith(('virtual', 'Standard_EXPORT', 'static', 'inline',
                                             'constexpr', 'template', 'friend')):
                    return None

    # Skip deleted methods (`= delete`)
    try:
        src_file = cursor.location.file.name
        if src_file:
            start_line = cursor.extent.start.line - 1
            end_line = cursor.extent.end.line
            src_lines = open(src_file).read().split('\n')
            src_text = ''.join(src_lines[start_line:end_line]).replace(' ', '')
            if '=delete' in src_text:
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


def _make_handle_occt_type(
    inner: str,
    unwrappable_types: set[str] | None = None,
) -> OCCTType | None:
    """Build an OCCTType representing a const-ref handle to the given inner type."""
    if unwrappable_types and inner in unwrappable_types:
        return None
    return OCCTType(
        spelling=f"const occ::handle<{inner}>&",
        base_name=inner,
        canonical_spelling=f"opencascade::handle<{inner}>",
        is_const=True,
        is_ref=True,
        is_pointer=False,
        is_handle=True,
        handle_inner=inner,
        is_transient_descendant=True,
    )


def _fix_handle_types_from_source(
    src_text: str,
    return_type: OCCTType,
    params: list[Parameter],
    func_name: str = "",
    unwrappable_types: set[str] | None = None,
) -> tuple[OCCTType, list[Parameter]] | None:
    """Fix libclang's misresolution of handle types in source text.

    libclang resolves occ::handle<T> and opencascade::handle<T> to int
    in some contexts (notably AIS_ManipulatorOwner methods and GeomAPI
    return types).  When the source text contains handle patterns,
    reconstruct the correct OCCTType for the return type and all params.
    """
    # Find all handle <T> occurrences in the source line.
    # Captures: namespace ("occ" or "opencascade"), inner type name.
    handle_spans: list[tuple[int, int, str]] = []  # (start, end, inner_type)
    for m in re.finditer(
        r'(?:occ|opencascade)::handle\s*<\s*(\w+)',
        src_text):
        handle_spans.append((m.start(), m.end(), m.group(1)))

    if not handle_spans:
        return None

    # Replace any param that libclang resolved to int/const int/const int&
    # with a proper handle type if the param name appears in the source near
    # a handle<...> pattern.
    primitive_ints = {"int", "const int", "int &", "const int &", "int &&",
                      "int32_t", "int64_t", "uint32_t", "uint64_t",
                      "short", "long", "signed", "unsigned"}

    def _is_misresolved_int(t: OCCTType) -> bool:
        return t.base_name in primitive_ints or t.spelling in primitive_ints

    # Find the opening paren of the function declaration. Used to distinguish
    # handles in the return type (before the paren) from handles in the
    # parameter list (after the paren).
    if func_name:
        # Find the LAST occurrence of func_name that is followed by '('
        # but NOT preceded by '->' or '.' (which would make it a method
        # call in an inline body, not a declaration).
        # Also NOT inside a string literal (which would be a false
        # match in deprecation strings like Standard_DEPRECATED(
        # "...func_name()...")).
        func_idx = -1
        pos = len(src_text)
        while pos > 0:
            idx = src_text.rfind(func_name, 0, pos)
            if idx < 0:
                break
            after = idx + len(func_name)
            ws = 0
            while after + ws < len(src_text) and src_text[after + ws] in ' \t':
                ws += 1
            if after + ws < len(src_text) and src_text[after + ws] == '(':
                # Skip if inside a string literal (detected by odd quote count before)
                if src_text[:idx].count('"') % 2 == 1:
                    pos = idx
                    continue
                # Check if preceded by '->' or '.' (method call, not declaration)
                before = idx - 1
                while before >= 0 and src_text[before] in ' \t':
                    before -= 1
                if not (before >= 1 and src_text[before-1:before+1] == '->') and \
                   not (before >= 0 and src_text[before] == '.'):
                    func_idx = idx
            pos = idx
        func_paren = -1
        if func_idx >= 0:
            func_paren = src_text.find('(', func_idx)
    else:
        func_paren = src_text.find('(')

    # Fix return type
    new_return_type = return_type
    if _is_misresolved_int(return_type) and handle_spans:
        # Verify the handle appears BEFORE the function name (i.e., in the
        # return type declaration, not in parameters).
        first_handle_start = handle_spans[0][0]
        if func_paren > first_handle_start:
            # Check that the first handle is NOT inside a collection template.
            # Must look beyond the nearest comma/paren because handle might be
            # inside NCollection_ template arguments with comma-separated params.
            pre_text = src_text[:first_handle_start]
            inside_collection = False
            for prefix in ('NCollection_', 'TColStd_', 'TColgp_'):
                idx = pre_text.rfind(prefix)
                if idx >= 0:
                    between = src_text[idx:first_handle_start]
                    if between.count('<') > between.count('>'):
                        inside_collection = True
                        break
            if not inside_collection:
                # Use the first handle found for the return type (common case:
                # the return type IS the handle, like const occ::handle<T>&)
                ht = _make_handle_occt_type(handle_spans[0][2], unwrappable_types)
                if ht is not None:
                    new_return_type = ht

    # Fix parameter types — match by param name in source text.
    # Only fix if handle<Inner> is the outermost type (i.e., not nested inside
    # NCollection_<..., handle<Inner>> or similar).
    new_params = list(params)
    for i, p in enumerate(params):
        if not _is_misresolved_int(p.type):
            continue
        # Look for this param name near a handle<T> pattern in the source
        pname = p.name or ""
        found = False
        for _, _, inner in handle_spans:
            if pname and not pname.startswith('arg'):
                # Build regex that matches the parameter declaration as a whole:
                # the handle must be the outermost template — no NCollection_ prefix
                # before handle<Inner> for this parameter.
                m = re.search(
                    r'handle<\s*' + re.escape(inner) + r'\s*>\s*&?\s*' + re.escape(pname) + r'\b',
                    src_text)
                if not m:
                    continue
            else:
                # Synthetic/empty name: match by position — find the Nth
                # bare handle (not inside NCollection_) that corresponds to
                # this parameter index. Match across ALL handle types,
                # not just the current inner type.
                param_pos = sum(1 for j in range(i) if _is_misresolved_int(params[j].type))
                # Build a list of all non-collection handles in the parameter
                # list (after the declaration paren), in order. Handles before
                # the paren belong to the return type and must be excluded so
                # the positional match is not off by one.
                all_bare = []
                for hm_all in re.finditer(
                    r'(?:occ|opencascade)::handle\s*<\s*(\w+)',
                    src_text):
                    if func_paren >= 0 and hm_all.start() < func_paren:
                        continue
                    pre_hm = src_text[:hm_all.start()]
                    inside_coll = any(
                        src_text.rfind(p, 0, hm_all.start()) >= 0 and
                        src_text[src_text.rfind(p, 0, hm_all.start()):hm_all.start()].count('<')
                        > src_text[src_text.rfind(p, 0, hm_all.start()):hm_all.start()].count('>')
                        for p in ('NCollection_', 'TColStd_', 'TColgp_')
                    )
                    if not inside_coll:
                        all_bare.append(hm_all)
                if param_pos >= len(all_bare):
                    continue
                m = all_bare[param_pos]
                inner = m.group(1)  # use the actual inner type from the matched handle
            # Verify handle<Inner> is NOT inside NCollection_<...> or similar.
            pre_m = src_text[:m.start()]
            inside_coll = any(
                src_text.rfind(p, 0, m.start()) >= 0 and
                src_text[src_text.rfind(p, 0, m.start()):m.start()].count('<') > src_text[src_text.rfind(p, 0, m.start()):m.start()].count('>')
                for p in ('NCollection_', 'TColStd_', 'TColgp_')
            )
            if inside_coll:
                continue
            ht = _make_handle_occt_type(inner, unwrappable_types)
            if ht is None:
                continue
            new_params[i] = Parameter(
                type=ht,
                name=p.name,
            )
            found = True
            break

    return (new_return_type, new_params)


# ---------------------------------------------------------------------------
# Typedef'd handle resolution (occ::handle<X> alias-template misresolution)
# ---------------------------------------------------------------------------

# libclang resolves `occ::handle<T>` (an alias template) to `int` whenever the
# spelled type is a typedef of it (e.g. `typedef occ::handle<IMeshData_Edge>
# IEdgeHandle;`).  `opencascade::handle<T>` spelled directly resolves fine.
# Recover these from the OCCT headers' typedef declarations.

_TYPEDEF_HANDLE_MAP: dict[str, str] | None = None

_TYPEDEF_HANDLE_PAT = re.compile(
    r'typedef\s+(?:occ|opencascade)::handle\s*<\s*([A-Za-z_]\w*)\s*>\s+'
    r'([A-Za-z_]\w*)\s*;'
    r'|using\s+([A-Za-z_]\w*)\s*=\s*(?:occ|opencascade)::handle\s*<\s*'
    r'([A-Za-z_]\w*)\s*>\s*;')


def _load_handle_typedef_map() -> dict[str, str]:
    """Map handle-typedef name -> inner handle type, from OCCT headers."""
    global _TYPEDEF_HANDLE_MAP
    if _TYPEDEF_HANDLE_MAP is not None:
        return _TYPEDEF_HANDLE_MAP
    mapping: dict[str, list[str]] = {}
    occt_dir = (Path.home() / "Projects" / "OpenCASCADE.gd" / "vcpkg"
                / "installed" / "x64-linux" / "include" / "opencascade")
    if occt_dir.is_dir():
        for h in sorted(occt_dir.glob("*.hxx")):
            try:
                src = h.read_text(errors="replace")
            except OSError:
                continue
            for m in _TYPEDEF_HANDLE_PAT.finditer(src):
                if m.group(1) and m.group(2):
                    inner, name = m.group(1), m.group(2)
                else:
                    name, inner = m.group(3), m.group(4)
                mapping.setdefault(name, []).append(inner)
    result: dict[str, str] = {}
    for name, inners in mapping.items():
        uniq = list(dict.fromkeys(inners))
        if len(uniq) == 1:
            result[name] = uniq[0]
    _TYPEDEF_HANDLE_MAP = result
    return result


def _resolve_typedef_handle(base_name: str) -> str | None:
    """If base_name is a typedef alias for a handle, return the inner type."""
    if not base_name:
        return None
    name = base_name.rsplit("::", 1)[-1]
    if not name.replace("_", "").isalnum():
        return None
    return _load_handle_typedef_map().get(name)


def _fix_typedef_handle_types(
    return_type: OCCTType,
    params: list[Parameter],
    unwrappable_types: set[str] | None = None,
) -> tuple[OCCTType, list[Parameter]]:
    """Recover handle types hidden behind typedefs (e.g. IMeshData::IEdgeHandle).

    libclang collapses `occ::handle<T>` alias-template typedefs to primitive
    int, losing the handle.  Reconstruct the proper OCCTType for any param or
    return type whose base name matches a known handle typedef.
    """
    def _maybe(t: OCCTType) -> OCCTType | None:
        if t.is_handle:
            return None
        inner = _resolve_typedef_handle(t.base_name)
        if inner is None:
            return None
        return _make_handle_occt_type(inner, unwrappable_types)

    new_return_type = return_type
    if not return_type.is_void:
        fixed = _maybe(return_type)
        if fixed is not None:
            new_return_type = fixed

    new_params = list(params)
    for i, p in enumerate(params):
        fixed = _maybe(p.type)
        if fixed is not None:
            new_params[i] = Parameter(type=fixed, name=p.name)

    return (new_return_type, new_params)
