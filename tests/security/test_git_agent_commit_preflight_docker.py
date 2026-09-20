"""Docker-gated ACCEPTANCE test: container-mode agent commit preflight.

End-to-end acceptance for the host-fallback closure strand: an AGENT commit on a
non-protected FEATURE branch, executed inside the resource container, with the
workspace host-resource kill switch OFF (host git execution denied, fail-closed).
The commit must complete without operator intervention in under 90 seconds and
must actually land in the repository.

Why this test exists
--------------------
The host-fallback closure makes containerized git execution MANDATORY whenever
the workspace denies host resources. This module is the real-daemon acceptance
proof: it drives a commit through ``GitWriteTool._exec_container_raw`` against a
REAL resource container and asserts (a) the kill switch reads "off" so the host
backend is unavailable, (b) the commit finishes inside the 90s budget, and
(c) ``HEAD`` advances -- the commit actually lands.

Gating (identical to tests/security/test_git_container_sandbox.py)
------------------------------------------------------------------
Marked ``@pytest.mark.docker`` (registered in ``pyproject.toml`` under
``[tool.pytest.ini_options].markers``). Skipped unless
``TM_RUN_DOCKER_SECURITY_TESTS=1`` AND a real Docker daemon is reachable; the
module-level autouse ``require_docker`` fixture additionally skips the whole
module when the daemon is unreachable. A real daemon and the resource images are
required (build them from the VAULT-managed build directory first)::

    docker build -t tm-workspace-runtime:latest \
        -f ~/.thoughtmachine/docker/resource/default_runtime.Dockerfile \
        ~/.thoughtmachine/docker/resource
    docker build -t tm-resource-git \
        -f ~/.thoughtmachine/docker/resource/git_overlay.Dockerfile \
        --build-arg BASE_IMAGE=tm-workspace-runtime:latest \
        ~/.thoughtmachine/docker/resource

MANUAL ACCEPTANCE PROCEDURE (merge-time fallback when no daemon is available)
----------------------------------------------------------------------------
1. On a host with a real Docker daemon and the resource images built, check out
   this branch and run::

       TM_RUN_DOCKER_SECURITY_TESTS=1 python -m pytest \
           tests/security/test_git_agent_commit_preflight_docker.py -v

2. Confirm the test RUNS (it does not skip) and PASSES; the commit step must
   report an elapsed wall-clock time below the 90-second budget.
3. Confirm the container path was used: the commit ran via
   ``_exec_container_raw`` inside the resource container, NOT host git, and
   ``_kill_switch_state()`` reported "off" for the workspace (host execution
   denied, fail-closed -- the container path is therefore mandatory).
4. Confirm the commit actually landed in ``/workspace``: ``git rev-parse HEAD``
   resolves to a real object, ``git log -1 --pretty=%s`` equals the commit
   message, and ``git status --porcelain`` is clean (the named file is
   committed).
5. Confirm no operator intervention was required at any point: the tool performs
   the staging and the commit itself; there is no host-side git fallback.

Conftest note: ``tests/docker_integration/conftest.py`` provides mock-docker
fixtures for unit tests; this module deliberately does NOT use them -- this test
needs a real daemon.
"""

import os
import time

import pytest

from tools.git_write_tool import GitWriteTool


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
# The docker marker is what -m "not docker and not e2e" deselects; the skipif is
# the belt-and-braces guard for a daemon-less run WITH the env var set.
pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(
        not _docker_security_tests_enabled(),
        reason="Docker-gated security test; set TM_RUN_DOCKER_SECURITY_TESTS=1 and ensure Docker is available",
    ),
]


@pytest.fixture(scope="module", autouse=True)
def require_docker():
    """Skip the entire module when no real Docker daemon is reachable.

    Intentionally NOT mocked: this test validates the real container commit
    path, which mocks cannot prove.
    """
    try:
        import docker

        docker.from_env().ping()
    except Exception as exc:  # pragma: no cover - depends on environment
        pytest.skip(f"Docker daemon unavailable: {exc}")


