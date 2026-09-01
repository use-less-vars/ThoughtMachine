"""Layer precedence and UI truth.

Five tests verify the configuration layer precedence implemented by
``resolve_full_config`` (factory defaults -> global user defaults ->
agent_config.json -> provider profile -> workspace defaults -> session
config -> worker overrides) by driving the resolver directly against the
fake HOME.

Five tests verify the truthfulness of the UI-facing surfaces: the WS
``config_changed`` event carries the permission grant with the workspace
ceiling applied (read-only); a restart + ``load_session`` re-applies the
workspace permission ceiling; the REST
``GET /api/workspace/{ws_id}/effective_permissions`` endpoint serves
read-only defaults without a session id and the raw grant with one (no
ceiling); the workspace summary reflects the UI permission state; and
the frontend<->backend config translation round-trips tool toggles.

Two genuine findings locked into assertions below:

* The WS ``config_changed`` event's ``permissions`` and
  ``effective_config`` DO apply the workspace permission ceiling - the raw
  write grant stays in the session store - whereas the REST
  ``effective_permissions`` endpoint does NOT apply the ceiling: it
  serves read-only defaults without a session id and the raw stored
  grant with one.
* An explicit ``model`` in agent_config.json wins over a provider
  profile's ``default_model``.

Expected log noise (not failures): docker.verify_integrity connection
errors, "apply_config: mode change to custom rejected" (mode is immutable
after session start), Pydantic V1 deprecations, ObservableList auto-wrap
warnings, "Could not backfill workspace_path from registry" and
"load_session: could not resolve workspace root from registry".
"""
import importlib
import json
from pathlib import Path

import pytest

try:
    from tests.integration import harness
except ImportError:  # non-package layout: tests/integration on sys.path
    import harness

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def env():
    app, tmp_home, stop_fn = harness.start_backend()
    harness.register_mock_provider()
    client = harness.make_client(app)
    yield client, tmp_home, stop_fn
    stop_fn()


def _restart(tmp_home, stop_fn):
    """Keep the fake HOME, purge modules, import a fresh backend."""
    stop_fn(rmtree=False, restore_env=False)
    app2 = harness.restart(tmp_home)
    harness.register_mock_provider()
    return harness.make_client(app2)


def _apply_config_dict(ws_root, session_permissions):
    return {
        "workspace_path": ws_root,
        "mode": "custom",
        "provider": "mock",
        "model": "mock-model",
        "session_permissions": session_permissions,
        "temperature": 0.2,
        "max_turns": 20,
        "enabled_tools": [],
    }


def _load_session(wsock, sid):
    wsock.send_json({"command": "load_session", "session_id": sid})
    evt, _ = harness.receive_until_session_loaded(wsock)
    assert not evt.get("load_error", False), f"session load failed: {evt}"
    return evt


def _post_p1(client):
    resp = harness.post_provider(
        client,
        id="p1",
        label="P1",
        provider_type="openai_compatible",
        base_url="http://127.0.0.1:9",
        api_key="test-key",
        default_model="m1",
        models=["m1"],
        timeout=45,
        max_retries=7,
    )
    assert resp.get("created") is True or resp.get("updated") is True, resp
    return resp


def _delete_p1():
    from agent.config.provider_profile import ProviderManager

    ProviderManager().delete_profile("p1")
    ProviderManager().save()


# ---------------------------------------------------------------------------
# 1-5. Layer precedence (pure resolve_full_config)
# ---------------------------------------------------------------------------

def test_factory_defaults_then_global_layer(env):
    client, tmp_home, stop_fn = env
    vault = Path(tmp_home) / ".thoughtmachine"
    factory = vault / "system" / "factory_defaults.json"
    user_defaults = vault / "user" / "defaults.json"
    factory.parent.mkdir(parents=True, exist_ok=True)
    user_defaults.parent.mkdir(parents=True, exist_ok=True)
    factory.write_text(
        json.dumps({"config": {"temperature": 0.11, "model": "factory-model"}})
    )
    user_defaults.write_text(json.dumps({"temperature": 0.22, "model": "global-model"}))
    try:
        from web_ui.backend.config_manager import resolve_full_config

        cfg = resolve_full_config()
        assert cfg["temperature"] == 0.22, cfg
        assert cfg["model"] == "global-model", cfg
    finally:
        factory.unlink(missing_ok=True)
        user_defaults.unlink(missing_ok=True)


