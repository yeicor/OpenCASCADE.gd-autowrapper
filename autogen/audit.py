"""Link-existence audit for generated wrappers.

Some OCCT methods are declared in headers but never defined in the static
libraries (e.g. ``OSD_Path::LocateExecFile`` -- only the free function
``LocateExecFile(OSD_Path&)`` is exported).  A wrapper calling such a method is
a link error that would only surface at the very end of a slow vcpkg rebuild.

The audit catches those at generation time instead:

  * Pass 1 (``generate-all --probe-out``) emits a probe TU with one discarded
    address-of expression per generated wrapper method.  An explicit member /
    function pointer cast disambiguates overloads, and namespace-scope
    variables force g++ to emit the undefined reference.
  * ``audit`` compiles the probe, lists undefined symbols via ``nm -u -C`` and
    keeps the ones absent from the OCCT libraries' defined symbol set (skipping
    non-OCCT noise such as ``std::`` or ``_GLOBAL_OFFSET_TABLE_``).
  * Pass 2 (``generate-all --missing``) regenerates, skipping every method whose
    symbol is in the missing file.
"""

from __future__ import annotations

from dataclasses import replace
import heapq
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from .model import ClassKind, MethodKind, OCCTType
from .occt import OCCTInstall, include_closure
from . import typemap as tm

# Source spelling of an operator for pointer casts / symbol names.
_OPERATOR_SPELLING = {
    "unary_minus": "-", "unary_plus": "+", "*deref": "*", "call": "()",
}

# Header pairs whose relative order matters but is invisible to ``_hygiene_order``
# (the referenced name is a template / namespace, not a registered class).  Each
# entry forces ``header`` to be included *after* every ``must_follow`` header.
_HEADER_PRECEDENCE: dict[str, frozenset[str]] = {
    # Circular include: NCollection_PackedMap.hxx includes NCollection_PackedMapAlgo.hxx
    # at line ~870, and NCollection_PackedMapAlgo.hxx re-includes NCollection_PackedMap.hxx.
    # If Algo is included first, PackedMap's member-template bodies reference the
    # ``NCollection_PackedMapAlgo`` namespace that the guarded inner include skipped.
    "NCollection_PackedMapAlgo.hxx": frozenset({"NCollection_PackedMap.hxx"}),
}


def _operator_spelling(op: str) -> str:
    return _OPERATOR_SPELLING.get(op, op)


def render_source_type(t: OCCTType) -> str:
    """Source-style type used inside the probe TU's explicit pointer casts.

    Handles are rendered as ``occ::handle<T>`` (the alias OCCT headers use;
    equivalent to ``opencascade::handle<T>``).  Base names are canonical, so
    typedefs such as ``Poly_MeshPurpose``/``unsigned int`` compare by type.
    """
    inner = f"occ::handle<{t.handle_inner}>" if t.is_handle else t.base_name
    if t.is_pointer:
        s = f"{inner}*"
        if t.pointee_is_const:
            s = f"const {s}"
    elif t.is_ref:
        s = f"{inner}&&" if t.is_rvalue_ref else f"{inner}&"
        if t.is_const:
            s = f"const {s}"
    else:
        s = inner
        if t.is_const:
            s = f"const {s}"
    return s


def render_nm_type(t: OCCTType) -> str:
    """Parameter type the way `nm -C` demangles it (Itanium ABI).

    Top-level ``const`` on a by-value parameter is dropped (it is not part of
    the mangled function type); low-level const (``T const&``, ``char const*``)
    is kept with the ``const`` after the type.  Handles demangle under the real
    ``opencascade::`` namespace, not the ``occ`` alias.
    """
    inner = f"opencascade::handle<{t.handle_inner}>" if t.is_handle else t.base_name
    if t.is_const and (t.is_ref or t.is_pointer):
        inner = f"{inner} const"
    if t.is_pointer:
        return f"{inner}*"
    if t.is_ref:
        return f"{inner}&&" if t.is_rvalue_ref else f"{inner}&"
    return inner


def symbol_for_method(cls, method) -> str:
    """Demangled member symbol a wrapper for `method` will reference at link."""
    args = ", ".join(render_nm_type(p.type) for p in method.parameters)
    if method.operator_type is not None:
        name = f"operator{_operator_spelling(method.operator_type.value)}"
    else:
        name = method.name
    symbol = f"{cls.name}::{name}({args})"
    if method.is_const:
        symbol += " const"
    return symbol


