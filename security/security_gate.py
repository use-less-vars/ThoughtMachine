"""
security_gate.py — Unified permission gate for the ThoughtMachine agent.

Replaces the ad-hoc ``_check_permissions`` / ``is_allowed`` flow with a single
entry point that merges **session permissions** (what the user chose for this
session) with **workspace capabilities** (what the workspace operator has
declared the workspace may do).

Design
------
1. ``get_effective_permissions()`` merges the two sources into one flat dict.
2. ``check_required_categories()`` compares a tool's declared requirements
   against that merged dict and, if needed, fires a ``SecurityPromptEvent``
   and waits for the user to approve or deny.

Module-level flag ``USE_UNIFIED_GATE`` tells the tool executor whether to use
this module (``True``) or keep the old path (``False``).
"""

from __future__ import annotations

import queue
import uuid
from typing import Any, Dict, List, Optional, Tuple

from thoughtmachine.workspace_capabilities import (
    WorkspaceCapabilities,
    load_workspace_capabilities,
)
from thoughtmachine.security import SessionPermissions, _pending_security_requests, _pending_requests_lock, resolve_security_prompt
from agent.events import SecurityPromptEvent, EventType

# ── Pending-prompt registry ──────────────────────────────────────────────

# Uses shared _pending_security_requests from thoughtmachine.security




# ══════════════════════════════════════════════════════════════════════════
#  Loader
# ══════════════════════════════════════════════════════════════════════════


def get_workspace_capabilities(workspace_id: str) -> WorkspaceCapabilities:
    """
    Load workspace capabilities via the canonical loader.

    Returns a default (fully-permissive) ``WorkspaceCapabilities`` when the
    file does not exist or cannot be parsed.
    """
    caps = load_workspace_capabilities(workspace_id)
    if caps is None:
        return WorkspaceCapabilities()
    return caps


# ══════════════════════════════════════════════════════════════════════════
#  Merge (session × workspace)
# ══════════════════════════════════════════════════════════════════════════


def _min_permission(
    session_value: Any,
    workspace_allows: Optional[bool],
) -> Any:
    """
    Combine a session permission value with a workspace capability boolean.

    * If *workspace_allows* is ``None``, return *session_value* unchanged.
    * If *workspace_allows* is ``False``, the effective value is ``False``
      (hard deny) for boolean-like categories; for string categories we
      return the lowest equivalent.
    * If *workspace_allows* is ``True``, return *session_value* unchanged.
    """
    if workspace_allows is None:
        return session_value
    if not workspace_allows:
        return False  # workspace denies → hard block
    return session_value


def get_effective_permissions(
    session: SessionPermissions,
    workspace: WorkspaceCapabilities,
) -> Dict[str, Any]:
    """
    Merge the session's permission profile with the workspace's capabilities.

    Returns a flat dict with keys matching the five permission categories::

        {"filesystem": ..., "network": ..., "container": ..., "git": ..., "system": ...}

    Each value is either a boolean (``True`` / ``False``) for hard allow/deny,
    a string level (``"write"``, ``"read"``, ``"none"``, ``"banned"``, ``"ask"``),
    or ``False`` if the workspace forbids the operation.
    """
    # ── Filesystem ──────────────────────────────────────────────────────
    # Workspace filesystem_write caps write access; if False, downgrade to read.
    fs = session.filesystem
    if not workspace.filesystem_write and fs == "write":
        fs = "read"  # downgrade write → read
    # (read / none / banned / ask pass through unchanged)

    # ── Network ─────────────────────────────────────────────────────────
    net: Any = _min_permission(session.network, workspace.allow_network)

    # ── Container ───────────────────────────────────────────────────────
    container: Any = session.container and workspace.allow_docker

    # ── Git ─────────────────────────────────────────────────────────────
    git: Any = _min_permission(session.git, workspace.git_available)

    # ── System ──────────────────────────────────────────────────────────
    system: Any = session.system  # no workspace cap yet

    return {
        "filesystem": fs,
        "network": net,
        "container": container,
        "git": git,
        "system": system,
    }


# ══════════════════════════════════════════════════════════════════════════
#  Internal helpers
# ══════════════════════════════════════════════════════════════════════════


