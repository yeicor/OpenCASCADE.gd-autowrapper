"""Generate .cpp wrapper source files."""

from __future__ import annotations

from model import ClassDecl, ClassKind, MethodDecl, MethodKind
from classify.overloads import get_method_unique_name
from generate.type_map import TypeMap


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

    # Include headers for all referenced wrapper types (needed for _native access in impl)
    referenced = set()
    for method in cls.all_wrappable_methods:
        if method.return_type and method.return_type.base_name in type_map._wrapper_names:
            if method.return_type.base_name != cname:
                referenced.add(method.return_type.base_name)
        for p in method.parameters:
            if p.type.base_name in type_map._wrapper_names:
                if p.type.base_name != cname:
                    referenced.add(p.type.base_name)
    for ref in sorted(referenced):
        wname_ref = type_map.wrapper_name(ref)
        if wname_ref:
            lines.append('#include "{}.hpp"'.format(wname_ref))

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
        lines.append("    _builder = std::make_unique<{}>();".format(cname))
        lines.append("    _builder->Build();")
        lines.append("    _result = _builder->Shape();")
    elif cls.kind == ClassKind.REF_COUNTED:
        if has_default_ctor:
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
        unique = get_method_unique_name(ctor)
        params = _gen_param_list(ctor, type_map)
        args = _occt_args_for_call(ctor, type_map)

        lines.append("Ref<{}> {}::{}({}) {{".format(wname, wname, unique, params))
        lines.append("    Ref<{}> ref; ref.instantiate();".format(wname))

        if cls.kind == ClassKind.BUILDER:
            lines.append("    ref->_builder = std::make_unique<{}>({});".format(cname, args))
            lines.append("    ref->_builder->Build();")
            lines.append("    ref->_result = ref->_builder->Shape();")
        elif cls.kind == ClassKind.REF_COUNTED:
            # Use :: prefix to avoid name collision with static factory method of same name
            lines.append("    ref->_handle = new ::{}({});".format(cname, args))
        else:
            lines.append("    ref->_native = ::{}({});".format(cname, args))

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
    return cls.kind in (ClassKind.BUILDER, ClassKind.REF_COUNTED)


def _native_call_expr(target: str, arrow: str, method: MethodDecl, args: str) -> str:
    """Generate the C++ expression for calling an OCCT method/operator on the native object."""
    name = method.name
    if method.operator_type is not None:
        # Always use operatorNAME() syntax — works for both value and pointer targets
        return "{}{}operator{}({})".format(target, arrow, name, args)
    return "{}{}{}({})".format(target, arrow, name, args)


def _gen_method_impl(lines: list[str], method: MethodDecl, cls: ClassDecl, type_map: TypeMap):
    """Generate implementation for a regular method."""
    wname = cls.wrapper_name
    unique = get_method_unique_name(method)
    ret = type_map.cpp_type_for_return(method.return_type)
    params = _gen_param_list(method, type_map)
    const = " const" if method.is_const else ""
    args = _occt_args_for_call(method, type_map)

    lines.append("{} {}::{}({}){} {{".format(ret, wname, unique, params, const))

    target = _value_type_target(cls)
    arrow = "->" if _needs_pointer_call(cls) else "."
    call = _native_call_expr(target, arrow, method, args)

    # Generate call
    if method.return_type and not method.return_type.is_void:
        ret_base = method.return_type.base_name
        wret = type_map.wrapper_name(ret_base)
        if type_map._is_enum(ret_base):
            lines.append("    return static_cast<{}>({});".format(ret, call))
        elif method.return_type.is_handle and wret:
            # Handle return: opencascade::handle<T> -> Ref<OcgT>
            lines.append("    auto result = {};".format(call))
            lines.append("    Ref<{}> wrapper; wrapper.instantiate();".format(wret))
            lines.append("    wrapper->_handle = result;")
            lines.append("    return wrapper;")
        elif wret:
            # Any wrapped type return (value, toposhape, builder): create Ref<T> + _native
            lines.append("    auto result = {};".format(call))
            lines.append("    Ref<{}> wrapper; wrapper.instantiate();".format(wret))
            lines.append("    wrapper->_native = result;")
            lines.append("    return wrapper;")
        else:
            lines.append("    return {};".format(call))
    else:
        lines.append("    {};".format(call))

    lines.append("}")
    lines.append("")


