#!/usr/bin/env bash
# Regenerate the OCCT autowrapper bindings (../src/autowrapper) from the OCCT
# headers installed via vcpkg (or OCCT_INCLUDE_DIR). Used by CI (build, test
# and export-demo jobs) and by validate.sh.
#
# This is a thin wrapper over `python -m autogen regenerate`, which runs the
# whole generate.sh pipeline (fresh scan-all, then repeated generate + symbol
# audit passes until the probe compiles clean) and writes a stamp file into
# ../src/autowrapper so the CMake build step can skip already-fresh wrappers.
#
# The pipeline is self-sufficient: it does not need a prior CMake configure,
# because compile_db.py synthesizes the parse arguments (resource dir, OCCT
# include path, OCCT feature defines) when no compile_commands.json exists.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PYTHON="${PYTHON:-python3}"

# Skip gracefully when no OCCT headers are present (the CI test/export jobs
# only consume the prebuilt .so; the build job installs OCCT first).  This
# check comes before the clang-bindings check so jobs without OCCT never need
# the (heavy) clang toolchain installed.
if ! "$PYTHON" -c "
import sys
from autogen.occt import find_occt_install
from autogen.cli import PROJECT_ROOT
find_occt_install(PROJECT_ROOT)
" >/dev/null 2>&1; then
    echo "No OCCT install found (install via vcpkg or set OCCT_INCLUDE_DIR)." >&2
    echo "Skipping autowrapper generation." >&2
    exit 0
fi

if ! "$PYTHON" -c "import clang.cindex" >/dev/null 2>&1; then
    echo "python clang (libclang) bindings not available; cannot generate the autowrapper." >&2
    echo "Install them with: \"$PYTHON\" -m pip install clang" >&2
    exit 1
fi

# One scan worker per core (each worker is a separate libclang process parsing
# large OCCT headers, so oversubscribing beyond the core count on small CI
# runners starves memory without speeding anything up), capped at 8 for very
# wide machines.  Override via the AUTOWRAPPER_JOBS environment variable.
NPROC="$(nproc 2>/dev/null || echo 4)"
AUTOWRAPPER_JOBS="${AUTOWRAPPER_JOBS:-$(( NPROC > 8 ? 8 : NPROC ))}"

"$PYTHON" -m autogen regenerate --jobs "${AUTOWRAPPER_JOBS}"