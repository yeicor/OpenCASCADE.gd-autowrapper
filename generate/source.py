"""Generate .cpp wrapper source files."""

from __future__ import annotations

from model import ClassDecl, ClassKind, MethodDecl, MethodKind, FieldDecl
from classify.overloads import get_method_unique_name
from generate.type_map import TypeMap, PRIMITIVE_MAP, _PRIMITIVE_WRAPPER_MAP, PRIMITIVE_WRAPPER_CPP_TYPE, COLLECTION_TYPES, HANDLE_COLLECTION_TYPES


def _has_string_member(cls: ClassDecl) -> bool:
    return any(m.name == "String" for m in cls.methods)


def _qstr(cls: ClassDecl | None, t: str) -> str:
    """Qualify 'String' as '::godot::String' if the class hides it."""
    if cls is not None and _has_string_member(cls) and t == "String":
        return "::godot::String"
    return t


def _null_check_for_refcounted(lines: list[str], ret_type: str) -> None:
    """Generate a null check for _handle (REF_COUNTED classes without default ctor)."""
    if ret_type == "void":
        lines.append("    ERR_FAIL_COND(!_handle);")
    else:
        lines.append("    ERR_FAIL_COND_V(!_handle, {}());".format(ret_type))


def _null_check_for_builder(lines: list[str], ret_type: str) -> None:
    """Generate a null check for _builder (BUILDER classes without default ctor)."""
    if ret_type == "void":
        lines.append("    ERR_FAIL_NULL(_builder);")
    else:
        lines.append("    ERR_FAIL_NULL_V(_builder, {}());".format(ret_type))


def _null_check_for_native_ptr(lines: list[str], ret_type: str) -> None:
    """Generate a null check for `_native` when stored as unique_ptr."""
    if ret_type == "void":
        lines.append("    ERR_FAIL_NULL(_native);")
    else:
        lines.append("    ERR_FAIL_NULL_V(_native, {}());".format(ret_type))


def generate_primitive_wrappers_header() -> str:
    """Generate the header file containing primitive wrapper classes.

    These are lightweight wrapper classes used for non-const ref output parameters
    of primitive types (e.g. Standard_Real&, Standard_Integer&).
    """
    # Mapping for types that godot-cpp can't bind directly — use int32_t in
    # get_value/set_value signatures instead.
    _UNBINDABLE = {"char": "int32_t"}

    lines = []
    lines.append("// Auto-generated primitive wrapper classes for non-const ref output params -- DO NOT EDIT")
    lines.append("#pragma once")
    lines.append("")
    lines.append("#include <godot_cpp/classes/ref_counted.hpp>")
    lines.append("#include <godot_cpp/core/class_db.hpp>")
    lines.append("")
    lines.append("using namespace godot;")
    lines.append("")

    # Generate a wrapper class for each primitive wrapper type
    for wrapper_name, cpp_type in sorted(PRIMITIVE_WRAPPER_CPP_TYPE.items()):
        bind_type = _UNBINDABLE.get(cpp_type, cpp_type)
        cast_to_bind = "(int32_t)" if bind_type != cpp_type else ""
        cast_from_bind = "static_cast<{}>({})".format(cpp_type, "v") if bind_type != cpp_type else "v"

        lines.append("class {} : public RefCounted {{".format(wrapper_name))
        lines.append("    GDCLASS({}, RefCounted)".format(wrapper_name))
        lines.append("public:")
        lines.append("    {} _native;".format(cpp_type))
        lines.append("")
        lines.append("    {}() : RefCounted(), _native() {{}}".format(wrapper_name))
        lines.append("")
        lines.append("    {} get_value() const {{ return {}_native; }}".format(bind_type, cast_to_bind))
        lines.append("    void set_value({} v) {{ _native = {}; }}".format(bind_type, cast_from_bind))
        lines.append("")
        lines.append("protected:")
        lines.append("    static void _bind_methods() {")
        lines.append('        ClassDB::bind_method(D_METHOD("get_value"), &{}::get_value);'.format(wrapper_name))
        lines.append('        ClassDB::bind_method(D_METHOD("set_value", "value"), &{}::set_value);'.format(wrapper_name))
        lines.append("    }")
        lines.append("};")
        lines.append("")

    return "\n".join(lines) + "\n"


