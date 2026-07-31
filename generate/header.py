"""Generate .hpp wrapper headers."""

from __future__ import annotations

from model import ClassDecl, ClassKind, MethodDecl, MethodKind, OCCTType
from classify.overloads import get_method_unique_name
from generate.type_map import (TypeMap, _PRIMITIVE_WRAPPER_MAP, COLLECTION_TYPES,
                               HANDLE_COLLECTION_TYPES, SYNTHESIZED_COLLECTION_TYPES)


def _qualify_godot_type(tname: str, shadowed_names: set[str]) -> str:
    """If a name is shadowed by a member of the same name, qualify it."""
    if tname == "String" and "String" in shadowed_names:
        return "::godot::String"
    return tname


def _method_ret_for_header(method: MethodDecl, type_map: TypeMap, shadowed_names: set[str] | None = None) -> str:
    """Get the return type for the wrapper method declaration,
    handling OStream (returns String only for void-return methods) and standard types."""
    has_ostream = any(p.type.base_name == "Standard_OStream" for p in method.parameters)
    if has_ostream and (method.return_type is None or method.return_type.is_void):
        return _qualify_godot_type("String", shadowed_names or set())
    return _qualify_godot_type(type_map.cpp_type_for_return(method.return_type), shadowed_names or set())


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
    lines.append("#include <godot_cpp/variant/string.hpp>")
    lines.append("#include <godot_cpp/core/class_db.hpp>")
    lines.append("")
    lines.append("#ifdef __GNUC__")
    lines.append("#pragma GCC diagnostic ignored \"-Wchanges-meaning\"")
    lines.append("#endif")
    lines.append("")

    def _type_in_set(otype: OCCTType, type_set: set[str]) -> bool:
        """Check if an OCCTType's base_name, canonical spelling, or original spelling
        is in the given set. Handles typedef aliases (e.g. Graphic3d_BndBox3d → BVH_Box<double, 3>)."""
        candidates = [otype.base_name]
        if otype.canonical_spelling:
            c = otype.canonical_spelling.replace("const ", "").rstrip("&").rstrip("*").strip()
            candidates.append(c)
        s = otype.spelling.replace("const ", "").replace("&", "").replace("*", "").strip()
        candidates.append(s)
        return any(c in type_set for c in candidates if c)

    # Check if any methods use primitive wrapper types
    needs_primitive_wrappers = False
    needs_collection_wrappers = False
    for method in cls.all_wrappable_methods:
        # Check return type
        if method.return_type:
            if method.return_type.base_name in _PRIMITIVE_WRAPPER_MAP:
                needs_primitive_wrappers = True
            if _type_in_set(method.return_type, set(COLLECTION_TYPES.keys()) | set(HANDLE_COLLECTION_TYPES.keys())):
                needs_collection_wrappers = True
        # Check parameters
        for p in method.parameters:
            if p.type.base_name in _PRIMITIVE_WRAPPER_MAP:
                needs_primitive_wrappers = True
            if _type_in_set(p.type, set(COLLECTION_TYPES.keys()) | set(HANDLE_COLLECTION_TYPES.keys())):
                needs_collection_wrappers = True

    # Include the class's own OCCT header. All transitive dependencies are
    # satisfied by the force-included occt_compat.hxx (build system).
    header_basename = cls.header_file.rsplit("/", 1)[-1] if "/" in cls.header_file else cls.header_file
    if header_basename:
        lines.append("#include <{}>".format(header_basename))

    # Include primitive wrappers if needed
    if needs_primitive_wrappers:
        lines.append('#include "OcgPrimitiveWrappers.hpp"')

    # Include collection wrappers if needed
    if needs_collection_wrappers:
        lines.append('#include "OcgCollectionWrappers.hpp"')

    # Collect wrapper type names referenced by this class's methods.
    # Resolve handle aliases (e.g. V3d_Light → Graphic3d_CLight) so the
    # forward-declared class matches what cpp_type_for_return / cpp_type_for_param emit.
    # Skip types defined in OcgCollectionWrappers.hpp (already included).
    from classify.skippable import _resolve_handle_inner
    collection_or_handle = set(COLLECTION_TYPES.keys()) | set(HANDLE_COLLECTION_TYPES.keys())
    referenced_wnames: set[str] = set()
    for method in cls.all_wrappable_methods:
        for otype in ([method.return_type] if method.return_type and not method.return_type.is_void else []) + [p.type for p in method.parameters]:
            base = otype.base_name
            # Resolve handle aliases to get the real wrapper class name
            if otype.is_handle:
                base = _resolve_handle_inner(otype.handle_inner)
            # Skip collection/handle-collection types (defined in OcgCollectionWrappers.hpp,
            # which is already included above) — EXCEPT synthesized collections, which are
            # real generated wrapper classes needing a forward declaration. Check the
            # RESOLVED name: the original otype may carry an alias spelling (e.g. V3d_Light)
            # that resolves to a real class.
            if base in collection_or_handle and base not in SYNTHESIZED_COLLECTION_TYPES:
                continue
            if _type_in_set(otype, collection_or_handle - SYNTHESIZED_COLLECTION_TYPES):
                continue
            ref_wname = type_map.wrapper_name(base)
            if ref_wname and ref_wname != type_map.wrapper_name(cname):
                referenced_wnames.add(ref_wname)

    lines.append("")
    lines.append("namespace godot {")
    lines.append("")

    if referenced_wnames:
        lines.append("// Forward declarations")
    for wname_ref in sorted(referenced_wnames):
        lines.append("class {};".format(wname_ref))
    if referenced_wnames:
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
        if cls.has_public_default_ctor:
            lines.append("    {} _native;".format(cname))
        else:
            lines.append("    std::unique_ptr<{}> _native = nullptr;".format(cname))
    elif cls.kind == ClassKind.REF_COUNTED:
        lines.append("    opencascade::handle<{}> _handle;".format(cname))
    else:
        if cls.has_public_default_ctor:
            lines.append("    {} _native;".format(cname))
        else:
            lines.append("    std::unique_ptr<{}> _native = nullptr;".format(cname))

    lines.append("")
    lines.append("    static void _bind_methods();")
    lines.append("")

    # Default constructor (mandatory for GDCLASS)
    lines.append("    {}();".format(wname))
    lines.append("")

    # Collect method names to detect shadowing of godot types (e.g. String)
    shadowed_names: set[str] = set()
    for method in cls.methods:
        if not method.skip:
            shadowed_names.add(get_method_unique_name(method))
    for op in cls.operators:
        if not op.skip:
            shadowed_names.add(get_method_unique_name(op))
    for sm in cls.static_methods:
        if not sm.skip:
            shadowed_names.add(get_method_unique_name(sm))

    # Static factory methods for constructors
    has_ctors = False
    for ctor in cls.constructors:
        if ctor.skip or len(ctor.parameters) == 0:
            continue
        has_ctors = True
        unique = get_method_unique_name(ctor)
        params = _gen_param_list(ctor, type_map, shadowed_names)
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
        ret = _method_ret_for_header(method, type_map, shadowed_names)
        params = _gen_param_list(method, type_map, shadowed_names)
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
        ret = _method_ret_for_header(op, type_map, shadowed_names)
        params = _gen_param_list(op, type_map, shadowed_names)
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
        ret = _method_ret_for_header(sm, type_map, shadowed_names)
        params = _gen_param_list(sm, type_map, shadowed_names)
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


def _gen_param_list(method: MethodDecl, type_map: TypeMap, shadowed_names: set[str] | None = None) -> str:
    """Generate a C++ parameter list for the method signature."""
    parts = []
    for p in method.parameters:
        ctype = type_map.cpp_type_for_param(p.type)
        if not ctype:
            continue  # OStream or absorbed param
        ctype = _qualify_godot_type(ctype, shadowed_names or set())
        name = p.name or "arg"
        parts.append("{} {}".format(ctype, name))
    return ", ".join(parts)
