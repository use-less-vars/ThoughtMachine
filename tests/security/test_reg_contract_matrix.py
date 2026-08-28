"""Regression contract matrix: git hardening + MCP connect hardening.

This module pins down the end-to-end *contracts* hardened in the D3 security
pass:

* GitInfoTool neutralizes repository-local hooks (post-commit, pre-commit),
  fsmonitor and textconv/external diff drivers on the HOST fallback path,
  and rejects non-whitelisted clone transports (``ext::`` and friends)
  before any subprocess can run. In container mode (resource container =
  security boundary) repo-local hooks are allowed to run -- see the
  container-mode companion tests below.
* MCPServerConnect refuses shell-metacharacter payloads (the
  SandboxedExecution ValueError propagates fail-closed) and never executes
  anything for unregistered servers.

It complements (and deliberately overlaps with) ``test_git_hardening.py`` and
``test_mcp_server_connect.py`` by exercising the same guarantees through a
single matrix of regression tests, each with explicit 7-key permission dicts
so the permission gate is always exercised.

NOTE: ``tests/security`` has no ``__init__.py``, so pytest imports these
modules at top level; the ``git_available`` fixture is therefore defined
locally (mirroring ``test_git_hardening.py``) instead of being imported.
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import tools.mcp_server_connect as mcp_server_connect
from tools.git_info_tool import GitInfoTool


# ---------------------------------------------------------------------------
# Shared helpers (mirror test_git_hardening.py / test_mcp_server_connect.py)
# ---------------------------------------------------------------------------

# 7-key permission dicts. The gate only runs when session_permissions is
# present; network:"write" satisfies the network:outbound atomic check so the
# clone URL validation is actually reached in the clone test below.
FULL_PERMISSIONS = {
    "git": "write",
    "container": False,
    "network": "write",
    "filesystem": "read",
    "system": "read",
    "execution": "banned",
    "mcp": "banned",
}

MCP_PERMISSIONS = {
    "mcp": "connect",
    "container": False,
    "network": "banned",
    "filesystem": "read",
    "system": "read",
    "git": "read",
    "execution": "banned",
}


def _run_git_clean(cwd, *args):
    """Run git with a fully sanitized environment (mirrors test_git_hardening)."""
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env={
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "HOME": "/dev/null",
        },
        capture_output=True,
        text=True,
    )


def _init_repo(repo):
    """git init plus repo-local identity so commits run in any environment."""
    repo.mkdir(parents=True, exist_ok=True)
    init = _run_git_clean(repo, "init", "-q")
    assert init.returncode == 0, init.stderr
    name = _run_git_clean(repo, "config", "user.name", "Test User")
    assert name.returncode == 0, name.stderr
    email = _run_git_clean(repo, "config", "user.email", "test@example.com")
    assert email.returncode == 0, email.stderr


def _write_hook(hook_path, marker):
    """Plant a repository-local hook that touches a marker file if run."""
    hook_path.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook_path.chmod(0o755)


def _write_registry(tmp_path, servers):
    """Write an MCP server registry file and return its path."""
    registry = tmp_path / "mcp_servers.json"
    registry.write_text(json.dumps({"servers": servers}))
    return registry


# Metacharacter-free mock MCP server: reads one stdin line, verifies it is an
# initialize request, and answers with a valid initialize result.
_MOCK_SERVER_SCRIPT = (
    "import sys, json\n"
    "line = sys.stdin.readline()\n"
    "if '\"method\": \"initialize\"' not in line:\n"
    "    sys.stderr.write('MOCK_ERROR: expected initialize method, got: %r\\n' % line)\n"
    "    sys.exit(3)\n"
    "print(json.dumps({'jsonrpc': '2.0', 'id': 1, 'result': "
    "{'capabilities': {'tools': {}}, 'serverInfo': {'name': 'mock', 'version': '1.0'}}}))\n"
)


@pytest.fixture(scope="module")
def git_available():
    """Skip git-dependent tests when git is not installed in the environment.

    Mirrors the module-scoped autouse fixture in test_git_hardening.py; here
    it is deliberately NOT autouse so the non-git tests in this module (clone
    URL rejection, MCP injection rejection, valid MCP connect) still run in
    git-less containers.
    """
    if shutil.which("git") is None:
        pytest.skip("git binary not available in this environment")


# ---------------------------------------------------------------------------
# 1. GitInfoTool hardening contracts
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("git_available")
def test_post_commit_hook_never_executes(tmp_path):
    """A planted .git/hooks/post-commit must never run during a tool commit."""
    workspace = tmp_path / "workspace"
    repo = workspace / "repo"
    _init_repo(repo)
    marker = tmp_path / "post_commit_marker.txt"
    _write_hook(repo / ".git" / "hooks" / "post-commit", marker)

    (repo / "hello.txt").write_text("hi\n")
    tool = GitInfoTool(
        operation="commit",
        message="add hello",
        file_path="hello.txt",
        working_dir=str(repo),
        session_permissions=FULL_PERMISSIONS,
    )
    result = tool.execute()
    assert "Git command failed" not in result
    assert not marker.exists(), (
        "post-commit hook executed despite core.hooksPath=/dev/null"
    )

    log = _run_git_clean(repo, "log", "--oneline")
    assert log.returncode == 0, log.stderr
    assert "add hello" in log.stdout, "commit did not land"


@pytest.mark.usefixtures("git_available")
def test_fsmonitor_never_executes(tmp_path):
    """A configured core.fsmonitor hook must never be invoked by the tool."""
    workspace = tmp_path / "workspace"
    repo = workspace / "repo"
    _init_repo(repo)

    helper = tmp_path / "fsmonitor_helper.sh"
    marker = tmp_path / "fsmonitor_marker.txt"
    helper.write_text(f"#!/bin/sh\ntouch {marker}\n")
    helper.chmod(0o755)
    config = _run_git_clean(repo, "config", "core.fsmonitor", str(helper))
    assert config.returncode == 0, config.stderr

    (repo / "a.txt").write_text("a\n")
    tool = GitInfoTool(
        operation="status",
        working_dir=str(repo),
        session_permissions=FULL_PERMISSIONS,
    )
    result = tool.execute()
    assert "Git command failed" not in result
    assert not marker.exists(), "core.fsmonitor executed despite -c core.fsmonitor="


@pytest.mark.usefixtures("git_available")
def test_textconv_never_executes(tmp_path):
    """A configured diff.textconv driver must never be invoked by the tool."""
    workspace = tmp_path / "workspace"
    repo = workspace / "repo"
    _init_repo(repo)

    (repo / "data.txt").write_text("line1\n")
    add = _run_git_clean(repo, "add", "data.txt")
    assert add.returncode == 0, add.stderr
    commit = _run_git_clean(repo, "commit", "-q", "-m", "base")
    assert commit.returncode == 0, commit.stderr

    helper = tmp_path / "textconv_helper.sh"
    marker = tmp_path / "textconv_marker.txt"
    helper.write_text(f"#!/bin/sh\ntouch {marker}\n")
    helper.chmod(0o755)
    config = _run_git_clean(repo, "config", "diff.textconv", str(helper))
    assert config.returncode == 0, config.stderr

    (repo / "data.txt").write_text("line2\n")
    tool = GitInfoTool(
        operation="diff",
        working_dir=str(repo),
        session_permissions=FULL_PERMISSIONS,
    )
    result = tool.execute()
    assert "Git command failed" not in result
    assert not marker.exists(), "diff.textconv executed despite -c diff.textconv="


def test_clone_ext_transport_rejected(tmp_path):
    """ext:: transport must be rejected before any git subprocess runs.

    NOTE: the atomic network:outbound check runs first, so the tool must be
    given permissions satisfying it (network:"write"); otherwise execute()
    would return an error string instead of reaching URL validation.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    tool = GitInfoTool(
        operation="clone",
        clone_url="ext::sh -c 'touch /tmp/pwned'",
        working_dir=str(workspace),
        session_permissions=FULL_PERMISSIONS,
        effective_permissions=FULL_PERMISSIONS,
    )
    with pytest.raises(ValueError) as excinfo:
        tool.execute()
    assert "Unsupported git protocol" in str(excinfo.value)