def generate_collection_wrappers_header() -> str:
    """Generate OcgCollectionWrappers.hpp with opaque RefCounted wrapper classes
    for NCollection-based typedefs (lists, sequences, arrays, maps).

    These types are typedefs of NCollection templates and cannot be extracted
    by libclang's class cursor.  They're wrapped as opaque RefCounted value
    types: the OCCT native type is stored in _native.

    Also generates handle-based wrappers for deprecated HSequence/HArray types
    that inherit from Standard_Transient (stored in _handle).
    """
    from model import occt_name_to_wrapper
    from generate.type_map import COLLECTION_TYPES, HANDLE_COLLECTION_TYPES

    lines = []
    lines.append("// Auto-generated opaque collection wrapper classes -- DO NOT EDIT")
    lines.append("#pragma once")
    lines.append("")
    lines.append("#include <godot_cpp/classes/ref_counted.hpp>")
    lines.append("#include <godot_cpp/core/class_db.hpp>")
    lines.append("")
    lines.append("#ifdef __GNUC__")
    lines.append("#pragma GCC diagnostic ignored \"-Wchanges-meaning\"")
    lines.append("#pragma GCC diagnostic ignored \"-Wdeprecated-declarations\"")
    lines.append("#pragma GCC diagnostic ignored \"-Wunused-parameter\"")
    lines.append("#endif")
    lines.append("")
    lines.append("using namespace godot;")
    lines.append("")

    # Include headers for value-type collections
    for occt_name, (cpp_type, include) in sorted(COLLECTION_TYPES.items()):
        lines.append("#include {}".format(include))
    # Include headers for handle-type collections
    for occt_name, (cpp_type, include) in sorted(HANDLE_COLLECTION_TYPES.items()):
        lines.append("#include {}".format(include))
    lines.append("")

    # Generate value-type wrappers (_native storage)
    for occt_name, (cpp_type, include) in sorted(COLLECTION_TYPES.items()):
        wname = occt_name_to_wrapper(occt_name, "")
        lines.append("class {} : public RefCounted {{".format(wname))
        lines.append("    GDCLASS({}, RefCounted)".format(wname))
        lines.append("")
        lines.append("public:")
        lines.append("    {} _native;".format(cpp_type))
        lines.append("")
        lines.append("    {}() = default;".format(wname))
        lines.append("};")
        lines.append("")

    # Generate handle-type wrappers (_handle storage)
    for occt_name, (cpp_type, include) in sorted(HANDLE_COLLECTION_TYPES.items()):
        wname = occt_name_to_wrapper(occt_name, "")
        lines.append("class {} : public RefCounted {{".format(wname))
        lines.append("    GDCLASS({}, RefCounted)".format(wname))
        lines.append("")
        lines.append("public:")
        lines.append("    opencascade::handle<{}> _handle;".format(cpp_type))
        lines.append("")
        lines.append("    {}() = default;".format(wname))
        lines.append("};")
        lines.append("")

    return "\n".join(lines) + "\n"


