"""
Regression tests for the order-dependent circular import between
``security.security_gate`` and ``tools`` (via ``tools.git_info_tool`` ->
``security.sandboxed_execution``).

Before the fix, ``import security.security_gate`` FIRST (then ``import tools``)
failed: security_gate's transitive imports reach ``tools``, whose
``git_info_tool`` imports ``security.sandboxed_execution``, which imported
``_value_satisfies`` from the *partially initialized* ``security_gate``.
``tools/__init__.py`` swallowed the ImportError and silently dropped
``GitReadTool`` / ``GitWriteTool`` from ``TOOL_CLASSES``.

``_value_satisfies`` now lives in the import-free leaf module
``security.gate_helpers``, so both import orders must work identically.

Each test runs a FRESH interpreter (``sys.executable -c``) so there is no
in-process ``sys.modules`` pollution between cases.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_script(script: str) -> subprocess.CompletedProcess:
    """Run ``script`` in a clean interpreter with the repo root on sys.path."""
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _assert_clean(result: subprocess.CompletedProcess) -> None:
    assert result.returncode == 0, (
        f"subprocess failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "ImportError" not in result.stderr, (
        f"ImportError in stderr:\n{result.stderr}"
    )
    assert "Failed to import GitReadTool" not in result.stderr
    assert "Failed to import GitWriteTool" not in result.stderr


def test_import_security_gate_then_tools_registers_git_tools():
    """ORDER-A: security_gate first used to drop GitReadTool/GitWriteTool."""
    script = (
        "import security.security_gate\n"
        "import tools\n"
        "names = [getattr(c, 'name', '') for c in tools.TOOL_CLASSES]\n"
        "print('git_read=' + ('present' if 'git_read' in names else 'MISSING'))\n"
        "print('git_write=' + ('present' if 'git_write' in names else 'MISSING'))\n"
    )
    result = _run_script(script)
    _assert_clean(result)
    assert "git_read=present" in result.stdout
    assert "git_write=present" in result.stdout


def test_import_tools_then_security_gate_registers_git_tools():
    """ORDER-B (reverse order) must keep working."""
    script = (
        "import tools\n"
        "import security.security_gate\n"
        "names = [getattr(c, 'name', '') for c in tools.TOOL_CLASSES]\n"
        "print('git_read=' + ('present' if 'git_read' in names else 'MISSING'))\n"
        "print('git_write=' + ('present' if 'git_write' in names else 'MISSING'))\n"
    )
    result = _run_script(script)
    _assert_clean(result)
    assert "git_read=present" in result.stdout
    assert "git_write=present" in result.stdout


def test_sandboxed_execution_importable_after_security_gate():
    """The exact failing edge: sandboxed_execution imports _value_satisfies at
    module level; it must now resolve from security.gate_helpers."""
    script = (
        "import security.security_gate\n"
        "from security.sandboxed_execution import SandboxedExecution\n"
        "print('sandboxed_execution=OK')\n"
    )
    result = _run_script(script)
    _assert_clean(result)
    assert "sandboxed_execution=OK" in result.stdout


def test_value_satisfies_truth_table_pinned():
    """Pin the COMPLETE ``_value_satisfies`` truth table across the known
    grant levels plus the boolean allowed side.

    ``security.gate_helpers._ASK_SILENT_MAX_RANK`` (the ``'ask'``-boundary
    constant) is derived from ``GRANT_LEVEL_RANKS`` instead of the former magic
    literal ``2``.  This matrix proves that refactor is a behavioural NO-OP:
    every cell matches the pre-refactor table exactly.  Rows are the ``allowed``
    side, columns the ``required`` side (order below).
    """
    from security.gate_helpers import _ASK_SILENT_MAX_RANK, _value_satisfies
    from security.resource_catalog import GRANT_LEVEL_RANKS

    # The boundary constant is the max rank over the {banned, read, ask} set.
    assert _ASK_SILENT_MAX_RANK == max(
        GRANT_LEVEL_RANKS["banned"],
        GRANT_LEVEL_RANKS["read"],
        GRANT_LEVEL_RANKS["ask"],
    )
    assert _ASK_SILENT_MAX_RANK == 2

    required_cols = (
        "banned",
        "read",
        "ask",
        "write",
        "write_on_feature_branch",
        "outbound",
        "full",
        "connect",
    )
    ASK = "ASK"
    expected: dict = {
        # allowed -> {required: expected result}
        "banned": {c: False for c in required_cols},
        "read": {c: False for c in required_cols},
        "ask": {c: False for c in required_cols},
        "write": {c: False for c in required_cols},
        "write_on_feature_branch": {c: False for c in required_cols},
        "outbound": {c: False for c in required_cols},
        "full": {c: False for c in required_cols},
        "connect": {c: False for c in required_cols},
    }
    for allowed in expected:
        expected[allowed]["banned"] = True  # every grant satisfies 'banned'
    # read tier: banned < read.
    for allowed in ("read", "ask", "write", "write_on_feature_branch",
                    "outbound", "full", "connect"):
        expected[allowed]["read"] = True
    # ask tier.
    for allowed in ("ask", "write", "write_on_feature_branch", "outbound",
                    "full", "connect"):
        expected[allowed]["ask"] = True
    # write tier: write == write_on_feature_branch == connect (rank 3).
    for allowed in ("write", "write_on_feature_branch", "outbound", "full",
                    "connect"):
        expected[allowed]["write"] = True
        expected[allowed]["write_on_feature_branch"] = True
        expected[allowed]["connect"] = True
    # outbound (3.5): satisfied by outbound/full only above write tier.
    for allowed in ("outbound", "full"):
        expected[allowed]["outbound"] = True
    # full (4): satisfied by 'full' only.
    expected["full"]["full"] = True
    # The 'ask' allowed side returns the ASK sentinel (not True) for every
    # requirement strictly above the silent boundary.
    expected["ask"]["write"] = ASK
    expected["ask"]["write_on_feature_branch"] = ASK
    expected["ask"]["outbound"] = ASK
    expected["ask"]["full"] = ASK
    expected["ask"]["connect"] = ASK

    for allowed, row in expected.items():
        for required in required_cols:
            got = _value_satisfies(required, allowed)
            assert got == row[required], (
                f"_value_satisfies({required!r}, {allowed!r}) == {got!r}, "
                f"expected {row[required]!r}"
            )

    # Boolean allowed side: True satisfies everything, False satisfies nothing.
    for required in required_cols:
        assert _value_satisfies(required, True) is True
        assert _value_satisfies(required, False) is False
