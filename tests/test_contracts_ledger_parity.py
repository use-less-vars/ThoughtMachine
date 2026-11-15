"""Parity between docs/CONTRACTS.md and the contract gate workflow.

The ledger names the test files that prove its invariants. Two things can rot:
a named file can move or disappear, and the gate can stop running a file the
ledger still cites. This test fails on either, naming the offending path.

Dependency-free on purpose (pathlib + re only): no docker, no network, no vault,
so it can run as the last step of the fast gate on a bare ubuntu-latest runner.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LEDGER = REPO_ROOT / "docs" / "CONTRACTS.md"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "contract-gate.yml"

#: A cited test file: backticked, repo-relative, under tests/, ending .py
_CITED_TEST = re.compile(r"`(tests/[A-Za-z0-9_./-]+\.py)`")


def cited_test_paths() -> set:
    """Every backticked test path the ledger cites."""
    return set(_CITED_TEST.findall(LEDGER.read_text(encoding="utf-8")))


def test_the_ledger_and_the_gate_workflow_exist():
    assert LEDGER.is_file(), f"the claim ledger is missing: {LEDGER}"
    assert WORKFLOW.is_file(), f"the contract gate workflow is missing: {WORKFLOW}"


def test_the_ledger_cites_at_least_one_test():
    paths = cited_test_paths()
    assert paths, (
        "docs/CONTRACTS.md cites no backticked test file; the ledger cannot be "
        "verified against anything"
    )


def test_every_cited_test_file_exists():
    missing = sorted(p for p in cited_test_paths() if not (REPO_ROOT / p).is_file())
    assert not missing, (
        "docs/CONTRACTS.md cites test file(s) that do not exist in this tree: "
        + ", ".join(missing)
    )


def test_every_cited_test_runs_in_the_contract_gate():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    absent = sorted(p for p in cited_test_paths() if p not in workflow)
    assert not absent, (
        "docs/CONTRACTS.md cites test file(s) that .github/workflows/"
        "contract-gate.yml does not run: " + ", ".join(absent)
    )
