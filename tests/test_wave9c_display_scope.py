"""Wave 9c display-scope tests: CheckSystem host_bash / runtime_state / runtime_commit.

Covers two display-truth members of wave 9c:

- M1: ``workspace_info`` surfaces the GATE-RESOLVED ``host_bash``, not the
  config-declared (on-disk request) value.
- M2: ``runtime_state`` renames the ambiguous ``container_status`` key (which
  actually means the session sandbox) to ``session_sandbox_status``.

(M4 -- the ``container_status`` -> ``runtime_commit`` alias -- is deliberately
NOT asserted here; see the note at the foot of this file.)

Style mirrors tests/test_workspace_info.py / tests/test_check_system_runtime.py
(allowlist pinned via patching, no live vault/Docker daemon needed).
"""

from __future__ import annotations

import json
from unittest.mock import patch

from tools.workspace.check_system import CheckSystem

BASE_KWARGS = {"session_permissions": {"filesystem": "read", "network": "write"}}


def test_workspace_info_reports_gate_resolved_host_bash(tmp_path, monkeypatch):
    """M1: config-declared host_bash=allow must NOT be reported when the gate says banned."""
    vault = tmp_path / "vault"
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(vault))
    ws_dir = vault / "workspaces" / "ws-x"
    ws_dir.mkdir(parents=True)
    (ws_dir / "config.json").write_text(
        json.dumps(
            {
                "purpose": "general",
                "permissions": {"host_bash": "allow"},
            }
        ),
        encoding="utf-8",
    )
    (ws_dir / "workers.json").write_text("[]", encoding="utf-8")
    (ws_dir / "mcp_servers.json").write_text("[]", encoding="utf-8")

    with patch.object(
        CheckSystem, "_load_allowlist_from_vault", return_value=["workspace_info"]
    ):
        tool = CheckSystem(
            query="workspace_info",
            workspace_id="ws-x",
            # Gate-resolved effective grant: host_bash is BANNED even though
            # the on-disk config requests "allow".
            effective_permissions={"host_bash": "banned"},
            **BASE_KWARGS,
        )
        out = json.loads(tool.execute())

    # The declared request is preserved verbatim under "permissions"...
    assert out["permissions"]["host_bash"] == "allow"
    # ...but the surfaced top-level value is the GATE-RESOLVED one.
    assert out["host_bash"] == "banned"


def test_runtime_state_uses_session_sandbox_status_key():
    """M2: the ambiguous container_status key is renamed to session_sandbox_status."""
    with patch.object(
        CheckSystem, "_load_allowlist_from_vault", return_value=["runtime_state"]
    ):
        tool = CheckSystem(query="runtime_state", **BASE_KWARGS)
        result = json.loads(tool.execute())

    assert result["status"] == "ok"
    assert "session_sandbox_status" in result
    # The old, unqualified key must be gone (no sibling to keep it alive).
    assert "container_status" not in result


# M4 coverage note -- ``container_status`` -> ``runtime_commit`` alias.
#
# The aliasing test that used to live here was removed as tautological: it
# asserted ``result["runtime_commit"] == web_ui.backend.server._SERVER_REVISION``,
# which is the very constant the implementation reads (check_system._server_revision
# imports it), so the assertion could never fail even if the wiring broke.
#
# The natural independent replacement -- ``result["runtime_commit"] ==
# `git rev-parse HEAD` `` -- is not usable in this test environment: the pytest
# sandbox has no ``git`` on PATH, so ``_SERVER_REVISION`` (and therefore
# ``runtime_commit``) resolves to the ``"unknown"`` sentinel rather than a real
# revision, making an independent comparison impossible. M4 therefore has no
# meaningful automated assertion of its own; the alias is still exercised
# indirectly because every ``_query_container_status`` call populates it via
# ``setdefault``. Do not re-add a test that reads ``_SERVER_REVISION`` directly --
# it would only restate the implementation.
