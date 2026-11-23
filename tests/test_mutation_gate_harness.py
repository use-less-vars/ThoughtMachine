"""Tests that every verdict of the mutation gate is reachable -- including the vacuous one.

The gate's whole value is that it can say "this control is empty".  A gate whose
own SURVIVED path has never fired is the same claim-not-check it exists to
eliminate, so each case below drives ``scripts/mutation_gate.py`` against a
throwaway tree in ``tmp_path`` and asserts on the *observed* verdict line:

    KILLED    the named test noticed                       (good)
    SURVIVED  the named test stayed green                  (the finding)
    REFUSED   the mutation could not be applied            (never a pass)
    plus the empty-registry refusal and hash-verified restoration.

Nothing here reads or writes a repository file: the fixture module, the fixture
test and the fixture registry are all written into ``tmp_path``, and the harness
is pointed at that directory with ``--root``/``--specs``.
"""

from __future__ import annotations

import hashlib
import pathlib
import re
import subprocess
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
GATE = REPO_ROOT / "scripts" / "mutation_gate.py"

VERDICT_LINE = re.compile(
    r"^MUTATION (?P<name>\S+): (?P<verdict>KILLED|SURVIVED|REFUSED|RESTORE_FAILED)$",
    re.MULTILINE,
)
PYTEST_EXIT = re.compile(r"pytest exit (?P<code>-?\d+)")
HARNESS_TIMEOUT_S = 120

# --- the throwaway tree ----------------------------------------------------
#
# One guard that a test observes, one decorative constant that nothing asserts,
# and a test that pins the guarded behaviour.  "return True" occurs exactly once
# so it is a legal anchor; "return" occurs three times so it is not.

FIXTURE_MODULE = '''"""Throwaway module: one guard, one decorative constant, nothing else."""


def guard_enabled():
    return True


DECORATIVE = "nobody asserts this"


def guarded_answer():
    if guard_enabled():
        return "refused"
    return "allowed"
'''

FIXTURE_TEST = '''from fixture_module import guarded_answer


def test_guard_refuses():
    assert guarded_answer() == "refused"
'''

#: The same test, already red on the pristine tree: it asserts a value neither
#: the unmutated nor the mutated module produces.  Without a baseline this
#: pre-existing failure is indistinguishable from a kill.
FIXTURE_TEST_ALWAYS_RED = '''from fixture_module import guarded_answer


def test_guard_refuses():
    assert guarded_answer() == "never produced"
'''

# --- the declared mutations, one per case ----------------------------------

KILLED_BODY = """\
    Mutation(
        name="fixture-guard-removed",
        path="fixture_module.py",
        old="return True",
        new="return False",
        tests=["test_fixture_guard.py::test_guard_refuses"],
        contract="fixture: the guard refuses",
    ),"""

SURVIVED_BODY = """\
    Mutation(
        name="fixture-decorative-only",
        path="fixture_module.py",
        old='DECORATIVE = "nobody asserts this"',
        new='DECORATIVE = "still nobody asserts this"',
        tests=["test_fixture_guard.py::test_guard_refuses"],
        contract="fixture: nothing asserts this constant",
    ),"""

ANCHOR_ABSENT_BODY = """\
    Mutation(
        name="fixture-anchor-absent",
        path="fixture_module.py",
        old="return False",
        new="return True",
        tests=["test_fixture_guard.py::test_guard_refuses"],
        contract="fixture: anchor that is not in the file",
    ),"""

ANCHOR_AMBIGUOUS_BODY = """\
    Mutation(
        name="fixture-anchor-ambiguous",
        path="fixture_module.py",
        old="return",
        new="pass",
        tests=["test_fixture_guard.py::test_guard_refuses"],
        contract="fixture: anchor that occurs three times",
    ),"""

UNCOLLECTABLE_BODY = """\
    Mutation(
        name="fixture-uncollectable-tests",
        path="fixture_module.py",
        old="return True",
        new="return False",
        tests=["test_fixture_guard.py::test_that_does_not_exist"],
        contract="fixture: a node id that is not there",
    ),"""

