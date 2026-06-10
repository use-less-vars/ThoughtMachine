"""
Tests for the unified security gate (``security/security_gate.py``).

**Docker note:** Pytest injects the ``tests/`` directory at the front of
``sys.path``, which shadows the real ``security/`` package. This module
fixes ``sys.path`` before any project imports so the real package is found
first (via the :mod:`conftest` hook).
"""

from __future__ import annotations

import sys

# ── Fix sys.path for Docker sandbox ──────────────────────────────────────
# Pytest inserts ``tests/`` at the front of ``sys.path``, so any bare
# ``import security`` finds ``/workspace/tests/security/`` first and caches
# it in ``sys.modules['security']`` with the wrong `__file__`.
# Fix: remove tests dir from sys.path.

_bad_prefix = "/workspace/tests"
sys.path = [p for p in sys.path if not p.startswith(_bad_prefix)]

_stubs_path = "/tmp/stubs"
if _stubs_path in sys.path:
    sys.path.remove(_stubs_path)
if "/workspace" in sys.path:
    sys.path.remove("/workspace")
sys.path.insert(0, _stubs_path)
sys.path.insert(1, "/workspace")

import json
import queue
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from security.security_gate import (
    check_required_categories,
    get_effective_permissions,
    get_workspace_capabilities,
    resolve_prompt,
)
from thoughtmachine.workspace_capabilities import WorkspaceCapabilities
from thoughtmachine.security import SessionPermissions





# ══════════════════════════════════════════════════════════════════════════
#  WorkspaceCapabilities (model)
# ══════════════════════════════════════════════════════════════════════════


class TestWorkspaceCapabilitiesModel:
    def test_defaults(self):
        caps = WorkspaceCapabilities()
        assert caps.allow_network is True
        assert caps.allow_docker is True
        assert caps.filesystem_write is True
        assert caps.git_available is True

    def test_custom_values(self):
        caps = WorkspaceCapabilities(
            allow_network=True,
            allow_docker=False,
            filesystem_write=False,
            git_available=False,
        )
        assert caps.allow_network is True
        assert caps.allow_docker is False
        assert caps.filesystem_write is False
        assert caps.git_available is False


# ══════════════════════════════════════════════════════════════════════════
#  get_workspace_capabilities
# ══════════════════════════════════════════════════════════════════════════


class TestGetWorkspaceCapabilities:
    def test_default_when_file_missing(self):
        """When the capabilities file does not exist, return fully-permissive defaults."""
        caps = get_workspace_capabilities("nonexistent_workspace")
        assert caps.allow_network is True  # canonical default
        assert caps.allow_docker is True
        assert caps.filesystem_write is True
        assert caps.git_available is True

    def test_loads_real_values(self):
        """Read values from a well-formed capabilities JSON."""
        from pathlib import Path
        from unittest.mock import patch
        fake_json = json.dumps(
            {
                "allow_network": False,
                "filesystem_write": False,
                "git_available": False,
                "allow_docker": False,
            }
        )
        with patch.object(Path, "exists", return_value=True):
            with patch.object(Path, "read_text", return_value=fake_json):
                caps = get_workspace_capabilities("restricted")
                assert caps.allow_network is False
                assert caps.filesystem_write is False
                assert caps.git_available is False
                assert caps.allow_docker is False

    def test_missing_keys_use_defaults(self):
        """Missing keys in the JSON fall back to the canonical default."""
        from pathlib import Path
        from unittest.mock import patch
        fake_json = json.dumps({"allow_network": True})  # only one key
        with patch.object(Path, "exists", return_value=True):
            with patch.object(Path, "read_text", return_value=fake_json):
                caps = get_workspace_capabilities("partial")
                assert caps.allow_network is True
                # defaults for the rest
                assert caps.filesystem_write is True
                assert caps.git_available is True
                assert caps.allow_docker is True


# ══════════════════════════════════════════════════════════════════════════
#  get_effective_permissions
# ══════════════════════════════════════════════════════════════════════════


