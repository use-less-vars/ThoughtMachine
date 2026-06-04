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
    _value_satisfies,
    _check_permissions,
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


# ---------------------------------------------------------------------------
# Test: _check_permissions with 'ask' value (no event system — timed out)
# ---------------------------------------------------------------------------

class TestCheckPermissionsAsk:
    """Tests for _check_permissions when session value is 'ask'."""

    PROFILE_GIT_ASK = {
        "container": False,
        "network": False,
        "filesystem": "read",
        "security": "read",
        "git": "ask",
        "execution": "banned",
    }

    def test_git_write_with_ask_profile_passes_when_approved(self):
        """
        When session git='ask', _check_permissions with git:write
        should pass if resolve_security_prompt is called with approved=True.
        """
        result_container = []

        def run_check():
            result = _check_permissions(
                ["git:write"],
                self.PROFILE_GIT_ASK,
                tool_name="GitWriteTool",
                agent_id=42,
            )
            result_container.append(result)

        # Start _check_permissions in a background thread
        t = threading.Thread(target=run_check, daemon=True)
        t.start()

        # Wait a bit for the queue to be registered
        import time
        time.sleep(0.1)

        # Find the pending request and approve it
        with _pending_requests_lock:
            request_ids = list(_pending_security_requests.keys())

        # There should be at least one pending request
        assert len(request_ids) > 0, (
            "No pending security requests found — "
            "the ask flow may not have registered one"
        )

        request_id = request_ids[0]
        resolve_security_prompt(request_id, approved=True)

        t.join(timeout=5)
        assert t.is_alive() is False, "Thread did not finish in time"

        assert len(result_container) == 1
        assert result_container[0] is None  # None = all checks passed

    def test_git_write_with_ask_profile_denied(self):
        """
        When session git='ask', _check_permissions with git:write
        should fail if resolve_security_prompt is called with approved=False.
        """
        result_container = []

        def run_check():
            result = _check_permissions(
                ["git:write"],
                self.PROFILE_GIT_ASK,
                tool_name="GitWriteTool",
                agent_id=42,
            )
            result_container.append(result)

        t = threading.Thread(target=run_check, daemon=True)
        t.start()

        import time
        time.sleep(0.1)

        with _pending_requests_lock:
            request_ids = list(_pending_security_requests.keys())

        assert len(request_ids) > 0
        request_id = request_ids[0]
        resolve_security_prompt(request_id, approved=False)

        t.join(timeout=5)

        assert len(result_container) == 1
        assert result_container[0] is not None
        assert "Permission denied" in result_container[0]
        assert "user denied" in result_container[0].lower()

    def test_git_read_with_ask_profile_also_asks(self):
        """
        Even 'read' level requires user approval when session is 'ask'.
        """
        result_container = []

        def run_check():
            result = _check_permissions(
                ["git:read"],
                self.PROFILE_GIT_ASK,
                tool_name="GitReadTool",
                agent_id=42,
            )
            result_container.append(result)

        t = threading.Thread(target=run_check, daemon=True)
        t.start()

        import time
        time.sleep(0.1)

        with _pending_requests_lock:
            request_ids = list(_pending_security_requests.keys())

        assert len(request_ids) > 0
        request_id = request_ids[0]
        resolve_security_prompt(request_id, approved=True)

        t.join(timeout=5)

        assert len(result_container) == 1
        assert result_container[0] is None  # approved

    def test_git_ask_does_not_prompt_for_banned(self):
        """
        When session git='ask' and required is 'banned', no prompt needed.
        """
        result = _check_permissions(
            ["git:banned"],
            self.PROFILE_GIT_ASK,
            tool_name="GitWriteTool",
            agent_id=42,
        )
        assert result is None

    def test_other_categories_still_work_with_ask_profile(self):
        """
        Non-git categories in an 'ask' profile should still use normal comparison.
        """
        profile = dict(self.PROFILE_GIT_ASK)
        # git is ask, but container is False
        result = _check_permissions(
            ["container:true"],
            profile,
            tool_name="ContainerTool",
            agent_id=42,
        )
        assert result is not None
        assert "container:true" in result


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

    def test_git_read_tool_with_git_ask(self):
        """
        Even git:read goes through the ask flow when session has git='ask'.
        """
        perms = SessionPermissions(git="ask")
        executor = self._make_executor([GitReadTool], permissions=perms)

        result_container = []

        def run_executor():
            r = executor._execute_single_tool(
                GitReadTool, {}, "GitReadTool", 0,
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
        resolve_security_prompt(request_id, approved=True)

        t.join(timeout=5)

        assert len(result_container) == 1
        assert result_container[0]["result"] == "Git read OK"

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
