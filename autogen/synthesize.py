"""Synthesize a concrete wrapper ClassDecl from a class-template specialization.

All structure comes from libclang (the CLASS_TEMPLATE cursor: members, macros
already expanded, source-text spellings).  Dependent types are resolved by
re-parsing a tiny probe TU whose struct inherits the *instantiated*
specialization, then running the pipeline's existing ``make_type`` on each
alias.  No source is hand-parsed and no class body is reconstructed.
"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import replace
from pathlib import Path

import clang.cindex as C
from clang.cindex import CursorKind

from .compile_db import ensure_occt_args, find_resource_dir
from .extract import _extract_class
from .model import ModuleDecl, occt_name_to_wrapper
from .occt import find_occt_install
from .parser import parse_header
from .types import make_type

# autogen/ -> autowrapper/ -> project root.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _split_top_level(s: str) -> list[str]:
    """Split on top-level commas (angle-bracket depth aware)."""
    parts: list[str] = []
    depth = 0
    cur: list[str] = []
    for ch in s:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return parts


def _chunk_noncopyable(chunk: str, noncopyable: set[str]) -> bool:
    """True if a template-argument chunk is a value (not handle/pointer/ref)
    OCCT class with deleted/broken copy semantics."""
    chunk = chunk.strip()
    if not chunk:
        return False
    if chunk.startswith("opencascade::handle"):
        return False  # handles copy regardless of the pointee
    if chunk.endswith("*") or chunk.endswith("&"):
        return False  # pointers/references copy
    m = re.match(r"^[A-Za-z_]\w*\s*<(.*)>\s*$", chunk, re.S)
    if m:
        # Nested specialization: only the inner VALUE args can be non-copyable.
        return any(_chunk_noncopyable(inner, noncopyable)
                   for inner in _split_top_level(m.group(1)))
    return chunk in noncopyable


def _noncopyable_classes(modules: list[ModuleDecl]) -> set[str]:
    """OCCT value classes whose copy semantics are deleted or broken."""
    return {cls.name for m in modules for cls in m.classes
            if not cls.has_copy_assignment}


def _specialization_broken(spec_name: str, noncopyable: set[str]) -> bool:
    """A container specialization is ill-formed when a value template arg is
    a non-copyable OCCT class (e.g. NCollection_Sequence<CSLib_Class2d>, whose
    Append/Insert copy the item).  handle<X>, pointer and reference args are
    exempt."""
    m = re.match(r"^[A-Za-z_]\w*\s*<(.*)>\s*$", spec_name, re.S)
    if not m:
        return False
    return any(_chunk_noncopyable(inner, noncopyable)
               for inner in _split_top_level(m.group(1)))


def filter_noncopyable(classes: list[object], modules: list[ModuleDecl]) -> list[object]:
    """Drop synthesized specializations over non-copyable value args.

    Applied both when synthesizing fresh and when loading a cached spec list,
    so a stale cache cannot resurrect a specialization whose members do not
    compile (the probe TU instantiates every wrapped member symbol).
    """
    noncopyable = _noncopyable_classes(modules)
    kept = []
    for cls in classes:
        if _specialization_broken(getattr(cls, "name", ""), noncopyable):
            print(f"synth          : SKIP {cls.name} (non-copyable template arg)",
                  file=__import__("sys").stderr)
            continue
        kept.append(cls)
    return kept


def filter_undeclarable(classes: list[object], install: Path,
                        modules: list[ModuleDecl] | None = None) -> list[object]:
    """Drop cached specializations whose args are not publicly nameable.

    Rebuilds the spec map from cached ClassDecls so a stale cache cannot
    resurrect a specialization over private/protected nested types (those
    cannot be referenced from generated wrappers and trip the probe TU).
    """
    specs: dict[str, tuple[str, list[str]]] = {}
    for cls in classes:
        m = _SPEC_RE.match(getattr(cls, "name", ""))
        if not m:
            continue
        header = getattr(cls, "header_file", "") or SYNTHESIZABLE_TEMPLATES.get(m.group(1), "")
        args = _split_args(m.group(2))
        if header and args:
            specs[cls.name] = (header, args)
    bad = _undeclarable_specs(specs, install, modules) if specs else set()
    header_map = _build_header_map(modules) if modules else None
    kept = []
    for cls in classes:
        if getattr(cls, "name", "") in bad:
            print(f"synth          : SKIP {cls.name} (template arg not publicly "
                  "nameable)", file=__import__("sys").stderr)
            continue
        m = _SPEC_RE.match(getattr(cls, "name", ""))
        if m and header_map:
            # A stale cache may carry a name-derived header that does not
            # exist (Graphic3d_Attribute.hxx); rebind to the declaring header
            # from the scanned IR so the wrapper compiles.
            args = _split_args(m.group(2))
            own = getattr(cls, "header_file", "") or ""
            cls.extra_occt_includes = [
                i for i in _collect_includes(args, header_map=header_map)
                if i != own]
        kept.append(cls)
    return kept


# Class templates that the pipeline can synthesize, by template name.  The
# header filename is the template's own header; argument headers are derived
# from the specialization (see _collect_includes).
SYNTHESIZABLE_TEMPLATES: dict[str, str] = {
    "NCollection_Array1": "NCollection_Array1.hxx",
    "NCollection_Array2": "NCollection_Array2.hxx",
    "NCollection_Vec2": "NCollection_Vec2.hxx",
    "NCollection_Vec3": "NCollection_Vec3.hxx",
    "NCollection_Sequence": "NCollection_Sequence.hxx",
    "NCollection_List": "NCollection_List.hxx",
    "NCollection_Map": "NCollection_Map.hxx",
    "NCollection_IndexedMap": "NCollection_IndexedMap.hxx",
    "NCollection_IndexedDataMap": "NCollection_IndexedDataMap.hxx",
    "NCollection_DataMap": "NCollection_DataMap.hxx",
    "NCollection_HArray1": "NCollection_HArray1.hxx",
    "NCollection_HArray2": "NCollection_HArray2.hxx",
    "NCollection_HSequence": "NCollection_HSequence.hxx",
    "NCollection_Set": "NCollection_Set.hxx",
}

_SPEC_RE = re.compile(r"^([A-Za-z_]\w*)<(.*)>$")

_OCG_RE = re.compile(r"\bOcg[A-Za-z_][A-Za-z0-9_]*\b")


def _collect_template_specs(modules: list[ModuleDecl]) -> dict[str, tuple[str, list[str]]]:
    """Distinct class-template specializations used in OCCT signatures.

    Returns ``{ "NCollection_Array2<gp_Pnt>": (header, [args...]) }`` for every
    specialization of a synthesizable template that appears in any method,
    constructor, static method, operator or field of the scanned IR.
    """
    specs: dict[str, tuple[str, list[str]]] = {}

    def handle(t) -> None:
        if t is None:
            return
        b = getattr(t, "base_name", "")
        m = _SPEC_RE.match(b)
        if not m:
            return
        tname, argstr = m.group(1), m.group(2)
        header = SYNTHESIZABLE_TEMPLATES.get(tname)
        if header is None:
            return
        args = _split_args(argstr)
        if not args:
            return
        key = f"{tname}<{', '.join(args)}>"
        specs.setdefault(key, (header, args))

    for module in modules:
        for cls in module.classes:
            for m in cls.all_methods:
                handle(m.return_type)
                for p in m.parameters:
                    handle(p.type)
            for f in cls.fields:
                if not f.is_public:
                    # A specialization reachable only through a private field
                    # can never be named by a wrapper; synthesizing it would
                    # only surface unusable methods (and, for private nested
                    # types, probe compile failures).
                    continue
                handle(f.type)
    return specs


def _collect_demo_refs(project_root: Path) -> set[str]:
    """Wrapper names referenced by the demo project's GDScript sources."""
    out: set[str] = set()
    demo = Path(project_root) / "demo"
    if not demo.is_dir():
        return out
    for p in demo.rglob("*.gd*"):
        if p.suffix not in (".gd", ".gd.disabled"):
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        out.update(_OCG_RE.findall(text))
    return out


