#!/usr/bin/env python3
"""Red-before gate: prove that a pytest selector DISCRIMINATES a fix.

A test that passes both before and after a change proves nothing about the
change.  This gate runs the SAME selector twice -- once in a throwaway
``git worktree`` of ``<base>`` (where it must FAIL) and once in the current
working tree (where it must PASS) -- and only then reports ``PASS``.

Deliberate design points
------------------------
* The selector is run **as it exists at ``<base>``**.  The gate never patches
  the old tree with the new test file: writing today's test into yesterday's
  tree would measure "does my new test fail against my new source", not
  "would this test have caught the old bug".
* A test file ADDED by the fix commit simply does not exist at ``<base>``.
  That is a legitimate RED by construction: the outcome is reported as
  ``absent_at_base`` (not as a failure), and the current tree must still pass.
* The caller's checkout is never mutated: no ``git stash``, no checkout, no
  ``git reset``.  Only a detached worktree in a temp directory is created.
* ``git worktree add/remove`` still writes metadata under ``.git/worktrees/``
  of the repository -- that is inherent to git worktrees and is pruned in a
  ``finally:`` block.
* stdout carries EXACTLY ONE JSON object (always; ``--json`` is accepted but
  never required).  Every human-readable line goes to stderr, including the
  captured pytest output, so a CI step can pipe stdout into ``jq`` or a file.

Usage
-----
    python scripts/verify_fix.py --test <pytest-selector> --base <git-rev>
                                 [--timeout-seconds 600] [--json]

Exit codes
----------
    0  PASS     -- RED at base (or absent at base) and GREEN in the current tree
    1  FAIL     -- the pattern did not hold (base already green, current red,
                   nothing collected, zero passed, or an exit-code/verdict
                   disagreement)
    2  BLOCKED  -- the gate could not run (``git`` missing, bad ``--base``,
                   worktree could not be created or could not be removed)
    3  TIMEOUT  -- one of the two pytest runs exceeded ``--timeout-seconds``

``--test`` is REQUIRED and there is no default.  The full suite is not a
usable selector here: it is far too slow and hangs at one node, so defaulting
to it would turn every invocation into a timeout.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

__version__ = "1.0.0"

SCHEMA_VERSION = 1

# ── Verdict vocabulary ────────────────────────────────────────────────────
PASS = "PASS"
FAIL = "FAIL"
TIMEOUT = "TIMEOUT"
BLOCKED = "BLOCKED"

EXIT_CODES = {PASS: 0, FAIL: 1, BLOCKED: 2, TIMEOUT: 3}

# ── Per-run outcome vocabulary (one pytest invocation) ────────────────────
OUTCOME_PASSED = "passed"
OUTCOME_FAILED = "failed"
OUTCOME_NO_TESTS = "no_tests_collected"
OUTCOME_TIMEOUT = "timeout"
OUTCOME_DISAGREEMENT = "exit_code_verdict_disagreement"
OUTCOME_ERROR = "run_error"
OUTCOME_ABSENT_AT_BASE = "absent_at_base"

DEFAULT_TIMEOUT_S = 600.0


# ══════════════════════════════════════════════════════════════════════════
# Data model (pure)
# ══════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class Counts:
    """Outcome counters parsed from one pytest summary line."""

    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    xfailed: int = 0
    xpassed: int = 0

    @property
    def collected(self) -> int:
        """Number of tests that produced a verdict (deselected excluded)."""
        return (self.passed + self.failed + self.errors
                + self.skipped + self.xfailed + self.xpassed)

    def as_dict(self) -> dict:
        """JSON-friendly view, with the derived ``collected`` count included."""
        return {
            "passed": self.passed,
            "failed": self.failed,
            "errors": self.errors,
            "skipped": self.skipped,
            "xfailed": self.xfailed,
            "xpassed": self.xpassed,
            "collected": self.collected,
        }


@dataclass(frozen=True)
class RunOutcome:
    """What one pytest run proved (or failed to prove)."""

    kind: str
    returncode: Optional[int] = None
    counts: Counts = field(default_factory=Counts)
    duration_s: float = 0.0
    detail: str = ""

    def as_dict(self) -> dict:
        """JSON-friendly view of the run."""
        return {
            "kind": self.kind,
            "returncode": self.returncode,
            "counts": self.counts.as_dict(),
            "duration_s": round(self.duration_s, 3),
            "detail": self.detail,
        }


@dataclass(frozen=True)
class Cleanup:
    """Result of the ``finally:`` worktree teardown."""

    leftover_path: str = ""
    detail: str = ""

    def as_dict(self) -> dict:
        """JSON-friendly view of the teardown."""
        return {
            "removed": not self.leftover_path,
            "leftover_path": self.leftover_path,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class Decision:
    """The verdict the gate reports."""

    verdict: str
    reasons: tuple = ()
    detail: str = ""

    @property
    def exit_code(self) -> int:
        """Process exit status for this verdict."""
        return EXIT_CODES[self.verdict]

    @property
    def ok(self) -> bool:
        """True only for the one acceptable pattern."""
        return self.verdict == PASS


@dataclass(frozen=True)
class Selection:
    """A pytest selector split into its file and node parts."""

    raw: str
    relative: str
    file_part: str
    node_part: str


# ══════════════════════════════════════════════════════════════════════════
# Parsing pytest output (pure)
# ══════════════════════════════════════════════════════════════════════════
_TOKEN_RE = re.compile(
    r"(\d+)\s+(passed|failed|errors|error|skipped|xfailed|xpassed|deselected|warnings|warning)\b"
)
_NO_TESTS_RE = re.compile(r"\bno tests ran\b")


def _summary_line(output: str) -> Optional[str]:
    """Return the pytest summary line of *output*, or ``None``.

    pytest prints the authoritative outcome last (``1 failed in 0.12s``), so
    the search walks backwards.  A line only qualifies when it carries a
    duration (`` in ``) or the explicit ``no tests ran`` marker, which keeps
    per-test progress lines out of the parse.
    """
    lines = output.splitlines()
    for line in reversed(lines):
        stripped = line.strip()
        if not stripped:
            continue
        if _NO_TESTS_RE.search(stripped):
            return stripped
        if _TOKEN_RE.search(stripped) and " in " in stripped:
            return stripped
    for line in reversed(lines):
        stripped = line.strip()
        if _TOKEN_RE.search(stripped):
            return stripped
    return None


def parse_counts(output: str) -> Optional[Counts]:
    """Parse the outcome counters out of *output*, or ``None`` if unparseable.

    ``None`` means "cannot tell", which the gate treats as a failure, never as
    zero-failure success.  ``N subtests passed`` is deliberately NOT counted as
    ``N passed`` (it is a unittest-subtest roll-up, not a test count).
    """
    line = _summary_line(output)
    if line is None:
        return None

    totals = {"passed": 0, "failed": 0, "errors": 0,
              "skipped": 0, "xfailed": 0, "xpassed": 0}
    for match in _TOKEN_RE.finditer(line):
        count, token = int(match.group(1)), match.group(2)
        key = "errors" if token in ("error", "errors") else token
        if key in totals:
            totals[key] = max(totals[key], count)

    counts = Counts(**totals)
    if counts.collected == 0 and not _NO_TESTS_RE.search(line):
        # A line such as "3 warnings in 0.2s" carries no verdict at all.
        return None
    return counts


def classify_run(returncode: int, output: str, *,
                 duration_s: float = 0.0) -> RunOutcome:
    """Turn one finished pytest invocation into a :class:`RunOutcome`.

    This is where the exit-code/verdict DISAGREEMENT pathology is caught: a
    runner that prints failures but exits 0 (the vitest ``Errors 1 error`` +
    exit 0 shape), or prints passes yet exits non-zero, is a hard failure --
    never a warning, because the exit code is what CI trusts.
    """
    counts = parse_counts(output)
    if counts is None:
        return RunOutcome(OUTCOME_NO_TESTS, returncode, Counts(), duration_s,
                          "pytest produced no parseable summary line")

    failures = counts.failed + counts.errors

    if returncode == 0 and failures > 0:
        return RunOutcome(
            OUTCOME_DISAGREEMENT, returncode, counts, duration_s,
            f"exit code 0 but the summary reports {failures} failure(s)/error(s)")
    if returncode == 0 and counts.passed == 0:
        return RunOutcome(
            OUTCOME_NO_TESTS, returncode, counts, duration_s,
            "exit code 0 but the summary reports zero passed tests")
    if returncode != 0 and failures == 0 and counts.passed > 0:
        return RunOutcome(
            OUTCOME_DISAGREEMENT, returncode, counts, duration_s,
            f"exit code {returncode} but the summary reports "
            f"{counts.passed} passed and no failures")
    if counts.collected == 0:
        return RunOutcome(OUTCOME_NO_TESTS, returncode, counts, duration_s,
                          "pytest collected no tests for the selector")
    if failures > 0:
        return RunOutcome(OUTCOME_FAILED, returncode, counts, duration_s,
                          f"{failures} failing test(s)/error(s)")
    if counts.passed == 0:
        return RunOutcome(
            OUTCOME_NO_TESTS, returncode, counts, duration_s,
            "no test actually passed (every collected test was skipped/xfailed)")
    return RunOutcome(OUTCOME_PASSED, returncode, counts, duration_s,
                      f"{counts.passed} passed")


# ══════════════════════════════════════════════════════════════════════════
# The decision table (pure -- this is the whole gate)
# ══════════════════════════════════════════════════════════════════════════
def decide(base: Optional[RunOutcome], head: Optional[RunOutcome], *,
           cleanup: Optional[Cleanup] = None,
           blocked: str = "") -> Decision:
    """Map the two run outcomes onto a verdict. No I/O, no side effects.

    ``blocked`` short-circuits everything (the gate could not run at all).
    A leftover worktree outranks any run outcome: a gate that claims PASS and
    then leaves a locked tree behind has still broken the caller's repository.
    """
    if blocked:
        return Decision(BLOCKED, (blocked,), "the gate could not run")
    if cleanup is not None and cleanup.leftover_path:
        return Decision(
            BLOCKED, ("worktree_leftover",),
            f"throwaway worktree still present after cleanup: "
            f"{cleanup.leftover_path}"
            + (f" ({cleanup.detail})" if cleanup.detail else ""))
    if base is None or head is None:
        return Decision(BLOCKED, ("gate_incomplete",),
                        "one of the two runs did not complete")

    # Timeouts outrank everything they touch: a hung run is not a RED and
    # is emphatically not a GREEN.
    if base.kind == OUTCOME_TIMEOUT:
        return Decision(TIMEOUT, ("base_timeout",), base.detail)
    if head.kind == OUTCOME_TIMEOUT:
        return Decision(TIMEOUT, ("head_timeout",), head.detail)

    if base.kind == OUTCOME_ERROR:
        return Decision(FAIL, ("base_run_error",), base.detail)
    if head.kind == OUTCOME_ERROR:
        return Decision(FAIL, ("head_run_error",), head.detail)

    if base.kind == OUTCOME_DISAGREEMENT:
        return Decision(FAIL, ("base_exit_code_verdict_disagreement",), base.detail)
    if head.kind == OUTCOME_DISAGREEMENT:
        return Decision(FAIL, ("head_exit_code_verdict_disagreement",), head.detail)

    # The CURRENT tree must be unambiguously green before anything else counts.
    if head.kind == OUTCOME_NO_TESTS:
        return Decision(FAIL, ("head_no_tests_collected",), head.detail)
    if head.kind == OUTCOME_FAILED:
        return Decision(FAIL, ("head_not_green",), head.detail)
    if head.counts.passed == 0:
        return Decision(FAIL, ("zero_passed",),
                        "the current run reported '0 passed'")
    if head.kind != OUTCOME_PASSED:
        return Decision(FAIL, ("head_unknown_outcome",),
                        f"unhandled head outcome {head.kind!r}")

    # head is green: now the base run must have been RED.
    if base.kind == OUTCOME_ABSENT_AT_BASE:
        return Decision(
            PASS, ("new_test_absent_at_base", "red_at_base", "green_now"),
            "the selector does not exist at base (new test file) and passes "
            "in the current tree")
    if base.kind == OUTCOME_PASSED:
        return Decision(FAIL, ("base_not_red",),
                        "the selector already passed at base: it does not "
                        "discriminate the fix")
    if base.kind == OUTCOME_NO_TESTS:
        return Decision(FAIL, ("base_no_tests_collected",), base.detail)
    if base.kind == OUTCOME_FAILED:
        return Decision(PASS, ("red_at_base", "green_now"),
                        "the selector failed at base and passes in the current tree")
    return Decision(FAIL, ("base_unknown_outcome",),
                    f"unhandled base outcome {base.kind!r}")


# ══════════════════════════════════════════════════════════════════════════
# Selector handling (pure)
# ══════════════════════════════════════════════════════════════════════════
def parse_selection(raw: str, root: Path) -> Selection:
    """Split *raw* into a repo-relative selector, file part and node part.

    Raises:
        ValueError: when the selector is empty, or is an absolute path that
            does not live inside *root*.
    """
    selector = (raw or "").strip()
    if not selector:
        raise ValueError("--test must not be empty")
    if os.path.isabs(selector):
        try:
            selector = str(Path(selector).resolve().relative_to(Path(root).resolve()))
        except ValueError as exc:
            raise ValueError(
                f"--test {raw!r} is outside the repository {root}") from exc
    while selector.startswith("./"):
        selector = selector[2:]
    file_part, _, node_part = selector.partition("::")
    if not file_part:
        raise ValueError(f"--test {raw!r} has no file part before '::'")
    return Selection(raw=raw, relative=selector, file_part=file_part,
                     node_part=node_part)


# ══════════════════════════════════════════════════════════════════════════
# I/O: git, worktrees, pytest
# ══════════════════════════════════════════════════════════════════════════
def _eprint(message: str = "") -> None:
    """Write a human-readable line to stderr (stdout is reserved for JSON)."""
    print(message, file=sys.stderr, flush=True)


def _git(root: Path, *args: str,
         timeout_s: float = 120.0) -> Optional[subprocess.CompletedProcess]:
    """Run ``git <args>`` in *root*; ``None`` when the call could not be made."""
    try:
        return subprocess.run(["git", *args], cwd=str(root), capture_output=True,
                              text=True, timeout=timeout_s)
    except (OSError, subprocess.SubprocessError) as exc:
        _eprint(f"verify_fix: git {' '.join(args)} failed: {exc}")
        return None


def repo_root() -> Optional[Path]:
    """Resolve the repository root from the current working directory."""
    try:
        proc = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                              capture_output=True, text=True, timeout=60.0)
    except (OSError, subprocess.SubprocessError) as exc:
        _eprint(f"verify_fix: git rev-parse failed: {exc}")
        return None
    if proc.returncode != 0:
        _eprint(f"verify_fix: not inside a git work tree: {proc.stderr.strip()}")
        return None
    root = Path(proc.stdout.strip())
    return root if root.is_dir() else None


def resolve_commit(root: Path, rev: str) -> Optional[str]:
    """Resolve *rev* to a full commit SHA, or ``None`` when it does not exist."""
    proc = _git(root, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}")
    if proc is None or proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def create_worktree(root: Path, path: Path, commit: str) -> tuple:
    """Add a detached worktree of *commit* at *path*.

    Returns ``(ok, detail)``.  Never raises.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    proc = _git(root, "worktree", "add", "--detach", str(path), commit)
    if proc is None:
        return False, "git worktree add could not be executed"
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout or "").strip()[:2000]
    return True, ""


