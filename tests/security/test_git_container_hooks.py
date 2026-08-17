"""Docker-gated integration tests: commit hook policy in container vs host mode.

These tests prove the GitInfoTool commit-hook contract against a REAL Docker
daemon (mirroring the gating in ``test_git_container_sandbox.py``):

1. Containerized commits run hooks ONLY from the workspace-owned ``.githooks``
   directory (``core.hooksPath=/workspace/.githooks`` injected by
   ``_exec_container_raw``); repo-local ``.git/hooks`` is never consulted and
   no ``--no-verify`` is applied, because the resource container IS the
   security boundary.
2. Host-mode commits neutralize hooks entirely: ``core.hooksPath=/dev/null``
   precedes the subcommand and ``--no-verify`` is injected, so a
   ``.githooks`` directory in the workspace is never referenced.

Marker note
-----------
Gating is identical to ``test_git_container_sandbox.py``: the module is
skipped unless ``TM_RUN_DOCKER_SECURITY_TESTS=1`` and a real Docker daemon is
reachable. The image must be built first (see the build instructions in
``test_git_container_sandbox.py``).

Conftest note: ``tests/docker_integration/conftest.py`` provides mock-docker
fixtures for unit tests; this module deliberately does NOT use them -- these
tests need a real daemon.
"""

import os
from types import SimpleNamespace

import pytest

from tools.git_info_tool import GitInfoTool


def _docker_security_tests_enabled():
    if os.environ.get("TM_RUN_DOCKER_SECURITY_TESTS") != "1":
        return False
    try:
        from docker import from_env

        from_env().ping()
        return True
    except Exception:
        return False


# Marker registration note: @pytest.mark.docker is defined in pyproject.toml
# [tool.pytest.ini_options].markers. No pytest.ini exists in this repo.
pytestmark = pytest.mark.skipif(
    not _docker_security_tests_enabled(),
    reason="Docker-gated security test; set TM_RUN_DOCKER_SECURITY_TESTS=1 and ensure Docker is available",
)


@pytest.fixture(scope="module", autouse=True)
def require_docker():
    """Skip the entire module when no real Docker daemon is reachable.

    Intentionally NOT mocked: these tests validate real container isolation,
    which mocks cannot prove.
    """
    try:
        import docker

        docker.from_env().ping()
    except Exception as exc:  # pragma: no cover - depends on environment
        pytest.skip(f"Docker daemon unavailable: {exc}")


@pytest.fixture
def resource_manager(tmp_path):
    """Construct the real manager, ensure the container, always clean up.

    ``workspace_id='test-ws'`` is a fixed id so the workspace-scoped labels
    are deterministic. ``network_mode='none'`` is the security-critical
    default under test.
    """
    from infra.resource_container_manager import ResourceContainerManager

    ws_dir = tmp_path / "workspace"
    ws_dir.mkdir(exist_ok=True)
    manager = ResourceContainerManager(
        workspace_id="test-ws",
        workspace_path=str(ws_dir),
        network_mode="none",
    )
    manager.ensure_container()
    try:
        yield manager
    finally:
        # Teardown always runs, even after assertion failures, so a failed
        # test never leaks a resource container.
        manager.remove()


def _container_tool(ws_dir):
    """GitInfoTool wired for container mode against a registry workspace."""
    tool = GitInfoTool(operation="commit", message="x")
    object.__setattr__(tool, "_resolved_workspace_path", str(ws_dir))
    object.__setattr__(tool, "_resolved_workspace_id", "test-ws")
    return tool


# ---------------------------------------------------------------------------
# Test 1: containerized commits run the workspace .githooks directory
# ---------------------------------------------------------------------------

def test_containerized_commit_runs_workspace_githooks(resource_manager, tmp_path):
    """A post-commit hook placed in ws/.githooks runs on containerized commit.

    The hook is written from the HOST side into the real workspace
    (bind-mounted at /workspace); ``_exec_container_raw`` injects
    ``-c core.hooksPath=/workspace/.githooks`` before ``commit``, so the hook
    executes and its marker lands back in the workspace -- proving the
    workspace-owned hooks directory is the only hook source consulted.
    """
    ws_dir = tmp_path / "workspace"

    # safe.directory: repo files are host-owned; if the test runner's uid is
    # not 1000, git >= 2.35.2 refuses operations on "dubious ownership".
    res = resource_manager.exec(
        ["git", "config", "--global", "--add", "safe.directory", "/workspace"]
    )
    assert res["exit_code"] == 0, res["stderr"]

    res = resource_manager.exec(["git", "init", "-q"], workdir="/workspace")
    assert res["exit_code"] == 0, res["stderr"]
    res = resource_manager.exec(
        ["git", "config", "user.email", "test@example.com"], workdir="/workspace"
    )
    assert res["exit_code"] == 0, res["stderr"]
    res = resource_manager.exec(
        ["git", "config", "user.name", "Test"], workdir="/workspace"
    )
    assert res["exit_code"] == 0, res["stderr"]

    (ws_dir / "hello.txt").write_text("hello\n", encoding="utf-8")
    res = resource_manager.exec(["git", "add", "hello.txt"], workdir="/workspace")
    assert res["exit_code"] == 0, res["stderr"]

    # Workspace-owned hooks dir (policy source for containerized commits).
    hooks_dir = ws_dir / ".githooks"
    hooks_dir.mkdir(exist_ok=True)
    hook_path = hooks_dir / "post-commit"
    hook_path.write_text(
        "#!/bin/sh\necho ran > /workspace/hook_marker.txt\n", encoding="utf-8"
    )
    hook_path.chmod(0o755)

    tool = _container_tool(ws_dir)
    exit_code, stdout, stderr = tool._exec_container_raw(
        ws_dir, ["commit", "-m", "x"], manager=resource_manager
    )
    assert exit_code == 0, f"commit failed: {stderr}\n{stdout}"

    marker = ws_dir / "hook_marker.txt"
    assert marker.exists(), "workspace .githooks post-commit hook did not run"
    assert marker.read_text(encoding="utf-8").strip() == "ran"


# ---------------------------------------------------------------------------
# Test 2: host-mode commits neutralize hooks entirely
# ---------------------------------------------------------------------------

class _FakeSandbox:
    """Stand-in for SandboxedExecution (host path)."""

    instances = []

    def __init__(self, **kwargs):
        _FakeSandbox.instances.append(self)
        self.calls = []

    def run(self, command, **kwargs):
        self.calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")


def test_host_commit_hooks_fully_neutralized(tmp_path, monkeypatch):
    """Host commits never reference a workspace .githooks directory.

    The hardened argv must carry ``core.hooksPath=/dev/null`` BEFORE the
    subcommand plus ``--no-verify``; a ``.githooks`` dir sitting in the
    workspace must not appear anywhere in the command.
    """
    _FakeSandbox.instances.clear()
    monkeypatch.setattr("tools.git_info_tool.SandboxedExecution", _FakeSandbox)

    ws_dir = tmp_path / "ws"
    ws_dir.mkdir(exist_ok=True)
    (ws_dir / ".githooks").mkdir(exist_ok=True)  # must be ignored on host path

    tool = GitInfoTool(operation="commit", message="x")
    exit_code, stdout, stderr = tool._exec_host_raw(ws_dir, ["commit", "-m", "x"])
    assert (exit_code, stdout, stderr) == (0, "ok", "")

    assert len(_FakeSandbox.instances) == 1
    command, _kwargs = _FakeSandbox.instances[0].calls[0]
    assert command[0] == "git"
    assert command.index("core.hooksPath=/dev/null") < command.index("commit")
    assert "--no-verify" in command
    assert ".githooks" not in command
