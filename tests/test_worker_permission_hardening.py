"""
Worker config + permission hardening (2C) contracts.

The session permission profile is the CEILING for any worker spawned by that
session.  This suite pins the hardening contracts:

A. ``_restrictive_merge`` (tools/workspace/worker.py): the stricter value wins
   per category; a category the session does NOT expose is resolved to the
   fail-closed safe default (``SAFE_DEFAULTS``) -- a worker can never fill it
   in with its own footprint value.
B. Fail-closed spawn guard in ``Worker._action_spawn``: a worker definition
   whose ``permission_footprint``/``worker_permissions`` requests a category
   absent from the session profile is rejected outright.
C. Temperature is worker-scoped: the definition's ``temperature`` wins, else
   ``WORKER_DEFAULT_TEMPERATURE`` (0.7); the parent agent's temperature is
   never inherited.
D. Worker ``AgentConfig.session_permissions`` = restrictive merge of session
   and footprint (``WorkerThread._build_agent_config``).
E. SessionPermission model round-trip (model_dump / JSON file / factory
   defaults backfill), network='banned' default, default policy 'deny'.
F. ToolExecutor enforces session permissions (deny / hot-swap).
G. 'ask' permission flow defers to the outer gate (approve / deny / cancel).
H. GitInfoTool routing: 'ask' defers, 'banned' denies (fail-closed), for both
   host and container paths.
I. Bridge apply_config / save / load round-trips session_permissions.
J. Global-defaults worker config allowlist (only the six known keys persist;
   absent keys fall back to constructor defaults).
K. Per-session worker spawn cap (``max_workers``) safe default 5.

Harnesses mirror the existing suites: tests/test_worker_max_workers.py
(registry snapshot + patched workspace plumbing), tests/test_ask_permission.py
(ask flow), tests/test_permission_routing_fix.py (FakeSandboxExecution),
tests/test_permissions_roundtrip.py (cancel prompts), tests/test_global_defaults.py
(hermetic_vault), tests/test_bridge_permissions_sync.py (WebAgentBridge).
"""

from __future__ import annotations

import json
import os
import queue
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar, Dict, List
from unittest import mock
from unittest.mock import MagicMock

import pytest

import thoughtmachine.security as _security_module
from agent.events import EventBus, create_event as _create_event
from thoughtmachine.security import (
    SAFE_DEFAULTS,
    SessionPermissions,
    cancel_pending_prompts,
    get_default_security_config,
    is_allowed,
    merge_security_config,
    resolve_security_prompt,
    _pending_security_requests,
    _pending_requests_lock,
    _prompt_cancelled,
)

from tools.workspace.worker import (
    WORKER_DEFAULT_TEMPERATURE,
    Worker,
    WorkerThread,
    _load_safe_defaults,
    _restrictive_merge,
)

from agent.config.loader import load_config, save_config
from agent.config.models import AgentConfig
from agent.core.tool_executor import ToolExecutor
from agent.core.state import AgentState
from agent.models.worker_definition import WorkerDefinition
from tools.base import ToolBase
from tools.file_preview_tool import FilePreviewTool
from tools.git_info_tool import GitInfoTool
from web_ui.backend.bridge import WebAgentBridge
from session.store import FileSystemSessionStore


# =========================================================================
# Stub tools (mirror tests/test_permissions_roundtrip.py / test_ask_permission.py)
# =========================================================================

class FileWriteTool(ToolBase):
    """A tool that requires filesystem:write."""
    tool: str = "FileWriteTool"
    required_categories: ClassVar[List[str]] = ["filesystem:write"]

    def execute(self) -> str:
        return "Write OK"


class GitReadTool(ToolBase):
    """A tool that requires git:read."""
    tool: str = "GitReadTool"
    required_categories: ClassVar[List[str]] = ["git:read"]

    def execute(self) -> str:
        return "Git read OK"


class GitWriteTool(ToolBase):
    """A tool that requires git:write."""
    tool: str = "GitWriteTool"
    required_categories: ClassVar[List[str]] = ["git:write"]

    def execute(self) -> str:
        return "Git write OK"


class MultiRequirementTool(ToolBase):
    """A tool that requires multiple categories."""
    tool: str = "MultiRequirementTool"
    required_categories: ClassVar[List[str]] = [
        "container:true", "filesystem:write", "network:true",
    ]

    def execute(self) -> str:
        return "Multi OK"


# =========================================================================
# A. _restrictive_merge -- session ceiling, fail-closed on missing categories
# =========================================================================

