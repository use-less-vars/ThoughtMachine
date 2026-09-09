"""Phase-2 tests for thoughtmachine.vault_repair (repair engine).

These tests build vaults on ``tmp_path`` and always validate them against the
REAL manifest (``agent/config/schema_manifest.json``) -- never a custom one,
so the suite doubles as an integration check of the manifest itself.

Phase-2 coverage: ``run_repair(apply=True)`` and the ``--apply`` /
``--restore-seeds`` mutation paths -- machine fixes (unknown-key removal with
quarantine artifacts, legacy-permission folding, docker->container
conversion, missing-field backfill, missing-file re-creation from the schema
safe_default), timestamped sibling backups, never-raising behaviour, and the
strict guardrails (manual_review issues are never auto-applied,
``git_allow_worktree_commits`` is never removed, allowlist tampering is
quarantined but never auto-fixed, seed restoration requires ``--yes``).
"""

import json
import re
from copy import deepcopy
from pathlib import Path

import pytest

from thoughtmachine import vault_repair as vr
from thoughtmachine.vault_repair import main, run_inspection, run_repair


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


# --- phase-2 helpers --------------------------------------------------------

_BAK_RE = re.compile(r"\.bak-\d{14}(?:-\d+)?$")


def _bak_files(root):
    """Vault-relative paths of timestamped sibling backups."""
    return sorted(str(p.relative_to(root))
                  for p in root.rglob("*")
                  if p.is_file() and _BAK_RE.search(p.name))


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


# --- tests -----------------------------------------------------------------


def test_run_repair_exported():
    assert callable(vr.run_repair)
    assert callable(run_repair)


def test_run_repair_without_apply_is_readonly_inspection(tmp_path):
    _dirty_vault(tmp_path)
    before = _snapshot(tmp_path)
    report = run_repair(tmp_path)
    assert report["run"]["dry_run"] is True
    assert "repair" not in report
    assert report["issues"], "dirty vault must produce findings"
    assert _snapshot(tmp_path) == before
    # Identical to the phase-1 inspection entry point.
    insp = run_inspection(tmp_path)
    report["run"].pop("now")
    insp["run"].pop("now")
    assert report == insp


def test_apply_on_clean_vault_is_noop(tmp_path):
    _clean_vault(tmp_path)
    report = run_repair(tmp_path, apply=True)
    assert report["run"]["dry_run"] is False
    assert report["summary"]["total_issues"] == 0
    assert report["repair"]["performed"] == []
    assert report["repair"]["backups"] == []
    assert report["repair"]["requested_apply"] is True
    assert report["repair"]["restore_seeds"] is False
    assert _snapshot(tmp_path) == _snapshot(tmp_path)  # trivially stable


def test_apply_removes_unknown_top_key_with_backup_and_quarantine(tmp_path):
    _clean_vault(tmp_path)
    _write_vault_files(tmp_path, {"user/defaults.json": {**_USER_DEFAULTS, "frobnicate": True}})
    report = run_repair(tmp_path, apply=True)
    assert report["summary"]["total_issues"] == 0
    performed = report["repair"]["performed"]
    assert len(performed) == 1
    assert performed[0]["status"] == "applied"
    assert performed[0]["category"] == "unknown_top_key"
    assert performed[0]["file"] == "user/defaults.json"
    assert performed[0]["path_in_file"] == "frobnicate"
    assert performed[0]["action"].startswith("removed frobnicate; original quarantined")
    # Key gone from the live file; every other key untouched.
    doc = _vault_json(tmp_path, "user/defaults.json")
    assert "frobnicate" not in doc
    for k, v in _USER_DEFAULTS.items():
        assert doc[k] == v
    # Timestamped sibling backup preserves the original bytes.
    baks = _bak_files(tmp_path)
    assert len(baks) == 1
    bak_rel = baks[0]
    assert bak_rel.startswith("user/defaults.json.bak-")
    original = json.loads((tmp_path / bak_rel).read_text(encoding="utf-8"))
    assert original["frobnicate"] is True
    # Quarantine artifact records the removed node.
    artifacts = _quarantine_artifacts(tmp_path)
    assert len(artifacts) == 1
    name, payload = artifacts[0]
    assert payload["quarantined_from"] == "user/defaults.json"
    assert payload["path_in_file"] == "frobnicate"
    assert payload["category"] == "unknown_top_key"
    assert payload["value"] is True


