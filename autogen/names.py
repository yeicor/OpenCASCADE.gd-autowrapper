"""GDScript-facing naming: snake_case, keyword guards, overload suffixes.

This is the wrapper-API contract: every GDScript-visible method name must be
stable and reproducible from the OCCT signature.  Overloads are disambiguated
with a short FNV-1a/base-63 hash of the parameter signature; parameterized
constructors become `from_<hash>` factories.
"""

from __future__ import annotations

import re
from collections import defaultdict

from .model import MethodDecl, MethodKind

OPERATOR_NAME_MAP = {
    "==": "equal", "!=": "not_equal", "<": "less", ">": "greater",
    "<=": "less_equal", ">=": "greater_equal",
    "+": "add", "-": "subtract", "*": "multiply", "/": "divide",
    "%": "modulo", "^": "cross",
    "+=": "add_assign", "-=": "subtract_assign",
    "*=": "multiply_assign", "/=": "divide_assign", "^=": "cross_assign",
    "++": "increment", "--": "decrement",
    "=": "assign",
    # Unrecognized operators fall through classify_operator (extract.py) and
    # reach codegen as plain methods named "operator<tok>"; those raw names
    # would produce invalid member declarations (e.g. `operator++_g`), so map
    # the spellings the extractor could not classify (cached IR included).
    "operator++": "increment", "operator--": "decrement",
    "operator!": "logical_not", "operator~": "complement",
    "operator&=": "and_assign", "operator|=": "or_assign",
    "operator->": "dereference", "operator&&": "logical_and",
    "operator||": "logical_or", "operator<<": "left_shift",
    "operator>>": "right_shift", "operator<<=": "left_shift_assign",
    "operator>>=": "right_shift_assign", "operator^=": "xor_assign",
    "operator!=": "not_equal", "operator==": "equal",
    "operator<": "less", "operator>": "greater",
    "operator<=": "less_equal", "operator>=": "greater_equal",
    "unary_minus": "negate", "unary_plus": "plus",
    "*deref": "dereference", "()": "call",
}

# Static members injected by GDCLASS/godot::Object that would collide with a
# generated instance method of the same name.
_GDCLASS_RESERVED = {
    "free", "initialize_class", "get_class_static", "get_parent_class_static",
    "register_virtuals", "has_get_property_list", "free_property_list_bind",
    "get_property_list_bind", "notification_bind", "property_can_revert_bind",
    "property_get_revert_bind", "set_bind", "get_bind",
    "validate_property_bind", "to_string_bind", "from_dict", "get_instance",
}

_CPP_KEYWORDS = {
    "alignas", "alignof", "and", "and_eq", "asm", "atomic_cancel",
    "atomic_commit", "atomic_noexcept", "auto", "bitand", "bitor", "bool",
    "break", "case", "catch", "char", "char16_t", "char32_t", "class",
    "compl", "concept", "const", "consteval", "constexpr", "constinit",
    "const_cast", "continue", "co_await", "co_return", "co_yield", "decltype",
    "default", "delete", "do", "double", "dynamic_cast", "else", "enum",
    "explicit", "export", "extern", "false", "float", "for", "friend", "goto",
    "if", "inline", "int", "long", "mutable", "namespace", "new", "noexcept",
    "not", "not_eq", "nullptr", "operator", "or", "or_eq", "private",
    "protected", "public", "reflexpr", "register", "reinterpret_cast",
    "requires", "return", "short", "signed", "sizeof", "static",
    "static_assert", "static_cast", "struct", "switch", "synchronized",
    "template", "this", "thread_local", "throw", "true", "try", "typedef",
    "typeid", "typename", "union", "unsigned", "using", "virtual", "void",
    "volatile", "wchar_t", "while", "xor", "xor_eq",
}

# godot-cpp RefCounted's refcount API is invoked through the wrapper pointer
# by Ref<>/instance binding (`reference->init_ref()`, `reference->reference()`);
# a generated wrapper method of the same name (e.g. CDM_Document::Reference)
# would hide the base member and break the FFI refcount.  Reserve the whole
# contract (unreference/get_reference_count for the same reason).
_REFCOUNT_RESERVED = {"reference", "unreference", "init_ref", "get_reference_count"}


