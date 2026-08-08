"""Generate wrapper C++ (Ocg*.hpp/.cpp) + OcgEnums + module.h from IR.

Output contract mirrors the legacy pipeline exactly (method names, factories,
hash suffixes, stream absorption, guard macros, field accessors, enums).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .model import (ClassDecl, ClassKind, MethodDecl, MethodKind, ModuleDecl,
                    OCCTType)
from .names import (_type_to_string, get_method_unique_name,
                    group_overloads, to_snake_case)
from . import typemap as tm

# ---------------------------------------------------------------------------
# Fixed source blocks
# ---------------------------------------------------------------------------

GODOT_INCLUDES = """#include <godot_cpp/classes/ref_counted.hpp>
#include <godot_cpp/classes/ref.hpp>
#include <godot_cpp/variant/string.hpp>
#include <godot_cpp/variant/variant.hpp>
#include <godot_cpp/variant/array.hpp>
#include <godot_cpp/variant/packed_float64_array.hpp>
#include <godot_cpp/variant/packed_int32_array.hpp>
#include <godot_cpp/core/class_db.hpp>"""

GCC_CHANGES = """#ifdef __GNUC__
#pragma GCC diagnostic ignored "-Wchanges-meaning"
#endif"""

GCC_DEPRECATED = """#ifdef __GNUC__
#pragma GCC diagnostic ignored "-Wdeprecated-declarations"
#pragma GCC diagnostic ignored "-Wunused-parameter"
#endif"""

OPERATOR_SPELLING = {
    "unary_minus": "-", "unary_plus": "+", "*deref": "*", "call": "()",
}


@dataclass
class CgClass:
    cls: ClassDecl
    wrapper_base: str | None      # wrapper name of wrapped OCCT base, or None
    base_occt: str | None         # OCCT name of that base
    storage: str                  # "handle" | "native" | "unique_ptr"
    has_sync: bool = False
    is_aggregate: bool = False
    inherited_native: bool = False  # shares base wrapper's _native via _native_ref()


# ---------------------------------------------------------------------------
# Context building
# ---------------------------------------------------------------------------

def build_context(modules: list[ModuleDecl]) -> tm.TypeContext:
    """Build a TypeContext shared across modules (in include-DAG order).

    Wrapped classes, sync bases, enums and unique_ptr storage are accumulated
    so that a module can reference classes/enums of every earlier module.
    """
    ctx = tm.TypeContext(module_name="__all__")
    for module in modules:
        ctx.occt_classes |= {cls.name for cls in module.classes}
        for cls in module.classes:
            if cls.header_file:
                ctx.occt_headers[cls.name] = Path(cls.header_file).name
    for module in modules:
        for cls in module.classes:
            if not cls.skip and cls.name != module.name:
                ctx.wrapped[cls.name] = cls.wrapper_name
    # Empty value-class type tags that derive a wrapped value class share the
    # base wrapper's native storage (accessed via a downcast _native_ref()).
    # Restricted to TopoDS_Shape tags: those classes are provably member-less,
    # so the base storage layout matches and a downcast reference is sound.
    # Other hierarchies (e.g. BRepMesh splitters) carry state/vptrs and would
    # slice or overflow, so they stay standalone RefCounted wrappers.
    for module in modules:
        for cls in module.classes:
            if cls.skip or cls.name == module.name:
                continue
            if cls.kind == ClassKind.REF_COUNTED:
                continue
            if not _default_constructible(cls):
                continue
            base = next((b for b in cls.base_classes if b in ctx.wrapped), None)
            if base != "TopoDS_Shape":
                continue
            if cls.fields or cls.methods or cls.operators or cls.static_methods:
                continue
            if any(not _default_ctor(c) for c in cls.constructors):
                continue
            ctx.inherited_value.add(cls.wrapper_name)
    for module in modules:
        for cls in module.classes:
            if cls.skip or cls.name == module.name:
                continue
            if not cls.has_copy_assignment and cls.kind != ClassKind.REF_COUNTED:
                ctx.noncopyable.add(cls.name)
    # Propagate non-copyability through data members: a class whose member is
    # non-copyable is itself non-copyable (implicitly deleted copy semantics).
    changed = True
    while changed:
        changed = False
        for module in modules:
            for cls in module.classes:
                if cls.skip or cls.name == module.name:
                    continue
                if cls.name in ctx.noncopyable:
                    continue
                if cls.kind == ClassKind.REF_COUNTED:
                    continue
                # Pointer and handle members do not delete copy semantics of the
                # enclosing class (copying the pointer/refcount is fine even when
                # the pointee is non-copyable); only by-value members propagate.
                if any(not f.type.is_pointer and not f.type.is_handle
                       and f.type.base_name in ctx.noncopyable for f in cls.fields):
                    ctx.noncopyable.add(cls.name)
                    changed = True
    for module in modules:
        for cls in module.classes:
            if cls.skip or cls.name == module.name:
                continue
            if cls.is_abstract:
                # Abstract classes cannot be instantiated; drop parameterized
                # constructors (the null-storage default ctor stays for
                # Ref.instantiate(), and factory methods take over).
                for ctor in cls.constructors:
                    ctor.skip = True
                    ctor.skip_reason = "abstract class (not instantiable)"
            if cls.kind == ClassKind.REF_COUNTED:
                base = next((b for b in cls.base_classes if b in ctx.wrapped), None)
                if base is not None:
                    ctx.sync_bases.add(cls.wrapper_name)
                ctx.handles.add(cls.wrapper_name)
            elif not _default_constructible(cls):
                ctx.unique_ptr.add(cls.wrapper_name)
            # Mirror _cg()'s "none" storage: EXCEPTION wrappers and pure-static
            # utility classes hold no native object, so they cannot appear as a
            # method parameter or return (the typemap drops such methods).
            if cls.kind == ClassKind.EXCEPTION or (
                    cls.static_methods and not cls.methods and not cls.operators
                    and not cls.fields and not cls.has_any_public_ctor):
                ctx.no_storage.add(cls.wrapper_name)
            if not cls.returnable:
                ctx.no_return.add(cls.wrapper_name)
    for module in modules:
        for enum in module.enums:
            if enum.is_public:
                ctx.enums[enum.name] = enum
    return ctx


def _default_constructible(cls: ClassDecl) -> bool:
    """A wrapper can hold `T _native` iff the class has a public default ctor
    or declares no constructors at all (implicit default ctor).

    `cls.default_constructible` overrides the heuristic when the symbol audit
    proved `T()` ill-formed (the probe compiled `(void)T();` and the compiler
    rejected it); such classes must fall back to unique_ptr storage.
    """
    if cls.default_constructible is not None:
        return cls.default_constructible
    # libclang cannot evaluate abstractness for class templates
    # (cursor.is_abstract_record() is False for them), but the pure-virtual
    # members are still extracted; either signal forbids value storage.
    if cls.is_abstract or cls.has_pure_virtual:
        return False  # cannot value-initialize an abstract type
    return cls.has_public_default_ctor or not cls.has_any_ctor


def _cg(cls: ClassDecl, ctx: tm.TypeContext) -> CgClass:
    base_occt = next((b for b in cls.base_classes if b in ctx.wrapped), None)
    if cls.kind == ClassKind.REF_COUNTED:
        # Only a Transient base shares the handle storage; a value-class base
        # (e.g. NCollection_HArray1<double> derives NCollection_Array1<double>)
        # is value-stored in its own wrapper, so picking it would emit a
        # _sync_base_storage() into a member that does not exist.
        base_occt = next((b for b in cls.base_classes
                          if b in ctx.wrapped and ctx.wrapped[b] in ctx.handles),
                         None)
        wrapper_base = ctx.wrapped.get(base_occt) if base_occt else None
        return CgClass(cls=cls, wrapper_base=wrapper_base,
                       base_occt=base_occt, storage="handle",
                       has_sync=wrapper_base is not None,
                       is_aggregate=cls.name == ctx.module_name)
    if cls.kind == ClassKind.EXCEPTION:
        # Standard_Failure hierarchy: wrapped as a diagnostics-only class chain.
        # No native storage (exceptions never cross the FFI); instance methods
        # read the thread-local last-error state recorded by OCCT_GUARD_CATCH.
        return CgClass(cls=cls,
                       wrapper_base=ctx.wrapped.get(base_occt) if base_occt else None,
                       base_occt=base_occt, storage="none",
                       is_aggregate=cls.name == ctx.module_name)
    storage = "native" if _default_constructible(cls) else "unique_ptr"
    # Pure-static utility classes (e.g. BRep_Tool) hold no native object: their
    # ctors are non-public so storage (and its new/delete requirements) is
    # skipped entirely.
    if cls.static_methods and not cls.methods and not cls.operators \
            and not cls.fields and not cls.has_any_public_ctor:
        return CgClass(cls=cls, wrapper_base=None, base_occt=None, storage="none",
                       is_aggregate=cls.name == ctx.module_name)
    if cls.wrapper_name in ctx.inherited_value:
        return CgClass(cls=cls,
                       wrapper_base=ctx.wrapped.get(base_occt) if base_occt else None,
                       base_occt=base_occt, storage="native",
                       inherited_native=True,
                       is_aggregate=cls.name == ctx.module_name)
    return CgClass(cls=cls, wrapper_base=None, base_occt=None,
                   storage=storage, is_aggregate=cls.name == ctx.module_name)