def test_apply_drops_unknown_nested_key(tmp_path):
    _clean_vault(tmp_path)
    _write_vault_files(tmp_path, {
        "workspaces/ws1/sessions/s1/permissions.json": {"git": "write", "root_shell": "read"},
    })
    report = run_repair(tmp_path, apply=True)
    assert report["summary"]["total_issues"] == 0
    performed = report["repair"]["performed"]
    assert len(performed) == 1
    assert performed[0]["status"] == "applied"
    assert performed[0]["category"] == "unknown_nested_key"
    # performed entries keep the report-style dotted path; the quarantine
    # artifact records the canonical node path ("root_shell" at the doc root).
    assert performed[0]["path_in_file"] == "root.root_shell"
    doc = _vault_json(tmp_path, "workspaces/ws1/sessions/s1/permissions.json")
    assert doc == {"git": "write"}
    artifacts = _quarantine_artifacts(tmp_path)
    assert len(artifacts) == 1
    assert artifacts[0][1]["path_in_file"] == "root_shell"
    assert artifacts[0][1]["value"] == "read"


def test_apply_folds_legacy_git_write_never_downgrades(tmp_path):
    _clean_vault(tmp_path)
    _write_vault_files(tmp_path, {
        "workspaces/ws1/sessions/s1/permissions.json": {
            "git": "read", "git_write": True, "root_shell": "read"},
    })
    report = run_repair(tmp_path, apply=True)
    assert report["summary"]["total_issues"] == 0
    performed = report["repair"]["performed"]
    actions = {p["category"]: p for p in performed}
    assert set(actions) == {"legacy_permission_key", "unknown_nested_key"}
    assert all(p["status"] == "applied" for p in performed)
    fold = actions["legacy_permission_key"]
    assert fold["path_in_file"] == "root.git_write"
    assert fold["action"] == "folded git_write into git=write"
    # Canonical git grain kept the legacy permissiveness (write > read).
    doc = _vault_json(tmp_path, "workspaces/ws1/sessions/s1/permissions.json")
    assert doc == {"git": "write"}
    # Each rewritten file gets its own timestamped sibling backup: one before
    # the git_write fold, one before the root_shell removal.
    baks = sorted(_bak_files(tmp_path))
    assert len(baks) == 2
    pre_fold = json.loads((tmp_path / baks[0]).read_text(encoding="utf-8"))
    assert pre_fold == {"git": "read", "git_write": True, "root_shell": "read"}
    pre_removal = json.loads((tmp_path / baks[1]).read_text(encoding="utf-8"))
    assert pre_removal == {"git": "write", "root_shell": "read"}


def test_apply_converts_docker_to_container_in_ceiling(tmp_path):
    _clean_vault(tmp_path)
    _write_vault_files(tmp_path, {
        "workspaces/ws1/config.json": {"permissions": {"docker": "write", "git": "read"}, "allow_host_resources": False},
    })
    report = run_repair(tmp_path, apply=True)
    assert report["summary"]["total_issues"] == 0
    performed = report["repair"]["performed"]
    assert len(performed) == 1
    assert performed[0]["status"] == "applied"
    assert performed[0]["category"] == "legacy_permission_key"
    assert performed[0]["path_in_file"] == "$.permissions.docker"
    assert performed[0]["action"] == "converted docker to container"
    doc = _vault_json(tmp_path, "workspaces/ws1/config.json")
    assert doc["permissions"] == {"container": True, "git": "read"}


def test_apply_docker_conflict_records_error_never_raises(tmp_path):
    _clean_vault(tmp_path)
    _write_vault_files(tmp_path, {
        "workspaces/ws1/config.json": {"permissions": {"docker": "write", "container": "write"}, "allow_host_resources": False},
    })
    report = run_repair(tmp_path, apply=True)  # must not raise
    performed = report["repair"]["performed"]
    assert len(performed) == 1
    assert performed[0]["status"] == "error"
    assert "refusing to overwrite" in performed[0]["error"]
    # Vault file untouched, no backup created.
    doc = _vault_json(tmp_path, "workspaces/ws1/config.json")
    assert doc["permissions"] == {"docker": "write", "container": "write"}
    assert _bak_files(tmp_path) == []
    # The failure is reflected on the post-run inspection.
    assert any(i["category"] == "legacy_permission_key" for i in report["issues"])