def remove_worktree(root: Path, path: Path) -> Cleanup:
    """Remove *path* and prune its metadata. Idempotent, never raises.

    A crashed previous run can leave the directory LOCKED; ``worktree
    unlock`` is attempted before the retry so a lock is never inherited by
    the next run.  Everything is best-effort: the caller turns a surviving
    directory into a BLOCKED verdict instead of pretending it went away.
    """
    notes = []
    try:
        if path.exists():
            proc = _git(root, "worktree", "remove", "--force", str(path))
            if proc is not None and proc.returncode != 0:
                notes.append(f"remove failed: {(proc.stderr or '').strip()[:400]}")
                locked = _git(root, "worktree", "unlock", str(path))
                if locked is not None and locked.returncode == 0:
                    notes.append("unlocked")
                    _git(root, "worktree", "remove", "--force", str(path))
            if path.exists():
                shutil.rmtree(path, ignore_errors=True)
                if not path.exists():
                    notes.append("removed by shutil.rmtree")
        pruned = _git(root, "worktree", "prune")
        if pruned is not None and pruned.returncode == 0:
            notes.append("pruned")
    except Exception as exc:  # cleanup must never mask the verdict
        notes.append(f"{type(exc).__name__}: {exc}")

    if path.exists():
        return Cleanup(leftover_path=str(path), detail="; ".join(notes))
    return Cleanup(leftover_path="", detail="; ".join(notes))


