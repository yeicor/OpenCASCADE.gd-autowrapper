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
import os
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
                    "/usr/lib/llvm-*/lib/clang/*",
                    "/opt/homebrew/lib/clang/*",
                    "/opt/homebrew/opt/llvm/lib/clang/*",
                    "/usr/local/opt/llvm/lib/clang/*",
                    "C:/Program Files/LLVM/lib/clang/*",
                    "C:/Program Files (x86)/LLVM/lib/clang/*"):
        candidates.extend(sorted(glob.glob(pattern)))
    for c in candidates:
        if c and (Path(c) / "include" / "stddef.h").exists():
            return c
    return None


def _android_sysroot() -> Path | None:
    """The NDK sysroot, or None when no usable NDK is in the environment."""
    ndk = os.environ.get("ANDROID_NDK_HOME")
    if not ndk:
        return None
    for host in ("linux-x86_64", "darwin-x86_64", "darwin-arm64", "linux-x86"):
        p = Path(ndk) / "toolchains" / "llvm" / "prebuilt" / host / "sysroot"
        if p.is_dir():
            return p
    return None


def _emscripten_sysroot() -> Path | None:
    """The Emscripten sysroot, or None when not available (not yet built)."""
    emsdk = os.environ.get("EMSDK")
    if not emsdk:
        return None
    p = Path(emsdk) / "upstream" / "emscripten" / "cache" / "sysroot"
    return p if p.is_dir() else None


# OCCT feature defines used when the vcpkg install carries no CMake metadata
# (a bare OCCT_INCLUDE_DIR).  The authoritative set is always imported from the
# vcpkg OCCT build itself (see _occt_compile_definitions); this is only a last
# resort for installs that predate the metadata.
_DEFAULT_OCCT_DEFINES = ["HAVE_FREETYPE", "HAVE_OPENGL_EXT", "HAVE_RAPIDJSON",
                         "HAVE_XLIB", "OCC_CONVERT_SIGNALS"]

# godot platform macro per vcpkg OS token (vcpkg's own triplet naming).
_PLATFORM_DEFINE_BY_OS = {
    "windows": "WINDOWS_ENABLED",
    "osx": "OSX_ENABLED",
    "ios": "OSX_ENABLED",
    "android": "ANDROID_ENABLED",
    "wasm": "WEB_ENABLED",
    "emscripten": "WEB_ENABLED",
}


def _platform_defines(triplet: str) -> list[str]:
    """godot platform macros matching the triplet's OS (the build env)."""
    for os_token, define in _PLATFORM_DEFINE_BY_OS.items():
        if os_token in triplet:
            out = [f"-D{define}"]
            if define != "WINDOWS_ENABLED":
                out.append("-DUNIX_ENABLED")
            return out
    return ["-DLINUX_ENABLED", "-DUNIX_ENABLED"]


def _occt_compile_definitions(include_dir: Path) -> list[str]:
    """The OCCT feature defines used by the vcpkg OCCT build, imported from it.

    OCCT's installed CMake config
    (``share/opencascade/OpenCASCADECompileDefinitionsAndFlags-*.cmake``)
    records the exact ``COMPILE_DEFINITIONS`` the built library was compiled
    with.  These select which optional code paths the headers expose
    (freetype, rapidjson, Xlib, ...); parsing with a stale set silently toggles
    ``#ifdef`` branches the library does not implement, so the definitions must
    come from the same vcpkg environment that built OCCT rather than a
    hardcoded list.  The metadata is per-triplet and installed with the
    package, so it stays correct even when a binary cache skips the build.

    Falls back to :data:`_DEFAULT_OCCT_DEFINES` only when the install has no
    ``share/opencascade`` tree (a bare OCCT_INCLUDE_DIR override).
    """
    config_dir = include_dir.parent.parent / "share" / "opencascade"
    if not config_dir.is_dir():
        return list(_DEFAULT_OCCT_DEFINES)
    defines: list[str] = []
    for f in sorted(config_dir.glob("OpenCASCADECompileDefinitionsAndFlags-*.cmake")):
        try:
            text = f.read_text(errors="replace")
        except OSError:
            continue
        for m in re.finditer(r"\bCOMPILE_DEFINITIONS\b[^\n]*", text):
            for genex in re.findall(r"\$<\$<CONFIG:[^<>]*>:([^<>]*)>", m.group(0)):
                for d in genex.split(";"):
                    d = d.strip()
                    if d and d not in defines:
                        defines.append(d)
    return defines or list(_DEFAULT_OCCT_DEFINES)


