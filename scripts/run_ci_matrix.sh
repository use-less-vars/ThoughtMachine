#!/usr/bin/env bash
# run_ci_matrix.sh - CI matrix runner for the ThoughtMachine test suite.
#
#   (1) runs the three Phase-3 regression files individually,
#   (2) runs every other test file under tests/ (all subdirectories),
#   (3) prints a PASS/FAIL summary line per file (with pytest counts) and
#       exits non-zero if any file fails.
#
# Plain bash + `python3 -m pytest -q` per file.  No heavy dependencies.

set -u
cd "$(dirname "$0")/.." || exit 1

PYTHON_BIN="${PYTHON:-python3}"
failures=0
total=0

run_one() {
    local file="$1"
    total=$((total + 1))
    local out rc tail_line
    out="$("$PYTHON_BIN" -m pytest -q -p no:cacheprovider "$file" 2>&1)"
    rc=$?
    if [ "$rc" -eq 0 ]; then
        tail_line="$(printf '%s\n' "$out" | tail -n 1)"
        echo "PASS  ${file}  (${tail_line})"
    else
        failures=$((failures + 1))
        echo "FAIL  ${file}"
        printf '%s\n' "$out" | tail -n 15
    fi
}

echo "== [1/2] Phase-3 regression files (individually) =="
for f in \
    tests/test_tool_import_robustness.py \
    tests/test_health_endpoint_robustness.py \
    tests/test_install_scripts.py; do
    run_one "$f"
done

echo "== [2/2] Rest of the suite =="
for f in $(find tests -type f -name 'test_*.py' | sort); do
    case "$f" in
        tests/test_tool_import_robustness.py|\
        tests/test_health_endpoint_robustness.py|\
        tests/test_install_scripts.py)
            continue ;;
    esac
    run_one "$f"
done

echo "== Summary =="
echo "files run: ${total}, failures: ${failures}"
if [ "$failures" -eq 0 ]; then
    echo "ALL GREEN"
    exit 0
else
    echo "SOME FAILURES"
    exit 1
fi