def generate_source(cls: ClassDecl, type_map: TypeMap) -> str:
    """Generate the .cpp source for a wrapper class."""
    lines = []
    wname = cls.wrapper_name
    cname = cls.name

    lines.append("// Auto-generated wrapper for {} -- DO NOT EDIT".format(cname))
    lines.append('#include "{}.hpp"'.format(wname))
    lines.append("")

    # Suppress warnings from OCCT headers that we can't control
    lines.append("#ifdef __GNUC__")
    lines.append("#pragma GCC diagnostic ignored \"-Wdeprecated-declarations\"")
    lines.append("#pragma GCC diagnostic ignored \"-Wunused-parameter\"")
    lines.append("#endif")
    lines.append("")

    # Include headers for all referenced wrapper types (needed for _native access in impl).
    # Collection and handle collection types live in OcgCollectionWrappers.hpp (included via .hpp),
    # so skip individual includes for those. However, if a COLLECTION_OR_HANDLE type resolves
    # to a real scanned class via HANDLE_ALIASES (e.g. Prs3d_Presentation → Graphic3d_Structure),
    # we must include that real class's header too, because the generated code uses the resolved name.
    from generate.type_map import COLLECTION_TYPES, HANDLE_COLLECTION_TYPES
    from classify.skippable import _resolve_handle_inner
    COLLECTION_OR_HANDLE = set(COLLECTION_TYPES.keys()) | set(HANDLE_COLLECTION_TYPES.keys())
    referenced = set()
    needs_sstream = False
    for method in cls.all_wrappable_methods:
        if method.return_type and method.return_type.base_name in type_map._wrapper_names:
            if method.return_type.base_name != cname and method.return_type.base_name not in COLLECTION_OR_HANDLE:
                referenced.add(method.return_type.base_name)
            elif method.return_type.base_name in COLLECTION_OR_HANDLE:
                resolved = _resolve_handle_inner(method.return_type.base_name)
                if resolved != method.return_type.base_name and resolved in type_map._wrapper_names:
                    referenced.add(resolved)
        for p in method.parameters:
            if p.type.base_name in type_map._wrapper_names:
                if p.type.base_name != cname and p.type.base_name not in COLLECTION_OR_HANDLE:
                    referenced.add(p.type.base_name)
            # For collection/handle types that alias a real scanned class, include the real class header
            if p.type.base_name in COLLECTION_OR_HANDLE:
                resolved = _resolve_handle_inner(p.type.base_name)
                if resolved != p.type.base_name and resolved in type_map._wrapper_names:
                    referenced.add(resolved)
            if p.type.base_name in ("Standard_OStream", "Standard_IStream"):
                needs_sstream = True
    for ref in sorted(referenced):
        wname_ref = type_map.wrapper_name(ref)
        if wname_ref:
            lines.append('#include "{}.hpp"'.format(wname_ref))

    lines.append("")
    if needs_sstream:
        lines.append("#include <sstream>")
        lines.append("")
    lines.append("#include <godot_cpp/core/error_macros.hpp>")
    lines.append("")
    lines.append("namespace godot {")
    lines.append("")

    # --- _bind_methods ---
    lines.append("void {}::_bind_methods() {{".format(wname))

    # Bind static factory methods for constructors
    for ctor in cls.constructors:
        if ctor.skip or len(ctor.parameters) == 0:
            continue
        unique = get_method_unique_name(ctor)
        bind_params = _gen_bind_params(ctor, unique)
        lines.append('    ClassDB::bind_static_method("{}", D_METHOD({}), &{}::{});'.format(
            wname, bind_params, wname, unique))

    # Bind regular methods
    for method in cls.methods:
        if method.skip:
            continue
        unique = get_method_unique_name(method)
        bind_params = _gen_bind_params(method, unique)
        lines.append('    ClassDB::bind_method(D_METHOD({}), &{}::{});'.format(
            bind_params, wname, unique))

    # Bind operators
    for op in cls.operators:
        if op.skip:
            continue
        unique = get_method_unique_name(op)
        bind_params = _gen_bind_params(op, unique)
        lines.append('    ClassDB::bind_method(D_METHOD({}), &{}::{});'.format(
            bind_params, wname, unique))

    # Bind static methods
    for sm in cls.static_methods:
        if sm.skip:
            continue
        unique = get_method_unique_name(sm)
        bind_params = _gen_bind_params(sm, unique)
        lines.append('    ClassDB::bind_static_method("{}", D_METHOD({}), &{}::{});'.format(
            wname, bind_params, wname, unique))

    # Bind nested enums as integer constants
    for enum in cls.nested_enums:
        for val in enum.values:
            const_name = "{}_{}".format(enum.name, val.name)
            lines.append('    ClassDB::bind_integer_constant(get_class_static(), "", "{}", static_cast<int64_t>({}::{}));'.format(
                const_name, wname, const_name))

    lines.append("}")
    lines.append("")

    # --- Default constructor ---
    lines.append("{}::{}() : RefCounted() {{".format(wname, wname))

    # Check if the class has a zero-arg constructor
    has_default_ctor = any(len(c.parameters) == 0 for c in cls.constructors)

    if cls.kind == ClassKind.BUILDER:
        if has_default_ctor:
            lines.append("    _builder = std::make_unique<{}>();".format(cname))
            lines.append("    _builder->Build();")
            lines.append("    _result = _builder->Shape();")
        else:
            lines.append("    // No default constructor — _builder is null; use factory methods")
    elif cls.kind == ClassKind.REF_COUNTED:
        if has_default_ctor and not cls.has_pure_virtual:
            lines.append("    _handle = new ::{}();".format(cname))
        else:
            lines.append("    // No default constructor — _handle is null; use factory methods")
    elif cls.kind in (ClassKind.VALUE, ClassKind.TOPODS_SHAPE):
        if has_default_ctor:
            # Rely on implicit member default construction — avoids copy assignment issues
            # (some OCCT classes like AIS_ViewController have deleted operator=)
            pass
        else:
            lines.append("    // No default constructor — use factory methods")
    else:
        if has_default_ctor:
            pass  # implicit default construction
        else:
            lines.append("    // No default constructor — use factory methods")

    lines.append("}")
    lines.append("")

    # --- Constructor factory methods ---
    for ctor in cls.constructors:
        if ctor.skip or len(ctor.parameters) == 0:
            continue
        if cls.has_pure_virtual:
            continue  # Abstract class: skip factory methods (can't instantiate)

        unique = get_method_unique_name(ctor)
        params = _gen_param_list(ctor, type_map, cls=cls)
        args = _occt_args_for_call(ctor, type_map, cname)

        lines.append("Ref<{}> {}::{}({}) {{".format(wname, wname, unique, params))
        lines.append("    Ref<{}> ref; ref.instantiate();".format(wname))

        if cls.kind == ClassKind.BUILDER:
            lines.append("    ref->_builder = std::make_unique<::{}>({});".format(cname, args))
            lines.append("    ref->_builder->Build();")
            lines.append("    ref->_result = ref->_builder->Shape();")
        elif cls.kind == ClassKind.REF_COUNTED:
            # Use :: prefix to avoid name collision with static factory method of same name
            lines.append("    ref->_handle = new ::{}({});".format(cname, args))
        elif cls.has_public_default_ctor:
            # Placement new avoids copy assignment (some OCCT classes delete operator=)
            lines.append("    new (&ref->_native) ::{}({});".format(cname, args))
        else:
            # No default constructor: use smart pointer
            lines.append("    ref->_native = std::make_unique<::{}>({});".format(cname, args))

        lines.append("    return ref;")
        lines.append("}")
        lines.append("")

    # --- Regular methods ---
    for method in cls.methods:
        if method.skip:
            continue
        _gen_method_impl(lines, method, cls, type_map)

    # --- Operator methods ---
    for op in cls.operators:
        if op.skip:
            continue
        _gen_operator_impl(lines, op, cls, type_map)

    # --- Static methods ---
    for sm in cls.static_methods:
        if sm.skip:
            continue
        _gen_static_impl(lines, sm, cls, type_map)

    lines.append("} // namespace godot")
    return "\n".join(lines) + "\n"


