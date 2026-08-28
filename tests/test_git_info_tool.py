"""Tests for the feature-branch agent commit policy in GitInfoTool.

Policy (operator-managed worktrees, i.e. a ``.git`` FILE pointing at a
gitdir): agent commits are blocked by default. They are allowed only when
ALL of the following hold:

1. ``agent_config['git_allow_worktree_commits']`` is exactly True.
2. The current branch is ``feat/*`` or ``fix/*``.
3. Container git execution is active (``_use_container_mode()``).
4. Container execution is mandatory for the branch check AND for the
   add/commit subprocesses themselves (``allow_host_fallback=False``) — the
   host backend injects ``--no-verify`` / ``core.hooksPath=/dev/null`` and
   would bypass the commit QA gate.
5. The commit names explicit ``file_path``(s): there is no full-worktree
   mode -- every commit is limited to the named paths (never ``git add -A``),
   so unvetted changes cannot be swept into the commit past the review gate.

These tests exercise ``GitInfoTool._git_commit`` directly (bypassing
``execute()``) with monkeypatched execution backends, so no git binary and
no Docker daemon are required.
"""

from pathlib import Path

import pytest

from tools.git_info_tool import GitInfoTool

OPERATOR_ERROR = (
    "Error: commits in this workspace are performed host-side by "
    "the operator (workspace is an operator-managed git worktree)"
)


def _tool(**overrides):
    """Build a commit tool with sane defaults for these tests."""
    params = {"operation": "commit", "message": "agent commit"}
    params.update(overrides)
    return GitInfoTool(**params)


