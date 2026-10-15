"""Wave 9a-2d: the last three destroy sites migrated through the funnel.

Sites covered:

  * site 8  ``ResourceContainerManager.remove``               (explicit reap)
  * site 9  ``resource_container_manager.cleanup_workspace_resources``
                                                              (explicit reap)
  * site 2  ``DockerExecutor._reap_container`` (the ``_ensure_container``
            recreate paths; MUST stay destructive -- pinned by
            ``tests/trust/test_toggle_live.py``)

Invariants asserted for each site:

  * destroy-on-destroys: when the funnel decision says ``destroys`` (an
    explicit reap always does) the container is stopped/removed, and
  * exactly ONE ``CONTAINER_LIFECYCLE`` event is emitted per decision, and
  * non-destroy-keeps-alive: a (patched) non-destroy decision leaves the
    container untouched -- no ``stop`` / ``remove``.

The ``_ensure_container`` reuse paths never call the helper at all (covered by
``tests/trust/test_toggle_live.py``), so they are not re-exercised here.
"""

import types
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeContainer:
    """Minimal docker-container stand-in recording stop/remove calls."""

    def __init__(self, cid="c1", name="tm-c1", status="running", labels=None):
        self.id = cid
        self.name = name
        self.status = status
        self.labels = labels or {}
        self.attrs = {"HostConfig": {"NetworkMode": "none"}, "Mounts": []}
        self.stop_calls = 0
        self.remove_calls = 0

    def reload(self):
        return None

    def stop(self, timeout=None, **kwargs):
        self.stop_calls += 1

    def remove(self, force=False, **kwargs):
        self.remove_calls += 1


class _FakeImages:
    def get(self, tag):
        raise RuntimeError("no image in this unit test")

    def remove(self, *args, **kwargs):
        raise RuntimeError("no image in this unit test")


class _FakeContainers:
    def __init__(self, containers):
        self._containers = list(containers)

    def get(self, ref):
        for container in self._containers:
            if ref in (container.id, container.name):
                return container
        raise RuntimeError(f"container {ref!r} not found")

    def list(self, all=False, filters=None):
        return list(self._containers)


class _FakeClient:
    def __init__(self, containers):
        self.containers = _FakeContainers(containers)
        self.images = _FakeImages()


class _FakeDockerModule:
    def __init__(self, client):
        self._client = client

    def from_env(self):
        return self._client


def _manager(monkeypatch, containers):
    import infra.resource_container_manager as rcm

    client = _FakeClient(containers)
    monkeypatch.setattr(rcm, "docker", _FakeDockerModule(client))
    return rcm.ResourceContainerManager(
        workspace_id="ws-1", workspace_path="/tmp/tm-2d-ws"
    )


def _make_executor(session_permissions=None, workspace_id="ws-1"):
    from docker_executor import DockerExecutor

    client = MagicMock()
    with patch("docker_executor.docker.from_env", return_value=client):
        return DockerExecutor(
            workspace_path="/tmp/tm-2d-exec-ws",
            image="agent-executor-test",
            network="none",
            mem_limit="128m",
            cpu_quota=50000,
            force_rebuild=False,
            idle_timeout=600,
            session_permissions=session_permissions or {},
            workspace_id=workspace_id,
        )


def _capture_events(monkeypatch):
    from agent.events import global_event_bus

    assert global_event_bus is not None, "expected a live global event bus"
    captured = []
    monkeypatch.setattr(global_event_bus, "publish", lambda ev: captured.append(ev))
    return captured


def _non_destroy_decision(monkeypatch):
    from infra.container_lifecycle import ContainerLifecycle

    fake = types.SimpleNamespace(destroys=False, reason="refused(unit-test)")
    monkeypatch.setattr(
        ContainerLifecycle, "reconcile", staticmethod(lambda *a, **k: fake)
    )


# ---------------------------------------------------------------------------
# site 8 -- ResourceContainerManager.remove
# ---------------------------------------------------------------------------