class TestRestrictiveMerge:
    """Session permissions are the ceiling; the stricter value wins."""

    def test_session_ceiling_wins_when_session_stricter(self):
        result = _restrictive_merge({"execution": "deny"}, {"execution": "allow"})
        assert result["execution"] == "deny"

    def test_session_ceiling_wins_when_worker_stricter(self):
        result = _restrictive_merge({"execution": "allow"}, {"execution": "deny"})
        assert result["execution"] == "deny"

    def test_filesystem_levels_stricter_wins(self):
        """Ordering: none < read < write -- lower strictness never survives."""
        assert (
            _restrictive_merge({"filesystem": "none"}, {"filesystem": "write"})["filesystem"]
            == "none"
        )
        assert (
            _restrictive_merge({"filesystem": "read"}, {"filesystem": "write"})["filesystem"]
            == "read"
        )
        assert (
            _restrictive_merge({"filesystem": "write"}, {"filesystem": "read"})["filesystem"]
            == "read"
        )

    def test_boolean_false_wins(self):
        """For bool categories, False (deny) is stricter than True (allow)."""
        assert _restrictive_merge({"container": True}, {"container": True})["container"] is True
        assert _restrictive_merge({"container": False}, {"container": True})["container"] is False
        assert _restrictive_merge({"container": True}, {"container": False})["container"] is False

    def test_worker_cannot_fill_missing_category(self):
        """Session missing a category -> fail-closed safe default, NOT worker value."""
        result = _restrictive_merge({"filesystem": "read"}, {"network": "write"})
        assert result["network"] == SAFE_DEFAULTS["network"] == "banned"

    def test_session_only_categories_kept(self):
        """Categories only the session exposes survive untouched."""
        result = _restrictive_merge(
            {"filesystem": "read", "git": "read"}, {"filesystem": "write"}
        )
        assert result["git"] == "read"
        assert result["filesystem"] == "read"

    def test_tie_prefers_session_value(self):
        assert _restrictive_merge({"filesystem": "read"}, {"filesystem": "read"})["filesystem"] == "read"

    def test_load_safe_defaults_matches_module_constant(self):
        """The lazy loader returns the same SAFE_DEFAULTS map (cached identity)."""
        assert _load_safe_defaults() == SAFE_DEFAULTS
        assert _load_safe_defaults() is SAFE_DEFAULTS


# =========================================================================
# D/C. WorkerThread._build_agent_config -- merged perms + worker-scoped temp
# =========================================================================

def _build_worker_config(
    tmp_path,
    *,
    definition=None,
    session_permissions=None,
    agent_config=None,
):
    """Construct a WorkerThread and build its AgentConfig (transplant harness)."""
    thread = WorkerThread(
        name="hw-worker",
        definition=definition or {},
        agent_config=agent_config
        or {"provider": "openai", "model": "gpt-4o"},
        workspace_dir=tmp_path,
        session_permissions=session_permissions or {},
    )
    cfg = thread._build_agent_config()
    assert cfg is not None, "AgentConfig unavailable"
    return cfg


class TestWorkerThreadPermissionMerge:
    """Worker AgentConfig.session_permissions = restrictive merge(session, footprint)."""

    def test_footprint_restricts_session(self, tmp_path):
        cfg = _build_worker_config(
            tmp_path,
            definition={
                "system_prompt": "x", "tools": [],
                "permission_footprint": {"filesystem": "read"},
            },
            session_permissions={"filesystem": "write"},
        )
        assert cfg.session_permissions.filesystem == "read"

    def test_session_ceiling_holds(self, tmp_path):
        cfg = _build_worker_config(
            tmp_path,
            definition={
                "system_prompt": "x", "tools": [],
                "permission_footprint": {"filesystem": "write"},
            },
            session_permissions={"filesystem": "read"},
        )
        assert cfg.session_permissions.filesystem == "read"

    def test_missing_category_falls_back_to_safe_default(self, tmp_path):
        """Session does not expose network -> worker cfg gets SAFE_DEFAULTS['network']."""
        cfg = _build_worker_config(
            tmp_path,
            definition={
                "system_prompt": "x", "tools": [],
                "permission_footprint": {"network": "write"},
            },
            session_permissions={"filesystem": "read"},
        )
        assert cfg.session_permissions.network == "banned"
        assert cfg.session_permissions.filesystem == "read"

    def test_worker_permissions_alias_key(self, tmp_path):
        """The legacy 'worker_permissions' definition key is honoured too."""
        cfg = _build_worker_config(
            tmp_path,
            definition={
                "system_prompt": "x", "tools": [],
                "worker_permissions": {"filesystem": "read"},
            },
            session_permissions={"filesystem": "write"},
        )
        assert cfg.session_permissions.filesystem == "read"


class TestTemperatureContract:
    """Worker temperature: definition wins, else 0.7; parent temp never inherited."""

    def test_definition_temperature_used(self, tmp_path):
        cfg = _build_worker_config(
            tmp_path,
            definition={"system_prompt": "x", "tools": [], "temperature": 0.3},
            agent_config={"provider": "openai", "model": "gpt-4o", "temperature": 1.0},
        )
        assert cfg.temperature == 0.3

    def test_default_temperature_when_definition_silent(self, tmp_path):
        cfg = _build_worker_config(
            tmp_path,
            definition={"system_prompt": "x", "tools": []},
            agent_config={"provider": "openai", "model": "gpt-4o", "temperature": 0.3},
        )
        assert cfg.temperature == WORKER_DEFAULT_TEMPERATURE == 0.7

    def test_never_inherits_parent_temperature(self, tmp_path):
        """Parent temp 0.3 + silent definition -> 0.7, never 0.3."""
        cfg = _build_worker_config(
            tmp_path,
            definition={"system_prompt": "x", "tools": []},
            agent_config={"provider": "openai", "model": "gpt-4o", "temperature": 0.3},
        )
        assert cfg.temperature == 0.7
        assert cfg.temperature != 0.3


