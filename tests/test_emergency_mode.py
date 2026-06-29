"""
Comprehensive tests for the emergency mode context reduction flow.

Emergency mode is activated when the LLM returns a token_limit_exceeded error.
When active, SummaryBuilder.build() skips the oldest ~20% of non-system messages
that appear AFTER the latest summary, preserving the main prompt and summary.

Important: SummaryBuilder.build() only includes messages that come AFTER the
latest summary message. Messages before the summary are excluded from context
(since the summary replaces them). The emergency mode skip operates on these
post-summary messages.

Test coverage:
1. SummaryBuilder: emergency_mode flag lifecycle
2. SummaryBuilder: message reduction when emergency_mode is True
3. SummaryBuilder: system prompt and summary preserved in emergency mode
4. HistoryProvider: emergency_mode delegation to SummaryBuilder
5. Full integration: HistoryProvider.get_context_for_llm with emergency_mode
"""

import pytest
from typing import List, Dict, Any
# Import from agent first to break circular import chain:
# session.context_builder -> agent.core.message -> agent.__init__ ->
# agent/core/agent -> session.context_builder
from agent.core.message_utils import group_messages_into_turns as _  # noqa: F401
from session.context_builder import SummaryBuilder
from session.history_provider import HistoryProvider


# ── Helpers ──────────────────────────────────────────────────────────────────

class FakeSession:
    """Minimal Session-like object for HistoryProvider testing."""
    def __init__(self):
        self.session_id = "test-emergency-session"
        self.user_history: List[Dict[str, Any]] = []
        self.summary = None
        self._seq = 0
        self.updated_at = None

    def _get_next_seq(self):
        self._seq += 1
        return self._seq


def make_turn(user_content: str, assistant_content: str) -> List[Dict[str, Any]]:
    """Create a simple user+assistant turn pair."""
    return [
        {"role": "user", "content": user_content},
        {"role": "assistant", "content": assistant_content},
    ]


def make_history_with_summary(num_pre_summary_turns: int = 5,
                              num_post_summary_turns: int = 5) -> List[Dict[str, Any]]:
    """Build history: system + N pre-summary turns + summary + M post-summary turns.

    This mirrors the real pattern where summarization collapses old history
    into a summary and newer messages follow it.
    """
    history = [
        {"role": "system", "content": "You are a helpful assistant."},
    ]
    for i in range(num_pre_summary_turns):
        history.extend(make_turn(f"Q{i+1}", f"A{i+1}"))
    history.append({
        "role": "system",
        "content": "Summary of previous conversation: User asked questions 1-5.",
        "pruning_keep_recent_turns": 5,
        "pruning_insertion_idx": 1 + 2 * num_pre_summary_turns,
    })
    for i in range(num_pre_summary_turns,
                   num_pre_summary_turns + num_post_summary_turns):
        history.extend(make_turn(f"Q{i+1}", f"A{i+1}"))
    return history


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def builder() -> SummaryBuilder:
    """A SummaryBuilder instance for context assembly."""
    return SummaryBuilder(default_keep_turns=10)


@pytest.fixture
def history_with_summary() -> List[Dict[str, Any]]:
    """History: system + 5 turns + summary + 5 more turns (10 post-summary msgs)."""
    return make_history_with_summary(5, 5)


# ── Tests: SummaryBuilder emergency_mode flag ─────────────────────────────────

class TestEmergencyModeFlag:

    def test_emergency_mode_defaults_to_false(self, builder):
        """emergency_mode is False on a new SummaryBuilder."""
        assert builder.emergency_mode is False

    def test_emergency_mode_can_be_set_true(self, builder):
        """Setting emergency_mode = True sticks."""
        builder.emergency_mode = True
        assert builder.emergency_mode is True

    def test_emergency_mode_can_be_toggled(self, builder):
        """emergency_mode can be toggled on and off."""
        builder.emergency_mode = True
        assert builder.emergency_mode is True
        builder.emergency_mode = False
        assert builder.emergency_mode is False


# ── Tests: SummaryBuilder message reduction ──────────────────────────────────

