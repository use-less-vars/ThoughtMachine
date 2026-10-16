"""Tests for ContainerRestartTool.

Wave 9b PHASE 1 introduces ``ContainerRestartTool`` — a stop + start
composition that recycles a container in place. These tests pin its
registration, its parameter surface, and its execute() decision table
entirely against MagicMock managers (no Docker required).
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


def _manager(stop_result=None, start_result=None, lifecycle_class="ephemeral"):
    manager = MagicMock()
    manager.class_of.return_value = lifecycle_class
    container = MagicMock()
    container.name = "tm-x"
    manager.client.containers.get.return_value = container
    manager.stop.return_value = dict(
        stop_result or {"status": "stopped", "container_id": "abc", "name": "tm-x"}
    )
    manager.start.return_value = dict(
        start_result or {"id": "abc", "name": "tm-x", "status": "reused"}
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
        manager.stop.assert_called_once_with("abc")
        manager.start.assert_called_once_with(name="tm-x")

    def test_missing_container_reports_missing(self):
        manager = _manager()
        manager.client.containers.get.side_effect = container_control.NotFound(
            "abc", "no such container"
        )
        tool = ContainerRestartTool(container_id="abc")
        with patch.object(ContainerRestartTool, "_make_manager", return_value=manager):
            result = _parse(tool.execute())

        assert result["success"] is False
        assert result["status"] == "missing"
        manager.stop.assert_not_called()
        manager.start.assert_not_called()

    def test_refused_stop_reports_reason_without_start(self):
        manager = _manager(
            stop_result={"status": "refused", "reason": "grain missing"}
        )
        tool = ContainerRestartTool(container_id="abc")
        with patch.object(ContainerRestartTool, "_make_manager", return_value=manager):
            result = _parse(tool.execute())

        assert result["success"] is False
        assert result["status"] == "refused"
        assert result["reason"] == "grain missing"
        manager.start.assert_not_called()

    def test_failed_stop_reports_error(self):
        manager = _manager(stop_result={"status": "error", "error": "daemon down"})
        tool = ContainerRestartTool(container_id="abc")
        with patch.object(ContainerRestartTool, "_make_manager", return_value=manager):
            result = _parse(tool.execute())

        assert result["success"] is False
        assert result["status"] == "error"
        assert result["error"] == "daemon down"
        manager.start.assert_not_called()

    def test_start_error_is_surfaced(self):
        manager = _manager(start_result={"error": "limit"})
        tool = ContainerRestartTool(container_id="abc")
        with patch.object(ContainerRestartTool, "_make_manager", return_value=manager):
            result = _parse(tool.execute())

        assert result["success"] is False
        assert result["status"] == "error"
        assert result["error"] == "limit"

    def test_missing_stop_still_restarts(self):
        # A container that has already gone away can still be (re)started; the
        # missing stop is tolerated rather than reported as a failure.
        manager = _manager(
            stop_result={"status": "missing", "container_id": "abc", "name": "tm-x"}
        )
        tool = ContainerRestartTool(container_id="abc")
        with patch.object(ContainerRestartTool, "_make_manager", return_value=manager):
            result = _parse(tool.execute())

        assert result["success"] is True
        assert result["status"] == "restarted"
        manager.start.assert_called_once_with(name="tm-x")

    def test_manager_construction_failure_reports_error(self):
        tool = ContainerRestartTool(container_id="abc")
        with patch.object(
            ContainerRestartTool,
            "_make_manager",
            side_effect=RuntimeError("no docker"),
        ):
            result = _parse(tool.execute())

        assert result["success"] is False
        assert "no docker" in result["error"]

    def test_container_lookup_failure_reports_error(self):
        manager = _manager()
        manager.client.containers.get.side_effect = RuntimeError("daemon down")
        tool = ContainerRestartTool(container_id="abc")
        with patch.object(ContainerRestartTool, "_make_manager", return_value=manager):
            result = _parse(tool.execute())

        assert result["success"] is False
        assert result["status"] == "error"
        manager.stop.assert_not_called()
