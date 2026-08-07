"""Command-line entry point for the autogen pipeline."""

from __future__ import annotations

import argparse
import enum
import json
import logging
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from .codegen import generate_all, generate_module
from .classify import classify_module
from .compile_db import CompileArgs, ensure_occt_args
from .ir import load_module
from .occt import OCCT_MODULES, find_occt_install
from .scanner import ModuleScanResult, scan_module, to_dict

# The autowrapper submodule lives next to the project root.
SUBMODULE_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = SUBMODULE_DIR.parent
DEFAULT_COMPILE_DB = PROJECT_ROOT / ".build-autowrapper" / "compile_commands.json"


def _count_metrics(result: ModuleScanResult) -> None:
    n_methods = sum(len(c.all_methods) for c in result.classes)
    n_wrap = sum(len(c.all_wrappable_methods) for c in result.classes)
    print(f"module         : {result.module}")
    print(f"headers        : {result.headers} (closure-retried: {result.attempts2})")
    print(f"classes        : {len(result.classes)}")
    print(f"enums          : {len(result.enums)}")
    print(f"typedefs       : {len(result.typedefs)}")
    print(f"methods        : {n_methods} (wrappable: {n_wrap})")
    if result.errors:
        print(f"errors         : {len(result.errors)}")
        for h, e in sorted(result.errors.items())[:5]:
            print(f"  - {h}: {e[:120]}")


def cmd_scan(args: argparse.Namespace) -> int:
    if args.module not in {m for m, _ in OCCT_MODULES}:
        sys.exit(f"unknown module: {args.module}")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    install = find_occt_install(PROJECT_ROOT)
    compile_args = CompileArgs(args.compile_db)
    args_list = ensure_occt_args(compile_args.args, install.include_dir)
    result = scan_module(args.module, install, args_list, jobs=args.jobs)
    payload = to_dict(result)
    payload["occt_version"] = install.version
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=1, default=_json_default))
    _count_metrics(result)
    print(f"wrote          : {out}")
    return 0


def cmd_scan_all(args: argparse.Namespace) -> int:
    """Scan every OCCT module into `out/ir/*.json`.

    Modules are scanned in parallel (each with a single inner worker); any
    module that raises or reports per-header scan errors is retried serially
    with the full job count, since transient libclang failures show up under
    parallel load.
    """
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    install = find_occt_install(PROJECT_ROOT)
    compile_args = CompileArgs(args.compile_db)
    args_list = ensure_occt_args(compile_args.args, install.include_dir)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    modules = [name for name, _ in OCCT_MODULES]
    results: dict[str, ModuleScanResult] = {}

    def scan_one(name: str) -> tuple[str, ModuleScanResult]:
        return name, scan_module(name, install, args_list, jobs=1)

    workers = max(1, min(args.jobs, len(modules)))
    if workers > 1:
        with ProcessPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(scan_one, m): m for m in modules}
            for fut in as_completed(futs):
                name = futs[fut]
                try:
                    _, res = fut.result()
                except Exception as e:  # noqa: BLE001
                    continue
                results[name] = res

    for name in modules:
        if name in results and not results[name].errors:
            continue
        try:
            results[name] = scan_module(name, install, args_list, jobs=args.jobs)
        except Exception as e:  # noqa: BLE001
            print(f"scan-all: {name} failed: {e}", file=sys.stderr)

    failures = {name: res.errors for name, res in results.items() if res.errors}
    for name, res in results.items():
        payload = to_dict(res)
        payload["occt_version"] = install.version
        (out_dir / f"{name}.json").write_text(
            json.dumps(payload, indent=1, default=_json_default))
        _count_metrics(res)

    if failures:
        print(f"scan-all: {len(failures)} module(s) with scan errors:", file=sys.stderr)
        for name, errs in sorted(failures.items()):
            print(f"  - {name}: {len(errs)} header error(s)", file=sys.stderr)
    print(f"wrote          : {out_dir} ({len(results)} modules)")
    return 0


def _json_default(o):
    if isinstance(o, enum.Enum):
        return o.value
    return str(o)


def cmd_generate(args: argparse.Namespace) -> int:
    src = Path(args.ir)
    module = load_module(src)
    classify_module(module)
    generate_module(module, Path(args.out))
    print(f"wrote          : {args.out}")
    return 0


