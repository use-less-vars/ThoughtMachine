"""Configuration control matrix: persistence across restarts + propagation.

Five tests verify that workspace permission ceilings, session permission
grants, session worker timeouts, provider settings and host-resource flags
survive a simulated process restart (fresh backend import against the same
fake HOME).  Three tests verify config propagation: provider timeout into the
LLM client, session worker timeout into WorkerManager/WorkerThread, and the
workspace permission ceiling capping a session's effective permissions.

Restart flow used throughout: ``stop_fn(rmtree=False, restore_env=False)``
keeps the fake HOME, then ``harness.restart(tmp_home)`` purges ``sys.modules``
and imports a fresh backend - a cold start against the persisted state.
"""
import importlib
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


class _FakeRegistry:
    """Dict-backed stand-in for the worker registry."""

    def __init__(self):
        self._workers = {}

    def register_worker(self, session_id, worker_name, thread, instance_id=1):
        self._workers[(session_id or "", worker_name, instance_id)] = thread

    def unregister_worker(self, session_id, worker_name, instance_id=1, default=None):
        return self._workers.pop(
            (session_id or "", worker_name, instance_id), default
        )

    def get_worker(self, session_id, worker_name, instance_id=1, default=None):
        return self._workers.get(
            (session_id or "", worker_name, instance_id), default
        )

    def get_all_workers(self):
        # The real registry returns a mapping key -> thread (WorkerManager
        # iterates ``.items()`` in ``alive_workers``).
        return dict(self._workers)

    def find_workers_by_name(self, worker_name):
        return [
            (key, thread)
            for key, thread in self._workers.items()
            if key[1] == worker_name
        ]


# ---------------------------------------------------------------------------
# 1-5. Persistence across restart
# ---------------------------------------------------------------------------

def test_persistence_workspace_permission_ceiling_survives_restart(env):
    client, tmp_home, stop_fn = env
    ws = harness.create_workspace(client, tmp_home, name="t1")
    harness.put_permissions(
        client, ws["workspace_id"], {"git": "read", "filesystem": "write"}
    )

    client2 = _restart(tmp_home, stop_fn)
    summary = harness.get_summary(client2, ws["workspace_id"])
    perms = summary["permissions"]
    assert perms["git"] == "read", perms
    assert perms["filesystem"] == "write", perms


def test_persistence_session_permission_grants_survive_restart_capped_by_ceiling(env):
    """Session permission grants persist across restart at the store level, but
    the effective permissions served by ``load_session`` are capped by the
    workspace permission ceiling (the default ceiling created with the
    workspace is read-only), so the reloaded effective perms are read/read.
    """
    client, tmp_home, stop_fn = env
    ws = harness.create_workspace(client, tmp_home, name="t2")
    sid = harness.create_session(client, workspace_path=ws["root"])

    with harness.ws_connect(client) as wsock:
        evt = _load_session(wsock, sid)
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

    # (i) Persistence: the grant survives restart at the store level.
    server_mod = importlib.import_module("web_ui.backend.server")
    session = server_mod._get_session_store().load_session(
        sid, workspace_id=ws["workspace_id"]
    )
    assert session is not None, "session missing from store"
    stored_perms = session.metadata["session_config"]["session_permissions"]
    assert stored_perms["git"] == "write", stored_perms
    assert stored_perms["filesystem"] == "write", stored_perms

    # Restart and reload over the wire.
    client2 = _restart(tmp_home, stop_fn)

    # Store-level metadata still carries the original write/write grant.
    server_mod2 = importlib.import_module("web_ui.backend.server")
    session2 = server_mod2._get_session_store().load_session(
        sid, workspace_id=ws["workspace_id"]
    )
    assert session2 is not None, "session missing from store after restart"
    stored2 = session2.metadata["session_config"]["session_permissions"]
    assert stored2["git"] == "write", stored2
    assert stored2["filesystem"] == "write", stored2

    # (ii) Precedence: load_session re-applies the workspace ceiling, and the
    # default workspace ceiling is read-only, so effective perms are read/read.
    with harness.ws_connect(client2) as wsock:
        evt = _load_session(wsock, sid)
        perms = evt["config"]["session_permissions"]
        assert perms["git"] == "read", perms
        assert perms["filesystem"] == "read", perms