def _value_type_target(cls: ClassDecl) -> str:
    """Get the method call target for value types (uses . not ->)."""
    if cls.kind == ClassKind.BUILDER:
        return "_builder.get()"
    elif cls.kind == ClassKind.REF_COUNTED:
        return "_handle.get()"
    elif cls.kind == ClassKind.VALUE:
        return "_native"  # value type, use .
    else:
        return "_native"


def _needs_pointer_call(cls: ClassDecl) -> bool:
    """Check if method calls need -> (pointer) vs . (direct)."""
    if cls.kind in (ClassKind.BUILDER, ClassKind.REF_COUNTED):
        return True
    if not cls.has_public_default_ctor:
        return True  # unique_ptr storage needs ->
    return False


def _native_call_expr(target: str, arrow: str, method: MethodDecl, args: str) -> str:
    """Generate the C++ expression for calling an OCCT method/operator on the native object."""
    name = method.name
    if method.operator_type is not None:
        # Always use operatorNAME() syntax — works for both value and pointer targets
        return "{}{}operator{}({})".format(target, arrow, name, args)
    return "{}{}{}({})".format(target, arrow, name, args)


def _has_ostream_param(method: MethodDecl) -> bool:
    """Check if method has a Standard_OStream output parameter."""
    return any(p.type.base_name == "Standard_OStream" for p in method.parameters)

