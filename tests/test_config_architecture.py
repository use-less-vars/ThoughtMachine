"""Architecture tests: config single-source, strictness, propagation, audit, fail-closed.

Each test maps to one architecture requirement:

1. ``test_config_defaults_single_source`` — literal defaults live in
   ``agent/config/defaults.py`` and source modules re-export the *same*
   objects (identity), so there is exactly one source of truth for worker /
   job-registry constants, the AgentConfig soft-budget timeout default, and
   the ProviderConfig timeout/max_retries defaults.
2. ``test_config_unknown_key_raises`` — config models are strict schemas:
   unknown keys must raise ValidationError instead of being silently dropped.
3. ``test_config_resolution_chain_precedence`` — ``resolve_full_config``
   precedence: factory < global < agent_config < provider profile
   < workspace < session < worker overrides, with GLOBAL_DEFAULT_KEYS
   ownership and mode repair.
4. ``test_provider_timeout_flows_to_llm_client`` — a provider profile's
   ``timeout`` must reach the LLMClient (no silent 120s fallback).
5. ``test_provider_max_retries_flows_to_llm_client`` — a provider profile's
   ``max_retries`` must reach the LLMClient (no silent 3-retry fallback).
6. ``test_timeout_seconds_from_web_ui_path`` — ``timeout_seconds`` set from
   the web UI (SessionConfig -> AgentConfig -> ConfigManager.apply_config
   -> bridge.apply_config) must propagate end-to-end.
7. ``test_operator_flags_inherited_consistently`` — operator feature flags
   (session git_write permission, allow_host_resources) survive
   SessionConfig -> AgentConfig -> WorkerThread._build_agent_config.
8. ``test_hot_swap_provider_config_restarts_and_applies`` — a provider_config
   change is not hot-swappable: the agent takes the full-restart branch,
   applies the new config and writes an audit entry with
   restart_required=True.
9. ``test_config_change_logs_actual_values`` — config-change logs must show
   the actual old -> new values and must never leak api_key.
10. ``test_missing_workspace_id_fails_closed`` — a configured workspace_path
    whose workspace_id cannot be resolved must DENY tool execution
    (fail-closed, no permissive fallback).
11. ``test_permission_fetch_exception_denies`` — when the permission gate
    computation raises, check_system must report effective_permissions={}
    plus permission_fetch_error (fail-closed, not raw session perms).
12. ``test_no_silent_agentconfig_empty_fallback`` — a broken agent_config
    service (exception or junk data) must raise RuntimeError instead of
    silently constructing ``AgentConfig(api_key='')``.
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pytest
from pydantic import ValidationError

import web_ui.backend.config_manager as cm
import agent.config.defaults as defaults
import agent.core.agent as agent_module
import agent.core.tool_executor as te_module
import tools.workspace.check_system as check_system_module
import tools.workspace.worker as worker_module
import tools.workspace.job_registry as job_registry_module
import web_ui.backend.bridge as bridge_module

from agent.config.session_config import SessionConfig
from agent.config.models import AgentConfig
from llm_providers.base import ProviderConfig
from agent.core.llm_client import LLMClient
from llm_providers.factory import ProviderFactory
from agent.core.agent import Agent
from agent.core.tool_executor import ToolExecutor, GATE_AVAILABLE
from tools.file_preview_tool import FilePreviewTool
from thoughtmachine.security import SessionPermissions


# ---------------------------------------------------------------------------
# Shared helpers (mirroring test_resolve_full_config / test_task6 patterns)
# ---------------------------------------------------------------------------


class FakeStore:
    """Drop-in replacement for FileSystemSessionStore used by resolve_full_config."""

    REGISTRY = {}

    def __init__(self):
        pass

    def load_session(self, session_id, workspace_id=None):
        return FakeStore.REGISTRY.get(session_id)


@pytest.fixture
def config_chain(tmp_path, monkeypatch):
    """Point every layer source at tmp_path; empty agent_config + no providers."""
    def factory_path():
        return tmp_path / "system" / "factory_defaults.json"

    def user_path():
        return tmp_path / "user" / "defaults.json"

    def ws_path(workspace_id):
        return tmp_path / "workspaces" / f"{workspace_id}" / "defaults.json"

    monkeypatch.setattr(cm, "_get_factory_defaults_path", factory_path)
    monkeypatch.setattr(cm, "_get_user_defaults_path", user_path)
    monkeypatch.setattr(cm, "_get_workspace_defaults_path", ws_path)

    agent_cfg = {}
    monkeypatch.setattr(
        cm,
        "create_agent_config_service",
        lambda: SimpleNamespace(get_all=lambda: agent_cfg),
    )

    import agent.config.provider_profile as pp
    monkeypatch.setattr(pp, "PROVIDERS_FILE", tmp_path / "providers.json")

    import session.store as store_mod
    monkeypatch.setattr(store_mod, "FileSystemSessionStore", FakeStore)
    FakeStore.REGISTRY = {}  # reset per test

    return tmp_path


def _write_providers(tmp_path, profiles, active="p1"):
    (tmp_path / "providers.json").write_text(
        json.dumps({"profiles": profiles, "active_profile_id": active}),
        encoding="utf-8",
    )


P1_PROFILE = {
    "id": "p1",
    "label": "P1",
    "api_key": "key-p1",
    "base_url": "https://p1.example",
    "default_model": "model-p1",
    "timeout": 42,
    "max_retries": 7,
}


def _fake_session(sid, session_config=None):
    metadata = {}
    if session_config is not None:
        metadata["session_config"] = session_config
    return SimpleNamespace(metadata=metadata, session_id=sid, workspace_id=None)


def _llm_stub(resolved):
    """Minimal AgentConfig stub carrying only the attrs LLMClient touches."""
    return SimpleNamespace(
        provider_type=resolved.get("provider_type", "openai_compatible"),
        api_key=resolved.get("api_key", "test-key"),
        base_url=resolved.get("base_url"),
        model=resolved.get("model", "test-model"),
        temperature=0.2,
        provider_config=resolved.get("provider_config") or {},
    )


def _capture_create_provider(monkeypatch):
    """Replace ProviderFactory.create_provider with a kwargs recorder."""
    captured = {}

    def fake_create(provider_type, api_key=None, **kwargs):
        captured['call'] = dict(provider_type=provider_type, api_key=api_key, **kwargs)
        return object()

    monkeypatch.setattr(ProviderFactory, 'create_provider', staticmethod(fake_create))
    return captured


def _make_executor(workspace_path=None):
    config = AgentConfig(
        session_permissions=SessionPermissions(filesystem="banned"),
        workspace_path=workspace_path,
    )
    from agent.core.state import AgentState
    executor = ToolExecutor(
        tool_classes=[FilePreviewTool],
        config=config,
        state=AgentState(config=config),
    )
    return executor


def _run(executor):
    return executor._execute_single_tool(
        FilePreviewTool, {"filename": "/etc/passwd"}, "file_preview", 0,
        lambda: False, lambda: None, lambda: 0,
    )


_skip_without_gate = pytest.mark.skipif(
    not GATE_AVAILABLE, reason="security gate not importable in this env")


# ---------------------------------------------------------------------------
# 1. Single source of truth for literal defaults
# ---------------------------------------------------------------------------


def test_config_defaults_single_source():
    """agent/config/defaults.py is the single source for worker/job-registry
    literals; consumers re-export the same objects (identity, not copies)."""
    assert defaults.SPAWN_QUEUE_TIMEOUT == 600
    assert defaults.MAX_WORKERS_PER_SESSION == 3
    assert defaults.WORKER_DEFAULT_TEMPERATURE == 0.7

    # worker.py re-exports the exact same objects
    assert worker_module.SPAWN_QUEUE_TIMEOUT is defaults.SPAWN_QUEUE_TIMEOUT
    assert worker_module.MAX_WORKERS_PER_SESSION is defaults.MAX_WORKERS_PER_SESSION
    assert worker_module.WORKER_DEFAULT_TEMPERATURE is defaults.WORKER_DEFAULT_TEMPERATURE

    # job_registry.py re-exports the exact same objects
    assert job_registry_module.JOB_REGISTRY_MAX_JOBS is defaults.JOB_REGISTRY_MAX_JOBS
    assert job_registry_module.PREVIEW_CAP is defaults.PREVIEW_CAP
    assert job_registry_module.PARTIAL_PREVIEW_CAP is defaults.PARTIAL_PREVIEW_CAP
    assert job_registry_module.TERMINAL_STATUSES is defaults.TERMINAL_STATUSES

    # AgentConfig timeout_seconds default comes from the same leaf constant
    assert AgentConfig().timeout_seconds == defaults.SOFT_BUDGET_FALLBACK_SECONDS == 300

    # ProviderConfig defaults (120s timeout / 3 retries) are explicit
    pc = ProviderConfig(api_key='x')
    assert pc.timeout == 120
    assert pc.max_retries == 3


# ---------------------------------------------------------------------------
# 2. Strict schemas: unknown keys raise
# ---------------------------------------------------------------------------


def test_config_unknown_key_raises():
    """Unknown keys must fail loudly instead of being silently dropped."""
    with pytest.raises(ValidationError):
        AgentConfig(bogus=1)
    with pytest.raises(ValidationError):
        SessionConfig(bogus=1)


# ---------------------------------------------------------------------------
# 3. Full resolution chain precedence
# ---------------------------------------------------------------------------


def test_config_resolution_chain_precedence(config_chain, monkeypatch):
    """Lowest->highest: factory < global < agent_config < provider profile
    < workspace < session < worker overrides; GLOBAL_DEFAULT_KEYS ownership;
    mode repaired to 'agent'."""
    (config_chain / "system").mkdir(parents=True, exist_ok=True)
    (config_chain / "system" / "factory_defaults.json").write_text(
        json.dumps({"config": {
            "provider_id": "p1",
            "model": "factory-model",
            "temperature": 0.1,
            "max_turns": 100,
        }}),
        encoding="utf-8",
    )
    (config_chain / "user").mkdir(parents=True, exist_ok=True)
    (config_chain / "user" / "defaults.json").write_text(
        json.dumps({"model": "global-model", "temperature": 0.2, "max_turns": 300}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        cm,
        "create_agent_config_service",
        lambda: SimpleNamespace(get_all=lambda: {
            "model": "agentcfg-model", "temperature": 0.3,
        }),
    )
    _write_providers(config_chain, [P1_PROFILE])
    (config_chain / "workspaces" / "ws1").mkdir(parents=True, exist_ok=True)
    (config_chain / "workspaces" / "ws1" / "defaults.json").write_text(
        json.dumps({"model": "ws-model", "temperature": 0.4}),
        encoding="utf-8",
    )
    FakeStore.REGISTRY = {
        "s1": _fake_session("s1", {"model": "sess-model", "temperature": 0.5})}

    merged = cm.resolve_full_config(
        workspace_id="ws1",
        session_id="s1",
        worker_overrides={"model": "worker-model", "temperature": 0.6},
    )
    assert merged["model"] == "worker-model"
    assert merged["temperature"] == 0.6
    assert merged["api_key"] == "key-p1"
    assert merged["provider_config"] == {"timeout": 42, "max_retries": 7}
    assert merged["max_turns"] == 300  # global beats factory for GLOBAL_DEFAULT_KEYS
    assert merged["mode"] == "agent"


# ---------------------------------------------------------------------------
# 4/5. Provider profile timeout / max_retries reach the LLMClient
# ---------------------------------------------------------------------------


def test_provider_timeout_flows_to_llm_client(config_chain, monkeypatch):
    """Profile timeout=42 must reach LLMClient (no silent 120s default)."""
    _write_providers(config_chain, [P1_PROFILE])
    monkeypatch.delenv("LLM_TIMEOUT", raising=False)
    monkeypatch.delenv("LLM_MAX_RETRIES", raising=False)
    resolved = cm._resolve_provider_layer({"provider_id": "p1"})
    assert resolved["provider_config"] == {"timeout": 42, "max_retries": 7}
    captured = _capture_create_provider(monkeypatch)
    LLMClient(_llm_stub(resolved))
    assert captured["call"]["timeout"] == 42


def test_provider_max_retries_flows_to_llm_client(config_chain, monkeypatch):
    """Profile max_retries=7 must reach LLMClient (no silent 3-retry default)."""
    _write_providers(config_chain, [P1_PROFILE])
    monkeypatch.delenv("LLM_TIMEOUT", raising=False)
    monkeypatch.delenv("LLM_MAX_RETRIES", raising=False)
    resolved = cm._resolve_provider_layer({"provider_id": "p1"})
    captured = _capture_create_provider(monkeypatch)
    LLMClient(_llm_stub(resolved))
    assert captured["call"]["max_retries"] == 7


# ---------------------------------------------------------------------------
# 6. timeout_seconds from the web UI path
# ---------------------------------------------------------------------------


def test_timeout_seconds_from_web_ui_path(tmp_path):
    """timeout_seconds set through the web UI layer stack must propagate
    SessionConfig -> AgentConfig -> ConfigManager.apply_config -> bridge."""
    from session.store import FileSystemSessionStore

    # SessionConfig -> AgentConfig
    acfg = SessionConfig(timeout_seconds=180).to_agent_config()
    assert acfg.timeout_seconds == 180

    # ConfigManager.apply_config
    _frontend, updated = cm.ConfigManager.apply_config(
        {"timeout_seconds": 180}, SessionConfig(), is_running=False, has_session=False)
    assert updated.timeout_seconds == 180
    assert updated.to_agent_config().timeout_seconds == 180

    # bridge.apply_config (the actual web_ui entry point)
    bridge = bridge_module.WebAgentBridge(
        event_callback=lambda e: None,
        session_store=FileSystemSessionStore(
            sessions_dir=str(tmp_path / "sessions"),
            state_dir=str(tmp_path / "state"),
        ),
    )
    bridge.apply_config({"timeout_seconds": 180})
    assert bridge._session_config.timeout_seconds == 180
    assert bridge._session_config.to_agent_config().timeout_seconds == 180


# ---------------------------------------------------------------------------
# 7. Operator feature flags inherited consistently
# ---------------------------------------------------------------------------


def test_operator_flags_inherited_consistently(tmp_path):
    """Operator flags survive SessionConfig -> AgentConfig and
    -> WorkerThread._build_agent_config."""
    acfg = SessionConfig(
        git_allow_worktree_commits=True,
        allow_host_resources=True,
    ).to_agent_config()
    assert acfg.session_permissions.git_write == 'write'
    assert acfg.allow_host_resources is True

    wt = worker_module.WorkerThread(
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
    worker_acfg = wt._build_agent_config()
    assert worker_acfg is not None
    assert worker_acfg.session_permissions.git_write == 'write'
    assert worker_acfg.allow_host_resources is True


# ---------------------------------------------------------------------------
# 8. provider_config change -> full restart + audit
# ---------------------------------------------------------------------------


def test_hot_swap_provider_config_restarts_and_applies(monkeypatch):
    """provider_config changes are not hot-swappable: the agent must take the
    full-restart branch, apply the new config, and audit with
    restart_required=True."""
    old = AgentConfig(api_key='test-key', enable_logging=False, provider_config={'timeout': 1})
    new = AgentConfig(api_key='test-key', enable_logging=False, provider_config={'timeout': 120})
    agent = Agent(config=old, session_id='cfg-arch-s')
    assert agent._can_hot_swap(new) is False

    captured = {}
    monkeypatch.setattr(
        agent_module, 'log_config_audit', lambda **kw: captured.update(kw))
    agent.request_config_update(new)
    assert agent._apply_pending_config() is True

    assert agent.config.provider_config == {'timeout': 120}
    assert captured.get('restart_required') is True
    assert captured.get('component') == 'agent.restart'
    assert captured.get('new').provider_config == {'timeout': 120}


# ---------------------------------------------------------------------------
# 9. Config-change logs show actual values (and never secrets)
# ---------------------------------------------------------------------------


def test_config_change_logs_actual_values(tmp_path, monkeypatch):
    """bridge.apply_config must log the actual old -> new values and must
    never leak api_key into the diff log."""
    from session.store import FileSystemSessionStore

    records = []
    monkeypatch.setattr(
        bridge_module, 'log',
        lambda level, cat, msg: records.append((level, cat, msg)),
    )
    bridge = bridge_module.WebAgentBridge(
        event_callback=lambda e: None,
        session_store=FileSystemSessionStore(
            sessions_dir=str(tmp_path / "sessions"),
            state_dir=str(tmp_path / "state"),
        ),
    )
    bridge.apply_config({"temperature": 0.9, "model": "m2"})
    diff_msgs = [
        r[2] for r in records
        if r[0] == 'INFO' and r[2].startswith('apply_config:')
        and 'change(s)' in r[2]
    ]
    assert diff_msgs, records
    assert 'temperature' in diff_msgs[0] and '0.9' in diff_msgs[0]
    assert 'model' in diff_msgs[0] and 'm2' in diff_msgs[0]

    # api_key must never show up in any logged message
    bridge.apply_config({"api_key": "sk-super-secret"})
    all_msgs = ' '.join(r[2] for r in records)
    assert 'sk-super-secret' not in all_msgs
    assert 'api_key:' not in all_msgs


# ---------------------------------------------------------------------------
# 10/11. Fail-closed security behaviour
# ---------------------------------------------------------------------------


@_skip_without_gate
def test_missing_workspace_id_fails_closed(monkeypatch):
    """Configured workspace_path + unresolvable workspace_id must DENY tool
    execution with a fail-closed result (no permissive fallback)."""
    monkeypatch.setattr(te_module, "resolve_workspace_id", lambda path: None)
    executor = _make_executor(workspace_path="/some/configured/workspace")
    result = _run(executor)
    assert "DENIED" in result.get("result", "")
    assert "fail-closed" in result.get("result", "")


@_skip_without_gate
def test_permission_fetch_exception_denies(monkeypatch):
    """A permission-gate exception must yield effective_permissions={} plus a
    permission_fetch_error (fail-closed, never raw session permissions)."""
    def boom(*args, **kwargs):
        raise RuntimeError("gate exploded")

    monkeypatch.setattr(check_system_module, "GATE_AVAILABLE", True)
    monkeypatch.setattr(check_system_module, "get_effective_permissions", boom)

    tool = check_system_module.CheckSystem(
        query="my_config", session_permissions={"filesystem": "read"})
    out = tool._query_permissions(None)
    assert out["effective_permissions"] == {}
    assert "gate exploded" in (out.get("permission_fetch_error") or "")


# ---------------------------------------------------------------------------
# 12. No silent AgentConfig(api_key='') fallback
# ---------------------------------------------------------------------------


def test_no_silent_agentconfig_empty_fallback(monkeypatch):
    """A broken agent_config service (exception OR junk data) must raise
    RuntimeError instead of silently building AgentConfig(api_key='')."""
    def _boom():
        raise ValueError("corrupt agent_config.json")

    monkeypatch.setattr(bridge_module, "create_agent_config_service", _boom)
    bridge = bridge_module.WebAgentBridge(event_callback=lambda e: None)
    with pytest.raises(RuntimeError, match="Could not build global agent config"):
        bridge._build_global_agent_config()

    class _JunkService:
        def get_all(self):
            return {"bogus_field": 1}

    monkeypatch.setattr(
        bridge_module, "create_agent_config_service", lambda: _JunkService())
    bridge2 = bridge_module.WebAgentBridge(event_callback=lambda e: None)
    with pytest.raises(RuntimeError, match="Could not build global agent config"):
        bridge2._build_global_agent_config()
