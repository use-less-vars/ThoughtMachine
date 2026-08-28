"""
Tests for the first-run wizard REST endpoints (/api/onboarding/*).

The clean_home fixture patches HOME/Path.home() so the vault lives in a
temp directory; the TestClient is created WITHOUT the ``with`` context
manager so the app lifespan does not run (the lifespan auto-registers the
project root as a workspace, which would make onboarding appear complete).
"""
from __future__ import annotations

import json
import os
import pathlib
import sys as sys_mod
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def clean_home():
    """Create temp HOME, patch Path.home() + HOME env, clear API keys."""
    import importlib
    import shutil

    tmp_home = tempfile.mkdtemp(prefix="test_onboarding_home_")
    fake_home_path = Path(tmp_home)

    old_home_env = os.environ.get("HOME")
    os.environ["HOME"] = tmp_home

    saved_env = {}
    for key in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_COMPATIBLE_API_KEY", "ANTHROPIC_API_KEY"):
        saved_env[key] = os.environ.pop(key, None)

    patcher = patch.object(pathlib.Path, "home", return_value=fake_home_path)
    patcher.start()

    mod_prefixes = (
        "web_ui.backend",
        "agent.config.provider_profile",
        "thoughtmachine.bootstrap",
        "thoughtmachine.workspace_registry",
    )
    for mod_name in list(sys_mod.modules.keys()):
        if any(mod_name.startswith(p) for p in mod_prefixes):
            del sys_mod.modules[mod_name]

    _project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _project_root not in sys_mod.path:
        sys_mod.path.insert(0, _project_root)

    server_mod = importlib.import_module("web_ui.backend.server")
    app = server_mod.app

    yield app, tmp_home

    patcher.stop()
    if old_home_env is not None:
        os.environ["HOME"] = old_home_env
    else:
        os.environ.pop("HOME", None)
    for key, val in saved_env.items():
        if val is not None:
            os.environ[key] = val
    shutil.rmtree(tmp_home, ignore_errors=True)


@pytest.fixture
def client(clean_home):
    """TestClient WITHOUT context manager: lifespan does not run, so no
    workspace is auto-registered and a fresh vault reports incomplete."""
    app, _ = clean_home
    return TestClient(app)


def _write_marker(tmp_home: str, payload) -> Path:
    """Write the onboarding marker file, returning its path."""
    vault = Path(tmp_home) / ".thoughtmachine"
    vault.mkdir(parents=True, exist_ok=True)
    marker = vault / "onboarding_complete.json"
    marker.write_text(json.dumps(payload), encoding="utf-8")
    return marker


def _write_provider_profile(tmp_home: str) -> Path:
    """Write a providers.json with one valid profile, returning its path."""
    vault = Path(tmp_home) / ".thoughtmachine"
    vault.mkdir(parents=True, exist_ok=True)
    providers_file = vault / "providers.json"
    data = {
        "profiles": [
            {
                "id": "test-provider",
                "label": "Test Provider",
                "provider_type": "openai_compatible",
                "base_url": "",
                "api_key": "sk-test",
                "default_model": "",
                "models": [],
                "timeout": 120,
            }
        ],
        "active_profile_id": None,
    }
    providers_file.write_text(json.dumps(data), encoding="utf-8")
    return providers_file


class TestOnboardingStatus:
    def test_status_false_when_empty(self, client):
        resp = client.get("/api/onboarding/status")
        assert resp.status_code == 200
        assert resp.json() == {"onboarding_complete": False}

    def test_status_false_with_malformed_marker(self, client, clean_home):
        _, tmp_home = clean_home
        marker = _write_marker(tmp_home, "{not valid json")
        assert marker.exists()
        resp = client.get("/api/onboarding/status")
        assert resp.status_code == 200
        assert resp.json() == {"onboarding_complete": False}

    def test_status_true_with_provider_profile(self, client, clean_home):
        _, tmp_home = clean_home
        _write_provider_profile(tmp_home)
        resp = client.get("/api/onboarding/status")
        assert resp.status_code == 200
        assert resp.json() == {"onboarding_complete": True}

    def test_status_true_with_workspace(self, client, clean_home):
        from thoughtmachine.workspace_registry import WorkspaceRegistry

        workspace_root = Path(tempfile.mkdtemp(prefix="test_workspace_root_"))
        try:
            WorkspaceRegistry.get_default().register_by_root(
                str(workspace_root), label="Test Workspace"
            )
            resp = client.get("/api/onboarding/status")
            assert resp.status_code == 200
            assert resp.json() == {"onboarding_complete": True}
        finally:
            import shutil

            shutil.rmtree(workspace_root, ignore_errors=True)