def _triplet_target_args(triplet: str) -> list[str]:
    """clang flags so the OCCT parse uses the build target's data model.

    libclang canonicalizes size-sensitive builtins against the *parse* target
    (``uint64_t`` is ``unsigned long`` on LP64 hosts but ``unsigned long
    long`` on ILP32 targets, and ``size_t`` shrinks to 32 bits).  Generated
    wrapper storage must match the build target, so for 32-bit triplets the
    headers are parsed with the target's flags.  LP64/LLP64 targets (x64,
    arm64, windows, osx, ios) already match their 64-bit hosts and need none.

    The mapping follows vcpkg's own triplet naming (``<arch>-<os>...``), where
    a 32-bit arch token means an ILP32 target that must be forced.  The cross
    sysroot is best-effort: the ``--target`` alone already fixes the data
    model (the part that corrupts wrapper storage); a missing sysroot only
    degrades standard-library header resolution visibly.
    """
    if triplet == "x86-linux":
        return ["-m32"]  # gcc-multilib provides the 32-bit headers
    if triplet == "arm-linux":
        return ["--target=arm-linux-gnueabihf"]
    if triplet == "x86-android":
        args = ["--target=i686-linux-android"]
        sysroot = _android_sysroot()
        return args + ([f"--sysroot={sysroot}"] if sysroot else [])
    if triplet == "arm-neon-android":
        args = ["--target=armv7a-linux-androideabi"]
        sysroot = _android_sysroot()
        return args + ([f"--sysroot={sysroot}"] if sysroot else [])
    if triplet == "wasm32-emscripten":
        args = ["--target=wasm32-unknown-emscripten"]
        sysroot = _emscripten_sysroot()
        return args + ([f"--sysroot={sysroot}"] if sysroot else [])
    return []


class CompileArgs:
    """Common compiler flags for parsing OCCT headers."""

    def __init__(self, compile_commands_path: Path | str):
        self.compile_commands_path = Path(compile_commands_path)
        self.args: list[str] = self._extract()

    def _extract(self) -> list[str]:
        db_path = self.compile_commands_path
        args = self._fallback_args()
        if db_path.exists():
            try:
                db = CompilationDatabase.fromDirectory(str(db_path.parent))
                cmds = db.getAllCompileCommands()
            except Exception:
                cmds = []
            if cmds:
                args = self._filter_args(list(cmds[0].arguments))
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
        triplet = os.environ.get("VCPKG_DEFAULT_TRIPLET", "x64-linux")
        return (["-std=gnu++17", "-DDEBUG_ENABLED", "-DGDEXTENSION",
                 "-DTHREADS_ENABLED"]
                + _platform_defines(triplet)
                + _triplet_target_args(triplet))


def ensure_occt_args(args: list[str], include_dir: Path) -> list[str]:
    """Return `args` guaranteed to parse OCCT headers like the target build.

    The OCCT headers are resolved through `-isystem <include_dir>`; without it
    libclang cannot see any `#include <*.hxx>` and the scan degrades to noise.
    The OCCT feature defines and godot platform macros are imported from the
    vcpkg environment (the OCCT install's CMake metadata + the triplet), so a
    fresh checkout with no compile_commands.json still parses against the same
    flags the target build uses.

    `-isystem` and its path must be separate argv entries: clang treats the
    combined string `"-isystem /path"` as an unknown option, which silently
    disables the OCCT include path (quoted `#include "x.hxx"` still resolved
    via the parse-file directory, masking the breakage).  Combined-form
    entries coming from a compile_commands.json are normalized to pairs here.
    """
    out: list[str] = []
    for a in args:
        if a.startswith("-isystem ") and not a.startswith("-isystem="):
            out += ["-isystem", a[len("-isystem "):]]
        else:
            out.append(a)
    if not any(a.startswith("-std=") for a in out):
        out.append("-std=gnu++17")
    target = str(include_dir)
    has_occt = False
    prev = None
    for a in out:
        if a == "-isystem":
            prev = "-isystem"
        elif prev == "-isystem":
            if a.endswith(target):
                has_occt = True
            prev = None
        elif a.startswith("-isystem="):
            if a[len("-isystem="):].endswith(target):
                has_occt = True
    if not has_occt:
        out += ["-isystem", target]
    # OCCT feature defines, imported from the vcpkg OCCT build's own CMake
    # metadata so the parse always matches the environment that built the
    # library (see _occt_compile_definitions).  Defines already present (from a
    # real compile_commands.json, which reflects the actual build) win.
    have = {a[2:] for a in out if a.startswith("-D")}
    for d in _occt_compile_definitions(include_dir):
        if d not in have:
            out.append(f"-D{d}")
            have.add(d)
    # godot platform macros matching the triplet's OS (the build env).
    for d in _platform_defines(os.environ.get("VCPKG_DEFAULT_TRIPLET", "x64-linux")):
        if d[2:] not in have:
            out.append(d)
            have.add(d)
    # Make size-sensitive builtins resolve against the build target's data
    # model (see _triplet_target_args).  Dedupe flags that a real
    # compile_commands.json already carries.
    triplet = os.environ.get("VCPKG_DEFAULT_TRIPLET", "x64-linux")
    have_target = any(
        a.startswith(("--target=", "-target")) or a in ("-m32", "-m64", "-marm")
        for a in out)
    if not have_target:
        out += _triplet_target_args(triplet)
    rd = find_resource_dir()
    if rd and not any(a.startswith("-resource-dir") for a in out):
        out.append(f"-resource-dir={rd}")
    return out
