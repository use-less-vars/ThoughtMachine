"""RC9 regression: user/defaults.json must declare CONCRETE, type-valid defaults.

``agent/config/schema_manifest.json`` -> ``files["user/defaults.json"].fields``
historically declared ``model_override`` and ``stop_check`` with
``"default": null``.  When a vault was missing those two keys the drift
inspector produced a ``missing_field`` / ``machine_apply`` finding WITHOUT a
``default_value`` (the engine only records a default when it is not None).
The repair path then failed with
``no schema default for user/defaults.json::<field>`` — the item came back with
status ``error``, the file was left unchanged, and no exception was raised.

RC9 declares type-valid concrete defaults (``""`` for model_override, ``false``
for stop_check — the same values the file's ``safe_default`` block uses) so a
missing_field repair actually backfills.

Fully hermetic: never touches the real ``~/.thoughtmachine`` (mirrors the
pattern of tests/test_fresh_install_no_drift.py).
"""

import json
from pathlib import Path

from thoughtmachine import bootstrap
from thoughtmachine import vault
from thoughtmachine import vault_repair

_FIELDS = {"model_override", "stop_check"}


def _install_into(tmp_path, monkeypatch) -> None:
    """Point the whole bootstrap/vault install at *tmp_path*."""
    monkeypatch.delenv("THOUGHTMACHINE_VAULT_ROOT", raising=False)
    # Patch bootstrap's resolver (not a bare ``USER_DIR``): assigning USER_DIR
    # would write a concrete module-dict entry that importlib.reload() cannot
    # clear, leaking the tmp_path into later tests.  ``_user_dir`` is a real
    # function, so monkeypatch restores it cleanly.
    monkeypatch.setattr(bootstrap, "_user_dir", lambda: tmp_path)
    # ensure_user_defaults imports vault_root from the vault module *inside* the
    # function body, so patch the module attribute (not a bootstrap-local name).
    monkeypatch.setattr(vault, "vault_root", lambda: tmp_path)


def _read_defaults(tmp_path) -> dict:
    path = tmp_path / "user" / "defaults.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_declared_defaults_make_missing_field_repairable(tmp_path, monkeypatch):
    _install_into(tmp_path, monkeypatch)
    bootstrap.ensure_user_defaults()

    defaults_path = tmp_path / "user" / "defaults.json"
    doc = _read_defaults(tmp_path)
    assert _FIELDS <= set(doc), f"pristine install missing keys: {sorted(doc)}"

    # Provoke the drift: drop the two keys.
    for field in _FIELDS:
        del doc[field]
    defaults_path.write_text(json.dumps(doc), encoding="utf-8")

    # ── Inspection: exactly the two missing_field / machine_apply findings,
    #    each carrying a concrete default_value (repairable). ──
    report = vault_repair.run_inspection(tmp_path)
    findings = [
        i for i in report["issues"]
        if i["file"] == "user/defaults.json" and i["path_in_file"] in _FIELDS
    ]
    assert {i["path_in_file"] for i in findings} == _FIELDS, report["issues"]
    for i in findings:
        assert i["category"] == "missing_field", i
        assert i["classification"] == "machine_apply", i
        assert i["default_value"] is not None, i

    # ── Repair with apply enabled: BOTH items succeed, no top-level error. ──
    repaired = vault_repair.run_repair(tmp_path, apply=True)
    repair = repaired["repair"]
    assert "error" not in repair, repair
    performed = {p["path_in_file"]: p for p in repair["performed"] if p["file"] == "user/defaults.json"}
    for field in _FIELDS:
        assert field in performed, repair["performed"]
        assert performed[field]["status"] == "applied", performed[field]

    # ── Re-inspect: clean, and the on-disk values are exactly "" and false. ──
    post = vault_repair.run_inspection(tmp_path)
    assert post["summary"]["total_issues"] == 0, post["issues"]
    on_disk = _read_defaults(tmp_path)
    assert on_disk["model_override"] == ""
    assert on_disk["stop_check"] is False
