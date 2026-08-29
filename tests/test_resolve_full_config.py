"""Tests for the full-config layer chain (``resolve_full_config`` and friends).

Covers precedence (fallback < factory < global defaults < agent_config.json
< provider profile < workspace < session < worker overrides), layer key
ownership (GLOBAL_DEFAULT_KEYS filter), provider profile resolution +
fallback, strict-schema filtering, and never-raises behaviour with missing
files.
"""

import json
from types import SimpleNamespace

import pytest

import web_ui.backend.config_manager as cm
from agent.config.session_config import SessionConfig
from agent.config.models import AgentConfig


# ── Helpers ────────────────────────────────────────────────────────────────


class FakeStore:
    """Drop-in replacement for FileSystemSessionStore used by resolve_full_config.

    resolve_full_config instantiates the store via ``FileSystemSessionStore()``,
    so the patched value must be a *class*.  Sessions live in a class-level
    REGISTRY that individual tests set directly.
    """

    REGISTRY = {}

    def __init__(self):
        pass

    def load_session(self, session_id, workspace_id=None):
        return FakeStore.REGISTRY.get(session_id)


@pytest.fixture(autouse=True)
def _config_chain_paths(tmp_path, monkeypatch):
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


# ── Full precedence chain ──────────────────────────────────────────────────


