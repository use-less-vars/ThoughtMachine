"""Tests for ContainerRemoveTool (the container-removal tool).

No Docker daemon is required: ``_make_manager`` is patched so the tool never
constructs a real ``ContainerManager``. Covers:

- registration in ``tools.TOOL_CLASSES`` and the required ``container:true``
  category (inherited from ``_ContainerControlBase``)
- the stable tool identifier follows the sibling container-tool convention
  (the class name, since these tools do not override the ``name`` ClassVar)
- ``execute()`` surfaces ``ContainerManager.remove()``'s dict verbatim as JSON
- the per-lifecycle-class removal gate: ephemeral removes freely, persistent
  removes with the base ``container:true`` grain (no sub-grain), resource is
  operator-only
- idempotent NotFound -> "removed"
- error results and RuntimeError failures are returned, never raised
- ``container_id_or_name`` is required
"""

import json
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from tools import TOOL_CLASSES
from tools.container_control import ContainerRemoveTool
import tools.container_control as container_control


def _parse(result: str) -> dict:
    return json.loads(result)


def _manager(lifecycle_class="ephemeral", remove_result=None):
    """Build a MagicMock manager whose container classifies as given."""
    manager = MagicMock()
    manager.class_of.return_value = lifecycle_class
    manager.client.containers.get.return_value = MagicMock()
    if remove_result is not None:
        manager.remove.return_value = remove_result
    return manager


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
        assert ContainerRemoveTool(container_id_or_name="x").tool == "ContainerRemoveTool"


class TestContainerRemoveToolParameters:
    def test_container_id_or_name_is_required(self):
        with pytest.raises(ValidationError):
            ContainerRemoveTool()

    def test_workspace_id_defaults_to_none(self):
        tool = ContainerRemoveTool(container_id_or_name="abc123")
        assert tool.workspace_id is None

    def test_workspace_id_is_forwarded_to_make_manager(self):
        manager = _manager(remove_result={"status": "removed", "name": "tm-x"})
        tool = ContainerRemoveTool(container_id_or_name="abc123", workspace_id="ws-9")
        with patch.object(
            ContainerRemoveTool, "_make_manager", return_value=manager
        ) as make_manager:
            _parse(tool.execute())
        make_manager.assert_called_once_with("ws-9")


class TestContainerRemoveToolExecute:
    def test_success_removed(self):
        manager = _manager(
            remove_result={"status": "removed", "container_id": "abc123"}
        )
        tool = ContainerRemoveTool(container_id_or_name="abc123")
        with patch.object(ContainerRemoveTool, "_make_manager", return_value=manager):
            result = _parse(tool.execute())
        manager.remove.assert_called_once_with("abc123", keep_record=False)
        assert result["success"] is True
        assert result["status"] == "removed"
        assert result["container_id"] == "abc123"
        assert "error" not in result

    def test_success_removed_with_name(self):
        manager = _manager(
            remove_result={
                "status": "removed", "container_id": "abc123", "name": "tm-x",
            }
        )
        tool = ContainerRemoveTool(container_id_or_name="abc123")
        with patch.object(ContainerRemoveTool, "_make_manager", return_value=manager):
            result = _parse(tool.execute())
        assert result["success"] is True
        assert result["name"] == "tm-x"

    def test_error_dict_surfaced_not_raised(self):
        manager = _manager(
            remove_result={
                "status": "error", "container_id": "abc123", "error": "boom",
            }
        )
        tool = ContainerRemoveTool(container_id_or_name="abc123")
        with patch.object(ContainerRemoveTool, "_make_manager", return_value=manager):
            result = _parse(tool.execute())
        assert result["success"] is False
        assert result["status"] == "error"
        assert result["error"] == "boom"
        assert result["container_id"] == "abc123"

    def test_runtime_error_surfaced_not_raised(self):
        tool = ContainerRemoveTool(container_id_or_name="abc123")
        with patch.object(
            ContainerRemoveTool,
            "_make_manager",
            side_effect=RuntimeError("Docker Python SDK not installed."),
        ):
            result = _parse(tool.execute())
        assert result["success"] is False
        assert "Docker Python SDK not installed." in result["error"]

    def test_unexpected_error_surfaced_not_raised(self):
        manager = _manager()
        manager.remove.side_effect = ValueError("kaboom")
        tool = ContainerRemoveTool(container_id_or_name="abc123")
        with patch.object(ContainerRemoveTool, "_make_manager", return_value=manager):
            result = _parse(tool.execute())
        assert result["success"] is False
        assert "kaboom" in result["error"]

    def test_missing_container_is_idempotent_removed(self):
        """Docker NotFound -> already removed; the manager is never called."""
        manager = _manager()
        manager.client.containers.get.side_effect = container_control.NotFound(
            "404 Client Error: No such container: abc123"
        )
        tool = ContainerRemoveTool(container_id_or_name="abc123")
        with patch.object(ContainerRemoveTool, "_make_manager", return_value=manager):
            result = _parse(tool.execute())
        assert result["success"] is True
        assert result["status"] == "removed"
        assert result["container_id"] == "abc123"
        manager.remove.assert_not_called()

    def test_container_lookup_failure_reports_error(self):
        manager = _manager()
        manager.client.containers.get.side_effect = RuntimeError("daemon down")
        tool = ContainerRemoveTool(container_id_or_name="abc123")
        with patch.object(ContainerRemoveTool, "_make_manager", return_value=manager):
            result = _parse(tool.execute())
        assert result["success"] is False
        assert result["status"] == "error"
        assert "daemon down" in result["error"]
        manager.remove.assert_not_called()


