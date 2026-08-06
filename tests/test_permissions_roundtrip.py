"""
Integration test: Permissions round-trip through save/load cycle.

There are TWO separate permission systems in this codebase:

1. **AgentConfig.session_permissions** (SessionPermissions Pydantic BaseModel)
   → Used by ToolExecutor._execute_single_tool() to gate tool access.
   → Round-trips through AgentConfig.model_dump() → JSON → AgentConfig(**dict).

2. **Session.security_config** (plain Dict[str, Any])
   → Used by Session dataclass for session-level security policy.
   → Round-trips through Session.to_dict() → JSON → Session.from_dict().

These two systems are NOT bridged — changing one does not affect the other.
This test validates that each system round-trips correctly independently.
"""

import json
import os
import tempfile
import pytest
from datetime import datetime
from typing import List, ClassVar

from agent.config.models import AgentConfig
from agent.config.loader import save_config, load_config
from thoughtmachine.security import SessionPermissions, get_default_security_config, merge_security_config
from session.models import Session, RuntimeParams
from agent.core.tool_executor import (
    DEFAULT_SESSION_PERMISSIONS,
    ToolExecutor,
)
from tools.base import ToolBase


# =========================================================================
# Stub tools
# =========================================================================

class FileWriteTool(ToolBase):
    """A tool that requires filesystem:write."""
    tool: str = "FileWriteTool"
    required_categories: ClassVar[List[str]] = ["filesystem:write"]

    def execute(self) -> str:
        return "Write OK"


class ContainerTool(ToolBase):
    """A tool that requires container:true."""
    tool: str = "ContainerTool"
    required_categories: ClassVar[List[str]] = ["container:true"]

    def execute(self) -> str:
        return "Container OK"


class NetworkTool(ToolBase):
    """A tool that requires network:true."""
    tool: str = "NetworkTool"
    required_categories: ClassVar[List[str]] = ["network:true"]

    def execute(self) -> str:
        return "Network OK"


class MultiRequirementTool(ToolBase):
    """A tool that requires multiple categories."""
    tool: str = "MultiRequirementTool"
    required_categories: ClassVar[List[str]] = ["container:true", "filesystem:write", "network:true"]

    def execute(self) -> str:
        return "Multi OK"


# =========================================================================
# Test 1: AgentConfig → SessionPermissions round-trip via model_dump/init
# =========================================================================