def _occt_qual(cls: ClassDecl) -> str:
    return f"::{cls.name}"


def _params_decl(method: MethodDecl, ctx: tm.TypeContext,
                 cls=None, is_ctor: bool = False) -> str | None:
    parts = []
    for p in method.parameters:
        conv = tm.cpp_param(p.type, p.name, ctx, cls, is_ctor)
        if conv is None:
            return None
        parts.append(f"{conv.cpp_type} {conv.name}")
    return ", ".join(parts)


def _unique(method: MethodDecl) -> str:
    return get_method_unique_name(method)


def _has_ostream_param(method: MethodDecl) -> bool:
    return any(tm.stream_kind(p.type) == "out" for p in method.parameters)


def _uses_streams(cls: ClassDecl) -> bool:
    return any(tm.stream_kind(p.type) is not None
               for m in cls.all_methods for p in m.parameters)


def _uses_fstream(cls: ClassDecl) -> bool:
    """Class has a custom file-I/O body that opens its own std::fstream."""
    for m in cls.all_methods:
        if cls.name == "BRepTools" and m.name in ("Read", "Write"):
            for p in m.parameters:
                if p.type.base_name == "char" and p.type.is_pointer \
                        and p.type.pointee_is_const:
                    return True
    return False


# ---------------------------------------------------------------------------
# Referenced headers / forward declarations
# ---------------------------------------------------------------------------

def _type_occt_header(t: OCCTType, ctx: tm.TypeContext) -> str | None:
    _spec_re = re.compile(r"^([A-Za-z_]\w*)<")

    def header_of(name: str) -> str:
        if name in ctx.occt_headers:
            return ctx.occt_headers[name]
        m = _spec_re.match(name)
        if m:
            return f"{m.group(1)}.hxx"
        return f"{name}.hxx"

    if t.is_handle and t.handle_inner in ctx.wrapped:
        return f"<{header_of(t.handle_inner)}>"
    if t.base_name in ctx.wrapped:
        return f"<{header_of(t.base_name)}>"
    if t.base_name in ("TCollection_AsciiString", "TCollection_ExtendedString"):
        return f"<{t.base_name}.hxx>"
    if t.base_name == "std::string" or t.base_name.startswith("std::basic_string<char>"):
        return "<string>"
    return None


def _type_wrapper(t: OCCTType, ctx: tm.TypeContext) -> str | None:
    if t.is_handle and t.handle_inner in ctx.wrapped:
        return ctx.wrapped[t.handle_inner]
    key = tm._wrapped_key(t.base_name, ctx)
    if key is not None:
        return ctx.wrapped[key]
    return None


def _referenced_headers(cls: ClassDecl, ctx: tm.TypeContext) -> list[str]:
    headers: set[str] = set()
    if cls.header_file:
        headers.add(f"<{Path(cls.header_file).name}>")
    for base in cls.base_classes:
        if base in ctx.occt_classes:
            headers.add(f"<{ctx.occt_headers.get(base, base + '.hxx')}>")
    for method in cls.all_methods:
        for p in method.parameters:
            h = _type_occt_header(p.type, ctx)
            if h:
                headers.add(h)
        if method.return_type is not None:
            h = _type_occt_header(method.return_type, ctx)
            if h:
                headers.add(h)
    for f in cls.fields:
        h = _type_occt_header(f.type, ctx)
        if h:
            headers.add(h)
    return sorted(headers)


def _referenced_wrappers(cls: ClassDecl, ctx: tm.TypeContext) -> set[str]:
    names: set[str] = set()
    for method in cls.all_methods:
        for p in method.parameters:
            w = _type_wrapper(p.type, ctx)
            if w:
                names.add(w)
            w = tm.base_list_iterator_list_wrapper(p.type, cls, ctx)
            if w:
                names.add(w)
        if method.return_type is not None:
            w = _type_wrapper(method.return_type, ctx)
            if w:
                names.add(w)
    for f in cls.fields:
        w = _type_wrapper(f.type, ctx)
        if w:
            names.add(w)
    return names


def _uses_primitive_wrappers(cls: ClassDecl, ctx: tm.TypeContext) -> bool:
    for method in cls.all_methods:
        for p in method.parameters:
            if p.type.base_name in tm.PRIMITIVE_WRAPPER_MAP:
                return True
        if method.return_type is not None and \
                method.return_type.base_name in tm.PRIMITIVE_WRAPPER_MAP:
            return True
    for f in cls.fields:
        if f.type.base_name in tm.PRIMITIVE_WRAPPER_MAP:
            return True
    return False


def _uses_enums(cls: ClassDecl, ctx: tm.TypeContext) -> bool:
    for method in cls.all_methods:
        for p in method.parameters:
            if p.type.base_name in ctx.enums:
                return True
        if method.return_type is not None and \
                method.return_type.base_name in ctx.enums:
            return True
    for f in cls.fields:
        if f.type.base_name in ctx.enums:
            return True
    return False


# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------

def _public_nested_enums(cls: ClassDecl) -> list[object]:
    return [e for e in cls.nested_enums if e.is_public]


def _nested_enum_hpp_lines(cls: ClassDecl) -> list[str]:
    lines: list[str] = []
    for enum in _public_nested_enums(cls):
        path = f"::{cls.name}::{enum.name}"
        lines.append(f"    enum {enum.name} : int64_t {{")
        for v in enum.values:
            lines.append(
                f"        {enum.name}_{v.name} = static_cast<int64_t>({path}::{v.name}),")
        lines.append("    };")
    return lines


def _field_accessor_decls(cls: ClassDecl, ctx: tm.TypeContext) -> list[str]:
    lines: list[str] = []
    for f in cls.fields:
        if not f.is_public or f.skip:
            continue
        snake = to_snake_case(f.name)
        gret = tm.cpp_return(f.type, ctx)
        sconv = tm.cpp_param(f.type, "value", ctx)
        if gret is None or gret.cpp_type == "void":
            continue  # not readable (e.g. void* return)
        lines.append(f"    {gret.cpp_type} _ocg_field_get_{snake}() const;")
        if sconv is not None and not f.is_const:
            lines.append(
                f"    void _ocg_field_set_{snake}({_field_setter_param(sconv)});")
    return lines


def _field_setter_param(sconv: tm.ParamConv) -> str:
    if sconv.cpp_type == "String":
        return "const ::godot::String& " + sconv.name
    return f"{sconv.cpp_type} {sconv.name}"


def _exception_method_kind(cls: ClassDecl, method: MethodDecl) -> str | None:
    """Diagnostic role of an instance method on an EXCEPTION-kind wrapper.

    Exception wrappers carry no native object; the instance API reads the
    thread-local last-error state recorded by OCCT_GUARD_CATCH.  Returns one
    of "message" / "stack" / "type" / "print", or None if the method has no
    diagnostics mapping (and must be skipped).
    """
    if method.kind == MethodKind.STATIC_METHOD:
        return "static"
    name = method.name
    if name in ("what", "GetMessageString"):
        return "message"
    if name == "GetStackString":
        return "stack"
    if name == "ExceptionType":
        return "type"
    if name == "Print" and _has_ostream_param(method):
        return "print"
    return None


def _exception_method_body(cls: ClassDecl, method: MethodDecl,
                           kind: str, params: str) -> str:
    unique = _unique(method)
    const_suffix = " const" if method.is_const else ""
    if kind == "message":
        body = "return ::godot::String(occt_gd::get_last_error_message());"
    elif kind == "stack":
        body = "return ::godot::String(occt_gd::get_last_error_stack());"
    elif kind == "type":
        body = f'return ::godot::String("{cls.name}");'
    else:  # print: the message, since exceptions have no native to stream.
        body = "return ::godot::String(occt_gd::get_last_error_message());"
    return f"""String {cls.wrapper_name}::{unique}({params}){const_suffix} {{
    {body}
}}"""


# Range-for iterator protocol: begin/end/cbegin/cend return container-internal
# iterator objects that have no GDScript meaning (containers are indexed
# directly).  Their unmappable signature is skipped with a dedicated reason.
_ITERATOR_PROTOCOL_METHODS = frozenset(
    {"begin", "end", "cbegin", "cend", "rbegin", "rend", "crbegin", "crend"})


