"""Route-level three-state hardening-conformance verdict on container status.

Pins the Strand-A ruling c1 change: the workspace-scoped STATUS route

    GET /api/workspace/{workspace_id}/containers/{container_name}/status

gains a ``"hardening"`` object carrying a THREE-state verdict derived from
``infra.container_create._hardening_conformance`` applied to the container's
docker ``attrs``:

* ``{"status": "conformant", "failed": []}``      -- every present axis is hardened;
* ``{"status": "drifted", "failed": [<axes>]}``   -- one or more REAL axes are weak;
* ``{"status": "unverified", "failed": []}``      -- attrs unreadable / container
  unobtainable / any other error.

Fail-closed contract: the unverifiable case must NEVER read as ``conformant``,
and surfacing the verdict must NEVER turn a working status response into a 5xx.

No docker daemon: a fake manager (``_make_container_manager`` is monkeypatched)
exposes ``list_containers()`` and ``client.containers.get(...)`` returning a fake
container with a chosen ``.attrs``.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

import infra.container_create as cc
import web_ui.backend.server as server_module
from web_ui.backend.server import app

client = TestClient(app)

STATUS = "/api/workspace/{ws}/containers/{name}/status"


# ── Fakes ───────────────────────────────────────────────────────────────────


class _FakeContainer:
    """A container whose ``attrs`` is a plain dict."""

    def __init__(self, attrs):
        self.attrs = attrs
        self.status = "running"


class _RaisingAttrs:
    """A container whose ``attrs`` property RAISES on every access."""

    @property
    def attrs(self):
        raise RuntimeError("attrs unavailable")


class _FakeContainers:
    def __init__(self, container):
        self._container = container

    def get(self, container_id):
        return self._container


class _FakeClient:
    def __init__(self, container):
        self.containers = _FakeContainers(container)


class _FakeManager:
    """Minimal stand-in exposing ``list_containers()`` + ``client.containers``."""

    def __init__(self, container):
        self.client = _FakeClient(container)
        self._entries = [{"name": "c-a", "container_id": "docker-aaa"}]

    def list_containers(self):
        return list(self._entries)

    def status(self, container_id):
        return {"container_id": container_id, "status": "running",
                "name": "c-a"}


def _install(monkeypatch, container):
    manager = _FakeManager(container)
    monkeypatch.setattr(
        server_module, "_make_container_manager",
        lambda workspace_id, workspace_path="": manager)
    return manager


def _hardened_attrs():
    """attrs for a container that is FULLY conformant on all four axes."""
    recipe = cc._expected_hardening_recipe()
    return {
        "HostConfig": {
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges:true"],
            "ReadonlyRootfs": True,
        },
        "Config": {"User": recipe.user},
    }


# ── Tests ───────────────────────────────────────────────────────────────────


def test_conformant_container_reports_conformant(monkeypatch):
    """A fully hardened container reports ``conformant`` with no failed axes."""
    _install(monkeypatch, _FakeContainer(_hardened_attrs()))
    resp = client.get(STATUS.format(ws="ws1", name="c-a"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["hardening"] == {"status": "conformant", "failed": []}
    # The pre-existing status payload is preserved verbatim.
    assert body["container_id"] == "docker-aaa"
    assert body["status"] == "running"


def test_weak_cap_drop_reports_drifted(monkeypatch):
    """A present-but-wrong cap_drop is REAL drift, not conformant."""
    recipe = cc._expected_hardening_recipe()
    attrs = {
        "HostConfig": {"CapDrop": ["NET_RAW"]},
        "Config": {"User": recipe.user},
    }
    _install(monkeypatch, _FakeContainer(attrs))
    resp = client.get(STATUS.format(ws="ws1", name="c-a"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["hardening"] == {"status": "drifted", "failed": ["cap_drop"]}


def test_unreadable_attrs_reports_unverified(monkeypatch):
    """Unreadable attrs must read as ``unverified``, NEVER ``conformant``."""
    _install(monkeypatch, _RaisingAttrs())
    resp = client.get(STATUS.format(ws="ws1", name="c-a"))
    # Fail-soft for the response: still a 200, not a 5xx.
    assert resp.status_code == 200
    body = resp.json()
    assert body["hardening"] == {"status": "unverified", "failed": []}


def test_unobtainable_container_reports_unverified(monkeypatch):
    """If the container object cannot be fetched, the verdict is ``unverified``."""
    class _RaisingGet:
        def get(self, container_id):
            raise RuntimeError("no such container")

    class _Manager:
        def __init__(self):
            self.client = type("C", (), {"containers": _RaisingGet()})()
            self._entries = [{"name": "c-a", "container_id": "docker-aaa"}]

        def list_containers(self):
            return list(self._entries)

        def status(self, container_id):
            return {"container_id": container_id, "status": "running"}

    monkeypatch.setattr(
        server_module, "_make_container_manager",
        lambda workspace_id, workspace_path="": _Manager())
    resp = client.get(STATUS.format(ws="ws1", name="c-a"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["hardening"] == {"status": "unverified", "failed": []}
    assert body["container_id"] == "docker-aaa"
