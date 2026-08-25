"""Session history size bounding for main-agent session files.

apply_session_size_bounding enforces the serialized session hard cap
(SESSION_SIZE_CAP_BYTES) when the session is persisted.

Mechanism (summary-anchored):
1. The boundary is the SECOND-LAST summary message (role=='system' with
   summary is True). Everything from that summary INCLUSIVE to the end of
   the history is kept fully intact (byte-identical) — i.e. the latest two
   pruning cycles survive verbatim. With fewer than two summaries nothing
   is filtered (the store-side re-measure guard handles the cap later).
2. The older region (messages before the second-last summary) is filtered
   to keep only: user query messages (role=='user' without the explicit
   is_system_notification flag), plain assistant messages (no tool_calls),
   and tool results whose resolved tool name is a final-answer tool
   (Respond / Final / FinalReport / RequestUserInteraction — see
   ALL_RESPOND_NAMES). Tool names are resolved via tool_call_id against
   assistant tool_calls in the same region. Everything else (tool-call
   turns, intermediate tool results, thinking, system messages) is
   dropped, preserving order.
3. If the payload is still over the cap, retained older-region messages
   are dropped one at a time (oldest first), re-measuring the payload
   after each drop; when the older region is exhausted, the oldest
   messages of the kept region are dropped the same way. There is no
   truncation-to-budget: dropping is the only mechanism.
4. metadata['history_over_capacity'] is set to True only when a single
   message alone serializes to more than SESSION_SIZE_CAP_BYTES
   (computed over the ORIGINAL history, before any dropping). The flag is
   never cleared. A later store-side re-measure guard (Phase 2b) refuses
   to persist an oversized session when this flag is absent.
"""

import json
import logging

from session.history_pruner import _find_summary_indices

logger = logging.getLogger(__name__)

SESSION_SIZE_CAP_BYTES = 2_000_000
# Kept exported for tests (no longer used by the bounding logic).
TOOL_CONTENT_BUDGET_BYTES = 4096
TRUNCATED_SUFFIX = "...[truncated]"

FINAL_TOOL_NAMES = frozenset({"Respond"})
LEGACY_TO_RESPOND = {
    "Final": {"response_type": "answer"},
    "FinalReport": {"response_type": "answer"},
    "RequestUserInteraction": {"response_type": "question"},
}
ALL_RESPOND_NAMES = FINAL_TOOL_NAMES | frozenset(LEGACY_TO_RESPOND)


def _serialize(data):
    """Serialize like the store does (json.dumps indent=2, default=str)."""
    return json.dumps(data, indent=2, default=str)


def payload_size_bytes(data):
    """Size in bytes of the serialized payload (same serializer as store)."""
    return len(_serialize(data).encode("utf-8"))


def set_session_size_bytes(data):
    """Stamp session_size_bytes onto the data (fixpoint, at most 8 iterations)."""
    size = payload_size_bytes(data)
    for _ in range(8):
        data["session_size_bytes"] = size
        next_size = payload_size_bytes(data)
        if next_size == size:
            break
        size = next_size


def is_main_agent_session(data):
    metadata = data.get("metadata")
    return isinstance(metadata, dict) and metadata.get("agent_type") == "main"


def _is_system_notification(msg):
    return msg.get("is_system_notification") is True


def _strip_message(msg):
    """Copy of a message with reasoning/tool-call fields removed."""
    return {
        key: value
        for key, value in msg.items()
        if key not in ("reasoning_content", "tool_calls")
    }


def _tool_name_map(older):
    """Map tool_call_id -> function.name from assistant tool_calls in a region."""
    name_map = {}
    for msg in older:
        if msg.get("role") != "assistant":
            continue
        for call in msg.get("tool_calls") or []:
            fn = call.get("function")
            cid = call.get("id")
            if isinstance(fn, dict) and cid:
                name_map[cid] = fn.get("name", "")
    return name_map


def _filter_older_region(older, name_map):
    """Keep only user queries, plain assistant messages, final-answer tool results."""
    kept = []
    for msg in older:
        role = msg.get("role")
        if role == "user" and not _is_system_notification(msg):
            kept.append(msg)
        elif role == "assistant" and not msg.get("tool_calls"):
            kept.append(msg)
        elif role == "tool" and name_map.get(msg.get("tool_call_id")) in ALL_RESPOND_NAMES:
            kept.append(msg)
    return kept


def _any_single_message_over_cap(history):
    """True if any single message alone serializes larger than the cap."""
    for msg in history:
        try:
            if len(_serialize(msg).encode("utf-8")) > SESSION_SIZE_CAP_BYTES:
                return True
        except Exception:
            continue
    return False


def apply_session_size_bounding(data):
    """Enforce the hard cap; return True if the history was changed."""
    if not is_main_agent_session(data):
        return False
    if payload_size_bytes(data) <= SESSION_SIZE_CAP_BYTES:
        return False
    history = data.get("user_history")
    if not isinstance(history, list) or not history:
        return False

    summary_indices = _find_summary_indices(history)
    if len(summary_indices) < 2:
        # No boundary to anchor on: keep everything; the store-side
        # re-measure guard enforces the cap on the next save.
        return False

    cut_idx = summary_indices[-2]
    kept = list(history[cut_idx:])
    older = list(history[:cut_idx])

    name_map = _tool_name_map(older)
    filtered_older = _filter_older_region(older, name_map)
    changed = len(filtered_older) != len(older)

    if _any_single_message_over_cap(history):
        metadata = data.get("metadata")
        if isinstance(metadata, dict):
            metadata["history_over_capacity"] = True
            logger.warning(
                "Session history contains a message larger than the %d-byte cap; "
                "flagging history_over_capacity.",
                SESSION_SIZE_CAP_BYTES,
            )

    data["user_history"] = filtered_older + kept

    while payload_size_bytes(data) > SESSION_SIZE_CAP_BYTES:
        if filtered_older:
            filtered_older.pop(0)
            changed = True
        elif kept:
            kept.pop(0)
            changed = True
        else:
            break
        data["user_history"] = filtered_older + kept

    return changed
