"""
Session Size Bounding: keep main-agent session payloads under a byte cap.

Pure functions applied from ``FileSystemSessionStore.save_session`` right
before the atomic write.  Only sessions whose metadata carries
``agent_type == 'main'`` and whose serialized payload exceeds
``SESSION_SIZE_CAP_BYTES`` are touched; every other session file is left
byte-for-byte unchanged.

Enforcement order (strictly sequential; each step only runs while the
serialized payload is still over the cap):

1. Keep the 2 most recent full cycles untouched.
2. Compact every older cycle to exactly ``[query, terminal_answer]``
   (or ``[query]`` when the cycle has no terminal answer), stripping
   ``reasoning_content`` and ``tool_calls`` and dropping all intermediate
   messages.
3. Drop oldest compacted cycles first, one at a time, until under the cap.
4. Truncate ONLY non-terminal tool-result messages (role ``tool`` without
   ``response_type``, excluding legacy Respond-family terminal results)
   inside the 2 kept full cycles, to a per-message content budget of
   ``TOOL_CONTENT_BUDGET_BYTES`` bytes with the ``...[truncated]`` suffix.
   User queries and terminal answers are never truncated.
5. If still over the cap, allow the overrun and record
   ``metadata['history_over_capacity'] = True`` (never cleared).

Serialization mirrors the store's atomic write exactly
(``json.dumps(data, indent=2, default=str)``) so the measured size equals
the actual on-disk byte count.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

SESSION_SIZE_CAP_BYTES: int = 2_000_000
"""Maximum allowed serialized byte size for a main-agent session payload."""

TOOL_CONTENT_BUDGET_BYTES: int = 4096
"""Per-message content budget (bytes) for step-4 tool-output truncation."""

TRUNCATED_SUFFIX: str = '...[truncated]'
"""Suffix appended to truncated tool-result content."""

# Terminal-answer tool names.  Mirrors web_ui/backend/session_manager.py:
# current Respond family plus the legacy Final/FinalReport/RequestUserInteraction
# names (session_manager's mapping also assigns response_type for those).
FINAL_TOOL_NAMES: frozenset = frozenset({'Respond'})
LEGACY_TO_RESPOND: Dict[str, Dict[str, str]] = {
    'Final': {'response_type': 'answer'},
    'FinalReport': {'response_type': 'answer'},
    'RequestUserInteraction': {'response_type': 'question'},
}
ALL_RESPOND_NAMES: frozenset = FINAL_TOOL_NAMES | frozenset(LEGACY_TO_RESPOND)

# ─────────────────────────────────────────────────────────────────────────────
# Serialization / size measurement
# ─────────────────────────────────────────────────────────────────────────────


def _serialize(data: Dict[str, Any]) -> str:
    """Serialize *data* exactly like the store's atomic write.

    The store writes with ``json.dump(data, f, indent=2, default=str)``;
    mirror that call so the measured size equals the real file size.
    """
    return json.dumps(data, indent=2, default=str)


def payload_size_bytes(data: Dict[str, Any]) -> int:
    """Byte length of the serialized session payload (UTF-8)."""
    return len(_serialize(data).encode('utf-8'))


def set_session_size_bytes(data: Dict[str, Any]) -> int:
    """Compute and store top-level ``session_size_bytes`` via fixpoint.

    The stored value equals the byte length of the serialized payload, so
    re-serializing the file yields exactly this number.  Iterating is needed
    because the field itself contributes to the payload size; convergence
    happens as soon as the digit count stabilizes.
    """
    data['session_size_bytes'] = 0
    size = payload_size_bytes(data)
    for _ in range(8):
        data['session_size_bytes'] = size
        new_size = payload_size_bytes(data)
        if new_size == size:
            return size
        size = new_size
    data['session_size_bytes'] = size
    return size

# ─────────────────────────────────────────────────────────────────────────────
# Main-agent gate
# ─────────────────────────────────────────────────────────────────────────────


def is_main_agent_session(data: Dict[str, Any]) -> bool:
    """True when the payload belongs to a main-agent session.

    Bounding applies ONLY to sessions whose metadata carries
    ``agent_type == 'main'``.  Worker sessions and any session file
    without the flag are skipped defensively.
    """
    meta = data.get('metadata')
    return isinstance(meta, dict) and meta.get('agent_type') == 'main'

# ─────────────────────────────────────────────────────────────────────────────
# Cycle splitting
# ─────────────────────────────────────────────────────────────────────────────


def _is_system_notification(msg: Dict[str, Any]) -> bool:
    """True for user-role informational messages (explicit flag only).

    Matches agent/core/message.py semantics: the flag is an explicit dict
    key set at injection sites, never derived from content.
    """
    return msg.get('is_system_notification') is True


def _is_real_user_query(msg: Dict[str, Any]) -> bool:
    """True for a real user query — a user message that is not a system notification."""
    return msg.get('role') == 'user' and not _is_system_notification(msg)


def _split_cycles(
    user_history: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[List[Dict[str, Any]]]]:
    """Split *user_history* into ``(leading, cycles)``.

    A new cycle starts at every real user query (role ``user`` and not a
    system notification).  All following assistant/tool/system messages
    belong to that cycle until the next real user query.  User-role system
    notifications attach to the preceding cycle as trailing content.
    Messages before the first real user query are returned as *leading*
    and are left untouched by bounding.
    """
    leading: List[Dict[str, Any]] = []
    cycles: List[List[Dict[str, Any]]] = []
    current: Optional[List[Dict[str, Any]]] = None
    for msg in user_history:
        if _is_real_user_query(msg):
            current = [msg]
            cycles.append(current)
        elif current is not None:
            current.append(msg)
        else:
            leading.append(msg)
    return leading, cycles

# ─────────────────────────────────────────────────────────────────────────────
# Terminal-answer identification
# ─────────────────────────────────────────────────────────────────────────────


def _respond_call_ids(cycle: List[Dict[str, Any]]) -> set:
    """tool_call_ids of Respond-family calls anywhere in *cycle*."""
    ids = set()
    for msg in cycle:
        if msg.get('role') != 'assistant':
            continue
        for call in msg.get('tool_calls') or []:
            if not isinstance(call, dict):
                continue
            fn = call.get('function')
            if isinstance(fn, dict) and fn.get('name') in ALL_RESPOND_NAMES:
                cid = call.get('id')
                if cid:
                    ids.add(cid)
    return ids


def _find_terminal_answer(cycle: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Find the cycle's single terminal answer, scanning from the END.

    The LAST message in the cycle matching any of the following wins:

    (a) a tool message with ``response_type`` set (set after a Respond call);
    (b) a tool message whose ``tool_call_id`` maps to a Respond-family call
        (Final / FinalReport / RequestUserInteraction / Respond) in a
        preceding assistant message's ``tool_calls`` — covers legacy
        sessions whose tool results never got ``response_type``;
    (c) a standalone assistant message without ``tool_calls`` (and not a
        system notification).

    Fallback: if no match is found (e.g. an aborted cycle), return the last
    assistant message without ``tool_calls``; if none exists either, return
    None — the caller then keeps the cycle's query only.
    """
    respond_ids = _respond_call_ids(cycle)
    for msg in reversed(cycle):
        role = msg.get('role')
        if role == 'tool':
            if msg.get('response_type') is not None:
                return msg
            if msg.get('tool_call_id') in respond_ids:
                return msg
        elif role == 'assistant':
            if not msg.get('tool_calls') and not _is_system_notification(msg):
                return msg
    return None