@pytest.mark.usefixtures("git_available")
def test_host_file_write_to_hooks_then_commit_hook_ignored(tmp_path):
    """HOST path: a pre-commit hook planted via Path.write_text is ignored.

    This caller passes only ``working_dir``/``session_permissions`` (no
    registry workspace), so git runs through the host hermetic sandbox,
    which neutralizes hooks via core.hooksPath=/dev/null + --no-verify.
    """
    workspace = tmp_path / "workspace"
    repo = workspace / "repo"
    _init_repo(repo)
    marker = tmp_path / "pre_commit_marker.txt"
    _write_hook(repo / ".git" / "hooks" / "pre-commit", marker)

    (repo / "hello.txt").write_text("hi\n")
    tool = GitInfoTool(
        operation="commit",
        message="add hello",
        file_path="hello.txt",
        working_dir=str(repo),
        session_permissions=FULL_PERMISSIONS,
    )
    result = tool.execute()
    assert "Git command failed" not in result
    assert not marker.exists(), "pre-commit hook executed despite --no-verify"

    log = _run_git_clean(repo, "log", "--oneline")
    assert log.returncode == 0, log.stderr
    assert "add hello" in log.stdout, "commit did not land"


class _FakeManager:
    """Resource-container stand-in recording exec() invocations."""

    def __init__(self):
        self.calls = []

    def exec(self, command, **kwargs):
        self.calls.append((command, kwargs))
        return {"exit_code": 0, "stdout": "ok", "stderr": ""}