def test_persistence_session_worker_timeout_survives_restart(env):
    client, tmp_home, stop_fn = env
    ws = harness.create_workspace(client, tmp_home, name="t3")
    sid = harness.create_session(client, workspace_path=ws["root"])

    # NOTE: WS apply_config does NOT persist worker_timeout_seconds - it is
    # absent from ConfigManager.apply_config's mutable-field list
    # (provider_id, model, base_url, temperature, top_p, max_turns,
    # timeout_seconds, token_monitor_warning_threshold,
    # token_monitor_critical_threshold).  We therefore inject the value
    # directly through the session store (the documented workaround) and
    # verify it survives a restart.
    server_mod = importlib.import_module("web_ui.backend.server")
    store = server_mod._get_session_store()
    session = store.load_session(sid, workspace_id=ws["workspace_id"])
    assert session is not None, "session missing from store"
    session.metadata.setdefault("session_config", {})["worker_timeout_seconds"] = 120
    store.save_session(session, workspace_id=ws["workspace_id"])

    # Restart: the value must be readable back from the store...
    client2 = _restart(tmp_home, stop_fn)
    server_mod2 = importlib.import_module("web_ui.backend.server")
    session2 = server_mod2._get_session_store().load_session(
        sid, workspace_id=ws["workspace_id"]
    )
    assert session2 is not None, "session missing from store after restart"
    assert (
        session2.metadata["session_config"]["worker_timeout_seconds"] == 120
    ), session2.metadata["session_config"]

    # ...and flow through to the loaded frontend config.
    with harness.ws_connect(client2) as wsock:
        evt = _load_session(wsock, sid)
        assert evt["config"]["worker_timeout_seconds"] == 120, evt["config"]


def test_persistence_provider_settings_survive_restart(env):
    client, tmp_home, stop_fn = env
    provider = {
        "id": "p1",
        "label": "P1",
        "provider_type": "openai_compatible",
        "base_url": "http://127.0.0.1:9",
        "api_key": "test-key",
        "default_model": "m1",
        "models": ["m1"],
        "timeout": 45,
        "max_retries": 7,
    }
    resp = harness.post_provider(client, **provider)
    assert resp.get("created") is True, resp

    client2 = _restart(tmp_home, stop_fn)
    providers = harness.get_providers(client2)
    by_id = {p["id"]: p for p in providers}
    assert "p1" in by_id, providers
    assert by_id["p1"]["timeout"] == 45, by_id["p1"]
    assert by_id["p1"]["default_model"] == "m1", by_id["p1"]


def test_persistence_allow_host_resources_survives_restart(env):
    client, tmp_home, stop_fn = env
    ws = harness.create_workspace(client, tmp_home, name="t5")
    harness.put_permissions(
        client, ws["workspace_id"], {}, allow_host_resources=True
    )

    client2 = _restart(tmp_home, stop_fn)
    summary = harness.get_summary(client2, ws["workspace_id"])
    assert summary["allow_host_resources"] is True, summary


# ---------------------------------------------------------------------------
# 6-8. Config propagation
# ---------------------------------------------------------------------------

def test_provider_timeout_propagates_to_llm_client(monkeypatch):
    from types import SimpleNamespace

    from agent.core.llm_client import LLMClient
    from llm_providers.factory import ProviderFactory

    captured = {}

    def _fake_create_provider(provider_type, api_key=None, **kwargs):
        captured["call"] = dict(provider_type=provider_type, api_key=api_key, **kwargs)
        return object()

    monkeypatch.setattr(ProviderFactory, "create_provider", _fake_create_provider)

    config = SimpleNamespace(
        provider_type="openai_compatible",
        api_key="test-key",
        base_url=None,
        model="test-model",
        temperature=0.2,
        provider_config={"timeout": 45, "max_retries": 7},
    )
    LLMClient(config)

    assert captured["call"]["timeout"] == 45, captured["call"]
    assert captured["call"]["max_retries"] == 7, captured["call"]


