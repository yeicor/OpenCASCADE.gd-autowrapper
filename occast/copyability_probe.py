"""Compile-time copyability probing for OCCT value classes.

libclang only surfaces *explicitly* `= delete`d copy constructors/assignments
as cursor children. A class is often non-copyable *implicitly* because it
contains a non-copyable member (e.g. Extrema_ExtPS holds Extrema_GenExtPS
whose copy ops are `= delete`; IntTools_FClass2d holds a std::unique_ptr).
Such classes would wrongly be reported as copyable, and methods returning
them by value/reference would generate wrapper code that fails to compile
(the wrapper copies the result into `_native` storage).

To catch these we compile one probe translation unit that includes every
scanned value-class header and static_asserts copy-constructibility AND
copy-assignability. Any class failing the assert is implicitly non-copyable.
The probe is compiled with the project's real compiler flags (from
compile_commands.json), so it reflects exactly what the generated wrappers
will be built against.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import tempfile
from pathlib import Path

from model import ClassKind

# Markers embedded in each static_assert message; parsed back out of the
# compiler output to identify which classes failed.
_PROBE_FAIL_MARK = "PROBE_FAIL"


def _extract_compile_flags(compile_commands_path: Path) -> tuple[list[str], list[str]]:
    """Return (defines, opencascade_include_flags) from the compilation DB."""
    cmds = json.loads(compile_commands_path.read_text())
    if not cmds:
        return [], []
    args = shlex.split(cmds[0]["command"]) if "command" in cmds[0] else cmds[0]["arguments"]
    defines: list[str] = []
    includes: list[str] = []
    for i, a in enumerate(args):
        if a.startswith("-D"):
            defines.append(a)
        elif a == "-isystem" or a == "-I":
            nxt = args[i + 1] if i + 1 < len(args) else ""
            if nxt and "opencascade" in nxt:
                includes.extend([a, nxt])
        elif a.startswith("-isystem") and "opencascade" in a:
            includes.append(a)
        elif a.startswith("-I") and "opencascade" in a:
            includes.append(a)
    return defines, includes


def _probe_source(value_classes: list) -> str:
    """Build the probe TU source for the given value classes."""
    lines: list[str] = ["#include <type_traits>"]
    included: set[str] = set()
    for cls in value_classes:
        hdr = Path(cls.header_file).name if cls.header_file else ""
        if not hdr or hdr in included:
            continue
        included.add(hdr)
        lines.append(f'#include "{hdr}"')
    lines.append("")
    for cls in value_classes:
        hdr = Path(cls.header_file).name if cls.header_file else ""
        if not hdr:
            continue
        check = (
            f"std::is_copy_constructible_v<{cls.name}> "
            f"&& std::is_copy_assignable_v<{cls.name}>"
        )
        lines.append(f'static_assert({check}, "{_PROBE_FAIL_MARK} {cls.name}");')
    return "\n".join(lines)


def detect_non_copyable_classes(
    classes: list,
    compile_commands_path: Path | str,
    cxx: str = "/usr/bin/c++",
) -> set[str]:
    """Probe which value classes are NOT copyable (copy-constructible + copy-assignable).

    Only classes currently believed copyable by libclang are probed; classes
    libclang already flagged non-copyable are returned by the caller from the
    existing detection. Returns the subset of `classes` that failed the probe.
    """
    compile_commands_path = Path(compile_commands_path)
    candidates = [c for c in classes if c.kind != ClassKind.REF_COUNTED]
    candidates = [c for c in candidates if c.header_file and Path(c.header_file).name]
    if not candidates:
        return set()

    defines, includes = _extract_compile_flags(compile_commands_path)
    src = _probe_source(candidates)
    with tempfile.NamedTemporaryFile(suffix=".cpp", mode="w", delete=False) as f:
        f.write(src)
        probe_path = f.name
    try:
        cmd = [
            cxx, "-fsyntax-only", "-std=gnu++17",
            *defines, *includes, probe_path,
        ]
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=300,
        )
        out = proc.stdout + proc.stderr
    finally:
        try:
            Path(probe_path).unlink()
        except OSError:
            pass

    failed: set[str] = set()
    for line in out.splitlines():
        if _PROBE_FAIL_MARK in line:
            m = re.search(rf'{_PROBE_FAIL_MARK}\s+([\w:]+)', line)
            if m:
                failed.add(m.group(1))
    return failed