def test_global_layer_then_agent_config(env):
    client, tmp_home, stop_fn = env
    vault = Path(tmp_home) / ".thoughtmachine"
    user_defaults = vault / "user" / "defaults.json"
    agent_config = vault / "agent_config.json"
    user_defaults.parent.mkdir(parents=True, exist_ok=True)
    user_defaults.write_text(json.dumps({"temperature": 0.22}))
    agent_config.write_text(json.dumps({"temperature": 0.33, "max_turns": 7}))
    try:
        from web_ui.backend.config_manager import resolve_full_config

        cfg = resolve_full_config()
        assert cfg["temperature"] == 0.33, cfg
        assert cfg["max_turns"] == 7, cfg
    finally:
        user_defaults.unlink(missing_ok=True)
        agent_config.unlink(missing_ok=True)


def test_agent_config_explicit_model_beats_provider_default_model(env):
    client, tmp_home, stop_fn = env
    agent_config = Path(tmp_home) / ".thoughtmachine" / "agent_config.json"
    agent_config.parent.mkdir(parents=True, exist_ok=True)
    agent_config.write_text(
        json.dumps(
            {
                "model": "agent-model",
                "provider_config": {"timeout": 9},
                "temperature": 0.33,
            }
        )
    )
    _post_p1(client)
    try:
        from web_ui.backend.config_manager import resolve_full_config

        cfg = resolve_full_config(provider_id="p1")
        assert cfg["temperature"] == 0.33, cfg
        assert cfg["model"] == "agent-model", cfg
        assert cfg["provider_config"] == {"timeout": 45, "max_retries": 7}, cfg
        assert cfg["base_url"] == "http://127.0.0.1:9", cfg
        assert cfg["api_key"] == "test-key", cfg
    finally:
        agent_config.unlink(missing_ok=True)
        _delete_p1()


def test_workspace_defaults_beat_provider_layer(env):
    client, tmp_home, stop_fn = env
    _post_p1(client)
    ws = harness.create_workspace(client, tmp_home, name="t4")
    ws_defaults = (
        Path(tmp_home)
        / ".thoughtmachine"
        / "workspaces"
        / ws["workspace_id"]
        / "defaults.json"
    )
    ws_defaults.parent.mkdir(parents=True, exist_ok=True)
    ws_defaults.write_text(
        json.dumps({"model": "ws-model", "provider_config": {"timeout": 30}})
    )
    try:
        from web_ui.backend.config_manager import resolve_full_config

        cfg = resolve_full_config(provider_id="p1", workspace_id=ws["workspace_id"])
        assert cfg["model"] == "ws-model", cfg
        assert cfg["provider_config"] == {"timeout": 30, "max_retries": 7}, cfg
    finally:
        ws_defaults.unlink(missing_ok=True)
        _delete_p1()


def test_session_layer_then_worker_overrides(env):
    client, tmp_home, stop_fn = env
    ws = harness.create_workspace(client, tmp_home, name="t5")
    ws_defaults = (
        Path(tmp_home)
        / ".thoughtmachine"
        / "workspaces"
        / ws["workspace_id"]
        / "defaults.json"
    )
    ws_defaults.parent.mkdir(parents=True, exist_ok=True)
    ws_defaults.write_text(json.dumps({"temperature": 0.44, "max_turns": 44}))
    sid = harness.create_session(client, workspace_path=ws["root"])
    try:
        with harness.ws_connect(client) as wsock:
            _load_session(wsock, sid)
            wsock.send_json(
                {
                    "command": "apply_config",
                    "config": _apply_config_dict(
                        ws["root"], {"git": "read", "filesystem": "read"}
                    ),
                }
            )
            harness.receive_until_type(wsock, "config_changed")

        from web_ui.backend.config_manager import resolve_full_config

        cfg = resolve_full_config(workspace_id=ws["workspace_id"], session_id=sid)
        assert cfg["temperature"] == 0.2, cfg
        assert cfg["max_turns"] == 20, cfg
        cfg2 = resolve_full_config(
            workspace_id=ws["workspace_id"],
            session_id=sid,
            worker_overrides={"temperature": 0.66, "max_turns": 66},
        )
        assert cfg2["temperature"] == 0.66, cfg2
        assert cfg2["max_turns"] == 66, cfg2
    finally:
        ws_defaults.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 6-10. UI truth
# ---------------------------------------------------------------------------

def test_config_changed_event_carries_raw_permission_grant(env):
    client, tmp_home, stop_fn = env
    ws = harness.create_workspace(client, tmp_home, name="t6")
    sid = harness.create_session(client, workspace_path=ws["root"])

    with harness.ws_connect(client) as wsock:
        _load_session(wsock, sid)
        wsock.send_json(
            {
                "command": "apply_config",
                "config": _apply_config_dict(
                    ws["root"], {"git": "write", "filesystem": "write"}
                ),
            }
        )
        evt, _ = harness.receive_until_type(wsock, "config_changed")

    assert evt["permissions"]["git"] == "read", evt["permissions"]
    assert evt["permissions"]["filesystem"] == "read", evt["permissions"]
    assert (
        evt["effective_config"]["session_permissions"]["git"] == "read"
    ), evt["effective_config"]
    assert (
        evt["permissions"]["git"] == evt["effective_config"]["session_permissions"]["git"]
    ), evt

    server_mod = importlib.import_module("web_ui.backend.server")
    session = server_mod._get_session_store().load_session(
        sid, workspace_id=ws["workspace_id"]
    )
    assert session is not None, "session missing from store"
    stored_perms = session.metadata["session_config"]["session_permissions"]
    assert stored_perms["git"] == "write", stored_perms


