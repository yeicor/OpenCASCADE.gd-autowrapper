"""Group overloaded methods and assign disambiguation suffixes."""

from __future__ import annotations

from collections import defaultdict

from model import ClassDecl, MethodDecl, MethodKind

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


def get_method_unique_name(method: MethodDecl, prefix: str = "") -> str:
    """Get a unique method name for binding.

    For non-overloaded methods: returns the original name.
    For overloaded methods: returns name_N where N is 1-based.
    """
    if not method.is_overload:
        safe = OPERATOR_NAME_MAP.get(method.name, method.name)
        return safe

    # For constructors: from_N (factory method naming)
    if method.kind == MethodKind.CONSTRUCTOR:
        if len(method.parameters) == 0:
            return "default"
        return f"from_{method.overload_index + 1}"

    safe = OPERATOR_NAME_MAP.get(method.name, method.name)
    return f"{safe}_{method.overload_index + 1}"
