"""Tests for the operator-managed worktree agent commit policy in GitWriteTool.

Policy: commits in an operator-managed worktree are blocked by default; they
are allowed only when all of the following hold:

1. the session ``git_write`` permission is ``"write"`` (via
   ``agent_config["session_permissions"]["git_write"]`` or effective
   permissions);
2. the current branch is NOT a protected branch (``dev``, ``master``,
   ``main``).  Feature-style branches (``feat/*``, ``fix/*``, ``refactor/*``,
   ``chore/*``, ``docs/*``, ``release/*``, others) are allowed, and a bare
   ``feat/`` prefix (including whitespace-only suffixes) is also allowed;
3. the tool is in containerized execution mode (``_use_container_mode()``);
4. the branch check plus the add/commit steps run with
   ``allow_host_fallback=False`` (host fallback is never permitted for
   worktree agent commits);
5. explicit ``file_path``(s) are provided -- ``git add -A`` is never issued.

These tests exercise ``GitWriteTool._git_commit`` directly (bypassing the
``execute()`` validation layer) to lock down the internal policy gates.
"""

from tools.git_write_tool import GitWriteTool

FLAG_ERROR = 'Error: git:write denied: session git_write permission is not "write"'
OPERATOR_ERROR = "Error: commits in this workspace are performed host-side by the operator (workspace is an operator-managed git worktree)"


def _tool(**overrides):
    params = {"operation": "commit", "message": "agent commit"}
    params.update(overrides)
    return GitWriteTool(**params)


class _RecordingManager:
    """Records every manager.exec() call; always succeeds."""

    def __init__(self):
        self.calls = []

    def exec(self, cmd, **kwargs):
        self.calls.append((list(cmd), kwargs))
        return {"exit_code": 0, "stdout": "ok\n", "stderr": ""}


class _RecordingExec:
    """Stand-in for _exec_container_raw/_exec_host_raw that records calls."""

    def __init__(self, stdout="ok\n", branch="feat/x\n"):
        self.calls = []
        self.stdout = stdout
        self.branch = branch

    def __call__(self, repo_root, args, **kwargs):
        self.calls.append((repo_root, list(args), kwargs))
        manager = kwargs.get("manager")
        if manager is not None:
            manager.exec(
                ["git"] + list(args),
                workdir=str(repo_root),
                timeout=kwargs.get("timeout", 30),
            )
        if args[:2] == ["rev-parse", "--abbrev-ref"]:
            return (0, self.branch, "")
        return (0, self.stdout, "")


def _branch_fake(recorder, branch):
    """Fake _run_git that records (args, allow_host_fallback) tuples."""

    def fake_run_git(repo_root, args, timeout=30, allow_host_fallback=True):
        recorder.append((list(args), allow_host_fallback))
        if args[:2] == ["rev-parse", "--abbrev-ref"]:
            return branch + "\n"
        return "ok\n"

    return fake_run_git


def _assert_no_commit_subprocess(exec_container, exec_host):
    assert exec_container.calls == []
    assert exec_host.calls == []


def test_worktree_commit_blocked_on_dev_by_default():
    """No operator flag -> blocked before any git call, even on dev."""
    tool = _tool()
    calls = []
    exec_container = _RecordingExec()
    exec_host = _RecordingExec()
    tool._is_operator_managed_worktree = lambda root: True  # noqa: SLF001
    tool._use_container_mode = lambda: True  # noqa: SLF001
    tool._run_git = _branch_fake(calls, "dev")  # noqa: SLF001
    tool._exec_container_raw = exec_container  # noqa: SLF001
    tool._exec_host_raw = exec_host  # noqa: SLF001

    result = tool._git_commit("/tmp/repo")

    assert FLAG_ERROR in result
    assert calls == []
    _assert_no_commit_subprocess(exec_container, exec_host)