def run_pytest(cwd: Path, selector: str, timeout_s: float) -> RunOutcome:
    """Run *selector* with pytest in *cwd*, time-boxed to *timeout_s*.

    The captured output is echoed to stderr (human record) and then turned
    into a :class:`RunOutcome`.  A timeout is ``OUTCOME_TIMEOUT``, never a
    pass and never a RED.
    """
    cmd = [sys.executable, "-m", "pytest", selector, "-q",
           "-p", "no:warnings", "-p", "no:cacheprovider"]
    _eprint(f"verify_fix: [{cwd}] {' '.join(cmd)}")
    started = time.monotonic()
    try:
        proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                              timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        elapsed = time.monotonic() - started
        _eprint(f"verify_fix: TIMEOUT after {timeout_s:.0f}s in {cwd}")
        partial = exc.stdout or ""
        if isinstance(partial, bytes):
            partial = partial.decode("utf-8", "replace")
        if partial:
            _eprint(partial)
        return RunOutcome(OUTCOME_TIMEOUT, None, Counts(), elapsed,
                          f"pytest exceeded {timeout_s:.0f}s in {cwd}")
    except OSError as exc:
        elapsed = time.monotonic() - started
        return RunOutcome(OUTCOME_ERROR, None, Counts(), elapsed,
                          f"could not execute pytest: {exc}")

    output = (proc.stdout or "") + "\n" + (proc.stderr or "")
    _eprint(output)
    return classify_run(proc.returncode, output,
                        duration_s=time.monotonic() - started)


