"""RED (COMMIT 4 / gap ii): cross-gate integration -- one branch-probe failure
must abort the commit in BOTH feature-branch gates.

The COMMIT 3 claim is "raise on timeout, fail-closed gates". The per-gate tests
in ``tests/test_git_write_tool.py`` cover each gate separately and only with
``subprocess.TimeoutExpired`` / ``TimeoutError``. This test drives the SAME single
source of failure through BOTH gates at once, for the whole set of exception
types the real ``_run_git`` can surface from the branch probe, and asserts the
commit is never permitted.

A probe failure that maps to a *raised* exception (``TimeoutExpired`` /
``TimeoutError``) is fail-closed. A failure that ``_run_git`` converts to an
error *string* (``FileNotFoundError`` -> "Git command not found ...",
``OSError`` -> "Error running git command: ...") is treated by the gate as a
valid branch name, so the commit proceeds -- fail-open. Those rows are the RED.
"""

from __future__ import annotations

import subprocess

import pytest

from tools.git_write_tool import GitWriteTool


class _BranchProbe:
    """Raise ``exc`` on the branch probe; succeed on every other git call."""

    def __init__(self, exc):
        self.exc = exc
        self.calls = []

    def __call__(self, repo_root, args, timeout=30, allow_host_fallback=True):
        self.calls.append(list(args))
        if list(args)[:2] == ["rev-parse", "--abbrev-ref"]:
            raise self.exc
        return (0, "COMMIT_OK\n", "")

    @property
    def commit_ran(self):
        return any(list(a)[:1] == ["commit"] for a in self.calls)


# gate name -> (git permission level, operator-managed-worktree?)
_GATES = {
    "wofb": ("write_on_feature_branch", False),
    "unprotected": ("write", True),
}


def _make_tool(monkeypatch, tmp_path, probe, git_perm, worktree):
    tool = GitWriteTool(
        operation="commit",
        message="agent commit",
        file_path=["note.txt"],
        session_permissions={"git": git_perm},
        effective_permissions={"git": git_perm},
    )
    object.__setattr__(tool, "_resolved_workspace_path", str(tmp_path))
    object.__setattr__(tool, "_resolved_workspace_id", "ws")
    monkeypatch.setattr(tool, "_run_git_raw", probe)
    monkeypatch.setattr(tool, "_use_container_mode", lambda: True)
    monkeypatch.setattr(tool, "_is_operator_managed_worktree", lambda repo_root: worktree)
    monkeypatch.setattr(
        tool, "_validated_rel_paths", lambda repo_root, paths: list(paths)
    )
    return tool


def _commit_permitted(tool, repo_root):
    try:
        result = tool._git_commit(repo_root)
    except (subprocess.TimeoutExpired, TimeoutError):
        return False
    return "COMMIT_OK" in result


@pytest.mark.parametrize("gate", ["wofb", "unprotected"])
@pytest.mark.parametrize(
    "exc",
    [
        subprocess.TimeoutExpired(cmd="git", timeout=30),
        TimeoutError("git timed out"),
        FileNotFoundError("git binary missing"),
        OSError("generic host git failure"),
    ],
    ids=["TimeoutExpired", "TimeoutError", "FileNotFoundError", "OSError"],
)
def test_branch_probe_failure_aborts_commit_in_both_gates(
    monkeypatch, tmp_path, gate, exc
):
    git_perm, worktree = _GATES[gate]
    probe = _BranchProbe(exc)
    tool = _make_tool(monkeypatch, tmp_path, probe, git_perm, worktree)

    permitted = _commit_permitted(tool, tmp_path)

    assert permitted is False, (
        f"gate={gate} exc={type(exc).__name__}: commit was permitted despite a "
        f"branch-probe failure (fail-open)"
    )
    assert probe.commit_ran is False, (
        f"gate={gate} exc={type(exc).__name__}: git commit subprocess was spawned "
        f"despite a branch-probe failure"
    )
