"""
Live-Docker integration suite for the persistent container machinery.

Covers the `ContainerManager` lifecycle: workspace→volume population
(init-container copy + sentinel), persistence across stop/start, session
cleanup (including orphaned containers tagged with a session label),
network/read-only isolation, and the permissive security gate that grants
bridge networking when session permissions allow it.

These tests talk to a real Docker daemon.  They are skipped cleanly when the
daemon is unavailable (see `needs_docker`); the only network dependency is
test 5's *assertion that networking is blocked*, and test 6 inspects container
config only (no network call).

Style mirrors tests/docker/test_persistence.py.
"""

import os
import shutil
import sys
import tempfile
import time
import uuid

import docker
import pytest

# Make the repository root importable when running `pytest tests/docker/` or
# this file directly (tests/docker has no conftest.py of its own; pytest's
# prepend import mode usually covers this, but be self-contained).
_SRC_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _SRC_ROOT not in sys.path:
    sys.path.insert(0, _SRC_ROOT)

from tools.container_manager import (  # noqa: E402  (after sys.path bootstrap)
    ContainerManager,
    cleanup_session,
    list_session_containers,
)
from docker_executor import ensure_workspace_volume_populated  # noqa: E402


# ---------------------------------------------------------------------------
# Docker-availability guard — mirrors tests/docker/test_persistence.py.
# ---------------------------------------------------------------------------
_DOCKER_AVAILABLE: bool = False


def docker_available():
    global _DOCKER_AVAILABLE
    if _DOCKER_AVAILABLE:
        return True
    try:
        client = docker.from_env()
        client.ping()
        _DOCKER_AVAILABLE = True
        return True
    except Exception:
        return False


needs_docker = pytest.mark.skipif(
    not docker_available(), reason="Docker daemon is not available"
)


# Image used by the suite — mirrors the Dockerfile in test_persistence
# (python:3.11-slim is already pulled on the host; it has /bin/sh, cp, touch
# but no curl, which is exactly what the network-isolation test relies on).
IMAGE_TAG = "tm-container-lifecycle:latest"

DOCKERFILE = (
    "FROM python:3.11-slim\n"
    "RUN adduser --uid 1000 --disabled-password --gecos '' agent\n"
    "USER agent\n"
    'CMD ["tail", "-f", "/dev/null"]\n'
)


@pytest.fixture(scope="module")
def lifecycle_image():
    """Build the suite image once per module; remove it on teardown."""
    context = tempfile.mkdtemp(prefix="tm-lifecycle-img-")
    with open(os.path.join(context, "Dockerfile"), "w") as fh:
        fh.write(DOCKERFILE)
    client = docker.from_env()
    try:
        client.images.build(path=context, tag=IMAGE_TAG, rm=True)
    except Exception:
        shutil.rmtree(context, ignore_errors=True)
        raise
    yield IMAGE_TAG
    shutil.rmtree(context, ignore_errors=True)
    try:
        client.images.get(IMAGE_TAG).remove(force=True)
    except (docker.errors.NotFound, docker.errors.APIError):
        pass