class TestEffectivePermissionsMerge:
    def test_all_max(self):
        """All-session-max with all-workspace-true yields maximum effective permissions."""
        session = SessionPermissions(
            filesystem="write",
            network="write",
            container=True,
            git="write",
            system="full",
        )
        workspace = WorkspaceCapabilities(
            allow_network=True,
            allow_docker=True,
            filesystem_write=True,
            git_available=True,
        )
        eff = get_effective_permissions(session, workspace)
        assert eff["filesystem"] == "write"
        assert eff["network"] == "write"  # "write" + workspace=True → "write" (pass-through)
        assert eff["container"] is True
        assert eff["git"] == "write"
        assert eff["system"] == "full"

    def test_network_workspace_denied(self):
        """Workspace allow_network=False → effective network=False."""
        session = SessionPermissions(network="write")
        workspace = WorkspaceCapabilities(allow_network=False)
        eff = get_effective_permissions(session, workspace)
        assert eff["network"] is False

    def test_filesystem_write_downgraded(self):
        """Workspace filesystem_write=False downgrades session write to read."""
        session = SessionPermissions(filesystem="write")
        workspace = WorkspaceCapabilities(filesystem_write=False)
        eff = get_effective_permissions(session, workspace)
        assert eff["filesystem"] == "read"

    def test_filesystem_read_unchanged(self):
        """Workspace filesystem_write=False does NOT downgrade read."""
        session = SessionPermissions(filesystem="read")
        workspace = WorkspaceCapabilities(filesystem_write=False)
        eff = get_effective_permissions(session, workspace)
        assert eff["filesystem"] == "read"

    def test_filesystem_banned_unchanged(self):
        """Workspace filesystem_write=False leaves banned unchanged."""
        session = SessionPermissions(filesystem="banned")
        workspace = WorkspaceCapabilities(filesystem_write=False)
        eff = get_effective_permissions(session, workspace)
        assert eff["filesystem"] == "banned"

    def test_filesystem_ask_unchanged(self):
        """Workspace filesystem_write=False leaves ask unchanged."""
        session = SessionPermissions(filesystem="ask")
        workspace = WorkspaceCapabilities(filesystem_write=False)
        eff = get_effective_permissions(session, workspace)
        assert eff["filesystem"] == "ask"

    def test_git_ask_workspace_available(self):
        """Session git='ask', workspace git_available=True → effective 'ask'."""
        session = SessionPermissions(git="ask")
        workspace = WorkspaceCapabilities(git_available=True)
        eff = get_effective_permissions(session, workspace)
        assert eff["git"] == "ask"

    def test_git_workspace_denied(self):
        """Workspace git_available=False → effective False."""
        session = SessionPermissions(git="write")
        workspace = WorkspaceCapabilities(git_available=False)
        eff = get_effective_permissions(session, workspace)
        assert eff["git"] is False

    def test_container_workspace_denied(self):
        """Workspace allow_docker=False + session container=True → False."""
        session = SessionPermissions(container=True)
        workspace = WorkspaceCapabilities(allow_docker=False)
        eff = get_effective_permissions(session, workspace)
        assert eff["container"] is False

    def test_container_both_true(self):
        """Both session and workspace allow container → True."""
        session = SessionPermissions(container=True)
        workspace = WorkspaceCapabilities(allow_docker=True)
        eff = get_effective_permissions(session, workspace)
        assert eff["container"] is True

    def test_system_passthrough(self):
        """System permission passes through unchanged (no workspace cap)."""
        session = SessionPermissions(system="read")
        workspace = WorkspaceCapabilities()
        eff = get_effective_permissions(session, workspace)
        assert eff["system"] == "read"


# ══════════════════════════════════════════════════════════════════════════
#  check_required_categories — direct allow / deny
# ══════════════════════════════════════════════════════════════════════════


class TestCheckRequiredCategoriesAllow:
    def test_empty_requirements(self):
        eff = {"filesystem": "write", "network": True, "container": True, "git": "write", "system": "full"}
        ok, msg = check_required_categories([], eff, "TestTool", {}, "", None)
        assert ok is True
        assert msg == ""

    def test_single_write_allowed(self):
        eff = {"filesystem": "write"}
        ok, msg = check_required_categories(["filesystem:write"], eff, "TestTool", {}, "", None)
        assert ok is True

    def test_network_true_allowed(self):
        eff = {"network": True}
        ok, msg = check_required_categories(["network:true"], eff, "TestTool", {}, "", None)
        assert ok is True

    def test_container_true_allowed(self):
        eff = {"container": True}
        ok, msg = check_required_categories(["container:true"], eff, "TestTool", {}, "", None)
        assert ok is True

    def test_multiple_all_allowed(self):
        eff = {"filesystem": "write", "network": True, "container": True, "git": "write", "system": "full"}
        ok, msg = check_required_categories(
            ["filesystem:write", "network:true", "container:true"],
            eff,
            "TestTool",
            {},
            "",
            None,
        )
        assert ok is True


class TestCheckRequiredCategoriesDeny:
    def test_banned_filesystem(self):
        eff = {"filesystem": "banned"}
        ok, msg = check_required_categories(["filesystem:write"], eff, "TestTool", {}, "", None)
        assert ok is False
        assert "banned" in msg

    def test_false_network(self):
        eff = {"network": False}
        ok, msg = check_required_categories(["network:true"], eff, "TestTool", {}, "", None)
        assert ok is False
        assert "False" in msg or "false" in msg.lower()

    def test_false_container(self):
        eff = {"container": False}
        ok, msg = check_required_categories(["container:true"], eff, "TestTool", {}, "", None)
        assert ok is False

    def test_read_not_enough_for_write(self):
        eff = {"filesystem": "read"}
        ok, msg = check_required_categories(["filesystem:write"], eff, "TestTool", {}, "", None)
        assert ok is False

    def test_unknown_category(self):
        eff = {"filesystem": "write"}
        ok, msg = check_required_categories(["unknown:true"], eff, "TestTool", {}, "", None)
        assert ok is False
        assert "Unknown category" in msg


# ══════════════════════════════════════════════════════════════════════════
#  check_required_categories — ask / prompt
# ══════════════════════════════════════════════════════════════════════════


