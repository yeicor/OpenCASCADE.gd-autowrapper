"""Generate GDScript "sweep" test suites that exercise every bound method.

Each suite file (one per OCCT module) contains one `test_<ClassName>` method
per wrapped class.  The method constructs an instance and calls every bound
constructor factory, instance method, static method and property accessor with
auto-generated argument values, printing `SWEEP <Class>.<method>` before each
call so that a crash in the log can be attributed to a single call.

The point is breadth: prove that the whole generated surface is *callable*
from GDScript without crashing or erroring.  Semantic correctness of
representative APIs is covered by the hand-written test suites.
"""

from __future__ import annotations

from model import ClassDecl, ClassKind, MethodDecl, ModuleDecl

from generate.type_map import FIXED_ARRAY_PARAMS, OPAQUE_POINTER_TYPES, TypeMap, _PRIMITIVE_WRAPPER_MAP
from generate.props import plan_properties

# GDScript reserved words / identifiers that cannot be used as direct call
# syntax (these fall back to callv()).
_GD_RESERVED = {
    "and", "as", "assert", "await", "break", "breakpoint", "class", "class_name",
    "const", "continue", "elif", "else", "enum", "export", "extends", "false",
    "for", "func", "if", "in", "is", "match", "not", "null", "or", "pass",
    "preload", "return", "self", "signal", "static", "super", "true", "var",
    "while", "void", "yield", "PI", "TAU", "INF", "NAN",
}

import re

_GD_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# Methods that must not be swept with synthetic arguments. Sweeping constructs
# cheap default instances and calls every bound method; a few OCCT APIs take
# arguments that cannot be built that way and either crash or corrupt global
# state when called with a standalone default instance. These methods remain
# fully callable from GDScript — they are only excluded from the auto sweep.
#
# Message_Report::AddLevel/RemoveLevel take Message_Level, a sentry class that
# OCCT explicitly restricts to OCCT_ADD_MESSAGE_LEVEL_SENTRY ("No other code is
# required outside"). A standalone Message_Level registers with the default
# report on construction, and passing a temporary to AddLevel then RemoveLevel
# leaves a dangling pointer in the report's level list; the subsequent Clear()
# segfaults/hangs the process (verified: sweep hangs at clear_g).
_SWEEP_SKIP_BY_CLASS = {
    "Message_Report": {"add_level", "remove_level"},
    # Standard_Condition::Wait() blocks indefinitely until the condition is
    # set; a sweep that calls reset() first would deadlock the test process.
    "Standard_Condition": {"wait"},
    # TopTools_ShapeSet::Read/ReadGeometry parse the given stream as a BREP
    # shape file; sweeping them with garbage input ("s") corrupts the set's
    # internal raw-pointer collections and the destructor aborts with
    # "free(): invalid size" (verified deterministic). Dump/Write are safe
    # (they only serialize the set's current state).
    "TopTools_ShapeSet": {
        "read_P", "read_h", "read_geometry_P", "read_geometry_9",
    },
}


def _needs_callv(name: str) -> bool:
    """True when `name` cannot be used as direct GDScript call syntax.

    GDScript identifiers are `[A-Za-z_][A-Za-z0-9_]*` and must not be reserved
    words. Bound names that violate this (e.g. C++ operator overloads bound as
    `operator|=`) can only be invoked via callv()/call().
    """
    return _GD_IDENT_RE.fullmatch(name) is None or name.lower() in _GD_RESERVED


def _has_live_default_instance(cls: ClassDecl) -> bool:
    """Whether `{wrapper}.new()` yields a wrapper backed by live native storage.

    Mirrors the wrapper generator (generate/source.py default-ctor logic): the
    GDExtension `.new()` allocates the OCCT object for BUILDER / REF_COUNTED
    classes only when the class *declares* a public zero-arg constructor (an
    implicit one is not enough — the class may still be abstract / have no
    usable instance). VALUE and OTHER kinds store a value member that is always
    default-constructible, so `has_public_default_ctor` suffices there.
    """
    if cls.kind in (ClassKind.BUILDER, ClassKind.REF_COUNTED):
        has_declared_default_ctor = any(len(c.parameters) == 0 for c in cls.constructors)
        return has_declared_default_ctor and not cls.has_pure_virtual
    return cls.has_public_default_ctor and not cls.has_pure_virtual

