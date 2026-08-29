"""Tests for agent.config.vault_drift — VaultDriftChecker."""

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


def test_vault_drift_detects_missing_file(tmp_path):
    """A required file with no safe_default is reported, never auto-created."""
    checker = _make_checker(tmp_path, {
        "data.json": {"required": True, "root_type": "dict", "fields": {}},
    })
    report = checker.check()  # must not raise

    assert report["status"] == "warnings"
    assert report["aborted"] is False
    entry = report["files"]["data.json"]
    assert entry["status"] == "warning"
    assert entry["missing"] is True
    assert not (tmp_path / "vault" / "data.json").exists()


def test_vault_drift_detects_missing_field(tmp_path):
    """Missing field with a declared default is backfilled and .bak preserved."""
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "data.json").write_text(json.dumps({"a": 1}), encoding="utf-8")
    checker = _make_checker(tmp_path, {
        "data.json": {
            "required": True,
            "root_type": "dict",
            "fields": {"b": {"type": "string", "required": True, "default": "x"}},
        },
    })
    report = checker.check()

    assert report["files"]["data.json"]["status"] == "backfilled"
    data = json.loads((vault / "data.json").read_text(encoding="utf-8"))
    assert data == {"a": 1, "b": "x"}
    backups = list(vault.glob("*.bak"))
    assert len(backups) == 1
    original = json.loads(backups[0].read_text(encoding="utf-8"))
    assert original == {"a": 1}


def test_vault_drift_warns_unknown_file(tmp_path):
    """Undeclared files in the vault root produce a warning, not an abort."""
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "stray.json").write_text("{}", encoding="utf-8")
    checker = _make_checker(tmp_path, {})
    report = checker.check()

    assert any("stray.json" in w for w in report["warnings"])
    assert report["status"] == "warnings"
    assert report["aborted"] is False


def test_vault_drift_backfills_safe_default(tmp_path):
    """Missing file with backfill_on_missing + safe_default is re-created."""
    checker = _make_checker(tmp_path, {
        "data.json": {
            "required": True,
            "backfill_on_missing": True,
            "safe_default": {"vault_version": 1},
            "root_type": "dict",
            "fields": {},
        },
    })
    report = checker.check()

    entry = report["files"]["data.json"]
    assert entry["status"] == "backfilled"
    assert entry["missing"] is True
    data = json.loads((tmp_path / "vault" / "data.json").read_text(encoding="utf-8"))
    assert data == {"vault_version": 1}
    # A brand-new file is written directly; no backup of a nonexistent file.
    assert list((tmp_path / "vault").glob("*.bak")) == []


def test_vault_drift_type_mismatch_raises(tmp_path):
    """Type mismatch without coerce is CRITICAL: raises, aborts, reports error."""
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "data.json").write_text(json.dumps({"n": "not-an-int"}), encoding="utf-8")
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


def test_vault_drift_coerces_type_and_backs_up(tmp_path):
    """Coercible mismatch is fixed in place and the original is backed up."""
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "data.json").write_text(json.dumps({"n": "42"}), encoding="utf-8")
    checker = _make_checker(tmp_path, {
        "data.json": {
            "required": True,
            "root_type": "dict",
            "fields": {"n": {"type": "int", "required": True, "coerce": True}},
        },
    })
    report = checker.check()

    assert report["files"]["data.json"]["status"] == "backfilled"
    data = json.loads((vault / "data.json").read_text(encoding="utf-8"))
    assert data == {"n": 42}
    assert len(list(vault.glob("*.bak"))) == 1


def test_vault_drift_redacts_secrets(tmp_path, caplog):
    """Secret values never appear in the report or the log, even on abort."""
    caplog.set_level("DEBUG")
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "data.json").write_text(
        json.dumps({"token": "sk-SUPERSECRET"}), encoding="utf-8"
    )
    checker = _make_checker(tmp_path, {
        "data.json": {
            "required": True,
            "root_type": "dict",
            "fields": {"token": {"type": "int", "redact": True, "coerce": True}},
        },
    })
    with pytest.raises(DriftAbortError):
        checker.check()

    report = checker.report()
    assert "sk-SUPERSECRET" not in json.dumps(report)
    assert "sk-SUPERSECRET" not in caplog.text