@pytest.fixture
def resource_manager(tmp_path):
    """Construct the real manager, ensure the container, always clean up.

    ``workspace_id='test-ws'`` is a fixed id so the workspace-scoped labels are
    deterministic. ``network_mode='none'`` is the security-critical default.
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
        # Teardown always runs, even after assertion failures, so a failed test
        # never leaks a resource container.
        manager.remove()


def _container_tool(ws_dir):
    """GitWriteTool wired for container mode against a registry workspace."""
    tool = GitWriteTool(
        operation="commit",
        message="x",
        session_permissions={"git": "write"},  # explicit perms: the git gate fails closed when session_permissions is unresolved
        effective_permissions={"git": "write"},  # container path enforces the atomic git:write category
    )
    object.__setattr__(tool, "_resolved_workspace_path", str(ws_dir))
    object.__setattr__(tool, "_resolved_workspace_id", "test-ws")
    return tool


def _git(manager, *args):
    """Run a git subcommand in /workspace; return the exec result dict."""
    return manager.exec(["git", *args], workdir="/workspace")


def test_container_agent_commit_lands_within_budget(resource_manager, tmp_path, monkeypatch):
    """A container-mode agent commit lands on a feature branch in < 90s.

    Kill switch OFF (host git denied) makes the container path mandatory, so a
    successful commit proves the container backend handled the whole commit --
    no host fallback, no operator intervention.
    """
    ws_dir = tmp_path / "workspace"

    # Kill switch OFF: point the vault at an empty dir so no
    # workspaces/test-ws/config.json exists -> host resources denied
    # (fail-closed) and the container path becomes mandatory.
    empty_vault = tmp_path / "vault"
    empty_vault.mkdir()
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(empty_vault))

    tool = _container_tool(ws_dir)

    # Precondition: the workspace denies host git, so there is no host fallback
    # to lean on -- the commit MUST run in the container.
    assert tool._kill_switch_state() == "off", "expected host-resource kill switch OFF"
    assert tool._host_execution_denied_reason(), (
        "expected host-side git execution to be denied for workspace test-ws"
    )

    # Repo setup inside the real container (workspace bind-mounted at /workspace).
    # safe.directory: repo files are host-owned; if the test runner's uid is not
    # 1000, git >= 2.35.2 refuses operations on "dubious ownership".
    res = resource_manager.exec(
        ["git", "config", "--global", "--add", "safe.directory", "/workspace"]
    )
    assert res["exit_code"] == 0, res["stderr"]
    res = _git(resource_manager, "init", "-q")
    assert res["exit_code"] == 0, res["stderr"]
    res = _git(resource_manager, "config", "user.email", "test@example.com")
    assert res["exit_code"] == 0, res["stderr"]
    res = _git(resource_manager, "config", "user.name", "Test")
    assert res["exit_code"] == 0, res["stderr"]

    # A non-protected FEATURE branch carrying one committable change.
    res = _git(resource_manager, "checkout", "-b", "feature/preflight-acceptance")
    assert res["exit_code"] == 0, res["stderr"]
    (ws_dir / "hello.txt").write_text("hello\n", encoding="utf-8")
    res = _git(resource_manager, "add", "hello.txt")
    assert res["exit_code"] == 0, res["stderr"]

    message = "agent commit preflight acceptance"

    # Timed container-mode agent commit: no host git, no operator intervention.
    start = time.monotonic()
    exit_code, stdout, stderr = tool._exec_container_raw(
        ws_dir, ["commit", "-m", message], manager=resource_manager
    )
    elapsed = time.monotonic() - start

    assert exit_code == 0, f"container commit failed: {stderr}\n{stdout}"
    assert elapsed < 90, f"container commit exceeded the 90s budget: {elapsed:.1f}s"

    # The commit actually landed.
    res = _git(resource_manager, "rev-parse", "HEAD")
    assert res["exit_code"] == 0, res["stderr"]
    assert res["stdout"].strip(), "HEAD did not resolve to a real commit object"
    res = _git(resource_manager, "log", "-1", "--pretty=%s")
    assert res["stdout"].strip() == message, "committed subject does not match the message"
    res = _git(resource_manager, "status", "--porcelain")
    assert res["stdout"].strip() == "", f"worktree not clean after commit: {res['stdout']!r}"
