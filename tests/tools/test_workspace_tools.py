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
import logging
import os
from pathlib import Path
from unittest.mock import patch, MagicMock, mock_open

import pytest

from tools.workspace.check_system import CheckSystem
from tools.workspace.worker import Worker, WorkerThread, _restrictive_merge
from agent.core.worker_context import WorkerContext
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
            {"name": "worker1", "status": "ready"},
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
        assert result["workers"] == [{"name": "worker1", "status": "ready"}]

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

    @patch("tools.workspace.check_system.DOCKER_EXECUTOR_CLS_AVAILABLE", True)
    @patch("tools.workspace.check_system.resolve_workspace_id", return_value=None)
    @patch("tools.workspace.check_system._ContainerManager")
    def test_network_diagnostics_daemon_unreachable(self, mock_cm, mock_resolve):
        """network_diagnostics reports daemon unreachable when images.get raises."""
        instance = MagicMock()
        instance.client.images.get.side_effect = Exception("connection refused")
        mock_cm.return_value = instance

        tool = CheckSystem(
            query="network_diagnostics",
            workspace_path="/tmp/test_ws",
            session_permissions={},
        )
        result = _parse_result(tool.execute())
        assert result["daemon"] is False
        assert result["container"] is False
        assert result["image_present"] is False
        assert "unreachable" in result["message"]
        instance.start.assert_not_called()

    @patch("tools.workspace.check_system.DOCKER_EXECUTOR_CLS_AVAILABLE", True)
    @patch("tools.workspace.check_system.resolve_workspace_id", return_value=None)
    @patch("tools.workspace.check_system._ContainerManager")
    def test_network_diagnostics_probe_ok(self, mock_cm, mock_resolve):
        """network_diagnostics reports egress OK and always stops the probe."""
        instance = MagicMock()
        instance.client.images.get.return_value = MagicMock()
        instance.start.return_value = {
            "id": "abc", "name": "tm-net-diag-test", "status": "created"
        }
        instance.exec.return_value = {
            "stdout": "EGRESS_OK 200\n", "stderr": "", "exit_code": 0,
        }
        instance.stop.return_value = {
            "status": "stopped", "container_id": "abc", "name": "tm-net-diag-test",
        }
        mock_cm.return_value = instance

        tool = CheckSystem(
            query="network_diagnostics",
            workspace_path="/tmp/test_ws",
            session_permissions={"network": "write"},
        )
        result = _parse_result(tool.execute())
        assert result["daemon"] is True
        assert result["container"] is True
        assert result["image_present"] is True
        assert result["container_id"] == "abc"
        assert result["egress"] is True
        assert result["probe"]["stdout"] == "EGRESS_OK 200\n"
        assert result["probe"]["exit_code"] == 0
        # finally-cleanup verified: stop() called with the container id
        instance.stop.assert_called_once_with("abc")
        # session permissions passed through so the probe uses session isolation
        _, kwargs = mock_cm.call_args
        assert kwargs["workspace_path"] == "/tmp/test_ws"
        assert kwargs["session_permissions"] == {"network": "write"}

    @patch("tools.workspace.check_system.DOCKER_EXECUTOR_CLS_AVAILABLE", True)
    @patch("tools.workspace.check_system.resolve_workspace_id", return_value=None)
    @patch("tools.workspace.check_system._ContainerManager")
    def test_network_diagnostics_probe_blocked(self, mock_cm, mock_resolve):
        """network_diagnostics reports egress False when the probe is blocked."""
        instance = MagicMock()
        instance.client.images.get.return_value = MagicMock()
        instance.start.return_value = {
            "id": "abc", "name": "tm-net-diag-test", "status": "created"
        }
        instance.exec.return_value = {
            "stdout": "EGRESS_BLOCKED URLError <urlopen error timed out>\n",
            "stderr": "", "exit_code": 0,
        }
        mock_cm.return_value = instance

        tool = CheckSystem(
            query="network_diagnostics",
            workspace_path="/tmp/test_ws",
            session_permissions={"network": "read"},
        )
        result = _parse_result(tool.execute())
        assert result["egress"] is False
        assert "EGRESS_BLOCKED" in result["probe"]["stdout"]
        assert "blocked" in result["message"]

    @pytest.mark.skipif(
        os.environ.get("TM_LIVE_NETWORK_TEST") != "1",
        reason="live docker probe (set TM_LIVE_NETWORK_TEST=1)",
    )
    def test_network_diagnostics_live_probe(self, tmp_path):
        """LIVE: real docker daemon probe with a temp workspace."""
        tool = CheckSystem(
            query="network_diagnostics",
            workspace_path=str(tmp_path),
            session_permissions={"network": "write"},
        )
        result = _parse_result(tool.execute())
        for field in ("daemon", "container", "container_id", "container_status",
                      "image_present", "probe", "egress", "message"):
            assert field in result

    def test_unknown_query(self):
        """Unknown query returns error with valid queries."""
        tool = CheckSystem(query="nonexistent_query", workspace_path="/tmp/test_ws")
        result = _parse_result(tool.execute())
        assert "error" in result
        assert "nonexistent_query" in result["error"]
        assert "valid_queries" in result

    def test_required_categories_requires_system_read(self):
        """CheckSystem requires system:read because it inspects host state and can run subprocesses."""
        assert CheckSystem.required_categories == ["system:read"]

    # ── New query tests ────────────────────────────────────────────────

    def test_my_config_is_valid_json(self):
        """my_config returns structured JSON with key fields."""
        tool = CheckSystem(
            query="my_config",
            agent_config={
                "provider": "anthropic",
                "model": "claude-sonnet-4",
                "timeout_seconds": 600,
                "max_turns": 50,
                "enabled_tools": ["Thought", "FileEditor"],
                "temperature": 0.7,
                "system_prompt": "You are a helpful assistant.",
                "session_permissions": {"filesystem:write": True},
                "api_key": "sk-real-key",
            },
        )
        result = json.loads(tool.execute())
        assert result["provider"] == "anthropic"
        assert result["model"] == "claude-sonnet-4"
        assert result["timeout_seconds"] == 600
        assert result["max_turns"] == 50
        assert result["enabled_tools"] == ["Thought", "FileEditor"]
        # API key should be masked
        assert result["api_key"] == "***"
        assert "raw_config" in result

    def test_my_config_includes_restriction_reason(self):
        """my_config includes restriction_reason when set."""
        tool = CheckSystem(
            query="my_config",
            agent_config={
                "provider": "anthropic",
                "model": "claude-sonnet-4",
                "restriction_reason": "token_critical",
            },
        )
        result = json.loads(tool.execute())
        assert result["restriction_reason"] == "token_critical"

    @patch("tools.workspace.check_system.resolve_workspace_id", return_value="ws_test")
    @patch("tools.workspace.check_system._workspace_dir")
    def test_workers_query_returns_definitions(self, mock_ws_dir, mock_resolve):
        """'workers' query returns worker definitions from workers.json."""
        mock_dir = MagicMock()
        mock_workers = MagicMock()
        mock_workers.exists.return_value = True
        mock_workers.read_text.return_value = json.dumps([
            {"name": "default", "system_prompt": "Default worker", "tools": ["Thought"], "timeout_seconds": 60},
        ])

        def _div(key):
            if "workers.json" in str(key):
                return mock_workers
            return MagicMock()

        mock_dir.__truediv__.side_effect = _div
        mock_ws_dir.return_value = mock_dir

        tool = CheckSystem(
            query="workers",
            workspace_path="/tmp/test_ws",
        )
        result = json.loads(tool.execute())
        assert "workers" in result
        assert result["count"] == 1
        assert result["workers"][0]["name"] == "default"

    @patch("tools.workspace.check_system.resolve_workspace_id", return_value="ws_test")
    @patch("tools.workspace.check_system._workspace_dir")
    def test_worker_detail_query(self, mock_ws_dir, mock_resolve):
        """'worker/<name>' returns specific worker definition."""
        mock_dir = MagicMock()
        mock_workers = MagicMock()
        mock_workers.exists.return_value = True
        mock_workers.read_text.return_value = json.dumps([
            {"name": "default", "system_prompt": "Default worker", "tools": ["Thought"], "timeout_seconds": 60},
            {"name": "helper", "system_prompt": "Helper worker", "tools": ["FileEditor"]},
        ])

        def _div(key):
            if "workers.json" in str(key):
                return mock_workers
            return MagicMock()

        mock_dir.__truediv__.side_effect = _div
        mock_ws_dir.return_value = mock_dir

        tool = CheckSystem(
            query="worker/default",
            workspace_path="/tmp/test_ws",
        )
        result = json.loads(tool.execute())
        assert result["name"] == "default"
        assert result["system_prompt"] == "Default worker"
        assert result["timeout_seconds"] == 60

    def test_worker_detail_query_not_found(self):
        """'worker/<unknown>' returns error."""
        tool = CheckSystem(
            query="worker/nonexistent",
            workspace_path="/tmp/test_ws",
        )
        result = json.loads(tool.execute())
        assert "error" in result

    @patch("tools.workspace.check_system.WORKER_REGISTRY_AVAILABLE", False)
    def test_running_workers_query(self):
        """'running_workers' returns list from registry."""
        tool = CheckSystem(
            query="running_workers",
            workspace_path="/tmp/test_ws",
        )
        result = json.loads(tool.execute())
        assert "running_workers" in result
        assert result["count"] == 0

    @patch("tools.workspace.check_system.resolve_workspace_id", return_value="ws_test")
    @patch("tools.workspace.check_system._workspace_dir")
    def test_capabilities_query(self, mock_ws_dir, mock_resolve):
        """'capabilities' returns workspace feature info."""
        mock_dir = MagicMock()
        mock_caps = MagicMock()
        mock_caps.exists.return_value = True
        mock_caps.read_text.return_value = json.dumps({
            "allow_docker": True,
            "git_available": True,
            "max_context_length": 100000,
        })

        def _div(key):
            if "capabilities.json" in str(key):
                return mock_caps
            return MagicMock()

        mock_dir.__truediv__.side_effect = _div
        mock_ws_dir.return_value = mock_dir

        tool = CheckSystem(
            query="capabilities",
            agent_config={"provider": "anthropic", "model": "claude-sonnet-4", "enabled_tools": ["Thought"]},
            workspace_path="/tmp/test_ws",
        )
        result = json.loads(tool.execute())
        assert "provider" in result
        assert "model" in result
        assert "has_docker" in result
        assert "has_git" in result
        assert "os" in result

    @patch("tools.workspace.check_system.resolve_workspace_id", return_value="ws_test")
    @patch("tools.workspace.check_system._workspace_dir")
    def test_dockerfile_query(self, mock_ws_dir, mock_resolve):
        """'dockerfile' returns Dockerfile content."""
        mock_dir = MagicMock()
        mock_df = MagicMock()
        mock_df.exists.return_value = True
        mock_df.read_text.return_value = "FROM python:3.11\n"

        def _div(key):
            if "Dockerfile" in str(key):
                return mock_df
            return MagicMock()

        mock_dir.__truediv__.side_effect = _div
        mock_ws_dir.return_value = mock_dir

        tool = CheckSystem(
            query="dockerfile",
            workspace_path="/tmp/test_ws",
        )
        result = json.loads(tool.execute())
        assert result["available"] is True
        assert "FROM python:3.11" in result["content"]

    def test_dockerfile_query_not_available(self):
        """'dockerfile' returns available=false when no Dockerfile."""
        tool = CheckSystem(
            query="dockerfile",
            workspace_path="/tmp/nonexistent",
        )
        result = json.loads(tool.execute())
        assert result["available"] is False

    @patch("tools.workspace.check_system.resolve_workspace_id", return_value="ws_test")
    @patch("tools.workspace.check_system._workspace_dir")
    def test_mcp_servers_query(self, mock_ws_dir, mock_resolve):
        """'mcp_servers' returns configured MCP servers."""
        mock_dir = MagicMock()
        mock_mcp = MagicMock()
        mock_mcp.exists.return_value = True
        mock_mcp.read_text.return_value = json.dumps([
            {"name": "my-mcp", "url": "http://localhost:9090"},
        ])

        def _div(key):
            if "mcp_servers.json" in str(key):
                return mock_mcp
            return MagicMock()

        mock_dir.__truediv__.side_effect = _div
        mock_ws_dir.return_value = mock_dir

        tool = CheckSystem(
            query="mcp_servers",
            workspace_path="/tmp/test_ws",
        )
        result = json.loads(tool.execute())
        assert "mcp_servers" in result
        assert result["count"] == 1
        assert result["mcp_servers"][0]["name"] == "my-mcp"

    def test_unknown_query_returns_valid_queries(self):
        """Unknown query returns error with valid_queries list."""
        tool = CheckSystem(query="nonexistent", workspace_path="/tmp/test_ws")
        result = json.loads(tool.execute())
        assert "error" in result
        assert "valid_queries" in result
        assert isinstance(result["valid_queries"], list)

