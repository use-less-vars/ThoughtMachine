"""Tests for the CheckSystem 'vault_status' query (vault drift vs manifest)."""

import json
from unittest.mock import patch

from tools.workspace.check_system import CheckSystem


BASE_KWARGS = {
    "session_permissions": {"filesystem": "read", "network": "write"},
}

ALLOWLIST_12 = [
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
    "workers",
    "workspace_info",
]

ALLOWLIST_SHA256 = "697c326308135db2b508b16261e849bb79dd75b1d529748a5f8d88e799afa2d8"


def _make_clean_vault(root):
    """Create a vault whose files are all type-correct per the REAL manifest.

    Every file written here must pass VaultDriftChecker without raising
    (no type mismatches, no missing required fields).
    """
    root.mkdir(parents=True, exist_ok=True)
    files = {
        "vault_version.json": {"vault_version": 1},
        "system/providers.json": {"profiles": [], "active_profile_id": None},
        "system/factory_defaults.json": {
            "version": "1",
            "description": "d",
            "config": {
                "max_turns": 50,
                "temperature": 0.7,
                "provider_id": "",
                "model": "",
                "system_prompt": "",
            },
        },
        "system/checksystem_allowlist.json": {
            "version": 1,
            "allowlist": ALLOWLIST_12,
            "sha256": ALLOWLIST_SHA256,
        },
        "state/session_registry.json": [],
        "state/workspace_registry.json": [],
        "user/defaults.json": {
            "provider_id": "",
            "model": "",
            "base_url": "",
            "temperature": 0.7,
            "max_turns": 50,
            "system_prompt": "",
        },
    }
    for relpath, data in files.items():
        path = root / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")


def test_check_system_vault_status_returns_report_without_secrets(tmp_path):
    """vault_status returns the drift report; secrets never leak into JSON."""
    tmp_vault = tmp_path / "vault"
    _make_clean_vault(tmp_vault)
    # agent_config.json is optional in the manifest: valid, secret-bearing entry.
    (tmp_vault / "agent_config.json").write_text(
        json.dumps({"api_key": "sk-SUPERSECRET", "model": "deepseek-v4-flash"}),
        encoding="utf-8",
    )
    # Undeclared root file -> warning drift (status "warnings").
    (tmp_vault / "stray.json").write_text("{}", encoding="utf-8")

    with patch.object(
        CheckSystem, "_load_allowlist_from_vault", return_value=["vault_status"]
    ), patch("thoughtmachine.vault.vault_root", return_value=tmp_vault):
        tool = CheckSystem(query="vault_status", **BASE_KWARGS)
        out = json.loads(tool.execute())

    assert "sk-SUPERSECRET" not in json.dumps(out)
    assert "files" in out
    assert out["status"] in ("ok", "warnings")
    assert out["vault_root"] == str(tmp_vault)


def test_check_system_vault_status_respects_permissions():
    """vault_status is gated by system:read and blocked by the allowlist."""
    assert CheckSystem.required_categories == ["system:read"]

    with patch.object(
        CheckSystem, "_load_allowlist_from_vault", return_value=["my_config"]
    ):
        tool = CheckSystem(query="vault_status", **BASE_KWARGS)
        out = json.loads(tool.execute())

    assert out["status"] == "denied"