# =========================================================================
# B/K. Fail-closed spawn guard + per-session spawn cap (Worker tool)
# =========================================================================

SESSION_H = "sess-hardening"


@pytest.fixture
def spawn_harness(monkeypatch):
    """Registry snapshot + patched workspace plumbing (max-workers harness)."""
    from tools.workspace.worker_registry import WorkerRegistry

    registry = WorkerRegistry.get_instance()
    with registry._registry_lock:
        old_registry = dict(registry._worker_registry)
        registry._worker_registry.clear()

    monkeypatch.setattr(
        "tools.workspace.worker.resolve_workspace_id",
        lambda ws: "ws_hardening",
    )
    mock_dir = MagicMock()
    mock_file = MagicMock()
    mock_file.exists.return_value = True
    mock_dir.__truediv__.return_value = mock_file
    monkeypatch.setattr("tools.workspace.worker._workspace_dir", lambda ws: mock_dir)
    thread_cls = MagicMock()
    monkeypatch.setattr("tools.workspace.worker.WorkerThread", thread_cls)

    harness = SimpleNamespace(
        registry=registry,
        mock_dir=mock_dir,
        mock_file=mock_file,
        thread_cls=thread_cls,
    )

    def set_definitions(defs):
        mock_file.read_text.return_value = json.dumps(defs)

    harness.set_definitions = set_definitions
    harness.set_definitions([{"name": "hw1", "status": "ready", "tools": []}])

    yield harness

    with registry._registry_lock:
        registry._worker_registry.clear()
        registry._worker_registry.update(old_registry)


def _spawn_worker(
    harness,
    name="hw1",
    session=SESSION_H,
    agent_config=None,
    session_permissions=None,
    force=False,
) -> dict:
    tool = Worker(
        action="spawn",
        worker_name=name,
        session_id=session,
        workspace_path="/tmp/test_ws",
        agent_config=agent_config or {"provider": "openai", "model": "gpt-4o"},
        session_permissions=session_permissions,
        force=force,
    )
    return json.loads(tool.execute())


class TestFailClosedSpawnGuard:
    """A footprint may only use categories the session explicitly exposes."""

    def test_footprint_category_not_exposed_rejected(self, spawn_harness):
        spawn_harness.set_definitions([
            {
                "name": "hw1", "status": "ready", "tools": [],
                "permission_footprint": {"network": "write"},
            }
        ])
        result = _spawn_worker(
            spawn_harness, session_permissions={"filesystem": "read"}
        )
        assert "error" in result
        assert "fail-closed" in result["error"]
        assert "network" in result["error"]
        assert result.get("worker_name") == "hw1"
        spawn_harness.thread_cls.assert_not_called()

    def test_worker_permissions_alias_also_guarded(self, spawn_harness):
        spawn_harness.set_definitions([
            {
                "name": "hw1", "status": "ready", "tools": [],
                "worker_permissions": {"git": "write"},
            }
        ])
        result = _spawn_worker(
            spawn_harness, session_permissions={"filesystem": "read"}
        )
        assert "error" in result
        assert "fail-closed" in result["error"]
        assert "git" in result["error"]

    def test_no_session_permissions_fail_closed(self, spawn_harness):
        """No session profile injected -> {} -> any footprint is denied."""
        spawn_harness.set_definitions([
            {
                "name": "hw1", "status": "ready", "tools": [],
                "permission_footprint": {"filesystem": "read"},
            }
        ])
        result = _spawn_worker(spawn_harness, session_permissions=None)
        assert "error" in result
        assert "fail-closed" in result["error"]

    def test_exposed_category_footprint_allowed(self, spawn_harness):
        spawn_harness.set_definitions([
            {
                "name": "hw1", "status": "ready", "tools": [],
                "permission_footprint": {"filesystem": "read"},
            }
        ])
        result = _spawn_worker(
            spawn_harness,
            session_permissions={"filesystem": "read", "network": "banned"},
        )
        assert result.get("spawned") is True
        spawn_harness.thread_cls.assert_called_once()