def _first_unmappable(cls: ClassDecl, method: MethodDecl,
                      ctx: tm.TypeContext) -> str | None:
    """Spelling of the first type that cannot cross the FFI, or None.

    Mirrors the None return of ``_method_decl_signature`` exactly so skip
    reasons name the actual offending type instead of a blanket label.
    """
    for p in method.parameters:
        conv = tm.cpp_param(p.type, p.name, ctx, cls)
        if conv is None:
            return p.type.spelling
    if method.return_type is None or (method.return_type.is_void
                                      and not method.return_type.is_pointer):
        return None
    has_ostream = _has_ostream_param(method)
    if tm.cpp_return(method.return_type, ctx, has_ostream=has_ostream,
                     cls=cls) is None:
        return method.return_type.spelling
    return None


def _method_skip_reason(cls: ClassDecl, method: MethodDecl,
                        ctx: tm.TypeContext) -> str:
    """Precise reason a method's signature cannot cross the FFI.

    Mirrors the None return of ``_method_decl_signature``/``_method_body`` so
    the skip registry (autogen/coverage.py) classifies every skip with the
    reason the generator actually emitted, not a blanket ``unmappable type``.
    """
    if cls.kind == ClassKind.EXCEPTION \
            and _exception_method_kind(cls, method) is None:
        return "exception diagnostic method (no native storage)"
    if method.name in _ITERATOR_PROTOCOL_METHODS:
        return "container iterator protocol (begin/end)"
    bad = _first_unmappable(cls, method, ctx)
    if bad is not None:
        return f"unmappable type: {bad}"
    return "unmappable type"


def _method_decl_signature(cls: ClassDecl, method: MethodDecl,
                           ctx: tm.TypeContext) -> str | None:
    if cls.kind == ClassKind.EXCEPTION \
            and _exception_method_kind(cls, method) is None:
        return None
    params = _params_decl(method, ctx, cls)
    if params is None:
        return None
    has_ostream = _has_ostream_param(method)
    if method.return_type is None or (method.return_type.is_void
                                      and not method.return_type.is_pointer):
        if has_ostream:
            ret = "String"
        else:
            ret = "void"
    else:
        rconv = tm.cpp_return(method.return_type, ctx, has_ostream=has_ostream, cls=cls)
        if rconv is None:
            return None
        ret = rconv.cpp_type
    const_suffix = " const" if method.is_const else ""
    return f"{ret} {_unique(method)}({params}){const_suffix}"


def generate_class_hpp(cls: ClassDecl, ctx: tm.TypeContext) -> str:
    _skip_ambiguous_ctor_calls(cls, ctx)
    cg = _cg(cls, ctx)
    base = cg.wrapper_base or "RefCounted"
    out: list[str] = []
    out.append(f"// Auto-generated wrapper for {cls.name} \u2014 DO NOT EDIT")
    out.append("#pragma once")
    out.append("")
    out.append(GODOT_INCLUDES)
    out.append("")
    out.append(GCC_CHANGES)
    out.append("")
    refs = _referenced_headers(cls, ctx)
    own = f"<{Path(cls.header_file).name}>" if cls.header_file else None
    # The class's own header is not self-contained: the scan needed these
    # pre-includes before it would parse, so the wrapper must include them
    # *before* the class header (extra_occt_includes), then the referenced
    # headers.  Dedupe them out of `refs` so they are not emitted again after.
    extras = [f"<{e}>" for e in cls.extra_occt_includes if e and f"<{e}>" != own]
    refs = [h for h in refs if h not in extras]
    if own:
        refs = [own] + [h for h in refs if h != own]
    for h in extras + refs:
        out.append(f"#include {h}")
    if cg.wrapper_base:
        out.append(f'#include "{cg.wrapper_base}.hpp"')
    if cg.storage == "unique_ptr":
        out.append("#include <memory>")
    if _uses_primitive_wrappers(cls, ctx):
        out.append('#include "OcgPrimitiveWrappers.hpp"')
    if _uses_streams(cls):
        out.append('#include "OcgCallableStreams.hpp"')
    if _uses_enums(cls, ctx):
        out.append('#include "OcgEnums.hpp"')
    out.append("")
    if _uses_enums(cls, ctx):
        out.append("")
    out.append("namespace godot {")
    out.append("")
    fwd = sorted(_referenced_wrappers(cls, ctx)
                 - {cls.wrapper_name}
                 - {base} if cg.wrapper_base
                 else _referenced_wrappers(cls, ctx) - {cls.wrapper_name})
    if fwd:
        out.append("// Forward declarations")
        for w in fwd:
            out.append(f"class {w};")
        out.append("")
    out.append("")
    out.append(f"class {cls.wrapper_name} : public {base} {{")
    out.append(f"    GDCLASS({cls.wrapper_name}, {base})")
    out.append("")
    out.append("public:")
    out.extend(_nested_enum_hpp_lines(cls))
    if _public_nested_enums(cls):
        out.append("")
    if cg.storage == "handle":
        out.append(f"    opencascade::handle<{cls.name}> _handle;")
    elif cg.storage == "unique_ptr":
        out.append(f"    std::unique_ptr<{cls.name}> _native = nullptr;")
    elif cg.inherited_native:
        out.append(f"    {cls.name}& _native_ref() {{ return *static_cast<{cls.name}*>(&this->_native); }}")
        out.append(f"    const {cls.name}& _native_ref() const {{ return *static_cast<const {cls.name}*>(&this->_native); }}")
    elif cg.storage == "native":
        out.append(f"    {cls.name} _native;")
    out.append("")
    out.append("    static void _bind_methods();")
    out.append("")
    out.append(f"    {cls.wrapper_name}();")
    if cg.has_sync:
        out.append("")
        out.append("    void _sync_base_storage();")
    out.append("")
    group_overloads(cls)
    emitted = False
    for ctor in cls.constructors:
        if cls.kind == ClassKind.EXCEPTION:
            # Exceptions are diagnostics-only: they are produced by caught
            # OCCT failures, never constructed from GDScript.
            ctor.skip = True
            ctor.skip_reason = "exception class constructor (diagnostics-only)"
            continue
        if _default_ctor(ctor):
            ctor.skip = True
            ctor.skip_reason = "default constructor (native default-construction)"
            continue
        if ctor.skip:
            continue
        params = _params_decl(ctor, ctx, cls, is_ctor=True)
        if params is None:
            ctor.skip = True
            ctor.skip_reason = "unmappable type"
            continue
        out.append(f"    static Ref<{cls.wrapper_name}> {_unique(ctor)}({params});")
        out.append("")
        emitted = True
    for m in cls.methods + cls.operators:
        if m.skip:
            continue
        sig = _method_decl_signature(cls, m, ctx)
        if sig is None:
            m.skip = True
            m.skip_reason = _method_skip_reason(cls, m, ctx)
            continue
        out.append(f"    {sig};")
        emitted = True
    if cls.static_methods:
        sigs = []
        for m in cls.static_methods:
            if m.skip:
                continue
            sig = _method_decl_signature(cls, m, ctx)
            if sig is None:
                m.skip = True
                m.skip_reason = _method_skip_reason(cls, m, ctx)
                continue
            sigs.append(f"    static {sig};")
        if sigs:
            if cls.methods or cls.operators:
                out.append("")
            out.extend(sigs)
            emitted = True
    field_decls = _field_accessor_decls(cls, ctx)
    if field_decls:
        out.extend(field_decls)
        emitted = True
    if emitted:
        out.append("")
    out.append("};")
    out.append("")
    out.append("} // namespace godot")
    if _public_nested_enums(cls):
        out.append("")
        for enum in _public_nested_enums(cls):
            out.append(f"VARIANT_ENUM_CAST({cls.wrapper_name}::{enum.name});")
    return "\n".join(out) + ("\n\n" if not _public_nested_enums(cls) else "\n")


# ---------------------------------------------------------------------------
# Implementation
# ---------------------------------------------------------------------------

def _occt_call(cls: ClassDecl, method: MethodDecl, args: str,
               ctx: tm.TypeContext) -> str:
    if method.kind == MethodKind.STATIC_METHOD:
        return f"{_occt_qual(cls)}::{method.name}({args})"
    if method.operator_type is not None:
        op = OPERATOR_SPELLING.get(method.operator_type.value,
                                   method.operator_type.value)
        if cls.kind == ClassKind.REF_COUNTED:
            return f"_handle.get()->operator{op}({args})"
        if _cg(cls, ctx).inherited_native:
            return f"_native_ref().operator{op}({args})"
        return f"_native.operator{op}({args})"
    if cls.kind == ClassKind.REF_COUNTED:
        return f"_handle.get()->{method.name}({args})"
    if _cg(cls, ctx).storage == "unique_ptr":
        return f"_native->{method.name}({args})"
    if _cg(cls, ctx).inherited_native:
        return f"_native_ref().{method.name}({args})"
    return f"_native.{method.name}({args})"


