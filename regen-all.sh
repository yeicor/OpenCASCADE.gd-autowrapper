#!/usr/bin/env bash
# Re-wrap ALL modules from the cached IR/synth/skips and run the symbol audit
# to a fixpoint -- generate.sh's post-scan loop without the header scan.
#
# Use after a codegen/typemap/synthesis change that affects many modules (the
# module-scoped dev loop `generate-all --module-filter` stays for single-module
# work; this settles the cross-module fallout and refreshes out/audit/*.txt).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PYTHON="${PYTHON:-python3}"

mkdir -p out/audit

PROBE="$SCRIPT_DIR/out/audit/probe.cpp"
MISSING="$SCRIPT_DIR/out/audit/missing.txt"
ILLFORMED="$SCRIPT_DIR/out/audit/illformed.txt"
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
        --illformed "$ACCUM_ILLFORMED"

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

echo "Autowrapper bindings regenerated from cached IR (out/ir -> ../src/autowrapper)."
