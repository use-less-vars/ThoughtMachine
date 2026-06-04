"""
Tests for the permission gate in tool_executor.py.

Covers:
  - _value_satisfies helper (boolean and string levels)
  - _check_permissions with default and custom profiles
  - Integration with ToolExecutor._execute_single_tool
  - Empty required_categories passes
  - Denied categories produce the right error message
"""

from typing import ClassVar, List
from agent.core.tool_executor import (
    _value_satisfies,
    _check_permissions,
    DEFAULT_SESSION_PERMISSIONS,
    ToolExecutor,
)
from tools.base import ToolBase


# ---------------------------------------------------------------------------
# Stub tools for testing
# ---------------------------------------------------------------------------

class PermissiveTool(ToolBase):
    """A tool that requires no special permissions."""
    tool: str = "PermissiveTool"
    required_categories: ClassVar[List[str]] = []

    def execute(self) -> str:
        return "OK"


class ContainerTool(ToolBase):
    """A tool that requires container access."""
    tool: str = "ContainerTool"
    required_categories: ClassVar[List[str]] = ["container:true"]

    def execute(self) -> str:
        return "Container OK"


class NetworkAndFilesystemTool(ToolBase):
    """A tool that requires both network and filesystem:write."""
    tool: str = "NetworkAndFilesystemTool"
    required_categories: ClassVar[List[str]] = ["network:true", "filesystem:write"]

    def execute(self) -> str:
        return "Network + FS OK"


# ---------------------------------------------------------------------------
# _value_satisfies
# ---------------------------------------------------------------------------

class TestValueSatisfies:
    """Unit tests for the _value_satisfies helper."""

    def test_boolean_true_allowed_true(self):
        assert _value_satisfies("true", True) is True

    def test_boolean_true_allowed_false(self):
        assert _value_satisfies("true", False) is False

    def test_boolean_false_allowed_false(self):
        assert _value_satisfies("false", False) is True

    def test_boolean_false_allowed_true(self):
        # True satisfies everything
        assert _value_satisfies("false", True) is True

    def test_string_read_allowed_read(self):
        assert _value_satisfies("read", "read") is True

    def test_string_read_allowed_banned(self):
        assert _value_satisfies("read", "banned") is False

    def test_string_write_allowed_banned(self):
        assert _value_satisfies("write", "banned") is False

    def test_string_write_allowed_read(self):
        assert _value_satisfies("write", "read") is False

    def test_string_write_allowed_write(self):
        assert _value_satisfies("write", "write") is True

    def test_string_write_allowed_full(self):
        assert _value_satisfies("write", "full") is True

    def test_string_full_allowed_write(self):
        assert _value_satisfies("full", "write") is False

    def test_string_full_allowed_full(self):
        assert _value_satisfies("full", "full") is True

    def test_string_banned_allowed_banned(self):
        assert _value_satisfies("banned", "banned") is True

    def test_string_banned_allowed_read(self):
        # banned required vs read allowed — read is higher, so it satisfies
        assert _value_satisfies("banned", "read") is True


# ---------------------------------------------------------------------------
# _check_permissions
# ---------------------------------------------------------------------------

class TestCheckPermissions:
    """Tests for the _check_permissions function."""

    def test_empty_categories_returns_none(self):
        assert _check_permissions([], DEFAULT_SESSION_PERMISSIONS) is None

    def test_empty_categories_with_custom_profile(self):
        profile = {"container": False}
        assert _check_permissions([], session_permissions=profile) is None

    def test_container_true_denied_in_default(self):
        error = _check_permissions(["container:true"], DEFAULT_SESSION_PERMISSIONS)
        assert error is not None
        assert "Permission denied" in error
        assert "container:true" in error
        assert "container:False" in error

    def test_container_false_allowed_in_default(self):
        assert _check_permissions(["container:false"], DEFAULT_SESSION_PERMISSIONS) is None

    def test_network_true_denied_in_default(self):
        error = _check_permissions(["network:true"], DEFAULT_SESSION_PERMISSIONS)
        assert error is not None
        assert "Permission denied" in error

    def test_filesystem_read_allowed_in_default(self):
        assert _check_permissions(["filesystem:read"], DEFAULT_SESSION_PERMISSIONS) is None

    def test_filesystem_write_denied_in_default(self):
        error = _check_permissions(["filesystem:write"], DEFAULT_SESSION_PERMISSIONS)
        assert error is not None
        assert "Permission denied" in error
        assert "filesystem:write" in error

    def test_execution_banned_allowed_in_default(self):
        assert _check_permissions(["execution:banned"], DEFAULT_SESSION_PERMISSIONS) is None

    def test_execution_read_denied_in_default(self):
        error = _check_permissions(["execution:read"], DEFAULT_SESSION_PERMISSIONS)
        assert error is not None
        assert "execution:read" in error
        assert "execution:banned" in error

    def test_unknown_category(self):
        error = _check_permissions(["nonexistent:true"], DEFAULT_SESSION_PERMISSIONS)
        assert error is not None
        assert "Unknown category" in error

    def test_custom_profile_overrides_default(self):
        custom = {"container": True, "network": False, "filesystem": "full", "security": "read", "execution": "banned"}
        assert _check_permissions(["container:true"], session_permissions=custom) is None
        assert _check_permissions(["network:true"], session_permissions=custom) is not None

    def test_multiple_requirements_all_pass(self):
        custom = {"container": True, "network": True, "filesystem": "write", "security": "read", "execution": "banned"}
        assert _check_permissions(["container:true", "filesystem:write"], session_permissions=custom) is None

    def test_multiple_requirements_one_fails(self):
        custom = {"container": True, "network": False, "filesystem": "write", "security": "read", "execution": "banned"}
        error = _check_permissions(["container:true", "network:true", "filesystem:write"], session_permissions=custom)
        assert error is not None
        assert "network:true" in error


