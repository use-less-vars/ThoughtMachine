"""Tests for ContainerRestartTool.

``ContainerRestartTool`` delegates to ``ContainerManager.restart`` to recycle
a container in place. These tests pin its registration, its parameter surface,
and its execute() decision table entirely against MagicMock managers (no
Docker required).
"""

import json
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

import tools.container_control as container_control
from tools import TOOL_CLASSES
from tools.container_control import ContainerRestartTool


def _parse(result):
    if isinstance(result, str):
        return json.loads(result)
    return result


def _manager(restart_result=None):
    manager = MagicMock()
    manager.restart.return_value = dict(
        restart_result
        or {"status": "restarted", "container_id": "abc", "name": "tm-x"}
    )
    return manager


# ---------------------------------------------------------------------------
# Registration / parameter surface
# ---------------------------------------------------------------------------


class TestContainerRestartToolRegistration:
    def test_registered_in_tool_classes(self):
        assert ContainerRestartTool in TOOL_CLASSES

    def test_tool_name(self):
        assert ContainerRestartTool.tool_name() == "ContainerRestartTool"

    def test_tool_literal_default(self):
        assert ContainerRestartTool.model_fields["tool"].default == "ContainerRestartTool"

    def test_required_categories(self):
        assert ContainerRestartTool.required_categories == ["container:true"]

    def test_container_id_required(self):
        with pytest.raises(ValidationError):
            ContainerRestartTool()


# ---------------------------------------------------------------------------
# Execute decision table
# ---------------------------------------------------------------------------


class TestContainerRestartToolExecute:
    def test_successful_restart(self):
        manager = _manager()
        tool = ContainerRestartTool(container_id="abc")
        with patch.object(ContainerRestartTool, "_make_manager", return_value=manager):
            result = _parse(tool.execute())

        assert result["success"] is True
        assert result["status"] == "restarted"
        assert result["container_id"] == "abc"
        assert result["name"] == "tm-x"
        manager.restart.assert_called_once_with("abc")
        manager.stop.assert_not_called()
        manager.start.assert_not_called()

    def test_missing_container_reports_missing(self):
        manager = _manager(
            restart_result={
                "status": "missing",
                "container_id": "abc",
                "error": "container not found",
            }
        )
        tool = ContainerRestartTool(container_id="abc")
        with patch.object(ContainerRestartTool, "_make_manager", return_value=manager):
            result = _parse(tool.execute())

        assert result["success"] is False
        assert result["status"] == "missing"
        assert result["error"] == "container not found"
        manager.restart.assert_called_once_with("abc")
        manager.stop.assert_not_called()
        manager.start.assert_not_called()

    def test_refused_restart_reports_reason(self):
        manager = _manager(
            restart_result={
                "status": "refused",
                "container_id": "abc",
                "name": "tm-x",
                "reason": "grain missing",
            }
        )
        tool = ContainerRestartTool(container_id="abc")
        with patch.object(ContainerRestartTool, "_make_manager", return_value=manager):
            result = _parse(tool.execute())

        assert result["success"] is False
        assert result["status"] == "refused"
        assert result["reason"] == "grain missing"
        manager.stop.assert_not_called()
        manager.start.assert_not_called()

    def test_restart_error_reports_error(self):
        manager = _manager(
            restart_result={
                "status": "error",
                "container_id": "abc",
                "error": "daemon down",
            }
        )
        tool = ContainerRestartTool(container_id="abc")
        with patch.object(ContainerRestartTool, "_make_manager", return_value=manager):
            result = _parse(tool.execute())

        assert result["success"] is False
        assert result["status"] == "error"
        assert result["error"] == "daemon down"
        manager.stop.assert_not_called()
        manager.start.assert_not_called()

    def test_start_error_is_surfaced(self):
        manager = _manager(
            restart_result={
                "status": "error",
                "container_id": "abc",
                "name": "tm-x",
                "error": "limit",
            }
        )
        tool = ContainerRestartTool(container_id="abc")
        with patch.object(ContainerRestartTool, "_make_manager", return_value=manager):
            result = _parse(tool.execute())

        assert result["success"] is False
        assert result["status"] == "error"
        assert result["error"] == "limit"

    def test_restart_reports_recreated_container_id(self):
        # restart may hand back the id of the recreated container; the tool
        # surfaces that id verbatim rather than the requested one.
        manager = _manager(
            restart_result={
                "status": "restarted",
                "container_id": "new-123",
                "name": "tm-x",
            }
        )
        tool = ContainerRestartTool(container_id="abc")
        with patch.object(ContainerRestartTool, "_make_manager", return_value=manager):
            result = _parse(tool.execute())

        assert result["success"] is True
        assert result["status"] == "restarted"
        assert result["container_id"] == "new-123"
        manager.restart.assert_called_once_with("abc")
        manager.stop.assert_not_called()
        manager.start.assert_not_called()

    def test_manager_construction_failure_reports_error(self):
        tool = ContainerRestartTool(container_id="abc")
        with patch.object(
            ContainerRestartTool,
            "_make_manager",
            side_effect=RuntimeError("no docker"),
        ):
            result = _parse(tool.execute())

        assert result["success"] is False
        assert result["status"] == "error"
        assert "no docker" in result["error"]

    def test_container_lookup_failure_reports_error(self):
        # The lookup now lives inside manager.restart; a raising lookup is
        # surfaced as an error without any direct stop/start from the tool.
        manager = _manager()
        manager.restart.side_effect = RuntimeError("daemon down")
        tool = ContainerRestartTool(container_id="abc")
        with patch.object(ContainerRestartTool, "_make_manager", return_value=manager):
            result = _parse(tool.execute())

        assert result["success"] is False
        assert result["status"] == "error"
        assert "daemon down" in result["error"]
        manager.stop.assert_not_called()