def _load_or_synthesize(modules, cache: Path | None):
    """Return the synthesized NCollection specialization classes.

    If `cache` exists, load them from it (fast, deterministic); otherwise run
    the API-driven synthesis and write the cache so later runs skip the
    ~8-minute libclang instantiation pass.
    """
    from .model import ModuleDecl
    if cache and cache.exists():
        from .ir import load_module, dump_module
        from .synthesize import (filter_noncopyable, filter_undeclarable,
                                 filter_unwrappable, upgrade_transitive)
        from .occt import find_occt_install
        synth = load_module(cache)
        if synth.name == "NCollection" and synth.classes:
            install = find_occt_install(PROJECT_ROOT)
            synth.classes = filter_unwrappable(synth.classes, modules, install)
            synth.classes = filter_undeclarable(
                filter_noncopyable(synth.classes, modules), install, modules)
            before = len(synth.classes)
            synth.classes = upgrade_transitive(synth.classes, modules, install)
            print(f"synth          : loaded {len(synth.classes)}"
                  f" specialization(s) from {cache}")
            if len(synth.classes) > before:
                # Extend a stale cache with the newly-synthesized transitive
                # specializations so later runs stay fast.
                cache.write_text(json.dumps(
                    dump_module(synth), indent=1, default=_json_default))
                print(f"synth          : upgraded {len(synth.classes) - before}"
                      f" transitive specialization(s) in {cache}")
            return synth.classes, True
    try:
        from .synthesize import synthesize_all
        classes = synthesize_all(modules)
        if cache:
            # Write the cache only when synthesizing fresh.  A partial module
            # set (e.g. a per-module coverage run) would otherwise silently
            # shrink a full-project cache, so never overwrite one that loads.
            from .ir import dump_module
            synth_mod = ModuleDecl(name="NCollection", classes=classes)
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(
                dump_module(synth_mod), indent=1, default=_json_default))
            print(f"synth          : wrote {len(classes)}"
                  f" specialization(s) to {cache}")
        return classes, False
    except Exception as e:  # noqa: BLE001
        print(f"synth          : skipped ({e})", file=sys.stderr)
        return [], False


def cmd_generate_all(args: argparse.Namespace) -> int:
    modules = [load_module(Path(p)) for p in args.irs]
    # Cross-module class map: exception detection and custom-allocation probing
    # follow base classes that live in other modules.
    global_by_name = {cls.name: cls for m in modules for cls in m.classes}
    for module in modules:
        classify_module(module, global_by_name)
    # API-driven class-template specialization synthesis: every specialization
    # of a synthesizable template that appears in any scanned signature becomes
    # a regular wrapper class (not just the ones the demo references).  This
    # is what re-enables NCollection_Array2<gp_Pnt> & co. so OCCT APIs like
    # GeomAPI_PointsToBSplineSurface::Init can bind.
    synthesized, _ = _load_or_synthesize(modules, args.synth_cache)
    if synthesized:
        from .model import ModuleDecl
        synth_mod = ModuleDecl(name="NCollection", classes=synthesized)
        classify_module(synth_mod, global_by_name)
        modules.append(synth_mod)
        print(f"synth          : {len(synthesized)} specialization(s): "
              + ", ".join(c.wrapper_name for c in synthesized[:8])
              + (" ..." if len(synthesized) > 8 else ""))
    missing = set()
    if args.missing and Path(args.missing).exists():
        from .audit import load_missing
        missing = load_missing(Path(args.missing))
    illformed = set()
    if args.illformed and Path(args.illformed).exists():
        from .audit import load_illformed
        illformed = load_illformed(Path(args.illformed))
    generate_all(modules, Path(args.out), probe_out=args.probe_out,
                 missing=missing, illformed=illformed,
                 module_filter=args.module_filter)
    print(f"wrote          : {args.out} ({len(modules)} modules)"
          + (f" ({len(missing)} missing symbols skipped)" if missing else "")
          + (f" ({len(illformed)} ill-formed methods skipped)" if illformed else ""))
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    from .audit import run_audit
    from .occt import find_occt_install
    install = find_occt_install(PROJECT_ROOT)
    modules = [load_module(Path(p)) for p in args.irs]
    occt_classes = {cls.name for m in modules for cls in m.classes}
    compile_args = CompileArgs(args.compile_db)
    args_list = ensure_occt_args(compile_args.args, install.include_dir)
    try:
        missing = run_audit(Path(args.probe), Path(args.work), Path(args.out),
                            PROJECT_ROOT, install, args_list, occt_classes,
                            compiler=args.compiler, nm_tool=args.nm,
                            illformed_path=Path(args.illformed_out))
    except (FileNotFoundError, RuntimeError) as e:
        print(f"audit          : skipped ({e})", file=sys.stderr)
        return 1
    illformed = []
    if Path(args.illformed_out).exists():
        from .audit import load_illformed
        illformed = sorted(load_illformed(Path(args.illformed_out)))
    if not missing and not illformed:
        print("audit          : probe clean; no missing symbols", flush=True)
        return 0
    print(f"audit          : {len(missing)} missing symbol(s) -> {args.out}"
          + (f": {', '.join(missing[:5])}" if missing else "")
          + (f"; {len(illformed)} ill-formed method(s) -> {args.illformed_out}"
             if illformed else ""), flush=True)
    return 0