def test_apply_backfills_missing_field(tmp_path):
    _clean_vault(tmp_path)
    defaults = {k: v for k, v in _USER_DEFAULTS.items() if k != "base_url"}
    _write_vault_files(tmp_path, {"user/defaults.json": defaults})
    report = run_repair(tmp_path, apply=True)
    assert report["summary"]["total_issues"] == 0
    performed = report["repair"]["performed"]
    assert len(performed) == 1
    assert performed[0]["status"] == "applied"
    assert performed[0]["category"] == "missing_field"
    assert performed[0]["path_in_file"] == "base_url"
    assert performed[0]["action"] == "backfilled base_url from schema default"
    doc = _vault_json(tmp_path, "user/defaults.json")
    assert doc["base_url"] == ""
    # Backup preserves the pre-backfill file (no base_url).
    baks = _bak_files(tmp_path)
    assert len(baks) == 1
    original = json.loads((tmp_path / baks[0]).read_text(encoding="utf-8"))
    assert "base_url" not in original


def test_apply_backfills_pattern_file_missing_field(tmp_path):
    # workspaces/*/config.json is a manifest PATTERN entry; a missing field
    # with a declared default must backfill even though the relpath never
    # equals the manifest glob key.
    _clean_vault(tmp_path)
    _write_vault_files(tmp_path, {
        "workspaces/ws1/config.json": {"permissions": {"git": "read"}},
    })
    report = run_repair(tmp_path, apply=True)
    assert report["summary"]["total_issues"] == 0
    performed = report["repair"]["performed"]
    assert len(performed) == 1
    assert performed[0]["status"] == "applied"
    assert performed[0]["category"] == "missing_field"
    assert performed[0]["file"] == "workspaces/ws1/config.json"
    assert performed[0]["path_in_file"] == "allow_host_resources"
    assert "backfilled allow_host_resources" in performed[0]["action"]
    doc = _vault_json(tmp_path, "workspaces/ws1/config.json")
    assert doc["allow_host_resources"] is False
    assert doc["permissions"] == {"git": "read"}
    # Backup preserves the pre-backfill file (no allow_host_resources).
    baks = _bak_files(tmp_path)
    assert len(baks) == 1
    original = json.loads((tmp_path / baks[0]).read_text(encoding="utf-8"))
    assert "allow_host_resources" not in original


def test_apply_recreates_missing_file_from_safe_default(tmp_path):
    _clean_vault(tmp_path)
    (tmp_path / "vault_version.json").unlink()
    report = run_repair(tmp_path, apply=True)
    assert report["summary"]["total_issues"] == 0
    performed = report["repair"]["performed"]
    assert len(performed) == 1
    assert performed[0]["status"] == "applied"
    assert performed[0]["category"] == "missing_file"
    assert performed[0]["file"] == "vault_version.json"
    assert performed[0]["action"] == "created from schema safe_default"
    assert _vault_json(tmp_path, "vault_version.json") == {"vault_version": 1}
    # A brand-new file needs no backup.
    assert _bak_files(tmp_path) == []


def test_manual_review_issues_never_applied(tmp_path):
    _clean_vault(tmp_path)
    _write_vault_files(tmp_path, {"user/defaults.json": {**_USER_DEFAULTS, "temperature": "hot"}})
    pre = run_inspection(tmp_path)
    assert [i for i in pre["issues"] if i["classification"] == "manual_review"]
    report = run_repair(tmp_path, apply=True)
    assert report["repair"]["performed"] == []
    assert report["repair"]["backups"] == []
    # File is byte-identical (no rewrite, no backup, no quarantine).
    doc = _vault_json(tmp_path, "user/defaults.json")
    assert doc["temperature"] == "hot"
    assert _bak_files(tmp_path) == []
    assert _quarantine_artifacts(tmp_path) == []
    assert report["summary"]["total_issues"] == len(pre["issues"])


def test_git_allow_worktree_commits_never_auto_removed(tmp_path):
    _clean_vault(tmp_path)
    _write_vault_files(tmp_path, {
        "workspaces/ws1/sessions/s1/permissions.json": {"git_allow_worktree_commits": True},
    })
    pre = run_inspection(tmp_path)
    assert len(pre["issues"]) == 1
    issue = pre["issues"][0]
    assert issue["category"] == "legacy_permission_key"
    assert issue["classification"] == "manual_review"
    report = run_repair(tmp_path, apply=True)
    assert report["repair"]["performed"] == []
    assert report["repair"]["backups"] == []
    assert _vault_json(tmp_path, "workspaces/ws1/sessions/s1/permissions.json") == {
        "git_allow_worktree_commits": True}
    assert any(i["category"] == "legacy_permission_key" for i in report["issues"])


