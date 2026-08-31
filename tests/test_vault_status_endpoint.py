"""Tests for GET /api/vault/status — structured vault drift status endpoint.

Hermetic: the drift checker is faked (or pointed at a throwaway tmp vault)
and ``thoughtmachine.vault.vault_root`` is patched, so the endpoint never
touches the real vault. The endpoint imports VaultDriftChecker and
vault_root lazily inside the handler, so monkeypatching the module
attributes at call time is sufficient.
"""

import json
import sys

import pytest
from starlette.testclient import TestClient


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


def _patch_vault(monkeypatch, tmp_path, report):
    """Point the endpoint at a deterministic checker + throwaway vault."""
    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)

    class _FakeChecker:
        """Closure-captured VaultDriftChecker stand-in."""

        def __init__(self, vault_root):
            self.vault_root = vault_root

        def check(self, apply_repairs=False):
            return report

    monkeypatch.setattr("agent.config.vault_drift.VaultDriftChecker", _FakeChecker)
    monkeypatch.setattr("thoughtmachine.vault.vault_root", lambda: vault)
    return vault


def test_vault_status_endpoint_returns_issues(client, monkeypatch, tmp_path):
    """Warning/error drift flips ok=False and the issue shape is pinned."""
    report = {
        "status": "warnings",
        "checked_at": "2026-01-01T00:00:00Z",
        "vault_root": str(tmp_path / "vault"),
        "issues": [
            {
                "file": "data.json",
                "severity": "warning",
                "message": "File 'data.json' is missing",
                "action": "run vault bootstrap or create file",
            }
        ],
    }
    _patch_vault(monkeypatch, tmp_path, report)

    resp = client.get("/api/vault/status")

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["status"] == "warnings"
    assert body["checked_at"] == "2026-01-01T00:00:00Z"
    assert body["vault_root"] == str(tmp_path / "vault")
    assert body["summary"] == {"error": 0, "warning": 1, "info": 0}
    assert len(body["issues"]) == 1
    issue = body["issues"][0]
    assert issue["file"] == "data.json"
    assert issue["severity"] == "warning"
    assert issue["message"]
    assert issue["action"]

    # Error severity also flips ok=False.
    err_report = dict(
        report,
        status="error",
        issues=[
            {
                "file": None,
                "severity": "error",
                "message": "Drift check aborted due to critical drift",
                "action": "inspect the vault and fix the critical drift",
            }
        ],
    )
    _patch_vault(monkeypatch, tmp_path, err_report)
    resp2 = client.get("/api/vault/status")
    body2 = resp2.json()
    assert body2["ok"] is False
    assert body2["summary"] == {"error": 1, "warning": 0, "info": 0}


def test_vault_status_endpoint_only_info_is_ok(client, monkeypatch, tmp_path):
    """Unknown-file (info) drift does not flip ok."""
    report = {
        "status": "warnings",
        "checked_at": "2026-01-01T00:00:00Z",
        "vault_root": str(tmp_path / "vault"),
        "issues": [
            {
                "file": "agent_config.json",
                "severity": "info",
                "message": "Unknown file 'agent_config.json' in vault root",
                "action": "ignore",
            }
        ],
    }
    _patch_vault(monkeypatch, tmp_path, report)

    resp = client.get("/api/vault/status")

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["summary"] == {"error": 0, "warning": 0, "info": 1}


def test_vault_status_endpoint_no_secrets(client, monkeypatch, tmp_path):
    """Real checker on a tmp vault: raw config secrets never leak."""
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "agent_config.json").write_text(
        json.dumps({"provider": "x", "model": "y", "api_key": "sk-secret"}),
        encoding="utf-8",
    )
    monkeypatch.setattr("thoughtmachine.vault.vault_root", lambda: vault)

    resp = client.get("/api/vault/status")

    assert resp.status_code == 200
    body = resp.json()
    # Missing required files in the throwaway vault -> warnings -> ok False.
    assert body["ok"] is False
    assert body["status"] == "warnings"
    assert "sk-secret" not in resp.text
    assert '"api_key"' not in resp.text


def test_vault_status_endpoint_abort_drifts(client, monkeypatch, tmp_path):
    """A DriftAbortError surfaces the partial report issues to the client."""
    from agent.config.vault_drift import DriftAbortError

    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)

    class _AbortingChecker:
        """VaultDriftChecker stand-in that aborts inside check()."""

        def __init__(self, vault_root):
            self.vault_root = vault_root

        def check(self, apply_repairs=False):
            raise DriftAbortError("manifest is invalid JSON")

        def report(self):
            return {
                "status": "error",
                "aborted": True,
                "issues": [
                    {
                        "file": None,
                        "severity": "error",
                        "message": "Drift check aborted due to critical drift",
                        "action": "inspect the vault and fix the critical drift",
                    }
                ],
            }

    monkeypatch.setattr("agent.config.vault_drift.VaultDriftChecker", _AbortingChecker)
    monkeypatch.setattr("thoughtmachine.vault.vault_root", lambda: vault)

    resp = client.get("/api/vault/status")

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["status"] == "error"
    assert "manifest" in body["error"]
    assert len(body["issues"]) >= 1
    for issue in body["issues"]:
        assert {"file", "severity", "message", "action"} <= set(issue.keys())


def test_vault_status_endpoint_unexpected_error(client, monkeypatch, tmp_path):
    """An unexpected checker failure returns ok=False with an empty issue list."""
    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)

    class _ExplodingChecker:
        """VaultDriftChecker stand-in that raises a non-drift exception."""

        def __init__(self, vault_root):
            self.vault_root = vault_root

        def check(self, apply_repairs=False):
            raise ValueError("boom")

    monkeypatch.setattr("agent.config.vault_drift.VaultDriftChecker", _ExplodingChecker)
    monkeypatch.setattr("thoughtmachine.vault.vault_root", lambda: vault)

    resp = client.get("/api/vault/status")

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["status"] == "error"
    assert body["issues"] == []