def test_remove_destroys_on_explicit_reap(monkeypatch):
    from agent.events import EventType

    captured = _capture_events(monkeypatch)
    container = FakeContainer(cid="c1")
    mgr = _manager(monkeypatch, [container])
    monkeypatch.setattr(mgr, "_container_id", lambda: "c1")

    result = mgr.remove()

    assert result["status"] == "removed"
    assert container.stop_calls == 1
    assert container.remove_calls == 1
    # exactly one funnel decision event (CONTAINER_LIFECYCLE) for the
    # single decision -- the pre-existing removal event is emitted too, so
    # scope to the funnel's decision events.
    decisions = [ev for ev in captured
                 if ev.data.get("event_type") == "container_lifecycle_decision"]
    assert len(decisions) == 1
    assert decisions[0].type == EventType.CONTAINER_LIFECYCLE
    assert decisions[0].data["action"] == "reap"


def test_remove_non_destroy_keeps_container_alive(monkeypatch):
    _non_destroy_decision(monkeypatch)
    container = FakeContainer(cid="c1")
    mgr = _manager(monkeypatch, [container])
    monkeypatch.setattr(mgr, "_container_id", lambda: "c1")

    result = mgr.remove()

    assert result["status"] == "error"
    assert container.stop_calls == 0
    assert container.remove_calls == 0


# ---------------------------------------------------------------------------
# site 9 -- cleanup_workspace_resources
# ---------------------------------------------------------------------------

def test_cleanup_destroys_on_explicit_reap(monkeypatch):
    from agent.events import EventType

    captured = _capture_events(monkeypatch)
    c1 = FakeContainer(cid="c1")
    c2 = FakeContainer(cid="c2")
    import infra.resource_container_manager as rcm

    monkeypatch.setattr(rcm, "docker", _FakeDockerModule(_FakeClient([c1, c2])))

    result = rcm.cleanup_workspace_resources("ws-1")

    assert result["removed_containers"] == 2
    assert c1.remove_calls == 1
    assert c2.remove_calls == 1
    # exactly one funnel decision event (CONTAINER_LIFECYCLE) per container
    decisions = [ev for ev in captured
                 if ev.data.get("event_type") == "container_lifecycle_decision"]
    assert len(decisions) == 2
    assert all(ev.type == EventType.CONTAINER_LIFECYCLE for ev in decisions)
    assert [ev.data["action"] for ev in decisions] == ["reap", "reap"]


def test_cleanup_non_destroy_keeps_containers_alive(monkeypatch):
    _non_destroy_decision(monkeypatch)
    c1 = FakeContainer(cid="c1")
    import infra.resource_container_manager as rcm

    monkeypatch.setattr(rcm, "docker", _FakeDockerModule(_FakeClient([c1])))

    result = rcm.cleanup_workspace_resources("ws-1")

    assert result["removed_containers"] == 0
    assert c1.remove_calls == 0
    assert c1.stop_calls == 0


# ---------------------------------------------------------------------------
# site 2 -- DockerExecutor._reap_container (ensure_container recreate)
# ---------------------------------------------------------------------------

def test_ensure_reap_destroys_on_explicit_reap(monkeypatch):
    from agent.events import EventType

    captured = _capture_events(monkeypatch)
    executor = _make_executor()
    container = FakeContainer(cid="abc123def456")

    executor._reap_container(container)

    assert container.stop_calls == 1
    assert container.remove_calls == 1
    assert len(captured) == 1
    assert captured[0].type == EventType.CONTAINER_LIFECYCLE
    assert captured[0].data["action"] == "reap"


def test_ensure_reap_non_destroy_keeps_container_alive(monkeypatch):
    _non_destroy_decision(monkeypatch)
    executor = _make_executor()
    container = FakeContainer(cid="abc123def456")

    executor._reap_container(container)

    assert container.stop_calls == 0
    assert container.remove_calls == 0
