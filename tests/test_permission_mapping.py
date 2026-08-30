"""
Permission mapping tests for the tool registration & naming cleanup.

Verifies the security-gate git permission split (merged ``git`` level into
``git_read`` / ``git_write`` sub-levels) and the frontend tool-list mapping
(backend ``enabled_tools`` -> frontend ``tools`` list keyed by stable tool
names) after the ``GitInfoTool`` -> ``GitReadTool`` / ``git_read`` cleanup.
"""

import pytest

from security.security_gate import (
    check_required_categories,
    get_effective_permissions,
    split_git_permission,
)
from thoughtmachine.security import SessionPermissions
from thoughtmachine.workspace_capabilities import WorkspaceCapabilities
from web_ui.backend.config_manager import backend_to_frontend_config


def _tools_by_name(cfg):
    """Return ``{tool_name: entry}`` from a frontend config's ``tools`` list."""
    return {t["name"]: t for t in cfg["tools"]}


class TestSplitGitPermission:
    """The merged ``git`` level splits into (git_read, git_write) sub-levels."""

    @pytest.mark.parametrize(
        "level,expected",
        [
            (False, (False, False)),
            (None, (False, False)),
            ("banned", ("banned", "banned")),
            ("ask", ("ask", "ask")),
            ("read", ("read", "banned")),
            ("write", ("write", "write")),
            ("full", ("full", "full")),
            ("BANNED", ("banned", "banned")),  # case-insensitive
            ("unknown_level", (False, False)),  # unknown -> fail closed
        ],
    )
    def test_table(self, level, expected):
        assert split_git_permission(level) == expected


class TestEffectivePermissionsGitSplit:
    """get_effective_permissions exposes git_read/git_write for the gate."""

    def _eff(self, git_level):
        return get_effective_permissions(
            SessionPermissions(git=git_level), WorkspaceCapabilities()
        )

    def test_read_splits_to_read_banned(self):
        eff = self._eff("read")
        assert eff["git"] == "read"
        assert eff["git_read"] == "read"
        assert eff["git_write"] == "banned"

    def test_write_splits_to_write_write(self):
        eff = self._eff("write")
        assert eff["git"] == "write"
        assert eff["git_read"] == "write"
        assert eff["git_write"] == "write"

    def test_banned_splits_to_banned_banned(self):
        eff = self._eff("banned")
        assert eff["git"] == "banned"
        assert eff["git_read"] == "banned"
        assert eff["git_write"] == "banned"

    def test_full_splits_to_full_full(self):
        eff = self._eff("full")
        assert eff["git"] == "full"
        assert eff["git_read"] == "full"
        assert eff["git_write"] == "full"

    def test_workspace_git_unavailable_fail_closed(self):
        eff = get_effective_permissions(
            SessionPermissions(git="read"),
            WorkspaceCapabilities(git_available=False),
        )
        assert eff["git"] is False
        assert eff["git_read"] is False
        assert eff["git_write"] is False

    def test_workspace_defaults_are_permissive(self):
        caps = WorkspaceCapabilities()
        assert caps.git_available is True
        assert caps.allow_network is True
        assert caps.allow_docker is True
        assert caps.filesystem_write is True


class TestCheckRequiredCategories:
    """The gate honours the split sub-categories (no interactive prompts)."""

    def _eff_read(self):
        return get_effective_permissions(
            SessionPermissions(git="read"), WorkspaceCapabilities()
        )

    def test_git_write_denied_on_read_session(self):
        eff = self._eff_read()
        allowed, msg = check_required_categories(
            ["git:write"], eff, "GitWriteTool", {}, "git write", event_bus=None
        )
        assert allowed is False
        assert "git:write" in msg

    def test_git_read_allowed_on_read_session(self):
        eff = self._eff_read()
        allowed, msg = check_required_categories(
            ["git:read"], eff, "GitReadTool", {}, "git read", event_bus=None
        )
        assert allowed is True
        assert msg == ""

    def test_git_write_allowed_on_write_session(self):
        eff = get_effective_permissions(
            SessionPermissions(git="write"), WorkspaceCapabilities()
        )
        allowed, msg = check_required_categories(
            ["git:write"], eff, "GitWriteTool", {}, "git write", event_bus=None
        )
        assert allowed is True
        assert msg == ""


class TestFrontendToolMapping:
    """backend_to_frontend_config emits stable tool names for the UI."""

    def test_stable_names_in_custom_mode(self):
        cfg = backend_to_frontend_config(
            {
                "mode": "custom",
                "enabled_tools": ["Respond", "git_read", "host_bash"],
                "allow_host_resources": False,
                "session_permissions": {},
            }
        )
        tools = _tools_by_name(cfg)
        assert "git_read" in tools
        assert tools["git_read"]["enabled"] is True
        assert "GitInfoTool" not in tools
        assert "git_write" in tools
        assert tools["git_write"]["enabled"] is False
        assert tools["host_bash"]["enabled"] is True
        assert (
            tools["host_bash"]["disabled_reason"]
            == "requires allow_host_resources: true"
        )
        assert tools["host_bash"]["permission_level"] is None

    def test_legacy_class_name_normalized(self):
        cfg = backend_to_frontend_config(
            {"enabled_tools": ["Respond", "GitInfoTool"]}
        )
        tools = _tools_by_name(cfg)
        assert tools["git_read"]["enabled"] is True

    def test_host_bash_enabled_when_allow_host_resources(self):
        cfg = backend_to_frontend_config(
            {
                "mode": "custom",
                "enabled_tools": ["host_bash"],
                "allow_host_resources": True,
                "session_permissions": {},
            }
        )
        tools = _tools_by_name(cfg)
        assert tools["host_bash"]["disabled_reason"] is None

    def test_host_bash_permission_level_surfaced(self):
        cfg = backend_to_frontend_config(
            {
                "mode": "custom",
                "enabled_tools": ["host_bash"],
                "allow_host_resources": True,
                "session_permissions": {"host_bash": "full"},
            }
        )
        tools = _tools_by_name(cfg)
        assert tools["host_bash"]["permission_level"] == "full"