_FLOAT_BASES = {"double", "float", "long double", "Standard_Real", "Standard_ShortReal"}
_INT_BASES = {"int", "unsigned", "char", "short", "long", "long long", "size_t",
              "Standard_Integer", "Standard_Size", "Standard_Byte", "uint8_t",
              "uint16_t", "uint32_t", "uint64_t", "int8_t", "int16_t", "int32_t",
              "int64_t", "Standard_Character", "Standard_ExtCharacter", "char16_t",
              "unsigned char", "signed char", "unsigned int", "unsigned long",
              "unsigned short", "unsigned long long"}

# Standard streams are handled specially by the generated wrapper: a
# Standard_OStream is absorbed into a local ostringstream (the return becomes
# String, the param is dropped), while Standard_IStream / Standard_SStream are
# exposed to GDScript as a String param that seeds the local stream.
_STREAM_BASES = {"Standard_OStream", "Standard_IStream", "Standard_SStream"}


def _primitive_arg(base: str) -> str:
    if base in _FLOAT_BASES:
        return "1.0"
    if base in ("bool", "Standard_Boolean"):
        return "true"
    return "1"


def _instance_arg_expr(wname: str, cls: ClassDecl | None, type_map: TypeMap, depth: int = 0) -> str | None:
    """Build a GDScript expression that produces a *usable* instance of `wname`.

    Wrappers whose OCCT class lacks a public default constructor store a null
    `unique_ptr`/`handle` after `.new()`. Passing such a wrapper as an argument
    dereferences null native storage in the generated C++ and crashes, so fall
    back to a cheap factory to get a live instance. Return None (call skipped)
    when no live instance can be produced.
    """
    if cls is None:
        return None
    if _has_live_default_instance(cls):
        return f"{wname}.new()"
    # REF_COUNTED / BUILDER / OTHER without a public default ctor: prefer a real
    # non-default construct if OCCT declares one, otherwise reuse a cheap
    # factory.
    from classify.overloads import get_method_unique_name
    if depth < 2:
        for ctor in cls.constructors:
            if ctor.skip or not ctor.parameters:
                continue
            args = _call_arg_exprs(ctor, type_map, depth + 1)
            if args is None:
                continue
            name = get_method_unique_name(ctor)
            return f"{wname}.{name}({', '.join(args)})"
    # VALUE wrappers store a default `_native` (never null), so `.new()` is safe.
    if cls.kind == ClassKind.VALUE:
        return f"{wname}.new()"
    return None


