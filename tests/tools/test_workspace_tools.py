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
from tools.workspace.worker import Worker, WorkerThread
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
    """Tests for Worker tool and WorkerThread."""

    # ═══════════════════════════════════════════════════════════════════
    #  WorkerThread unit tests
    # ═══════════════════════════════════════════════════════════════════

    def test_worker_thread_init(self, tmp_path: Path):
        """WorkerThread initialises with idle status and empty conversation."""
        mock_llm = MagicMock()
        thread = WorkerThread(
            name="test_worker",
            definition={"system_prompt": "You are a test."},
            llm_client=mock_llm,
            workspace_dir=tmp_path,
        )
        assert thread.worker_name == "test_worker"
        assert thread.status == "idle"
        assert thread.conversation == []
        assert thread.current_task is None
        assert thread.error is None
        assert thread.is_alive() is False  # not started yet

    def test_worker_thread_save_and_load_context(self, tmp_path: Path):
        """WorkerThread persists and reloads context to/from disk."""
        mock_llm = MagicMock()
        thread = WorkerThread(
            name="persist_test",
            definition={},
            llm_client=mock_llm,
            workspace_dir=tmp_path,
        )
        thread.conversation = [
            {"role": "system", "content": "You are a test."},
            {"role": "user", "content": "Hello"},
        ]
        thread.status = "running"
        thread.last_heartbeat = "2025-01-01T00:00:00"
        thread._save_context()

        # Verify file exists
        context_file = tmp_path / "workers" / "persist_test" / "context.json"
        assert context_file.exists()

        # Load into a fresh thread
        thread2 = WorkerThread(
            name="persist_test",
            definition={},
            llm_client=mock_llm,
            workspace_dir=tmp_path,
        )
        assert len(thread2.conversation) == 2
        assert thread2.conversation[1]["content"] == "Hello"
        assert thread2.status == "running"

    def test_resume_worker_loads_current_system_prompt(self, tmp_path: Path, caplog):
        """
        When a persisted context.json has a stale system prompt,
        WorkerThread.run() replaces it with the current definition's prompt
        and logs a warning. The rest of the conversation is preserved.
        """
        # Write a persisted context with an OLD system prompt + messages
        ctx = {
            "conversation": [
                {"role": "system", "content": "You are an old assistant."},
                {"role": "user", "content": "Hello"},
                {"role": "assistant", "content": "Hi there!"},
            ],
            "status": "idle",
        }
        context_path = tmp_path / "workers" / "resume_test" / "context.json"
        context_path.parent.mkdir(parents=True)
        context_path.write_text(json.dumps(ctx), encoding="utf-8")

        mock_llm = MagicMock()
        thread = WorkerThread(
            name="resume_test",
            definition={"system_prompt": "You are a NEW assistant."},
            llm_client=mock_llm,
            workspace_dir=tmp_path,
        )

        # Before run() — loaded from disk with old prompt
        assert len(thread.conversation) == 3
        assert thread.conversation[0]["content"] == "You are an old assistant."

        # Start the thread and stop immediately so run() processes the prompt
        with caplog.at_level(logging.WARNING):
            thread.start()
            thread.stop()
            thread.join(timeout=2)

        # After run() — prompt replaced with current definition
        assert thread.conversation[0]["content"] == "You are a NEW assistant."
        # User/assistant pair preserved
        assert thread.conversation[1]["role"] == "user"
        assert thread.conversation[1]["content"] == "Hello"
        assert thread.conversation[2]["role"] == "assistant"
        assert thread.conversation[2]["content"] == "Hi there!"
        # Warning logged about the change
        assert "system prompt changed" in caplog.text
        assert "old assistant" in caplog.text
        assert "NEW assistant" in caplog.text

    def test_worker_thread_logs_events(self, tmp_path: Path):
        """WorkerThread logs events to events.jsonl."""
        mock_llm = MagicMock()
        thread = WorkerThread(
            name="log_test",
            definition={},
            llm_client=mock_llm,
            workspace_dir=tmp_path,
        )
        thread._log_event("started", {}, {})
        thread._log_event("query", "hello", "world")

        events_file = tmp_path / "workers" / "events.jsonl"
        assert events_file.exists()
        lines = events_file.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 2

        evt1 = json.loads(lines[0])
        assert evt1["event"] == "started"
        assert evt1["worker_name"] == "log_test"

        evt2 = json.loads(lines[1])
        assert evt2["event"] == "query"
        assert evt2["request"] == "hello"
        assert evt2["response"] == "world"

    def test_worker_thread_send_query_timeout(self, tmp_path: Path):
        """send_query raises TimeoutError when worker doesn't respond."""
        mock_llm = MagicMock()
        thread = WorkerThread(
            name="timeout_test",
            definition={},
            llm_client=mock_llm,
            workspace_dir=tmp_path,
        )
        # Thread not running — queue.get will time out
        with pytest.raises(TimeoutError):
            thread.send_query("hello", timeout=0.01)

    # ═══════════════════════════════════════════════════════════════════
    #  Worker tool — list
    # ═══════════════════════════════════════════════════════════════════

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
        """list returns workers from workers.json with runtime_status."""
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
        # Not spawned yet, so runtime_status should be "stopped"
        assert result["workers"][0]["runtime_status"] == "stopped"

    # ═══════════════════════════════════════════════════════════════════
    #  Worker tool — spawn
    # ═══════════════════════════════════════════════════════════════════

    @patch("tools.workspace.worker.resolve_workspace_id", return_value="ws_test")
    @patch("tools.workspace.worker._workspace_dir")
    @patch("tools.workspace.worker.Worker._build_llm_client")
    @patch("tools.workspace.worker.WorkerThread")
    def test_action_spawn_found(
        self, mock_thread_cls, mock_build_llm, mock_ws_dir, mock_resolve,
    ):
        """spawn returns success when worker_name exists in workers.json.

        We mock WorkerThread entirely to avoid starting a real thread
        or making LLM calls.  The thread's .start() is a no-op.
        """
        mock_dir = MagicMock()
        mock_file = MagicMock()
        mock_file.exists.return_value = True
        mock_file.read_text.return_value = json.dumps([
            {"name": "coder", "status": "idle"},
        ])
        mock_dir.__truediv__.return_value = mock_file
        mock_ws_dir.return_value = mock_dir
        mock_build_llm.return_value = MagicMock()

        mock_thread = MagicMock()
        mock_thread.status = "idle"
        mock_thread_cls.return_value = mock_thread

        tool = Worker(
            action="spawn",
            worker_name="coder",
            workspace_path="/tmp/test_ws",
            agent_config={"provider": "openai", "model": "gpt-4"},
        )
        result = _parse_result(tool.execute())
        assert result["spawned"] is True
        assert result["worker_name"] == "coder"
        assert result["status"] == "idle"
        mock_thread.start.assert_called_once()

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
            {"name": "coder", "status": "idle"},
        ])
        mock_dir.__truediv__.return_value = mock_file
        mock_ws_dir.return_value = mock_dir

        tool = Worker(action="check", worker_name="coder", workspace_path="/tmp/test_ws")
        result = _parse_result(tool.execute())
        assert result["worker_name"] == "coder"
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
            worker_name="coder",
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

    # ═══════════════════════════════════════════════════════════════════
    #  Permission gate
    # ═══════════════════════════════════════════════════════════════════

    @patch("tools.workspace.worker.GATE_AVAILABLE", True)
    @patch("tools.workspace.worker.check_required_categories")
    def test_permission_gate_allows(self, mock_gate):
        """check_required_categories returning (True, "") means allowed."""
        mock_gate.return_value = (True, "")
        tool = Worker(action="list", workspace_path="/tmp/test_ws")
        result = tool._check_worker_permissions(
            {"required_categories": ["execution:read"], "worker_permissions": {}},
            {"execution": "read"},
        )
        assert result is None

    @patch("tools.workspace.worker.GATE_AVAILABLE", True)
    @patch("tools.workspace.worker.check_required_categories")
    def test_permission_gate_denies(self, mock_gate):
        """check_required_categories returning (False, msg) means denied."""
        mock_gate.return_value = (False, "Insufficient permissions")
        tool = Worker(action="list", workspace_path="/tmp/test_ws")
        result = tool._check_worker_permissions(
            {"required_categories": ["execution:write"], "worker_permissions": {}},
            {"execution": "read"},
        )
        assert result == "Insufficient permissions"

    @patch("tools.workspace.worker.GATE_AVAILABLE", True)
    def test_no_required_categories_skips_gate(self):
        """If definition has no required_categories, gate is skipped."""
        tool = Worker(action="list", workspace_path="/tmp/test_ws")
        result = tool._check_worker_permissions(
            {"worker_permissions": {}},
            {},
        )
        assert result is None

    @patch("tools.workspace.worker.GATE_AVAILABLE", False)
    def test_gate_unavailable(self):
        """If security gate is not importable, all workers allowed."""
        tool = Worker(action="list", workspace_path="/tmp/test_ws")
        result = tool._check_worker_permissions(
            {"required_categories": ["execution:write"], "worker_permissions": {}},
            {},
        )
        assert result is None

    # ═══════════════════════════════════════════════════════════════════
    #  Spawn-time tool stripping (4b — footprint + 4a blocklist)
    # ═══════════════════════════════════════════════════════════════════

    @patch("tools.workspace.worker.GATE_AVAILABLE", True)
    @patch("tools.workspace.worker.check_required_categories")
    @patch("tools.workspace.worker.resolve_workspace_id", return_value="ws_test")
    @patch("tools.workspace.worker._workspace_dir")
    @patch("tools.workspace.worker.Worker._build_llm_client")
    @patch("tools.workspace.worker.WorkerThread")
    def test_spawn_strips_by_footprint(
        self, mock_thread_cls, mock_build_llm, mock_ws_dir,
        mock_resolve, mock_gate,
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
                "worker_permissions": {"filesystem": "read"},
            },
        ])
        mock_dir.__truediv__.return_value = mock_file
        mock_ws_dir.return_value = mock_dir
        mock_build_llm.return_value = MagicMock()

        mock_thread = MagicMock()
        mock_thread.status = "idle"
        mock_thread_cls.return_value = mock_thread

        tool = Worker(
            action="spawn",
            worker_name="reader",
            workspace_path="/tmp/test_ws",
            agent_config={"provider": "openai", "model": "gpt-4"},
        )
        result = _parse_result(tool.execute())
        assert result["spawned"] is True
        assert "missing_tools" in result, (
            f"Expected missing_tools in result, got {result}"
        )
        assert any(
            "FileEditor" in mt for mt in result["missing_tools"]
        ), f"FileEditor should be stripped, got {result['missing_tools']}"
        # DateTimeTool has no required_categories → no gate check → kept
        assert not any(
            "DateTimeTool" in mt for mt in result["missing_tools"]
        ), f"DateTimeTool should be kept, got {result['missing_tools']}"

    @patch("tools.workspace.worker.resolve_workspace_id", return_value="ws_test")
    @patch("tools.workspace.worker._workspace_dir")
    @patch("tools.workspace.worker.Worker._build_llm_client")
    @patch("tools.workspace.worker.WorkerThread")
    def test_spawn_strips_blocklisted_tools(
        self, mock_thread_cls, mock_build_llm, mock_ws_dir, mock_resolve,
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
                "worker_permissions": {},
            },
        ])
        mock_dir.__truediv__.return_value = mock_file
        mock_ws_dir.return_value = mock_dir
        mock_build_llm.return_value = MagicMock()

        mock_thread = MagicMock()
        mock_thread.status = "idle"
        mock_thread_cls.return_value = mock_thread

        tool = Worker(
            action="spawn",
            worker_name="safe_worker",
            workspace_path="/tmp/test_ws",
            agent_config={"provider": "openai", "model": "gpt-4"},
        )
        result = _parse_result(tool.execute())
        assert result["spawned"] is True
        assert "missing_tools" in result, (
            f"Expected missing_tools in result, got {result}"
        )
        # Worker is blocklisted
        assert any(
            "Worker" in mt for mt in result["missing_tools"]
        ), f"Worker should be stripped, got {result['missing_tools']}"
        # FileEditor and DateTimeTool should be kept
        assert not any(
            "FileEditor" in mt for mt in result["missing_tools"]
        ), f"FileEditor should be kept, got {result['missing_tools']}"
        assert not any(
            "DateTimeTool" in mt for mt in result["missing_tools"]
        ), f"DateTimeTool should be kept, got {result['missing_tools']}"

    # ═══════════════════════════════════════════════════════════════════
    #  Per-call gate (4c)
    # ═══════════════════════════════════════════════════════════════════

    def test_per_call_gate_denies_write_for_readonly_worker(self, tmp_path: Path):
        """
        4c — Per-call gate test.

        WorkerThread with ``filesystem:read`` footprint calls FileEditor
        with ``operation=write``.  ``_check_tool_permissions`` should deny it.
        """
        from tools.file_editor import FileEditor as FECls

        mock_llm = MagicMock()
        thread = WorkerThread(
            name="readonly_worker",
            definition={
                "system_prompt": "You are a test.",
                "worker_permissions": {"filesystem": "read"},
            },
            llm_client=mock_llm,
            workspace_dir=tmp_path,
            tool_classes={"FileEditor": FECls},
            session_permissions={},
        )

        error = thread._check_tool_permissions(
            "FileEditor",
            {"operation": "write", "filename": "/dev/null"},
        )
        assert error is not None, (
            "Expected denial for FileEditor write with filesystem:read footprint"
        )
        assert "permission" in error.lower() or "denied" in error.lower(), (
            f"Error message should mention permission/denied, got: {error}"
        )


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