class TestMaxWorkersCap:
    """Per-session live-worker cap: safe default 5, config key lowers/raises."""

    def _seed(self, harness, names, session=SESSION_H, alive=True):
        with harness.registry._registry_lock:
            for name in names:
                t = MagicMock()
                t.is_alive.return_value = alive
                t.status = "ready" if alive else "stopped"
                t._timeout_seconds = 30
                harness.registry._worker_registry[(session, name, 1)] = t

    def test_default_cap_refuses_sixth(self, spawn_harness):
        spawn_harness.set_definitions([
            {"name": f"w{i}", "status": "ready", "tools": []} for i in range(1, 7)
        ])
        for i in range(1, 6):
            result = _spawn_worker(spawn_harness, f"w{i}")
            assert result.get("spawned"), f"spawn w{i} should succeed below cap: {result}"
        result = _spawn_worker(spawn_harness, "w6")
        assert "error" in result
        assert "limit" in result["error"].lower()
        assert result.get("max_workers") == 5
        assert result.get("live_workers") == 5
        # w1..w5 created threads; the refused w6 must NOT create a 6th.
        assert spawn_harness.thread_cls.call_count == 5

    def test_dead_workers_do_not_count_toward_cap(self, spawn_harness):
        spawn_harness.set_definitions([
            {"name": f"w{i}", "status": "ready", "tools": []} for i in range(1, 7)
        ])
        self._seed(spawn_harness, ["w1", "w2", "w3", "w4"], alive=True)
        self._seed(spawn_harness, ["w5"], alive=False)
        result = _spawn_worker(spawn_harness, "w6")
        assert result.get("spawned"), f"4 live < cap 5: {result}"

    def test_session_config_key_lowers_cap(self, spawn_harness):
        cfg = {
            "provider": "openai",
            "model": "gpt-4o",
            "session_config": {"max_workers": 2},
        }
        spawn_harness.set_definitions([
            {"name": f"w{i}", "status": "ready", "tools": []} for i in range(1, 4)
        ])
        assert _spawn_worker(spawn_harness, "w1", agent_config=cfg).get("spawned")
        assert _spawn_worker(spawn_harness, "w2", agent_config=cfg).get("spawned")
        result = _spawn_worker(spawn_harness, "w3", agent_config=cfg)
        assert "error" in result
        assert result.get("max_workers") == 2

    def test_force_replace_does_not_count_as_new_spawn(self, spawn_harness):
        spawn_harness.set_definitions([
            {"name": f"w{i}", "status": "ready", "tools": []} for i in range(1, 6)
        ])
        self._seed(spawn_harness, ["w1", "w2", "w3", "w4", "w5"])
        result = _spawn_worker(spawn_harness, "w3", force=True)
        assert result.get("spawned"), f"force-replace at cap: {result}"
        with spawn_harness.registry._registry_lock:
            live = [
                (sid, name)
                for (sid, name, _iid), t in spawn_harness.registry._worker_registry.items()
                if sid == SESSION_H and t.is_alive()
            ]
        assert len(live) == 5


# =========================================================================
# L. WorkerDefinition schema (worker config model)
# =========================================================================

class TestWorkerDefinitionSchema:
    """WorkerDefinition required fields, defaults and schema-file sync."""

    @pytest.fixture
    def valid_kwargs(self) -> dict:
        return {
            "name": "code-reviewer",
            "description": "Reviews pull requests for style and correctness.",
            "system_prompt": "You are a code reviewer. Be concise.\n",
            "tools": ["FileEditor", "GlobTool", "Respond"],
            "permission_footprint": {"filesystem": "read"},
        }

    def test_required_fields_include_worker_permissions(self, valid_kwargs):
        schema = WorkerDefinition.model_json_schema()
        required = set(schema.get("required", []))
        assert {"name", "description", "system_prompt", "tools", "worker_permissions"} <= required
        assert "timeout_seconds" not in required
        assert "temperature" not in required

    def test_critical_threshold_default(self, valid_kwargs):
        wd = WorkerDefinition(**valid_kwargs)
        assert wd.critical_threshold_tokens == 80000
        props = WorkerDefinition.model_json_schema()["properties"]["critical_threshold_tokens"]
        assert props.get("default") == 80000
        assert props.get("title") == "Critical Threshold Tokens"

    def test_temperature_defaults_none(self, valid_kwargs):
        assert WorkerDefinition(**valid_kwargs).temperature is None

    def test_schema_file_matches(self):
        schema_path = Path("resources/worker_definition_schema.json")
        assert schema_path.exists()
        on_disk = json.loads(schema_path.read_text())
        assert on_disk == WorkerDefinition.model_json_schema()

    def test_json_round_trip(self, valid_kwargs):
        kwargs = dict(valid_kwargs)
        kwargs["temperature"] = 0.7
        wd = WorkerDefinition(**kwargs)
        assert WorkerDefinition(**wd.model_dump()) == wd


# =========================================================================
# E. Permission defaults and round-trip (security model)
# =========================================================================

