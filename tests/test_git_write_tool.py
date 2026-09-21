"""Tests for the operator-managed worktree agent commit policy in GitWriteTool.

Policy: commits in an operator-managed worktree are blocked by default; they
are allowed only when all of the following hold:

1. the session ``git`` permission is ``"write"`` (via
   ``agent_config["session_permissions"]["git"]`` or effective
   permissions);
2. the current branch is NOT a protected branch (``dev``, ``master``,
   ``main``).  Feature-style branches (``feat/*``, ``fix/*``, ``refactor/*``,
   ``chore/*``, ``docs/*``, ``release/*``, others) are allowed, and a bare
   ``feat/`` prefix (including whitespace-only suffixes) is also allowed;
3. the tool is in containerized execution mode (``_use_container_mode()``);
4. host execution is never permitted for worktree agent commits: the
   container-mandatory branch check and the add/commit steps hard-fail
   rather than degrade to the host;
5. explicit ``file_path``(s) are provided -- ``git add -A`` is never issued.

These tests exercise ``GitWriteTool._git_commit`` directly (bypassing the
``execute()`` validation layer) to lock down the internal policy gates.
"""

import subprocess
from pathlib import Path

import pytest

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
    """Fake _run_git that records each call's argv."""

    def fake_run_git(repo_root, args, timeout=30):
        recorder.append(list(args))
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
    tool = _tool(agent_config={"session_permissions": {"git": "write"}})
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
    assert calls[0] == ["rev-parse", "--abbrev-ref", "HEAD"]
    _assert_no_commit_subprocess(exec_container, exec_host)


def test_feature_branch_commit_allowed_with_flag_container_mode(tmp_path):
    """Flag + unprotected branch + container mode -> commit runs in container."""
    (tmp_path / "agent_change.py").write_text("print('x')\n")
    tool = _tool(
        file_path="agent_change.py",
        agent_config={"session_permissions": {"git": "write"}},
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
    assert ["commit", "-m", "agent commit", "--", "agent_change.py"] in container_args
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
        agent_config={"session_permissions": {"git": "write"}},
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
        ["rev-parse", "--abbrev-ref", "HEAD"],
        ["add", "--", "agent_change.py"],
        ["commit", "-m", "agent commit", "--", "agent_change.py"],
    ]
    assert all("-A" not in c for c in calls)
    _assert_no_commit_subprocess(exec_container, exec_host)


