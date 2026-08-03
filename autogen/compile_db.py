"""Compilation database loading and common argument extraction.

The single most important step for parse quality: the correct clang
`-resource-dir` must be supplied.  Without it libclang cannot find its builtin
headers (stddef.h, etc.), the parse degrades, and template types such as
`occ::handle<T>` collapse to `int`.  The legacy pipeline papered over that
degradation with thousands of lines of source-text recovery heuristics; the
clean pipeline fixes the parse instead.
"""

from __future__ import annotations

import glob
import re
import shutil
import subprocess
from pathlib import Path

from clang.cindex import CompilationDatabase


def find_resource_dir() -> str | None:
    """Locate the clang resource dir that libclang needs for builtin headers.

    Tries the clang driver on PATH first, then the layouts libclang is
    typically installed into.
    """
    candidates: list[str] = []
    clang_bin = shutil.which("clang")
    if clang_bin:
        try:
            out = subprocess.run([clang_bin, "-print-resource-dir"],
                                 capture_output=True, text=True, timeout=30)
            if out.returncode == 0 and out.stdout.strip():
                candidates.append(out.stdout.strip())
        except (OSError, subprocess.SubprocessError):
            pass
    for pattern in ("/usr/lib/clang/*", "/usr/local/lib/clang/*",
                    "/usr/lib/llvm-*/lib/clang/*"):
        candidates.extend(sorted(glob.glob(pattern)))
    for c in candidates:
        if c and (Path(c) / "include" / "stddef.h").exists():
            return c
    return None


class CompileArgs:
    """Common compiler flags for parsing OCCT headers."""

    def __init__(self, compile_commands_path: Path | str):
        self.compile_commands_path = Path(compile_commands_path)
        self.args: list[str] = self._extract()

    def _extract(self) -> list[str]:
        db_path = self.compile_commands_path
        if not db_path.exists():
            raise FileNotFoundError(
                f"compile_commands.json not found: {db_path}\n"
                "Run: cmake -S . -B .build-autowrapper -DCMAKE_EXPORT_COMPILE_COMMANDS=ON")
        try:
            db = CompilationDatabase.fromDirectory(str(db_path.parent))
            cmds = db.getAllCompileCommands()
        except Exception:
            cmds = []
        if cmds:
            args = self._filter_args(list(cmds[0].arguments))
        else:
            args = self._fallback_args()
        # Resource dir must be correct for template types to resolve.
        rd = find_resource_dir()
        if rd:
            args = [a for a in args if not a.startswith("-resource-dir")]
            args.append(f"-resource-dir={rd}")
        return args

    @staticmethod
    def _filter_args(args: list[str]) -> list[str]:
        """Drop compiler/entry args; keep the flags that define the language setup."""
        filtered: list[str] = []
        skip_next = False
        for i, arg in enumerate(args):
            if skip_next:
                skip_next = False
                continue
            if i == 0 and not arg.startswith("-"):
                continue
            if arg.startswith("--driver-mode"):
                continue
            if arg in ("-o", "-c", "-x"):
                skip_next = True
                continue
            if arg.endswith((".o", ".obj")) or (arg.endswith((".cpp", ".cxx", ".c", ".cc"))
                                                and not arg.startswith("-")):
                continue
            filtered.append(arg)
        if not any(a.startswith("-std=") for a in filtered):
            filtered.append("-std=gnu++17")
        return filtered

    @staticmethod
    def _fallback_args() -> list[str]:
        return ["-std=gnu++17", "-DDEBUG_ENABLED", "-DGDEXTENSION",
                "-DTHREADS_ENABLED", "-DUNIX_ENABLED", "-DLINUX_ENABLED"]
