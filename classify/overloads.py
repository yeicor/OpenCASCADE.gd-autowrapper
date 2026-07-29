"""Group overloaded methods and assign disambiguation suffixes."""

from __future__ import annotations

from collections import defaultdict

from model import ClassDecl, MethodDecl, MethodKind, OCCTType

OPERATOR_NAME_MAP = {
    "==": "equal", "!=": "not_equal", "<": "less", ">": "greater",
    "<=": "less_equal", ">=": "greater_equal",
    "+": "add", "-": "subtract", "*": "multiply", "/": "divide",
    "%": "modulo", "^": "cross",
    "+=": "add_assign", "-=": "subtract_assign",
    "*=": "multiply_assign", "/=": "divide_assign", "^=": "cross_assign",
    "=": "assign",
    "unary_minus": "negate", "unary_plus": "plus",
    "*deref": "dereference", "()": "call",
}


def group_overloads(cls: ClassDecl) -> None:
    """Group overloaded methods and assign unique names.

    For methods with the same name but different parameters, appends a suffix:
    - First overload: no suffix (or "_1" if there's a no-arg version)
    - Second overload: "_2"
    - etc.

    Sets method.overload_index and generates a unique wrapper_method_name.
    """
    # Group methods by name (excluding constructors)
    groups: dict[str, list[MethodDecl]] = defaultdict(list)
    for method in cls.methods:
        groups[method.name].append(method)
    for method in cls.static_methods:
        groups[method.name].append(method)
    for method in cls.operators:
        groups[method.name].append(method)

    # For each group with multiple overloads, assign suffixes
    for name, group in groups.items():
        if len(group) <= 1:
            continue

        # Sort by parameter count for deterministic ordering
        group.sort(key=lambda m: len(m.parameters))

        for i, method in enumerate(group):
            method.is_overload = True
            method.overload_index = i

    # Mark constructors as overloaded too
    if len(cls.constructors) > 1:
        cls.constructors.sort(key=lambda m: len(m.parameters))
        for i, ctor in enumerate(cls.constructors):
            ctor.is_overload = True
            ctor.overload_index = i


def _type_to_string(t: OCCTType) -> str:
    """Convert an OCCTType to a stable string for use in method names.

    Includes cv-qualifiers for ref/ptr types so that `const gp_Dir&`
    and `gp_Dir&&` produce different names.  Top-level const on a
    by-value parameter is omitted (it does not affect overloading).
    """
    parts = [t.base_name]
    if t.is_const and (t.is_ref or t.is_pointer):
        parts.append("const")
    if t.is_pointer:
        parts.append("ptr")
    elif t.is_ref:
        parts.append("ref")
    return "_".join(parts)


def _sanitize_typename(s: str) -> str:
    """Sanitize a C++ type name for use in a GDScript method name."""
    s = s.replace("::", "_")
    s = s.replace("<", "_of_")
    s = s.replace(">", "")
    s = s.replace(", ", "_and_")
    s = s.replace(",", "_and_")
    s = s.replace(" ", "_")
    # Collapse consecutive underscores
    while "__" in s:
        s = s.replace("__", "_")
    s = s.strip("_")
    if not s:
        s = "unknown"
    return s


def get_method_unique_name(method: MethodDecl, prefix: str = "") -> str:
    """Get a unique method name for binding.

    For non-overloaded methods: returns the original name.
    For overloaded methods: returns name_N where N is 1-based.
    For constructors with arguments: returns from_ParamType1_ParamType2_...
    """
    # Constructors: default ctor -> "default", parameterized -> from_ParamTypes
    if method.kind == MethodKind.CONSTRUCTOR:
        if len(method.parameters) == 0:
            return "default"
        param_types = [_sanitize_typename(_type_to_string(p.type)) for p in method.parameters]
        return "from_" + "_".join(param_types)

    if not method.is_overload:
        safe = OPERATOR_NAME_MAP.get(method.name, method.name)
        return safe

    safe = OPERATOR_NAME_MAP.get(method.name, method.name)
    return f"{safe}_{method.overload_index + 1}"
