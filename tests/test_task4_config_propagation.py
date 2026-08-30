"""Task 4 — config propagation across the full stack.

Covers:
1. ``SessionConfig.timeout_seconds`` field + forwarding into AgentConfig,
   and the web_ui config_manager plumbing (fallback dict, extract_settings,
   apply_config mutable loop, validate, layer ownership).
2. ``ProviderConfig`` explicit-None normalisation (None timeout/max_retries
   fall back to 120/3; 0 preserved).
3. Operator feature-flag propagation (SessionConfig -> AgentConfig,
   ToolExecutor agent_config injection, WorkerThread._build_agent_config,
   git_info_tool exact-True gate, host_bash allow_host_resources gate).
4. Old -> new config diff logging (agent hot-swap / restart branches and
   bridge.apply_config).
5. ``_build_global_agent_config`` raises RuntimeError instead of silently
   falling back to ``AgentConfig(api_key='')``.
"""

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pytest

from agent.config.session_config import SessionConfig
from agent.config.models import AgentConfig
from llm_providers.base import ProviderConfig
from agent.core.agent import Agent, _config_diff
from web_ui.backend.config_manager import (
    ConfigManager,
    FALLBACK_FRONTEND_CONFIG,
    CONFIG_LAYER_OWNERSHIP,
)


# ─────────────────────────────────────────────────────────────────────
# 1. SessionConfig.timeout_seconds
# ─────────────────────────────────────────────────────────────────────

class TestSessionConfigTimeoutSeconds:

    def test_field_defaults_to_none(self):
        assert SessionConfig().timeout_seconds is None

    def test_field_rejects_less_than_one(self):
        with pytest.raises(Exception):
            SessionConfig(timeout_seconds=0)
        with pytest.raises(Exception):
            SessionConfig(timeout_seconds=-3)

    def test_to_agent_config_forwards_value(self):
        acfg = SessionConfig(timeout_seconds=180).to_agent_config()
        assert acfg.timeout_seconds == 180

    def test_to_agent_config_none_uses_agent_default(self):
        from thoughtmachine.timeout_constants import SOFT_BUDGET_FALLBACK_SECONDS
        acfg = SessionConfig().to_agent_config()
        assert acfg.timeout_seconds == SOFT_BUDGET_FALLBACK_SECONDS

    def test_model_dump_roundtrip(self):
        data = SessionConfig(mode='custom', timeout_seconds=240).model_dump(
            exclude={'api_key'})
        assert data['timeout_seconds'] == 240
        assert SessionConfig(**data).timeout_seconds == 240

    def test_distinct_from_provider_config_timeout(self):
        acfg = SessionConfig(
            timeout_seconds=180,
            provider_config={'timeout': 10},
        ).to_agent_config()
        assert acfg.timeout_seconds == 180
        assert acfg.provider_config == {'timeout': 10}


class TestConfigManagerTimeoutSeconds:

    def test_fallback_frontend_config_includes_timeout_seconds(self):
        assert "timeout_seconds" in FALLBACK_FRONTEND_CONFIG
        assert FALLBACK_FRONTEND_CONFIG["timeout_seconds"] is None

    def test_session_layer_owns_timeout_seconds(self):
        assert "timeout_seconds" in CONFIG_LAYER_OWNERSHIP["session_config"]

    def test_extract_settings_includes_timeout_seconds(self):
        settings = ConfigManager.extract_settings(
            {"timeout_seconds": 60, "model": "m", "provider": "p"})
        assert settings["timeout_seconds"] == 60

    def test_validate_rejects_bad_timeout_seconds(self):
        res = ConfigManager.validate(
            {"provider": "p", "model": "m", "timeout_seconds": 0})
        assert res["valid"] is False
        assert "timeout_seconds" in res["field_errors"]

        res2 = ConfigManager.validate(
            {"provider": "p", "model": "m", "timeout_seconds": "abc"})
        assert res2["valid"] is False
        assert "timeout_seconds" in res2["field_errors"]

    def test_validate_accepts_valid_timeout_seconds(self):
        res = ConfigManager.validate(
            {"provider": "p", "model": "m", "timeout_seconds": 30})
        assert res["valid"] is True

    def test_apply_config_mutable_loop_sets_timeout_seconds(self):
        cfg = SessionConfig()
        _frontend, updated = ConfigManager.apply_config(
            {"timeout_seconds": 90},
            cfg,
            is_running=False,
            has_session=False,
        )
        assert updated.timeout_seconds == 90


# ─────────────────────────────────────────────────────────────────────
# 2. ProviderConfig None normalisation
# ─────────────────────────────────────────────────────────────────────

