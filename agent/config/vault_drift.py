"""vault_drift.py — VaultDriftChecker.

Detects drift between the user's ThoughtMachine vault
(``~/.thoughtmachine`` or ``THOUGHTMACHINE_VAULT_ROOT``) and the declared
schema manifest (``agent/config/schema_manifest.json``).

The checker is intentionally conservative:

* Checks are **read-only by default**: :meth:`check` reports repairable
  drift (missing file with ``safe_default``, missing field with ``default``,
  coercible type mismatch) as warnings and records it in
  ``report["pending_repairs"]``.  Pass ``apply_repairs=True`` to actually
  apply those repairs — the checker never writes without operator approval.
* Missing files with a declared ``safe_default`` (and ``backfill_on_missing``)
  are re-created from that default when repairs are applied.
* Missing **fields** with a declared ``default`` are backfilled; everything
  else is reported as a warning with an actionable hint.
* Type mismatches are CRITICAL: unless the manifest entry declares
  auto-coercion the checker raises :class:`DriftAbortError` and stops (in
  read-only mode a *coercible* mismatch is only reported as pending — an
  uncoercible one still aborts).
* Whenever an existing file is rewritten (field backfill / coercion), the
  original is first copied to a timestamped ``.bak`` sibling.
* Secret-bearing fields (``api_key``, ``secret``, ``token``, ...) are
  REDACTED in every report and log line — raw values are never echoed.

The module is deliberately dependency-free (stdlib only) so it can be
imported from tools without pulling the agent stack.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Field-name substrings that mark a value as secret (redacted everywhere).
_SECRET_HINTS = ("api_key", "apikey", "secret", "token", "password", "credential")

# JSON primitive type name -> (accepted python types, canonical type name).
_TYPE_CHECKERS = {
    "string": (str,),
    "int": (int,),
    "number": (int, float),
    "bool": (bool,),
    "list": (list,),
    "dict": (dict,),
    "null": (type(None),),
    "any": (),
}

# Manifest relpath -> resources/ filename for files deployed from seeds during
# bootstrap. When a seeded file exists in the vault but differs from the seed,
# the checker reports a warning drift (never auto-fixes — the operator may have
# deliberately customised it).
_SEED_MAP = {
    "system/checksystem_allowlist.json": "checksystem_allowlist.json",
    "system/providers.json": "default_providers.json",
    "providers.json": "default_providers.json",
    "system/factory_defaults.json": "factory_defaults.json",
    "system/default_system_prompt.txt": "default_system_prompt.txt",
    "system/engineer_system_prompt.txt": "engineer_system_prompt.txt",
    "default_system_prompt.txt": "default_system_prompt.txt",
    "engineer_system_prompt.txt": "engineer_system_prompt.txt",
}


def _resources_dir() -> Path:
    """Absolute path of the repository resources directory (seed files)."""
    return Path(__file__).resolve().parent.parent.parent / "resources"


class DriftAbortError(Exception):
    """Raised when vault drift is severe enough that automated backfill must stop."""


def _is_secret_name(name: str) -> bool:
    lower = name.lower()
    return any(hint in lower for hint in _SECRET_HINTS)


def _redact(value: Any) -> str:
    """Replace a value with a redaction placeholder (value is never echoed)."""
    return "<redacted>"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


class VaultDriftChecker:
    """Compare a vault against the schema manifest and (safely) repair drift.

    Args:
        vault_root: Absolute path of the vault directory to inspect.
        manifest_path: Path to the schema manifest JSON. Defaults to
            ``agent/config/schema_manifest.json`` next to this module.
        logger: Optional ``logging.Logger``. Defaults to module logger.
    """

    def __init__(
        self,
        vault_root: Any,
        manifest_path: Optional[Any] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.vault_root = Path(vault_root)
        if manifest_path is None:
            self.manifest_path = Path(__file__).resolve().parent / "schema_manifest.json"
        else:
            self.manifest_path = Path(manifest_path)
        self.logger = logger if logger is not None else logging.getLogger(__name__)
        self._last_report: Optional[dict] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check(self, apply_repairs: bool = False) -> dict:
        """Run the drift check and return the structured report.

        Args:
            apply_repairs: When False (default) the check is strictly
                read-only — no files are created, rewritten or backed up.
                Repairable drift (missing file with ``safe_default``,
                missing field with ``default``, coercible type mismatch)
                is reported as warnings and recorded in
                ``report["pending_repairs"]`` so the operator can apply
                it with ``check(apply_repairs=True)``.  Non-repairable
                drift (type mismatch without auto-coercion, unparseable
                JSON, invalid manifest) still raises
                :class:`DriftAbortError` in both modes.

        Raises:
            DriftAbortError: on CRITICAL drift (type mismatch without
                auto-coercion, unparseable JSON, invalid manifest).
                ``report()`` still returns the partial report afterwards
                (``aborted=True``, ``status="error"``).
        """
        manifest = self._load_manifest()
        report = self._new_report(manifest)
        try:
            self._check_files(manifest, report, apply_repairs)
        except DriftAbortError:
            report["status"] = "error"
            report["aborted"] = True
            report["issues"] = [
                {
                    "file": None,
                    "severity": "error",
                    "message": "Drift check aborted due to critical drift",
                    "action": "inspect the vault and fix the critical drift",
                }
            ]
            self._last_report = report
            raise
        self._check_unknown_root_files(manifest, report)
        self._finalize(report)
        self._last_report = report
        return report

    def report(self) -> dict:
        """Return the last report, or run :meth:`check` if none exists yet.

        If the last :meth:`check` aborted, this returns the partial report
        with ``aborted=True`` / ``status="error"`` instead of raising.
        """
        if self._last_report is None:
            return self.check()
        return self._last_report

    # ------------------------------------------------------------------
    # Manifest loading
    # ------------------------------------------------------------------

    def _load_manifest(self) -> dict:
        try:
            data = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DriftAbortError(
                f"Cannot load schema manifest {self.manifest_path}: {exc}"
            ) from exc
        if not isinstance(data, dict) or not isinstance(data.get("files"), dict):
            raise DriftAbortError(
                f"Schema manifest {self.manifest_path} is invalid: "
                "expected top-level object with a 'files' dict"
            )
        return data

    def _new_report(self, manifest: dict) -> dict:
        return {
            "status": "ok",
            "vault_root": str(self.vault_root),
            "schema_version": manifest.get("schema_version", 1),
            "vault_version": manifest.get("vault_version", 1),
            "checked_at": _utcnow_iso(),
            "files": {},
            "warnings": [],
            "pending_repairs": [],
            "aborted": False,
        }

    # ------------------------------------------------------------------
    # Per-file checks
    # ------------------------------------------------------------------

    def _check_files(self, manifest: dict, report: dict, apply_repairs: bool = False) -> None:
        for relpath, spec in manifest["files"].items():
            if spec.get("pattern"):
                matches = sorted(self.vault_root.glob(relpath))
                if not matches:
                    if spec.get("required"):
                        msg = (
                            f"No files match pattern '{relpath}' (required by manifest); "
                            "no workspace entries exist to backfill"
                        )
                        report["files"][relpath] = {
                            "status": "warning",
                            "missing": False,
                            "drifts": [
                                {
                                    "field": "*",
                                    "issue": "no files match required pattern",
                                    "action": "create the workspace files or ignore if no workspaces exist",
                                    "hint": msg,
                                }
                            ],
                            "hint": msg,
                        }
                        self._log("WARNING", "pattern_no_match", relpath=relpath, detail=msg)
                    continue
                for match in matches:
                    key = str(match.relative_to(self.vault_root))
                    self._check_file(spec, key, match, report, apply_repairs)
            else:
                self._check_file(spec, relpath, self.vault_root / relpath, report,
                                 apply_repairs)

    def _check_file(self, spec: dict, relpath: str, path: Path, report: dict,
                    apply_repairs: bool = False) -> None:
        if not path.exists():
            if spec.get("backfill_on_missing") and "safe_default" in spec:
                if not apply_repairs:
                    hint = (
                        f"Missing vault file '{relpath}' has a schema safe_default; "
                        "run check(apply_repairs=True) to re-create it."
                    )
                    report["files"][relpath] = {
                        "status": "backfill_pending",
                        "missing": True,
                        "drifts": [
                            {"field": "*", "issue": "file missing",
                             "action": "run check(apply_repairs=True)", "hint": hint}
                        ],
                        "hint": hint,
                    }
                    report["warnings"].append(hint)
                    report["pending_repairs"].append({
                        "file": relpath, "action": "backfill_safe_default",
                    })
                    self._log("INFO", "backfill_pending", relpath=relpath, detail=hint)
                    return
                try:
                    self._write_safe_default(spec, path)
                except OSError as exc:
                    raise DriftAbortError(
                        f"Failed to backfill missing file {relpath}: {exc}"
                    ) from exc
                hint = f"File was missing; re-created from schema safe_default"
                report["files"][relpath] = {
                    "status": "backfilled",
                    "missing": True,
                    "drifts": [
                        {"field": "*", "issue": "file missing",
                         "action": "backfilled from safe_default", "hint": hint}
                    ],
                    "hint": hint,
                }
                report["warnings"].append(f"Backfilled missing file {relpath} from schema default")
                self._log("INFO", "file_backfilled", relpath=relpath, detail=hint)
            elif spec.get("required", True):
                hint = (
                    f"Missing required vault file '{relpath}'. Re-run vault bootstrap "
                    "(thoughtmachine.vault.ensure_vault_structure/ensure_vault_defaults) "
                    "or create the file manually."
                )
                report["files"][relpath] = {
                    "status": "warning",
                    "missing": True,
                    "drifts": [
                        {"field": "*", "issue": "file missing",
                         "action": "run vault bootstrap or create file", "hint": hint}
                    ],
                    "hint": hint,
                }
                report["warnings"].append(hint)
                self._log("WARNING", "file_missing", relpath=relpath, detail=hint)
            # Optional missing file: no action, omit from report.
            return

        # File exists.
        seed_drift = self._check_seeded_file(relpath, path, report)
        if spec.get("root_type") == "string":
            self._check_text_file(spec, relpath, path, report, seed_drift)
            return

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DriftAbortError(
                f"Vault file {relpath} is not valid JSON: {exc}"
            ) from exc

        self._check_root_type(spec, relpath, data, report)
        if not isinstance(data, dict):
            # Root type matched (e.g. list registries); no field checks apply.
            report["files"][relpath] = {"status": "ok", "missing": False,
                                        "drifts": [], "hint": ""}
            return

        drifts: List[dict] = []
        if seed_drift is not None:
            drifts.append(seed_drift)
        rewritten = False
        for field_name, fspec in spec.get("fields", {}).items():
            repaired = self._check_field(
                relpath, path, data, field_name, fspec, spec, drifts, report,
                apply_repairs,
            )
            if repaired:
                rewritten = True

        # Undeclared top-level fields (dict files with a declared schema):
        # warning drift, never auto-fixed.
        declared_fields = set(spec.get("fields", {}).keys())
        if declared_fields:
            for key in data:
                if key not in declared_fields:
                    hint = (
                        f"Field '{key}' in {relpath} is not declared in the "
                        "schema manifest; it will not be validated or repaired."
                    )
                    drifts.append({
                        "field": key,
                        "issue": "undeclared field",
                        "action": "remove the field or declare it in the schema manifest",
                        "hint": hint,
                    })
                    report["warnings"].append(hint)
                    self._log("WARNING", "undeclared_field", relpath=relpath,
                              field=key, detail=hint)

        if rewritten:
            self._backup_and_write_json(path, data)
            status = "backfilled"
            hint = "Missing fields backfilled (original preserved as .bak)"
        elif drifts:
            status = "warning"
            hint = "; ".join(d.get("hint", d["issue"]) for d in drifts[:3])
        else:
            status = "ok"
            hint = ""
        report["files"][relpath] = {
            "status": status,
            "missing": False,
            "drifts": drifts,
            "hint": hint,
        }
        if rewritten:
            report["warnings"].append(f"Backfilled missing fields in {relpath}")

    def _check_seeded_file(self, relpath: str, path: Path, report: dict) -> Optional[dict]:
        """Compare an existing file against its resources/ seed, if any.

        Seeded files are deployed from ``resources/`` during bootstrap (see
        ``_SEED_MAP``). A vault copy that differs from the seed is reported as
        a warning drift — never auto-fixed, because the operator may have
        deliberately customised it.

        Returns the drift dict to merge into the per-file ``drifts`` list, or
        None when the file is not seeded / matches its seed.
        """
        seed_name = _SEED_MAP.get(relpath)
        if seed_name is None:
            return None
        seed_path = _resources_dir() / seed_name
        if not seed_path.is_file():
            return None
        try:
            vault_bytes = path.read_bytes()
            seed_bytes = seed_path.read_bytes()
        except OSError:
            return None
        if seed_name.endswith(".json"):
            # JSON: compare parsed content (formatting/indent differences are
            # not drift).
            try:
                same = json.loads(vault_bytes) == json.loads(seed_bytes)
            except (json.JSONDecodeError, UnicodeDecodeError):
                same = False
        else:
            same = vault_bytes == seed_bytes
        if same:
            return None
        hint = (
            f"Seeded file '{relpath}' differs from resources/{seed_name}; "
            "it was modified after bootstrap."
        )
        report["warnings"].append(hint)
        self._log("WARNING", "seeded_file_modified", relpath=relpath,
                  detail=hint)
        return {
            "field": relpath,
            "issue": "seeded file modified",
            "action": f"restore from resources/{seed_name} or accept drift",
            "hint": hint,
        }

    def _check_text_file(self, spec: dict, relpath: str, path: Path, report: dict,
                         seed_drift: Optional[dict] = None) -> None:
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise DriftAbortError(f"Cannot read text file {relpath}: {exc}") from exc
        drifts: List[dict] = []
        if seed_drift is not None:
            drifts.append(seed_drift)
        if spec.get("required") and not content.strip():
            drifts.append({
                "field": "*",
                "issue": "file is empty",
                "action": "populate the file or run vault bootstrap",
                "hint": f"Text file '{relpath}' is empty but marked required",
            })
            report["warnings"].append(f"Vault text file {relpath} is empty")
            self._log("WARNING", "file_empty", relpath=relpath, detail=f"empty required text file")
        report["files"][relpath] = {
            "status": "warning" if drifts else "ok",
            "missing": False,
            "drifts": drifts,
            "hint": drifts[0]["hint"] if drifts else "",
        }

    def _check_root_type(self, spec: dict, relpath: str, data: Any, report: dict) -> None:
        declared = spec.get("root_type")
        if not declared or declared == "any":
            return
        if declared == "string":
            return  # text files handled separately
        if not self._type_matches(data, declared):
            raise DriftAbortError(
                f"Vault file {relpath}: expected JSON root type '{declared}', "
                f"got {type(data).__name__}"
            )

    def _check_field(
        self,
        relpath: str,
        path: Path,
        data: dict,
        field_name: str,
        fspec: dict,
        file_spec: dict,
        drifts: List[dict],
        report: dict,
        apply_repairs: bool = False,
    ) -> bool:
        """Check one manifest field; returns True when auto-repaired.

        Auto-repair (only when ``apply_repairs`` is True) covers backfilling
        a missing field from its declared default and coercing a value to
        the declared type when the manifest opts in. In read-only mode
        repairable drift is reported as warnings + ``pending_repairs``;
        everything else is a warning drift or a raised
        :class:`DriftAbortError`.
        """
        fspec = dict(fspec)
        redact = bool(fspec.get("redact")) or _is_secret_name(field_name)
        value_label = _redact(None) if redact else "<value>"

        if field_name not in data:
            if "default" in fspec:
                if not apply_repairs:
                    hint = (
                        f"Field '{field_name}' missing from {relpath} "
                        f"(declared default available); "
                        "run check(apply_repairs=True) to backfill."
                    )
                    drifts.append({
                        "field": field_name,
                        "issue": "missing field (repair pending)",
                        "action": "run check(apply_repairs=True)",
                        "hint": hint,
                    })
                    report["warnings"].append(hint)
                    report["pending_repairs"].append({
                        "file": relpath, "field": field_name,
                        "action": "backfill_default",
                    })
                    self._log("INFO", "backfill_pending", relpath=relpath,
                              field=field_name, detail=hint)
                    return False
                data[field_name] = fspec["default"]
                self._log(
                    "INFO", "field_backfilled", relpath=relpath, field=field_name,
                    detail=f"missing field backfilled with declared default",
                )
                return True
            if fspec.get("required"):
                hint = (
                    f"Field '{field_name}' missing from {relpath}; "
                    f"expected type {fspec.get('type', 'any')}. "
                    "Add the field or run vault bootstrap."
                )
                drifts.append({
                    "field": field_name,
                    "issue": "missing required field",
                    "action": "add the field or run vault bootstrap",
                    "hint": hint,
                })
                report["warnings"].append(hint)
                self._log("WARNING", "field_missing", relpath=relpath,
                          field=field_name, detail=hint)
            return False

        value = data[field_name]
        declared_type = fspec.get("type", "any")
        if not self._type_matches(value, declared_type):
            coerce = bool(fspec.get("coerce")) or bool(file_spec.get("coerce"))
            if coerce:
                coerced = self._coerce(value, declared_type)
                if coerced is None:
                    raise DriftAbortError(
                        f"Vault file {relpath}: field '{field_name}' value {value_label} "
                        f"cannot be coerced to '{declared_type}'"
                    )
                if not apply_repairs:
                    hint = (
                        f"Field '{field_name}' in {relpath} has type "
                        f"{type(value).__name__}, expected '{declared_type}' "
                        "(coercible); run check(apply_repairs=True) to fix."
                    )
                    drifts.append({
                        "field": field_name,
                        "issue": "type mismatch (repair pending)",
                        "action": "run check(apply_repairs=True)",
                        "hint": hint,
                    })
                    report["warnings"].append(hint)
                    report["pending_repairs"].append({
                        "file": relpath, "field": field_name, "action": "coerce",
                    })
                    self._log("INFO", "coerce_pending", relpath=relpath,
                              field=field_name, detail=hint)
                    return False
                data[field_name] = coerced
                self._log(
                    "INFO", "field_coerced", relpath=relpath, field=field_name,
                    detail=f"coerced to {declared_type}",
                )
                return True
            else:
                raise DriftAbortError(
                    f"Vault file {relpath}: field '{field_name}' has type "
                    f"{type(value).__name__}, expected '{declared_type}'. "
                    "Declare auto-coercion in the schema manifest or fix the value."
                )

        # Enum / range checks (warning-only, never auto-fixed).
        if "enum" in fspec and value not in fspec["enum"]:
            hint = (f"Field '{field_name}' in {relpath} has value {value_label} "
                    f"outside declared enum {fspec['enum']}")
            drifts.append({"field": field_name, "issue": "value outside enum",
                           "action": "fix the value", "hint": hint})
            report["warnings"].append(hint)
            self._log("WARNING", "enum_violation", relpath=relpath,
                      field=field_name, detail=hint)
        if "min" in fspec or "max" in fspec:
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                numeric = None
            if numeric is not None:
                if "min" in fspec and numeric < float(fspec["min"]):
                    hint = (f"Field '{field_name}' in {relpath} is below minimum "
                            f"{fspec['min']}")
                    drifts.append({"field": field_name, "issue": "below minimum",
                                   "action": "fix the value", "hint": hint})
                    report["warnings"].append(hint)
                    self._log("WARNING", "range_violation", relpath=relpath,
                              field=field_name, detail=hint)
                if "max" in fspec and numeric > float(fspec["max"]):
                    hint = (f"Field '{field_name}' in {relpath} exceeds maximum "
                            f"{fspec['max']}")
                    drifts.append({"field": field_name, "issue": "above maximum",
                                   "action": "fix the value", "hint": hint})
                    report["warnings"].append(hint)
                    self._log("WARNING", "range_violation", relpath=relpath,
                              field=field_name, detail=hint)
        return False

    # ------------------------------------------------------------------
    # Unknown-file scan (vault root only)
    # ------------------------------------------------------------------

    def _check_unknown_root_files(self, manifest: dict, report: dict) -> None:
        if not self.vault_root.is_dir():
            return
        expected: set = set()
        for relpath in manifest["files"]:
            if not manifest["files"][relpath].get("pattern") and "*" not in relpath:
                expected.add(relpath)
        for entry in self.vault_root.iterdir():
            if not entry.is_file():
                continue
            name = entry.name
            if name in expected:
                continue
            # Ignore dotfiles, backup siblings, and the version marker.
            if name.startswith(".") or name.endswith(".bak") or name.endswith(".tmp"):
                continue
            hint = (
                f"Unknown file '{name}' in vault root is not declared in the "
                "schema manifest; it will be ignored by the checker."
            )
            report["warnings"].append(hint)
            self._log("WARNING", "unknown_root_file", relpath=name, detail=hint)

    # ------------------------------------------------------------------
    # Type helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _type_matches(value: Any, declared: str) -> bool:
        if value is None:
            # JSON null = "unset": accepted for every declared type. Manifests
            # declare null as the default of optional fields (e.g. providers.json
            # active_profile_id), so a null value must never abort the check or
            # trigger a rewrite.
            return True
        if declared == "any":
            return True
        checkers = _TYPE_CHECKERS.get(declared)
        if not checkers:
            return True  # unknown declared type: treat as unconstrained
        if declared == "int":
            return isinstance(value, int) and not isinstance(value, bool)
        if declared == "number":
            return isinstance(value, (int, float)) and not isinstance(value, bool)
        if declared == "bool":
            return isinstance(value, bool)
        return isinstance(value, checkers)

    @staticmethod
    def _coerce(value: Any, declared: str) -> Any:
        """Best-effort coercion; returns None when impossible."""
        try:
            if declared == "int":
                if isinstance(value, bool):
                    return None
                return int(value)
            if declared == "number":
                if isinstance(value, bool):
                    return None
                return float(value)
            if declared == "string":
                if isinstance(value, (dict, list, bool)):
                    return None
                return str(value)
            if declared == "bool":
                if isinstance(value, bool):
                    return value
                if isinstance(value, str):
                    low = value.strip().lower()
                    if low in ("true", "1", "yes", "on"):
                        return True
                    if low in ("false", "0", "no", "off"):
                        return False
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    return bool(value)
                return None
            if declared == "list":
                if isinstance(value, list):
                    return value
                if isinstance(value, tuple):
                    return list(value)
                return None
            if declared == "dict":
                if isinstance(value, dict):
                    return value
                return None
        except (ValueError, TypeError):
            return None
        return None

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def _write_safe_default(self, spec: dict, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        default = spec["safe_default"]
        if spec.get("root_type") == "string":
            self._atomic_write_text(path, str(default))
        else:
            self._atomic_write_json(path, default)

    def _backup_and_write_json(self, path: Path, data: dict) -> None:
        """Copy the original to a timestamped .bak, then atomically rewrite."""
        backup_path = path.with_name(f"{path.name}.{_timestamp()}.bak")
        shutil.copy2(str(path), str(backup_path))
        self._log("INFO", "backup_created", relpath=str(path.relative_to(self.vault_root)),
                  detail=f"original backed up to {backup_path.name}")
        self._atomic_write_json(path, data)

    def _atomic_write_json(self, path: Path, data: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{_timestamp()}.tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        os.replace(str(tmp), str(path))
        try:
            path.chmod(0o600)
        except OSError:
            pass

    def _atomic_write_text(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{_timestamp()}.tmp")
        tmp.write_text(content, encoding="utf-8")
        os.replace(str(tmp), str(path))
        try:
            path.chmod(0o600)
        except OSError:
            pass

    # ------------------------------------------------------------------
    # Report finalization + logging
    # ------------------------------------------------------------------

    def _finalize(self, report: dict) -> None:
        file_statuses = [f.get("status") for f in report["files"].values()]
        if report["aborted"] or "error" in file_statuses:
            report["status"] = "error"
        elif (report["warnings"] or "backfilled" in file_statuses
              or "backfill_pending" in file_statuses):
            report["status"] = "warnings"
        else:
            report["status"] = "ok"
        report["issues"] = self._build_issues(report)

    def _build_issues(self, report: dict) -> List[dict]:
        """Flatten the report into a top-level ``issues`` list.

        Each issue is ``{file, severity, message, action}`` with severity one
        of ``error`` / ``warning`` / ``info``:

        * files in ``error`` state (or an aborted check) -> ``error``;
        * files with warning drifts (missing required file/field, type
          mismatch, undeclared field, seeded-file modified) -> ``warning``;
        * ``backfill_pending`` / ``backfilled`` files -> ``info``;
        * unknown root files -> ``info`` (they do not flip the status);
        * every other top-level warning -> ``warning``.
        """
        issues: List[dict] = []
        seen_messages: set = set()

        for relpath in sorted(report["files"]):
            finfo = report["files"][relpath]
            status = finfo.get("status")
            if status == "ok":
                continue
            if status in ("backfill_pending", "backfilled"):
                severity = "info"
            elif status == "warning":
                severity = "warning"
            else:
                severity = "error"
            drifts = finfo.get("drifts") or []
            for d in drifts:
                message = d.get("hint") or d.get("issue", status)
                action = d.get("action")
                seen_messages.add(message)
                issues.append({
                    "file": relpath,
                    "severity": severity,
                    "message": message,
                    "action": action,
                })

        for w in report["warnings"]:
            if w in seen_messages:
                continue
            if "Unknown file" in w:
                issues.append({
                    "file": None,
                    "severity": "info",
                    "message": w,
                    "action": "declare the file in the schema manifest or ignore",
                })
            else:
                issues.append({
                    "file": None,
                    "severity": "warning",
                    "message": w,
                    "action": None,
                })
        return issues

    def _log(self, level: str, event: str, **payload: Any) -> None:
        """Emit one JSON line per event. Secret values are never included."""
        line = {
            "ts": _utcnow_iso(),
            "level": level,
            "event": "vault_drift",
            "kind": event,
            **payload,
        }
        text = json.dumps(line, default=str, sort_keys=True)
        if level == "ERROR":
            self.logger.error(text)
        elif level == "WARNING":
            self.logger.warning(text)
        else:
            self.logger.info(text)
