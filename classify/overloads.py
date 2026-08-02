"""Group overloaded methods and assign disambiguation suffixes."""

from __future__ import annotations

import re
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


def to_snake_case(name: str) -> str:
    """Convert a CamelCase/PascalCase identifier to snake_case.

    Idempotent for already-snake_case input, so it is safe to apply to any
    method name (including OPERATOR_NAME_MAP values and OCCT names that are
    already lowercase).  Examples: NbNodes -> nb_nodes, GetRange -> get_range,
    TShape -> t_shape, Shape -> shape.
    """
    s = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s).lower()


# Static member functions injected into every wrapper class by the GDCLASS
# macro (e.g. static void free(void*, GDExtensionClassInstancePtr)) and by
# godot::Object (static Ref<Dictionary> from_dict(...), get_instance()).  A
# snake_cased OCCT method with the same name (e.g. NCollection_BaseAllocator::
# Free) would make `&Wrapper::name` an overloaded-name lookup between the
# injected static and the generated instance method, which template deduction
# in ClassDB::bind_method cannot resolve.  Such names get a trailing underscore
# (free -> free_), like C++ keywords.
_GDCLASS_RESERVED = {
    "free",
    "initialize_class",
    "get_class_static",
    "get_parent_class_static",
    "register_virtuals",
    "has_get_property_list",
    "free_property_list_bind",
    "get_property_list_bind",
    "notification_bind",
    "property_can_revert_bind",
    "property_get_revert_bind",
    "set_bind",
    "get_bind",
    "validate_property_bind",
    "to_string_bind",
    "from_dict",
    "get_instance",
}


# C++ keywords that a snake_cased OCCT name may collide with (e.g. Delete ->
# delete).  Such names get a trailing underscore so the generated C++ wrapper
# method stays a valid identifier.
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


def safe_gd_name(name: str) -> str:
    """snake_case a name, guarding against C++ keywords (delete -> delete_) and
    GDCLASS-injected statics (free -> free_)."""
    s = to_snake_case(name)
    if s in _CPP_KEYWORDS or s in _GDCLASS_RESERVED:
        s += "_"
    return s


def group_overloads(cls: ClassDecl) -> None:
    """Group overloaded methods and assign disambiguation suffixes.

    For methods with the same name but different parameters, appends a short
    stable hash of the parameter signature (e.g. Init_a).  The hash starts at
    1 character and grows only when it collides with a name already claimed in
    this class, so distinct overloads can never silently map to the same
    wrapper method (dedupe_methods would otherwise drop the colliding one).

    Sets method.overload_index / overload_suffix and generates a unique
    wrapper method name.
    """
    # Group methods by name (excluding constructors)
    groups: dict[str, list[MethodDecl]] = defaultdict(list)
    for method in cls.methods:
        groups[method.name].append(method)
    for method in cls.static_methods:
        groups[method.name].append(method)
    for method in cls.operators:
        groups[method.name].append(method)

    # Class-wide pool of wrapper method names already claimed.  Singleton
    # groups keep their plain safe name; overloaded methods and parameterized
    # constructors claim hashed suffixes that must not collide with any name in
    # this pool (including other overload groups and "from_<hash>" factories).
    claimed: set[str] = set()
    for name, group in groups.items():
        if len(group) <= 1:
            claimed.add(safe_gd_name(OPERATOR_NAME_MAP.get(name, name)))

    # Assign hashed suffixes to overloaded groups (sorted for determinism).
    for name in sorted(groups.keys()):
        group = groups[name]
        if len(group) <= 1:
            continue
        group.sort(key=lambda m: len(m.parameters))
        safe = safe_gd_name(OPERATOR_NAME_MAP.get(name, name))
        for i, method in enumerate(group):
            method.is_overload = True
            method.overload_index = i
            method.overload_suffix = _unique_suffix(safe, _param_signature(method), claimed)
            claimed.add("{}_{}".format(safe, method.overload_suffix))

    # Constructors: default ctor -> "default", parameterized -> from_<hash>.
    # Even a single parameterized ctor becomes a from_<hash> factory, so it
    # must claim a unique suffix against the class-wide pool.
    cls.constructors.sort(key=lambda m: len(m.parameters))
    claimed.add("default")
    for ctor in cls.constructors:
        if len(ctor.parameters) == 0:
            continue
        ctor.overload_suffix = _unique_suffix("from", _param_signature(ctor), claimed)
        claimed.add("from_{}".format(ctor.overload_suffix))

    if len(cls.constructors) > 1:
        for i, ctor in enumerate(cls.constructors):
            ctor.is_overload = True
            ctor.overload_index = i