#: case name -> (registry body, the verdict that case must produce)
CASES = {
    "killed": (KILLED_BODY, "KILLED"),
    "survived": (SURVIVED_BODY, "SURVIVED"),
    "refused-anchor-absent": (ANCHOR_ABSENT_BODY, "REFUSED"),
    "refused-anchor-ambiguous": (ANCHOR_AMBIGUOUS_BODY, "REFUSED"),
    "refused-tests-uncollectable": (UNCOLLECTABLE_BODY, "REFUSED"),
}


# --- helpers ---------------------------------------------------------------


def _sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_fixture(root: pathlib.Path, test_body: str = FIXTURE_TEST) -> pathlib.Path:
    (root / "fixture_module.py").write_text(FIXTURE_MODULE, encoding="utf-8")
    (root / "test_fixture_guard.py").write_text(test_body, encoding="utf-8")
    return root / "fixture_module.py"


def _write_specs(root: pathlib.Path, body: str) -> pathlib.Path:
    path = root / "specs.py"
    path.write_text(
        "from mutation_gate import Mutation\n\nMUTATIONS = [\n" + body + "\n]\n",
        encoding="utf-8",
    )
    return path


def _run_gate(root: pathlib.Path, specs: pathlib.Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(GATE), "--root", str(root), "--specs", str(specs), *extra],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=HARNESS_TIMEOUT_S,
    )


class Drive:
    """One harness run against a throwaway tree, and what was observed."""

    def __init__(
        self,
        proc: subprocess.CompletedProcess,
        target: pathlib.Path,
        sha_before: str,
        sha_after: str,
    ) -> None:
        self.proc = proc
        self.target = target
        self.sha_before = sha_before
        self.sha_after = sha_after
        self.verdicts = {
            match.group("name"): match.group("verdict")
            for match in VERDICT_LINE.finditer(proc.stdout)
        }

    def sole_verdict(self) -> str:
        values = set(self.verdicts.values())
        assert len(self.verdicts) == 1 and len(values) == 1, (
            f"expected exactly one MUTATION line, saw {self.verdicts!r}\n{self.context()}"
        )
        return values.pop()

    def context(self) -> str:
        return (
            f"exit={self.proc.returncode}\n"
            f"--- stdout ---\n{self.proc.stdout}"
            f"--- stderr ---\n{self.proc.stderr}"
        )


def _drive(root: pathlib.Path, body: str, *extra: str, test_body: str = FIXTURE_TEST) -> Drive:
    target = _write_fixture(root, test_body)
    specs = _write_specs(root, body)
    sha_before = _sha256(target)
    proc = _run_gate(root, specs, *extra)
    return Drive(proc, target, sha_before, _sha256(target))


@pytest.fixture(scope="session")
def case_runs(tmp_path_factory):
    """Run every case once; the assertions below read the observed verdict."""

    runs = {}
    for case_name, (body, _expected) in CASES.items():
        root = tmp_path_factory.mktemp("gate_" + case_name.replace("-", "_"))
        runs[case_name] = _drive(root, body)
    return runs


# --- (a) KILLED ------------------------------------------------------------


def test_killed_when_the_named_test_notices(case_runs):
    run = case_runs["killed"]
    assert run.sole_verdict() == "KILLED", run.context()
    assert run.proc.returncode == 0, run.context()
    assert "SURVIVED" not in run.proc.stdout, run.context()
    assert "killed=1 survived=0 refused=0 restore_failed=0" in run.proc.stdout, run.context()


# --- (b) SURVIVED ----------------------------------------------------------


def test_survived_when_the_named_test_stays_green(case_runs):
    run = case_runs["survived"]
    verdict = run.sole_verdict()
    assert verdict == "SURVIVED", run.context()
    assert verdict != "KILLED", run.context()
    assert run.proc.returncode != 0, "a vacuous control must not exit zero\n" + run.context()
    assert "KILLED" not in run.proc.stdout, run.context()
    # The verdict line alone could be a mutation that never ran; the summary
    # proves it was applied and the named test really did pass.
    assert "declared=1 ran=1 killed=0 survived=1 refused=0 restore_failed=0" in run.proc.stdout, (
        run.context()
    )