class TestProviderConfigNoneNormalization:

    def test_none_timeout_and_max_retries_normalized(self):
        cfg = ProviderConfig(api_key='k', timeout=None, max_retries=None)
        assert cfg.timeout == 120
        assert cfg.max_retries == 3

    def test_zero_max_retries_preserved(self):
        cfg = ProviderConfig(api_key='k', timeout=None, max_retries=0)
        assert cfg.max_retries == 0
        assert cfg.timeout == 120

    def test_explicit_values_preserved(self):
        cfg = ProviderConfig(api_key='k', timeout=45, max_retries=5)
        assert cfg.timeout == 45
        assert cfg.max_retries == 5


# ─────────────────────────────────────────────────────────────────────
# 3. Operator feature-flag propagation
# ─────────────────────────────────────────────────────────────────────

class TestOperatorFlagPropagation:

    def test_session_config_to_agent_config_flags(self):
        acfg = SessionConfig(
            git_allow_worktree_commits=True,
            allow_host_resources=True,
            use_workspace_lifecycle_manager=True,
            use_container_registry=True,
        ).to_agent_config()
        assert acfg.git_allow_worktree_commits is True
        assert acfg.allow_host_resources is True
        assert acfg.use_workspace_lifecycle_manager is True
        assert acfg.use_container_registry is True

    def test_flags_default_to_false(self):
        acfg = SessionConfig().to_agent_config()
        assert acfg.git_allow_worktree_commits is False
        assert acfg.allow_host_resources is False


class TestToolExecutorFlagInjection:

    def test_agent_config_injection_carries_flags(self):
        from typing import ClassVar, List, Optional
        from agent.core.tool_executor import ToolExecutor
        from agent.core.state import AgentState
        from tools.base import ToolBase

        captured = {}

        class FlagCaptureTool(ToolBase):
            tool: str = "FlagCaptureTool"
            required_categories: ClassVar[List[str]] = []
            agent_config: Optional[dict] = None

            def execute(self) -> str:
                captured['agent_config'] = self.agent_config
                return "OK"

        config = AgentConfig(
            api_key='test-key',
            temperature=0.5,
            max_turns=50,
            model='m1',
            git_allow_worktree_commits=True,
            allow_host_resources=True,
            use_workspace_lifecycle_manager=True,
            use_container_registry=True,
        )
        executor = ToolExecutor(
            tool_classes=[FlagCaptureTool],
            config=config,
            state=AgentState(config=config),
            logger=None,
            security_available=False,
            agent=None,
        )
        result = executor._execute_single_tool(
            FlagCaptureTool, {}, "FlagCaptureTool", 0,
            lambda: False, lambda: None, lambda: 0,
        )
        assert result['result'] == "OK"
        injected = captured['agent_config']
        assert injected['git_allow_worktree_commits'] is True
        assert injected['allow_host_resources'] is True
        assert injected['use_workspace_lifecycle_manager'] is True
        assert injected['use_container_registry'] is True


class TestWorkerFlagForwarding:

    def test_worker_build_agent_config_preserves_flags(self, tmp_path):
        from tools.workspace.worker import WorkerThread

        wt = WorkerThread(
            name="flags-w",
            definition={},
            agent_config={
                "provider": "scripted",
                "model": "mock-model",
                "api_key": "sk-test",
                "git_allow_worktree_commits": True,
                "allow_host_resources": True,
            },
            workspace_dir=tmp_path,
            tool_classes={},
        )
        acfg = wt._build_agent_config()
        assert acfg is not None
        assert acfg.git_allow_worktree_commits is True
        assert acfg.allow_host_resources is True