class TestSessionPermissionsRoundTrip:
    """Validate that SessionPermissions survives AgentConfig serialization."""

    def test_default_round_trip(self):
        """Default SessionPermissions survives model_dump → re-init."""
        cfg1 = AgentConfig()
        d = cfg1.model_dump()
        cfg2 = AgentConfig(**d)

        sp1 = cfg1.session_permissions
        sp2 = cfg2.session_permissions

        assert sp2.container == sp1.container
        assert sp2.network == sp1.network
        assert sp2.filesystem == sp1.filesystem
        assert sp2.system == sp1.system
        assert sp2.git == sp1.git
        assert sp2.execution == sp1.execution

    def test_custom_permissions_round_trip(self):
        """Custom SessionPermissions survives model_dump → re-init."""
        cfg1 = AgentConfig()
        cfg1.session_permissions = SessionPermissions(
            container=True,
            network=True,
            filesystem="full",
            system="write",
            git="read",
            execution="banned",
        )
        d = cfg1.model_dump()
        cfg2 = AgentConfig(**d)

        sp2 = cfg2.session_permissions
        assert sp2.container is True
        assert sp2.network == "write"  # True coercees to 'write'
        assert sp2.filesystem == "full"
        assert sp2.system == "write"
        assert sp2.git == "read"
        assert sp2.execution == "banned"

    def test_permissive_permissions_round_trip(self):
        """Maximally permissive SessionPermissions round-trips correctly."""
        cfg1 = AgentConfig()
        cfg1.session_permissions = SessionPermissions(
            container=True,
            network=True,
            filesystem="full",
            system="full",
            git="full",
            execution="full",
        )
        d = cfg1.model_dump()
        cfg2 = AgentConfig(**d)

        sp2 = cfg2.session_permissions
        assert sp2.container is True
        assert sp2.network == "write"  # True coercees to 'write'
        assert sp2.filesystem == "full"
        assert sp2.system == "full"
        assert sp2.git == "full"
        assert sp2.execution == "full"

    def test_restrictive_permissions_round_trip(self):
        """Restrictive permissions round-trip correctly."""
        cfg1 = AgentConfig()
        cfg1.session_permissions = SessionPermissions(
            container=False,
            network=False,
            filesystem="read",
            system="banned",
            git="banned",
            execution="banned",
        )
        d = cfg1.model_dump()
        cfg2 = AgentConfig(**d)

        sp2 = cfg2.session_permissions
        assert sp2.container is False
        assert sp2.network == "banned"  # False coercees to 'banned'
        assert sp2.filesystem == "read"
        assert sp2.system == "banned"
        assert sp2.git == "banned"
        assert sp2.execution == "banned"

    def test_model_dump_is_serializable(self):
        """model_dump() output must be JSON-serializable (no Pydantic models leaked)."""
        cfg = AgentConfig()
        cfg.session_permissions = SessionPermissions(
            container=True,
            network=False,
            filesystem="write",
            system="read",
            git="full",
            execution="banned",
        )
        d = cfg.model_dump()
        # Should not raise TypeError
        json_str = json.dumps(d, indent=2)
        parsed = json.loads(json_str)
        assert parsed["session_permissions"]["container"] is True
        assert parsed["session_permissions"]["filesystem"] == "write"
        assert parsed["session_permissions"]["execution"] == "banned"

    def test_exclude_api_key_keeps_permissions(self):
        """Excluding api_key from serialization must NOT drop session_permissions."""
        cfg = AgentConfig()
        cfg.session_permissions = SessionPermissions(filesystem="full")
        d = cfg.model_dump(exclude={"api_key"})
        assert "session_permissions" in d
        assert d["session_permissions"]["filesystem"] == "full"


# =========================================================================
# Test 2: AgentConfig → JSON file → AgentConfig (full file I/O round-trip)
# =========================================================================

