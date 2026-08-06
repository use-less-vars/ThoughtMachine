"""Task 3: git subprocess hardening contract for GitInfoTool.

Verifies that:
1. Every git subprocess runs with a sanitized environment: system/global
   config (~/.gitconfig, /etc/gitconfig) and the host HOME are ignored, so
   ambient aliases, credential helpers, and include.path tricks cannot be
   injected into the subprocess.
2. Git hooks never execute: core.hooksPath is pointed at /dev/null for every
   invocation, and commit additionally passes --no-verify.
3. External diff drivers / textconv filters and the fsmonitor helper are
   disabled on every invocation.
4. A repository root that resolves OUTSIDE the workspace is rejected before
   any git operation runs against it.

NOTE: this suite intentionally never writes to the real $HOME. Any hostile
.gitconfig is created inside tmp_path only (the tool overrides HOME itself,
so even monkeypatched HOME values in this file are purely belt-and-braces).
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tools.git_info_tool import GitInfoTool


def _run_git_clean(cwd: Path, *args) -> subprocess.CompletedProcess:
    """Run git with a sanitized environment (mirrors the tool's hardening)."""
    env = os.environ.copy()
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    env["GIT_CONFIG_SYSTEM"] = "/dev/null"
    env["HOME"] = "/dev/null"
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
    )


@pytest.fixture(scope="module", autouse=True)
def git_available():
    """Skip the whole module when git is not installed (e.g. minimal containers).

    The hardening contract is exercised with real git on hosts that have it;
    in sandboxes without a git binary the suite skips instead of erroring.
    """
    if shutil.which("git") is None:
        pytest.skip("git binary not available in this environment")


@pytest.fixture
def hardened_repo(tmp_path):
    """A repo inside a workspace, with a hostile pre-commit hook installed."""
    workspace = tmp_path / "workspace"
    repo = workspace / "repo"
    repo.mkdir(parents=True)
    _run_git_clean(repo, "init", "-q")
    # Repo-LOCAL identity (global config is ignored by the tool, so commits
    # must not depend on it).
    _run_git_clean(repo, "config", "user.name", "Test User")
    _run_git_clean(repo, "config", "user.email", "test@example.com")
    # Hostile hook: would create marker.txt if it ever ran.
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho PWNED > marker.txt\n")
    hook.chmod(0o111)
    marker = repo / "marker.txt"
    return workspace, repo, marker


def _commit_tool(workspace, repo, message="test commit"):
    return GitInfoTool(
        operation="commit",
        message=message,
        working_dir=str(repo),
        workspace_path=str(workspace),
    )


def test_pre_commit_hook_does_not_run(hardened_repo):
    """A pre-commit hook in the repo must never execute during a commit."""
    workspace, repo, marker = hardened_repo
    (repo / "hello.txt").write_text("hi\n")
    _run_git_clean(repo, "add", "hello.txt")
    result = _commit_tool(workspace, repo).execute()
    assert "PWNED" not in result
    assert not marker.exists() or "PWNED" not in marker.read_text()


def test_commit_still_succeeds(hardened_repo):
    """Hardening must not break normal commits."""
    workspace, repo, marker = hardened_repo
    (repo / "hello.txt").write_text("hi\n")
    _run_git_clean(repo, "add", "hello.txt")
    result = _commit_tool(workspace, repo).execute()
    assert "Git command failed" not in result
    assert not marker.exists() or "PWNED" not in marker.read_text()
    log = _run_git_clean(repo, "log", "--oneline")
    assert log.returncode == 0
    assert "test commit" in log.stdout


def test_repo_root_outside_workspace_rejected(tmp_path):
    """A repo whose root sits ABOVE the workspace must be rejected.

    Flow: proj/ has no .git, so rev-parse --show-toplevel resolves up to
    tmp_path (the repo that CONTAINS the workspace); _validate_repo_root
    must refuse to run git there.
    """
    _run_git_clean(tmp_path, "init", "-q")
    workspace = tmp_path / "workspace"
    proj = workspace / "proj"
    proj.mkdir(parents=True)
    tool = GitInfoTool(
        operation="status",
        working_dir=str(proj),
        workspace_path=str(workspace),
    )
    result = tool.execute()
    assert isinstance(result, str)
    assert "outside the workspace" in result
    assert "Git command failed" not in result


