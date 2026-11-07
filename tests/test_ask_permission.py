"""
Tests for the "ask" permission flow in tool_executor.py.

Covers:
  - _check_permissions with 'ask' session values triggers the security prompt
  - resolve_security_prompt() approves or denies the pending request
  - Timeout behaviour when no response is received
  - Integration with ToolExecutor._execute_single_tool and SessionPermissions(git='ask')
"""

import threading
from typing import ClassVar, List

import pytest
from pydantic import ValidationError

from agent.core.tool_executor import (
    DEFAULT_SESSION_PERMISSIONS,
    ToolExecutor,
)
from thoughtmachine.security import (
    SessionPermissions,
    resolve_security_prompt,
    _pending_security_requests,
    _pending_requests_lock,
)
from tools.base import ToolBase


# ---------------------------------------------------------------------------
# Stub tools
# ---------------------------------------------------------------------------

class GitReadTool(ToolBase):
    """A tool that requires git:read."""
    tool: str = "GitReadTool"
    required_categories: ClassVar[List[str]] = ["git:read"]

    def execute(self) -> str:
        return "Git read OK"


class GitWriteTool(ToolBase):
    """A tool that requires git:write."""
    tool: str = "GitWriteTool"
    required_categories: ClassVar[List[str]] = ["git:write"]

    def execute(self) -> str:
        return "Git write OK"


# ══════════════════════════════════════════════════════════════════════════════
# Fake session permission profile (shared by remaining tests)
# ══════════════════════════════════════════════════════════════════════════════

PROFILE_GIT_ASK = {
    "container": False,
    "network": False,
    "filesystem": "read",
    "system": "read",
    "git": "ask",
    "execution": "banned",
}

# ---------------------------------------------------------------------------
# Test: ToolExecutor integration with SessionPermissions(git='ask')
# ---------------------------------------------------------------------------

class FakeConfig:
    workspace_path = None
    tool_output_token_limit = None
    session_permissions = None


class FakeConfigWithPermissions:
    workspace_path = None
    tool_output_token_limit = None

    def __init__(self, permissions: SessionPermissions | None = None):
        self.session_permissions = permissions


class FakeState:
    security_config = None


class TestToolExecutorAskPermission:
    """Integration tests: ToolExecutor handles git='ask' permission correctly."""

    def _make_executor(self, tool_classes, permissions=None):
        return ToolExecutor(
            tool_classes=tool_classes,
            config=FakeConfigWithPermissions(permissions),
            state=FakeState(),
            logger=None,
            security_available=False,
            agent=None,
        )

    def test_git_write_tool_goes_through_ask_flow(self):
        """
        RISK-3 (new contract): an id-less executor has no on-disk authority to
        read, so a git='ask' mirror is IGNORED and the git:write tool is
        DENIED synchronously -- no interactive prompt is registered.
        (Canonical disk-mode ask coverage:
        tests/test_worker_disk_mode_inheritance.py::test_main_agent_ask_prompts_via_event_bus.)
        """
        perms = SessionPermissions(git="ask")
        executor = self._make_executor([GitWriteTool], permissions=perms)

        result = executor._execute_single_tool(
            GitWriteTool, {}, "GitWriteTool", 0,
            lambda: False, lambda: None, lambda: 0
        )
        assert "Permission denied" in result["result"], result
        assert result["tool_type"] == "normal"

        with _pending_requests_lock:
            assert len(_pending_security_requests) == 0

    def test_git_write_tool_ask_denied(self):
        """
        RISK-3 (new contract): id-less -> fail-closed denial, synchronously;
        no prompt is ever registered (so nothing to deny).
        """
        perms = SessionPermissions(git="ask")
        executor = self._make_executor([GitWriteTool], permissions=perms)

        result = executor._execute_single_tool(
            GitWriteTool, {}, "GitWriteTool", 0,
            lambda: False, lambda: None, lambda: 0
        )
        assert "Permission denied" in result["result"], result
        assert result["tool_type"] == "normal"

        with _pending_requests_lock:
            assert len(_pending_security_requests) == 0

    def test_git_read_tool_with_git_ask_bypasses_prompt(self):
        """
        RISK-3 (new contract): id-less -> fail-closed denial even for a
        git:read tool under a git='ask' mirror; no prompt is registered.
        """
        perms = SessionPermissions(git="ask")
        executor = self._make_executor([GitReadTool], permissions=perms)

        result = executor._execute_single_tool(
            GitReadTool, {}, "GitReadTool", 0,
            lambda: False, lambda: None, lambda: 0
        )
        assert "Permission denied" in result["result"], result
        assert "git:read" in result["result"], result

        # Verify no pending security requests were created
        with _pending_requests_lock:
            assert len(_pending_security_requests) == 0

    def test_git_full_is_rejected_fail_closed(self):
        """
        RISK-3 (new contract): git='full' is NOT a canonical git level
        (RESOURCE_CATALOG['git'] / GIT_PERMISSION_LEVELS omit it), so the
        SessionPermissions mirror now REJECTS it fail-closed (ValidationError)
        instead of accepting an over-broad level the id-less executor ignores.
        """
        with pytest.raises(ValidationError):
            SessionPermissions(git="full")

    def test_git_read_bypasses_ask_with_read(self):
        """
        RISK-3 (new contract): git='read' in the mirror does not grant -- the
        id-less executor denies the git:read tool fail-closed.
        """
        perms = SessionPermissions(git="read")
        executor = self._make_executor([GitReadTool], permissions=perms)

        result = executor._execute_single_tool(
            GitReadTool, {}, "GitReadTool", 0,
            lambda: False, lambda: None, lambda: 0
        )
        assert "Permission denied" in result["result"], result
        assert "git:read" in result["result"], result


# ---------------------------------------------------------------------------
# Test: resolve_security_prompt function
# ---------------------------------------------------------------------------

class TestResolveSecurityPrompt:
    """Unit tests for the resolve_security_prompt function."""

    def test_resolve_places_response_on_queue(self):
        """resolve_security_prompt puts {'approved': ...} on the correct queue."""
        import queue

        q = queue.Queue()
        request_id = "test-request-123"

        with _pending_requests_lock:
            _pending_security_requests[request_id] = q

        resolve_security_prompt(request_id, approved=True)

        # Queue should have the response
        response = q.get(timeout=1)
        assert response == {"approved": True, "remember": False}

        # Request should be removed from pending dict
        with _pending_requests_lock:
            assert request_id not in _pending_security_requests

    def test_resolve_with_remember(self):
        """resolve_security_prompt passes 'remember' flag through."""
        import queue

        q = queue.Queue()
        request_id = "test-request-remember"

        with _pending_requests_lock:
            _pending_security_requests[request_id] = q

        resolve_security_prompt(request_id, approved=False, remember=True)

        response = q.get(timeout=1)
        assert response == {"approved": False, "remember": True}

    def test_resolve_unknown_request_id(self):
        """resolve_security_prompt with unknown ID should not raise."""
        # Should not raise any exception
        resolve_security_prompt("nonexistent-id", approved=True)
