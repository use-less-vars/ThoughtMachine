#!/usr/bin/env python3
"""Anti-vacuity collection guard for the platform-matrix CI workflow.

A test job that "passes" because pytest silently collected *zero* tests proves
nothing.  This guard runs pytest in collection-only mode, parses the reported
collection count, and fails (exit 1) when fewer tests are collected than the
expected minimum.

It is dependency-free (standard library only) so it can run before the
project's own dependencies are installed, and it is deliberately named without
a ``test_`` prefix so pytest never collects it.  ``pyproject.toml`` also scopes
collection to ``testpaths = ["tests", "web_ui/backend/tests"]``, which excludes
``scripts/`` entirely --
either mechanism alone keeps this file out of the suite.

Usage
-----
    python scripts/ci_assert_collected.py [--expected N] [--marker EXPR] [TARGET ...]

Defaults target the primary Linux/macOS gate::

    --expected 7600   # `-m "not docker and not e2e"` floor (measured 7721)
    --marker   "not docker and not e2e"

Passing one or more positional ``TARGET`` paths (files or directories) runs
pytest against exactly those targets and IGNORES ``--marker`` (the explicit
target list is authoritative).  This is how the Windows thin-slice job pins its
small, self-contained node set.

Recognised pytest summaries (checked in this order)::

    7721/7727 tests collected (6 deselected) in 6.93s    -> 7721
    7727 tests collected in 6.93s                        -> 7727
    no tests collected in 0.10s                           -> 0

Exit status is 0 when at least the expected number of tests is collected and 1
when fewer are collected *or* when no collection count can be parsed (e.g. a
collection error aborted the run).
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys

# Lower-bound baseline for the primary Linux/macOS gate: `-m "not docker and
# not e2e"` must collect at least 7600 tests.  Measured 7721 live (7727 total,
# 6 deselected) over `testpaths = ["tests", "web_ui/backend/tests"]`, so the
# floor sits a couple of percent below the measurement.  The comparison is
# `actual < expected`, so new tests never fail the gate; only a shrinking
# collection does.
# Floor raised by C91 to match actual test count. Update on every test addition.
#
# WHY THE FLOOR IS NOT THE MEASUREMENT.  Every count above was measured with
# Python 3.12 (the only interpreter with this project's dependencies
# installed), but the CI jobs running this script set
# `env.PYTHON_VERSION: "3.14"` in .github/workflows/platform-matrix.yml.
# Collected counts can differ between 3.12 and 3.14: a module guarded by a
# `sys.version_info` check, or imported inside a try/except that starts
# succeeding, changes the size of the collection.  A floor of exactly 7721
# would therefore let a cross-version difference of one test fail the primary
# gate on the very first pull request after this lands -- the repo freezes, and
# the anti-vacuity guard becomes the reason nothing can merge.  A floor a
# couple of percent lower still catches the failure it exists to catch (a
# suite that silently collects zero, or a large block of tests that stopped
# being collected) while tolerating interpreter noise.
#
# PIN IT AFTER THE FIRST REAL RUN.  The first green 3.14 CI run prints
# `ci_assert_collected: expected=7600 actual=N`; N is the true 3.14 count and
# the number to pin afterwards, minus the same couple of percent of headroom.
# Until that run exists, 7600 is the largest floor defensible from the
# measurements actually taken.
DEFAULT_EXPECTED = 7600
DEFAULT_MARKER = "not docker and not e2e"

# "7721/7727 tests collected (6 deselected)" -> 7721 (the selected count).
_DESELECTED_RE = re.compile(r"(\d+)/\d+\s+tests?\s+collected")
# "7727 tests collected" -> 7727.
_COLLECTED_RE = re.compile(r"(\d+)\s+tests?\s+collected")
# "no tests collected" -> 0.
_NONE_RE = re.compile(r"no\s+tests?\s+collected")


def parse_collected(output: str):
    """Return the collected test count from pytest output, or ``None``.

    The deselected form (``N/M tests collected (K deselected)``) must be tested
    first because the plain form would otherwise also match its ``M``.
    """
    match = _DESELECTED_RE.search(output)
    if match:
        return int(match.group(1))
    if _NONE_RE.search(output):
        return 0
    match = _COLLECTED_RE.search(output)
    if match:
        return int(match.group(1))
    return None


def build_command(args) -> list:
    cmd = [sys.executable, "-m", "pytest", "--collect-only", "-q"]
    if args.targets:
        cmd.extend(args.targets)
    elif args.marker:
        cmd.extend(["-m", args.marker])
    return cmd


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Assert that pytest collects an expected number of tests "
            "(anti-vacuity guard)."
        )
    )
    parser.add_argument(
        "--expected",
        type=int,
        default=DEFAULT_EXPECTED,
        help=f"expected number of collected tests (default: {DEFAULT_EXPECTED})",
    )
    parser.add_argument(
        "--marker",
        default=DEFAULT_MARKER,
        help=(
            "pytest -m expression; ignored when positional targets are given "
            f"(default: '{DEFAULT_MARKER}')"
        ),
    )
    parser.add_argument(
        "targets",
        nargs="*",
        help="explicit pytest target paths; when given, --marker is ignored",
    )
    args = parser.parse_args(argv)

    cmd = build_command(args)
    print(f"ci_assert_collected: running: {' '.join(cmd)}", flush=True)

    proc = subprocess.run(cmd, capture_output=True, text=True)
    output = (proc.stdout or "") + "\n" + (proc.stderr or "")

    actual = parse_collected(output)
    if actual is None:
        print(
            "ci_assert_collected: could not parse a collection count from "
            "pytest output.",
            file=sys.stderr,
        )
        print("----- pytest output (tail) -----", file=sys.stderr)
        print("\n".join(output.splitlines()[-40:]), file=sys.stderr)
        return 1

    print(f"ci_assert_collected: expected={args.expected} actual={actual}")
    if actual < args.expected:
        print(
            f"ci_assert_collected: FAIL - expected at least {args.expected} "
            f"collected tests but pytest collected only {actual}.",
            file=sys.stderr,
        )
        return 1

    print("ci_assert_collected: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
