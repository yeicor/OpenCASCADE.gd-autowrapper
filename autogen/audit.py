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

import heapq
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from .model import MethodKind, OCCTType
from .occt import OCCTInstall, include_closure
from . import typemap as tm

# Source spelling of an operator for pointer casts / symbol names.
_OPERATOR_SPELLING = {
    "unary_minus": "-", "unary_plus": "+", "*deref": "*", "call": "()",
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


def _probe_line(cls, method, index: int) -> str:
    params = ", ".join(render_source_type(p.type) for p in method.parameters)
    ret_is_void = (method.return_type is None
                   or (method.return_type.is_void
                       and not method.return_type.is_pointer))
    ret = ("void" if ret_is_void else render_source_type(method.return_type))
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
        j = idx.get(hdr)
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
        for i in range(len(closure)):
            if not emitted[i]:
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
    out.append("// One discarded address-of per generated wrapper method; overloads")
    out.append("// are disambiguated by explicit pointer casts.  The undefined symbols")
    out.append("// of this TU are the member/static symbols the wrappers will emit.")
    lines: list[str] = []
    index = 0
    for cls in classes:
        for method in (cls.methods + cls.operators + cls.static_methods):
            if method.skip or method.is_deleted or method.is_pure_virtual \
                    or method.is_variadic:
                continue
            lines.append(f"    // {_method_display_name(cls, method)}")
            lines.append(f"    {_probe_line(cls, method, index)}")
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
    cmd = [compiler, "-std=gnu++17", "-fPIC", "-w", "-c",
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
        elif "ocg_sym_" in line:
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
    """
    skipped = 0
    for module in modules:
        for cls in module.classes:
            if cls.skip:
                continue
            for method in cls.all_methods:
                if method.kind == MethodKind.CONSTRUCTOR or method.skip:
                    continue
                if f"{cls.name}::{method.name}" in illformed:
                    method.skip = True
                    method.skip_reason = ("ill-formed instantiation "
                                          "(OCCT member does not compile for "
                                          "the substituted template args)")
                    skipped += 1
    return skipped


# ABI-tagged std templates demangle with the `__cxx11` inline namespace and
# the full default template arguments; the IR's `std::basic_*<char>` short
# forms are mapped back so pass-2 symbol matching is independent of libstdc++.
_STD_TEMPLATE_MAP = {
    "std::__cxx11::basic_string<char, std::char_traits<char>, std::allocator<char> >": "std::basic_string<char>",
    "std::basic_string<char, std::char_traits<char>, std::allocator<char> >": "std::basic_string<char>",
    "std::__cxx11::basic_stringstream<char, std::char_traits<char>, std::allocator<char> >": "std::basic_stringstream<char>",
    "std::basic_stringstream<char, std::char_traits<char>, std::allocator<char> >": "std::basic_stringstream<char>",
    "std::__cxx11::basic_ostream<char, std::char_traits<char> >": "std::basic_ostream<char>",
    "std::basic_ostream<char, std::char_traits<char> >": "std::basic_ostream<char>",
    "std::__cxx11::basic_istream<char, std::char_traits<char> >": "std::basic_istream<char>",
    "std::basic_istream<char, std::char_traits<char> >": "std::basic_istream<char>",
}


def _normalize_symbol(name: str) -> str:
    for full, short in _STD_TEMPLATE_MAP.items():
        if full in name:
            return name.replace(full, short)
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
                if method.kind == MethodKind.CONSTRUCTOR or method.skip:
                    continue
                if symbol_for_method(cls, method) in missing:
                    method.skip = True
                    method.skip_reason = "missing OCCT symbol (not exported by linked libraries)"
                    skipped += 1
    return skipped
