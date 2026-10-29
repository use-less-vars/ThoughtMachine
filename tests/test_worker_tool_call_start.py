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


class ParallelEchoToolProvider(LLMProvider):
    """First call returns TWO tool calls with distinct ids.

    The two calls name DIFFERENT tools (``Thought`` and ``NoSuchTool``) so that
    both the ``tool_call`` and ``tool_result`` payloads are distinguishable and
    per-id attribution can be asserted (a swapped/mis-attributed id is caught).
    """

    def __init__(self, config: ProviderConfig) -> None:
        super().__init__(config)
        self.call_count = 0

    def chat_completion(
        self,
        messages: List[Dict[str, str]],
        tools: Optional[List[Dict]] = None,
        **kwargs,
    ) -> LLMResponse:
        self.call_count += 1

        if self.call_count == 1:
            return LLMResponse(
                content="",
                reasoning="mock-reasoning",
                tool_calls=[
                    {
                        "id": "call_par_001",
                        "type": "function",
                        "function": {
                            "name": "Thought",
                            "arguments": json.dumps({"content": "first call"}),
                        },
                    },
                    {
                        "id": "call_par_002",
                        "type": "function",
                        "function": {
                            "name": "NoSuchTool",
                            "arguments": json.dumps({"content": "second call"}),
                        },
                    },
                ],
                usage={"prompt_tokens": 10, "completion_tokens": 5},
                provider="echopar",
                model="mock-model",
            )
        return LLMResponse(
            content="Final mock response after two tool calls.",
            reasoning="mock-reasoning-final",
            tool_calls=None,
            usage={"prompt_tokens": 5, "completion_tokens": 3},
            provider="echopar",
            model="mock-model",
        )

    def count_tokens(self, messages: List[Dict], tools: Optional[List] = None) -> int:
        return 42


class EmptyIdToolProvider(LLMProvider):
    """First call returns one ``Thought`` tool call with an EMPTY id."""

    def __init__(self, config: ProviderConfig) -> None:
        super().__init__(config)
        self.call_count = 0

    def chat_completion(
        self,
        messages: List[Dict[str, str]],
        tools: Optional[List[Dict]] = None,
        **kwargs,
    ) -> LLMResponse:
        self.call_count += 1

        if self.call_count == 1:
            return LLMResponse(
                content="",
                reasoning="mock-reasoning",
                tool_calls=[
                    {
                        "id": "",
                        "type": "function",
                        "function": {
                            "name": "Thought",
                            "arguments": json.dumps({"content": "no id from provider"}),
                        },
                    }
                ],
                usage={"prompt_tokens": 10, "completion_tokens": 5},
                provider="echoidless",
                model="mock-model",
            )
        return LLMResponse(
            content="Final mock response after the id-less tool call.",
            reasoning="mock-reasoning-final",
            tool_calls=None,
            usage={"prompt_tokens": 5, "completion_tokens": 3},
            provider="echoidless",
            model="mock-model",
        )

    def count_tokens(self, messages: List[Dict], tools: Optional[List] = None) -> int:
        return 42


@pytest.fixture(scope="module")
def register_parallel_echo_provider():
    """Register the two-tool-call provider once for all tests."""
    if ProviderFactory._providers is None:
        ProviderFactory._providers = {}
    if "echopar" not in ProviderFactory._providers:
        ProviderFactory._providers["echopar"] = ParallelEchoToolProvider


@pytest.fixture(scope="module")
def register_empty_id_provider():
    """Register the empty-id provider once for all tests."""
    if ProviderFactory._providers is None:
        ProviderFactory._providers = {}
    if "echoidless" not in ProviderFactory._providers:
        ProviderFactory._providers["echoidless"] = EmptyIdToolProvider


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

    def test_tool_call_id_present_on_all_three_phases(self, config, ctx):
        """(a) One call: start/tool_call/tool_result carry the SAME provider id."""
        agent = Agent(config, session=ctx)
        events = list(agent.process_query("check the id on every phase"))

        start = next(e for e in events if e["type"] == "tool_call_start")
        call = next(e for e in events if e["type"] == "tool_call")
        result = next(e for e in events if e["type"] == "tool_result")

        assert start["tool_call_id"] == "call_mock_001", start
        assert call["tool_call_id"] == "call_mock_001", call
        assert result["tool_call_id"] == "call_mock_001", result

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


# ======================================================================
# 3. Two tool calls in one turn -- each result keeps its OWN tool_call_id
# ======================================================================

