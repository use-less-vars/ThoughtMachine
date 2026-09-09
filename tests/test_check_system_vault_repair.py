"""Tests for the CheckSystem 'vault_repair_status' / 'vault_repair_apply' queries.

``vault_repair_status`` is strictly read-only: it returns the full
vault_repair.run_inspection dry-run report and never mutates the vault.
``vault_repair_apply`` is MUTATING: it refuses — never calling run_repair, never
writing a backup or quarantine dir — unless the invocation carries explicit
operator approval via ``approved=True``.
"""

import json
import re
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from tools.workspace.check_system import CheckSystem


BASE_KWARGS = {
    "session_permissions": {"filesystem": "read", "network": "write"},
}

# Allowlist patch value: both repair queries allowed (mirrors how the
# vault_status tests patch _load_allowlist_from_vault).
ALLOWED_REPAIR_QUERIES = ["vault_repair_status", "vault_repair_apply"]


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


def _quarantine_artifacts(root):
    """Parsed JSON artifacts under the default ``.quarantine`` dir."""
    qdir = root / ".quarantine"
    if not qdir.is_dir():
        return []
    out = []
    for p in sorted(qdir.iterdir()):
        if p.is_file() and p.suffix == ".json":
            out.append((p.name, json.loads(p.read_text(encoding="utf-8"))))
    return out


def _vault_json(root, rel):
    return json.loads((root / rel).read_text(encoding="utf-8"))


def _run_query(query, root, **kwargs):
    """Execute a CheckSystem query against *root* (allowlist + vault_root patched)."""
    with patch.object(
        CheckSystem, "_load_allowlist_from_vault", return_value=ALLOWED_REPAIR_QUERIES
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


# --- vault_repair_apply: mutating, approval-gated ----------------------------

def test_vault_repair_apply_without_approval_refuses(tmp_path):
    """Bare vault_repair_apply refuses and never touches the vault."""
    tmp_vault = _dirty_vault(tmp_path)
    before = _snapshot(tmp_vault)

    out = _run_query("vault_repair_apply", tmp_vault)  # approved defaults False

    assert out["status"] == "refused"
    assert out["approved"] is False
    assert out["applied"] is False
    assert out["performed"] == []
    assert out["backups"] == []
    # Refusal path must not have mutated anything.
    assert _snapshot(tmp_vault) == before
    assert _bak_files(tmp_vault) == []
    assert not (tmp_vault / ".quarantine").exists()
    # Findings are still there (nothing was fixed).
    assert "frobnicate" in _vault_json(tmp_vault, "user/defaults.json")


def test_vault_repair_apply_with_approval_repairs(tmp_path):
    """approved=True runs the mutating repair: performed entries + on-disk fix."""
    tmp_vault = _dirty_vault(tmp_path)
    pre = _run_query("vault_repair_status", tmp_vault)
    machine_ids = {i["id"] for i in pre["issues"] if i["classification"] == "machine_apply"}
    assert machine_ids

    out = _run_query("vault_repair_apply", tmp_vault, approved=True)

    repair = out["repair"]
    assert repair["requested_apply"] is True
    performed = repair["performed"]
    assert performed
    assert {p["status"] for p in performed} == {"applied"}
    assert {p["id"] for p in performed} == machine_ids
    assert {p["file"] for p in performed} == {
        "user/defaults.json",
        "workspaces/ws1/sessions/s1/permissions.json",
    }
    # On-disk fixes applied.
    doc = _vault_json(tmp_vault, "user/defaults.json")
    assert "frobnicate" not in doc
    perms = _vault_json(tmp_vault, "workspaces/ws1/sessions/s1/permissions.json")
    assert perms == {"git": "write"}
    # Guardrails left their trace: sibling backups + quarantine artifact.
    assert _bak_files(tmp_vault)
    assert _quarantine_artifacts(tmp_vault)
    assert out["summary"]["total_issues"] == 0


def test_vault_repair_apply_approved_on_clean_vault_is_noop(tmp_path):
    """Approved apply on a clean vault performs nothing and writes nothing."""
    tmp_vault = _clean_vault(tmp_path)
    before = _snapshot(tmp_vault)

    out = _run_query("vault_repair_apply", tmp_vault, approved=True)

    assert out["summary"]["total_issues"] == 0
    assert out["repair"]["requested_apply"] is True
    assert out["repair"]["restore_seeds"] is False
    assert out["repair"]["performed"] == []
    assert out["repair"]["backups"] == []
    assert _snapshot(tmp_vault) == before
    assert _bak_files(tmp_vault) == []
    # run_repair(apply=True) materializes its quarantine dir even with zero
    # performed fixes — it must stay empty (no artifacts, no mutations).
    assert _quarantine_artifacts(tmp_vault) == []
