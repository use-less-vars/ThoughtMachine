"""
Live-Docker integration suite for the persistent container machinery.

Covers the `ContainerManager` lifecycle: the Phase-2 mount model (host
workspace bind-mounted at /workspace — live, no named workspace volumes —
plus a per-workspace package volume), persistence across stop/start,
workspace-scoped cleanup (including orphaned containers tagged with a
workspace label), cross-session container sharing, the per-workspace
container limit, network/read-only isolation, and the permissive security
gate that grants bridge networking when session permissions allow it.

These tests talk to a real Docker daemon.  They are skipped cleanly when the
daemon is unavailable (see `needs_docker`); the only network dependency is
test 5's *assertion that networking is blocked*, and test 6 inspects container
config only (no network call).

Style mirrors tests/docker/test_persistence.py.
"""

import json
import os
import shutil
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import docker
import pytest

# Make the repository root importable when running `pytest tests/docker/` or
# this file directly (tests/docker has no conftest.py of its own; pytest's
# prepend import mode usually covers this, but be self-contained).
_SRC_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _SRC_ROOT not in sys.path:
    sys.path.insert(0, _SRC_ROOT)

# NOTE: the container machinery (infra.container_manager / docker_executor) is
# imported LAZILY inside the test methods, never at module import time.
# Importing infra.container_manager here would pull in the whole ``tools``
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
        from infra.container_manager import ContainerManager  # lazy (see module docstring)
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

    def test_workspace_cleanup_removes_containers(self):
        """cleanup_workspace removes every container labelled with the workspace id."""
        from infra.container_manager import cleanup_workspace  # lazy
        workspace_id = uuid.uuid4()
        session_id = uuid.uuid4()
        manager, res = self._start_manager(workspace_id=workspace_id, session_id=session_id)
        assert res["status"] == "created"

        cleanup = cleanup_workspace(workspace_id, self.client)
        assert cleanup["removed"] >= 1, f"expected >=1 removal, got {cleanup!r}"
        assert manager.list_containers() == []

        # Second pass is a no-op.
        cleanup_again = cleanup_workspace(workspace_id, self.client)
        assert cleanup_again["removed"] == 0, f"expected no removals, got {cleanup_again!r}"

    def test_orphan_cleanup_fake_workspace_label(self):
        """Orphaned containers carrying a workspace label are reclaimed."""
        from infra.container_manager import cleanup_workspace  # lazy
        fake_workspace_id = str(uuid.uuid4())
        orphan = self.client.containers.run(
            image=self.image_tag,
            name=f"tm-orphan-{uuid.uuid4().hex[:8]}",
            labels={"thoughtmachine.workspace_id": fake_workspace_id},
            command=["tail", "-f", "/dev/null"],
            detach=True,
            user="1000:1000",
        )
        self._tracked.append(orphan.id)

        cleanup = cleanup_workspace(fake_workspace_id, self.client)
        assert cleanup["removed"] >= 1, f"expected orphan removal, got {cleanup!r}"
        leftovers = self.client.containers.list(
            all=True,
            filters={"label": f"thoughtmachine.workspace_id={fake_workspace_id}"},
        )
        assert leftovers == []

    def test_multi_session_shares_container_for_same_workspace(self):
        """Same workspace_id + same name across sessions reuses ONE container."""
        from infra.container_manager import ContainerManager  # lazy
        workspace_id = uuid.uuid4()
        session_id_1 = uuid.uuid4()
        session_id_2 = uuid.uuid4()
        manager1, res1 = self._start_manager(
            workspace_id=workspace_id, session_id=session_id_1
        )
        name = res1["name"]
        assert res1["status"] == "created", f"expected created, got {res1!r}"

        # Second manager: SAME workspace_id, DIFFERENT session_id.
        manager2 = ContainerManager(
            workspace_path=str(self.workspace_dir),
            session_id=session_id_2,
            workspace_id=workspace_id,
            session_permissions=None,
            image=self.image_tag,
        )
        res2 = manager2.start(name=name)
        assert res2["status"] == "reused", f"expected reuse, got {res2!r}"
        assert (res2.get("container_id") or res2.get("id")) == res1["id"], (
            f"expected same container, got {res1!r} vs {res2!r}"
        )
        # Exactly one container carries the workspace label.
        labeled = self.client.containers.list(
            all=True,
            filters={"label": f"thoughtmachine.workspace_id={workspace_id}"},
        )
        assert len(labeled) == 1, (
            f"expected exactly 1 workspace container, found {len(labeled)}"
        )

    def test_container_limit_enforced_before_create(self):
        """max_containers=1: second distinct name rejected, same name reused."""
        from infra.container_manager import ContainerManager  # lazy
        workspace_id = uuid.uuid4()
        manager = ContainerManager(
            workspace_path=str(self.workspace_dir),
            session_id=uuid.uuid4(),
            workspace_id=workspace_id,
            image=self.image_tag,
        )
        manager.max_containers = 1
        name_a = f"limit-a-{uuid.uuid4().hex[:8]}"
        name_b = f"limit-b-{uuid.uuid4().hex[:8]}"

        r_a = manager.start(name=name_a)
        assert r_a["status"] == "created", f"expected created, got {r_a!r}"
        self._tracked.append(r_a["id"])

        r_b = manager.start(name=name_b)
        assert r_b == {
            "error": "Workspace container limit (1) reached. "
                      "Stop an unused container first."
        }, f"unexpected limit result: {r_b!r}"

        r_a_again = manager.start(name=name_a)
        assert r_a_again["status"] == "reused", (
            f"expected reuse of existing container, got {r_a_again!r}"
        )
        assert (r_a_again.get("container_id") or r_a_again.get("id")) == r_a["id"]

    def test_created_container_has_workspace_and_package_mounts(self):
        """start() mounts BOTH the /workspace bind and the tm-packages volume."""
        workspace_id = uuid.uuid4()
        manager, res = self._start_manager(
            workspace_id=workspace_id, session_id=uuid.uuid4()
        )
        attrs = self.client.containers.get(res["id"]).attrs
        mounts = attrs["Mounts"]

        bind_mounts = [
            m for m in mounts
            if m.get("Type") == "bind" and m.get("Destination") == "/workspace"
        ]
        assert bind_mounts, f"no /workspace bind mount in {mounts!r}"

        pkg_mounts = [
            m for m in mounts
            if m.get("Type") == "volume"
            and m.get("Destination") == "/home/agent/.local"
        ]
        assert pkg_mounts, f"no /home/agent/.local volume mount in {mounts!r}"
        pkg = pkg_mounts[0]
        assert (pkg.get("Name") or pkg.get("Source")) == f"tm-packages-{workspace_id}", (
            f"unexpected package volume source: {pkg!r}"
        )

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