def _arg_expr(otype, type_map: TypeMap, depth: int = 0) -> str | None:
    """Return a GDScript expression for a parameter of the given OCCT type."""
    base = otype.base_name
    if otype.is_void:
        return None
    # char* / char16_t* / uint8_t* / Standard_CString → String / PackedByteArray.
    # Must precede the primitive and out-param checks: char, char16_t and uint8_t
    # are in _INT_BASES (so they'd otherwise read as int), and non-const char*/
    # uint8_t* are input buffers, not output params.
    if base == "Standard_CString":
        return '"s"'
    if otype.is_pointer and base in ("char", "char16_t", "uint8_t"):
        if base == "uint8_t":
            return "PackedByteArray([1, 2])"
        return '"s"'
    # Non-const ref/pointer output params are bound by the wrapper as
    # Ref<OcgPrimitiveWrapper>, Ref<OcgEnumValue>, or Ref<OcgWrappedClass>
    # (the callee writes into the wrapper's internal storage). Pass a fresh
    # wrapper instance, not a primitive literal. Must run before the primitive
    # check so `Standard_Real&` (etc.) yields a wrapper, not a float literal.
    if (otype.is_ref or otype.is_pointer) and not otype.is_const and not otype.is_handle:
        if base in _PRIMITIVE_WRAPPER_MAP:
            return f"{_PRIMITIVE_WRAPPER_MAP[base]}.new()"
        if type_map._is_enum(base):
            return "OcgEnumValue.new()"
        wname = type_map.wrapper_name(base)
        if wname is not None:
            cls = type_map.class_decl(base)
            return _instance_arg_expr(wname, cls, type_map, depth)
        return None
    if otype.is_primitive:
        return _primitive_arg(base)
    if otype.is_string or base == "NCollection_String":
        return '"s"'
    if type_map._is_enum(base):
        return "0"
    if base in OPAQUE_POINTER_TYPES:
        return "0"
    if base == "void" and otype.is_pointer:
        return "0"
    # Fixed-size C array input params.
    if base in FIXED_ARRAY_PARAMS:
        gd_type, _, size, _ = FIXED_ARRAY_PARAMS[base]
        if gd_type == "Array":
            from model import occt_name_to_wrapper
            elem_exprs = ", ".join(["OcgGpXYZ.new()"] * size)
            return f"Array([{elem_exprs}])"
        if gd_type == "PackedInt32Array":
            return f"PackedInt32Array([{', '.join(['1'] * size)}])"
        if gd_type == "PackedByteArray":
            return f"PackedByteArray([{', '.join(['1'] * size)}])"
        return None
    # opencascade::handle<T> / occ::handle<T> → fresh wrapper instance
    if otype.is_handle:
        from classify.skippable import _resolve_handle_inner
        inner = _resolve_handle_inner(otype.handle_inner)
        wname = type_map.wrapper_name(inner)
        if wname is None:
            return None
        cls = type_map.class_decl(inner)
        return _instance_arg_expr(wname, cls, type_map, depth)
    # Primitive holder wrappers (out-params etc.) get a fresh wrapper.
    wname = type_map.wrapper_name(base)
    if wname is None:
        return None
    cls = type_map.class_decl(base)
    return _instance_arg_expr(wname, cls, type_map, depth)


def _instance_construction_lines(cls: ClassDecl, type_map: TypeMap) -> list[str]:
    """Return GDScript statements that construct a usable instance `_o`.

    Prefer a real default construct; otherwise reuse a cheap factory so that
    builders/abstract classes are exercised on a live instance instead of on a
    null native object (which only yields "Parameter _native is null" noise).
    """
    wname = wrapper_ref(cls)
    if _has_live_default_instance(cls):
        return [f"\tvar _o := {wname}.new()"]
    if cls.has_pure_virtual:
        # Abstract class: no factory is bound (see generate/source.py), so a
        # null-native `.new()` is the only safe construction.
        return [f"\tvar _o := {wname}.new()"]
    from classify.overloads import get_method_unique_name
    for ctor in cls.constructors:
        if ctor.skip or not ctor.parameters:
            continue
        args = _call_arg_exprs(ctor, type_map)
        if args is None:
            continue
        name = get_method_unique_name(ctor)
        return [f"\tvar _o := {wname}.{name}({', '.join(args)})"]
    return [f"\tvar _o := {wname}.new()"]


def _call_arg_exprs(method: MethodDecl, type_map: TypeMap, depth: int = 0) -> list[str] | None:
    """Argument expressions for a bound method, omitting absorbed stream params.

    Returns None if any parameter has no GDScript expression (the method is
    then skipped by the caller)."""
    args: list[str] = []
    for p in method.parameters:
        if p.type.base_name == "Standard_OStream":
            continue  # absorbed ostream: not exposed to GDScript
        if p.type.base_name in ("Standard_IStream", "Standard_SStream"):
            args.append('"s"')  # bound as a String param
            continue
        expr = _arg_expr(p.type, type_map, depth)
        if expr is None:
            return None
        args.append(expr)
    return args