class TestConfigFileRoundTrip:
    """Full save-to-file / load-from-file round-trip of AgentConfig including permissions."""

    @pytest.fixture
    def temp_config_path(self):
        """Provide a temporary JSON config path."""
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
            path = f.name
        yield path
        if os.path.exists(path):
            os.unlink(path)

    def test_default_permissions_survive_file_io(self, temp_config_path):
        """Default permissions survive save_config → load_config cycle."""
        cfg = AgentConfig()
        config_dict = cfg.model_dump(exclude={"api_key"}, exclude_none=True)

        # Save to file
        save_config(config_dict, temp_config_path)

        # Load from file and reconstruct
        loaded_dict = load_config(temp_config_path)
        cfg2 = AgentConfig(**loaded_dict)

        sp1 = cfg.session_permissions
        sp2 = cfg2.session_permissions

        assert sp2.container == sp1.container
        assert sp2.network == sp1.network
        assert sp2.filesystem == sp1.filesystem
        assert sp2.system == sp1.system
        assert sp2.git == sp1.git
        assert sp2.execution == sp1.execution

    def test_custom_permissions_survive_file_io(self, temp_config_path):
        """Custom permissions survive save_config → load_config cycle."""
        cfg = AgentConfig()
        cfg.session_permissions = SessionPermissions(
            container=True,
            network=True,
            filesystem="full",
            system="write",
            git="read",
            execution="banned",
        )
        config_dict = cfg.model_dump(exclude={"api_key"}, exclude_none=True)

        save_config(config_dict, temp_config_path)
        loaded_dict = load_config(temp_config_path)
        cfg2 = AgentConfig(**loaded_dict)

        sp2 = cfg2.session_permissions
        assert sp2.container is True
        assert sp2.network == "write"  # True coercees to 'write'
        assert sp2.filesystem == "full"
        assert sp2.system == "write"
        assert sp2.git == "read"
        assert sp2.execution == "banned"

    def test_json_file_contents_are_human_readable(self, temp_config_path):
        """The saved JSON should have readable permission values."""
        cfg = AgentConfig()
        cfg.session_permissions = SessionPermissions(
            container=True,
            network=False,
            filesystem="write",
        )
        config_dict = cfg.model_dump(exclude={"api_key"}, exclude_none=True)
        save_config(config_dict, temp_config_path)

        with open(temp_config_path, "r") as f:
            raw = json.load(f)

        sp = raw["session_permissions"]
        assert sp["container"] is True
        assert sp["network"] == "banned"  # False coercees to 'banned'
        assert sp["filesystem"] == "write"
        assert sp["system"] == "read"  # default
        assert sp["execution"] == "banned"  # default

    def test_missing_session_permissions_backfilled_from_defaults(self, temp_config_path):
        """If loaded JSON has no session_permissions key, defaults are used."""
        cfg = AgentConfig()
        # Save a minimal config without session_permissions
        config_dict = cfg.model_dump(exclude={"api_key", "session_permissions"}, exclude_none=True)
        save_config(config_dict, temp_config_path)

        loaded_dict = load_config(temp_config_path)
        cfg2 = AgentConfig(**loaded_dict)

        # Should have factory defaults (layered config: resources/default_config.json
        # deep-merges the PERMISSIVE profile — verified at runtime in the container:
        # SessionPermissions(container=True, network='write', filesystem='write',
        #                    system='read', git='read', execution='banned'))
        sp = cfg2.session_permissions
        assert sp.container is True
        assert sp.network == "write"  # factory default (True coerces to 'write')
        assert sp.filesystem == "write"  # factory default
        assert sp.system == "read"  # factory default
        assert sp.git == "read"  # factory default
        assert sp.execution == "banned"

    def test_partial_permissions_backfilled_from_defaults(self, temp_config_path):
        """Partial session_permissions should have missing fields backfilled."""
        raw = {
            "session_permissions": {
                "container": True,
                # network, filesystem, security, git, execution missing
            }
        }
        with open(temp_config_path, "w") as f:
            json.dump(raw, f)

        loaded_dict = load_config(temp_config_path)
        cfg = AgentConfig(**loaded_dict)

        sp = cfg.session_permissions
        assert sp.container is True       # from file
        assert sp.network == "write"      # factory default (network: true -> 'write')
        assert sp.filesystem == "write"   # factory default
        assert sp.execution == "banned"


# =========================================================================
# Test 3: Session.security_config round-trip
# =========================================================================

class TestSessionSecurityConfigRoundTrip:
    """Validate that Session.security_config survives to_dict/from_dict."""

    def test_default_security_config_round_trip(self):
        """Default security_config survives to_dict → from_dict."""
        session = Session(
            session_id="test-session",
            runtime_params=RuntimeParams(),
        )
        d = session.to_dict()
        restored = Session.from_dict(d)

        assert restored.security_config == get_default_security_config()

    def test_custom_security_config_round_trip(self):
        """Custom security_config survives to_dict → from_dict."""
        custom_config = {
            "version": 1,
            "session_policy": {
                "read_only": True,
                "allowed_networks": ["*.example.com"],
                "tool_overrides": {"FileEditor": "deny"},
                "default_policy": "deny",
                "capability_requirements": {"fs:write": "ask"},
            },
            "agent_overrides": {},
        }
        session = Session(
            session_id="test-session-2",
            runtime_params=RuntimeParams(),
            security_config=custom_config,
        )
        d = session.to_dict()
        restored = Session.from_dict(d)

        policy = restored.security_config["session_policy"]
        assert policy["read_only"] is True
        assert policy["allowed_networks"] == ["*.example.com"]
        assert policy["tool_overrides"] == {"FileEditor": "deny"}
        assert policy["default_policy"] == "deny"

    def test_security_config_isolation(self):
        """Modifying restored Session should not affect original."""
        original = Session(
            session_id="test-session-3",
            runtime_params=RuntimeParams(),
        )
        d = original.to_dict()
        restored = Session.from_dict(d)

        # Modify restored
        restored.security_config["session_policy"]["read_only"] = True
        # Original should be unchanged
        assert original.security_config["session_policy"]["read_only"] is False

    def test_security_config_json_serializable(self):
        """Session.to_dict() must be JSON-serializable."""
        session = Session(
            session_id="test-session-4",
            runtime_params=RuntimeParams(),
            security_config={
                "version": 1,
                "session_policy": {
                    "read_only": True,
                    "allowed_networks": [],
                    "tool_overrides": {},
                    "default_policy": "allow",
                    "capability_requirements": {},
                },
                "agent_overrides": {},
            },
        )
        d = session.to_dict()
        json_str = json.dumps(d, indent=2)
        parsed = json.loads(json_str)
        assert parsed["security_config"]["session_policy"]["read_only"] is True


