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

# Pass 1: generate wrappers and a symbol-audit probe TU.  The audit then
# compares the probe's undefined symbols against the OCCT libraries' defined
# set; methods whose member symbol is missing from the libs (e.g.
# OSD_Path::LocateExecFile, where only the free function is exported) are
# skipped in pass 2, before the slow vcpkg rebuild runs.
mkdir -p out/audit

echo "Generating wrappers (pass 1) + symbol probe..."
"$PYTHON" -m autogen generate-all --irs out/ir/*.json \
    --out "$SCRIPT_DIR/../src/autowrapper" \
    --probe-out "$SCRIPT_DIR/out/audit/probe.cpp"

if "$PYTHON" -m autogen audit --irs out/ir/*.json \
        --probe "$SCRIPT_DIR/out/audit/probe.cpp" \
        --work "$SCRIPT_DIR/out/audit" \
        --out "$SCRIPT_DIR/out/audit/missing.txt"; then
    if [ -s "$SCRIPT_DIR/out/audit/missing.txt" ]; then
        echo "Regenerating wrappers (pass 2): skipping $(wc -l < "$SCRIPT_DIR/out/audit/missing.txt") missing symbol(s)..."
        "$PYTHON" -m autogen generate-all --irs out/ir/*.json \
            --out "$SCRIPT_DIR/../src/autowrapper" \
            --missing "$SCRIPT_DIR/out/audit/missing.txt"
    fi
else
    echo "Symbol audit unavailable (g++/nm or OCCT libraries missing); using pass-1 output." >&2
fi

echo "Autowrapper bindings generated (out/ir -> ../src/autowrapper)."
