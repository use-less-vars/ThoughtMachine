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


# ---------------------------------------------------------------------------
# Wave 9a-T1b -- drift handling in _ensure_container (B3 name-match refuse) +
# funnel sites U9 (_remove_container_by_workspace) and U10 (close()).
#
# B3 (name-match drift) refuses a workspace-owned container whose envelope
# cannot be shown to match the desired one: that "recreate" decision is routed
# through the container-lifecycle funnel, whose rule (10) refuses to destroy
# such a container.  B1 (fast-path drift on the cached live object) was reverted
# to its original destructive behaviour by the operator ruling (Gen 44):
# a drifted live container is reaped and recreated.  The other recreate branches
# (force_rebuild / stale image / ImageNotFound / dead / start-failed) are
# likewise explicitly destructive and are pinned here.
# ---------------------------------------------------------------------------


class _CreateReached(Exception):
    """Sentinel raised once the (offline) create path is reached."""


def _drifted_container(cid="c1", name="tm-c1", net="bridge", mode="rw",
                       status="running"):
    """A container whose (network, workspace_mode) drifts from the desired one."""
    c = FakeContainer(cid=cid, name=name, status=status)
    c.attrs = {
        "HostConfig": {"NetworkMode": net},
        "Mounts": [{"Destination": "/workspace", "Mode": mode}],
    }
    return c


def _matched_container(cid="c1", name="tm-c1", status="running"):
    """A container whose envelope matches the desired ``("none", "ro")``."""
    c = FakeContainer(cid=cid, name=name, status=status)
    c.attrs = {"HostConfig": {"NetworkMode": "none"}, "Mounts": []}
    return c


def _wire_ensure(executor, compute=("none", "ro")):
    """Neutralise the image/config probes so ``_ensure_container`` runs offline."""
    executor._ensure_image = lambda: None
    executor._compute_container_config = lambda: compute
    return executor


def _lifecycle_decisions(captured):
    return [ev for ev in captured
            if ev.data.get("event_type") == "container_lifecycle_decision"]


def _spy_reconcile_intents(monkeypatch):
    """Record every ``intent`` passed to ``ContainerLifecycle.reconcile``."""
    from infra.container_lifecycle import ContainerLifecycle

    seen = []
    real = ContainerLifecycle.reconcile

    def spy(container, session, intent=None):
        seen.append(intent)
        return real(container, session, intent)

    monkeypatch.setattr(ContainerLifecycle, "reconcile", staticmethod(spy))
    return seen


def _shortcircuit_create(monkeypatch):
    """Make the (offline) create path raise immediately at create_hardened_*."""
    import docker_executor as de
    import security.admission_gate as ag

    dummy = types.SimpleNamespace(mounts=[], tmpfs={})
    monkeypatch.setattr(de, "git_shadow_for_workspace", lambda *a, **k: dummy)
    monkeypatch.setattr(de, "_host_ids", lambda: (1000, 1000))
    monkeypatch.setattr(de, "_load_admission_capabilities", lambda *a, **k: None)
    monkeypatch.setattr(ag, "admit", lambda *a, **k: MagicMock())
    monkeypatch.setattr(
        de, "create_hardened_container",
        lambda *a, **k: (_ for _ in ()).throw(_CreateReached()),
    )


# --- B1: fast-path (cached live object) drift -> recreate (reverted) -------

def test_ensure_fastpath_drift_still_recreates(monkeypatch):
    """B1 reverted (Gen 44): a drifted live container is reaped & recreated."""
    import docker

    captured = _capture_events(monkeypatch)
    _shortcircuit_create(monkeypatch)
    executor = _wire_ensure(_make_executor())
    container = _drifted_container(net="bridge", mode="rw")
    executor.container = container
    # the post-recreate name lookup must miss so the create path is reached
    executor.client.containers.get.side_effect = docker.errors.NotFound("gone")

    with pytest.raises(_CreateReached):
        executor._ensure_container()

    assert container.stop_calls == 1
    assert container.remove_calls == 1
    assert executor.container is None
    # the drift was REAPED (destructive funnel decision), never refused
    decisions = _lifecycle_decisions(captured)
    assert len(decisions) == 1
    assert decisions[0].data["action"] == "reap"


