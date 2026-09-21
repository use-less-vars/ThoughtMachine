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

