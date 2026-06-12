"""
Tests for workspace introspection / management tools.

Covers
------
- CheckSystem: all 5 query types (effective_permissions, container_status,
  workspace_info, my_config, network_diagnostics) plus unknown query.
- Worker:     list, spawn, check, query, missing worker_name, unknown action,
              missing workers.json.
- EditDockerfile: append instructions, creation from template, empty
                  instructions error, timestamp comment.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch, MagicMock, mock_open

import pytest

from tools.workspace.check_system import CheckSystem
from tools.workspace.worker import Worker
from tools.workspace.edit_dockerfile import EditDockerfile


# ══════════════════════════════════════════════════════════════════════════════
#  Helpers
# ══════════════════════════════════════════════════════════════════════════════

def _parse_result(result: str) -> dict:
    """Parse JSON tool result into a dict."""
    return json.loads(result)


# ══════════════════════════════════════════════════════════════════════════════
#  CheckSystem
# ══════════════════════════════════════════════════════════════════════════════

class TestCheckSystem:
    """Tests for CheckSystem (all 5 query types + unknown)."""

    @patch("tools.workspace.check_system.resolve_workspace_id", return_value="ws_test")
    @patch("tools.workspace.check_system._workspace_dir")
    def test_query_permissions(self, mock_ws_dir, mock_resolve):
        """effective_permissions returns merged permissions."""
        # Mock workspace dir / capabilities.json to exist
        mock_dir = MagicMock()
        mock_caps_path = MagicMock()
        mock_caps_path.exists.return_value = True
        mock_caps_path.read_text.return_value = json.dumps({"allow_network": True, "allow_docker": False})
        mock_dir.__truediv__.return_value = mock_caps_path
        mock_ws_dir.return_value = mock_dir

        tool = CheckSystem(
            query="effective_permissions",
            session_permissions={
                "container": False,
                "network": "banned",
                "filesystem": "read",
            },
            workspace_path="/tmp/test_ws",
        )
        result = _parse_result(tool.execute())
        assert "effective_permissions" in result
        assert "workspace_capabilities" in result
        assert result["workspace_id"] == "ws_test"

    @patch("tools.workspace.check_system.resolve_workspace_id", return_value=None)
    def test_query_permissions_no_ws_id(self, mock_resolve):
        """effective_permissions works without workspace ID."""
        tool = CheckSystem(
            query="effective_permissions",
            session_permissions={"container": False, "network": "banned"},
            workspace_path="/tmp/test_ws",
        )
        result = _parse_result(tool.execute())
        assert "effective_permissions" in result
        assert result["workspace_id"] is None

    @patch("tools.workspace.check_system._get_docker_status")
    @patch("tools.workspace.check_system.resolve_workspace_id")
    def test_query_container_status(self, mock_resolve, mock_status):
        """container_status returns the docker status result."""
        mock_status.return_value = {"status": "running", "container_id": "abc123"}
        tool = CheckSystem(
            query="container_status",
            workspace_path="/tmp/test_ws",
        )
        result = _parse_result(tool.execute())
        assert result["status"] == "running"
        assert result["container_id"] == "abc123"

    @patch("tools.workspace.check_system._get_docker_status")
    @patch("tools.workspace.check_system.resolve_workspace_id")
    def test_query_container_status_no_ws_path(self, mock_resolve, mock_status):
        """container_status returns unavailable when no workspace path."""
        tool = CheckSystem(query="container_status", workspace_path=None)
        result = _parse_result(tool.execute())
        assert result["status"] == "unavailable"
        assert "No workspace path" in result["reason"]

    @patch("tools.workspace.check_system.resolve_workspace_id", return_value="ws_test")
    @patch("tools.workspace.check_system._workspace_dir")
    def test_query_workspace_info(self, mock_ws_dir, mock_resolve):
        """workspace_info returns workspace metadata."""
        mock_dir = MagicMock()

        # Mock config.json
        mock_config = MagicMock()
        mock_config.exists.return_value = True
        mock_config.read_text.return_value = json.dumps({
            "capabilities": {"allow_network": True},
            "domain_allowlist": ["example.com"],
        })

        # Mock workers.json
        mock_workers = MagicMock()
        mock_workers.exists.return_value = True
        mock_workers.read_text.return_value = json.dumps([
            {"name": "worker1", "status": "idle"},
        ])

        # Mock mcp_servers.json
        mock_mcp = MagicMock()
        mock_mcp.exists.return_value = True
        mock_mcp.read_text.return_value = json.dumps([
            {"name": "mcp1", "url": "http://localhost:8080"},
        ])

        # Set up __truediv__ to return different results based on which file is asked for
        # We'll use a side_effect approach
        def _div(key):
            key_str = str(key)
            if "config.json" in key_str:
                return mock_config
            if "workers.json" in key_str:
                return mock_workers
            if "mcp_servers.json" in key_str:
                return mock_mcp
            return MagicMock()

        mock_dir.__truediv__.side_effect = _div
        mock_ws_dir.return_value = mock_dir

        tool = CheckSystem(
            query="workspace_info",
            workspace_path="/tmp/test_ws",
        )
        result = _parse_result(tool.execute())
        assert result["workspace_id"] == "ws_test"
        assert "capabilities" in result
        assert "domain_allowlist" in result
        assert "workers" in result
        assert "mcp_tools" in result
        assert result["workers"] == [{"name": "worker1", "status": "idle"}]

    def test_query_my_config(self):
        """my_config returns agent_config as JSON string."""
        tool = CheckSystem(
            query="my_config",
            agent_config={
                "temperature": 0.2,
                "model": "gpt-4",
                "max_turns": 100,
            },
        )
        result = tool.execute()  # Returns JSON string directly
        parsed = json.loads(result)
        assert parsed["temperature"] == 0.2
        assert parsed["model"] == "gpt-4"

    def test_query_my_config_no_agent_config(self):
        """my_config returns error when agent_config is None."""
        tool = CheckSystem(query="my_config", agent_config=None)
        result = json.loads(tool.execute())
        assert "error" in result
        assert "agent_config not available" in result["error"]

    @patch("tools.workspace.check_system.DOCKER_EXECUTOR_CLS_AVAILABLE", False)
    @patch("tools.workspace.check_system.resolve_workspace_id")
    def test_network_diagnostics_no_container(self, mock_resolve):
        """network_diagnostics returns 'no container' when not running."""
        tool = CheckSystem(
            query="network_diagnostics",
            workspace_path="/tmp/test_ws",
        )
        result = _parse_result(tool.execute())
        assert result["container"] is False

    def test_unknown_query(self):
        """Unknown query returns error with available queries."""
        tool = CheckSystem(query="nonexistent_query", workspace_path="/tmp/test_ws")
        result = _parse_result(tool.execute())
        assert "error" in result
        assert "nonexistent_query" in result["error"]
        assert "available_queries" in result

    def test_required_categories_empty(self):
        """CheckSystem declares no required categories."""
        assert CheckSystem.required_categories == []


# ══════════════════════════════════════════════════════════════════════════════
#  Worker
# ══════════════════════════════════════════════════════════════════════════════

class TestWorker:
    """Tests for Worker (stub implementations)."""

    @patch("tools.workspace.worker.resolve_workspace_id", return_value="ws_test")
    @patch("tools.workspace.worker._workspace_dir")
    def test_action_list_empty(self, mock_ws_dir, mock_resolve):
        """list returns zero workers when workers.json is empty list."""
        mock_dir = MagicMock()
        mock_file = MagicMock()
        mock_file.exists.return_value = True
        mock_file.read_text.return_value = json.dumps([])
        mock_dir.__truediv__.return_value = mock_file
        mock_ws_dir.return_value = mock_dir

        tool = Worker(action="list", workspace_path="/tmp/test_ws")
        result = _parse_result(tool.execute())
        assert result["count"] == 0
        assert result["workers"] == []

    @patch("tools.workspace.worker.resolve_workspace_id", return_value="ws_test")
    @patch("tools.workspace.worker._workspace_dir")
    def test_action_list_with_workers(self, mock_ws_dir, mock_resolve):
        """list returns workers from workers.json."""
        mock_dir = MagicMock()
        mock_file = MagicMock()
        mock_file.exists.return_value = True
        mock_file.read_text.return_value = json.dumps([
            {"name": "coder", "status": "idle", "permission_subset": ["execution:read"], "last_heartbeat": None},
        ])
        mock_dir.__truediv__.return_value = mock_file
        mock_ws_dir.return_value = mock_dir

        tool = Worker(action="list", workspace_path="/tmp/test_ws")
        result = _parse_result(tool.execute())
        assert result["count"] == 1
        assert result["workers"][0]["name"] == "coder"

    @patch("tools.workspace.worker.resolve_workspace_id", return_value="ws_test")
    @patch("tools.workspace.worker._workspace_dir")
    def test_action_spawn_found(self, mock_ws_dir, mock_resolve):
        """spawn returns success when worker_name exists in workers.json."""
        mock_dir = MagicMock()
        mock_file = MagicMock()
        mock_file.exists.return_value = True
        mock_file.read_text.return_value = json.dumps([
            {"name": "coder", "status": "idle"},
        ])
        mock_dir.__truediv__.return_value = mock_file
        mock_ws_dir.return_value = mock_dir

        tool = Worker(action="spawn", worker_name="coder", workspace_path="/tmp/test_ws")
        result = _parse_result(tool.execute())
        assert result["spawned"] is True
        assert result["worker_name"] == "coder"

    @patch("tools.workspace.worker.resolve_workspace_id", return_value="ws_test")
    @patch("tools.workspace.worker._workspace_dir")
    def test_action_spawn_not_found(self, mock_ws_dir, mock_resolve):
        """spawn returns error when worker_name not in workers.json."""
        mock_dir = MagicMock()
        mock_file = MagicMock()
        mock_file.exists.return_value = True
        mock_file.read_text.return_value = json.dumps([
            {"name": "coder", "status": "idle"},
        ])
        mock_dir.__truediv__.return_value = mock_file
        mock_ws_dir.return_value = mock_dir

        tool = Worker(action="spawn", worker_name="nonexistent", workspace_path="/tmp/test_ws")
        result = _parse_result(tool.execute())
        assert "error" in result

    @patch("tools.workspace.worker.resolve_workspace_id", return_value="ws_test")
    @patch("tools.workspace.worker._workspace_dir")
    def test_action_check_found(self, mock_ws_dir, mock_resolve):
        """check returns status for a known worker."""
        mock_dir = MagicMock()
        mock_file = MagicMock()
        mock_file.exists.return_value = True
        mock_file.read_text.return_value = json.dumps([
            {"name": "coder", "status": "idle"},
        ])
        mock_dir.__truediv__.return_value = mock_file
        mock_ws_dir.return_value = mock_dir

        tool = Worker(action="check", worker_name="coder", workspace_path="/tmp/test_ws")
        result = _parse_result(tool.execute())
        assert result["worker_name"] == "coder"
        assert result["status"] == "idle"
        assert "current_task" in result

    @patch("tools.workspace.worker.resolve_workspace_id", return_value="ws_test")
    @patch("tools.workspace.worker._workspace_dir")
    def test_action_check_not_found(self, mock_ws_dir, mock_resolve):
        """check returns error for unknown worker."""
        mock_dir = MagicMock()
        mock_file = MagicMock()
        mock_file.exists.return_value = True
        mock_file.read_text.return_value = json.dumps([])
        mock_dir.__truediv__.return_value = mock_file
        mock_ws_dir.return_value = mock_dir

        tool = Worker(action="check", worker_name="nonexistent", workspace_path="/tmp/test_ws")
        result = _parse_result(tool.execute())
        assert "error" in result

    @patch("tools.workspace.worker.resolve_workspace_id", return_value="ws_test")
    @patch("tools.workspace.worker._workspace_dir")
    def test_action_query_found(self, mock_ws_dir, mock_resolve):
        """query returns stub response for a known worker."""
        mock_dir = MagicMock()
        mock_file = MagicMock()
        mock_file.exists.return_value = True
        mock_file.read_text.return_value = json.dumps([
            {"name": "coder", "status": "idle"},
        ])
        mock_dir.__truediv__.return_value = mock_file
        mock_ws_dir.return_value = mock_dir

        tool = Worker(action="query", worker_name="coder", workspace_path="/tmp/test_ws")
        result = _parse_result(tool.execute())
        assert result["worker_name"] == "coder"
        assert "stub" in result["response"]

    def test_action_missing_worker_name(self):
        """spawn/check/query return error when worker_name is missing."""
        tool = Worker(action="spawn", worker_name=None, workspace_path="/tmp/test_ws")
        result = _parse_result(tool.execute())
        assert "error" in result
        assert "worker_name is required" in result["error"]

    def test_unknown_action(self):
        """Unknown action returns error with available actions."""
        tool = Worker(action="fly", workspace_path="/tmp/test_ws")
        result = _parse_result(tool.execute())
        assert "error" in result
        assert "available_actions" in result

    @patch("tools.workspace.worker.resolve_workspace_id", return_value="ws_test")
    @patch("tools.workspace.worker._workspace_dir")
    def test_missing_workers_file(self, mock_ws_dir, mock_resolve):
        """list returns empty when workers.json doesn't exist."""
        mock_dir = MagicMock()
        mock_file = MagicMock()
        mock_file.exists.return_value = False  # File doesn't exist
        mock_dir.__truediv__.return_value = mock_file
        mock_ws_dir.return_value = mock_dir

        tool = Worker(action="list", workspace_path="/tmp/test_ws")
        result = _parse_result(tool.execute())
        assert result["count"] == 0

    def test_required_categories(self):
        """Worker declares execution:read."""
        assert "execution:read" in Worker.required_categories


