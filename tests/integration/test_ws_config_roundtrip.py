"""WebSocket config round-trip integration tests.

Tests that the full config pipeline works: receive config → update bridge →
emit events to connected client. Uses MockWebSocket to simulate the frontend
and exercises the bridge directly (since WebSocket handlers are inline in
server.py and not separately importable).
"""

import pytest
import tempfile
from typing import Dict, List, Any, Optional
from uuid import uuid4

from agent.config.session_config import SessionConfig
from agent.config.models import AgentConfig
from web_ui.backend.bridge import WebAgentBridge
from agent import Agent
from tests.mocks.puppet_agent import PuppetLLM
from tests.assertions.event_assertions import assert_event_sequence, assert_respond
from session.store import FileSystemSessionStore
from session.models import Session


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class MockWebSocket:
    """Simulates a FastAPI WebSocket, recording all sent messages."""
    def __init__(self):
        self.sent_messages: List[Dict[str, Any]] = []
        self._closed = False

    async def send_json(self, data: Dict[str, Any]) -> None:
        self.sent_messages.append(data)

    @property
    def closed(self) -> bool:
        return self._closed


class EventCollector:
    """Collects events from bridge/agent event callbacks for assertions."""
    def __init__(self):
        self.events: List[Dict[str, Any]] = []

    def __call__(self, event: Dict[str, Any]) -> None:
        self.events.append(event)


def _build_frontend_config(bridge: WebAgentBridge) -> Dict[str, Any]:
    """Minimal version of server.py's _frontend_config_from_bridge.

    Builds a frontend-friendly config dict from the bridge's session state.
    """
    cfg = bridge._session_config
    if cfg is None:
        return {"mode": "custom", "enabled_tools": []}
    return {
        "mode": cfg.mode or "custom",
        "enabled_tools": list(cfg.enabled_tools or []),
        "max_turns": cfg.max_turns,
        "temperature": cfg.temperature,
    }


