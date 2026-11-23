#!/usr/bin/env python3
"""The mutation gate: turn "this test would FAIL if that enforcement were removed" into a check.

``docs/CONTRACTS.md`` names, for every invariant it claims, an enforcement site
and a test that "would FAIL if that enforcement were removed".  Nothing in this
repository has ever executed that claim, and at least one of them is false: the
``DEFAULT_RUNTIME_MODE`` literal named by Contract 3 can be flipped while every
test cited for it stays green (Contract 3, Known gap 1).  A claim nobody runs is
a comment.

This harness runs it.  One declared mutation at a time: verify the anchor,
write the mutated file, run *only* the tests that are supposed to die, restore
the file, verify the restoration by sha256, report a verdict.

Verdicts -- this vocabulary is the whole contract of the tool
-------------------------------------------------------------
``KILLED``
    The mutation was applied and the named tests failed.  The control is real.
``SURVIVED``
    The mutation was applied and the named tests *passed*.  The control is
    vacuous -- this is the finding the gate exists to produce, not an error in
    the gate.
``REFUSED``
    The mutation could not be applied: the file is missing, ``old`` is absent,
    ``old`` occurs more than once, or the named test ids are not collectable or
    were already failing before the mutation (``baseline-not-green``).
    A refusal is NEVER a pass and always makes the process exit non-zero; the
    reason is printed on stderr.
``RESTORE_FAILED``
    After restoring, the file's sha256 does not match what it was before.  The
    tree is dirty.  Printed as a banner with the backup path and the exact
    commands to put it right, and it exits non-zero -- it must never be read as
    an ordinary test failure.

Why the anchor is text, never a line number
-------------------------------------------
Line numbers drift; a gate that mutates "line 90" starts mutating something
else the moment a docstring above it grows, and it would then be *testing* the
wrong thing while still reporting KILLED or SURVIVED.  ``old`` must therefore
occur exactly once in the file, checked *before* anything is written.  A count
of 0 and a count of 2 are both refusals, never a best guess.

Why restoration is verified rather than assumed
-----------------------------------------------
A gate that leaves a mutated file behind is worse than no gate: the next test
run in the tree tests the mutation.  So the original bytes are hashed before,
written back in a ``finally``, re-hashed, and compared; a copy also goes to a
``.bak`` file in a temp dir, an ``atexit`` hook restores, and SIGINT/SIGTERM
restore before dying.

Why there is a baseline
-----------------------
``KILLED`` is a DELTA, not a property of the mutation: it claims the named
tests went from green to red *because* of the change.  A delta needs both
endpoints measured, so before the target file is touched the harness runs the
same pytest command against the unmutated tree.  If those tests are already
failing -- a flake, a missing dependency, someone else's red -- the run is
refused as ``baseline-not-green``: a test which is already red cannot witness a
mutation.  Without that check a pre-existing failure would be scored as
``KILLED``, which is the one verdict everybody trusts, and the gate would be
reporting that the control is real on the strength of a failure it did not
cause.  ``KILLED`` therefore means "green before, red after" and nothing else.

Usage
-----
    python3 scripts/mutation_gate.py                     # run every declared mutation
    python3 scripts/mutation_gate.py --list               # print them, run nothing
    python3 scripts/mutation_gate.py --only NAME          # run exactly one
    python3 scripts/mutation_gate.py --specs PATH --root PATH --timeout SECONDS

Output is one line per mutation plus one summary line, both greppable::

    MUTATION contract-3-default-mode: SURVIVED
    GATE_SUMMARY declared=1 ran=1 killed=0 survived=1 refused=0 restore_failed=0 exit=1

Exit codes: 0 all KILLED; 1 something SURVIVED or was REFUSED; 2 RESTORE_FAILED;
3 nothing declared (a sweep of zero mutations is not a pass); 4 internal
inconsistency (the verdict counts must sum to the number that ran).

The registry is ``scripts/mutation_specs.py``: data, not code.  It is
deliberately EMPTY in the commit that introduces this harness, so the gate
refuses (exit 3) instead of reporting a green sweep over nothing.
"""

from __future__ import annotations

import argparse
import atexit
import hashlib
import importlib.util
import os
import re
import signal
import subprocess
import sys
import tempfile
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

