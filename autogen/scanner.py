"""Module-scoped scanning: headers -> libclang -> IR (JSON).

Parsing runs header-parallel across processes.  Each header is first parsed
standalone; if it is not self-contained (hard parse errors) it is retried with
its include closure pre-included.  Declarations are attributed to the header
that defines them via `location.file`.
"""

from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .extract import HeaderResult, extract_header
from .occt import OCCTInstall, module_headers, transitive_closure_for_header
from .parser import ParseError, parse_header

log = logging.getLogger("autogen.scanner")


def _quoted_types(err: str) -> list[str]:
    """Deduplicated identifier(s) quoted in a libclang error, e.g.
    "use of undeclared identifier 'X'", "unknown type name 'Y'", the
    downcast failure "static_cast from 'A *' to 'B *'" (quotations carry the
    full type spelling, including '*')."""
    out: list[str] = []
    for q in re.findall(r"'([^']*)'", err):
        q = q.strip().rstrip("*").strip()
        if not q or q.endswith(".hxx") \
                or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_:]*", q):
            continue
        q = q.split("::")[-1]
        if q not in out:
            out.append(q)
    return out


def _candidate_idents(err: str) -> list[str]:
    """Ordered candidate identifiers to pre-include for a hard parse error.

    For a "static_cast ... to 'B *'" failure the *target* B is what must be
    complete, so it is tried before the source A.
    """
    quoted = _quoted_types(err)
    if not quoted:
        return []
    if "static_cast" in err:
        return [quoted[-1]] + quoted[:-1]
    return [quoted[0]]


def _macro_headers(ident: str, install: OCCTInstall) -> list[str]:
    """Headers in the install that ``#define`` the identifier (OCCT macros like
    OCCT_DUMP_FIELD_VALUE_NUMERICAL, which lives in Standard_Dump.hxx)."""
    pat = re.compile(rf"^\s*#\s*define\s+{re.escape(ident)}\b", re.MULTILINE)
    out = []
    for h in install.include_dir.glob("*.hxx"):
        try:
            with open(h, encoding="utf-8", errors="replace") as f:
                if pat.search(f.read()):
                    out.append(h.name)
        except OSError:
            continue
    return out


def _scan_one(header: str, args: list[str], install_dir: str,
              ) -> tuple[str, HeaderResult, int, str]:
    """Worker: parse one header (with closure retry) and extract its decls."""
    from .occt import OCCTInstall
    from .parser import parse_header
    from .extract import extract_header

    install = OCCTInstall(include_dir=Path(install_dir), source="")
    hpath = Path(header)
    try:
        tu = parse_header(hpath, args)
        return hpath.name, extract_header(hpath, tu), 1, ""
    except ParseError:
        pass

    # Pre-check: headers whose own #include references a header missing from
    # the install (deprecated alias headers, e.g. Graphic3d_MapIteratorOfMapOfStructure
    # including the non-installed Graphic3d_MapOfStructure.hxx) can never parse;
    # skip them instead of reporting a hard scan error.
    if _has_missing_include(hpath, install):
        return hpath.name, HeaderResult(header=hpath.name), 0, ""

    pre = [p.name for p in transitive_closure_for_header(hpath, install)]
    # Iterative dependency completion: a header may rely on a type its includer
    # provides without #including it itself (OCCT's cyclic TopoDS_Shape/
    # TopoDS_TShape headers), or on an incomplete class being complete
    # (V3d_Trihedron needs V3d_View.hxx for its handle<->raw downcasts), or on
    # an OCCT macro (OCCT_DUMP_FIELD_VALUE_NUMERICAL -> Standard_Dump.hxx).
    # Each hard error names the missing identifier; pre-include its OCCT
    # header (<Name>.hxx) or the header that #defines it, then retry.  The fix
    # header must be parsed *before* the header whose use failed (which may be
    # a pre-included dependency, not the header being scanned).
    for _ in range(24):
        try:
            tu = parse_header(hpath, args, pre_headers=pre)
            hr = extract_header(hpath, tu)
            hr.extra_includes = list(pre)
            return hpath.name, hr, 2, ""
        except ParseError as e:
            fix: str | None = None
            for ident in _candidate_idents(str(e)):
                dep = f"{ident}.hxx"
                if dep not in pre and (install.include_dir / dep).exists():
                    fix = dep
                    break
                if re.fullmatch(r"[A-Z_][A-Z0-9_]*", ident):
                    for mh in _macro_headers(ident, install):
                        if mh not in pre:
                            fix = mh
                            break
                    if fix:
                        break
                for eh in _enum_headers(ident, install):
                    if eh not in pre:
                        fix = eh
                        break
                if fix:
                    break
            if not fix:
                return hpath.name, HeaderResult(header=hpath.name), 2, str(e)
            if e.loc_file and e.loc_file in pre:
                pre.insert(pre.index(e.loc_file), fix)
            else:
                pre.insert(0, fix)
            continue
    return hpath.name, HeaderResult(header=hpath.name), 2, "too many retries"


