"""API tests for vault repair REST endpoints (web_ui/backend/vault_repair_routes.py).

Covers: the read-only ``GET /api/vault/repair/status`` endpoint, the
origin-guarded ``POST /api/vault/repair/apply`` endpoint, the engine-level
selection semantics (categories like ``missing_field`` /
``unknown_root_file``), the confirmation gate, and error mapping (400/403/500).

Vaults are built on ``tmp_path`` and validated against the REAL manifest
(``agent/config/schema_manifest.json``), mirroring
``tests/test_vault_repair_phase2.py`` helpers byte-for-byte.
"""

import json
from copy import deepcopy
from pathlib import Path

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from web_ui.backend import vault_repair_routes

import thoughtmachine.vault_repair as vr


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _write_vault_files(root, files):
    """Write ``{relpath: json-serializable}`` under *root*."""
    for rel, obj in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(obj), encoding="utf-8")


def _snapshot(root):
    """relpath -> bytes for every regular file under *root*."""
    snap = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            snap[str(path.relative_to(root))] = path.read_bytes()
    return snap


# --- clean vault contents (real-manifest-correct; seed-exact) -------------

_FACTORY_DEFAULTS = {
    "version": "1",
    "description": "System factory defaults \u2014 immutable base configuration for ThoughtMachine vault.",
    "config": {
        "max_turns": 50,
        "temperature": 0.7,
        "provider_id": "",
        "model": "",
        "system_prompt": "",
    },
}

_ALLOWLIST = {
    "version": 1,
    "allowlist": [
        "capabilities",
        "container_status",
        "dockerfile",
        "effective_permissions",
        "event_bus_status",
        "event_log",
        "mcp_servers",
        "my_config",
        "network_diagnostics",
        "running_workers",
        "runtime_state",
        "vault_status",
        "workers",
        "workspace_info",
    ],
    "sha256": "201f3e9839ceaca5e3241bd914d2343c0059c6efbe35f49594650686b6ec335f",
}

_USER_DEFAULTS = {
    "provider_id": "",
    "model": "",
    "base_url": "https://api.deepseek.com/v1/",
    "temperature": 1.0,
    "max_turns": 200,
    "system_prompt": "",
    "provider_type": "openai_compatible",
    "provider_config": {},
    "model_override": None,
    "stop_check": None,
}

_CLEAN_FILES = {
    "vault_version.json": {"vault_version": 1},
    "system/providers.json": {"profiles": [], "active_profile_id": None},
    "system/resource_catalog.json": [],
    "system/factory_defaults.json": deepcopy(_FACTORY_DEFAULTS),
    "system/checksystem_allowlist.json": deepcopy(_ALLOWLIST),
    "user/defaults.json": deepcopy(_USER_DEFAULTS),
    "state/session_registry.json": {},
    "state/workspace_registry.json": {},
}


def _clean_vault(root):
    _write_vault_files(root, _CLEAN_FILES)
    return root


def _defaults_without_base_url():
    """user/defaults.json as the clean doc minus base_url (missing_field)."""
    doc = deepcopy(_USER_DEFAULTS)
    del doc["base_url"]
    return doc


# --- fixtures --------------------------------------------------------------

_HEADERS = {"Origin": "http://localhost:5173"}


@pytest.fixture
def app():
    application = FastAPI()
    application.include_router(vault_repair_routes.router)
    return application


