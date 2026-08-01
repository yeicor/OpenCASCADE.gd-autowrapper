"""Generate .cpp wrapper source files."""

from __future__ import annotations

from model import ClassDecl, ClassKind, MethodDecl, MethodKind, FieldDecl, OCCTType
from classify.overloads import get_method_unique_name
from classify.skippable import _resolve_handle_inner
from generate.type_map import (TypeMap, PRIMITIVE_MAP, _PRIMITIVE_WRAPPER_MAP,
                               PRIMITIVE_WRAPPER_CPP_TYPE, COLLECTION_TYPES,
                               HANDLE_COLLECTION_TYPES, SYNTHESIZED_COLLECTION_TYPES,
                               resd_members_for_type, bnd_limits_members_for_type)


def _resolve_via_canonical(otype: OCCTType, type_map: TypeMap) -> str | None:
    """Resolve a typedef alias to its canonical base name via canonical_spelling."""
    if not otype.canonical_spelling:
        return None
    canon_clean = otype.canonical_spelling.replace("const ", "").strip()
    canon_base = canon_clean.rstrip("&").rstrip("*").strip()
    if canon_base == otype.base_name:
        return None
    if type_map.is_wrapped(canon_base) or type_map._is_enum(canon_base) or canon_base in PRIMITIVE_MAP:
        return canon_base
    return None


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

    # String output wrappers: _native holds a TCollection string, exposed as String.
    _STRING_WRAPPERS = {
        "OcgTCollectionAsciiString": "TCollection_AsciiString",
        "OcgTCollectionExtendedString": "TCollection_ExtendedString",
    }

    lines = []
    lines.append("// Auto-generated primitive wrapper classes for non-const ref output params -- DO NOT EDIT")
    lines.append("#pragma once")
    lines.append("")
    lines.append("#include <godot_cpp/classes/ref_counted.hpp>")
    lines.append("#include <godot_cpp/core/class_db.hpp>")
    if _STRING_WRAPPERS:
        lines.append("#include <godot_cpp/variant/string.hpp>")
        lines.append("#include <TCollection_AsciiString.hxx>")
        lines.append("#include <TCollection_ExtendedString.hxx>")
    lines.append("")
    lines.append("using namespace godot;")
    lines.append("")

    # Generate a wrapper class for each primitive wrapper type
    for wrapper_name, cpp_type in sorted(PRIMITIVE_WRAPPER_CPP_TYPE.items()):
        if wrapper_name in _STRING_WRAPPERS:
            get_body = "return ::godot::String::utf8(_native.ToCString());"
            if wrapper_name == "OcgTCollectionExtendedString":
                get_body = (
                    "Standard_Integer ocg_len = _native.LengthOfCString();"
                    "char* ocg_buf = new char[ocg_len + 1];"
                    "_native.ToUTF8CString(ocg_buf);"
                    "ocg_buf[ocg_len] = '\\0';"
                    "::godot::String ocg_ret = ::godot::String::utf8(ocg_buf);"
                    "delete[] ocg_buf;"
                    "return ocg_ret;")
            lines.append("class {} : public RefCounted {{".format(wrapper_name))
            lines.append("    GDCLASS({}, RefCounted)".format(wrapper_name))
            lines.append("public:")
            lines.append("    {} _native;".format(cpp_type))
            lines.append("")
            lines.append("    {}() : RefCounted(), _native() {{}}".format(wrapper_name))
            lines.append("")
            lines.append("    ::godot::String get_value() const {{ {} }}".format(get_body))
            lines.append("    void set_value(const ::godot::String& v) {{ _native = {}(v.utf8().get_data()); }}".format(cpp_type))
            lines.append("")
            lines.append("protected:")
            lines.append("    static void _bind_methods() {")
            lines.append('        ClassDB::bind_method(D_METHOD("get_value"), &{}::get_value);'.format(wrapper_name))
            lines.append('        ClassDB::bind_method(D_METHOD("set_value", "value"), &{}::set_value);'.format(wrapper_name))
            lines.append("    }")
            lines.append("};")
            lines.append("")
            continue

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