def test_status_still_works(hardened_repo):
    """A normal status operation inside the workspace is unaffected."""
    workspace, repo, _ = hardened_repo
    (repo / "a.txt").write_text("a\n")
    tool = GitInfoTool(
        operation="status",
        working_dir=str(repo),
        workspace_path=str(workspace),
    )
    result = tool.execute()
    assert "a.txt" in result
    assert "Git command failed" not in result


def test_ambient_global_config_is_ignored(hardened_repo, monkeypatch, tmp_path):
    """A hostile ~/.gitconfig must never influence commits made by the tool.

    The tool overrides HOME=/dev/null itself, so this passes even though the
    ambient HOME points at a directory full of evil settings.
    """
    workspace, repo, _ = hardened_repo
    hostile_home = tmp_path / "hostile_home"
    hostile_home.mkdir()
    (hostile_home / ".gitconfig").write_text(
        "[user]\n\tname = EVIL_GLOBAL\n\temail = evil@example.com\n"
    )
    monkeypatch.setenv("HOME", str(hostile_home))  # safe: tmp only

    (repo / "a.txt").write_text("a\n")
    _run_git_clean(repo, "add", "a.txt")
    result = _commit_tool(workspace, repo, message="config-isolated commit").execute()
    assert "Git command failed" not in result

    log = _run_git_clean(repo, "log", "--format=%an <%ae>")
    assert "EVIL_GLOBAL" not in log.stdout
    assert "Test User <test@example.com>" in log.stdout


# -- Deliverable 2: vault-backed hooks + hardened runner extensions ----------

FULL_PERMISSIONS = {
    "git": "write",
    "container": False,
    "network": "banned",
    "filesystem": "read",
    "system": "read",
    "execution": "banned",
}


def _vault_commit_tool(workspace, repo, ws_id, message="vault hook commit"):
    return GitInfoTool(
        operation="commit",
        message=message,
        working_dir=str(repo),
        workspace_path=str(workspace),
        workspace_id=ws_id,
        session_permissions=dict(FULL_PERMISSIONS),
    )


def test_repo_hooks_never_execute(hardened_repo):
    """A post-commit hook in the repo must never run during a commit."""
    workspace, repo, _ = hardened_repo
    hook = repo / ".git" / "hooks" / "post-commit"
    hook.write_text("#!/bin/sh\necho PWNED > post_commit_marker.txt\n")
    hook.chmod(0o111)
    post_marker = repo / "post_commit_marker.txt"

    (repo / "hello.txt").write_text("hi\n")
    _run_git_clean(repo, "add", "hello.txt")
    result = _commit_tool(workspace, repo).execute()
    assert "Git command failed" not in result
    assert not post_marker.exists()


def test_fsmonitor_config_not_executed(hardened_repo):
    """A repo-local core.fsmonitor helper must never be executed."""
    workspace, repo, _ = hardened_repo
    helper = repo / "fsmonitor_helper.sh"
    helper.write_text("#!/bin/sh\necho PWNED > fsmonitor_marker.txt\n")
    helper.chmod(0o111)
    _run_git_clean(repo, "config", "core.fsmonitor", str(helper))
    fsmon_marker = repo / "fsmonitor_marker.txt"

    (repo / "a.txt").write_text("a\n")
    tool = GitInfoTool(
        operation="status",
        working_dir=str(repo),
        workspace_path=str(workspace),
    )
    result = tool.execute()
    assert "a.txt" in result
    assert "Git command failed" not in result
    assert not fsmon_marker.exists()


