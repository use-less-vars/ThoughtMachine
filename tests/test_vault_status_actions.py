"""Tests for GET /api/vault/status — the vault drift check report.

Hermetic: the endpoint is exercised through the real ``web_ui.backend.server``
app (fresh module import under a temp HOME + sys.modules prefix purge, same
pattern as ``test_resource_catalog_endpoint.py``).  ``thoughtmachine.vault.
vault_root`` is monkeypatched to a throwaway tmp vault *before* the request,
so the handler's lazy import picks up the patched root.  A private SSH key
and an API key are seeded into the vault to prove no secret value ever leaks
into the response, and every reported issue carries an actionable hint.
"""

import json
import sys

import pytest
from starlette.testclient import TestClient

_FORBIDDEN_FRAGMENTS = ("sk-super-secret", "BEGIN", "token=", "api_key")


@pytest.fixture(scope="module")
def server_module():
    """Fresh import of web_ui.backend.server (temp HOME + prefix purge)."""
    import os
    import tempfile

    with tempfile.TemporaryDirectory() as tmp_home:
        old_home = os.environ.get("HOME")
        os.environ["HOME"] = tmp_home
        try:
            for prefix in (
                "web_ui.backend",
                "agent.config.provider_profile",
                "thoughtmachine.bootstrap",
                "session",
            ):
                for mod in list(sys.modules):
                    if mod == prefix or mod.startswith(prefix + "."):
                        del sys.modules[mod]
            import web_ui.backend.server as server_mod

            yield server_mod
        finally:
            if old_home is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = old_home


@pytest.fixture(scope="module")
def client(server_module):
    with TestClient(server_module.app) as test_client:
        yield test_client


def _seed_vault(vault):
    """A vault whose only declared file is a valid agent_config.json.

    Everything else in the schema manifest is missing, which produces
    backfill_pending / missing-file warnings (never an abort, because the
    one existing file is valid JSON).  A private key sits in an undeclared
    subdirectory file (user/credentials.json) that the checker never reads.
    """
    vault.mkdir(exist_ok=True)
    (vault / "agent_config.json").write_text(
        json.dumps(
            {"provider_id": "p", "model": "m", "api_key": "sk-super-secret",
             "extra_setting": "x"}
        ),
        encoding="utf-8",
    )
    user_dir = vault / "user"
    user_dir.mkdir(exist_ok=True)
    (user_dir / "credentials.json").write_text(
        "-----BEGIN OPENSSH PRIVATE KEY-----\ntoken=abc123\nsk-super-secret\n",
        encoding="utf-8",
    )


def test_vault_status_warnings_with_actions_and_no_secrets(
    client, monkeypatch, tmp_path
):
    vault = tmp_path / "vault"
    _seed_vault(vault)
    monkeypatch.setattr("thoughtmachine.vault.vault_root", lambda: vault)

    resp = client.get("/api/vault/status")

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["status"] == "warnings"
    assert isinstance(body["issues"], list) and body["issues"]

    for issue in body["issues"]:
        assert "action" in issue, issue
        assert issue["action"] is None or isinstance(issue["action"], str), issue
    for issue in body["issues"]:
        if "missing" in str(issue.get("message", "")).lower():
            assert issue["action"], issue

    text = resp.text
    for fragment in _FORBIDDEN_FRAGMENTS:
        assert fragment not in text, fragment


def test_vault_status_aborts_on_invalid_json(client, monkeypatch, tmp_path):
    """Invalid JSON in a declared file aborts to the error shape, no secrets.

    A DriftAbortError surfaces the partial report issues (never empty) in
    addition to the error message.
    """
    vault = tmp_path / "vault"
    _seed_vault(vault)
    (vault / "agent_config.json").write_text("{not json{{", encoding="utf-8")
    monkeypatch.setattr("thoughtmachine.vault.vault_root", lambda: vault)

    resp = client.get("/api/vault/status")

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["status"] == "error"
    assert body["error"]
    # DriftAbortError surfaces the partial report issues (never empty).
    assert len(body["issues"]) >= 1
    for issue in body["issues"]:
        assert {"file", "severity", "message", "action"} <= set(issue.keys())
    for fragment in _FORBIDDEN_FRAGMENTS:
        assert fragment not in resp.text, fragment