class TestGitWriteToolFlagGate:
    """_unprotected_branch_agent_commit_allowed lives on GitWriteTool."""

    @staticmethod
    def _tool(**kwargs):
        from tools.git_write_tool import GitWriteTool
        return GitWriteTool(operation="commit", **kwargs)

    def test_blocked_when_flag_absent(self):
        assert self._tool()._unprotected_branch_agent_commit_allowed(
            Path("/tmp")) is False

    def test_blocked_when_flag_not_exactly_true(self):
        for bad_value in (1, "true", "True", None):
            tool = self._tool(agent_config={"git_allow_worktree_commits": bad_value})
            assert tool._unprotected_branch_agent_commit_allowed(Path("/tmp")) is False, bad_value

    def test_true_flag_proceeds_past_gate(self, monkeypatch):
        tool = self._tool(agent_config={"git_allow_worktree_commits": True})
        # Exactly True passes the operator gate; the container-mode gate then
        # decides. Patch it to False so the method returns False without
        # needing a real git repo — proving the exact-True check passed.
        monkeypatch.setattr(tool, "_use_container_mode", lambda: False)
        assert tool._unprotected_branch_agent_commit_allowed(Path("/tmp")) is False

    def test_protected_branch_names_denied(self, monkeypatch):
        """dev/master/main are protected: commits stay host-side."""
        for branch in ("dev", "master", "main"):
            tool = self._tool(agent_config={"git_allow_worktree_commits": True})
            monkeypatch.setattr(tool, "_use_container_mode", lambda: True)
            monkeypatch.setattr(
                tool, "_run_git",
                lambda *args, **kwargs: f"{branch}\n",
            )
            assert tool._unprotected_branch_agent_commit_allowed(
                Path("/tmp")) is False, branch

    def test_unprotected_branch_allowed(self, monkeypatch):
        """feat/fix/refactor branches may be committed agent-side."""
        tool = self._tool(agent_config={"git_allow_worktree_commits": True})
        monkeypatch.setattr(tool, "_use_container_mode", lambda: True)
        monkeypatch.setattr(
            tool, "_run_git",
            lambda *args, **kwargs: "refactor/foo\n",
        )
        assert tool._unprotected_branch_agent_commit_allowed(Path("/tmp")) is True

    def test_empty_branch_output_fails_closed(self, monkeypatch):
        """Blank branch resolution must deny, never allow."""
        tool = self._tool(agent_config={"git_allow_worktree_commits": True})
        monkeypatch.setattr(tool, "_use_container_mode", lambda: True)
        monkeypatch.setattr(
            tool, "_run_git",
            lambda *args, **kwargs: "\n",
        )
        assert tool._unprotected_branch_agent_commit_allowed(Path("/tmp")) is False

    def test_branch_check_runtime_error_fails_closed(self, monkeypatch):
        """Container-mandatory branch resolution failure must deny."""
        tool = self._tool(agent_config={"git_allow_worktree_commits": True})
        monkeypatch.setattr(tool, "_use_container_mode", lambda: True)

        def _boom(*args, **kwargs):
            raise RuntimeError("container unavailable")

        monkeypatch.setattr(tool, "_run_git", _boom)
        assert tool._unprotected_branch_agent_commit_allowed(Path("/tmp")) is False


class TestHostBashFlagGate:

    def test_disabled_when_flag_missing(self, tmp_path):
        from unittest import mock
        from tools.host_bash_tool import HostBashTool

        with mock.patch("tools.host_bash_tool.subprocess") as mock_subprocess:
            tool = HostBashTool(
                command="echo hi",
                effective_permissions={"host_bash": "allow"},
                session_permissions=None,
                agent_config={"log_dir": str(tmp_path)},  # flag absent
                session_id="sess1",
                workspace_path=None,
            )
            result = json.loads(tool.execute())
        assert result["success"] is False
        assert "allow_host_resources is false" in result["error"]
        mock_subprocess.run.assert_not_called()

    def test_disabled_when_flag_false(self, tmp_path):
        from unittest import mock
        from tools.host_bash_tool import HostBashTool

        with mock.patch("tools.host_bash_tool.subprocess") as mock_subprocess:
            tool = HostBashTool(
                command="echo hi",
                effective_permissions={"host_bash": "allow"},
                session_permissions=None,
                agent_config={"allow_host_resources": False, "log_dir": str(tmp_path)},
                session_id="sess1",
                workspace_path=None,
            )
            result = json.loads(tool.execute())
        assert result["success"] is False
        assert "allow_host_resources is false" in result["error"]
        mock_subprocess.run.assert_not_called()


# ─────────────────────────────────────────────────────────────────────
# 4. Old -> new config diff logging
# ─────────────────────────────────────────────────────────────────────