class TestPermissionDefaultsAndRoundTrip:
    """network defaults to 'banned'; default policy 'deny'; round-trips intact."""

    def test_default_network_is_banned(self):
        assert SessionPermissions().network == "banned"

    def test_explicit_network_write_overrides(self):
        assert SessionPermissions(network="write").network == "write"

    def test_default_policy_is_deny(self):
        assert get_default_security_config()["session_policy"]["default_policy"] == "deny"

    def test_merge_explicit_allow_survives(self):
        merged = merge_security_config({"session_policy": {"default_policy": "allow"}})
        assert merged["session_policy"]["default_policy"] == "allow"

    def test_custom_permissions_round_trip(self):
        cfg = AgentConfig()
        cfg.session_permissions = SessionPermissions(
            container=True, network=True, filesystem="full",
            system="write", git="read", execution="banned",
        )
        sp2 = AgentConfig(**cfg.model_dump()).session_permissions
        assert sp2.container is True
        assert sp2.network == "write"  # True coerces to 'write'
        assert sp2.filesystem == "full"
        assert sp2.system == "write"
        assert sp2.git == "read"
        assert sp2.execution == "banned"

    def test_restrictive_permissions_round_trip(self):
        cfg = AgentConfig()
        cfg.session_permissions = SessionPermissions(
            container=False, network=False, filesystem="read",
            system="banned", git="banned", execution="banned",
        )
        sp2 = AgentConfig(**cfg.model_dump()).session_permissions
        assert sp2.container is False
        assert sp2.network == "banned"  # False coerces to 'banned'
        assert sp2.filesystem == "read"

    def test_model_dump_json_serializable(self):
        cfg = AgentConfig()
        cfg.session_permissions = SessionPermissions(
            container=True, network=False, filesystem="write"
        )
        parsed = json.loads(json.dumps(cfg.model_dump()))
        assert parsed["session_permissions"]["container"] is True
        assert parsed["session_permissions"]["network"] == "banned"
        assert parsed["session_permissions"]["filesystem"] == "write"

    def test_exclude_api_key_keeps_permissions(self):
        cfg = AgentConfig()
        cfg.session_permissions = SessionPermissions(filesystem="full")
        d = cfg.model_dump(exclude={"api_key"})
        assert d["session_permissions"]["filesystem"] == "full"

    def test_file_io_round_trip(self, tmp_path):
        cfg = AgentConfig()
        cfg.session_permissions = SessionPermissions(
            container=True, network=True, filesystem="full",
            system="write", git="read", execution="banned",
        )
        path = str(tmp_path / "config.json")
        save_config(cfg.model_dump(exclude={"api_key"}, exclude_none=True), path)
        cfg2 = AgentConfig(**load_config(path))
        sp = cfg2.session_permissions
        assert sp.container is True
        assert sp.network == "write"
        assert sp.filesystem == "full"
        assert sp.system == "write"
        assert sp.git == "read"
        assert sp.execution == "banned"

    def test_missing_permissions_backfilled_from_factory_defaults(self, tmp_path):
        cfg = AgentConfig()
        path = str(tmp_path / "config2.json")
        save_config(
            cfg.model_dump(exclude={"api_key", "session_permissions"}, exclude_none=True),
            path,
        )
        sp = AgentConfig(**load_config(path)).session_permissions
        assert sp.container is True
        assert sp.network == "write"
        assert sp.filesystem == "write"
        assert sp.system == "read"
        assert sp.git == "read"
        assert sp.execution == "banned"


# =========================================================================
# F. ToolExecutor enforcement (permission gate)
# =========================================================================

class TestToolExecutorEnforcement:
    """The ToolExecutor outer gate enforces session permissions."""

    def _executor(self, tool_classes, permissions):
        cfg = AgentConfig(session_permissions=permissions)
        return ToolExecutor(
            tool_classes=tool_classes,
            config=cfg,
            state=AgentState(config=cfg),
            logger=None,
            security_available=False,
            agent=None,
        )

    @staticmethod
    def _run(executor, tool_cls, args=None):
        # pydantic v2 models do not expose fields as class attributes, so use
        # the class name as the tool name (matches the stubs' `tool` values).
        return executor._execute_single_tool(
            tool_cls, args or {}, tool_cls.__name__, 0,
            lambda: False, lambda: None, lambda: 0,
        )

    def test_permissive_allows_write(self):
        executor = self._executor([FileWriteTool], SessionPermissions(filesystem="write"))
        assert self._run(executor, FileWriteTool)["result"] == "Write OK"

    def test_restrictive_denies_write(self):
        executor = self._executor([FileWriteTool], SessionPermissions(filesystem="read"))
        result = self._run(executor, FileWriteTool)["result"]
        assert "Permission denied" in result
        assert "filesystem:write" in result

    def test_multi_requirement_denied_if_one_missing(self):
        executor = self._executor(
            [MultiRequirementTool],
            SessionPermissions(container=True, network=False, filesystem="write"),
        )
        result = self._run(executor, MultiRequirementTool)["result"]
        assert "Permission denied" in result
        assert "network" in result

    def test_hot_swap_banned_to_read(self):
        """Replacing session_permissions at runtime changes the gate outcome."""
        cfg = AgentConfig(session_permissions=SessionPermissions(filesystem="banned"))
        executor = ToolExecutor(
            tool_classes=[FilePreviewTool], config=cfg, state=AgentState(config=cfg)
        )
        r1 = executor._execute_single_tool(
            FilePreviewTool, {"filename": "x.txt"}, "FilePreviewTool", 0,
            lambda: False, lambda: "", lambda: 0,
        )
        assert "Permission denied" in r1.get("result", "")

        cfg.session_permissions = SessionPermissions(filesystem="read")
        r2 = executor._execute_single_tool(
            FilePreviewTool, {"filename": "/nonexistent/file.txt"}, "FilePreviewTool", 0,
            lambda: False, lambda: "", lambda: 0,
        )
        assert "Permission denied" not in r2.get("result", "")
        assert any(
            msg in r2.get("result", "")
            for msg in ("No such file", "not found", "not a file", "not exist")
        )


