"""Unit tests for GitInfoTool's new git operations.

Covers the operations added to GitInfoTool: ``diff_cached``, ``branch_list``,
``branch_create``, ``checkout``, ``stage``, ``unstage``, plus selective commit
and the operator-managed worktree commit guard. Mock-based (fake
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
- the new operations report a trailing ``execution_mode: <mode>`` line while
  legacy operations (status/diff/log/branch) stay byte-identical (no trailer).
"""

from types import SimpleNamespace

import pytest

from tools.git_info_tool import GitInfoTool


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


def _tool(tmp_path, **params):
    """Construct a GitInfoTool wired to the workspace for path validation."""
    tool = GitInfoTool(**params)
    object.__setattr__(tool, "workspace_path", str(tmp_path))
    return tool


def _host_tool(tmp_path, **params):
    """Tool that runs git on the hardened host path (no registry workspace)."""
    return _tool(tmp_path, **params)


def _container_tool(tmp_path, manager, **params):
    """Tool wired for containerized git execution via a fake manager."""
    tool = _tool(tmp_path, **params)
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
            GitInfoTool._validate_branch_name(bad)

    def test_safe_branch_name_accepted(self):
        assert GitInfoTool._validate_branch_name("feature/x-1.2") == "feature/x-1.2"


# ---------------------------------------------------------------------------
# branch_create
# ---------------------------------------------------------------------------
class TestBranchCreate:
    def test_containerized_argv(self, tmp_path, fake_manager):
        tool = _container_tool(
            tmp_path, fake_manager, operation="branch_create", branch="feature/x"
        )
        result = tool._git_branch_create(tmp_path)

        _kind, command, kwargs = _last_manager_exec(fake_manager)
        assert command == ["git", "branch", "feature/x"]
        assert kwargs["workdir"] == "/workspace"
        assert "execution_mode: containerized" in result

    def test_host_argv_and_trailer(self, tmp_path, fake_sandbox):
        tool = _host_tool(
            tmp_path, operation="branch_create", branch="feature/x"
        )
        result = tool._git_branch_create(tmp_path)

        command = _last_sandbox_command()
        assert command[-2:] == ["branch", "feature/x"]
        assert "execution_mode: host_fallback" in result

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
        tool = _container_tool(
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
        tool = _host_tool(tmp_path, operation="diff_cached")
        result = tool._git_diff_cached(tmp_path)

        command = _last_sandbox_command()
        assert command[-4:] == ["diff", "--cached", "--no-ext-diff", "--no-textconv"]
        assert "--" not in command
        assert "execution_mode: host_fallback" in result


# ---------------------------------------------------------------------------
# branch_list
# ---------------------------------------------------------------------------
class TestBranchList:
    def test_plain_list(self, tmp_path, fake_manager):
        tool = _container_tool(tmp_path, fake_manager, operation="branch_list")
        result = tool._git_branch_list(tmp_path)

        _kind, command, _kwargs = _last_manager_exec(fake_manager)
        assert command == ["git", "branch", "--list"]
        assert "execution_mode: containerized" in result

    def test_all_branches_flag(self, tmp_path, fake_sandbox):
        tool = _host_tool(
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
        assert "execution_mode:" not in result  # commit stays byte-identical

    def test_containerized_full_commit_argv(self, tmp_path, fake_manager):
        (tmp_path / ".git").mkdir()
        tool = _container_tool(
            tmp_path, fake_manager, operation="commit", message="msg"
        )
        tool._git_commit(tmp_path)

        # Full commit = auto-stage the whole worktree -> commit (two
        # container exec calls, in that order; no index reset).
        execs = [c for c in fake_manager.calls if c[0] == "exec"]
        commands = [c[1] for c in execs]
        assert commands == [
            ["git", "add", "-A"],
            ["git", "-c", "core.hooksPath=/workspace/.githooks",
             "commit", "-m", "msg"],
        ]

    def test_full_commit_auto_stages_untracked_file(self, tmp_path, fake_manager):
        """Regression: full commit re-stages untracked files (add -A).

        Mirrors the contract tests (test_post_commit_hook_never_executes,
        test_git_add_status_diff_log_work), which commit an untracked file on
        a fresh repo via the tool without file_path: the auto-stage MUST run
        before the commit.
        """
        (tmp_path / ".git").mkdir()
        (tmp_path / "hello.txt").write_text("hi\n", encoding="utf-8")
        tool = _container_tool(
            tmp_path, fake_manager, operation="commit", message="add hello"
        )
        tool._git_commit(tmp_path)

        execs = [c for c in fake_manager.calls if c[0] == "exec"]
        commands = [c[1] for c in execs]
        assert commands[0] == ["git", "add", "-A"]  # auto-stage before commit
        assert commands[1][-3:] == ["commit", "-m", "add hello"]

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
            tmp_path, operation="commit", message="x --no-verify"
        )
        tool._git_commit(tmp_path)

        # Full commit = add -A -> commit (2 sandbox invocations); each
        # _run_git creates its own SandboxedExecution instance, so sum calls
        # across all instances; the LAST one is the commit. No index reset.
        assert sum(len(i.calls) for i in _FakeSandbox.instances) == 2
        assert all(
            "reset" not in command
            for inst in _FakeSandbox.instances
            for command, _kwargs in inst.calls
        )
        command = _last_sandbox_command()
        assert "x --no-verify" in command
        idx = command.index("commit")
        assert command[idx + 1] == "--no-verify"  # the ONE injected by hardening
        assert command.count("--no-verify") == 1  # message never re-parsed into an option
        assert command[command.index("-m") + 1] == "x --no-verify"

    def test_message_is_single_argv_element_containerized(self, tmp_path, fake_manager):
        """CONTAINERIZED path: no --no-verify anywhere; message stays one argv element."""
        (tmp_path / ".git").mkdir()
        tool = _container_tool(
            tmp_path, fake_manager, operation="commit", message="x --no-verify"
        )
        tool._git_commit(tmp_path)

        execs = [c for c in fake_manager.calls if c[0] == "exec"]
        assert len(execs) == 2  # add -A -> commit
        assert all("reset" not in c[1] for c in execs)  # no index reset
        _kind, command, _kwargs = _last_manager_exec(fake_manager)
        assert "x --no-verify" in command
        assert "--no-verify" not in command
        assert command[command.index("-m") + 1] == "x --no-verify"

    def test_host_commit_hooks_neutralized(self, tmp_path, fake_sandbox):
        (tmp_path / ".git").mkdir()
        tool = _host_tool(tmp_path, operation="commit", message="x")
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

    def test_other_write_ops_allowed_in_worktree(self, tmp_path, fake_sandbox):
        # The guard is commit-only: branch_create / stage / checkout keep
        # working in an operator-managed worktree workspace.
        self._make_worktree(tmp_path)

        tool = _host_tool(tmp_path, operation="branch_create", branch="feature/x")
        assert "Error" not in tool._git_branch_create(tmp_path)

        tool = _host_tool(tmp_path, operation="stage", file_path="a.txt")
        assert "Error" not in tool._git_stage(tmp_path)

        tool = _host_tool(tmp_path, operation="checkout", branch="feature/x")
        assert "Error" not in tool._git_checkout(tmp_path)

    def test_commit_allowed_when_git_is_directory(self, tmp_path, fake_sandbox):
        (tmp_path / ".git").mkdir()
        tool = _host_tool(tmp_path, operation="commit", message="x")
        tool._git_commit(tmp_path)

        command = _last_sandbox_command()
        assert command[command.index("commit"):] == ["commit", "--no-verify", "-m", "x"]


# ---------------------------------------------------------------------------
# execution_mode trailer reporting
# ---------------------------------------------------------------------------
class TestExecutionModeTrailer:
    @pytest.mark.parametrize(
        "op,params",
        [
            ("diff_cached", {}),
            ("branch_list", {}),
            ("branch_create", {"branch": "feature/x"}),
            ("checkout", {"branch": "feature/x"}),
            ("stage", {"file_path": "a.txt"}),
            ("unstage", {"file_path": "a.txt"}),
        ],
    )
    def test_host_fallback_trailer(self, tmp_path, fake_sandbox, op, params):
        tool = _host_tool(tmp_path, operation=op, **params)
        result = tool.execute() if False else getattr(tool, f"_git_{op}")(tmp_path)
        assert "execution_mode: host_fallback" in result

    @pytest.mark.parametrize(
        "op,params",
        [
            ("diff_cached", {}),
            ("branch_list", {}),
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


# ---------------------------------------------------------------------------
# no raw git flags on the agent-visible surface
# ---------------------------------------------------------------------------
class TestNoRawFlagsExposed:
    FORBIDDEN_KEYS = {
        "--no-verify", "-c", "--config", "core.hooksPath",
        "no_verify", "hooks_path", "git_config",
    }

    def test_schema_has_no_raw_flag_fields(self):
        field_names = set(GitInfoTool.model_fields)
        assert not (field_names & self.FORBIDDEN_KEYS)
        assert not any(name.startswith("-") for name in field_names)

    def test_unknown_flag_kwargs_rejected(self):
        with pytest.raises(Exception):
            GitInfoTool(operation="commit", message="x", no_verify=True)
        with pytest.raises(Exception):
            GitInfoTool(operation="commit", message="x", **{"--no-verify": True})

    def test_write_categories_required(self):
        for op in ("branch_create", "checkout", "stage", "unstage", "commit"):
            assert GitInfoTool.get_required_categories({"operation": op}) == ["git:write"]
        assert GitInfoTool.get_required_categories(
            {"operation": "diff_cached"}
        ) == ["git:read"]
        assert GitInfoTool.get_required_categories(
            {"operation": "branch_list"}
        ) == ["git:read"]


# ---------------------------------------------------------------------------
# regression: legacy operations stay byte-identical (no trailer)
# ---------------------------------------------------------------------------
class TestLegacyOperationsUnchanged:
    @pytest.mark.parametrize("op", ["status", "diff", "log", "branch"])
    def test_no_execution_mode_trailer(self, tmp_path, fake_sandbox, op):
        tool = _host_tool(tmp_path, operation=op)
        result = getattr(tool, f"_git_{op}")(tmp_path)

        assert result == "ok"
        assert "execution_mode:" not in result