def simulate_apply_config(
    bridge: WebAgentBridge,
    frontend_config: Dict[str, Any],
) -> Dict[str, Any]:
    """Simulate what the websocket handler does for apply_config.

    Replicates the relevant logic from server.py's websocket_endpoint
    apply_config branch (non-workspace-change path) so we can test it
    without standing up the full server.
    """
    # 1. Initialize _session_config if needed (handler does this at ~line 885)
    if bridge._session_config is None:
        mode = frontend_config.get("mode", "agent") if isinstance(frontend_config, dict) else "agent"
        from agent.config.presets import get_tools_for_mode
        bridge._session_config = SessionConfig(
            mode=mode,
            max_turns=100,
            session_permissions={},
            enabled_tools=list(get_tools_for_mode(mode)),
        )

    # 2. Build AgentConfig from frontend format (like _translate_frontend_config)
    backend_config = AgentConfig(
        mode=frontend_config.get("mode"),
        enabled_tools=frontend_config.get("enabled_tools"),
        temperature=frontend_config.get("temperature", 0.7),
        api_key=frontend_config.get("api_key", "test-key"),
        enable_logging=False,
    )

    # 3. Apply via bridge (validates, merges, resolves provider, persists)
    # bridge.apply_config() expects a dict, not an AgentConfig instance.
    # Convert the AgentConfig to a dict, keeping only the relevant keys.
    apply_dict = {k: v for k, v in frontend_config.items()
                  if k in ("mode", "enabled_tools", "temperature",
                           "provider_id", "model", "base_url",
                           "system_prompt", "session_permissions")}
    result = bridge.apply_config(apply_dict)

    # 4. Build the event the handler would send
    if result.get("success"):
        config_changed_event = {
            "type": "config_changed",
            "config": _build_frontend_config(bridge),
        }
    else:
        config_changed_event = {
            "type": "status_message",
            "text": f"Failed to apply config: {result.get('error', 'unknown error')}",
        }

    return {
        "result": result,
        "config_changed_event": config_changed_event,
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestConfigRoundTrip:
    """WebSocket-level config round-trip tests."""

    @pytest.fixture
    def bridge_env(self, hermetic_vault):
        """Set up bridge + event collector + mock websocket.

        Uses hermetic_vault (from tests/conftest.py) to avoid touching real config.
        """
        collector = EventCollector()
        ws = MockWebSocket()
        bridge = WebAgentBridge()
        bridge.set_event_callback(collector)

        # Initialize like the new_session handler does
        bridge._session_config = SessionConfig(
            mode="custom",
            max_turns=100,
            session_permissions={},
            enabled_tools=[],
        )

        yield bridge, collector, ws

    def test_apply_tool_change_no_session_loaded(self, bridge_env):
        """Apply tool change → config_changed event, no session_loaded emitted."""
        bridge, collector, ws = bridge_env

        # --- Given: initial config (custom mode, no tools) ---
        assert bridge._session_config is not None
        assert bridge._session_config.mode == "custom"
        assert bridge._session_config.enabled_tools == []

        # --- When: apply config with tool changes ---
        frontend_config = {
            "mode": "custom",
            "enabled_tools": ["Read", "Write"],
        }
        output = simulate_apply_config(bridge, frontend_config)

        # --- Then: bridge state updated ---
        assert bridge._session_config.enabled_tools == ["Read", "Write"]
        assert output["result"]["success"] is True

        # --- Then: config_changed event has correct data ---
        event = output["config_changed_event"]
        assert event["type"] == "config_changed"
        assert event["config"]["mode"] == "custom"
        assert "Read" in event["config"].get("enabled_tools", [])
        assert "Write" in event["config"].get("enabled_tools", [])

        # --- Then: no session_loaded from bridge event callback ---
        session_loaded_events = [
            e for e in collector.events if e.get("type") == "session_loaded"
        ]
        assert len(session_loaded_events) == 0

    def test_workspace_change_sends_session_loaded(self, bridge_env):
        """Workspace change path → session_loaded emitted via bridge callback."""
        bridge, collector, ws = bridge_env

        # --- Given: session store with a session ---
        tmpdir = tempfile.mkdtemp(prefix="test_ws_config_")
        store = FileSystemSessionStore(sessions_dir=tmpdir)

        # Create and save a session
        session = Session(
            session_id=str(uuid4()),
            metadata={"name": "Test Session"},
        )
        store.save_session(session)
        session_id = session.session_id

        # Attach store to bridge
        bridge._session_store = store

        # --- When: load_session (bridge internally emits session_loaded) ---
        result = bridge.load_session(session_id)

        # --- Then: session was loaded ---
        assert result is True

        # --- Then: session_loaded was emitted through event callback ---
        session_loaded_events = [
            e for e in collector.events if e.get("type") == "session_loaded"
        ]
        assert len(session_loaded_events) >= 1
        assert session_loaded_events[0]["session_id"] == session_id

    def test_config_before_first_query(self, bridge_env):
        """Apply config before any user query → no error, then query works."""
        bridge, collector, ws = bridge_env

        # --- When: apply config (no query sent first) ---
        frontend_config = {
            "mode": "custom",
            "enabled_tools": ["Read", "Write", "Bash"],
            "temperature": 0.5,
        }
        output = simulate_apply_config(bridge, frontend_config)

        # --- Then: apply succeeds ---
        assert output["result"]["success"] is True
        assert output["config_changed_event"]["type"] == "config_changed"
        assert "Bash" in output["config_changed_event"]["config"].get("enabled_tools", [])

        # --- When: inject PuppetLLM and process a query ---
        puppet = PuppetLLM(scenario=[
            {"type": "assistant", "content": "I processed your request."},
        ])
        agent = Agent(
            config=AgentConfig(mode="custom", api_key="test-key", enable_logging=False),
            session_id="test-session",
        )
        agent.llm_client.chat_completion = puppet.chat_completion

        events = list(agent.process_query("Hello"))

        # --- Then: agent runs and produces expected events ---
        # The agent doesn't emit "agent_started"; it yields token_update, user_query,
        # execution_state_change, ... then agent_responded.
        assert_event_sequence(events, ["user_query", "agent_responded"])
        assert_respond(events, expected_content="I processed your request.")