def test_container_commit_does_not_skip_hooks(tmp_path):
    """CONTAINER path: commit must NOT pass --no-verify to git.

    The resource container is the security boundary, so repo-local hooks
    (e.g. the pre-commit above) are allowed to run inside it. This pins the
    command contract: no --no-verify is injected in container mode, and the
    container-mapped absolute hooks path core.hooksPath=/workspace/.githooks
    is injected instead.
    """
    manager = _FakeManager()
    tool = GitInfoTool(operation="commit", message="x")
    object.__setattr__(tool, "_resolved_workspace_path", str(tmp_path))
    object.__setattr__(tool, "_resolved_workspace_id", "test-ws")
    object.__setattr__(tool, "_ensure_resource_container", lambda: manager)

    tool._exec_container_raw(tmp_path, ["commit", "-m", "x"])

    assert len(manager.calls) == 1
    command, _kwargs = manager.calls[0]
    assert command == ["git", "-c", "core.hooksPath=/workspace/.githooks", "commit", "-m", "x"]
    assert "--no-verify" not in command


@pytest.mark.usefixtures("git_available")
def test_git_add_status_diff_log_work(tmp_path):
    """add/status/diff/log all succeed through the hardened runner.

    DEVIATION: GitInfoTool's public ``operation`` Literal has no "add" entry
    (add is a private helper used internally by every commit), so the staging
    step here uses the sanitized git helper; the tool's own add path is
    exercised by the commit operations in the other tests.
    """
    workspace = tmp_path / "workspace"
    repo = workspace / "repo"
    _init_repo(repo)

    (repo / "base.txt").write_text("base\n")
    tool = GitInfoTool(
        operation="commit",
        message="base",
        file_path="base.txt",
        working_dir=str(repo),
        session_permissions=FULL_PERMISSIONS,
    )
    result = tool.execute()
    assert "Git command failed" not in result

    (repo / "new.txt").write_text("new\n")
    add = _run_git_clean(repo, "add", "new.txt")
    assert add.returncode == 0, add.stderr

    tool = GitInfoTool(
        operation="status",
        working_dir=str(repo),
        session_permissions=FULL_PERMISSIONS,
    )
    status = tool.execute()
    assert "Git command failed" not in status
    assert "new.txt" in status

    # Make an unstaged change so plain `git diff` (worktree vs index) has
    # output; the staged new.txt shows up in status instead.
    (repo / "base.txt").write_text("base2\n")
    tool = GitInfoTool(
        operation="diff",
        working_dir=str(repo),
        session_permissions=FULL_PERMISSIONS,
    )
    diff = tool.execute()
    assert "Git command failed" not in diff
    assert "base2" in diff

    tool = GitInfoTool(
        operation="log",
        working_dir=str(repo),
        session_permissions=FULL_PERMISSIONS,
    )
    log = tool.execute()
    assert "Git command failed" not in log
    assert "base" in log


# ---------------------------------------------------------------------------
# 2. MCPServerConnect hardening contracts
# ---------------------------------------------------------------------------


