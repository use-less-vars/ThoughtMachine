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
    report = checker.check(apply_repairs=True)

    assert report["files"]["data.json"]["status"] == "backfilled"
    data = json.loads((vault / "data.json").read_text(encoding="utf-8"))
    assert data == {"a": 1, "b": "x"}
    backups = list(vault.glob("*.bak"))
    assert len(backups) == 1
    original = json.loads(backups[0].read_text(encoding="utf-8"))
    assert original == {"a": 1}


def test_vault_drift_detects_missing_field_read_only(tmp_path):
    """Default check() is read-only: pending repair, no mutation, no .bak."""
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
    report = checker.check()  # read-only by default

    assert report["status"] == "warnings"
    entry = report["files"]["data.json"]
    assert entry["status"] == "warning"
    assert entry["drifts"][0]["issue"] == "missing field (repair pending)"
    assert report["pending_repairs"] == [
        {"file": "data.json", "field": "b", "action": "backfill_default"}
    ]
    data = json.loads((vault / "data.json").read_text(encoding="utf-8"))
    assert data == {"a": 1}  # unchanged
    assert list(vault.glob("*.bak")) == []


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
    report = checker.check(apply_repairs=True)

    entry = report["files"]["data.json"]
    assert entry["status"] == "backfilled"
    assert entry["missing"] is True
    data = json.loads((tmp_path / "vault" / "data.json").read_text(encoding="utf-8"))
    assert data == {"vault_version": 1}
    # A brand-new file is written directly; no backup of a nonexistent file.
    assert list((tmp_path / "vault").glob("*.bak")) == []


def test_vault_drift_missing_file_safe_default_read_only(tmp_path):
    """Read-only check never creates missing safe-default files; flags pending."""
    checker = _make_checker(tmp_path, {
        "data.json": {
            "required": True,
            "backfill_on_missing": True,
            "safe_default": {"vault_version": 1},
            "root_type": "dict",
            "fields": {},
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
    assert not (tmp_path / "vault" / "data.json").exists()


def test_vault_drift_fresh_empty_vault_read_only(tmp_path):
    """Fresh empty vault: read-only check reports pending backfills, writes nothing;
    apply_repairs=True then seeds the safe-default files."""
    checker = _make_checker(tmp_path, {
        "vault_version.json": {
            "required": True,
            "backfill_on_missing": True,
            "safe_default": {"vault_version": 1},
            "root_type": "dict",
            "fields": {},
        },
        "user/defaults.json": {
            "required": True,
            "backfill_on_missing": True,
            "safe_default": {"provider_id": "", "model": ""},
            "root_type": "dict",
            "fields": {},
        },
        "state/session_registry.json": {
            "required": True,
            "backfill_on_missing": True,
            "safe_default": [],
            "root_type": "list",
            "fields": {},
        },
    })
    report = checker.check()  # read-only: no vault files created

    assert report["status"] == "warnings"
    assert report["aborted"] is False
    assert len(report["pending_repairs"]) == 3
    assert not (tmp_path / "vault" / "vault_version.json").exists()
    assert not (tmp_path / "vault" / "user").exists()

    # Applying repairs seeds the fresh vault.
    seeded = checker.check(apply_repairs=True)
    assert seeded["aborted"] is False
    assert seeded["pending_repairs"] == []
    assert json.loads((tmp_path / "vault" / "vault_version.json").read_text(encoding="utf-8")) == {"vault_version": 1}
    assert json.loads((tmp_path / "vault" / "user" / "defaults.json").read_text(encoding="utf-8")) == {"provider_id": "", "model": ""}
    assert json.loads((tmp_path / "vault" / "state" / "session_registry.json").read_text(encoding="utf-8")) == []


def test_manifest_checksystem_allowlist_matches_resource(tmp_path):
    """Manifest safe_default for the allowlist matches resources exactly (gap 6)."""
    import hashlib
    from pathlib import Path

    repo_root = Path(__file__).resolve().parent.parent
    manifest = json.loads((repo_root / "agent/config/schema_manifest.json").read_text(encoding="utf-8"))
    resource = json.loads((repo_root / "resources/checksystem_allowlist.json").read_text(encoding="utf-8"))
    safe = manifest["files"]["system/checksystem_allowlist.json"]["safe_default"]

    assert safe == resource
    assert resource["allowlist"] == sorted(resource["allowlist"])
    assert "vault_status" in resource["allowlist"]
    assert resource["sha256"] == hashlib.sha256(
        "\n".join(sorted(str(e) for e in resource["allowlist"])).encode()
    ).hexdigest()


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
    report = checker.check(apply_repairs=True)

    assert report["files"]["data.json"]["status"] == "backfilled"
    data = json.loads((vault / "data.json").read_text(encoding="utf-8"))
    assert data == {"n": 42}
    assert len(list(vault.glob("*.bak"))) == 1


def test_vault_drift_coercion_pending_read_only(tmp_path):
    """Read-only check records coercible mismatches as pending, no write."""
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
    report = checker.check()  # read-only by default

    assert report["status"] == "warnings"
    entry = report["files"]["data.json"]
    assert entry["status"] == "warning"
    assert entry["drifts"][0]["issue"] == "type mismatch (repair pending)"
    assert report["pending_repairs"] == [
        {"file": "data.json", "field": "n", "action": "coerce"}
    ]
    data = json.loads((vault / "data.json").read_text(encoding="utf-8"))
    assert data == {"n": "42"}  # unchanged
    assert list(vault.glob("*.bak")) == []


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
