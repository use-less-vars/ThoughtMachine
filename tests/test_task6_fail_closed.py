"""Task 6 — fail-closed security behaviour.

Covers:
1. ``ToolExecutor`` denies tool execution when ``workspace_path`` is
   configured but its ``workspace_id`` cannot be resolved (fail-closed
   instead of silently falling back to potentially permissive defaults).
2. ``check_system`` reports ``effective_permissions={}`` plus a
   ``permission_fetch_error`` when the gate computation raises (fail-closed
   instead of returning the raw session permissions).
3. ``WorkerBusAdapter.emit_config_changed`` redacts secrets before the
   config is broadcast on the event bus.
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pytest

from agent.config.models import AgentConfig
from agent.core.state import AgentState
from agent.core.tool_executor import ToolExecutor, GATE_AVAILABLE
import agent.core.tool_executor as te_module
import tools.workspace.check_system as check_system_module
import tools.workspace.worker as worker_module
from tools.file_preview_tool import FilePreviewTool
from thoughtmachine.security import SessionPermissions

pytestmark = pytest.mark.skipif(
    not GATE_AVAILABLE, reason="security gate not importable in this env")


def _make_executor(workspace_path=None):
    config = AgentConfig(
        session_permissions=SessionPermissions(filesystem="banned"),
        workspace_path=workspace_path,
    )
    state = AgentState(config=config)
    executor = ToolExecutor(
        tool_classes=[FilePreviewTool],
        config=config,
        state=state,
    )
    return executor


def _run(executor):
    return executor._execute_single_tool(
        FilePreviewTool, {"filename": "/etc/passwd"}, "file_preview", 0,
        lambda: False, lambda: None, lambda: 0,
    )


# ─────────────────────────────────────────────────────────────────────
# 1. ToolExecutor fail-closed deny on unresolvable workspace_id
# ─────────────────────────────────────────────────────────────────────

class TestToolExecutorFailClosed:

    def test_denies_when_workspace_path_set_but_id_unresolvable(self, monkeypatch):
        monkeypatch.setattr(te_module, "resolve_workspace_id", lambda path: None)
        executor = _make_executor(workspace_path="/some/configured/workspace")
        result = _run(executor)
        assert "DENIED" in result.get("result", "")
        assert "fail-closed" in result.get("result", "")

    def test_no_fail_closed_deny_without_workspace_path(self, monkeypatch):
        # No workspace_path -> the gate must not fire the fail-closed branch
        # (execution proceeds into the normal permission check, which bans
        # filesystem access with the regular denied message).
        monkeypatch.setattr(te_module, "resolve_workspace_id", lambda path: None)
        executor = _make_executor(workspace_path=None)
        result = _run(executor)
        assert "fail-closed" not in result.get("result", "")

    def test_resolvable_id_bypasses_fail_closed_deny(self, monkeypatch):
        monkeypatch.setattr(te_module, "resolve_workspace_id", lambda path: "ws-123")
        executor = _make_executor(workspace_path="/some/workspace")
        result = _run(executor)
        assert "fail-closed" not in result.get("result", "")


# ─────────────────────────────────────────────────────────────────────
# 2. check_system effective-permission fail-closed
# ─────────────────────────────────────────────────────────────────────

class TestCheckSystemFailClosed:

    def test_gate_error_yields_empty_effective_and_error_field(self, monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError("gate exploded")

        monkeypatch.setattr(check_system_module, "GATE_AVAILABLE", True)
        monkeypatch.setattr(check_system_module, "get_effective_permissions", boom)

        tool = check_system_module.CheckSystem(
            query="my_config", session_permissions={"filesystem": "read"})
        out = tool._query_permissions(None)
        assert out["effective_permissions"] == {}
        assert "gate exploded" in (out.get("permission_fetch_error") or "")

    def test_success_path_has_no_error_field(self, monkeypatch):
        monkeypatch.setattr(check_system_module, "GATE_AVAILABLE", False)
        tool = check_system_module.CheckSystem(
            query="my_config", session_permissions={"filesystem": "read"})
        out = tool._query_permissions(None)
        assert out["permission_fetch_error"] is None
        assert out["effective_permissions"] == {"filesystem": "read"}


# ─────────────────────────────────────────────────────────────────────
# 3. WorkerBusAdapter.emit_config_changed redaction
# ─────────────────────────────────────────────────────────────────────

class TestEmitConfigChangedRedaction:

    def test_secrets_redacted_before_publish(self):
        published = []

        class FakeBus:
            def publish(self, event):
                # WorkerBusAdapter._publish passes a BaseEvent object.
                published.append((event.type.value if hasattr(event.type, "value")
                                  else str(event.type), event.data))

        adapter = worker_module.WorkerBusAdapter(
            event_bus=FakeBus(), worker_name="w1")
        adapter.emit_config_changed({
            "api_key": "sk-leak",
            "model": "gpt-4",
            "provider": {"openai_api_key": "sk-nested"},
        })
        assert len(published) == 1
        event, data = published[0]
        assert event == "config_changed"
        cfg = data["config"]
        assert cfg["api_key"] == "<redacted>"
        assert cfg["model"] == "gpt-4"
        assert cfg["provider"]["openai_api_key"] == "<redacted>"
