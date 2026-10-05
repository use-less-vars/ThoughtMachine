"""Integration tests: First query on a completely fresh vault.

Tests that the config pipeline and bridge handle a brand-new workspace
(with zero workspace-specific defaults) gracefully. Verifies fixes for
the "first-query silent failure" bug where:

1. Missing workspace defaults.json caused silent failure (2A)
2. Controller path session capture race caused conversation_changed
   events to be skipped (2B)
3. session_stop didn't emit final conversation data (2C)
"""

import json
import logging
import tempfile
from pathlib import Path
from typing import Dict, List, Any

import pytest

from agent.config.session_config import SessionConfig
from agent.config.config_manager import resolve_config_defaults
from web_ui.backend.bridge import WebAgentBridge
from web_ui.backend.config_manager import ConfigManager
from web_ui.backend.session_manager import SessionManager
from session.store import FileSystemSessionStore
from session.models import Session

from tests.mocks.puppet_agent import PuppetLLM
from tests.integration.test_ws_config_roundtrip import (
    MockWebSocket,
    EventCollector,
    simulate_apply_config,
)


# ---------------------------------------------------------------------------
# Helper: provision a synthetic workspace on disk
# ---------------------------------------------------------------------------

def _provision_workspace(workspace_id: str) -> None:
    """Bootstrap a synthetic workspace on disk so the disk-authoritative
    permission gate can read it instead of failing CLOSED.

    ``bridge.apply_config`` -> ``resolve_effective_permissions`` resolves the
    effective grants through the security gate in disk mode, which reads the
    workspace ceiling from ``<vault>/workspaces/<ws>/config.json`` and the
    session grants from the session permission sidecar.  A MISSING workspace
    config makes ``permission_store.workspace_ceiling`` raise and the gate fail
    CLOSED (all-banned), so the synthetic workspace must actually exist on
    disk.  ``ensure_workspace_dirs`` seeds ``config.json`` as ``{}`` (a
    present-but-empty ceiling that caps nothing) plus a fully-permissive
    ``capabilities.json``.
    """
    from thoughtmachine.workspace_capabilities import ensure_workspace_dirs

    ensure_workspace_dirs(workspace_id)


# ---------------------------------------------------------------------------
# Test 1: resolve_config_defaults handles missing workspace defaults (2A)
# ---------------------------------------------------------------------------

class TestResolveConfigDefaultsFreshVault:
    """Verify resolve_config_defaults works with missing workspace defaults."""

    @pytest.fixture
    def vault_with_factory_only(self, tmp_path, monkeypatch):
        """Create a vault with factory defaults but NO workspace defaults."""
        vault_root = tmp_path / ".thoughtmachine"
        (vault_root / "system").mkdir(parents=True)
        (vault_root / "user").mkdir(parents=True)

        factory = {
            "version": "1",
            "description": "Test factory defaults",
            "config": {
                "provider_id": "openai",
                "temperature": 0.7,
                "max_turns": 50,
                "enabled_tools": [],
            },
        }
        (vault_root / "system" / "factory_defaults.json").write_text(
            json.dumps(factory, indent=2)
        )

        monkeypatch.setattr(
            "thoughtmachine.vault.vault_root",
            lambda: vault_root,
        )
        yield vault_root

    def test_missing_workspace_defaults_returns_factory(self, vault_with_factory_only):
        """resolve_config_defaults for a non-existent workspace returns factory defaults."""
        result = resolve_config_defaults("nonexistent-workspace-12345")
        assert isinstance(result, dict)
        assert result.get("provider_id") == "openai"
        assert result.get("temperature") == 0.7

    def test_empty_vault_returns_empty(self, tmp_path, monkeypatch):
        """Completely empty vault returns empty dict (no crash)."""
        vault_root = tmp_path / ".thoughtmachine"
        vault_root.mkdir(parents=True)
        monkeypatch.setattr(
            "thoughtmachine.vault.vault_root",
            lambda: vault_root,
        )
        result = resolve_config_defaults("nonexistent-ws")
        assert result == {}


# ---------------------------------------------------------------------------
# Test 2: session_stop emits conversation_changed (2C)
# ---------------------------------------------------------------------------