# --- (c) REFUSED, anchor absent -------------------------------------------


def test_refused_when_the_anchor_is_absent(case_runs):
    run = case_runs["refused-anchor-absent"]
    assert run.sole_verdict() == "REFUSED", run.context()
    assert run.proc.returncode != 0, run.context()
    assert "anchor-absent" in run.proc.stderr, run.context()
    assert "'return False' does not occur" in run.proc.stderr, run.context()
    assert run.sha_after == run.sha_before, run.context()


# --- (d) REFUSED, anchor ambiguous ----------------------------------------


def test_refused_when_the_anchor_is_ambiguous(case_runs):
    run = case_runs["refused-anchor-ambiguous"]
    assert run.sole_verdict() == "REFUSED", run.context()
    assert run.proc.returncode != 0, run.context()
    assert "anchor-ambiguous" in run.proc.stderr, run.context()
    # It counted rather than guessed: the fixture has three "return" tokens.
    assert "occurs 3 times" in run.proc.stderr, run.context()
    assert run.sha_after == run.sha_before, "nothing may be written for an ambiguous anchor"


# --- (e) REFUSED, the named tests are not collectable ---------------------


def test_refused_when_the_named_tests_are_not_collectable(case_runs):
    run = case_runs["refused-tests-uncollectable"]
    verdict = run.sole_verdict()
    assert verdict == "REFUSED", run.context()
    assert verdict != "SURVIVED", run.context()
    assert run.proc.returncode != 0, run.context()
    assert "tests-not-collectable" in run.proc.stderr, run.context()
    match = PYTEST_EXIT.search(run.proc.stderr)
    assert match is not None, run.context()
    # 0 would be "passed" and 1 would be "the test failed"; everything else is
    # "the control did not execute", which is a refusal.
    assert int(match.group("code")) not in (0, 1), run.context()
    assert run.sha_after == run.sha_before, run.context()


# --- (f) restoration, after every case -----------------------------------


@pytest.mark.parametrize("case_name", sorted(CASES))
def test_restoration_is_byte_identical_after_every_case(case_runs, case_name):
    run = case_runs[case_name]
    assert run.sha_before, "the pre-run hash is empty; this test would prove nothing"
    assert run.sha_after == run.sha_before, (
        f"{case_name} left {run.target.name} modified\n{run.context()}"
    )


def test_the_restoration_sweep_did_not_iterate_zero_times():
    assert len(CASES) > 0, "a parametrised sweep over zero cases is not a pass"


def test_every_verdict_was_reachable(case_runs):
    observed = {run.sole_verdict() for run in case_runs.values()}
    assert len(case_runs) > 0, "a loop over zero cases is not a pass"
    assert len(observed) > 0, "a loop over zero verdicts is not a pass"
    assert observed == {"KILLED", "SURVIVED", "REFUSED"}, observed


# --- (g) zero is not a pass ----------------------------------------------


def test_an_empty_registry_refuses_instead_of_reporting_a_green_sweep(tmp_path):
    target = _write_fixture(tmp_path)
    specs = _write_specs(tmp_path, "")
    sha_before = _sha256(target)

    proc = _run_gate(tmp_path, specs)

    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert "MUTATIONS_DECLARED=0 - a sweep of zero mutations is not a pass" in proc.stderr
    assert not VERDICT_LINE.search(proc.stdout), proc.stdout
    assert "GATE_SUMMARY" not in proc.stdout, "an empty registry must not print a summary"
    assert _sha256(target) == sha_before


def test_only_an_undeclared_name_is_not_a_pass(tmp_path):
    target = _write_fixture(tmp_path)
    specs = _write_specs(tmp_path, KILLED_BODY)
    sha_before = _sha256(target)

    proc = _run_gate(tmp_path, specs, "--only", "no-such-mutation")

    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert "MUTATION_NOT_DECLARED" in proc.stderr
    assert "MUTATIONS_DECLARED=0 - a sweep of zero mutations is not a pass" in proc.stderr
    assert _sha256(target) == sha_before


