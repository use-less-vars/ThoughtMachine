"""Phase 2 — cross-tenant scoping of the CheckSystem ``vault_status`` query.

A session must not be able to read another workspace's session titles / drift
entries through ``vault_status``.  The report is scoped to the caller's own
workspace plus vault-wide files by default; the vault-wide view is reserved for
the explicit ``vault_status: global`` grant (operator-only).
"""

import json
from unittest.mock import patch

import pytest

from tools.workspace.check_system import CheckSystem


def _report():
    """A synthetic vault_status report spanning two workspaces + shared files."""
    return {
        "status": "warnings",
        "vault_root": "/tmp/vault",
        "schema_version": 1,
        "vault_version": 1,
        "checked_at": "2026-01-01T00:00:00Z",
        "files": {
            "vault_version.json": {
                "status": "ok", "missing": False, "drifts": [], "hint": "",
            },
            "workspaces/ws-a/sessions/mine.json": {
                "status": "ok", "missing": False, "drifts": [], "hint": "",
            },
            "workspaces/ws-b/sessions/secret-title.json": {
                "status": "warning",
                "missing": False,
                "drifts": [
                    {"field": "x", "issue": "undeclared field", "action": "a",
                     "hint": "h"},
                ],
                "hint": "foreign drift",
            },
        },
        "warnings": [
            "Field 'x' in workspaces/ws-b/sessions/secret-title.json is not "
            "declared in the schema manifest; it will not be validated.",
            "Backfilled missing fields in workspaces/ws-a/sessions/mine.json",
        ],
        "pending_repairs": [
            {"file": "workspaces/ws-b/sessions/secret-title.json",
             "action": "backfill_safe_default"},
            {"file": "workspaces/ws-a/sessions/mine.json",
             "action": "backfill_safe_default"},
        ],
        "aborted": False,
    }


def _tool(**kwargs):
    return CheckSystem(query="vault_status", **kwargs)


def test_default_scope_hides_foreign_workspace():
    """Caller in ws-a sees own + shared files; ws-b titles never appear."""
    tool = _tool(session_permissions={"filesystem": "read"})
    scoped = tool._scope_vault_status_report(_report(), "ws-a")

    assert scoped["scope"] == "workspace"
    assert scoped["scoped_to"] == "ws-a"
    assert "vault_version.json" in scoped["files"]
    assert "workspaces/ws-a/sessions/mine.json" in scoped["files"]
    assert "workspaces/ws-b/sessions/secret-title.json" not in scoped["files"]
    # The foreign session title must not leak anywhere in the scoped report.
    assert "secret-title" not in json.dumps(scoped)
    # warnings + pending_repairs are scoped too.
    assert all("ws-b" not in w for w in scoped["warnings"])
    assert all(
        not p["file"].startswith("workspaces/ws-b/")
        for p in scoped["pending_repairs"]
    )


def test_no_workspace_context_reports_only_vault_wide():
    """With no resolved workspace, no tenant files are exposed (fail-closed)."""
    scoped = _tool()._scope_vault_status_report(_report(), None)

    assert scoped["scope"] == "workspace"
    assert list(scoped["files"]) == ["vault_version.json"]
    assert "secret-title" not in json.dumps(scoped)


def test_global_grant_returns_the_full_report():
    """The explicit ``vault_status: global`` grant yields the vault-wide view."""
    tool = _tool(
        session_permissions={"filesystem": "read", "vault_status": "global"}
    )
    scoped = tool._scope_vault_status_report(_report(), "ws-a")

    assert scoped["scope"] == "global"
    assert "workspaces/ws-b/sessions/secret-title.json" in scoped["files"]


@pytest.mark.parametrize("bad", [True, "workspace", "GLOBAL", "all", "yes", 1])
def test_non_literal_vault_status_values_are_not_a_global_grant(bad):
    """Only the exact string ``global`` grants the vault-wide view."""
    tool = _tool(session_permissions={"vault_status": bad})
    assert tool._vault_status_global_allowed() is False


def test_execute_scopes_report_to_caller_workspace(monkeypatch):
    """End-to-end: ``vault_status`` returned through execute() is scoped."""
    report = _report()

    class _FakeChecker:
        def __init__(self, vault_root):
            self.vault_root = vault_root

        def check(self, apply_repairs=False):
            return dict(report)

    # Order matters: importing thoughtmachine.vault_repair binds the REAL
    # VaultDriftChecker at module load (vault_repair.py line ~56).  Patch its
    # run_inspection BEFORE the checker is faked, so the fake never leaks into
    # vault_repair's module namespace for later tests.
    monkeypatch.setattr(
        "thoughtmachine.vault_repair.run_inspection",
        lambda root: {"summary": {"by_classification": {"machine_apply": 0}}},
    )
    monkeypatch.setattr("agent.config.vault_drift.VaultDriftChecker", _FakeChecker)
    monkeypatch.setattr("thoughtmachine.vault.vault_root", lambda: "/tmp/vault")

    with patch.object(
        CheckSystem, "_load_allowlist_from_vault", return_value=["vault_status"]
    ):
        tool = CheckSystem(
            query="vault_status",
            workspace_id="ws-a",
            session_permissions={"filesystem": "read"},
        )
        out = json.loads(tool.execute())

    assert out["scope"] == "workspace"
    assert out["scoped_to"] == "ws-a"
    assert "workspaces/ws-a/sessions/mine.json" in out["files"]
    assert "workspaces/ws-b/sessions/secret-title.json" not in out["files"]
    assert "secret-title" not in json.dumps(out)
