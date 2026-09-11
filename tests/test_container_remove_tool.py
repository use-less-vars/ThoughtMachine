"""Tests for ContainerRemoveTool (the container-removal tool).

No Docker daemon is required: ``_make_manager`` is patched so the tool never
constructs a real ``ContainerManager``. Covers:

- registration in ``tools.TOOL_CLASSES`` and the required ``container:true``
  category (inherited from ``_ContainerControlBase``)
- the stable tool identifier follows the sibling container-tool convention
  (the class name, since these tools do not override the ``name`` ClassVar)
- ``execute()`` surfaces ``ContainerManager.remove()``'s dict verbatim as JSON
- error results and RuntimeError failures are returned, never raised
- ``container_id`` is required
"""

import json
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from tools import TOOL_CLASSES
from tools.container_control import ContainerRemoveTool


def _parse(result: str) -> dict:
    return json.loads(result)


class TestContainerRemoveToolRegistration:
    def test_registered_in_tool_classes(self):
        assert ContainerRemoveTool in TOOL_CLASSES

    def test_requires_container_category(self):
        assert "container:true" in ContainerRemoveTool.required_categories

    def test_stable_name_matches_sibling_convention(self):
        # Sibling container tools (Start/Stop/Status/...) expose their class
        # name as the tool identifier; ContainerRemoveTool must match.
        assert ContainerRemoveTool.tool_name() == "ContainerRemoveTool"
        assert ContainerRemoveTool.model_fields["tool"].default == "ContainerRemoveTool"
        assert ContainerRemoveTool(container_id="x").tool == "ContainerRemoveTool"


class TestContainerRemoveToolExecute:
    def test_success_removed(self):
        manager = MagicMock()
        manager.remove.return_value = {"status": "removed", "container_id": "abc123"}
        tool = ContainerRemoveTool(container_id="abc123")
        with patch.object(ContainerRemoveTool, "_make_manager", return_value=manager):
            result = _parse(tool.execute())
        manager.remove.assert_called_once_with("abc123")
        assert result["success"] is True
        assert result["status"] == "removed"
        assert result["container_id"] == "abc123"
        assert "error" not in result

    def test_success_removed_with_name(self):
        manager = MagicMock()
        manager.remove.return_value = {
            "status": "removed", "container_id": "abc123", "name": "tm-x",
        }
        tool = ContainerRemoveTool(container_id="abc123")
        with patch.object(ContainerRemoveTool, "_make_manager", return_value=manager):
            result = _parse(tool.execute())
        assert result["success"] is True
        assert result["name"] == "tm-x"

    def test_error_dict_surfaced_not_raised(self):
        manager = MagicMock()
        manager.remove.return_value = {
            "status": "error", "container_id": "abc123", "error": "boom",
        }
        tool = ContainerRemoveTool(container_id="abc123")
        with patch.object(ContainerRemoveTool, "_make_manager", return_value=manager):
            result = _parse(tool.execute())
        assert result["success"] is False
        assert result["status"] == "error"
        assert result["error"] == "boom"
        assert result["container_id"] == "abc123"

    def test_runtime_error_surfaced_not_raised(self):
        tool = ContainerRemoveTool(container_id="abc123")
        with patch.object(
            ContainerRemoveTool,
            "_make_manager",
            side_effect=RuntimeError("Docker Python SDK not installed."),
        ):
            result = _parse(tool.execute())
        assert result["success"] is False
        assert "Docker Python SDK not installed." in result["error"]

    def test_unexpected_error_surfaced_not_raised(self):
        manager = MagicMock()
        manager.remove.side_effect = ValueError("kaboom")
        tool = ContainerRemoveTool(container_id="abc123")
        with patch.object(ContainerRemoveTool, "_make_manager", return_value=manager):
            result = _parse(tool.execute())
        assert result["success"] is False
        assert "kaboom" in result["error"]


def test_container_id_is_required():
    with pytest.raises(ValidationError):
        ContainerRemoveTool()