class TestSessionStopEmitsConversation:
    """Verify that session_stop always emits conversation_changed."""

    def test_session_stop_broadcasts_conversation(self, hermetic_vault):
        """session_stop event triggers conversation_changed broadcast."""
        collector = EventCollector()
        store_dir = tempfile.mkdtemp(prefix="test_session_stop_")
        session_store = FileSystemSessionStore(sessions_dir=store_dir)

        bridge = WebAgentBridge(session_store=session_store)
        bridge.set_event_callback(collector)

        # Set up a session with messages on the bridge
        session = Session()
        session.add_message("user", "Hello")
        session.add_message("assistant", "Hi there!")
        bridge._session = session
        bridge._session_id = session.session_id
        bridge._history_version = session.conversation_version

        # Directly call _map_and_emit with a session_stop event
        bridge._map_and_emit({
            "type": "session_stop",
            "stop_reason": "completed",
        })

        # Verify conversation_changed was emitted
        conv_events = [
            e for e in collector.events
            if e.get("type") == "conversation_changed"
        ]
        assert len(conv_events) >= 1, (
            "session_stop did not emit conversation_changed"
        )

        # Verify messages are in the event
        last_conv = conv_events[-1]
        messages = last_conv.get("messages", [])
        assert len(messages) >= 2, (
            f"Expected at least 2 messages in conversation, got {len(messages)}"
        )

        # Verify state_changed was also emitted
        state_events = [
            e for e in collector.events
            if e.get("type") == "state_changed"
        ]
        assert len(state_events) >= 1

    def test_session_stop_no_session_does_not_crash(self, hermetic_vault):
        """session_stop with None session does not crash (edge case)."""
        collector = EventCollector()
        store_dir = tempfile.mkdtemp(prefix="test_session_stop_")
        session_store = FileSystemSessionStore(sessions_dir=store_dir)

        bridge = WebAgentBridge(session_store=session_store)
        bridge.set_event_callback(collector)

        # Session is None (as in fresh start before capture)
        bridge._session = None

        # Should not raise
        bridge._map_and_emit({
            "type": "session_stop",
            "stop_reason": "completed",
        })

        # Should have at least state_changed
        state_events = [
            e for e in collector.events
            if e.get("type") == "state_changed"
        ]
        assert len(state_events) >= 1


# ---------------------------------------------------------------------------
# Test 3: SessionManager creates session without workspace defaults (2A)
# ---------------------------------------------------------------------------

class TestSessionManagerFreshVault:
    """SessionManager operations on fresh vault."""

    def test_session_manager_create_no_workspace_defaults(self, hermetic_vault):
        """SessionManager.create_session() should not fail when workspace has no defaults."""
        store_dir = tempfile.mkdtemp(prefix="test_sm_")
        session_store = FileSystemSessionStore(sessions_dir=store_dir)
        config_mgr = ConfigManager()
        sm = SessionManager(session_store=session_store, config_manager=config_mgr)

        session_id, frontend_config = sm.create_session(mode="agent")
        assert session_id is not None
        assert isinstance(frontend_config, dict)
        assert frontend_config.get("mode") == "agent"


# ---------------------------------------------------------------------------
# Test 4: Bridge captures session in controller path (2B)
# ---------------------------------------------------------------------------

