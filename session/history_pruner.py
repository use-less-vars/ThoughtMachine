"""
History Pruner: Pure-function-based pruning of user_history at save time.

Takes the full user_history list and returns a pruned copy, leaving the
original untouched. Designed to be called from FileSystemSessionStore.save_session
to reduce on-disk session file size while preserving enough context for
the GUI and future agent runs.

The algorithm:
1. Count summary messages (role='system' with summary=True).
2. If count < min_summaries_before_pruning → return copy unchanged.
3. Find the second-last summary index → this is cut_idx.
4. Partition: old = user_history[:cut_idx], safe = user_history[cut_idx:].
5. Walk 'old' and compact turns (user→final or user→last assistant).
6. Return compacted + safe.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────

FINAL_TOOL_NAMES: Set[str] = {'Final', 'FinalReport', 'RequestUserInteraction'}
"""Tool names that signal the end of a logical turn."""

# ──────────────────────────────────────────────────────────────────────
# Policy
# ──────────────────────────────────────────────────────────────────────


@dataclass
class PruningPolicy:
    """Configuration for how aggressively to prune history.

    Attributes:
        keep_reasoning: If True, keep assistant reasoning_content if present.
        keep_all_final_turns: If True, keep the full turn that ends with a Final tool.
        keep_plain_answer_only: If True, for turns without a final tool, keep only
            the last assistant with content and no tool_calls (plain answer).
            If False, keep the last assistant with any content (may have tool_calls).
        keep_system_notifications: If True, keep system notification messages in
            the pruned region. If False, drop them entirely.
        min_summaries_before_pruning: Minimum number of summary messages that
            must exist before any pruning is performed.
    """
    keep_reasoning: bool = False
    keep_all_final_turns: bool = True
    keep_plain_answer_only: bool = True
    keep_system_notifications: bool = False
    min_summaries_before_pruning: int = 2


# ──────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────


def prune_user_history(
    user_history: List[Dict[str, Any]],
    policy: PruningPolicy = PruningPolicy(),
) -> List[Dict[str, Any]]:
    """Return a pruned copy of *user_history* according to *policy*.

    The original list is never mutated.  If pruning is not warranted (e.g.
    fewer summaries than *min_summaries_before_pruning*) a shallow copy of
    the input is returned.
    """
    # 1. Count summary messages
    summary_indices = _find_summary_indices(user_history)
    if len(summary_indices) < policy.min_summaries_before_pruning:
        logger.debug(
            'prune_user_history: only %d summaries (< %d), returning copy',
            len(summary_indices), policy.min_summaries_before_pruning,
        )
        return list(user_history)

    # 2. Determine cut point — second-last summary
    cut_idx = summary_indices[-2]

    # 3. Partition
    old_segment = user_history[:cut_idx]
    safe_segment = user_history[cut_idx:]

    # 4. Compact old segment
    compacted = _compact_segment(old_segment, policy)

    # 5. Return compacted + safe
    result = compacted + safe_segment
    logger.debug(
        'prune_user_history: %d messages → %d (%.1f%% reduction)',
        len(user_history), len(result),
        (1 - len(result) / max(len(user_history), 1)) * 100,
    )
    return result


# ──────────────────────────────────────────────────────────────────────
# Internal helpers
# ──────────────────────────────────────────────────────────────────────


def _find_summary_indices(
    user_history: List[Dict[str, Any]],
) -> List[int]:
    """Return ascending list of indices of summary messages."""
    indices: List[int] = []
    for i, msg in enumerate(user_history):
        if msg.get('role') == 'system' and msg.get('summary') is True:
            indices.append(i)
    return indices


def _is_system_notification(msg: Dict[str, Any]) -> bool:
    """Check if a message is a system notification (user-role informational)."""
    if msg.get('is_system_notification') is True:
        return True
    content = msg.get('content', '')
    if isinstance(content, str) and '[SYSTEM NOTIFICATION]' in content:
        return True
    return False


def _compact_segment(
    segment: List[Dict[str, Any]],
    policy: PruningPolicy,
) -> List[Dict[str, Any]]:
    """Compact a list of messages by collapsing turns.

    Walks the segment sequentially, preserving original chronological order.
    - System messages pass through unchanged.
    - System notifications pass through only if keep_system_notifications is True.
    - Non-system messages are grouped into turns (starting at each user
      or assistant-with-tool_calls message) and each turn is compacted.
    - Orphaned messages (not in any valid turn) are dropped.
    """
    result: List[Dict[str, Any]] = []
    i = 0
    n = len(segment)

    while i < n:
        msg = segment[i]
        role = msg.get('role', '')

        # System messages pass through unchanged
        if role == 'system':
            result.append(msg)
            i += 1
            continue

        # System notifications — kept only if policy says so
        if _is_system_notification(msg):
            if policy.keep_system_notifications:
                result.append(msg)
            i += 1
            continue

        # Turn starter: user or assistant-with-tool_calls
        if role == 'user' or (role == 'assistant' and msg.get('tool_calls')):
            # Collect the full turn starting at i
            turn: List[Dict[str, Any]] = [msg]
            j = i + 1
            while j < n:
                next_msg = segment[j]
                next_role = next_msg.get('role', '')
                # Stop at next turn starter
                if next_role == 'user' or (next_role == 'assistant' and next_msg.get('tool_calls')):
                    break
                # System messages and notifications are handled separately — stop
                if next_role == 'system' or _is_system_notification(next_msg):
                    break
                # All other messages (tool, plain assistant) belong to current turn
                turn.append(next_msg)
                j += 1

            # Compact the turn and append
            compacted = _compact_turn(turn, policy)
            result.extend(compacted)
            i = j
            continue

        # Orphaned message (non-turnable, not system, not notification) — drop
        i += 1

    return result


def _group_turns(messages: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    """Group messages into turns, matching SummaryBuilder's logic.

    Rules (mirrors SummaryBuilder._group_messages_into_turns):
    - User messages always start a new turn
    - Assistant messages with tool_calls can also start a turn (after pruning)
    - Tool messages are attached only if the current turn already contains an
      assistant with tool_calls
    - All other messages (plain assistant responses) belong to current turn
    - Turns that don't start with user or assistant-with-tools are discarded
    - Orphaned messages that don't belong to any turn are dropped
    """
    turns: List[List[Dict[str, Any]]] = []
    current_turn: List[Dict[str, Any]] = []

    for msg in messages:
        role = msg.get('role', '')
        if role == 'user':
            if current_turn:
                turns.append(current_turn)
            current_turn = [msg]
        elif role == 'assistant' and msg.get('tool_calls'):
            if current_turn:
                turns.append(current_turn)
            current_turn = [msg]
        elif current_turn:
            if role == 'tool':
                # Check if ANY message in the current turn is an assistant
                # with tool_calls (not just the last one), to support
                # multi-tool-call scenarios.
                has_tool_call_assistant = any(
                    m.get('role') == 'assistant' and m.get('tool_calls')
                    for m in current_turn
                )
                if has_tool_call_assistant:
                    current_turn.append(msg)
                else:
                    # Orphaned tool — drop it
                    continue
            else:
                current_turn.append(msg)
        else:
            # Orphaned message with no turn context — drop it
            continue

    if current_turn:
        turns.append(current_turn)

    # Validate: only keep turns starting with user or assistant-with-tc
    valid_turns: List[List[Dict[str, Any]]] = []
    for turn in turns:
        if not turn:
            continue
        first_msg = turn[0]
        first_role = first_msg.get('role')
        if first_role == 'user':
            valid_turns.append(turn)
        elif first_role == 'assistant' and first_msg.get('tool_calls'):
            valid_turns.append(turn)
        else:
            logger.debug('_group_turns: discarding turn starting with %s', first_role)

    return valid_turns


def _compact_turn(
    turn: List[Dict[str, Any]],
    policy: PruningPolicy,
) -> List[Dict[str, Any]]:
    """Compact a single turn, returning only the messages to keep.

    A turn is a list starting with a user message OR an assistant with
    tool_calls (the latter occurs when pruning has cut off the user).
    """
    if not turn:
        return []

    first_msg = turn[0]
    first_role = first_msg.get('role')

    # Determine if we have a user message to keep
    keep_user: bool = (first_role == 'user')
    result: List[Dict[str, Any]] = []
    if keep_user:
        result.append(first_msg)  # always keep the user message

    # Collect all assistant messages in this turn
    assistant_msgs: List[Dict[str, Any]] = [
        msg for msg in turn if msg.get('role') == 'assistant'
    ]

    if not assistant_msgs:
        return result

    # Search for a final tool call among all assistants
    final_call: Optional[Dict[str, Any]] = None
    final_assistant: Optional[Dict[str, Any]] = None
    final_call_id: Optional[str] = None

    for asst in assistant_msgs:
        tool_calls = asst.get('tool_calls', [])
        if isinstance(tool_calls, list):
            for tc in tool_calls:
                func = tc.get('function', {})
                name = func.get('name', '') if isinstance(func, dict) else ''
                if isinstance(tc.get('function'), str):
                    # Handle legacy string format if any
                    pass
                if name in FINAL_TOOL_NAMES:
                    final_call = tc
                    final_assistant = asst
                    final_call_id = tc.get('id', '')
                    break
        if final_call:
            break

    if final_call is not None and final_assistant is not None:
        # Turn ends with a final tool call
        if policy.keep_all_final_turns:
            # Keep the assistant that has the final call
            result.append(final_assistant)

            # Find and keep the tool result matching the final call's id
            found_result = False
            for msg in turn:
                if (msg.get('role') == 'tool'
                        and msg.get('tool_call_id') == final_call_id):
                    result.append(msg)
                    found_result = True
                    break

            if not found_result:
                logger.warning(
                    'Compact turn: final tool call %s has no matching tool result',
                    final_call_id,
                )
        return result

    # No final tool found — find the last assistant to keep
    if policy.keep_plain_answer_only:
        # Keep only the LAST assistant with content AND no tool_calls
        # (a plain text answer, not a tool-calling intermediate)
        last_plain_assistant: Optional[Dict[str, Any]] = None
        for asst in reversed(assistant_msgs):
            content = asst.get('content', '')
            tool_calls = asst.get('tool_calls', [])
            has_content = bool(content and isinstance(content, str) and content.strip())
            has_tool_calls = bool(tool_calls)
            if has_content and not has_tool_calls:
                last_plain_assistant = asst
                break
        if last_plain_assistant is not None:
            result.append(last_plain_assistant)
        else:
            # Fallback: no plain assistant found (all had tool_calls).
            # Keep the last assistant with any content (content or tool_calls).
            last_content_assistant: Optional[Dict[str, Any]] = None
            for asst in reversed(assistant_msgs):
                content = asst.get('content', '')
                if content and isinstance(content, str) and content.strip():
                    last_content_assistant = asst
                    break

                # Also check tool_calls — an assistant without content
                # but with tool_calls is meaningful
                tool_calls = asst.get('tool_calls', [])
                if tool_calls:
                    last_content_assistant = asst
                    break

            if last_content_assistant is not None:
                result.append(last_content_assistant)
    else:
        # Legacy behavior: keep last assistant with content
        last_content_assistant: Optional[Dict[str, Any]] = None
        for asst in reversed(assistant_msgs):
            content = asst.get('content', '')
            if content and isinstance(content, str) and content.strip():
                last_content_assistant = asst
                break

            # Also check tool_calls — an assistant without content
            # but with tool_calls is meaningful
            tool_calls = asst.get('tool_calls', [])
            if tool_calls:
                last_content_assistant = asst
                break

        if last_content_assistant is not None:
            result.append(last_content_assistant)

    return result