def _has_istream_param(method: MethodDecl) -> bool:
    """Check if method has a Standard_IStream input parameter."""
    return any(p.type.base_name == "Standard_IStream" for p in method.parameters)


def _gen_method_impl(lines: list[str], method: MethodDecl, cls: ClassDecl, type_map: TypeMap):
    """Generate implementation for a regular method."""
    from classify.skippable import _resolve_handle_inner
    wname = cls.wrapper_name
    unique = get_method_unique_name(method)
    ret = type_map.cpp_type_for_return(method.return_type)
    has_ostream = _has_ostream_param(method)
    has_istream = _has_istream_param(method)
    if has_ostream and (method.return_type is None or method.return_type.is_void):
        ret = "String"
    ret = _qstr(cls, ret)
    params = _gen_param_list(method, type_map, cls=cls)
    const = " const" if method.is_const else ""
    args = _occt_args_for_call(method, type_map, cls.name)

    lines.append("{} {}::{}({}){} {{".format(ret, wname, unique, params, const))

    if cls.kind == ClassKind.REF_COUNTED:
        _null_check_for_refcounted(lines, ret)
    elif cls.kind == ClassKind.BUILDER:
        _null_check_for_builder(lines, ret)
    elif not cls.has_public_default_ctor:
        _null_check_for_native_ptr(lines, ret)

    # Create stream variables
    if has_ostream:
        lines.append("    std::ostringstream ocg_os;")
    if has_istream:
        lines.append("    std::istringstream ocg_is({}.utf8().get_data());".format(
            next(p.name for p in method.parameters if p.type.base_name == "Standard_IStream")))

    target = _value_type_target(cls)
    arrow = "->" if _needs_pointer_call(cls) else "."
    call = _native_call_expr(target, arrow, method, args)

    # Generate call
    if method.return_type and not method.return_type.is_void:
        ret_base_orig = method.return_type.base_name
        # Resolve handle aliases (e.g. Prs3d_Presentation → Graphic3d_Structure)
        ret_base = ret_base_orig
        if method.return_type.is_handle and ret_base in HANDLE_COLLECTION_TYPES:
            ret_base = _resolve_handle_inner(ret_base)
        # const char* / Standard_CString return → wrap in String()
        if method.return_type.is_pointer and ret_base_orig in ("char", "Standard_CString"):
            lines.append("    return ::godot::String({});".format(call))
        elif ret_base_orig in ("TCollection_AsciiString",):
            lines.append("    return ::godot::String({}.ToCString());".format(call))
        elif type_map._is_enum(ret_base):
            lines.append("    return static_cast<{}>({});".format(ret, call))
        elif method.return_type.is_handle and type_map.wrapper_name(ret_base):
            wret = type_map.wrapper_name(ret_base)
            # Handle return: opencascade::handle<T> -> Ref<OcgT>
            lines.append("    auto result = {};".format(call))
            lines.append("    Ref<{}> wrapper; wrapper.instantiate();".format(wret))
            ret_kind = type_map.class_kind(ret_base)
            if ret_kind == ClassKind.REF_COUNTED or ret_base_orig in HANDLE_COLLECTION_TYPES:
                lines.append("    wrapper->_handle = result;")
            else:
                # VALUE/TOPODS_SHAPE: dereference handle into native storage
                lines.append("    wrapper->_native = *result.get();")
            lines.append("    return wrapper;")
        elif type_map.wrapper_name(ret_base):
            wret = type_map.wrapper_name(ret_base)
            ref = "&" if method.return_type.is_ref and not method.return_type.is_const else ""
            lines.append("    auto{} result = {};".format(ref, call))
            lines.append("    Ref<{}> wrapper; wrapper.instantiate();".format(wret))
            if ret_base_orig in HANDLE_COLLECTION_TYPES:
                lines.append("    wrapper->_handle = result;")
            elif type_map.is_refcounted(ret_base):
                lines.append("    wrapper->_handle = opencascade::handle<::{}>(&result);".format(ret_base))
            elif not type_map.has_public_default_ctor(ret_base):
                lines.append("    wrapper->_native = std::make_unique<::{}>(result);".format(ret_base))
            else:
                lines.append("    wrapper->_native = result;")
            lines.append("    return wrapper;")
        elif has_ostream:
            lines.append("    return {};".format(call))
        else:
            lines.append("    return {};".format(call))
    else:
        lines.append("    {};".format(call))
        if has_ostream:
            lines.append("    return ::godot::String(ocg_os.str().c_str());")

    lines.append("}")
    lines.append("")


