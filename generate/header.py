"""Generate .hpp wrapper headers."""

from __future__ import annotations

from model import ClassDecl, ClassKind, MethodDecl, MethodKind, OCCTType
from classify.overloads import get_method_unique_name
from generate.type_map import TypeMap


def generate_header(cls: ClassDecl, type_map: TypeMap) -> str:
    """Generate the .hpp header for a wrapper class."""
    lines = []
    wname = cls.wrapper_name
    cname = cls.name

    lines.append("// Auto-generated wrapper for {} — DO NOT EDIT".format(cname))
    lines.append("#pragma once")
    lines.append("")
    lines.append("#include <godot_cpp/classes/ref_counted.hpp>")
    lines.append("#include <godot_cpp/classes/ref.hpp>")
    lines.append("#include <godot_cpp/core/class_db.hpp>")
    lines.append("")
    lines.append("#ifdef __GNUC__")
    lines.append("#pragma GCC diagnostic ignored \"-Wchanges-meaning\"")
    lines.append("#endif")
    lines.append("")

    # Include the class's own OCCT header. All transitive dependencies are
    # satisfied by the force-included occt_compat.hxx (build system).
    header_basename = cls.header_file.rsplit("/", 1)[-1] if "/" in cls.header_file else cls.header_file
    if header_basename:
        lines.append("#include <{}>".format(header_basename))

    # Forward-declare all referenced wrapper types (Ref<T> only needs forward decl)
    referenced = set()
    for method in cls.all_wrappable_methods:
        if method.return_type and method.return_type.base_name in type_map._wrapper_names:
            if method.return_type.base_name != cname:
                referenced.add(method.return_type.base_name)
        for p in method.parameters:
            if p.type.base_name in type_map._wrapper_names:
                if p.type.base_name != cname:
                    referenced.add(p.type.base_name)

    lines.append("")
    lines.append("namespace godot {")
    lines.append("")

    if referenced:
        lines.append("// Forward declarations")
    for ref in sorted(referenced):
        wname_ref = type_map.wrapper_name(ref)
        if wname_ref:
            lines.append("class {};".format(wname_ref))
    if referenced:
        lines.append("")
    lines.append("")
    lines.append("class {} : public RefCounted {{".format(wname))
    lines.append("    GDCLASS({}, RefCounted)".format(wname))
    lines.append("")
    lines.append("public:")

    # Native storage
    if cls.kind == ClassKind.BUILDER:
        lines.append("    // Builder stores result after Build()")
        lines.append("    TopoDS_Shape _result;")
        lines.append("    std::unique_ptr<{}> _builder;".format(cname))
    elif cls.kind == ClassKind.TOPODS_SHAPE:
        lines.append("    {} _native;".format(cname))
    elif cls.kind == ClassKind.VALUE:
        lines.append("    {} _native;".format(cname))
    elif cls.kind == ClassKind.REF_COUNTED:
        lines.append("    opencascade::handle<{}> _handle;".format(cname))
    else:
        lines.append("    {} _native;".format(cname))

    lines.append("")
    lines.append("    static void _bind_methods();")
    lines.append("")

    # Default constructor (mandatory for GDCLASS)
    lines.append("    {}();".format(wname))
    lines.append("")

    # Static factory methods for constructors
    has_ctors = False
    for ctor in cls.constructors:
        if ctor.skip or len(ctor.parameters) == 0:
            continue
        has_ctors = True
        unique = get_method_unique_name(ctor)
        params = _gen_param_list(ctor, type_map)
        lines.append("    static Ref<{}> {}({});".format(wname, unique, params))

    if has_ctors:
        lines.append("")

    # Regular methods
    has_methods = False
    for method in cls.methods:
        if method.skip:
            continue
        has_methods = True
        unique = get_method_unique_name(method)
        ret = type_map.cpp_type_for_return(method.return_type)
        params = _gen_param_list(method, type_map)
        const = " const" if method.is_const else ""
        lines.append("    {} {}({}){};".format(ret, unique, params, const))

    if has_methods:
        lines.append("")

    # Operators
    has_ops = False
    for op in cls.operators:
        if op.skip:
            continue
        has_ops = True
        unique = get_method_unique_name(op)
        ret = type_map.cpp_type_for_return(op.return_type)
        params = _gen_param_list(op, type_map)
        const = " const" if op.is_const else ""
        lines.append("    {} {}({}){};".format(ret, unique, params, const))

    if has_ops:
        lines.append("")

    # Static methods
    has_static = False
    for sm in cls.static_methods:
        if sm.skip:
            continue
        has_static = True
        unique = get_method_unique_name(sm)
        ret = type_map.cpp_type_for_return(sm.return_type)
        params = _gen_param_list(sm, type_map)
        lines.append("    static {} {}({});".format(ret, unique, params))

    if has_static:
        lines.append("")

    # Nested enums as static constants
    for enum in cls.nested_enums:
        for val in enum.values:
            const_name = "{}_{}".format(enum.name, val.name)
            lines.append("    static constexpr int64_t {} = static_cast<int64_t>({}::{}::{});".format(
                const_name, cname, enum.name, val.name))

    lines.append("};")
    lines.append("")
    lines.append("} // namespace godot")

    return "\n".join(lines) + "\n"


def _gen_param_list(method: MethodDecl, type_map: TypeMap) -> str:
    """Generate a C++ parameter list for the method signature."""
    parts = []
    for p in method.parameters:
        ctype = type_map.cpp_type_for_param(p.type)
        name = p.name or "arg"
        parts.append("{} {}".format(ctype, name))
    return ", ".join(parts)