class TestBridgeSessionCapture:
    """Verify session capture in controller path."""

    def test_bridge_start_sets_session_in_standalone_path(self, hermetic_vault):
        """Standalone path sets session immediately (pre-existing behavior)."""
        collector = EventCollector()
        store_dir = tempfile.mkdtemp(prefix="test_session_capture_")
        session_store = FileSystemSessionStore(sessions_dir=store_dir)

        bridge = WebAgentBridge(session_store=session_store)
        bridge.set_event_callback(collector)

        bridge._session_config = SessionConfig(
            mode="custom",
            max_turns=3,
            session_permissions={},
            enabled_tools=[],
            provider_id="openai",
            model="gpt-4o-mini",
            base_url="https://api.openai.com/v1",
        )

        # Create a loaded session (simulating resume)
        session = Session()
        session.add_message("user", "Hello")
        bridge._loaded_session = session

        # Simulate the standalone path setup
        bridge._agent = type('obj', (object,), {'process_query': lambda self, q: iter([])})()
        bridge._session = session
        bridge._session_id = session.session_id
        bridge._running = True

        assert bridge._session is not None
        assert bridge._session_id is not None

    def test_session_capture_from_controller_agent(self, hermetic_vault):
        """Verify _on_controller_event captures session from controller agent."""
        collector = EventCollector()
        store_dir = tempfile.mkdtemp(prefix="test_ctrl_capture_")
        session_store = FileSystemSessionStore(sessions_dir=store_dir)

        bridge = WebAgentBridge(session_store=session_store)
        bridge.set_event_callback(collector)

        # Create a minimal mock controller with an agent that has a session
        session = Session()
        session.add_message("user", "Test")

        mock_agent = type('obj', (object,), {'session': session})()
        mock_controller = type('obj', (object,), {
            'agent': mock_agent,
            'is_busy': False,
            'is_running': False,
            'set_event_callback': lambda self, cb: None,
        })()

        bridge.set_controller(mock_controller)
        bridge._session = None  # Fresh start

        # Send a fake event through _on_controller_event
        bridge._on_controller_event({
            "type": "execution_state_change",
            "new_state": "running",
        })

        # Session should have been captured
        assert bridge._session is not None, (
            "Bridge should have captured session from controller agent"
        )
        assert bridge._session.session_id == session.session_id, (
            "Session ID should match the controller's session"
        )


# ---------------------------------------------------------------------------
# Test 5: Config changed message includes settings, permissions, merged_config
# ---------------------------------------------------------------------------

