"""Tests for the global credentials CRUD API (GET/POST/DELETE /api/credentials).

Hermetic: the routes live in ``global_routes`` (import is side-effect-free);
a lightweight FastAPI app includes only ``global_routes.router`` and
``global_routes.vault_root`` is pointed at a throwaway tmp vault so no real
``~/.thoughtmachine`` state is touched.  Secret values must never appear in
any HTTP response body (only the file names / booleans are returned).
"""

import os
import stat

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import web_ui.backend.global_routes as global_routes

_SECRET_VALUE = "ghp_super_secret_value_123"


@pytest.fixture
def app():
    application = FastAPI()
    application.include_router(global_routes.router)
    return application


@pytest.fixture
def client(app, monkeypatch, tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    monkeypatch.setattr(global_routes, "vault_root", lambda: vault)
    with TestClient(app) as test_client:
        yield test_client


def _post(client, name, value):
    return client.post("/api/credentials", json={"name": name, "value": value})


def test_credentials_post_create_writes_file(client, tmp_path):
    resp = _post(client, "alpha", "v1")

    assert resp.status_code == 200
    assert resp.json() == {"created": True}
    cred_file = tmp_path / "vault" / "credentials" / "global" / "alpha"
    assert cred_file.read_text(encoding="utf-8") == "v1"


def test_credentials_get_returns_sorted_bare_array(client):
    for name in ("gamma", "alpha", "beta"):
        _post(client, name, f"value-{name}")

    resp = client.get("/api/credentials")

    assert resp.status_code == 200
    assert resp.json() == ["alpha", "beta", "gamma"]


def test_credentials_post_update_overwrites_value(client, tmp_path):
    assert _post(client, "alpha", "v1").json() == {"created": True}

    resp = _post(client, "alpha", "v2")

    assert resp.status_code == 200
    assert resp.json() == {"updated": True}
    cred_file = tmp_path / "vault" / "credentials" / "global" / "alpha"
    assert cred_file.read_text(encoding="utf-8") == "v2"


def test_credentials_secret_never_in_response_bodies(client, tmp_path):
    """The secret is persisted on disk but never echoed by any endpoint."""
    _post(client, "alpha", _SECRET_VALUE)
    assert (
        tmp_path / "vault" / "credentials" / "global" / "alpha"
    ).read_text(encoding="utf-8") == _SECRET_VALUE

    responses = [
        client.get("/api/credentials"),
        _post(client, "beta", _SECRET_VALUE),
        client.delete("/api/credentials/alpha"),
    ]
    for resp in responses:
        assert resp.status_code == 200
        assert _SECRET_VALUE not in resp.text


def test_credentials_file_and_directory_permissions(client, tmp_path):
    _post(client, "alpha", "v1")

    cred_file = tmp_path / "vault" / "credentials" / "global" / "alpha"
    cred_dir = cred_file.parent
    assert stat.S_IMODE(os.stat(cred_file).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(cred_dir).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(cred_dir.parent).st_mode) == 0o700


def test_credentials_delete_idempotent_then_empty(client, tmp_path):
    _post(client, "alpha", "v1")

    first = client.delete("/api/credentials/alpha")
    assert first.status_code == 200
    assert first.json() == {"deleted": True}

    second = client.delete("/api/credentials/alpha")
    assert second.status_code == 200
    assert second.json() == {"deleted": True}

    assert client.get("/api/credentials").json() == []


@pytest.mark.parametrize(
    "bad_name",
    ["", ".", "..", "../x", "a/b", "a\\b", "a" * (global_routes._MAX_CREDENTIAL_NAME_LEN + 1)],
)
def test_credentials_invalid_names_rejected(client, bad_name):
    resp = _post(client, bad_name, "v")

    assert resp.status_code == 400
    body = resp.json()
    assert "error" in body
    assert isinstance(body["error"], str) and body["error"]