def synthesize_used(project_root: Path,
                    modules: list[ModuleDecl]) -> list[object]:
    """Synthesize the class-template specializations the demo code uses.

    Usage-driven: only specializations whose wrapper name appears in the demo
    GDScript sources (and that occur in the scanned OCCT IR) are synthesized,
    keeping the generated surface small and deterministic.
    """
    demo_refs = _collect_demo_refs(project_root)
    if not demo_refs:
        return []
    specs = _collect_template_specs(modules)
    if not specs:
        return []
    install = find_occt_install(_default_project_root())
    out: list[object] = []
    for key, (header, args) in sorted(specs.items()):
        tname = key.split("<", 1)[0]
        if occt_name_to_wrapper(key, "NCollection") not in demo_refs:
            continue
        try:
            cls = synth_template_spec(header, tname, args, install=install,
                                      header_map=_build_header_map(modules))
            cls.name = key  # exact spelling used in signatures
            out.append(cls)
        except Exception as e:  # noqa: BLE001
            print(f"synthesize    : SKIP {key}: {e}", file=__import__("sys").stderr)
    return out


def _undeclarable_specs(specs: dict[str, tuple[str, list[str]]],
                        install: Path,
                        modules: list[ModuleDecl] | None = None) -> set[str]:
    """Spec keys whose template arguments are not publicly nameable.

    A free-namespace ``using OcgUndeclN = Spec<args>;`` fails to compile when
    any argument names a private/protected nested type (e.g.
    ``NCollection_Array1<Aspect_VKeySet::KeyState>`` where ``KeyState`` is a
    private nested struct).  No wrapper can ever name such a type, so the spec
    must not be synthesized at all; otherwise every one of its methods trips
    the symbol-audit probe.  Member-level breakage (e.g. an ambiguous ``abs``
    in ``NCollection_Vec3<unsigned long>::cwiseAbs``) is *not* caught here --
    that spec is declarable and is handled by the audit's ill-formed-method
    skipping instead.
    """
    args = ensure_occt_args([], install.include_dir)
    rd = find_resource_dir()
    if rd:
        args.append(f"-resource-dir={rd}")
    header_map = _build_header_map(modules) if modules else None
    includes: set[str] = set()
    for key, (header, _) in specs.items():
        includes.add(header)
    for key, (_, as_) in specs.items():
        includes.update(_collect_includes(as_, header_map=header_map))
    # Some specializations carry args whose derived "header" does not exist
    # (e.g. array bounds); a missing #include would abort the whole batch TU,
    # so only pull in headers that are actually present.
    include_dir = install.include_dir
    includes = {i for i in includes if (include_dir / i).exists()}
    lines: list[str] = [f"#include <{i}>" for i in sorted(includes)]
    lines.append("")
    lines.append("namespace ocg_undecl {")
    key_lines: dict[str, int] = {}
    for i, key in enumerate(sorted(specs)):
        lines.append(f"using OcgUndecl{i} = {key};")
        key_lines[key] = len(lines)
    lines.append("}")
    src = "\n".join(lines) + "\n"
    with tempfile.NamedTemporaryFile(suffix=".cpp", mode="w", delete=False) as f:
        f.write(src)
        tmp = f.name
    try:
        index = C.Index.create()
        tu = index.parse(tmp, args=args + ["-x", "c++", "-I",
                                           str(install.include_dir)])
        out: set[str] = set()
        for d in tu.diagnostics:
            if d.severity >= C.Diagnostic.Error:
                for key, line_no in key_lines.items():
                    if d.location.line == line_no:
                        out.add(key)
                        break
        return out
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def synthesize_all(modules: list[ModuleDecl]) -> list[object]:
    """API-driven: synthesize *every* specialization of a synthesizable
    template that appears in any scanned signature, not just the ones the demo
    references.  This is what closes the NCollection coverage gap: a method is
    bindable once its container argument types have concrete wrapper classes.

    Specializations that cannot be instantiated cleanly (nested templates,
    args naming skipped classes, ...) are reported in `last_failures` and left
    for the typemap/synthesis generalizations that target them.
    """
    specs = _collect_template_specs(modules)
    out: list[object] = []
    failures: list[str] = []
    if not specs:
        synthesize_all.last_failures = failures
        return out
    install = find_occt_install(_default_project_root())
    header_map = _build_header_map(modules)
    undeclarable = _undeclarable_specs(specs, install, modules)
    if undeclarable:
        print(f"synth          : dropping {len(undeclarable)}"
              f" specialization(s) with private/protected template args",
              file=__import__("sys").stderr)
    noncopyable = _noncopyable_classes(modules)
    for i, (key, (header, args)) in enumerate(sorted(specs.items())):
        tname = key.split("<", 1)[0]
        print(f"synth[{i + 1}/{len(specs)}]    : {key}", flush=True)
        if key in undeclarable:
            print(f"synth          : SKIP {key} (template arg not publicly "
                  "nameable)", flush=True)
            continue
        if _specialization_broken(key, noncopyable):
            print(f"synth          : SKIP {key} (non-copyable template arg)",
                  flush=True)
            continue
        try:
            cls = synth_template_spec(header, tname, args, install=install,
                                      header_map=header_map)
            cls.name = key  # exact spelling used in signatures
            out.append(cls)
        except Exception as e:  # noqa: BLE001
            failures.append(f"{key}: {e}")
            print(f"synth          : SKIP {key}: {e}", flush=True)
    synthesize_all.last_failures = failures
    return out