# =========================================================================
# G. Ask permission flow (defer to the outer gate / cancel = deny)
# =========================================================================

class FakeConfigWithPermissions:
    workspace_path = None
    tool_output_token_limit = None

    def __init__(self, permissions):
        self.session_permissions = permissions


class FakeState:
    security_config = None


@pytest.fixture
def clean_prompts():
    """Reset the prompt machinery so ask-flow tests never leak pending state."""
    yield
    _prompt_cancelled.clear()
    with _pending_requests_lock:
        _pending_security_requests.clear()


class TestAskPermissionFlow:
    """'ask' git + git:write tool -> prompt flow; approve/deny/cancel contracts."""

    def _make_executor(self, tool_classes, permissions=None):
        return ToolExecutor(
            tool_classes=tool_classes,
            config=FakeConfigWithPermissions(permissions),
            state=FakeState(),
            logger=None,
            security_available=False,
            agent=None,
        )

    def test_ask_approve_runs(self, clean_prompts):
        perms = SessionPermissions(git="ask")
        executor = self._make_executor([GitWriteTool], permissions=perms)
        result_container = []

        def run_executor():
            result_container.append(executor._execute_single_tool(
                GitWriteTool, {}, "GitWriteTool", 0,
                lambda: False, lambda: None, lambda: 0,
            ))

        t = threading.Thread(target=run_executor, daemon=True)
        t.start()
        time.sleep(0.2)

        with _pending_requests_lock:
            request_ids = list(_pending_security_requests.keys())
        assert len(request_ids) > 0, "executor did not trigger the ask flow"
        resolve_security_prompt(request_ids[0], approved=True)

        t.join(timeout=5)
        assert len(result_container) == 1
        assert result_container[0]["result"] == "Git write OK"
        assert result_container[0]["tool_type"] == "normal"

    def test_ask_deny_blocks(self, clean_prompts):
        perms = SessionPermissions(git="ask")
        executor = self._make_executor([GitWriteTool], permissions=perms)
        result_container = []

        def run_executor():
            result_container.append(executor._execute_single_tool(
                GitWriteTool, {}, "GitWriteTool", 0,
                lambda: False, lambda: None, lambda: 0,
            ))

        t = threading.Thread(target=run_executor, daemon=True)
        t.start()
        time.sleep(0.2)

        with _pending_requests_lock:
            request_ids = list(_pending_security_requests.keys())
        assert len(request_ids) > 0
        resolve_security_prompt(request_ids[0], approved=False)

        t.join(timeout=5)
        assert len(result_container) == 1
        assert "Permission denied" in result_container[0]["result"]

    def test_ask_read_bypasses_prompt(self, clean_prompts):
        perms = SessionPermissions(git="ask")
        executor = self._make_executor([GitReadTool], permissions=perms)
        result = executor._execute_single_tool(
            GitReadTool, {}, "GitReadTool", 0,
            lambda: False, lambda: None, lambda: 0,
        )
        assert result["result"] == "Git read OK"
        with _pending_requests_lock:
            assert len(list(_pending_security_requests.keys())) == 0

    def test_resolve_places_response_on_queue(self, clean_prompts):
        q = queue.Queue()
        request_id = "hardening-req-1"
        with _pending_requests_lock:
            _pending_security_requests[request_id] = q
        resolve_security_prompt(request_id, approved=True)
        assert q.get(timeout=1) == {"approved": True, "remember": False}
        with _pending_requests_lock:
            assert request_id not in _pending_security_requests

    def test_resolve_with_remember(self, clean_prompts):
        q = queue.Queue()
        request_id = "hardening-req-2"
        with _pending_requests_lock:
            _pending_security_requests[request_id] = q
        resolve_security_prompt(request_id, approved=False, remember=True)
        assert q.get(timeout=1) == {"approved": False, "remember": True}

    def test_resolve_unknown_id_no_raise(self):
        resolve_security_prompt("hardening-nonexistent", approved=True)

    def test_cancel_sets_event_and_clears_pending(self, clean_prompts):
        _prompt_cancelled.clear()
        q1 = queue.Queue()
        with _pending_requests_lock:
            _pending_security_requests["hardening-req-3"] = q1
        cancel_pending_prompts()
        assert _prompt_cancelled.is_set()
        with _pending_requests_lock:
            assert len(_pending_security_requests) == 0

    def test_cancel_twice_is_idempotent(self, clean_prompts):
        _prompt_cancelled.clear()
        cancel_pending_prompts()
        cancel_pending_prompts()
        assert _prompt_cancelled.is_set()

    def test_thread_exits_with_denial_on_cancel(self, monkeypatch, clean_prompts):
        """Cancel means deny, through the REAL prompt-resolution flow."""
        monkeypatch.setattr(_security_module, "global_event_bus", EventBus())
        monkeypatch.setattr(_security_module, "EVENT_SYSTEM_AVAILABLE", True)
        monkeypatch.setattr(_security_module, "create_event", _create_event)

        _prompt_cancelled.clear()
        result = {"approved": None}

        def waiter():
            config = get_default_security_config()
            config["session_policy"]["tool_overrides"] = {"TestTool": "ask"}
            try:
                result["approved"] = is_allowed(
                    agent_id="test-agent",
                    tool_name="TestTool",
                    security_config=config,
                )
            except Exception as e:  # pragma: no cover - diagnostic only
                result["exception"] = e

        t = threading.Thread(target=waiter, daemon=True)
        t.start()

        deadline = time.monotonic() + 5.0
        pending = 0
        while time.monotonic() < deadline:
            with _pending_requests_lock:
                pending = len(_pending_security_requests)
            if pending:
                break
            time.sleep(0.02)
        assert pending > 0, "Security prompt was never registered as pending"

        cancel_pending_prompts()
        t.join(timeout=5.0)
        assert not t.is_alive(), "Waiter thread did not exit"
        assert result.get("approved") is False, f"Expected denial on cancel, got {result}"