def test_worktree_commit_blocked_on_main_with_flag():
    """Flag present but branch is protected (main) -> operator error."""
    tool = _tool(agent_config={"session_permissions": {"git_write": "write"}})
    calls = []
    exec_container = _RecordingExec()
    exec_host = _RecordingExec()
    tool._is_operator_managed_worktree = lambda root: True  # noqa: SLF001
    tool._use_container_mode = lambda: True  # noqa: SLF001
    tool._run_git = _branch_fake(calls, "main")  # noqa: SLF001
    tool._exec_container_raw = exec_container  # noqa: SLF001
    tool._exec_host_raw = exec_host  # noqa: SLF001

    result = tool._git_commit("/tmp/repo")

    assert OPERATOR_ERROR in result
    assert len(calls) == 1
    assert calls[0] == (["rev-parse", "--abbrev-ref", "HEAD"], False)
    _assert_no_commit_subprocess(exec_container, exec_host)


def test_feature_branch_commit_allowed_with_flag_container_mode(tmp_path):
    """Flag + unprotected branch + container mode -> commit runs in container."""
    (tmp_path / "agent_change.py").write_text("print('x')\n")
    tool = _tool(
        file_path="agent_change.py",
        agent_config={"session_permissions": {"git_write": "write"}},
    )
    manager = _RecordingManager()
    exec_container = _RecordingExec()
    exec_host = _RecordingExec()
    tool._is_operator_managed_worktree = lambda root: True  # noqa: SLF001
    tool._use_container_mode = lambda: True  # noqa: SLF001
    tool._resolve_resource_execution = lambda *a, **k: (  # noqa: SLF001
        {"mode": "containerized", "detail": "test"},
        manager,
    )
    tool._exec_container_raw = exec_container  # noqa: SLF001
    tool._exec_host_raw = exec_host  # noqa: SLF001

    result = tool._git_commit(tmp_path)

    assert "ok\n" in result
    assert OPERATOR_ERROR not in result
    container_args = [args for (_r, args, _kw) in exec_container.calls]
    assert [a[0] for a in container_args] == ["rev-parse", "add", "commit"]
    assert ["rev-parse", "--abbrev-ref", "HEAD"] in container_args
    assert ["add", "--", "agent_change.py"] in container_args
    assert all("-A" not in a for a in container_args)
    assert ["commit", "-m", "agent commit"] in container_args
    assert all("--no-verify" not in a for a in container_args)
    assert exec_host.calls == []
    assert [cmd[1] for cmd, _kw in manager.calls] == ["rev-parse", "add", "commit"]


def test_feature_branch_commit_denied_without_flag():
    """Empty agent_config -> flag gate fires before any git call."""
    tool = _tool(agent_config={})
    calls = []
    exec_container = _RecordingExec()
    exec_host = _RecordingExec()
    tool._is_operator_managed_worktree = lambda root: True  # noqa: SLF001
    tool._use_container_mode = lambda: True  # noqa: SLF001
    tool._run_git = _branch_fake(calls, "feat/x")  # noqa: SLF001
    tool._exec_container_raw = exec_container  # noqa: SLF001
    tool._exec_host_raw = exec_host  # noqa: SLF001

    result = tool._git_commit("/tmp/repo")

    assert FLAG_ERROR in result
    assert calls == []
    _assert_no_commit_subprocess(exec_container, exec_host)


def test_feature_branch_commit_allowed_on_non_protected_branch(tmp_path):
    """Unprotected branch (release/1.0) -> full add+commit with no fallback."""
    (tmp_path / "agent_change.py").write_text("print('x')\n")
    tool = _tool(
        file_path="agent_change.py",
        agent_config={"session_permissions": {"git_write": "write"}},
    )
    calls = []
    exec_container = _RecordingExec()
    exec_host = _RecordingExec()
    tool._is_operator_managed_worktree = lambda root: True  # noqa: SLF001
    tool._use_container_mode = lambda: True  # noqa: SLF001
    tool._run_git = _branch_fake(calls, "release/1.0")  # noqa: SLF001
    tool._exec_container_raw = exec_container  # noqa: SLF001
    tool._exec_host_raw = exec_host  # noqa: SLF001

    result = tool._git_commit(tmp_path)

    assert "ok\n" in result
    assert OPERATOR_ERROR not in result
    assert calls == [
        (["rev-parse", "--abbrev-ref", "HEAD"], False),
        (["add", "--", "agent_change.py"], False),
        (["commit", "-m", "agent commit"], False),
    ]
    assert all("-A" not in c[0] for c in calls)
    _assert_no_commit_subprocess(exec_container, exec_host)