# ─────────────────────────────────────────────────────────────────────────────
# Sticky-note tests (mock-based, no daemon needed).
#
# Notes live on a per-workspace vault bulletin board
# (<vault_root>/workspaces/<workspace_id>/container_notes.json), NOT in Docker
# labels (docker SDK 7.1.0 has no update_labels; Container.update(**kwargs)
# forwards to POST /containers/{id}/update which ignores unknown fields), so
# these tests verify the API contract with a fake client: note persisted to the
# JSON file on create/reuse/set_note, note in the start/status/list responses,
# no thoughtmachine.note label anywhere, and per-workspace isolation.
# ─────────────────────────────────────────────────────────────────────────────


class FakeContainer:
    """Minimal docker Container stand-in recording update() calls."""

    def __init__(self, cid, name, labels=None, status="running", attrs=None):
        self.id = cid
        self.name = name
        self.labels = dict(labels or {})
        self.status = status
        self.image = SimpleNamespace(tags=["agent-executor:latest"])
        self.attrs = attrs or {
            "State": {"StartedAt": datetime.now(timezone.utc).isoformat()},
            "Mounts": [{"Destination": "/workspace", "RW": True}],
            "HostConfig": {"NetworkMode": "none"},
        }
        self.update_calls = []

    def reload(self):
        return None

    def update(self, **kwargs):
        self.update_calls.append(kwargs)
        if "labels" in kwargs:
            self.labels.update(kwargs["labels"])

    def start(self):
        self.status = "running"

    def stop(self, timeout=None):
        self.status = "exited"

    def kill(self):
        self.status = "exited"

    def remove(self, force=False):
        self.status = "removed"


class FakeContainers:
    """docker.models.containers.ContainerCollection stand-in."""

    def __init__(self, containers=None):
        self._all = list(containers or [])

    def list(self, all=True, filters=None):
        label_filter = (filters or {}).get("label")
        if label_filter is None:
            return list(self._all)
        if isinstance(label_filter, str):
            label_filter = [label_filter]
        result = []
        for c in self._all:
            matches = True
            for flt in label_filter:
                key, _, value = flt.partition("=")
                if c.labels.get(key) != value:
                    matches = False
                    break
            if matches:
                result.append(c)
        return result

    def get(self, container_id):
        for c in self._all:
            if c.id == container_id or c.name == container_id:
                return c
        raise docker.errors.NotFound("container not found")

    def run(self, **kwargs):
        labels = dict(kwargs.get("labels") or {})
        name = kwargs.get("name") or "fake-" + uuid.uuid4().hex[:8]
        c = FakeContainer("fake-" + uuid.uuid4().hex[:16], name, labels=labels)
        self._all.append(c)
        return c