def _method_display_name(cls, method) -> str:
    if method.operator_type is not None:
        return f"{cls.name}::operator{_operator_spelling(method.operator_type.value)}"
    return f"{cls.name}::{method.name}"


def _probe_type(t: OCCTType, ctx: tm.TypeContext) -> str:
    """Source spelling of a signature type inside the probe TU's casts.

    Placeholder-spelled self-specializations (``NCollection_IndexedDataMap<
    TheKeyType, TheItemType, Hasher>&`` from an in-class member signature) are
    substituted with the concrete class name; the 2-arg form is the same type
    as the fully-defaulted 3-arg declaration, so the cast resolves.
    """
    return render_source_type(t)


def _field_probe_line(cls, f, index: int) -> str:
    """Probe the generated ``_ocg_field_get_/set_`` accessors of a public data
    member: the getter copy-constructs the field value and the setter assigns
    it, so a member type with implicitly deleted copy semantics (e.g. a class
    holding ``std::atomic`` through a template) makes the accessors ill-formed.
    ``std::declval`` cannot be *called* in an evaluated context, so the lambda
    body reaches a reference through ``ocg_field_probe_ref`` (a never-executed
    inline helper that dereferences a null pointer, making the copy/assign
    expressions compile or fail here).  Emitted as a function definition (like
    the ctor probes) since expression statements are invalid at namespace
    scope; the ``ocg_field_`` function name carries the diagnostic marker."""
    assign = (f"ocg_field_probe_ref<_C>().{f.name} = "
              f"ocg_field_probe_ref<const _T>(); "
              if not f.is_const else "")
    head = f"void ocg_field_{index:05d}() {{ (void)[] {{ using _C = ::{cls.name}; "
    tail = f"using _T = std::remove_reference<decltype(std::declval<_C&>().{f.name})>::type; "
    body = f"_T _v = ocg_field_probe_ref<const _C>().{f.name}; {assign}"
    return head + tail + body + "}(); }"


def _probe_line(cls, method, index: int, ctx: tm.TypeContext) -> str:
    def resolve(t):
        base = tm._self_specialization_base(t.base_name, cls.name, ctx)
        if base is not None and base != t.base_name:
            t = replace(t, base_name=base)
        return t

    params = ", ".join(render_source_type(resolve(p.type))
                       for p in method.parameters)
    ret_is_void = (method.return_type is None
                   or (method.return_type.is_void
                       and not method.return_type.is_pointer))
    if ret_is_void:
        ret = "void"
    else:
        ret = render_source_type(resolve(method.return_type))
    if method.operator_type is not None:
        name = f"operator{_operator_spelling(method.operator_type.value)}"
    else:
        name = method.name
    target = f"&::{cls.name}::{name}"
    if method.kind == MethodKind.STATIC_METHOD:
        cast = f"static_cast<{ret} (*)({params})>({target})"
    else:
        const = " const" if method.is_const else ""
        cast = f"static_cast<{ret} (::{cls.name}::*)({params}){const}>({target})"
    return f"auto const ocg_sym_{index:05d} = {cast};"


# Constructor probes
# ------------------
# Constructors cannot be named by a member pointer, so the probe references
# their symbols with `new ::Cls(args)` inside a non-static function.  The
# construction expression emits the same complete-object (C1) symbol the
# wrapper's `new Cls(...)` / `make_unique<Cls>(...)` will reference, and an
# external-linkage function cannot be optimized away.  Arguments are only
# default-constructed for types that are certain to compile (primitives,
# enums, handles, and wrapped default-constructible value classes); a ctor
# with any other parameter type is left unprobed rather than risk a false
# ill-formed flag that would wrongly drop a wrappable ctor.

_PRIMITIVE_BASES = frozenset({
    "int", "unsigned int", "long", "unsigned long", "long long",
    "unsigned long long", "short", "unsigned short", "signed char",
    "unsigned char", "char", "wchar_t", "bool", "float", "double",
    "size_t", "void", "unsigned char",
})


def _default_constructible_set(classes) -> set[str]:
    """Wrapped value classes a probe can default-construct as a value."""
    from .codegen import _default_constructible
    out: set[str] = set()
    for cls in classes:
        if cls.kind in (ClassKind.REF_COUNTED, ClassKind.EXCEPTION):
            continue
        if _default_constructible(cls):
            out.add(cls.name)
    return out