def _custom_method_body(cls: ClassDecl, method: MethodDecl,
                        ctx: tm.TypeContext) -> str | None:
    """Hand-written bodies for signatures the generic FFI cannot express.

    BRepTools' file-based Read/Write overloads take a ``const char*`` path and
    open their own std::fstream; TopTools_ShapeSet then runs an
    imbue(std::locale::classic())/restore dance on that stream.  Because
    Godot's binary interposes its own std::locale symbols, that dance drains
    Godot's global locale's reference count and eventually frees it through the
    wrong allocator.  We open the fstream ourselves, move it onto the classic
    locale (leaking the replaced locale so its reference is never dropped) and
    use the stream overload instead, so OCCT's locale dance stays inside a
    single, consistent libstdc++ universe (see OcgCallableStreams.hpp).
    """
    if cls.name == "Standard_Dump" and method.name == "AddValuesSeparator":
        # OCCT's AddValuesSeparator only writes ", " when tellp() > 0, i.e.
        # when called mid-stream between already-dumped values.  The wrapper
        # hands it a fresh OcgCallableOStream every call (the sink Callable is
        # the only durable state), so that check would never trigger and the
        # API would always return an empty string.  Write the separator
        # unconditionally instead.
        params = _params_decl(method, ctx, cls)
        if params is None:
            return None
        return f"""String {cls.wrapper_name}::add_values_separator({params}) {{
    try {{
        OCC_CATCH_SIGNALS
        occt_gd::OcgCallableOStream ocg_os(theOStream);
        ocg_os.stream() << ", ";
        ::godot::String ocg_text = ::godot::String::utf8(ocg_os.str().c_str());
        ocg_os.stream().flush();
        return ocg_text;
    }} OCCT_GUARD_CATCH({{}});
}}"""
    if cls.name != "BRepTools" or method.name not in ("Read", "Write"):
        return None
    file_param = next((p for p in method.parameters
                       if p.type.base_name == "char" and p.type.is_pointer
                       and p.type.pointee_is_const), None)
    if file_param is None:
        return None
    unique = _unique(method)
    params = _params_decl(method, ctx, cls)
    if params is None:
        return None
    arg_exprs = []
    for p in method.parameters:
        if p is file_param:
            arg_exprs.append("ocg_fs")
            continue
        conv = tm.cpp_param(p.type, p.name, ctx, cls)
        if conv is None:
            return None
        if conv.prelude:
            return None
        arg_exprs.append(conv.call_expr)
    call = _occt_call(cls, method, ", ".join(arg_exprs), ctx)
    stype = "std::ifstream" if method.name == "Read" else "std::ofstream"
    const_suffix = " const" if method.is_const else ""
    body_lines = [
        f"        {stype} ocg_fs({file_param.name}.utf8().get_data());",
        "        if (!ocg_fs)",
        "            return false;",
        "        new std::locale(ocg_fs.imbue(std::locale::classic()));",
        f"        {call};",
        "        return ocg_fs.good();",
    ]
    return f"""bool {cls.wrapper_name}::{unique}({params}){const_suffix} {{
    try {{
        OCC_CATCH_SIGNALS
{chr(10).join(body_lines)}
    }} OCCT_GUARD_CATCH({{}});
}}"""


def _method_body(cls: ClassDecl, method: MethodDecl,
                 ctx: tm.TypeContext) -> str | None:
    unique = _unique(method)
    params = _params_decl(method, ctx, cls)
    if params is None:
        return None
    custom = _custom_method_body(cls, method, ctx)
    if custom is not None:
        return custom
    if cls.kind == ClassKind.EXCEPTION:
        kind = _exception_method_kind(cls, method)
        if kind is None:
            return None
        if kind == "static":
            pass  # static methods take the normal native-call path
        else:
            return _exception_method_body(cls, method, kind, params)
    preludes: list[str] = []
    arg_exprs: list[str] = []
    for p in method.parameters:
        conv = tm.cpp_param(p.type, p.name, ctx, cls)
        if conv is None:
            return None
        if conv.prelude:
            preludes.append(conv.prelude)
        arg_exprs.append(conv.call_expr)
    args = ", ".join(arg_exprs)
    call = _occt_call(cls, method, args, ctx)
    const_suffix = " const" if method.is_const else ""
    ret_is_void = method.return_type is None or (
        method.return_type.is_void and not method.return_type.is_pointer)
    has_ostream = _has_ostream_param(method)

    if ret_is_void and not has_ostream:
        rconv = tm.RetConv(cpp_type="void", body="{call};")
    else:
        rconv = tm.cpp_return(method.return_type, ctx, has_ostream=has_ostream, cls=cls)
        if rconv is None:
            return None

    guard = ""
    if method.kind != MethodKind.STATIC_METHOD:
        if cls.kind == ClassKind.REF_COUNTED:
            if rconv.cpp_type == "void":
                guard = "        ERR_FAIL_COND(!_handle);\n"
            else:
                guard = (f"        ERR_FAIL_COND_V(!_handle, "
                         f"{tm.default_value(rconv.cpp_type)});\n")
        elif _cg(cls, ctx).storage == "unique_ptr":
            if rconv.cpp_type == "void":
                guard = "        ERR_FAIL_NULL(_native);\n"
            else:
                guard = (f"        ERR_FAIL_NULL_V(_native, "
                         f"{tm.default_value(rconv.cpp_type)});\n")

    body_lines = [f"        {p}" for p in preludes]
    body_lines.append(f"        {rconv.body.replace('{call}', call)}")
    catch = ("OCCT_GUARD_CATCH_VOID();" if rconv.cpp_type == "void"
             else "OCCT_GUARD_CATCH({});")
    return f"""{rconv.cpp_type} {cls.wrapper_name}::{unique}({params}){const_suffix} {{
    try {{
        OCC_CATCH_SIGNALS
{guard}{chr(10).join(body_lines)}
    }} {catch}
}}"""


def _ctor_body(cls: ClassDecl, ctor: MethodDecl, ctx: tm.TypeContext) -> str:
    unique = _unique(ctor)
    params = _params_decl(ctor, ctx, cls, is_ctor=True)
    if params is None:
        return ""  # unmappable param; caller marks skip
    preludes: list[str] = []
    arg_exprs: list[str] = []
    for p in ctor.parameters:
        conv = tm.cpp_param(p.type, p.name, ctx, cls, is_ctor=True)
        if conv is None:
            return ""
        if conv.prelude:
            preludes.append(conv.prelude)
        arg_exprs.append(conv.call_expr)
    args = ", ".join(arg_exprs)
    pre = "\n".join(f"        {p}" for p in preludes) + "\n" if preludes else ""
    cg = _cg(cls, ctx)
    if cg.storage == "unique_ptr":
        return f"""Ref<{cls.wrapper_name}> {cls.wrapper_name}::{unique}({params}) {{
    try {{
        OCC_CATCH_SIGNALS
        Ref<{cls.wrapper_name}> ref; ref.instantiate();
        occt_gd::clear_last_error();
{pre}        ref->_native = std::make_unique<{_occt_qual(cls)}>({args});
        return ref;
    }} OCCT_GUARD_CATCH({{}});
}}"""
    if cg.storage == "handle":
        sync = "\n        ref->_sync_base_storage();" if cg.has_sync else ""
        return f"""Ref<{cls.wrapper_name}> {cls.wrapper_name}::{unique}({params}) {{
    try {{
        OCC_CATCH_SIGNALS
        Ref<{cls.wrapper_name}> ref; ref.instantiate();
        occt_gd::clear_last_error();
{pre}        ref->_handle = new {_occt_qual(cls)}({args});
{sync}
        return ref;
    }} OCCT_GUARD_CATCH({{}});
}}"""
    if cg.inherited_native:
        return f"""Ref<{cls.wrapper_name}> {cls.wrapper_name}::{unique}({params}) {{
    try {{
        OCC_CATCH_SIGNALS
        Ref<{cls.wrapper_name}> ref; ref.instantiate();
        occt_gd::clear_last_error();
{pre}        ref->_native_ref() = {_occt_qual(cls)}({args});
        return ref;
    }} OCCT_GUARD_CATCH({{}});
}}"""
    return f"""Ref<{cls.wrapper_name}> {cls.wrapper_name}::{unique}({params}) {{
    try {{
        OCC_CATCH_SIGNALS
        Ref<{cls.wrapper_name}> ref; ref.instantiate();
        occt_gd::clear_last_error();
{pre}        new (&ref->_native) {_occt_qual(cls)}({args});
        return ref;
    }} OCCT_GUARD_CATCH({{}});
}}"""


