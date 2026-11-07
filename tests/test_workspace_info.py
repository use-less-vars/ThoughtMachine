"""
Tests for CheckSystem workspace_info output (purpose/permissions/risk).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from agent.config.risk_model import compute_workspace_risk
from tools.workspace.check_system import CheckSystem

BASE_KWARGS = {"session_permissions": {"filesystem": "read", "network": "write"}}


def test_workspace_info_includes_purpose_and_risk(tmp_path, monkeypatch):
    """workspace_info surfaces purpose, permissions and computed risk."""
    vault = tmp_path / "vault"
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(vault))
    ws_dir = vault / "workspaces" / "ws-1"
    ws_dir.mkdir(parents=True)
    (ws_dir / "config.json").write_text(
        json.dumps(
            {
                "purpose": "coding",
                "permissions": {"git_read": "read", "git_write": "ask", "host_bash": "banned"},
                "allow_host_resources": False,
                "capabilities": {"allow_docker": True},
                "domain_allowlist": ["example.com"],
            }
        ),
        encoding="utf-8",
    )
    (ws_dir / "workers.json").write_text("[]", encoding="utf-8")
    (ws_dir / "mcp_servers.json").write_text("[]", encoding="utf-8")

    with patch.object(CheckSystem, "_load_allowlist_from_vault", return_value=["workspace_info"]):
        tool = CheckSystem(query="workspace_info", workspace_id="ws-1", **BASE_KWARGS)
        out = json.loads(tool.execute())

    assert out["workspace_id"] == "ws-1"
    assert out["purpose"] == "coding"
    assert out["permissions"]["git_write"] == "ask"
    assert out["allow_host_resources"] is False
    assert out["capabilities"] == {"allow_docker": True}
    assert out["domain_allowlist"] == ["example.com"]
    assert out["risk"]["level"] in ("low", "medium", "high")
    assert out["risk"]["granted_count"] > 0


def test_vault_schema_manifest_covers_new_workspace_fields():
    """The vault schema manifest documents the new workspace config fields."""
    manifest_path = (
        Path(__file__).resolve().parent.parent / "agent/config/schema_manifest.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    fields = manifest["files"]["workspaces/*/config.json"]["fields"]
    assert "purpose" in fields
    assert "permissions" in fields
    assert fields["purpose"]["type"] == "string"
    assert fields["permissions"]["type"] == "dict"


def test_check_system_workspace_info_uses_runtime(tmp_path, monkeypatch):
    """workspace_info computes risk at read time instead of trusting a stored key."""
    vault = tmp_path / "vault"
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(vault))
    ws_dir = vault / "workspaces" / "ws-2"
    ws_dir.mkdir(parents=True)
    # No "risk" key stored on disk on purpose.
    (ws_dir / "config.json").write_text(
        json.dumps(
            {
                "purpose": "general",
                "permissions": {"host_bash": "write", "git_write": "write"},
                "allow_host_resources": True,
            }
        ),
        encoding="utf-8",
    )
    (ws_dir / "workers.json").write_text("[]", encoding="utf-8")
    (ws_dir / "mcp_servers.json").write_text("[]", encoding="utf-8")

    tool = CheckSystem(query="unused", **BASE_KWARGS)
    out = tool._query_workspace_info("ws-2")

    expected = compute_workspace_risk(
        permissions={"host_bash": "write", "git_write": "write"},
        allow_host_resources=True,
        purpose="general",
    )
    assert out["risk"] == expected
    assert out["risk"]["level"] == "high"