# ---------------------------------------------------------------------------
# Vocabulary.  These strings are printed verbatim and are parsed by whoever
# reads the log, so they are constants rather than inline literals.
# ---------------------------------------------------------------------------

KILLED = "KILLED"
SURVIVED = "SURVIVED"
REFUSED = "REFUSED"
RESTORE_FAILED = "RESTORE_FAILED"
VERDICTS = (KILLED, SURVIVED, REFUSED, RESTORE_FAILED)

# Refusal reasons.  The first four are the ones the spec of this tool names;
# the rest are pre-flight refusals -- a mutation that cannot mean anything is
# refused for the same reason: running it would produce a number that lies.
REFUSE_FILE_MISSING = "file-missing"
REFUSE_ANCHOR_ABSENT = "anchor-absent"
REFUSE_ANCHOR_AMBIGUOUS = "anchor-ambiguous"
REFUSE_TESTS_NOT_COLLECTABLE = "tests-not-collectable"
REFUSE_BASELINE_RED = "baseline-not-green"
REFUSE_PATH_ESCAPES_ROOT = "path-escapes-root"
REFUSE_NOT_TEXT = "not-a-text-file"
REFUSE_NO_OP = "no-op"
REFUSE_TIMEOUT = "tests-timed-out"

EXIT_OK = 0
EXIT_GATE_FAILED = 1
EXIT_RESTORE_FAILED = 2
EXIT_NOTHING_DECLARED = 3
EXIT_INTERNAL = 4

#: Printed verbatim whenever there is nothing to sweep.
ZERO_MESSAGE = "MUTATIONS_DECLARED=0 - a sweep of zero mutations is not a pass"

DEFAULT_TIMEOUT_S = 900.0
DEFAULT_SPECS_RELPATH = Path("scripts") / "mutation_specs.py"

_HERE = Path(__file__).resolve().parent


# ---------------------------------------------------------------------------
# The declared-mutation shape
# ---------------------------------------------------------------------------


@dataclass
class Mutation:
    """One text-anchored mutation and the tests that are supposed to notice it.

    ``old`` must occur EXACTLY ONCE in ``path`` -- that is what makes the
    mutation a statement about one enforcement site rather than about whatever
    happens to be on a line number today.

    Example (the shape the next commit's first real entry takes)::

        Mutation(
            name="contract-3-default-mode",
            path="tools/workspace/worker_runtime.py",
            old="DEFAULT_RUNTIME_MODE = MODE_PROCESS",
            new="DEFAULT_RUNTIME_MODE = MODE_THREAD",
            tests=["tests/test_worker_is_a_real_process.py::test_the_default"],
            contract="Contract 3 -- the default runtime mode is the process substrate",
        )

    ``tests`` are pytest node ids, relative to the repository root; they are the
    tests ``docs/CONTRACTS.md`` claims would fail.  If they do not fail, the
    claim is false and the gate says so in one word: SURVIVED.
    """

    name: str
    path: str
    old: str
    new: str
    tests: list[str]
    contract: str = ""

    def __post_init__(self) -> None:
        self.name = str(self.name)
        self.path = str(self.path).replace("\\", "/")
        self.old = str(self.old)
        self.new = str(self.new)
        self.tests = [str(t) for t in self.tests]
        self.contract = str(self.contract or "")


class SpecsError(Exception):
    """The registry itself is unusable -- distinct from a mutation being refused."""


@dataclass
class Result:
    """The observed outcome for one mutation."""

    mutation: Mutation
    verdict: str
    detail: str = ""


# ---------------------------------------------------------------------------
# Loading the registry
# ---------------------------------------------------------------------------


def _self_register() -> None:
    """Make ``import mutation_gate`` in a specs file resolve to *this* module.

    ``scripts/mutation_specs.py`` writes ``from mutation_gate import
    Mutation``.  When this file is executed as a script the module object is
    ``__main__``, so without this alias the import would build a *second*
    module object (and a second ``Mutation`` class), and every ``isinstance``
    check below would be politely wrong.
    """

    if __name__ == "__main__":
        sys.modules.setdefault("mutation_gate", sys.modules[__name__])


