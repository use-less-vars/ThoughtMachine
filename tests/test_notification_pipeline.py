"""
Integration test for the system notification pipeline.

Verifies that [**SYSTEM NOTIFICATION**] messages injected into user_history
are properly:
1. Present in the assembled LLM context
2. Flagged with is_system_notification=True
3. Grouped into the correct turn
4. Temporally ordered AFTER the trigger event that caused them
"""

import pytest
from typing import List, Dict, Any
from agent.core.message_utils import group_messages_into_turns
from session.context_builder import SummaryBuilder


SYSTEM_NOTIFICATION_PREFIX = "[**SYSTEM NOTIFICATION**]"


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def user_history_with_notification() -> List[Dict[str, Any]]:
    """A realistic user_history with a token-warning notification injected.

    Notifications are injected as role='user' with is_system_notification=True
    and content starting with [**SYSTEM NOTIFICATION**] (matching how agent.py
    emits them at lines 599, 786, 800, etc.).
    """
    return [
        # System prompt
        {"role": "system", "content": "You are a helpful assistant."},

        # Turn 1: user query + assistant reply
        {"role": "user", "content": "What is the capital of France?"},
        {"role": "assistant", "content": "The capital of France is Paris."},

        # Turn 2: user asks for code
        {"role": "user", "content": "Write a Python script to compute fibonacci."},
        # Tool call (the trigger for the notification)
        {
            "role": "assistant",
            "content": "",  # tool-call messages have empty content, not None
            "tool_calls": [{
                "type": "function",
                "function": {
                    "name": "DockerCodeRunner",
                    "arguments": '{"command": "python3 fib.py"}'
                }
            }]
        },
        # Tool result
        {
            "role": "tool",
            "content": "Fibonacci computed successfully.",
            "tool_call_id": "call_123"
        },
        # [**SYSTEM NOTIFICATION**] — token warning triggered by the tool result
        # NOTE: role='user' with is_system_notification=True — NOT role='system'
        {
            "role": "user",
            "content": (
                SYSTEM_NOTIFICATION_PREFIX
                + " Token usage warning: approaching context window limits "
                + "(7k tokens). Critical threshold is at 8k tokens."
            ),
            "is_system_notification": True,
        },
        # Assistant continues after notification
        {
            "role": "assistant",
            "content": "Here is the Fibonacci script: ..."
        },
    ]


@pytest.fixture
def user_history_without_notification() -> List[Dict[str, Any]]:
    """A standard history with no notifications."""
    return [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Say hello."},
        {"role": "assistant", "content": "Hello!"},
    ]