def generate_collection_wrappers_header(exclude: set[str] | None = None) -> str:
    """Generate OcgCollectionWrappers.hpp with opaque RefCounted wrapper classes
    for NCollection-based typedefs (lists, sequences, arrays, maps).

    These types are typedefs of NCollection templates and cannot be extracted
    by libclang's class cursor.  They're wrapped as opaque RefCounted value
    types: the OCCT native type is stored in _native.

    Also generates handle-based wrappers for deprecated HSequence/HArray types
    that inherit from Standard_Transient (stored in _handle).

    `exclude` lists OCCT names that now have REAL generated wrappers (see
    generate/collections.py) and must not be emitted here.
    """
    from model import occt_name_to_wrapper
    from generate.type_map import COLLECTION_TYPES, HANDLE_COLLECTION_TYPES

    exclude = exclude or set()
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
        if occt_name in exclude:
            continue
        lines.append("#include {}".format(include))
    # Include headers for handle-type collections
    for occt_name, (cpp_type, include) in sorted(HANDLE_COLLECTION_TYPES.items()):
        lines.append("#include {}".format(include))
    lines.append("")

    # Generate value-type wrappers (_native storage)
    for occt_name, (cpp_type, include) in sorted(COLLECTION_TYPES.items()):
        if occt_name in exclude:
            continue
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
    needs_array = False

    def _resolve_ref_name(otype: OCCTType) -> list[str]:
        """Resolve a method return/param type to the real wrapper class names,
        following handle aliases (e.g. Prs3d_Presentation → Graphic3d_Structure)
        and extracting std::optional / std::pair template arguments."""
        if otype.is_handle and otype.handle_inner:
            return [_resolve_handle_inner(otype.handle_inner)]
        if otype.base_name.startswith("std::optional") or otype.base_name.startswith("std::pair"):
            from generate.type_map import _std_template_args
            return [a for a in _std_template_args(otype.base_name)]
        return [otype.base_name]

    def _maybe_add_referenced(name: str) -> None:
        # Collection/handle-collection types are defined in OcgCollectionWrappers.hpp
        # (already included via the class header), so skip individual includes —
        # EXCEPT synthesized collections, which have their own real wrapper headers.
        if name in COLLECTION_OR_HANDLE and name not in SYNTHESIZED_COLLECTION_TYPES:
            return
        if name != cname and name in type_map._wrapper_names:
            referenced.add(name)

    for method in cls.all_wrappable_methods:
        if method.return_type:
            for name in _resolve_ref_name(method.return_type):
                _maybe_add_referenced(name)
            for member_type, _ in (resd_members_for_type(method.return_type) or []):
                _maybe_add_referenced(member_type)
                needs_array = True
        for p in method.parameters:
            for name in _resolve_ref_name(p.type):
                _maybe_add_referenced(name)
            if p.type.base_name in ("Standard_OStream", "Standard_IStream", "Standard_SStream"):
                needs_sstream = True
    for ref in sorted(referenced):
        wname_ref = type_map.wrapper_name(ref)
        if wname_ref:
            lines.append('#include "{}.hpp"'.format(wname_ref))

    lines.append("")
    if needs_sstream:
        lines.append("#include <sstream>")
        lines.append("")
    if needs_array:
        lines.append("#include <godot_cpp/variant/array.hpp>")
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
        if cls.has_pure_virtual:
            continue  # Abstract class: skip factory methods (can't instantiate)
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

    # Bind nested enums as real GDScript enums (OcgClass.EnumName.VALUE).
    # C++ enumerator names are prefixed with the enum name, deduplicated the
    # same way as in header.py so the names match exactly.
    used_enum_names: set[str] = set()
    for enum in cls.nested_enums:
        for val in enum.values:
            cpp_name = "{}_{}".format(enum.name, val.name)
            while cpp_name in used_enum_names:
                cpp_name += "_"
            used_enum_names.add(cpp_name)
            lines.append('    ClassDB::bind_integer_constant(get_class_static(), "{}", "{}", static_cast<int64_t>({}::{}));'.format(
                enum.name, val.name, wname, cpp_name))

    lines.append("}")
    lines.append("")

    # --- Default constructor ---
    # Value-member storage (VALUE/TOPODS_SHAPE/OTHER with a default ctor) is
    # explicitly initialized with _native() so the OCCT default constructor is
    # visibly called — leaving it implicit is identical in effect, but the
    # explicit form makes the generated code self-documenting.
    uses_value_member = cls.kind not in (ClassKind.BUILDER, ClassKind.REF_COUNTED) and cls.has_public_default_ctor
    init_list = ", _native()" if uses_value_member else ""
    lines.append("{}::{}() : RefCounted(){} {{".format(wname, wname, init_list))

    # Declared default ctor: a public, non-deleted zero-arg constructor. Only
    # this enables BUILDER/REF_COUNTED allocation — implicitly default-constructible
    # classes (no declared ctors, e.g. Bnd_Box2d) may still be abstract (e.g.
    # TDataStd_GenericExtString), so we never allocate them via `new`.
    has_declared_default_ctor = any(len(c.parameters) == 0 for c in cls.constructors)

    if cls.kind == ClassKind.BUILDER:
        if has_declared_default_ctor:
            lines.append("    _builder = std::make_unique<{}>();".format(cname))
            lines.append("    _builder->Build();")
            lines.append("    _result = _builder->Shape();")
        else:
            lines.append("    // No default constructor — _builder is null; use factory methods")
    elif cls.kind == ClassKind.REF_COUNTED:
        if has_declared_default_ctor and not cls.has_pure_virtual:
            lines.append("    _handle = new ::{}();".format(cname))
        else:
            lines.append("    // No default constructor — _handle is null; use factory methods")
    elif cls.kind in (ClassKind.VALUE, ClassKind.TOPODS_SHAPE):
        if cls.has_public_default_ctor:
            # VALUE/TOPODS_SHAPE with default ctor (declared or implicit): _native
            # member is implicitly default-constructed (C++ guarantees this before
            # the constructor body). We do NOT add an explicit initializer because
            # some OCCT classes (e.g. AIS_ViewController) have deleted operator=,
            # and GCC may generate spurious -Wchanges-meaning warnings with it.
            pass
        else:
            lines.append("    // No default constructor — use factory methods")
    else:
        if cls.has_public_default_ctor:
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

        ctor_start = len(lines)
        lines.append("Ref<{}> {}::{}({}) {{".format(wname, wname, unique, params))
        lines.append("    Ref<{}> ref; ref.instantiate();".format(wname))
        _emit_stream_locals(lines, ctor)

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
        _guard_wrap_impl(lines, ctor_start, "Ref<{}>".format(wname))
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

