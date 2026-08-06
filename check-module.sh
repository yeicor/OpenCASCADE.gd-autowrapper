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

CXX="${CXX:-c++}"
set -x
"$CXX" -std=gnu++17 -fsyntax-only -fPIC \
    -DDEBUG_ENABLED -DGDEXTENSION -DHAVE_FREETYPE -DHAVE_OPENGL_EXT \
    -DHAVE_RAPIDJSON -DHAVE_XLIB -DLINUX_ENABLED -DOCC_CONVERT_SIGNALS \
    -DOpenCASCADE_gd_EXPORTS -DTHREADS_ENABLED -DUNIX_ENABLED \
    -include "$ROOT/src/occt_guard.hxx" \
    -I"$ROOT/src" \
    -isystem "$ROOT/godot-cpp/include" \
    -isystem "$ROOT/src/autowrapper" \
    -isystem "$GEXT/gen/include" \
    -isystem "$OCC" \
    "${CPPS[@]}"
