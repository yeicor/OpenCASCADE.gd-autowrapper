"""Command-line entry point for the autogen pipeline."""

from __future__ import annotations

import argparse
import enum
import json
import logging
import sys
from pathlib import Path

from .compile_db import CompileArgs
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
    result = scan_module(args.module, install, compile_args.args, jobs=args.jobs)
    payload = to_dict(result)
    payload["occt_version"] = install.version
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=1, default=_json_default))
    _count_metrics(result)
    print(f"wrote          : {out}")
    return 0


def _json_default(o):
    if isinstance(o, enum.Enum):
        return o.value
    return str(o)


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

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