class TestContainerNoteFileStore:
    """Mock-based tests for the sticky-note vault bulletin board (no daemon needed).

    Notes are persisted to ``<vault_root>/workspaces/<workspace_id>/container_notes.json``
    and shared by every manager of the workspace; Docker labels never carry the
    note (docker has no label-update API, so labels are immutable after create).
    """

    def _make_manager(self, fake_containers, workspace_id, vault_root):
        # Lazy import: top-level import of infra.container_manager triggers the
        # thoughtmachine.security circular-import cascade (see module docstring).
        from infra.container_manager import ContainerManager

        manager = ContainerManager.__new__(ContainerManager)
        manager.workspace_path = "/tmp/tm-note-test-ws"
        manager.session_id = "note-test-session"
        manager.workspace_id = workspace_id
        manager.vault_root = str(vault_root)
        manager.session_permissions = None
        manager.image = "agent-executor"
        manager.mem_limit = "512m"
        manager.cpu_quota = 50000
        manager._containers = {}
        manager.workspace_config = {}
        manager.max_containers = 4
        manager.client = SimpleNamespace(containers=fake_containers)
        manager.container_notes = manager._load_container_notes()
        manager._compute_config = lambda ws, wid, sp: ("none", "ro")
        return manager

    def _notes_file(self, vault_root, workspace_id):
        return Path(vault_root) / "workspaces" / str(workspace_id) / "container_notes.json"

    def _read_notes(self, vault_root, workspace_id):
        path = self._notes_file(vault_root, workspace_id)
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def test_set_note_writes_bulletin_board_file(self):
        vault_root = tempfile.mkdtemp(prefix="tm-note-")
        try:
            workspace_id = str(uuid.uuid4())
            fake = FakeContainers()
            manager = self._make_manager(fake, workspace_id, vault_root)
            r = manager.start(name="note-write", note="hello")
            assert r["status"] == "created"
            container = fake.get(r["id"])
            assert "thoughtmachine.note" not in container.labels
            assert self._read_notes(vault_root, workspace_id) == {
                "note-write": {"note": "hello"}
            }
            sn = manager.set_note(r["id"], "sticky")
            assert sn == {"success": True, "note": "sticky"}
            assert self._read_notes(vault_root, workspace_id) == {
                "note-write": {"note": "sticky"}
            }
        finally:
            shutil.rmtree(vault_root, ignore_errors=True)

    def test_note_loaded_for_running_container(self):
        vault_root = tempfile.mkdtemp(prefix="tm-note-")
        try:
            workspace_id = str(uuid.uuid4())
            fake = FakeContainers()
            manager = self._make_manager(fake, workspace_id, vault_root)
            r = manager.start(name="note-status", note="hello")
            assert r["status"] == "created"
            st = manager.status(r["id"])
            assert st["status"] == "running"
            assert st["note"] == "hello"
            entries = manager.list_containers()
            assert entries[0]["note"] == "hello"
        finally:
            shutil.rmtree(vault_root, ignore_errors=True)

    def test_unknown_container_has_no_note(self):
        vault_root = tempfile.mkdtemp(prefix="tm-note-")
        try:
            workspace_id = str(uuid.uuid4())
            fake = FakeContainers()
            manager = self._make_manager(fake, workspace_id, vault_root)
            missing = manager.set_note("fake-nonexistent", "x")
            assert missing["success"] is False
            assert "error" in missing
            notes_path = self._notes_file(vault_root, workspace_id)
            if notes_path.exists():
                assert json.loads(notes_path.read_text(encoding="utf-8")) == {}
        finally:
            shutil.rmtree(vault_root, ignore_errors=True)

    def test_no_thoughtmachine_note_label_on_create(self):
        vault_root = tempfile.mkdtemp(prefix="tm-note-")
        try:
            workspace_id = str(uuid.uuid4())
            fake = FakeContainers()
            manager = self._make_manager(fake, workspace_id, vault_root)
            r1 = manager.start(name="note-label-1", note="hello")
            assert r1["status"] == "created"
            assert "thoughtmachine.note" not in fake.get(r1["id"]).labels
            r2 = manager.start(name="note-label-2")
            assert r2["status"] == "created"
            assert "thoughtmachine.note" not in fake.get(r2["id"]).labels
        finally:
            shutil.rmtree(vault_root, ignore_errors=True)

    def test_notes_isolated_by_workspace(self):
        vault_root = tempfile.mkdtemp(prefix="tm-note-")
        try:
            workspace_x = str(uuid.uuid4())
            workspace_y = str(uuid.uuid4())
            fake = FakeContainers()
            manager1 = self._make_manager(fake, workspace_x, vault_root)
            r1 = manager1.start(name="note-cross", note="sticky")
            assert r1["status"] == "created"
            assert r1["note"] == "sticky"

            # A different session, same workspace + name -> reuse, same container,
            # and the note is re-read from the bulletin board file.
            manager2 = self._make_manager(fake, workspace_x, vault_root)
            manager2.session_id = "other-session"
            r2 = manager2.start(name="note-cross")
            assert r2["status"] == "reused"
            assert r2["id"] == r1["id"]
            assert r2["note"] == "sticky"
            entries = manager2.list_containers()
            assert entries[0]["note"] == "sticky"

            # Different workspace, same fake daemon: the workspace label filter
            # hides X's containers, and the fresh create gets no note and writes
            # no bulletin-board entry for Y.
            manager3 = self._make_manager(fake, workspace_y, vault_root)
            assert manager3.list_containers() == []
            r3 = manager3.start(name="note-cross")
            assert r3["status"] == "created"
            assert r3["id"] != r1["id"]
            assert r3["note"] == ""
            assert "note-cross" not in self._read_notes(vault_root, workspace_y)
        finally:
            shutil.rmtree(vault_root, ignore_errors=True)