def to_snake_case(name: str) -> str:
    """Convert a CamelCase/PascalCase identifier to snake_case (idempotent)."""
    s = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s).lower()


def safe_gd_name(name: str) -> str:
    """snake_case a name, guarding C++ keywords and GDCLASS-injected statics."""
    s = to_snake_case(name)
    if s in _CPP_KEYWORDS or s in _GDCLASS_RESERVED or s in _REFCOUNT_RESERVED:
        s += "_"
    return s


_IDENT_CHARS = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_"


def _hash_suffix(s: str, length: int = 1) -> str:
    """FNV-1a 32-bit hash of `s`, base-63 encoded (stable across runs)."""
    h = 2166136261
    for ch in s.encode("utf-8"):
        h ^= ch
        h = (h * 16777619) & 0xFFFFFFFF
    out = []
    for _ in range(length):
        out.append(_IDENT_CHARS[h % len(_IDENT_CHARS)])
        h //= len(_IDENT_CHARS)
    return "".join(out)


def _type_to_string(t) -> str:
    """Stable per-param string used in the overload hash."""
    parts = [t.base_name]
    if t.is_const and (t.is_ref or t.is_pointer):
        parts.append("const")
    if t.is_pointer:
        parts.append("ptr")
    elif t.is_ref:
        parts.append("ref")
    return "_".join(parts)


def _param_signature(method: MethodDecl) -> str:
    parts = [_type_to_string(p.type) for p in method.parameters]
    if method.is_const:
        parts.append("const")
    return "|".join(parts)


def unique_suffix(base: str, sig: str, claimed: set[str], max_len: int = 8) -> str:
    for length in range(1, max_len + 1):
        suffix = _hash_suffix(sig, length)
        if f"{base}_{suffix}" not in claimed:
            return suffix
    n = 0
    while True:
        suffix = f"x{n}"
        if f"{base}_{suffix}" not in claimed:
            return suffix
        n += 1


def _unique_base(method: MethodDecl) -> str:
    if method.kind == MethodKind.CONSTRUCTOR:
        return "from"
    return safe_gd_name(OPERATOR_NAME_MAP.get(method.name, method.name))


def get_method_unique_name(method: MethodDecl) -> str:
    """The GDScript-facing wrapper method name for a MethodDecl."""
    if method.kind == MethodKind.CONSTRUCTOR:
        if not method.parameters:
            return "default"
        return "from_" + (method.overload_suffix
                          or _hash_suffix(_param_signature(method)))
    safe = safe_gd_name(OPERATOR_NAME_MAP.get(method.name, method.name))
    if method.is_overload and method.overload_suffix:
        return f"{safe}_{method.overload_suffix}"
    return safe


def _method_base_name(method: MethodDecl) -> str:
    """The GDScript-facing base name before any overload disambiguation."""
    return safe_gd_name(OPERATOR_NAME_MAP.get(method.name, method.name))


def group_overloads(cls) -> None:
    """Assign overload suffixes and constructor factory names in-place."""
    groups: dict[str, list[MethodDecl]] = defaultdict(list)
    for method in (*cls.methods, *cls.static_methods, *cls.operators):
        groups[_method_base_name(method)].append(method)

    claimed: set[str] = set()
    for name, group in groups.items():
        if len(group) <= 1:
            claimed.add(name)

    for name in sorted(groups.keys()):
        group = groups[name]
        if len(group) <= 1:
            continue
        group.sort(key=lambda m: len(m.parameters))
        for i, method in enumerate(group):
            method.is_overload = True
            method.overload_index = i
            method.overload_suffix = unique_suffix(
                name, _param_signature(method), claimed)
            claimed.add(f"{name}_{method.overload_suffix}")

    cls.constructors.sort(key=lambda m: len(m.parameters))
    claimed.add("default")
    for ctor in cls.constructors:
        if len(ctor.parameters) == 0:
            continue
        ctor.overload_suffix = unique_suffix(
            "from", _param_signature(ctor), claimed)
        claimed.add(f"from_{ctor.overload_suffix}")

    if len(cls.constructors) > 1:
        for i, ctor in enumerate(cls.constructors):
            ctor.is_overload = True
            ctor.overload_index = i