def _plain_ctor_body(cls: ClassDecl, ctx: tm.TypeContext) -> str:
    cg = _cg(cls, ctx)
    base_init = f"{cg.wrapper_base}()" if cg.wrapper_base else "RefCounted()"
    if cg.storage == "handle":
        if cls.has_public_default_ctor and not cls.is_abstract:
            sync = "\n        _sync_base_storage();" if cg.has_sync else ""
            return f"""{cls.wrapper_name}::{cls.wrapper_name}() : {base_init} {{
    try {{
        OCC_CATCH_SIGNALS
        _handle = new {_occt_qual(cls)}();
{sync}
    }} OCCT_GUARD_CATCH_CTOR()
}}"""
        return f"""{cls.wrapper_name}::{cls.wrapper_name}() : {base_init} {{
    // No default constructor — _handle is null; use factory methods
}}"""
    if cg.storage == "unique_ptr":
        return f"""{cls.wrapper_name}::{cls.wrapper_name}() : {base_init} {{
    // No default constructor — use factory methods
}}"""
    if cg.storage == "none":
        return f"""{cls.wrapper_name}::{cls.wrapper_name}() : {base_init} {{
}}"""
    if cg.inherited_native:
        return f"""{cls.wrapper_name}::{cls.wrapper_name}() : {base_init} {{
}}"""
    return (f"""{cls.wrapper_name}::{cls.wrapper_name}() : {base_init} , _native() {{
}}""")


def _sync_body(cls: ClassDecl, ctx: tm.TypeContext) -> str:
    cg = _cg(cls, ctx)
    if not cg.has_sync:
        return ""
    lines = [f"    {cg.wrapper_base}::_handle = opencascade::handle<::{cg.base_occt}>"
             f"(static_cast<::{cg.base_occt}*>(_handle.get()));"]
    # Propagate up the whole inheritance chain: the direct base's own
    # _sync_base_storage() copies its (just-set) handle to the next level, so a
    # method taking e.g. Ref<OcgGeomSurface> sees a valid handle even when the
    # concrete wrapper is OcgGeomBSplineSurface (two levels below).
    if cg.wrapper_base in ctx.sync_bases:
        lines.append(f"    {cg.wrapper_base}::_sync_base_storage();")
    return "\n".join(lines)


def _field_accessor_bodies(cls: ClassDecl, ctx: tm.TypeContext) -> list[str]:
    out: list[str] = []
    cg = _cg(cls, ctx)
    if cg.storage == "handle":
        target = "(*_handle)"
        get_guard_tmpl = "ERR_FAIL_COND_V(!_handle, {dflt});"
        set_guard = "ERR_FAIL_COND(!_handle);"
    elif cg.storage == "unique_ptr":
        target = "(*_native)"
        get_guard_tmpl = "ERR_FAIL_NULL_V(_native, {dflt});"
        set_guard = "ERR_FAIL_NULL(_native);"
    else:
        target = "_native_ref()" if cg.inherited_native else "_native"
        get_guard_tmpl, set_guard = None, None
    for f in cls.fields:
        if not f.is_public or f.skip:
            continue
        snake = to_snake_case(f.name)
        gret = tm.cpp_return(f.type, ctx)
        sconv = tm.cpp_param(f.type, "value", ctx)
        if gret is None or gret.cpp_type == "void":
            continue
        get_body = gret.body.replace("{call}", f"{target}.{f.name}")
        if get_guard_tmpl is not None:
            guard = get_guard_tmpl.format(dflt=tm.default_value(gret.cpp_type))
            get_body = f"    {guard}\n    {get_body}"
        out.append(f"""{gret.cpp_type} {cls.wrapper_name}::_ocg_field_get_{snake}() const {{
{get_body}
}}""")
        out.append("")
        if sconv is not None and not f.is_const:
            pre = f"\n    {sconv.prelude}" if sconv.prelude else ""
            set_body = f"{target}.{f.name} = {sconv.call_expr};"
            if set_guard is not None:
                set_body = f"    {set_guard}\n    {set_body}"
            out.append(f"""void {cls.wrapper_name}::_ocg_field_set_{snake}({_field_setter_param(sconv)}) {{{pre}
{set_body}
}}""")
            out.append("")
    return out


def _default_ctor(ctor: MethodDecl) -> bool:
    """True for a no-argument constructor (native default-construction)."""
    return len(ctor.parameters) == 0


def _skip_ambiguous_ctor_calls(cls: ClassDecl, ctx: tm.TypeContext) -> None:
    """Skip ctor bindings whose emitted ``new T(args...)`` is ambiguous.

    A call passing N args is ambiguous when another ctor of arity M > N has
    the same first-N parameter *types* and defaulted trailing params: both are
    viable with identical conversion sequences, and default arguments do not
    participate in overload-resolution tie-breaking (e.g. an ``IntPolyh_Array``
    ``(int)`` binding colliding with ``(int, int = 256)``).  The shorter
    binding is dropped; the longest binding in a collision chain stays
    unambiguous.

    Comparison uses the canonical C++ type of each parameter, not the mapped
    GDScript type: ``const char16_t*`` and ``const char*`` both map to
    ``String`` but are distinct C++ types, so a ``(const char16_t*)`` ctor is
    *not* ambiguous with a ``(const char*, bool = false)`` one.  Idempotent:
    safe to call from both the hpp and cpp paths.
    """
    bound: list[tuple[MethodDecl, list[str]]] = []
    for ctor in cls.constructors:
        if ctor.skip or _default_ctor(ctor):
            continue
        types: list[str] = []
        for p in ctor.parameters:
            conv = tm.cpp_param(p.type, p.name, ctx, cls)
            if conv is None:
                types = []
                break
            types.append(_type_to_string(p.type))
        if types:
            bound.append((ctor, types))
    for ctor, types in bound:
        for other, other_types in bound:
            if other is ctor or len(other_types) <= len(types):
                continue
            if other_types[: len(types)] != types:
                continue
            if all(p.default_value is not None
                   for p in other.parameters[len(types):]):
                ctor.skip = True
                ctor.skip_reason = ("ambiguous constructor call "
                                    "(collides with a defaulted-argument "
                                    "constructor overload)")
                break


_NUMERIC_DEFVAL_TYPES = {
    "bool", "char", "unsigned char", "int", "long", "long long",
    "unsigned long", "unsigned long long", "char16_t", "float", "double",
}

_CXX_KEYWORDS = frozenset({"true", "false", "nullptr"})


def _qualify_default(cls: ClassDecl, dflt: str) -> str:
    """Qualify a bare-identifier default with its owning OCCT class so it
    resolves from the wrapper's _bind_methods.

    Only identifiers that name a static member of the class itself are
    qualified; global enumerators (e.g. Graphic3d_ZLayerId_UNKNOWN) and
    namespace-qualified expressions are left untouched.
    """
    if dflt.isidentifier() and dflt not in _CXX_KEYWORDS:
        if dflt in cls.static_constants:
            return f"{cls.name}::{dflt}"
    return dflt


def _defval_suffix(cls: ClassDecl, method: MethodDecl, ctx: tm.TypeContext) -> str:
    """DEFVAL(...) clauses for trailing parameters that carry C++ defaults.

    Only defaults that are expressible as a godot-cpp `Variant` (numeric
    primitives, or the synthetic ``Callable()`` sink for out-stream params) are
    emitted; object/enum/string defaults cannot be forwarded through `DEFVAL`,
    so the clause is dropped at the first such parameter.
    """
    parts = []
    for p in reversed(method.parameters):
        if tm.stream_kind(p.type) == "out":
            parts.append("DEFVAL(Callable())")
            continue
        if p.default_value is None:
            break
        if p.type.is_enum or p.type.base_name not in _NUMERIC_DEFVAL_TYPES:
            break
        parts.append(f"DEFVAL({_clean_numeric_default(
            _qualify_default(cls, p.default_value))})")
    if not parts:
        return ""
    return ", " + ", ".join(reversed(parts))


def _clean_numeric_default(dflt: str) -> str:
    """Strip a functional-cast type from a numeric default.

    Substituted template defaults read e.g. ``unsigned long(0)`` (from ``T()``
    with ``T`` a multi-word primitive); ``DEFVAL(unsigned long(0))`` does not
    parse, so unwrap the inner literal.  ``true``/``false``/plain literals pass
    through untouched.
    """
    if dflt.isidentifier():
        return dflt
    m = re.match(r"^[A-Za-z_]\w*(?: [A-Za-z_]\w*)*\((.*)\)$", dflt, re.S)
    if m and m.group(1).strip():
        return m.group(1).strip()
    return dflt


def _bind_arg_names(method: MethodDecl, ctx: tm.TypeContext,
                    cls=None) -> str:
    """D_METHOD argument names; callable stream params are exposed by name."""
    names = []
    for p in method.parameters:
        conv = tm.cpp_param(p.type, p.name, ctx, cls)
        if conv is None:
            continue
        names.append(f'"{p.name}"')
    return ", ".join(names)


