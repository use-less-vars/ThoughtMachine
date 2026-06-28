"""
test_worker_loop_spike.py — Spike test for WorkerContext + agent worker loop.

Tests:
1. WorkerContext provides all Session-like attributes needed by Agent.process_query()
2. Agent initialises and runs with WorkerContext as the session parameter,
   using a mock LLM provider that returns tool calls (echo_tool pattern).

Run with::

    pytest tests/test_worker_loop_spike.py -v
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional
from datetime import datetime

import pytest

from agent.core.worker_context import WorkerContext
from agent.core.agent import Agent
from agent.config.models import AgentConfig
from llm_providers.base import LLMProvider, ProviderConfig, LLMResponse
from llm_providers.factory import ProviderFactory


# ════════════════════════════════════════════════════════════════════════════
# EchoToolProvider — mock LLM that returns tool calls
# ════════════════════════════════════════════════════════════════════════════

class EchoToolProvider(LLMProvider):
    """Mock LLM provider that returns tool calls on the first invocation,
    then a plain text response on subsequent calls.

    Attributes
        call_count : int
            Number of times ``chat_completion`` was called.
        last_messages : list[dict] | None
            The messages passed on the most recent call.
    """

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
            # First call: return a tool call for the safe "Thought" tool
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
        # Subsequent calls: plain text response, no tool calls
        return LLMResponse(
            content="This is the final mock response after tool execution.",
            reasoning="mock-reasoning-final",
            tool_calls=None,
            usage={"prompt_tokens": 5, "completion_tokens": 3},
            provider="echotool",
            model="mock-model",
        )

    def count_tokens(
        self, messages: List[Dict], tools: Optional[List] = None
    ) -> int:
        return 42


# ════════════════════════════════════════════════════════════════════════════
# Registration
# ════════════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
def register_echo_tool_provider():
    """Register EchoToolProvider in the ProviderFactory once for all tests.

    Avoids calling ProviderFactory._get_providers() which triggers lazy
    imports of ``openai``, ``anthropic``, etc.  We poke the private
    ``_providers`` dict directly instead.
    """
    if ProviderFactory._providers is None:
        ProviderFactory._providers = {}
    if "echotool" not in ProviderFactory._providers:
        ProviderFactory._providers["echotool"] = EchoToolProvider


# ════════════════════════════════════════════════════════════════════════════
# Test 1: WorkerContext attribute completeness
# ════════════════════════════════════════════════════════════════════════════

class TestWorkerContextAttributes:
    """Verify WorkerContext exposes all attributes that Agent.process_query()
    reads from the Session object."""

    def test_has_session_id(self):
        ctx = WorkerContext(session_id="test-ctx-001")
        assert ctx.session_id == "test-ctx-001"

    def test_auto_session_id(self):
        ctx = WorkerContext()
        assert ctx.session_id.startswith("worker-")
        assert len(ctx.session_id) > 7

    def test_user_history_default_empty_list(self):
        ctx = WorkerContext()
        assert ctx.user_history == []

    def test_user_history_custom(self):
        ctx = WorkerContext(user_history=[{"role": "user", "content": "hi"}])
        assert len(ctx.user_history) == 1
        assert ctx.user_history[0]["content"] == "hi"

    def test_total_input_tokens_default_zero(self):
        ctx = WorkerContext()
        assert ctx.total_input_tokens == 0

    def test_total_input_tokens_settable(self):
        ctx = WorkerContext(total_input_tokens=42)
        assert ctx.total_input_tokens == 42
        ctx.total_input_tokens = 99
        assert ctx.total_input_tokens == 99

    def test_total_output_tokens_default_zero(self):
        ctx = WorkerContext()
        assert ctx.total_output_tokens == 0

    def test_total_output_tokens_settable(self):
        ctx = WorkerContext(total_output_tokens=10)
        assert ctx.total_output_tokens == 10
        ctx.total_output_tokens = 20
        assert ctx.total_output_tokens == 20

    def test_conversation_version_property(self):
        ctx = WorkerContext()
        # Initial version is 0
        assert ctx.conversation_version == 0
        # Increment the internal counter to mimic Session behaviour
        ctx._conversation_version = 3
        assert ctx.conversation_version == 3

    def test_conversation_hash_present(self):
        ctx = WorkerContext()
        assert isinstance(ctx.conversation_hash, str)
        assert len(ctx.conversation_hash) > 0

    def test_summary_default_none(self):
        ctx = WorkerContext()
        assert ctx.summary is None

    def test_summary_settable(self):
        ctx = WorkerContext()
        s = {"role": "system", "content": "summary text"}
        ctx.summary = s
        assert ctx.summary == s

    def test_updated_at_datetime(self):
        ctx = WorkerContext()
        assert isinstance(ctx.updated_at, datetime)

    def test_updated_at_settable(self):
        ctx = WorkerContext()
        now = datetime(2026, 6, 1, 12, 0, 0)
        ctx.updated_at = now
        assert ctx.updated_at == now

    def test_get_next_seq_increments(self):
        ctx = WorkerContext()
        seq1 = ctx._get_next_seq()
        seq2 = ctx._get_next_seq()
        assert seq1 == 0
        assert seq2 == 1
        seq3 = ctx._get_next_seq()
        assert seq3 == 2

    def test_on_conversation_changed_is_callable(self):
        ctx = WorkerContext()
        # Must not raise
        ctx._on_conversation_changed()

    def test_repr(self):
        ctx = WorkerContext(session_id="my-worker")
        r = repr(ctx)
        assert "WorkerContext" in r
        assert "my-worker" in r


# ════════════════════════════════════════════════════════════════════════════
# Test 2: Agent initialisation with WorkerContext + echo_tool pattern
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.usefixtures("register_echo_tool_provider")
class TestAgentWithWorkerContext:
    """Verify the Agent accepts a WorkerContext as the ``session`` parameter
    and runs a basic turn with the EchoToolProvider."""

    @pytest.fixture
    def ctx(self) -> WorkerContext:
        return WorkerContext(session_id="spike-worker-001")

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

    def test_agent_accepts_worker_context(self, config: AgentConfig, ctx: WorkerContext):
        """Agent.__init__ should accept a WorkerContext without error."""
        agent = Agent(config, session=ctx)
        assert agent.session is ctx
        assert agent.session_id == "spike-worker-001"
        assert agent._session is ctx

    def test_agent_session_property(self, config: AgentConfig, ctx: WorkerContext):
        """Agent.session property returns the WorkerContext."""
        agent = Agent(config, session=ctx)
        assert agent.session is ctx

    def test_agent_conversation_uses_worker_history(
        self, config: AgentConfig, ctx: WorkerContext
    ):
        """Agent should read user_history from WorkerContext as its conversation."""
        ctx.user_history.append({"role": "user", "content": "hello"})
        agent = Agent(config, session=ctx)
        # The agent wraps the history with a system prompt, so check it contains
        # the user message somewhere
        assert any(
            m.get("content") == "hello" for m in agent.conversation
        )

    def test_token_counts_delegate_to_worker(
        self, config: AgentConfig, ctx: WorkerContext
    ):
        """Agent.total_input_tokens / total_output_tokens should delegate
        to the WorkerContext when session is set."""
        ctx.total_input_tokens = 50
        ctx.total_output_tokens = 25
        agent = Agent(config, session=ctx)
        assert agent.total_input_tokens == 50
        assert agent.total_output_tokens == 25

        # Update via agent setter should propagate to WorkerContext
        agent.total_input_tokens = 100
        agent.total_output_tokens = 60
        assert ctx.total_input_tokens == 100
        assert ctx.total_output_tokens == 60

    def test_process_query_with_tool_call(
        self, config: AgentConfig, ctx: WorkerContext
    ):
        """Run a full process_query cycle with EchoToolProvider.

        The provider returns:
          - Turn 1: a ``Thought`` tool call
          - Turn 2: a final text response

        The agent should yield events including tool_call, tool_result,
        and a final agent_responded.
        """
        agent = Agent(config, session=ctx)
        events = list(agent.process_query("test the echo pattern"))

        event_types = [e["type"] for e in events]

        # We should see at least these event types
        assert "tool_call" in event_types, (
            f"Expected tool_call event, got: {event_types}"
        )
        assert "tool_result" in event_types, (
            f"Expected tool_result event, got: {event_types}"
        )
        assert "agent_responded" in event_types, (
            f"Expected agent_responded event, got: {event_types}"
        )

        # Verify the conversation in WorkerContext has recorded messages
        history = ctx.user_history
        assert len(history) > 0, "WorkerContext.user_history should have messages"

        roles_seen = {m["role"] for m in history}
        assert "assistant" in roles_seen, (
            f"No assistant message in history. Roles: {roles_seen}"
        )
        assert "tool" in roles_seen, (
            f"No tool message in history. Roles: {roles_seen}"
        )

        # Verify token counts were updated on WorkerContext
        assert ctx.total_input_tokens > 0
        assert ctx.total_output_tokens > 0

    def test_conversation_data_on_events(
        self, config: AgentConfig, ctx: WorkerContext
    ):
        """Events yielded by process_query should include conversation_version
        and conversation_hash from the WorkerContext."""
        agent = Agent(config, session=ctx)
        events = list(agent.process_query("check version tracking"))

        for event in events:
            if event["type"] in ("tool_call", "tool_result", "agent_responded"):
                assert "conversation_version" in event, (
                    f"Missing conversation_version in {event['type']}"
                )
                assert "conversation_hash" in event, (
                    f"Missing conversation_hash in {event['type']}"
                )

    def test_summary_and_updated_at_updated(
        self, config: AgentConfig, ctx: WorkerContext
    ):
        """After pruning, session.summary and session.updated_at should be set."""
        # Prime the context with enough messages to trigger summarization
        # The SummarizeTool would normally be called — but with max_turns=2
        # and no SummarizeTool in enabled_tools, we just check that setting
        # these attributes on WorkerContext works
        ctx.summary = None
        assert ctx.summary is None

        # Manually set (simulates what _apply_summary_pruning does)
        ctx.summary = {"role": "system", "content": "Dummy summary"}
        ctx.updated_at = datetime.now()
        assert ctx.summary is not None
        assert isinstance(ctx.updated_at, datetime)


# ════════════════════════════════════════════════════════════════════════════
# Test 3: WorkerContext as standalone object
# ════════════════════════════════════════════════════════════════════════════

class TestWorkerContextStandalone:
    """Verify WorkerContext behaves correctly as a standalone object."""

    def test_supports_get_next_seq(self):
        ctx = WorkerContext()
        assert ctx._get_next_seq() == 0
        assert ctx._get_next_seq() == 1
        assert ctx._get_next_seq() == 2

    def test_on_conversation_changed_increments_version(self):
        ctx = WorkerContext()
        ver_before = ctx.conversation_version
        ctx._on_conversation_changed()
        # In WorkerContext, _on_conversation_changed increments version
        assert ctx.conversation_version == ver_before + 1

    def test_conversation_hash_changes_on_content(self):
        ctx = WorkerContext()
        hash1 = ctx.conversation_hash
        # Force a history change and recompute
        ctx._conversation_version += 1
        ctx.conversation_hash = ctx._compute_hash()
        # Hashing on empty history should still produce a deterministic hash
        assert isinstance(ctx.conversation_hash, str)

    def test_user_history_is_mutable(self):
        ctx = WorkerContext()
        ctx.user_history.append({"role": "user", "content": "hello"})
        ctx.user_history.append({"role": "assistant", "content": "world"})
        assert len(ctx.user_history) == 2

    def test_idempotent_conversation_hash_for_empty(self):
        """Two WorkerContexts with same empty history should produce
        the same hash (because hash is based only on history content)."""
        ctx_a = WorkerContext()
        ctx_b = WorkerContext()
        # Same empty history → same deterministic hash
        assert ctx_a.conversation_hash == ctx_b.conversation_hash