def _probe_ctor_arg(t: OCCTType, dc_set: set[str]) -> str | None:
    """A discardable value expression of type `t`, or None if constructing one
    for the probe is not known-safe.

    Arguments are cast to the exact declared parameter type so overload
    resolution is never ambiguous (a bare ``nullptr`` or ``0`` would be an
    ambiguous match when a class overloads on pointer/arithmetic types).

    A non-default-constructible value type is probed through a borrowed
    reference (``ocg_field_probe_ref``, a null dereference never executed at
    runtime -- the probe TU is only compiled and nm'd).  That keeps the ctor's
    C1 symbol in the undefined-symbol set even when no value of the parameter
    type can be fabricated, closing a gap where ctors of newly-wrapped classes
    were never audited and their missing symbols only surfaced at wrapper link
    time."""
    if t.is_handle:
        if t.is_pointer:
            return f"static_cast<opencascade::handle<{t.handle_inner}>*>(nullptr)"
        return f"occ::handle<{t.handle_inner}>()"
    if t.is_pointer:
        const = "const " if t.pointee_is_const else ""
        return f"static_cast<{const}{t.base_name}*>(nullptr)"
    if t.is_rvalue_ref:
        return None
    if t.is_ref:
        if not t.is_const:
            return None
        return _probe_ctor_arg(replace(t, is_ref=False, is_rvalue_ref=False,
                                       is_const=False), dc_set)
    if t.is_enum:
        return f"static_cast<{t.base_name}>(0)"
    base = t.base_name
    if base == "bool":
        return f"static_cast<{base}>(false)"
    if base in ("char", "signed char", "unsigned char", "wchar_t"):
        return f"static_cast<{base}>('\\0')"
    if base in _PRIMITIVE_BASES:
        return f"static_cast<{base}>(0)"
    if base in dc_set:
        return f"{base}()"
    return f"ocg_field_probe_ref<const {base}>()"


def _ctor_probe_line(cls, ctor, index: int, dc_set: set[str], ctx) -> str:
    """Probe line referencing the native constructor symbol a wrapper ctor
    emits; "" means the ctor is not probed."""
    if cls.is_abstract or cls.kind == ClassKind.EXCEPTION:
        return ""
    from .codegen import _cg
    cg = _cg(cls, ctx)
    if cg.storage == "none":
        return ""
    args = []
    for p in ctor.parameters:
        arg = _probe_ctor_arg(p.type, dc_set)
        if arg is None:
            return ""
        args.append(arg)
    joined = ", ".join(args)
    if cg.storage == "handle":
        # `new Cls(args)` also covers plain unique_ptr storage (make_unique is
        # new underneath) and references the same C1 symbol + class operator new.
        return (f"::{cls.name}* ocg_ctor_{index:05d}() "
                f"{{ return new ::{cls.name}({joined}); }}")
    if cls.wrapper_name in ctx.stdalloc:
        # stdalloc wrappers placement-construct on Standard::Allocate memory and
        # never call the class operator new (which allocator-tagged classes hide
        # and protected bases make inaccessible); a discarded prvalue references
        # the same C1 ctor symbol without pulling in operator new.
        return (f"void ocg_ctor_{index:05d}() "
                f"{{ (void)::{cls.name}({joined}); }}")
    # unique_ptr storage (plain operator new); the wrapper emits make_unique.
    return (f"void ocg_ctor_{index:05d}() "
            f"{{ (void)::{cls.name}({joined}); }}")


def _default_ctor_probe_line(cls, ctx, index: int) -> str:
    """Probe line for the native default-construction a wrapper's own default
    constructor emits (``_native()`` or ``_handle = new Cls()``).

    The default ctor itself is never bound as a factory (see
    ``_default_ctor``), so it is invisible to the ctor probes above; yet a
    value/handle-stored wrapper references its symbol from the member init.
    """
    if cls.kind == ClassKind.EXCEPTION:
        return ""
    from .codegen import _cg
    cg = _cg(cls, ctx)
    if cg.storage == "handle":
        if not (cls.has_public_default_ctor and not cls.is_abstract):
            return ""
        return (f"::{cls.name}* ocg_dctor_{index:05d}() "
                f"{{ return new ::{cls.name}(); }}")
    if cg.storage == "native" and not cg.inherited_native:
        return (f"void ocg_dctor_{index:05d}() "
                f"{{ (void)::{cls.name}(); }}")
    return ""