def test_full_precedence_chain(tmp_path, monkeypatch):
    """Lowest→highest: factory < global < agent_config < provider < workspace
    < session < worker overrides; provider fields come from the profile."""
    (tmp_path / "system").mkdir(parents=True, exist_ok=True)
    (tmp_path / "system" / "factory_defaults.json").write_text(
        json.dumps({"config": {
            "provider_id": "p1",
            "model": "factory-model",
            "temperature": 0.1,
            "max_turns": 100,
        }}),
        encoding="utf-8",
    )
    (tmp_path / "user").mkdir(parents=True, exist_ok=True)
    (tmp_path / "user" / "defaults.json").write_text(
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
    _write_providers(tmp_path, [P1_PROFILE])
    (tmp_path / "workspaces" / "ws1").mkdir(parents=True, exist_ok=True)
    (tmp_path / "workspaces" / "ws1" / "defaults.json").write_text(
        json.dumps({"model": "ws-model", "temperature": 0.4}),
        encoding="utf-8",
    )
    FakeStore.REGISTRY = {"s1": _fake_session("s1", {"model": "sess-model", "temperature": 0.5})}

    merged = cm.resolve_full_config(
        workspace_id="ws1",
        session_id="s1",
        worker_overrides={"model": "worker-model", "temperature": 0.6},
    )
    assert merged["model"] == "worker-model"
    assert merged["temperature"] == 0.6
    assert merged["api_key"] == "key-p1"
    assert merged["base_url"] == "https://p1.example"
    assert merged["provider_config"] == {"timeout": 42, "max_retries": 7}
    assert merged["mode"] == "agent"
    # GLOBAL_DEFAULT_KEYS flow: global overrides factory for max_turns
    assert merged["max_turns"] == 300


def test_global_layer_ownership(tmp_path):
    """Global-defaults layer only contributes GLOBAL_DEFAULT_KEYS; mode/tools/
    session_permissions must come from the fallback."""
    (tmp_path / "user").mkdir(parents=True, exist_ok=True)
    (tmp_path / "user" / "defaults.json").write_text(
        json.dumps({
            "model": "g-model",
            "mode": "custom",
            "enabled_tools": ["x"],
            "session_permissions": {"container": True},
            "temperature": 0.7,
        }),
        encoding="utf-8",
    )
    merged = cm.resolve_full_config()
    assert merged["model"] == "g-model"
    assert merged["temperature"] == 0.7
    assert merged["mode"] == "agent"
    assert merged["enabled_tools"] == cm.FALLBACK_FRONTEND_CONFIG["enabled_tools"]
    assert merged["session_permissions"] == cm.FALLBACK_FRONTEND_CONFIG["session_permissions"]
    assert merged["session_permissions"]["container"] is False


# ── Provider layer ─────────────────────────────────────────────────────────


def test_provider_layer_unit(tmp_path):
    _write_providers(tmp_path, [P1_PROFILE])
    resolved = cm._resolve_provider_layer({"provider_id": "p1", "model": "explicit-m"})
    assert resolved["model"] == "explicit-m"
    assert resolved["api_key"] == "key-p1"
    assert resolved["base_url"] == "https://p1.example"
    assert resolved["provider_config"] == {"timeout": 42, "max_retries": 7}

    resolved2 = cm._resolve_provider_layer({"provider_id": "p1"})
    assert resolved2["model"] == "model-p1"


def test_provider_fallback_any(tmp_path):
    _write_providers(tmp_path, [{"id": "p2", "label": "P2", "api_key": "key-p2"}])
    resolved = cm._resolve_provider_layer({"provider_id": "missing"})
    assert resolved["api_key"] == "key-p2"
    assert resolved["provider_id"] == "p2"


def test_session_mode_default():
    """Missing mode in session_config is repaired to 'agent'."""
    FakeStore.REGISTRY = {"s1": _fake_session("s1", {"model": "sm"})}
    merged = cm.resolve_full_config(session_id="s1")
    assert merged["mode"] == "agent"
    assert merged["model"] == "sm"


def test_worker_overrides_beat_session():
    FakeStore.REGISTRY = {"s1": _fake_session("s1", {"model": "sess-m"})}
    merged = cm.resolve_full_config(
        session_id="s1", worker_overrides={"model": "w-m"}
    )
    assert merged["model"] == "w-m"


# ── Strict-schema filtering ────────────────────────────────────────────────


def test_session_config_from_merged_filters():
    merged = {
        "tools": ["read_file"],
        "provider": "local",
        "api_key_configured": True,
        "model": None,
        "mode": "agent",
        "temperature": 0.5,
    }
    filtered = cm._filter_model_fields(merged, SessionConfig)
    assert "tools" not in filtered
    assert "provider" not in filtered
    assert "api_key_configured" not in filtered
    assert "model" not in filtered  # None values dropped
    cfg = cm.session_config_from_merged(merged)
    assert isinstance(cfg, SessionConfig)
    assert cfg.mode == "agent"
    assert cfg.temperature == 0.5


def test_agent_config_from_merged_filters():
    merged = {
        "provider": "local",
        "api_key_configured": True,
        "model": "deepseek-v4-flash",
        "mode": "agent",
        "temperature": 0.5,
    }
    filtered = cm._filter_model_fields(merged, AgentConfig)
    assert "provider" not in filtered
    assert "api_key_configured" not in filtered
    cfg = cm.agent_config_from_merged(merged)
    assert isinstance(cfg, AgentConfig)
    assert cfg.model == "deepseek-v4-flash"
    assert cfg.mode == "agent"


# ── Never-raises behaviour ─────────────────────────────────────────────────


def test_never_raises_missing_files(tmp_path):
    """All layer files absent → fallback config, no exception."""
    merged = cm.resolve_full_config(workspace_id="nope", session_id="nope")
    assert isinstance(merged, dict)
    assert merged["model"] == "deepseek-v4-flash"
    assert merged["mode"] == "agent"


# ── default_frontend_config ────────────────────────────────────────────────


def test_default_frontend_config_api_key(tmp_path):
    _write_providers(tmp_path, [P1_PROFILE])
    result = cm.default_frontend_config()
    assert result["api_key_configured"] is True
    assert "api_key" not in result


def test_default_frontend_config_no_key(tmp_path, monkeypatch):
    for var in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_COMPATIBLE_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    result = cm.default_frontend_config()
    assert result["api_key_configured"] is False


# ── ConfigManager staticmethod ─────────────────────────────────────────────


def test_config_manager_staticmethod():
    merged = cm.ConfigManager.resolve_full_config(worker_overrides={"model": "x"})
    assert merged["model"] == "x"


# ── server.set_default_config fallback args (Edit 13 expression) ───────────


def test_server_set_default_config_fallback_args():
    """The Edit-13 fallback expression must forward bridge state to
    resolve_full_config (workspace_id / _session_id / _session_config.provider_id)."""
    bridge = SimpleNamespace(
        get_config=lambda: None,
        workspace_id="ws1",
        _session_id="s1",
        _loaded_session=None,
        _session_config=SimpleNamespace(provider_id="p1"),
    )
    captured = {}

    class FakeCM:
        @staticmethod
        def resolve_full_config(**kwargs):
            captured.update(kwargs)
            return {}

    config_manager = FakeCM()
    cfg_dict = bridge.get_config() or config_manager.resolve_full_config(
        workspace_id=getattr(bridge, "workspace_id", None),
        session_id=getattr(bridge, "_session_id", None)
        or (
            bridge._loaded_session.session_id
            if getattr(bridge, "_loaded_session", None) else None
        ),
        provider_id=(
            bridge._session_config.provider_id
            if getattr(bridge, "_session_config", None) else None
        ),
    )
    assert cfg_dict == {}
    assert captured["workspace_id"] == "ws1"
    assert captured["session_id"] == "s1"
    assert captured["provider_id"] == "p1"
