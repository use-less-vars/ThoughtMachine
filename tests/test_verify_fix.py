#!/usr/bin/env python3
"""Tests for ``scripts/verify_fix.py`` -- the red-before gate (defect #22).

The gate exists to answer one question: *does this selectors discriminate the
fix?*  Everything that decides it (``parse_counts``, ``classify_run``,
``decide``) is a pure function, so the bulk of this file exercises the
decision table directly with hand-built outcomes -- no git, no subprocesses,
no temporary repositories.  Only the last test drives the real CLI against a
real throwaway git repository.

Usage
-----
    python -m pytest tests/test_verify_fix.py -q

The end-to-end test is skipped when ``git`` is not on ``PATH`` (this
sandbox/container has no git); it runs in CI, where git exists.  Everything
else runs everywhere.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "verify_fix.py"


def _load_module():
    """Import ``scripts/verify_fix.py`` as a module object (alias ``vf``)."""
    spec = importlib.util.spec_from_file_location("verify_fix_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


vf = _load_module()


# ────────────────────────────────────────────────────────────────────────────
# Helpers: build the two outcomes the decision table reasons about
# ────────────────────────────────────────────────────────────────────────────
def _outcome(kind, *, passed=0, failed=0, errors=0, skipped=0,
             returncode=0, detail=""):
    """Build a ``RunOutcome`` of *kind* with the given counters."""
    counts = vf.Counts(passed=passed, failed=failed, errors=errors,
                       skipped=skipped)
    return vf.RunOutcome(kind=kind, returncode=returncode, counts=counts,
                         duration_s=0.1, detail=detail)


def _red():
    """The base outcome we want: the selector failed there."""
    return _outcome(vf.OUTCOME_FAILED, failed=1, returncode=1,
                    detail="1 failed")


def _green():
    """The head outcome we want: the selector passes now."""
    return _outcome(vf.OUTCOME_PASSED, passed=1, returncode=0,
                    detail="1 passed")


# ────────────────────────────────────────────────────────────────────────────
# The happy path
# ────────────────────────────────────────────────────────────────────────────
def test_red_at_base_green_now_is_pass():
    decision = vf.decide(_red(), _green())
    assert decision.verdict == vf.PASS
    assert decision.exit_code == 0
    assert decision.ok is True
    assert "red_at_base" in decision.reasons


def test_pass_is_the_only_ok_verdict():
    assert vf.Decision(vf.PASS).ok is True
    for verdict in (vf.FAIL, vf.BLOCKED, vf.TIMEOUT):
        assert vf.Decision(verdict).ok is False


def test_exit_codes():
    assert vf.EXIT_CODES == {vf.PASS: 0, vf.FAIL: 1, vf.BLOCKED: 2, vf.TIMEOUT: 3}


def test_only_pass_exits_zero():
    for verdict, code in vf.EXIT_CODES.items():
        assert (code == 0) is (verdict == vf.PASS)


# ────────────────────────────────────────────────────────────────────────────
# Non-discriminating / non-green runs are FAIL, never PASS
# ────────────────────────────────────────────────────────────────────────────
def test_green_at_base_is_fail_base_not_red():
    decision = vf.decide(_green(), _green())
    assert decision.verdict == vf.FAIL
    assert decision.reasons == ("base_not_red",)


def test_red_now_is_fail_head_not_green():
    decision = vf.decide(_red(), _red())
    assert decision.verdict == vf.FAIL
    assert decision.reasons == ("head_not_green",)


def test_zero_passed_now_is_fail():
    head = _outcome(vf.OUTCOME_PASSED, passed=0, returncode=0)
    decision = vf.decide(_red(), head)
    assert decision.verdict == vf.FAIL
    assert decision.reasons == ("zero_passed",)


def test_head_no_tests_collected_is_fail():
    head = _outcome(vf.OUTCOME_NO_TESTS, returncode=5)
    decision = vf.decide(_red(), head)
    assert decision.verdict == vf.FAIL
    assert decision.reasons == ("head_no_tests_collected",)


def test_base_no_tests_collected_is_fail():
    base = _outcome(vf.OUTCOME_NO_TESTS, returncode=5)
    decision = vf.decide(base, _green())
    assert decision.verdict == vf.FAIL
    assert decision.reasons == ("base_no_tests_collected",)


def test_both_sides_empty_is_still_fail():
    empty = _outcome(vf.OUTCOME_NO_TESTS, returncode=5)
    assert vf.decide(empty, empty).verdict == vf.FAIL


def test_head_disagreement_is_fail():
    head = _outcome(vf.OUTCOME_DISAGREEMENT, passed=5, returncode=2)
    decision = vf.decide(_red(), head)
    assert decision.verdict == vf.FAIL
    assert decision.reasons == ("head_exit_code_verdict_disagreement",)


def test_base_disagreement_is_fail():
    base = _outcome(vf.OUTCOME_DISAGREEMENT, passed=5, returncode=2)
    decision = vf.decide(base, _green())
    assert decision.verdict == vf.FAIL
    assert decision.reasons == ("base_exit_code_verdict_disagreement",)


def test_head_run_error_is_fail():
    head = _outcome(vf.OUTCOME_ERROR, returncode=None,
                    detail="could not execute pytest")
    decision = vf.decide(_red(), head)
    assert decision.verdict == vf.FAIL
    assert decision.reasons == ("head_run_error",)


def test_base_run_error_is_fail():
    base = _outcome(vf.OUTCOME_ERROR, returncode=None)
    assert vf.decide(base, _green()).reasons == ("base_run_error",)


def test_unknown_outcomes_are_fail():
    head = _outcome("something_else", passed=1)
    assert vf.decide(_red(), head).verdict == vf.FAIL
    base = _outcome("something_else", failed=1)
    assert vf.decide(base, _green()).verdict == vf.FAIL


# ────────────────────────────────────────────────────────────────────────────
# A test file ADDED by the fix is RED by construction
# ────────────────────────────────────────────────────────────────────────────
def test_absent_at_base_plus_green_is_pass():
    base = vf.RunOutcome(vf.OUTCOME_ABSENT_AT_BASE, None, vf.Counts(), 0.0,
                         "tests/test_new.py does not exist at abc123")
    decision = vf.decide(base, _green())
    assert decision.verdict == vf.PASS
    assert "new_test_absent_at_base" in decision.reasons


def test_absent_at_base_plus_red_is_fail():
    base = vf.RunOutcome(vf.OUTCOME_ABSENT_AT_BASE, None, vf.Counts(), 0.0, "")
    decision = vf.decide(base, _red())
    assert decision.verdict == vf.FAIL
    assert decision.reasons == ("head_not_green",)


def test_absent_at_base_plus_empty_is_fail():
    base = vf.RunOutcome(vf.OUTCOME_ABSENT_AT_BASE, None, vf.Counts(), 0.0, "")
    head = _outcome(vf.OUTCOME_NO_TESTS, returncode=5)
    assert vf.decide(base, head).verdict == vf.FAIL


# ────────────────────────────────────────────────────────────────────────────
# BLOCKED and TIMEOUT outrank everything
# ────────────────────────────────────────────────────────────────────────────
def test_timeout_at_base_is_timeout():
    base = _outcome(vf.OUTCOME_TIMEOUT, returncode=None, detail="600s")
    decision = vf.decide(base, _green())
    assert decision.verdict == vf.TIMEOUT
    assert decision.exit_code == 3
    assert decision.reasons == ("base_timeout",)


def test_timeout_now_is_timeout():
    head = _outcome(vf.OUTCOME_TIMEOUT, returncode=None)
    assert vf.decide(_red(), head).reasons == ("head_timeout",)


def test_timeout_outranks_a_green_base_and_failing_head():
    base = _outcome(vf.OUTCOME_TIMEOUT)
    head = _red()
    assert vf.decide(base, head).verdict == vf.TIMEOUT


def test_leftover_worktree_is_blocked():
    cleanup = vf.Cleanup(leftover_path="/tmp/verify-fix-abc/worktree",
                         detail="remove failed; pruned")
    decision = vf.decide(_red(), _green(), cleanup=cleanup)
    assert decision.verdict == vf.BLOCKED
    assert decision.exit_code == 2
    assert decision.reasons == ("worktree_leftover",)
    assert "/tmp/verify-fix-abc/worktree" in decision.detail


def test_clean_cleanup_does_not_block():
    cleanup = vf.Cleanup(leftover_path="", detail="pruned")
    assert vf.decide(_red(), _green(), cleanup=cleanup).verdict == vf.PASS


def test_blocked_short_circuits_every_other_signal():
    decision = vf.decide(_red(), _green(), blocked="git-unavailable")
    assert decision.verdict == vf.BLOCKED
    assert decision.reasons == ("git-unavailable",)


def test_missing_outcome_is_blocked():
    assert vf.decide(None, _green()).verdict == vf.BLOCKED
    assert vf.decide(_red(), None).verdict == vf.BLOCKED
    assert vf.decide(None, None).reasons == ("gate_incomplete",)


# ────────────────────────────────────────────────────────────────────────────
# parse_counts / _summary_line
# ────────────────────────────────────────────────────────────────────────────
def test_parse_counts_plain():
    counts = vf.parse_counts("1 failed in 0.12s")
    assert counts == vf.Counts(failed=1)
    assert counts.collected == 1


def test_parse_counts_mixed():
    counts = vf.parse_counts("1 failed, 2 passed, 1 error, 3 skipped in 0.45s")
    assert counts.passed == 2
    assert counts.failed == 1
    assert counts.errors == 1
    assert counts.skipped == 3
    assert counts.collected == 7


def test_parse_counts_ignores_the_subtests_rollup():
    counts = vf.parse_counts("46 passed, 10 subtests passed in 1.20s")
    assert counts is not None
    assert counts.passed == 46


def test_parse_counts_no_tests_ran():
    counts = vf.parse_counts("no tests ran in 0.01s")
    assert counts is not None
    assert counts.collected == 0


def test_parse_counts_warnings_only_is_none():
    assert vf.parse_counts("3 warnings in 0.2s") is None


def test_parse_counts_garbage_is_none():
    assert vf.parse_counts("boom: segmentation fault") is None
    assert vf.parse_counts("") is None


def test_parse_counts_ignores_per_test_progress_lines():
    output = "tests/test_x.py .F\n2 passed, 1 failed in 0.30s"
    counts = vf.parse_counts(output)
    assert counts.passed == 2
    assert counts.failed == 1


def test_counts_as_dict_includes_collected():
    payload = vf.Counts(passed=2, failed=1).as_dict()
    assert payload["collected"] == 3
    assert set(payload) == {"passed", "failed", "errors", "skipped",
                            "xfailed", "xpassed", "collected"}


# ────────────────────────────────────────────────────────────────────────────
# classify_run -- the exit-code / verdict disagreement pathology
# ────────────────────────────────────────────────────────────────────────────
def test_classify_clean_pass():
    outcome = vf.classify_run(0, "1 passed in 0.10s")
    assert outcome.kind == vf.OUTCOME_PASSED
    assert outcome.counts.passed == 1


def test_classify_real_failure():
    outcome = vf.classify_run(1, "1 failed in 0.10s")
    assert outcome.kind == vf.OUTCOME_FAILED


def test_classify_exit_zero_with_an_error_is_disagreement():
    outcome = vf.classify_run(0, "1 error in 0.10s")
    assert outcome.kind == vf.OUTCOME_DISAGREEMENT
    assert "exit code 0" in outcome.detail


def test_classify_nonzero_exit_with_passes_is_disagreement():
    outcome = vf.classify_run(2, "5 passed in 0.50s")
    assert outcome.kind == vf.OUTCOME_DISAGREEMENT
    assert "exit code 2" in outcome.detail


def test_classify_no_tests_rc5():
    assert vf.classify_run(5, "no tests ran in 0.01s").kind == vf.OUTCOME_NO_TESTS


def test_classify_no_tests_rc0():
    assert vf.classify_run(0, "no tests ran in 0.01s").kind == vf.OUTCOME_NO_TESTS


def test_classify_all_skipped_is_no_tests():
    outcome = vf.classify_run(0, "2 skipped in 0.10s")
    assert outcome.kind == vf.OUTCOME_NO_TESTS
    assert outcome.counts.skipped == 2


def test_classify_unparseable_output_is_no_tests():
    outcome = vf.classify_run(0, "Fatal Python error")
    assert outcome.kind == vf.OUTCOME_NO_TESTS
    assert "no parseable summary" in outcome.detail


# ────────────────────────────────────────────────────────────────────────────
# parse_selection
# ────────────────────────────────────────────────────────────────────────────
def test_parse_selection_splits_node_id(tmp_path):
    selection = vf.parse_selection("tests/test_x.py::test_y", tmp_path)
    assert selection.file_part == "tests/test_x.py"
    assert selection.node_part == "test_y"
    assert selection.relative == "tests/test_x.py::test_y"


def test_parse_selection_strips_dot_slash(tmp_path):
    selection = vf.parse_selection("./tests/test_x.py::TestK::test_y", tmp_path)
    assert selection.relative == "tests/test_x.py::TestK::test_y"
    assert selection.file_part == "tests/test_x.py"
    assert selection.node_part == "TestK::test_y"


def test_parse_selection_relativises_absolute_paths_inside_the_repo(tmp_path):
    absolute = str(tmp_path / "tests" / "test_x.py") + "::test_y"
    selection = vf.parse_selection(absolute, tmp_path)
    assert selection.relative == "tests/test_x.py::test_y"


def test_parse_selection_rejects_paths_outside_the_repo(tmp_path):
    with pytest.raises(ValueError):
        vf.parse_selection("/etc/passwd::test_y", tmp_path)


def test_parse_selection_rejects_empty(tmp_path):
    with pytest.raises(ValueError):
        vf.parse_selection("", tmp_path)
    with pytest.raises(ValueError):
        vf.parse_selection("   ", tmp_path)


def test_parse_selection_rejects_missing_file_part(tmp_path):
    with pytest.raises(ValueError):
        vf.parse_selection("::test_y", tmp_path)


# ────────────────────────────────────────────────────────────────────────────
# CLI surface
# ────────────────────────────────────────────────────────────────────────────
def _run_cli(*args, cwd=None):
    """Run the gate as a subprocess and return the CompletedProcess."""
    return subprocess.run([sys.executable, str(SCRIPT), *args],
                          capture_output=True, text=True, timeout=600,
                          cwd=str(cwd) if cwd else None)


def test_help_exits_zero_and_documents_the_time_box():
    proc = _run_cli("--help")
    assert proc.returncode == 0
    assert "--timeout-seconds" in proc.stdout
    assert "--test" in proc.stdout
    assert "--base" in proc.stdout


def test_version_flag():
    proc = _run_cli("--version")
    assert proc.returncode == 0
    assert vf.__version__ in proc.stdout


def test_test_selector_is_required():
    proc = _run_cli("--base", "HEAD~1")
    assert proc.returncode == 2
    assert "--test" in proc.stderr


def test_base_is_required():
    proc = _run_cli("--test", "tests/test_x.py::test_y")
    assert proc.returncode == 2
    assert "--base" in proc.stderr


@pytest.mark.skipif(shutil.which("git") is None,
                    reason="git binary required; runs in CI, not in this sandbox")
def test_gate_end_to_end_red_then_green(tmp_path):
    """A buggy commit plus an uncommitted fix must produce PASS/exit 0."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "calc.py").write_text(
        "def add(a, b):\n    return a - b\n", encoding="utf-8")
    (repo / "test_calc.py").write_text(
        "import calc\n\n\ndef test_add():\n    assert calc.add(2, 3) == 5\n",
        encoding="utf-8")

    def git(*args):
        return subprocess.run(["git", *args], cwd=str(repo), check=True,
                              capture_output=True, text=True)

    git("init", "-q")
    git("config", "user.email", "gate@example.com")
    git("config", "user.name", "Gate Test")
    git("add", "calc.py", "test_calc.py")
    git("commit", "-q", "-m", "buggy add()")

    # The fix is left UNCOMMITTED in the working tree: that is the "current tree".
    (repo / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8")

    proc = _run_cli("--test", "test_calc.py::test_add", "--base", "HEAD",
                    cwd=repo)
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout)
    assert report["verdict"] == "PASS"
    assert report["exit_code"] == 0
    assert report["base_outcome"] == "failed"
    assert report["head_outcome"] == "passed"
    assert report["counts"]["base"]["failed"] == 1
    assert report["counts"]["head"]["passed"] == 1
    assert report["cleanup"]["removed"] is True
    assert not Path(report["worktree_path"]).exists()
