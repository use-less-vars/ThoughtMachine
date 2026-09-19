"""Behaviour tests for the retained container-lifecycle primitives.

``registry_wiring`` (the process-wide ContainerRegistry singleton) was removed;
the behaviour it used to mediate now lives directly on the retained primitives.
These tests pin that behaviour:

- ``ContainerManager.start`` creates a fresh container through the hardened
  docker path, caches it by name, and enforces the per-workspace container limit
  with its own summary message (no registry error is remapped);
- ``ContainerManager.stop``/``remove`` act on a tracked container via the docker
  path, resolving the name from the id and dropping the name from the cache;
- an untracked id still routes to the docker path and reports ``missing``;
- ``WorkerSupervisor.request_container``/``release_container`` delegate to the
  container manager's ``start``/``stop`` (the legacy path) rather than a
  registry.
"""

import docker
import pytest

from infra import container_manager
from infra.container_manager import ContainerManager
from infra.workspace_lifecycle_manager import WorkerSupervisor

_RESETTABLE_INDEXES = (
    "_NAME_INDEX",
    "_NAME_INDEX_COLLISIONS",
    "_NAME_INDEX_BUILT",
    "_NAME_MIGRATED",
    "_NOTES_WARNED",
    "_NOTES_MIGRATED",
)


def _reset_module_caches():
    for name in _RESETTABLE_INDEXES:
        cache = getattr(container_manager, name, None)
        if cache is not None:
            cache.clear()


@pytest.fixture(autouse=True)
def _tmp_vault(tmp_path, monkeypatch):
    """Isolate vault writes and clear the module-level name/note caches."""
    vault = tmp_path / "vault"
    vault.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(vault))
    _reset_module_caches()
    yield
    _reset_module_caches()


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------

class _FakeImageRef:
    def __init__(self, image_id="sha256:deadbeef", tags=None):
        self.id = image_id
        self.tags = list(tags or [])

    def __str__(self):
        return self.id


class _FakeCtr:
    """Duck-typed docker container recording the destructive calls made on it."""

    def __init__(self, cid, name, status="running", labels=None, image=None,
                 attrs=None):
        self.id = cid
        self.name = name
        self.status = status
        self.labels = dict(labels or {})
        self.image = image if image is not None else _FakeImageRef()
        self.attrs = attrs if attrs is not None else {"State": {"StartedAt": None}}
        self.stop_calls = []
        self.kill_calls = []
        self.removed = []
        self.reload_calls = 0

    def reload(self):
        self.reload_calls += 1
        return self

    def stop(self, timeout=None):
        self.stop_calls.append({"timeout": timeout})
        self.status = "stopped"
        return self

    def kill(self):
        self.kill_calls.append(True)
        self.status = "dead"
        return self

    def remove(self, **kwargs):
        self.removed.append(kwargs)
        return self


class _FakeContainers:
    def __init__(self, containers=None):
        self._by_id = {c.id: c for c in (containers or [])}
        self.run_calls = []
        self.list_calls = []

    def list(self, all=False, filters=None):
        self.list_calls.append({"all": all, "filters": filters})
        return list(self._by_id.values())

    def get(self, container_id):
        try:
            return self._by_id[container_id]
        except KeyError:
            raise docker.errors.NotFound(f"no such container: {container_id}")

    def run(self, *args, **kwargs):
        self.run_calls.append({"args": args, "kwargs": kwargs})
        ctr = _FakeCtr(
            "c-run-1",
            name=kwargs.get("name"),
            status="created",
            labels=kwargs.get("labels") or {},
        )
        self._by_id[ctr.id] = ctr
        return ctr


class _FakeDockerClient:
    def __init__(self, containers=None):
        self.containers = _FakeContainers(containers)

    def ping(self):
        return True


class _FakeCM:
    """Minimal container-manager double recording start/stop routing."""

    def __init__(self, session_config=None):
        self.session_config = session_config or {}
        self.started = []
        self.stopped = []

    def start(self, image=None, name=None, note=None):
        self.started.append({"image": image, "name": name, "note": note})
        return {"id": "legacy-1", "name": name or "agent-exec-x",
                "status": "created", "note": ""}

    def stop(self, container_id):
        self.stopped.append(container_id)
        return {"status": "stopped", "container_id": container_id}