@needs_docker
class TestContainerLifecycle:
    """Persistent-container lifecycle: volume sync, persistence, cleanup, isolation."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path, lifecycle_image):
        self.client = docker.from_env()
        self.workspace_dir = tmp_path / "ws"
        self.workspace_dir.mkdir()
        self.hello_text = f"hello-{uuid.uuid4()}"
        hello_file = self.workspace_dir / "hello.txt"
        hello_file.write_text(self.hello_text)
        os.chmod(hello_file, 0o644)
        self.image_tag = lifecycle_image
        # (container_id, volume_name-or-None) pairs removed at teardown.
        self._tracked = []
        yield
        # Exception-safe teardown: never touch anything we did not create.
        for container_id, volume_name in self._tracked:
            try:
                self.client.containers.get(container_id).remove(force=True)
            except (docker.errors.NotFound, docker.errors.APIError):
                pass
            if volume_name is not None:
                try:
                    self.client.volumes.get(volume_name).remove(force=True)
                except (docker.errors.NotFound, docker.errors.APIError):
                    pass

    def _start_manager(self, session_permissions=None, workspace_id=None, session_id=None):
        """Start a ContainerManager-backed container and track it for teardown."""
        manager = ContainerManager(
            workspace_path=str(self.workspace_dir),
            session_id=session_id,
            workspace_id=workspace_id,
            session_permissions=session_permissions,
            image=self.image_tag,
        )
        result = manager.start()
        # Give the init container / first exec a moment to settle
        # (mirrors test_persistence).
        time.sleep(1)
        volume_name = f"tm-workspace-{workspace_id}" if workspace_id is not None else None
        self._tracked.append((result["id"], volume_name))
        return manager, result

    def _exec_ok(self, manager, container_id, command, timeout=20):
        """Exec a command and assert it succeeded, with output in the message."""
        result = manager.exec(container_id, command, timeout=timeout)
        assert result["exit_code"] == 0, (
            f"command failed (exit {result['exit_code']}): {command}\n"
            f"stdout: {result['stdout']!r}\nstderr: {result['stderr']!r}"
        )
        return result

    def test_volume_population_syncs_workspace(self):
        """Workspace files are copied into the named volume (copy, not bind)."""
        workspace_id = uuid.uuid4()
        session_id = uuid.uuid4()
        manager, res = self._start_manager(
            session_permissions={"filesystem": "write"},
            workspace_id=workspace_id,
            session_id=session_id,
        )
        container_id = res["id"]
        # Workspace file visible in the container.
        result = self._exec_ok(manager, container_id, "cat /workspace/hello.txt")
        assert result["stdout"].strip() == self.hello_text

        # Delete the host file, then re-run population: the copy survives
        # (a bind mount would lose the file), and the sentinel is already in
        # place so the re-population is a no-op.
        os.remove(self.workspace_dir / "hello.txt")
        assert ensure_workspace_volume_populated(
            self.client,
            self.image_tag,
            str(self.workspace_dir),
            f"tm-workspace-{workspace_id}",
            network_mode="none",
        ) is True
        result = self._exec_ok(manager, container_id, "cat /workspace/hello.txt")
        assert result["stdout"].strip() == self.hello_text
        self._exec_ok(manager, container_id, "test -f /workspace/.workspace_synced")

        # Volume is mounted read-write: writes from inside the container stick.
        self._exec_ok(manager, container_id, "echo marker > /workspace/marker.txt")

    def test_persistence_across_stop_start(self):
        """Files written in the container survive a stop/start cycle."""
        workspace_id = uuid.uuid4()
        session_id = uuid.uuid4()
        manager, res = self._start_manager(
            session_permissions={"filesystem": "write"},
            workspace_id=workspace_id,
            session_id=session_id,
        )
        container_id = res["id"]
        self._exec_ok(manager, container_id, "echo persisted > /workspace/persist.txt")

        stop_result = manager.stop(container_id)
        assert "status" in stop_result, f"unexpected stop result: {stop_result}"

        restart = manager.start()
        assert restart["status"] == "reused", (
            f"expected container reuse after stop, got {restart!r}"
        )
        result = self._exec_ok(manager, restart["id"], "cat /workspace/persist.txt")
        assert result["stdout"].strip() == "persisted"

    def test_session_cleanup_removes_containers(self):
        """cleanup_session removes every container labelled with the session id."""
        session_id = uuid.uuid4()
        manager, res = self._start_manager(session_id=session_id)
        assert res["status"] == "created"

        cleanup = cleanup_session(session_id, self.client)
        assert cleanup["removed"] >= 1, f"expected >=1 removal, got {cleanup!r}"
        assert list_session_containers(session_id, self.client) == []

        # Second pass is a no-op.
        cleanup_again = cleanup_session(session_id, self.client)
        assert cleanup_again["removed"] == 0, f"expected no removals, got {cleanup_again!r}"

    def test_orphan_cleanup_fake_label(self):
        """Orphaned containers carrying a session label are reclaimed."""
        fake_session_id = str(uuid.uuid4())
        orphan = self.client.containers.run(
            image=self.image_tag,
            name=f"tm-orphan-{uuid.uuid4().hex[:8]}",
            labels={"thoughtmachine.session_id": fake_session_id},
            command=["tail", "-f", "/dev/null"],
            detach=True,
            user="1000:1000",
        )
        self._tracked.append((orphan.id, None))

        cleanup = cleanup_session(fake_session_id, self.client)
        assert cleanup["removed"] >= 1, f"expected orphan removal, got {cleanup!r}"
        assert list_session_containers(fake_session_id, self.client) == []

    def test_isolation_network_banned(self):
        """Default isolation: network banned, rootfs read-only, workspace ro."""
        session_id = uuid.uuid4()
        manager, res = self._start_manager(session_id=session_id)
        container_id = res["id"]

        # No curl in the image: fall back to python urllib. Under network=none
        # both fail, so the compound command exits non-zero.
        network_result = manager.exec(
            container_id,
            "curl -sS --max-time 5 http://example.com || python3 -c 'import urllib.request; "
            'urllib.request.urlopen("http://example.com", timeout=5)\'',
            timeout=30,
        )
        assert network_result["exit_code"] != 0, (
            f"expected network to be blocked, got exit {network_result['exit_code']}"
        )

        # Read-only rootfs: cannot write outside the workspace mount.
        assert manager.exec(container_id, "touch /root/probe", timeout=20)["exit_code"] != 0
        # Read-only workspace mount: cannot write inside /workspace either.
        assert manager.exec(container_id, "touch /workspace/probe", timeout=20)["exit_code"] != 0

        attrs = self.client.containers.get(container_id).attrs
        assert attrs["HostConfig"]["NetworkMode"] == "none"

    def test_isolation_network_allowed_gate(self):
        """With network=write permission the gate grants bridge + ro workspace."""
        workspace_id = uuid.uuid4()
        session_id = uuid.uuid4()
        manager, res = self._start_manager(
            session_permissions={"network": "write", "filesystem": "read"},
            workspace_id=workspace_id,
            session_id=session_id,
        )
        container_id = res["id"]

        # Config-only assertions: no network traffic happens in this test.
        attrs = self.client.containers.get(container_id).attrs
        assert attrs["HostConfig"]["NetworkMode"] in ("bridge", "default"), (
            "expected bridge networking for network=write session, got "
            f"{attrs['HostConfig']['NetworkMode']!r}"
        )
        workspace_mount = None
        for mount in attrs["Mounts"]:
            if mount.get("Destination") == "/workspace":
                workspace_mount = mount
                break
        assert workspace_mount is not None, "no /workspace mount found"
        # Docker reports Mode 'z' for named volume mounts on this host, so
        # Mode cannot distinguish ro from rw — RW is the authoritative flag.
        assert workspace_mount.get("RW") is False, (
            f"expected read-only workspace mount, got RW={workspace_mount.get('RW')!r}"
        )
        # Cross-check: the HostConfig mount spec records ReadOnly explicitly.
        host_mounts = (attrs.get("HostConfig") or {}).get("Mounts") or []
        ws_spec = [m for m in host_mounts if m.get("Target") == "/workspace"]
        assert ws_spec and ws_spec[0].get("ReadOnly") is True, (
            "expected HostConfig /workspace mount ReadOnly=True"
        )