def _gen_operator_impl(lines: list[str], method: MethodDecl, cls: ClassDecl, type_map: TypeMap):
    """Generate implementation for an operator method."""
    from classify.skippable import _resolve_handle_inner
    wname = cls.wrapper_name
    unique = get_method_unique_name(method)
    ret = type_map.cpp_type_for_return(method.return_type)
    has_ostream = _has_ostream_param(method)
    has_istream = _has_istream_param(method)
    if has_ostream and (method.return_type is None or method.return_type.is_void):
        ret = "String"
    ret = _qstr(cls, ret)
    params = _gen_param_list(method, type_map, cls=cls)
    const = " const" if method.is_const else ""
    args = _occt_args_for_call(method, type_map, cls.name)

    lines.append("{} {}::{}({}){} {{".format(ret, wname, unique, params, const))

    if cls.kind == ClassKind.REF_COUNTED:
        _null_check_for_refcounted(lines, ret)
    elif cls.kind == ClassKind.BUILDER:
        _null_check_for_builder(lines, ret)
    elif not cls.has_public_default_ctor:
        _null_check_for_native_ptr(lines, ret)

    if has_ostream:
        lines.append("    std::ostringstream ocg_os;")
    if has_istream:
        lines.append("    std::istringstream ocg_is({}.utf8().get_data());".format(
            next(p.name for p in method.parameters if p.type.base_name == "Standard_IStream")))

    target = _value_type_target(cls)
    arrow = "->" if _needs_pointer_call(cls) else "."
    call = _native_call_expr(target, arrow, method, args)

    if method.return_type and not method.return_type.is_void:
        ret_base_orig = method.return_type.base_name
        ret_base = _resolve_handle_inner(ret_base_orig) if ret_base_orig in HANDLE_COLLECTION_TYPES else ret_base_orig
        if method.return_type.is_pointer and ret_base_orig in ("char", "Standard_CString"):
            lines.append("    return ::godot::String({});".format(call))
        elif ret_base_orig in ("TCollection_AsciiString",):
            lines.append("    return ::godot::String({}.ToCString());".format(call))
        elif type_map._is_enum(ret_base):
            lines.append("    return static_cast<{}>({});".format(ret, call))
        elif method.return_type.is_handle and type_map.wrapper_name(ret_base):
            wret = type_map.wrapper_name(ret_base)
            lines.append("    auto result = {};".format(call))
            lines.append("    Ref<{}> wrapper; wrapper.instantiate();".format(wret))
            ret_kind = type_map.class_kind(ret_base)
            if ret_kind == ClassKind.REF_COUNTED or ret_base_orig in HANDLE_COLLECTION_TYPES:
                lines.append("    wrapper->_handle = result;")
            else:
                lines.append("    wrapper->_handle = result;")
            lines.append("    return wrapper;")
        elif type_map.wrapper_name(ret_base):
            wret = type_map.wrapper_name(ret_base)
            lines.append("    auto result = {};".format(call))
            lines.append("    Ref<{}> wrapper; wrapper.instantiate();".format(wret))
            if ret_base_orig in HANDLE_COLLECTION_TYPES:
                lines.append("    wrapper->_handle = result;")
            elif not type_map.has_public_default_ctor(ret_base):
                lines.append("    wrapper->_native = std::make_unique<::{}>(result);".format(ret_base))
            else:
                lines.append("    wrapper->_native = result;")
            lines.append("    return wrapper;")
        elif has_ostream:
            lines.append("    return {};".format(call))
        else:
            lines.append("    return {};".format(call))
    else:
        lines.append("    {};".format(call))
        if has_ostream:
            lines.append("    return ::godot::String(ocg_os.str().c_str());")

    lines.append("}")
    lines.append("")


