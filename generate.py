#!/usr/bin/env python3
"""Main entry point for the OpenCASCADE autowrapper generator.

Usage:
    python3 generate.py --compile-commands PATH [--output-dir DIR] [--modules gp TopoDS]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from model import ModuleDecl, ClassKind
from scanner import scan_all_modules, MODULES
from generate.type_map import TypeMap
from generate.header import generate_header
from generate.source import generate_source, generate_primitive_wrappers_header, generate_collection_wrappers_header
from generate.module import generate_module_header
from generate.docs_xml import generate_doc_xml


def main():
    parser = argparse.ArgumentParser(description="OpenCASCADE autowrapper generator (libclang-based)")
    parser.add_argument("--compile-commands", type=str, required=True,
                        help="Path to compile_commands.json")
    parser.add_argument("--output-dir", type=str, default=None,
                        help="Output directory for generated files")
    parser.add_argument("--doc-dir", type=str, default=None,
                        help="Output directory for XML documentation files")
    parser.add_argument("--modules", nargs="*", default=None,
                        help="Only scan these modules (e.g. gp TopoDS)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Scan headers but don't generate code")
    parser.add_argument("--dump-model", action="store_true",
                        help="Dump parsed model as JSON for debugging")
    args = parser.parse_args()

    script_dir = Path(__file__).parent
    project_root = script_dir.parent

    if args.output_dir:
        output_dir = Path(args.output_dir)
    else:
        output_dir = project_root / "src" / "autowrapper"

    doc_dir = Path(args.doc_dir) if args.doc_dir else project_root / "doc_classes"

    print("OpenCASCADE Autowrapper Generator (libclang)")
    print(f"  compile_commands: {args.compile_commands}")
    print(f"  Output dir:       {output_dir}")
    print()

    # Step 1: Scan headers via libclang
    print("Step 1: Scanning OCCT headers via libclang AST ...")
    modules, occt_compat_includes = scan_all_modules(args.compile_commands, module_names=args.modules)
    total_classes = sum(len(m.classes) for m in modules)
    total_enums = sum(len(m.enums) for m in modules)
    total_methods = sum(len(c.all_wrappable_methods) for m in modules for c in m.classes)
    skipped = sum(1 for m in modules for c in m.classes for method in c.all_methods if method.skip)
    print(f"  Total: {total_classes} classes, {total_enums} enums, {total_methods} methods ({skipped} skipped)")
    print()

    if args.dump_model:
        _dump_model(modules)
        return

    if args.dry_run:
        print("Dry run — skipping code generation.")
        return

    # Step 2: Generate wrappers
    print("Step 2: Generating godot-cpp wrapper code ...")
    output_dir.mkdir(parents=True, exist_ok=True)

    # Generate the OCCT compatibility header
    compat_path = output_dir / "occt_compat.hxx"
    with open(compat_path, 'w') as f:
        f.write("// Auto-generated OCCT compatibility header — DO NOT EDIT\n")
        f.write("// Includes all OCCT headers in topological dependency order.\n")
        f.write("// Derived from libclang's include graphs, not hardcoded knowledge.\n")
        f.write("#pragma once\n")
        for hdr in occt_compat_includes:
            f.write("#include <{}>\n".format(hdr))
    print(f"  Generated occt_compat.hxx ({len(occt_compat_includes)} headers)")

    # Clean old generated files to avoid stale wrappers from skipped classes
    for old_file in output_dir.glob("Ocg*.hpp"):
        old_file.unlink()
    for old_file in output_dir.glob("Ocg*.cpp"):
        old_file.unlink()

    # Generate primitive wrapper classes header (after clean to avoid deletion)
    prim_path = output_dir / "OcgPrimitiveWrappers.hpp"
    with open(prim_path, 'w') as f:
        f.write(generate_primitive_wrappers_header())
    print(f"  Generated OcgPrimitiveWrappers.hpp")

    # Build type map from all modules
    all_classes = [cls for m in modules for cls in m.classes]
    all_enums = [e for m in modules for e in m.enums]

    # Filter out classes whose OCCT headers don't exist in vcpkg (version mismatch)
    # The scanner may use system OCCT which has different headers than vcpkg OCCT
    vcpkg_occt_inc = output_dir.parent.parent / "vcpkg" / "installed" / "x64-linux" / "include" / "opencascade"
    if vcpkg_occt_inc.exists():
        filtered_classes = []
        for cls in all_classes:
            hdr = Path(cls.header_file).name if cls.header_file else ""
            if hdr and not (vcpkg_occt_inc / hdr).exists():
                print(f"  WARNING: skipping {cls.name} — header {hdr} not found in vcpkg OCCT",
                      file=sys.stderr)
                continue
            filtered_classes.append(cls)
        if len(filtered_classes) != len(all_classes):
            skipped = len(all_classes) - len(filtered_classes)
            print(f"  Filtered out {skipped} classes with headers not in vcpkg OCCT", file=sys.stderr)
            all_classes = filtered_classes
            # Rebuild modules with filtered classes
            for m in modules:
                m.classes = [c for c in m.classes if c in all_classes]
            total_classes = len(all_classes)

    # Discover NCollection typedef aliases, unscanned handle types, and value
    # types from method signatures. These are opaque wrappers that bridge
    # NCollection containers and unscanned-module types across the FFI.
    from generate.type_map import discover_type_aliases, COLLECTION_TYPES, HANDLE_COLLECTION_TYPES
    discovered_coll, discovered_handle = discover_type_aliases(all_classes)
    COLLECTION_TYPES.update(discovered_coll)
    HANDLE_COLLECTION_TYPES.update(discovered_handle)
    if discovered_coll or discovered_handle:
        print(f"  Discovered {len(discovered_coll)} collection types, {len(discovered_handle)} handle types")

    # Generate collection wrapper classes header (after type discovery)
    coll_path = output_dir / "OcgCollectionWrappers.hpp"
    with open(coll_path, 'w') as f:
        f.write(generate_collection_wrappers_header())
    print(f"  Generated OcgCollectionWrappers.hpp")

    # Build complete enum name set (scanned + non-scanned) for type mapping
    all_enum_names: set[str] = set()
    for e in all_enums:
        all_enum_names.add(e.name)
        if e.is_nested and e.parent_class:
            all_enum_names.add(f"{e.parent_class}::{e.name}")
    # Non-scanned enum types from OCCT modules not in our MODULES list.
    # These are plain C enums or scoped enums that can be passed as int32_t.
    NON_SCANNED_ENUMS: set[str] = {
        # Extrema
        "Extrema_ExtAlgo", "Extrema_ExtFlag",
        # IFSelect
        "IFSelect_ReturnStatus", "IFSelect_PrintFail", "IFSelect_PrintCount",
        # Font
        "Font_FontAspect",
        # Aspect (non-scanned nested enums)
        "Aspect_VKey", "Aspect_HatchStyle",
        "Aspect_GraphicsLibrary",
        # PCDM
        "PCDM_StoreStatus", "PCDM_ReaderStatus",
        # DsgPrs
        "DsgPrs_ArrowSide",
        # Select3D
        "Select3D_TypeOfSensitivity",
        # Approx
        "Approx_ParametrizationType",
        # Poly
        "Poly_MeshPurpose",
        # ChFi3d
        "ChFi3d_FilletShape",
        # ChFiDS
        "ChFiDS_ErrorStatus", "ChFiDS_ChamfMode",
        # ChFi2d
        "ChFi2d_ConstructionError",
        # GeomFill
        "GeomFill_Trihedron",
        # Draft
        "Draft_ErrorStatus",
        # BRepFill
        "BRepFill_TypeOfContact", "BRepFill_ThruSectionErrorStatus",
        # BRepOffset
        "BRepOffset_Mode",
        # BRepMesh
        "BRepMesh_GeomTool::IntFlag",
        # XSAlgo
        # "XSAlgo_ShapeProcessor::ProcessingFlags",  # Not an enum (std::pair)
        # ShapeProcess
        # "ShapeProcess::OperationsFlags",  # Not an enum (std::bitset)
        # LocOpe
        "LocOpe_Operation",
        # XCAFPrs
        "XCAFPrs_DocumentExplorerFlags",
        # XCAFDoc
        "XCAFDoc_AssemblyGraph::NodeType",
        # FS
        "FS_VARStatuses",


        # StdSelect
        "StdSelect_TypeOfSelectionImage",
        "AIS_SelectionScheme",
        # Graphic3d
        # gp_Dir/Dir2d nested enums — scoped enum classes not scanned properly
        "gp_Dir::D",
        "gp_Dir2d::D",
        # Interface module (not scanned)
        "Interface_CheckStatus",
        "Interface_ParamType",
        # IMeshTools
        "IMeshTools_MeshAlgoType",
        # AIS
        "AIS_Manipulator::ManipulatorSkin",
        "AIS_SelectionScheme",
        # Aspect
        "Aspect_XRSession::TrackingUniverseOrigin",
        # Bnd
        "Bnd_Range::IntersectStatus",
        # Graphic3d
        "Graphic3d_Camera::Projection",
        "Graphic3d_Camera::IODType",
        "Graphic3d_Camera::FocusType",
        "Graphic3d_RenderingParams::PerfCounters",

    }
    all_enum_names |= NON_SCANNED_ENUMS

    type_map = TypeMap(all_classes, all_enums, extra_enum_names=all_enum_names)

    # Re-run skippable marking after vcpkg filtering — some types may have been removed,
    # leaving methods referencing now-unwrapped types.
    # Also includes collection types (NCollection typedefs) and non-scanned enums as wrapped.
    from classify.skippable import mark_skippable_methods
    updated_wrapped_names = {cls.name for c in all_classes for cls in [c]} | set(COLLECTION_TYPES.keys()) | set(HANDLE_COLLECTION_TYPES.keys())
    # Reset skip flags and re-run marking with complete type info
    for mod in modules:
        for cls in mod.classes:
            for m in cls.all_methods:
                m.skip = False
                m.skip_reason = ""
            mark_skippable_methods(cls, updated_wrapped_names, all_enum_names)

    files_generated = 0
    for mod in modules:
        if not mod.classes:
            continue
        print(f"  Generating wrappers for module: {mod.name} ({len(mod.classes)} classes) ...", end=" ", flush=True)
        count = 0
        for cls in mod.classes:
            # Generate header
            header_content = generate_header(cls, type_map)
            header_path = output_dir / f"{cls.wrapper_name}.hpp"
            header_path.write_text(header_content)

            # Generate source
            source_content = generate_source(cls, type_map)
            source_path = output_dir / f"{cls.wrapper_name}.cpp"
            source_path.write_text(source_content)

            count += 2
        print(f"{count} files")
        files_generated += count

    print()

    # Step 3: Generate module.h
    print("Step 3: Generating module registration header ...")
    generate_module_header(modules, output_dir)
    print(f"  Generated module.h")

    # Step 4: Generate documentation
    print("Step 4: Generating XML documentation ...")
    doc_count = 0
    for mod in modules:
        for cls in mod.classes:
            result = generate_doc_xml(cls, doc_dir)
            if result:
                doc_count += 1
    print(f"  Generated {doc_count} doc files")
    print()

    print(f"Done! Generated {files_generated} source files for {total_classes} classes.")


def _dump_model(modules: list[ModuleDecl]):
    """Dump the parsed model as JSON for debugging."""
    import dataclasses

    def serialize(obj):
        if dataclasses.is_dataclass(obj):
            return {k: serialize(v) for k, v in dataclasses.asdict(obj).items()}
        elif isinstance(obj, list):
            return [serialize(v) for v in obj]
        elif isinstance(obj, (int, float, str, bool, type(None))):
            return obj
        else:
            return str(obj)

    print(json.dumps(serialize(modules), indent=2))


if __name__ == "__main__":
    main()