# ══════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════
def build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser for the gate."""
    parser = argparse.ArgumentParser(
        prog="verify_fix.py",
        description=(
            "Red-before gate: prove a pytest selector fails at <base> and "
            "passes in the current tree. Exactly one JSON object is printed "
            "to stdout; all human output goes to stderr."
        ),
        epilog=(
            "Exit codes: 0 PASS, 1 FAIL, 2 BLOCKED, 3 TIMEOUT. "
            "A selector is mandatory: the full suite is too slow and hangs "
            "at one node, so it is never used as a default."
        ),
    )
    parser.add_argument(
        "--test", required=True, metavar="SELECTOR",
        help="pytest selector, e.g. tests/test_x.py::test_y (required)")
    parser.add_argument(
        "--base", required=True, metavar="REV",
        help="git revision the selector must FAIL at, e.g. HEAD~1 or a SHA")
    parser.add_argument(
        "--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_S,
        metavar="N",
        help=f"time-box applied to EACH pytest run (default: {DEFAULT_TIMEOUT_S:.0f})")
    parser.add_argument(
        "--json", action="store_true",
        help=("accepted for symmetry only: stdout is always one JSON object, "
              "so this flag changes nothing"))
    parser.add_argument(
        "--version", action="version",
        version=f"verify_fix {__version__}")
    return parser


def _parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    """Parse command-line arguments."""
    return build_parser().parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the gate and return the process exit code."""
    args = _parse_args(argv)
    started = time.monotonic()

    report: dict = {
        "schema_version": SCHEMA_VERSION,
        "tool": "verify_fix",
        "version": __version__,
        "verdict": BLOCKED,
        "exit_code": EXIT_CODES[BLOCKED],
        "reasons": [],
        "detail": "",
        "test_selector": args.test,
        "base_rev": args.base,
        "base_commit": None,
        "repo_root": None,
        "worktree_path": None,
        "base_outcome": None,
        "head_outcome": None,
        "counts": {"base": None, "head": None},
        "runs": {"base": None, "head": None},
        "cleanup": {"removed": True, "leftover_path": "",
                    "detail": "not attempted"},
        "timeout_seconds": args.timeout_seconds,
        "durations": {"base_s": 0.0, "head_s": 0.0, "total_s": 0.0},
    }

    def finish(decision: Decision) -> int:
        """Print the single JSON object and return the exit code."""
        report["verdict"] = decision.verdict
        report["reasons"] = list(decision.reasons)
        report["detail"] = decision.detail
        report["exit_code"] = decision.exit_code
        report["durations"]["total_s"] = round(time.monotonic() - started, 3)
        print(json.dumps(report, indent=2, sort_keys=False))
        sys.stdout.flush()
        _eprint(f"verify_fix: {decision.verdict} "
                f"[{', '.join(decision.reasons) or 'ok'}] "
                f"-> exit {decision.exit_code}")
        if decision.detail:
            _eprint(f"verify_fix: {decision.detail}")
        return decision.exit_code

    # ── Preconditions ────────────────────────────────────────────────────
    if shutil.which("git") is None:
        return finish(Decision(
            BLOCKED, ("git-unavailable",),
            "the 'git' binary is not on PATH; the gate needs a worktree"))

    root = repo_root()
    if root is None:
        return finish(Decision(
            BLOCKED, ("repo_root_unresolved",),
            "`git rev-parse --show-toplevel` failed: not inside a git work tree"))
    report["repo_root"] = str(root)

    try:
        selection = parse_selection(args.test, root)
    except ValueError as exc:
        return finish(Decision(BLOCKED, ("bad_selector",), str(exc)))

    commit = resolve_commit(root, args.base)
    if commit is None:
        return finish(Decision(
            BLOCKED, ("base_rev_unknown",),
            f"`git rev-parse --verify {args.base}^{{commit}}` failed"))
    report["base_commit"] = commit

    # ── The two runs, with a guaranteed teardown ─────────────────────────
    tmpdir = Path(tempfile.mkdtemp(prefix="verify-fix-"))
    worktree = tmpdir / "worktree"
    base_outcome: Optional[RunOutcome] = None
    head_outcome: Optional[RunOutcome] = None
    hard_block: Optional[Decision] = None
    cleanup = Cleanup(leftover_path="", detail="not attempted")

    try:
        ok, detail = create_worktree(root, worktree, commit)
        if not ok:
            hard_block = Decision(
                BLOCKED, ("worktree_create_failed",),
                f"could not create a worktree of {commit[:12]} at "
                f"{worktree}: {detail}")
        else:
            report["worktree_path"] = str(worktree)

            # (1) base run: the selector AS IT EXISTS AT <base>.
            base_file = worktree / selection.file_part
            if not base_file.exists():
                base_outcome = RunOutcome(
                    OUTCOME_ABSENT_AT_BASE, None, Counts(), 0.0,
                    f"{selection.file_part} does not exist at {commit[:12]} "
                    f"(a new test file is RED by construction)")
                _eprint(f"verify_fix: {selection.file_part} absent at base -- "
                        f"recorded as absent_at_base, head run decides")
            else:
                base_outcome = run_pytest(worktree, selection.relative,
                                          args.timeout_seconds)
            report["base_outcome"] = base_outcome.kind
            report["runs"]["base"] = base_outcome.as_dict()
            report["counts"]["base"] = base_outcome.counts.as_dict()
            report["durations"]["base_s"] = round(base_outcome.duration_s, 3)

            # (2) head run: the current working tree must be GREEN.
            head_outcome = run_pytest(root, selection.relative,
                                      args.timeout_seconds)
            report["head_outcome"] = head_outcome.kind
            report["runs"]["head"] = head_outcome.as_dict()
            report["counts"]["head"] = head_outcome.counts.as_dict()
            report["durations"]["head_s"] = round(head_outcome.duration_s, 3)
    except Exception as exc:  # a crash is a BLOCKED verdict, never a pass
        hard_block = Decision(BLOCKED, ("gate_crashed",),
                              f"{type(exc).__name__}: {exc}")
    finally:
        cleanup = remove_worktree(root, worktree)
        shutil.rmtree(tmpdir, ignore_errors=True)
        report["cleanup"] = cleanup.as_dict()
        if cleanup.leftover_path:
            _eprint(f"verify_fix: WARNING leftover worktree: "
                    f"{cleanup.leftover_path}")
        _eprint(f"verify_fix: teardown: {cleanup.detail or 'clean'}")

    if hard_block is not None:
        decision = hard_block
    else:
        decision = decide(base_outcome, head_outcome, cleanup=cleanup)
    return finish(decision)


if __name__ == "__main__":
    raise SystemExit(main())
