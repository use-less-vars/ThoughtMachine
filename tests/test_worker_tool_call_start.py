"""test_worker_tool_call_start.py -- worker "tool call STARTED" event.

The contract (web_ui/frontend/docs/toolcalls_streaming_backend_contract.md)
prescribes a raw agent event named ``tool_call_start`` that the worker bus
adapter forwards as a ``"tool_call_start"`` bus event carrying
``{tool_name, arguments, tool_call_id}`` so the frontend can render a *pending*
tool-call row before the tool finishes executing.

Covered here:
1. ``Agent.process_query`` yields a ``tool_call_start`` event for the tool
   call, BEFORE the corresponding ``tool_call`` / ``tool_result`` / terminal
   events.
2. ``WorkerBusAdapter.forward_agent_event`` republishes ``tool_call_start``
   with the documented payload (including ``tool_call_id``) plus the
   ``worker_name`` stamp added by ``_publish``.

Run with::

    pytest tests/test_worker_tool_call_start.py -v
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import pytest

from agent.core.worker_context import WorkerContext
from agent.core.agent import Agent
from agent.config.models import AgentConfig
from llm_providers.base import LLMProvider, ProviderConfig, LLMResponse
from llm_providers.factory import ProviderFactory

import tools.workspace.worker as worker_module


# ======================================================================
# EchoToolProvider -- mock LLM that returns a tool call then a final text
# ======================================================================

class EchoToolProvider(LLMProvider):
    """First call returns a ``Thought`` tool call; later calls return text."""

    def __init__(self, config: ProviderConfig) -> None:
        super().__init__(config)
        self.call_count = 0
        self.last_messages: Optional[List[Dict[str, Any]]] = None

    def chat_completion(
        self,
        messages: List[Dict[str, str]],
        tools: Optional[List[Dict]] = None,
        **kwargs,
    ) -> LLMResponse:
        self.call_count += 1
        self.last_messages = messages

        if self.call_count == 1:
            return LLMResponse(
                content="",
                reasoning="mock-reasoning",
                tool_calls=[
                    {
                        "id": "call_mock_001",
                        "type": "function",
                        "function": {
                            "name": "Thought",
                            "arguments": json.dumps({"content": "echo from mock"}),
                        },
                    }
                ],
                usage={"prompt_tokens": 10, "completion_tokens": 5},
                provider="echotool",
                model="mock-model",
            )
        return LLMResponse(
            content="This is the final mock response after tool execution.",
            reasoning="mock-reasoning-final",
            tool_calls=None,
            usage={"prompt_tokens": 5, "completion_tokens": 3},
            provider="echotool",
            model="mock-model",
        )

    def count_tokens(self, messages: List[Dict], tools: Optional[List] = None) -> int:
        return 42


@pytest.fixture(scope="module")
def register_echo_tool_provider():
    """Register EchoToolProvider in the ProviderFactory once for all tests."""
    if ProviderFactory._providers is None:
        ProviderFactory._providers = {}
    if "echotool" not in ProviderFactory._providers:
        ProviderFactory._providers["echotool"] = EchoToolProvider


# ======================================================================
# 1. Agent emits tool_call_start BEFORE tool_call / tool_result
# ======================================================================

@pytest.mark.usefixtures("register_echo_tool_provider")
class TestAgentEmitsToolCallStart:

    @pytest.fixture
    def ctx(self) -> WorkerContext:
        return WorkerContext(session_id="toolcallstart-001")

    @pytest.fixture
    def config(self) -> AgentConfig:
        return AgentConfig(
            api_key="sk-test-echotool",
            base_url="http://localhost:9999",
            model="mock-model",
            provider_type="echotool",
            enabled_tools=["Thought"],
            system_prompt="You are a helpful assistant.",
            max_turns=2,
            enable_logging=False,
        )

    def test_tool_call_start_emitted_before_tool_call(self, config, ctx):
        agent = Agent(config, session=ctx)
        events = list(agent.process_query("test the start event"))
        types = [e["type"] for e in events]

        starts = [e for e in events if e["type"] == "tool_call_start"]
        assert starts, f"Expected tool_call_start event, got: {types}"

        start = starts[0]
        assert start["tool_name"] == "Thought", start
        assert start["tool_call_id"] == "call_mock_001", start
        # 'arguments' is the raw JSON string the LLM produced
        assert json.loads(start["arguments"]) == {"content": "echo from mock"}, start

        start_idx = next(i for i, e in enumerate(events) if e["type"] == "tool_call_start")
        first_tool_call_idx = next(
            i for i, e in enumerate(events) if e["type"] == "tool_call")
        first_tool_result_idx = next(
            i for i, e in enumerate(events) if e["type"] == "tool_result")
        responded_idx = next(
            i for i, e in enumerate(events) if e["type"] == "agent_responded")

        assert start_idx < first_tool_call_idx, types
        assert first_tool_call_idx < first_tool_result_idx, types
        assert first_tool_result_idx < responded_idx, types

    def test_tool_call_start_carries_conversation_data(self, config, ctx):
        agent = Agent(config, session=ctx)
        events = list(agent.process_query("check conversation data"))
        for event in events:
            if event["type"] == "tool_call_start":
                assert "conversation_version" in event, event
                assert "conversation_hash" in event, event


# ======================================================================
# 2. WorkerBusAdapter forwards tool_call_start with payload + worker_name
# ======================================================================

class TestWorkerBusAdapterForwardsToolCallStart:

    def test_forward_tool_call_start_publishes_payload(self):
        published = []

        class FakeBus:
            def publish(self, event):
                published.append((
                    event.type.value if hasattr(event.type, "value") else str(event.type),
                    event.data,
                ))

        adapter = worker_module.WorkerBusAdapter(
            event_bus=FakeBus(), worker_name="w1")
        adapter.forward_agent_event({
            "type": "tool_call_start",
            "tool_name": "Thought",
            "arguments": '{"content": "hi"}',
            "tool_call_id": "id1",
        })

        assert len(published) == 1, published
        etype, data = published[0]
        assert etype == "tool_call_start", etype
        assert data["tool_name"] == "Thought", data
        assert data["arguments"] == '{"content": "hi"}', data
        assert data["tool_call_id"] == "id1", data
        assert data["worker_name"] == "w1", data