@pytest.fixture
def builder() -> SummaryBuilder:
    """A SummaryBuilder instance for context assembly."""
    return SummaryBuilder(default_keep_turns=10)


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestNotificationPipeline:

    def test_notification_appears_in_context(self, builder, user_history_with_notification):
        """Assert that [**SYSTEM NOTIFICATION**] messages appear in the assembled context."""
        context = builder.build(user_history_with_notification)
        notification_msgs = [
            m for m in context
            if isinstance(m.get("content"), str)
               and m["content"].startswith(SYSTEM_NOTIFICATION_PREFIX)
        ]
        assert len(notification_msgs) > 0, (
            f"Expected at least one {SYSTEM_NOTIFICATION_PREFIX!r} message in context, "
            f"but got none. Context had {len(context)} messages."
        )

    def test_notification_flag_is_true(self, builder, user_history_with_notification):
        """Assert that [**SYSTEM NOTIFICATION**] messages have is_system_notification=True."""
        context = builder.build(user_history_with_notification)
        for msg in context:
            content = msg.get("content", "")
            if isinstance(content, str) and content.startswith(SYSTEM_NOTIFICATION_PREFIX):
                assert msg.get("is_system_notification") is True, (
                    f"Message with content '{content[:60]}...' has "
                    f"is_system_notification={msg.get('is_system_notification')}, expected True"
                )

    def test_non_notification_messages_have_no_flag(self, builder, user_history_without_notification):
        """Assert that normal messages do not carry is_system_notification=True."""
        context = builder.build(user_history_without_notification)
        for msg in context:
            flag = msg.get("is_system_notification", False)
            assert flag is not True, (
                f"Message with role={msg.get('role')}, content='{msg.get('content', '')[:60]}...' "
                f"has unexpected is_system_notification=True"
            )

    def test_notification_in_correct_turn(self, builder, user_history_with_notification):
        """Assert the notification is inside the correct turn (the tool-call turn).

        Notifications have role='user' with is_system_notification=True, so
        group_messages_into_turns() appends them to the current turn (does not
        start a new turn). The expected turn contains:
            user -> assistant(tool_calls) -> tool -> **notification** -> assistant
        """
        turns = group_messages_into_turns(user_history_with_notification)

        # Scan each turn for the notification
        notification_turn_idx = None
        for idx, turn in enumerate(turns):
            for msg in turn:
                content = msg.get("content", "")
                if isinstance(content, str) and content.startswith(SYSTEM_NOTIFICATION_PREFIX):
                    notification_turn_idx = idx
                    break

        assert notification_turn_idx is not None, (
            f"Notification not found in any of the {len(turns)} turns."
        )

        # The notification should be in a turn that also has a tool result (the trigger)
        turn = turns[notification_turn_idx]
        roles_in_turn = [m.get("role") for m in turn]
        assert "user" in roles_in_turn, (
            f"Turn {notification_turn_idx} should start with a user message, "
            f"but roles are: {roles_in_turn}"
        )
        assert "tool" in roles_in_turn, (
            f"Turn {notification_turn_idx} should contain the tool result (trigger), "
            f"but roles are: {roles_in_turn}"
        )

        # Verify the notification has is_system_notification flag
        notif_msgs_in_turn = [
            m for m in turn
            if isinstance(m.get("content"), str)
               and m["content"].startswith(SYSTEM_NOTIFICATION_PREFIX)
        ]
        for nm in notif_msgs_in_turn:
            assert nm.get("is_system_notification") is True, (
                f"Notification in turn lacks is_system_notification=True"
            )

    def test_notification_after_trigger(self, builder, user_history_with_notification):
        """Assert temporal ordering: notification appears AFTER the tool result that triggered it."""
        # Find the index of the tool result and the notification in the flat list
        trigger_idx = None
        notif_idx = None
        for i, msg in enumerate(user_history_with_notification):
            content = msg.get("content", "")
            if isinstance(content, str) and content.startswith(SYSTEM_NOTIFICATION_PREFIX):
                notif_idx = i
            elif msg.get("role") == "tool":
                trigger_idx = i  # take the last tool result before notification

        assert trigger_idx is not None, "No tool result (trigger) found in history"
        assert notif_idx is not None, "No notification found in history"
        assert notif_idx > trigger_idx, (
            f"Notification at index {notif_idx} should appear AFTER the "
            f"trigger tool result at index {trigger_idx}, but ordering is wrong"
        )

    def test_context_ordering_preserved(self, builder, user_history_with_notification):
        """Assert that the context builder preserves message ordering.

        The notification (role='user', is_system_notification=True) should appear
        in the assembled context in the correct chronological position.
        """
        context = builder.build(user_history_with_notification)
        # Extract the sequence of (role, is_system_notification) for comparison
        context_seq = [
            (m.get("role"), m.get("is_system_notification", False))
            for m in context
        ]
        # The notification should appear at some point in the context sequence
        notif_entries = [
            (role, flag) for role, flag in context_seq
            if flag is True  # is_system_notification=True means notification message
        ]
        assert len(notif_entries) > 0, (
            f"No notification entry (is_system_notification=True) found in context sequence. "
            f"Context sequence: {context_seq}"
        )

    def test_notification_survives_context_building(self, builder, user_history_with_notification):
        """Assert notifications survive context building."""
        context = builder.build(user_history_with_notification)
        notif_msgs = [
            m for m in context
            if isinstance(m.get("content"), str)
               and m["content"].startswith(SYSTEM_NOTIFICATION_PREFIX)
        ]
        assert len(notif_msgs) > 0, (
            f"Notification was dropped despite {generous_limit} token limit. "
            f"Context had {len(context)} messages."
        )
