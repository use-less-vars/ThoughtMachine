"""Wave 10a -- ``ContainerRestartTool`` delegates to ``ContainerManager.restart``.

Two contracts are pinned:

* the tool performs NO manual stop/start of its own -- it calls the native
  ``manager.restart(container_id)`` exactly once and surfaces the resulting
  envelope verbatim;
* a restart is a lifecycle action IN ITS OWN RIGHT: driving the real
  ``ContainerManager.restart`` path emits exactly ONE
  ``CONTAINER_LIFECYCLE`` event with ``action="restart"`` (never the
  shape-identical ``action="reap"`` a bare stop would log), and ``create`` vs
  ``restart`` are distinguishable action kinds.

Hermetic: the delegation test uses a MagicMock manager; the event test uses a
real ``ContainerManager`` built with ``__new__`` and patched seams (no Docker).
"""

import json
from unittest.mock import MagicMock, patch

import infra.container_manager as cm_mod
from infra.container_lifecycle import ContainerLifecycle
from infra.container_manager import ContainerManager
from tools.container_control import ContainerRestartTool


def _parse(result):
    if isinstance(result, str):
        return json.loads(result)
    return result


class _FakeContainer:
    """Minimal container stand-in; only attrs the funnel reads are set."""

    def __init__(self, container_id="abc", name="tm-x"):
        self.id = container_id
        self.name = name
        self.labels = {}
        self.status = "running"
        self.lifecycle_class = "ephemeral"


# --------------------------------------------------------------------------- (a)
def test_tool_delegates_to_manager_restart():
    manager = MagicMock()
    manager.restart.return_value = {
        "status": "restarted",
        "container_id": "abc",
        "name": "tm-x",
    }
    tool = ContainerRestartTool(container_id="abc")
    with patch.object(ContainerRestartTool, "_make_manager", return_value=manager):
        result = _parse(tool.execute())

    assert result["success"] is True
    assert result["status"] == "restarted"
    manager.restart.assert_called_once_with("abc")
    # The tool does NOT compose stop+start itself any more.
    manager.stop.assert_not_called()
    manager.start.assert_not_called()


# --------------------------------------------------------------------------- (b)
def _capture_lifecycle(monkeypatch):
    from agent.events import EventType, global_event_bus

    assert global_event_bus is not None, "expected a live global event bus"
    captured = []
    monkeypatch.setattr(global_event_bus, "publish", lambda ev: captured.append(ev))
    monkeypatch.setattr(cm_mod, "_audit", lambda *a, **k: None)
    return EventType, captured


def test_restart_emits_exactly_one_restart_event(monkeypatch):
    EventType, captured = _capture_lifecycle(monkeypatch)

    mgr = ContainerManager.__new__(ContainerManager)
    mgr.workspace_id = "ws-1"
    mgr.session_id = "sessA"
    mgr.session_permissions = {"container": True}
    container = _FakeContainer()
    mgr.client = MagicMock()
    mgr.client.containers.get.return_value = container

    # Patch the instance seams the real restart() consults so the REAL
    # reconcile + record_restart emission path runs against the fake.
    mgr.class_of = lambda c: "ephemeral"
    mgr._agent_access_denial = lambda cls: None
    mgr._stop_container = lambda c, cid: {"status": "stopped", "container_id": cid}
    mgr.start = lambda **kw: {"id": "c1", "name": "tm-c1", "status": "created"}

    tool = ContainerRestartTool(container_id="abc")
    with patch.object(ContainerRestartTool, "_make_manager", return_value=mgr):
        result = _parse(tool.execute())

    assert result["success"] is True
    assert result["status"] == "restarted"

    events = [e for e in captured if e.type == EventType.CONTAINER_LIFECYCLE]
    restart_events = [e for e in events if e.data["action"] == "restart"]
    reap_events = [e for e in events if e.data["action"] == "reap"]
    assert len(restart_events) == 1
    assert reap_events == []


# --------------------------------------------------------------------------- (c)
def test_create_and_restart_are_distinct_action_kinds(monkeypatch):
    EventType, captured = _capture_lifecycle(monkeypatch)
    container = _FakeContainer()

    ContainerLifecycle.record_create(
        container, workspace_id="ws-1", session_id="sessA",
    )
    ContainerLifecycle.record_restart(
        container, workspace_id="ws-1", session_id="sessA",
    )

    events = [e for e in captured if e.type == EventType.CONTAINER_LIFECYCLE]
    actions = [e.data["action"] for e in events]
    assert actions.count("create") == 1
    assert actions.count("restart") == 1