class TestEmergencyModeReduction:

    def test_no_reduction_when_emergency_off(self, builder, history_with_summary):
        """Normal build returns all post-summary messages when emergency_mode is False.

        History layout:
          system(1) + 5 turns(10 msgs) + summary(1) + 5 turns(10 msgs) = 22 total
        Post-summary: 10 non-system messages (Q6-A6 … Q10-A10)
        """
        context = builder.build(history_with_summary)
        non_system_in_context = [m for m in context if m.get("role") != "system"]
        assert len(non_system_in_context) == 10, (
            f"Expected 10 non-system messages with emergency OFF, "
            f"got {len(non_system_in_context)}"
        )

    def test_reduction_when_emergency_on(self, builder, history_with_summary):
        """Emergency mode skips oldest ~20% of post-summary non-system messages.

        10 post-summary messages → 20% = 2 skipped → 8 remain.
        The skipped messages are Q6-A6 (the oldest turn after the summary).
        """
        builder.emergency_mode = True
        context = builder.build(history_with_summary)

        non_system_in_context = [m for m in context if m.get("role") != "system"]

        # 10 msgs → int(10*0.2) = 2 → max(1, 2) = 2 skipped → 8 remain
        assert len(non_system_in_context) == 8, (
            f"Expected 8 non-system messages with emergency ON "
            f"(10 - 20% = 8), got {len(non_system_in_context)}"
        )

        # The oldest turn (Q6-A6) should have been dropped
        first_non_system = non_system_in_context[0]
        assert first_non_system.get("content") == "Q7", (
            f"Expected first non-system message to be 'Q7', "
            f"got '{first_non_system.get('content')}'"
        )

    def test_most_recent_messages_preserved(self, builder, history_with_summary):
        """The most recent messages survive emergency mode reduction."""
        builder.emergency_mode = True
        context = builder.build(history_with_summary)

        non_system = [m for m in context if m.get("role") != "system"]
        last_msg = non_system[-1]
        assert last_msg.get("content") == "A10", (
            f"Expected last message to be 'A10', got '{last_msg.get('content')}'"
        )

    def test_system_prompt_preserved(self, builder, history_with_summary):
        """Main system prompt survives emergency mode."""
        builder.emergency_mode = True
        context = builder.build(history_with_summary)

        system_msgs = [m for m in context if m.get("role") == "system"]
        main_prompts = [
            m for m in system_msgs
            if "Summary of previous conversation:" not in m.get("content", "")
        ]
        assert len(main_prompts) >= 1, "Main system prompt lost during emergency mode"
        assert main_prompts[0].get("content") == "You are a helpful assistant."

    def test_summary_preserved(self, builder, history_with_summary):
        """Summary message survives emergency mode."""
        builder.emergency_mode = True
        context = builder.build(history_with_summary)

        summaries = [
            m for m in context
            if m.get("role") == "system"
            and "Summary of previous conversation:" in m.get("content", "")
        ]
        assert len(summaries) >= 1, "Summary message lost during emergency mode"

    def test_emergency_with_small_history(self, builder):
        """Emergency mode with 4 post-summary messages.

        History: system + 2 turns (4 non-system messages), no summary.
        Emergency: skip_count = max(1, int(4*0.2)) = max(1, 0) = 1.
        After skip: ['Hi!', 'How are you?', 'Good!'].
        Turn group drops orphaned 'Hi!' (assistant without preceding user in this turn).
        Final: [How are you?, Good!] = 2 non-system.
        """
        small_history = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi!"},
            {"role": "user", "content": "How are you?"},
            {"role": "assistant", "content": "Good!"},
        ]
        builder.emergency_mode = True
        context = builder.build(small_history)
        non_system = [m for m in context if m.get("role") != "system"]
        assert len(non_system) == 2, (
            f"Expected 2 non-system messages from 4 with emergency ON "
            f"(skip 1, orphaned Hi! dropped), got {len(non_system)}"
        )
        assert non_system[0].get("content") == "How are you?"
        assert non_system[1].get("content") == "Good!"

    def test_emergency_no_summary_no_system(self, builder):
        """Emergency mode with no system prompt and no summary.

        History: 6 messages (3 turns: A-1, B-2, C-3), no system.
        No system found → all 6 are post_summary, all non_system.
        Emergency: skip_count = max(1, int(6*0.2)) = max(1, 1) = 1.
        After skip: [1, B, 2, C, 3].
        Turn group drops orphaned '1' (assistant without preceding user).
        Final: [B, 2, C, 3] = 4 non-system.
        """
        raw_history = [
            {"role": "user", "content": "A"},
            {"role": "assistant", "content": "1"},
            {"role": "user", "content": "B"},
            {"role": "assistant", "content": "2"},
            {"role": "user", "content": "C"},
            {"role": "assistant", "content": "3"},
        ]
        builder.emergency_mode = True
        context = builder.build(raw_history)
        non_system = [m for m in context if m.get("role") != "system"]
        # No system messages in history, and no system added by build
        assert len(non_system) == 4, (
            f"Expected 4 messages from 6 with emergency ON "
            f"(skip 1, orphaned assistant dropped), got {len(non_system)}"
        )

    def test_emergency_too_few_to_skip(self, builder):
        """Emergency mode with only 1 non-system message: forced to skip 1 -> 0."""
        tiny_history = [
            {"role": "system", "content": "Be helpful."},
            {"role": "user", "content": "Hi"},
        ]
        builder.emergency_mode = True
        context = builder.build(tiny_history)
        non_system = [m for m in context if m.get("role") != "system"]
        # 1 non-system → int(1*0.2) = 0 → max(1, 0) = 1 skipped → 0 remain
        assert len(non_system) == 0, (
            f"Expected 0 non-system messages from 1 with emergency ON, "
            f"got {len(non_system)}"
        )


