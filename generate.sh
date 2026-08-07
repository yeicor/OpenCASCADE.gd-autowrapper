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
# OSD_Path::LocateExecFile, where only the free function is exported) or whose
# instantiation does not compile (e.g. a synthesized NCollection_Vec3<unsigned
# long>::cwiseAbs with an ambiguous std::abs) are skipped in a regeneration.
# Both findings are re-audited until the probe compiles clean and no symbols
# are missing, so the final wrappers are exactly what the library supports.
mkdir -p out/audit

PROBE="$SCRIPT_DIR/out/audit/probe.cpp"
MISSING="$SCRIPT_DIR/out/audit/missing.txt"
ILLFORMED="$SCRIPT_DIR/out/audit/illformed.txt"
# Append-only skip sets that grow across passes.  The audit rewrites
# missing/illformed.txt with *current* findings every run, so they cannot be
# passed straight to generate-all (a later pass would silently un-skip earlier
# findings); the accumulated files carry everything found so far.
ACCUM_MISSING="$SCRIPT_DIR/out/audit/skips-missing.txt"
ACCUM_ILLFORMED="$SCRIPT_DIR/out/audit/skips-illformed.txt"
: > "$ACCUM_MISSING"
: > "$ACCUM_ILLFORMED"

for i in 1 2 3 4 5 6; do
    echo "Generating wrappers (pass $i) + symbol probe..."
    "$PYTHON" -m autogen generate-all --irs out/ir/*.json \
        --out "$SCRIPT_DIR/../src/autowrapper" \
        --probe-out "$PROBE" \
        --missing "$ACCUM_MISSING" \
        --illformed "$ACCUM_ILLFORMED" \
        --synth-cache "$SCRIPT_DIR/out/synth/specs.json"

    if ! "$PYTHON" -m autogen audit --irs out/ir/*.json \
            --probe "$PROBE" \
            --work "$SCRIPT_DIR/out/audit" \
            --out "$MISSING" \
            --illformed-out "$ILLFORMED"; then
        echo "Symbol audit unavailable (g++/nm or OCCT libraries missing); using pass-$i output." >&2
        break
    fi

    if [ ! -s "$MISSING" ] && [ ! -s "$ILLFORMED" ]; then
        break
    fi

    # Merge this pass's findings into the accumulated skip sets.
    if [ -s "$MISSING" ]; then
        cat "$MISSING" "$ACCUM_MISSING" | sort -u > "$ACCUM_MISSING.tmp"
        mv "$ACCUM_MISSING.tmp" "$ACCUM_MISSING"
        echo "Pass $i found $(wc -l < "$MISSING") missing symbol(s); regenerating..."
    fi
    if [ -s "$ILLFORMED" ]; then
        cat "$ILLFORMED" "$ACCUM_ILLFORMED" | sort -u > "$ACCUM_ILLFORMED.tmp"
        mv "$ACCUM_ILLFORMED.tmp" "$ACCUM_ILLFORMED"
        echo "Pass $i found $(wc -l < "$ILLFORMED") ill-formed method(s); regenerating..."
    fi
done

echo "Autowrapper bindings generated (out/ir -> ../src/autowrapper)."
