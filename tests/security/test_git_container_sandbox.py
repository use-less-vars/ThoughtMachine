"""Integration-test SKELETON for the hidden git resource container.

These tests prove the security properties of ``infra.resource_container_manager``
against a REAL Docker daemon:

1. A malicious git post-commit hook running inside the resource container
   cannot escape the sandbox: it can write to the container's /tmp, but
   cannot touch the host outside the workspace bind, cannot read the vault
   (/vault is never mounted), cannot reach the docker socket, and cannot
   open network connections.
2. The resource container is invisible to the agent-facing container listing
   (``ContainerManager.list_containers``) — the defense-in-depth exclusion
   contract.

Marker note
-----------
These tests are marked ``@pytest.mark.docker``. The marker IS registered in
``pyproject.toml`` under ``[tool.pytest.ini_options].markers``
("docker: marks tests that require a real Docker daemon"); there is NO
``pytest.ini`` in this repo, and none should be added for this marker.

They are COLLECTED but not run in CI/this audit environment: the module-level
``require_docker`` fixture skips the whole module when the daemon is
unreachable. A real daemon is required for these tests to exercise the actual
container. The image must be built first::

    docker build -t tm-resource-git ~/.thoughtmachine/docker/resource/

Conftest note: ``tests/docker_integration/conftest.py`` provides mock-docker
fixtures for unit tests; this module deliberately does NOT use them — these
tests need a real daemon.
"""

import os

import pytest

# Marker registration note: @pytest.mark.docker is defined in pyproject.toml
# [tool.pytest.ini_options].markers. No pytest.ini exists in this repo.
pytestmark = pytest.mark.docker


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


# ---------------------------------------------------------------------------
# Test 1: a malicious post-commit hook cannot escape the sandbox
# ---------------------------------------------------------------------------

POST_COMMIT_HOOK = """#!/bin/sh
touch /tmp/hook_ran
touch /host_proof 2>/dev/null || true
cat /vault/secrets.txt > /workspace/exfil_output 2>/dev/null || true
"""


def _egress_probe(resource_manager):
    """Probe network egress; returns the last exec result dict.

    Primary probe: curl (may be absent in python:3.12-slim). Fallback probe:
    python3 socket connect (python3 is always present in the base image).
    Both must fail under network_mode='none'.
    """
    res = resource_manager.exec(["curl", "-sS", "--max-time", "5", "http://example.com"])
    if res["exit_code"] == 127:  # curl: command not found
        res = resource_manager.exec(
            ["python3", "-c",
             "import socket; socket.create_connection(('example.com', 80), timeout=5)"]
        )
    return res