# ---------------------------------------------------------------------------
# Integration with ToolExecutor._execute_single_tool
# ---------------------------------------------------------------------------

class FakeConfig:
    """Minimal config stub for ToolExecutor."""
    workspace_path = None
    tool_output_token_limit = None
    session_permissions = None


class FakeState:
    """Minimal state stub."""
    security_config = None


class TestToolExecutorPermissions:
    """Integration tests: ToolExecutor rejects/accepts tools based on categories."""

    def _make_executor(self, tool_classes):
        return ToolExecutor(
            tool_classes=tool_classes,
            config=FakeConfig(),
            state=FakeState(),
            logger=None,
            security_available=False,
            agent=None,
        )

    def test_permissive_tool_runs(self):
        executor = self._make_executor([PermissiveTool])
        result = executor._execute_single_tool(
            PermissiveTool, {}, "PermissiveTool", 0,
            lambda: False, lambda: None, lambda: 0
        )
        assert result["result"] == "OK"
        assert result["tool_type"] == "normal"

    def test_container_tool_denied(self):
        executor = self._make_executor([ContainerTool])
        result = executor._execute_single_tool(
            ContainerTool, {}, "ContainerTool", 0,
            lambda: False, lambda: None, lambda: 0
        )
        assert "Permission denied" in result["result"]
        assert "container:true" in result["result"]
        assert result["tool_type"] == "normal"

    def test_multiple_requirements(self):
        executor = self._make_executor([NetworkAndFilesystemTool])
        result = executor._execute_single_tool(
            NetworkAndFilesystemTool, {}, "NetworkAndFilesystemTool", 0,
            lambda: False, lambda: None, lambda: 0
        )
        # Both network:true and filesystem:write are denied in default profile
        assert "Permission denied" in result["result"]
        assert result["tool_type"] == "normal"

    def test_respects_custom_permissions_via_check_permissions(self):
        """If we monkey-patch _check_permissions, the executor still works.
        This tests the integration point. We use the ToolExecutor with a tool
        that requires nothing and verify it passes through."""
        executor = self._make_executor([PermissiveTool])
        result = executor._execute_single_tool(
            PermissiveTool, {}, "PermissiveTool", 0,
            lambda: False, lambda: None, lambda: 0
        )
        assert result["result"] == "OK"

    def test_error_returned_as_normal_tool_type(self):
        """Permission errors must be returned with tool_type='normal' so the LLM
        sees them as ordinary tool failures."""
        executor = self._make_executor([ContainerTool])
        result = executor._execute_single_tool(
            ContainerTool, {}, "ContainerTool", 0,
            lambda: False, lambda: None, lambda: 0
        )
        assert result["tool_type"] == "normal"
        assert result["result"].startswith("Permission denied")


# ---------------------------------------------------------------------------
# Dynamic category resolution (get_required_categories with params)
# ---------------------------------------------------------------------------

class StubFileEditor(ToolBase):
    """A FileEditor-like tool with operation-level categories."""
    tool: str = "StubFileEditor"
    required_categories: ClassVar[List[str]] = ["filesystem:write"]

    @classmethod
    def get_required_categories(cls, params: dict | None = None) -> list[str]:
        if params and params.get("operation") in ("read", "grep"):
            return ["filesystem:read"]
        return ["filesystem:write"]

    def execute(self) -> str:
        return "OK"


class StubRespondTool(ToolBase):
    """A Respond-like tool with dynamic categories based on report_body."""
    tool: str = "StubRespondTool"
    required_categories: ClassVar[List[str]] = []

    @classmethod
    def get_required_categories(cls, params: dict | None = None) -> list[str]:
        if params and params.get("report_body"):
            return ["filesystem:write"]
        return []

    def execute(self) -> str:
        return "OK"