# --- (h) a red baseline refuses: an unearned KILLED is the worst verdict ---


def test_a_red_baseline_is_refused_rather_than_scored_as_a_kill(tmp_path):
    target = _write_fixture(tmp_path, FIXTURE_TEST_ALWAYS_RED)
    specs = _write_specs(tmp_path, KILLED_BODY)
    sha_before = _sha256(target)

    proc = _run_gate(tmp_path, specs)
    run = Drive(proc, target, sha_before, _sha256(target))

    verdict = run.sole_verdict()
    assert verdict == "REFUSED", run.context()
    assert verdict != "KILLED", (
        "a test that was already failing must never be scored as a kill\n" + run.context()
    )
    assert "baseline-not-green" in run.proc.stderr, run.context()
    assert "already red cannot witness a mutation" in run.proc.stderr, run.context()
    assert "killed=0" in run.proc.stdout, run.context()
    assert run.proc.returncode != 0, run.context()
    assert run.sha_after == run.sha_before, run.context()


# --- (i) a red baseline writes nothing at all ------------------------------


def test_a_red_baseline_never_applies_the_mutation(tmp_path):
    target = _write_fixture(tmp_path, FIXTURE_TEST_ALWAYS_RED)
    specs = _write_specs(tmp_path, KILLED_BODY)
    sha_before = _sha256(target)

    proc = _run_gate(tmp_path, specs)
    run = Drive(proc, target, sha_before, _sha256(target))

    assert run.sole_verdict() == "REFUSED", run.context()
    assert run.sha_after == run.sha_before, run.context()
    assert target.read_text(encoding="utf-8") == FIXTURE_MODULE, (
        "the mutation was applied even though the baseline was red\n" + run.context()
    )


# --- (j) a green baseline does not turn a real kill into a refusal ----------


def test_a_green_baseline_still_lets_a_real_kill_through(case_runs):
    assert len(case_runs) > 0, "a loop over zero cases is not a pass"
    run = case_runs["killed"]
    assert run.sole_verdict() == "KILLED", run.context()
    assert "baseline-not-green" not in run.proc.stderr, (
        "the baseline turned a real kill into a refusal\n" + run.context()
    )
    assert run.proc.returncode == 0, run.context()
    assert "killed=1 survived=0 refused=0" in run.proc.stdout, run.context()


# --- (k) a baseline that cannot run is a refusal, not a kill ---------------


def test_an_uncollectable_baseline_is_refused_not_killed(tmp_path):
    # The fixture test is fine; the declared node id is what does not exist.
    target = _write_fixture(tmp_path)
    specs = _write_specs(tmp_path, UNCOLLECTABLE_BODY)
    sha_before = _sha256(target)

    proc = _run_gate(tmp_path, specs)
    run = Drive(proc, target, sha_before, _sha256(target))

    verdict = run.sole_verdict()
    assert verdict == "REFUSED", run.context()
    assert verdict != "KILLED", run.context()
    assert "tests-not-collectable" in run.proc.stderr, run.context()
    assert "baseline" in run.proc.stderr, run.context()
    match = PYTEST_EXIT.search(run.proc.stderr)
    assert match is not None, run.context()
    assert int(match.group("code")) not in (0, 1), run.context()
    assert run.sha_after == run.sha_before, run.context()


# --- (l) RESTORE_FAILED, made reachable without breaking the filesystem ----

RESTORE_FAILURE_RUNNER = '''\
"""Child process: run the gate with a restore that always reports a problem."""

import importlib.util
import sys

_spec = importlib.util.spec_from_file_location("mutation_gate_restore_probe", {gate!r})
gate = importlib.util.module_from_spec(_spec)
# dataclasses resolve annotations through sys.modules, so register before exec.
sys.modules["mutation_gate_restore_probe"] = gate
_spec.loader.exec_module(gate)


def _broken_restore(self):
    return "sha256 after restore is deadbeef, expected " + self.original_sha


gate._AppliedMutation.restore = _broken_restore
sys.exit(gate.main(["--root", {root!r}, "--specs", {specs!r}]))
'''