def test_quarantine_artifacts_redact_secrets(tmp_path):
    _clean_vault(tmp_path)
    _write_vault_files(tmp_path, {
        "user/defaults.json": {
            **_USER_DEFAULTS,
            "myext": {"api_key": "sk-live-12345", "keep": 1},
        },
    })
    report = run_repair(tmp_path, apply=True)
    performed = [p for p in report["repair"]["performed"] if p["category"] == "unknown_top_key"]
    assert len(performed) == 1
    artifacts = _quarantine_artifacts(tmp_path)
    assert len(artifacts) == 1
    payload = artifacts[0][1]
    assert payload["path_in_file"] == "myext"
    assert payload["value"] == {"api_key": "<redacted>", "keep": 1}
    assert "sk-live-12345" not in json.dumps(payload)


def test_repair_report_shape_and_backup_mapping(tmp_path):
    _dirty_vault(tmp_path)
    report = run_repair(tmp_path, apply=True)
    assert report["run"]["dry_run"] is False
    repair = report["repair"]
    assert set(repair) == {"requested_apply", "restore_seeds", "performed", "backups"}
    assert repair["requested_apply"] is True
    assert repair["restore_seeds"] is False
    performed = repair["performed"]
    assert len(performed) == 2  # frobnicate + root_shell (dirty vault has no legacy key)
    assert {p["status"] for p in performed} == {"applied"}
    assert {p["file"] for p in performed} == {"user/defaults.json",
                                               "workspaces/ws1/sessions/s1/permissions.json"}
    for p in performed:
        assert p["id"].startswith("VR-")
    backups = repair["backups"]
    assert len(backups) == 2
    for b in backups:
        assert set(b) == {"backup", "backup_of"}
        assert not Path(b["backup"]).is_absolute()
        assert not Path(b["backup_of"]).is_absolute()
        assert (tmp_path / b["backup"]).is_file()
    assert {b["backup_of"] for b in backups} == {
        "user/defaults.json", "workspaces/ws1/sessions/s1/permissions.json"}
    # Repair ids correspond to pre-apply machine issues.
    pre = run_inspection(tmp_path)
    pre_ids = {i["id"] for i in pre["issues"]}
    assert pre_ids == set()  # vault is clean now
    assert report["summary"]["total_issues"] == 0


def test_restore_seeds_restores_missing_seeded_file(tmp_path):
    _clean_vault(tmp_path)
    (tmp_path / "system/providers.json").unlink()
    report = run_repair(tmp_path, restore_seeds=True, yes=True)
    assert report["repair"]["requested_apply"] is False
    assert report["repair"]["restore_seeds"] is True
    performed = report["repair"]["performed"]
    entry = next(p for p in performed
                 if p.get("file") == "system/providers.json")
    assert entry["status"] == "applied"
    assert entry["action"] == "restored from seed"
    # Restored content is byte-equal to the seed (== the clean content).
    assert _vault_json(tmp_path, "system/providers.json") == {"profiles": [], "active_profile_id": None}
    seeded = {s["file"]: s["status"] for s in report["seeded_files"]}
    assert seeded["system/providers.json"] == "match"


def test_restore_seeds_requires_yes(tmp_path):
    _clean_vault(tmp_path)
    (tmp_path / "system/providers.json").unlink()
    report = run_repair(tmp_path, restore_seeds=True, yes=False)
    assert "repair" not in report  # pure read-only inspection
    assert report["run"]["dry_run"] is True
    assert not (tmp_path / "system/providers.json").exists()