def dedupe_methods(cls: ClassDecl) -> None:
    """Drop methods whose wrapper registration collides with an earlier one.

    Two kinds of collisions are resolved, keeping the first in emission order
    (constructors, methods, operators, static methods):

    1. Identical wrapper names.  Distinct OCCT overloads can wrap to the same
       GDScript signature and therefore the same wrapper name:
         - handle<X>& vs X& (both resolve to Ref<X>)
         - int vs size_t under libclang (size_t resolves to int when <stddef.h>
           is unavailable)
         - typedef vs canonical collection spellings (TopTools_ListOfShape vs
           NCollection_List<TopoDS_Shape>)
         - absorbed out-parameters
       Emitting both would produce duplicate C++ declarations (header) and
       duplicate _bind_methods registrations.

    2. Overloads identical in GDScript.  Different OCCT types often collapse to
       one GDScript signature (const char* / TCollection_AsciiString /
       TCollection_ExtendedString / NCollection_String all bind as String;
       handle<X> and X& both bind as Ref<X>).  Such overloads are
       indistinguishable from GDScript, so all but the first (in stable sorted
       order) are dropped, and if a group collapses to a single method it is
       demoted to its plain name (SetText_m/Q/f -> SetText).
    """
    seen: set[str] = set()
    seen_gd: set[tuple[str, tuple[str, ...]]] = set()
    for lst in (cls.constructors, cls.methods, cls.operators, cls.static_methods):
        keep: list[MethodDecl] = []
        for m in lst:
            if m.skip:
                keep.append(m)
                continue
            name = get_method_unique_name(m)
            if name in seen:
                continue
            base = _unique_base(m)
            key = (base, _gd_signature(m))
            if key in seen_gd:
                continue
            seen.add(name)
            seen_gd.add(key)
            keep.append(m)
        lst[:] = keep

    # Demote overload groups that collapse to a single method back to a plain
    # name (e.g. the surviving Graphic3d_Text::SetText overload).  Constructors
    # keep their from_<hash> factory names.
    survivors: dict[str, int] = defaultdict(int)
    for lst in (cls.methods, cls.operators, cls.static_methods):
        for m in lst:
            if not m.skip:
                survivors[_unique_base(m)] += 1
    for lst in (cls.methods, cls.operators, cls.static_methods):
        for m in lst:
            if m.skip:
                continue
            if m.is_overload and survivors[_unique_base(m)] == 1:
                m.is_overload = False
                m.overload_suffix = ""


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


def _unique_suffix(base: str, sig: str, claimed: set[str], max_len: int = 8) -> str:
    """Return a short hash suffix of `sig` that does not collide with any
    `{base}_{suffix}` name already claimed in the class.

    Starts at 1 character and grows until unique (the whole name, not just the
    raw hash, is compared, so collisions with other overload groups or plain
    methods are also resolved).
    """
    for length in range(1, max_len + 1):
        suffix = _hash_suffix(sig, length)
        if "{}_{}".format(base, suffix) not in claimed:
            return suffix
    # Virtually unreachable (8 chars of base-63 = ~48 bits); numeric fallback.
    n = 0
    while True:
        suffix = "x{}".format(n)
        if "{}_{}".format(base, suffix) not in claimed:
            return suffix
        n += 1


def _hash_suffix(s: str, length: int = 1) -> str:
    """Deterministic short hash (FNV-1a 32-bit, base-63 identifier encoding).

    Stable across runs and Python versions (unlike hash()), and independent of
    the order/count of other overloads, so names survive overload-set changes.
    """
    h = 2166136261
    for ch in s.encode("utf-8"):
        h ^= ch
        h = (h * 16777619) & 0xFFFFFFFF
    out = []
    for _ in range(length):
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


