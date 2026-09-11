"""Regression test: a pristine install scans clean (no vault drift).

``thoughtmachine.vault.ensure_vault_defaults()`` historically did NOT create the
two manifest-declared files ``vault_version.json`` and ``user/defaults.json``,
so a pristine install reported two ``missing_file`` drift findings.  It now
seeds both from the schema manifest's own ``safe_default``
(``agent/config/schema_manifest.json`` -> ``files[<relpath>].safe_default``).
This test locks in "a pristine install scans clean" and is fully hermetic: it
never touches the real ``~/.thoughtmachine``.
"""

import json
from pathlib import Path

from thoughtmachine import bootstrap
from thoughtmachine import vault
from thoughtmachine import vault_repair


def _manifest() -> dict:
    path = (
        Path(__file__).resolve().parent.parent
        / "agent" / "config" / "schema_manifest.json"
    )
    return json.loads(path.read_text(encoding="utf-8"))


def _install_into(tmp_path, monkeypatch) -> None:
    """Point the whole bootstrap/vault install at *tmp_path*."""
    monkeypatch.delenv("THOUGHTMACHINE_VAULT_ROOT", raising=False)
    monkeypatch.setattr(bootstrap, "USER_DIR", tmp_path)
    # ``ensure_user_defaults`` imports ``vault_root`` from the vault module
    # *inside* the function body, so patching the module attribute is what
    # actually takes effect (not a bootstrap-local name).
    monkeypatch.setattr(vault, "vault_root", lambda: tmp_path)


def test_fresh_install_has_no_drift(tmp_path, monkeypatch):
    _install_into(tmp_path, monkeypatch)
    bootstrap.ensure_user_defaults()

    report = vault_repair.run_inspection(tmp_path)
    summary = report["summary"]
    assert summary["total_issues"] == 0
    assert summary["by_category"] == {}
    assert report["issues"] == []

    # The manifest-declared vault version marker must now exist.
    vv_path = tmp_path / "vault_version.json"
    assert vv_path.exists()
    assert json.loads(vv_path.read_text(encoding="utf-8")) == {"vault_version": 1}

    # The global defaults file must carry ALL declared fields (tracking the
    # manifest, not a hard-coded list).
    defaults_path = tmp_path / "user" / "defaults.json"
    assert defaults_path.exists()
    on_disk = json.loads(defaults_path.read_text(encoding="utf-8"))
    declared = _manifest()["files"]["user/defaults.json"]["fields"]
    assert set(on_disk) == set(declared)
    assert {
        "provider_type", "provider_config", "model_override", "stop_check",
    } <= set(on_disk)
    # Fields whose declared default is ``null`` are seeded with type-valid
    # placeholders (otherwise the scan would report a ``type_mismatch``).
    assert on_disk["model_override"] == ""
    assert on_disk["stop_check"] is False


def test_repeated_install_is_idempotent(tmp_path, monkeypatch):
    _install_into(tmp_path, monkeypatch)

    first = bootstrap.ensure_user_defaults()
    assert first  # a fresh install creates files

    second = bootstrap.ensure_user_defaults()
    assert second == []  # nothing re-created or clobbered on the 2nd run

    assert vault_repair.run_inspection(tmp_path)["summary"]["total_issues"] == 0
