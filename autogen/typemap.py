"""OCCT type -> wrapper C++ type / call-expression mapping.

This is the FFI contract: every OCCT parameter and return type is mapped to a
GDScript-representable C++ type, plus the expression that converts the wrapper
argument into the native OCCT argument (and back for returns).  Anything that
cannot cross the FFI boundary maps to None and the owning method is skipped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .model import ClassKind, OCCTType

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
    "unsigned int": ("OcgStandardUInteger", "INT"),
    "TCollection_AsciiString": ("OcgTCollectionAsciiString", "STRING"),
    "TCollection_ExtendedString": ("OcgTCollectionExtendedString", "STRING"),
}

# Const raw pointers to these canonical primitives denote input arrays (the
# callee reads an element sequence; the caller owns the buffer).  They cross
# the FFI as Godot's packed arrays, passed with zero copy through ptr() so the
# element type must match the packed array exactly.
ARRAY_POINTER_MAP: dict[str, tuple[str, str]] = {
    "unsigned char": ("PackedByteArray", "PACKED_BYTE_ARRAY"),
    "int": ("PackedInt32Array", "PACKED_INT32_ARRAY"),
    "long": ("PackedInt64Array", "PACKED_INT64_ARRAY"),
    "long long": ("PackedInt64Array", "PACKED_INT64_ARRAY"),
    "float": ("PackedFloat32Array", "PACKED_FLOAT32_ARRAY"),
    "double": ("PackedFloat64Array", "PACKED_FLOAT64_ARRAY"),
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


_TEMPLATE_RE = re.compile(r"^([A-Za-z_]\w*)\s*<(.*)>$", re.S)


def _split_template_args(argstr: str) -> list[str]:
    """Split top-level template arguments, honouring nested angle brackets."""
    args: list[str] = []
    depth, start = 0, 0
    for i, ch in enumerate(argstr):
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth -= 1
        elif ch == "," and depth == 0:
            args.append(argstr[start:i].strip())
            start = i + 1
    args.append(argstr[start:].strip())
    return args


def _wrapped_key(base_name: str, ctx: TypeContext) -> str | None:
    """OCCT name in `ctx.wrapped` matching `base_name`.

    Exact match first; otherwise OCCT signatures often spell out defaulted
    trailing template parameters (e.g. ``NCollection_DataMap<K,V,H>`` against
    the wrapped ``NCollection_DataMap<K,V>``), so fall back to the wrapped
    specialization whose arguments form a strict prefix of the spelled ones.
    """
    if base_name in ctx.wrapped:
        return base_name
    m = _TEMPLATE_RE.match(base_name)
    if not m:
        return None
    tname = m.group(1)
    args = _split_template_args(m.group(2))
    for key in ctx.wrapped:
        km = _TEMPLATE_RE.match(key)
        if not km or km.group(1) != tname:
            continue
        kargs = _split_template_args(km.group(2))
        if len(kargs) < len(args) and kargs == args[: len(kargs)]:
            return key
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


def _container_expr(cls, ctx: TypeContext) -> str:
    """C++ expression yielding the wrapped container for iterator positioning."""
    if cls.kind == ClassKind.REF_COUNTED:
        return "*_handle"
    if cls.wrapper_name in ctx.unique_ptr:
        return "*_native"
    if cls.wrapper_name in ctx.inherited_value:
        return "_native_ref()"
    return "_native"


def _is_own_iterator_type(t: OCCTType, cls) -> bool:
    """True if `t` is the current class's iterator type.

    NCollection_Sequence<T> addresses elements via its nested
    ``Sequence<T>::Iterator``; NCollection_List<T> aliases ``Iterator`` to the
    base-class ``NCollection_TListIterator<T>`` that its methods actually take.
    Both are positionable from the wrapped container and map to an int index.
    """
    bn = t.base_name
    if bn == f"{cls.name}::Iterator":
        return True
    if cls.name.startswith("NCollection_List<") \
            and bn.startswith("NCollection_TListIterator<"):
        return f"NCollection_List<{bn[len('NCollection_TListIterator<'):]}" == cls.name
    return False


def _container_iterator_param(t: OCCTType, name: str, cls,
                              ctx: TypeContext, is_ctor: bool = False) -> ParamConv | None:
    """Map a `{Container}::Iterator&` in-parameter to an int position.

    OCCT sequence/list/map methods address elements by an opaque iterator that
    can only be obtained by walking from the front.  GDScript cannot build one,
    so the wrapper takes a 0-based position and walks the iterator there.  Only
    the current class's own iterator type is recognized (container-internal
    iterators of *other* types stay unmappable).  Ctors never take their own
    iterator, so the mapping is method-only.
    """
    if is_ctor or t.is_pointer or not _is_own_iterator_type(t, cls):
        return None
    it = f"ocg_it_{name}"
    idx = f"ocg_idx_{name}"
    prelude = (f"{t.base_name} {it}({_container_expr(cls, ctx)});\n"
               f"    for (int32_t {idx} = 0; {idx} < {name}; ++{idx}) "
               f"{{ {it}.Next(); }}")
    return ParamConv(cpp_type="int32_t", gd_type="INT", name=name,
                     prelude=prelude, call_expr=it)


def _self_specialization_base(base_name: str, cls_name: str,
                              ctx: TypeContext) -> str | None:
    """Concrete class name if `base_name` is the enclosing class's own
    specialization spelled with in-class template parameter names (e.g.
    ``NCollection_IndexedDataMap<TheKeyType, TheItemType, Hasher>`` against the
    class ``NCollection_IndexedDataMap<K, V>``); else None.

    Only the SAME specialization qualifies: every top-level arg must be a bare
    identifier that is no known OCCT type (a param like
    ``Append(const NCollection_List<TopoDS_Shape>&)`` shares the template head
    but is the ITEM type, not the self type).
    """
    m = _TEMPLATE_RE.match(base_name)
    cm = _TEMPLATE_RE.match(cls_name)
    if not m or not cm or m.group(1) != cm.group(1):
        return None
    args = _split_template_args(m.group(2))
    if len(args) < len(_split_template_args(cm.group(2))):
        return None
    for arg in args:
        if re.match(r"^[A-Za-z_]\w*$", arg) \
                and arg not in ctx.wrapped \
                and arg not in ctx.occt_classes \
                and arg not in PRIMITIVE_MAP:
            continue
        return None
    return cls_name


def _self_specialization_param(t: OCCTType, name: str, cls,
                               ctx: TypeContext, is_ctor: bool = False) -> ParamConv | None:
    """Map a parameter spelled with the enclosing template's placeholder names
    (e.g. ``Exchange(NCollection_IndexedDataMap<TheKeyType, TheItemType,
    Hasher>&)``) to the wrapper's own class.

    In-class signatures render the self type with the template parameter names
    (TheKeyType/TheItemType/Hasher), so ``_wrapped_key`` cannot match them; the
    parameter is the same specialization as ``*this``, so it is passed as a
    ``Ref`` to the enclosing wrapper's own storage.  A ctor taking a move-only
    self by rvalue ref is skipped: the generated call passes the source as an
    lvalue (which would fall back to a deleted copy ctor on non-copyable
    classes); copyable self params and copy ctors are unaffected.
    """
    if is_ctor and t.is_rvalue_ref and cls.name in ctx.noncopyable:
        return None
    if not (t.is_ref or t.is_pointer):
        return None
    if _self_specialization_base(t.base_name, cls.name, ctx) is None:
        return None
    w = cls.wrapper_name
    if w in ctx.handles:
        call = f"*{name}->_handle"
    elif w in ctx.unique_ptr:
        call = f"*{name}->_native"
    elif w in ctx.inherited_value:
        call = f"{name}->_native_ref()"
    else:
        call = f"{name}->_native"
    return ParamConv(cpp_type=f"Ref<{w}>", gd_type="OBJECT", name=name,
                     call_expr=call)


def base_list_iterator_list_wrapper(t: OCCTType, cls,
                                    ctx: TypeContext) -> str | None:
    """Wrapper name of the owning list for a TListIterator `const BaseList&` ctor.

    Shared by `_base_list_iterator_ctor_param` and the codegen forward-decl
    collector so the iterator wrapper can name its owning NCollection_List<X>.
    """
    if cls is None or not t.is_ref or t.base_name != "NCollection_BaseList":
        return None
    m = _TEMPLATE_RE.match(cls.name)
    if not m or m.group(1) != "NCollection_TListIterator":
        return None
    args = _split_template_args(m.group(2))
    if len(args) != 1:
        return None
    key = f"NCollection_List<{args[0]}>"
    if key not in ctx.wrapped:
        return None
    return ctx.wrapped[key]


def _base_list_iterator_ctor_param(t: OCCTType, name: str, cls,
                                   ctx: TypeContext) -> ParamConv | None:
    """Map the `const NCollection_BaseList&` ctor param of a TListIterator.

    ``NCollection_TListIterator<X>`` binds to its owning list in its ctor, but
    ``NCollection_BaseList`` has no public constructors so it is never wrapped.
    GDScript constructs the iterator from the wrapped ``NCollection_List<X>``
    instead; the list's native storage upcasts to the base ref the ctor wants.
    """
    w = base_list_iterator_list_wrapper(t, cls, ctx)
    if w is None:
        return None
    native = "_native_ref()" if w in ctx.inherited_value else "_native"
    return ParamConv(cpp_type=f"Ref<{w}>", gd_type="OBJECT", name=name,
                     call_expr=f"{name}->{native}")


def cpp_param(t: OCCTType, name: str, ctx: TypeContext,
              cls=None, is_ctor: bool = False) -> ParamConv | None:
    name = safe_param_name(name)
    move = t.is_rvalue_ref
    stream = stream_kind(t) if t.is_ref else None
    if cls is not None:
        conv = _container_iterator_param(t, name, cls, ctx, is_ctor)
        if conv is not None:
            return conv
        conv = _self_specialization_param(t, name, cls, ctx, is_ctor)
        if conv is not None:
            return conv
        conv = _base_list_iterator_ctor_param(t, name, cls, ctx)
        if conv is not None:
            return conv
    if t.is_ref and stream == "out":
        # A Standard_OStream&/std::ostream& sink becomes a Godot Callable that
        # receives the text OCCT writes.  The shared OcgCallableOStream shim
        # wraps it in a std::ostream; the accumulated text is also surfaced as
        # the wrapper's String return for Print/Dump-style methods (flush runs
        # before the return).
        return ParamConv(cpp_type="Callable", gd_type="CALLABLE", name=name,
                         prelude=f"occt_gd::OcgCallableOStream ocg_os({name});",
                         call_expr="ocg_os.stream()",
                         is_ostream=True)
    if t.is_ref and stream == "ss":
        return ParamConv(cpp_type="String", gd_type="STRING", name=name,
                         prelude=f"std::stringstream ocg_ss({name}.utf8().get_data());",
                         call_expr="ocg_ss")
    if t.is_ref and stream == "in":
        # A Standard_IStream&/std::istream& source becomes a Godot Callable
        # that OCCT pulls String chunks from as it reads (see the
        # OcgCallableIStream shim).
        return ParamConv(cpp_type="Callable", gd_type="CALLABLE", name=name,
                         prelude=f"occt_gd::OcgCallableIStream ocg_is({name});",
                         call_expr="ocg_is.stream()")
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
            if t.is_rvalue_ref:
                # rvalue-ref: the callee takes ownership of a temporary, so a
                # by-value argument (moved in) is a sound, minimal binding.
                if t.base_name in PRIMITIVE_MAP:
                    cpp, gd = PRIMITIVE_MAP[t.base_name]
                    return ParamConv(cpp_type=cpp, gd_type=gd, name=name,
                                     call_expr=_rw(move, name))
                return _enum_param(t, name, ctx, move=move)
            return None  # no box class -> cannot bind a by-value param to a T&
    if t.is_pointer:
        return _cpp_pointer_param(t, name, ctx)
    if t.base_name in PRIMITIVE_MAP:
        cpp, gd = PRIMITIVE_MAP[t.base_name]
        return ParamConv(cpp_type=cpp, gd_type=gd, name=name,
                         call_expr=_rw(move, name))
    if t.is_handle and t.handle_inner in ctx.wrapped \
            and ctx.wrapped[t.handle_inner] in ctx.handles:
        w = ctx.wrapped[t.handle_inner]
        return ParamConv(cpp_type=f"Ref<{w}>", gd_type="OBJECT", name=name,
                         call_expr=_rw(move, f"{name}->_handle"))
    key = _wrapped_key(t.base_name, ctx)
    if key is not None:
        w = ctx.wrapped[key]
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
    if t.is_ref and (t.base_name == "std::string_view"
                     or t.base_name.startswith("std::basic_string_view<")):
        # std::string_view is a non-owning view of a string the caller still
        # owns, so a Godot String can back it directly for the call.
        if t.base_name.startswith("std::basic_string_view<char16_t>"):
            call = f"std::u16string_view({name}.utf16())"
        else:
            call = f"std::string_view({name}.utf8().get_data())"
        return ParamConv(cpp_type="String", gd_type="STRING", name=name,
                         call_expr=call)
    return None


def _cpp_pointer_param(t: OCCTType, name: str, ctx: TypeContext) -> ParamConv | None:
    b = t.base_name
    skind = stream_kind(t)
    if skind == "out" and not t.pointee_is_const:
        # Standard_OStream* / std::ostream* sink (e.g.
        # FastSewing::GetStatuses) — same Callable shim as the ref form.
        return ParamConv(cpp_type="Callable", gd_type="CALLABLE", name=name,
                         prelude=f"occt_gd::OcgCallableOStream ocg_os({name});",
                         call_expr="&ocg_os.stream()",
                         is_ostream=True)
    if skind == "in":
        return ParamConv(cpp_type="Callable", gd_type="CALLABLE", name=name,
                         prelude=f"occt_gd::OcgCallableIStream ocg_is({name});",
                         call_expr="&ocg_is.stream()")
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
    if t.pointee_is_const and b in ARRAY_POINTER_MAP:
        # const T* input array -> typed packed array, passed zero-copy via
        # ptr().  Only element-exact matches are bound (see ARRAY_POINTER_MAP).
        pa, gd = ARRAY_POINTER_MAP[b]
        return ParamConv(cpp_type=pa, gd_type=gd, name=name,
                         call_expr=f"{name}.ptr()")
    if t.is_handle and t.handle_inner in ctx.wrapped \
            and ctx.wrapped[t.handle_inner] in ctx.handles:
        # handle<T>* — pointer to a handle, not to the pointee (e.g. the
        # NCollection_Array1/Array2 ctor taking `const T* theBegin` with
        # T = handle<...>).  Pass the address of the wrapper's stored handle.
        return ParamConv(cpp_type=f"Ref<{ctx.wrapped[t.handle_inner]}>",
                         gd_type="OBJECT", name=name,
                         call_expr=f"&{name}->_handle")
    if b in ctx.wrapped or _TEMPLATE_RE.match(b):
        key = _wrapped_key(b, ctx)
        if key is not None:
            # Raw T* to a wrapped class: pass the wrapper's native storage address
            # (null GDScript refs pass nullptr).  Mutations through the pointer are
            # visible on the caller's object, matching OCCT in/out semantics.
            w = ctx.wrapped[key]
            if w in ctx.handles:
                call = f"{name}->_handle.get()"
            elif w in ctx.unique_ptr:
                call = f"{name}->_native.get()"
            else:
                native = "_native_ref()" if w in ctx.inherited_value else "_native"
                call = f"&{name}->{native}"
            return ParamConv(cpp_type=f"Ref<{w}>", gd_type="OBJECT", name=name,
                             call_expr=call)
    if not t.pointee_is_const and b in PRIMITIVE_WRAPPER_MAP:
        # Non-const scalar pointer = out-parameter (e.g. `int* theCount`).
        # Pass the address of the box's native storage so OCCT writes the
        # result back into the caller's box in place — the same semantics as
        # non-const reference parameters.
        wrapper = PRIMITIVE_WRAPPER_MAP[b]
        return ParamConv(cpp_type=f"Ref<{wrapper[0]}>", gd_type=wrapper[1],
                         name=name, call_expr=f"&{name}->_native")
    return None


def _enum_param(t: OCCTType, name: str, ctx: TypeContext, move: bool = False) -> ParamConv | None:
    """Map an enum-typed parameter; wrapped enums cross as their own type."""
    enum_decl = ctx.enums.get(t.base_name)
    if enum_decl is not None:
        return ParamConv(cpp_type=f"OcgEnums::{t.base_name}", gd_type="INT", name=name,
                         call_expr=f"static_cast<{_enum_occt_path(enum_decl)}>({_rw(move, name)})")
    # Any other enum crosses the FFI as an int, cast back to its own type.
    return ParamConv(cpp_type="int32_t", gd_type="INT", name=name,
                     call_expr=f"static_cast<{t.base_name}>({_rw(move, name)})")


def cpp_return(t: OCCTType, ctx: TypeContext, has_ostream: bool = False,
               cls=None) -> RetConv | None:
    """Map a return type; has_ostream: method consumes a Standard_OStream&."""
    rc = _cpp_return_core(t, ctx, has_ostream, cls)
    if rc is None:
        return None
    if has_ostream and not (
            stream_kind(t) is not None or
            (t.base_name == "void" and not t.is_pointer)):
        # A non-stream return alongside an out-stream sink (e.g.
        # StlAPI_Writer::Write -> bool): flush the Callable shim before
        # returning so the sink receives everything OCCT wrote.
        rc = RetConv(cpp_type=rc.cpp_type, gd_type=rc.gd_type,
                     body=_with_ostream_flush(rc.body))
    return rc


def _with_ostream_flush(body: str) -> str:
    """Insert ``ocg_os.stream().flush();`` between the OCCT call and the return.

    ``body`` is a return-body template containing the ``{call}`` placeholder.
    For ``return <expr>;`` bodies the expression is evaluated into a temp first
    so the OCCT call runs exactly once; otherwise the flush is placed after
    the ``{call};`` statement (multi-line wrapped-class return bodies).
    """
    stripped = body.lstrip()
    if stripped.startswith("return "):
        return ("auto ocg_result = {call};\n"
                "        ocg_os.stream().flush();\n"
                "        return ocg_result;")
    return re.sub(r"\{call\};", "{call};\n        ocg_os.stream().flush();",
                  body, count=1)


def _cpp_return_core(t: OCCTType, ctx: TypeContext, has_ostream: bool,
                     cls=None) -> RetConv | None:
    if has_ostream and (stream_kind(t) is not None or
                        (t.base_name == "void" and not t.is_pointer)):
        # Print/Dump(Standard_OStream&) -> Standard_OStream& or void: the
        # stream argument was captured into ocg_os by the parameter
        # conversion, so surface the text that would have been written (the
        # text is captured before the flush delivers it to the Callable sink).
        return RetConv(cpp_type="String", gd_type="STRING",
                       body="{call};\n"
                            "        ::godot::String ocg_text = ::godot::String::utf8(ocg_os.str().c_str());\n"
                            "        ocg_os.stream().flush();\n"
                            "        return ocg_text;")
    if t.is_ref and stream_kind(t) == "ss":
        # Accessor returning a stringstream by reference (e.g.
        # Message_AttributeStream::Stream): surface its contents as a String.
        return RetConv(cpp_type="String", gd_type="STRING",
                       body="return ::godot::String::utf8({call}.str().c_str());")
    if cls is not None and t.is_ref:
        self_base = _self_specialization_base(t.base_name, cls.name, ctx)
        if self_base is not None:
            # `Container& Assign(const Container&)`-style chaining returns the
            # very object the method was called on; surface that identity as a
            # Ref to the enclosing wrapper itself instead of a copy.
            w = cls.wrapper_name
            return RetConv(cpp_type=f"Ref<{w}>", gd_type="OBJECT",
                           body="{call};\n        return Ref<" + w + ">(this);")
    if t.base_name == "void" and not t.is_pointer:
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
        if w in ctx.handles:
            sync = f"\n        wrapper->_sync_base_storage();" if w in ctx.sync_bases else ""
            body = ("auto result = {call};\n"
                    "        Ref<" + w + "> wrapper; wrapper.instantiate();\n"
                    "        wrapper->_handle = result;" + sync + "\n"
                    "        return wrapper;")
            return RetConv(cpp_type=f"Ref<{w}>", gd_type="OBJECT", body=body)
        # handle<T> return whose inner type's wrapper stores the value natively
        # (e.g. Graphic3d_BvhCStructureSetTrsfPers::BVH returning
        # handle<BVH_Tree<double, 3>>): the parser flags `is_handle` from the
        # spelling, but the wrapper has no `_handle`; copy the pointee out.
        key = t.handle_inner
        if key in ctx.noncopyable:
            return None
        native = "_native_ref()" if w in ctx.inherited_value else "_native"
        body = ("auto result = {call};\n"
                "        Ref<" + w + "> wrapper; wrapper.instantiate();\n"
                "        wrapper->" + native + " = *result;\n"
                "        return wrapper;")
        return RetConv(cpp_type=f"Ref<{w}>", gd_type="OBJECT", body=body)
    key = _wrapped_key(t.base_name, ctx)
    if key is not None:
        w = ctx.wrapped[key]
        if key in ctx.noncopyable:
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
                    _occt_qual(key) + ">(result);\n"
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
    if b == "TCollection_AsciiString":
        return RetConv(cpp_type="String", gd_type="STRING",
                       body="auto result = {call};\n"
                            '        if (!result) { return ""; }\n'
                            "        return ::godot::String::utf8(result->ToCString());")
    if b == "TCollection_ExtendedString":
        return RetConv(cpp_type="String", gd_type="STRING",
                       body="auto result = {call};\n"
                            '        if (!result) { return ""; }\n'
                            "        return ::godot::String::utf16(result->ToExtString());")
    # Raw V* (pointer to one wrapped value, e.g. map Seek/ChangeSeek/Bound or
    # Vec2::GetData): transfer a copy of the pointee; a null pointee (absent
    # key) surfaces as an empty object/zero value.
    key = _wrapped_key(b, ctx)
    if key is not None and key not in ctx.noncopyable:
        w = ctx.wrapped[key]
        if w in ctx.handles:
            # Two spellings of a ref-counted pointee cross here:
            #   * is_handle:  `handle<T>*`, a pointer to a stored handle (e.g.
            #     DataMap<K, handle<T>>::Seek/Bound) -> deref the pointer.
            #   * plain class: the `handle<T>`/`const handle<T>&` spelling is
            #     stripped by the parser (e.g. InteractiveContext()), so
            #     `result` is already a handle (or a raw T*, which
            #     handle::operator=(const T*) also accepts) -> assign directly.
            sync = f"\n        wrapper->_sync_base_storage();" if w in ctx.sync_bases else ""
            deref = "*" if t.is_handle else ""
            body = ("auto result = {call};\n"
                    '        if (!result) { return Ref<' + w + '>(); }\n'
                    "        Ref<" + w + "> wrapper; wrapper.instantiate();\n"
                    "        wrapper->_handle = " + deref + "result;" + sync + "\n"
                    "        return wrapper;")
        elif w in ctx.unique_ptr:
            body = ("auto result = {call};\n"
                    '        if (!result) { return Ref<' + w + '>(); }\n'
                    "        Ref<" + w + "> wrapper; wrapper.instantiate();\n"
                    "        wrapper->_native = std::make_unique<" + _occt_qual(key) + ">(*result);\n"
                    "        return wrapper;")
        else:
            native = "_native_ref()" if w in ctx.inherited_value else "_native"
            body = ("auto result = {call};\n"
                    '        if (!result) { return Ref<' + w + '>(); }\n'
                    "        Ref<" + w + "> wrapper; wrapper.instantiate();\n"
                    "        wrapper->" + native + " = *result;\n"
                    "        return wrapper;")
        return RetConv(cpp_type=f"Ref<{w}>", gd_type="OBJECT", body=body)
    if b in PRIMITIVE_MAP:
        cpp, gd = PRIMITIVE_MAP[b]
        body = ("auto result = {call};\n"
                "        if (!result) { return " + cpp + "(); }\n"
                "        return *result;")
        return RetConv(cpp_type=cpp, gd_type=gd, body=body)
    return None


def _occt_qual(base_name: str) -> str:
    return f"::{base_name}"


def default_value(cpp_type: str) -> str:
    return f"{cpp_type}()"