def _make_container_manager(client=None, *, session_id="s1", workspace_id="w1",
                            session_config=None):
    """Bare ContainerManager wired for facade tests (no daemon)."""
    cm = ContainerManager.__new__(ContainerManager)
    cm.workspace_path = "/tmp/ws-test"
    cm.session_id = session_id
    cm.workspace_id = workspace_id
    cm.session_permissions = {}
    cm._session_config = dict(session_config) if session_config else {}
    cm.image = "agent-executor"
    cm.mem_limit = "1g"
    cm.cpu_quota = 100000
    cm._containers = {}
    cm.client = client if client is not None else _FakeDockerClient()
    cm._compute_config = lambda *a, **k: ("none", "ro")
    cm.vault_root = "/tmp/tm-vault-test"
    cm.container_notes = {}
    cm.max_containers = 4
    cm.workspace_config = {"max_containers": 4}
    cm.dockerfile_path = None
    return cm


def _make_supervisor(cm):
    return WorkerSupervisor("w1", cm, None, feature_flag_check=lambda: True)


# ---------------------------------------------------------------------------
# ContainerManager start path
# ---------------------------------------------------------------------------

def test_container_manager_start_delegates_fresh_create_to_registry():
    client = _FakeDockerClient()
    cm = _make_container_manager(client)

    result = cm.start(name="my-box")

    assert result == {"id": "c-run-1", "name": "my-box",
                      "status": "created", "note": ""}
    assert cm._containers == {"my-box": "c-run-1"}
    assert len(client.containers.run_calls) == 1
    call = client.containers.run_calls[0]
    assert call["args"][0] == "agent-executor"  # image is the first run() arg
    assert call["kwargs"]["name"] == "my-box"
    assert call["kwargs"]["labels"]["thoughtmachine.workspace_id"] == "w1"


def test_container_manager_start_registry_limit_error_is_mapped(monkeypatch):
    cm = _make_container_manager(_FakeDockerClient())
    cm.max_containers = 1
    # One running container already occupies the single slot; the manager
    # reports its own summary message instead of remapping a registry error.
    monkeypatch.setattr(
        cm, "list_containers",
        lambda: [{"container_id": "c-other", "name": "other-box",
                  "status": "running", "note": ""}],
    )

    result = cm.start(name="my-box")

    assert result == {
        "error": "Workspace container limit (1) reached (1 active container(s)). "
                 "Stop or remove a running container to free a slot."
    }
    assert "Container limit reached" not in result["error"]
    assert cm._containers == {}


# ---------------------------------------------------------------------------
# ContainerManager destructive paths
# ---------------------------------------------------------------------------

def test_container_manager_stop_destroys_via_registry_by_name():
    ctr = _FakeCtr("c123", "my-box", status="running")
    cm = _make_container_manager(_FakeDockerClient([ctr]))
    cm._containers["my-box"] = "c123"

    result = cm.stop("c123")

    assert result == {"status": "stopped", "container_id": "c123",
                      "name": "my-box"}
    assert ctr.status == "stopped"
    assert ctr.stop_calls == [{"timeout": 5}]
    assert cm._containers == {}


def test_container_manager_remove_destroys_via_registry_by_name():
    ctr = _FakeCtr("c123", "my-box", status="running")
    cm = _make_container_manager(_FakeDockerClient([ctr]))
    cm._containers["my-box"] = "c123"

    result = cm.remove("c123")

    assert result == {"status": "removed", "container_id": "c123"}
    assert ctr.removed == [{"force": True}]
    assert cm._containers == {}


def test_container_manager_stop_untracked_falls_back_to_legacy():
    cm = _make_container_manager(_FakeDockerClient())

    result = cm.stop("legacy-1")

    # Not present on the daemon -> the docker path reports it as missing.
    assert result["status"] == "missing"
    assert result["container_id"] == "legacy-1"


# ---------------------------------------------------------------------------
# WorkerSupervisor delegation (legacy container-management path)
# ---------------------------------------------------------------------------

def test_supervisor_request_container_delegates_to_registry():
    cm = _FakeCM()
    sup = _make_supervisor(cm)

    result = sup.request_container(
        {"image": "python:3.12", "name": "my-box"},
        worker_id="w2",
        session_id="s9",
    )

    assert cm.started == [
        {"image": "python:3.12", "name": "my-box", "note": None}
    ]
    assert result["id"] == "legacy-1"


def test_supervisor_release_container_destroys_via_registry():
    cm = _FakeCM()
    sup = _make_supervisor(cm)

    result = sup.release_container("abc")

    assert cm.stopped == ["abc"]
    assert result == {"status": "stopped", "container_id": "abc"}


def test_supervisor_release_container_untracked_falls_back_to_cm():
    cm = _FakeCM()
    sup = _make_supervisor(cm)

    result = sup.release_container("legacy-9")

    assert cm.stopped == ["legacy-9"]
    assert result == {"status": "stopped", "container_id": "legacy-9"}
    assert sup._active_container_ids == set()