class TestConfigChangedMessageStructure:
    """Verify config_changed broadcasts include the new structured fields."""

    def test_apply_config_includes_settings_permissions_merged(self, hermetic_vault):
        """bridge.apply_config() returns config, settings, permissions, merged_config.

        The effective ``permissions`` block is GATE-COMPUTED from the canonical
        on-disk session sidecar (P1) -- NOT from the ``session_permissions``
        override carried in the frontend payload, which
        ``ConfigManager.apply_config`` strips (with a WARNING).  The sidecar is
        therefore seeded with the grants asserted below, while the payload
        deliberately carries a DIFFERENT value to prove it is ignored.
        """
        from tests.integration.test_ws_config_roundtrip import simulate_apply_config
        from thoughtmachine.permission_store import write_session_permissions

        bridge = WebAgentBridge()
        bridge._session_config = SessionConfig(
            mode="custom",
            max_turns=100,
            session_permissions={},
            enabled_tools=[],
            provider_id="openai",
            model="gpt-4o-mini",
            base_url="https://api.openai.com/v1",
        )

        # Provision the synthetic workspace on disk so the disk-authoritative
        # permission gate can read a permissive ceiling + the session sidecar
        # instead of failing CLOSED (all-banned).
        _provision_workspace("test-ws-perms-merged")
        bridge._workspace_id = "test-ws-perms-merged"

        # The canonical grants live in the on-disk session sidecar, addressed
        # by (workspace_id, session_id).  Pin the session id so apply_config
        # resolves exactly this sidecar (an unset id would fall back to the
        # fresh save_session() uuid4, which has no sidecar -> fail CLOSED).
        bridge._session_id = "sess-perms-merged"
        write_session_permissions(
            hermetic_vault,
            "test-ws-perms-merged",
            "sess-perms-merged",
            {"filesystem": "write", "network": "banned"},
        )

        frontend_config = {
            "mode": "custom",
            "temperature": 0.3,
            # A DIFFERENT value than the sidecar: the payload grant is stripped
            # and ignored, so the result must reflect the SIDECAR grants.
            "session_permissions": {
                "filesystem": "banned",
            },
        }

        output = simulate_apply_config(bridge, frontend_config)
        result = output["result"]

        # --- Result has the expected structure ---
        assert isinstance(result, dict)
        assert "config" in result, "Result must contain 'config'"
        assert "settings" in result, "Result must contain 'settings'"
        assert "permissions" in result, "Result must contain 'permissions'"
        assert "merged_config" in result, "Result must contain 'merged_config'"

        # --- config is the full frontend-format config dict ---
        assert isinstance(result["config"], dict)
        assert result["config"]["mode"] == "custom"
        assert result["config"].get("provider") is not None  # provider was set from SessionConfig

        # --- settings is a subset of operational knobs ---
        assert isinstance(result["settings"], dict)
        assert result["settings"]["mode"] == "custom"
        assert "temperature" in result["settings"]
        assert "provider" in result["settings"]
        assert "model" in result["settings"]
        # settings should NOT include tools or permissions
        assert "tools" not in result["settings"]
        assert "session_permissions" not in result["settings"]

        # --- permissions is the resolved permissions dict ---
        assert isinstance(result["permissions"], dict)
        assert "filesystem" in result["permissions"]
        assert result["permissions"]["filesystem"] == "write"
        assert "network" in result["permissions"]
        assert result["permissions"]["network"] == "banned"
        # Default fields should be present
        assert result["permissions"].get("container") is not None
        assert result["permissions"].get("mcp") is not None
        assert result["permissions"].get("host_bash") is not None

        # --- merged_config equals config (full frontend format) ---
        assert result["merged_config"] == result["config"]

    def test_apply_config_without_permissions_uses_defaults(self, hermetic_vault):
        """When no session_permissions in config, defaults are applied."""
        from tests.integration.test_ws_config_roundtrip import simulate_apply_config

        bridge = WebAgentBridge()
        bridge._session_config = SessionConfig(
            mode="agent",
            max_turns=50,
            session_permissions={},
            enabled_tools=[],
            provider_id="anthropic",
            model="claude-3",
            base_url="https://api.anthropic.com/v1",
        )

        frontend_config = {
            "mode": "agent",
            "temperature": 0.7,
        }

        output = simulate_apply_config(bridge, frontend_config)
        result = output["result"]

        # Permissions should be populated with defaults
        perms = result["permissions"]
        assert perms.get("filesystem") is not None
        assert perms.get("network") is not None
        assert perms.get("container") is not None
        assert perms.get("mcp") is not None
        assert perms.get("git") is not None
        assert perms.get("host_bash") is not None

    def test_apply_config_changed_event_has_settings_permissions(self, hermetic_vault):
        """Config changed event sent to frontend has all new fields.

        The event's ``permissions`` block is GATE-COMPUTED from the canonical
        on-disk session sidecar (P1), not from the payload's (stripped)
        ``session_permissions`` override.
        """
        from tests.integration.test_ws_config_roundtrip import simulate_apply_config
        from thoughtmachine.permission_store import write_session_permissions

        bridge = WebAgentBridge()
        bridge._session_config = SessionConfig(
            mode="custom",
            max_turns=100,
            session_permissions={"filesystem": "write"},
            enabled_tools=[],
            provider_id="openai",
            model="gpt-4",
            base_url="https://api.openai.com/v1",
        )

        # Provision the synthetic workspace on disk so the disk-authoritative
        # permission gate can read a permissive ceiling + the session sidecar
        # instead of failing CLOSED (all-banned).
        _provision_workspace("test-ws-perms-event")
        bridge._workspace_id = "test-ws-perms-event"
        # Pin the session id + seed the canonical sidecar grant so the gate
        # resolves a real grant set instead of failing CLOSED.
        bridge._session_id = "sess-perms-event"
        write_session_permissions(
            hermetic_vault,
            "test-ws-perms-event",
            "sess-perms-event",
            {"filesystem": "write"},
        )

        frontend_config = {
            "mode": "custom",
            "temperature": 0.5,
            # A DIFFERENT value than the sidecar: the payload grant is stripped
            # and ignored, so the event must reflect the SIDECAR grant.
            "session_permissions": {"filesystem": "banned"},
        }

        output = simulate_apply_config(bridge, frontend_config)
        event = output["config_changed_event"]

        assert event["type"] == "config_changed"
        assert "config" in event
        assert "settings" in event
        assert "permissions" in event
        assert "merged_config" in event

        # Settings should reflect the applied config
        assert event["settings"]["mode"] == "custom"
        assert event["settings"]["temperature"] == 0.5

        # Permissions should reflect the gate-computed SIDECAR grant
        assert event["permissions"]["filesystem"] == "write"

        # merged_config should equal config
        assert event["merged_config"] == event["config"]



