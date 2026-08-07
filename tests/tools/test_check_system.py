"""
Unit tests for the CheckSystem 'containers' query (Phase 4.5).

Covers the ``containers`` handler added in Phase 4.5: it lists the
per-workspace containers via ``ContainerManager.list_containers()`` (scoped by
the ``thoughtmachine.workspace_id`` label) and surfaces the sticky ``note``
from the vault bulletin board
(``<vault_root>/workspaces/<workspace_id>/container_notes.json``).

Style mirrors the CheckSystem block in tests/tools/test_workspace_tools.py
(allowlist pinned via patching, no live Docker daemon needed).
"""

import json
from unittest.mock import MagicMock, patch

from tools.workspace.check_system import CheckSystem


def _parse_result(result: str) -> dict:
    return json.loads(result)


class _FakeManager:
    """ContainerManager stand-in whose list_containers() returns one entry."""

    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs

    def list_containers(self):
        ws_id = self.kwargs.get("workspace_id") or "ws-x"
        return [
            {
                "container_id": "c1",
                "name": "c1",
                "image": None,
                "status": "running",
                "uptime_seconds": 5,
                "workspace_id": ws_id,
                "note": "n1",
            }
        ]

    def container_summary(self, container_id):
        return {}


class TestCheckSystemContainers:
    """Tests for the CheckSystem 'containers' query."""

    @patch("tools.workspace.check_system.resolve_workspace_id", return_value=None)
    def test_containers_query_returns_list_with_notes(self, mock_resolve):
        """Success path: entries carry name/status/note/uptime_seconds."""
        with patch.object(
            CheckSystem, "_load_allowlist_from_vault", return_value=["containers"]
        ), patch("tools.workspace.check_system._ContainerManager", _FakeManager):
            tool = CheckSystem(
                query="containers",
                workspace_path="/tmp/test_ws",
                session_permissions={"container": True},
            )
            result = _parse_result(tool.execute())
        assert result["status"] == "ok"
        assert result["count"] == 1
        entry = result["containers"][0]
        assert entry["name"] == "c1"
        assert entry["status"] == "running"
        assert entry["note"] == "n1"
        assert entry["uptime_seconds"] == 5

    @patch("tools.workspace.check_system.resolve_workspace_id", return_value=None)
    def test_containers_query_requires_container_permission(self, mock_resolve):
        """No container permission -> unavailable, never raises."""
        with patch.object(
            CheckSystem, "_load_allowlist_from_vault", return_value=["containers"]
        ):
            tool = CheckSystem(
                query="containers",
                workspace_path="/tmp/test_ws",
                session_permissions={},
            )
            result = _parse_result(tool.execute())
        assert result["status"] == "unavailable"
        assert result["containers"] == []
        assert result["count"] == 0
        assert result["reason"] == "no container permission"

    @patch("tools.workspace.check_system.resolve_workspace_id", return_value=None)
    def test_containers_query_unavailable_when_manager_missing(self, mock_resolve):
        """_ContainerManager None -> graceful degraded response."""
        with patch.object(
            CheckSystem, "_load_allowlist_from_vault", return_value=["containers"]
        ), patch("tools.workspace.check_system._ContainerManager", None):
            tool = CheckSystem(
                query="containers",
                workspace_path="/tmp/test_ws",
                session_permissions={"container": True},
            )
            result = _parse_result(tool.execute())
        assert result["status"] == "unavailable"
        assert result["containers"] == []
        assert result["reason"] == "Container manager not available"

    def test_containers_query_is_documented(self):
        """'containers' is a documented query value and a real handler exists."""
        desc = CheckSystem.model_fields["query"].description
        assert "containers" in desc
        assert callable(getattr(CheckSystem, "_query_containers", None))

    @patch("tools.workspace.check_system.resolve_workspace_id", return_value=None)
    def test_containers_query_merges_running_summary(self, mock_resolve):
        """Phase 6: running entries are decorated with container_summary();
        stopped entries are not, and the summary is fetched once per running id."""
        mock_cls = MagicMock()
        mock_manager = MagicMock()
        mock_manager.list_containers.return_value = [
            {"container_id": "c1", "name": "c1", "status": "running",
             "note": "n1", "uptime_seconds": 5},
            {"container_id": "c2", "name": "c2", "status": "exited",
             "note": "", "uptime_seconds": None},
        ]
        mock_manager.container_summary.return_value = {
            "packages_count": 3,
            "disk_usage": {"workspace": "12M", "packages": "3M"},
        }
        mock_cls.return_value = mock_manager
        with patch.object(
            CheckSystem, "_load_allowlist_from_vault", return_value=["containers"]
        ), patch("tools.workspace.check_system._ContainerManager", mock_cls):
            tool = CheckSystem(
                query="containers",
                workspace_path="/tmp/test_ws",
                session_permissions={"container": True},
            )
            result = _parse_result(tool.execute())
        assert result["status"] == "ok"
        assert result["count"] == 2
        assert result["containers"][0]["packages_count"] == 3
        assert result["containers"][0]["disk_usage"] == {
            "workspace": "12M", "packages": "3M",
        }
        assert "packages_count" not in result["containers"][1]
        mock_manager.container_summary.assert_called_once_with("c1")