def _coerce_mutation(obj: object, index: int) -> Mutation:
    """Accept a real :class:`Mutation` or anything with its attributes."""

    if isinstance(obj, Mutation):
        return obj
    attributes = ("name", "path", "old", "new", "tests")
    missing = [a for a in attributes if not hasattr(obj, a)]
    if missing:
        raise SpecsError(
            f"MUTATIONS[{index}] is {type(obj).__name__}, not a Mutation "
            f"(missing {', '.join(missing)})"
        )
    return Mutation(
        name=getattr(obj, "name"),
        path=getattr(obj, "path"),
        old=getattr(obj, "old"),
        new=getattr(obj, "new"),
        tests=getattr(obj, "tests"),
        contract=getattr(obj, "contract", ""),
    )


def load_specs(specs_path: Path) -> list[Mutation]:
    """Import *specs_path* and return its ``MUTATIONS`` as validated objects."""

    _self_register()
    if not specs_path.is_file():
        raise SpecsError(f"registry not found: {specs_path}")
    spec = importlib.util.spec_from_file_location("_mutation_gate_specs", specs_path)
    if spec is None or spec.loader is None:
        raise SpecsError(f"registry is not importable: {specs_path}")
    module = importlib.util.module_from_spec(spec)

    added_path = False
    if str(_HERE) not in sys.path:
        sys.path.insert(0, str(_HERE))
        added_path = True
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001 - reported, never swallowed
        raise SpecsError(f"importing {specs_path} raised {type(exc).__name__}: {exc}") from exc
    finally:
        sys.modules.pop(spec.name, None)
        if added_path and str(_HERE) in sys.path:
            sys.path.remove(str(_HERE))

    declared = getattr(module, "MUTATIONS", None)
    if declared is None:
        raise SpecsError(f"{specs_path} does not define MUTATIONS")
    if not isinstance(declared, (list, tuple)):
        raise SpecsError(f"MUTATIONS must be a list, got {type(declared).__name__}")

    out: list[Mutation] = []
    seen: set[str] = set()
    for index, entry in enumerate(declared):
        mutation = _coerce_mutation(entry, index)
        if not mutation.name:
            raise SpecsError(f"MUTATIONS[{index}] has an empty name")
        if mutation.name in seen:
            raise SpecsError(f"duplicate mutation name {mutation.name!r}")
        seen.add(mutation.name)
        out.append(mutation)
    return out


# ---------------------------------------------------------------------------
# Restoration: hashed, backed up, hooked
# ---------------------------------------------------------------------------


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def _slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-") or "mutation"


_SESSION_DIR: Path | None = None
_ACTIVE: "_AppliedMutation | None" = None


def session_dir() -> Path:
    """One temp dir per process holding the ``.bak`` copies."""

    global _SESSION_DIR
    if _SESSION_DIR is None:
        _SESSION_DIR = Path(tempfile.mkdtemp(prefix="mutation-gate-"))
    return _SESSION_DIR


class _AppliedMutation:
    """The bytes needed to put one file back, plus the hash that proves it."""

    def __init__(self, mutation: Mutation, target: Path, original: bytes, backup: Path) -> None:
        self.mutation = mutation
        self.target = target
        self.original = original
        self.original_sha = sha256_bytes(original)
        self.backup = backup
        self.restored = False

    def restore(self) -> str:
        """Put the original bytes back and verify by hash.

        Returns an empty string on success, or a description of what is wrong.
        Idempotent: calling it twice is not an error and never re-writes.
        """

        if self.restored:
            return ""
        try:
            self.target.write_bytes(self.original)
        except OSError as exc:
            return f"could not write the original bytes back: {exc}"
        try:
            after = sha256_file(self.target)
        except OSError as exc:
            return f"could not re-read {self.target} after restoring: {exc}"
        if after != self.original_sha:
            return f"sha256 after restore is {after}, expected {self.original_sha}"
        self.restored = True
        try:
            self.backup.unlink()
        except OSError:
            pass
        return ""


def _scream(active: _AppliedMutation, detail: str) -> None:
    """Make a failed restoration impossible to read as an ordinary failure."""

    bar = "!" * 78
    lines = [
        "",
        bar,
        "RESTORE_FAILED -- THE TREE IS NOT CLEAN. This is not a test result.",
        bar,
        f"  mutation : {active.mutation.name}",
        f"  target   : {active.target}",
        f"  backup   : {active.backup}",
        f"  expected : sha256 {active.original_sha}",
        f"  problem  : {detail}",
        "  restore  : cp %s %s" % (active.backup, active.target),
        "  verify   : sha256sum %s   # must print %s" % (active.target, active.original_sha),
        bar,
        "",
    ]
    print("\n".join(lines), file=sys.stderr, flush=True)