synthesize_all.last_failures: list[str] = []



def _default_project_root() -> Path:
    """Locate the repo root (with its vcpkg install)."""
    if (_PROJECT_ROOT / "vcpkg" / "installed").exists():
        return _PROJECT_ROOT
    for p in [Path.cwd().resolve(), *Path.cwd().resolve().parents]:
        if (p / "vcpkg" / "installed").exists():
            return p
    return _PROJECT_ROOT


def _find_class_template(root: C.Cursor, name: str) -> C.Cursor | None:
    for c in root.get_children():
        if c.kind == CursorKind.CLASS_TEMPLATE and c.spelling == name:
            return c
        r = _find_class_template(c, name)
        if r:
            return r
    return None


def _split_args(argstr: str) -> list[str]:
    args, depth, cur = [], 0, ""
    for ch in argstr:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth -= 1
        if ch == "," and depth == 0:
            args.append(cur)
            cur = ""
        else:
            cur += ch
    if cur.strip():
        args.append(cur)
    return [a.strip() for a in args]


_BUILTINS = {"void", "bool", "char", "short", "int", "long", "float", "double",
             "unsigned", "signed", "size_t", "char16_t", "char32_t", "wchar_t",
             "int16_t", "uint16_t", "int32_t", "uint32_t", "int64_t", "uint64_t",
             "unsigned char", "signed char", "unsigned short", "signed short",
             "unsigned int", "signed int", "unsigned long", "signed long",
             "unsigned long long", "signed long long", "long double",
             "long long", "unsigned long long"}


