#!/usr/bin/env bash
# generate.sh - Generate GDExtension autowrapper bindings for OpenCASCADE
#
# This scans OCCT headers using libclang (via compilation database) and
# generates godot-cpp wrapper code that exposes the full OpenCASCADE API
# to GDScript.

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
AUTOWRAPPER_DIR="$PROJECT_ROOT/src/autowrapper"

# --- Step 1: Generate compile_commands.json via cmake configure ---
BUILD_DIR="${PROJECT_ROOT}/.build-autowrapper"

if [ ! -f "$BUILD_DIR/compile_commands.json" ]; then
    echo "Generating compile_commands.json via cmake configure..."
    mkdir -p "$BUILD_DIR"
    cmake -S "$PROJECT_ROOT" -B "$BUILD_DIR" \
        -DCMAKE_BUILD_TYPE=Debug \
        -DGODOTCPP_TARGET=template_debug \
        -DCMAKE_EXPORT_COMPILE_COMMANDS=ON \
        -DCMAKE_PREFIX_PATH="$PROJECT_ROOT/vcpkg/installed/${VCPKG_DEFAULT_TRIPLET:-x64-linux}" \
        2>&1 | tail -5
    echo ""
fi

if [ ! -f "$BUILD_DIR/compile_commands.json" ]; then
    echo "Error: Failed to generate compile_commands.json"
    exit 1
fi

echo "OpenCASCADE.gd-autowrapper"
echo "  compile_commands.json: $BUILD_DIR/compile_commands.json"
echo "  Output dir:            $AUTOWRAPPER_DIR"
echo ""

# Pass any extra arguments to generate.py (e.g. --modules gp TopoDS)
EXTRA_ARGS="${GENERATE_ARGS:-}"

# Run the Python generator
python3 "$SCRIPT_DIR/generate.py" \
    --compile-commands "$BUILD_DIR/compile_commands.json" \
    --output-dir "$AUTOWRAPPER_DIR" \
    $EXTRA_ARGS

echo ""
echo "OpenCASCADE.gd-autowrapper: generation complete"