def restore_active() -> bool:
    """Restore whatever is currently mutated.  Safe to call repeatedly."""

    global _ACTIVE
    active = _ACTIVE
    if active is None:
        return True
    detail = active.restore()
    if detail:
        _scream(active, detail)
        return False
    _ACTIVE = None
    return True


def install_restore_hooks() -> None:
    """Restore on normal exit, and on SIGINT/SIGTERM/SIGHUP before dying."""

    atexit.register(restore_active)

    def _handler(signum: int, frame: object) -> None:
        restore_active()
        raise SystemExit(128 + signum)

    for name in ("SIGINT", "SIGTERM", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _handler)
        except (OSError, ValueError):
            # Not the main thread, or the platform refuses the signal.
            pass


# ---------------------------------------------------------------------------
# Applying one mutation
# ---------------------------------------------------------------------------


class _Refused(Exception):
    """Pre-flight refusal: nothing was written."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


@dataclass
class _Prepared:
    mutation: Mutation
    target: Path
    original: bytes
    mutated: bytes
    backup: Path


def _prepare(mutation: Mutation, root: Path) -> _Prepared:
    """All the checks that must pass BEFORE the target file is touched."""

    root_resolved = root.resolve()
    target = (root_resolved / mutation.path).resolve()
    if not target.is_relative_to(root_resolved):
        raise _Refused(
            REFUSE_PATH_ESCAPES_ROOT,
            f"{mutation.path} resolves outside {root_resolved}",
        )
    if not target.is_file():
        raise _Refused(REFUSE_FILE_MISSING, f"no such file: {target}")
    try:
        original = target.read_bytes()
    except OSError as exc:
        raise _Refused(REFUSE_FILE_MISSING, f"could not read {target}: {exc}") from exc
    try:
        text = original.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _Refused(REFUSE_NOT_TEXT, f"{mutation.path} is not utf-8 ({exc})") from exc

    count = text.count(mutation.old)
    if count == 0:
        raise _Refused(REFUSE_ANCHOR_ABSENT, f"{mutation.old!r} does not occur in {mutation.path}")
    if count > 1:
        raise _Refused(
            REFUSE_ANCHOR_AMBIGUOUS,
            f"{mutation.old!r} occurs {count} times in {mutation.path}; a text anchor must be unique",
        )
    if mutation.old == mutation.new:
        raise _Refused(REFUSE_NO_OP, "old and new are identical; nothing would change")
    if not mutation.tests:
        raise _Refused(REFUSE_TESTS_NOT_COLLECTABLE, "no test ids declared for this mutation")

    mutated = text.replace(mutation.old, mutation.new, 1).encode("utf-8")
    backup = session_dir() / f"{_slug(mutation.name)}.bak"
    backup.write_bytes(original)
    return _Prepared(mutation, target, original, mutated, backup)


def _child_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _pytest_command(mutation: Mutation) -> list[str]:
    # `-p no:cacheprovider` keeps the run from writing `.pytest_cache` into the
    # tree: this tool writes the target file and nothing else.
    return [sys.executable, "-m", "pytest", *mutation.tests, "-x", "-q", "-p", "no:cacheprovider"]


def _last_line(text: str) -> str:
    for line in reversed((text or "").splitlines()):
        if line.strip():
            return line.strip()[:200]
    return ""


def _baseline(mutation: Mutation, root: Path, timeout: float) -> str:
    """Run the named tests against the UNMUTATED tree before anything is written.

    Returns an empty string when the baseline is green (the only state in which
    a mutation may be applied), otherwise the refusal detail.  ``KILLED`` is a
    delta; the green endpoint has to be measured, not assumed.
    """

    try:
        proc = subprocess.run(
            _pytest_command(mutation),
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_child_env(),
        )
    except subprocess.TimeoutExpired:
        return (
            f"{REFUSE_TIMEOUT}: the baseline run of the named tests did not finish "
            f"within {timeout:g}s; a timed-out baseline is not green"
        )
    except OSError as exc:
        return f"{REFUSE_TESTS_NOT_COLLECTABLE}: could not start pytest for the baseline: {exc}"

    last = _last_line(proc.stdout) or _last_line(proc.stderr)
    if proc.returncode == 0:
        return ""
    if proc.returncode == 1:
        return (
            f"{REFUSE_BASELINE_RED}: pytest exit 1 before the mutation was applied "
            f"({last}); a test which is already red cannot witness a mutation"
        )
    # 5 (no tests ran) in particular: the named tests never executed, so there
    # is no baseline and no evidence in either direction.
    return f"{REFUSE_TESTS_NOT_COLLECTABLE}: baseline pytest exit {proc.returncode} ({last})"


def _apply_and_run(prepared: _Prepared, root: Path, timeout: float) -> tuple[str, str]:
    """Write the mutation, run the named tests, map the exit code to a verdict."""

    mutation = prepared.mutation
    try:
        prepared.target.write_bytes(prepared.mutated)
    except OSError as exc:
        return REFUSED, f"{REFUSE_FILE_MISSING}: could not write {mutation.path}: {exc}"

    try:
        proc = subprocess.run(
            _pytest_command(mutation),
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_child_env(),
        )
    except subprocess.TimeoutExpired:
        return REFUSED, (
            f"{REFUSE_TIMEOUT}: the named tests did not finish within {timeout:g}s; "
            "a timed-out run is not evidence in either direction"
        )
    except OSError as exc:
        return REFUSED, f"{REFUSE_TESTS_NOT_COLLECTABLE}: could not start pytest: {exc}"

    if proc.returncode == 1:
        return KILLED, f"pytest exit 1: {_last_line(proc.stdout)}"
    if proc.returncode == 0:
        return SURVIVED, f"pytest exit 0: the named tests stayed green: {_last_line(proc.stdout)}"
    # 2 = interrupted / collection error, 3 = internal error, 4 = usage error
    # (pytest reports an unknown node id this way), 5 = no tests ran.  Every one
    # of those is "the control did not execute", which is a refusal -- exit 5 in
    # particular is the classic false green: a gate that read it as "passed"
    # would report SURVIVED for a test that never ran.
    return REFUSED, (
        f"{REFUSE_TESTS_NOT_COLLECTABLE}: pytest exit {proc.returncode} "
        f"({_last_line(proc.stdout) or _last_line(proc.stderr)})"
    )


def run_mutation(mutation: Mutation, root: Path, timeout: float) -> Result:
    """Run one mutation end to end.  The file is restored on every path out."""

    global _ACTIVE

    try:
        prepared = _prepare(mutation, root)
    except _Refused as refusal:
        return Result(mutation, REFUSED, f"{refusal.reason}: {refusal.detail}")

    # Always on, and before the target file is touched: a baseline that is red
    # (or did not run at all) would make whatever pytest says next meaningless.
    baseline_problem = _baseline(mutation, root, timeout)
    if baseline_problem:
        return Result(mutation, REFUSED, baseline_problem)

    active = _AppliedMutation(mutation, prepared.target, prepared.original, prepared.backup)
    _ACTIVE = active
    try:
        verdict, detail = _apply_and_run(prepared, root, timeout)
        problem = active.restore()
        if problem:
            return Result(mutation, RESTORE_FAILED, problem)
        _ACTIVE = None
        return Result(mutation, verdict, detail)
    finally:
        # Reached with something in flight -- SIGINT/SIGTERM (our handler raises
        # SystemExit), or an internal bug.  Put the tree back before dying.
        if _ACTIVE is not None:
            restore_active()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mutation_gate.py",
        description="Run the declared mutations; every verdict is KILLED, SURVIVED, "
        "REFUSED or RESTORE_FAILED.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="print the declared mutations and exit without running anything",
    )
    parser.add_argument("--only", metavar="NAME", help="run exactly one declared mutation")
    parser.add_argument(
        "--specs",
        metavar="PATH",
        help=f"registry file (default: <root>/{DEFAULT_SPECS_RELPATH.as_posix()})",
    )
    parser.add_argument(
        "--root",
        metavar="PATH",
        help="repository root (default: the parent of this script's directory)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_S,
        metavar="SECONDS",
        help="per-mutation pytest timeout (default: %(default)s)",
    )
    return parser


def default_root() -> Path:
    return _HERE.parent


def _format_listing(mutation: Mutation) -> str:
    return (
        f"MUTATION {mutation.name} :: {mutation.path} :: "
        f"{mutation.old!r} -> {mutation.new!r} :: tests={len(mutation.tests)} :: "
        f"contract={mutation.contract or '-'}"
    )


def main(argv: Sequence[str] | None = None) -> int:
    _self_register()
    args = build_parser().parse_args(argv)

    root = Path(args.root).resolve() if args.root else default_root()
    specs_path = (
        Path(args.specs).resolve() if args.specs else root / DEFAULT_SPECS_RELPATH
    )

    try:
        declared = load_specs(specs_path)
    except SpecsError as exc:
        print(f"SPECS_UNREADABLE: {exc}", file=sys.stderr)
        print(ZERO_MESSAGE, file=sys.stderr)
        return EXIT_NOTHING_DECLARED

    if args.list:
        for mutation in declared:
            print(_format_listing(mutation))
        if not declared:
            print(ZERO_MESSAGE, file=sys.stderr)
            return EXIT_NOTHING_DECLARED
        print(f"MUTATIONS_LISTED={len(declared)}")
        return EXIT_OK

    if args.only:
        selected = [m for m in declared if m.name == args.only]
        if not selected:
            names = ", ".join(repr(m.name) for m in declared) or "none"
            print(f"MUTATION_NOT_DECLARED: {args.only!r} (declared: {names})", file=sys.stderr)
            print(ZERO_MESSAGE, file=sys.stderr)
            return EXIT_NOTHING_DECLARED
    else:
        selected = list(declared)

    if not selected:
        print(ZERO_MESSAGE, file=sys.stderr)
        return EXIT_NOTHING_DECLARED

    install_restore_hooks()

    results: list[Result] = []
    for mutation in selected:
        print(f"RUN MUTATION {mutation.name}", file=sys.stderr, flush=True)
        result = run_mutation(mutation, root, args.timeout)
        if result.detail:
            print(f"  {result.verdict} {mutation.name}: {result.detail}", file=sys.stderr, flush=True)
        print(f"MUTATION {mutation.name}: {result.verdict}", flush=True)
        results.append(result)
        if result.verdict == RESTORE_FAILED:
            # The tree is dirty; stacking another mutation on top of it would
            # make the first problem unrecoverable.
            print("STOPPING: the tree is not clean, no further mutations were run", file=sys.stderr)
            break

    counts = {verdict: sum(1 for r in results if r.verdict == verdict) for verdict in VERDICTS}
    ran = len(results)
    if sum(counts.values()) != ran:
        print(
            f"GATE_INTERNAL: verdict counts sum to {sum(counts.values())} but {ran} mutations ran",
            file=sys.stderr,
        )
        return EXIT_INTERNAL
    if ran == 0:
        print(ZERO_MESSAGE, file=sys.stderr)
        return EXIT_NOTHING_DECLARED

    if counts[RESTORE_FAILED]:
        exit_code = EXIT_RESTORE_FAILED
    elif counts[SURVIVED] or counts[REFUSED]:
        exit_code = EXIT_GATE_FAILED
    else:
        exit_code = EXIT_OK

    print(
        f"GATE_SUMMARY declared={len(declared)} ran={ran} "
        f"killed={counts[KILLED]} survived={counts[SURVIVED]} "
        f"refused={counts[REFUSED]} restore_failed={counts[RESTORE_FAILED]} "
        f"exit={exit_code}",
        flush=True,
    )
    return exit_code


if __name__ == "__main__":
    # `sys.exit(main())` is deliberately NOT wrapped: SystemExit is a
    # BaseException, and catching it here would print a traceback on every
    # ordinary non-zero exit.  Only real crashes need the extra restore.
    try:
        _exit_code = main()
    except KeyboardInterrupt:  # pragma: no cover - the signal handler normally wins
        restore_active()
        raise
    except Exception:  # pragma: no cover - never lose the restore to a traceback
        restore_active()
        traceback.print_exc()
        raise
    sys.exit(_exit_code)