def _has_missing_include(hpath: Path, install: OCCTInstall) -> bool:
    """True if the header's whole #include graph references a header that can
    never resolve here.

    Only the project's own OCCT install counts (never system include dirs):
    a header that reaches a non-installed OCCT header (deprecated alias
    headers, OCCT source-only ".pxx/.ixx/.gxx" files, or the Windows-only
    <windows.h> family on Linux) can never parse and is skipped instead of
    reporting a hard error.  Stdlib/system headers (".h" or no extension,
    e.g. <vector>, <stddef.h>) are ignored here.
    """
    from .occt import _direct_includes
    by_name = {h.name: h for h in install.include_dir.glob("*.hxx")}
    seen: set[str] = set()
    queue: list[Path] = [hpath]
    while queue:
        h = queue.pop(0)
        if h.name in seen:
            continue
        seen.add(h.name)
        for dep in _direct_includes(h):
            if dep.endswith((".hxx", ".pxx", ".ixx", ".gxx", ".cxx", ".cpp")) \
                    and dep not in by_name:
                return True
            if dep in ("windows.h", "winsock2.h", "windowsx.h", "windows_win.h"):
                return True
            dep_path = by_name.get(dep)
            if dep_path is not None:
                queue.append(dep_path)
    return False


def _enum_headers(ident: str, install: OCCTInstall) -> list[str]:
    """Headers defining `ident` as an enum enumerator (e.g. GeomAbs_C2 lives
    in the `enum GeomAbs_Shape { ... }` of GeomAbs_Shape.hxx)."""
    pat = re.compile(rf"\benum\b[^;{{}}]*\{{[^}}]*\b{re.escape(ident)}\b")
    out = []
    for h in install.include_dir.glob("*.hxx"):
        try:
            with open(h, encoding="utf-8", errors="replace") as f:
                if pat.search(f.read()):
                    out.append(h.name)
        except OSError:
            continue
    return out


@dataclass
class ModuleScanResult:
    module: str
    classes: list = field(default_factory=list)
    enums: list = field(default_factory=list)
    typedefs: dict[str, str] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    headers: int = 0
    attempts2: int = 0


def _sort_key(name: str) -> str:
    return name.lower()


def scan_module(module: str, install: OCCTInstall, args: list[str],
                jobs: int = 8, _reuse_install: bool = True) -> ModuleScanResult:
    headers = module_headers(module, install)
    result = ModuleScanResult(module=module, headers=len(headers))

    if not headers:
        log.warning("module %s: no headers", module)
        return result

    install_dir = str(install.include_dir)
    items = [(str(h), args, install_dir) for h in headers]

    results: list[HeaderResult] = []
    with ProcessPoolExecutor(max_workers=jobs) as ex:
        futs = {ex.submit(_scan_one, *it): it[0] for it in items}
        for fut in as_completed(futs):
            name, hr, attempts, err = fut.result()
            if attempts > 1:
                result.attempts2 += 1
            if err:
                result.errors[name] = err
                continue
            results.append(hr)

    seen_classes: dict[str, object] = {}
    seen_enums: dict[str, object] = {}
    typedefs: dict[str, str] = {}
    for hr in results:
        for c in hr.classes:
            if c.name not in seen_classes:
                c.module_name = module
                if hr.extra_includes:
                    c.extra_occt_includes = hr.extra_includes
                seen_classes[c.name] = c
        for e in hr.enums:
            if e.name not in seen_enums:
                seen_enums[e.name] = e
        typedefs.update(dict(hr.typedefs))

    result.classes = sorted(seen_classes.values(), key=lambda c: _sort_key(c.name))
    result.enums = sorted(seen_enums.values(), key=lambda e: _sort_key(e.name))
    result.typedefs = dict(sorted(typedefs.items()))
    return result


def to_dict(result: ModuleScanResult) -> dict:
    return {
        "module": result.module,
        "classes": [asdict(c) for c in result.classes],
        "enums": [asdict(e) for e in result.enums],
        "typedefs": result.typedefs,
        "errors": result.errors,
        "headers": result.headers,
        "attempts2": result.attempts2,
        "occt_version": "",
    }