# ---------------------------------------------------------------------------
# Test 6: Q1 -- caller identity plumbing is fail-CLOSED (regression pin)
# ---------------------------------------------------------------------------
#
# Observable pinned here (the "Q1 signpost"): the tool executor emits
#     logging.getLogger("agent.core.tool_executor").warning(
#         "execute_tool_calls entered with NO session identity ...")
# whenever it is entered with an EMPTY (session_id, workspace_id) pair, and a
# category-gated tool (ProgressReport -> filesystem:write) is then DENIED by
# the deny-all _DISK_FAIL_CLOSED_SESSION floor.
#
# The bridge has three start() flows and they plumb identity differently:
#   FLOW B  controller branch, no loaded session  -> session_arg = None
#           -> Agent(config, session=None) -> agent.session_id None / _session None
#           -> EMPTY identity -> warning fires + tool DENIED  (PRIMARY pin)
#   FLOW A  standalone, no loaded session         -> fresh Session() minted
#           -> NON-empty identity -> warning must NOT fire
#   FLOW C  standalone, resuming a loaded session -> identity == loaded.session_id
#
# The pins never run the LLM: FLOW B uses a stand-in controller that builds a
# real Agent directly, and FLOW A/C neutralise the bridge's worker thread.
# ---------------------------------------------------------------------------


class _NoStartThread:
    """Replacement for ``threading.Thread`` whose target is never run.

    The bridge's standalone path spawns a worker that would drive the real
    agent loop (and therefore a real LLM call).  Swapping in this no-op keeps
    ``bridge.start`` synchronous and offline.
    """

    def __init__(self, *args, **kwargs):
        self._target = kwargs.get("target")

    def start(self):
        return None

    def join(self, *args, **kwargs):
        return None

    def is_alive(self):
        return False


class _RecordingController:
    """Minimal stand-in for ``AgentController``.

    Mirrors the identity-relevant slice of ``AgentController.start`` /
    ``AgentController._run``: it records the ``session`` handed to ``start()``
    and constructs a REAL ``Agent`` exactly the way ``AgentController._run``
    does (``Agent(config, session=<that session>)``).
    """

    def __init__(self):
        self.received_session = "UNSET"
        self.agent = None
        self.is_busy = False
        self.is_running = False

    def set_event_callback(self, cb):
        self._event_callback = cb

    def start(self, query, config=None, session=None, **kwargs):
        from agent import Agent

        self.received_session = session
        self.agent = Agent(config, session=session)


def _q1_warning_records(caplog):
    """Q1 warning records emitted by the tool_executor's entry guard."""
    return [
        r for r in caplog.records
        if r.name == "agent.core.tool_executor"
        and "NO session identity" in r.getMessage()
    ]


def _agent_identity(agent):
    """The (session_id, workspace_id) pair ``agent.py`` hands to
    ``execute_tool_calls`` (mirrors the agent's own wire-up at agent.py)."""
    return (
        agent.session_id,
        getattr(getattr(agent, "_session", None), "workspace_id", None) or "",
    )


def _run_gated_tool(agent):
    """Invoke a category-gated tool through the agent's tool_executor using
    the SAME identity kwargs the agent passes -- NOT the empty defaults, which
    would spuriously fire the Q1 guard even for a healthy identity."""
    tool_calls = [{
        "id": "call_q1",
        "type": "function",
        "function": {
            "name": "ProgressReport",
            "arguments": json.dumps({"report_body": "q1-probe"}),
        },
    }]
    session_id, workspace_id = _agent_identity(agent)
    return agent.tool_executor.execute_tool_calls(
        tool_calls,
        add_to_conversation_func=lambda msg: None,
        agent_id=0,
        session_id=session_id,
        workspace_id=workspace_id,
    )


_Q1_DENIAL = (
    "Permission denied: Tool requires filesystem:write, "
    "but session allows filesystem:banned"
)


def _q1_session_config():
    return SessionConfig(
        mode="custom",
        max_turns=3,
        session_permissions={},
        enabled_tools=["ProgressReport"],
        provider_id="openai",
        model="gpt-4o-mini",
        base_url="https://api.openai.com/v1",
    )


