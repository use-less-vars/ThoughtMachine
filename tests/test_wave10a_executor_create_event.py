"""Wave 10a -- the executor CREATE path emits a ``container_lifecycle`` event.

Pin: driving the real ``DockerExecutor._ensure_container`` to its container
CREATE site emits exactly ONE ``CONTAINER_LIFECYCLE`` event with
``action="create"``.  This closes the wave-10a open item
``Emit a container_lifecycle event with action="create" when a container is
created.`` for the executor code path.

Hermetic: the docker client is a fake (never a live daemon); the vault is a tmp
dir via ``THOUGHTMACHINE_VAULT_ROOT``; the global event bus ``publish`` is
monkeypatched to capture the emitted events.  A captured event is itself proof
that ``ContainerLifecycle.record_create`` returned a NON-NULL event: the funnel
only calls ``_emit`` when the built event is not ``None``.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import infra.containers.executor as docker_executor
from infra.containers.executor import DockerExecutor
import security.admission_gate as admission_gate
from security.admission_gate import Allow

import infra.container_manager as container_manager

_WS = "ws-exec-create-event"


class _FakeCtr:
    """Minimal stand-in for the object ``containers.run`` returns."""

    def __init__(self, container_id="c-exec-create-1"):
        self.id = container_id
        self.name = container_id
        self.status = "running"
        self.attrs = {"State": {"Status": "running"}}

    def start(self):
        self.status = "running"

    def stop(self, timeout=None):
        pass

    def remove(self, **kwargs):
        pass

    def reload(self):
        pass


@pytest.fixture(autouse=True)
def _tmp_vault(tmp_path, monkeypatch):
    """Isolated default vault + a cleared module-level ``_NAME_INDEX``."""
    vault = tmp_path / "vault"
    vault.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(vault))

    def _reset():
        container_manager._NAME_INDEX.clear()
        container_manager._NAME_INDEX_COLLISIONS.clear()
        container_manager._NAME_INDEX_BUILT.clear()

    _reset()
    yield
    _reset()


def _drive_ensure_container(monkeypatch):
    """Drive ``DockerExecutor._ensure_container`` to the CREATE path hermetically.

    Mirrors the proven harness in ``tests/test_container_executor_record.py`` so
    the real create site (and the new event emission right after it) runs with no
    live Docker daemon.
    """
    try:
        from docker.errors import NotFound
    except Exception:  # pragma: no cover - docker SDK absent
        NotFound = LookupError

    client = MagicMock()
    client.containers.run.return_value = _FakeCtr("c-exec-create-1")
    client.containers.get.side_effect = NotFound("nope")
    client.volumes.get_or_create.return_value = MagicMock()

    monkeypatch.setattr(DockerExecutor, "_ensure_image", lambda self, *a, **k: None)
    monkeypatch.setattr(
        DockerExecutor, "_compute_container_config", lambda self: ("none", "ro")
    )
    monkeypatch.setattr(
        admission_gate, "admit", lambda request, *, probes=None: Allow(spec=request.spec)
    )
    # Pass the root-host guard (non-root host id).
    monkeypatch.setattr(docker_executor, "_host_ids", lambda: (1000, 1000))

    with patch("infra.containers.executor.docker.from_env", return_value=client):
        executor = DockerExecutor(
            workspace_path="/tmp/tm-exec-create-event-ws",
            image="agent-executor-test",
            network="none",
            mem_limit="128m",
            cpu_quota=50000,
            session_permissions={"container": True},
            workspace_id=_WS,
        )

    executor._ensure_container()
    return executor


def test_ensure_container_emits_exactly_one_create_lifecycle_event(monkeypatch):
    from agent.events import EventType, global_event_bus

    assert global_event_bus is not None, "expected a live global event bus"
    captured = []
    monkeypatch.setattr(global_event_bus, "publish", lambda ev: captured.append(ev))

    _drive_ensure_container(monkeypatch)

    events = [e for e in captured if e.type == EventType.CONTAINER_LIFECYCLE]
    create_events = [e for e in events if e.data.get("action") == "create"]

    # Exactly one create event; its presence proves a NON-NULL event was built
    # (``_emit`` is only reached when the funnel's event is not ``None``).
    assert len(create_events) == 1
    ev = create_events[0]
    assert ev is not None
    assert ev.data["container_id"] == "c-exec-create-1"
    assert ev.data["workspace_id"] == _WS
    assert ev.data["reason"] == "executor_container_created"
    assert ev.data["contributing_source"] == "docker_executor._ensure_container"