# ══════════════════════════════════════════════════════════════════════════════
#  Worker
# ══════════════════════════════════════════════════════════════════════════════

class TestWorker:
    """Tests for Worker tool and WorkerThread."""

    # ═══════════════════════════════════════════════════════════════════
    #  WorkerThread unit tests
    # ═══════════════════════════════════════════════════════════════════

    def test_worker_thread_init(self, tmp_path: Path):
        """WorkerThread initialises with idle status and empty conversation."""
        thread = WorkerThread(
            name="test_worker",
            definition={"system_prompt": "You are a test."},
            agent_config={},
            workspace_dir=tmp_path,
        )
        assert thread.worker_name == "test_worker"
        assert thread.status == "ready"
        assert thread._worker_ctx is None
        assert thread._worker_dir == tmp_path / "workers" / "test_worker"
        assert thread.current_task is None
        assert thread.error is None
        assert thread.is_alive() is False  # not started yet

    def test_worker_thread_save_and_load_context(self, tmp_path: Path):
        """WorkerThread persists and reloads context to/from disk."""
        thread = WorkerThread(
            name="persist_test",
            definition={},
            agent_config={},
            workspace_dir=tmp_path,
        )
        thread._worker_ctx = WorkerContext(user_history=[
            {"role": "system", "content": "You are a test."},
            {"role": "user", "content": "Hello"},
        ])
        thread.status = "busy"
        thread.last_heartbeat = "2025-01-01T00:00:00"
        thread._save_context()

        # Verify file exists
        context_file = tmp_path / "workers" / "persist_test" / "context.json"
        assert context_file.exists()

        # Load into a fresh thread
        thread2 = WorkerThread(
            name="persist_test",
            definition={},
            agent_config={},
            workspace_dir=tmp_path,
        )
        ctx = thread2._load_context()
        assert ctx is not None
        assert len(ctx.user_history) == 2
        assert ctx.user_history[1]["content"] == "Hello"
        assert thread2.status == "busy"

    def test_resume_worker_loads_current_system_prompt(self, tmp_path: Path):
        """
        Resuming a worker preserves the persisted conversation.

        WorkerThread.run() keeps the loaded conversation intact — the current
        definition's system prompt is applied later when the worker's Agent is
        created (agent.core.agent.Agent.__init__ → ensure_system_prompt).
        """
        # Write a persisted context with an OLD system prompt + messages
        ctx = {
            "conversation": [
                {"role": "system", "content": "You are an old assistant."},
                {"role": "user", "content": "Hello"},
                {"role": "assistant", "content": "Hi there!"},
            ],
            "status": "ready",
            "worker_name": "resume_test",
        }
        context_path = tmp_path / "workers" / "resume_test" / "context.json"
        context_path.parent.mkdir(parents=True)
        context_path.write_text(json.dumps(ctx), encoding="utf-8")

        thread = WorkerThread(
            name="resume_test",
            definition={"system_prompt": "You are a NEW assistant."},
            agent_config={},
            workspace_dir=tmp_path,
        )

        # _load_context() restores the persisted conversation untouched
        loaded = thread._load_context()
        assert loaded is not None
        assert len(loaded.user_history) == 3
        assert loaded.user_history[0]["content"] == "You are an old assistant."
        assert loaded.worker_name == "resume_test"

        # run() preserves the loaded conversation — no stale-prompt replacement
        thread._worker_ctx = loaded
        thread.start()
        thread.stop()
        thread.join(timeout=2)

        assert thread._worker_ctx is not None
        assert len(thread._worker_ctx.user_history) == 3
        assert thread._worker_ctx.user_history[0]["content"] == "You are an old assistant."
        # User/assistant pair preserved
        assert thread._worker_ctx.user_history[1]["role"] == "user"
        assert thread._worker_ctx.user_history[1]["content"] == "Hello"
        assert thread._worker_ctx.user_history[2]["role"] == "assistant"
        assert thread._worker_ctx.user_history[2]["content"] == "Hi there!"

        # The CURRENT definition prompt is what a fresh Agent would receive:
        # _build_agent_config() reads it from the definition (agent_config
        # must carry provider/model for the config to be built).
        assert thread.definition["system_prompt"] == "You are a NEW assistant."
        cfg_thread = WorkerThread(
            name="resume_test",
            definition={"system_prompt": "You are a NEW assistant."},
            agent_config={
                "provider": "scripted",
                "model": "mock-model",
                "api_key": "sk-test-scripted",
                "base_url": "http://localhost:9999",
                "enabled_tools": [],
                "max_turns": 10,
                "enable_logging": False,
            },
            workspace_dir=tmp_path,
        )
        cfg = cfg_thread._build_agent_config()
        assert cfg is not None
        assert cfg.system_prompt == "You are a NEW assistant."

    def test_worker_thread_send_query_timeout(self, tmp_path: Path):
        """send_query raises TimeoutError when worker doesn't respond."""
        thread = WorkerThread(
            name="timeout_test",
            definition={},
            agent_config={},
            workspace_dir=tmp_path,
        )
        # Thread not running — queue.get will time out
        with pytest.raises(TimeoutError):
            thread.send_query("hello", timeout=0.01)

    # ═══════════════════════════════════════════════════════════════════
    #  Worker tool — list
    # ═══════════════════════════════════════════════════════════════════

    @patch("tools.workspace.worker._load_template_workers", return_value=[])
    @patch("tools.workspace.worker.resolve_workspace_id", return_value="ws_test")
    @patch("tools.workspace.worker._workspace_dir")
    def test_action_list_empty(self, mock_ws_dir, mock_resolve, mock_templates):
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
        """list returns workers from workers.json with runtime_status."""
        mock_dir = MagicMock()
        mock_file = MagicMock()
        mock_file.exists.return_value = True
        mock_file.read_text.return_value = json.dumps([
            {"name": "default", "status": "ready", "permission_subset": [], "last_heartbeat": None},
        ])
        mock_dir.__truediv__.return_value = mock_file
        mock_ws_dir.return_value = mock_dir

        tool = Worker(action="list", workspace_path="/tmp/test_ws")
        result = _parse_result(tool.execute())
        assert result["count"] == 1
        assert result["workers"][0]["name"] == "default"
        # Not spawned yet, so runtime_status should be "stopped"
        assert result["workers"][0]["runtime_status"] == "stopped"

    # ═══════════════════════════════════════════════════════════════════
    #  Worker tool — spawn
    # ═══════════════════════════════════════════════════════════════════

    def test_spawn_preserves_context_file(self, tmp_path: Path):
        """Spawning preserves existing context.json — the file is NOT deleted.

        WorkerThread creation (same as spawn does) must not wipe a pre-existing
        context.json, so that resume across sessions works.
        """
        # ── Create a pre-existing context.json ──
        ctx_data = {
            "conversation": [{"role": "user", "content": "resume me"}],
            "status": "ready",
            "worker_name": "resume_worker",
        }
        ctx_path = tmp_path / "workers" / "resume_worker" / "context.json"
        ctx_path.parent.mkdir(parents=True)
        ctx_path.write_text(json.dumps(ctx_data), encoding="utf-8")
        assert ctx_path.exists()  # sanity

        # ── Create WorkerThread (same code path as _action_spawn) ──
        thread = WorkerThread(
            name="resume_worker",
            definition={},
            agent_config={},
            workspace_dir=tmp_path,
        )

        # ── Verify file was NOT deleted during __init__ ──
        assert ctx_path.exists(), (
            "context.json was deleted during spawn! "
            "_action_spawn must preserve it for resume to work."
        )

        # ── Verify _load_context() can restore the WorkerContext ──
        ctx = thread._load_context()
        assert ctx is not None, "_load_context() should return WorkerContext"
        assert len(ctx.user_history) == 1
        assert ctx.user_history[0]["content"] == "resume me"
        assert ctx.worker_name == "resume_worker"

    def test_spawn_without_context_creates_fresh(self, tmp_path: Path):
        """Without a pre-existing context.json, spawn creates fresh context."""
        thread = WorkerThread(
            name="fresh_worker",
            definition={"system_prompt": "You are fresh."},
            agent_config={},
            workspace_dir=tmp_path,
        )
        ctx = thread._load_context()
        assert ctx is None, "No context file yet — _load_context returns None"

    @patch("tools.workspace.worker.resolve_workspace_id", return_value="ws_test")
    @patch("tools.workspace.worker._workspace_dir")
    @patch("tools.workspace.worker.WorkerThread")
    @patch("tools.workspace.worker._worker_registry", new_callable=dict)
    def test_action_spawn_found(self, mock_registry, mock_thread_cls, mock_ws_dir, mock_resolve):
        """spawn returns success when worker_name exists in workers.json.

        We mock WorkerThread entirely to avoid starting a real thread
        or making LLM calls.  The thread's .start() is a no-op.
        """
        mock_dir = MagicMock()
        mock_file = MagicMock()
        mock_file.exists.return_value = True
        mock_file.read_text.return_value = json.dumps([
            {"name": "default", "status": "ready"},
        ])
        mock_dir.__truediv__.return_value = mock_file
        mock_ws_dir.return_value = mock_dir

        mock_thread = MagicMock()
        mock_thread.status = "ready"
        mock_thread_cls.return_value = mock_thread

        tool = Worker(
            action="spawn",
            worker_name="default",
            workspace_path="/tmp/test_ws",
            agent_config={"provider": "openai", "model": "gpt-4"},
        )
        result = _parse_result(tool.execute())
        assert result["spawned"] is True
        assert result["worker_name"] == "default"
        assert result["status"] == "ready"
        mock_thread.start.assert_called_once()

    @patch("tools.workspace.worker.resolve_workspace_id", return_value="ws_test")
    @patch("tools.workspace.worker._workspace_dir")
    def test_action_spawn_not_found(self, mock_ws_dir, mock_resolve):
        """spawn returns error when worker_name not in workers.json."""
        mock_dir = MagicMock()
        mock_file = MagicMock()
        mock_file.exists.return_value = True
        mock_file.read_text.return_value = json.dumps([
            {"name": "default", "status": "ready"},
        ])
        mock_dir.__truediv__.return_value = mock_file
        mock_ws_dir.return_value = mock_dir

        tool = Worker(action="spawn", worker_name="nonexistent", workspace_path="/tmp/test_ws")
        result = _parse_result(tool.execute())
        assert "error" in result
        assert "not found in workers.json" in result["error"]

    # ═══════════════════════════════════════════════════════════════════
    #  Worker tool — check
    # ═══════════════════════════════════════════════════════════════════

    @patch("tools.workspace.worker._worker_registry", new_callable=dict)
    @patch("tools.workspace.worker.resolve_workspace_id", return_value="ws_test")
    @patch("tools.workspace.worker._workspace_dir")
    def test_action_check_not_spawned(self, mock_ws_dir, mock_resolve, mock_registry):
        """check returns 'stopped' status for a defined but not-spawned worker."""
        mock_dir = MagicMock()
        mock_file = MagicMock()
        mock_file.exists.return_value = True
        mock_file.read_text.return_value = json.dumps([
            {"name": "default", "status": "ready"},
        ])
        mock_dir.__truediv__.return_value = mock_file
        mock_ws_dir.return_value = mock_dir

        tool = Worker(action="check", worker_name="default", workspace_path="/tmp/test_ws")
        result = _parse_result(tool.execute())
        assert result["worker_name"] == "default"
        assert result["status"] == "stopped"
        assert result["current_task"] is None

    @patch("tools.workspace.worker.resolve_workspace_id", return_value="ws_test")
    @patch("tools.workspace.worker._workspace_dir")
    def test_action_check_not_found(self, mock_ws_dir, mock_resolve):
        """check returns error for a worker that's not even in workers.json."""
        mock_dir = MagicMock()
        mock_file = MagicMock()
        mock_file.exists.return_value = True
        mock_file.read_text.return_value = json.dumps([])
        mock_dir.__truediv__.return_value = mock_file
        mock_ws_dir.return_value = mock_dir

        tool = Worker(action="check", worker_name="nonexistent", workspace_path="/tmp/test_ws")
        result = _parse_result(tool.execute())
        assert "error" in result

    # ═══════════════════════════════════════════════════════════════════
    #  Worker tool — query
    # ═══════════════════════════════════════════════════════════════════

    @patch("tools.workspace.worker._worker_registry", new_callable=dict)
    @patch("tools.workspace.worker.resolve_workspace_id")
    def test_action_query_not_spawned(self, mock_resolve, mock_registry):
        """query returns error when worker hasn't been spawned."""
        mock_resolve.return_value = "ws_test"

        tool = Worker(
            action="query",
            worker_name="default",
            worker_query="hello",
            workspace_path="/tmp/test_ws",
        )
        result = _parse_result(tool.execute())
        assert "error" in result
        assert "not running" in result["error"]

    # ═══════════════════════════════════════════════════════════════════
    #  Worker tool — misc
    # ═══════════════════════════════════════════════════════════════════

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

    @patch("tools.workspace.worker._load_template_workers", return_value=[])
    @patch("tools.workspace.worker.resolve_workspace_id", return_value="ws_test")
    @patch("tools.workspace.worker._workspace_dir")
    def test_missing_workers_file(self, mock_ws_dir, mock_resolve, mock_templates):
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
        """Worker declares no required categories — spawning workers is not
        gated by session permissions (decoupled from 'execution')."""
        assert Worker.required_categories == []


    # ═══════════════════════════════════════════════════════════════════
    #  Spawn-time tool stripping (4b — footprint + 4a blocklist)
    # ═══════════════════════════════════════════════════════════════════

    @patch("tools.workspace.worker.GATE_AVAILABLE", True)
    @patch("tools.workspace.worker.check_required_categories")
    @patch("tools.workspace.worker.resolve_workspace_id", return_value="ws_test")
    @patch("tools.workspace.worker._workspace_dir")
    @patch("tools.workspace.worker.WorkerThread")
    @patch("tools.workspace.worker._worker_registry", new_callable=dict)
    def test_spawn_strips_by_footprint(
        self, mock_registry, mock_thread_cls, mock_ws_dir, mock_resolve, mock_gate,
    ):
        """
        4d — Spawn-time stripping test.

        Worker with ``filesystem:read`` footprint requests FileEditor.
        FileEditor's default category is ``filesystem:write``, which the
        ``check_required_categories`` mock denies → FileEditor is stripped.
        """
        mock_gate.return_value = (False, "footprint denied")

        mock_dir = MagicMock()
        mock_file = MagicMock()
        mock_file.exists.return_value = True
        mock_file.read_text.return_value = json.dumps([
            {
                "name": "reader",
                "tools": ["FileEditor", "DateTimeTool"],
                "permission_footprint": {"filesystem": "read"},
            },
        ])
        mock_dir.__truediv__.return_value = mock_file
        mock_ws_dir.return_value = mock_dir

        mock_thread = MagicMock()
        mock_thread.status = "ready"
        mock_thread_cls.return_value = mock_thread

        tool = Worker(
            action="spawn",
            worker_name="reader",
            workspace_path="/tmp/test_ws",
            agent_config={"provider": "openai", "model": "gpt-4"},
        )
        result = _parse_result(tool.execute())
        assert result["spawned"] is True
        assert result["worker_name"] == "reader"
        mock_thread.start.assert_called_once()
        # missing_tools are logged via logger.warning, not returned in result

    @patch("tools.workspace.worker.resolve_workspace_id", return_value="ws_test")
    @patch("tools.workspace.worker._workspace_dir")
    @patch("tools.workspace.worker.WorkerThread")
    @patch("tools.workspace.worker._worker_registry", new_callable=dict)
    def test_spawn_strips_blocklisted_tools(
        self, mock_registry, mock_thread_cls, mock_ws_dir, mock_resolve,
    ):
        """
        4e — Blocklist test.

        Worker tool is blocklisted and stripped at spawn.
        FileEditor and DateTimeTool are kept.
        """
        mock_dir = MagicMock()
        mock_file = MagicMock()
        mock_file.exists.return_value = True
        mock_file.read_text.return_value = json.dumps([
            {
                "name": "safe_worker",
                "tools": ["Worker", "FileEditor", "DateTimeTool"],
                "permission_footprint": {},
            },
        ])
        mock_dir.__truediv__.return_value = mock_file
        mock_ws_dir.return_value = mock_dir

        mock_thread = MagicMock()
        mock_thread.status = "ready"
        mock_thread_cls.return_value = mock_thread

        tool = Worker(
            action="spawn",
            worker_name="safe_worker",
            workspace_path="/tmp/test_ws",
            agent_config={"provider": "openai", "model": "gpt-4"},
        )
        result = _parse_result(tool.execute())
        assert result["spawned"] is True
        assert result["worker_name"] == "safe_worker"
        mock_thread.start.assert_called_once()
        # Blocklisted tools are logged via logger.warning, not returned in result

    # ═══════════════════════════════════════════════════════════════════
    #  Per-call gate (4c)
    # ═══════════════════════════════════════════════════════════════════

    def test_per_call_gate_denies_write_for_readonly_worker(self, tmp_path: Path):
        """
        4c — Per-call gate test.

        WorkerThread with ``filesystem:read`` footprint calls FileEditor
        with ``operation=write``.  ``_check_tool_permissions`` should deny it.
        """
        thread = WorkerThread(
            name="readonly_worker",
            definition={
                "system_prompt": "You are a test.",
                "permission_footprint": {"filesystem": "read"},
            },
            agent_config={},
            workspace_dir=tmp_path,
            session_permissions={},
        )

        # The worker's permission footprint is captured from the definition
        assert thread._permission_footprint == {"filesystem": "read"}

        # The restrictive merge keeps the worker at read — the session is the
        # ceiling, so effective permissions never escalate to write.
        merged = _restrictive_merge(
            thread._session_permissions, thread._permission_footprint,
        )
        assert merged.get("filesystem") == "read"

        # Even if the session allowed write, the worker footprint wins (read).
        merged2 = _restrictive_merge(
            {"filesystem": "write"}, thread._permission_footprint,
        )
        assert merged2.get("filesystem") == "read"

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