# =========================================================================
# H. Permission routing: 'ask' defers, 'banned' denies (fail-closed)
# =========================================================================

class FakeSandboxExecution:
    """Drop-in SandboxedExecution: non-None required_category raises PermissionError."""

    instances = []

    def __init__(self, *args, **kwargs):
        self.init_kwargs = kwargs
        self.calls = []
        FakeSandboxExecution.instances.append(self)

    def run(self, command, **kwargs):
        self.calls.append((command, kwargs))
        if kwargs.get("required_category") is not None:
            raise PermissionError(
                f"Permission denied: requires {kwargs['required_category']}, "
                f"but session allows git:read"
            )
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")


class FakeManager:
    """Resource-container stand-in recording exec() invocations."""

    def __init__(self):
        self.calls = []

    def exec(self, command, **kwargs):
        self.calls.append((command, kwargs))
        return {"exit_code": 0, "stdout": "ok", "stderr": ""}


@pytest.fixture
def fake_sandbox(monkeypatch):
    FakeSandboxExecution.instances = []
    monkeypatch.setattr(
        "tools.git_info_tool.SandboxedExecution", FakeSandboxExecution
    )
    return FakeSandboxExecution


def _git_tool(operation, session_perms=None, effective_perms=None, **kwargs):
    return GitInfoTool(
        operation=operation,
        session_permissions=session_perms,
        effective_permissions=effective_perms,
        **kwargs,
    )


class TestPermissionRouting:
    """'ask' defers to the outer gate; 'banned' denies inside the tool."""

    def test_host_ask_commit_defers_gate(self, tmp_path, fake_sandbox):
        tool = _git_tool("commit", {"git": "ask"}, {"git": "ask"})
        result = tool._exec_host_raw(tmp_path, ["commit", "-m", "x"])
        assert result == (0, "ok", "")
        inst = fake_sandbox.instances[0]
        command, kwargs = inst.calls[0]
        assert command[0] == "git"
        assert "commit" in command
        assert "--no-verify" in command
        assert kwargs["required_category"] is None

    def test_host_banned_commit_denied(self, tmp_path, fake_sandbox):
        tool = _git_tool("commit", {"git": "banned"}, {"git": "banned"})
        with pytest.raises(PermissionError):
            tool._exec_host_raw(tmp_path, ["commit", "-m", "x"])
        inst = fake_sandbox.instances[0]
        assert inst.calls[0][1]["required_category"] == "git:write"

    def _container_tool(self, session_perms, effective_perms, tmp_path, manager):
        tool = _git_tool("commit", session_perms, effective_perms)
        object.__setattr__(tool, "_resolved_workspace_path", str(tmp_path))
        object.__setattr__(tool, "_resolved_workspace_id", "test-ws")
        object.__setattr__(tool, "_ensure_resource_container", lambda: manager)
        return tool

    def test_container_ask_commit_defers_gate(self, tmp_path):
        manager = FakeManager()
        tool = self._container_tool({"git": "ask"}, {"git": "ask"}, tmp_path, manager)
        result = tool._exec_container_raw(tmp_path, ["commit", "-m", "x"])
        assert result == (0, "ok", "")
        assert len(manager.calls) == 1
        command, _kwargs = manager.calls[0]
        assert command == [
            "git", "-c", "core.hooksPath=/workspace/.githooks", "commit", "-m", "x",
        ]

    def test_container_banned_commit_denied(self, tmp_path):
        manager = FakeManager()
        tool = self._container_tool({"git": "banned"}, {"git": "banned"}, tmp_path, manager)
        with pytest.raises(PermissionError):
            tool._exec_container_raw(tmp_path, ["commit", "-m", "x"])
        assert manager.calls == []

    def test_network_ask_defers_gate(self):
        tool = _git_tool(
            "remote",
            {"network": "ask", "git": "ask"},
            {"network": "ask", "git": "ask"},
        )
        result = tool.execute()
        assert "Atomic permission check failed" not in result

    def test_network_banned_fail_closed(self):
        tool = _git_tool(
            "remote",
            {"network": "banned", "git": "read"},
            {"network": "banned", "git": "read"},
        )
        result = tool.execute()
        assert (
            "Atomic permission check failed: network:outbound required for remote"
            in result
        )

    def test_network_missing_fail_closed(self):
        tool = _git_tool("remote", {"git": "read"}, None)
        result = tool.execute()
        assert "Atomic permission check failed" in result

    def test_clone_ask_network_defers_gate(self):
        tool = _git_tool(
            "clone",
            {"network": "ask", "git": "ask"},
            {"network": "ask", "git": "ask"},
            clone_url="https://example.com/repo.git",
        )
        result = tool.execute()
        assert "Atomic permission check failed" not in result