def _build_header_map(modules: list[ModuleDecl]) -> dict[str, str]:
    """OCCT class name -> header basename, from the scanned IR.

    OCCT usually declares one class per header, so ``Graphic3d_Attribute.hxx``
    is the natural include for ``Graphic3d_Attribute``.  Some classes break the
    convention and live in another header (``Graphic3d_Attribute`` is declared
    in ``Graphic3d_Buffer.hxx``); the scanner records the real header.  Passing
    this map to ``_collect_includes`` lets a synthesized spec include the
    declaring header instead of a name-derived one that does not exist.
    """
    out: dict[str, str] = {}
    for module in modules:
        for cls in module.classes:
            hf = getattr(cls, "header_file", "") or ""
            if hf:
                out[cls.name] = Path(hf).name
    return out


def _collect_includes(args_list: list[str],
                      include_dir: Path | None = None,
                      header_map: dict[str, str] | None = None) -> list[str]:
    """OCCT convention: header file name == (outermost) class name.

    A nested class A::B lives in the enclosing class's header A.hxx, the
    std/opencascade namespaces contribute no header of their own (handles and
    std::* types resolve from their template arguments), and a template's
    arguments are recursed into so ``handle<Geom_Curve>`` yields
    ``Geom_Curve.hxx``.  ``header_map`` (see ``_build_header_map``) overrides
    the name-derived header when the class lives in a different header.  When
    ``include_dir`` is given only headers that exist there are returned, so a
    speculative include can never break the probe TU.
    """
    out: list[str] = []

    def rec(arg: str) -> None:
        head = re.sub(r"^(?:const|volatile)\s+", "", arg.strip())
        head = re.sub(r"(?:[*&])$", "", head)
        head = head.split("<")[0].strip()
        parts = [p for p in head.split("::") if p]
        if parts and parts[0] not in ("std", "opencascade") \
                and parts[0] not in _BUILTINS and not parts[0].endswith("_t") \
                and re.match(r"^[A-Za-z_]\w*$", parts[0]):
            out.append(f"{parts[0]}.hxx")
        m = re.match(r"^[^<]*<(.*)>$", arg.strip(), re.S)
        if m:
            for inner in _split_args(m.group(1)):
                rec(inner)

    for a in args_list:
        rec(a)
    if header_map:
        out = [header_map.get(i[:-len(".hxx")] if i.endswith(".hxx") else i, i)
               for i in out]
    if include_dir is not None:
        existing = {p.name for p in include_dir.iterdir()} if include_dir.is_dir() else set()
        out = [i for i in out if i in existing]
    seen: set[str] = set()
    return [i for i in out if not (i in seen or seen.add(i))]


