"""Tests for the provider REST CRUD API (GET/POST/DELETE /api/providers).

Hermetic: the routes live in ``provider_routes`` (import is side-effect-free);
a lightweight FastAPI app includes only ``provider_routes.router``.
``provider_routes.PROVIDERS_STORE`` is pointed at a throwaway tmp file and
``agent.config.provider_profile.THOUGHTMACHINE_DIR`` at a throwaway tmp dir,
so ``ProviderManager.save()`` never touches the real ``~/.thoughtmachine``.
"""

import json

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import agent.config.provider_profile as provider_profile
import web_ui.backend.provider_routes as provider_routes


@pytest.fixture
def app():
    application = FastAPI()
    application.include_router(provider_routes.router)
    return application


@pytest.fixture
def client(app, monkeypatch, tmp_path):
    monkeypatch.setattr(
        provider_profile, "THOUGHTMACHINE_DIR", tmp_path
    )
    store = tmp_path / "providers.json"
    monkeypatch.setattr(provider_routes, "PROVIDERS_STORE", store)
    with TestClient(app) as test_client:
        yield test_client


def _post(client, provider):
    return client.post("/api/providers", json={"provider": provider})


def _provider(**overrides):
    data = {
        "id": "openai",
        "label": "OpenAI",
        "provider_type": "openai_compatible",
        "base_url": "https://api.openai.com/v1",
        "api_key": "sk-test-key",
        "default_model": "gpt-4o",
        "models": ["gpt-4o", "gpt-4o-mini"],
        "timeout": 120,
    }
    data.update(overrides)
    return data


def test_providers_get_empty_initial(client, tmp_path):
    resp = client.get("/api/providers")

    assert resp.status_code == 200
    assert resp.json() == []


def test_providers_post_creates_and_persists(client, tmp_path):
    resp = _post(client, _provider())

    assert resp.status_code == 200
    body = resp.json()
    assert body["created"] is True
    assert body["provider"]["id"] == "openai"
    assert body["provider"]["api_key"] == "sk-test-key"

    store_file = tmp_path / "providers.json"
    stored = json.loads(store_file.read_text(encoding="utf-8"))
    assert [p["id"] for p in stored["profiles"]] == ["openai"]

    listing = client.get("/api/providers").json()
    assert [p["id"] for p in listing] == ["openai"]
    assert listing[0]["api_key"] == "sk-test-key"


def test_providers_post_update_existing(client, tmp_path):
    assert _post(client, _provider()).json()["created"] is True

    resp = _post(client, _provider(label="OpenAI (prod)", default_model="gpt-4.1"))

    assert resp.status_code == 200
    body = resp.json()
    assert body["updated"] is True
    assert body["provider"]["label"] == "OpenAI (prod)"
    assert body["provider"]["default_model"] == "gpt-4.1"
    assert len(client.get("/api/providers").json()) == 1


def test_providers_post_empty_api_key_preserves_existing(client, tmp_path):
    assert _post(client, _provider()).json()["created"] is True

    resp = _post(client, _provider(api_key=""))

    assert resp.status_code == 200
    assert resp.json()["updated"] is True
    assert resp.json()["provider"]["api_key"] == "sk-test-key"


def test_providers_delete_removes(client, tmp_path):
    _post(client, _provider())

    resp = client.delete("/api/providers/openai")

    assert resp.status_code == 200
    assert resp.json() == {"deleted": True}
    assert client.get("/api/providers").json() == []


def test_providers_delete_missing_returns_404(client):
    resp = client.delete("/api/providers/does-not-exist")

    assert resp.status_code == 404
    body = resp.json()
    assert "error" in body
    assert "does-not-exist" in body["error"]


def test_providers_post_missing_id_400(client):
    resp = _post(client, {"label": "No id here"})

    assert resp.status_code == 400
    assert "Provider must have an id" in resp.json()["error"]


def test_providers_post_unknown_field_400(client):
    resp = _post(client, _provider(bogus_field="nope"))

    assert resp.status_code == 400
    body = resp.json()
    assert "error" in body
    assert "Invalid provider" in body["error"]