def _gen_operator_impl(lines: list[str], method: MethodDecl, cls: ClassDecl, type_map: TypeMap):
    """Generate implementation for an operator method."""
    wname = cls.wrapper_name
    unique = get_method_unique_name(method)
    ret = type_map.cpp_type_for_return(method.return_type)
    params = _gen_param_list(method, type_map)
    const = " const" if method.is_const else ""
    args = _occt_args_for_call(method, type_map)

    lines.append("{} {}::{}({}){} {{".format(ret, wname, unique, params, const))

    target = _value_type_target(cls)
    arrow = "->" if _needs_pointer_call(cls) else "."
    call = _native_call_expr(target, arrow, method, args)

    if method.return_type and not method.return_type.is_void:
        ret_base = method.return_type.base_name
        wret = type_map.wrapper_name(ret_base)
        if type_map._is_enum(ret_base):
            lines.append("    return static_cast<{}>({});".format(ret, call))
        elif method.return_type.is_handle and wret:
            lines.append("    auto result = {};".format(call))
            lines.append("    Ref<{}> wrapper; wrapper.instantiate();".format(wret))
            lines.append("    wrapper->_handle = result;")
            lines.append("    return wrapper;")
        elif wret:
            lines.append("    auto result = {};".format(call))
            lines.append("    Ref<{}> wrapper; wrapper.instantiate();".format(wret))
            lines.append("    wrapper->_native = result;")
            lines.append("    return wrapper;")
        else:
            lines.append("    return {};".format(call))
    else:
        lines.append("    {};".format(call))

    lines.append("}")
    lines.append("")


def _gen_static_impl(lines: list[str], method: MethodDecl, cls: ClassDecl, type_map: TypeMap):
    """Generate implementation for a static method."""
    wname = cls.wrapper_name
    unique = get_method_unique_name(method)
    ret = type_map.cpp_type_for_return(method.return_type)
    params = _gen_param_list(method, type_map)
    args = _occt_args_for_call(method, type_map)

    lines.append("{} {}::{}({}) {{".format(ret, wname, unique, params))

    if method.return_type and not method.return_type.is_void:
        ret_base = method.return_type.base_name
        wret = type_map.wrapper_name(ret_base)
        static_call = "::{}::{}({})".format(cls.name, method.name, args)
        if type_map._is_enum(ret_base):
            lines.append("    return static_cast<{}>({});".format(ret, static_call))
        elif method.return_type.is_handle and wret:
            lines.append("    auto result = {};".format(static_call))
            lines.append("    Ref<{}> wrapper; wrapper.instantiate();".format(wret))
            lines.append("    wrapper->_handle = result;")
            lines.append("    return wrapper;")
        elif wret:
            lines.append("    auto result = {};".format(static_call))
            lines.append("    Ref<{}> wrapper; wrapper.instantiate();".format(wret))
            lines.append("    wrapper->_native = result;")
            lines.append("    return wrapper;")
        else:
            lines.append("    return {};".format(static_call))
    else:
        lines.append("    {}::{}({});".format(cls.name, method.name, args))

    lines.append("}")
    lines.append("")


def _occt_args_for_call(method: MethodDecl, type_map: TypeMap) -> str:
    """Build argument list that extracts ._native from wrapped parameters (via Ref<T> ->)."""
    parts = []
    for p in method.parameters:
        base = p.type.base_name
        if p.type.is_handle:
            # Handle params need ._handle (ref-counted wrapper) not ._native
            if type_map.is_wrapped(base):
                parts.append("{}->_handle".format(p.name))
            else:
                # Unwrapped handle type — can't convert Ref<T> to handle<T>
                # This method should have been skipped, but if not, pass through
                parts.append(p.name)
        elif type_map.is_wrapped(base):
            # REF_COUNTED classes always use _handle (even when passed as non-handle ref/value)
            if type_map.is_refcounted(base):
                # For non-handle params, dereference the handle to get the object reference
                if p.type.is_ref:
                    parts.append("*{}->_handle.get()".format(p.name))
                elif p.type.is_pointer:
                    parts.append("{}.get()".format(p.name))
                else:
                    parts.append("*{}->_handle.get()".format(p.name))
            else:
                parts.append("{}->_native".format(p.name))
        elif type_map._is_enum(base):
            parts.append("static_cast<{}>({})".format(base, p.name))
        elif base == "Standard_CString":
            # godot String -> const char* for OCCT
            parts.append("{}.utf8().get_data()".format(p.name))
        else:
            parts.append(p.name)
    return ", ".join(parts)


def _gen_param_list(method: MethodDecl, type_map: TypeMap) -> str:
    """Generate C++ parameter list for the method signature."""
    parts = []
    for p in method.parameters:
        ctype = type_map.cpp_type_for_param(p.type)
        name = p.name or "arg"
        parts.append("{} {}".format(ctype, name))
    return ", ".join(parts)


def _gen_bind_params(method: MethodDecl, unique_name: str) -> str:
    """Generate D_METHOD parameters for ClassDB binding.

    First arg is the method name as exposed to GDScript (the unique name).
    """
    parts = ['"{}"'.format(unique_name)]
    for p in method.parameters:
        parts.append('"{}"'.format(p.name))
    return ", ".join(parts)