def test_feature_branch_commit_denied_when_host_mode():
    """Host execution mode -> worktree commit denied before branch check."""
    tool = _tool(agent_config={"session_permissions": {"git_write": "write"}})
    calls = []
    exec_container = _RecordingExec()
    exec_host = _RecordingExec()
    tool._is_operator_managed_worktree = lambda root: True  # noqa: SLF001
    tool._use_container_mode = lambda: False  # noqa: SLF001
    tool._run_git = _branch_fake(calls, "feat/foo")  # noqa: SLF001
    tool._exec_container_raw = exec_container  # noqa: SLF001
    tool._exec_host_raw = exec_host  # noqa: SLF001

    result = tool._git_commit("/tmp/repo")

    assert OPERATOR_ERROR in result
    assert calls == []
    _assert_no_commit_subprocess(exec_container, exec_host)


def test_feature_branch_commit_denied_when_container_unavailable():
    """No containerized resource -> branch check fails closed."""
    tool = _tool(agent_config={"session_permissions": {"git_write": "write"}})
    exec_container = _RecordingExec()
    exec_host = _RecordingExec()
    tool._is_operator_managed_worktree = lambda root: True  # noqa: SLF001
    tool._use_container_mode = lambda: True  # noqa: SLF001
    tool._resolve_resource_execution = lambda *a, **k: (  # noqa: SLF001
        {"mode": "unavailable", "detail": "docker daemon unreachable", "failure_reason": "policy denial"},
        None,
    )
    tool._exec_container_raw = exec_container  # noqa: SLF001
    tool._exec_host_raw = exec_host  # noqa: SLF001

    result = tool._git_commit("/tmp/repo")

    assert OPERATOR_ERROR in result
    _assert_no_commit_subprocess(exec_container, exec_host)


def test_feature_branch_commit_does_not_use_host_fallback():
    """Execution degraded to host -> denied, no host or container subprocess."""
    tool = _tool(agent_config={"session_permissions": {"git_write": "write"}})
    exec_container = _RecordingExec()
    exec_host = _RecordingExec()
    tool._is_operator_managed_worktree = lambda root: True  # noqa: SLF001
    tool._use_container_mode = lambda: True  # noqa: SLF001
    tool._resolve_resource_execution = lambda *a, **k: (  # noqa: SLF001
        {"mode": "host_fallback", "detail": "image build failed"},
        None,
    )
    tool._exec_container_raw = exec_container  # noqa: SLF001
    tool._exec_host_raw = exec_host  # noqa: SLF001

    result = tool._git_commit("/tmp/repo")

    assert OPERATOR_ERROR in result
    assert exec_host.calls == []
    assert exec_container.calls == []


def test_feature_branch_commit_rejects_merge_or_push_intent(tmp_path):
    """Merge-looking messages are allowed only when the branch is unprotected."""
    (tmp_path / "agent_change.py").write_text("print('x')\n")
    tool = _tool(
        file_path="agent_change.py",
        message="Merge branch 'main' into feat/x",
        agent_config={"session_permissions": {"git_write": "write"}},
    )
    calls = []
    exec_container = _RecordingExec()
    exec_host = _RecordingExec()
    tool._is_operator_managed_worktree = lambda root: True  # noqa: SLF001
    tool._use_container_mode = lambda: True  # noqa: SLF001
    tool._run_git = _branch_fake(calls, "feat/x")  # noqa: SLF001
    tool._exec_container_raw = exec_container  # noqa: SLF001
    tool._exec_host_raw = exec_host  # noqa: SLF001

    result = tool._git_commit(tmp_path)

    assert "ok\n" in result
    assert OPERATOR_ERROR not in result
    assert [c[0][0] for c in calls] == ["rev-parse", "add", "commit"]
    assert calls[2][0] == ["commit", "-m", "Merge branch 'main' into feat/x"]
    assert all("--no-verify" not in c[0] for c in calls)
    assert exec_host.calls == []

    # Same intent but on a protected branch -> denied.
    tool2 = _tool(
        message="Merge branch 'main' into main",
        agent_config={"session_permissions": {"git_write": "write"}},
    )
    calls2 = []
    exec_container2 = _RecordingExec()
    exec_host2 = _RecordingExec()
    tool2._is_operator_managed_worktree = lambda root: True  # noqa: SLF001
    tool2._use_container_mode = lambda: True  # noqa: SLF001
    tool2._run_git = _branch_fake(calls2, "main")  # noqa: SLF001
    tool2._exec_container_raw = exec_container2  # noqa: SLF001
    tool2._exec_host_raw = exec_host2  # noqa: SLF001

    result2 = tool2._git_commit("/tmp/repo")

    assert OPERATOR_ERROR in result2
    assert len(calls2) == 1
    assert calls2[0][1] is False
    _assert_no_commit_subprocess(exec_container2, exec_host2)


