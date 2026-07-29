"""libclang wrapper that parses C++ headers using the project's compilation database."""

from __future__ import annotations

import json
import os
import shlex
from pathlib import Path

from clang.cindex import Index, Config, CompilationDatabase, TranslationUnit


# Don't override libclang loading — let it use the system default which
# handles opencascade::handle<T> template resolution correctly.
# Explicit Config.set_library_file() breaks template type resolution.


class ClangParser:
    """Parses C++ headers using libclang with the project's compilation database."""

    def __init__(self, compile_commands_path: str):
        self.compile_commands_path = Path(compile_commands_path)
        self._index = Index.create()
        self._db: CompilationDatabase | None = None
        self._common_args: list[str] = []
        self._load_database()

    def _load_database(self):
        """Load the compilation database from compile_commands.json."""
        if not self.compile_commands_path.exists():
            raise FileNotFoundError(
                f"compile_commands.json not found: {self.compile_commands_path}\n"
                "Run cmake configure first: cmake -S . -B .build-autowrapper "
                "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON"
            )
        self._db = CompilationDatabase.fromDirectory(
            str(self.compile_commands_path.parent)
        )
        # Extract common flags from the first entry
        self._common_args = self._extract_common_args()

    def _extract_common_args(self) -> list[str]:
        """Extract common compiler flags from the compilation database.

        These flags apply to all source files and can be used for parsing
        headers that aren't in the compilation database (like OCCT headers).
        """
        all_cmds = self._db.getAllCompileCommands()
        if not all_cmds:
            return self._fallback_args()

        cmd = all_cmds[0]
        args = list(cmd.arguments)

        # Filter out: compiler, -o, output file, -c, input file, --driver-mode
        filtered = []
        skip_next = False
        for i, arg in enumerate(args):
            if skip_next:
                skip_next = False
                continue
            # Skip compiler path (first non-flag arg)
            if i == 0 and not arg.startswith('-'):
                continue
            # Skip --driver-mode
            if arg.startswith('--driver-mode'):
                continue
            # Skip -o and its argument
            if arg == '-o':
                skip_next = True
                continue
            # Skip -c and its argument
            if arg == '-c':
                skip_next = True
                continue
            # Skip output object files
            if arg.endswith('.o') or arg.endswith('.obj'):
                continue
            # Skip input source files
            if arg.endswith(('.cpp', '.cxx', '.c', '.cc')) and not arg.startswith('-'):
                continue
            filtered.append(arg)

        # Use vcpkg OCCT include path directly (no system header substitution).
        # Vcpkg and system OCCT differ in exception class hierarchy (std::exception
        # vs Standard_Transient), so we must parse with the same headers used for
        # compilation to avoid REF_COUNTED/class-kind mismatches.
        return filtered

    def _fallback_args(self) -> list[str]:
        """Fallback compiler flags if compilation database is unavailable."""
        project_root = self.compile_commands_path.parent.parent
        return [
            "-std=gnu++17",
            "-DDEBUG_ENABLED", "-DGDEXTENSION", "-DTHREADS_ENABLED",
            "-DUNIX_ENABLED", "-DLINUX_ENABLED",
            "-isystem", str(project_root / "vcpkg" / "installed" / "x64-linux" / "include" / "opencascade"),
            "-isystem", str(project_root / "godot-cpp" / "include"),
            "-isystem", str(project_root / ".build-autowrapper" / "godot-cpp" / "gen" / "include"),
        ]

    def parse_header(self, header_path: str) -> TranslationUnit:
        """Parse a C++ header file and return the translation unit.

        We create a temporary .cpp wrapper IN the same directory as the header
        so that relative includes (e.g. #include "AIS_Animation.hxx") resolve
        correctly. Direct parsing of .hxx headers produces wrong template types
        because libclang can't resolve transitive includes from a different directory.

        For system headers (/usr/include), we write the wrapper in a temp dir
        and use -I to make the header findable.
        """
        import tempfile
        header_dir = str(Path(header_path).parent)
        header_name = Path(header_path).name
        # System dirs are read-only, so use temp dir with include flag
        if header_dir.startswith('/usr/'):
            tmp_dir = tempfile.mkdtemp(prefix='_aw_parse_')
            try:
                tmp_path = os.path.join(tmp_dir, '_aw_parse.cpp')
                with open(tmp_path, 'w') as f:
                    f.write(f'#include <{header_name}>\n')
                args = self._common_args + ["-x", "c++", "-I", header_dir]
                return self._index.parse(tmp_path, args=args)
            finally:
                try:
                    os.unlink(tmp_path)
                    os.rmdir(tmp_dir)
                except OSError:
                    pass
        else:
            with tempfile.NamedTemporaryFile(
                suffix='.cpp', mode='w', delete=False,
                dir=header_dir,
                prefix='_aw_parse_',
            ) as f:
                f.write(f'#include "{header_name}"\n')
                tmp_path = f.name
            try:
                args = self._common_args + ["-x", "c++"]
                return self._index.parse(tmp_path, args=args)
            finally:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass


def find_occt_header(header_name: str, occt_include_dir: str | None = None) -> str:
    """Find an OCCT header file path."""
    candidates = [
        Path(occt_include_dir) / header_name if occt_include_dir else None,
        Path.home() / "Projects" / "OpenCASCADE.gd" / "vcpkg" / "installed" / "x64-linux" / "include" / "opencascade" / header_name,
        Path("/usr/include/opencascade") / header_name,
        Path("/usr/local/include/opencascade") / header_name,
    ]
    for c in candidates:
        if c and c.exists():
            return str(c)
    raise FileNotFoundError(f"Cannot find OCCT header: {header_name}")
