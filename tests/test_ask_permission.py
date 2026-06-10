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
        When SessionPermissions has git='ask', a tool requiring git:write
        triggers the security prompt flow and can be approved.
        """
        perms = SessionPermissions(git="ask")
        executor = self._make_executor([GitWriteTool], permissions=perms)

        result_container = []

        def run_executor():
            r = executor._execute_single_tool(
                GitWriteTool, {}, "GitWriteTool", 0,
                lambda: False, lambda: None, lambda: 0
            )
            result_container.append(r)

        t = threading.Thread(target=run_executor, daemon=True)
        t.start()

        import time
        time.sleep(0.2)

        with _pending_requests_lock:
            request_ids = list(_pending_security_requests.keys())

        assert len(request_ids) > 0, (
            "No pending security requests — the executor did not trigger the ask flow"
        )

        request_id = request_ids[0]
        resolve_security_prompt(request_id, approved=True)

        t.join(timeout=5)

        assert len(result_container) == 1
        assert result_container[0]["result"] == "Git write OK"
        assert result_container[0]["tool_type"] == "normal"

    def test_git_write_tool_ask_denied(self):
        """
        When SessionPermissions has git='ask', the tool execution is denied
        if the user denies the prompt.
        """
        perms = SessionPermissions(git="ask")
        executor = self._make_executor([GitWriteTool], permissions=perms)

        result_container = []

        def run_executor():
            r = executor._execute_single_tool(
                GitWriteTool, {}, "GitWriteTool", 0,
                lambda: False, lambda: None, lambda: 0
            )
            result_container.append(r)

        t = threading.Thread(target=run_executor, daemon=True)
        t.start()

        import time
        time.sleep(0.2)

        with _pending_requests_lock:
            request_ids = list(_pending_security_requests.keys())

        assert len(request_ids) > 0
        request_id = request_ids[0]
        resolve_security_prompt(request_id, approved=False)

        t.join(timeout=5)

        assert len(result_container) == 1
        assert "Permission denied" in result_container[0]["result"]
        assert result_container[0]["tool_type"] == "normal"

    def test_git_read_tool_with_git_ask_bypasses_prompt(self):
        """
        When session has git='ask', a tool requiring git:read executes
        directly without triggering the security prompt (read is a no-op).
        """
        perms = SessionPermissions(git="ask")
        executor = self._make_executor([GitReadTool], permissions=perms)

        # Should execute immediately — no background thread needed
        result = executor._execute_single_tool(
            GitReadTool, {}, "GitReadTool", 0,
            lambda: False, lambda: None, lambda: 0
        )
        assert result["result"] == "Git read OK"

        # Verify no pending security requests were created
        with _pending_requests_lock:
            assert len(list(_pending_security_requests.keys())) == 0

    def test_git_full_bypasses_ask(self):
        """
        When session has git='full' (not 'ask'), no prompt is triggered.
        """
        perms = SessionPermissions(git="full")
        executor = self._make_executor([GitWriteTool], permissions=perms)

        result = executor._execute_single_tool(
            GitWriteTool, {}, "GitWriteTool", 0,
            lambda: False, lambda: None, lambda: 0
        )
        assert result["result"] == "Git write OK"

    def test_git_read_bypasses_ask_with_read(self):
        """
        When session has git='read', no prompt is triggered for read.
        """
        perms = SessionPermissions(git="read")
        executor = self._make_executor([GitReadTool], permissions=perms)

        result = executor._execute_single_tool(
            GitReadTool, {}, "GitReadTool", 0,
            lambda: False, lambda: None, lambda: 0
        )
        assert result["result"] == "Git read OK"


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
