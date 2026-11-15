"""Credential handover: the parent provider api key reaches a CHILD AGENT tool
only when that tool declares ``inherits_parent_api_key``.

One test per behaviour:

1. ``ToolBase.inherits_parent_api_key`` defaults to False -- the executor's
   injected ``agent_config`` is introspection data, not a credential channel.
2. The Worker tool declares True: it builds a child agent inside the same
   session trust domain, which is the only case that needs the live key.
3. The executor-injected ``agent_config`` for an ordinary tool contains no
   ``api_key`` key at all (not ``None``: the key is ABSENT, so no tool can
   pin the key's presence as a contract).
4. The same dict for a declaring tool carries the parent's key value.
5. Every handover is audited as exactly ONE ``CREDENTIAL_USE`` event naming
   the credential (``parent_api_key``), the tool and the session -- the
   credential VALUE must never appear anywhere in the payload.
6. No declaration, no handover, no audit event.
7. When the worker cannot construct its Agent (e.g. no provider key), the
   failure becomes a clean ``{"error": ...}`` reply naming the provider and
   the ``<PROVIDER>_API_KEY`` remedy, instead of a dead thread.
"""

import json
import tempfile
from pathlib import Path
from typing import Any, ClassVar, Dict, List

from agent.core.tool_executor import ToolExecutor
from agent.events import EventBus, EventType
from tools.base import ToolBase

PARENT_API_KEY = "sk-parent-secret-never-injected-into-plain-tools"

_SEEN: Dict[str, Any] = {}


# ---------------------------------------------------------------------------
# Stub tools / config
# ---------------------------------------------------------------------------


class PlainTool(ToolBase):
    """An ordinary tool: introspection only, never a credential sink."""

    tool: str = "PlainTool"
    required_categories: ClassVar[List[str]] = []

    def execute(self) -> str:
        _SEEN["PlainTool"] = self.agent_config
        return "OK"


class ChildAgentTool(ToolBase):
    """Stands in for the Worker tool: builds a child agent in-session."""

    tool: str = "ChildAgentTool"
    required_categories: ClassVar[List[str]] = []
    inherits_parent_api_key: ClassVar[bool] = True

    def execute(self) -> str:
        _SEEN["ChildAgentTool"] = self.agent_config
        return "OK"


class ParentConfig:
    """Minimal ToolExecutor config stub carrying a live provider key."""

    workspace_path = None
    worktree_root = None
    tool_output_token_limit = None
    temperature = 0.2
    max_turns = 5
    provider_type = "anthropic"
    model = "claude-test-model"
    api_key = PARENT_API_KEY

    def __init__(self):
        self.session_permissions = None


class FakeState:
    security_config = None


def _run_tool(tool_cls, tool_name, bus=None, session_id="sess-handover"):
    """Drive the REAL executor path and return the injected agent_config."""
    _SEEN.clear()
    executor = ToolExecutor(
        tool_classes=[tool_cls],
        config=ParentConfig(),
        state=FakeState(),
        logger=None,
        security_available=False,
        agent=None,
        event_bus=bus,
    )
    result = executor._execute_single_tool(
        tool_cls,
        {},
        tool_name,
        0,
        lambda: False,
        lambda: None,
        lambda: 0,
        session_id=session_id,
    )
    assert result["result"] == "OK", result
    assert tool_name in _SEEN, "the tool never executed"
    return _SEEN[tool_name]


def _credential_events(captured):
    return [
        event
        for event in captured
        if getattr(event, "type", None) == EventType.CREDENTIAL_USE
    ]


# ---------------------------------------------------------------------------
# 1./2. The declaration itself
# ---------------------------------------------------------------------------


def test_tool_base_does_not_inherit_the_parent_api_key():
    assert ToolBase.inherits_parent_api_key is False


def test_worker_tool_declares_parent_api_key_inheritance():
    from tools.workspace.worker_thread import Worker

    assert Worker.inherits_parent_api_key is True