def cmd_synth_check(args: argparse.Namespace) -> int:
    from .synthesize import synth_check
    failures, count = synth_check(verbose=not args.quiet)
    if failures:
        print(f"synth-check    : {failures} specialization(s) failed")
        return 1
    print(f"synth-check    : {count} specialization(s) OK")
    return 0


def cmd_coverage(args: argparse.Namespace) -> int:
    from .coverage import compute_all, format_module_detail, format_table

    ir_dir = Path(args.ir_dir)
    missing: set[str] = set()
    if args.missing and Path(args.missing).exists():
        from .audit import load_missing
        missing = load_missing(Path(args.missing))
    illformed: set[str] = set()
    if args.illformed and Path(args.illformed).exists():
        from .audit import load_illformed
        illformed = load_illformed(Path(args.illformed))
    modules = [load_module(p) for p in sorted(ir_dir.glob("*.json"))]
    table, entries, meta = compute_all(
        modules, missing=missing, illformed=illformed,
        synthesized=_load_or_synthesize(modules, args.synth_cache)[0])
    print(format_table(table, module_filter=args.module))
    print()
    if args.module:
        for cov in table:
            if cov.name == args.module:
                print(format_module_detail(cov))
                print()
    else:
        print(f"TOTAL          : {meta['classes_wrapped']}/{meta['classes_total']} classes, "
              f"{meta['methods_wrapped']}/{meta['methods_total']} methods, "
              f"{meta['enums_total']} enums")
        if meta["synthesis_failures"]:
            print(f"synth-fail     : {len(meta['synthesis_failures'])}"
                  f" specialization(s) failed to synthesize")
            for f in meta["synthesis_failures"][:5]:
                print(f"  - {f[:120]}")
    if meta["unclassified_reasons"]:
        print("UNCLASSIFIED   : skip reasons missing from autogen/policy.py:")
        for reason, targets in sorted(meta["unclassified_reasons"].items()):
            print(f"  {reason} ({len(targets)}):")
            for t in targets[:5]:
                print(f"    - {t}")
        if args.check:
            print("coverage-check : FAIL (unclassified skip reasons)")
            return 1
    if args.check:
        from .policy import classify_reason
        if args.module:
            entries = [e for e in entries if e.module == args.module]
        unaccepted = [e for e in entries
                      if e.status != "accepted"]
        scope = f"module {args.module}" if args.module else "all modules"
        print(f"coverage-check : {len(entries)} skips ({scope}), "
              f"{len(entries) - len(unaccepted)} accepted, "
              f"{len(unaccepted)} gap/unclassified")
        if unaccepted:
            print("  remaining gaps (first 25):")
            for e in unaccepted[:25]:
                print(f"    {e.module}:{e.where or e.target} [{e.reason}]")
            return 1
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        # dataclasses.asdict() corrupts Counter fields (it rebuilds them with
        # (key, value) pairs as keys), so serialise rows manually.
        rows = [{
            "name": c.name,
            "classes_total": c.classes_total,
            "classes_wrapped": c.classes_wrapped,
            "classes_skipped": c.classes_skipped,
            "methods_total": c.methods_total,
            "methods_wrapped": c.methods_wrapped,
            "methods_skipped": c.methods_skipped,
            "enums_total": c.enums_total,
            "enum_values": c.enum_values,
            "class_skip_reasons": dict(c.class_skip_reasons),
            "method_skip_reasons": dict(c.method_skip_reasons),
        } for c in table]
        out.write_text(json.dumps({
            "meta": meta,
            "modules": rows,
            "skips": [__import__("dataclasses").asdict(e) for e in entries],
        }, indent=1, default=_json_default))
        print(f"wrote          : {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="autogen")
    sub = parser.add_subparsers(dest="command", required=True)

    p_scan = sub.add_parser("scan", help="scan one OCCT module into IR JSON")
    p_scan.add_argument("--module", default="Standard")
    p_scan.add_argument("--jobs", type=int, default=8)
    p_scan.add_argument("--out", type=Path,
                        default=SUBMODULE_DIR / "out" / "ir" / "Standard.json")
    p_scan.add_argument("--compile-db", type=Path, default=DEFAULT_COMPILE_DB)
    p_scan.set_defaults(func=cmd_scan)

    p_scan_all = sub.add_parser("scan-all", help="scan every OCCT module into out/ir/*.json")
    p_scan_all.add_argument("--jobs", type=int, default=8)
    p_scan_all.add_argument("--out", type=Path, default=SUBMODULE_DIR / "out" / "ir")
    p_scan_all.add_argument("--compile-db", type=Path, default=DEFAULT_COMPILE_DB)
    p_scan_all.set_defaults(func=cmd_scan_all)

    p_gen = sub.add_parser("generate", help="generate wrappers from IR JSON")
    p_gen.add_argument("--ir", type=Path,
                       default=SUBMODULE_DIR / "out" / "ir" / "Standard.json")
    p_gen.add_argument("--out", type=Path,
                       default=SUBMODULE_DIR / "out" / "gen" / "Standard")
    p_gen.set_defaults(func=cmd_generate)

    p_all = sub.add_parser("generate-all", help="generate wrappers from multiple IR JSONs")
    p_all.add_argument("--irs", nargs="+", type=Path,
                       default=[SUBMODULE_DIR / "out" / "ir" / "Standard.json"])
    p_all.add_argument("--out", type=Path,
                       default=SUBMODULE_DIR / "out" / "gen")
    p_all.add_argument("--probe-out", type=Path, default=None,
                       help="also write a symbol-audit probe TU to this path")
    p_all.add_argument("--missing", type=Path, default=None,
                       help="skip methods whose link symbols are in this file")
    p_all.add_argument("--illformed", type=Path, default=None,
                       help="skip methods whose instantiation fails to compile "
                            "(Class::method lines from the audit)")
    p_all.add_argument("--synth-cache", type=Path,
                       default=SUBMODULE_DIR / "out" / "synth" / "specs.json",
                       help="reuse/cache synthesized specializations (fast reruns)")
    p_all.add_argument("--module-filter", default=None,
                       help="rewrap only this module's classes "
                            "(full context still built; skips enums/module.h)")
    p_all.set_defaults(func=cmd_generate_all)

    p_audit = sub.add_parser(
        "audit", help="compile the probe TU and diff undefined vs library symbols")
    p_audit.add_argument("--irs", nargs="+", type=Path,
                         default=[SUBMODULE_DIR / "out" / "ir" / "Standard.json"])
    p_audit.add_argument("--probe", type=Path,
                         default=SUBMODULE_DIR / "out" / "audit" / "probe.cpp")
    p_audit.add_argument("--work", type=Path,
                         default=SUBMODULE_DIR / "out" / "audit")
    p_audit.add_argument("--out", type=Path,
                         default=SUBMODULE_DIR / "out" / "audit" / "missing.txt")
    p_audit.add_argument("--illformed-out", type=Path,
                         default=SUBMODULE_DIR / "out" / "audit" / "illformed.txt")
    p_audit.add_argument("--compile-db", type=Path, default=DEFAULT_COMPILE_DB)
    p_audit.add_argument("--compiler", type=str, default="g++")
    p_audit.add_argument("--nm", type=str, default="nm")
    p_audit.set_defaults(func=cmd_audit)

    p_synth = sub.add_parser(
        "synth-check", help="validate class-template specialization synthesis")
    p_synth.add_argument("--quiet", action="store_true",
                         help="only print pass/fail counts")
    p_synth.set_defaults(func=cmd_synth_check)

    p_cov = sub.add_parser(
        "coverage", help="per-module wrapped/skipped report (skip-registry gate)")
    p_cov.add_argument("--ir-dir", type=Path,
                       default=SUBMODULE_DIR / "out" / "ir")
    p_cov.add_argument("--missing", type=Path,
                       default=SUBMODULE_DIR / "out" / "audit" / "skips-missing.txt")
    p_cov.add_argument("--illformed", type=Path,
                       default=SUBMODULE_DIR / "out" / "audit" / "skips-illformed.txt",
                       help="skip methods whose instantiation fails to compile")
    p_cov.add_argument("--module", type=str, default=None,
                       help="focus the report on one OCCT module")
    p_cov.add_argument("--out", type=Path,
                       default=SUBMODULE_DIR / "out" / "coverage.json")
    p_cov.add_argument("--check", action="store_true",
                       help="exit 1 while any skip is a gap or unclassified")
    p_cov.add_argument("--synth-cache", type=Path,
                       default=SUBMODULE_DIR / "out" / "synth" / "specs.json",
                       help="reuse/cache synthesized specializations")
    p_cov.set_defaults(func=cmd_coverage)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
