"""Phase-1 tests for thoughtmachine.vault_repair (read-only vault inspector).

These tests build vaults on ``tmp_path`` and always validate them against the
REAL manifest (``agent/config/schema_manifest.json``) -- never a custom one,
so the suite doubles as an integration check of the manifest itself.
"""

import json
from copy import deepcopy
from pathlib import Path

import pytest

from thoughtmachine import vault_repair as vr
from thoughtmachine.vault_repair import main, run_inspection


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


def _dirty_vault(root):
    """Clean vault plus representative drift/permission violations."""
    _clean_vault(root)
    _write_vault_files(root, {
        "user/defaults.json": {**_USER_DEFAULTS, "frobnicate": True},
        "workspaces/ws1/sessions/s1/permissions.json": {"git": "write", "root_shell": "read"},
    })
    return root


# --- tests ----------------------------------------------------------------


def test_clean_vault_no_issues(tmp_path):
    _clean_vault(tmp_path)
    report = run_inspection(tmp_path)
    assert report["issues"] == []
    assert report["extra_files"] == []
    assert report["summary"]["total_issues"] == 0
    assert report["summary"]["by_classification"] == {"machine_apply": 0, "manual_review": 0}
    assert report["run"]["dry_run"] is True
    assert report["run"]["tool_version"] == vr.TOOL_VERSION
    assert set(report["run"]) >= {"now", "vault_root", "tool_version", "dry_run"}
    assert report["run"]["vault_root"] == str(tmp_path)
    assert not any(s["status"] == "drift" for s in report["seeded_files"])


def test_clean_vault_seeded_files_all_tracked(tmp_path):
    _clean_vault(tmp_path)
    report = run_inspection(tmp_path)
    by_rel = {s["file"]: s["status"] for s in report["seeded_files"]}
    assert sorted(by_rel) == [
        "default_system_prompt.txt",
        "engineer_system_prompt.txt",
        "providers.json",
        "system/checksystem_allowlist.json",
        "system/default_system_prompt.txt",
        "system/engineer_system_prompt.txt",
        "system/factory_defaults.json",
        "system/providers.json",
    ]
    assert by_rel["system/providers.json"] == "match"
    assert by_rel["system/checksystem_allowlist.json"] == "match"
    assert by_rel["system/factory_defaults.json"] == "match"
    # Legacy optional providers.json is deliberately absent from the clean vault.
    assert by_rel["providers.json"] == "missing"


def test_unknown_top_key_reported(tmp_path):
    _clean_vault(tmp_path)
    _write_vault_files(tmp_path, {"user/defaults.json": {**_USER_DEFAULTS, "frobnicate": 1}})
    report = run_inspection(tmp_path)
    assert len(report["issues"]) == 1
    issue = report["issues"][0]
    assert issue["category"] == "unknown_top_key"
    assert issue["classification"] == "machine_apply"
    assert issue["severity"] == "warning"
    assert issue["file"] == "user/defaults.json"
    assert issue["path_in_file"] == "frobnicate"


def test_unknown_session_permission_key(tmp_path):
    _clean_vault(tmp_path)
    _write_vault_files(tmp_path, {
        "workspaces/ws1/sessions/s1/permissions.json": {"git": "write", "root_shell": "read"},
    })
    report = run_inspection(tmp_path)
    matches = [i for i in report["issues"]
               if i["category"] == "unknown_nested_key" and i["file"] == "workspaces/ws1/sessions/s1/permissions.json"]
    assert len(matches) == 1
    assert matches[0]["path_in_file"] == "root.root_shell"
    assert matches[0]["classification"] == "machine_apply"
    assert matches[0]["severity"] == "warning"
    assert "root_shell" in matches[0]["message"]


def test_missing_field_with_default(tmp_path):
    _clean_vault(tmp_path)
    defaults = {k: v for k, v in _USER_DEFAULTS.items() if k != "base_url"}
    _write_vault_files(tmp_path, {"user/defaults.json": defaults})
    report = run_inspection(tmp_path)
    assert len(report["issues"]) == 1
    issue = report["issues"][0]
    assert issue["category"] == "missing_field"
    assert issue["classification"] == "machine_apply"
    assert issue["severity"] == "info"
    assert issue["file"] == "user/defaults.json"
    assert issue["path_in_file"] == "base_url"
    assert issue["default_value"] == ""