@pytest.fixture
def client(app, monkeypatch, tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    monkeypatch.setattr("thoughtmachine.vault.vault_root", lambda: vault)
    with TestClient(app) as test_client:
        yield test_client, vault


# --- tests -----------------------------------------------------------------


def test_status_endpoint_reports_clean_vault(client):
    test_client, vault = client
    _clean_vault(vault)

    resp = test_client.get("/api/vault/repair/status", headers=_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {
        "run", "summary", "issues", "extra_files", "seeded_files",
    }
    assert body["issues"] == []
    assert body["summary"]["total_issues"] == 0
    expected = vr.run_inspection(vault)
    expected["run"]["now"] = body["run"]["now"]
    assert body == expected


def test_apply_missing_field_backfills_schema_default(client):
    test_client, vault = client
    _clean_vault(vault)
    _write_vault_files(vault,
                       {"user/defaults.json": _defaults_without_base_url()})

    resp = test_client.post(
        "/api/vault/repair/apply",
        json={"categories": ["missing_field"], "confirmed": True},
        headers=_HEADERS,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["backups_created"] == 1
    assert body["files_changed"] == ["user/defaults.json"]
    assert body["errors"] == []
    assert body["quarantine_moves"] == []
    repair = body["report"]["repair"]
    assert repair["requested_categories"] == ["missing_field"]
    performed = repair["performed"]
    assert len(performed) == 1
    assert performed[0]["category"] == "missing_field"
    assert performed[0]["status"] == "applied"
    assert performed[0]["action"] == "backfilled base_url from schema default"
    assert body["report"]["summary"]["total_issues"] == 0

    doc = json.loads(
        (vault / "user" / "defaults.json").read_text(encoding="utf-8"))
    assert doc == {**_USER_DEFAULTS, "base_url": ""}
    assert doc["base_url"] == ""


def test_apply_quarantines_unknown_root_file(client):
    test_client, vault = client
    _clean_vault(vault)
    _write_vault_files(vault, {"stray.json": {"note": "stray"}})

    resp = test_client.post(
        "/api/vault/repair/apply",
        json={"categories": ["unknown_root_file"], "confirmed": True},
        headers=_HEADERS,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["quarantine_moves"]) >= 1
    assert body["errors"] == []
    assert not (vault / "stray.json").exists()
    quarantined = [
        p for p in (vault / ".quarantine").rglob("*")
        if p.is_file() and (p.name == "stray.json"
                            or p.name.startswith("stray.json-"))
    ]
    assert quarantined
    assert body["report"]["issues"] == []
    assert body["report"]["extra_files"] == []


def test_apply_requires_confirmation(client):
    test_client, vault = client
    _clean_vault(vault)
    _write_vault_files(vault,
                       {"user/defaults.json": _defaults_without_base_url()})
    before = _snapshot(vault)

    resp = test_client.post(
        "/api/vault/repair/apply",
        json={"categories": ["missing_field"], "confirmed": False},
        headers=_HEADERS,
    )
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert isinstance(error, str) and error
    assert _snapshot(vault) == before


def test_apply_requires_exactly_one_selection(client):
    test_client, vault = client
    _clean_vault(vault)
    before = _snapshot(vault)

    resp = test_client.post(
        "/api/vault/repair/apply",
        json={"confirmed": True},
        headers=_HEADERS,
    )
    assert resp.status_code == 400
    assert "exactly one of repair_ids or categories" in resp.json()["error"]

    resp = test_client.post(
        "/api/vault/repair/apply",
        json={"repair_ids": ["VR-001"], "categories": ["missing_field"],
              "confirmed": True},
        headers=_HEADERS,
    )
    assert resp.status_code == 400
    assert "exactly one of repair_ids or categories" in resp.json()["error"]
    assert _snapshot(vault) == before


def test_apply_rejects_disallowed_origins(client):
    test_client, vault = client
    _clean_vault(vault)
    before = _snapshot(vault)
    payload = {"categories": ["missing_field"], "confirmed": True}

    resp = test_client.post("/api/vault/repair/apply", json=payload)
    assert resp.status_code == 403
    assert "error" in resp.json()

    resp = test_client.post(
        "/api/vault/repair/apply", json=payload,
        headers={"Origin": "http://evil.example"},
    )
    assert resp.status_code == 403
    assert "error" in resp.json()
    assert _snapshot(vault) == before


def test_apply_categories_rejected_when_selection_covers_security_critical(
        client):
    # Engine policy A3b: a categories filter is never an explicit selection,
    # so it must not be able to sweep security_critical findings in -- the
    # whole request is rejected up front (HTTP 400) and the vault stays
    # untouched.
    test_client, vault = client
    _clean_vault(vault)
    _write_vault_files(vault, {
        "workspaces/ws1/config.json": {"permissions": {"git": "read"}},
    })
    before = _snapshot(vault)

    resp = test_client.post(
        "/api/vault/repair/apply",
        json={"categories": ["missing_field"], "confirmed": True},
        headers=_HEADERS,
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == (
        "security_critical findings require explicit repair_ids selection")
    assert _snapshot(vault) == before

    # The finding is unchanged on a follow-up status check.
    status = test_client.get("/api/vault/repair/status", headers=_HEADERS)
    assert status.status_code == 200
    assert any(
        i["risk_category"] == "security_critical"
        and i["path_in_file"] == "allow_host_resources"
        for i in status.json()["issues"])


def test_apply_repair_ids_allows_security_critical_backfill(client):
    # Explicit repair_ids selection is the sanctioned route for
    # security_critical findings: the backfill goes through.
    test_client, vault = client
    _clean_vault(vault)
    _write_vault_files(vault, {
        "workspaces/ws1/config.json": {"permissions": {"git": "read"}},
    })
    pre = vr.run_inspection(vault)
    crit = [i for i in pre["issues"]
            if i["risk_category"] == "security_critical"
            and i["category"] == "missing_field"
            and i["path_in_file"] == "allow_host_resources"]
    assert len(crit) == 1
    rid = crit[0]["id"]

    resp = test_client.post(
        "/api/vault/repair/apply",
        json={"repair_ids": [rid], "confirmed": True},
        headers=_HEADERS,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["errors"] == []
    assert body["backups_created"] == 1
    assert "workspaces/ws1/config.json" in body["files_changed"]
    assert body["report"]["summary"]["total_issues"] == 0
    doc = json.loads((vault / "workspaces" / "ws1" / "config.json")
                     .read_text(encoding="utf-8"))
    assert doc["allow_host_resources"] is False
    assert doc["permissions"] == {"git": "read"}


def test_apply_maps_engine_errors(client, monkeypatch, tmp_path):
    test_client, vault = client
    payload = {"categories": ["missing_field"], "confirmed": True}

    # (a) vault root missing -> 400 from the handler's root check.
    missing = tmp_path / "missing"
    monkeypatch.setattr("thoughtmachine.vault.vault_root", lambda: missing)
    resp = test_client.post("/api/vault/repair/apply", json=payload,
                            headers=_HEADERS)
    assert resp.status_code == 400
    assert "does not exist" in resp.json()["error"]

    # (b) engine raising -> 500, no crash.
    monkeypatch.setattr("thoughtmachine.vault.vault_root", lambda: vault)
    _clean_vault(vault)

    def _boom(*args, **kwargs):
        raise RuntimeError("engine exploded")

    monkeypatch.setattr(vr, "run_repair", _boom)
    resp = test_client.post("/api/vault/repair/apply", json=payload,
                            headers=_HEADERS)
    assert resp.status_code == 500
    assert resp.json()["error"] == "engine exploded"