# ---------------------------------------------------------------------------
# 3./4. What the executor actually injects
# ---------------------------------------------------------------------------


def test_executor_omits_api_key_for_tools_without_the_flag():
    cfg = _run_tool(PlainTool, "PlainTool")

    assert isinstance(cfg, dict)
    assert "api_key" not in cfg, (
        "a tool without inherits_parent_api_key must receive no api_key key"
    )
    assert PARENT_API_KEY not in json.dumps(cfg, default=str)


def test_executor_hands_the_parent_key_to_a_declaring_tool():
    cfg = _run_tool(ChildAgentTool, "ChildAgentTool")

    assert cfg.get("api_key") == PARENT_API_KEY


# ---------------------------------------------------------------------------
# 5./6. The audit trail
# ---------------------------------------------------------------------------


def test_handover_is_audited_with_the_credential_name_only():
    bus = EventBus()
    captured = []
    bus.subscribe(None, lambda event: captured.append(event))

    _run_tool(ChildAgentTool, "ChildAgentTool", bus=bus)

    events = _credential_events(captured)
    assert len(events) == 1, [getattr(e, "type", None) for e in captured]

    data = events[0].data
    assert data["credential_name"] == "parent_api_key"
    assert data["tool_name"] == "ChildAgentTool"
    assert data["session_id"] == "sess-handover"

    serialized = json.dumps(events[0].to_dict(), default=str)
    assert PARENT_API_KEY not in serialized


def test_no_handover_event_without_the_flag():
    bus = EventBus()
    captured = []
    bus.subscribe(None, lambda event: captured.append(event))

    _run_tool(PlainTool, "PlainTool", bus=bus)

    assert _credential_events(captured) == []


# ---------------------------------------------------------------------------
# 7. A construction failure is a clean reply, not a dead thread
# ---------------------------------------------------------------------------


def test_agent_construction_failure_becomes_a_clean_error_reply(tmp_path, monkeypatch):
    """Drives the REAL ``WorkerThread.run()`` lazy-Agent branch.

    ``Agent`` is replaced by a stub that raises an ``InvalidConfigError``-named
    exception -- exactly what ``ProviderFactory`` raises when a provider key is
    missing -- so the handler under test (not a live model call) is what runs.
    The thread must answer the query with ``{"error": ...}`` that names the
    provider and the ``<PROVIDER>_API_KEY`` remedy, then exit cleanly.
    """
    import agent.core.agent as agent_module
    from tools.workspace.worker_thread import WorkerThread

    class InvalidConfigError(Exception):
        """Name-matched by the worker's handler."""

    class _AgentThatCannotBeBuilt:
        def __init__(self, *args, **kwargs):
            raise InvalidConfigError("No API key for provider 'anthropic'")

    monkeypatch.setattr(agent_module, "Agent", _AgentThatCannotBeBuilt)
    # A live heartbeat thread would only add noise to this synchronous run.
    monkeypatch.setattr(WorkerThread, "_heartbeat_tick", lambda self: None)

    thread = WorkerThread(
        name="w-key-handover",
        definition={"name": "w-key-handover", "system_prompt": "You are a test worker."},
        agent_config={"provider": "anthropic", "model": "claude-test-model"},
        workspace_dir=tmp_path,
        session_id="sess-handover",
        max_container_count=4,
        max_token_usage=None,
        max_runtime_s=None,
    )
    thread._agent = None
    try:
        thread.start()
        thread._input_queue.put("hello")

        reply = thread._output_queue.get(timeout=30)
        payload = json.loads(reply)

        assert "error" in payload, payload
        error = payload["error"]
        assert "no API key available for provider 'anthropic'" in error, error
        assert "ANTHROPIC_API_KEY" in error, error
        assert "InvalidConfigError" not in error, error
    finally:
        thread._stop_event.set()
        thread._input_queue.put(None)
        thread.join(timeout=30)
    assert not thread.is_alive()
