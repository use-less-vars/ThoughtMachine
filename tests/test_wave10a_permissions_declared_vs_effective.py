"""Wave 10a Commit 1 — workspace_info DECLARED vs EFFECTIVE permissions.

``CheckSystem.workspace_info`` returns BOTH the DECLARED ``permissions``
(verbatim from ``config.json``) and the GATE-RESOLVED
``effective_permissions`` (session x workspace), plus a fail-closed
``host_bash`` derived from the effective block.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from tools.workspace.check_system import CheckSystem

BASE_KWARGS = {"session_permissions": {"filesystem": "read", "network": "write"}}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _make_workspace(tmp_path, monkeypatch, ws_id, config):
    """Materialise a minimal vault workspace (config/workers/mcp)."""
    vault = tmp_path / "vault"
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(vault))
    ws_dir = vault / "workspaces" / ws_id
    ws_dir.mkdir(parents=True)
    (ws_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")
    (ws_dir / "workers.json").write_text("[]", encoding="utf-8")
    (ws_dir / "mcp_servers.json").write_text("[]", encoding="utf-8")
    return ws_dir


# ---------------------------------------------------------------------------
# Commit 1 — declared vs effective permissions in workspace_info
# ---------------------------------------------------------------------------

def test_workspace_info_surfaces_declared_and_effective(tmp_path, monkeypatch):
    declared = {"git_read": "read", "git_write": "ask", "host_bash": "banned"}
    _make_workspace(
        tmp_path,
        monkeypatch,
        "ws-1",
        {
            "purpose": "coding",
            "permissions": declared,
            "capabilities": {"allow_docker": True},
        },
    )

    with patch.object(
        CheckSystem, "_load_allowlist_from_vault", return_value=["workspace_info"]
    ):
        tool = CheckSystem(query="workspace_info", workspace_id="ws-1", **BASE_KWARGS)
        # Executor-injected, disk-authoritative effective dict (per-call gate).
        tool.effective_permissions = {
            "filesystem": "read",
            "network": "write",
            "host_bash": "write",
            "git": "read",
        }
        out = json.loads(tool.execute())

    # DECLARED: config.json verbatim, NOT gate-resolved.
    assert out["permissions"] == declared
    # GATE-RESOLVED: reflects the injected effective dict (canonical keys only).
    assert out["effective_permissions"] == {
        "filesystem": "read",
        "network": "write",
        "host_bash": "write",
        "git": "read",
    }
    # host_bash follows the EFFECTIVE grain, not the declared one.
    assert out["host_bash"] == "write"


def test_workspace_info_permissions_stay_declared_not_effective(tmp_path, monkeypatch):
    declared = {"host_bash": "write"}
    _make_workspace(
        tmp_path, monkeypatch, "ws-2", {"purpose": "general", "permissions": declared}
    )

    tool = CheckSystem(query="unused", **BASE_KWARGS)
    # Effective grants are WEAKER than the on-disk request; the declared block
    # must still report the request verbatim.
    tool.effective_permissions = {"filesystem": "read"}
    out = tool._query_workspace_info("ws-2")

    assert out["permissions"] == declared
    assert out["effective_permissions"] == {"filesystem": "read"}
    # No effective host_bash grant -> fail closed to "banned".
    assert out["host_bash"] == "banned"
