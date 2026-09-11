"""RC10 surface-error tests: per-item repair failures must surface at the
summary and HTTP-response level, not only inside ``performed``.

Before RC10 a Vault Health UI could believe a vault was healed because an
``apply`` run in which every fix failed (``performed[i]['status'] == 'error'``)
still produced no summary-level failure signal: ``report['repair']`` had no
``ok`` flag and the apply REST endpoint kept HTTP 200.  These two tests pin the
two surfaces the fix adds:

* ``test_repair_report_ok_false_when_item_fails`` -- UNIT: a vault whose only
  machine_apply fix cannot be applied yields ``report['repair']['ok'] is
  False`` with ``error_count >= 1`` and a ``status == 'error'`` performed
  entry, while the fatal-only ``error`` key stays absent.
* ``test_apply_endpoint_surfaces_partial_failure`` -- ENDPOINT: the same
  failure posted to ``/api/vault/repair/apply`` returns HTTP 200 with
  ``ok`` False, ``error_count >= 1`` and a non-empty ``errors`` list.

Vaults are built on ``tmp_path`` and validated against the REAL manifest,
mirroring ``tests/test_api_vault_repair.py`` helpers byte-for-byte.
"""

import json
from copy import deepcopy
from pathlib import Path

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from web_ui.backend import vault_repair_routes

import thoughtmachine.vault_repair as vr
from thoughtmachine.vault_repair import run_repair


def _write_vault_files(root, files):
    """Write ``{relpath: json-serializable}`` under *root*."""
    for rel, obj in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(obj), encoding="utf-8")


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


# A ceiling config carrying BOTH ``docker`` and ``container`` makes the
# legacy ``docker`` -> ``container`` conversion refuse to overwrite the
# existing key: the machine_apply fix is attempted and fails with a per-item
# ``status == "error"`` entry (the vault file is left untouched).
_CONFLICT_FILES = {
    "workspaces/ws1/config.json": {
        "permissions": {"docker": "write", "container": "write"},
        "allow_host_resources": False,
    },
}


# --- unit ------------------------------------------------------------------


def test_repair_report_ok_false_when_item_fails(tmp_path):
    _clean_vault(tmp_path)
    _write_vault_files(tmp_path, _CONFLICT_FILES)

    report = run_repair(tmp_path, apply=True)  # must not raise
    repair = report["repair"]

    # The per-item failure is now visible at the summary level.
    assert repair["ok"] is False
    assert repair["error_count"] >= 1
    assert any(p.get("status") == "error" for p in repair["performed"])
    # ``error`` stays reserved for the fatal outer-except path: a partial
    # (per-item) failure must NOT populate it.
    assert "error" not in repair


# --- endpoint --------------------------------------------------------------

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


def test_apply_endpoint_surfaces_partial_failure(client):
    test_client, vault = client
    _clean_vault(vault)
    _write_vault_files(vault, _CONFLICT_FILES)

    pre = vr.run_inspection(vault)
    conflicts = [i for i in pre["issues"]
                 if i["category"] == "legacy_permission_key"]
    assert conflicts, "conflicting docker/container must raise an issue"
    rid = conflicts[0]["id"]

    resp = test_client.post(
        "/api/vault/repair/apply",
        json={"repair_ids": [rid], "confirmed": True},
        headers=_HEADERS,
    )

    # A partial failure is still HTTP 200 (no status-code change), but the
    # body now carries an explicit ok=False plus the per-item errors.
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["error_count"] >= 1
    assert body["errors"], "per-item errors must surface in the response"
    assert body["report"]["repair"]["ok"] is False