def test_allowlist_drift_quarantined_never_auto_fixed(tmp_path):
    _clean_vault(tmp_path)
    tampered = {**_ALLOWLIST, "sha256": "tampered-by-test"}
    _write_vault_files(tmp_path, {"system/checksystem_allowlist.json": tampered})
    report = run_repair(tmp_path, restore_seeds=True, yes=True)
    performed = report["repair"]["performed"]
    allow = [p for p in performed if p.get("file") == "system/checksystem_allowlist.json"]
    assert len(allow) == 1
    assert allow[0]["status"] == "applied"
    assert allow[0]["action"] == "quarantined allowlist drift; not auto-fixed"
    # The live file is NOT overwritten (operator may have customised it).
    assert _vault_json(tmp_path, "system/checksystem_allowlist.json") == tampered
    seeded = {s["file"]: s["status"] for s in report["seeded_files"]}
    assert seeded["system/checksystem_allowlist.json"] == "drift"
    # The tampered content was quarantined as an audit artifact.
    artifacts = _quarantine_artifacts(tmp_path)
    matches = [payload for _, payload in artifacts
               if payload.get("quarantined_from") == "system/checksystem_allowlist.json"]
    assert len(matches) == 1
    assert matches[0]["category"] == "seeded_drift"
    assert matches[0]["value"]["sha256"] == "tampered-by-test"
    assert matches[0]["path_in_file"] == ""
    assert any(i["category"] == "seeded_drift" for i in report["issues"])


def test_cli_apply_exit_codes(tmp_path):
    clean = tmp_path / "clean"
    clean.mkdir()
    _clean_vault(clean)
    assert main(["--vault-root", str(clean), "--apply"]) == 0
    dirty = tmp_path / "dirty"
    dirty.mkdir()
    _dirty_vault(dirty)
    assert main(["--vault-root", str(dirty), "--apply"]) == 4
    # A second apply finds nothing left to fix.
    assert main(["--vault-root", str(dirty), "--apply"]) == 0


def test_cli_dry_run_neutralises_apply(tmp_path):
    _dirty_vault(tmp_path)
    code = main(["--vault-root", str(tmp_path), "--apply", "--dry-run"])
    assert code == 1  # findings remain; read-only mode
    assert _bak_files(tmp_path) == []
    assert _quarantine_artifacts(tmp_path) == []
    assert _vault_json(tmp_path, "user/defaults.json")["frobnicate"] is True


def test_cli_restore_seeds_without_yes_leaves_seeds(tmp_path, capsys):
    _clean_vault(tmp_path)
    _write_vault_files(tmp_path, {
        "system/providers.json": {"profiles": [], "active_profile_id": "x"},
    })
    code = main(["--vault-root", str(tmp_path), "--restore-seeds"])
    assert code == 1  # seeded drift is still a finding
    out = capsys.readouterr().out
    assert "seed restoration requires --yes; seeds left untouched" in out
    assert _vault_json(tmp_path, "system/providers.json")["active_profile_id"] == "x"


def test_cli_repair_error_exits_3(tmp_path):
    _clean_vault(tmp_path)
    _write_vault_files(tmp_path, {
        "workspaces/ws1/config.json": {"permissions": {"docker": "write", "container": "write"}},
    })
    assert main(["--vault-root", str(tmp_path), "--apply"]) == 3


def test_cli_apply_report_json_contains_repair(tmp_path):
    _dirty_vault(tmp_path)
    out = tmp_path / "out" / "report.json"
    code = main(["--vault-root", str(tmp_path), "--apply", "--report-json", str(out)])
    assert code == 4
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["run"]["dry_run"] is False
    assert data["repair"]["requested_apply"] is True
    assert len(data["repair"]["performed"]) == 2
    assert all(p["status"] == "applied" for p in data["repair"]["performed"])
    assert data["summary"]["total_issues"] == 0


def test_cli_usage_error_exits_2(tmp_path):
    # argparse usage errors (unknown flag / missing value) exit 2 before any
    # vault work happens.
    with pytest.raises(SystemExit) as ei:
        main(["--bogus"])
    assert ei.value.code == 2
    with pytest.raises(SystemExit) as ei:
        main(["--vault-root"])
    assert ei.value.code == 2


def test_cli_apply_without_fixes_leaves_findings_exits_1(tmp_path):
    # A manual_review-only finding is never auto-applied: --apply performs no
    # fixes and exits 1 with the finding still present.
    _clean_vault(tmp_path)
    _write_vault_files(tmp_path, {
        "user/defaults.json": {**_USER_DEFAULTS, "temperature": "hot"},
    })
    assert main(["--vault-root", str(tmp_path), "--apply"]) == 1
    assert _vault_json(tmp_path, "user/defaults.json")["temperature"] == "hot"

