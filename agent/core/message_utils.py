"""
Shared message grouping and manipulation utilities.

Provides a single source of truth for turn-grouping logic used by
both agent.py (summary pruning) and context_builder.py (context assembly).
"""

from typing import Any, Dict, List, Optional


def group_messages_into_turns(messages: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    """Group non-system messages into conversation turns (user + assistant + tool responses).

    Rules:
    - User messages always start a new turn
    - Assistant messages with tool_calls can also start a turn (after pruning)
    - All messages after a turn start belong to that turn until next user message
    - System messages should be filtered out before calling this method
    - Turns that don't start with user or assistant-with-tools are discarded

    This ensures tool call sequences stay together, even when they start with
    an assistant (due to pruning cutting off the user part of the turn).

    Key improvement over earlier versions:
    - Tool results are attached to the current turn if ANY message in the turn
      is an assistant with tool_calls (not just the last message), supporting
      multi-tool-call scenarios where multiple tool results follow a single
      assistant message.
    """
    turns = []
    current_turn = []

    import os
    debug = os.environ.get('DEBUG_CONTEXT') or os.environ.get('DEBUG_TURN_GROUPING')

    if debug:
        log('DEBUG', 'core.message_utils', f'[DEBUG_TURN_GROUPING] Grouping {len(messages)} messages')
        max_to_show = 10
        for i, msg in enumerate(messages[:max_to_show]):
            role = msg.get('role')
            content_preview = str(msg.get('content', ''))[:50]
            has_tool_calls = 'tool_calls' in msg and msg['tool_calls']
            log('DEBUG', 'core.message_utils', f'  [{i}] {role}: {content_preview}... tool_calls={has_tool_calls}')
        if len(messages) > max_to_show:
            log('DEBUG', 'core.message_utils', f'  ... and {len(messages) - max_to_show} more messages')

    for msg in messages:
        role = msg.get('role')
        content = msg.get('content', '')

        # Skip system messages
        if role == 'system':
            continue

        # Start a new turn on user messages or assistant messages with tool_calls
        if role == 'user':
            is_sys_notif = msg.get('is_system_notification')
            if is_sys_notif:
                # System notification: append to current turn, don't start a new one
                if current_turn:
                    current_turn.append(msg)
                # Else: orphan notification, skip it
                continue
            if current_turn:
                turns.append(current_turn)
            current_turn = [msg]
        elif role == 'assistant' and msg.get('tool_calls'):
            if current_turn:
                turns.append(current_turn)
            current_turn = [msg]
        elif current_turn:
            # Check if the message belongs to the current turn
            if role == 'tool':
                # Attach tool result if ANY message in the current turn is an
                # assistant with tool_calls (multi-tool-call support)
                has_tool_call_assistant = any(
                    m.get('role') == 'assistant' and m.get('tool_calls')
                    for m in current_turn
                )
                if has_tool_call_assistant:
                    current_turn.append(msg)
                else:
                    if debug:
                        tool_call_id = msg.get('tool_call_id', 'unknown')
                        log('DEBUG', 'core.message_utils', f'[DEBUG_TURN_GROUPING] Discarding orphaned tool message: {tool_call_id}')
                    continue
            else:
                current_turn.append(msg)
        else:
            # No current turn, discard orphaned message
            if debug:
                log('DEBUG', 'core.message_utils', f'[DEBUG_TURN_GROUPING] Discarding orphaned {role} message')
            continue

    if current_turn:
        turns.append(current_turn)

    # Filter to only valid turns (start with user or assistant-with-tools)
    valid_turns = []
    for turn in turns:
        if not turn:
            continue
        first_msg = turn[0]
        first_role = first_msg.get('role')
        if first_role == 'user':
            valid_turns.append(turn)
        elif first_role == 'assistant' and first_msg.get('tool_calls'):
            valid_turns.append(turn)
        elif debug:
            log('DEBUG', 'core.message_utils', f'[DEBUG_TURN_GROUPING] Discarding turn starting with {first_role}')

    if debug:
        log('DEBUG', 'core.message_utils', f'[DEBUG_TURN_GROUPING] Returned {len(valid_turns)} valid turns')
        max_to_show = 10
        for i, turn in enumerate(valid_turns[:max_to_show]):
            log('DEBUG', 'core.message_utils', f"  Turn {i}: {[msg.get('role') for msg in turn]}")
        if len(valid_turns) > max_to_show:
            log('DEBUG', 'core.message_utils', f'  ... and {len(valid_turns) - max_to_show} more turns')

    return valid_turns


def group_messages_into_turns_with_indices(
    messages: List[Dict[str, Any]]
) -> tuple[List[List[Dict[str, Any]]], List[int]]:
    """Group messages into turns AND track the original index of each turn's first message.

    This is used by _find_summary_insertion_index which needs to map turns back
    to their positions in the original user_history (which includes system messages).

    Returns:
        (turns, turn_start_indices) where turn_start_indices[i] is the original
        index in `messages` of the first message of turns[i].
    """
    turns = []
    current_turn = []
    turn_start_indices = []

    for i, msg in enumerate(messages):
        role = msg.get('role')

        # Skip system messages
        if role == 'system':
            continue

        if role == 'user':
            is_sys_notif = msg.get('is_system_notification')
            if is_sys_notif:
                # System notification: append to current turn, don't start a new one
                if current_turn:
                    current_turn.append(msg)
                # Else: orphan notification, skip it
                continue
            if current_turn:
                turns.append(current_turn)
            current_turn = [msg]
            turn_start_indices.append(i)
        elif role == 'assistant' and msg.get('tool_calls'):
            if current_turn:
                turns.append(current_turn)
            current_turn = [msg]
            turn_start_indices.append(i)
        elif current_turn:
            if role == 'tool':
                # Attach tool result if ANY message in the current turn is an
                # assistant with tool_calls (multi-tool-call support)
                has_tool_call_assistant = any(
                    m.get('role') == 'assistant' and m.get('tool_calls')
                    for m in current_turn
                )
                if has_tool_call_assistant:
                    current_turn.append(msg)
                else:
                    continue
            else:
                current_turn.append(msg)
        else:
            continue

    if current_turn:
        turns.append(current_turn)

    # Filter to only valid turns, keeping matching start indices
    valid_turns = []
    valid_indices = []
    for idx, turn in enumerate(turns):
        if not turn:
            continue
        first_msg = turn[0]
        first_role = first_msg.get('role')
        if first_role == 'user':
            valid_turns.append(turn)
            valid_indices.append(turn_start_indices[idx])
        elif first_role == 'assistant' and first_msg.get('tool_calls'):
            valid_turns.append(turn)
            valid_indices.append(turn_start_indices[idx])

    return valid_turns, valid_indices

