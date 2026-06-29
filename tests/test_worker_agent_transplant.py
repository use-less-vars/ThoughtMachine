"""
test_worker_agent_transplant.py — Transplant test suite for WorkerContext + Agent integration.

Tests the key behaviours that the WorkerThread requires from its Agent sub-process:

1. **test_smoke_multi_turn_task** — Agent runs multiple turns with tool calls + final response
2. **test_resume_worker_continues_conversation** — Sequential process_query() calls persist history
3. **test_timeout_enforces_restrictions** — Short timeout triggers CRITICAL time_state
4. **test_token_critical_triggers_summarisation** — Low token threshold triggers restrictions
5. **test_stop_flag_graceful_exit** — stop_check stops agent mid-execution
6. **test_gate_denial_instant** — Tool rejection via NullEventBus gate path

Run with::

    pytest tests/test_worker_agent_transplant.py -v
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest

from agent.core.worker_context import WorkerContext
from agent.core.agent import Agent
from agent.config.models import AgentConfig
from llm_providers.base import LLMProvider, ProviderConfig, LLMResponse
from llm_providers.factory import ProviderFactory
from agent.core.state import TokenState, TimeState


# ════════════════════════════════════════════════════════════════════════════
# ScriptedProvider — flexible mock LLM with pre-configured responses
# ════════════════════════════════════════════════════════════════════════════

class ScriptedProvider(LLMProvider):
    """Mock LLM provider that returns pre-configured responses per call index.

    Provide a list of ``LLMResponse`` objects via ``responses``.  Each call to
    ``chat_completion`` pops the next response from the list.

    Attributes
        call_count : int
            Number of times ``chat_completion`` has been called.
        last_messages : list[dict] | None
            The messages passed on the most recent call.
    """

    def __init__(self, config: ProviderConfig, responses: Optional[List[LLMResponse]] = None) -> None:
        super().__init__(config)
        self.call_count = 0
        self.last_messages: Optional[List[Dict[str, Any]]] = None
        self._responses: List[LLMResponse] = responses or []

    def chat_completion(
        self,
        messages: List[Dict[str, str]],
        tools: Optional[List[Dict]] = None,
        **kwargs,
    ) -> LLMResponse:
        self.call_count += 1
        self.last_messages = messages
        if self._responses:
            return self._responses.pop(0)
        # Fallback: plain text response
        return LLMResponse(
            content="Fallback mock response.",
            reasoning="mock-fallback",
            tool_calls=None,
            usage={"prompt_tokens": 5, "completion_tokens": 3},
            provider="scripted",
            model="mock-model",
        )

    def count_tokens(
        self, messages: List[Dict], tools: Optional[List] = None
    ) -> int:
        return 42


# ════════════════════════════════════════════════════════════════════════════
# Provider factory registration
# ════════════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
def register_scripted_provider():
    """Register ScriptedProvider in ProviderFactory once for all tests."""
    if ProviderFactory._providers is None:
        ProviderFactory._providers = {}
    if "scripted" not in ProviderFactory._providers:
        ProviderFactory._providers["scripted"] = ScriptedProvider


# ════════════════════════════════════════════════════════════════════════════
# Helper: build a tool-call LLMResponse
# ════════════════════════════════════════════════════════════════════════════

def _tool_response(
    tool_name: str,
    arguments: dict,
    content: str = "",
    usage: Optional[dict] = None,
) -> LLMResponse:
    """Return an LLMResponse that tells the agent to call *tool_name*."""
    return LLMResponse(
        content=content,
        reasoning="mock-reasoning",
        tool_calls=[
            {
                "id": "call_mock_001",
                "type": "function",
                "function": {
                    "name": tool_name,
                    "arguments": json.dumps(arguments),
                },
            }
        ],
        usage=usage or {"prompt_tokens": 10, "completion_tokens": 5},
        provider="scripted",
        model="mock-model",
    )


def _text_response(
    content: str,
    usage: Optional[dict] = None,
) -> LLMResponse:
    """Return an LLMResponse with plain text (no tool calls)."""
    return LLMResponse(
        content=content,
        reasoning="mock-reasoning-final",
        tool_calls=None,
        usage=usage or {"prompt_tokens": 5, "completion_tokens": 3},
        provider="scripted",
        model="mock-model",
    )


# ════════════════════════════════════════════════════════════════════════════
# Test 1: Smoke test — multi-turn with tool calls + final response
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.usefixtures("register_scripted_provider")
class TestSmokeMultiTurnTask:
    """Smoke test: agent runs multiple turns with tool execution."""

    @pytest.fixture
    def ctx(self) -> WorkerContext:
        return WorkerContext(session_id="transplant-smoke-001")

    @pytest.fixture
    def config(self) -> AgentConfig:
        return AgentConfig(
            api_key="sk-test-scripted",
            base_url="http://localhost:9999",
            model="mock-model",
            provider_type="scripted",
            enabled_tools=["Thought"],
            system_prompt="You are a helpful assistant.",
            max_turns=2,
            enable_logging=False,
        )

    def test_smoke_multi_turn_task(self, config: AgentConfig, ctx: WorkerContext):
        """Multi-turn with tool calls then final response."""
        # Script: turn 1 → Thought tool call, turn 2 → text response
        config._provider_responses = [
            _tool_response("Thought", {"content": "echo from mock"}),
            _text_response("This is the final answer."),
        ]

        # Inject responses into the provider's __init__
        original_init = ScriptedProvider.__init__

        def patched_init(self, cfg):
            original_init(self, cfg)
            self._responses = list(config._provider_responses)

        ScriptedProvider.__init__ = patched_init

        agent = Agent(config, session=ctx)

        try:
            events = list(agent.process_query("run a multi-turn task"))

            event_types = [e["type"] for e in events]

            # Must see tool_call, tool_result, and agent_responded
            assert "tool_call" in event_types, (
                f"Expected tool_call event, got: {event_types}"
            )
            assert "tool_result" in event_types, (
                f"Expected tool_result event, got: {event_types}"
            )
            assert "agent_responded" in event_types, (
                f"Expected agent_responded event, got: {event_types}"
            )

            # Verify conversation history recorded in WorkerContext
            history = ctx.user_history
            assert len(history) > 0, "WorkerContext.user_history should have messages"

            roles_seen = {m["role"] for m in history}
            assert "assistant" in roles_seen, (
                f"No assistant message in history. Roles: {roles_seen}"
            )
            assert "tool" in roles_seen, (
                f"No tool message in history. Roles: {roles_seen}"
            )

            # Token counts should have been updated on WorkerContext
            assert ctx.total_input_tokens > 0
            assert ctx.total_output_tokens > 0

            # Verify the final response content
            final_events = [e for e in events if e["type"] == "agent_responded"]
            assert len(final_events) >= 1
            assert "final answer" in final_events[-1].get("content", "").lower()

        finally:
            # Restore original __init__
            ScriptedProvider.__init__ = original_init


# ════════════════════════════════════════════════════════════════════════════
# Test 2: Resume — sequential queries with WorkerContext persistence
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.usefixtures("register_scripted_provider")
class TestResumeWorkerContinuesConversation:
    """Sequential process_query() calls with context persistence."""

    @pytest.fixture
    def ctx(self) -> WorkerContext:
        return WorkerContext(session_id="transplant-resume-001")

    @pytest.fixture
    def config(self) -> AgentConfig:
        return AgentConfig(
            api_key="sk-test-scripted",
            base_url="http://localhost:9999",
            model="mock-model",
            provider_type="scripted",
            enabled_tools=["Thought"],
            system_prompt="You are a helpful assistant.",
            max_turns=1,
            enable_logging=False,
        )

    def test_resume_worker_continues_conversation(
        self, config: AgentConfig, ctx: WorkerContext
    ):
        """Two sequential queries with WorkerContext history persistence."""
        # Both responses are text-only (no tool calls)
        config._provider_responses = [
            _text_response("First response: hello from mock."),
            _text_response("Second response: continuing the conversation."),
        ]

        original_init = ScriptedProvider.__init__

        def patched_init(self, cfg):
            original_init(self, cfg)
            self._responses = list(config._provider_responses)

        ScriptedProvider.__init__ = patched_init

        agent_1 = Agent(config, session=ctx)
        try:
            events_1 = list(agent_1.process_query("First query"))
            event_types_1 = [e["type"] for e in events_1]

            # First query should complete successfully
            assert "agent_responded" in event_types_1, (
                f"Expected agent_responded, got: {event_types_1}"
            )

            # Conversation should have 3+ messages: system, user, assistant
            assert len(ctx.user_history) >= 3, (
                f"Expected 3+ messages after first query, got {len(ctx.user_history)}"
            )

            # Extract the second batch of responses for agent_2
            # Agent(2) should only have the second response available
            config._provider_responses = [
                _text_response("Second response: continuing the conversation."),
            ]

            # Create a NEW agent with the SAME WorkerContext
            agent_2 = Agent(config, session=ctx)
            events_2 = list(agent_2.process_query("Second query"))
            event_types_2 = [e["type"] for e in events_2]

            assert "agent_responded" in event_types_2, (
                f"Expected agent_responded on second query, got: {event_types_2}"
            )

            # After two queries, history should have 5+ messages:
            # system, user1, assistant1, user2, assistant2
            assert len(ctx.user_history) >= 5, (
                f"Expected 5+ messages after two queries, got {len(ctx.user_history)}"
            )

            # Verify both user messages are in history
            user_contents = [
                m["content"]
                for m in ctx.user_history
                if m.get("role") == "user" and not m.get("content", "").startswith("[SYSTEM")
            ]
            assert len(user_contents) >= 2, (
                f"Expected 2+ user messages, got {len(user_contents)}"
            )
        finally:
            ScriptedProvider.__init__ = original_init


# ════════════════════════════════════════════════════════════════════════════
# Test 3: Timeout enforces restrictions
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.usefixtures("register_scripted_provider")
class TestTimeoutEnforcesRestrictions:
    """Short timeout triggers CRITICAL time_state and stops execution."""

    @pytest.fixture
    def ctx(self) -> WorkerContext:
        return WorkerContext(session_id="transplant-timeout-001")

    @pytest.fixture
    def config(self) -> AgentConfig:
        return AgentConfig(
            api_key="sk-test-scripted",
            base_url="http://localhost:9999",
            model="mock-model",
            provider_type="scripted",
            enabled_tools=["Thought"],
            system_prompt="You are a helpful assistant.",
            max_turns=5,
            enable_logging=False,
            # Ultra-short timeout — even one turn should exceed this
            timeout_seconds=0,
            time_monitor_enabled=True,
            time_warning_threshold=0,
        )

    def test_timeout_enforces_restrictions(self, config: AgentConfig, ctx: WorkerContext):
        """Agent should detect timeout and apply soft restriction (no hard stop)."""
        # Script: always return tool calls so the loop continues
        config._provider_responses = [
            _tool_response("Thought", {"content": f"turn-{i}"})
            for i in range(10)
        ]

        original_init = ScriptedProvider.__init__

        def patched_init(self, cfg):
            original_init(self, cfg)
            self._responses = list(config._provider_responses)

        ScriptedProvider.__init__ = patched_init

        agent = Agent(config, session=ctx)
        try:
            events = list(agent.process_query("test timeout"))

            event_types = [e["type"] for e in events]

            # Soft restriction: agent should NOT stop with 'stopped' event
            # Instead, restrictions should be active with restriction_reason='timeout'
            stopped_events = [e for e in events if e["type"] == "stopped"]
            assert len(stopped_events) == 0, (
                f"Expected no 'stopped' event (soft restriction), got: {stopped_events}"
            )
            # Verify CRITICAL time_state and soft restriction
            assert agent.state.time_state == TimeState.CRITICAL, (
                f"Expected CRITICAL time_state, got {agent.state.time_state}"
            )
            assert agent.state.restrictions_active, (
                "Expected restrictions_active=True after timeout"
            )
            assert agent.state.restriction_reason == 'timeout', (
                f"Expected restriction_reason='timeout', got {agent.state.restriction_reason}"
            )
            # Only Respond should be allowed
            allowed = agent.state.get_allowed_tools()
            assert allowed == ['Respond'], (
                f"Expected only ['Respond'], got {allowed}"
            )
        finally:
            ScriptedProvider.__init__ = original_init


# ════════════════════════════════════════════════════════════════════════════
# Test 4: Token critical triggers summarisation restrictions
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.usefixtures("register_scripted_provider")
class TestTokenCriticalTriggersSummarisation:
    """Low token threshold triggers CRITICAL token state and restrictions."""

    @pytest.fixture
    def ctx(self) -> WorkerContext:
        return WorkerContext(session_id="transplant-token-001")

    @pytest.fixture
    def config(self) -> AgentConfig:
        return AgentConfig(
            api_key="sk-test-scripted",
            base_url="http://localhost:9999",
            model="mock-model",
            provider_type="scripted",
            enabled_tools=["Thought"],
            system_prompt="You are a helpful assistant.",
            max_turns=2,
            enable_logging=False,
            # Very low thresholds — the LLM response will report high tokens
            token_monitor_warning_threshold=10,
            token_monitor_critical_threshold=50,
        )

    def test_token_critical_triggers_summarisation(
        self, config: AgentConfig, ctx: WorkerContext
    ):
        """Agent should detect token critical state and activate restrictions."""
        # Script: return responses with high token usage to trigger critical
        # The first response reports high prompt_tokens → critical threshold exceeded
        config._provider_responses = [
            _tool_response(
                "Thought",
                {"content": "echo"},
                usage={"prompt_tokens": 50000, "completion_tokens": 5},
            ),
            _text_response(
                "Final answer after critical.",
                usage={"prompt_tokens": 50000, "completion_tokens": 3},
            ),
        ]

        original_init = ScriptedProvider.__init__

        def patched_init(self, cfg):
            original_init(self, cfg)
            self._responses = list(config._provider_responses)

        ScriptedProvider.__init__ = patched_init

        # Mock update_token_state to inject high token count
        agent = Agent(config, session=ctx)
        try:
            events = list(agent.process_query("test token critical"))

            event_types = [e["type"] for e in events]

            # Verify token_state became CRITICAL
            assert agent.state.token_state == TokenState.CRITICAL, (
                f"Expected CRITICAL token_state, got {agent.state.token_state}"
            )

            # Restrictions should be active
            assert agent.state.restrictions_active, (
                "Expected restrictions_active=True in CRITICAL token state"
            )

            # The Thought tool should be restricted
            allowed = agent.state.get_allowed_tools()
            assert "Thought" not in allowed, (
                f"Thought should be restricted in CRITICAL state. Allowed: {allowed}"
            )

            # Restriction message should mention only Respond and SummarizeTool
            rejection = agent.state.restrictions_active
            assert rejection is True

            allowed_tools = agent.state.get_allowed_tools()
            assert "SummarizeTool" in allowed_tools, (
                f"SummarizeTool should be allowed in CRITICAL state. Got: {allowed_tools}"
            )
        finally:
            ScriptedProvider.__init__ = original_init


# ════════════════════════════════════════════════════════════════════════════
# Test 5: Stop flag graceful exit
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.usefixtures("register_scripted_provider")
class TestStopFlagGracefulExit:
    """stop_check stops agent mid-execution."""

    @pytest.fixture
    def ctx(self) -> WorkerContext:
        return WorkerContext(session_id="transplant-stop-001")

    @pytest.fixture
    def config(self) -> AgentConfig:
        return AgentConfig(
            api_key="sk-test-scripted",
            base_url="http://localhost:9999",
            model="mock-model",
            provider_type="scripted",
            enabled_tools=["Thought"],
            system_prompt="You are a helpful assistant.",
            max_turns=10,
            enable_logging=False,
        )

    def test_stop_flag_graceful_exit(self, config: AgentConfig, ctx: WorkerContext):
        """Agent should stop when stop_check returns True."""
        # Script: always return tool calls (so the loop continues)
        config._provider_responses = [
            _tool_response("Thought", {"content": f"turn-{i}"})
            for i in range(10)
        ]

        original_init = ScriptedProvider.__init__

        def patched_init(self, cfg):
            original_init(self, cfg)
            self._responses = list(config._provider_responses)

        ScriptedProvider.__init__ = patched_init

        # stop_check that activates after 3 calls
        call_counter = {"count": 0}

        def stop_check() -> bool:
            call_counter["count"] += 1
            return call_counter["count"] >= 3

        config.stop_check = stop_check

        agent = Agent(config, session=ctx)
        try:
            events = list(agent.process_query("test stop flag"))

            event_types = [e["type"] for e in events]

            # Should see a 'stopped' event with stop_reason='stopped'
            assert "stopped" in event_types, (
                f"Expected 'stopped' event type, got: {event_types}"
            )

            stopped_events = [e for e in events if e["type"] == "stopped"]
            assert len(stopped_events) >= 1
            assert stopped_events[0].get("stop_reason") == "stopped", (
                f"Expected stop_reason='stopped', got: {stopped_events[0]}"
            )

            # Agent should NOT have run all 10 max turns
            assert call_counter["count"] < 10, (
                f"Agent ran {call_counter['count']} turns but should have stopped after stop_check"
            )
        finally:
            ScriptedProvider.__init__ = original_init
            config.stop_check = None


# ════════════════════════════════════════════════════════════════════════════
# Test 7: compact_after_summary — conversation compaction
# ════════════════════════════════════════════════════════════════════════════


class TestCompactAfterSummary:
    """WorkerContext.compact_after_summary() behaviour."""

    def test_no_summary_returns_false(self):
        """No summary message → compact_after_summary does nothing."""
        ctx = WorkerContext(session_id="compact-test-001")
        history = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
        ]
        ctx.user_history = list(history)

        result = ctx.compact_after_summary()

        assert result is False, "Should return False when no summary exists"
        assert ctx.user_history == history, (
            "History should be unchanged when no summary exists"
        )

    def test_summary_removes_old_messages(self):
        """Messages before the latest summary are removed, system prompts + summary + recent kept."""
        ctx = WorkerContext(session_id="compact-test-002")
        ctx.user_history = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Q1"},
            {"role": "assistant", "content": "A1"},
            {"role": "user", "content": "Q2"},
            {"role": "assistant", "content": "A2"},
            {
                "role": "system",
                "content": "Summary of previous conversation: ...",
                "summary": True,
                "pruning_insertion_idx": 1,
            },
            {
                "role": "user",
                "content": "[SYSTEM NOTIFICATION] Context has been summarized...",
                "is_system_notification": True,
            },
        ]

        result = ctx.compact_after_summary()

        assert result is True, "Should return True when compaction performed"
        # Should keep: system prompt + summary + context-cleared notification
        assert len(ctx.user_history) == 3, (
            f"Expected 3 messages, got {len(ctx.user_history)}: {ctx.user_history}"
        )
        assert ctx.user_history[0]["role"] == "system"
        assert ctx.user_history[0]["content"] == "You are a helpful assistant."
        assert ctx.user_history[1]["summary"] is True
        assert ctx.user_history[2]["is_system_notification"] is True

    def test_keeps_multiple_leading_system_prompts(self):
        """All leading system prompts are preserved."""
        ctx = WorkerContext(session_id="compact-test-003")
        ctx.user_history = [
            {"role": "system", "content": "System prompt 1"},
            {"role": "system", "content": "System prompt 2"},
            {"role": "user", "content": "Q1"},
            {"role": "assistant", "content": "A1"},
            {
                "role": "system",
                "content": "Summary of previous conversation: ...",
                "summary": True,
            },
            {
                "role": "user",
                "content": "[SYSTEM NOTIFICATION] Context has been summarized...",
                "is_system_notification": True,
            },
        ]

        result = ctx.compact_after_summary()

        assert result is True
        assert len(ctx.user_history) == 4, (
            f"Expected 4 messages (2 sys + summary + notification), got {len(ctx.user_history)}"
        )
        assert ctx.user_history[0]["content"] == "System prompt 1"
        assert ctx.user_history[1]["content"] == "System prompt 2"
        assert ctx.user_history[2]["summary"] is True
        assert ctx.user_history[3]["is_system_notification"] is True

    def test_keeps_extra_system_msg_before_non_system(self):
        """A system message that is NOT a summary, appearing between non-system
        and the actual summary, is treated as an old message and removed.
        This tests that only *leading* system messages are preserved."""
        ctx = WorkerContext(session_id="compact-test-004")
        ctx.user_history = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Q1"},
            {"role": "assistant", "content": "A1"},
            {"role": "system", "content": "Some inline instruction"},
            {
                "role": "system",
                "content": "Summary of previous conversation: ...",
                "summary": True,
            },
        ]

        result = ctx.compact_after_summary()

        assert result is True
        assert len(ctx.user_history) == 2, (
            f"Expected 2 messages (sys_prompt + summary), got {len(ctx.user_history)}"
        )
        assert ctx.user_history[0]["content"] == "You are a helpful assistant."
        assert ctx.user_history[1]["summary"] is True

    def test_only_latest_summary_preserved(self):
        """If multiple summaries exist (e.g. from resumption), only the
        latest is kept, and messages before it are removed."""
        ctx = WorkerContext(session_id="compact-test-005")
        ctx.user_history = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Q1"},
            {"role": "assistant", "content": "A1"},
            {
                "role": "system",
                "content": "Summary of previous conversation: first summary",
                "summary": True,
            },
            {
                "role": "user",
                "content": "[SYSTEM NOTIFICATION] Context has been summarized...",
                "is_system_notification": True,
            },
            {"role": "user", "content": "Q2"},
            {"role": "assistant", "content": "A2"},
            {
                "role": "system",
                "content": "Summary of previous conversation: second summary",
                "summary": True,
            },
            {
                "role": "user",
                "content": "[SYSTEM NOTIFICATION] Context has been summarized...",
                "is_system_notification": True,
            },
        ]

        result = ctx.compact_after_summary()

        assert result is True
        # Kept: system prompt + second summary + notification after it
        assert len(ctx.user_history) == 3, (
            f"Expected 3 messages, got {len(ctx.user_history)}"
        )
        assert ctx.user_history[0]["content"] == "You are a helpful assistant."
        assert ctx.user_history[1]["content"] == (
            "Summary of previous conversation: second summary"
        )
        assert ctx.user_history[2]["is_system_notification"] is True

    def test_empty_history_returns_false(self):
        """Empty user_history should return False (nothing to compact)."""
        ctx = WorkerContext(session_id="compact-test-006")
        ctx.user_history = []

        result = ctx.compact_after_summary()

        assert result is False
        assert ctx.user_history == []

    def test_updates_conversation_hash_and_version(self):
        """After compaction, conversation_hash and version should update."""
        ctx = WorkerContext(session_id="compact-test-007")
        ctx.user_history = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Q1"},
            {"role": "assistant", "content": "A1"},
            {
                "role": "system",
                "content": "Summary of previous conversation: ...",
                "summary": True,
            },
        ]
        old_hash = ctx.conversation_hash
        old_version = ctx.conversation_version

        result = ctx.compact_after_summary()

        assert result is True
        assert ctx.conversation_version > old_version, (
            f"Version should increment from {old_version} to {ctx.conversation_version}"
        )
        assert ctx.conversation_hash != old_hash, (
            "Hash should change after compaction"
        )


# ════════════════════════════════════════════════════════════════════════════
# Test 6: Gate denial via NullEventBus path
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.usefixtures("register_scripted_provider")
class TestGateDenialInstant:
    """Tool rejection via NullEventBus gate path."""

    @pytest.fixture
    def ctx(self) -> WorkerContext:
        return WorkerContext(session_id="transplant-gate-001")

    @pytest.fixture
    def config(self) -> AgentConfig:
        # Build a SessionPermissions with filesystem="ask" so that write
        # operations require interactive approval — which the NullEventBus
        # (or None event_bus) will deny instantly.
        try:
            from thoughtmachine.security import SessionPermissions
            perms = SessionPermissions(filesystem="ask")
        except ImportError:
            pytest.skip("SessionPermissions not available — cannot test gate denial")

        return AgentConfig(
            api_key="sk-test-scripted",
            base_url="http://localhost:9999",
            model="mock-model",
            provider_type="scripted",
            enabled_tools=["FileEditor"],
            system_prompt="You are a helpful assistant.",
            max_turns=2,
            enable_logging=False,
            session_permissions=perms,
        )

    def test_gate_denial_instant(self, config: AgentConfig, ctx: WorkerContext):
        """When effective permission is 'ask', gate denies via NullEventBus instantly."""
        # Script: call FileEditor with write operation (requires filesystem:write)
        # Patch global_event_bus to None to simulate worker context with no interactive user
        patcher = patch('agent.core.tool_executor.global_event_bus', None)
        patcher.start()
        config._provider_responses = [
            _tool_response(
                "FileEditor",
                {
                    "operation": "write",
                    "filename": "/tmp/test.txt",
                    "content": "hello",
                },
            ),
            _text_response("Gate denied the tool call, continuing without it."),
        ]

        original_init = ScriptedProvider.__init__

        def patched_init(self, cfg):
            original_init(self, cfg)
            self._responses = list(config._provider_responses)

        ScriptedProvider.__init__ = patched_init

        agent = Agent(config, session=ctx)
        try:
            events = list(agent.process_query("write a file"))

            event_types = [e["type"] for e in events]

            # Should see tool_call and tool_result events
            assert "tool_call" in event_types, (
                f"Expected tool_call event, got: {event_types}"
            )
            assert "tool_result" in event_types, (
                f"Expected tool_result event, got: {event_types}"
            )

            # Find the tool result — should contain denial message
            tool_results = [
                e for e in events
                if e["type"] == "tool_result"
            ]
            assert len(tool_results) >= 1

            denied_result = tool_results[0]
            result_content = str(denied_result.get("result", ""))
            assert "Permission denied" in result_content, (
                f"Expected 'Permission denied' in tool result, got: {result_content}"
            )

            # Should mention 'no interactive user available' (the NullEventBus message)
            # or a similar denial explanation
            denial_phrases = [
                "no interactive user",
                "permission denied",
                "filesystem:write",
            ]
            assert any(
                phrase in result_content.lower()
                for phrase in denial_phrases
            ), (
                f"Tool result should contain denial explanation. Got: {result_content}"
            )

            # The agent should still complete (agent_responded with fallback text)
            assert "agent_responded" in event_types, (
                f"Expected agent_responded even after gate denial, got: {event_types}"
            )
        finally:
            patcher.stop()
            ScriptedProvider.__init__ = original_init


# ════════════════════════════════════════════════════════════════════════════
# Test 7: Reasoning passthrough in agent_responded events
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.usefixtures("register_scripted_provider")
class TestReasoningPassthrough:
    """Verify reasoning from the LLM provider is passed through in agent_responded events."""

    @pytest.fixture
    def ctx(self) -> WorkerContext:
        return WorkerContext(session_id="transplant-reasoning-001")

    @pytest.fixture
    def config(self) -> AgentConfig:
        return AgentConfig(
            api_key="sk-test-scripted",
            base_url="http://localhost:9999",
            model="mock-model",
            provider_type="scripted",
            enabled_tools=["Thought"],
            system_prompt="You are a helpful assistant.",
            max_turns=5,
            enable_logging=False,
            timeout_seconds=60,
            time_monitor_enabled=False,
        )

    def test_reasoning_passed_to_main_agent(self, config: AgentConfig, ctx: WorkerContext):
        """agent_responded events should include 'reasoning' when the provider returns it."""
        # Script: return a plain text response WITH reasoning
        config._provider_responses = [
            LLMResponse(
                content="Final answer with chain-of-thought.",
                reasoning="Let me think step by step: first, I need to analyze...",
                tool_calls=None,
                usage={"prompt_tokens": 10, "completion_tokens": 5},
                provider="scripted",
                model="mock-model",
            )
        ]

        original_init = ScriptedProvider.__init__

        def patched_init(self, cfg):
            original_init(self, cfg)
            self._responses = list(config._provider_responses)

        ScriptedProvider.__init__ = patched_init

        agent = Agent(config, session=ctx)
        try:
            events = list(agent.process_query("test reasoning passthrough"))

            final_events = [e for e in events if e["type"] == "agent_responded"]
            assert len(final_events) >= 1, "Expected at least one agent_responded event"

            last = final_events[-1]
            assert "reasoning" in last, (
                f"Expected 'reasoning' in agent_responded event, got keys: {list(last.keys())}"
            )
            assert last["reasoning"] == "Let me think step by step: first, I need to analyze...", (
                f"Unexpected reasoning content: {last.get('reasoning')}"
            )
        finally:
            ScriptedProvider.__init__ = original_init

    def test_reasoning_is_none_when_not_provided(self, config: AgentConfig, ctx: WorkerContext):
        """agent_responded events should have reasoning=None when provider doesn't return it."""
        config._provider_responses = [
            LLMResponse(
                content="Final answer without reasoning.",
                reasoning=None,
                tool_calls=None,
                usage={"prompt_tokens": 10, "completion_tokens": 5},
                provider="scripted",
                model="mock-model",
            )
        ]

        original_init = ScriptedProvider.__init__

        def patched_init(self, cfg):
            original_init(self, cfg)
            self._responses = list(config._provider_responses)

        ScriptedProvider.__init__ = patched_init

        agent = Agent(config, session=ctx)
        try:
            events = list(agent.process_query("test no reasoning"))

            final_events = [e for e in events if e["type"] == "agent_responded"]
            assert len(final_events) >= 1, "Expected at least one agent_responded event"

            last = final_events[-1]
            # reasoning may be absent or None depending on the code path
            if "reasoning" in last:
                assert last["reasoning"] is None or last["reasoning"] == "", (
                    f"Expected reasoning to be None or empty, got: {last.get('reasoning')}"
                )
        finally:
            ScriptedProvider.__init__ = original_init

    def test_reasoning_passthrough_with_tool_calls(self, config: AgentConfig, ctx: WorkerContext):
        """Reasoning should be passed through when there are also tool calls in the response."""
        # First response: tool call with reasoning
        # Second response: final answer
        config._provider_responses = [
            LLMResponse(
                content="",
                reasoning="I need to think about this carefully...",
                tool_calls=[{
                    "name": "Thought",
                    "arguments": {"content": "thinking step 1"},
                }],
                usage={"prompt_tokens": 10, "completion_tokens": 8},
                provider="scripted",
                model="mock-model",
            ),
            LLMResponse(
                content="Here is my final answer after thinking.",
                reasoning="My final reasoning for the answer.",
                tool_calls=None,
                usage={"prompt_tokens": 20, "completion_tokens": 5},
                provider="scripted",
                model="mock-model",
            ),
        ]

        original_init = ScriptedProvider.__init__

        def patched_init(self, cfg):
            original_init(self, cfg)
            self._responses = list(config._provider_responses)

        ScriptedProvider.__init__ = patched_init

        agent = Agent(config, session=ctx)
        try:
            events = list(agent.process_query("test reasoning with tool calls"))

            final_events = [e for e in events if e["type"] == "agent_responded"]
            assert len(final_events) >= 1, "Expected at least one agent_responded event"

            last = final_events[-1]
            assert "reasoning" in last, (
                f"Expected 'reasoning' in agent_responded event, got keys: {list(last.keys())}"
            )
            assert last["reasoning"] is not None, "Reasoning should not be None when provided"
        finally:
            ScriptedProvider.__init__ = original_init
