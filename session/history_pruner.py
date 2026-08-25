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
5. Walk 'old' and compact turns (text-only semantics, see _compact_turn).
6. Return compacted + safe.

Old-region compaction is TEXT-ONLY: assistant messages are kept without
tool_calls/reasoning_content, and tool results are kept only when their
call id maps to a Respond-family tool (converted to assistant-style text).
All messages keep their original in-turn order, which guarantees no
orphaned tool_call_ids survive into the compacted region.
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

RESPOND_FAMILY_NAMES: Set[str] = FINAL_TOOL_NAMES | {'Respond'}
"""Tool names whose outputs are preserved as assistant-style text
(Respond plus the Final-family tools)."""

# ──────────────────────────────────────────────────────────────────────
# Policy
# ──────────────────────────────────────────────────────────────────────


@dataclass
class PruningPolicy:
    """Configuration for how aggressively to prune history.

    Attributes:
        keep_reasoning: If True, keep assistant reasoning_content if present.
        keep_all_final_turns: API-compatibility only. The old-region compactor
            always strips tool_calls from assistant messages, so this flag no
            longer affects the output.
        keep_plain_answer_only: API-compatibility only. The old-region compactor
            always keeps assistant messages as text without tool_calls, so this
            flag no longer affects the output.
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
    """Compact a single turn into plain text only.

    Deterministic semantics for the old (pre-second-last-summary) region:

    - The first user message (if the turn starts with one) is kept as-is.
    - Assistant messages are kept as TEXT ONLY: ``tool_calls`` and
      ``reasoning_content`` are stripped, so no orphaned tool call IDs can
      survive and no assistant is ever preserved with a full tool_calls
      array (regardless of keep_all_final_turns / keep_plain_answer_only).
    - Tool results whose call id maps to a Respond-family tool name
      (Respond/Final/FinalReport/RequestUserInteraction) are CONVERTED to
      assistant-style text messages; all other tool results are dropped.
    - Messages keep their original in-turn order.

    A turn is a list starting with a user message OR an assistant with
    tool_calls (the latter occurs when pruning has cut off the user).
    """
    if not turn:
        return []

    first_msg = turn[0]
    first_role = first_msg.get('role')
    keep_user: bool = (first_role == 'user')

    # Map tool call ids -> tool names across every assistant in the turn.
    name_map: Dict[str, str] = {}
    for msg in turn:
        if msg.get('role') != 'assistant':
            continue
        tool_calls = msg.get('tool_calls', [])
        if not isinstance(tool_calls, list):
            continue
        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            func = tc.get('function', {})
            if isinstance(func, dict) and tc.get('id'):
                name_map[tc['id']] = func.get('name', '')

    result: List[Dict[str, Any]] = []
    for msg in turn:
        role = msg.get('role', '')
        if role == 'user':
            if keep_user:
                result.append(msg)
            continue
        if role == 'assistant':
            content = msg.get('content', '')
            if not (isinstance(content, str) and content.strip()):
                # Tool-call carrier with no text -> dropped
                continue
            kept = dict(msg)
            kept.pop('tool_calls', None)
            kept.pop('reasoning_content', None)
            result.append(kept)
            continue
        if role == 'tool':
            call_id = msg.get('tool_call_id', '')
            if name_map.get(call_id) in RESPOND_FAMILY_NAMES:
                converted = dict(msg)
                converted['role'] = 'assistant'
                converted.pop('tool_call_id', None)
                converted.pop('name', None)
                result.append(converted)
            continue
        # Any other role inside the turn is dropped.

    return result
