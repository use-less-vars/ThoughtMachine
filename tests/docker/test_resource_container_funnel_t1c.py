"""Wave 9a T1c -- the resource-container destroy sites route through the
lifecycle funnel (``ContainerLifecycle.reconcile``).

Sites exercised:
  U6  ``ResourceContainerManager.ensure_resource`` -- the stale branch reconciles
      first; only ``decision.destroys`` removes+recreates, a refuse falls back to
      the host leaving the stale container ALIVE.
  U7  ``ResourceContainerManager.stop`` -- reconcile (explicit reap); a refuse
      leaves the container running; a missing container short-circuits.
  U8b ``sweep_stale_resource_containers`` -- reconcile per orphan; a refuse is
      HONOURED (orphan left, ``removed`` unchanged).

These tests deliberately use their OWN fakes (mirroring the conventions of
``tests/docker/test_resource_lifecycle.py``) so the suites stay independent.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import infra.resource_container_manager as rcm
from infra.container_lifecycle import ContainerLifecycle, ReconcileDecision

try:
    from docker.errors import NotFound
except Exception:  # pragma: no cover - docker SDK absent
    NotFound = Exception


# ---------------------------------------------------------------------------
# reconcile spy -- records every call, optionally forces a canned decision
# ---------------------------------------------------------------------------
class _ReconcileSpy:
    def __init__(self, monkeypatch, decision=None):
        self.calls = []
        self._real = ContainerLifecycle.reconcile
        self._decision = decision

        def spy(container, session, intent=None):
            self.calls.append((container, session, intent))
            if self._decision is not None:
                return self._decision
            return self._real(container, session, intent)

        monkeypatch.setattr(ContainerLifecycle, "reconcile", spy)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class _FakeImageRef:
    def __init__(self, image_id):
        self.id = image_id


class _FakeContainer:
    def __init__(
        self,
        container_id,
        name=None,
        image_id="sha256:img-current",
        status="running",
        labels=None,
        attrs=None,
    ):
        self.id = container_id
        self.name = name
        self.image = _FakeImageRef(image_id)
        self.status = status
        self.labels = dict(labels or {})
        self.attrs = dict(attrs or {})
        self.stop_calls = []
        self.remove_calls = 0

    def start(self):
        self.status = "running"

    def stop(self, timeout=None):
        self.stop_calls.append({"timeout": timeout})
        self.status = "exited"

    def remove(self, force=False):
        self.remove_calls += 1


class _FakeContainers:
    def __init__(self, containers=None):
        self.containers = list(containers or [])
        self._by_id = {c.id: c for c in self.containers}

    def list(self, all=True, filters=None):  # noqa: A002 - Docker SDK signature
        return list(self.containers)

    def get(self, cid):
        if cid in self._by_id:
            return self._by_id[cid]
        raise NotFound(cid)


class _FakeClient:
    def __init__(self, containers=None):
        self.containers = containers if containers is not None else _FakeContainers()


class _FakeDockerModule:
    """Stands in for the ``docker`` module: ``from_env()`` -> client."""

    def __init__(self, containers=None):
        self.containers = containers if containers is not None else _FakeContainers()
        self.from_env_calls = 0

    def from_env(self):
        self.from_env_calls += 1
        return _FakeClient(self.containers)


def _labels_for(workspace_id, kind="git", name="tm-res-abc-git"):
    return {
        rcm.ResourceContainerManager.WORKSPACE_LABEL: workspace_id,
        rcm.ResourceContainerManager.RESOURCE_LABEL: kind,
        rcm.ResourceContainerManager.CONTAINER_NAME_LABEL: name,
    }


def _iso(seconds_ago=0):
    dt = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    return dt.isoformat().replace("+00:00", "Z")


@pytest.fixture(autouse=True)
def _reset_image_ready():
    """Isolate the module-level image-readiness cache per test."""
    rcm._RESOURCE_IMAGE_READY = False
    yield
    rcm._RESOURCE_IMAGE_READY = False


def _mgr(monkeypatch, containers, workspace_id="ws-1", workspace_path="/tmp/ws"):
    monkeypatch.setattr(rcm, "docker", _FakeDockerModule(containers))
    return rcm.ResourceContainerManager(
        workspace_id=workspace_id, workspace_path=workspace_path
    )


# ===========================================================================
# U7 -- stop()
# ===========================================================================
def test_u7_stop_routes_through_funnel_and_stops(monkeypatch):
    c = _FakeContainer("c1", name="tm-res-x-git", labels=_labels_for("ws-1"))
    mgr = _mgr(monkeypatch, _FakeContainers([c]))
    monkeypatch.setattr(mgr, "_container_id", lambda: "c1")
    spy = _ReconcileSpy(monkeypatch)

    result = mgr.stop()

    assert result == {"status": "stopped", "container_id": "c1"}
    assert c.stop_calls == [{"timeout": 5}]
    assert len(spy.calls) == 1
    _, session, intent = spy.calls[0]
    assert intent.kind == "reap"
    assert intent.explicit is True
    assert intent.reason == "resource_stop"


def test_u7_stop_refused_leaves_container_running(monkeypatch):
    c = _FakeContainer("c1", name="tm-res-x-git", labels=_labels_for("ws-1"))
    mgr = _mgr(monkeypatch, _FakeContainers([c]))
    monkeypatch.setattr(mgr, "_container_id", lambda: "c1")
    _ReconcileSpy(
        monkeypatch, decision=ReconcileDecision("refuse", "nope", "default")
    )

    result = mgr.stop()

    assert result["status"] == "error"
    assert result["container_id"] == "c1"
    assert "resource stop refused" in result["error"]
    assert c.stop_calls == []


def test_u7_stop_missing_container(monkeypatch):
    mgr = _mgr(monkeypatch, _FakeContainers())
    monkeypatch.setattr(mgr, "_container_id", lambda: None)

    result = mgr.stop()

    assert result == {
        "status": "missing",
        "container_id": None,
        "error": "resource container not found",
    }


# ===========================================================================
# U6 -- ensure_resource() stale branch
# ===========================================================================
def _u6_setup(monkeypatch):
    containers = _FakeContainers()
    mgr = _mgr(monkeypatch, containers)
    cname = mgr.container_name
    c = _FakeContainer("c1", name=cname, labels=_labels_for("ws-1", name=cname))
    containers.containers.append(c)
    containers._by_id["c1"] = c
    # Force the stale predicate without image plumbing.
    monkeypatch.setattr(rcm, "_user_drift_axis", lambda container, user: "drift")
    monkeypatch.setattr(rcm, "_ensure_resource_image", lambda: True)
    new_c = _FakeContainer(
        "new-1", name=cname, labels=_labels_for("ws-1", name=cname)
    )
    monkeypatch.setattr(mgr, "_create_resource_container", lambda name=None: new_c)
    return mgr, c


def test_u6_ensure_resource_stale_removes_and_recreates(monkeypatch):
    mgr, c = _u6_setup(monkeypatch)
    spy = _ReconcileSpy(monkeypatch)

    result = mgr.ensure_resource("git")

    assert result["mode"] == "containerized"
    assert result["container_id"] == "new-1"
    assert c.remove_calls == 1
    assert len(spy.calls) == 1
    _, session, intent = spy.calls[0]
    assert intent.kind == "reap"
    assert intent.explicit is True
    assert intent.reason == "resource_stale_removal"


def test_u6_ensure_resource_stale_refused_falls_back(monkeypatch):
    mgr, c = _u6_setup(monkeypatch)
    _ReconcileSpy(
        monkeypatch, decision=ReconcileDecision("refuse", "nope", "default")
    )

    result = mgr.ensure_resource("git")

    assert result["mode"] == "host_fallback"
    assert "resource stale removal refused" in result["detail"]
    assert result["failure_reason"] == "fallback_used"
    assert result["fallback_used"] is True
    assert c.remove_calls == 0


# ===========================================================================
# U8b -- sweep_stale_resource_containers()
# ===========================================================================
def _u8b_orphan():
    return _FakeContainer(
        "c-orphan",
        name="tm-res-x-git",
        labels=_labels_for("ws-orphan"),
        attrs={"Created": _iso(2 * 86400)},
    )


def test_u8b_sweep_orphan_routes_through_funnel(monkeypatch):
    orphan = _u8b_orphan()
    monkeypatch.setattr(rcm, "docker", _FakeDockerModule(_FakeContainers([orphan])))
    spy = _ReconcileSpy(monkeypatch)

    result = rcm.sweep_stale_resource_containers(["ws-1"])

    assert result["removed"] == 1
    assert orphan.remove_calls == 1
    assert len(spy.calls) == 1
    _, session, intent = spy.calls[0]
    assert intent.kind == "reap"
    assert intent.explicit is True
    assert intent.reason == "resource_orphan_sweep"
    assert session is None
    assert intent.workspace_id == "ws-orphan"


def test_u8b_sweep_orphan_refused_keeps_container(monkeypatch):
    orphan = _u8b_orphan()
    monkeypatch.setattr(rcm, "docker", _FakeDockerModule(_FakeContainers([orphan])))
    _ReconcileSpy(
        monkeypatch, decision=ReconcileDecision("refuse", "nope", "default")
    )

    result = rcm.sweep_stale_resource_containers(["ws-1"])

    assert result["removed"] == 0
    assert orphan.remove_calls == 0
    assert "skipped" in result["detail"]
