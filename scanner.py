"""Orchestrator: headers -> libclang parse -> extract -> classify -> model.

Replaces the old regex-based scanner with a proper libclang AST extraction.
Uses multiprocessing to parallelize the expensive libclang header parsing.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed

from model import ModuleDecl, ClassDecl, EnumDecl, ClassKind, occt_name_to_wrapper
from occast.classes import extract_classes
from occast.enums import extract_tu_enums
from classify.kind import classify_all
from classify.skippable import mark_skippable_methods
from classify.overloads import group_overloads


# Module definitions: (module_name, header_prefixes)
MODULES = [
    # Fundamental types first (referenced by all other modules)
    ("Standard", ["Standard_"]),
    ("TCollection", ["TCollection_"]),
    ("TColStd", ["TColStd_"]),
    ("NCollection", ["NCollection_"]),
    ("TopTools", ["TopTools_"]),
    # Core geometry
    ("gp", ["gp_"]),
    ("GeomAbs", ["GeomAbs_"]),
    ("TopAbs", ["TopAbs_"]),
    ("Geom", ["Geom_"]),
    ("Geom2d", ["Geom2d_"]),
    ("Bnd", ["Bnd_"]),
    ("TopLoc", ["TopLoc_"]),
    # Topology
    ("TopoDS", ["TopoDS_"]),
    ("BRep", ["BRep_"]),
    ("TopExp", ["TopExp_"]),
    ("BRepTools", ["BRepTools_"]),
    # After core geometry (needs gp, Geom, etc.)
    ("TDF", ["TDF_"]),
    ("Message", ["Message_"]),
    ("SelectMgr", ["SelectMgr_"]),
    ("IntRes2d", ["IntRes2d_"]),
    ("ShapeExtend", ["ShapeExtend_"]),
    # Image/Poly (needed by many modules)
    ("Image", ["Image_"]),
    ("Poly", ["Poly_"]),
    ("TopLoc", ["TopLoc_"]),
    ("PrsMgr", ["PrsMgr_"]),
    ("Adaptor3d", ["Adaptor3d_"]),
    # Higher-level modules
    ("BRepBuilderAPI", ["BRepBuilderAPI_"]),
    ("BRepPrimAPI", ["BRepPrimAPI_"]),
    ("BRepAlgoAPI", ["BRepAlgoAPI_"]),
    ("BRepMesh", ["BRepMesh_"]),
    ("StlAPI", ["StlAPI_"]),
    ("STEPControl", ["STEPControl_"]),
    ("IGESControl", ["IGESControl_"]),
    ("XCAFDoc", ["XCAFDoc_"]),
    ("XCAFPrs", ["XCAFPrs_"]),
    ("TDocStd", ["TDocStd_"]),
    ("Quantity", ["Quantity_"]),
    ("AIS", ["AIS_"]),
    ("V3d", ["V3d_"]),
    ("Prs3d", ["Prs3d_"]),
    ("Graphic3d", ["Graphic3d_"]),
    ("gce", ["gce_"]),
    ("GC", ["GC_"]),
    ("BRepFilletAPI", ["BRepFilletAPI_"]),
    ("BRepOffsetAPI", ["BRepOffsetAPI_"]),
    ("BRepFeat", ["BRepFeat_"]),
    ("ShapeFix", ["ShapeFix_"]),
    ("ShapeAnalysis", ["ShapeAnalysis_"]),
    ("ShapeUpgrade", ["ShapeUpgrade_"]),
    ("BRepCheck", ["BRepCheck_"]),
    ("GeomAPI", ["GeomAPI_"]),
    ("IntPolyh", ["IntPolyh_"]),
    ("Law", ["Law_"]),
]


# ---------------------------------------------------------------------------
# Worker function for multiprocessing (must be top-level for pickling)
# ---------------------------------------------------------------------------

def _parse_single_header(args: tuple[str, str, str, str]) -> dict | None:
    """Parse a single header and extract classes, enums, and include graph.

    Runs in a child process. Creates its own ClangParser instance.
    Returns a dict with serializable results, or None on failure.
    """
    header_path, mod_name, prefix, compile_commands_path = args
    try:
        from occast.parser import ClangParser
        from occast.classes import extract_classes
        from occast.enums import extract_tu_enums
        from pathlib import Path

        parser = ClangParser(compile_commands_path)
        tu = parser.parse_header(header_path)

        # Extract transitive OCCT include graph
        vcpkg_occt_dir = (Path.home() / "Projects" / "OpenCASCADE.gd" / "vcpkg"
                          / "installed" / "x64-linux" / "include" / "opencascade")
        seen: set[str] = set()
        transitive_includes: list[str] = []
        for inc in tu.get_includes():
            if inc.location.file:
                name = str(inc.location.file).split('/')[-1]
                if name.endswith('.hxx') and name not in seen:
                    if (vcpkg_occt_dir / name).exists():
                        seen.add(name)
                        transitive_includes.append(name)

        # Extract classes
        known_transient: set[str] = set()
        classes = extract_classes(tu.cursor, mod_name, known_transient,
                                  header_prefix=prefix)

        # Extract top-level enums
        enums = extract_tu_enums(tu.cursor)
        header_path_name = str(Path(header_path).name)
        enums = [e for e in enums
                 if e.header_file and Path(e.header_file).name.startswith(prefix)]

        # Serialize classes and enums into plain dicts for pickling
        class_dicts = []
        for cls in classes:
            # Skip macro-generated types where header file doesn't match class name
            if cls.header_file:
                hdr_name = Path(cls.header_file).stem
                if hdr_name != cls.name:
                    continue
            class_dicts.append(cls)

        return {
            "header": header_path_name,
            "classes": class_dicts,
            "enums": enums,
            "transitive_includes": transitive_includes,
        }
    except Exception as e:
        print(f"\n    WARNING: failed to parse {Path(header_path).name}: {e}",
              file=sys.stderr)
        return None


def scan_all_modules(
    compile_commands_path: str,
    occt_include_dir: str | None = None,
    module_names: list[str] = None,
) -> tuple[list[ModuleDecl], list[str]]:
    """Scan OCCT headers using libclang and extract all declarations.

    Uses multiprocessing to parallelize the expensive libclang header parsing.
    Returns (modules, all_transitive_includes) where all_transitive_includes
    is the topologically-sorted union of every header's transitive include graph.
    """
    occt_dir = _find_occt_include(occt_include_dir)

    # -----------------------------------------------------------------------
    # Phase 1: Collect all header parse tasks
    # -----------------------------------------------------------------------
    tasks: list[tuple[str, str, str, str]] = []  # (header_path, mod_name, prefix, cc_path)
    module_headers: dict[str, list[tuple[str, str]]] = {}  # mod_name -> [(header_name, prefix)]

    for mod_name, prefixes in MODULES:
        if module_names and mod_name not in module_names:
            continue
        for prefix in prefixes:
            headers = sorted(occt_dir.glob(f"{prefix}*.hxx"))
            for header in headers:
                name_stem = header.stem
                if name_stem.startswith("_") or ".lxx" in header.suffix:
                    continue
                tasks.append((str(header), mod_name, prefix, compile_commands_path))
                if mod_name not in module_headers:
                    module_headers[mod_name] = []
                module_headers[mod_name].append((header.name, prefix))

    total_tasks = len(tasks)
    print(f"  Parsing {total_tasks} headers across {os.cpu_count()} cores ...",
          flush=True)

    # -----------------------------------------------------------------------
    # Phase 2: Parse headers in parallel
    # -----------------------------------------------------------------------
    # Map header_name -> result dict
    results_by_header: dict[str, dict] = {}

    num_workers = min(os.cpu_count() or 4, 32)
    with ProcessPoolExecutor(max_workers=num_workers) as executor:
        futures = {executor.submit(_parse_single_header, t): t for t in tasks}
        done_count = 0
        for future in as_completed(futures):
            done_count += 1
            result = future.result()
            if result is not None:
                results_by_header[result["header"]] = result
            if done_count % 200 == 0 or done_count == total_tasks:
                print(f"    ... parsed {done_count}/{total_tasks} headers",
                      flush=True)

    # -----------------------------------------------------------------------
    # Phase 3: Assemble results into modules (sequential)
    # -----------------------------------------------------------------------
    all_include_lists: list[list[str]] = []
    modules: list[ModuleDecl] = []
    known_transient: set[str] = set()

    for mod_name, prefixes in MODULES:
        if module_names and mod_name not in module_names:
            continue

        mod = ModuleDecl(name=mod_name)

        # Collect all results for this module's headers
        if mod_name in module_headers:
            for header_name, prefix in module_headers[mod_name]:
                result = results_by_header.get(header_name)
                if result is None:
                    continue

                all_include_lists.append(result["transitive_includes"])

                for cls in result["classes"]:
                    # Track transient types across the module
                    if cls.is_transient_descendant:
                        known_transient.add(cls.name)
                    # Attach the transitive include graph
                    cls.transitive_occt_includes = result["transitive_includes"]
                    # Avoid duplicates
                    if not any(c.name == cls.name for c in mod.classes):
                        mod.classes.append(cls)

                for enum in result["enums"]:
                    if not any(e.name == enum.name for e in mod.enums):
                        mod.enums.append(enum)

        print(f"  {mod_name}: {len(mod.classes)} classes, {len(mod.enums)} enums")

        # Classify all classes in this module
        classify_all(mod.classes)

        # Assign wrapper names
        for cls in mod.classes:
            cls.wrapper_name = occt_name_to_wrapper(cls.name, mod_name)

        # Collect all known wrapper names (from all modules so far + current)
        all_wrapped_names: set[str] = set()
        for prev_mod in modules:
            for c in prev_mod.classes:
                all_wrapped_names.add(c.name)
        for cls in mod.classes:
            all_wrapped_names.add(cls.name)

        # Collect all known enum names (from all modules so far + current)
        all_enum_names: set[str] = set()
        for prev_mod in modules:
            for e in prev_mod.enums:
                all_enum_names.add(e.name)
                if e.is_nested and e.parent_class:
                    all_enum_names.add(f"{e.parent_class}::{e.name}")
        for e in mod.enums:
            all_enum_names.add(e.name)
            if e.is_nested and e.parent_class:
                all_enum_names.add(f"{e.parent_class}::{e.name}")

        # Mark skippable methods
        for cls in mod.classes:
            mark_skippable_methods(cls, all_wrapped_names, all_enum_names)

        # Group overloads
        for cls in mod.classes:
            group_overloads(cls)

        modules.append(mod)

    # Merge all per-header include lists into a single topologically-sorted list.
    merged = _topological_merge_includes(all_include_lists)

    # Filter out headers whose own includes reference files that don't exist
    # in vcpkg (broken packaging — e.g. a header includes another that's missing).
    merged = _filter_broken_includes(merged, _vcpkg_occt_dir())

    return modules, merged


def _vcpkg_occt_dir() -> Path:
    return (Path.home() / "Projects" / "OpenCASCADE.gd" / "vcpkg"
            / "installed" / "x64-linux" / "include" / "opencascade")


def _topological_merge_includes(include_lists: list[list[str]]) -> list[str]:
    """Merge multiple ordered include lists into a single topologically-sorted list.

    Each list from libclang is already a valid topological order for its header.
    This merges them preserving all pairwise orderings from every input list.
    Uses Kahn's algorithm on the union of all dependency edges.
    """
    # Collect all unique headers and build a dependency graph
    all_headers: set[str] = set()
    # edges: a depends on b means b must come before a
    edges: dict[str, set[str]] = {}

    for lst in include_lists:
        for i, h in enumerate(lst):
            all_headers.add(h)
            if h not in edges:
                edges[h] = set()
            for j in range(i):
                # h depends on lst[j] (lst[j] must come before h)
                edges[h].add(lst[j])

    # Kahn's algorithm
    in_degree: dict[str, int] = {h: 0 for h in all_headers}
    rev_edges: dict[str, list[str]] = {h: [] for h in all_headers}
    for h, deps in edges.items():
        in_degree[h] = len(deps)
        for d in deps:
            rev_edges[d].append(h)

    queue = sorted(h for h in all_headers if in_degree[h] == 0)
    result: list[str] = []

    while queue:
        node = queue.pop(0)
        result.append(node)
        for dependent in rev_edges[node]:
            in_degree[dependent] -= 1
            if in_degree[dependent] == 0:
                # Insert in sorted position to keep deterministic order
                import bisect
                bisect.insort(queue, dependent)

    if len(result) != len(all_headers):
        # Circular dependency — fall back to insertion-order merge
        result = []
        seen: set[str] = set()
        for lst in include_lists:
            for h in lst:
                if h not in seen:
                    seen.add(h)
                    result.append(h)

    return result


def _find_occt_include(occt_include_dir: str | None = None) -> Path:
    """Find the OCCT include directory."""
    if occt_include_dir:
        p = Path(occt_include_dir)
        if p.exists():
            return p

    # Prefer system OCCT headers for parsing — the vcpkg version uses a newer
    # Standard_Handle.hxx whose handle<T> template libclang can't resolve.
    # System headers use an older but fully compatible handle implementation.
    candidates = [
        Path("/usr/include/opencascade"),
        Path("/usr/local/include/opencascade"),
        Path.home() / "Projects" / "OpenCASCADE.gd" / "vcpkg" / "installed" / "x64-linux" / "include" / "opencascade",
    ]
    for c in candidates:
        if c.exists() and (c / "gp_Pnt.hxx").exists():
            return c

    raise FileNotFoundError("Cannot find OCCT include directory")


def _filter_broken_includes(headers: list[str], vcpkg_dir: Path) -> list[str]:
    """Remove headers whose own #include directives reference files missing in vcpkg.

    Iterates until stable (removing a broken header may expose others).
    """
    import re
    # Pre-load all header contents for fast lookup
    contents: dict[str, str] = {}
    exists_set: set[str] = set()
    for name in headers:
        path = vcpkg_dir / name
        if path.exists():
            try:
                contents[name] = path.read_text(errors='replace')
                exists_set.add(name)
            except OSError:
                pass

    exclude: set[str] = set()
    changed = True
    while changed:
        changed = False
        for name in list(headers):
            if name in exclude or name not in exists_set:
                if name not in exists_set:
                    exclude.add(name)
                    changed = True
                continue
            src = contents[name]
            for m in re.finditer(r'#\s*include\s*[<"]([^>"]+)[>"]', src):
                dep = m.group(1)
                if not dep.endswith('.hxx'):
                    continue
                if dep in exclude:
                    exclude.add(name)
                    changed = True
                    break
                if not (vcpkg_dir / dep).exists():
                    exclude.add(name)
                    changed = True
                    break
    return [h for h in headers if h not in exclude]
