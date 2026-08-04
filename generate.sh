#!/usr/bin/env bash
# Regenerate the OCCT autowrapper bindings (../src/autowrapper) from the OCCT
# headers installed via vcpkg (or OCCT_INCLUDE_DIR). Used by CI (build, test
# and export-demo jobs) and by validate.sh.
#
# The pipeline is self-sufficient: it does not need a prior CMake configure,
# because compile_db.py synthesizes the parse arguments (resource dir, OCCT
# include path, OCCT feature defines) when no compile_commands.json exists.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PYTHON="${PYTHON:-python3}"

if ! "$PYTHON" -c "import clang.cindex" >/dev/null 2>&1; then
    echo "python clang (libclang) bindings not available; cannot generate the autowrapper." >&2
    echo "Skipping autowrapper generation." >&2
    exit 0
fi

# Skip gracefully when no OCCT headers are present (the CI test/export jobs
# only consume the prebuilt .so; the build job installs OCCT first).
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

# Fresh scan: out/ir/*.json is the IR produced by the local OCCT headers.
rm -rf out/ir
mkdir -p out/ir

echo "Scanning OCCT headers (all modules)..."
"$PYTHON" -m autogen scan-all --jobs "${AUTOWRAPPER_JOBS:-8}"

echo "Generating wrappers into ../src/autowrapper..."
"$PYTHON" -m autogen generate-all --irs out/ir/*.json --out "$SCRIPT_DIR/../src/autowrapper"

echo "Autowrapper bindings generated (out/ir -> ../src/autowrapper)."