def _copy_probe_line(cls, ctx, index: int) -> str:
    """Probe the copy operation a wrapped value/reference return emits (native
    wrappers copy-assign into ``_native``; unique_ptr wrappers copy-construct
    via make_unique).  A rejection means the OCCT type is implicitly
    non-copyable (copy semantics deleted through members/bases the extractor
    cannot see), so methods returning it cannot be bound.

    The operation is wrapped in a *template* helper: when it is ill-formed the
    instantiation happens inside an OCCT template member (e.g. a
    ``NCollection_Map<Cell, Hasher>::operator=`` inlined in a class body), and
    a bare expression in this non-template function would anchor GCC's
    "required from here" inside the OCCT header instead of at this probe line.
    The helper template puts the probe line in the instantiation chain.
    """
    from .codegen import _cg
    cg = _cg(cls, ctx)
    if cg.storage not in ("native", "unique_ptr"):
        return ""
    helper = ("ocg_copy_probe_construct<_C>()" if cg.storage == "unique_ptr"
              else "ocg_copy_probe_assign<_C>()")
    return (f"void ocg_copy_{index:05d}() "
            f"{{ (void)[] {{ using _C = ::{cls.name}; {helper}; }}; }}")


# ---------------------------------------------------------------------------
# Probe TU generation
# ---------------------------------------------------------------------------

def _type_headers(t: OCCTType, ctx: tm.TypeContext) -> list[str]:
    """OCCT header basenames a signature type requires to be complete."""
    if t.is_handle and t.handle_inner in ctx.wrapped:
        return [ctx.occt_headers.get(t.handle_inner, t.handle_inner + ".hxx")]
    key = tm._wrapped_key(t.base_name, ctx)
    if key is not None:
        return [ctx.occt_headers.get(key, key + ".hxx")]
    # A templated type the header map has no key for (e.g. spelled with
    # defaulted template args): the class template header is still required.
    m = re.match(r"^([A-Za-z_]\w*)<", t.base_name)
    if m:
        tname = m.group(1)
        return [ctx.occt_headers.get(tname, tname + ".hxx")]
    return []


def probe_headers(classes, ctx: tm.TypeContext, install: OCCTInstall) -> list[Path]:
    """OCCT headers the probe TU needs, include-closure ordered (deps first).

    The BFS closure only orders headers linked by ``#include``; several OCCT
    headers are not self-contained and rely on a type being declared by an
    earlier include (e.g. ``GeomFill_SimpleBound.hxx`` uses ``Adaptor3d_Curve``
    without including its header).  ``_hygiene_order`` closes that gap so the
    probe compiles deterministically regardless of set iteration order.
    """
    names: set[str] = set()
    for cls in classes:
        if cls.header_file:
            names.add(Path(cls.header_file).name)
        for e in cls.extra_occt_includes:
            if e and (install.include_dir / e).exists():
                names.add(e)
        for base in cls.base_classes:
            if base in ctx.occt_classes:
                names.add(ctx.occt_headers.get(base, base + ".hxx"))
        for method in cls.all_methods:
            for p in method.parameters:
                names.update(_type_headers(p.type, ctx))
            if method.return_type is not None:
                names.update(_type_headers(method.return_type, ctx))
        for f in cls.fields:
            names.update(_type_headers(f.type, ctx))
    paths = [install.include_dir / n for n in names
             if (install.include_dir / n).exists()]
    return _hygiene_order(include_closure(paths, install, include_self=True),
                          ctx)


_CLASSNAME_RE_CACHE: dict[int, re.Pattern] = {}


