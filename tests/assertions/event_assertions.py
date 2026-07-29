"""Event assertion helpers for agent loop testing."""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def assert_event_sequence(
    events: List[Dict[str, Any]],
    expected_types: List[str],
) -> None:
    """Assert that expected_types appear as events in order (subsequence match)."""
    remaining = list(expected_types)
    for event in events:
        if remaining and event.get("type") == remaining[0]:
            remaining.pop(0)
    if remaining:
        raise AssertionError(
            f"Expected event types {expected_types} in order, but missing: {remaining}\n"
            f"Actual event types: {[e.get('type') for e in events]}"
        )


def assert_tool_call(
    events: List[Dict[str, Any]],
    tool_name: str,
    params: Optional[Dict[str, Any]] = None,
) -> None:
    """Assert at least one tool_call event for the given tool."""
    for event in events:
        if event.get("type") == "tool_call" and event.get("tool_name") == tool_name:
            if params is not None:
                args = event.get("arguments", {})
                for key, value in params.items():
                    if args.get(key) != value:
                        break
                else:
                    return
            else:
                return
    raise AssertionError(
        f"No tool_call event found for tool '{tool_name}'"
        + (f" with params {params}" if params else "")
    )


def assert_respond(
    events: List[Dict[str, Any]],
    expected_content: Optional[str] = None,
    expected_status: Optional[str] = None,
) -> None:
    """Assert at least one agent_responded event with matching content/status."""
    for event in events:
        if event.get("type") != "agent_responded":
            continue
        if expected_content is not None:
            content = event.get("content", "") or ""
            if expected_content not in content:
                continue
        if expected_status is not None:
            if event.get("status") != expected_status:
                continue
        return
    raise AssertionError(
        "No matching agent_responded event found"
        + (f" with content containing '{expected_content}'" if expected_content else "")
        + (f" with status '{expected_status}'" if expected_status else "")
    )
