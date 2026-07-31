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


def dedupe_methods(cls: ClassDecl) -> None:
    """Drop methods whose wrapper name collides with an earlier one.

    Distinct OCCT overloads can wrap to the same GDScript signature and
    therefore the same wrapper name:
      - handle<X>& vs X& (both resolve to Ref<X>)
      - int vs size_t under libclang (size_t resolves to int when <stddef.h>
        is unavailable)
      - typedef vs canonical collection spellings (TopTools_ListOfShape vs
        NCollection_List<TopoDS_Shape>)
      - absorbed out-parameters
    Emitting both would produce duplicate C++ declarations (header) and
    duplicate _bind_methods registrations.  Keep the first in emission order
    (constructors, methods, operators, static methods).
    """
    seen: set[str] = set()
    for lst in (cls.constructors, cls.methods, cls.operators, cls.static_methods):
        keep: list[MethodDecl] = []
        for m in lst:
            if m.skip:
                keep.append(m)
                continue
            name = get_method_unique_name(m)
            if name in seen:
                continue
            seen.add(name)
            keep.append(m)
        lst[:] = keep


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


# Characters valid in GDScript/C++ identifiers, used to encode the hash.
# 63 symbols -> 3 chars ~= 17.9 bits, more than enough to disambiguate
# the handful of overloads that share a name.
_IDENT_CHARS = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_"


def _hash_suffix(s: str) -> str:
    """Deterministic 3-char hash (FNV-1a 32-bit, base-63 identifier encoding).

    Stable across runs and Python versions (unlike hash()), and independent of
    the order/count of other overloads, so names survive overload-set changes.
    """
    h = 2166136261
    for ch in s.encode("utf-8"):
        h ^= ch
        h = (h * 16777619) & 0xFFFFFFFF
    out = []
    for _ in range(3):
        out.append(_IDENT_CHARS[h % len(_IDENT_CHARS)])
        h //= len(_IDENT_CHARS)
    return "".join(out)


def _param_signature(method: MethodDecl) -> str:
    """Overload-distinguishing signature string for a method.

    Includes the parameter types (with const/ref/ptr qualification) and the
    method's const-ness, so any two overloads that can coexist in C++ produce
    different names.
    """
    parts = [_type_to_string(p.type) for p in method.parameters]
    if method.is_const:
        parts.append("const")
    return "|".join(parts)


def get_method_unique_name(method: MethodDecl, prefix: str = "") -> str:
    """Get a unique method name for binding.

    Non-overloaded methods keep their plain name.  Overloaded methods get a
    short stable hash of their parameter signature (e.g. Init_g5o).  Parameterized
    constructors become factory methods named from_<hash>.
    """
    # Constructors: default ctor -> "default", parameterized -> from_<hash>
    if method.kind == MethodKind.CONSTRUCTOR:
        if len(method.parameters) == 0:
            return "default"
        return "from_" + _hash_suffix(_param_signature(method))

    safe = OPERATOR_NAME_MAP.get(method.name, method.name)
    if not method.is_overload:
        return safe

    return "{}_{}".format(safe, _hash_suffix(_param_signature(method)))