def _hygiene_order(closure: list[Path], ctx: tm.TypeContext) -> list[Path]:
    """Deterministically reorder ``closure`` so each header comes after the
    declaring header of every OCCT class name it references."""
    idx = {h.name: i for i, h in enumerate(closure)}
    class_idx: dict[str, int] = {}
    for cls, hdr in ctx.occt_headers.items():
        j = idx.get(Path(hdr).name) if hdr else None
        if j is not None:
            class_idx[cls] = j
    if not class_idx:
        return closure
    size = len(class_idx)
    rx = _CLASSNAME_RE_CACHE.get(size)
    if rx is None:
        names = sorted(class_idx)
        rx = re.compile(r"\b(?:%s)\b" % "|".join(map(re.escape, names)))
        _CLASSNAME_RE_CACHE[size] = rx

    deps: list[set[int]] = [set() for _ in closure]
    for i, h in enumerate(closure):
        try:
            text = h.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for cls in rx.findall(text):
            j = class_idx[cls]
            if j != i:
                deps[i].add(j)
    for name, must_follow in _HEADER_PRECEDENCE.items():
        i = idx.get(name)
        if i is None:
            continue
        for pre in must_follow:
            j = idx.get(pre)
            if j is not None and j != i:
                deps[i].add(j)

    heap = [i for i, d in enumerate(deps) if not d]
    heapq.heapify(heap)
    emitted = [False] * len(closure)
    out: list[Path] = []
    while heap:
        i = heapq.heappop(heap)
        if emitted[i]:
            continue
        emitted[i] = True
        out.append(closure[i])
        for k in range(len(closure)):
            if not emitted[k] and i in deps[k]:
                deps[k].discard(i)
                if not deps[k]:
                    heapq.heappush(heap, k)
    if len(out) != len(closure):
        rest = [i for i in range(len(closure)) if not emitted[i]]
        rest_set = set(rest)
        pred: dict[int, set[int]] = {i: set() for i in rest}
        for name, must_follow in _HEADER_PRECEDENCE.items():
            i = idx.get(name)
            if i is None or i not in rest_set:
                continue
            for pre in must_follow:
                j = idx.get(pre)
                if j is not None and j in rest_set and j != i:
                    pred[i].add(j)
        heap2 = [i for i in rest if not pred[i]]
        heapq.heapify(heap2)
        emitted2: set[int] = set()
        while heap2:
            i = heapq.heappop(heap2)
            if i in emitted2:
                continue
            emitted2.add(i)
            out.append(closure[i])
            for k in rest:
                if k not in emitted2 and i in pred[k]:
                    pred[k].discard(i)
                    if not pred[k]:
                        heapq.heappush(heap2, k)
        for i in rest:
            if i not in emitted2:
                out.append(closure[i])
    return out


def generate_probe_tu(modules, ctx: tm.TypeContext, install: OCCTInstall) -> str:
    """A TU referencing every method the wrappers will emit at link time."""
    classes = [cls for m in modules for cls in m.classes if not cls.skip]
    headers = probe_headers(classes, ctx, install)
    out: list[str] = [
        "// Auto-generated symbol audit probe TU -- DO NOT EDIT",
    ]
    out.extend(f"#include <{h.name}>" for h in headers)
    out.append("")
    out.append("#include <utility>   // std::declval (field-accessor probes)")
    out.append("#include <type_traits>  // std::remove_reference (field probes)")
    out.append("")
    out.append("// Compile-time-only reference into a hypothetical instance; the")
    out.append("// field probes copy/assign through it without ever constructing.")
    out.append("template <typename T> inline T& ocg_field_probe_ref() noexcept {")
    out.append("    T* p = nullptr;")
    out.append("    return *p;")
    out.append("}")
    out.append("")
    out.append("// Template-wrapped copy probes (see _copy_probe_line): keeping the")
    out.append("// copy operation inside a template anchors GCC's instantiation")
    out.append("// chain at the probe line instead of inside an OCCT header.")
    out.append("template <typename T> inline void ocg_copy_probe_assign() {")
    out.append("    ocg_field_probe_ref<T>() = ocg_field_probe_ref<const T>();")
    out.append("}")
    out.append("template <typename T> inline void ocg_copy_probe_construct() {")
    out.append("    T a(ocg_field_probe_ref<const T>()); (void)a;")
    out.append("}")
    out.append("")
    out.append("// One discarded address-of per generated wrapper method; overloads")
    out.append("// are disambiguated by explicit pointer casts.  The undefined symbols")
    out.append("// of this TU are the member/static symbols the wrappers will emit.")
    lines: list[str] = []
    index = 0
    dc_set = _default_constructible_set(classes)
    for cls in classes:
        for method in (cls.methods + cls.operators + cls.static_methods):
            if method.skip or method.is_deleted or method.is_pure_virtual \
                    or method.is_variadic:
                continue
            lines.append(f"    // {_method_display_name(cls, method)}")
            lines.append(f"    {_probe_line(cls, method, index, ctx)}")
            index += 1
        for ctor in cls.constructors:
            if ctor.skip or ctor.is_deleted or ctor.is_pure_virtual:
                continue
            line = _ctor_probe_line(cls, ctor, index, dc_set, ctx)
            if not line:
                continue
            lines.append(f"    // {_method_display_name(cls, ctor)}")
            lines.append(f"    {line}")
            index += 1
        line = _default_ctor_probe_line(cls, ctx, index)
        if line:
            lines.append(f"    // {cls.name}::{cls.name} (default construction)")
            lines.append(f"    {line}")
            index += 1
        for f in cls.fields:
            if f.skip or not f.is_public:
                continue
            # Array members are mapped element-wise by the accessor (not by
            # value copy), so a by-value copy probe would falsely reject them.
            if f.type.spelling.rstrip().endswith("]"):
                continue
            lines.append(f"    // {cls.name}::{f.name} (field accessor)")
            lines.append(f"    {_field_probe_line(cls, f, index)}")
            index += 1
        line = _copy_probe_line(cls, ctx, index)
        if line and cls.returnable:
            lines.append(f"    // {cls.name}::copy (return value)")
            lines.append(f"    {line}")
            index += 1
    if not lines:
        out.append("auto const ocg_sym_none = 0;")
    else:
        out.extend(lines)
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# nm parsing
# ---------------------------------------------------------------------------