def _has_sstream_param(method: MethodDecl) -> bool:
    """Check if method has a Standard_SStream (stringstream) input parameter."""
    return any(p.type.base_name == "Standard_SStream" for p in method.parameters)


def _emit_stream_locals(lines: list[str], method: MethodDecl) -> None:
    """Emit local stream variables for absorbed ostream/istream/stringstream params."""
    if _has_ostream_param(method):
        lines.append("    std::ostringstream ocg_os;")
    if _has_istream_param(method):
        lines.append("    std::istringstream ocg_is({}.utf8().get_data());".format(
            next(p.name for p in method.parameters if p.type.base_name == "Standard_IStream")))
    if _has_sstream_param(method):
        lines.append("    std::stringstream ocg_ss({}.utf8().get_data());".format(
            next(p.name for p in method.parameters if p.type.base_name == "Standard_SStream")))


def _try_gen_pointer_return(lines: list[str], method: MethodDecl, type_map: TypeMap, call: str) -> bool:
    """Generate return wrapping for raw-pointer-to-wrapped-class returns.

    Only const-pointee pointers, transient (refcounted) objects, and handle
    collections are admitted by check_type_wrappable, so the generated body is
    always a safe copy (value/collection classes) or ref-grab (transient).
    Returns True if handled; the caller must then close the function.
    """
    rt = method.return_type
    if not rt or not rt.is_pointer:
        return False
    ret_base = _resolve_handle_inner(rt.base_name)
    wret = type_map.wrapper_name(ret_base)
    if not wret:
        wret = type_map._wrapper_name_for_otype(rt)
        if not wret:
            return False
    lines.append("    auto result = {};".format(call))
    lines.append("    Ref<{}> wrapper; wrapper.instantiate();".format(wret))
    if ret_base in HANDLE_COLLECTION_TYPES or rt.base_name in HANDLE_COLLECTION_TYPES:
        lines.append("    if (result) wrapper->_handle = result;")
    elif type_map.is_refcounted(ret_base):
        lines.append("    if (result) wrapper->_handle = opencascade::handle<::{}>(result);".format(ret_base))
    else:
        lines.append("    if (result) wrapper->_native = *result;")
    lines.append("    return wrapper;")
    return True