class _RecordingManager:
    """Stand-in for the resource container manager returned by
    ``_resolve_resource_execution`` in the containerized path."""

    def __init__(self):
        self.calls = []

    def exec(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        return {"exit_code": 0, "stdout": "ok\n", "stderr": ""}


class _RecordingExec:
    """Records ``_exec_container_raw`` / ``_exec_host_raw`` invocations.

    Branch-aware: ``rev-parse --abbrev-ref HEAD`` answers with ``branch``
    (default ``feat/x\n``) and every other command with ``stdout``, so the
    policy gate's container-mandatory branch resolution sees a real feature
    branch. When the invocation carries a ``manager`` (container path), the
    call is forwarded to it so tests can assert what the resource manager
    received.
    """

    def __init__(self, stdout="ok\n", branch="feat/x\n"):
        self.calls = []  # list of (repo_root, args, kwargs)
        self.stdout = stdout
        self.branch = branch

    def __call__(self, repo_root, args, **kwargs):
        args = list(args)
        self.calls.append((repo_root, args, kwargs))
        manager = kwargs.get("manager")
        if manager is not None:
            manager.exec(
                ["git"] + args,
                workdir=str(repo_root),
                timeout=kwargs.get("timeout", 30),
            )
        if args[:2] == ["rev-parse", "--abbrev-ref"]:
            return (0, self.branch, "")
        return (0, self.stdout, "")


def _branch_fake(recorder, branch):
    """A ``_run_git`` replacement that answers the branch query and records
    every call (used for tests that must NOT reach a real backend)."""

    def fake_run_git(repo_root, args, timeout=30, allow_host_fallback=True):
        recorder.append((list(args), allow_host_fallback))
        if args[:2] == ["rev-parse", "--abbrev-ref"]:
            return branch + "\n"
        return "ok\n"

    return fake_run_git


def _assert_no_commit_subprocess(exec_container, exec_host):
    """Denied paths must never reach either git execution backend."""
    assert exec_container.calls == []
    assert exec_host.calls == []


# ---------------------------------------------------------------------------
# Default (no flag): worktree commits blocked, including on dev branches.
# ---------------------------------------------------------------------------


def test_worktree_commit_blocked_on_dev_by_default(tmp_path, monkeypatch):
    tool = _tool()  # no git_allow_worktree_commits in agent_config
    monkeypatch.setattr(tool, "_is_operator_managed_worktree", lambda r: True)
    monkeypatch.setattr(tool, "_use_container_mode", lambda: True)
    calls = []
    monkeypatch.setattr(tool, "_run_git", _branch_fake(calls, "dev"))
    exec_container = _RecordingExec()
    monkeypatch.setattr(tool, "_exec_container_raw", exec_container)
    monkeypatch.setattr(tool, "_exec_host_raw", _RecordingExec())

    result = tool._git_commit(tmp_path)

    assert OPERATOR_ERROR in result
    # No git subprocess (not even the branch query) may run: the missing flag
    # short-circuits before any execution.
    assert calls == []
    _assert_no_commit_subprocess(exec_container, tool._exec_host_raw)


# ---------------------------------------------------------------------------
# Flag set but branch is not feat/* / fix/*.
# ---------------------------------------------------------------------------


def test_worktree_commit_blocked_on_main_with_flag(tmp_path, monkeypatch):
    tool = _tool(agent_config={"git_allow_worktree_commits": True})
    monkeypatch.setattr(tool, "_is_operator_managed_worktree", lambda r: True)
    monkeypatch.setattr(tool, "_use_container_mode", lambda: True)
    calls = []
    monkeypatch.setattr(tool, "_run_git", _branch_fake(calls, "main"))
    exec_container = _RecordingExec()
    monkeypatch.setattr(tool, "_exec_container_raw", exec_container)
    monkeypatch.setattr(tool, "_exec_host_raw", _RecordingExec())

    result = tool._git_commit(tmp_path)

    assert OPERATOR_ERROR in result
    # Exactly one call: the container-mandatory branch resolution. The
    # branch check must itself be container-mandatory (no host fallback).
    assert len(calls) == 1
    assert calls[0][0] == ["rev-parse", "--abbrev-ref", "HEAD"]
    assert calls[0][1] is False
    _assert_no_commit_subprocess(exec_container, tool._exec_host_raw)


# ---------------------------------------------------------------------------
# Full allow: flag + feat/* branch + container mode -> containerized commit,
# no --no-verify, no host fallback.
# ---------------------------------------------------------------------------


def test_feature_branch_commit_allowed_with_flag_container_mode(
    tmp_path, monkeypatch
):
    (tmp_path / "agent_change.py").write_text("x")
    tool = _tool(
        file_path="agent_change.py",
        agent_config={"git_allow_worktree_commits": True},
    )
    monkeypatch.setattr(tool, "_is_operator_managed_worktree", lambda r: True)
    monkeypatch.setattr(tool, "_use_container_mode", lambda: True)
    manager = _RecordingManager()
    monkeypatch.setattr(
        tool,
        "_resolve_resource_execution",
        lambda: ({"mode": "containerized", "detail": "test"}, manager),
    )
    exec_container = _RecordingExec()
    monkeypatch.setattr(tool, "_exec_container_raw", exec_container)
    exec_host = _RecordingExec()
    monkeypatch.setattr(tool, "_exec_host_raw", exec_host)

    result = tool._git_commit(tmp_path)

    assert "ok\n" in result
    assert OPERATOR_ERROR not in result
    # Branch check + git add <path> + git commit -m: all through the
    # container. The add stages ONLY the named path -- never -A.
    container_args = [args for (_r, args, _kw) in exec_container.calls]
    assert [a[0] for a in container_args] == [
        "rev-parse",
        "add",
        "commit",
    ]
    assert ["rev-parse", "--abbrev-ref", "HEAD"] in container_args
    assert ["add", "--", "agent_change.py"] in container_args
    assert all("-A" not in args for args in container_args)
    assert ["commit", "-m", "agent commit"] in container_args
    # The container path must never inject --no-verify.
    for args in container_args:
        assert "--no-verify" not in args
    # The host backend (which injects --no-verify) must never run.
    assert exec_host.calls == []
    # The container manager received the three executions (argv is
    # ["git", <subcommand>, ...], mirroring the real backend contract).
    assert [cmd[1] for cmd, _kw in manager.calls] == [
        "rev-parse",
        "add",
        "commit",
    ]


# ---------------------------------------------------------------------------
# Flag absent: denied even on a feat/* branch in container mode.
# ---------------------------------------------------------------------------


def test_feature_branch_commit_denied_without_flag(tmp_path, monkeypatch):
    tool = _tool(agent_config={})
    monkeypatch.setattr(tool, "_is_operator_managed_worktree", lambda r: True)
    monkeypatch.setattr(tool, "_use_container_mode", lambda: True)
    calls = []
    monkeypatch.setattr(tool, "_run_git", _branch_fake(calls, "feat/foo"))
    exec_container = _RecordingExec()
    monkeypatch.setattr(tool, "_exec_container_raw", exec_container)
    monkeypatch.setattr(tool, "_exec_host_raw", _RecordingExec())

    result = tool._git_commit(tmp_path)

    assert OPERATOR_ERROR in result
    assert calls == []
    _assert_no_commit_subprocess(exec_container, tool._exec_host_raw)


# ---------------------------------------------------------------------------
# Flag set, container mode, but branch is not a feature branch.
# ---------------------------------------------------------------------------


def test_feature_branch_commit_denied_on_non_feature_branch(
    tmp_path, monkeypatch
):
    tool = _tool(agent_config={"git_allow_worktree_commits": True})
    monkeypatch.setattr(tool, "_is_operator_managed_worktree", lambda r: True)
    monkeypatch.setattr(tool, "_use_container_mode", lambda: True)
    calls = []
    monkeypatch.setattr(tool, "_run_git", _branch_fake(calls, "release/1.0"))
    exec_container = _RecordingExec()
    monkeypatch.setattr(tool, "_exec_container_raw", exec_container)
    monkeypatch.setattr(tool, "_exec_host_raw", _RecordingExec())

    result = tool._git_commit(tmp_path)

    assert OPERATOR_ERROR in result
    assert len(calls) == 1
    assert calls[0][0] == ["rev-parse", "--abbrev-ref", "HEAD"]
    assert calls[0][1] is False
    _assert_no_commit_subprocess(exec_container, tool._exec_host_raw)


# ---------------------------------------------------------------------------
# Host execution mode: denied before any branch query — the host backend
# would bypass the QA gate.
# ---------------------------------------------------------------------------


def test_feature_branch_commit_denied_when_host_mode(tmp_path, monkeypatch):
    tool = _tool(agent_config={"git_allow_worktree_commits": True})
    monkeypatch.setattr(tool, "_is_operator_managed_worktree", lambda r: True)
    monkeypatch.setattr(tool, "_use_container_mode", lambda: False)
    calls = []
    monkeypatch.setattr(tool, "_run_git", _branch_fake(calls, "feat/foo"))
    exec_container = _RecordingExec()
    monkeypatch.setattr(tool, "_exec_container_raw", exec_container)
    monkeypatch.setattr(tool, "_exec_host_raw", _RecordingExec())

    result = tool._git_commit(tmp_path)

    assert OPERATOR_ERROR in result
    # Container mode is a precondition, so the branch is never even queried.
    assert calls == []
    _assert_no_commit_subprocess(exec_container, tool._exec_host_raw)


# ---------------------------------------------------------------------------
# Container unavailable (policy denial / docker outage): the branch
# resolution raises RuntimeError and the gate fails closed.
# ---------------------------------------------------------------------------


def test_feature_branch_commit_denied_when_container_unavailable(
    tmp_path, monkeypatch
):
    tool = _tool(agent_config={"git_allow_worktree_commits": True})
    monkeypatch.setattr(tool, "_is_operator_managed_worktree", lambda r: True)
    monkeypatch.setattr(tool, "_use_container_mode", lambda: True)
    monkeypatch.setattr(
        tool,
        "_resolve_resource_execution",
        lambda: (
            {
                "mode": "unavailable",
                "detail": "docker daemon unreachable",
                "failure_reason": "policy denial",
            },
            None,
        ),
    )
    exec_container = _RecordingExec()
    monkeypatch.setattr(tool, "_exec_container_raw", exec_container)
    exec_host = _RecordingExec()
    monkeypatch.setattr(tool, "_exec_host_raw", exec_host)

    result = tool._git_commit(tmp_path)

    assert OPERATOR_ERROR in result
    # Fail closed: no commit subprocess, and no degradation to the host
    # backend (which would inject --no-verify).
    _assert_no_commit_subprocess(exec_container, exec_host)


# ---------------------------------------------------------------------------
# Container execution degraded to host_fallback: never allowed, never used.
# ---------------------------------------------------------------------------


def test_feature_branch_commit_does_not_use_host_fallback(
    tmp_path, monkeypatch
):
    tool = _tool(agent_config={"git_allow_worktree_commits": True})
    monkeypatch.setattr(tool, "_is_operator_managed_worktree", lambda r: True)
    monkeypatch.setattr(tool, "_use_container_mode", lambda: True)
    monkeypatch.setattr(
        tool,
        "_resolve_resource_execution",
        lambda: ({"mode": "host_fallback", "detail": "image build failed"}, None),
    )
    exec_container = _RecordingExec()
    monkeypatch.setattr(tool, "_exec_container_raw", exec_container)
    exec_host = _RecordingExec()
    monkeypatch.setattr(tool, "_exec_host_raw", exec_host)

    result = tool._git_commit(tmp_path)

    assert OPERATOR_ERROR in result
    # The degraded branch resolution raises RuntimeError (caught by the gate);
    # the hardened host backend must NEVER run for a policy-allowed commit.
    assert exec_host.calls == []
    assert exec_container.calls == []


# ---------------------------------------------------------------------------
# Merge/push intent: the gate never permits anything but a plain commit, and
# commit messages can never be coerced into extra subcommands.
# ---------------------------------------------------------------------------


def test_feature_branch_commit_rejects_merge_or_push_intent(
    tmp_path, monkeypatch
):
    # Merge-intent message on a feature branch: allowed as a NORMAL commit —
    # the message stays a -m argument, no merge/push subcommand is spawned.
    (tmp_path / "agent_change.py").write_text("x")
    tool = _tool(
        message="Merge branch 'main' into feat/x",
        file_path="agent_change.py",
        agent_config={"git_allow_worktree_commits": True},
    )
    monkeypatch.setattr(tool, "_is_operator_managed_worktree", lambda r: True)
    monkeypatch.setattr(tool, "_use_container_mode", lambda: True)
    manager = _RecordingManager()
    monkeypatch.setattr(
        tool,
        "_resolve_resource_execution",
        lambda: ({"mode": "containerized", "detail": "test"}, manager),
    )
    exec_container = _RecordingExec()
    monkeypatch.setattr(tool, "_exec_container_raw", exec_container)
    exec_host = _RecordingExec()
    monkeypatch.setattr(tool, "_exec_host_raw", exec_host)

    result = tool._git_commit(tmp_path)
    assert OPERATOR_ERROR not in result
    subcommands = [args[0] for (_r, args, _kw) in exec_container.calls]
    assert subcommands == ["rev-parse", "add", "commit"]
    commit_argv = [
        args
        for (_r, args, _kw) in exec_container.calls
        if args[0] == "commit"
    ]
    assert commit_argv == [
        ["commit", "-m", "Merge branch 'main' into feat/x"]
    ]
    # No merge/push/--no-verify anywhere in the executed argv.
    for (_r, args, _kw) in exec_container.calls:
        assert "--no-verify" not in args
    assert exec_host.calls == []

    # Same intent on a non-feature branch: the gate blocks it entirely.
    tool2 = _tool(
        message="Merge branch 'main' into main",
        agent_config={"git_allow_worktree_commits": True},
    )
    monkeypatch.setattr(tool2, "_is_operator_managed_worktree", lambda r: True)
    monkeypatch.setattr(tool2, "_use_container_mode", lambda: True)
    calls = []
    monkeypatch.setattr(tool2, "_run_git", _branch_fake(calls, "main"))
    exec_container2 = _RecordingExec()
    monkeypatch.setattr(tool2, "_exec_container_raw", exec_container2)
    monkeypatch.setattr(tool2, "_exec_host_raw", _RecordingExec())

    result2 = tool2._git_commit(tmp_path)
    assert OPERATOR_ERROR in result2
    assert len(calls) == 1  # branch query only
    assert calls[0][1] is False
    _assert_no_commit_subprocess(exec_container2, tool2._exec_host_raw)


# ---------------------------------------------------------------------------
# Branch-name shape: bare "feat/" / whitespace-padded prefixes are NOT
# feature branches -- startswith() alone would wrongly accept them.
# ---------------------------------------------------------------------------


def test_feature_branch_allowed_rejects_bare_feature_prefix(
    tmp_path, monkeypatch
):
    tool = _tool(agent_config={"git_allow_worktree_commits": True})
    monkeypatch.setattr(tool, "_use_container_mode", lambda: True)
    calls = []
    monkeypatch.setattr(tool, "_run_git", _branch_fake(calls, "feat/"))

    assert tool._feature_branch_agent_commit_allowed(tmp_path) is False
    # The branch check is container-mandatory: no host fallback.
    assert [c[0] for c in calls] == [["rev-parse", "--abbrev-ref", "HEAD"]]
    assert calls[0][1] is False


def test_feature_branch_allowed_rejects_whitespace_only_suffix(
    tmp_path, monkeypatch
):
    tool = _tool(agent_config={"git_allow_worktree_commits": True})
    monkeypatch.setattr(tool, "_use_container_mode", lambda: True)
    calls = []
    monkeypatch.setattr(tool, "_run_git", _branch_fake(calls, "feat/   "))

    assert tool._feature_branch_agent_commit_allowed(tmp_path) is False
    assert [c[0] for c in calls] == [["rev-parse", "--abbrev-ref", "HEAD"]]
    assert calls[0][1] is False


# ---------------------------------------------------------------------------
# Every commit must name its paths: full-commit mode (git add -A) is
# removed, so a commit without file_path is rejected before any git
# subprocess runs.
# ---------------------------------------------------------------------------


def test_commit_requires_file_path(tmp_path, monkeypatch):
    """No full-worktree mode: every commit must name its paths."""
    tool = _tool(agent_config={"git_allow_worktree_commits": True})
    monkeypatch.setattr(tool, "_is_operator_managed_worktree", lambda r: True)
    monkeypatch.setattr(tool, "_use_container_mode", lambda: True)
    calls = []
    monkeypatch.setattr(tool, "_run_git", _branch_fake(calls, "feat/x"))
    exec_container = _RecordingExec()
    monkeypatch.setattr(tool, "_exec_container_raw", exec_container)
    monkeypatch.setattr(tool, "_exec_host_raw", _RecordingExec())

    result = tool._git_commit(tmp_path)

    assert result == (
        "Error: file_path is required for commit operation (at least one path)"
    )
    # Only the container-mandatory branch query ran; no add/commit.
    assert calls == [(["rev-parse", "--abbrev-ref", "HEAD"], False)]
    _assert_no_commit_subprocess(exec_container, tool._exec_host_raw)


def test_feature_branch_commit_stages_only_named_path(
    tmp_path, monkeypatch
):
    (tmp_path / "agent_change.py").write_text("x")
    tool = _tool(
        file_path="agent_change.py",
        agent_config={"git_allow_worktree_commits": True},
    )
    monkeypatch.setattr(tool, "_is_operator_managed_worktree", lambda r: True)
    monkeypatch.setattr(tool, "_use_container_mode", lambda: True)
    calls = []
    monkeypatch.setattr(tool, "_run_git", _branch_fake(calls, "feat/x"))
    exec_container = _RecordingExec()
    monkeypatch.setattr(tool, "_exec_container_raw", exec_container)
    monkeypatch.setattr(tool, "_exec_host_raw", _RecordingExec())

    result = tool._git_commit(tmp_path)

    assert "ok\n" in result
    assert OPERATOR_ERROR not in result
    # Branch query + add of ONLY the named path + commit, all
    # container-mandatory (no host fallback anywhere).
    assert calls == [
        (["rev-parse", "--abbrev-ref", "HEAD"], False),
        (["add", "--", "agent_change.py"], False),
        (["commit", "-m", "agent commit"], False),
    ]
    # The full-worktree sweep (`git add -A`) must never be used.
    assert all("-A" not in c[0] for c in calls)
    _assert_no_commit_subprocess(exec_container, tool._exec_host_raw)