class TestDynamicCategories:
    """Tests for get_required_categories with parameter-level granularity."""

    def test_file_editor_read_operation(self):
        """Reading a file only requires filesystem:read."""
        cats = StubFileEditor.get_required_categories({"operation": "read"})
        assert cats == ["filesystem:read"]

    def test_file_editor_write_operation(self):
        """Writing a file requires filesystem:write."""
        cats = StubFileEditor.get_required_categories({"operation": "write"})
        assert cats == ["filesystem:write"]

    def test_respond_without_report_body(self):
        """Normal respond doesn't require filesystem access."""
        cats = StubRespondTool.get_required_categories({})
        assert cats == []

    def test_respond_with_report_body(self):
        """Respond with a report body requires filesystem:write."""
        cats = StubRespondTool.get_required_categories({"report_body": "Hello"})
        assert cats == ["filesystem:write"]

    def test_static_fallback(self):
        """A tool without get_required_categories override uses the ClassVar."""
        cats = PermissiveTool.get_required_categories({})
        assert cats == []

    def test_no_params_fallback(self):
        """get_required_categories called without params uses ClassVar."""
        cats = StubFileEditor.get_required_categories()
        assert cats == ["filesystem:write"]


# ---------------------------------------------------------------------------
# Integration: ToolExecutor with custom SessionPermissions via config
# ---------------------------------------------------------------------------

from thoughtmachine.security import SessionPermissions



class FakeConfigWithPermissions:
    """Config stub with a SessionPermissions model."""
    workspace_path = None
    tool_output_token_limit = None

    def __init__(self, permissions: SessionPermissions | None = None):
        self.session_permissions = permissions


class TestToolExecutorCustomPermissions:
    """Integration tests: ToolExecutor respects custom session permissions."""

    def _make_executor(self, tool_classes, permissions=None):
        return ToolExecutor(
            tool_classes=tool_classes,
            config=FakeConfigWithPermissions(permissions),
            state=FakeState(),
            logger=None,
            security_available=False,
            agent=None,
        )

    def test_container_tool_allowed_with_custom_config(self):
        """Container tool runs when config has container=True."""
        perms = SessionPermissions(container=True)
        executor = self._make_executor([ContainerTool], permissions=perms)
        result = executor._execute_single_tool(
            ContainerTool, {}, "ContainerTool", 0,
            lambda: False, lambda: None, lambda: 0
        )
        assert result["result"] == "Container OK"

    def test_container_tool_denied_with_explicit_false(self):
        """Container tool denied when config has container=False."""
        perms = SessionPermissions(container=False)
        executor = self._make_executor([ContainerTool], permissions=perms)
        result = executor._execute_single_tool(
            ContainerTool, {}, "ContainerTool", 0,
            lambda: False, lambda: None, lambda: 0
        )
        assert "Permission denied" in result["result"]

    def test_mixed_allowed_and_denied(self):
        """Multiple requirements: allow container but deny network."""
        perms = SessionPermissions(container=True, network=False)
        executor = self._make_executor([NetworkAndFilesystemTool], permissions=perms)
        result = executor._execute_single_tool(
            NetworkAndFilesystemTool, {}, "NetworkAndFilesystemTool", 0,
            lambda: False, lambda: None, lambda: 0
        )
        # network:true is denied, filesystem:write is denied
        assert "Permission denied" in result["result"]

    def test_all_categories_allowed(self):
        """Everything allowed when SessionPermissions is maximally permissive."""
        perms = SessionPermissions(
            container=True, network=True,
            filesystem="full", security="full", execution="full"
        )
        executor = self._make_executor([NetworkAndFilesystemTool], permissions=perms)
        result = executor._execute_single_tool(
            NetworkAndFilesystemTool, {}, "NetworkAndFilesystemTool", 0,
            lambda: False, lambda: None, lambda: 0
        )
        assert result["result"] == "Network + FS OK"

    def test_none_permissions_falls_back_to_default(self):
        """When session_permissions is None, defaults are used (container denied)."""
        executor = self._make_executor([ContainerTool], permissions=None)
        result = executor._execute_single_tool(
            ContainerTool, {}, "ContainerTool", 0,
            lambda: False, lambda: None, lambda: 0
        )
        assert "Permission denied" in result["result"]

    def test_respond_tool_dynamic_category_integration(self):
        """Respond without report_body passes even with restrictive permissions."""
        perms = SessionPermissions(filesystem="read")
        executor = self._make_executor([StubRespondTool], permissions=perms)
        result = executor._execute_single_tool(
            StubRespondTool, {}, "StubRespondTool", 0,
            lambda: False, lambda: None, lambda: 0
        )
        # No report_body → empty required_categories → always passes
        assert result["result"] == "OK"