def test_git_commit_hook_isolated(resource_manager, tmp_path):
    """A malicious post-commit hook runs, but stays inside the sandbox.

    Security properties proven:
    - hooks EXECUTE (git plumbing works in the rw workspace bind) — the
      interesting case is what the hook can reach, not that it is suppressed;
    - /tmp is writable (tmpfs) — the hook's own scratch space works;
    - the read-only rootfs blocks writes outside /workspace and /tmp
      (/host_proof can never be created);
    - /vault is NOT mounted — the exfil attempt yields an empty file at most;
    - no docker socket is mounted;
    - network egress is blocked (network_mode='none').
    """
    ws_dir = tmp_path / "workspace"

    # safe.directory: repo files are host-owned; if the test runner's uid is
    # not 1000, git >= 2.35.2 refuses operations on "dubious ownership".
    # The container user is uid 1000 (Dockerfile + user='1000:1000'), so this
    # is belt-and-braces — but cheap and deterministic.
    res = resource_manager.exec(
        ["git", "config", "--global", "--add", "safe.directory", "/workspace"]
    )
    assert res["exit_code"] == 0, res["stderr"]

    # Init the repo on the REAL workspace (bind-mounted at /workspace).
    res = resource_manager.exec(["git", "init", "-q"], workdir="/workspace")
    assert res["exit_code"] == 0, res["stderr"]

    res = resource_manager.exec(["git", "config", "user.email", "test@example.com"],
                                workdir="/workspace")
    assert res["exit_code"] == 0, res["stderr"]
    res = resource_manager.exec(["git", "config", "user.name", "Test"],
                                workdir="/workspace")
    assert res["exit_code"] == 0, res["stderr"]

    # A tracked file, written from the HOST side (proves the bind is live rw).
    (ws_dir / "hello.txt").write_text("hello\n", encoding="utf-8")
    res = resource_manager.exec(["git", "add", "hello.txt"], workdir="/workspace")
    assert res["exit_code"] == 0, res["stderr"]

    # Malicious post-commit hook, written from the host into the real .git.
    hook_path = ws_dir / ".git" / "hooks" / "post-commit"
    hook_path.write_text(POST_COMMIT_HOOK, encoding="utf-8")
    hook_path.chmod(0o755)

    res = resource_manager.exec(["git", "commit", "-m", "x"], workdir="/workspace")
    assert res["exit_code"] == 0, res["stderr"]

    # 1) The hook RAN: /tmp/hook_ran exists inside the container (/tmp is tmpfs).
    res = resource_manager.exec(["test", "-f", "/tmp/hook_ran"])
    assert res["exit_code"] == 0, "hook did not execute: /tmp/hook_ran missing"

    # 2) /host_proof was NOT created even inside the container: the rootfs is
    # read-only, so `touch /host_proof` failed (2>/dev/null hid the EROFS).
    res = resource_manager.exec(["test", "-f", "/host_proof"])
    assert res["exit_code"] != 0, "hook wrote to the container rootfs (read_only broken)"

    # 3) Host-side proof: nothing escaped the workspace bind. /host_proof is
    # container-local (no host path exists for it); the host-side check is
    # that no stray file appeared inside the only host dir the container sees.
    assert not os.path.exists(str(ws_dir / "host_proof")), \
        "hook wrote a file into the workspace that it should not have created"

    # 4) Exfiltration blocked: /vault is never mounted, so
    # `cat /vault/secrets.txt > /workspace/exfil_output` redirects an empty
    # stream — the file may exist on the host but must be empty (or absent).
    exfil = ws_dir / "exfil_output"
    assert not exfil.exists() or exfil.stat().st_size == 0, \
        "hook exfiltrated data into the workspace: /vault must not be mounted"

    # 5) No docker socket inside the container.
    res = resource_manager.exec(["test", "-S", "/var/run/docker.sock"])
    assert res["exit_code"] != 0, "docker socket is mounted inside the sandbox"

    # 6) Network egress blocked (curl or python fallback — see helper).
    res = _egress_probe(resource_manager)
    assert res["exit_code"] != 0, \
        f"network egress unexpectedly allowed (exit_code={res['exit_code']})"


# ---------------------------------------------------------------------------
# Test 2: the resource container is hidden from agent-facing listing
# ---------------------------------------------------------------------------

def test_resource_container_hidden_from_agent_listing(resource_manager, tmp_path):
    """``ContainerManager.list_containers`` must not surface the resource container.

    Exclusion contract: the resource container carries the SAME
    ``thoughtmachine.workspace_id`` label as agent containers (so
    ``cleanup_workspace`` still sweeps it), but it is additionally labeled
    ``thoughtmachine.resource=git``. The defense-in-depth exclusion diff on
    ``ContainerManager.list_containers`` (see audit report section A) skips
    any container whose labels contain ``thoughtmachine.resource``.

    NOTE: this assertion FAILS against the current (un-patched)
    ``infra.container_manager.py`` — the resource container would appear,
    because the listing filters by workspace label only. The test documents
    and guards the contract; it passes once the exclusion diff is applied.
    """
    from infra.container_manager import ContainerManager

    # vault_root is pinned inside tmp_path so the REAL ContainerManager never
    # touches the user's ~/.thoughtmachine during the test.
    cm = ContainerManager(
        workspace_path=str(tmp_path / "workspace"),
        workspace_id="test-ws",
        session_id=None,
        session_permissions=None,
        vault_root=str(tmp_path / "vault"),
    )
    listed_ids = {entry["container_id"] for entry in cm.list_containers()}

    resource_id = resource_manager._container_id()
    assert resource_id is not None, "resource container should exist after ensure_container()"
    assert resource_id not in listed_ids, (
        "resource container leaked into agent-facing list_containers() — "
        "apply the thoughtmachine.resource exclusion diff "
        "(infra.container_manager.py list_containers)"
    )