def _bind_entries(cls: ClassDecl, ctx: tm.TypeContext) -> list[str]:
    out: list[str] = []
    for ctor in cls.constructors:
        if ctor.skip or _default_ctor(ctor):
            continue
        unique = _unique(ctor)
        args = _bind_arg_names(ctor, ctx, cls)
        out.append(
            f'    ClassDB::bind_static_method("{cls.wrapper_name}", '
            f'D_METHOD("{unique}"{", " + args if args else ""}), '
            f"&{cls.wrapper_name}::{unique}{_defval_suffix(cls, ctor, ctx)});")
    for m in cls.methods + cls.operators + cls.static_methods:
        if m.skip:
            continue
        unique = _unique(m)
        args = _bind_arg_names(m, ctx, cls)
        defv = _defval_suffix(cls, m, ctx)
        if m.kind == MethodKind.STATIC_METHOD:
            out.append(
                f'    ClassDB::bind_static_method("{cls.wrapper_name}", '
                f'D_METHOD("{unique}"{", " + args if args else ""}), '
                f"&{cls.wrapper_name}::{unique}{defv});")
        else:
            out.append(
                f"    ClassDB::bind_method(D_METHOD(\"{unique}\""
                f'{", " + args if args else ""}), '
                f"&{cls.wrapper_name}::{unique}{defv});")
    for f in cls.fields:
        if not f.is_public or f.skip:
            continue
        snake = to_snake_case(f.name)
        gret = tm.cpp_return(f.type, ctx)
        sconv = tm.cpp_param(f.type, "value", ctx)
        if gret is None or gret.cpp_type == "void":
            continue
        gd = gret.gd_type
        out.append(
            f'    ClassDB::bind_method(D_METHOD("_ocg_field_get_{snake}"), '
            f"&{cls.wrapper_name}::_ocg_field_get_{snake});")
        if sconv is None or f.is_const:
            # Read-only property: no setter; pass "" to add_property.
            out.append(
                f'    ClassDB::add_property(get_class_static(), '
                f'PropertyInfo(Variant::{gd}, "{snake}", PROPERTY_HINT_NONE, "", '
                f'PROPERTY_USAGE_DEFAULT, "{cls.wrapper_name}"), '
                f'"", "_ocg_field_get_{snake}");')
            continue
        out.append(
            f'    ClassDB::bind_method(D_METHOD("_ocg_field_set_{snake}", "value"), '
            f"&{cls.wrapper_name}::_ocg_field_set_{snake});")
        out.append(
            f'    ClassDB::add_property(get_class_static(), '
            f'PropertyInfo(Variant::{gd}, "{snake}", PROPERTY_HINT_NONE, "", '
            f'PROPERTY_USAGE_DEFAULT, "{cls.wrapper_name}"), '
            f'"_ocg_field_set_{snake}", "_ocg_field_get_{snake}");')
    # Godot's ClassDB keys integer constants per class by constant NAME (the
    # enum name is not part of the key), so enumerators repeated across nested
    # enums of one OCCT class (e.g. GeomFill_Gordon::ResultStatus::NotStarted
    # and GeomFill_Gordon::BuildStage::NotStarted) collide. Bind each name once.
    bound_constants: set[str] = set()
    for enum in _public_nested_enums(cls):
        for v in enum.values:
            if v.name in bound_constants:
                continue
            bound_constants.add(v.name)
            out.append(
                f'    ClassDB::bind_integer_constant(get_class_static(), '
                f'"{enum.name}", "{v.name}", '
                f"static_cast<int64_t>({cls.wrapper_name}::{enum.name}_{v.name}));")
    return out


def generate_class_cpp(cls: ClassDecl, ctx: tm.TypeContext) -> str:
    _skip_ambiguous_ctor_calls(cls, ctx)
    cg = _cg(cls, ctx)
    out: list[str] = []
    out.append(f"// Auto-generated wrapper for {cls.name} -- DO NOT EDIT")
    out.append(f'#include "{cls.wrapper_name}.hpp"')
    out.append("")
    out.append(GCC_DEPRECATED)
    out.append("")
    for w in sorted(_referenced_wrappers(cls, ctx) - {cls.wrapper_name}):
        out.append(f'#include "{w}.hpp"')
    if _uses_streams(cls):
        out.append("")
        out.append("#include <sstream>")
    if _uses_fstream(cls):
        out.append("")
        out.append("#include <fstream>")
    out.append("")
    out.append("#include <godot_cpp/core/error_macros.hpp>")
    out.append("")
    out.append("namespace godot {")
    out.append("")
    out.append(f"void {cls.wrapper_name}::_bind_methods() {{")
    out.extend(_bind_entries(cls, ctx))
    out.append("}")
    out.append("")
    out.append(_plain_ctor_body(cls, ctx))
    out.append("")
    if cg.has_sync:
        out.append(f"void {cls.wrapper_name}::_sync_base_storage() {{")
        out.append(_sync_body(cls, ctx))
        out.append("}")
        out.append("")
    for ctor in cls.constructors:
        if ctor.skip:
            continue
        out.append(_ctor_body(cls, ctor, ctx))
        out.append("")
        out.append("")
    for m in cls.methods + cls.operators + cls.static_methods:
        if m.skip:
            continue
        body = _method_body(cls, m, ctx)
        if body is None:
            m.skip = True
            m.skip_reason = _method_skip_reason(cls, m, ctx)
            continue
        out.append(body)
        out.append("")
    out.extend(_field_accessor_bodies(cls, ctx))
    out.append("} // namespace godot")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# OcgEnums
# ---------------------------------------------------------------------------

def generate_enums_hpp(modules: list[ModuleDecl]) -> str:
    enums = [e for m in modules for e in m.enums if e.is_public]
    out: list[str] = []
    out.append("// Auto-generated host class for standalone OCCT enums -- DO NOT EDIT")
    out.append("#pragma once")
    out.append("")
    out.append(GODOT_INCLUDES)
    out.append("")
    out.append(GCC_CHANGES)
    out.append("")
    for enum in enums:
        out.append(f"#include <{Path(enum.header_file).name}>")
    out.append("")
    out.append("namespace godot {")
    out.append("")
    out.append("class OcgEnums : public RefCounted {")
    out.append("    GDCLASS(OcgEnums, RefCounted)")
    out.append("")
    out.append("public:")
    out.append("    OcgEnums() = default;")
    out.append("")
    out.append("    static void _bind_methods();")
    out.append("")
    for enum in enums:
        out.append(f"    enum {enum.name} : int64_t {{")
        for v in enum.values:
            out.append(
                f"        {enum.name}_{v.name} = static_cast<int64_t>(::{enum.name}::{v.name}),")
        out.append("    };")
        out.append("")
    out.append("};")
    out.append("")
    out.append("} // namespace godot")
    out.append("")
    for enum in enums:
        out.append(f"VARIANT_ENUM_CAST(OcgEnums::{enum.name});")
    return "\n".join(out) + "\n"


def generate_enums_cpp(modules: list[ModuleDecl]) -> str:
    enums = [e for m in modules for e in m.enums if e.is_public]
    out: list[str] = []
    out.append("// Auto-generated host class for standalone OCCT enums -- DO NOT EDIT")
    out.append('#include "OcgEnums.hpp"')
    out.append("")
    out.append("#include <godot_cpp/core/error_macros.hpp>")
    out.append("")
    out.append("namespace godot {")
    out.append("")
    out.append("void OcgEnums::_bind_methods() {")
    for enum in enums:
        for v in enum.values:
            out.append(
                f'    ClassDB::bind_integer_constant(get_class_static(), '
                f'"{enum.name}", "{v.name}", '
                f"static_cast<int64_t>(OcgEnums::{enum.name}_{v.name}));")
    out.append("}")
    out.append("")
    out.append("} // namespace godot")
    out.append("")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# OcgPrimitiveWrappers.hpp
# ---------------------------------------------------------------------------

