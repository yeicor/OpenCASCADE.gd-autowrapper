"""OCCT type -> wrapper C++ type / call-expression mapping.

This is the FFI contract: every OCCT parameter and return type is mapped to a
GDScript-representable C++ type, plus the expression that converts the wrapper
argument into the native OCCT argument (and back for returns).  Anything that
cannot cross the FFI boundary maps to None and the owning method is skipped.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .model import OCCTType

# Canonical builtin name -> (wrapper C++ type, Godot Variant type name).
# Types are canonicalized in types.py, so OCCT aliases (Standard_Real = double,
# Standard_Integer = int, Standard_Size = unsigned long, ...) never appear here.
PRIMITIVE_MAP: dict[str, tuple[str, str]] = {
    "bool": ("bool", "BOOL"),
    "char": ("int32_t", "INT"),
    "unsigned char": ("uint8_t", "INT"),
    "signed char": ("int8_t", "INT"),
    "short": ("int16_t", "INT"),
    "unsigned short": ("uint16_t", "INT"),
    "int": ("int32_t", "INT"),
    "unsigned int": ("uint32_t", "INT"),
    "long": ("int64_t", "INT"),
    "unsigned long": ("uint64_t", "INT"),
    "long long": ("int64_t", "INT"),
    "unsigned long long": ("uint64_t", "INT"),
    "char16_t": ("uint16_t", "INT"),
    "float": ("float", "FLOAT"),
    "double": ("double", "FLOAT"),
    "long double": ("double", "FLOAT"),
}

# Non-const reference out-parameters of these canonical types become small
# RefCounted box classes (see OcgPrimitiveWrappers.hpp).
PRIMITIVE_WRAPPER_MAP: dict[str, tuple[str, str]] = {
    "bool": ("OcgStandardBoolean", "BOOL"),
    "unsigned char": ("OcgStandardByte", "INT"),
    "char": ("OcgStandardCharacter", "INT"),
    "int": ("OcgStandardInteger", "INT"),
    "long": ("OcgStandardLongInteger", "INT"),
    "double": ("OcgStandardReal", "FLOAT"),
    "float": ("OcgStandardShortReal", "FLOAT"),
    "unsigned long": ("OcgStandardULongInteger", "INT"),
    "TCollection_AsciiString": ("OcgTCollectionAsciiString", "STRING"),
    "TCollection_ExtendedString": ("OcgTCollectionExtendedString", "STRING"),
}


@dataclass
class ParamConv:
    """Conversion for one wrapper parameter."""
    cpp_type: str               # wrapper param C++ type
    gd_type: str                # Godot Variant type name (for PropertyInfo)
    name: str                   # wrapper param name (OCCT name kept)
    call_expr: str = ""         # expression to pass to the OCCT call
    prelude: str = ""           # statements emitted before the call
    postlude: str = ""          # statements emitted after the call
    is_ostream: bool = False    # consumed Standard_OStream& (not in signature)


@dataclass
class RetConv:
    """Conversion for one wrapper return."""
    cpp_type: str               # wrapper return C++ type ("void" = none)
    gd_type: str = "NIL"
    # template body with "{call}" replaced by the native call expression
    body: str = "return {call};"
    prelude: str = ""           # statements emitted before the call
    postlude: str = ""          # statements after the call (before return)


class TypeContext:
    """Cross-declaration knowledge (wrapped classes, enums, module)."""

    def __init__(self, module_name: str):
        self.module_name = module_name
        self.wrapped: dict[str, str] = {}       # occt name -> wrapper name
        self.occt_classes: set[str] = set()     # all scanned occt class names
        self.sync_bases: set[str] = set()       # wrapper names that have _sync_base_storage
        self.enums: dict[str, object] = {}      # enum name -> EnumDecl
        self.occt_headers: dict[str, str] = {}  # occt class name -> header basename
        self.unique_ptr: set[str] = set()       # wrapper names with unique_ptr storage
        self.handles: set[str] = set()          # wrapper names with handle storage
        self.noncopyable: set[str] = set()      # occt names that cannot be copied
        self.inherited_value: set[str] = set()  # wrapper names sharing base storage via _native_ref()


def _enum_occt_path(enum_decl) -> str:
    if enum_decl.parent_class:
        return f"::{enum_decl.parent_class}::{enum_decl.name}"
    return f"::{enum_decl.name}"


def _enum_value_expr(enum_decl, value_name: str) -> str:
    return f"{_enum_occt_path(enum_decl)}::{value_name}"


def stream_kind(t: OCCTType) -> str | None:
    """Classify a canonical std:: stream base_name, or None."""
    b = t.base_name
    if b.startswith("std::basic_ostream") or b == "std::ostream":
        return "out"
    if b.startswith("std::basic_istream") or b == "std::istream":
        return "in"
    if b.startswith("std::basic_stringstream") or b == "std::stringstream":
        return "ss"
    return None


def _rw(move: bool, expr: str) -> str:
    """Wrap a call expression in std::move for rvalue-reference parameters."""
    return f"std::move({expr})" if move else expr


# OCCT parameter names must never collide with the C++ identifiers used by the
# generated wrappers (body locals, Godot API types, language keywords).
_RESERVED_PARAM_NAMES: frozenset[str] = frozenset({
    # C++ keywords
    "alignas", "alignof", "and", "and_eq", "asm", "auto", "bitand", "bitor",
    "bool", "break", "case", "catch", "char", "class", "compl", "const",
    "constexpr", "const_cast", "continue", "decltype", "default", "delete",
    "do", "double", "dynamic_cast", "else", "enum", "explicit", "export",
    "extern", "false", "float", "for", "friend", "goto", "if", "inline",
    "int", "long", "mutable", "namespace", "new", "noexcept", "not", "not_eq",
    "nullptr", "operator", "or", "or_eq", "private", "protected", "public",
    "register", "reinterpret_cast", "return", "short", "signed", "sizeof",
    "static", "static_assert", "static_cast", "struct", "switch", "template",
    "this", "thread_local", "throw", "true", "try", "typedef", "typeid",
    "typename", "union", "unsigned", "using", "virtual", "void", "volatile",
    "wchar_t", "while", "xor", "xor_eq",
    # Godot API identifiers used in generated wrapper code
    "Ref", "RefCounted", "Object", "String", "StringName", "Variant", "Array",
    "Dictionary", "Signal", "Callable", "RID", "NodePath", "Vector2", "Vector3",
    "Vector2i", "Vector3i", "Vector4", "Vector4i", "Transform2D", "Transform3D",
    "Basis", "Quaternion", "Color", "Rect2", "Rect2i", "AABB", "Plane",
    "PackedByteArray", "PackedStringArray", "PackedFloat32Array",
    "PackedFloat64Array", "PackedInt32Array", "PackedVector2Array",
    "PackedVector3Array", "PackedColorArray", "PackedInt64Array",
    "MethodInfo", "PropertyInfo", "ClassDB", "D_METHOD", "GDCLASS",
    "VARIANT_ENUM_CAST", "TypedArray",
    # Body locals emitted by the code generator
    "wrapper", "result", "ref", "value", "ok",
    "ocg_os", "ocg_ss", "ocg_is", "ocg_len", "ocg_buf", "ocg_ret", "arg",
})


def safe_param_name(name: str) -> str:
    """A C++ identifier for a wrapper parameter that cannot shadow codegen locals."""
    if name in _RESERVED_PARAM_NAMES or name.startswith("ocg_") or name.startswith("arg_"):
        return f"arg_{name}"
    return name


def cpp_param(t: OCCTType, name: str, ctx: TypeContext) -> ParamConv | None:
    name = safe_param_name(name)
    move = t.is_rvalue_ref
    stream = stream_kind(t) if t.is_ref else None
    if t.is_ref and stream == "out":
        return ParamConv(cpp_type="", gd_type="", name=name,
                         prelude="std::ostringstream ocg_os;",
                         call_expr="ocg_os", is_ostream=True)
    if t.is_ref and stream == "ss":
        return ParamConv(cpp_type="String", gd_type="STRING", name=name,
                         prelude=f"std::stringstream ocg_ss({name}.utf8().get_data());",
                         call_expr="ocg_ss")
    if t.is_ref and stream == "in":
        return ParamConv(cpp_type="String", gd_type="STRING", name=name,
                         prelude=f"std::istringstream ocg_is({name}.utf8().get_data());",
                         call_expr="ocg_is")
    if t.is_ref and not t.is_const:
        # Non-const reference = in/out parameter.  Primitives and strings use
        # the small box classes; wrapped OCCT value classes fall through to the
        # shared wrapped-class conversion below, which passes the wrapper's
        # native storage by reference so OCCT mutates the caller's object in
        # place (exact in/out semantics, no copying).
        wrapper = PRIMITIVE_WRAPPER_MAP.get(t.base_name)
        if wrapper is not None:
            return ParamConv(cpp_type=f"Ref<{wrapper[0]}>", gd_type=wrapper[1],
                             name=name, call_expr=_rw(move, f"{name}->_native"))
        if t.base_name in PRIMITIVE_MAP or t.is_enum:
            return None  # no box class -> cannot bind a by-value param to a T&
    if t.is_pointer:
        return _cpp_pointer_param(t, name, ctx)
    if t.base_name in PRIMITIVE_MAP:
        cpp, gd = PRIMITIVE_MAP[t.base_name]
        return ParamConv(cpp_type=cpp, gd_type=gd, name=name,
                         call_expr=_rw(move, name))
    if t.is_handle and t.handle_inner in ctx.wrapped:
        w = ctx.wrapped[t.handle_inner]
        return ParamConv(cpp_type=f"Ref<{w}>", gd_type="OBJECT", name=name,
                         call_expr=_rw(move, f"{name}->_handle"))
    if t.base_name in ctx.wrapped:
        w = ctx.wrapped[t.base_name]
        if w in ctx.handles:
            call = f"*{name}->_handle"
        elif w in ctx.unique_ptr:
            call = f"*{name}->_native"
        else:
            native = "_native_ref()" if w in ctx.inherited_value else "_native"
            call = f"{name}->{native}"
        return ParamConv(cpp_type=f"Ref<{w}>", gd_type="OBJECT", name=name,
                         call_expr=_rw(move, call))
    if t.is_enum:
        return _enum_param(t, name, ctx, move=move)
    if t.base_name in ("TCollection_AsciiString",):
        return ParamConv(cpp_type="String", gd_type="STRING", name=name,
                         call_expr=f"TCollection_AsciiString({name}.utf8().get_data())")
    if t.base_name in ("TCollection_ExtendedString",):
        return ParamConv(cpp_type="String", gd_type="STRING", name=name,
                         call_expr=f"TCollection_ExtendedString({name}.utf16())")
    if t.base_name == "std::string" or t.base_name.startswith("std::basic_string<char>"):
        return ParamConv(cpp_type="String", gd_type="STRING", name=name,
                         call_expr=f"std::string({name}.utf8().get_data())")
    return None


def _cpp_pointer_param(t: OCCTType, name: str, ctx: TypeContext) -> ParamConv | None:
    b = t.base_name
    if b == "void":
        cast = "const void*" if t.pointee_is_const else "void*"
        return ParamConv(cpp_type="uint64_t", gd_type="INT", name=name,
                         call_expr=f"reinterpret_cast<{cast}>({name})")
    if b in ("char", "char8_t") and t.pointee_is_const:
        return ParamConv(cpp_type="String", gd_type="STRING", name=name,
                         call_expr=f"{name}.utf8().get_data()")
    if b in ("char16_t",) and t.pointee_is_const:
        return ParamConv(cpp_type="String", gd_type="STRING", name=name,
                         call_expr=f"{name}.utf16()")
    return None


def _enum_param(t: OCCTType, name: str, ctx: TypeContext, move: bool = False) -> ParamConv | None:
    enum_decl = ctx.enums.get(t.base_name)
    if enum_decl is not None:
        return ParamConv(cpp_type=f"OcgEnums::{t.base_name}", gd_type="INT", name=name,
                         call_expr=f"static_cast<{_enum_occt_path(enum_decl)}>({_rw(move, name)})")
    # Any other enum crosses the FFI as an int, cast back to its own type.
    return ParamConv(cpp_type="int32_t", gd_type="INT", name=name,
                     call_expr=f"static_cast<{t.base_name}>({_rw(move, name)})")


def cpp_return(t: OCCTType, ctx: TypeContext, has_ostream: bool = False,
               ostream_is_only_param: bool = False) -> RetConv | None:
    """Map a return type; has_ostream: method consumes a Standard_OStream&."""
    if t.base_name == "void" and not t.is_pointer:
        if has_ostream and not ostream_is_only_param:
            return RetConv(cpp_type="String", gd_type="STRING",
                           body="{call};\n        return ::godot::String::utf8(ocg_os.str().c_str());")
        return RetConv(cpp_type="void", gd_type="NIL", body="{call};")
    if t.is_pointer:
        return _cpp_pointer_return(t, ctx)
    if t.base_name in PRIMITIVE_MAP:
        cpp, gd = PRIMITIVE_MAP[t.base_name]
        return RetConv(cpp_type=cpp, gd_type=gd, body="return {call};")
    if t.base_name in ("char", "char8_t"):
        return RetConv(cpp_type="String", gd_type="STRING",
                       body="return ::godot::String::utf8({call});")
    if t.base_name == "char16_t":
        return RetConv(cpp_type="String", gd_type="STRING",
                       body="return ::godot::String::utf16({call});")
    if t.base_name == "TCollection_AsciiString":
        return RetConv(cpp_type="String", gd_type="STRING",
                       body="return ::godot::String::utf8({call}.ToCString());")
    if t.base_name == "TCollection_ExtendedString":
        return RetConv(cpp_type="String", gd_type="STRING",
                       body="return ::godot::String::utf16({call}.ToExtString());")
    if t.base_name == "std::string" or t.base_name.startswith("std::basic_string<char>"):
        return RetConv(cpp_type="String", gd_type="STRING",
                       body="return ::godot::String::utf8({call}.c_str());")
    if t.is_handle and t.handle_inner in ctx.wrapped:
        w = ctx.wrapped[t.handle_inner]
        sync = f"\n        wrapper->_sync_base_storage();" if w in ctx.sync_bases else ""
        body = ("auto result = {call};\n"
                "        Ref<" + w + "> wrapper; wrapper.instantiate();\n"
                "        wrapper->_handle = result;" + sync + "\n"
                "        return wrapper;")
        return RetConv(cpp_type=f"Ref<{w}>", gd_type="OBJECT", body=body)
    if t.base_name in ctx.wrapped:
        w = ctx.wrapped[t.base_name]
        if t.base_name in ctx.noncopyable:
            # The value type cannot be copied/assigned (e.g. holds a
            # std::unique_ptr member); a reference to a callee-owned object
            # cannot be transferred safely, and by-value returns cannot be
            # copied either. Drop the method.
            return None
        if w in ctx.handles:
            decl = "auto& result" if t.is_ref else "auto result"
            body = (decl + " = {call};\n"
                    "        Ref<" + w + "> wrapper; wrapper.instantiate();\n"
                    "        wrapper->_handle = &result;\n"
                    "        return wrapper;")
        elif w in ctx.unique_ptr:
            decl = "auto& result" if t.is_ref else "auto result"
            body = (decl + " = {call};\n"
                    "        Ref<" + w + "> wrapper; wrapper.instantiate();\n"
                    "        wrapper->_native = std::make_unique<" +
                    _occt_qual(t.base_name) + ">(result);\n"
                    "        return wrapper;")
        else:
            decl = "auto& result" if t.is_ref else "auto result"
            native = "_native_ref()" if w in ctx.inherited_value else "_native"
            body = (decl + " = {call};\n"
                    "        Ref<" + w + "> wrapper; wrapper.instantiate();\n"
                    "        wrapper->" + native + " = result;\n"
                    "        return wrapper;")
        return RetConv(cpp_type=f"Ref<{w}>", gd_type="OBJECT", body=body)
    if t.is_enum:
        return RetConv(cpp_type="int32_t", gd_type="INT",
                       body="return static_cast<int32_t>({call});")
    return None


def _cpp_pointer_return(t: OCCTType, ctx: TypeContext) -> RetConv | None:
    b = t.base_name
    if b == "void":
        # Raw memory pointers cannot cross the FFI; legacy drops them to void.
        return RetConv(cpp_type="void", gd_type="NIL", body="{call};")
    if b in ("char", "char8_t") and t.pointee_is_const:
        return RetConv(cpp_type="String", gd_type="STRING",
                       body="return ::godot::String::utf8({call});")
    if b == "char16_t" and t.pointee_is_const:
        return RetConv(cpp_type="String", gd_type="STRING",
                       body="return ::godot::String::utf16({call});")
    return None


def _occt_qual(base_name: str) -> str:
    return f"::{base_name}"


def default_value(cpp_type: str) -> str:
    return f"{cpp_type}()"
