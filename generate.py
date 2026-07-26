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
from generate.source import generate_source
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

    # Generate the OCCT compatibility header — a single file that includes ALL
    # OCCT headers in topological dependency order, derived entirely from
    # libclang's include graphs. Force-included via CMake, it ensures every
    # vcpkg header has all its transitive dependencies satisfied.
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

    type_map = TypeMap(all_classes, all_enums)

    # Re-run skippable marking after vcpkg filtering — some types may have been removed,
    # leaving methods referencing now-unwrapped types
    from classify.skippable import mark_skippable_methods
    updated_wrapped_names = {cls.name for cls in all_classes}
    for mod in modules:
        for cls in mod.classes:
            mark_skippable_methods(cls, updated_wrapped_names)

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