def _method_call_lines(cls: ClassDecl, type_map: TypeMap) -> list[str]:
    """Return GDScript statement lines that exercise every bound method of cls."""
    from classify.overloads import get_method_unique_name
    lines: list[str] = []
    for m in cls.all_wrappable_methods:
        if m.kind.name in ("CONSTRUCTOR",):
            continue  # handled below (only non-default constructors)
        name = get_method_unique_name(m)
        if name in _SWEEP_SKIP_BY_CLASS.get(cls.name, set()):
            continue
        args = _call_arg_exprs(m, type_map)
        if args is None:
            continue
        name = get_method_unique_name(m)
        argstr = ", ".join(args)
        lines.append(f'\tif _ocg_verbose: print("SWEEP {cls.name}.{name}")')
        if m.kind.name == "STATIC_METHOD":
            if _needs_callv(name):
                lines.append(f"\t{wrapper_ref(cls)}.callv({name!r}, [{argstr}])")
            else:
                lines.append(f"\t{wrapper_ref(cls)}.{name}({argstr})")
        else:
            if _needs_callv(name):
                lines.append(f"\t_o.callv({name!r}, [{argstr}])")
            else:
                lines.append(f"\t_o.{name}({argstr})")
    # Non-default constructors (factories).
    for ctor in cls.constructors:
        if ctor.skip or len(ctor.parameters) == 0:
            continue
        if cls.has_pure_virtual:
            continue
        args = _call_arg_exprs(ctor, type_map)
        if args is None:
            continue
        name = get_method_unique_name(ctor)
        argstr = ", ".join(args)
        lines.append(f'\tif _ocg_verbose: print("SWEEP {cls.name}.ctor.{name}")')
        if _needs_callv(name):
            lines.append(f"\t{wrapper_ref(cls)}.callv({name!r}, [{argstr}])")
        else:
            lines.append(f"\t{wrapper_ref(cls)}.{name}({argstr})")
    return lines


def _prop_call_lines(cls: ClassDecl, type_map: TypeMap) -> list[str]:
    lines: list[str] = []
    for p in plan_properties(cls, type_map):
        setter_value = _prop_setter_expr(p.prop_type, type_map)
        lines.append(f'\tif _ocg_verbose: print("SWEEP {cls.name}.prop.{p.name}")')
        lines.append(f"\t_ocg_ignore = _o.{p.name}")
        if setter_value is not None:
            lines.append(f"\t_o.{p.name} = {setter_value}")
    return lines


def _prop_setter_expr(prop_type: str, type_map: TypeMap) -> str | None:
    if prop_type == "Variant::INT":
        return "1"
    if prop_type == "Variant::FLOAT":
        return "1.0"
    if prop_type == "Variant::BOOL":
        return "true"
    if prop_type == "Variant::STRING":
        return '"s"'
    if prop_type == "Variant::OBJECT":
        return "null"
    return None


def wrapper_ref(cls: ClassDecl) -> str:
    return cls.wrapper_name


def generate_sweep_suite(module: ModuleDecl, type_map: TypeMap) -> str:
    """Generate the GDScript source of one sweep suite for a module."""
    classes = sorted((c for c in module.classes if c.wrapper_name), key=lambda c: c.wrapper_name)
    header = (
        "# Auto-generated sweep suite for module %s -- DO NOT EDIT\n"
        "# Exercises every bound method of every wrapped class in this module.\n"
        "# Generated by OpenCASCADE.gd-autowrapper.\n"
        "extends Node\n\n\n"
    ) % module.name

    funcs: list[str] = []
    for cls in classes:
        call_lines = _method_call_lines(cls, type_map)
        prop_lines = _prop_call_lines(cls, type_map)
        if not call_lines and not prop_lines:
            continue
        body = []
        body.append("\tvar _ocg_verbose := OS.get_environment(\"OCG_SWEEP_VERBOSE\") == \"true\"")
        body.extend(_instance_construction_lines(cls, type_map))
        body.append('\tif _o == null: return "OK"  # no live instance could be built; nothing to sweep')
        body.append("\tvar _ocg_ignore: Variant = null")
        body.extend(call_lines)
        body.extend(prop_lines)
        body.append('\treturn "OK"')
        funcs.append("func test_{name}() -> String:\n{body}\n".format(
            name=cls.wrapper_name, body="\n".join(body)))
    return header + "\n\n".join(funcs) + "\n"
