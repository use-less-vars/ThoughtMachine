"""
Tests for workspace introspection / management tools.

Covers
------
- CheckSystem: all 5 query types (effective_permissions, container_status,
  workspace_info, my_config, network_diagnostics) plus unknown query.
- Worker:     list, spawn, check, query, missing worker_name, unknown action,
              missing workers.json.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from tools.workspace.check_system import CheckSystem
from tools.workspace.worker import Worker, WorkerThread, _restrictive_merge
from agent.core.worker_context import WorkerContext

# ══════════════════════════════════════════════════════════════════════════════
#  Helpers
# ══════════════════════════════════════════════════════════════════════════════

def _parse_result(result: str) -> dict:
    """Parse JSON tool result into a dict."""
    return json.loads(result)

# ══════════════════════════════════════════════════════════════════════════════
#  CheckSystem
# ══════════════════════════════════════════════════════════════════════════════

# Full set of queries CheckSystem can answer (mirrors the vault allowlist in
# tests/security/test_checksystem_allowlist.py).  Pinned here so these tests are
# deterministic regardless of the host's ~/.thoughtmachine vault state.
FULL_ALLOWLIST = [
    "capabilities",
    "container_status",
    "dockerfile",
    "effective_permissions",
    "event_bus_status",
    "event_log",
    "mcp_servers",
    "my_config",
    "network_diagnostics",
    "running_workers",
    "workers",
    "workspace_info",
]


class TestCheckSystem:
    """Tests for CheckSystem (all 5 query types + unknown)."""

    @pytest.fixture(autouse=True)
    def _pin_vault_allowlist(self):
        """Pin the vault allowlist so query handlers are reached deterministically.

        execute() reloads the allowlist from the vault on every call.  On hosts
        with no bootstrapped vault (e.g. the container verification env) the
        allowlist is EMPTY, and CheckSystem fail-closes by denying EVERY query,
        which would fail every test here.  Patching the loader to the full
        allowlist exercises the real product flow (allowlist enforcement for
        valid queries + query handlers) independent of the host vault state.
        """
        with patch.object(
            CheckSystem, "_load_allowlist_from_vault", return_value=FULL_ALLOWLIST
        ):
            yield

    @pytest.fixture(autouse=True)
    def _pin_docker_guards(self):
        """Pin the Docker-availability guards so this class is order-independent.
        DOCKER_EXECUTOR_AVAILABLE / CONTAINER_MANAGER_AVAILABLE / DockerExecutor
        are import-time constants in tools/workspace/check_system.py.  Under the
        full host suite, an earlier-collected test's module-level import of the
        ``tools`` package can leave agent.logging mid-import (the circular-import
        cascade documented in tests/docker/test_container_lifecycle.py), so the
        ``from docker_executor import ...`` / ``from infra.container_manager
        import ...`` inside check_system raise ImportError when this file is
        collected and all three guards land False.  No test in this class ever
        patches these names, so pinning them makes every test here deterministic
        regardless of collection order (mirrors _pin_vault_allowlist).
        """
        import tools.workspace.check_system as _check_system_mod
        executor_cls = getattr(_check_system_mod, "DockerExecutor", None)
        if executor_cls is None:
            # check_system's import-time docker_executor import failed.  These
            # tests only truthiness-check DockerExecutor (the ContainerManager is
            # always mocked), so a stand-in class keeps the daemon branch
            # reachable.
            class _DockerExecutorStub:
                pass
            executor_cls = _DockerExecutorStub
        with patch.object(_check_system_mod, "DOCKER_EXECUTOR_AVAILABLE", True), \
             patch.object(_check_system_mod, "CONTAINER_MANAGER_AVAILABLE", True), \
             patch.object(_check_system_mod, "DockerExecutor", executor_cls):
            yield

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
        """Unknown query is denied by the vault allowlist (fail-closed)."""
        tool = CheckSystem(query="nonexistent_query", workspace_path="/tmp/test_ws")
        result = _parse_result(tool.execute())
        assert "error" in result
        assert "nonexistent_query" in result["error"]
        assert result["status"] == "denied"

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

    def test_unknown_query_denied_no_valid_queries(self):
        """Unknown query denied when allowlist active: no valid_queries leak."""
        tool = CheckSystem(query="nonexistent", workspace_path="/tmp/test_ws")
        result = json.loads(tool.execute())
        assert "error" in result
        assert result["status"] == "denied"
        assert "valid_queries" not in result

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

    def test_resume_worker_loads_current_system_prompt(self, tmp_path: Path, monkeypatch):
        """
        Resuming a worker preserves the persisted conversation.

        WorkerThread.run() keeps the loaded conversation intact — the current
        definition's system prompt is applied later when the worker's Agent is
        created (agent.core.agent.Agent.__init__ → ensure_system_prompt).
        Whether that prompt reaches the LLM is governed by the worker_mode
        guard in _apply_mode_system_prompt — see the comment at the
        _build_agent_config() call below.
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
        #
        # worker_mode contract: _build_agent_config() marks the worker's
        # AgentConfig with worker_mode=True, and _apply_mode_system_prompt
        # (agent/config/models.py) early-returns for worker_mode configs — so
        # the definition's explicit system_prompt survives even though the
        # agent_config dict above has no "mode" key (mode defaults to 'agent').
        # The mode factory prompt / tool-preset stomp applies to non-worker
        # configs only.
        #
        # monkeypatch home first so the field-validator fallback is
        # deterministic (~/.thoughtmachine/custom_system_prompt.txt neutralized).
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
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
        # worker_mode=True → the definition's system_prompt survives the
        # mode-factory stomp (mode still defaults to 'agent').
        assert cfg.worker_mode is True
        assert cfg.mode == "agent"
        assert cfg.system_prompt == "You are a NEW assistant."

        # Same guarantee under an explicit mode == 'custom' parent config
        custom_thread = WorkerThread(
            name="resume_test_custom",
            definition={"system_prompt": "You are a NEW assistant."},
            agent_config={
                "mode": "custom",
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
        custom_cfg = custom_thread._build_agent_config()
        assert custom_cfg is not None
        assert custom_cfg.mode == "custom"
        assert custom_cfg.system_prompt == "You are a NEW assistant."

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

    def test_worker_send_query_stale_reply_discarded(self, tmp_path: Path):
        """Stale envelopes pre-seeded in _output_queue must not satisfy a
        send_query call — with the per-caller reply channel the fresh reply
        arrives on the caller's private queue (and stale items are drained)."""
        thread = WorkerThread(
            name="stale_test",
            definition={},
            agent_config={},
            workspace_dir=tmp_path,
        )
        # Stale replies left over from a previous (timed-out / superseded)
        # query — exactly what the per-caller reply channel must bypass.
        thread._output_queue.put(json.dumps(
            {"content": "STALE-OLD", "status": "completed", "query_id": "old-id"}))
        thread._output_queue.put(json.dumps(
            {"content": "STALE-NOID", "status": "completed"}))

        captured = {}

        def fake_put(item):
            captured["item"] = item
            qid, q = item[0], item[1]
            payload = json.dumps(
                {"content": f"FRESH-{q}", "status": "completed", "query_id": qid})
            if len(item) == 3:
                # Retrofit: send_query carries a private reply_q — deliver there.
                item[2].put(payload)
            else:
                thread._output_queue.put(payload)

        with patch.object(thread._input_queue, "put", side_effect=fake_put):
            response = thread.send_query("hi", timeout=2.0)

        payload = json.loads(response)
        assert payload["content"] == "FRESH-hi"
        assert payload["query_id"] == captured["item"][0]

    def test_worker_legacy_output_queue_drains_after_timeout(self, tmp_path: Path):
        """A reply landing after send_query timed out must never satisfy the
        next send_query call: the timeout path drains the shared output queue
        and each call owns a private reply queue."""
        thread = WorkerThread(
            name="legacy_drain_test",
            definition={},
            agent_config={},
            workspace_dir=tmp_path,
        )

        def fake_put_slow(item):
            qid = item[0]

            def deliver():
                payload = json.dumps({"content": "LATE", "query_id": qid})
                if len(item) == 3:
                    item[2].put(payload)
                else:
                    thread._output_queue.put(payload)

            threading.Timer(0.3, deliver).start()

        with patch.object(thread._input_queue, "put", side_effect=fake_put_slow):
            with pytest.raises(TimeoutError):
                thread.send_query("slow", timeout=0.05)

        # Nothing pending immediately after the timeout...
        assert thread._output_queue.empty()
        # ... and even after the late reply would have landed (0.3s timer),
        # it must have been drained / routed to the orphaned private queue.
        time.sleep(0.5)
        assert thread._output_queue.empty()

        def fake_put_fast(item):
            qid, q = item[0], item[1]
            payload = json.dumps(
                {"content": f"FRESH-{q}", "status": "completed", "query_id": qid})
            if len(item) == 3:
                item[2].put(payload)
            else:
                thread._output_queue.put(payload)

        with patch.object(thread._input_queue, "put", side_effect=fake_put_fast):
            response = thread.send_query("next", timeout=2.0)

        payload = json.loads(response)
        assert payload["content"] == "FRESH-next"
        assert payload["content"] != "LATE"

    def test_worker_overlapping_send_query_no_crosstalk(self, tmp_path: Path):
        """Two concurrent send_query callers each get their own reply: the
        per-caller reply queue carried in the (query_id, query, reply_q)
        input item prevents reply stealing via the shared output queue."""
        thread = WorkerThread(
            name="overlap_test",
            definition={},
            agent_config={},
            workspace_dir=tmp_path,
        )
        results = {}
        errors = {}

        def caller(name):
            try:
                results[name] = thread.send_query(name, timeout=5.0)
            except Exception as exc:  # pragma: no cover - surfaced below
                errors[name] = exc

        ta = threading.Thread(target=caller, args=("q-A",))
        tb = threading.Thread(target=caller, args=("q-B",))
        ta.daemon = True
        tb.daemon = True

        def fake_worker():
            # Consume both (query_id, query, reply_q) items, then answer each
            # on ITS private reply queue.
            items = []
            for _ in range(2):
                items.append(thread._input_queue.get(timeout=6.0))
            for item in items:
                qid, q = item[0], item[1]
                time.sleep(0.2)
                payload = json.dumps(
                    {"content": f"reply-{q}", "status": "completed", "query_id": qid})
                item[2].put(payload)

        fake = threading.Thread(target=fake_worker)
        fake.daemon = True
        fake.start()
        ta.start()
        tb.start()
        fake.join(timeout=6.0)
        ta.join(timeout=6.0)
        tb.join(timeout=6.0)

        assert not fake.is_alive()
        assert not ta.is_alive()
        assert not tb.is_alive()
        assert errors == {}
        for x in ("q-A", "q-B"):
            assert json.loads(results[x])["content"] == f"reply-{x}"

    # ═══════════════════════════════════════════════════════════════════
    #  Worker tool — list
    # ═══════════════════════════════════════════════════════════════════

    @patch("tools.workspace.worker.resolve_workspace_id", return_value="ws_test")
    @patch("tools.workspace.worker._workspace_dir")
    def test_action_list_empty(self, mock_ws_dir, mock_resolve):
        """list falls back to the seeded template worker when workers.json is empty list."""
        mock_dir = MagicMock()
        mock_file = MagicMock()
        mock_file.exists.return_value = True
        mock_file.read_text.return_value = json.dumps([])
        mock_dir.__truediv__.return_value = mock_file
        mock_ws_dir.return_value = mock_dir

        tool = Worker(action="list", workspace_path="/tmp/test_ws")
        result = _parse_result(tool.execute())
        # Product behavior: an empty workers.json falls back to template
        # workers, so the seeded 'default' worker is listed.
        assert result["count"] == 1
        assert result["workers"][0]["name"] == "default"

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

    @patch("tools.workspace.worker.resolve_workspace_id", return_value="ws_test")
    @patch("tools.workspace.worker._workspace_dir")
    def test_missing_workers_file(self, mock_ws_dir, mock_resolve):
        """list falls back to the seeded template worker when workers.json doesn't exist."""
        mock_dir = MagicMock()
        mock_file = MagicMock()
        mock_file.exists.return_value = False  # File doesn't exist
        mock_dir.__truediv__.return_value = mock_file
        mock_ws_dir.return_value = mock_dir

        tool = Worker(action="list", workspace_path="/tmp/test_ws")
        result = _parse_result(tool.execute())
        # Product behavior: a missing workers.json falls back to template
        # workers, so the seeded 'default' worker is listed.
        assert result["count"] == 1
        assert result["workers"][0]["name"] == "default"

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
            # NOTE: spawn is fail-closed without an explicit session
            # permission scope; the worker's footprint
            # {"filesystem": "read"} must be within the session's allowed
            # permissions.
            session_permissions={"filesystem": "read"},
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
#  EditDockerfile removal guard
# ══════════════════════════════════════════════════════════════════════════════

class TestEditDockerfileRemoved:
    """EditDockerfile was removed: no registered tool class, no module, and no
    worker blocklist entry — the vestigial container-config tool is gone."""

    def test_not_in_registered_tool_classes(self):
        import tools
        names = {getattr(t, "tool", t.__name__) for t in tools.TOOL_CLASSES}
        assert "EditDockerfile" not in names

    def test_module_absent(self):
        import importlib.util
        assert importlib.util.find_spec("tools.workspace.edit_dockerfile") is None

    def test_not_in_worker_blocklist(self):
        from tools.workspace.worker import _WORKER_BLOCKLIST
        assert "EditDockerfile" not in _WORKER_BLOCKLIST