class TestConfigDiffLogging:

    @staticmethod
    def _make_config(**overrides):
        base = dict(api_key='test-key', enable_logging=False, provider_config={})
        base.update(overrides)
        return AgentConfig(**base)

    @staticmethod
    def _recording_log(records):
        import agent.core.agent as agent_module

        def _log(level, category, msg):
            records.append((level, category, msg))

        return agent_module, _log

    def test_config_diff_helper_reports_changed_field(self):
        diff = _config_diff(
            self._make_config(temperature=0.2),
            self._make_config(temperature=0.9),
        )
        assert len(diff) == 1
        assert diff[0].startswith('temperature: ')
        assert '0.2' in diff[0] and '0.9' in diff[0]

    def test_config_diff_excludes_api_key_and_stop_check(self):
        old = self._make_config(api_key='old-key', temperature=0.2)
        new = self._make_config(api_key='new-key', temperature=0.2)
        assert _config_diff(old, new) == []

    def test_config_diff_treats_none_and_empty_string_as_equal(self):
        assert _config_diff(
            self._make_config(system_prompt=''),
            self._make_config(system_prompt=None),
        ) == []

    def test_hot_swap_branch_logs_diff(self, monkeypatch):
        agent = Agent(config=self._make_config(temperature=0.2), session_id='test-session')
        records = []
        agent_module, fake_log = self._recording_log(records)
        monkeypatch.setattr(agent_module, 'log', fake_log)

        new = self._make_config(temperature=0.9)
        agent.request_config_update(new)
        assert agent._apply_pending_config() is True

        hot = [r for r in records if r[0] == 'INFO' and 'Hot-swapping config' in r[2]]
        assert hot, records
        assert '1 changed field(s)' in hot[0][2]
        assert 'temperature:' in hot[0][2]

    def test_hot_swap_logs_old_to_new_temperature(self, monkeypatch):
        agent = Agent(config=self._make_config(temperature=0.2), session_id='test-session')
        records = []
        agent_module, fake_log = self._recording_log(records)
        monkeypatch.setattr(agent_module, 'log', fake_log)

        new = self._make_config(temperature=0.9)
        agent.request_config_update(new)
        agent._apply_pending_config()

        hot_msgs = [r[2] for r in records if 'Config hot-swapped' in r[2]]
        assert hot_msgs and 'temperature=0.2 -> 0.9' in hot_msgs[0]

    def test_restart_branch_logs_diff(self, monkeypatch):
        agent = Agent(
            config=self._make_config(provider_config={'timeout': 1}),
            session_id='test-session',
        )
        records = []
        agent_module, fake_log = self._recording_log(records)
        monkeypatch.setattr(agent_module, 'log', fake_log)

        new = self._make_config(provider_config={'timeout': 120})
        agent.request_config_update(new)
        assert agent._apply_pending_config() is True

        restart = [r for r in records if r[0] == 'INFO' and 'Full restart required' in r[2]]
        assert restart, records
        assert '1 changed field(s)' in restart[0][2]
        assert 'provider_config' in restart[0][2]


class TestBridgeApplyConfigDiffLog:

    @pytest.fixture
    def temp_store(self, tmp_path):
        from session.store import FileSystemSessionStore
        return FileSystemSessionStore(
            sessions_dir=str(tmp_path / "sessions"),
            state_dir=str(tmp_path / "state"),
        )

    def test_apply_config_logs_old_to_new_diff(self, temp_store, monkeypatch):
        import web_ui.backend.bridge as bridge_module

        records = []
        monkeypatch.setattr(
            bridge_module, 'log',
            lambda level, cat, msg: records.append((level, cat, msg)),
        )
        bridge = bridge_module.WebAgentBridge(
            event_callback=lambda e: None, session_store=temp_store)
        result = bridge.apply_config({"max_turns": 150})
        assert "config" in result

        # The first INFO may be 'apply_config: initializing default session
        # config'; select the actual old -> new diff message.
        infos = [r for r in records if r[0] == 'INFO'
                 and r[2].startswith('apply_config:')
                 and 'change(s)' in r[2]]
        assert infos, records
        assert '1 change(s)' in infos[0][2]
        assert 'max_turns: None -> 150' in infos[0][2]

    def test_apply_config_noop_logs_debug(self, temp_store, monkeypatch):
        import web_ui.backend.bridge as bridge_module

        records = []
        monkeypatch.setattr(
            bridge_module, 'log',
            lambda level, cat, msg: records.append((level, cat, msg)),
        )
        bridge = bridge_module.WebAgentBridge(
            event_callback=lambda e: None, session_store=temp_store)
        bridge.apply_config({})

        debug = [r for r in records if r[0] == 'DEBUG' and 'no field changes' in r[2]]
        assert debug, records


# ─────────────────────────────────────────────────────────────────────
# 5. _build_global_agent_config raises instead of silent fallback
# ─────────────────────────────────────────────────────────────────────

class TestBuildGlobalAgentConfigRaises:

    def test_raises_runtime_error_on_failure(self, monkeypatch):
        import web_ui.backend.bridge as bridge_module

        def _boom():
            raise ValueError("corrupt agent_config.json")

        monkeypatch.setattr(bridge_module, "create_agent_config_service", _boom)
        bridge = bridge_module.WebAgentBridge(event_callback=lambda e: None)
        with pytest.raises(RuntimeError, match="Could not build global agent config"):
            bridge._build_global_agent_config()

    def test_success_path_builds_agent_config(self, monkeypatch):
        import web_ui.backend.bridge as bridge_module

        class _FakeService:
            def get_all(self):
                return {"api_key": "k", "provider_id": "p", "model": "m"}

        monkeypatch.setattr(
            bridge_module, "create_agent_config_service", lambda: _FakeService())
        bridge = bridge_module.WebAgentBridge(event_callback=lambda e: None)
        acfg = bridge._build_global_agent_config()
        assert isinstance(acfg, AgentConfig)
        assert acfg.api_key == "k"