def _nm_undefined(obj: Path, nm_tool: str) -> list[tuple[str, str]]:
    out = subprocess.run([nm_tool, "-u", "-C", str(obj)],
                         capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"nm failed on {obj}: {out.stderr[:1000]}")
    syms: list[tuple[str, str]] = []
    for line in out.stdout.splitlines():
        line = line.strip()
        m = re.match(r"^([A-Za-z])\s+(.*)$", line)
        if m:
            syms.append((m.group(1), m.group(2)))
    return syms


def _defined_symbols(lib_dir: Path, nm_tool: str) -> set[str]:
    static = sorted(lib_dir.glob("*.a"))
    shared = sorted(lib_dir.glob("*.so*"))
    defined: set[str] = set()
    for lib in static or shared:
        args = [nm_tool, "-C", "--defined-only"]
        if not static:
            args.insert(1, "-D")
        out = subprocess.run(args + [str(lib)], capture_output=True, text=True)
        for line in out.stdout.splitlines():
            line = line.strip()
            if not line or line.endswith(":"):
                continue
            m = re.match(r"^[0-9a-fA-F]{8,16}\s+([A-Za-z])\s+(.*)$", line)
            if m:
                defined.add(m.group(2))
    return defined


# ---------------------------------------------------------------------------
# Audit runner
# ---------------------------------------------------------------------------

def _occt_lib_dir(project_root: Path | None, install: OCCTInstall) -> Path | None:
    triplet = os.environ.get("VCPKG_DEFAULT_TRIPLET", "x64-linux")
    candidates: list[Path] = []
    if project_root is not None:
        candidates.append(project_root / "vcpkg" / "installed" / triplet / "lib")
    candidates.append(install.include_dir.parent.parent / "lib")
    for d in candidates:
        if d.is_dir() and list(d.glob("libTKMath.*")):
            return d
    return None


def _gcc_args(args: list[str]) -> list[str]:
    """Filter libclang/compile-db flags that g++ would reject or that drag in
    godot-cpp (the `-include occt_guard.hxx` of the real compile DB)."""
    out: list[str] = []
    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg.startswith("-resource-dir"):
            continue
        if arg in ("-include", "-Xclang", "-x", "-c", "-o"):
            skip_next = True
            continue
        if arg.startswith("-isystem "):
            out += ["-isystem", arg[len("-isystem "):]]
            continue
        if arg.startswith("-MJ") or arg.startswith("-dependency-file"):
            continue
        out.append(arg)
    return out


def _write_lines(path: Path, lines: list[str]) -> None:
    """Write findings as one-per-line, or an empty file when there are none.

    A bare trailing newline (``"\n"``) would make ``test -s`` treat the file
    as non-empty, so an empty result must produce a zero-byte file.
    """
    path.write_text("" if not lines else "\n".join(sorted(lines)) + "\n")