def _substitute(spelling: str, subst: dict[str, str]) -> str:
    out = spelling
    for name, repl in subst.items():
        if not name:
            continue
        out = re.sub(r"(?<![A-Za-z0-9_])" + re.escape(name) + r"(?![A-Za-z0-9_])",
                     repl, out)
    return out


_KNOWN_BASIC = {
    "void", "bool", "char", "short", "int", "long", "float", "double",
    "unsigned", "signed", "size_t", "wchar_t", "char16_t", "char32_t",
    "int8_t", "uint8_t", "int16_t", "uint16_t", "int32_t", "uint32_t",
    "int64_t", "uint64_t", "intptr_t", "uintptr_t",
}


def _finalize_type(t: object) -> object:
    """Make a probe-resolved type usable for self-contained wrapper codegen.

    Nested typedef names must be canonicalized even when they appear inside
    qualifiers (``const Array1Type &`` -> ``const NCollection_Array1<double> &``)
    so codegen stays self-contained.  Basic/Standard_ spellings are kept as-is
    to preserve typemap conventions.
    """
    if t is None:
        return t
    sp = getattr(t, "spelling", "").strip()
    canon = getattr(t, "canonical_spelling", "").strip()
    if canon and canon != sp:
        if sp in _KNOWN_BASIC or sp.startswith("Standard_"):
            return t
        return replace(t, spelling=canon)
    return t