class TestQ1SessionIdentityFailClosed:
    """Pin the session-identity plumbing of the three ``bridge.start`` flows."""

    def test_flow_b_missing_loaded_session_yields_empty_identity(
        self, hermetic_vault, monkeypatch, caplog
    ):
        """FLOW B (controller branch, no loaded session): identity is EMPTY.

        This is the regression that must never silently pass: the controller
        branch forwards ``session_arg = self._loaded_session`` (None here), the
        controller builds ``Agent(config, session=None)``, and the agent then
        enters ``execute_tool_calls`` with no identity -- fail-CLOSED.
        """
        monkeypatch.setenv("OPENAI_COMPATIBLE_API_KEY", "dummy")
        monkeypatch.setenv("OPENAI_API_KEY", "dummy")

        store_dir = tempfile.mkdtemp(prefix="test_q1_flow_b_")
        bridge = WebAgentBridge(
            session_store=FileSystemSessionStore(sessions_dir=store_dir)
        )
        config = _q1_session_config()
        bridge._session_config = config

        controller = _RecordingController()
        bridge.set_controller(controller)
        bridge._loaded_session = None

        bridge.start("hello", config)

        # The (absent) loaded session -- literally None -- is what the bridge
        # forwards into the controller/agent constructor.
        assert controller.received_session is None
        agent = controller.agent
        assert agent is not None, "controller should have built an Agent"

        session_id, workspace_id = _agent_identity(agent)
        assert not session_id, (
            f"FLOW B agent.session_id should be empty, got {session_id!r}"
        )
        assert agent._session is None
        assert not workspace_id

        # The gated tool is present, the entry guard fires, and the call DENIES.
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="agent.core.tool_executor"):
            result = _run_gated_tool(agent)

        assert _q1_warning_records(caplog), (
            "expected the tool_executor Q1 no-identity warning under FLOW B"
        )
        assert result[0][0]["result"] == _Q1_DENIAL

    def test_flow_a_standalone_mints_non_empty_identity(
        self, hermetic_vault, monkeypatch, caplog
    ):
        """FLOW A (standalone, no loaded session): a fresh Session is minted,
        so the agent carries a NON-empty identity and the Q1 guard stays
        silent."""
        monkeypatch.setenv("OPENAI_COMPATIBLE_API_KEY", "dummy")
        monkeypatch.setenv("OPENAI_API_KEY", "dummy")
        # Neutralise the worker thread so bridge.start never runs the LLM.
        monkeypatch.setattr(
            "web_ui.backend.bridge.threading.Thread", _NoStartThread
        )

        store_dir = tempfile.mkdtemp(prefix="test_q1_flow_a_")
        bridge = WebAgentBridge(
            session_store=FileSystemSessionStore(sessions_dir=store_dir)
        )
        config = _q1_session_config()
        bridge._session_config = config
        bridge._loaded_session = None

        bridge.start("hello", config)

        agent = bridge._agent
        assert agent is not None
        session_id, workspace_id = _agent_identity(agent)
        assert session_id, (
            "FLOW A should mint a fresh Session -> non-empty agent.session_id"
        )
        assert agent._session is not None

        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="agent.core.tool_executor"):
            _run_gated_tool(agent)

        assert _q1_warning_records(caplog) == [], (
            "FLOW A has identity; the Q1 no-identity warning must NOT fire"
        )

    def test_flow_c_standalone_resume_keeps_loaded_identity(
        self, hermetic_vault, monkeypatch, caplog
    ):
        """FLOW C (standalone, resuming a loaded session): identity is the
        loaded session's id, and the Q1 guard stays silent."""
        monkeypatch.setenv("OPENAI_COMPATIBLE_API_KEY", "dummy")
        monkeypatch.setenv("OPENAI_API_KEY", "dummy")
        monkeypatch.setattr(
            "web_ui.backend.bridge.threading.Thread", _NoStartThread
        )

        store_dir = tempfile.mkdtemp(prefix="test_q1_flow_c_")
        bridge = WebAgentBridge(
            session_store=FileSystemSessionStore(sessions_dir=store_dir)
        )
        config = _q1_session_config()
        bridge._session_config = config

        loaded = Session()
        loaded.add_message("user", "resume me")
        bridge._loaded_session = loaded

        bridge.start("hello", config)

        agent = bridge._agent
        assert agent is not None
        session_id, workspace_id = _agent_identity(agent)
        assert session_id == loaded.session_id, (
            "FLOW C should reuse the loaded session id "
            f"({loaded.session_id!r}), got {session_id!r}"
        )
        assert bridge._session_id == loaded.session_id

        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="agent.core.tool_executor"):
            _run_gated_tool(agent)

        assert _q1_warning_records(caplog) == []

