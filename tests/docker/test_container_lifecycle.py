"""
Live-Docker integration suite for the persistent container machinery.

Covers the `ContainerManager` lifecycle: the Phase-2 mount model (host
workspace bind-mounted at /workspace — live, no named workspace volumes —
plus a per-workspace package volume), persistence across stop/start,
session cleanup (including orphaned containers tagged with a session
label), network/read-only isolation, and the permissive security gate that
grants bridge networking when session permissions allow it.

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

# NOTE: the container machinery (tools.container_manager / docker_executor) is
# imported LAZILY inside the test methods, never at module import time.
# Importing tools.container_manager here would pull in the whole ``tools``
# package, which triggers a circular-import cascade (agent.logging is left
# mid-import while thoughtmachine.security runs its ``from agent.events
# import global_event_bus, EventType, create_event``). That ImportError sends
# thoughtmachine.security down its ``except ImportError`` branch, permanently
# setting global_event_bus=EventType=create_event=None and
# EVENT_SYSTEM_AVAILABLE=False for the whole pytest process. Because
# tests/docker/ collects FIRST, a broken thoughtmachine.security then
# silently disables the security gate / prompt machinery for every later
# test suite (ask_permission, permissions_roundtrip, tool_executor, ...).


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
    """Persistent-container lifecycle: bind-mount visibility, persistence, cleanup, isolation."""

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
        # Container ids removed at teardown. Phase-2 creates NO named
        # workspace volumes (host dir is bind-mounted), so there is no
        # volume tracking anymore.
        self._tracked = []
        yield
        # Exception-safe teardown: never touch anything we did not create.
        for container_id in self._tracked:
            try:
                self.client.containers.get(container_id).remove(force=True)
            except (docker.errors.NotFound, docker.errors.APIError):
                pass

    def _start_manager(self, session_permissions=None, workspace_id=None, session_id=None):
        """Start a ContainerManager-backed container and track it for teardown."""
        from tools.container_manager import ContainerManager  # lazy (see module docstring)
        manager = ContainerManager(
            workspace_path=str(self.workspace_dir),
            session_id=session_id,
            workspace_id=workspace_id,
            session_permissions=session_permissions,
            image=self.image_tag,
        )
        result = manager.start()
        # Give the container a moment to settle (mirrors test_persistence).
        time.sleep(1)
        self._tracked.append(result["id"])
        return manager, result

    def _exec_ok(self, manager, container_id, command, timeout=20):
        """Exec a command and assert it succeeded, with output in the message."""
        result = manager.exec(container_id, command, timeout=timeout)
        assert result["exit_code"] == 0, (
            f"command failed (exit {result['exit_code']}): {command}\n"
            f"stdout: {result['stdout']!r}\nstderr: {result['stderr']!r}"
        )
        return result

    def test_host_workspace_visible_via_bind_mount(self):
        """The host workspace is bind-mounted: live, read-write, no copies."""
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

        # Bind mounts are LIVE: deleting the host file makes it disappear
        # from the running container immediately, and recreating it brings
        # it back — no population/refresh step in between.
        os.remove(self.workspace_dir / "hello.txt")
        gone = manager.exec(container_id, "cat /workspace/hello.txt", timeout=20)
        assert gone["exit_code"] != 0, (
            f"expected cat to fail after host file removal, got {gone!r}"
        )
        (self.workspace_dir / "hello.txt").write_text(self.hello_text)
        result = self._exec_ok(manager, container_id, "cat /workspace/hello.txt")
        assert result["stdout"].strip() == self.hello_text

        # Mount is read-write: writes from inside the container stick.
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
        from tools.container_manager import cleanup_session, list_session_containers  # lazy
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
        from tools.container_manager import cleanup_session, list_session_containers  # lazy
        fake_session_id = str(uuid.uuid4())
        orphan = self.client.containers.run(
            image=self.image_tag,
            name=f"tm-orphan-{uuid.uuid4().hex[:8]}",
            labels={"thoughtmachine.session_id": fake_session_id},
            command=["tail", "-f", "/dev/null"],
            detach=True,
            user="1000:1000",
        )
        self._tracked.append(orphan.id)

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
        # Docker reports Mode strings like 'z'/'rw,z' for some mounts, so
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

    def test_host_changes_visible_to_new_container(self):
        """A host workspace change is visible to a later container.

        Phase-2 bind-mounts the host directory directly: no named volume, no
        manifest/sha tracking. A file added on the host while the first
        container runs is immediately visible to a SECOND container started
        for the same workspace path (the bind reflects the live host tree).
        """
        workspace_id = uuid.uuid4()
        session_id_1 = uuid.uuid4()
        manager1, res1 = self._start_manager(
            workspace_id=workspace_id, session_id=session_id_1
        )
        container_id_1 = res1["id"]
        self._exec_ok(manager1, container_id_1, "cat /workspace/hello.txt")

        # Change the host workspace while the first container is running.
        probe_text = f"refreshed-{uuid.uuid4()}"
        (self.workspace_dir / "refresh-probe.txt").write_text(probe_text)

        # Second container, same workspace path, new session: the bind mount
        # reflects the live host tree, so the new file is visible immediately.
        session_id_2 = uuid.uuid4()
        manager2, res2 = self._start_manager(
            workspace_id=workspace_id, session_id=session_id_2
        )
        assert res2["status"] == "created", f"expected fresh container, got {res2!r}"

        result = self._exec_ok(manager2, res2["id"], "cat /workspace/refresh-probe.txt")
        assert result["stdout"].strip() == probe_text
        # ...and the original workspace file is still visible via the bind.
        result = self._exec_ok(manager2, res2["id"], "cat /workspace/hello.txt")
        assert result["stdout"].strip() == self.hello_text

    def test_no_workspace_volume_created(self):
        """Phase-2 creates NO named workspace volume (host dir is bind-mounted)."""
        workspace_id = uuid.uuid4()
        session_id_1 = uuid.uuid4()
        manager1, res1 = self._start_manager(
            workspace_id=workspace_id, session_id=session_id_1
        )
        container_id_1 = res1["id"]
        self._exec_ok(manager1, container_id_1, "cat /workspace/hello.txt")

        # The old model's named workspace volume must never be created.
        with pytest.raises(docker.errors.NotFound):
            self.client.volumes.get(f"tm-workspace-{workspace_id}")

        # Second container, same workspace path: still created fresh, and the
        # workspace files are visible via the bind mount (no population step).
        session_id_2 = uuid.uuid4()
        manager2, res2 = self._start_manager(
            workspace_id=workspace_id, session_id=session_id_2
        )
        assert res2["status"] == "created", f"expected fresh container, got {res2!r}"
        result = self._exec_ok(manager2, res2["id"], "cat /workspace/hello.txt")
        assert result["stdout"].strip() == self.hello_text
