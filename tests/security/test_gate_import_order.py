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
