"""Unit tests for the git operations added in the GitInfoTool/GitWriteTool split.

Covers the operations added to GitInfoTool: ``diff_cached``, ``branch_list``,
``branch_create``, ``checkout``, ``stage``, ``unstage``, plus selective commit
and the operator-managed worktree commit guard. Read operations (diff_cached,
branch_list, ...) live in ``GitInfoTool``; write operations (branch_create,
checkout, stage, unstage, commit) live in ``GitWriteTool`` (which subclasses
GitInfoTool) and require the session ``git`` permission to be at least
``'write'`` (``agent_config['session_permissions']['git']``). Mock-based (fake
SandboxedExecution / fake resource manager, mirroring
``tests/security/test_git_execution_mode.py``): no real git binary and no
docker daemon are required.

Security properties asserted per operation:
- argv is assembled from fixed lists; agent input is validated BEFORE it can
  reach git (branch names via ``_validate_branch_name``, paths via
  ``_validated_rel_paths``).
- the agent-visible param surface exposes no raw git flags (``--no-verify``,
  ``-c``/``--config``, ``core.hooksPath``).
- commit messages travel as a single argv element, never re-parsed.
- every operation reports two trailing lines ``execution_mode: <mode>`` and
  ``failure_reason: <reason-or-none>`` (legacy operations included);
  argument-validation errors keep their byte-exact form (no trailer).
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.git_info_tool import GitInfoTool, GitUnavailableError
from tools.git_write_tool import GitWriteTool

FLAG_ERROR = 'Error: git:write denied: session git_write permission is not "write"'

# Workspace the host-path helpers bind so the workspace ceiling admits host
# git; its ``allow_host_resources`` config is provisioned by the autouse
# ``_allow_host_resources_gate`` fixture below.
HOST_TEST_WS = "host-test-ws"


# ---------------------------------------------------------------------------
# Fakes (mirror tests/security/test_git_execution_mode.py)
# ---------------------------------------------------------------------------
class _FakeSandbox:
    """Stand-in for SandboxedExecution (host execution path)."""

    instances = []

    def __init__(self, **kwargs):
        _FakeSandbox.instances.append(self)
        self.calls = []

    def run(self, command, **kwargs):
        self.calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")


class _FakeManager:
    """Stand-in for the resource-container manager (containerized path)."""

    def __init__(self, mode="containerized"):
        self.mode = mode
        self.calls = []

    def ensure_resource(self, name):
        self.calls.append(("ensure_resource", name))
        if self.mode == "containerized":
            return {
                "mode": "containerized",
                "container_id": "c1",
                "status": "running",
                "image": "tm-resource-git",
                "detail": "",
            }
        return {
            "mode": self.mode,
            "container_id": None,
            "status": None,
            "image": None,
            "detail": "resource image unavailable",
        }

    def exec(self, command, **kwargs):
        self.calls.append(("exec", command, kwargs))
        return {"exit_code": 0, "stdout": "ok", "stderr": ""}


@pytest.fixture
def fake_sandbox(monkeypatch):
    """Host path: replace SandboxedExecution with a recording fake."""
    _FakeSandbox.instances.clear()
    monkeypatch.setattr("tools.git_info_tool.SandboxedExecution", _FakeSandbox)
    return _FakeSandbox


@pytest.fixture
def fake_manager():
    """Containerized path: a recording resource manager."""
    return _FakeManager(mode="containerized")


@pytest.fixture(autouse=True)
def _allow_host_resources_gate(tmp_path, monkeypatch):
    """Provision an ``allow_host_resources`` config for ``HOST_TEST_WS``.

    Host-side git is now fail-CLOSED on an unbound workspace id: with no
    workspace id there is no ``allow_host_resources`` ceiling to resolve, so the
    legacy ``workspace_path``-only host path is denied.  The host-path helpers
    (``_host_tool`` / ``_read_host_tool``) therefore bind ``HOST_TEST_WS``, and
    this fixture gives it an ``allow_host_resources: true`` config so the
    hardened host path stays exercisable.  The no-workspace deny itself is
    covered by ``TestHostFallbackNoWorkspaceIdGate``; the workspace-gate tests
    override ``THOUGHTMACHINE_VAULT_ROOT`` to exercise the deny branches.
    """
    vault = tmp_path / "_host_gate_vault"
    cfg_dir = vault / "workspaces" / HOST_TEST_WS
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.json").write_text(
        json.dumps({"allow_host_resources": True}), encoding="utf-8")
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(vault))


def _tool(tmp_path, **params):
    """Construct a GitWriteTool wired to the workspace for path validation.

    Write operations require the session git permission, so it is set
    by default unless the caller overrides agent_config explicitly. The
    resolved session/effective permission grains are also defaulted to
    git:"write" (satisfying git:read and git:write) unless the caller
    supplies them, reproducing the fully-allowed behaviour.
    """
    params.setdefault("agent_config", {"session_permissions": {"git": "write"}})
    params.setdefault("session_permissions", {"git": "write"})
    params.setdefault("effective_permissions", {"git": "write"})
    tool = GitWriteTool(**params)
    object.__setattr__(tool, "workspace_path", str(tmp_path))
    return tool


def _read_tool(tmp_path, **params):
    """Construct a GitInfoTool (read operations) wired to the workspace.

    Read operations require the session git permission, so the resolved
    session/effective permission grains are defaulted to git:"write"
    (satisfying git:read) unless the caller supplies them.
    """
    params.setdefault("session_permissions", {"git": "write"})
    params.setdefault("effective_permissions", {"git": "write"})
    tool = GitInfoTool(**params)
    object.__setattr__(tool, "workspace_path", str(tmp_path))
    return tool


def _host_tool(tmp_path, **params):
    """Write tool on the hardened host path, bound to an allowing workspace.

    ``HOST_TEST_WS`` is bound so the workspace ceiling admits host git (see the
    autouse ``_allow_host_resources_gate`` fixture); an unbound workspace id now
    denies the host path outright.
    """
    tool = _tool(tmp_path, **params)
    object.__setattr__(tool, "_resolved_workspace_id", HOST_TEST_WS)
    return tool


def _read_host_tool(tmp_path, **params):
    """Read tool on the hardened host path, bound to an allowing workspace."""
    tool = _read_tool(tmp_path, **params)
    object.__setattr__(tool, "_resolved_workspace_id", HOST_TEST_WS)
    return tool


def _container_tool(tmp_path, manager, **params):
    """Write tool wired for containerized git execution via a fake manager."""
    tool = _tool(tmp_path, **params)
    object.__setattr__(tool, "_resolved_workspace_path", str(tmp_path))
    object.__setattr__(tool, "_resolved_workspace_id", "test-ws")
    object.__setattr__(tool, "_resource_manager", manager)
    return tool


def _read_container_tool(tmp_path, manager, **params):
    """Read tool wired for containerized git execution via a fake manager."""
    tool = _read_tool(tmp_path, **params)
    object.__setattr__(tool, "_resolved_workspace_path", str(tmp_path))
    object.__setattr__(tool, "_resolved_workspace_id", "test-ws")
    object.__setattr__(tool, "_resource_manager", manager)
    return tool


def _last_sandbox_command():
    assert _FakeSandbox.instances, "no SandboxedExecution instance was created"
    assert _FakeSandbox.instances[-1].calls, "sandbox.run was never called"
    return _FakeSandbox.instances[-1].calls[-1][0]


def _last_manager_exec(manager):
    execs = [c for c in manager.calls if c[0] == "exec"]
    assert execs, "manager.exec was never called"
    return execs[-1]


# ---------------------------------------------------------------------------
# Branch-name validation (allowlist)
# ---------------------------------------------------------------------------
class TestBranchNameValidation:
    @pytest.mark.parametrize(
        "bad",
        ["-x", ".x", "a..b", "a@{b}", "a b", "", None, 42, "a--no-verify",
         "x\x00y", " a"],
    )
    def test_invalid_branch_names_rejected(self, bad):
        with pytest.raises(ValueError):
            GitWriteTool._validate_branch_name(bad)

    def test_safe_branch_name_accepted(self):
        assert GitWriteTool._validate_branch_name("feature/x-1.2") == "feature/x-1.2"


# ---------------------------------------------------------------------------
# branch_create
# ---------------------------------------------------------------------------
class TestBranchCreate:
    def test_containerized_argv(self, tmp_path, fake_manager):
        tool = _container_tool(
            tmp_path, fake_manager, operation="branch_create", branch="feature/x"
        )
        result = tool._git_branch_create(tmp_path)

        execs = [c for c in fake_manager.calls if c[0] == "exec"]
        # first exec resolves the base -> immutable SHA; the second pins the
        # branch to that SHA (never the bare `git branch <name>` form).
        assert execs[0][1] == ["git", "rev-parse", "--verify", "HEAD^{commit}"]
        assert execs[1][1] == ["git", "branch", "feature/x", "ok"]
        assert execs[1][2]["workdir"] == "/workspace"
        assert "execution_mode: containerized" in result
        assert "Created branch 'feature/x' at ok" in result

    def test_host_argv_and_trailer(self, tmp_path, fake_sandbox):
        tool = _host_tool(
            tmp_path, operation="branch_create", branch="feature/x"
        )
        result = tool._git_branch_create(tmp_path)

        # each host git invocation builds its own sandbox instance; flatten
        calls = [c[0] for inst in _FakeSandbox.instances for c in inst.calls]
        assert calls[0][-3:] == ["rev-parse", "--verify", "HEAD^{commit}"]
        assert calls[1][-3:] == ["branch", "feature/x", "ok"]
        assert "execution_mode: host" in result
        assert "working tree HEAD not moved" in result

    @pytest.mark.parametrize(
        "bad", ["-x", ".x", "a..b", "a@{b}", "a b", "a--no-verify"]
    )
    def test_invalid_branch_rejected_before_git(self, tmp_path, fake_sandbox, bad):
        tool = _host_tool(tmp_path, operation="branch_create", branch=bad)
        result = tool._git_branch_create(tmp_path)

        assert result.startswith("Error: Invalid branch name")
        assert not _FakeSandbox.instances  # no git was run (no SandboxedExecution created)

    def test_missing_branch_errors(self, tmp_path, fake_sandbox):
        tool = _host_tool(tmp_path, operation="branch_create")
        result = tool._git_branch_create(tmp_path)

        assert result == "Error: branch is required for branch_create operation"
        assert not _FakeSandbox.instances

    # --- defect A3: the branch is pinned to an immutable base SHA ---------
    def test_default_base_resolves_workspace_head(self, tmp_path, fake_sandbox):
        # base=None -> rev-parse HEAD^{commit}; branch argv pins the resolved SHA
        tool = _host_tool(tmp_path, operation="branch_create", branch="feature/x")
        calls = _shadow_run_git_raw(tool, stdout="cafe1234\n")
        result = tool._git_branch_create(tmp_path)

        assert calls[0] == ["rev-parse", "--verify", "HEAD^{commit}"]
        assert calls[1] == ["branch", "feature/x", "cafe1234"]
        assert "Created branch 'feature/x' at cafe1234 (base: HEAD)" in result
        assert "HEAD not moved" in result

    def test_explicit_base_resolved_to_sha(self, tmp_path, fake_sandbox):
        # explicit base -> rev-parse <base>^{commit}, branch pinned to the SHA
        tool = _host_tool(
            tmp_path, operation="branch_create", branch="feature/x",
            base="origin/main",
        )
        calls = _shadow_run_git_raw(tool, stdout="deadbeef\n")
        result = tool._git_branch_create(tmp_path)

        assert calls[0] == ["rev-parse", "--verify", "origin/main^{commit}"]
        assert calls[1] == ["branch", "feature/x", "deadbeef"]
        assert "(base: origin/main)" in result

    @pytest.mark.parametrize(
        "bad", ["-x", "--force", ".x", "a..b", "a@{b}", "a b"]
    )
    def test_unsafe_base_rejected_before_git(self, tmp_path, fake_sandbox, bad):
        # a caller base starting with '-' (or otherwise unsafe) must error
        # BEFORE any git call: it can never reach rev-parse as a flag.
        tool = _host_tool(
            tmp_path, operation="branch_create", branch="feature/x", base=bad
        )
        calls = _shadow_run_git_raw(tool)
        result = tool._git_branch_create(tmp_path)

        assert result.startswith("Error: Invalid branch name")
        assert calls == []
        assert not _FakeSandbox.instances

    def test_unresolvable_base_errors_without_creating_branch(
        self, tmp_path, fake_sandbox
    ):
        tool = _host_tool(tmp_path, operation="branch_create", branch="feature/x")
        calls = _shadow_run_git_raw(
            tool, stdout="", exit_code=128,
            stderr="fatal: Needed a single revision",
        )
        result = tool._git_branch_create(tmp_path)

        assert result.startswith("Git command failed (exit code 128):")
        assert "fatal: Needed a single revision" in result
        # only the rev-parse ran; no branch was created
        assert calls == [["rev-parse", "--verify", "HEAD^{commit}"]]

    def test_branch_create_argv_never_bare_name(self, tmp_path, fake_sandbox):
        tool = _host_tool(tmp_path, operation="branch_create", branch="feature/x")
        tool._git_branch_create(tmp_path)

        calls = [c[0] for inst in _FakeSandbox.instances for c in inst.calls]
        branch_cmds = [c for c in calls if "branch" in c]
        assert branch_cmds, "no branch-create command was run"
        cmd = branch_cmds[-1]
        assert cmd[-2:] == ["feature/x", "ok"]
        assert cmd != ["git", "branch", "feature/x"]

    def test_empty_resolution_errors_without_creating_branch(
        self, tmp_path, fake_sandbox
    ):
        # exit 0 but empty stdout must NEVER create a branch with an empty
        # start point: it is an explicit error instead.
        tool = _host_tool(tmp_path, operation="branch_create", branch="feature/x")
        calls = _shadow_run_git_raw(tool, stdout="", exit_code=0)
        result = tool._git_branch_create(tmp_path)

        assert result.startswith("Error:")
        assert calls == [["rev-parse", "--verify", "HEAD^{commit}"]]


# ---------------------------------------------------------------------------
# checkout
# ---------------------------------------------------------------------------
class TestCheckout:
    def test_containerized_argv(self, tmp_path, fake_manager):
        tool = _container_tool(
            tmp_path, fake_manager, operation="checkout", branch="feature/x"
        )
        result = tool._git_checkout(tmp_path)

        _kind, command, _kwargs = _last_manager_exec(fake_manager)
        assert command == ["git", "checkout", "feature/x"]
        assert "execution_mode: containerized" in result

    def test_no_b_or_double_dash_smuggled(self, tmp_path, fake_sandbox):
        tool = _host_tool(
            tmp_path, operation="checkout", branch="feature/x"
        )
        tool._git_checkout(tmp_path)

        command = _last_sandbox_command()
        assert command[-2:] == ["checkout", "feature/x"]
        assert "-b" not in command
        assert "--" not in command

    def test_invalid_branch_rejected(self, tmp_path, fake_sandbox):
        tool = _host_tool(tmp_path, operation="checkout", branch="-x")
        result = tool._git_checkout(tmp_path)

        assert result.startswith("Error: Invalid branch name")
        assert not _FakeSandbox.instances

    def test_missing_branch_errors(self, tmp_path, fake_sandbox):
        tool = _host_tool(tmp_path, operation="checkout")
        result = tool._git_checkout(tmp_path)

        assert result == "Error: branch is required for checkout operation"
        assert not _FakeSandbox.instances


# ---------------------------------------------------------------------------
# stage
# ---------------------------------------------------------------------------
class TestStage:
    def test_containerized_argv_list_paths(self, tmp_path, fake_manager):
        tool = _container_tool(
            tmp_path, fake_manager, operation="stage",
            file_path=["a.txt", "b.txt"],
        )
        result = tool._git_stage(tmp_path)

        _kind, command, _kwargs = _last_manager_exec(fake_manager)
        assert command == ["git", "add", "--", "a.txt", "b.txt"]
        assert "execution_mode: containerized" in result

    def test_single_str_path(self, tmp_path, fake_sandbox):
        tool = _host_tool(tmp_path, operation="stage", file_path="a.txt")
        tool._git_stage(tmp_path)

        command = _last_sandbox_command()
        assert command[-3:] == ["add", "--", "a.txt"]

    def test_missing_paths_errors(self, tmp_path, fake_sandbox):
        tool = _host_tool(tmp_path, operation="stage")
        result = tool._git_stage(tmp_path)

        assert result == (
            "Error: file_path is required for stage operation (at least one path)"
        )
        assert not _FakeSandbox.instances

    def test_path_outside_workspace_rejected(self, tmp_path, fake_sandbox):
        tool = _host_tool(
            tmp_path, operation="stage", file_path="../../etc/passwd"
        )
        result = tool._git_stage(tmp_path)

        assert result.startswith("Error:")
        assert not _FakeSandbox.instances

    # --- whole-tree / sweep-shaped inputs must be rejected up front: the
    # tool's contract is named files only (never `git add -A` or an
    # equivalent sweep via ".", globs, pathspec magic or option smuggling).
    def test_dot_path_rejected(self, tmp_path, fake_sandbox):
        tool = _host_tool(tmp_path, operation="stage", file_path=".")
        result = tool._git_stage(tmp_path)

        assert result.startswith("Error:")
        assert not _FakeSandbox.instances

    def test_dot_slash_resolves_to_whole_tree_rejected(
        self, tmp_path, fake_sandbox
    ):
        tool = _host_tool(tmp_path, operation="stage", file_path="./")
        result = tool._git_stage(tmp_path)

        assert result.startswith("Error:")
        assert not _FakeSandbox.instances

    def test_glob_path_rejected(self, tmp_path, fake_sandbox):
        tool = _host_tool(tmp_path, operation="stage", file_path="*.py")
        result = tool._git_stage(tmp_path)

        assert result.startswith("Error:")
        assert not _FakeSandbox.instances

    def test_recursive_glob_path_rejected(self, tmp_path, fake_sandbox):
        tool = _host_tool(tmp_path, operation="stage", file_path="**/*")
        result = tool._git_stage(tmp_path)

        assert result.startswith("Error:")
        assert not _FakeSandbox.instances

    def test_pathspec_magic_rejected(self, tmp_path, fake_sandbox):
        tool = _host_tool(tmp_path, operation="stage", file_path=":(glob)**")
        result = tool._git_stage(tmp_path)

        assert result.startswith("Error:")
        assert not _FakeSandbox.instances

    def test_option_like_path_rejected(self, tmp_path, fake_sandbox):
        # file_path="-A" must never reach git: `git add -A` would sweep.
        tool = _host_tool(tmp_path, operation="stage", file_path="-A")
        result = tool._git_stage(tmp_path)

        assert result.startswith("Error:")
        assert not _FakeSandbox.instances


# ---------------------------------------------------------------------------
# unstage
# ---------------------------------------------------------------------------
class TestUnstage:
    def test_containerized_argv(self, tmp_path, fake_manager):
        tool = _container_tool(
            tmp_path, fake_manager, operation="unstage", file_path="a.txt"
        )
        result = tool._git_unstage(tmp_path)

        _kind, command, _kwargs = _last_manager_exec(fake_manager)
        assert command == ["git", "reset", "HEAD", "--", "a.txt"]
        assert "execution_mode: containerized" in result

    def test_never_bare_reset(self, tmp_path, fake_sandbox):
        # Without paths unstage errors out: no bare `git reset` is possible.
        tool = _host_tool(tmp_path, operation="unstage")
        result = tool._git_unstage(tmp_path)

        assert result.startswith(
            "Error: file_path is required for unstage operation"
        )
        assert not _FakeSandbox.instances

        # With paths, HEAD and '--' are always present.
        tool = _host_tool(tmp_path, operation="unstage", file_path="a.txt")
        tool._git_unstage(tmp_path)
        command = _last_sandbox_command()
        assert command[-4:] == ["reset", "HEAD", "--", "a.txt"]
        assert command[-2] == "--"


# ---------------------------------------------------------------------------
# diff_cached
# ---------------------------------------------------------------------------
class TestDiffCached:
    def test_with_paths(self, tmp_path, fake_manager):
        tool = _read_container_tool(
            tmp_path, fake_manager, operation="diff_cached", file_path="a.txt"
        )
        result = tool._git_diff_cached(tmp_path)

        _kind, command, _kwargs = _last_manager_exec(fake_manager)
        assert command == [
            "git", "diff", "--cached", "--no-ext-diff", "--no-textconv",
            "--", "a.txt",
        ]
        assert "execution_mode: containerized" in result

    def test_without_paths_no_separator(self, tmp_path, fake_sandbox):
        tool = _read_host_tool(tmp_path, operation="diff_cached")
        result = tool._git_diff_cached(tmp_path)

        command = _last_sandbox_command()
        assert command[-4:] == ["diff", "--cached", "--no-ext-diff", "--no-textconv"]
        assert "--" not in command
        assert "execution_mode: host" in result


# ---------------------------------------------------------------------------
# branch_list
# ---------------------------------------------------------------------------
class TestBranchList:
    def test_plain_list(self, tmp_path, fake_manager):
        tool = _read_container_tool(tmp_path, fake_manager, operation="branch_list")
        result = tool._git_branch_list(tmp_path)

        _kind, command, _kwargs = _last_manager_exec(fake_manager)
        assert command == ["git", "branch", "--list"]
        assert "execution_mode: containerized" in result

    def test_all_branches_flag(self, tmp_path, fake_sandbox):
        tool = _read_host_tool(
            tmp_path, operation="branch_list", all_branches=True
        )
        tool._git_branch_list(tmp_path)

        command = _last_sandbox_command()
        assert command[-3:] == ["branch", "--list", "--all"]


# ---------------------------------------------------------------------------
# commit (selective + worktree guard)
# ---------------------------------------------------------------------------
class TestCommit:
    def test_containerized_selective_commit_argv(self, tmp_path, fake_manager):
        (tmp_path / ".git").mkdir()  # real repo dir -> not a worktree gitfile
        tool = _container_tool(
            tmp_path, fake_manager, operation="commit",
            message="msg", file_path="a.txt",
        )
        result = tool._git_commit(tmp_path)

        _kind, command, _kwargs = _last_manager_exec(fake_manager)
        assert command == [
            "git", "-c", "core.hooksPath=/workspace/.githooks",
            "commit", "-m", "msg", "--", "a.txt",
        ]
        assert "--no-verify" not in command
        assert "execution_mode: containerized" in result
        assert "failure_reason: none" in result

    def test_containerized_commit_without_file_path_rejected(self, tmp_path, fake_manager):
        (tmp_path / ".git").mkdir()
        tool = _container_tool(
            tmp_path, fake_manager, operation="commit", message="msg"
        )
        result = tool._git_commit(tmp_path)

        # Full-commit mode (git add -A) is removed: a commit without an
        # explicit file_path is rejected before any git subprocess runs.
        assert result == (
            "Error: file_path is required for commit operation (at least one path)"
        )
        assert not [c for c in fake_manager.calls if c[0] == "exec"]

    def test_commit_with_explicit_path_stages_untracked_file(self, tmp_path, fake_manager):
        """Regression: a commit naming an untracked file lands it in the repo.

        Mirrors the contract tests (test_post_commit_hook_never_executes,
        test_git_add_status_diff_log_work), which commit an untracked file on
        a fresh repo via the tool with an explicit file_path. The named path
        is staged explicitly first (``git add -- <path>`` -- never -A) because
        ``git commit -- <path>`` only commits files git already knows.
        """
        (tmp_path / ".git").mkdir()
        (tmp_path / "hello.txt").write_text("hi\n", encoding="utf-8")
        tool = _container_tool(
            tmp_path, fake_manager, operation="commit",
            message="add hello", file_path="hello.txt",
        )
        result = tool._git_commit(tmp_path)

        execs = [c for c in fake_manager.calls if c[0] == "exec"]
        assert len(execs) == 2  # explicit stage of the named path + commit
        assert execs[0][1] == ["git", "add", "--", "hello.txt"]
        assert "-A" not in execs[0][1]
        _kind, command, _kwargs = execs[-1]
        assert command == [
            "git", "-c", "core.hooksPath=/workspace/.githooks",
            "commit", "-m", "add hello", "--", "hello.txt",
        ]
        assert "-A" not in command
        assert "execution_mode: containerized" in result

    def test_missing_message_errors(self, tmp_path, fake_sandbox):
        (tmp_path / ".git").mkdir()
        tool = _host_tool(tmp_path, operation="commit", message=None)
        result = tool._git_commit(tmp_path)

        assert result == "Error: message is required for commit operation"
        assert not _FakeSandbox.instances

    def test_blank_message_errors(self, tmp_path, fake_sandbox):
        (tmp_path / ".git").mkdir()
        tool = _host_tool(tmp_path, operation="commit", message="   ")
        result = tool._git_commit(tmp_path)

        assert result == "Error: message is required for commit operation"
        assert not _FakeSandbox.instances

    def test_message_is_single_argv_element_host_fallback(self, tmp_path, fake_sandbox):
        """HOST fallback path: the message stays one argv element.

        Host hardening legitimately injects exactly ONE ``--no-verify``
        immediately after ``commit``; the message itself must never be
        re-parsed into an option for git.
        """
        (tmp_path / ".git").mkdir()
        tool = _host_tool(
            tmp_path, operation="commit", message="x --no-verify",
            file_path="a.txt",
        )
        tool._git_commit(tmp_path)

        # Selective commit = explicit stage of the named path (git add -- <path>)
        # + git commit -- <path>; each _run_git creates its own
        # SandboxedExecution instance, so sum calls across all instances.
        # No index reset.
        assert sum(len(i.calls) for i in _FakeSandbox.instances) == 2
        assert all(
            "reset" not in command
            for inst in _FakeSandbox.instances
            for command, _kwargs in inst.calls
        )
        commands = [
            command
            for inst in _FakeSandbox.instances
            for command, _kwargs in inst.calls
        ]
        add_cmd, commit_cmd = commands[0], commands[-1]
        assert add_cmd[add_cmd.index("add"):] == ["add", "--", "a.txt"]
        assert "-A" not in add_cmd
        command = commit_cmd
        assert "x --no-verify" in command
        idx = command.index("commit")
        assert command[idx + 1] == "--no-verify"  # the ONE injected by hardening
        assert command.count("--no-verify") == 1  # message never re-parsed into an option
        assert command[command.index("-m") + 1] == "x --no-verify"

    def test_message_is_single_argv_element_containerized(self, tmp_path, fake_manager):
        """CONTAINERIZED path: no --no-verify anywhere; message stays one argv element."""
        (tmp_path / ".git").mkdir()
        tool = _container_tool(
            tmp_path, fake_manager, operation="commit",
            message="x --no-verify", file_path="a.txt",
        )
        tool._git_commit(tmp_path)

        execs = [c for c in fake_manager.calls if c[0] == "exec"]
        assert len(execs) == 2  # selective commit: explicit stage + commit
        assert all("reset" not in c[1] for c in execs)  # no index reset
        # The stage subprocess precedes the commit; the message only ever
        # appears in the commit argv, as a single element.
        assert execs[0][1] == ["git", "add", "--", "a.txt"]
        assert "-A" not in execs[0][1]
        _kind, command, _kwargs = execs[-1]
        assert "x --no-verify" in command
        assert "--no-verify" not in command
        assert command[command.index("-m") + 1] == "x --no-verify"

    def test_host_commit_hooks_neutralized(self, tmp_path, fake_sandbox):
        (tmp_path / ".git").mkdir()
        tool = _host_tool(
            tmp_path, operation="commit", message="x", file_path="a.txt"
        )
        tool._git_commit(tmp_path)

        command = _last_sandbox_command()
        assert command.index("core.hooksPath=/dev/null") < command.index("commit")
        assert "--no-verify" in command
        assert ".githooks" not in command


class TestWorktreeCommitGuard:
    def _make_worktree(self, tmp_path):
        (tmp_path / ".git").write_text(
            "gitdir: /some/host/repo/.git/worktrees/ws\n", encoding="utf-8"
        )

    def test_commit_blocked_in_operator_worktree(self, tmp_path, fake_sandbox):
        self._make_worktree(tmp_path)
        tool = _host_tool(tmp_path, operation="commit", message="x")
        result = tool._git_commit(tmp_path)

        assert "host-side" in result
        assert "operator" in result
        assert result.startswith("Error:")
        assert not _FakeSandbox.instances

    def test_detector_true_for_gitfile(self, tmp_path):
        self._make_worktree(tmp_path)
        tool = _host_tool(tmp_path, operation="commit", message="x")
        assert tool._is_operator_managed_worktree(tmp_path) is True

    def test_detector_false_for_git_directory(self, tmp_path):
        (tmp_path / ".git").mkdir()
        tool = _host_tool(tmp_path, operation="commit", message="x")
        assert tool._is_operator_managed_worktree(tmp_path) is False

    def test_other_write_ops_denied_without_flag_in_worktree(self, tmp_path):
        # Without the session git permission the gate fires for every
        # write op, worktree or not: branch_create / stage / checkout all return
        # FLAG_ERROR before any git subprocess could run.
        self._make_worktree(tmp_path)

        tool = GitWriteTool(operation="branch_create", branch="feature/x")
        assert tool._git_branch_create(tmp_path) == FLAG_ERROR

        tool = GitWriteTool(operation="stage", file_path="a.txt")
        assert tool._git_stage(tmp_path) == FLAG_ERROR

        tool = GitWriteTool(operation="checkout", branch="feature/x")
        assert tool._git_checkout(tmp_path) == FLAG_ERROR

    def test_other_write_ops_allowed_in_worktree(self, tmp_path, fake_sandbox):
        # The worktree guard is commit-only: branch_create / stage / checkout
        # keep working in an operator-managed worktree workspace when the
        # operator flag is set.
        self._make_worktree(tmp_path)

        tool = _host_tool(tmp_path, operation="branch_create", branch="feature/x")
        assert "Error" not in tool._git_branch_create(tmp_path)

        tool = _host_tool(tmp_path, operation="stage", file_path="a.txt")
        assert "Error" not in tool._git_stage(tmp_path)

        tool = _host_tool(tmp_path, operation="checkout", branch="feature/x")
        assert "Error" not in tool._git_checkout(tmp_path)

    def test_commit_allowed_when_git_is_directory(self, tmp_path, fake_sandbox):
        (tmp_path / ".git").mkdir()
        tool = _host_tool(
            tmp_path, operation="commit", message="x", file_path="a.txt"
        )
        tool._git_commit(tmp_path)

        command = _last_sandbox_command()
        assert command[command.index("commit"):] == [
            "commit", "--no-verify", "-m", "x", "--", "a.txt",
        ]


# ---------------------------------------------------------------------------
# execution_mode trailer reporting
# ---------------------------------------------------------------------------
class TestExecutionModeTrailer:
    @pytest.mark.parametrize(
        "op,params",
        [
            ("branch_create", {"branch": "feature/x"}),
            ("checkout", {"branch": "feature/x"}),
            ("stage", {"file_path": "a.txt"}),
            ("unstage", {"file_path": "a.txt"}),
        ],
    )
    def test_host_fallback_trailer(self, tmp_path, fake_sandbox, op, params):
        tool = _host_tool(tmp_path, operation=op, **params)
        result = getattr(tool, f"_git_{op}")(tmp_path)
        assert "execution_mode: host" in result

    @pytest.mark.parametrize(
        "op,params",
        [
            ("diff_cached", {}),
            ("branch_list", {}),
        ],
    )
    def test_host_fallback_read_trailer(self, tmp_path, fake_sandbox, op, params):
        tool = _read_host_tool(tmp_path, operation=op, **params)
        result = getattr(tool, f"_git_{op}")(tmp_path)
        assert "execution_mode: host" in result

    @pytest.mark.parametrize(
        "op,params",
        [
            ("branch_create", {"branch": "feature/x"}),
            ("checkout", {"branch": "feature/x"}),
            ("stage", {"file_path": "a.txt"}),
            ("unstage", {"file_path": "a.txt"}),
        ],
    )
    def test_containerized_trailer(self, tmp_path, fake_manager, op, params):
        tool = _container_tool(tmp_path, fake_manager, operation=op, **params)
        result = getattr(tool, f"_git_{op}")(tmp_path)
        assert "execution_mode: containerized" in result

    @pytest.mark.parametrize(
        "op,params",
        [
            ("diff_cached", {}),
            ("branch_list", {}),
        ],
    )
    def test_containerized_read_trailer(self, tmp_path, fake_manager, op, params):
        tool = _read_container_tool(tmp_path, fake_manager, operation=op, **params)
        result = getattr(tool, f"_git_{op}")(tmp_path)
        assert "execution_mode: containerized" in result


# ---------------------------------------------------------------------------
# no raw git flags on the agent-visible surface
# ---------------------------------------------------------------------------
class TestNoRawFlagsExposed:
    FORBIDDEN_KEYS = {
        "--no-verify", "-c", "--config", "core.hooksPath",
        "no_verify", "hooks_path", "git_config",
    }

    def test_schema_has_no_raw_flag_fields(self):
        # Both halves of the split must keep the agent-visible surface clean:
        # GitInfoTool (read ops) and GitWriteTool (write ops, a subclass).
        for tool_cls in (GitInfoTool, GitWriteTool):
            field_names = set(tool_cls.model_fields)
            assert not (field_names & self.FORBIDDEN_KEYS)
            assert not any(name.startswith("-") for name in field_names)

    def test_unknown_flag_kwargs_rejected(self):
        with pytest.raises(Exception):
            GitWriteTool(operation="commit", message="x", no_verify=True)
        with pytest.raises(Exception):
            GitWriteTool(operation="commit", message="x", **{"--no-verify": True})

    def test_write_categories_required(self):
        for op in ("branch_create", "checkout", "stage", "unstage", "commit"):
            assert GitWriteTool.get_required_categories({"operation": op}) == ["git:write"]
        assert GitWriteTool.get_required_categories(
            {"operation": "clone"}
        ) == ["git:write", "network:outbound"]
        assert GitInfoTool.get_required_categories(
            {"operation": "diff_cached"}
        ) == ["git:read"]
        assert GitInfoTool.get_required_categories(
            {"operation": "branch_list"}
        ) == ["git:read"]
        # ``remote`` is a pure local read (configured remotes): it must NOT
        # require network:outbound.
        assert GitInfoTool.get_required_categories(
            {"operation": "remote"}
        ) == ["git:read"]


# ---------------------------------------------------------------------------
# legacy operations report the execution-mode trailer too
# ---------------------------------------------------------------------------
class TestLegacyOperationsTrailer:
    @pytest.mark.parametrize("op", ["status", "diff", "log", "branch"])
    def test_legacy_operations_report_trailer(self, tmp_path, fake_sandbox, op):
        tool = _read_host_tool(tmp_path, operation=op)
        result = getattr(tool, f"_git_{op}")(tmp_path)

        assert result.startswith("ok")
        assert "execution_mode: host" in result
        assert "failure_reason: none" in result


# ---------------------------------------------------------------------------
# workspace-config allow_host_resources gate on host-side git fallback
# ---------------------------------------------------------------------------
class TestHostFallbackWorkspaceGate:
    """Host-side git fallback is gated by the workspace vault config
    allow_host_resources flag when a resolved workspace id exists."""

    @staticmethod
    def _write_ws_config(vault, ws_id, allow):
        import json
        cfg_dir = vault / "workspaces" / ws_id
        cfg_dir.mkdir(parents=True, exist_ok=True)
        (cfg_dir / "config.json").write_text(
            json.dumps({"allow_host_resources": allow}), encoding="utf-8")

    def test_allowed_when_workspace_config_allows(
        self, tmp_path, fake_sandbox, monkeypatch
    ):
        vault = tmp_path / "vault"
        self._write_ws_config(vault, "test-ws", True)
        monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(vault))

        tool = _read_host_tool(tmp_path, operation="status")
        object.__setattr__(tool, "_resolved_workspace_id", "test-ws")
        result = tool._git_status(tmp_path)

        assert result.startswith("ok")
        assert "execution_mode: host" in result
        assert tool._last_execution_mode == "host"
        assert _FakeSandbox.instances  # host-side git actually ran

    def test_denied_when_workspace_config_disallows(
        self, tmp_path, fake_sandbox, monkeypatch
    ):
        vault = tmp_path / "vault"
        self._write_ws_config(vault, "test-ws", False)
        monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(vault))

        tool = _read_host_tool(tmp_path, operation="status")
        object.__setattr__(tool, "_resolved_workspace_id", "test-ws")
        with pytest.raises(
            RuntimeError, match="allow_host_resources: true in the workspace config"
        ):
            tool._git_status(tmp_path)
        assert tool._last_execution_mode == "unavailable"
        assert not _FakeSandbox.instances

    def test_denied_when_workspace_config_missing(
        self, tmp_path, fake_sandbox, monkeypatch
    ):
        vault = tmp_path / "vault"
        monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(vault))

        tool = _read_host_tool(tmp_path, operation="status")
        object.__setattr__(tool, "_resolved_workspace_id", "test-ws")
        with pytest.raises(
            RuntimeError, match="allow_host_resources: true in the workspace config"
        ):
            tool._git_status(tmp_path)
        assert tool._last_execution_mode == "unavailable"
        assert not _FakeSandbox.instances

    def test_denied_when_policy_helper_raises(
        self, tmp_path, fake_sandbox, monkeypatch
    ):
        """Fail-closed: a raising host-resource helper must DENY host git.

        The policy lookup is fail-closed everywhere else in this layer (see
        ``tools.host_resource_policy.workspace_allows_host_resources``, whose
        reader returns ``False`` on error, and the
        ``security_gate.get_effective_permissions`` host_bash override, which
        bans on any reader error).  A helper import/read error must therefore
        DENY host execution, never silently allow it.
        """

        def _boom(_ws_id):
            raise RuntimeError("policy helper unavailable")

        monkeypatch.setattr(
            "tools.host_resource_policy.workspace_allows_host_resources", _boom
        )

        tool = _read_host_tool(tmp_path, operation="status")
        object.__setattr__(tool, "_resolved_workspace_id", "test-ws")
        with pytest.raises(RuntimeError, match="could not be resolved"):
            tool._git_status(tmp_path)
        assert tool._last_execution_mode == "unavailable"
        assert not _FakeSandbox.instances


# ---------------------------------------------------------------------------
# host-side git is fail-CLOSED when no workspace id is bound
# ---------------------------------------------------------------------------
class TestHostFallbackNoWorkspaceIdGate:
    """Host-side git DENIES when no workspace id is bound to the session.

    With no workspace id there is no ``allow_host_resources`` ceiling to
    resolve, and this state is production-reachable (the deprecated
    ``workspace_path`` fallback and direct callers), so
    ``_host_execution_denied_reason`` must DENY the legacy host path rather than
    silently allow it.  Driven through the real ``_run_git_raw`` path.
    """

    DENY = (
        "GitReadTool: host-side git execution denied; no workspace id "
        "is bound to this session, so no host-resource policy can be "
        "resolved for host-side git"
    )

    def test_denied_when_no_workspace_id(self, tmp_path, fake_sandbox):
        # The production no-workspace shape: a workspace_path is set but no
        # workspace id is resolvable (no registry session, no executor-bound id).
        tool = _read_tool(tmp_path, operation="status")
        assert not tool._use_container_mode()  # would otherwise run host git

        with pytest.raises(RuntimeError, match="no workspace id") as exc:
            tool._git_status(tmp_path)

        assert str(exc.value) == self.DENY
        assert tool._last_execution_mode == "unavailable"
        assert not _FakeSandbox.instances  # no host-side git actually ran

    def test_denied_when_workspace_id_blank(self, tmp_path, fake_sandbox):
        tool = _read_tool(tmp_path, operation="status")
        object.__setattr__(tool, "_resolved_workspace_id", "")

        with pytest.raises(RuntimeError, match="no workspace id"):
            tool._git_status(tmp_path)
        assert tool._last_execution_mode == "unavailable"
        assert not _FakeSandbox.instances



# ---------------------------------------------------------------------------
# _git_repo_root: a missing git binary must NEVER read as "not a repository"
# ---------------------------------------------------------------------------
class TestGitRepoRootAvailability:
    """Defect A5: git-unavailable and not-a-repo were conflated."""

    @staticmethod
    def _spawn_failure(repo_root, args, timeout=30):
        raise FileNotFoundError("git")

    def test_t1_spawn_failure_reports_unavailable(self, tmp_path, monkeypatch):
        tool = _read_tool(tmp_path, operation="status")
        monkeypatch.setattr(tool, "_run_git_raw", self._spawn_failure)
        with pytest.raises(GitUnavailableError):
            tool._git_repo_root(tmp_path)
        result = tool.execute()
        assert "git executable not available" in result
        assert "Not a git repository" not in result

    def test_t1b_exit_127_reports_unavailable(self, tmp_path, monkeypatch):
        tool = _read_tool(tmp_path, operation="status")
        monkeypatch.setattr(
            tool, "_run_git_raw",
            lambda repo_root, args, timeout=30: (127, "", "git: not found"),
        )
        result = tool.execute()
        assert "git executable not available" in result
        assert "Not a git repository" not in result

    def test_t2_not_a_repo_message(self, tmp_path, monkeypatch):
        tool = _read_tool(tmp_path, operation="status")
        monkeypatch.setattr(
            tool, "_run_git_raw",
            lambda repo_root, args, timeout=30: (
                128, "",
                "fatal: not a git repository (or any of the parent directories): .git",
            ),
        )
        result = tool.execute()
        assert "Not a git repository" in result
        assert "not available" not in result

    def test_t3_happy_path_still_resolves_root(self, tmp_path, monkeypatch):
        tool = _read_tool(tmp_path, operation="status")
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.setattr(
            tool, "_run_git_raw",
            lambda repo_root, args, timeout=30: (0, str(repo), ""),
        )
        assert tool._git_repo_root(tmp_path) == repo
        result = tool.execute()
        assert "not available" not in result
        assert "Not a git repository" not in result

    def test_t4_conflated_string_absent_from_sources(self):
        repo_root = Path(__file__).resolve().parents[2]
        for rel in ("tools/git_info_tool.py", "tools/git_write_tool.py"):
            src = (repo_root / rel).read_text(encoding="utf-8")
            assert "not available or not a git repository" not in src, rel



# ---------------------------------------------------------------------------
# A2: the workspace's OWN container mount as ``working_dir`` is accepted
# ---------------------------------------------------------------------------
class TestContainerWorkingDirAccepted:
    """``working_dir`` naming the workspace's own container mount (``/workspace``
    and below) is normalised to the canonical host workspace root, so the
    container path an agent actually speaks is no longer rejected as "outside
    workspace".  Genuine escapes keep the byte-exact rejection.

    Container mode is OFF in this module: no ``session_id``/registry is bound,
    so ``_resolve_registry_workspace_info()`` returns ``(None, None)`` and
    ``_use_container_mode()`` is False.  The mapping is nonetheless observable
    because ``_normalise_working_dir`` resolves the host root through
    ``ToolBase._resolve_registry_workspace``'s deprecated ``workspace_path``
    fallback (the value the module helpers bind).  The assertions therefore pin
    (a) the mapped return value of ``_normalise_working_dir`` and (b) that
    ``execute()`` no longer rejects the alias -- not container-mode plumbing.
    """

    @staticmethod
    def _shadow_git(tool):
        calls = []
        def _raw(repo_root, args, timeout=30):
            calls.append((str(repo_root), list(args)))
            return (0, "ok", "")
        object.__setattr__(tool, "_run_git_raw", _raw)
        return calls

    @staticmethod
    def _ws_abs(tmp_path):
        return str(Path(tmp_path).resolve())

    def test_t1_mount_accepted_by_read_tool(self, tmp_path):
        (tmp_path / ".git").mkdir()
        tool = _read_tool(tmp_path, operation="status", working_dir="/workspace")
        calls = self._shadow_git(tool)
        result = tool.execute()
        assert "outside workspace" not in result
        assert calls

    def test_t2_trailing_slash_and_redundant_separators(self, tmp_path):
        (tmp_path / ".git").mkdir()
        (tmp_path / "sub" / ".git").mkdir(parents=True)
        for rawdir, expect in (
            ("/workspace/", tmp_path),
            ("/workspace//sub", tmp_path / "sub"),
        ):
            tool = _read_tool(tmp_path, operation="status", working_dir=rawdir)
            assert Path(tool._normalise_working_dir(rawdir)).resolve() == expect.resolve(), rawdir
            calls = self._shadow_git(tool)
            result = tool.execute()
            assert "outside workspace" not in result, rawdir
            assert calls, rawdir

    @pytest.mark.parametrize(
        "rawdir", ["/etc", "/tmp", "/workspace/../outside", "/outside/workspace"]
    )
    def test_t3_genuine_violations_rejected_byte_exact(self, tmp_path, rawdir):
        tool = _read_tool(tmp_path, operation="status", working_dir=rawdir)
        result = tool.execute()
        expected = (
            f"Error: Path {rawdir} is outside workspace {self._ws_abs(tmp_path)}"
        )
        assert result == expected

    @pytest.mark.parametrize("kind", ["read", "write"])
    def test_t4_read_and_write_tools_agree_on_accept(self, tmp_path, kind):
        (tmp_path / ".git").mkdir()
        if kind == "read":
            tool = _read_tool(tmp_path, operation="status", working_dir="/workspace")
        else:
            tool = _tool(tmp_path, operation="branch_create", branch="feature/x",
                         working_dir="/workspace")
        calls = self._shadow_git(tool)
        result = tool.execute()
        assert "outside workspace" not in result, kind
        assert calls, kind

    @pytest.mark.parametrize("kind", ["read", "write"])
    def test_t4b_read_and_write_tools_agree_on_reject(self, tmp_path, kind):
        if kind == "read":
            tool = _read_tool(tmp_path, operation="status", working_dir="/etc")
        else:
            tool = _tool(tmp_path, operation="branch_create", branch="feature/x",
                         working_dir="/etc")
        result = tool.execute()
        expected = f"Error: Path /etc is outside workspace {self._ws_abs(tmp_path)}"
        assert result == expected, kind

    def test_t5_host_path_working_dir_unchanged(self, tmp_path):
        (tmp_path / ".git").mkdir()
        raw = str(tmp_path)
        tool = _read_tool(tmp_path, operation="status", working_dir=raw)
        assert tool._normalise_working_dir(raw) == raw
        calls = self._shadow_git(tool)
        result = tool.execute()
        assert "outside workspace" not in result
        assert calls

    def test_t6_normalise_working_dir_matrix(self, tmp_path):
        tool = _read_tool(tmp_path, operation="status")
        root = Path(self._ws_abs(tmp_path))
        sub = (tmp_path / "sub").resolve()
        assert Path(tool._normalise_working_dir("/workspace")).resolve() == root
        assert Path(tool._normalise_working_dir("/workspace/sub")).resolve() == sub
        assert tool._normalise_working_dir("/etc") == "/etc"
        assert tool._normalise_working_dir("/workspaceX") == "/workspaceX"
        assert tool._normalise_working_dir("/workspace/../outside") == "/workspace/../outside"


# ---------------------------------------------------------------------------
# show honours file scope + optional line range (defect A1)
# ---------------------------------------------------------------------------
def _shadow_run_git_raw(tool, stdout="ok", exit_code=0, stderr=""):
    """Shadow the git seam on ``tool``, recording argv per invocation."""
    calls = []

    def fake(repo_root, args, timeout=30):
        calls.append(list(args))
        return (exit_code, stdout, stderr)

    object.__setattr__(tool, "_run_git_raw", fake)
    return calls


class TestShowHonoursScope:
    """`show` must honour file_path / line range, never silently drop them."""

    UNSCOPED = ["show", "--no-ext-diff", "--no-textconv", "HEAD"]

    def test_file_path_scopes_argv(self, tmp_path):
        tool = _read_host_tool(
            tmp_path, operation="show", commit="HEAD", file_path="a.txt"
        )
        calls = _shadow_run_git_raw(tool)
        tool._git_show(tmp_path)
        assert calls == [self.UNSCOPED + ["--", "a.txt"]]
        assert calls[0] != self.UNSCOPED

    def test_no_scope_argv_unchanged(self, tmp_path):
        tool = _read_host_tool(tmp_path, operation="show")
        calls = _shadow_run_git_raw(tool)
        tool._git_show(tmp_path)
        assert calls == [self.UNSCOPED]

    def test_format_only_argv_unchanged(self, tmp_path):
        tool = _read_host_tool(tmp_path, operation="show", format="%H %s")
        calls = _shadow_run_git_raw(tool)
        tool._git_show(tmp_path)
        assert calls == [
            ["show", "--no-ext-diff", "--no-textconv", "--format=%H %s", "HEAD"]
        ]

    def test_range_reads_scoped_blob_and_slices(self, tmp_path):
        blob = "L1\nL2\nL3\nL4\nL5\n"
        tool = _read_host_tool(
            tmp_path, operation="show", commit="HEAD",
            file_path="a.txt", line_start=2, line_end=4,
        )
        calls = _shadow_run_git_raw(tool, stdout=blob)
        out = tool._git_show(tmp_path)
        # the range is honoured via the scoped blob <commit>:<path>
        assert calls == [
            ["show", "--no-ext-diff", "--no-textconv", "HEAD:a.txt"]
        ]
        assert "L2" in out and "L4" in out
        assert "L1" not in out and "L5" not in out
        assert "L1\nL2\nL3\nL4\nL5" not in out  # not the whole blob

    def test_range_without_file_path_errors_before_git(self, tmp_path):
        tool = _read_host_tool(
            tmp_path, operation="show", line_start=1, line_end=2
        )
        calls = _shadow_run_git_raw(tool)
        out = tool._git_show(tmp_path)
        assert out.startswith("Error:")
        assert "line_start" in out or "line_end" in out
        assert calls == []  # no git run -> not a silent success

    def test_invalid_range_errors_before_git(self, tmp_path):
        tool = _read_host_tool(
            tmp_path, operation="show", file_path="a.txt",
            line_start=5, line_end=2,
        )
        calls = _shadow_run_git_raw(tool)
        out = tool._git_show(tmp_path)
        assert out.startswith("Error:")
        assert calls == []

    def test_path_matching_nothing_is_scoped_not_full_patch(self, tmp_path):
        tool = _read_host_tool(
            tmp_path, operation="show", commit="HEAD", file_path="nope.txt"
        )
        calls = _shadow_run_git_raw(tool, stdout="")
        tool._git_show(tmp_path)
        assert calls == [self.UNSCOPED + ["--", "nope.txt"]]
        assert calls[0] != self.UNSCOPED  # never the full unscoped patch


    def test_ranged_show_git_failure_is_error_not_sliced(self, tmp_path):
        tool = _read_host_tool(
            tmp_path, operation="show", commit="HEAD",
            file_path="a.txt", line_start=2, line_end=4,
        )
        calls = _shadow_run_git_raw(
            tool, stdout="SHOULD-NOT-LEAK", exit_code=128,
            stderr="fatal: bad object HEAD:a.txt",
        )
        out = tool._git_show(tmp_path)
        # the ranged invocation did run ...
        assert calls == [
            ["show", "--no-ext-diff", "--no-textconv", "HEAD:a.txt"]
        ]
        # ... and its failure surfaced as an error, never sliced as content
        assert out.startswith("Git command failed")
        assert "fatal: bad object HEAD:a.txt" in out
        assert "SHOULD-NOT-LEAK" not in out




# ---------------------------------------------------------------------------
# Literal acceptance: the 5 new read operation names must be constructible
# ---------------------------------------------------------------------------
class TestNewOperationNamesAccepted:
    @pytest.mark.parametrize(
        "name", ["rev_parse", "show_ref", "for_each_ref", "ls_tree", "cat_file"]
    )
    def test_literal_accepts_new_operation(self, tmp_path, name):
        tool = _read_host_tool(tmp_path, operation=name)
        assert tool.operation == name


# ---------------------------------------------------------------------------
# rev_parse
# ---------------------------------------------------------------------------
class TestRevParse:
    def test_default_argv(self, tmp_path):
        tool = _read_host_tool(tmp_path, operation="rev_parse")
        calls = _shadow_run_git_raw(tool, stdout="abc123\n")
        out = tool._git_rev_parse(tmp_path)
        assert calls == [["rev-parse", "HEAD"]]
        assert "abc123" in out

    def test_abbrev_ref_and_explicit_ref(self, tmp_path):
        tool = _read_host_tool(
            tmp_path, operation="rev_parse", abbrev=True, ref="main"
        )
        calls = _shadow_run_git_raw(tool, stdout="main\n")
        tool._git_rev_parse(tmp_path)
        assert calls == [["rev-parse", "--abbrev-ref", "main"]]

    @pytest.mark.parametrize("bad", ["-x", "--abbrev-ref", "a b", "", "x\ny", " a", None])
    def test_invalid_ref_rejected_before_git(self, tmp_path, bad):
        tool = _read_host_tool(tmp_path, operation="rev_parse", ref=bad)
        calls = _shadow_run_git_raw(tool)
        out = tool._git_rev_parse(tmp_path)
        assert out.startswith("Error:")
        assert calls == []

    def test_failure_surfaced(self, tmp_path):
        tool = _read_host_tool(tmp_path, operation="rev_parse", ref="deadbeef")
        calls = _shadow_run_git_raw(
            tool, stdout="", exit_code=128,
            stderr="fatal: ambiguous argument 'deadbeef'",
        )
        out = tool._git_rev_parse(tmp_path)
        assert calls == [["rev-parse", "deadbeef"]]
        assert out.startswith("Git command failed")
        assert "ambiguous argument" in out


# ---------------------------------------------------------------------------
# show_ref
# ---------------------------------------------------------------------------
class TestShowRef:
    def test_default_argv(self, tmp_path):
        tool = _read_host_tool(tmp_path, operation="show_ref")
        calls = _shadow_run_git_raw(tool, stdout="abc123 refs/heads/main\n")
        out = tool._git_show_ref(tmp_path)
        assert calls == [["show-ref"]]
        assert "refs/heads/main" in out

    def test_pattern_argv(self, tmp_path):
        tool = _read_host_tool(
            tmp_path, operation="show_ref", pattern="refs/heads/*"
        )
        calls = _shadow_run_git_raw(tool)
        tool._git_show_ref(tmp_path)
        assert calls == [["show-ref", "refs/heads/*"]]

    def test_empty_match_is_benign(self, tmp_path):
        tool = _read_host_tool(
            tmp_path, operation="show_ref", pattern="refs/heads/*"
        )
        calls = _shadow_run_git_raw(tool, stdout="", exit_code=1)
        out = tool._git_show_ref(tmp_path)
        assert "No matching refs." in out
        assert "Git command failed" not in out

    def test_real_failure_surfaced(self, tmp_path):
        tool = _read_host_tool(tmp_path, operation="show_ref")
        calls = _shadow_run_git_raw(
            tool, stdout="whatever", exit_code=128,
            stderr="fatal: not a git repository",
        )
        out = tool._git_show_ref(tmp_path)
        assert out.startswith("Git command failed")
        assert "fatal: not a git repository" in out
        assert "whatever" not in out

    @pytest.mark.parametrize(
        "bad", ["a b", ";", "|", "&", "$", "`", ">", "<", "x\ny", "-x"]
    )
    def test_invalid_pattern_rejected_before_git(self, tmp_path, bad):
        tool = _read_host_tool(tmp_path, operation="show_ref", pattern=bad)
        calls = _shadow_run_git_raw(tool)
        out = tool._git_show_ref(tmp_path)
        assert out.startswith("Error:")
        assert calls == []


# ---------------------------------------------------------------------------
# for_each_ref
# ---------------------------------------------------------------------------
class TestForEachRef:
    def test_default_argv(self, tmp_path):
        tool = _read_host_tool(tmp_path, operation="for_each_ref")
        calls = _shadow_run_git_raw(tool, stdout="abc123 refs/heads/main\n")
        out = tool._git_for_each_ref(tmp_path)
        assert calls == [["for-each-ref"]]
        assert "refs/heads/main" in out

    def test_format_and_prefix_argv(self, tmp_path):
        tool = _read_host_tool(
            tmp_path, operation="for_each_ref",
            format="%(refname:short) %(objectname)", prefix="refs/heads/",
        )
        calls = _shadow_run_git_raw(tool)
        tool._git_for_each_ref(tmp_path)
        assert calls == [
            ["for-each-ref",
             "--format=%(refname:short) %(objectname)", "refs/heads/"]
        ]

    def test_empty_output_benign(self, tmp_path):
        tool = _read_host_tool(tmp_path, operation="for_each_ref")
        calls = _shadow_run_git_raw(tool, stdout="", exit_code=0)
        out = tool._git_for_each_ref(tmp_path)
        assert "Git command failed" not in out

    def test_valid_format_accepted(self, tmp_path):
        fmt = "%(refname) %(objectname)"
        assert GitInfoTool._validate_for_each_ref_format(fmt) == fmt

    @pytest.mark.parametrize(
        "bad",
        ["$(bogus)", "no-token-here %(refname", "%x", "%(subject) %(bad)", "a\nb"],
    )
    def test_invalid_format_rejected_before_git(self, tmp_path, bad):
        tool = _read_host_tool(tmp_path, operation="for_each_ref", format=bad)
        calls = _shadow_run_git_raw(tool)
        out = tool._git_for_each_ref(tmp_path)
        assert out.startswith("Error:")
        assert calls == []

    @pytest.mark.parametrize("bad", ["a b", "$(x)", "refs;heads", "-x"])
    def test_invalid_prefix_rejected_before_git(self, tmp_path, bad):
        tool = _read_host_tool(tmp_path, operation="for_each_ref", prefix=bad)
        calls = _shadow_run_git_raw(tool)
        out = tool._git_for_each_ref(tmp_path)
        assert out.startswith("Error:")
        assert calls == []


# ---------------------------------------------------------------------------
# ls_tree
# ---------------------------------------------------------------------------
class TestLsTree:
    def test_default_argv(self, tmp_path):
        tool = _read_host_tool(tmp_path, operation="ls_tree")
        calls = _shadow_run_git_raw(tool, stdout="100644 blob abc\tf.txt\n")
        out = tool._git_ls_tree(tmp_path)
        assert calls == [["ls-tree", "HEAD"]]

    def test_recursive_treeish_and_path_argv(self, tmp_path):
        tool = _read_host_tool(
            tmp_path, operation="ls_tree", treeish="main",
            recursive=True, path="src",
        )
        calls = _shadow_run_git_raw(tool)
        tool._git_ls_tree(tmp_path)
        assert calls == [["ls-tree", "-r", "main", "--", "src"]]

    def test_failure_surfaced(self, tmp_path):
        tool = _read_host_tool(tmp_path, operation="ls_tree", treeish="nope")
        calls = _shadow_run_git_raw(
            tool, stdout="", exit_code=128,
            stderr="fatal: Not a valid object name nope",
        )
        out = tool._git_ls_tree(tmp_path)
        assert calls == [["ls-tree", "nope"]]
        assert out.startswith("Git command failed")
        assert "Not a valid object name" in out

    @pytest.mark.parametrize("bad", ["-r", "a b", "", "x\ny"])
    def test_invalid_treeish_rejected_before_git(self, tmp_path, bad):
        tool = _read_host_tool(tmp_path, operation="ls_tree", treeish=bad)
        calls = _shadow_run_git_raw(tool)
        out = tool._git_ls_tree(tmp_path)
        assert out.startswith("Error:")
        assert calls == []


# ---------------------------------------------------------------------------
# cat_file
# ---------------------------------------------------------------------------
class TestCatFile:
    def test_default_pretty_print_argv(self, tmp_path):
        tool = _read_host_tool(
            tmp_path, operation="cat_file", object="HEAD:a.txt"
        )
        calls = _shadow_run_git_raw(tool, stdout="hello\n")
        out = tool._git_cat_file(tmp_path)
        assert calls == [["cat-file", "-p", "HEAD:a.txt"]]
        assert "hello" in out

    def test_explicit_type_argv(self, tmp_path):
        tool = _read_host_tool(
            tmp_path, operation="cat_file", object="abc123", type="commit"
        )
        calls = _shadow_run_git_raw(tool)
        tool._git_cat_file(tmp_path)
        assert calls == [["cat-file", "commit", "abc123"]]

    def test_missing_object_errors_before_git(self, tmp_path):
        tool = _read_host_tool(tmp_path, operation="cat_file")
        calls = _shadow_run_git_raw(tool)
        out = tool._git_cat_file(tmp_path)
        assert out.startswith("Error:")
        assert calls == []

    @pytest.mark.parametrize("bad", ["nope", "BLOB", "-p", "blobby"])
    def test_invalid_type_rejected_before_git(self, tmp_path, bad):
        tool = _read_host_tool(
            tmp_path, operation="cat_file", object="abc", type=bad
        )
        calls = _shadow_run_git_raw(tool)
        out = tool._git_cat_file(tmp_path)
        assert out.startswith("Error:")
        assert calls == []

    def test_unknown_object_failure_surfaced(self, tmp_path):
        tool = _read_host_tool(tmp_path, operation="cat_file", object="nope")
        calls = _shadow_run_git_raw(
            tool, stdout="", exit_code=128,
            stderr="fatal: Not a valid object name nope",
        )
        out = tool._git_cat_file(tmp_path)
        assert calls == [["cat-file", "-p", "nope"]]
        assert out.startswith("Git command failed")
        assert "Not a valid object name" in out


# ---------------------------------------------------------------------------
# merge_base / stash_list / reflog / show_file (B2)
# ---------------------------------------------------------------------------
class TestNewOperationNamesAcceptedB2:
    @pytest.mark.parametrize(
        "name", ["merge_base", "stash_list", "reflog", "show_file"]
    )
    def test_literal_accepts_new_operation(self, tmp_path, name):
        tool = _read_host_tool(tmp_path, operation=name)
        assert tool.operation == name


class TestLogRevisionHonoured:
    BASE = [
        "log", "--no-ext-diff", "--no-textconv", "--max-count=50", "--oneline"
    ]

    def test_no_branch_argv_unchanged(self, tmp_path):
        tool = _read_host_tool(tmp_path, operation="log")
        calls = _shadow_run_git_raw(tool)
        tool._git_log(tmp_path)
        assert calls == [list(self.BASE)]

    def test_branch_inserted_as_revision(self, tmp_path):
        tool = _read_host_tool(tmp_path, operation="log", branch="main")
        calls = _shadow_run_git_raw(tool)
        tool._git_log(tmp_path)
        assert calls == [self.BASE + ["main"]]

    def test_branch_precedes_path_separator(self, tmp_path):
        tool = _read_host_tool(
            tmp_path, operation="log", branch="main", file_path="a.txt"
        )
        calls = _shadow_run_git_raw(tool)
        tool._git_log(tmp_path)
        assert calls == [self.BASE + ["main", "--", "a.txt"]]

    def test_unknown_revision_surfaced_as_error(self, tmp_path):
        tool = _read_host_tool(tmp_path, operation="log", branch="nope")
        calls = _shadow_run_git_raw(
            tool, stdout="", exit_code=128, stderr="fatal: bad revision 'nope'"
        )
        out = tool._git_log(tmp_path)
        assert calls == [self.BASE + ["nope"]]
        assert out.startswith("Git command failed")
        assert "bad revision" in out

    @pytest.mark.parametrize("bad", ["-x", "a b", "\tx", "x\ny"])
    def test_invalid_branch_rejected_before_git(self, tmp_path, bad):
        tool = _read_host_tool(tmp_path, operation="log", branch=bad)
        calls = _shadow_run_git_raw(tool)
        out = tool._git_log(tmp_path)
        assert out.startswith("Error:")
        assert calls == []

    def test_max_count_clamped_high(self, tmp_path):
        tool = _read_host_tool(tmp_path, operation="log", max_count=99999)
        calls = _shadow_run_git_raw(tool)
        tool._git_log(tmp_path)
        assert calls[0][3] == "--max-count=1000"

    def test_max_count_clamped_low(self, tmp_path):
        tool = _read_host_tool(tmp_path, operation="log", max_count=0)
        calls = _shadow_run_git_raw(tool)
        tool._git_log(tmp_path)
        assert calls[0][3] == "--max-count=1"

    def test_since_until_still_honoured(self, tmp_path):
        tool = _read_host_tool(
            tmp_path, operation="log", since="2020-01-01", until="2021-01-01"
        )
        calls = _shadow_run_git_raw(tool)
        tool._git_log(tmp_path)
        assert "--since=2020-01-01" in calls[0]
        assert "--until=2021-01-01" in calls[0]


class TestMergeBase:
    def test_default_argv(self, tmp_path):
        tool = _read_host_tool(tmp_path, operation="merge_base", a="main", b="dev")
        calls = _shadow_run_git_raw(tool, stdout="abc123\n")
        out = tool._git_merge_base(tmp_path)
        assert calls == [["merge-base", "main", "dev"]]
        assert "abc123" in out

    def test_is_ancestor_argv(self, tmp_path):
        tool = _read_host_tool(
            tmp_path, operation="merge_base", a="main", b="dev", is_ancestor=True
        )
        calls = _shadow_run_git_raw(tool, stdout="")
        tool._git_merge_base(tmp_path)
        assert calls == [["merge-base", "--is-ancestor", "main", "dev"]]

    def test_no_common_ancestor_exit1_meaningful(self, tmp_path):
        tool = _read_host_tool(tmp_path, operation="merge_base", a="main", b="dev")
        calls = _shadow_run_git_raw(tool, stdout="", exit_code=1)
        out = tool._git_merge_base(tmp_path)
        assert calls == [["merge-base", "main", "dev"]]
        assert "No common ancestor (no merge base)." in out
        assert out.strip() != ""

    def test_is_ancestor_true_exit0_positive(self, tmp_path):
        tool = _read_host_tool(
            tmp_path, operation="merge_base", a="main", b="dev", is_ancestor=True
        )
        calls = _shadow_run_git_raw(tool, stdout="", exit_code=0)
        out = tool._git_merge_base(tmp_path)
        assert "is an ancestor of" in out

    def test_is_ancestor_false_exit1_negative(self, tmp_path):
        tool = _read_host_tool(
            tmp_path, operation="merge_base", a="main", b="dev", is_ancestor=True
        )
        calls = _shadow_run_git_raw(tool, stdout="", exit_code=1)
        out = tool._git_merge_base(tmp_path)
        assert "is not an ancestor of" in out
        assert out.strip() != ""

    def test_other_nonzero_exit_is_standard_error(self, tmp_path):
        tool = _read_host_tool(tmp_path, operation="merge_base", a="main", b="dev")
        calls = _shadow_run_git_raw(
            tool, stdout="", exit_code=128,
            stderr="fatal: Not a valid object name main",
        )
        out = tool._git_merge_base(tmp_path)
        assert out.startswith("Git command failed")
        assert "Not a valid object name" in out

    @pytest.mark.parametrize(
        "a,b",
        [(None, "dev"), ("main", None), ("", "dev"), ("main", "")],
    )
    def test_missing_refs_error_before_git(self, tmp_path, a, b):
        tool = _read_host_tool(tmp_path, operation="merge_base", a=a, b=b)
        calls = _shadow_run_git_raw(tool)
        out = tool._git_merge_base(tmp_path)
        assert out.startswith("Error:")
        assert calls == []

    @pytest.mark.parametrize("bad", ["-x", "a b", "x\ny"])
    def test_invalid_ref_rejected_before_git(self, tmp_path, bad):
        tool = _read_host_tool(tmp_path, operation="merge_base", a=bad, b="dev")
        calls = _shadow_run_git_raw(tool)
        out = tool._git_merge_base(tmp_path)
        assert out.startswith("Error:")
        assert calls == []


class TestStashList:
    def test_argv(self, tmp_path):
        tool = _read_host_tool(tmp_path, operation="stash_list")
        calls = _shadow_run_git_raw(tool, stdout="stash@{0}: WIP\n")
        out = tool._git_stash_list(tmp_path)
        assert calls == [["stash", "list"]]
        assert "WIP" in out

    def test_empty_is_benign(self, tmp_path):
        tool = _read_host_tool(tmp_path, operation="stash_list")
        calls = _shadow_run_git_raw(tool, stdout="", exit_code=0)
        out = tool._git_stash_list(tmp_path)
        assert calls == [["stash", "list"]]
        assert "No stashes." in out
        assert not out.startswith("Git command failed")

    def test_nonzero_is_standard_error(self, tmp_path):
        tool = _read_host_tool(tmp_path, operation="stash_list")
        calls = _shadow_run_git_raw(
            tool, stdout="", exit_code=128, stderr="fatal: not a git repository"
        )
        out = tool._git_stash_list(tmp_path)
        assert out.startswith("Git command failed")
        assert "not a git repository" in out


class TestReflog:
    def test_default_argv(self, tmp_path):
        tool = _read_host_tool(tmp_path, operation="reflog")
        calls = _shadow_run_git_raw(tool, stdout="abc HEAD@{0}: commit\n")
        out = tool._git_reflog(tmp_path)
        assert calls == [["reflog", "-n20", "HEAD"]]
        assert "HEAD@{0}" in out

    def test_custom_ref_and_limit(self, tmp_path):
        tool = _read_host_tool(tmp_path, operation="reflog", ref="main", limit=5)
        calls = _shadow_run_git_raw(tool)
        tool._git_reflog(tmp_path)
        assert calls == [["reflog", "-n5", "main"]]

    def test_limit_clamped_high(self, tmp_path):
        tool = _read_host_tool(tmp_path, operation="reflog", limit=5000)
        calls = _shadow_run_git_raw(tool)
        tool._git_reflog(tmp_path)
        assert calls == [["reflog", "-n1000", "HEAD"]]

    def test_limit_clamped_low(self, tmp_path):
        tool = _read_host_tool(tmp_path, operation="reflog", limit=0)
        calls = _shadow_run_git_raw(tool)
        tool._git_reflog(tmp_path)
        assert calls == [["reflog", "-n1", "HEAD"]]

    def test_empty_is_benign(self, tmp_path):
        tool = _read_host_tool(tmp_path, operation="reflog")
        calls = _shadow_run_git_raw(tool, stdout="", exit_code=0)
        out = tool._git_reflog(tmp_path)
        assert "No reflog entries." in out
        assert not out.startswith("Git command failed")

    def test_bad_ref_nonzero_is_standard_error(self, tmp_path):
        tool = _read_host_tool(tmp_path, operation="reflog", ref="nope")
        calls = _shadow_run_git_raw(
            tool, stdout="", exit_code=128,
            stderr="fatal: ambiguous argument 'nope'",
        )
        out = tool._git_reflog(tmp_path)
        assert out.startswith("Git command failed")
        assert "ambiguous argument" in out

    @pytest.mark.parametrize("bad", ["-x", "a b", "x\ny"])
    def test_invalid_ref_rejected_before_git(self, tmp_path, bad):
        tool = _read_host_tool(tmp_path, operation="reflog", ref=bad)
        calls = _shadow_run_git_raw(tool)
        out = tool._git_reflog(tmp_path)
        assert out.startswith("Error:")
        assert calls == []


class TestShowFile:
    def test_default_argv(self, tmp_path):
        tool = _read_host_tool(tmp_path, operation="show_file", path="a.txt")
        calls = _shadow_run_git_raw(tool, stdout="hello\n")
        out = tool._git_show_file(tmp_path)
        assert calls == [["show", "--no-ext-diff", "--no-textconv", "HEAD:a.txt"]]
        assert "hello" in out

    def test_custom_ref(self, tmp_path):
        tool = _read_host_tool(
            tmp_path, operation="show_file", ref="main", path="a.txt"
        )
        calls = _shadow_run_git_raw(tool, stdout="x\n")
        tool._git_show_file(tmp_path)
        assert calls == [
            ["show", "--no-ext-diff", "--no-textconv", "main:a.txt"]
        ]

    def test_missing_ref_error_before_git(self, tmp_path):
        tool = _read_host_tool(tmp_path, operation="show_file", ref=None, path="a.txt")
        calls = _shadow_run_git_raw(tool)
        out = tool._git_show_file(tmp_path)
        assert out.startswith("Error:")
        assert calls == []

    def test_missing_path_error_before_git(self, tmp_path):
        tool = _read_host_tool(tmp_path, operation="show_file")
        calls = _shadow_run_git_raw(tool)
        out = tool._git_show_file(tmp_path)
        assert out.startswith("Error:")
        assert calls == []

    def test_path_escaping_workspace_rejected(self, tmp_path):
        tool = _read_host_tool(
            tmp_path, operation="show_file", path="../outside.txt"
        )
        calls = _shadow_run_git_raw(tool)
        out = tool._git_show_file(tmp_path)
        assert out.startswith("Error:")
        assert calls == []

    def test_not_tracked_at_ref_is_error(self, tmp_path):
        tool = _read_host_tool(tmp_path, operation="show_file", path="a.txt")
        calls = _shadow_run_git_raw(
            tool, stdout="", exit_code=128,
            stderr="fatal: path 'a.txt' does not exist in 'HEAD'",
        )
        out = tool._git_show_file(tmp_path)
        assert calls == [["show", "--no-ext-diff", "--no-textconv", "HEAD:a.txt"]]
        assert out.startswith("Git command failed")
        assert "does not exist" in out

    def test_empty_file_is_not_error(self, tmp_path):
        tool = _read_host_tool(tmp_path, operation="show_file", path="a.txt")
        calls = _shadow_run_git_raw(tool, stdout="", exit_code=0)
        out = tool._git_show_file(tmp_path)
        assert calls == [["show", "--no-ext-diff", "--no-textconv", "HEAD:a.txt"]]
        assert not out.startswith("Git command failed")
        assert not out.startswith("Error:")

