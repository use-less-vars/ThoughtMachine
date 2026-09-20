#!/usr/bin/env python3
"""Anti-vacuity collection guard for the platform-matrix CI workflow.

A test job that "passes" because pytest silently collected *zero* tests proves
nothing.  This guard runs pytest in collection-only mode, parses the reported
collection count, and fails (exit 1) when it does not match an expected value.

It is dependency-free (standard library only) so it can run before the
project's own dependencies are installed, and it is deliberately named without
a ``test_`` prefix so pytest never collects it.  ``pyproject.toml`` also scopes
collection to ``testpaths = ["tests"]``, which excludes ``scripts/`` entirely --
either mechanism alone keeps this file out of the suite.

Usage
-----
    python scripts/ci_assert_collected.py [--expected N] [--marker EXPR] [TARGET ...]

Defaults target the primary Linux/macOS gate::

    --expected 3644   # `-m "not docker and not e2e"` selection
    --marker   "not docker and not e2e"

Passing one or more positional ``TARGET`` paths (files or directories) runs
pytest against exactly those targets and IGNORES ``--marker`` (the explicit
target list is authoritative).  This is how the Windows thin-slice job pins its
small, self-contained node set.

Recognised pytest summaries (checked in this order)::

    3644/3649 tests collected (5 deselected) in 10.61s   -> 3644
    3649 tests collected in 10.49s                        -> 3649
    no tests collected in 0.10s                           -> 0

Exit status is 0 on an exact match and 1 on mismatch *or* when no collection
count can be parsed (e.g. a collection error aborted the run).
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys

# Measured on branch fix/git-host-fallback-closure: 3649 tests collect in total
# and `-m "not docker and not e2e"` selects 3644 of them (5 deselected).
DEFAULT_EXPECTED = 3644
DEFAULT_MARKER = "not docker and not e2e"

# "3644/3649 tests collected (5 deselected)" -> 3644 (the selected count).
_DESELECTED_RE = re.compile(r"(\d+)/\d+\s+tests?\s+collected")
# "3649 tests collected" -> 3649.
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
    if actual != args.expected:
        print(
            f"ci_assert_collected: FAIL - expected {args.expected} collected "
            f"tests but pytest collected {actual}.",
            file=sys.stderr,
        )
        return 1

    print("ci_assert_collected: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