def test_missing_file_with_safe_default(tmp_path):
    _clean_vault(tmp_path)
    (tmp_path / "vault_version.json").unlink()
    report = run_inspection(tmp_path)
    assert len(report["issues"]) == 1
    issue = report["issues"][0]
    assert issue["category"] == "missing_file"
    assert issue["classification"] == "machine_apply"
    assert issue["severity"] == "info"
    assert issue["file"] == "vault_version.json"
    assert issue["default_value"] == {"vault_version": 1}


def test_type_mismatch_does_not_raise(tmp_path):
    _clean_vault(tmp_path)
    _write_vault_files(tmp_path, {"user/defaults.json": {**_USER_DEFAULTS, "temperature": "hot"}})
    report = run_inspection(tmp_path)  # must not raise
    errors = [i for i in report["issues"] if i["severity"] == "error"]
    assert any(i["category"] == "type_mismatch" and i["classification"] == "manual_review"
               for i in errors)
    assert report["issues"][0]["id"].startswith("VR-")
    # Issue ids are unique.
    ids = [i["id"] for i in report["issues"]]
    assert len(ids) == len(set(ids))


def test_legacy_permission_key(tmp_path):
    _clean_vault(tmp_path)
    _write_vault_files(tmp_path, {
        "workspaces/ws1/sessions/s1/permissions.json": {"git_write": "write_on_feature_branch"},
    })
    report = run_inspection(tmp_path)
    matches = [i for i in report["issues"]
               if i["category"] == "legacy_permission_key" and i["file"] == "workspaces/ws1/sessions/s1/permissions.json"]
    assert len(matches) == 1
    assert matches[0]["path_in_file"] == "root.git_write"
    assert matches[0]["classification"] == "machine_apply"
    assert matches[0]["severity"] == "warning"
    assert "git_write" in matches[0]["message"]


def test_seeded_drift_reported(tmp_path):
    _clean_vault(tmp_path)
    _write_vault_files(tmp_path, {
        "system/providers.json": {"profiles": [], "active_profile_id": "x"},
    })
    report = run_inspection(tmp_path)
    assert any(i["category"] == "seeded_drift" and i["classification"] == "manual_review"
               and i["severity"] == "warning" and i["file"] == "system/providers.json"
               for i in report["issues"])
    entry = next(s for s in report["seeded_files"] if s["file"] == "system/providers.json")
    assert entry["status"] == "drift"


def test_extra_root_file(tmp_path):
    _clean_vault(tmp_path)
    (tmp_path / "config_audit.jsonl").write_text("{}\n", encoding="utf-8")
    report = run_inspection(tmp_path)
    assert report["extra_files"] == ["config_audit.jsonl"]
    assert any(i["category"] == "extra_file" and i["classification"] == "manual_review"
               and i["severity"] == "warning" and i["file"] == "config_audit.jsonl"
               for i in report["issues"])


def test_main_missing_root_exits_3(tmp_path, capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["--vault-root", str(tmp_path / "does-not-exist")])
    assert excinfo.value.code == 3
    assert "VAULT ROOT ERROR" in capsys.readouterr().err


def test_inspection_does_not_mutate(tmp_path):
    _dirty_vault(tmp_path)
    before = _snapshot(tmp_path)
    report = run_inspection(tmp_path)
    assert report["issues"], "dirty vault must produce findings"
    assert report["run"]["dry_run"] is True
    assert _snapshot(tmp_path) == before


def test_idempotent_reports(tmp_path):
    _dirty_vault(tmp_path)
    first = run_inspection(tmp_path)
    second = run_inspection(tmp_path)
    first["run"].pop("now")
    second["run"].pop("now")
    assert first == second


def test_report_json_written(tmp_path):
    _dirty_vault(tmp_path)
    out = tmp_path / "out" / "report.json"
    code = main(["--vault-root", str(tmp_path), "--report-json", str(out)])
    assert code == 1
    assert out.is_file()
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["run"]["dry_run"] is True
    assert data["issues"]
    assert data["summary"]["total_issues"] == len(data["issues"])


def test_cli_exit_codes(tmp_path):
    clean = tmp_path / "clean"
    clean.mkdir()
    _clean_vault(clean)
    assert main(["--vault-root", str(clean)]) == 0
    dirty = tmp_path / "dirty"
    dirty.mkdir()
    _dirty_vault(dirty)
    assert main(["--vault-root", str(dirty)]) == 1


def test_seeded_files_provider_entry_match(tmp_path):
    _clean_vault(tmp_path)
    report = run_inspection(tmp_path)
    entry = next(s for s in report["seeded_files"] if s["file"] == "system/providers.json")
    assert entry["status"] == "match"