# ── Tests: HistoryProvider delegation ────────────────────────────────────────

class TestHistoryProviderEmergencyMode:

    @pytest.fixture
    def provider(self) -> HistoryProvider:
        session = FakeSession()
        return HistoryProvider(session)

    def test_provider_emergency_mode_delegates(self, provider):
        """HistoryProvider.emergency_mode delegates to SummaryBuilder."""
        assert provider.emergency_mode is False, "Default should be False"
        provider.emergency_mode = True
        assert provider.emergency_mode is True, "Should reflect True after setting"
        assert provider.context_builder.emergency_mode is True, (
            "Underlying SummaryBuilder should also be True"
        )

    def test_provider_emergency_mode_toggle(self, provider):
        """Toggle emergency_mode through HistoryProvider."""
        provider.emergency_mode = True
        assert provider.emergency_mode is True
        provider.emergency_mode = False
        assert provider.emergency_mode is False
        assert provider.context_builder.emergency_mode is False

    def test_provider_emergency_reduces_context(self, provider):
        """HistoryProvider.get_context_for_llm with emergency_mode reduces context.

        History: system + 10 turns (20 non-system msgs), no summary.
        Normal: all 20 non-system messages.
        Emergency: skip_count = max(1, int(20*0.2)) = max(1, 4) = 4.
        After skip: 16 non-system messages remain (Q5-A5 … Q10-A10).
        """
        session = provider.session
        session.user_history.append({"role": "system", "content": "You are helpful."})
        for i in range(10):
            session.user_history.extend(make_turn(f"Q{i+1}", f"A{i+1}"))

        normal_context = provider.get_context_for_llm()
        normal_non_system = len([m for m in normal_context if m.get("role") != "system"])

        provider.clear_cache()
        provider.emergency_mode = True
        emergency_context = provider.get_context_for_llm()
        emergency_non_system = len([m for m in emergency_context if m.get("role") != "system"])

        assert emergency_non_system < normal_non_system, (
            f"Emergency context ({emergency_non_system} non-system) should be smaller "
            f"than normal context ({normal_non_system} non-system)"
        )
        # 20 non-system → int(20*0.2)=4 → max(1,4)=4 skipped → 16 remain
        assert emergency_non_system == 16, (
            f"Expected 16 non-system messages with emergency ON, "
            f"got {emergency_non_system}"
        )

    def test_provider_emergency_off_after_toggle(self, provider):
        """Turning emergency_mode off restores normal context size."""
        session = provider.session
        session.user_history.append({"role": "system", "content": "You are helpful."})
        for i in range(10):
            session.user_history.extend(make_turn(f"Q{i+1}", f"A{i+1}"))

        provider.clear_cache()
        provider.emergency_mode = True
        emergency_context = provider.get_context_for_llm()
        emergency_count = len([m for m in emergency_context if m.get("role") != "system"])

        provider.clear_cache()
        provider.emergency_mode = False
        normal_context = provider.get_context_for_llm()
        normal_count = len([m for m in normal_context if m.get("role") != "system"])

        assert normal_count > emergency_count, (
            f"Normal mode ({normal_count}) should have more messages than "
            f"emergency mode ({emergency_count})"
        )
        assert normal_count == 20, (
            f"Expected 20 non-system messages in normal mode, got {normal_count}"
        )