def test_restart_reapplies_workspace_permission_ceiling_on_session_load(env):
    client, tmp_home, stop_fn = env
    ws = harness.create_workspace(client, tmp_home, name="t7")
    sid = harness.create_session(client, workspace_path=ws["root"])

    with harness.ws_connect(client) as wsock:
        evt = _load_session(wsock, sid)
        assert evt["workspace_id"] == ws["workspace_id"], evt
        assert evt["workspace_path"] == ws["root"], evt
        assert evt["config"]["workspace_path"] == ws["root"], evt
        wsock.send_json(
            {
                "command": "apply_config",
                "config": _apply_config_dict(
                    ws["root"], {"git": "write", "filesystem": "write"}
                ),
            }
        )
        evt, _ = harness.receive_until_type(wsock, "config_changed")

    server_mod = importlib.import_module("web_ui.backend.server")
    session = server_mod._get_session_store().load_session(
        sid, workspace_id=ws["workspace_id"]
    )
    assert session is not None, "session missing from store"
    stored_perms = session.metadata["session_config"]["session_permissions"]
    assert stored_perms["git"] == "write", stored_perms

    client2 = _restart(tmp_home, stop_fn)
    with harness.ws_connect(client2) as wsock:
        evt = _load_session(wsock, sid)
        perms = evt["config"]["session_permissions"]
        assert perms["git"] == "read", perms
        assert perms["filesystem"] == "read", perms


def test_rest_effective_permissions_serve_defaults_then_raw_grant(env):
    client, tmp_home, stop_fn = env
    ws = harness.create_workspace(client, tmp_home, name="t8")
    harness.put_permissions(
        client, ws["workspace_id"], {"git": "read", "filesystem": "write"}
    )

    ep = harness.get_effective_permissions(client, ws["workspace_id"])
    perms = ep["effective_permissions"]
    assert perms["git"] == "read", ep
    assert perms["filesystem"] == "read", ep
    assert perms["network"] == "banned", ep
    assert perms["container"] is False, ep

    sid = harness.create_session(client, workspace_path=ws["root"])
    with harness.ws_connect(client) as wsock:
        _load_session(wsock, sid)
        wsock.send_json(
            {
                "command": "apply_config",
                "config": _apply_config_dict(
                    ws["root"], {"git": "write", "filesystem": "write"}
                ),
            }
        )
        harness.receive_until_type(wsock, "config_changed")

    ep2 = harness.get_effective_permissions(client, ws["workspace_id"], session_id=sid)
    assert ep2["effective_permissions"]["git"] == "write", ep2


def test_workspace_summary_reflects_ui_permission_state(env):
    client, tmp_home, stop_fn = env
    ws = harness.create_workspace(client, tmp_home, name="t9")
    harness.put_permissions(
        client, ws["workspace_id"], {"git": "read", "filesystem": "write"}
    )

    s = harness.get_summary(client, ws["workspace_id"])
    assert s["permissions"]["git"] == "read", s
    assert s["permissions"]["filesystem"] == "write", s
    assert s["allow_host_resources"] is False, s
    assert s["workspace_id"] == ws["workspace_id"], s


def test_frontend_backend_config_translation_roundtrip(env):
    client, tmp_home, stop_fn = env
    from web_ui.backend.config_manager import (
        backend_to_frontend_config,
        translate_frontend_config,
    )

    fe = {
        "provider": "openai",
        "model": "m",
        "temperature": 0.5,
        "tools": [
            {"name": "git_read", "enabled": True},
            {"name": "git_write", "enabled": False},
        ],
    }
    backend = translate_frontend_config(fe)
    assert backend["provider_type"] == "openai", backend
    assert backend["enabled_tools"] == ["git_read"], backend

    fe2 = backend_to_frontend_config(
        {**backend, "mode": "custom", "session_permissions": {}, "allow_host_resources": False}
    )
    assert fe2["provider"] == "openai", fe2
    tools = {t["name"]: t for t in fe2["tools"]}
    assert tools["git_read"]["enabled"] is True, fe2
    assert tools["git_write"]["enabled"] is False, fe2

    fe3 = backend_to_frontend_config(
        {"provider_type": "openai_compatible", "mode": "custom", "enabled_tools": []}
    )
    assert fe3["provider"] == "local", fe3