def test_restore_failure_is_loud_and_exits_two(tmp_path):
    target = _write_fixture(tmp_path)
    specs = _write_specs(tmp_path, KILLED_BODY)
    sha_before = _sha256(target)
    runner = tmp_path / "run_with_broken_restore.py"
    runner.write_text(
        RESTORE_FAILURE_RUNNER.format(gate=str(GATE), root=str(tmp_path), specs=str(specs)),
        encoding="utf-8",
    )

    proc = subprocess.run(
        [sys.executable, str(runner)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=HARNESS_TIMEOUT_S,
    )

    verdicts = {
        match.group("name"): match.group("verdict") for match in VERDICT_LINE.finditer(proc.stdout)
    }
    assert verdicts.get("fixture-guard-removed") == "RESTORE_FAILED", proc.stdout + proc.stderr
    assert proc.returncode == 2, (
        "a restore failure must exit 2, not 1\n" + proc.stdout + proc.stderr
    )
    assert "RESTORE_FAILED -- THE TREE IS NOT CLEAN" in proc.stderr, proc.stderr
    # The recovery route has to be in the banner: backup path, hash, commands.
    assert "restore  : cp " in proc.stderr, proc.stderr
    assert "sha256sum" in proc.stderr, proc.stderr
    assert sha_before in proc.stderr, proc.stderr
    assert "exit=2" in proc.stdout, proc.stdout
    # And the banner is not crying wolf: the tree really is dirty, so this test
    # would fail if the restore had quietly worked after all.
    assert target.read_text(encoding="utf-8") == FIXTURE_MODULE.replace(
        "return True", "return False", 1
    ), "the fixture was restored, so RESTORE_FAILED was not exercised\n" + proc.stdout


# --- the CLI -------------------------------------------------------------


def test_only_a_declared_name_runs_exactly_that_one(tmp_path):
    target = _write_fixture(tmp_path)
    specs = _write_specs(tmp_path, KILLED_BODY + "\n" + SURVIVED_BODY)
    sha_before = _sha256(target)

    proc = _run_gate(tmp_path, specs, "--only", "fixture-decorative-only")

    verdicts = {
        match.group("name"): match.group("verdict")
        for match in VERDICT_LINE.finditer(proc.stdout)
    }
    assert verdicts == {"fixture-decorative-only": "SURVIVED"}, proc.stdout + proc.stderr
    assert "declared=2 ran=1" in proc.stdout, proc.stdout
    assert proc.returncode != 0, "SURVIVED is never a green exit"
    assert _sha256(target) == sha_before


def test_list_prints_the_registry_without_running_anything(tmp_path):
    target = _write_fixture(tmp_path)
    specs = _write_specs(tmp_path, KILLED_BODY)
    sha_before = _sha256(target)

    proc = _run_gate(tmp_path, specs, "--list")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "MUTATION fixture-guard-removed ::" in proc.stdout, proc.stdout
    assert "MUTATIONS_LISTED=1" in proc.stdout, proc.stdout
    assert not VERDICT_LINE.search(proc.stdout), "list must not run anything"
    assert _sha256(target) == sha_before


def test_the_registry_in_this_tree_loads():
    """The real registry is importable; zero declared is a refusal, not a green."""

    proc = subprocess.run(
        [sys.executable, str(GATE), "--list"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=HARNESS_TIMEOUT_S,
    )
    assert proc.returncode in (0, 3), proc.stdout + proc.stderr
    if proc.returncode == 0:
        assert re.search(r"^MUTATIONS_LISTED=\d+$", proc.stdout, re.MULTILINE), proc.stdout
    else:
        assert "MUTATIONS_DECLARED=0" in proc.stderr, proc.stderr