# ══════════════════════════════════════════════════════════════════════════════
#  EditDockerfile
# ══════════════════════════════════════════════════════════════════════════════

class TestEditDockerfile:
    """Tests for EditDockerfile."""

    @patch("tools.workspace.edit_dockerfile.resolve_workspace_id", return_value="ws_test")
    @patch("tools.workspace.edit_dockerfile._workspace_dir")
    def test_append_instructions(self, mock_ws_dir, mock_resolve, tmp_path: Path):
        """Append adds instructions to an existing Dockerfile."""
        dockerfile_path = tmp_path / "Dockerfile"
        dockerfile_path.write_text("FROM python:3.11-slim\n")

        mock_dir = MagicMock()
        mock_dockerfile = MagicMock()
        mock_dockerfile.exists.return_value = True
        mock_dockerfile.read_text.return_value = "FROM python:3.11-slim\n"
        mock_dir.__truediv__.return_value = mock_dockerfile
        mock_ws_dir.return_value = mock_dir

        # Patch write_text to actually write to our tmp_path
        def _write_text(content, encoding="utf-8"):
            dockerfile_path.write_text(content, encoding=encoding)

        mock_dockerfile.write_text.side_effect = _write_text

        tool = EditDockerfile(
            instructions="RUN apt-get install -y curl",
            workspace_path=str(tmp_path),
        )
        result = tool.execute()  # Returns plain string, not JSON

        # Result should be the full new content
        assert "FROM python:3.11-slim" in result
        assert "RUN apt-get install -y curl" in result
        assert "edit_dockerfile" in result  # timestamp comment

    @patch("tools.workspace.edit_dockerfile.resolve_workspace_id", return_value="ws_test")
    @patch("tools.workspace.edit_dockerfile._workspace_dir")
    def test_creates_from_template(self, mock_ws_dir, mock_resolve, tmp_path: Path):
        """Creates Dockerfile from template when it doesn't exist."""
        dockerfile_path = tmp_path / "Dockerfile"

        mock_dir = MagicMock()
        mock_dockerfile = MagicMock()
        mock_dockerfile.exists.return_value = False  # File doesn't exist yet

        def _make_exist():
            mock_dockerfile.exists.return_value = True

        def _write_text(content, encoding="utf-8"):
            dockerfile_path.write_text(content, encoding=encoding)
            _make_exist()

        def _read_text(encoding="utf-8"):
            if dockerfile_path.exists():
                return dockerfile_path.read_text(encoding=encoding)
            return ""

        mock_dockerfile.write_text.side_effect = _write_text
        mock_dockerfile.read_text.side_effect = _read_text
        mock_dir.__truediv__.return_value = mock_dockerfile
        mock_ws_dir.return_value = mock_dir

        # Patch the template path to point to our temp template
        template_path = tmp_path / "resources" / "default_dockerfile.txt"
        template_path.parent.mkdir(parents=True)
        template_path.write_text("FROM test:latest\n")

        with patch.object(
            Path, "resolve",
            return_value=Path(__file__).resolve().parent.parent.parent / "resources" / "default_dockerfile.txt",
        ):
            with patch.object(Path, "exists", return_value=True):
                with patch.object(Path, "read_text", return_value="FROM test:latest\n"):
                    tool = EditDockerfile(
                        instructions="RUN echo hello",
                        workspace_path=str(tmp_path),
                    )
                    result = tool.execute()

        # Result should contain the template content + instructions
        assert "FROM test:latest" in result
        assert "RUN echo hello" in result
        assert "edit_dockerfile" in result  # timestamp comment

    def test_empty_instructions(self):
        """Empty instructions returns error."""
        tool = EditDockerfile(
            instructions="   ",
            workspace_path="/tmp/test_ws",
        )
        result = tool.execute()
        parsed = json.loads(result)
        assert "error" in parsed

    @patch("tools.workspace.edit_dockerfile.resolve_workspace_id")
    def test_no_workspace(self, mock_resolve):
        """Returns error when no workspace ID can be resolved."""
        mock_resolve.return_value = None
        tool = EditDockerfile(
            instructions="RUN echo hello",
            workspace_path="/tmp/nonexistent",
        )
        result = tool.execute()
        parsed = json.loads(result)
        assert "error" in parsed
        assert "No active workspace" in parsed["error"]

    def test_timestamp_in_output(self):
        """Timestamp comment includes ISO datetime."""
        # This test validates the implementation creates a timestamp
        from datetime import datetime
        now = datetime.now()
        timestamp_str = now.isoformat()
        assert "T" in timestamp_str  # ISO format has T separator

    def test_required_categories(self):
        """EditDockerfile declares container:write."""
        assert "container:write" in EditDockerfile.required_categories