def test_session_worker_timeout_propagates_to_workermanager(tmp_path):
    from unittest import mock

    from tools.workspace.worker_manager import WorkerManager
    from tools.workspace.worker_thread import WorkerThread

    definition = {
        "name": "test-worker",
        "description": "desc",
        "system_prompt": "sys",
        "tools": [],
        "permission_footprint": {},
    }
    agent_config = {"session_config": {"worker_timeout_seconds": 120}}
    thread = WorkerThread(
        name="test-worker",
        definition=definition,
        agent_config=agent_config,
        workspace_dir=Path(tmp_path),
    )
    assert thread._timeout_seconds == 120, thread._timeout_seconds

    class _FakeThread:
        worker_name = "w"
        instance_id = 1
        context_tag = None
        _timeout_seconds = 123

        def is_alive(self):
            return True

    registry = _FakeRegistry()
    mgr = WorkerManager(registry=registry)
    with mock.patch(
        "tools.workspace.worker_manager.deliver_query_and_block",
        return_value={"ok": True},
    ) as deliver:
        envelope = mgr.request_worker("s1", "query", spawner=lambda: _FakeThread())

    assert deliver.call_args.kwargs["timeout"] == 123, deliver.call_args
    assert envelope["delivery"]["spawned"] is True, envelope


def test_workspace_ceiling_caps_session_effective_permission(env):
    """A session permission grant can never exceed the workspace ceiling.

    The test drives the real WS flow to persist a ``git: write`` grant, then
    asserts the cap through the exact recap code path ``bridge.load_session``
    uses (bridge.py): ``apply_workspace_ceiling(ceiling, stored_perms)`` with
    the real workspace ceiling loaded by
    ``config_manager._load_workspace_permission_ceiling``.

    Why not assert the cap via a second WS ``load_session``? server.py's
    ``load_session`` handler takes the cached-bridge reuse path when a live
    bridge already exists for the session (same process/connection): it
    streams the live state without re-running ``bridge.load_session``, so the
    recap only fires on the cold/fresh-bridge path (e.g. after a restart).
    """
    client, tmp_home, stop_fn = env
    ws = harness.create_workspace(client, tmp_home, name="t8")
    harness.put_permissions(client, ws["workspace_id"], {"git": "read"})
    sid = harness.create_session(client, workspace_path=ws["root"])

    with harness.ws_connect(client) as wsock:
        evt = _load_session(wsock, sid)
        assert evt["config"]["workspace_path"] == ws["root"], evt
        wsock.send_json(
            {
                "command": "apply_config",
                "config": _apply_config_dict(ws["root"], {"git": "write"}),
            }
        )
        evt, _ = harness.receive_until_type(wsock, "config_changed")

    # Real stored grant (persisted by the WS flow above).
    server_mod = importlib.import_module("web_ui.backend.server")
    session = server_mod._get_session_store().load_session(
        sid, workspace_id=ws["workspace_id"]
    )
    assert session is not None, "session missing from store"
    stored_perms = session.metadata["session_config"]["session_permissions"]
    assert stored_perms["git"] == "write", stored_perms

    # The same recap code path bridge.load_session uses:
    #   ceiling = _load_workspace_permission_ceiling(workspace_id)
    #   sc.session_permissions = apply_workspace_ceiling(ceiling, stored_perms)
    from security.security_gate import apply_workspace_ceiling
    from web_ui.backend.config_manager import _load_workspace_permission_ceiling

    ceiling = _load_workspace_permission_ceiling(ws["workspace_id"])
    assert ceiling.get("git") == "read", ceiling
    effective = apply_workspace_ceiling(ceiling, dict(stored_perms))
    assert effective["git"] == "read", effective
    assert effective["git"] != stored_perms["git"], (
        "session grant must be capped by the workspace ceiling"
    )