def _gen_optional_return(lines: list[str], method: MethodDecl, type_map: TypeMap, call: str) -> None:
    """Generate a return for a std::optional<T> return type: a Variant that is
    null when the optional is unset, otherwise the value (primitive → scalar,
    wrapped value class → Ref<Wrapper>)."""
    from generate.type_map import _std_template_args
    args = _std_template_args(method.return_type.base_name)
    inner = args[0] if args else ""
    lines.append("    auto ocg_opt = {};".format(call))
    lines.append("    if (!ocg_opt.has_value())")
    lines.append("        return ::godot::Variant();")
    wname = type_map.wrapper_name(inner)
    if wname and type_map.is_value_type(inner):
        lines.append("    Ref<{}> ocg_wrap; ocg_wrap.instantiate();".format(wname))
        lines.append("    ocg_wrap->_native = ocg_opt.value();")
        lines.append("    return ::godot::Variant(ocg_wrap);")
    else:
        lines.append("    return ::godot::Variant(ocg_opt.value());")


def _gen_pair_return(lines: list[str], method: MethodDecl, type_map: TypeMap, call: str) -> None:
    """Generate a return for a std::pair<T, U> return type: a packed numeric
    array for numeric pairs, otherwise a generic Array."""
    from generate.type_map import pair_return_gd_type
    gtype = pair_return_gd_type(method.return_type.base_name)
    lines.append("    auto ocg_pair = {};".format(call))
    if gtype == "PackedFloat64Array":
        lines.append("    ::godot::PackedFloat64Array ocg_arr;")
        lines.append("    ocg_arr.append(static_cast<double>(ocg_pair.first));")
        lines.append("    ocg_arr.append(static_cast<double>(ocg_pair.second));")
    elif gtype == "PackedInt32Array":
        lines.append("    ::godot::PackedInt32Array ocg_arr;")
        lines.append("    ocg_arr.append(static_cast<int32_t>(ocg_pair.first));")
        lines.append("    ocg_arr.append(static_cast<int32_t>(ocg_pair.second));")
    else:
        lines.append("    ::godot::Array ocg_arr;")
        lines.append("    ocg_arr.append(ocg_pair.first);")
        lines.append("    ocg_arr.append(ocg_pair.second);")
    lines.append("    return ocg_arr;")


def _gen_char16_return(lines: list[str], method: MethodDecl, type_map: TypeMap, call: str) -> bool:
    """Handle const char16_t* returns (UTF-16 strings) → String."""
    rt = method.return_type
    if rt and rt.is_pointer and rt.base_name in ("char16_t", "Standard_ExtCharacter"):
        lines.append("    return ::godot::String({});".format(call))
        return True
    return False


def _guard_wrap_impl(lines: list[str], start: int, ret: str) -> None:
    """Wrap a generated function body (lines[start:] in the shared list) in a
    try/catch guard so OCCT C++ exceptions never escape the GDExtension
    boundary (they would otherwise call std::terminate and kill Godot).

    Expects lines[start] to be the function signature line ending in '{' and,
    optionally, the last two entries of `lines` to be the function's closing
    '}' and a blank line (which are then consumed). Re-indents the body into
    the try block and appends the OCCT_GUARD_CATCH epilogue (defined in the
    force-included occt_guard.hxx).
    """
    if len(lines) >= 2 and lines[-1] == "" and lines[-2] == "}":
        body_end = len(lines) - 2
    else:
        body_end = len(lines)
    sig = lines[start]
    body = lines[start + 1:body_end]
    del lines[start:]
    out = [sig, "    try {", "        OCC_CATCH_SIGNALS"]
    for l in body:
        out.append("    " + l if l else "")
    macro = "OCCT_GUARD_CATCH_VOID();" if ret == "void" else "OCCT_GUARD_CATCH({});"
    out.append("    } " + macro)
    out.append("}")
    out.append("")
    lines.extend(out)