def test_mcp_shell_injection_rejected(tmp_path, monkeypatch):
    """Shell metacharacters must never reach a subprocess via MCP connect."""
    # (i) Metacharacters in the server NAME: registry lookup only, and the
    # unknown-server error is returned; nothing is executed.
    registry = _write_registry(
        tmp_path,
        {"mock": {"command": sys.executable, "args": ["-c", "print('hi')"]}},
    )
    monkeypatch.setattr(mcp_server_connect, "REGISTRY_PATH", str(registry))
    before = sorted(str(p) for p in tmp_path.rglob("*"))

    tool = mcp_server_connect.MCPServerConnect(
        server_name="$(cat /etc/passwd)",
        session_permissions=MCP_PERMISSIONS,
        effective_permissions=MCP_PERMISSIONS,
        workspace_id="reg-contract",
    )
    out = tool.execute()
    assert "not found in registry" in out
    assert "Available" in out
    after = sorted(str(p) for p in tmp_path.rglob("*"))
    assert before == after, "something was executed/written for an unknown server"

    # (ii) Metacharacters in the server ARGS: SandboxedExecution raises
    # ValueError, which propagates out of execute() (fail-closed).
    marker = tmp_path / "mcp_pwned.txt"
    registry = _write_registry(
        tmp_path,
        {
            "evil": {
                "command": sys.executable,
                "args": ["-c", f"$(touch {marker})"],
            }
        },
    )
    monkeypatch.setattr(mcp_server_connect, "REGISTRY_PATH", str(registry))
    tool = mcp_server_connect.MCPServerConnect(
        server_name="evil",
        session_permissions=MCP_PERMISSIONS,
        effective_permissions=MCP_PERMISSIONS,
        workspace_id="reg-contract",
    )
    with pytest.raises(ValueError):
        tool.execute()
    assert not marker.exists(), "shell metacharacters reached a subprocess"


def test_mcp_connect_valid_server_works(tmp_path, monkeypatch):
    """A well-formed server completing the initialize handshake connects."""
    registry = _write_registry(
        tmp_path,
        {
            "mock": {
                "command": sys.executable,
                "args": ["-c", _MOCK_SERVER_SCRIPT],
            }
        },
    )
    monkeypatch.setattr(mcp_server_connect, "REGISTRY_PATH", str(registry))
    tool = mcp_server_connect.MCPServerConnect(
        server_name="mock",
        session_permissions=MCP_PERMISSIONS,
        effective_permissions=MCP_PERMISSIONS,
        workspace_id="reg-contract",
    )
    out = tool.execute()
    parsed = json.loads(out)
    assert parsed["status"] == "connected"
    assert parsed["server"] == "mock"
    assert "tools" in parsed["capabilities"]

@pytest.mark.usefixtures("git_available")
def test_nested_repo_textconv_blocked(tmp_path):
    """Nested-repo textconv exploit chain: no execution via git show.

    The attacker's report chain: a nested repo inside the workspace carries
    .gitattributes routing *.bin through a diff driver and .git/config
    defining that driver's textconv as an executable script. git show would
    render the diff via textconv and execute the script; GitInfoTool passes
    --no-textconv, so the marker must never appear.
    """
    workspace = tmp_path / "workspace"
    nested = workspace / "pwn"
    nested.mkdir(parents=True)
    _init_repo(nested)

    marker = workspace / "pwn_textconv_marker.txt"
    evil = nested / "evil.sh"
    evil.write_text(f"#!/bin/sh\ntouch {marker}\n")
    evil.chmod(0o755)
    config = _run_git_clean(nested, "config", "diff.test.textconv", str(evil))
    assert config.returncode == 0, config.stderr
    (nested / ".gitattributes").write_text("*.bin diff=test\n")

    data = nested / "data.bin"
    data.write_bytes(b"\x00\x01binary\xff\xfe\n")
    add = _run_git_clean(nested, "add", ".")
    assert add.returncode == 0, add.stderr
    commit = _run_git_clean(nested, "commit", "-q", "-m", "payload")
    assert commit.returncode == 0, commit.stderr

    tool = GitInfoTool(
        operation="show",
        commit="HEAD",
        working_dir=str(nested),
        session_permissions=FULL_PERMISSIONS,
    )
    result = tool.execute()
    assert "Git command failed" not in result
    assert not marker.exists(), (
        "textconv executed during git show despite --no-textconv"
    )