# ─────────────────────────────────────────────────────────────────────────────
# Cycle compaction
# ─────────────────────────────────────────────────────────────────────────────


def _strip_message(msg: Dict[str, Any]) -> Dict[str, Any]:
    """Copy *msg* with ``reasoning_content`` and ``tool_calls`` removed."""
    return {k: v for k, v in msg.items() if k not in ('reasoning_content', 'tool_calls')}


def _compact_cycle(cycle: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Compact a cycle to exactly ``[query, terminal]`` (or ``[query]``).

    The original user query content is never truncated or altered.  The
    terminal answer (assistant or tool message) is kept; ``reasoning_content``
    and ``tool_calls`` are stripped from the kept messages.  Every other
    message (intermediate tool outputs, non-terminal
    Final/RequestUserInteraction results, notifications, summaries) is
    dropped.
    """
    query = _strip_message(cycle[0])
    terminal = _find_terminal_answer(cycle)
    if terminal is None:
        return [query]
    return [query, _strip_message(terminal)]

# ─────────────────────────────────────────────────────────────────────────────
# Step-4 truncation
# ─────────────────────────────────────────────────────────────────────────────


def _truncate_content(content: str, budget: int = TOOL_CONTENT_BUDGET_BYTES) -> str:
    """Byte-aware truncate *content* to *budget* bytes + ``TRUNCATED_SUFFIX``.

    Never splits a multi-byte UTF-8 character at the cut point.
    """
    if len(content.encode('utf-8')) <= budget:
        return content
    prefix_budget = max(0, budget - len(TRUNCATED_SUFFIX.encode('utf-8')))
    prefix = content[:prefix_budget]
    while len(prefix.encode('utf-8')) > prefix_budget:
        prefix = prefix[:-1]
    return prefix + TRUNCATED_SUFFIX


def _truncate_full_cycles(
    full_cycles: List[List[Dict[str, Any]]],
) -> Optional[List[List[Dict[str, Any]]]]:
    """Truncate non-terminal tool outputs inside the kept full cycles.

    Only messages with role ``tool``, no ``response_type``, and a
    ``tool_call_id`` that is NOT a Respond-family terminal result are
    candidates.  User queries and terminal answers are never touched.

    Returns a new list of cycles with truncated copies, or None when
    nothing was truncated.
    """
    modified = False
    new_full: List[List[Dict[str, Any]]] = []
    for cycle in full_cycles:
        respond_ids = _respond_call_ids(cycle)
        new_cycle: List[Dict[str, Any]] = []
        for msg in cycle:
            if (
                msg.get('role') == 'tool'
                and msg.get('response_type') is None
                and msg.get('tool_call_id') not in respond_ids
            ):
                content = msg.get('content')
                if isinstance(content, str):
                    truncated = _truncate_content(content)
                    if truncated != content:
                        copied = dict(msg)
                        copied['content'] = truncated
                        new_cycle.append(copied)
                        modified = True
                        continue
            new_cycle.append(msg)
        new_full.append(new_cycle)
    if not modified:
        return None
    return new_full

# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────


def apply_session_size_bounding(data: Dict[str, Any]) -> bool:
    """Bound *data*'s ``user_history`` in place; return True if bounding ran.

    Gate: the payload must belong to a main-agent session
    (``metadata['agent_type'] == 'main'``) AND its serialized size must
    exceed ``SESSION_SIZE_CAP_BYTES``.  Sessions without the flag, or under
    the cap, are never mutated and return False.

    When the gate passes, steps 1-5 of the enforcement order are applied
    strictly; step 5 records ``metadata['history_over_capacity'] = True``
    (never cleared) when the payload is still over the cap.
    """
    if not is_main_agent_session(data):
        return False
    history = data.get('user_history')
    if not isinstance(history, list) or not history:
        return False
    if payload_size_bytes(data) <= SESSION_SIZE_CAP_BYTES:
        return False

    leading, cycles = _split_cycles(history)
    if not cycles:
        # No real user query anywhere — nothing to bound.
        return False

    bounded = True

    # Steps 1-2: keep the 2 most recent cycles FULL; compact everything older.
    full_cycles: List[List[Dict[str, Any]]] = list(cycles[-2:])
    compacted: List[List[Dict[str, Any]]] = [_compact_cycle(c) for c in cycles[:-2]]

    def _rebuild(
        compacted_list: List[List[Dict[str, Any]]],
        full_list: List[List[Dict[str, Any]]],
    ) -> List[Dict[str, Any]]:
        rebuilt = list(leading)
        for cycle in compacted_list + full_list:
            rebuilt.extend(cycle)
        return rebuilt

    data['user_history'] = _rebuild(compacted, full_cycles)
    if payload_size_bytes(data) <= SESSION_SIZE_CAP_BYTES:
        return bounded

    # Step 3: drop oldest compacted cycles first, one at a time.
    while compacted and payload_size_bytes(data) > SESSION_SIZE_CAP_BYTES:
        compacted.pop(0)
        data['user_history'] = _rebuild(compacted, full_cycles)
    if payload_size_bytes(data) <= SESSION_SIZE_CAP_BYTES:
        return bounded

    # Step 4: truncate non-terminal tool outputs inside the 2 kept full cycles.
    new_full = _truncate_full_cycles(full_cycles)
    if new_full is not None:
        full_cycles = new_full
        data['user_history'] = _rebuild(compacted, full_cycles)
    if payload_size_bytes(data) <= SESSION_SIZE_CAP_BYTES:
        return bounded

    # Step 5: only the 2 full cycles remain and the payload is still over
    # the cap — allow the overrun and record it (never cleared).
    meta = data.get('metadata')
    if isinstance(meta, dict):
        meta['history_over_capacity'] = True
    logger.warning(
        f"[SessionSizeBounding] Session {data.get('session_id', '?')} exceeds "
        f"{SESSION_SIZE_CAP_BYTES} bytes after bounding; history_over_capacity set"
    )
    return bounded