# =========================================================================
# I. Bridge apply_config / save / load round-trip
# =========================================================================

class TestBridgePermissionSync:
    """WebAgentBridge persists session_permissions through save/load."""

    @pytest.fixture
    def temp_store(self, tmp_path):
        return FileSystemSessionStore(
            sessions_dir=str(tmp_path / "sessions"),
            state_dir=str(tmp_path / "state"),
        )

    def test_apply_config_accepts_custom_permissions(self, temp_store):
        bridge = WebAgentBridge(event_callback=lambda e: None, session_store=temp_store)
        result = bridge.apply_config({"session_permissions": {"filesystem": "banned"}})
        assert "config" in result and "merged_config" in result
        assert result["permissions"]["filesystem"] == "banned"
        config = bridge.get_config()
        assert config is not None
        assert config["session_permissions"]["filesystem"] == "banned"

    def test_roundtrip_preserves_permissions(self, temp_store):
        bridge = WebAgentBridge(event_callback=lambda e: None, session_store=temp_store)
        bridge.apply_config({"session_permissions": {"filesystem": "banned"}})
        saved = bridge.save_session()
        assert saved is not None
        session_id = saved.session_id

        path = temp_store._find_session_path(session_id)
        assert path is not None, "session file not found on disk"
        with open(path, "r") as f:
            raw = json.load(f)
        perms_disk = (
            raw.get("metadata", {})
            .get("session_config", {})
            .get("session_permissions", {})
        )
        assert perms_disk.get("filesystem") == "banned"

        bridge2 = WebAgentBridge(event_callback=lambda e: None, session_store=temp_store)
        assert bridge2.load_session(session_id)
        config = bridge2.get_config()
        assert config is not None
        assert config["session_permissions"]["filesystem"] == "banned"


# =========================================================================
# J. Global-defaults worker config (allowlist persistence + fallbacks)
# =========================================================================

class TestGlobalDefaultsWorkerConfig:
    """Only the six allowlist keys persist; absent keys fall back."""

    def test_allowlist_only_keys_persisted(self, hermetic_vault):
        import web_ui.backend.server as server_mod

        sentinel = {"sentinel": True}
        agent_cfg = hermetic_vault / "agent_config.json"
        agent_cfg.write_text(json.dumps(sentinel))

        payload = {
            "provider_id": "p1",
            "model": "m1",
            "base_url": "http://localhost:11434/v1",
            "temperature": 0.4,
            "max_turns": 5,
            "system_prompt": "sys prompt",
            "workspace_path": "/some/workspace",
            "workspace_id": "ws_1",
            "mode": "agent",
        }
        saved_path = server_mod.save_global_defaults(payload)

        assert json.loads(agent_cfg.read_text()) == sentinel
        assert saved_path == hermetic_vault / "user" / "defaults.json"

        defaults = json.loads((hermetic_vault / "user" / "defaults.json").read_text())
        assert defaults["provider_id"] == "p1"
        assert defaults["model"] == "m1"
        assert defaults["base_url"] == "http://localhost:11434/v1"
        assert defaults["temperature"] == 0.4
        assert defaults["max_turns"] == 5
        assert defaults["system_prompt"] == "sys prompt"
        assert "workspace_path" not in defaults
        assert "workspace_id" not in defaults
        assert "mode" not in defaults

    def test_absent_keys_fall_back(self, hermetic_vault):
        """Empty defaults.json -> constructor fallbacks, no system_prompt key."""
        from web_ui.backend.config_manager import ConfigManager
        from web_ui.backend.session_manager import SessionManager

        store = FileSystemSessionStore(
            sessions_dir=tempfile.mkdtemp(prefix="test_worker_hardening_")
        )
        manager = SessionManager(session_store=store, config_manager=ConfigManager())
        session_id, _ = manager.create_session(mode="custom")
        loaded = store.load_session(session_id)
        assert loaded is not None
        cfg = loaded.metadata["session_config"]
        assert cfg["max_turns"] == 100
        assert cfg["temperature"] == 0.7
        assert cfg["provider_id"] == ""
        assert cfg["model"] == ""
        assert cfg["base_url"] == ""
        assert "system_prompt" not in cfg