def _value_satisfies(required: str, allowed: object) -> bool | str:
    """
    Check whether a single required value is satisfied by the allowed setting.
    (Mirrors the logic in ``tool_executor._value_satisfies``.)

    Returns:
        * ``True`` if permission is granted.
        * ``False`` if permission is denied.
        * ``"ASK"`` if the allowed value is ``'ask'`` and the required access
          is above the read level.
    """
    sentinel_ask = "ASK"
    required_lower = str(required).lower()

    # --- Determine required level ---
    if required_lower in ("false", "no", "banned"):
        required_level = 0
    elif required_lower in ("true", "yes", "outbound"):
        required_level = 3  # write-level
    else:
        aliases = {"deny": "banned", "denied": "banned", "all": "full"}
        level_name = aliases.get(required_lower, required_lower)
        level_map = {"banned": 0, "ask": 1, "read": 2, "write": 3, "full": 4}
        required_level = level_map.get(level_name)

    # --- Handle allowed value ---
    if isinstance(allowed, bool):
        # True satisfies everything; False satisfies nothing
        return allowed is True

    allowed_str = str(allowed).lower()

    # --- Handle 'ask' ---
    if allowed_str == "ask":
        if required_level is None:
            return sentinel_ask
        if required_level <= 2:  # banned/read
            return True
        return sentinel_ask

    # --- String comparison ---
    aliases = {"deny": "banned", "denied": "banned", "all": "full"}
    level_map = {"banned": 0, "ask": 1, "read": 2, "write": 3, "full": 4}
    allowed_level_name = aliases.get(allowed_str, allowed_str)
    allowed_level = level_map.get(allowed_level_name)

    if required_level is None or allowed_level is None:
        # Fall back to exact match
        return allowed_str == required_lower

    return allowed_level >= required_level


# ══════════════════════════════════════════════════════════════════════════
#  Main gate
# ══════════════════════════════════════════════════════════════════════════


def check_required_categories(
    required: List[str],
    effective: Dict[str, Any],
    tool_name: str,
    tool_args: Dict[str, Any],
    description: str,
    event_bus: Any,
    agent_id: str = "0",
    session_id: str = "",
) -> Tuple[bool, str]:
    """
    Check a tool's required categories against the effective permission dict.

    Args:
        required:
            List of ``"category:value"`` strings (e.g. ``["filesystem:write"]``).
        effective:
            Output of ``get_effective_permissions()``.
        tool_name:
            Name of the tool being checked (for prompt context).
        tool_args:
            The tool call arguments (for prompt context).
        description:
            Human-readable description of what the tool is about to do
            (obtained from ``tool_class.describe_action()``).
        event_bus:
            An ``EventBus`` instance used to publish ``SecurityPromptEvent``.
            Pass ``agent.events.global_event_bus`` in production.
        agent_id:
            Agent identifier string (for prompt context).
        session_id:
            Session identifier (for prompt context).

    Returns:
        ``(True, "")`` if all checks pass.
        ``(False, error_message)`` if any check fails or the user denies.
    """
    PROMPT_TIMEOUT = 120.0

    ask_categories: List[str] = []
    prompts_needed = False

    for req in required:
        if ":" not in req:
            continue  # malformed, skip

        category, required_value = req.split(":", 1)
        allowed = effective.get(category)

        if allowed is None:
            return False, f"Permission denied: Unknown category '{category}' required by tool."

        result = _value_satisfies(required_value, allowed)

        if result is False:
            return False, (
                f"Permission denied: Tool requires {category}:{required_value}, "
                f"but session allows {category}:{allowed}"
            )

        if result == "ASK":
            prompts_needed = True
            ask_categories.append(req)

    if not prompts_needed:
        return True, ""

    # ── Prompt the user for approval ────────────────────────────────────
    request_id = str(uuid.uuid4())
    response_queue: queue.Queue = queue.Queue()
    if _pending_requests_lock is not None:
        with _pending_requests_lock:
            _pending_security_requests[request_id] = response_queue

    # Publish SecurityPromptEvent
    event = SecurityPromptEvent(
        data={
            "request_id": request_id,
            "agent_id": agent_id,
            "tool_name": tool_name,
            "capabilities": ask_categories,
            "arguments": tool_args,
            "session_id": session_id,
            "description": description,
        }
    )
    if event_bus is not None:
        event_bus.publish(event)

    # Wait for response
    try:
        response = response_queue.get(timeout=PROMPT_TIMEOUT)
        approved = response.get("approved", False)
        if approved:
            return True, ""
        else:
            reason = response.get("reason", "User denied the request")
            return False, (
                f"Permission denied: {', '.join(ask_categories)} required by "
                f"'{tool_name}' — {reason}"
            )
    except queue.Empty:
        return False, (
            f"Permission denied: {', '.join(ask_categories)} required by "
            f"'{tool_name}' — security prompt timed out."
        )
    finally:
        if _pending_requests_lock is not None:
            with _pending_requests_lock:
                _pending_security_requests.pop(request_id, None)


def resolve_prompt(request_id: str, approved: bool, remember: bool = False) -> bool:
    """
    Resolve a pending security prompt.

    Delegates to ``resolve_security_prompt`` from ``thoughtmachine.security``
    which uses the shared ``_pending_security_requests`` registry.

    Returns ``True`` if the prompt was found and resolved, ``False`` otherwise.
    """
    # Check if the request exists before delegating
    found = False
    if _pending_requests_lock is not None:
        with _pending_requests_lock:
            found = request_id in _pending_security_requests
    else:
        found = request_id in _pending_security_requests

    if found:
        resolve_security_prompt(request_id, approved, remember)
    return found