def _gen_method_impl(lines: list[str], method: MethodDecl, cls: ClassDecl, type_map: TypeMap):
    """Generate implementation for a regular method."""
    from classify.skippable import _resolve_handle_inner
    wname = cls.wrapper_name
    unique = get_method_unique_name(method)
    ret = type_map.cpp_type_for_return(method.return_type)
    has_ostream = _has_ostream_param(method)
    has_istream = _has_istream_param(method)
    has_sstream = _has_sstream_param(method)
    if has_ostream and (method.return_type is None or method.return_type.is_void):
        ret = "String"
    ret = _qstr(cls, ret)
    params = _gen_param_list(method, type_map, cls=cls)
    const = " const" if method.is_const else ""
    args = _occt_args_for_call(method, type_map, cls.name)

    func_start = len(lines)
    lines.append("{} {}::{}({}){} {{".format(ret, wname, unique, params, const))

    if cls.kind == ClassKind.REF_COUNTED:
        _null_check_for_refcounted(lines, ret)
    elif cls.kind == ClassKind.BUILDER:
        _null_check_for_builder(lines, ret)
    elif not cls.has_public_default_ctor:
        _null_check_for_native_ptr(lines, ret)

    _emit_stream_locals(lines, method)

    target = _value_type_target(cls)
    arrow = "->" if _needs_pointer_call(cls) else "."
    call = _native_call_expr(target, arrow, method, args)

    # Generate call
    if method.return_type and not method.return_type.is_void:
        ret_base_orig = method.return_type.base_name
        # Resolve handle aliases (e.g. Prs3d_Presentation → Graphic3d_Structure)
        # whenever the type is a handle, not only for HANDLE_COLLECTION_TYPES —
        # aliases resolve to real scanned classes that are registered directly.
        ret_base = ret_base_orig
        if method.return_type.is_handle and method.return_type.handle_inner:
            ret_base = _resolve_handle_inner(method.return_type.handle_inner)
        # For typedef aliases (e.g. Graphic3d_BndBox3d → BVH_Box<double, 3>),
        # use the original spelling name for wrapper lookup
        if not type_map.wrapper_name(ret_base):
            alt = type_map._wrapper_name_for_otype(method.return_type)
            if alt:
                ret_base = method.return_type.spelling.replace("const ", "").replace("&", "").replace("*", "").strip()
        if _try_gen_pointer_return(lines, method, type_map, call):
            _guard_wrap_impl(lines, func_start, ret)
            return
        if method.return_type.base_name.startswith("std::optional"):
            _gen_optional_return(lines, method, type_map, call)
            _guard_wrap_impl(lines, func_start, ret)
            return
        if method.return_type.base_name.startswith("std::pair"):
            _gen_pair_return(lines, method, type_map, call)
            _guard_wrap_impl(lines, func_start, ret)
            return
        if _gen_char16_return(lines, method, type_map, call):
            _guard_wrap_impl(lines, func_start, ret)
            return
        # const char* / Standard_CString return → wrap in String (treat as UTF-8)
        if method.return_type.is_pointer and ret_base_orig in ("char", "Standard_CString"):
            lines.append("    return ::godot::String::utf8({});".format(call))
        elif ret_base_orig in ("TCollection_AsciiString", "NCollection_String"):
            lines.append("    return ::godot::String::utf8({}.ToCString());".format(call))
        elif ret_base_orig in ("TCollection_ExtendedString",):
            lines.append("    auto ocg_result = {};".format(call))
            lines.append("    Standard_Integer ocg_len = ocg_result.LengthOfCString();")
            lines.append("    char* ocg_buf = new char[ocg_len + 1];")
            lines.append("    ocg_result.ToUTF8CString(ocg_buf);")
            lines.append("    ocg_buf[ocg_len] = '\\0';")
            lines.append("    ::godot::String ocg_ret = ::godot::String::utf8(ocg_buf);")
            lines.append("    delete[] ocg_buf;")
            lines.append("    return ocg_ret;")
        elif ret_base_orig == "Standard_OStream":
            # Dump-style method: emit the call into the absorbed ostream, then
            # return its contents as a String.
            lines.append("    {};".format(call))
            lines.append("    return ::godot::String::utf8(ocg_os.str().c_str());")
        elif resd_members_for_type(method.return_type):
            # Geom_*::ResD{0,1,2,3} nested struct return → Array of wrapped gp values.
            resd = resd_members_for_type(method.return_type)
            lines.append("    auto ocg_result = {};".format(call))
            lines.append("    Array ocg_ret;")
            for i, (member_type, member_name) in enumerate(resd):
                wmember = type_map.wrapper_name(member_type)
                lines.append("    {")
                lines.append("        Ref<{}> ocg_m{}; ocg_m{}.instantiate();".format(wmember, i, i))
                lines.append("        ocg_m{}->_native = ocg_result.{};".format(i, member_name))
                lines.append("        ocg_ret.append(ocg_m{});".format(i))
                lines.append("    }")
            lines.append("    return ocg_ret;")
        elif bnd_limits_members_for_type(method.return_type):
            # Bnd_*::Limits nested double-struct return → PackedFloat64Array
            members = bnd_limits_members_for_type(method.return_type)
            lines.append("    auto ocg_result = {};".format(call))
            lines.append("    PackedFloat64Array ocg_ret;")
            lines.append("    ocg_ret.resize({});".format(len(members)))
            for i, member in enumerate(members):
                lines.append("    ocg_ret.set({}, ocg_result.{});".format(i, member))
            lines.append("    return ocg_ret;")
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
            lines.append("    return ::godot::String::utf8(ocg_os.str().c_str());")

    _guard_wrap_impl(lines, func_start, ret)