class TestOnboardingComplete:
    def test_complete_and_status_true(self, client, clean_home):
        _, tmp_home = clean_home
        resp = client.post("/api/onboarding/complete")
        assert resp.status_code == 200
        assert resp.json() == {"onboarding_complete": True}

        marker = Path(tmp_home) / ".thoughtmachine" / "onboarding_complete.json"
        assert marker.exists()
        assert json.loads(marker.read_text(encoding="utf-8")) == {"completed": True}

        status = client.get("/api/onboarding/status")
        assert status.json() == {"onboarding_complete": True}

    def test_complete_idempotent(self, client, clean_home):
        _, tmp_home = clean_home
        first = client.post("/api/onboarding/complete")
        second = client.post("/api/onboarding/complete")
        assert first.status_code == 200
        assert second.status_code == 200
        assert first.json() == {"onboarding_complete": True}
        assert second.json() == {"onboarding_complete": True}

    def test_complete_repairs_malformed_marker(self, client, clean_home):
        _, tmp_home = clean_home
        marker = _write_marker(tmp_home, "garbage")
        assert marker.exists()

        resp = client.post("/api/onboarding/complete")
        assert resp.status_code == 200
        assert resp.json() == {"onboarding_complete": True}
        assert json.loads(marker.read_text(encoding="utf-8")) == {"completed": True}


class TestConnection:
    def test_connection_ok(self, client, monkeypatch):
        from llm_providers.factory import ProviderFactory

        class FakeProvider:
            def chat_completion(self, messages, **kwargs):
                assert messages == [{"role": "user", "content": "ping"}]
                return None

        monkeypatch.setattr(
            ProviderFactory, "create_provider", staticmethod(lambda *a, **kw: FakeProvider())
        )
        resp = client.post(
            "/api/onboarding/test-connection",
            json={
                "provider": "openai_compatible",
                "api_key": "sk-test",
                "base_url": "https://example.com/v1",
                "model": "gpt-4o-mini",
            },
        )
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

    def test_connection_failure_no_key_leak(self, client, monkeypatch):
        from llm_providers.factory import ProviderFactory

        def fake_factory(*args, **kwargs):
            raise RuntimeError("boom sk-test-abc")

        monkeypatch.setattr(ProviderFactory, "create_provider", staticmethod(fake_factory))
        resp = client.post(
            "/api/onboarding/test-connection",
            json={"provider": "openai_compatible", "api_key": "sk-test-abc"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        assert "sk-test-abc" not in resp.text
        assert "sk-" not in body["error"]

    def test_connection_failure_chat_completion_raises(self, client, monkeypatch):
        from llm_providers.factory import ProviderFactory

        class FailingProvider:
            def chat_completion(self, messages, **kwargs):
                raise RuntimeError("connection refused for sk-super-secret-key")

        monkeypatch.setattr(
            ProviderFactory, "create_provider", staticmethod(lambda *a, **kw: FailingProvider())
        )
        resp = client.post(
            "/api/onboarding/test-connection",
            json={"provider": "openai_compatible", "api_key": "sk-super-secret-key"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        assert "sk-super-secret-key" not in resp.text

    def test_connection_invalid_provider(self, client, monkeypatch):
        from llm_providers.factory import ProviderFactory
        from llm_providers.exceptions import ProviderNotFoundError

        def fake_factory(*args, **kwargs):
            raise ProviderNotFoundError(
                "Provider 'nope' not found. Available: ['openai_compatible']"
            )

        monkeypatch.setattr(ProviderFactory, "create_provider", staticmethod(fake_factory))
        resp = client.post(
            "/api/onboarding/test-connection",
            json={"provider": "nope", "api_key": "sk-secret"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        assert "nope" in body["error"]
        assert "sk-secret" not in resp.text