def test_ensure_fastpath_config_match_still_reuses(monkeypatch):
    """A matching envelope is still reused unchanged (fast path)."""
    _capture_events(monkeypatch)
    executor = _wire_ensure(_make_executor())
    container = _matched_container()
    executor.container = container

    result = executor._ensure_container()

    assert result is None
    assert executor.container is container
    assert container.stop_calls == 0
    assert container.remove_calls == 0


# --- B3: name-match drift -> refuse ----------------------------------------

def test_ensure_namematch_drift_refuses(monkeypatch):
    from agent.events import EventType

    captured = _capture_events(monkeypatch)
    executor = _wire_ensure(_make_executor())
    executor.container = None
    existing = _drifted_container(cid="exist1", name="tm-exist")
    executor.client.containers.get.return_value = existing

    result = executor._ensure_container()

    assert result == {
        "status": "refused",
        "reason": "workspace-owned envelope mismatch; refusing to destroy",
        "container": existing.id,
    }
    assert existing.stop_calls == 0
    assert existing.remove_calls == 0
    assert executor.container is existing
    decisions = _lifecycle_decisions(captured)
    assert len(decisions) == 1
    assert decisions[0].type == EventType.CONTAINER_LIFECYCLE
    assert decisions[0].data["action"] == "refuse"


def test_ensure_namematch_drift_intent_is_use(monkeypatch):
    seen = _spy_reconcile_intents(monkeypatch)
    executor = _wire_ensure(_make_executor())
    executor.container = None
    executor.client.containers.get.return_value = _drifted_container(cid="exist1")

    executor._ensure_container()

    assert len(seen) == 1
    intent = seen[0]
    assert intent.explicit is False
    assert intent.kind == "use"
    assert intent.reason == "ensure_drift_config"


# --- ITEM 4 pins: the other recreate branches STILL reap -------------------

def test_ensure_force_rebuild_still_reaps(monkeypatch):
    _shortcircuit_create(monkeypatch)
    executor = _wire_ensure(_make_executor())
    executor.force_rebuild = True
    executor.container = None
    existing = _matched_container(cid="fr1", name="tm-fr")
    executor.client.containers.get.return_value = existing

    with pytest.raises(_CreateReached):
        executor._ensure_container()

    assert existing.stop_calls == 1
    assert existing.remove_calls == 1


def test_ensure_stale_image_still_reaps(monkeypatch):
    _shortcircuit_create(monkeypatch)
    executor = _wire_ensure(_make_executor())
    executor.container = None
    existing = _matched_container(cid="st1", name="tm-st")
    existing.attrs["Image"] = "sha256:" + "a" * 12
    executor.client.containers.get.return_value = existing
    image = MagicMock()
    image.id = "sha256:" + "b" * 12
    executor.client.images.get.return_value = image

    with pytest.raises(_CreateReached):
        executor._ensure_container()

    assert existing.stop_calls == 1
    assert existing.remove_calls == 1


def test_ensure_image_not_found_still_reaps(monkeypatch):
    import docker

    _shortcircuit_create(monkeypatch)
    executor = _wire_ensure(_make_executor())
    executor.container = None
    existing = _matched_container(cid="nf1", name="tm-nf")
    executor.client.containers.get.return_value = existing
    executor.client.images.get.side_effect = docker.errors.ImageNotFound("nope")

    with pytest.raises(_CreateReached):
        executor._ensure_container()

    assert existing.stop_calls == 1
    assert existing.remove_calls == 1


def test_ensure_dead_container_still_reaps(monkeypatch):
    executor = _wire_ensure(_make_executor())
    executor.container = None
    dead = _matched_container(cid="dead1", name="tm-dead", status="dead")
    executor.client.containers.get.return_value = dead

    with pytest.raises(Exception):
        executor._ensure_container()

    assert dead.stop_calls == 1
    assert dead.remove_calls == 1


