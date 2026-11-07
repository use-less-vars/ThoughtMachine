"""
SandboxedExecution contract tests.

Verifies the hardened subprocess runner:
1. Rejects shell metacharacters in arguments (no shell injection).
2. Denies commands whose required permission category is not satisfied.
3. Strips the ambient environment (HOME/PATH) by default.
4. Allows safe commands through with a clean return code.
5. Enforces the caller-supplied timeout.
6. Passes through extra environment variables.

Each test builds the runner with a fully-populated 6-key session permission
dict and no logger (audit logging is a separate concern).
"""

import os
import subprocess

import pytest

from security.sandboxed_execution import SandboxedExecution

# Fully-populated session permission dict (6 categories).
FULL_PERMISSIONS = {
    "git": "read",
    "container": False,
    "network": "banned",
    "filesystem": "read",
    "system": "read",
    "execution": "banned",
}


@pytest.mark.parametrize("arg", ["a;b", "a|b", "a&&b", "a||b", "$(id)", "`id`"])
def test_rejects_shell_metacharacters(arg):
    """Shell metacharacters in a single argument must raise ValueError."""
    runner = SandboxedExecution(session_permissions=FULL_PERMISSIONS, logger=None)
    with pytest.raises(ValueError):
        runner.run(["echo", arg])


def test_rejects_missing_permission():
    """A git:write requirement with only git:read allowed must be denied."""
    runner = SandboxedExecution(session_permissions=FULL_PERMISSIONS, logger=None)
    with pytest.raises(PermissionError):
        runner.run(["echo", "hi"], required_category="git:write")


def test_strips_environment():
    """Default execution must not leak the ambient HOME or PATH."""
    runner = SandboxedExecution(session_permissions=FULL_PERMISSIONS, logger=None)
    result = runner.run(["env"])
    assert "HOME=/dev/null" in result.stdout
    assert os.environ["PATH"] not in result.stdout


def test_allows_safe_command():
    """A plain safe command must run and produce its output."""
    runner = SandboxedExecution(session_permissions=FULL_PERMISSIONS, logger=None)
    result = runner.run(["echo", "hello"])
    assert result.returncode == 0
    assert result.stdout == "hello\n"


def test_respects_timeout():
    """A command exceeding the timeout must raise TimeoutExpired."""
    runner = SandboxedExecution(session_permissions=FULL_PERMISSIONS, logger=None)
    with pytest.raises(subprocess.TimeoutExpired):
        runner.run(["sleep", "60"], timeout=1)


def test_passes_extra_env():
    """extra_env must be visible to the child process."""
    runner = SandboxedExecution(session_permissions=FULL_PERMISSIONS, logger=None)
    result = runner.run(
        ["sh", "-c", "echo $REG_TEST_VAR"],
        extra_env={"REG_TEST_VAR": "hello"},
    )
    assert "hello" in result.stdout