def _gen_operator_impl(lines: list[str], method: MethodDecl, cls: ClassDecl, type_map: TypeMap):
    """Generate implementation for an operator method."""
    from classify.skippable import _resolve_handle_inner
    wname = cls.wrapper_name
    unique = get_method_unique_name(method)
    ret = type_map.cpp_type_for_return(method.return_type)
    has_ostream = _has_ostream_param(method)
    has_istream = _has_istream_param(method)
    has_sstream = _has_sstream_param(method)
    if has_ostream and (method.return_type is None or method.return_type.is_void):
        ret = "String"
    ret = _qstr(cls, ret)
    params = _gen_param_list(method, type_map, cls=cls)
    const = " const" if method.is_const else ""
    args = _occt_args_for_call(method, type_map, cls.name)

    func_start = len(lines)
    lines.append("{} {}::{}({}){} {{".format(ret, wname, unique, params, const))

    if cls.kind == ClassKind.REF_COUNTED:
        _null_check_for_refcounted(lines, ret)
    elif cls.kind == ClassKind.BUILDER:
        _null_check_for_builder(lines, ret)
    elif not cls.has_public_default_ctor:
        _null_check_for_native_ptr(lines, ret)

    _emit_stream_locals(lines, method)

    target = _value_type_target(cls)
    arrow = "->" if _needs_pointer_call(cls) else "."
    call = _native_call_expr(target, arrow, method, args)

    if method.return_type and not method.return_type.is_void:
        ret_base_orig = method.return_type.base_name
        ret_base = ret_base_orig
        if method.return_type.is_handle and method.return_type.handle_inner:
            ret_base = _resolve_handle_inner(method.return_type.handle_inner)
        # For typedef aliases (e.g. Graphic3d_BndBox3d → BVH_Box<double, 3>),
        # use the original spelling name for wrapper lookup
        if not type_map.wrapper_name(ret_base):
            alt = type_map._wrapper_name_for_otype(method.return_type)
            if alt:
                ret_base = method.return_type.spelling.replace("const ", "").replace("&", "").replace("*", "").strip()
        if _try_gen_pointer_return(lines, method, type_map, call):
            _guard_wrap_impl(lines, func_start, ret)
            return
        if method.return_type.is_pointer and ret_base_orig in ("char", "Standard_CString"):
            lines.append("    return ::godot::String::utf8({});".format(call))
        elif ret_base_orig in ("TCollection_AsciiString", "NCollection_String"):
            lines.append("    return ::godot::String::utf8({}.ToCString());".format(call))
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
            lines.append("    return ::godot::String::utf8(ocg_os.str().c_str());")

    _guard_wrap_impl(lines, func_start, ret)