# =========================================================================
# Test 4: Combined — After config cycle, tool execution still works
# =========================================================================

class FakeState:
    """Minimal state stub for ToolExecutor tests."""
    security_config = None
    agent_context = None


class TestToolExecutionAfterConfigCycle:
    """After loading a config with custom permissions, ToolExecutor enforces them."""

    def _make_executor(self, tool_classes, config):
        return ToolExecutor(
            tool_classes=tool_classes,
            config=config,
            state=FakeState(),
            logger=None,
            security_available=False,
            agent=None,
        )

    def test_permissive_config_allows_write_after_cycle(self, tmp_path):
        """After save/load cycle with full permissions, file writes are allowed."""
        cfg1 = AgentConfig()
        cfg1.session_permissions = SessionPermissions(
            container=True,
            network=True,
            filesystem="full",
            system="full",
            git="full",
            execution="full",
        )
        config_path = os.path.join(tmp_path, "test_config.json")
        config_dict = cfg1.model_dump(exclude={"api_key"}, exclude_none=True)
        save_config(config_dict, config_path)

        loaded_dict = load_config(config_path)
        cfg2 = AgentConfig(**loaded_dict)
        executor = self._make_executor([FileWriteTool], cfg2)

        result = executor._execute_single_tool(
            FileWriteTool, {}, "FileWriteTool", 0,
            lambda: False, lambda: None, lambda: 0
        )
        assert result["result"] == "Write OK"

    def test_restrictive_config_denies_after_cycle(self, tmp_path):
        """After save/load cycle with restrictive permissions, writes are denied."""
        cfg1 = AgentConfig()
        cfg1.session_permissions = SessionPermissions(
            container=False,
            network=False,
            filesystem="read",  # <--- read only
            system="read",
            git="read",
            execution="banned",
        )
        config_path = os.path.join(tmp_path, "test_config_restrictive.json")
        config_dict = cfg1.model_dump(exclude={"api_key"}, exclude_none=True)
        save_config(config_dict, config_path)

        loaded_dict = load_config(config_path)
        cfg2 = AgentConfig(**loaded_dict)
        executor = self._make_executor([FileWriteTool], cfg2)

        result = executor._execute_single_tool(
            FileWriteTool, {}, "FileWriteTool", 0,
            lambda: False, lambda: None, lambda: 0
        )
        assert "Permission denied" in result["result"]
        assert "filesystem:write" in result["result"]

    def test_multi_requirement_tool_after_cycle(self, tmp_path):
        """Multi-requirement tools are correctly gated after config cycle."""
        cfg1 = AgentConfig()
        cfg1.session_permissions = SessionPermissions(
            container=True,
            network=True,
            filesystem="full",
            system="full",
            git="full",
            execution="full",
        )
        config_path = os.path.join(tmp_path, "test_config_multi.json")
        config_dict = cfg1.model_dump(exclude={"api_key"}, exclude_none=True)
        save_config(config_dict, config_path)

        loaded_dict = load_config(config_path)
        cfg2 = AgentConfig(**loaded_dict)
        executor = self._make_executor([MultiRequirementTool], cfg2)

        result = executor._execute_single_tool(
            MultiRequirementTool, {}, "MultiRequirementTool", 0,
            lambda: False, lambda: None, lambda: 0
        )
        assert result["result"] == "Multi OK"

    def test_multi_requirement_denied_if_one_missing_after_cycle(self, tmp_path):
        """One missing permission blocks multi-requirement tool after cycle."""
        cfg1 = AgentConfig()
        cfg1.session_permissions = SessionPermissions(
            container=True,
            network=False,  # <--- missing
            filesystem="full",
            system="full",
            git="full",
            execution="full",
        )
        config_path = os.path.join(tmp_path, "test_config_deny_multi.json")
        config_dict = cfg1.model_dump(exclude={"api_key"}, exclude_none=True)
        save_config(config_dict, config_path)

        loaded_dict = load_config(config_path)
        cfg2 = AgentConfig(**loaded_dict)
        executor = self._make_executor([MultiRequirementTool], cfg2)

        result = executor._execute_single_tool(
            MultiRequirementTool, {}, "MultiRequirementTool", 0,
            lambda: False, lambda: None, lambda: 0
        )
        assert "Permission denied" in result["result"]
        assert "network" in result["result"]