def _resolve_types(spellings: list[str], template_spec: str,
                   includes: list[str], args: list[str],
                   include_dir: Path) -> dict[str, object]:
    """Resolve each spelling to an OCCTType via a scoped probe TU."""
    aliases = "\n".join(f"  using AW_T{i} = {s};"
                        for i, s in enumerate(spellings))
    incs = "\n".join(f"#include <{i}>" for i in includes)
    src = (f"#include <{includes[0]}>\n{incs}\n"
           f"template class {template_spec};\n"
           f"namespace ocg_synth {{\n"
           f"struct AW_Scope : public {template_spec} {{\n{aliases}\n}};\n}}\n")
    with tempfile.NamedTemporaryFile(suffix=".cpp", mode="w", delete=False) as f:
        f.write(src)
        tmp = f.name
    try:
        index = C.Index.create()
        tu = index.parse(tmp, args=args + ["-x", "c++", "-I", str(include_dir)])
        out: dict[str, object] = {}
        for ns in tu.cursor.get_children():
            if ns.kind == CursorKind.NAMESPACE and ns.spelling == "ocg_synth":
                for s in ns.get_children():
                    if s.kind == CursorKind.STRUCT_DECL:
                        for ta in s.get_children():
                            if ta.kind == CursorKind.TYPE_ALIAS_DECL:
                                idx = int(ta.spelling[len("AW_T"):])
                                if 0 <= idx < len(spellings):
                                    try:
                                        out[spellings[idx]] = make_type(ta.underlying_typedef_type)
                                    except Exception:
                                        pass
        return out
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _expand_spec_args(header_name: str, template_name: str,
                      args_list: list[str], includes: list[str],
                      args: list[str], include_dir: Path) -> list[str]:
    """Fill in default template arguments from the specialization itself."""
    incs = "\n".join(f"#include <{i}>" for i in includes)
    spec = f"{template_name}<{', '.join(args_list)}>"
    src = f"#include <{header_name}>\n{incs}\ntemplate class {spec};\n"
    with tempfile.NamedTemporaryFile(suffix=".cpp", mode="w", delete=False) as f:
        f.write(src)
        tmp = f.name
    try:
        index = C.Index.create()
        tu = index.parse(tmp, args=args + ["-x", "c++", "-I", str(include_dir)])
        for d in tu.diagnostics:
            if d.severity >= C.Diagnostic.Error:
                return args_list

        def find_spec(cursor: C.Cursor):
            for c in cursor.get_children():
                if c.kind in (CursorKind.CLASS_DECL, CursorKind.STRUCT_DECL) \
                        and c.spelling == template_name and c.is_definition():
                    try:
                        n = c.get_num_template_arguments()
                    except Exception:
                        n = -1
                    if n > 0:
                        return c
                r = find_spec(c)
                if r:
                    return r
            return None

        sp = find_spec(tu.cursor)
        if sp is None:
            return args_list
        try:
            n = sp.get_num_template_arguments()
            out = [sp.get_template_argument_type(i).spelling for i in range(n)]
        except Exception:
            return args_list
        return out if out and len(out) >= len(args_list) else args_list
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def synth_template_spec(header_name: str, template_name: str,
                        args_list: list[str],
                        install: Path | None = None,
                        header_map: dict[str, str] | None = None) -> object:
    """Return a ClassDecl for the specialization ``template_name<args_list>``."""
    if install is None:
        install = find_occt_install(_default_project_root())
    args = ensure_occt_args([], install.include_dir)
    rd = find_resource_dir()
    if rd:
        args.append(f"-resource-dir={rd}")

    header = install.include_dir / header_name
    tu = parse_header(header, args)
    ct = _find_class_template(tu.cursor, template_name)
    if ct is None:
        raise ValueError(f"no CLASS_TEMPLATE {template_name} in {header_name}")

    params: list[tuple[str, bool]] = []
    for c in ct.get_children():
        if c.kind == CursorKind.TEMPLATE_TYPE_PARAMETER:
            params.append((c.spelling, True))
        elif c.kind == CursorKind.PARM_DECL:
            params.append((c.spelling, False))

    subst: dict[str, str] = {}
    for idx, (pname, is_type) in enumerate(params):
        if idx < len(args_list):
            subst[pname] = args_list[idx]
            subst[f"type-parameter-0-{idx}"] = args_list[idx]

    includes = [header_name] + _collect_includes(args_list, header_map=header_map)
    full_args = _expand_spec_args(header_name, template_name, args_list,
                                  includes, args, install.include_dir)
    if full_args != args_list:
        args_list = full_args
        subst = {}
        for idx, (pname, is_type) in enumerate(params):
            if idx < len(args_list):
                subst[pname] = args_list[idx]
                subst[f"type-parameter-0-{idx}"] = args_list[idx]
    template_spec = f"{template_name}<{', '.join(args_list)}>"

    cls = _extract_class(ct, header_name)
    # Full specialization spelling (e.g. "NCollection_Array2<gp_Pnt>") is the
    # class name: it is exactly the spelling the scanner reports for template
    # arguments in other classes' signatures, so build_context registers the
    # specialization -> wrapper mapping and `native` storage emits the full
    # type in the generated header.  Wrapper naming derives Ocg-prefixed names
    # from it via occt_name_to_wrapper.
    cls.name = template_spec
    cls.base_classes = [_substitute(b, subst) for b in cls.base_classes]
    # The generated header spells `_native` as the full specialization, so the
    # template argument headers must be available even though no individual
    # method signature mentions them (e.g. TopTools_ShapeMapHasher.hxx for
    # NCollection_IndexedMap<TopoDS_Shape, TopTools_ShapeMapHasher>).
    cls.extra_occt_includes = [i for i in _collect_includes(args_list, header_map=header_map)
                               if i != header_name]

    to_resolve: dict[str, str] = {}  # substituted spelling -> substituted spelling

    def queue(t: object) -> None:
        s = _substitute(getattr(t, "spelling", ""), subst)
        to_resolve.setdefault(s, s)

    for b in cls.base_classes:
        queue(type("T", (), {"spelling": b})())
    for m in cls.constructors + cls.methods + cls.operators + cls.static_methods:
        if m.return_type is not None:
            queue(m.return_type)
        for p in m.parameters:
            queue(p.type)
    for f in cls.fields:
        queue(f.type)

    resolved = _resolve_types(list(to_resolve), template_spec, includes,
                              args, install.include_dir)

    def rebind(t: object) -> object:
        s = _substitute(getattr(t, "spelling", ""), subst)
        nt = resolved.get(s)
        return _finalize_type(nt) if nt is not None else t

    for m in cls.constructors + cls.methods + cls.operators + cls.static_methods:
        if m.return_type is not None:
            m.return_type = rebind(m.return_type)
        for p in m.parameters:
            p.type = rebind(p.type)
            dflt = getattr(p, "default_value", None)
            if dflt:
                p.default_value = _substitute(dflt, subst)
    for f in cls.fields:
        f.type = rebind(f.type)
    for i, b in enumerate(cls.base_classes):
        nt = rebind(type("T", (), {"spelling": b})())
        if isinstance(nt, object) and hasattr(nt, "spelling"):
            cls.base_classes[i] = nt.spelling

    cls.is_template = False
    return cls