def test_feature_branch_commit_denied_when_host_mode():
    """Host execution mode -> worktree commit denied before branch check."""
    tool = _tool(agent_config={"session_permissions": {"git": "write"}})
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
    tool = _tool(agent_config={"session_permissions": {"git": "write"}})
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
    tool = _tool(agent_config={"session_permissions": {"git": "write"}})
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
        agent_config={"session_permissions": {"git": "write"}},
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
    assert [c[0] for c in calls] == ["rev-parse", "add", "commit"]
    assert calls[2] == ["commit", "-m", "Merge branch 'main' into feat/x", "--", "agent_change.py"]
    assert all("--no-verify" not in c for c in calls)
    assert exec_host.calls == []

    # Same intent but on a protected branch -> denied.
    tool2 = _tool(
        message="Merge branch 'main' into main",
        agent_config={"session_permissions": {"git": "write"}},
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
    _assert_no_commit_subprocess(exec_container2, exec_host2)


def test_unprotected_branch_allows_bare_feature_prefix():
    """Bare 'feat/' prefix is an unprotected branch."""
    tool = _tool(agent_config={"session_permissions": {"git": "write"}})
    calls = []
    tool._use_container_mode = lambda: True  # noqa: SLF001
    tool._run_git = _branch_fake(calls, "feat/")  # noqa: SLF001

    assert tool._unprotected_branch_agent_commit_allowed("/tmp/repo") is True
    assert calls == [["rev-parse", "--abbrev-ref", "HEAD"]]


def test_unprotected_branch_allows_whitespace_only_suffix():
    """Bare 'feat/' with a whitespace-only suffix is still unprotected."""
    tool = _tool(agent_config={"session_permissions": {"git": "write"}})
    calls = []
    tool._use_container_mode = lambda: True  # noqa: SLF001
    tool._run_git = _branch_fake(calls, "feat/   ")  # noqa: SLF001

    assert tool._unprotected_branch_agent_commit_allowed("/tmp/repo") is True
    assert calls == [["rev-parse", "--abbrev-ref", "HEAD"]]


def test_commit_requires_file_path():
    """Missing file_path -> explicit error after the branch check passes."""
    tool = _tool(agent_config={"session_permissions": {"git": "write"}})
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
    assert calls == [["rev-parse", "--abbrev-ref", "HEAD"]]
    _assert_no_commit_subprocess(exec_container, exec_host)


def test_feature_branch_commit_stages_only_named_path(tmp_path):
    """Only the named file_path is staged; git add -A is never issued."""
    (tmp_path / "agent_change.py").write_text("print('x')\n")
    (tmp_path / "unrelated.txt").write_text("untracked\n")
    tool = _tool(
        file_path="agent_change.py",
        agent_config={"session_permissions": {"git": "write"}},
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
        ["rev-parse", "--abbrev-ref", "HEAD"],
        ["add", "--", "agent_change.py"],
        ["commit", "-m", "agent commit", "--", "agent_change.py"],
    ]
    assert all("-A" not in c for c in calls)
    _assert_no_commit_subprocess(exec_container, exec_host)


# --- Timeout fail-open defect (RED stage) -----------------------------------
#
# ``GitReadTool._run_git`` swallows ``subprocess.TimeoutExpired`` and
# ``TimeoutError`` into the literal string ``"Git command timed out"``. Both
# commit gates parse that string as a *branch name*: it is non-empty and not
# in ``_PROTECTED_BRANCHES``, so a branch probe that times out is read as an
# unprotected branch and the commit is PERMITTED (fail open). The tests below
# pin the fail-closed contract: a timed-out branch probe must never result in
# a permitted commit.


class _TimeoutOnBranchProbe:
    """Fake ``_run_git_raw`` that times out on the branch probe only.

    ``rev-parse --abbrev-ref HEAD`` (the branch resolution) raises the supplied
    timeout exception; every other git command reports the ``COMMIT_OK``
    sentinel. The argv of every call is recorded so a test can assert whether
    the ``git commit`` subprocess was ever reached.
    """

    def __init__(self, timeout_exc):
        self.timeout_exc = timeout_exc
        self.calls = []

    def __call__(self, repo_root, args, timeout=30):
        self.calls.append(list(args))
        if list(args)[:2] == ["rev-parse", "--abbrev-ref"]:
            raise self.timeout_exc
        return (0, "COMMIT_OK\n", "")

    @property
    def commit_ran(self):
        return any(args[:1] == ["commit"] for args in self.calls)


def _commit_permitted(tool, repo_root):
    """True iff ``_git_commit`` permitted the commit to run.

    A timeout that propagates (``subprocess.TimeoutExpired`` /
    ``TimeoutError``) is the fail-closed signal -- the error surfaces as a hard
    failure instead of being swallowed into a string -- so nothing is permitted.
    """
    try:
        result = tool._git_commit(repo_root)
    except (subprocess.TimeoutExpired, TimeoutError):
        return False
    return "COMMIT_OK" in result


def _timeout_expired():
    return subprocess.TimeoutExpired(cmd="git", timeout=30)


@pytest.mark.parametrize(
    "timeout_exc_factory",
    [_timeout_expired, lambda: TimeoutError("git timed out")],
    ids=["TimeoutExpired", "TimeoutError"],
)
def test_unprotected_branch_agent_commit_fails_closed_on_branch_timeout(
    timeout_exc_factory,
):
    """The worktree agent-commit gate must fail CLOSED on a branch timeout.

    ``_unprotected_branch_agent_commit_allowed`` reads the "Git command timed
    out" string as a valid, unprotected branch and returns True, so the commit
    runs. That container-mandatory branch check is the only guard against an
    agent commit in an operator-managed worktree; a timeout there must deny the
    commit instead of permitting it.
    """
    tool = _tool(
        file_path="agent_change.py",
        agent_config={"session_permissions": {"git": "write"}},
    )
    tool._is_operator_managed_worktree = lambda root: True  # noqa: SLF001
    tool._use_container_mode = lambda: True  # noqa: SLF001
    tool._validated_rel_paths = lambda root, paths: [  # noqa: SLF001
        "agent_change.py"
    ]
    raw = _TimeoutOnBranchProbe(timeout_exc_factory())
    tool._run_git_raw = raw  # noqa: SLF001

    assert _commit_permitted(tool, "/tmp/repo") is False
    assert raw.commit_ran is False


@pytest.mark.parametrize(
    "timeout_exc_factory",
    [_timeout_expired, lambda: TimeoutError("git timed out")],
    ids=["TimeoutExpired", "TimeoutError"],
)
def test_wofb_feature_branch_gate_fails_closed_on_branch_timeout(
    timeout_exc_factory,
):
    """The write_on_feature_branch commit gate must fail CLOSED on a timeout.

    Same defect, second gate (``_git_commit``): the branch probe returns "Git
    command timed out", the gate reads it as an unprotected branch and lets the
    commit through -- so a wofb session can commit on a protected branch
    whenever the branch probe times out.
    """
    tool = _tool(
        file_path=["note.txt"],
        agent_config={"session_permissions": {"git": "write_on_feature_branch"}},
    )
    tool._is_operator_managed_worktree = lambda root: False  # noqa: SLF001
    tool._validated_rel_paths = lambda root, paths: ["note.txt"]  # noqa: SLF001
    raw = _TimeoutOnBranchProbe(timeout_exc_factory())
    tool._run_git_raw = raw  # noqa: SLF001

    assert _commit_permitted(tool, "/tmp/repo") is False
    assert raw.commit_ran is False



# --- Pathspec-scoped commit defect (C3) --------------------------------------
#
# ``_git_commit`` historically had TWO arms: a plain arm that emitted the
# path-scoped argv ``["commit", "-m", msg, "--", *paths]`` and an
# operator-managed-worktree arm that emitted the BARE argv
# ``["commit", "-m", msg]`` -- which commits the WHOLE index, sweeping any
# pre-staged unrelated file into the commit. The unification collapses both
# arms into the single path-scoped flow; these tests pin that contract.


def test_commit_with_pathspec_ignores_pre_staged_unrelated_file(tmp_path):
    """A pre-staged unrelated file must not be swept into a selective commit.

    ``pre_staged.py`` is left in the index; the commit names only
    ``agent_change.py``. The commit argv must therefore carry the pathspec
    (``-- agent_change.py``) so the unrelated staged file is excluded.
    """
    (tmp_path / "agent_change.py").write_text("print('x')\n")
    (tmp_path / "pre_staged.py").write_text("print('y')\n")
    tool = _tool(
        file_path="agent_change.py",
        agent_config={"session_permissions": {"git": "write"}},
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
    commit_args = [c for c in calls if c and c[0] == "commit"]
    assert commit_args == [
        ["commit", "-m", "agent commit", "--", "agent_change.py"]
    ]
    # The unrelated pre-staged file must never appear in any argv.
    assert all("pre_staged.py" not in c for c in calls)
    _assert_no_commit_subprocess(exec_container, exec_host)


def test_commit_worktree_arm_uses_pathspec(tmp_path):
    """The operator-managed-worktree path must emit the path-scoped argv.

    The historic worktree arm committed the whole index (bare
    ``git commit -m``); after unification every code path passes the validated
    paths through ``-- <paths>``.
    """
    (tmp_path / "agent_change.py").write_text("print('x')\n")
    tool = _tool(
        file_path="agent_change.py",
        agent_config={"session_permissions": {"git": "write"}},
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
        ["rev-parse", "--abbrev-ref", "HEAD"],
        ["add", "--", "agent_change.py"],
        ["commit", "-m", "agent commit", "--", "agent_change.py"],
    ]
    assert all("-A" not in c for c in calls)
    _assert_no_commit_subprocess(exec_container, exec_host)


@pytest.mark.parametrize(
    "empty_paths", [None, "", []], ids=["none", "empty_str", "empty_list"]
)
def test_commit_empty_paths_errors_no_subprocess(empty_paths):
    """Empty file_path -> explicit error with NO git subprocess at all.

    The empty-path guard runs before any ``git add``/``git commit``: no write
    subprocess may ever be issued for a commit that names no paths.
    """
    tool = _tool(
        file_path=empty_paths,
        agent_config={"session_permissions": {"git": "write"}},
    )
    calls = []
    exec_container = _RecordingExec()
    exec_host = _RecordingExec()
    tool._is_operator_managed_worktree = lambda root: False  # noqa: SLF001
    tool._use_container_mode = lambda: True  # noqa: SLF001
    tool._run_git = _branch_fake(calls, "feat/x")  # noqa: SLF001
    tool._exec_container_raw = exec_container  # noqa: SLF001
    tool._exec_host_raw = exec_host  # noqa: SLF001

    result = tool._git_commit("/tmp/repo")

    assert result == "Error: file_path is required for commit operation (at least one path)"
    assert calls == []
    _assert_no_commit_subprocess(exec_container, exec_host)



# --- Detached-HEAD fail-open defect (A4) -------------------------------------
#
# ``git rev-parse --abbrev-ref HEAD`` reports the literal string "HEAD" when
# HEAD is detached. "HEAD" is a *valid* branch ref and is NOT in
# ``_PROTECTED_BRANCHES``, so both commit gates would otherwise treat a
# detached HEAD as an unprotected branch and PERMIT the commit (fail-OPEN).
# A detached HEAD is not on a branch: both gates must refuse it with a
# distinguishable "not on a branch" message, and no add/commit may run.


def test_detached_head_denied_by_agent_commit_gate():
    """Gate (1) refuses a detached HEAD and records the refusal reason."""
    tool = _tool(agent_config={"session_permissions": {"git": "write"}})
    calls = []
    tool._use_container_mode = lambda: True  # noqa: SLF001
    tool._run_git = _branch_fake(calls, "HEAD")  # noqa: SLF001

    allowed = tool._unprotected_branch_agent_commit_allowed("/tmp/repo")

    assert allowed is False
    assert "not on a branch" in tool._agent_commit_refusal_reason
    assert calls == [["rev-parse", "--abbrev-ref", "HEAD"]]


def test_detached_head_commit_denied_operator_managed():
    """Operator-managed worktree + detached HEAD -> refused, no subprocess.

    ``_git_commit`` must not fall back to the operator-managed-worktree error
    (which would be misleading) and must not run any add/commit subprocess.
    """
    tool = _tool(
        file_path="agent_change.py",
        agent_config={"session_permissions": {"git": "write"}},
    )
    calls = []
    exec_container = _RecordingExec()
    exec_host = _RecordingExec()
    tool._is_operator_managed_worktree = lambda root: True  # noqa: SLF001
    tool._use_container_mode = lambda: True  # noqa: SLF001
    tool._run_git = _branch_fake(calls, "HEAD")  # noqa: SLF001
    tool._exec_container_raw = exec_container  # noqa: SLF001
    tool._exec_host_raw = exec_host  # noqa: SLF001

    result = tool._git_commit("/tmp/repo")

    assert "not on a branch" in result
    assert OPERATOR_ERROR not in result
    assert calls == [["rev-parse", "--abbrev-ref", "HEAD"]]
    _assert_no_commit_subprocess(exec_container, exec_host)


def test_detached_head_denied_by_wofb_commit_gate(tmp_path):
    """write_on_feature_branch grant + detached HEAD -> gate (2) refuses."""
    (tmp_path / "note.txt").write_text("x\n")
    tool = _tool(
        file_path=["note.txt"],
        agent_config={"session_permissions": {"git": "write_on_feature_branch"}},
    )
    calls = []
    exec_container = _RecordingExec()
    exec_host = _RecordingExec()
    tool._is_operator_managed_worktree = lambda root: False  # noqa: SLF001
    tool._run_git = _branch_fake(calls, "HEAD")  # noqa: SLF001
    tool._exec_container_raw = exec_container  # noqa: SLF001
    tool._exec_host_raw = exec_host  # noqa: SLF001

    result = tool._git_commit(tmp_path)

    assert "not on a branch" in result
    assert calls == [["rev-parse", "--abbrev-ref", "HEAD"]]
    _assert_no_commit_subprocess(exec_container, exec_host)


def test_normal_branch_commit_still_allowed_regression(tmp_path):
    """Regression: a real unprotected branch is still permitted unchanged."""
    (tmp_path / "agent_change.py").write_text("print('x')\n")
    tool = _tool(
        file_path="agent_change.py",
        agent_config={"session_permissions": {"git": "write"}},
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
    assert "not on a branch" not in result
    assert OPERATOR_ERROR not in result
    assert [c[0] for c in calls] == ["rev-parse", "add", "commit"]
    _assert_no_commit_subprocess(exec_container, exec_host)


# ---------------------------------------------------------------------------
# worktree_add / worktree_remove / stash_push / stash_pop write operations.
#
# Exercised directly (bypassing execute()) to pin down argv assembly, the
# fail-closed validation surface and the bare-vs-trailer output convention:
# argument-validation refusals keep the byte-exact bare form (NO execution-mode
# trailer), while every git result (success AND git-error) is wrapped by
# _with_mode().
# ---------------------------------------------------------------------------

_TRAILER = "execution_mode: unavailable\nfailure_reason: none"


class _RawRecorder:
    """Fake _run_git_raw that replays queued (exit, stdout, stderr) tuples."""

    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def __call__(self, repo_root, args, timeout=30):
        self.calls.append(list(args))
        if self.results:
            return self.results.pop(0)
        return (0, "", "")


def _write_tool(**overrides):
    """GitWriteTool with a write-capable session git permission."""
    overrides.setdefault("agent_config", {"session_permissions": {"git": "write"}})
    return _tool(**overrides)


def _run_git_outputting(recorder, output):
    """Fake _run_git that records argv and returns a fixed output string."""

    def fake(repo_root, args, timeout=30):
        recorder.append(list(args))
        return output

    return fake


# --- worktree_add ---------------------------------------------------------


def test_worktree_add_requires_path(tmp_path):
    tool = _write_tool(operation="worktree_add")
    raw = _RawRecorder()
    add_calls = []
    tool._run_git_raw = raw  # noqa: SLF001
    tool._run_git = _branch_fake(add_calls, "ok")  # noqa: SLF001

    result = tool._git_worktree_add(tmp_path)

    assert result == "Error: path is required for worktree_add operation"
    assert raw.calls == []
    assert add_calls == []


def test_worktree_add_defaults_base_to_head(tmp_path):
    tool = _write_tool(operation="worktree_add", path="wt")
    raw = _RawRecorder((0, "", ""))
    add_calls = []
    tool._run_git_raw = raw  # noqa: SLF001
    tool._run_git = _branch_fake(add_calls, "ok")  # noqa: SLF001

    result = tool._git_worktree_add(tmp_path)

    assert raw.calls == [["worktree", "list", "--porcelain"]]
    assert add_calls == [["worktree", "add", "wt", "HEAD"]]
    assert result == f"Created worktree at 'wt' (base: HEAD)\n{_TRAILER}"


def test_worktree_add_explicit_base(tmp_path):
    tool = _write_tool(operation="worktree_add", path="wt", base="feat/y")
    raw = _RawRecorder((0, "", ""))
    add_calls = []
    tool._run_git_raw = raw  # noqa: SLF001
    tool._run_git = _branch_fake(add_calls, "ok")  # noqa: SLF001

    result = tool._git_worktree_add(tmp_path)

    assert add_calls == [["worktree", "add", "wt", "feat/y"]]
    assert result == f"Created worktree at 'wt' (base: feat/y)\n{_TRAILER}"


def test_worktree_add_already_registered_refused(tmp_path):
    target = str((tmp_path / "wt").resolve())
    tool = _write_tool(operation="worktree_add", path="wt")
    raw = _RawRecorder((0, f"worktree {target}\n", ""))
    add_calls = []
    tool._run_git_raw = raw  # noqa: SLF001
    tool._run_git = _branch_fake(add_calls, "ok")  # noqa: SLF001

    result = tool._git_worktree_add(tmp_path)

    assert result == (
        "Error: a worktree is already registered at 'wt'; remove it before adding"
    )
    assert raw.calls == [["worktree", "list", "--porcelain"]]
    assert add_calls == []


def test_worktree_add_invalid_base_ref_refused(tmp_path):
    tool = _write_tool(operation="worktree_add", path="wt", base="bad ref")
    raw = _RawRecorder()
    add_calls = []
    tool._run_git_raw = raw  # noqa: SLF001
    tool._run_git = _branch_fake(add_calls, "ok")  # noqa: SLF001

    result = tool._git_worktree_add(tmp_path)

    assert result == "Error: invalid base ref for worktree_add: 'bad ref'"
    assert raw.calls == []
    assert add_calls == []


def test_worktree_add_outside_workspace_refused(tmp_path):
    tool = _write_tool(operation="worktree_add", path="../escape")
    object.__setattr__(tool, "workspace_path", str(tmp_path))
    raw = _RawRecorder()
    add_calls = []
    tool._run_git_raw = raw  # noqa: SLF001
    tool._run_git = _branch_fake(add_calls, "ok")  # noqa: SLF001

    result = tool._git_worktree_add(tmp_path)

    assert result.startswith("Error: ")
    assert "outside workspace" in result
    assert "execution_mode" not in result
    assert raw.calls == []
    assert add_calls == []


def test_worktree_add_git_error_wrapped(tmp_path):
    tool = _write_tool(operation="worktree_add", path="wt")
    raw = _RawRecorder((0, "", ""))
    add_calls = []
    tool._run_git_raw = raw  # noqa: SLF001
    tool._run_git = _run_git_outputting(  # noqa: SLF001
        add_calls, "Git command failed (exit code 128):\nfatal: boom\n"
    )

    result = tool._git_worktree_add(tmp_path)

    assert "Git command failed (exit code 128):" in result
    assert result.endswith(_TRAILER)


# --- worktree_remove ------------------------------------------------------


def test_worktree_remove_requires_path(tmp_path):
    tool = _write_tool(operation="worktree_remove")
    raw = _RawRecorder()
    rm_calls = []
    tool._run_git_raw = raw  # noqa: SLF001
    tool._run_git = _branch_fake(rm_calls, "ok")  # noqa: SLF001

    result = tool._git_worktree_remove(tmp_path)

    assert result == "Error: path is required for worktree_remove operation"
    assert raw.calls == []
    assert rm_calls == []


def test_worktree_remove_primary_worktree_refused(tmp_path):
    repo = tmp_path.resolve()
    tool = _write_tool(operation="worktree_remove", path=".")
    raw = _RawRecorder()
    rm_calls = []
    tool._run_git_raw = raw  # noqa: SLF001
    tool._run_git = _branch_fake(rm_calls, "ok")  # noqa: SLF001

    result = tool._git_worktree_remove(repo)

    assert result == (
        f"Error: refusing to remove the primary worktree ({repo}); "
        "worktree_remove only removes linked worktrees"
    )
    assert raw.calls == []
    assert rm_calls == []


def test_worktree_remove_locked_refused_without_force(tmp_path):
    repo = tmp_path.resolve()
    target = str((repo / "wt").resolve())
    tool = _write_tool(operation="worktree_remove", path="wt")
    raw = _RawRecorder((0, f"worktree {target}\nlocked\n", ""))
    rm_calls = []
    tool._run_git_raw = raw  # noqa: SLF001
    tool._run_git = _branch_fake(rm_calls, "ok")  # noqa: SLF001

    result = tool._git_worktree_remove(repo)

    assert result == (
        "Error: refusing to remove worktree 'wt': it is locked; retry with force=true"
    )
    assert raw.calls == [["worktree", "list", "--porcelain"]]
    assert rm_calls == []


def test_worktree_remove_dirty_refused_without_force(tmp_path):
    repo = tmp_path.resolve()
    target = str((repo / "wt").resolve())
    tool = _write_tool(operation="worktree_remove", path="wt")
    raw = _RawRecorder((0, f"worktree {target}\n", ""), (0, " M a.py\n", ""))
    rm_calls = []
    tool._run_git_raw = raw  # noqa: SLF001
    tool._run_git = _branch_fake(rm_calls, "ok")  # noqa: SLF001

    result = tool._git_worktree_remove(repo)

    assert result == (
        "Error: refusing to remove worktree 'wt': it is has local changes; "
        "retry with force=true"
    )
    assert raw.calls == [
        ["worktree", "list", "--porcelain"],
        ["-C", "wt", "status", "--porcelain"],
    ]
    assert rm_calls == []


def test_worktree_remove_registered_clean_removes(tmp_path):
    repo = tmp_path.resolve()
    target = str((repo / "wt").resolve())
    tool = _write_tool(operation="worktree_remove", path="wt")
    raw = _RawRecorder((0, f"worktree {target}\n", ""), (0, "", ""))
    rm_calls = []
    tool._run_git_raw = raw  # noqa: SLF001
    tool._run_git = _branch_fake(rm_calls, "ok")  # noqa: SLF001

    result = tool._git_worktree_remove(repo)

    assert result == f"Removed worktree 'wt'\n{_TRAILER}"
    assert rm_calls == [["worktree", "remove", "wt"]]


def test_worktree_remove_force_flag_appended(tmp_path):
    repo = tmp_path.resolve()
    target = str((repo / "wt").resolve())
    tool = _write_tool(operation="worktree_remove", path="wt", force=True)
    raw = _RawRecorder((0, f"worktree {target}\nlocked\n", ""))
    rm_calls = []
    tool._run_git_raw = raw  # noqa: SLF001
    tool._run_git = _branch_fake(rm_calls, "ok")  # noqa: SLF001

    result = tool._git_worktree_remove(repo)

    assert result == f"Removed worktree 'wt'\n{_TRAILER}"
    assert rm_calls == [["worktree", "remove", "--force", "wt"]]
    # force skips the dirty-status probe: only the registration probe ran.
    assert raw.calls == [["worktree", "list", "--porcelain"]]


def test_worktree_remove_unregistered_removes(tmp_path):
    repo = tmp_path.resolve()
    tool = _write_tool(operation="worktree_remove", path="wt")
    raw = _RawRecorder((0, "", ""))
    rm_calls = []
    tool._run_git_raw = raw  # noqa: SLF001
    tool._run_git = _branch_fake(rm_calls, "ok")  # noqa: SLF001

    result = tool._git_worktree_remove(repo)

    assert result == f"Removed worktree 'wt'\n{_TRAILER}"
    assert rm_calls == [["worktree", "remove", "wt"]]
    assert raw.calls == [["worktree", "list", "--porcelain"]]


def test_worktree_remove_git_error_wrapped(tmp_path):
    repo = tmp_path.resolve()
    target = str((repo / "wt").resolve())
    tool = _write_tool(operation="worktree_remove", path="wt")
    raw = _RawRecorder((0, f"worktree {target}\n", ""), (0, "", ""))
    rm_calls = []
    tool._run_git_raw = raw  # noqa: SLF001
    tool._run_git = _run_git_outputting(  # noqa: SLF001
        rm_calls, "Git command failed (exit code 1):\nnope\n"
    )

    result = tool._git_worktree_remove(repo)

    assert "Git command failed (exit code 1):" in result
    assert result.endswith(_TRAILER)


def test_worktree_remove_outside_workspace_refused(tmp_path):
    tool = _write_tool(operation="worktree_remove", path="../escape")
    object.__setattr__(tool, "workspace_path", str(tmp_path))
    raw = _RawRecorder()
    rm_calls = []
    tool._run_git_raw = raw  # noqa: SLF001
    tool._run_git = _branch_fake(rm_calls, "ok")  # noqa: SLF001

    result = tool._git_worktree_remove(tmp_path)

    assert result.startswith("Error: ")
    assert "outside workspace" in result
    assert "execution_mode" not in result
    assert raw.calls == []
    assert rm_calls == []


# --- stash_push -----------------------------------------------------------


def test_stash_push_requires_message(tmp_path):
    tool = _write_tool(operation="stash_push", message=None)
    raw = _RawRecorder()
    tool._run_git_raw = raw  # noqa: SLF001

    result = tool._git_stash_push(tmp_path)

    assert result == "Error: message is required for stash_push operation"
    assert raw.calls == []


def test_stash_push_blank_message_refused(tmp_path):
    tool = _write_tool(operation="stash_push", message="   ")
    raw = _RawRecorder()
    tool._run_git_raw = raw  # noqa: SLF001

    result = tool._git_stash_push(tmp_path)

    assert result == "Error: message is required for stash_push operation"
    assert raw.calls == []


def test_stash_push_basic_argv_never_all_or_untracked(tmp_path):
    tool = _write_tool(operation="stash_push", message="wip")
    raw = _RawRecorder((0, "Saved working directory\n", ""))
    tool._run_git_raw = raw  # noqa: SLF001

    result = tool._git_stash_push(tmp_path)

    assert raw.calls == [["stash", "push", "-m", "wip"]]
    argv = raw.calls[0]
    assert "-a" not in argv and "-u" not in argv and "--all" not in argv
    assert result.startswith("Saved working directory")
    assert result.endswith(_TRAILER)


def test_stash_push_with_paths(tmp_path):
    repo = tmp_path.resolve()
    tool = _write_tool(
        operation="stash_push", message="wip", paths=["a.py", "b.py"]
    )
    raw = _RawRecorder((0, "", ""))
    tool._run_git_raw = raw  # noqa: SLF001

    tool._git_stash_push(repo)

    assert raw.calls == [["stash", "push", "-m", "wip", "--", "a.py", "b.py"]]


def test_stash_push_rejects_pathspec_wildcard(tmp_path):
    tool = _write_tool(operation="stash_push", message="wip", paths=["*.py"])
    raw = _RawRecorder()
    tool._run_git_raw = raw  # noqa: SLF001

    result = tool._git_stash_push(tmp_path)

    assert result.startswith("Error: ")
    assert "pathspec wildcards" in result
    assert "execution_mode" not in result
    assert raw.calls == []


def test_stash_push_no_local_changes(tmp_path):
    tool = _write_tool(operation="stash_push", message="wip")
    raw = _RawRecorder((0, "No local changes to save\n", ""))
    tool._run_git_raw = raw  # noqa: SLF001

    result = tool._git_stash_push(tmp_path)

    assert result == f"No local changes to save.\n{_TRAILER}"


def test_stash_push_git_error_uses_stderr(tmp_path):
    tool = _write_tool(operation="stash_push", message="wip")
    raw = _RawRecorder((1, "ignored stdout\n", "fatal: boom\n"))
    tool._run_git_raw = raw  # noqa: SLF001

    result = tool._git_stash_push(tmp_path)

    assert result.startswith("Git command failed (exit code 1):\nfatal: boom")
    assert "ignored stdout" not in result
    assert result.endswith(_TRAILER)


# --- stash_pop ------------------------------------------------------------


def test_stash_pop_default_index(tmp_path):
    tool = _write_tool(operation="stash_pop")
    raw = _RawRecorder((0, "Dropped refs/stash@{0}\n", ""))
    tool._run_git_raw = raw  # noqa: SLF001

    result = tool._git_stash_pop(tmp_path)

    assert raw.calls == [["stash", "pop", "stash@{0}"]]
    assert result.startswith("Dropped refs/stash@{0}")
    assert result.endswith(_TRAILER)


def test_stash_pop_custom_index(tmp_path):
    tool = _write_tool(operation="stash_pop", index=3)
    raw = _RawRecorder((0, "ok\n", ""))
    tool._run_git_raw = raw  # noqa: SLF001

    tool._git_stash_pop(tmp_path)

    assert raw.calls == [["stash", "pop", "stash@{3}"]]


def test_stash_pop_negative_index_rejected(tmp_path):
    tool = _write_tool(operation="stash_pop", index=-1)
    raw = _RawRecorder()
    tool._run_git_raw = raw  # noqa: SLF001

    result = tool._git_stash_pop(tmp_path)

    assert result == "Error: index must be a non-negative integer for stash_pop"
    assert raw.calls == []


def test_stash_pop_bool_index_rejected(tmp_path):
    tool = _write_tool(operation="stash_pop")
    object.__setattr__(tool, "index", True)
    raw = _RawRecorder()
    tool._run_git_raw = raw  # noqa: SLF001

    result = tool._git_stash_pop(tmp_path)

    assert result == "Error: index must be a non-negative integer for stash_pop"
    assert raw.calls == []


def test_stash_pop_non_int_index_rejected(tmp_path):
    tool = _write_tool(operation="stash_pop")
    object.__setattr__(tool, "index", "1")
    raw = _RawRecorder()
    tool._run_git_raw = raw  # noqa: SLF001

    result = tool._git_stash_pop(tmp_path)

    assert result == "Error: index must be a non-negative integer for stash_pop"
    assert raw.calls == []


def test_stash_pop_git_error_concatenates_stdout_stderr(tmp_path):
    tool = _write_tool(operation="stash_pop")
    raw = _RawRecorder((2, "partial\n", "conflict\n"))
    tool._run_git_raw = raw  # noqa: SLF001

    result = tool._git_stash_pop(tmp_path)

    assert result.startswith("Git command failed (exit code 2):\npartial\nconflict")
    assert result.endswith(_TRAILER)


# --- fail-closed git permission gate (defense-in-depth) -------------------


def test_worktree_add_denied_without_write_permission(tmp_path):
    tool = _tool(operation="worktree_add", path="wt")
    raw = _RawRecorder()
    add_calls = []
    tool._run_git_raw = raw  # noqa: SLF001
    tool._run_git = _branch_fake(add_calls, "ok")  # noqa: SLF001

    result = tool._git_worktree_add(tmp_path)

    assert result == FLAG_ERROR
    assert raw.calls == []
    assert add_calls == []


def test_worktree_remove_denied_without_write_permission(tmp_path):
    tool = _tool(operation="worktree_remove", path="wt")
    raw = _RawRecorder()
    rm_calls = []
    tool._run_git_raw = raw  # noqa: SLF001
    tool._run_git = _branch_fake(rm_calls, "ok")  # noqa: SLF001

    result = tool._git_worktree_remove(tmp_path)

    assert result == FLAG_ERROR
    assert raw.calls == []
    assert rm_calls == []


def test_stash_push_denied_without_write_permission(tmp_path):
    tool = _tool(operation="stash_push", message="wip")
    raw = _RawRecorder()
    tool._run_git_raw = raw  # noqa: SLF001

    result = tool._git_stash_push(tmp_path)

    assert result == FLAG_ERROR
    assert raw.calls == []


def test_stash_pop_denied_without_write_permission(tmp_path):
    tool = _tool(operation="stash_pop")
    raw = _RawRecorder()
    tool._run_git_raw = raw  # noqa: SLF001

    result = tool._git_stash_pop(tmp_path)

    assert result == FLAG_ERROR
    assert raw.calls == []




# =========================================================================
# §E6 operation-Literal guard + §E3 hook-output capture / host-mode parity
# =========================================================================

import typing  # noqa: E402

from tools.git_info_tool import GitReadTool  # noqa: E402

_C_LIST = {"push", "fetch", "reset", "clean", "config", "cherry-pick", "rebase"}


def test_operation_literals_exclude_c_list():
    """§E6: the C-list ops must never appear in the WRITE Literal; they must
    also stay out of the READ Literal EXCEPT ``config``, which is a
    pre-existing READ-ONLY inspection op (``git config --list`` /
    ``git config --get <key>``). §C prohibits config *mutation*, not
    inspection, so ``config`` is excluded from the read-side check.
    """
    read_ops = set(typing.get_args(GitReadTool.model_fields["operation"].annotation))
    write_ops = set(typing.get_args(GitWriteTool.model_fields["operation"].annotation))
    assert _C_LIST.isdisjoint(write_ops)
    assert (_C_LIST - {"config"}).isdisjoint(read_ops)


# --- §E3-A: _run_git records hook output on a successful commit ----------


def test_run_git_commit_surfaces_stderr(tmp_path):
    tool = _write_tool()
    tool._run_git_raw = _RawRecorder((0, "OUT\n", "[pre-commit] banner"))
    result = tool._run_git(tmp_path, ["commit", "-m", "x", "--", "f"])
    assert "OUT" in result
    assert "[pre-commit] banner" in result


def test_run_git_non_commit_discards_stderr(tmp_path):
    tool = _write_tool()
    tool._run_git_raw = _RawRecorder((0, "OUT\n", "noise"))
    result = tool._run_git(tmp_path, ["status"])
    assert result == "OUT\n"


def test_run_git_commit_failure_unchanged(tmp_path):
    tool = _write_tool()
    tool._run_git_raw = _RawRecorder((1, "", "boom"))
    result = tool._run_git(tmp_path, ["commit", "-m", "x", "--", "f"])
    assert result == "Git command failed (exit code 1):\nboom"


# --- §E3-B: host-mode parity fails closed when hooks are configured ------


class _FakeExecResult:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _FakeSandboxExecution:
    def __init__(self, *args, **kwargs):
        self.calls = []

    def run(self, argv, **kwargs):
        self.calls.append(list(argv))
        return _FakeExecResult(returncode=0, stdout="HOSTOUT", stderr="")


def test_exec_host_raw_refuses_commit_when_hooks_configured(tmp_path):
    hooks = tmp_path / ".githooks"
    hooks.mkdir()
    (hooks / "pre-commit").write_text("#!/bin/sh\nexit 0\n")
    tool = _write_tool()
    tool._resolved_workspace_path = str(tmp_path)
    result = tool._exec_host_raw(tmp_path, ["commit", "-m", "m", "--", "f"])
    assert result[0] != 0
    assert "hooks" in result[2]
    assert "host" in result[2]


def test_exec_host_raw_commit_without_hooks_not_short_circuited(tmp_path, monkeypatch):
    tool = _write_tool()
    tool.session_permissions = {"git": "write"}
    tool.effective_permissions = {"git": "write"}
    tool._resolved_workspace_path = str(tmp_path)
    monkeypatch.setattr("tools.git_info_tool.SandboxedExecution", _FakeSandboxExecution)
    result = tool._exec_host_raw(tmp_path, ["commit", "-m", "m", "--", "f"])
    assert result[0] == 0
    assert result[1] == "HOSTOUT"



def test_run_git_surfaces_commit_stderr(monkeypatch):
    """§E3-A: commit stderr (hook output) must reach the returned text; other
    ops and the failure branch must be unchanged."""
    tool = _write_tool()
    tool._resolved_workspace_path = "/workspace"

    # (a) successful commit with hook output on stderr -> append it
    monkeypatch.setattr(
        tool,
        "_run_git_raw",
        lambda *a, **k: (
            0,
            "[main abc1234] msg\n 1 file changed\n",
            "[pre-commit] 1/4 import gate\n",
        ),
    )
    out = tool._run_git(Path("/workspace"), ["commit", "-m", "msg", "--", "f.txt"])
    assert "[pre-commit] 1/4 import gate" in out  # banner surfaced
    assert "[main abc1234] msg" in out  # stdout preserved
    assert out.index("[main abc1234] msg") < out.index("[pre-commit] 1/4")  # stdout first

    # (b) successful non-commit op with stderr -> stderr must NOT be appended
    monkeypatch.setattr(tool, "_run_git_raw", lambda *a, **k: (0, "clean\n", "some-noise\n"))
    out2 = tool._run_git(Path("/workspace"), ["status", "--porcelain"])
    assert out2 == "clean\n" and "some-noise" not in out2

    # (c) failure branch unchanged (stderr surfaced via the failure message)
    monkeypatch.setattr(tool, "_run_git_raw", lambda *a, **k: (1, "", "boom"))
    out3 = tool._run_git(Path("/workspace"), ["commit", "-m", "msg"])
    assert out3.startswith("Git command failed (exit code 1)")
    assert "boom" in out3