_PRIMITIVE_WRAPPERS: dict[str, tuple[str, str, str, str, str]] = {
    "bool": ("OcgStandardBoolean", "bool", "BOOL",
             "bool get_value() const { return _native; }",
             "void set_value(bool v) { _native = v; }"),
    "unsigned char": ("OcgStandardByte", "uint8_t", "INT",
                      "uint8_t get_value() const { return _native; }",
                      "void set_value(uint8_t v) { _native = v; }"),
    "char": ("OcgStandardCharacter", "char", "INT",
             "int32_t get_value() const { return (int32_t)_native; }",
             "void set_value(int32_t v) { _native = static_cast<char>(v); }"),
    "int": ("OcgStandardInteger", "int32_t", "INT",
            "int32_t get_value() const { return _native; }",
            "void set_value(int32_t v) { _native = v; }"),
    "long": ("OcgStandardLongInteger", "int64_t", "INT",
             "int64_t get_value() const { return _native; }",
             "void set_value(int64_t v) { _native = v; }"),
    "double": ("OcgStandardReal", "double", "FLOAT",
               "double get_value() const { return _native; }",
               "void set_value(double v) { _native = v; }"),
    "float": ("OcgStandardShortReal", "float", "FLOAT",
              "float get_value() const { return _native; }",
              "void set_value(float v) { _native = v; }"),
    "unsigned long": ("OcgStandardULongInteger", "uint64_t", "INT",
                      "uint64_t get_value() const { return _native; }",
                      "void set_value(uint64_t v) { _native = v; }"),
    "unsigned int": ("OcgStandardUInteger", "uint32_t", "INT",
                     "uint32_t get_value() const { return _native; }",
                     "void set_value(uint32_t v) { _native = v; }"),
    "TCollection_AsciiString": (
        "OcgTCollectionAsciiString", "TCollection_AsciiString", "STRING",
        "::godot::String get_value() const { return ::godot::String::utf8(_native.ToCString()); }",
        "void set_value(const ::godot::String& v) { _native = TCollection_AsciiString(v.utf8().get_data()); }"),
    "TCollection_ExtendedString": (
        "OcgTCollectionExtendedString", "TCollection_ExtendedString", "STRING",
        "::godot::String get_value() const { Standard_Integer ocg_len = _native.LengthOfCString();char* ocg_buf = new char[ocg_len + 1];_native.ToUTF8CString(ocg_buf);ocg_buf[ocg_len] = '\\0';::godot::String ocg_ret = ::godot::String::utf8(ocg_buf);delete[] ocg_buf;return ocg_ret; }",
        "void set_value(const ::godot::String& v) { _native = TCollection_ExtendedString(v.utf8().get_data()); }"),
}


def _primitive_wrapper_names_used(modules: list[ModuleDecl],
                                  ctx: tm.TypeContext) -> set[str]:
    keys: set[str] = set()
    for module in modules:
        for cls in module.classes:
            if cls.skip:
                continue
            for method in cls.all_methods:
                for p in method.parameters:
                    if (p.type.is_ref and not p.type.is_const) \
                            or (p.type.is_pointer and not p.type.pointee_is_const):
                        if p.type.base_name in tm.PRIMITIVE_WRAPPER_MAP:
                            keys.add(p.type.base_name)
    return keys


def generate_primitive_wrappers(keys: set[str]) -> str:
    out: list[str] = []
    out.append("// Auto-generated primitive wrapper classes for non-const ref output params -- DO NOT EDIT")
    out.append("#pragma once")
    out.append("")
    out.append("#include <godot_cpp/classes/ref_counted.hpp>")
    out.append("#include <godot_cpp/core/class_db.hpp>")
    out.append("#include <godot_cpp/variant/string.hpp>")
    out.append("")
    for key in sorted(keys):
        if key.startswith("TCollection_"):
            out.append(f"#include <{key}.hxx>")
    out.append("")
    out.append("using namespace godot;")
    out.append("")
    for key in sorted(keys, key=lambda k: _PRIMITIVE_WRAPPERS[k][0]):
        wclass, native, gd, getter, setter = _PRIMITIVE_WRAPPERS[key]
        out.append(f"class {wclass} : public RefCounted {{")
        out.append(f"    GDCLASS({wclass}, RefCounted)")
        out.append("public:")
        out.append(f"    {native} _native;")
        out.append("")
        out.append(f"    {wclass}() : RefCounted(), _native() {{}}")
        out.append("")
        out.append(f"    {getter}")
        out.append(f"    {setter}")
        out.append("")
        out.append("protected:")
        out.append("    static void _bind_methods() {")
        out.append(f"        ClassDB::bind_method(D_METHOD(\"get_value\"), &{wclass}::get_value);")
        out.append(f"        ClassDB::bind_method(D_METHOD(\"set_value\", \"value\"), &{wclass}::set_value);")
        out.append(f"        ClassDB::add_property(get_class_static(), PropertyInfo(Variant::{gd}, \"value\"), \"set_value\", \"get_value\");")
        out.append("    }")
        out.append("};")
        out.append("")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# OcgCallableStreams.hpp
# ---------------------------------------------------------------------------

def generate_callable_streams_hpp() -> str:
    """Stream <-> Godot Callable trampolines used by every wrapper.

    OCCT methods that consume a ``Standard_OStream&``/``std::ostream&`` (or
    the pointer/`istream` spellings) are exposed to GDScript as Callables.
    ``OcgCallableOStream`` adapts a sink Callable to the ``std::ostream`` OCCT
    writes to; ``OcgCallableIStream`` adapts a source Callable to the
    ``std::istream`` OCCT reads from.  The shims are header-only so wrapper
    TUs pick them up via ``#include "OcgCallableStreams.hpp"`` and no extra
    link-time object is needed.
    """
    return """// Auto-generated Callable <-> std::stream trampolines -- DO NOT EDIT
//
// OCCT methods that write to / read from a std::ostream& / std::istream& (and
// the OCCT typedefs Standard_OStream / Standard_IStream) are exposed to
// GDScript as Godot Callables.  These two small shims adapt a Callable to the
// std::stream interface OCCT uses:
//
//   * OcgCallableOStream (sink): OCCT writes text into the shim's ostream;
//     the accumulated text is forwarded to the Callable, which receives a
//     single String argument, when the stream is flushed.  The generated
//     wrappers always flush before returning (and after capturing the text
//     for Print/Dump-style String returns), so a sink with an invalid
//     Callable simply discards the output.
//
//   * OcgCallableIStream (source): OCCT reads text from the shim's istream;
//     whenever the input area is exhausted the shim calls the Callable (with
//     no arguments) to fetch the next chunk, given back as a String.  An
//     empty String signals end of input, and an invalid Callable yields an
//     empty (EOF) stream.
//
// LOCALE NOTE: Godot's binary embeds its own copy of libstdc++ and exports
// std::locale symbols, so wrapper code can end up touching TWO different
// libstdc++ copies at once:
//
//   * Versioned symbol references in this .so (e.g. std::locale's copy
//     constructor, destructor and std::locale::classic(), resolved as
//     `_ZNSt6locale*@GLIBCXX_3.4`) bind to the SYSTEM libstdc++.
//   * basic_ios::init's inline `std::locale()` calls the interposed
//     `locale::_S_global()` from GODOT's embedded copy, handing the stream a
//     locale object whose _Impl belongs to Godot's libstdc++.
//
// The two copies disagree on reference-count bookkeeping, so every stream
// construction net-drains one reference from that shared _Impl (verified:
// the same _Impl's count walks 3 -> 2 -> 1 across successive shims even
// while a permanent "pin" reference is held).  When the count reaches zero,
// system libstdc++ destroys the _Impl through Godot's copy and the free
// fails ("free(): invalid size").  Both shims therefore:
//
//   * force the stream onto the classic locale at construction (OCCT's
//     imbue(classic())/restore dance then stays inside the system universe),
//   * deliberately increment the drained _Impl's reference count once per
//     construction (see OcgPinInterposedLocale) so the drain can never reach
//     zero -- a bare one-time pin is NOT enough because the drain is
//     per-construction, not per-_Impl.
//
// The pin does not allocate: it bumps std::locale::_Impl's first member
// (_M_references, an _Atomic_word) with the same primitive libstdc++ uses.
// std::locale is layout-compatible with a single _Impl* and _Impl begins
// with _M_references in every libstdc++ ABI; the bump itself is what keeps
// the _Impl alive, and no owning object is required to be destroyed.
#pragma once

#include <godot_cpp/variant/callable.hpp>
#include <godot_cpp/variant/string.hpp>

#include <istream>
#include <locale>
#include <ostream>
#include <streambuf>
#include <string>

namespace occt_gd {

// One phantom reference on the locale that basic_ios::init handed this stream
// (the interposed Godot-global _Impl).  Nothing ever releases it, so that
// _Impl's reference count can never reach zero regardless of how the two
// libstdc++ copies mismatch their bookkeeping; the object leaks exactly once
// and is never freed through the wrong allocator.
inline void OcgPinInterposedLocale(const std::locale &p_replaced) {
    const void *const impl = *(const void *const *)&p_replaced;
    __atomic_fetch_add(static_cast<int *>(const_cast<void *>(impl)), 1, __ATOMIC_ACQ_REL);
}

class OcgCallableOStream final : public std::ostream {
public:
    explicit OcgCallableOStream(const ::godot::Callable &p_sink)
        : std::ostream(&myBuffer), myBuffer(p_sink) {
        OcgPinInterposedLocale(imbue(std::locale::classic()));
    }

    // The std::ostream OCCT writes into.
    std::ostream &stream() { return *this; }

    // The text written so far; valid until the next flush delivers it to the
    // sink Callable (wrapper code captures this before flushing for the
    // Print/Dump-style String-return sugar).
    const std::string &str() const { return myBuffer.myText; }

private:
    class CallableBuffer : public std::streambuf {
    public:
        explicit CallableBuffer(const ::godot::Callable &p_sink) : mySink(p_sink) {}

        std::string myText;

        void deliver() {
            if (myText.empty() || !mySink.is_valid()) {
                return;
            }
            mySink.call(::godot::String::utf8(myText.c_str()));
            myText.clear();
        }

    protected:
        int_type overflow(int_type ch) override {
            if (!traits_type::eq_int_type(ch, traits_type::eof())) {
                myText.push_back(traits_type::to_char_type(ch));
            }
            return ch;
        }

        std::streamsize xsputn(const char *pData, std::streamsize pCount) override {
            myText.append(pData, static_cast<std::size_t>(pCount));
            return pCount;
        }

        // std::ostream::flush() (the wrapper's pre-return flush) lands here.
        int sync() override {
            deliver();
            return 0;
        }

    private:
        ::godot::Callable mySink;
    };

    CallableBuffer myBuffer;
};

class OcgCallableIStream final : public std::istream {
public:
    explicit OcgCallableIStream(const ::godot::Callable &p_source)
        : std::istream(&myBuffer), myBuffer(p_source) {
        OcgPinInterposedLocale(imbue(std::locale::classic()));
    }

    // The std::istream OCCT reads from.
    std::istream &stream() { return *this; }

private:
    class CallableSource : public std::streambuf {
    public:
        explicit CallableSource(const ::godot::Callable &p_source) : mySource(p_source) {}

    protected:
        int_type underflow() override {
            if (myDone) {
                return traits_type::eof();
            }
            if (gptr() < egptr()) {
                return traits_type::to_int_type(*gptr());
            }
            if (!mySource.is_valid()) {
                myDone = true;
                return traits_type::eof();
            }
            ::godot::String next = mySource.call();
            if (next.is_empty()) {
                myDone = true;
                return traits_type::eof();
            }
            myChunk = std::string(next.utf8().get_data());
            if (myChunk.empty()) {
                myDone = true;
                return traits_type::eof();
            }
            char *begin = myChunk.data();
            setg(begin, begin, begin + myChunk.size());
            return traits_type::to_int_type(*begin);
        }

    private:
        ::godot::Callable mySource;
        std::string myChunk;
        bool myDone = false;
    };

    CallableSource myBuffer;
};

} // namespace occt_gd
"""