# =========================================================================
# Test 5: Bridge awareness — Session.security_config DOES NOT affect tool execution
# =========================================================================

class TestSessionConfigDoesNotBridgeToToolExecution:
    """Demonstrate that Session.security_config is NOT the same as
    AgentConfig.session_permissions. Changing one does NOT affect the other.
    This is by design — they are separate systems."""

    def test_session_config_is_independent_of_tool_permissions(self):
        """Session.security_config can be set to 'deny' everything but
        tool execution still uses AgentConfig.session_permissions."""
        restrictive_session_config = {
            "version": 1,
            "session_policy": {
                "read_only": True,
                "allowed_networks": [],
                "tool_overrides": {"FileWriteTool": "deny"},
                "default_policy": "deny",
                "capability_requirements": {"fs:write": "deny"},
            },
            "agent_overrides": {},
        }
        session = Session(
            session_id="bridge-test",
            runtime_params=RuntimeParams(),
            security_config=restrictive_session_config,
        )
        # Verify the session loaded restrictive config
        assert session.security_config["session_policy"]["default_policy"] == "deny"

        # Now verify ToolExecutor doesn't use this — it uses AgentConfig
        cfg = AgentConfig()
        cfg.session_permissions = SessionPermissions(filesystem="full")

        # Even though Session says deny, ToolExecutor says full → allowed
        executor = ToolExecutor(
            tool_classes=[FileWriteTool],
            config=cfg,
            state=FakeState(),
            logger=None,
            security_available=False,
            agent=None,
        )
        result = executor._execute_single_tool(
            FileWriteTool, {}, "FileWriteTool", 0,
            lambda: False, lambda: None, lambda: 0
        )
        assert result["result"] == "Write OK"

    def test_session_config_can_be_independently_modified(self):
        """Modifying Session.security_config has zero effect on AgentConfig."""
        cfg = AgentConfig()
        cfg.session_permissions = SessionPermissions(filesystem="read")

        session = Session(
            session_id="bridge-test-2",
            runtime_params=RuntimeParams(),
        )
        # Modify session security_config
        session.security_config["session_policy"]["default_policy"] = "deny"

        # AgentConfig is unaffected
        assert cfg.session_permissions.filesystem == "read"
        assert cfg.session_permissions.container is False

        # to_dict is unaffected
        d = session.to_dict()
        assert d["security_config"]["session_policy"]["default_policy"] == "deny"


# =========================================================================
# Test 6: Interruptible prompt queue — cancel_pending_prompts
# =========================================================================

import threading
import queue

import thoughtmachine.security as _security_module
from agent.events import EventBus, create_event as _create_event
from thoughtmachine.security import (
    cancel_pending_prompts,
    get_default_security_config,
    is_allowed,
    _prompt_cancelled,
    _pending_security_requests,
    _pending_requests_lock,
)