def _gen_static_impl(lines: list[str], method: MethodDecl, cls: ClassDecl, type_map: TypeMap):
    """Generate implementation for a static method."""
    from classify.skippable import _resolve_handle_inner
    wname = cls.wrapper_name
    unique = get_method_unique_name(method)
    ret = type_map.cpp_type_for_return(method.return_type)
    has_ostream = _has_ostream_param(method)
    has_istream = _has_istream_param(method)
    has_sstream = _has_sstream_param(method)
    if has_ostream and (method.return_type is None or method.return_type.is_void):
        ret = "String"
    ret = _qstr(cls, ret)
    params = _gen_param_list(method, type_map, cls=cls)
    args = _occt_args_for_call(method, type_map, cls.name)

    func_start = len(lines)
    lines.append("{} {}::{}({}) {{".format(ret, wname, unique, params))

    _emit_stream_locals(lines, method)

    static_call = "::{}::{}({})".format(cls.name, method.name, args)

    if method.return_type and not method.return_type.is_void:
        ret_base_orig = method.return_type.base_name
        ret_base = ret_base_orig
        if method.return_type.is_handle and method.return_type.handle_inner:
            ret_base = _resolve_handle_inner(method.return_type.handle_inner)
        # For typedef aliases (e.g. Graphic3d_BndBox3d → BVH_Box<double, 3>),
        # use the original spelling name for wrapper lookup
        if not type_map.wrapper_name(ret_base):
            alt = type_map._wrapper_name_for_otype(method.return_type)
            if alt:
                ret_base = method.return_type.spelling.replace("const ", "").replace("&", "").replace("*", "").strip()
        if _try_gen_pointer_return(lines, method, type_map, static_call):
            _guard_wrap_impl(lines, func_start, ret)
            return
        if method.return_type.base_name.startswith("std::optional"):
            _gen_optional_return(lines, method, type_map, static_call)
            _guard_wrap_impl(lines, func_start, ret)
            return
        if method.return_type.base_name.startswith("std::pair"):
            _gen_pair_return(lines, method, type_map, static_call)
            _guard_wrap_impl(lines, func_start, ret)
            return
        if _gen_char16_return(lines, method, type_map, static_call):
            _guard_wrap_impl(lines, func_start, ret)
            return
        if method.return_type.is_pointer and ret_base_orig in ("char", "Standard_CString"):
            lines.append("    return ::godot::String::utf8({});".format(static_call))
        elif ret_base_orig in ("TCollection_AsciiString", "NCollection_String"):
            lines.append("    return ::godot::String::utf8({}.ToCString());".format(static_call))
        elif ret_base_orig in ("TCollection_ExtendedString",):
            lines.append("    auto ocg_result = {};".format(static_call))
            lines.append("    Standard_Integer ocg_len = ocg_result.LengthOfCString();")
            lines.append("    char* ocg_buf = new char[ocg_len + 1];")
            lines.append("    ocg_result.ToUTF8CString(ocg_buf);")
            lines.append("    ocg_buf[ocg_len] = '\\0';")
            lines.append("    ::godot::String ocg_ret = ::godot::String::utf8(ocg_buf);")
            lines.append("    delete[] ocg_buf;")
            lines.append("    return ocg_ret;")
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
            lines.append("    return ::godot::String::utf8(ocg_os.str().c_str());")
        else:
            lines.append("    {}::{}({});".format(cls.name, method.name, args))

    _guard_wrap_impl(lines, func_start, ret)


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
        # Standard_SStream → local stringstream
        if base == "Standard_SStream":
            parts.append("ocg_ss")
            continue
        # NCollection_String → temporary UTF-8 string (lives for the call)
        if base == "NCollection_String":
            parts.append("NCollection_String({}.utf8().get_data())".format(p.name))
            continue
        # void* / const void* → raw address as uint64_t.
        # NB: is_const is True for BOTH "const void*" (const pointee) and
        # "void* const" (const pointer, non-const pointee); use pointee_is_const
        # to distinguish, since a plain void* is required for the latter.
        if base == "void" and p.type.is_pointer:
            parts.append("reinterpret_cast<{}void*>({})".format("const " if p.type.pointee_is_const else "", p.name))
            continue
        # uint8_t* / const uint8_t* → PackedByteArray data
        if base == "uint8_t" and p.type.is_pointer:
            if p.type.pointee_is_const:
                parts.append("{}.ptr()".format(p.name))
            else:
                parts.append("{}.ptrw()".format(p.name))
            continue
        # const char16_t* → String UTF-16
        if base == "char16_t" and p.type.is_pointer and p.type.pointee_is_const:
            parts.append("{}.utf16()".format(p.name))
            continue
        if p.type.is_handle:
            # Handle params need ._handle (ref-counted wrapper) not ._native
            # Resolve aliases (e.g. Prs3d_Presentation → Graphic3d_Structure) first,
            # since base_name may be an unwrapped alias spelling.
            resolved_base = _resolve_handle_inner(p.type.handle_inner) if p.type.handle_inner else base
            if type_map.is_wrapped(base) or type_map.is_wrapped(resolved_base):
                parts.append("{}->_handle".format(p.name))
            else:
                # Unwrapped handle type — can't convert Ref<T> to handle<T>
                # This method should have been skipped, but if not, pass through
                parts.append(p.name)
        elif p.type.is_pointer and not p.type.is_const and not p.type.is_handle:
            # Non-const primitive pointer output (e.g. bool* theIsInside): pass the
            # address of the primitive inside the caller's wrapper object.
            if base in _PRIMITIVE_WRAPPER_MAP and base not in ("char", "void"):
                parts.append("{}.is_valid() ? &{}->_native : nullptr".format(p.name, p.name))
            elif type_map.is_wrapped(base):
                # Raw pointer to a wrapped class (no ownership transfer): pass
                # the native object/handle address.
                if type_map.class_kind(base) == ClassKind.BUILDER:
                    parts.append("{}->_builder.get()".format(p.name))
                elif type_map.is_refcounted(base) or base in HANDLE_COLLECTION_TYPES:
                    parts.append("{}->_handle.get()".format(p.name))
                else:
                    parts.append("&{}->_native".format(p.name))
            else:
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
                if p.type.is_pointer:
                    parts.append("{}->_builder.get()".format(p.name))
                else:
                    parts.append("{}->_builder.get()".format(p.name))
            elif type_map.is_refcounted(base) or base in HANDLE_COLLECTION_TYPES:
                # REF_COUNTED classes always use _handle
                if p.type.is_pointer:
                    # Raw pointer to a handle-owned object (no ownership transfer)
                    parts.append("{}->_handle.get()".format(p.name))
                elif p.type.is_ref:
                    parts.append("*{}->_handle.get()".format(p.name))
                else:
                    parts.append("*{}->_handle.get()".format(p.name))
            else:
                # VALUE/TOPODS_SHAPE: _native may be T or std::unique_ptr<T
                if p.type.is_pointer:
                    parts.append("&{}->_native".format(p.name))
                elif not type_map.has_public_default_ctor(base) and p.type.is_ref:
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
            # Check canonical spelling for typedef aliases (e.g. "Point" → "gp_XYZ")
            canon_base = _resolve_via_canonical(p.type, type_map)
            if canon_base and type_map.is_wrapped(canon_base):
                # Convert Ref<OcgXxx> → _native member for VALUE/TOPODS_SHAPE
                if type_map.class_kind(canon_base) == ClassKind.BUILDER:
                    parts.append("{}->_builder.get()".format(p.name))
                elif type_map.is_refcounted(canon_base) or canon_base in HANDLE_COLLECTION_TYPES:
                    if p.type.is_pointer:
                        parts.append("{}->_handle.get()".format(p.name))
                    elif p.type.is_ref:
                        parts.append("*{}->_handle.get()".format(p.name))
                    else:
                        parts.append("*{}->_handle.get()".format(p.name))
                else:
                    if not type_map.has_public_default_ctor(canon_base) and p.type.is_ref:
                        parts.append("*{}->_native".format(p.name))
                    else:
                        parts.append("{}->_native".format(p.name))
            elif canon_base and type_map._is_enum(canon_base):
                # Canonical alias of an enum (e.g. typedef → GeomAbs_Shape)
                enum_qualified = type_map.qualified_enum_name(canon_base, cls_name)
                parts.append("static_cast<{}>({})".format(enum_qualified, p.name))
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