def test_unprotected_branch_allows_bare_feature_prefix():
    """Bare 'feat/' prefix is an unprotected branch."""
    tool = _tool(agent_config={"session_permissions": {"git_write": "write"}})
    calls = []
    tool._use_container_mode = lambda: True  # noqa: SLF001
    tool._run_git = _branch_fake(calls, "feat/")  # noqa: SLF001

    assert tool._unprotected_branch_agent_commit_allowed("/tmp/repo") is True
    assert [c[0] for c in calls] == [["rev-parse", "--abbrev-ref", "HEAD"]]
    assert calls[0][1] is False


def test_unprotected_branch_allows_whitespace_only_suffix():
    """Bare 'feat/' with a whitespace-only suffix is still unprotected."""
    tool = _tool(agent_config={"session_permissions": {"git_write": "write"}})
    calls = []
    tool._use_container_mode = lambda: True  # noqa: SLF001
    tool._run_git = _branch_fake(calls, "feat/   ")  # noqa: SLF001

    assert tool._unprotected_branch_agent_commit_allowed("/tmp/repo") is True
    assert [c[0] for c in calls] == [["rev-parse", "--abbrev-ref", "HEAD"]]
    assert calls[0][1] is False


def test_commit_requires_file_path():
    """Missing file_path -> explicit error after the branch check passes."""
    tool = _tool(agent_config={"session_permissions": {"git_write": "write"}})
    calls = []
    exec_container = _RecordingExec()
    exec_host = _RecordingExec()
    tool._is_operator_managed_worktree = lambda root: True  # noqa: SLF001
    tool._use_container_mode = lambda: True  # noqa: SLF001
    tool._run_git = _branch_fake(calls, "feat/x")  # noqa: SLF001
    tool._exec_container_raw = exec_container  # noqa: SLF001
    tool._exec_host_raw = exec_host  # noqa: SLF001

    result = tool._git_commit("/tmp/repo")

    assert result == "Error: file_path is required for commit operation (at least one path)"
    assert calls == [(["rev-parse", "--abbrev-ref", "HEAD"], False)]
    _assert_no_commit_subprocess(exec_container, exec_host)


def test_feature_branch_commit_stages_only_named_path(tmp_path):
    """Only the named file_path is staged; git add -A is never issued."""
    (tmp_path / "agent_change.py").write_text("print('x')\n")
    (tmp_path / "unrelated.txt").write_text("untracked\n")
    tool = _tool(
        file_path="agent_change.py",
        agent_config={"session_permissions": {"git_write": "write"}},
    )
    calls = []
    exec_container = _RecordingExec()
    exec_host = _RecordingExec()
    tool._is_operator_managed_worktree = lambda root: True  # noqa: SLF001
    tool._use_container_mode = lambda: True  # noqa: SLF001
    tool._run_git = _branch_fake(calls, "feat/x")  # noqa: SLF001
    tool._exec_container_raw = exec_container  # noqa: SLF001
    tool._exec_host_raw = exec_host  # noqa: SLF001

    result = tool._git_commit(tmp_path)

    assert "ok\n" in result
    assert OPERATOR_ERROR not in result
    assert calls == [
        (["rev-parse", "--abbrev-ref", "HEAD"], False),
        (["add", "--", "agent_change.py"], False),
        (["commit", "-m", "agent commit"], False),
    ]
    assert all("-A" not in c[0] for c in calls)
    _assert_no_commit_subprocess(exec_container, exec_host)
