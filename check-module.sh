#!/usr/bin/env bash
# Fast dev loop: syntax-check ONE generated wrapper module (or a single .cpp)
# without rebuilding the whole gdext port. Mirrors the flags from
# vcpkg/buildtrees/gdext/build-x64-linux-dbg-out.log.
#
# Usage: ./check-module.sh <OcgModule or path-to.cpp>   (default: NCollection)
#   e.g. ./check-module.sh OcgGraphic3dCamera   # globs the .cpp
#   e.g. ./check-module.sh                       # the default target
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

ARG="${1:-OcgNCollection}"
if [[ "$ARG" == *.cpp ]]; then
    CPPS=("$ARG")
else
    mapfile -t CPPS < <(ls "$ROOT"/src/autowrapper/"$ARG"*.cpp 2>/dev/null)
fi
if [ "${#CPPS[@]}" -eq 0 ]; then
    echo "no generated cpp matched: $ARG" >&2
    exit 2
fi
echo "checking ${#CPPS[@]} translation unit(s): ${CPPS[0]##*/} ..."

# Use the gdext build tree's flags (falls back gracefully if absent).
GEXT="$ROOT/vcpkg/buildtrees/gdext/x64-linux-dbg/godot-cpp"
OCC="$ROOT/vcpkg/installed/x64-linux/include/opencascade"

# Import the OCCT feature + platform defines from the vcpkg OCCT install's own
# CMake metadata (see autogen/compile_db.py::_occt_compile_definitions) so the
# check mirrors the exact environment that built the library.
PYTHON="${PYTHON:-python3}"
OCCT_DEFS=$("$PYTHON" -c "
import sys
sys.path.insert(0, r'$SCRIPT_DIR')
from pathlib import Path
from autogen.compile_db import _occt_compile_definitions, _platform_defines
inc = Path(r'$OCC')
defs = ['-D' + d for d in _occt_compile_definitions(inc)]
defs += _platform_defines('x64-linux')
print(' '.join(defs))
")

CXX="${CXX:-c++}"
set -x
"$CXX" -std=gnu++17 -fsyntax-only -fPIC \
    -DDEBUG_ENABLED -DGDEXTENSION \
    -DOpenCASCADE_gd_EXPORTS -DTHREADS_ENABLED \
    $OCCT_DEFS \
    -include "$ROOT/src/occt_guard.hxx" \
    -I"$ROOT/src" \
    -isystem "$ROOT/godot-cpp/include" \
    -isystem "$ROOT/src/autowrapper" \
    -isystem "$GEXT/gen/include" \
    -isystem "$OCC" \
    "${CPPS[@]}"