@pytest.mark.usefixtures("register_parallel_echo_provider")
class TestParallelToolCallIds:

    @pytest.fixture
    def ctx(self) -> WorkerContext:
        return WorkerContext(session_id="toolcallstart-parallel-001")

    @pytest.fixture
    def config(self) -> AgentConfig:
        return AgentConfig(
            api_key="sk-test-echopar",
            base_url="http://localhost:9999",
            model="mock-model",
            provider_type="echopar",
            enabled_tools=["Thought"],
            system_prompt="You are a helpful assistant.",
            max_turns=3,
            enable_logging=False,
        )

    def test_two_calls_get_distinct_ids_and_own_results(self, config, ctx):
        """(b) Both calls are distinguishable by id and each result is attributed."""
        agent = Agent(config, session=ctx)
        events = list(agent.process_query("two tool calls in one turn"))

        starts = [e for e in events if e["type"] == "tool_call_start"]
        calls = [e for e in events if e["type"] == "tool_call"]
        results = [e for e in events if e["type"] == "tool_result"]

        assert len(starts) == 2, [e["type"] for e in events]
        assert len(calls) == 2, calls
        assert len(results) == 2, results

        assert [e["tool_call_id"] for e in starts] == ["call_par_001", "call_par_002"], starts
        assert [e["tool_call_id"] for e in calls] == ["call_par_001", "call_par_002"], calls
        assert [e["tool_call_id"] for e in results] == ["call_par_001", "call_par_002"], results

        assert [e["tool_name"] for e in starts] == ["Thought", "NoSuchTool"], starts
        assert [e["tool_name"] for e in calls] == ["Thought", "NoSuchTool"], calls

        # Attribution (not just position): each result must describe the call
        # carrying the SAME id, not merely the result in the same slot. Neither
        # name resolves to a real tool class in this harness, so each result is
        # the deterministic "Unknown tool: <its own name>" text.
        name_by_id = {e["tool_call_id"]: e["tool_name"] for e in calls}
        for result in results:
            cid = result["tool_call_id"]
            assert cid in name_by_id, results
            assert result["tool_name"] == name_by_id[cid], (cid, result)
            assert result["result"] == f"Unknown tool: {name_by_id[cid]}", (cid, result)


# ======================================================================
# 4. Provider that supplies NO id -- empty-string fallback, no crash
# ======================================================================

@pytest.mark.usefixtures("register_empty_id_provider")
class TestEmptyToolCallIdFallback:

    @pytest.fixture
    def ctx(self) -> WorkerContext:
        return WorkerContext(session_id="toolcallstart-noid-001")

    @pytest.fixture
    def config(self) -> AgentConfig:
        return AgentConfig(
            api_key="sk-test-echoidless",
            base_url="http://localhost:9999",
            model="mock-model",
            provider_type="echoidless",
            enabled_tools=["Thought"],
            system_prompt="You are a helpful assistant.",
            max_turns=2,
            enable_logging=False,
        )

    def test_empty_id_falls_back_to_empty_string(self, config, ctx):
        """(c) An empty provider id yields ``""`` on all phases without crashing."""
        agent = Agent(config, session=ctx)
        events = list(agent.process_query("tool call without an id"))

        start = next(e for e in events if e["type"] == "tool_call_start")
        call = next(e for e in events if e["type"] == "tool_call")
        result = next(e for e in events if e["type"] == "tool_result")

        assert start["tool_call_id"] == "", start
        assert call["tool_call_id"] == "", call
        assert result["tool_call_id"] == "", result


# ======================================================================
# 5. Bus adapter: reverse-order completions keep their own ids
# ======================================================================

class TestWorkerBusAdapterToolCallIds:

    @staticmethod
    def _adapter(published):
        class FakeBus:
            def publish(self, event):
                published.append((
                    event.type.value if hasattr(event.type, "value") else str(event.type),
                    event.data,
                ))

        return worker_module.WorkerBusAdapter(event_bus=FakeBus(), worker_name="w1")

    def test_tool_call_and_tool_result_forward_tool_call_id(self):
        published = []
        adapter = self._adapter(published)

        adapter.forward_agent_event({
            "type": "tool_call",
            "tool_name": "Thought",
            "arguments": {"content": "hi"},
            "tool_call_id": "id1",
        })
        adapter.forward_agent_event({
            "type": "tool_result",
            "tool_name": "Thought",
            "result": "hi",
            "success": True,
            "tool_call_id": "id1",
        })

        assert [(t, d["tool_call_id"]) for t, d in published] == [
            ("tool_call", "id1"), ("tool_result", "id1"),
        ], published

    def test_reverse_order_completions_keep_their_own_ids(self):
        """(b) Results arriving in REVERSE order stay correctly attributed."""
        published = []
        adapter = self._adapter(published)

        # Two calls start in order 1, 2 ...
        adapter.forward_agent_event({
            "type": "tool_call", "tool_name": "Thought",
            "arguments": {"content": "first call"}, "tool_call_id": "call_rev_001",
        })
        adapter.forward_agent_event({
            "type": "tool_call", "tool_name": "Thought",
            "arguments": {"content": "second call"}, "tool_call_id": "call_rev_002",
        })
        # ... and complete in reverse order (2 finishes before 1).
        adapter.forward_agent_event({
            "type": "tool_result", "tool_name": "Thought",
            "result": "second done", "success": True, "tool_call_id": "call_rev_002",
        })
        adapter.forward_agent_event({
            "type": "tool_result", "tool_name": "Thought",
            "result": "first done", "success": True, "tool_call_id": "call_rev_001",
        })

        results = [
            (d["tool_call_id"], d["result"])
            for t, d in published if t == "tool_result"
        ]
        assert results == [
            ("call_rev_002", "second done"),
            ("call_rev_001", "first done"),
        ], published

        # Every published payload (both phases) carried an id: none dropped.
        assert all(d.get("tool_call_id") for t, d in published), published

    def test_missing_tool_call_id_defaults_to_empty_string(self):
        """(c) Events without the key at all fall back to ``""`` (no crash)."""
        published = []
        adapter = self._adapter(published)

        adapter.forward_agent_event({
            "type": "tool_call", "tool_name": "Thought", "arguments": {},
        })
        adapter.forward_agent_event({
            "type": "tool_result", "tool_name": "Thought", "result": "ok",
            "success": True,
        })

        assert [(t, d["tool_call_id"]) for t, d in published] == [
            ("tool_call", ""), ("tool_result", ""),
        ], published