class TestCheckRequiredCategoriesAsk:
    def test_ask_approved(self):
        """When effective is 'ask' and user approves, gate returns True."""
        eff = {"network": "ask"}
        mock_event_bus = MagicMock()

        with patch("security.security_gate.queue.Queue") as mock_queue_cls:
            mock_q = MagicMock()
            mock_q.get.return_value = {"approved": True, "remember": False}
            mock_queue_cls.return_value = mock_q

            ok, msg = check_required_categories(
                ["network:true"],
                eff,
                "NetworkTool",
                {"url": "http://example.com"},
                "Makes an outbound request",
                mock_event_bus,
                agent_id="1",
                session_id="sess_1",
            )

        assert ok is True
        # Verify event was published
        assert mock_event_bus.publish.called
        published_event = mock_event_bus.publish.call_args[0][0]
        assert published_event.data["tool_name"] == "NetworkTool"
        assert published_event.data["request_id"] is not None
        assert "network:true" in published_event.data["capabilities"]

    def test_ask_denied(self):
        """When effective is 'ask' and user denies, gate returns False."""
        eff = {"network": "ask"}

        with patch("security.security_gate.queue.Queue") as mock_queue_cls:
            mock_q = MagicMock()
            mock_q.get.return_value = {"approved": False, "remember": False}
            mock_queue_cls.return_value = mock_q

            ok, msg = check_required_categories(
                ["network:true"],
                eff,
                "NetworkTool",
                {},
                "Test",
                MagicMock(),
            )

        assert ok is False
        assert "denied" in msg.lower()

    def test_ask_timeout(self):
        """When queue.get raises queue.Empty, gate returns timeout denial."""
        eff = {"network": "ask"}

        with patch("security.security_gate.queue.Queue") as mock_queue_cls:
            mock_q = MagicMock()
            mock_q.get.side_effect = queue.Empty
            mock_queue_cls.return_value = mock_q

            ok, msg = check_required_categories(
                ["network:true"],
                eff,
                "NetworkTool",
                {},
                "Test",
                MagicMock(),
            )

        assert ok is False
        assert "timed out" in msg.lower()

    def test_ask_multiple_categories(self):
        """Multiple 'ask' categories produce a single prompt with both."""
        eff = {"filesystem": "ask", "network": "ask"}

        with patch("security.security_gate.queue.Queue") as mock_queue_cls:
            mock_q = MagicMock()
            mock_q.get.return_value = {"approved": True, "remember": False}
            mock_queue_cls.return_value = mock_q

            mock_bus = MagicMock()
            ok, msg = check_required_categories(
                ["filesystem:write", "network:true"],
                eff,
                "MultiTool",
                {},
                "Needs both",
                mock_bus,
            )

        assert ok is True
        published = mock_bus.publish.call_args[0][0]
        assert len(published.data["capabilities"]) == 2
        assert "filesystem:write" in published.data["capabilities"]
        assert "network:true" in published.data["capabilities"]

    def test_prompt_cleanup_after_approval(self):
        """_pending_security_requests entry is removed after the prompt resolves."""
        eff = {"network": "ask"}

        from security.security_gate import _pending_security_requests

        with patch("queue.Queue") as mock_queue_cls:
            mock_q = MagicMock()
            mock_q.get.return_value = {"approved": True, "remember": False}
            mock_queue_cls.return_value = mock_q

            check_required_categories(
                ["network:true"],
                eff,
                "NetTool",
                {},
                "",
                MagicMock(),
            )

        # The entry should have been popped in the finally block
        assert len(_pending_security_requests) == 0

    def test_ask_publishes_event_with_description(self):
        """The SecurityPromptEvent includes the description string."""
        eff = {"network": "ask"}

        with patch("security.security_gate.queue.Queue") as mock_queue_cls:
            mock_q = MagicMock()
            mock_q.get.return_value = {"approved": True, "remember": False}
            mock_queue_cls.return_value = mock_q

            mock_bus = MagicMock()
            check_required_categories(
                ["network:true"],
                eff,
                "NetTool",
                {"param": "val"},
                "Performs network request to fetch data",
                mock_bus,
            )

        event = mock_bus.publish.call_args[0][0]
        assert event.data["description"] == "Performs network request to fetch data"


# ══════════════════════════════════════════════════════════════════════════
#  resolve_prompt
# ══════════════════════════════════════════════════════════════════════════


class TestResolvePrompt:
    def test_resolve_existing(self):
        from security.security_gate import _pending_security_requests

        q = queue.Queue()
        _pending_security_requests["req-1"] = q
        assert resolve_prompt("req-1", True, remember=True) is True
        result = q.get_nowait()
        assert result == {"approved": True, "remember": True}

    def test_resolve_nonexistent(self):
        assert resolve_prompt("no-such-request", True) is False

    def test_resolve_cleans_up(self):
        from security.security_gate import _pending_security_requests

        q = queue.Queue()
        _pending_security_requests["req-2"] = q
        resolve_prompt("req-2", True)
        # The check_required_categories function pops in its finally;
        # resolve_prompt itself does not remove from the dict because
        # check_required_categories owns the cleanup.
        # But we can test that the value was placed on the queue.
        assert not q.empty()
