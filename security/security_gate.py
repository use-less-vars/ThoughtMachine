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

This module is always active — there is no fallback path.
"""

from __future__ import annotations

import logging
import queue
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from thoughtmachine.workspace_capabilities import (
    WorkspaceCapabilities,
    load_workspace_capabilities,
)
from thoughtmachine.security import SessionPermissions, PERMISSION_SCHEMA, _pending_security_requests, _pending_requests_lock, resolve_security_prompt
from agent.events import SecurityPromptEvent, EventType, NullEventBus

logger = logging.getLogger(__name__)

# ── Pending-prompt registry ──────────────────────────────────────────────

# Uses shared _pending_security_requests from thoughtmachine.security




# ══════════════════════════════════════════════════════════════════════════
#  Loader
# ══════════════════════════════════════════════════════════════════════════


# ── Fail-closed capabilities constant ──
# When a workspace has no capabilities file (or it cannot be parsed), the
# loader returns None. Instead of falling back to a fully-permissive
# default, we return RESTRICTIVE capabilities: filesystem read-only, no
# docker, no network, no git. Session permissions can still be more
# restrictive; this constant only lowers the ceiling, never raises it.
_FAIL_CLOSED_CAPABILITIES = WorkspaceCapabilities(
    filesystem_write=False,
    allow_docker=False,
    allow_network=False,
    git_available=False,
)


def get_workspace_capabilities(workspace_id: str) -> WorkspaceCapabilities:
    """
    Load workspace capabilities via the canonical loader.

    Returns a fail-closed (restrictive) ``WorkspaceCapabilities`` when the
    file does not exist or cannot be parsed.
    """
    caps = load_workspace_capabilities(workspace_id)
    if caps is None:
        return _FAIL_CLOSED_CAPABILITIES
    return caps


# ══════════════════════════════════════════════════════════════════════════
#  Merge (session × workspace)
# ══════════════════════════════════════════════════════════════════════════


def _min_permission(
    effective_val: Any,
    override_val: Any,
) -> Any:
    """
    Combine two permission values, returning the more restrictive one.

    Used for two purposes:

    1. **Session × workspace** (existing callers):
       *override_val* is ``Optional[bool]``.
       * ``None`` → *effective_val* unchanged.
       * ``False`` → hard deny (``False``).
       * ``True`` → *effective_val* unchanged.

    2. **Effective × worker** (worker_permissions):
       *override_val* is a string level (``"banned"``, ``"read"``,
       ``"write"``, ``"full"``).  Compared by permission level;
       the lower (more restrictive) level wins.
    """
    if override_val is None:
        return effective_val
    if isinstance(override_val, bool):
        if not override_val:
            return False  # hard deny
        return effective_val  # True → passthrough

    # override_val is a string — compare permission levels
    _LEVEL_MAP: dict[str, float] = {
        "banned": 0.0, "ask": 1.0, "none": 1.0,
        "read": 2.0, "outbound": 2.5, "write": 3.0, "full": 4.0,
    }

    def _level(v: Any) -> float:
        if isinstance(v, bool):
            return 3.0 if v else 0.0
        if v is None:
            return 4.0
        return _LEVEL_MAP.get(str(v).lower(), 2.0)

    return override_val if _level(override_val) < _level(effective_val) else effective_val


def get_effective_permissions(
    session: SessionPermissions,
    workspace: WorkspaceCapabilities,
) -> Dict[str, Any]:
    """
    Merge the session's permission profile with the workspace's capabilities.

    Returns a flat dict with keys matching the seven permission categories::

        {"filesystem": ..., "network": ..., "container": ..., "git": ..., "system": ..., "mcp": ..., "execution": ...}

    Each value is either a boolean (``True`` / ``False``) for hard allow/deny,
    a string level (``"write"``, ``"read"``, ``"none"``, ``"banned"``, ``"ask"``, ``"outbound"``),
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
        "mcp": session.mcp,
        "execution": session.execution,
    }


# ══════════════════════════════════════════════════════════════════════════
#  Container config resolver
# ══════════════════════════════════════════════════════════════════════════