class TestCancelPendingPrompts:
    """Validate cancel_pending_prompts() cancels in-flight prompts."""

    def test_cancel_sets_event(self):
        """cancel_pending_prompts sets the _prompt_cancelled event."""
        _prompt_cancelled.clear()
        assert not _prompt_cancelled.is_set()
        cancel_pending_prompts()
        assert _prompt_cancelled.is_set()
        # Cleanup for other tests
        _prompt_cancelled.clear()

    def test_cancel_clears_pending_requests(self):
        """cancel_pending_prompts drains and clears all pending queues."""
        q1 = queue.Queue()
        q2 = queue.Queue()
        q1.put((True, False))  # Simulate a response that was never consumed

        with _pending_requests_lock:
            _pending_security_requests["req-1"] = q1
            _pending_security_requests["req-2"] = q2

        cancel_pending_prompts()

        with _pending_requests_lock:
            assert len(_pending_security_requests) == 0

        _prompt_cancelled.clear()

    def test_cancel_twice_is_idempotent(self):
        """Calling cancel_pending_prompts twice is safe."""
        _prompt_cancelled.clear()
        cancel_pending_prompts()
        cancel_pending_prompts()  # second call should not raise
        assert _prompt_cancelled.is_set()
        _prompt_cancelled.clear()

    def test_thread_exits_with_denial_on_cancel(self, monkeypatch):
        """Cancel means deny, through the REAL prompt-resolution flow.

        The prompt is raised through the public ``is_allowed`` API (the same
        path the live gate / tool-executor uses when a policy is "ask"), then
        cancelled via ``cancel_pending_prompts`` (the live app's cancel
        channel). The blocked call must return False (denial).

        (The previous version called the private ``_request_security_prompt``
        directly; that function short-circuits to "allow" whenever the event
        system is not wired up at module import time, so the test could see
        approved=True even though the live system denies on cancel.)
        """
        import time

        # Wire a deterministic, isolated event bus so the prompt actually
        # blocks.  (In some environments the ambient module-level bus ends up
        # None and _request_security_prompt degrades to an instant "allow" —
        # exactly the drift this test is fixing.)
        monkeypatch.setattr(_security_module, "global_event_bus", EventBus())
        monkeypatch.setattr(_security_module, "EVENT_SYSTEM_AVAILABLE", True)
        monkeypatch.setattr(_security_module, "create_event", _create_event)

        _prompt_cancelled.clear()
        result = {"approved": None}

        def waiter():
            # Real security-config shape with an explicit "ask" override,
            # which is what makes is_allowed raise a security prompt.
            config = get_default_security_config()
            config["session_policy"]["tool_overrides"] = {"TestTool": "ask"}
            try:
                approved = is_allowed(
                    agent_id="test-agent",
                    tool_name="TestTool",
                    security_config=config,
                )
                result["approved"] = approved
            except Exception as e:  # pragma: no cover - diagnostic only
                result["exception"] = e

        t = threading.Thread(target=waiter, daemon=True)
        t.start()

        # Wait until the prompt is actually pending (registered in
        # _pending_security_requests) before cancelling — deterministic, and
        # avoids the race where cancel fires before the prompt starts blocking.
        deadline = time.monotonic() + 5.0
        pending = 0
        while time.monotonic() < deadline:
            with _pending_requests_lock:
                pending = len(_pending_security_requests)
            if pending:
                break
            time.sleep(0.02)
        assert pending > 0, "Security prompt was never registered as pending"

        # Cancel from the main thread (the live app's cancel channel)
        cancel_pending_prompts()

        t.join(timeout=5.0)
        assert not t.is_alive(), "Waiter thread did not exit"
        assert result.get("approved") is False, (
            f"Expected denial (False) on cancel, got {result}"
        )

        _prompt_cancelled.clear()
        with _pending_requests_lock:
            _pending_security_requests.clear()