# Representative specializations used by `autogen synth-check` to prove the
# synthesis covers the pipeline's usage tiers (simple, handle-args, nested
# templates, defaults expansion, macro-free HArray1).
REPRESENTATIVE_SPECS: list[tuple[str, str, list[str]]] = [
    ("NCollection_Vec3.hxx", "NCollection_Vec3", ["float"]),
    ("NCollection_Array2.hxx", "NCollection_Array2", ["gp_Pnt"]),
    ("NCollection_Array1.hxx", "NCollection_Array1", ["gp_Pnt"]),
    ("NCollection_HArray1.hxx", "NCollection_HArray1", ["double"]),
    ("NCollection_Sequence.hxx", "NCollection_Sequence", ["gp_Pnt"]),
    ("NCollection_DataMap.hxx", "NCollection_DataMap",
     ["TCollection_AsciiString", "TCollection_AsciiString"]),
]


def synth_check(verbose: bool = True) -> int:
    """Synthesize the representative specs; return 0 when all succeed."""
    failures = 0
    for header, tname, targs in REPRESENTATIVE_SPECS:
        label = f"{tname}<{', '.join(targs)}>"
        try:
            cls = synth_template_spec(header, tname, targs)
        except Exception as e:  # noqa: BLE001
            print(f"FAILED {label}: {e}")
            failures += 1
            continue
        print(f"{label}: methods={len(cls.methods)} ctors={len(cls.constructors)} "
              f"ops={len(cls.operators)} statics={len(cls.static_methods)} "
              f"fields={len(cls.fields)} bases={cls.base_classes}")
        if verbose:
            for m in (cls.methods + cls.constructors + cls.operators)[:6]:
                ps = ", ".join(p.type.spelling for p in m.parameters)
                ret = m.return_type.spelling if m.return_type else "void"
                print(f"    {m.name}({ps}) -> {ret}")
    return failures