class TestContainerRemoveToolLifecycleGrain:
    def test_ephemeral_removes_freely(self):
        manager = _manager(
            lifecycle_class="ephemeral",
            remove_result={"status": "removed", "container_id": "abc123"},
        )
        tool = ContainerRemoveTool(
            container_id_or_name="abc123",
            session_permissions={"container": True},
        )
        with patch.object(ContainerRemoveTool, "_make_manager", return_value=manager):
            result = _parse(tool.execute())
        assert result["success"] is True
        assert result["status"] == "removed"

    def test_persistent_removes_with_container_true(self):
        """Persistent remove needs only the base ``container:true`` grain."""
        manager = _manager(
            lifecycle_class="persistent",
            remove_result={"status": "removed", "container_id": "abc123"},
        )
        tool = ContainerRemoveTool(
            container_id_or_name="abc123",
            session_permissions={"container": True},
        )
        with patch.object(ContainerRemoveTool, "_make_manager", return_value=manager):
            result = _parse(tool.execute())
        assert result["success"] is True
        assert result["status"] == "removed"
        assert "error" not in result
        manager.remove.assert_called_once_with("abc123", keep_record=False)

    def test_persistent_no_longer_requires_a_sub_grain(self):
        """The ungrantable ``container:remove`` token is no longer consulted."""
        manager = _manager(
            lifecycle_class="persistent",
            remove_result={"status": "removed", "container_id": "abc123"},
        )
        tool = ContainerRemoveTool(container_id_or_name="abc123")  # no permissions
        with patch.object(ContainerRemoveTool, "_make_manager", return_value=manager):
            result = _parse(tool.execute())
        assert result["status"] == "removed"
        manager.remove.assert_called_once_with("abc123", keep_record=False)

    def test_envelope_is_the_session_gate(self):
        """The hard session gate is the base category, not a per-class grain."""
        assert ContainerRemoveTool.required_categories == ["container:true"]

    def test_resource_is_operator_only(self):
        """Resource containers stay operator-only for agent sessions."""
        from infra.container_manager import ContainerManager

        manager = _manager(lifecycle_class="resource")
        tool = ContainerRemoveTool(
            container_id_or_name="abc123",
            session_permissions={"container": True},
        )
        with patch.object(ContainerRemoveTool, "_make_manager", return_value=manager):
            result = _parse(tool.execute())
        assert result["success"] is False
        assert result["status"] == "refused"
        assert "operator" in result["reason"]
        manager.remove.assert_not_called()
        # ... and the funnel refuses it too, independently of the tool.
        assert (
            ContainerManager._agent_access_denial("resource")
            == "Resource container access denied"
        )

    def test_unknown_lifecycle_class_is_left_to_the_manager(self):
        """An unclassifiable/unknown class is not refused by the tool itself."""
        manager = _manager(
            lifecycle_class=None,
            remove_result={"status": "removed", "container_id": "abc123"},
        )
        tool = ContainerRemoveTool(container_id_or_name="abc123")
        with patch.object(ContainerRemoveTool, "_make_manager", return_value=manager):
            result = _parse(tool.execute())
        assert result["status"] == "removed"

    def test_unclassifiable_is_refused_by_the_funnel(self):
        """A4: the unclassifiable refusal lives in the funnel, not the tool."""
        from infra.container_manager import ContainerManager
        from security.admission_gate import REASON_UNKNOWN_LIFECYCLE_CLASS

        # The layer that actually refuses (None has no lifecycle policy).
        assert (
            ContainerManager._agent_access_denial(None)
            == REASON_UNKNOWN_LIFECYCLE_CLASS
        )
        manager = _manager(
            lifecycle_class=None,
            remove_result={
                "status": "error",
                "container_id": "abc123",
                "error": REASON_UNKNOWN_LIFECYCLE_CLASS,
            },
        )
        tool = ContainerRemoveTool(container_id_or_name="abc123")
        with patch.object(ContainerRemoveTool, "_make_manager", return_value=manager):
            result = _parse(tool.execute())
        # The tool passed the call through; the funnel's refusal is surfaced.
        manager.remove.assert_called_once_with("abc123", keep_record=False)
        assert result["success"] is False
        assert result["error"] == REASON_UNKNOWN_LIFECYCLE_CLASS

    def test_class_of_failure_is_tolerated(self):
        manager = _manager(
            remove_result={"status": "removed", "container_id": "abc123"},
        )
        manager.class_of.side_effect = RuntimeError("classification failed")
        tool = ContainerRemoveTool(container_id_or_name="abc123")
        with patch.object(ContainerRemoveTool, "_make_manager", return_value=manager):
            result = _parse(tool.execute())
        assert result["success"] is True
        assert result["status"] == "removed"