# ---------------------------------------------------------------------------
# module.h
# ---------------------------------------------------------------------------

def _registration_order(wrappers: list[ClassDecl]) -> list[str]:
    by_occt = {w.name: w for w in wrappers}
    order: list[str] = []
    seen: set[str] = set()

    def visit(occt_name: str) -> None:
        cls = by_occt.get(occt_name)
        if cls is None:
            return
        if cls.wrapper_name in seen:
            return
        seen.add(cls.wrapper_name)
        for base in cls.base_classes:
            visit(base)
        order.append(cls.wrapper_name)

    for w in sorted(wrappers, key=lambda c: c.name):
        visit(w.name)
    return order


def generate_module_h(module: ModuleDecl, wrappers: list[ClassDecl],
                      primitive_keys: set[str]) -> str:
    out: list[str] = []
    out.append("// AUTOGENERATED by OpenCASCADE.gd-autowrapper — DO NOT EDIT")
    out.append("#ifndef AUTOWRAPPER_MODULE_H")
    out.append("#define AUTOWRAPPER_MODULE_H")
    out.append("")
    out.append("#include <godot_cpp/core/class_db.hpp>")
    out.append("#include <godot_cpp/godot.hpp>")
    out.append("")
    out.append('#include "OcgEnums.hpp"')
    out.append('#include "OcgCallableStreams.hpp"')
    for w in sorted({c.wrapper_name for c in wrappers}):
        out.append(f'#include "{w}.hpp"')
    out.append("")
    out.append("namespace godot {")
    out.append("")
    out.append("inline void gdext_initialize_module_auto(godot::ModuleInitializationLevel p_level) {")
    out.append("    (void)p_level;")
    wrapper_names = {c.wrapper_name for c in wrappers}
    for key in sorted(primitive_keys, key=lambda k: _PRIMITIVE_WRAPPERS[k][0]):
        wclass = _PRIMITIVE_WRAPPERS[key][0]
        # Primitive wrapper names that are also full generated wrappers (e.g.
        # TCollection_AsciiString) are registered by the wrapper loop below.
        if wclass in wrapper_names:
            continue
        out.append(f"    godot::ClassDB::register_class<{wclass}>();")
    out.append("    godot::ClassDB::register_class<OcgEnums>();")
    for w in _registration_order(wrappers):
        out.append(f"    godot::ClassDB::register_class<{w}>();")
    out.append("}")
    out.append("")
    out.append("inline void gdext_uninitialize_module_auto(godot::ModuleInitializationLevel p_level) {")
    out.append("    (void)p_level;")
    out.append("}")
    out.append("")
    out.append("} // namespace godot")
    out.append("")
    out.append("#endif // AUTOWRAPPER_MODULE_H")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# Top-level generation
# ---------------------------------------------------------------------------

def generate_all(modules: list[ModuleDecl], out_dir: Path,
                 probe_out: Path | None = None,
                 missing: set[str] | None = None,
                 illformed: set[str] | None = None,
                 module_filter: str | None = None) -> list[Path]:
    """Generate all wrapper files for modules (in include-DAG order) into out_dir.

    `missing` (see autogen.audit) marks every generated method whose OCCT symbol
    is absent from the linked libraries as skipped.  `illformed` (same source)
    marks methods whose instantiation does not compile for the substituted
    template arguments.  When `probe_out` is set, a symbol-audit probe TU is
    also written there after all skip decisions are final.

    `module_filter` restricts *writing* to one module's classes (all modules are
    still loaded so the cross-module context stays complete); used by the dev
    loop to rewrap just the module under work without rescanning.  In filtered
    mode the enums/module.h files and the global stale-file cleanup are skipped.
    """
    # Skip decisions (missing symbols / ill-formed instantiations) mutate the
    # classes (e.g. pinning default_constructible / has_public_default_ctor),
    # so they must land BEFORE build_context: the storage of every wrapper (and
    # the typemap's unique_ptr/handle sets) is derived from those flags.
    if missing:
        from .audit import apply_missing
        apply_missing(modules, missing)
    if illformed:
        from .audit import apply_illformed
        apply_illformed(modules, illformed)
    ctx = build_context(modules)
    wrappers: list[ClassDecl] = []
    written: list[Path] = []
    out_dir.mkdir(parents=True, exist_ok=True)

    def write(name: str, content: str) -> Path:
        p = out_dir / name
        if p.exists() and p.read_text() == content:
            written.append(p)
            return p
        p.write_text(content)
        written.append(p)
        return p

    for module in modules:
        if module_filter is not None and module.name != module_filter:
            continue
        for cls in module.classes:
            if cls.skip:
                continue
            group_overloads(cls)
            write(f"{cls.wrapper_name}.hpp", generate_class_hpp(cls, ctx))
            write(f"{cls.wrapper_name}.cpp", generate_class_cpp(cls, ctx))
            wrappers.append(cls)

    if module_filter is not None:
        return written

    write("OcgEnums.hpp", generate_enums_hpp(modules))
    write("OcgEnums.cpp", generate_enums_cpp(modules))
    keys = _primitive_wrapper_names_used(modules, ctx)
    write("OcgPrimitiveWrappers.hpp", generate_primitive_wrappers(keys))
    write("OcgCallableStreams.hpp", generate_callable_streams_hpp())
    write("module.h", generate_module_h(module, wrappers, keys))

    # Remove any wrapper files that are no longer generated (e.g. classes that
    # became skippable since the last run).
    generated = {p.name for p in written}
    for stale in list(out_dir.glob("*.hpp")) + list(out_dir.glob("*.cpp")):
        if stale.name not in generated:
            stale.unlink()

    if probe_out:
        from .audit import generate_probe_tu
        from .occt import find_occt_install
        project_root = Path(__file__).resolve().parent.parent.parent
        probe = Path(probe_out)
        probe.parent.mkdir(parents=True, exist_ok=True)
        probe.write_text(generate_probe_tu(modules, ctx,
                                           find_occt_install(project_root)))
    return written


def generate_module(module: ModuleDecl, out_dir: Path) -> list[Path]:
    """Generate wrapper files for a single module (kept for compat)."""
    return generate_all([module], out_dir)
