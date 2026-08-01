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
from classify.skippable import NON_SCANNED_ENUMS
from generate.fileio import write_if_changed
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
    parser.add_argument("--dump-skips", action="store_true",
                        help="Dump every skipped method's class, signature, and reason")
    args = parser.parse_args()

    script_dir = Path(__file__).parent
    project_root = script_dir.parent

    if args.output_dir:
        output_dir = Path(args.output_dir)
    else:
        output_dir = project_root / "src" / "autowrapper"

    doc_dir = Path(args.doc_dir) if args.doc_dir else project_root / "doc_classes" / "autowrapper"

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

    # Track every generated file so stale wrappers (classes that were removed
    # or skipped) can be deleted without touching unchanged files' mtimes.
    generated_files: set[Path] = set()

    # Generate the OCCT compatibility header
    compat_path = output_dir / "occt_compat.hxx"
    compat_content = "// Auto-generated OCCT compatibility header — DO NOT EDIT\n"
    compat_content += "// Includes all OCCT headers in topological dependency order.\n"
    compat_content += "// Derived from libclang's include graphs, not hardcoded knowledge.\n"
    compat_content += "#pragma once\n"
    for hdr in occt_compat_includes:
        compat_content += "#include <{}>\n".format(hdr)
    write_if_changed(compat_path, compat_content)
    generated_files.add(compat_path)
    print(f"  Generated occt_compat.hxx ({len(occt_compat_includes)} headers)")

    # Generate primitive wrapper classes header
    prim_path = output_dir / "OcgPrimitiveWrappers.hpp"
    write_if_changed(prim_path, generate_primitive_wrappers_header())
    generated_files.add(prim_path)
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

    # Build complete enum name set (scanned + non-scanned) for type mapping
    all_enum_names: set[str] = set()
    for e in all_enums:
        all_enum_names.add(e.name)
        if e.is_nested and e.parent_class:
            all_enum_names.add(f"{e.parent_class}::{e.name}")
    all_enum_names |= NON_SCANNED_ENUMS

    # Synthesize real wrapper classes for every NCollection instantiation (and
    # the handful of element types that lack a scanned wrapper).  These replace
    # their opaque OcgCollectionWrappers.hpp entries, so they are also excluded
    # from that header below.
    from generate.collections import synthesize_collections
    from generate.type_map import SYNTHESIZED_COLLECTION_TYPES
    syn_classes, syn_names = synthesize_collections(COLLECTION_TYPES,
                                                    {cls.name for cls in all_classes},
                                                    all_enum_names)
    SYNTHESIZED_COLLECTION_TYPES.update(syn_names)
    if syn_classes:
        print(f"  Synthesized {len(syn_classes)} collection wrapper classes")
        modules.append(ModuleDecl(name="autowrapper", classes=syn_classes, enums=[]))

    # Generate collection wrapper classes header (after type discovery and
    # synthesis, so names with real generated wrappers are excluded)
    coll_path = output_dir / "OcgCollectionWrappers.hpp"
    write_if_changed(coll_path, generate_collection_wrappers_header(exclude=SYNTHESIZED_COLLECTION_TYPES))
    generated_files.add(coll_path)
    print(f"  Generated OcgCollectionWrappers.hpp")

    type_map = TypeMap(all_classes + syn_classes, all_enums, extra_enum_names=all_enum_names)

    # Re-run skippable marking after vcpkg filtering — some types may have been removed,
    # leaving methods referencing now-unwrapped types.
    # Also includes collection types (NCollection typedefs) and non-scanned enums as wrapped.
    from classify.skippable import mark_skippable_methods
    updated_wrapped_names = {cls.name for c in all_classes for cls in [c]} | set(COLLECTION_TYPES.keys()) | set(HANDLE_COLLECTION_TYPES.keys()) | set(SYNTHESIZED_COLLECTION_TYPES)
    updated_copyable_names = {cls.name for cls in all_classes if cls.has_copy_assignment}
    # Reset skip flags and re-run marking with complete type info
    for mod in modules:
        for cls in mod.classes:
            for m in cls.all_methods:
                m.skip = False
                m.skip_reason = ""
            mark_skippable_methods(cls, updated_wrapped_names, all_enum_names, updated_copyable_names)

    # Report final coverage after complete type info is available
    total_all_methods = sum(len(c.all_methods) for m in modules for c in m.classes)
    total_skipped = sum(1 for m in modules for c in m.classes for meth in c.all_methods if meth.skip)
    total_wrapped = total_all_methods - total_skipped
    pct = (total_wrapped / total_all_methods * 100.0) if total_all_methods else 0.0
    print(f"  Coverage: {total_wrapped}/{total_all_methods} methods wrapped ({pct:.1f}%)",
          file=sys.stderr)
    from collections import Counter
    reason_counts = Counter()
    for m in modules:
        for c in m.classes:
            for meth in c.all_methods:
                if meth.skip:
                    if meth.skip_reason:
                        reason_counts[meth.skip_reason] += 1
                    else:
                        reason_counts["(no reason)"] += 1
    print("  Top skip reasons:", file=sys.stderr)
    for reason, count in reason_counts.most_common(250):
        print(f"    {count:4d}  {reason}", file=sys.stderr)

    if args.dump_skips:
        from classify.overloads import _type_to_string
        for mod in modules:
            for c in mod.classes:
                for meth in c.all_methods:
                    if meth.skip:
                        sig = ",".join(_type_to_string(p.type) for p in meth.parameters)
                        print(f"  SKIP {c.name}::{meth.name}({sig}) = {meth.skip_reason}",
                              file=sys.stderr)

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
            write_if_changed(header_path, header_content)
            generated_files.add(header_path)

            # Generate source
            source_content = generate_source(cls, type_map)
            source_path = output_dir / f"{cls.wrapper_name}.cpp"
            write_if_changed(source_path, source_content)
            generated_files.add(source_path)

            count += 2
        print(f"{count} files")
        files_generated += count

    print()

    # Delete stale wrappers from classes that were removed or skipped in this run.
    # Only files NOT regenerated now are deleted, so unchanged files keep their mtime.
    stale_count = 0
    for pattern in ("Ocg*.hpp", "Ocg*.cpp"):
        for old_file in output_dir.glob(pattern):
            if old_file not in generated_files:
                old_file.unlink()
                stale_count += 1
    if stale_count:
        print(f"  Removed {stale_count} stale wrapper files")

    # Step 3: Generate module.h
    print("Step 3: Generating module registration header ...")

    # Generate the OcgEnums host class for standalone OCCT enums (global-scope
    # enums like GeomAbs_Shape have no OCCT class to attach to).  It is
    # registered from module.h alongside the wrapper classes.
    from generate.enums_host import generate_enums_host_header, generate_enums_host_source
    standalone_enums = [e for m in modules for e in m.enums
                        if not e.is_nested and e.name and not e.name.startswith("(unnamed")]
    if standalone_enums:
        enum_host_hpp = output_dir / "OcgEnums.hpp"
        write_if_changed(enum_host_hpp, generate_enums_host_header(standalone_enums))
        generated_files.add(enum_host_hpp)
        enum_host_cpp = output_dir / "OcgEnums.cpp"
        write_if_changed(enum_host_cpp, generate_enums_host_source(standalone_enums))
        generated_files.add(enum_host_cpp)
        print(f"  Generated OcgEnums (host for {len(standalone_enums)} standalone enums)")

    module_path = output_dir / "module.h"
    write_if_changed(module_path, generate_module_header(modules, output_dir, enums_host=bool(standalone_enums)))
    generated_files.add(module_path)
    print(f"  Generated module.h")

    # Step 4: Generate documentation
    print("Step 4: Generating XML documentation ...")
    doc_count = 0
    for mod in modules:
        for cls in mod.classes:
            content = generate_doc_xml(cls, doc_dir)
            if content:
                doc_path = doc_dir / f"{cls.wrapper_name}.xml"
                write_if_changed(doc_path, content)
                generated_files.add(doc_path)
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
