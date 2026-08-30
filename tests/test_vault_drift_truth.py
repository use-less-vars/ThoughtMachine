"""Truth tests for agent.config.vault_drift drift detection.

Complements tests/test_vault_drift.py: pins the exact issue strings and
report shapes for the core drift classes (missing file, missing required
field, undeclared field, type mismatch, unknown file, seeded-file modified,
safe-default backfill) so the runtime-truth contract is locked down.
"""

import json

import pytest

from agent.config.vault_drift import VaultDriftChecker, DriftAbortError


def _manifest(**specs):
    """Build a minimal valid schema manifest dict from per-file specs."""
    return {
        "schema_version": 1,
        "vault_version": 1,
        "manifest_version": 1,
        "vault_version_file": "vault_version.json",
        "files": specs,
    }


def _write_manifest(tmp_path, specs):
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(_manifest(**specs)), encoding="utf-8")
    return manifest_path


def _make_checker(tmp_path, specs):
    manifest_path = _write_manifest(tmp_path, specs)
    return VaultDriftChecker(
        vault_root=tmp_path / "vault", manifest_path=manifest_path
    )


def _write_vault_file(tmp_path, relpath, content):
    path = tmp_path / "vault" / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        path.write_text(content, encoding="utf-8")
    else:
        path.write_text(json.dumps(content), encoding="utf-8")
    return path


def test_drift_detects_missing_file(tmp_path):
    """Required file without safe_default: warning, never auto-created."""
    checker = _make_checker(tmp_path, {
        "data.json": {"required": True, "root_type": "dict", "fields": {}},
    })
    report = checker.check()  # must not raise

    assert report["status"] == "warnings"
    assert report["aborted"] is False
    entry = report["files"]["data.json"]
    assert entry["status"] == "warning"
    assert entry["missing"] is True
    assert entry["drifts"][0]["issue"] == "file missing"
    assert not (tmp_path / "vault" / "data.json").exists()


def test_drift_detects_missing_required_field(tmp_path):
    """Required field without a default: warning drift, no pending repair."""
    _write_vault_file(tmp_path, "data.json", {"a": 1})
    checker = _make_checker(tmp_path, {
        "data.json": {
            "required": True,
            "root_type": "dict",
            "fields": {"b": {"type": "string", "required": True}},
        },
    })
    report = checker.check()

    assert report["status"] == "warnings"
    entry = report["files"]["data.json"]
    assert entry["status"] == "warning"
    assert any(d["issue"] == "missing required field" for d in entry["drifts"])
    assert report["pending_repairs"] == []


def test_drift_detects_extra_field(tmp_path):
    """Undeclared top-level field: warning drift, never auto-fixed."""
    _write_vault_file(tmp_path, "data.json", {"a": 1, "extra": 2})
    checker = _make_checker(tmp_path, {
        "data.json": {
            "required": True,
            "root_type": "dict",
            "fields": {"a": {"type": "int"}},
        },
    })
    report = checker.check()

    assert report["status"] == "warnings"
    entry = report["files"]["data.json"]
    assert entry["status"] == "warning"
    assert any(
        d["issue"] == "undeclared field" and d["field"] == "extra"
        for d in entry["drifts"]
    )


def test_drift_detects_type_mismatch(tmp_path):
    """Uncoercible type mismatch is CRITICAL: raises and aborts."""
    _write_vault_file(tmp_path, "data.json", {"n": "not-an-int"})
    checker = _make_checker(tmp_path, {
        "data.json": {
            "required": True,
            "root_type": "dict",
            "fields": {"n": {"type": "int", "required": True}},
        },
    })
    with pytest.raises(DriftAbortError):
        checker.check()

    report = checker.report()
    assert report["aborted"] is True
    assert report["status"] == "error"


def test_drift_detects_unknown_file(tmp_path):
    """Undeclared files in the vault root warn, never abort."""
    _write_vault_file(tmp_path, "stray.json", {})
    checker = _make_checker(tmp_path, {})
    report = checker.check()

    assert any("stray.json" in w for w in report["warnings"])
    assert report["status"] == "warnings"
    assert report["aborted"] is False


def test_drift_detects_seeded_file_altered(tmp_path):
    """A seeded file modified after bootstrap is flagged as warning drift."""
    _write_vault_file(tmp_path, "system/factory_defaults.json", {"version": "9"})
    checker = _make_checker(tmp_path, {
        "system/factory_defaults.json": {
            "required": True,
            "root_type": "dict",
            "fields": {},
        },
    })
    report = checker.check()

    assert report["status"] == "warnings"
    entry = report["files"]["system/factory_defaults.json"]
    assert entry["status"] == "warning"
    assert any(d["issue"] == "seeded file modified" for d in entry["drifts"])
    assert any("factory_defaults.json" in w for w in report["warnings"])


def test_drift_backfill_safe_defaults_only(tmp_path):
    """Repairs write ONLY the missing safe-default file; user files untouched."""
    _write_vault_file(tmp_path, "keep.json", {"name": "y"})
    checker = _make_checker(tmp_path, {
        "data.json": {
            "required": True,
            "backfill_on_missing": True,
            "safe_default": {"vault_version": 1},
            "root_type": "dict",
            "fields": {},
        },
        "keep.json": {
            "required": True,
            "root_type": "dict",
            "fields": {"name": {"type": "string", "required": True}},
        },
    })
    report = checker.check()  # read-only by default

    assert report["status"] == "warnings"
    entry = report["files"]["data.json"]
    assert entry["status"] == "backfill_pending"
    assert entry["missing"] is True
    assert report["pending_repairs"] == [
        {"file": "data.json", "action": "backfill_safe_default"}
    ]
    assert all(i["severity"] == "info" for i in report["issues"])
    assert not (tmp_path / "vault" / "data.json").exists()

    repaired = checker.check(apply_repairs=True)
    assert repaired["files"]["data.json"]["status"] == "backfilled"
    data = json.loads((tmp_path / "vault" / "data.json").read_text(encoding="utf-8"))
    assert data == {"vault_version": 1}
    # Existing user file was not touched by the repair pass.
    keep = json.loads((tmp_path / "vault" / "keep.json").read_text(encoding="utf-8"))
    assert keep == {"name": "y"}
    assert list((tmp_path / "vault").glob("*.bak")) == []