def run_audit(probe_path: Path, work_dir: Path, out_path: Path,
              project_root: Path | None, install: OCCTInstall,
              args: list[str], occt_classes: set[str],
              compiler: str = "g++", nm_tool: str = "nm",
              illformed_path: Path | None = None) -> list[str]:
    """Compile the probe, diff undefined vs library-defined symbols.

    Returns the sorted list of missing member symbols (also written to
    `out_path`).  When the probe fails to compile, the offending members are
    extracted from the compiler diagnostics and written to `illformed_path`
    (default: ``<work_dir>/illformed.txt`` as ``Class::method`` lines) so
    pass-2 regeneration can skip them; the returned list is then empty.
    Raises if the tools/libs are unavailable (caller decides how to degrade).
    """
    if shutil.which(compiler) is None or shutil.which(nm_tool) is None:
        raise FileNotFoundError(
            f"symbol audit needs {compiler!r} and {nm_tool!r} on PATH")
    lib_dir = _occt_lib_dir(project_root, install)
    if lib_dir is None:
        raise FileNotFoundError("no OCCT library directory found for symbol audit")

    work_dir.mkdir(parents=True, exist_ok=True)
    if illformed_path is None:
        illformed_path = work_dir / "illformed.txt"
    # Both outputs are refreshed on every run so a stale file from a previous
    # invocation (e.g. a probe that has since been fixed) cannot leak into
    # pass-2 regeneration.
    out_path.write_text("")
    illformed_path.write_text("")
    obj = work_dir / (probe_path.stem + ".o")
    cmd = [compiler, "-std=gnu++17", "-fPIC", "-w",
           "-ftemplate-backtrace-limit=0", "-c",
           str(probe_path), "-o", str(obj), *_gcc_args(args)]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        # A generated method whose body does not compile (e.g. a synthesized
        # NCollection_Vec3<unsigned long>::cwiseAbs instantiating an ambiguous
        # std::abs) aborts the probe.  Map each failing probe line back to its
        # "// Class::method" comment and skip exactly those methods in pass 2.
        illformed = _extract_illformed(res.stderr, probe_path)
        if not illformed:
            print("symbol audit : probe failed; no methods mapped to errors"
                  f" (tried {probe_path.name}:NN) -- using pass-1 output",
                  file=sys.stderr)
            print(res.stderr[-2000:], file=sys.stderr, end="")
            raise RuntimeError("symbol audit probe failed to compile")
        _write_lines(illformed_path, sorted(illformed))
        return []

    defined = _defined_symbols(lib_dir, nm_tool)
    missing: set[str] = set()
    for letter, name in _nm_undefined(obj, nm_tool):
        if letter != "U":
            continue
        cls = name.split("::")[0]
        if cls not in occt_classes:
            continue
        if name not in defined:
            missing.add(name)
    _write_lines(out_path, sorted(missing))
    return sorted(missing)


def _extract_illformed(stderr: str, probe_path: Path) -> set[str]:
    """Class::method names of probe lines rejected by the compiler.

    Every probe line is preceded by a ``// Class::method`` comment; GCC/Clang
    diagnostics reference the offending ``ocg_sym_*`` line by file:line, so the
    nearest preceding comment names the method whose instantiation failed.
    """
    probe = probe_path.read_text().splitlines()
    line_index: dict[int, str] = {}
    last_comment = ""
    for no, text in enumerate(probe, start=1):
        line = text.strip()
        if line.startswith("// ") and "::" in line:
            last_comment = line[3:].strip()
        elif "ocg_sym_" in line or "ocg_ctor_" in line or "ocg_dctor_" in line \
                or "ocg_field_" in line or "ocg_copy_" in line:
            line_index[no] = last_comment
    out: set[str] = set()
    for m in re.finditer(rf"{re.escape(probe_path.name)}:(\d+):", stderr):
        target = line_index.get(int(m.group(1)), "")
        if target:
            out.add(target)
    return out


def load_illformed(path: Path) -> set[str]:
    """Read an ill-formed-methods file (one ``Class::method`` per line)."""
    illformed: set[str] = set()
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        illformed.add(line)
    return illformed


def apply_illformed(modules, illformed: set[str]) -> int:
    """Skip every generated method whose instantiation does not compile.

    These are OCCT template members that are ill-formed for the substituted
    arguments (e.g. ``NCollection_Vec3<unsigned long>::cwiseAbs`` calling an
    ambiguous ``std::abs``); the API itself is unusable there, so the method
    is dropped exactly as if it were unmappable.

    ``Class::Class (default construction)`` entries come from the probe of the
    wrapper's own default constructor (``_native()`` / ``new Cls()``); a
    rejection means ``T()`` does not exist even though the extractor could not
    tell (libclang misses a deleted implicit default ctor, e.g. when the base
    class suppresses it).  The class is pinned not-default-constructible so
    codegen falls back to unique_ptr storage.
    """
    skipped = 0
    for module in modules:
        for cls in module.classes:
            if cls.skip:
                continue
            dctor = f"{cls.name}::{cls.name} (default construction)"
            if dctor in illformed:
                cls.default_constructible = False
                cls.has_public_default_ctor = False
                skipped += 1
                # No `continue` here: a class can be flagged both as not
                # default-constructible AND not returnable (the dctor label
                # must not shadow the copy-return label, or the copy probe is
                # re-emitted next pass and convergence never terminates).
            for method in cls.all_methods:
                if method.skip:
                    continue
                # _extract_illformed records operators as ``Class::operator()``
                # (via _method_display_name), not the raw ``Class::()``; match
                # with the same spelling so operator methods are actually skipped.
                if _method_display_name(cls, method) in illformed:
                    method.skip = True
                    method.skip_reason = ("ill-formed instantiation "
                                          "(OCCT member does not compile for "
                                          "the substituted template args)")
                    skipped += 1
            for f in cls.fields:
                if f.skip:
                    continue
                # ``Class::field (field accessor)`` entries come from the probe
                # of the generated get/set property accessors: a member whose
                # type has implicitly deleted copy semantics (the getter copies
                # it, the setter assigns it) cannot be exposed as a property.
                label = f"{cls.name}::{f.name} (field accessor)"
                if label in illformed:
                    f.skip = True
                    f.skip_reason = ("ill-formed field accessor "
                                     "(field type is not copyable)")
                    skipped += 1
            # ``Class::copy (return value)`` entries come from the probe of the
            # copy operation a wrapped return emits (copy-assign for native
            # storage, copy-construct for unique_ptr storage).  A rejection
            # means the OCCT type is implicitly non-copyable through members or
            # bases; value/reference returns of it cannot be bound.
            if f"{cls.name}::copy (return value)" in illformed:
                cls.returnable = False
                skipped += 1
    return skipped