class TestContainerWorkerLabel:
    """Mock-based tests for the worker->container label bridge (no daemon needed).

    Fresh creates stamp ``thoughtmachine.worker`` onto the container labels so
    worker teardown can reclaim them; containers created without a worker carry
    no such label; and the label is immutable after create, so a reuse never
    overwrites it with a different worker's name.
    """

    def _make_manager(self, fake_containers, workspace_id, vault_root):
        # Lazy import: top-level import of infra.container_manager triggers the
        # thoughtmachine.security circular-import cascade (see module docstring).
        from infra.container_manager import ContainerManager

        manager = ContainerManager.__new__(ContainerManager)
        manager.workspace_path = "/tmp/tm-worker-label-test-ws"
        manager.session_id = "worker-label-test-session"
        manager.workspace_id = workspace_id
        manager.vault_root = str(vault_root)
        manager.session_permissions = None
        manager.image = "agent-executor"
        manager.mem_limit = "512m"
        manager.cpu_quota = 50000
        manager._containers = {}
        manager.workspace_config = {}
        manager.max_containers = 4
        manager.client = SimpleNamespace(containers=fake_containers)
        manager.container_notes = manager._load_container_notes()
        manager._compute_config = lambda ws, wid, sp: ("none", "ro")
        return manager

    def test_worker_label_stamped_on_fresh_create(self):
        vault_root = tempfile.mkdtemp(prefix="tm-worker-label-")
        try:
            workspace_id = str(uuid.uuid4())
            fake = FakeContainers()
            manager = self._make_manager(fake, workspace_id, vault_root)
            r = manager.start(name="worker-owned", worker_name="w1")
            assert r["status"] == "created"
            container = fake.get(r["id"])
            assert container.labels["thoughtmachine.worker"] == "w1"
            assert container.labels["thoughtmachine.workspace_id"] == workspace_id
            assert container.labels["thoughtmachine.container_name"] == "worker-owned"
        finally:
            shutil.rmtree(vault_root, ignore_errors=True)

    def test_no_worker_label_without_worker_name(self):
        vault_root = tempfile.mkdtemp(prefix="tm-worker-label-")
        try:
            workspace_id = str(uuid.uuid4())
            fake = FakeContainers()
            manager = self._make_manager(fake, workspace_id, vault_root)
            r = manager.start(name="worker-owned")
            assert r["status"] == "created"
            container = fake.get(r["id"])
            assert "thoughtmachine.worker" not in container.labels
            assert container.labels["thoughtmachine.workspace_id"] == workspace_id
        finally:
            shutil.rmtree(vault_root, ignore_errors=True)

    def test_worker_label_not_added_on_reuse(self):
        vault_root = tempfile.mkdtemp(prefix="tm-worker-label-")
        try:
            workspace_id = str(uuid.uuid4())
            fake = FakeContainers()
            manager = self._make_manager(fake, workspace_id, vault_root)
            r1 = manager.start(name="shared", worker_name="w1")
            assert r1["status"] == "created"
            r2 = manager.start(name="shared", worker_name="w2")
            assert r2["status"] == "reused"
            assert r2["id"] == r1["id"]
            container = fake.get(r2["id"])
            assert container.labels["thoughtmachine.worker"] == "w1"
        finally:
            shutil.rmtree(vault_root, ignore_errors=True)