def _gen_static_impl(lines: list[str], method: MethodDecl, cls: ClassDecl, type_map: TypeMap):
    """Generate implementation for a static method."""
    from classify.skippable import _resolve_handle_inner
    wname = cls.wrapper_name
    unique = get_method_unique_name(method)
    ret = type_map.cpp_type_for_return(method.return_type)
    has_ostream = _has_ostream_param(method)
    has_istream = _has_istream_param(method)
    if has_ostream and (method.return_type is None or method.return_type.is_void):
        ret = "String"
    ret = _qstr(cls, ret)
    params = _gen_param_list(method, type_map, cls=cls)
    args = _occt_args_for_call(method, type_map, cls.name)

    lines.append("{} {}::{}({}) {{".format(ret, wname, unique, params))

    if has_ostream:
        lines.append("    std::ostringstream ocg_os;")
    if has_istream:
        lines.append("    std::istringstream ocg_is({}.utf8().get_data());".format(
            next(p.name for p in method.parameters if p.type.base_name == "Standard_IStream")))

    static_call = "::{}::{}({})".format(cls.name, method.name, args)

    if method.return_type and not method.return_type.is_void:
        ret_base_orig = method.return_type.base_name
        ret_base = _resolve_handle_inner(ret_base_orig) if ret_base_orig in HANDLE_COLLECTION_TYPES else ret_base_orig
        if method.return_type.is_pointer and ret_base_orig in ("char", "Standard_CString"):
            lines.append("    return ::godot::String({});".format(static_call))
        elif ret_base_orig in ("TCollection_AsciiString",):
            lines.append("    return ::godot::String({}.ToCString());".format(static_call))
        elif type_map._is_enum(ret_base):
            lines.append("    return static_cast<{}>({});".format(ret, static_call))
        elif method.return_type.is_handle and type_map.wrapper_name(ret_base):
            wret = type_map.wrapper_name(ret_base)
            lines.append("    auto result = {};".format(static_call))
            lines.append("    Ref<{}> wrapper; wrapper.instantiate();".format(wret))
            if ret_base_orig in HANDLE_COLLECTION_TYPES:
                lines.append("    wrapper->_handle = result;")
            else:
                lines.append("    wrapper->_handle = result;")
            lines.append("    return wrapper;")
        elif type_map.wrapper_name(ret_base):
            wret = type_map.wrapper_name(ret_base)
            lines.append("    auto result = {};".format(static_call))
            lines.append("    Ref<{}> wrapper; wrapper.instantiate();".format(wret))
            if ret_base_orig in HANDLE_COLLECTION_TYPES:
                lines.append("    wrapper->_handle = result;")
            elif type_map.is_refcounted(ret_base):
                lines.append("    wrapper->_handle = opencascade::handle<::{}>(result);".format(ret_base))
            elif not type_map.has_public_default_ctor(ret_base):
                lines.append("    wrapper->_native = std::make_unique<::{}>(result);".format(ret_base))
            else:
                lines.append("    wrapper->_native = result;")
            lines.append("    return wrapper;")
        elif has_ostream:
            lines.append("    return {};".format(static_call))
        else:
            lines.append("    return {};".format(static_call))
    else:
        if has_ostream:
            lines.append("    {};".format(static_call))
            lines.append("    return ::godot::String(ocg_os.str().c_str());")
        else:
            lines.append("    {}::{}({});".format(cls.name, method.name, args))

    lines.append("}")
    lines.append("")