def test_ensure_start_failed_container_still_reaps(monkeypatch):
    import docker

    executor = _wire_ensure(_make_executor())
    executor.container = None
    stopped = _matched_container(cid="stop1", name="tm-stop", status="created")

    def _boom(*a, **k):
        raise docker.errors.APIError("cannot start")

    stopped.start = _boom
    executor.client.containers.get.return_value = stopped

    with pytest.raises(Exception):
        executor._ensure_container()

    assert stopped.stop_calls == 1
    assert stopped.remove_calls == 1


# --- U9: _remove_container_by_workspace routes through the funnel ----------

def _wire_rebuild(monkeypatch, container):
    import docker_executor as de

    client = MagicMock()
    client.containers.get.return_value = container
    monkeypatch.setattr(de, "_resolve_workspace_id", lambda p: "ws-1")
    monkeypatch.setattr(de, "_resolve_container_config_via_gate",
                        lambda *a, **k: ("none", "ro"))
    monkeypatch.setattr(de.docker, "from_env", lambda: client)
    return de


def test_remove_container_by_workspace_destroys_on_explicit_reap(monkeypatch):
    from agent.events import EventType

    captured = _capture_events(monkeypatch)
    container = _matched_container(cid="c1", name="tm-c1")
    de = _wire_rebuild(monkeypatch, container)

    de._remove_container_by_workspace("/tmp/tm-2d-ws")

    assert container.stop_calls == 1
    assert container.remove_calls == 1
    decisions = _lifecycle_decisions(captured)
    assert len(decisions) == 1
    assert decisions[0].type == EventType.CONTAINER_LIFECYCLE
    assert decisions[0].data["action"] == "reap"


def test_remove_container_by_workspace_intent_is_explicit(monkeypatch):
    seen = _spy_reconcile_intents(monkeypatch)
    container = _matched_container(cid="c1", name="tm-c1")
    de = _wire_rebuild(monkeypatch, container)

    de._remove_container_by_workspace("/tmp/tm-2d-ws")

    assert len(seen) == 1
    assert seen[0].explicit is True
    assert seen[0].kind == "reap"
    assert seen[0].reason == "remove_container_by_workspace"


def test_remove_container_by_workspace_non_destroy_keeps(monkeypatch):
    _non_destroy_decision(monkeypatch)
    container = _matched_container(cid="c1", name="tm-c1")
    de = _wire_rebuild(monkeypatch, container)

    de._remove_container_by_workspace("/tmp/tm-2d-ws")

    assert container.stop_calls == 0
    assert container.remove_calls == 0


# --- U10: close() routes through the funnel --------------------------------

def test_close_destroys_on_explicit_reap(monkeypatch):
    from agent.events import EventType

    captured = _capture_events(monkeypatch)
    executor = _make_executor()
    container = _matched_container(cid="c1", name="tm-c1")
    executor.container = container

    executor.close()

    assert container.stop_calls == 1
    assert container.remove_calls == 1
    assert executor.container is None
    decisions = _lifecycle_decisions(captured)
    assert len(decisions) == 1
    assert decisions[0].type == EventType.CONTAINER_LIFECYCLE
    assert decisions[0].data["action"] == "reap"


def test_close_intent_is_explicit(monkeypatch):
    seen = _spy_reconcile_intents(monkeypatch)
    executor = _make_executor()
    executor.container = _matched_container(cid="c1", name="tm-c1")

    executor.close()

    assert len(seen) == 1
    assert seen[0].explicit is True
    assert seen[0].kind == "reap"
    assert seen[0].reason == "executor_close"


def test_close_non_destroy_keeps_container(monkeypatch):
    _non_destroy_decision(monkeypatch)
    executor = _make_executor()
    container = _matched_container(cid="c1", name="tm-c1")
    executor.container = container

    executor.close()

    assert container.stop_calls == 0
    assert container.remove_calls == 0
    assert executor.container is container