def get_expected_container_config(
    session_permissions: Dict[str, Any],
    workspace_caps: Optional[WorkspaceCapabilities] = None,
) -> Dict[str, Any]:
    """
    Compute the expected Docker container config from session permissions.

    Uses ``get_effective_permissions()`` to merge session permissions with
    workspace capabilities, then translates the merged result into container
    configuration values that match what ``DockerExecutor._compute_container_config()``
    would produce.

    This is the canonical reference for container config — all callers
    (``_compute_container_config``, ``_compute_desired_config``,
    ``verify_container_integrity``) derive their config through the same logic.

    Args:
        session_permissions:
            Dict with keys like ``"network"``, ``"filesystem"``, ``"container"``
            (the same dict that ``SessionPermissions`` accepts).
        workspace_caps:
            ``WorkspaceCapabilities`` instance. When ``None``, a fully-permissive
            default is used (all capabilities ``True``).

    Returns:
        Dict with keys:

        - **network_mode** (``"bridge"`` or ``"none"``):
          ``"bridge"`` when effective network is ``True`` or ``"write"``.
        - **workspace_mode** (``"rw"`` or ``"ro"``):
          ``"rw"`` when effective filesystem is ``"write"`` or ``"full"``.
        - **effective** (dict):
          The full effective permissions dict from ``get_effective_permissions()``.
    """
    from thoughtmachine.security import SessionPermissions

    if workspace_caps is None:
        workspace_caps = WorkspaceCapabilities()

    # Attempt to construct SessionPermissions; fall back to safe defaults if
    # the dict contains values that Pydantic rejects (e.g. unknown literals).
    # This mirrors the try/except in _compute_container_config and
    # _compute_desired_config.
    try:
        session = SessionPermissions(**session_permissions)
        eff = get_effective_permissions(session, workspace_caps)
    except Exception:
        # Validation or merge failure → safe defaults
        return {
            "network_mode": "none",
            "workspace_mode": "ro",
            "effective": {},
        }

    # Network mode
    net = eff.get("network")
    network_mode = "bridge" if (net is True or net == "write") else "none"

    # Workspace mount mode
    fs = eff.get("filesystem", "read")
    workspace_mode = "rw" if fs in ("write", "full") else "ro"

    return {
        "network_mode": network_mode,
        "workspace_mode": workspace_mode,
        "effective": eff,
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
        level_map = {"banned": 0, "ask": 1, "read": 2, "connect": 3, "write": 3, "full": 4}
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
    level_map = {"banned": 0, "ask": 1, "read": 2, "connect": 3, "write": 3, "full": 4}
    allowed_level_name = aliases.get(allowed_str, allowed_str)
    allowed_level = level_map.get(allowed_level_name)

    if required_level is None or allowed_level is None:
        # Fall back to exact match
        return allowed_str == required_lower

    return allowed_level >= required_level


def check_atomic_operation(
    operation_required: str,
    effective_permissions: dict,
    tool_name: str,
    description: str = "",
) -> bool:
    """
    Synchronous in-tool re-check of an operation.
    Returns True if allowed, False if denied.
    If the permission level is 'ask', treats it as DENIED (escalation not pre-approved).
    """
    parts = operation_required.split(":", 1)
    if len(parts) != 2:
        logger.warning(
            "check_atomic_operation: malformed required '%s' for %s",
            operation_required, tool_name
        )
        return False

    category, required_value = parts
    allowed = effective_permissions.get(category)
    if allowed is None:
        logger.warning(
            "check_atomic_operation: unknown category '%s' for %s",
            category, tool_name
        )
        return False

    result = _value_satisfies(required_value, allowed)
    if result == "ASK":
        logger.warning(
            "check_atomic_operation: '%s' for %s would require user prompt (ASK) — denying by policy",
            operation_required, tool_name
        )
        return False
    return bool(result)


# ══════════════════════════════════════════════════════════════════════════
#  Main gate
# ══════════════════════════════════════════════════════════════════════════


def check_required_categories(
    required: List[str],
    effective: Dict[str, Any],
    tool_name: str,
    tool_args: Dict[str, Any],
    description: str,
    event_bus: Any = None,
    agent_id: str = "0",
    session_id: str = "",
    permission_footprint: Optional[Dict[str, Any]] = None,
    is_worker_context: bool = False,
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
        permission_footprint:
            Optional dict of worker-level permission overrides using the
            same string hierarchy as session/workspace permissions
            (e.g. ``{"network": "read", "filesystem": "banned"}``).
            Each value is a string level (``"banned"``, ``"read"``,
            ``"write"``, ``"full"``).  Applied via ``_min_permission``
            which returns the more restrictive of the effective and
            worker value.  If a key exists in *permission_footprint* but
            not in *effective* — or its value is not a known level for
            that category — the category resolves to a hard deny
            (fail-closed): a worker never gains access to a category the
            session does not explicitly expose.
        is_worker_context:
            If True, the call originates from a worker where no interactive
            user is available — deny immediately without prompting.

    Returns:
        ``(True, "")`` if all checks pass.
        ``(False, error_message)`` if any check fails or the user denies.
    """
    PROMPT_TIMEOUT = 120.0

    # ── Apply worker-level restrictions ─────────────────────────────────
    if permission_footprint is not None:
        for category, worker_val in permission_footprint.items():
            valid_levels = PERMISSION_SCHEMA.get(category)
            if valid_levels is None or worker_val not in valid_levels:
                # Unknown category or unknown level — fail closed: deny.
                effective[category] = False
            elif category in effective:
                effective[category] = _min_permission(
                    effective[category], worker_val
                )
            else:
                # Category absent from the session's effective dict: the
                # worker may NOT grant itself a category the session does
                # not expose — resolve to a hard deny (fail-closed).
                effective[category] = False

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

    # ── Worker context (no interactive user): deny immediately without blocking ──
    if event_bus is None or is_worker_context or isinstance(event_bus, NullEventBus):
        return False, (
            f"Permission denied: {', '.join(ask_categories)} required by "
            f"'{tool_name}' — no interactive user available for worker prompt approval."
        )

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
