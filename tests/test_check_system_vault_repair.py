"""Tests for the CheckSystem 'vault_repair_status' query.

``vault_repair_status`` is strictly read-only: it returns the full
vault_repair.run_inspection dry-run report and never mutates the vault.

There is deliberately NO agent-callable apply path. The old
``vault_repair_apply`` query was removed from CheckSystem (dispatch entry,
passthrough fields and handler are gone): machine fixes are applied only via
the CLI (``python3 -m thoughtmachine.vault_repair --apply``), never through a
CheckSystem query.
"""

import json
import re
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from tools.workspace import check_system as check_system_module
from tools.workspace.check_system import CheckSystem


BASE_KWARGS = {
    "session_permissions": {"filesystem": "read", "network": "write"},
}

# Allowlist patch value: only the read-only status query is allowed (mirrors
# how the vault_status tests patch _load_allowlist_from_vault).
ALLOWED_REPAIR_QUERIES = ["vault_repair_status"]


# --- clean vault contents (byte-verbatim from tests/test_vault_repair.py, so
#     the seeded files match the real repo resources and run_inspection sees a
#     genuinely clean vault: issues == []). -----------------------------------

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


def _clean_vault(root):
    _write_vault_files(root, _CLEAN_FILES)
    return root


def _dirty_vault(root):
    """Clean vault plus representative machine_apply drift/permission violations."""
    _clean_vault(root)
    _write_vault_files(root, {
        "user/defaults.json": {**_USER_DEFAULTS, "frobnicate": True},
        "workspaces/ws1/sessions/s1/permissions.json": {"git": "write", "root_shell": "read"},
    })
    return root


def _bak_files(root):
    """Vault-relative paths of timestamped sibling backups."""
    bak_re = re.compile(r"\.bak-\d{14}(?:-\d+)?$")
    return sorted(str(p.relative_to(root))
                  for p in root.rglob("*")
                  if p.is_file() and bak_re.search(p.name))


def _vault_json(root, rel):
    return json.loads((root / rel).read_text(encoding="utf-8"))


def _run_query(query, root, allowed=None, **kwargs):
    """Execute a CheckSystem query against *root* (allowlist + vault_root patched)."""
    allowed = ALLOWED_REPAIR_QUERIES if allowed is None else allowed
    with patch.object(
        CheckSystem, "_load_allowlist_from_vault", return_value=allowed
    ), patch("thoughtmachine.vault.vault_root", return_value=root):
        tool = CheckSystem(query=query, **BASE_KWARGS, **kwargs)
        return json.loads(tool.execute())


# --- vault_repair_status: read-only -----------------------------------------

def test_vault_repair_status_returns_full_dry_run_report_zero_findings(tmp_path):
    """vault_repair_status returns the run_inspection report on a clean vault."""
    tmp_vault = _clean_vault(tmp_path)
    out = _run_query("vault_repair_status", tmp_vault)

    # Expected top-level keys of the full repair dry-run report.
    assert {"run", "summary", "issues", "extra_files", "seeded_files"} <= set(out)
    assert out["summary"]["total_issues"] == 0
    assert out["issues"] == []
    assert out["extra_files"] == []
    assert out["run"]["vault_root"] == str(tmp_vault)
    assert out["run"]["dry_run"] is True


def test_vault_repair_status_never_mutates(tmp_path):
    """vault_repair_status on a dirty vault reports findings but changes nothing."""
    tmp_vault = _dirty_vault(tmp_path)
    before = _snapshot(tmp_vault)

    out = _run_query("vault_repair_status", tmp_vault)

    assert out["summary"]["total_issues"] >= 1
    assert {i["classification"] for i in out["issues"]} >= {"machine_apply"}
    # No writes: byte-identical tree, no quarantine dir, no sibling backups.
    assert _snapshot(tmp_vault) == before
    assert _bak_files(tmp_vault) == []
    assert not (tmp_vault / ".quarantine").exists()


# --- no agent-callable apply path -------------------------------------------

def test_vault_repair_apply_query_is_unknown(tmp_path):
    """vault_repair_apply no longer dispatches: even allowlisted it is Unknown."""
    tmp_vault = _dirty_vault(tmp_path)
    before = _snapshot(tmp_vault)

    # Allowlist *includes* vault_repair_apply so the request clears the gate
    # and reaches dispatch -- proving the handler is gone.
    out = _run_query(
        "vault_repair_apply", tmp_vault,
        allowed=["vault_repair_status", "vault_repair_apply"],
    )

    assert out["error"] == "Unknown query: vault_repair_apply"
    assert "vault_repair_apply" not in out["valid_queries"]
    assert "vault_repair_status" in out["valid_queries"]
    # Read-only outcome: nothing was touched.
    assert _snapshot(tmp_vault) == before
    assert _bak_files(tmp_vault) == []
    assert not (tmp_vault / ".quarantine").exists()


def test_check_system_source_has_no_vault_repair_apply():
    """Source-level guard: check_system.py never references the removed query."""
    src = Path(check_system_module.__file__).read_text(encoding="utf-8")
    assert "vault_repair_apply" not in src


def test_vault_repair_apply_passthrough_params_rejected():
    """Removed apply fields (approved/...) are extra='forbid'-rejected."""
    with pytest.raises(ValidationError):
        CheckSystem(query="vault_repair_status", approved=True, **BASE_KWARGS)