def _unique_base(method: MethodDecl) -> str:
    """Base name used to group methods for GDScript-signature dedupe."""
    if method.kind == MethodKind.CONSTRUCTOR:
        return "from"
    return safe_gd_name(OPERATOR_NAME_MAP.get(method.name, method.name))


def _canon_base(t: OCCTType) -> str:
    """Clean canonical base name for an OCCTType (falls back to base_name)."""
    if t.canonical_spelling:
        c = t.canonical_spelling.replace("const ", "").rstrip("&").rstrip("*").strip()
        if c:
            return c
    return t.base_name


def _gd_param_type(t: OCCTType) -> str:
    """GDScript-visible type key for a parameter.

    Mirrors the binding rules in generate/type_map.py's cpp_type_for_param so
    that two OCCT overloads which bind to the SAME GDScript signature produce
    the same key (and are therefore deduplicated).  Conservative: types that
    bind differently get different keys.
    """
    from classify.skippable import OPAQUE_POINTER_TYPES, _resolve_handle_inner
    from generate.type_map import PRIMITIVE_MAP, _PRIMITIVE_WRAPPER_MAP

    base = t.base_name

    # Absorbed ostream params disappear from the wrapper signature.
    if base == "Standard_OStream":
        return ""

    # Streams and wide strings all bind as Godot String.
    if (base in ("Standard_IStream", "Standard_SStream") and t.is_ref
            or base in ("NCollection_String", "TCollection_AsciiString",
                        "TCollection_ExtendedString", "Standard_CString")
            or base == "char" and t.is_pointer
            or base == "char16_t" and t.is_pointer and t.pointee_is_const):
        return "String"

    # Non-const refs/pointers are output params -> mutable holder wrapper.
    if not t.is_handle and (t.is_ref or t.is_pointer) and not t.is_const:
        wname = _PRIMITIVE_WRAPPER_MAP.get(base)
        if wname:
            return "PRIM:" + wname
        return "REF:" + _canon_base(t)

    # Raw buffers / opaque pointers.
    if base == "void" and t.is_pointer:
        return "uint64_t"
    if base in OPAQUE_POINTER_TYPES:
        return "uint64_t"
    if base == "uint8_t" and t.is_pointer:
        return "PackedByteArray"

    # char (by value) binds as int32_t (godot-cpp has no char support).
    if base in ("char", "Standard_Character"):
        return "int32_t"

    if base in PRIMITIVE_MAP:
        return PRIMITIVE_MAP[base]

    # Handles and wrapped classes both bind as Ref<Wrapper>.
    if t.is_handle:
        return "REF:" + _resolve_handle_inner(t.handle_inner)
    return "REF:" + _canon_base(t)


def _gd_signature(method: MethodDecl) -> tuple[str, ...]:
    """GDScript-visible parameter signature (absorbed params dropped)."""
    return tuple(t for t in (_gd_param_type(p.type) for p in method.parameters) if t)


def get_method_unique_name(method: MethodDecl, prefix: str = "") -> str:
    """Get a unique method name for binding.

    All GDScript-facing names are snake_case (the project's standard), converted
    automatically from the OCCT CamelCase name.  Non-overloaded methods keep
    their plain snake_case name.  Overloaded methods get a short stable hash of
    their parameter signature (e.g. init_a), grown only on collision.
    Parameterized constructors become factory methods named from_<hash>.
    """
    # Constructors: default ctor -> "default", parameterized -> from_<hash>
    if method.kind == MethodKind.CONSTRUCTOR:
        if len(method.parameters) == 0:
            return "default"
        return "from_" + (method.overload_suffix or _hash_suffix(_param_signature(method)))

    safe = safe_gd_name(OPERATOR_NAME_MAP.get(method.name, method.name))
    if not method.is_overload:
        return safe

    return "{}_{}".format(safe, method.overload_suffix or _hash_suffix(_param_signature(method)))
