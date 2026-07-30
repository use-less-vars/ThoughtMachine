"""Full session lifecycle integration test (capstone).

Proves the entire pipeline — vault, session creation, config apply, agent
query with PuppetLLM, reconfiguration, second query, and session deletion —
works deterministically without a real LLM.
"""

import pytest
import tempfile
from typing import Dict, List, Any
from uuid import uuid4

from agent.config.session_config import SessionConfig
from agent.config import AgentConfig
from agent.core.agent import Agent
from web_ui.backend.bridge import WebAgentBridge
from session.store import FileSystemSessionStore
from session.models import Session

from tests.mocks.puppet_agent import PuppetLLM
from tests.assertions.event_assertions import (
    assert_event_sequence,
    assert_tool_call,
    assert_respond,
)
from tests.integration.test_ws_config_roundtrip import (
    MockWebSocket,
    EventCollector,
    simulate_apply_config,
)


class TestFullSessionLifecycle:
    """Full lifecycle test: create, configure, query, reconfigure, query, delete."""

    @pytest.fixture
    def lifecycle_env(self, hermetic_vault):
        """Set up bridge, event collector, mock websocket, and session store."""
        collector = EventCollector()
        ws = MockWebSocket()
        store_dir = tempfile.mkdtemp(prefix="test_lifecycle_")
        session_store = FileSystemSessionStore(sessions_dir=store_dir)

        bridge = WebAgentBridge(session_store=session_store)
        bridge.set_event_callback(collector)

        yield bridge, collector, ws, session_store

    def _create_session(self, bridge, session_store):
        """Simulate the new_session WebSocket handler logic."""
        new_session = Session()
        new_session.metadata["source"] = "web_ui"
        new_session.ensure_name()

        bridge._session_config = SessionConfig(
            mode="custom",
            max_turns=100,
            session_permissions={},
            enabled_tools=[],
            provider_id="",
            model="",
            base_url="",
        )

        bridge._loaded_session = new_session
        session_id = new_session.session_id

        session_store.save_session(new_session)
        session_store.add_open_session(session_id)

        return session_id

    # ── Test 1: Tool change, no session_loaded ──────────────────────

    def test_apply_tool_change_no_session_loaded(self, lifecycle_env):
        """Apply tool change -> config_changed, no session_loaded."""
        bridge, collector, ws, session_store = lifecycle_env

        session_id = self._create_session(bridge, session_store)
        assert bridge._session_config.enabled_tools == []

        frontend_config = {
            "mode": "custom",
            "enabled_tools": ["FileEditor", "ReadFile"],
        }
        output = simulate_apply_config(bridge, frontend_config)

        assert bridge._session_config.enabled_tools == ["FileEditor", "ReadFile"]
        assert "error" not in output["result"]

        event = output["config_changed_event"]
        assert event["type"] == "config_changed"
        assert event["config"]["mode"] == "custom"
        tool_names = [t["name"] for t in event["config"].get("tools", [])]
        assert "FileEditor" in tool_names
        assert "ReadFile" in tool_names

        session_loaded_events = [
            e for e in collector.events if e.get("type") == "session_loaded"
        ]
        assert len(session_loaded_events) == 0

    # ── Test 2: Full lifecycle ─────────────────────────────────────

    def test_full_lifecycle(self, lifecycle_env):
        """Complete lifecycle: create -> configure -> query -> reconfigure -> query -> delete."""
        bridge, collector, ws, session_store = lifecycle_env

        # ── Step 1: Create session ──
        session_id = self._create_session(bridge, session_store)
        assert session_id is not None
        assert bridge._session_config.mode == "custom"

        # ── Step 2: Apply config ──
        frontend_config = {
            "mode": "custom",
            "enabled_tools": ["FileEditor", "ReadFile"],
        }
        output = simulate_apply_config(bridge, frontend_config)
        assert "error" not in output["result"]
        assert output["config_changed_event"]["type"] == "config_changed"
        assert bridge._session_config.enabled_tools == ["FileEditor", "ReadFile"]

        # ── Step 3: First query with PuppetLLM (tool call then respond) ──
        puppet = PuppetLLM(scenario=[
            {
                "type": "tool_call",
                "tool_name": "Read",
                "arguments": {"path": "test.txt"},
            },
            {
                "type": "assistant",
                "content": "File read successfully.",
            },
        ])
        agent = Agent(
            config=AgentConfig(mode="custom", api_key="test-key", enable_logging=False),
            session_id=session_id,
        )
        agent.llm_client.chat_completion = puppet.chat_completion

        events = list(agent.process_query("Read test.txt"))
        assert len(events) > 0

        # Assert event sequence: user_query -> tool_call -> agent_responded
        assert_event_sequence(events, ["user_query", "agent_responded"])
        assert_tool_call(events, "Read", {"path": "test.txt"})
        assert_respond(events, expected_content="File read successfully.")

        # ── Step 4: Reconfigure (add more tools) ──
        reconfigure_config = {
            "mode": "custom",
            "enabled_tools": ["FileEditor", "ReadFile", "DockerCodeRunner"],
        }
        output2 = simulate_apply_config(bridge, reconfigure_config)
        assert "error" not in output2["result"]
        assert output2["config_changed_event"]["type"] == "config_changed"
        assert "DockerCodeRunner" in bridge._session_config.enabled_tools
        assert bridge._session_config.enabled_tools == ["FileEditor", "ReadFile", "DockerCodeRunner"]

        # ── Step 5: Second query (assistant-only, no tool calls) ──
        puppet2 = PuppetLLM(scenario=[
            {
                "type": "assistant",
                "content": "Reconfiguration confirmed. Ready for next task.",
            },
        ])
        agent2 = Agent(
            config=AgentConfig(mode="custom", api_key="test-key", enable_logging=False),
            session_id=session_id,
        )
        agent2.llm_client.chat_completion = puppet2.chat_completion

        events2 = list(agent2.process_query("Confirm reconfiguration"))
        assert len(events2) > 0

        assert_event_sequence(events2, ["user_query", "agent_responded"])
        assert_respond(events2, expected_content="Reconfiguration confirmed. Ready for next task.")

        # ── Step 6: Delete session ──
        delete_result = bridge.delete_session(session_id)
        assert delete_result is True

        # Verify session is removed from store
        loaded = session_store.load_session(session_id)
        assert loaded is None, f"Session {session_id} should not exist after deletion"