# ABI-tagged std templates demangle with the `__cxx11` inline namespace and
# the full default template arguments; the IR's `std::basic_*<char>` short
# forms are mapped back so pass-2 symbol matching is independent of libstdc++.
# libstdc++'s demangler also prints the standard typedefs (`std::ostream`,
# `std::string`) where the IR keeps the underlying `std::basic_*<char>` form;
# both spellings must collapse onto the same symbol name.
_STD_TEMPLATE_MAP = {
    "std::__cxx11::basic_string<char, std::char_traits<char>, std::allocator<char> >": "std::basic_string<char>",
    "std::basic_string<char, std::char_traits<char>, std::allocator<char> >": "std::basic_string<char>",
    "std::__cxx11::basic_stringstream<char, std::char_traits<char>, std::allocator<char> >": "std::basic_stringstream<char>",
    "std::basic_stringstream<char, std::char_traits<char>, std::allocator<char> >": "std::basic_stringstream<char>",
    "std::__cxx11::basic_ostream<char, std::char_traits<char> >": "std::basic_ostream<char>",
    "std::basic_ostream<char, std::char_traits<char> >": "std::basic_ostream<char>",
    "std::__cxx11::basic_istream<char, std::char_traits<char> >": "std::basic_istream<char>",
    "std::basic_istream<char, std::char_traits<char> >": "std::basic_istream<char>",
    "std::ostream": "std::basic_ostream<char>",
    "std::istream": "std::basic_istream<char>",
    "std::string": "std::basic_string<char>",
    "std::stringstream": "std::basic_stringstream<char>",
}


def _normalize_symbol(name: str) -> str:
    for full, short in _STD_TEMPLATE_MAP.items():
        if full in name:
            name = name.replace(full, short)
    # The Itanium demangler separates nested closing brackets with a space
    # (`handle<NCollection_HArray1<double> >`); our source spellings (and the
    # wrapper's undefined symbols) use the adjacent `>>` form.  Normalize every
    # nesting level so symbol matching is independent of the demangler.
    while "> >" in name:
        name = name.replace("> >", ">>")
    return name


def load_missing(path: Path) -> set[str]:
    """Read a missing-symbols file (one demangled symbol per line)."""
    missing: set[str] = set()
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        missing.add(_normalize_symbol(line))
    return missing


def apply_missing(modules, missing: set[str]) -> int:
    """Skip every generated method whose link symbol is absent from the libs."""
    skipped = 0
    for module in modules:
        for cls in module.classes:
            if cls.skip:
                continue
            for method in cls.all_methods:
                if method.skip:
                    continue
                if symbol_for_method(cls, method) in missing:
                    method.skip = True
                    method.skip_reason = "missing OCCT symbol (not exported by linked libraries)"
                    if method.kind == MethodKind.CONSTRUCTOR and not method.parameters:
                        # The wrapper's own default ctor constructs the native
                        # object (`_native()` / `_handle = new Cls()`); when the
                        # OCCT default ctor is absent from the libs, fall back to
                        # no-default-construction (unique_ptr / null handle).
                        # NB: only a zero-arg CONSTRUCTOR means this -- a missing
                        # zero-arg regular method (e.g. an unexported accessor)
                        # must not demote the whole class to unique_ptr storage.
                        cls.has_public_default_ctor = False
                    skipped += 1
    return skipped