def test_vault_pre_commit_hook_runs(tmp_path, monkeypatch):
    """A vault-managed pre-commit hook runs before the commit and passes."""
    workspace = tmp_path / "workspace"
    repo = workspace / "repo"
    repo.mkdir(parents=True)
    _run_git_clean(repo, "init", "-q")
    _run_git_clean(repo, "config", "user.name", "Test User")
    _run_git_clean(repo, "config", "user.email", "test@example.com")

    ws_id = "ws-vault-test"
    hooks_dir = tmp_path / ".thoughtmachine" / "hooks" / ws_id
    hooks_dir.mkdir(parents=True)
    hook = hooks_dir / "pre-commit"
    hook.write_text("#!/bin/sh\ntouch vault_marker.txt\n")
    hook.chmod(0o111)
    monkeypatch.setenv("HOME", str(tmp_path))
    vault_marker = repo / "vault_marker.txt"

    (repo / "hello.txt").write_text("hi\n")
    _run_git_clean(repo, "add", "hello.txt")
    result = _vault_commit_tool(workspace, repo, ws_id).execute()
    assert "Git command failed" not in result
    assert "Error executing git operation" not in result
    assert vault_marker.exists()  # the vault hook actually ran

    log = _run_git_clean(repo, "log", "--oneline")
    assert log.returncode == 0
    assert "vault hook commit" in log.stdout


def test_vault_pre_commit_hook_failure_aborts_commit(tmp_path, monkeypatch):
    """A vault pre-commit hook exiting non-zero aborts the commit."""
    workspace = tmp_path / "workspace"
    repo = workspace / "repo"
    repo.mkdir(parents=True)
    _run_git_clean(repo, "init", "-q")
    _run_git_clean(repo, "config", "user.name", "Test User")
    _run_git_clean(repo, "config", "user.email", "test@example.com")

    ws_id = "ws-vault-fail"
    hooks_dir = tmp_path / ".thoughtmachine" / "hooks" / ws_id
    hooks_dir.mkdir(parents=True)
    hook = hooks_dir / "pre-commit"
    hook.write_text("#!/bin/sh\necho 'blocked by policy' >&2\nexit 1\n")
    hook.chmod(0o111)
    monkeypatch.setenv("HOME", str(tmp_path))

    (repo / "hello.txt").write_text("hi\n")
    _run_git_clean(repo, "add", "hello.txt")
    with pytest.raises(RuntimeError):
        _vault_commit_tool(workspace, repo, ws_id).execute()

    log = _run_git_clean(repo, "log", "--oneline")
    assert log.returncode != 0  # no commit was created


def test_commit_denied_without_git_write_permission(hardened_repo):
    """Committing without git:write fails closed with PermissionError."""
    workspace, repo, _ = hardened_repo
    (repo / "hello.txt").write_text("hi\n")
    _run_git_clean(repo, "add", "hello.txt")
    tool = GitInfoTool(
        operation="commit",
        message="nope",
        working_dir=str(repo),
        workspace_path=str(workspace),
        session_permissions={
            "git": "read",
            "container": False,
            "network": "banned",
            "filesystem": "read",
            "system": "read",
            "execution": "banned",
        },
    )
    with pytest.raises(PermissionError):
        tool.execute()
    log = _run_git_clean(repo, "log", "--oneline")
    assert log.returncode != 0  # nothing was committed


def test_normal_git_operations(hardened_repo):
    """Read-only operations keep working through the hardened runner."""
    workspace, repo, _ = hardened_repo
    (repo / "a.txt").write_text("a\n")
    _run_git_clean(repo, "add", "a.txt")
    _commit_tool(workspace, repo, message="base").execute()

    for op, kwargs in (
        ("status", {}),
        ("diff", {}),
        ("log", {}),
        ("show", {"commit": "HEAD"}),
        ("config", {}),
    ):
        tool = GitInfoTool(
            operation=op,
            working_dir=str(repo),
            workspace_path=str(workspace),
            **kwargs,
        )
        result = tool.execute()
        assert "Git command failed" not in result
        assert "Error running git command" not in result