def _occt_args_for_call(method: MethodDecl, type_map: TypeMap, cls_name: str = "") -> str:
    """Build argument list that extracts ._native from wrapped parameters (via Ref<T> ->)."""
    parts = []
    for p in method.parameters:
        base = p.type.base_name
        # Standard_OStream → local ostringstream
        if base == "Standard_OStream":
            if p.type.is_ref:
                parts.append("ocg_os")
            else:
                parts.append("&ocg_os")
            continue
        # Standard_IStream → local istringstream
        if base == "Standard_IStream":
            parts.append("ocg_is")
            continue
        if p.type.is_handle:
            # Handle params need ._handle (ref-counted wrapper) not ._native
            if type_map.is_wrapped(base):
                parts.append("{}->_handle".format(p.name))
            else:
                # Unwrapped handle type — can't convert Ref<T> to handle<T>
                # This method should have been skipped, but if not, pass through
                parts.append(p.name)
        elif p.type.is_ref and not p.type.is_const and not p.type.is_handle:
            # Non-const ref output params: pass mutable reference to wrapper's internal storage
            if type_map.is_wrapped(base):
                if type_map.class_kind(base) == ClassKind.BUILDER:
                    parts.append("*{}->_builder.get()".format(p.name))
                elif type_map.is_refcounted(base) or base in HANDLE_COLLECTION_TYPES:
                    parts.append("*{}->_handle.get()".format(p.name))
                elif not type_map.has_public_default_ctor(base):
                    parts.append("*{}->_native".format(p.name))
                else:
                    parts.append("{}->_native".format(p.name))
            elif base in _PRIMITIVE_WRAPPER_MAP:
                # Primitive wrapper: pass ref->_native
                parts.append("{}->_native".format(p.name))
            else:
                parts.append(p.name)
        elif type_map.is_wrapped(base):
            # BUILDER classes use _builder, not _native
            if type_map.class_kind(base) == ClassKind.BUILDER:
                parts.append("{}->_builder.get()".format(p.name))
            elif type_map.is_refcounted(base) or base in HANDLE_COLLECTION_TYPES:
                # REF_COUNTED classes always use _handle
                if p.type.is_ref:
                    parts.append("*{}->_handle.get()".format(p.name))
                elif p.type.is_pointer:
                    parts.append("{}.get()".format(p.name))
                else:
                    parts.append("*{}->_handle.get()".format(p.name))
            else:
                # VALUE/TOPODS_SHAPE: _native may be T or std::unique_ptr<T>
                if not type_map.has_public_default_ctor(base) and p.type.is_ref:
                    parts.append("*{}->_native".format(p.name))
                else:
                    parts.append("{}->_native".format(p.name))
        elif type_map._is_enum(base):
            enum_qualified = type_map.qualified_enum_name(base, cls_name)
            parts.append("static_cast<{}>({})".format(enum_qualified, p.name))
        elif base == "Standard_CString":
            # godot String -> const char* for OCCT
            parts.append("{}.utf8().get_data()".format(p.name))
        elif p.type.is_pointer and base in ("char",):
            # const char* param → String
            parts.append("{}.utf8().get_data()".format(p.name))
        elif base in ("TCollection_AsciiString", "TCollection_ExtendedString"):
            # godot String → TCollection_{Ascii,Extended}String
            parts.append("{}({}.utf8().get_data())".format(base, p.name))
        elif base in ("char", "Standard_Character"):
            # char/int8_t param → cast from int32_t
            parts.append("static_cast<char>({})".format(p.name))
        else:
            parts.append(p.name)
    return ", ".join(parts)


def _gen_param_list(method: MethodDecl, type_map: TypeMap, *, cls: ClassDecl | None = None) -> str:
    """Generate C++ parameter list for the method signature."""
    parts = []
    for p in method.parameters:
        ctype = type_map.cpp_type_for_param(p.type)
        if not ctype:
            continue  # OStream or absorbed param
        name = p.name or "arg"
        parts.append("{} {}".format(_qstr(cls, ctype), name))
    return ", ".join(parts)


def _gen_bind_params(method: MethodDecl, unique_name: str) -> str:
    """Generate D_METHOD parameters for ClassDB binding.

    First arg is the method name as exposed to GDScript (the unique name).
    """
    parts = ['"{}"'.format(unique_name)]
    for p in method.parameters:
        if p.type.base_name == "Standard_OStream":
            continue  # OStream is not exposed to GDScript
        parts.append('"{}"'.format(p.name))
    return ", ".join(parts)
