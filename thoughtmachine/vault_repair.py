"""vault_repair.py -- read-only vault integrity scanner (phase 1 of vault repair).

Phase-1 scope: DIAGNOSIS ONLY.  This module inspects a ThoughtMachine vault
(``~/.thoughtmachine`` or ``$THOUGHTMACHINE_VAULT_ROOT``) and produces a
structured, never-raising report of every integrity finding:

* schema drift vs ``agent/config/schema_manifest.json`` (via
  :class:`~agent.config.vault_drift.VaultDriftChecker`),
* permission-dict drift (legacy / unknown permission keys, mirrored from
  ``scripts/vault_permission_cleanup_dryrun.py``),
* seeded-file drift vs the ``resources/`` seed files,
* files present in the vault root but undeclared by the manifest.

``--apply`` repair, quarantine, seed restoration and GC hooks are LATER
phases (see ``working_docs/vault_repair_design.md``); this module never
writes, and ``--dry-run`` is accepted as a forward-compatibility no-op.

The module is stdlib-only apart from the repo-local drift checker import so
it can be invoked on the host as ``python3 -m thoughtmachine.vault_repair``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from agent.config.vault_drift import (
    DriftAbortError,
    VaultDriftChecker,
    _SEED_MAP,
    _resources_dir,
)

__version__ = "0.1.0"
TOOL_VERSION = "thoughtmachine.vault_repair 0.1.0"
DEFAULT_VAULT_ROOT = "~/.thoughtmachine"

# ---------------------------------------------------------------------------
# Permission vocabulary (mirrored verbatim from
# scripts/vault_permission_cleanup_dryrun.py -- the source of truth for the
# phase-later cleanup; this module only reports).
# ---------------------------------------------------------------------------
SESSION_VOCAB = ("git", "filesystem", "container", "network", "mcp", "host_bash")
SESSION_VOCAB_SET = frozenset(SESSION_VOCAB)
CEILING_EXTRA = ("system",)
CEILING_VOCAB = SESSION_VOCAB_SET | set(CEILING_EXTRA)
LEGACY_KEYS = ("git_read", "git_write", "execution", "git_allow_worktree_commits")
LEGACY_SET = frozenset(LEGACY_KEYS)
_PERM_INTEREST = LEGACY_SET | SESSION_VOCAB_SET | {"system", "docker"}


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def classify(relpath: str) -> Tuple[str, str]:
    """Classify a vault-relative JSON path into a ``(role, variant)`` pair.

    Mirrors ``scripts/vault_permission_cleanup_dryrun.py: classify()`` and
    additionally classifies any ``config.json`` file whose path contains an
    ``agent`` directory segment as an agent config.
    """
    parts = relpath.split("/")
    n = len(parts)
    if n == 0:
        return ("other", "other")
    base = parts[-1]
    if n == 3 and parts[0] == "workspaces" and base == "config.json":
        return ("ceiling", "config")
    if (n >= 4 and parts[0] == "workspaces" and parts[2] == "sessions"
            and base == "permissions.json" and n in (4, 5)):
        return ("session", "permissions_json")
    if n == 4 and parts[0] == "workspaces":
        return ("session", "session_metadata")
    if (n == 5 and parts[0] == "workspaces"
            and (base == "session.json" or base.startswith("session_metadata"))):
        return ("session", "session_metadata")
    if parts[0] == "sessions":
        # Legacy root-level session tree.
        if base == "permissions.json":
            return ("session", "permissions_json")
        return ("session", "session_metadata")
    if n == 2 and parts[0] == "user" and base == "defaults.json":
        return ("session", "defaults")
    if base == "agent_config.json":
        return ("agent", "agent_config")
    if base == "config.json" and "agent" in parts[:-1]:
        return ("agent", "agent_config")
    return ("other", "other")


def locate(role: str, variant: str, doc: Any) -> List[Tuple[str, dict, frozenset]]:
    """Locate permission dicts inside a parsed document.

    Mirrors ``scripts/vault_permission_cleanup_dryrun.py: locate()``; returns
    ``[(label, perm_dict, vocab)]`` (empty when the document has no
    permission dict of the classified shape).
    """
    if role == "ceiling" and variant == "config":
        if isinstance(doc, dict) and isinstance(doc.get("permissions"), dict):
            return [("$.permissions", doc["permissions"], CEILING_VOCAB)]
        return []
    if role == "session" and variant == "permissions_json":
        if isinstance(doc, dict):
            return [("root", doc, SESSION_VOCAB_SET)]
        return []
    if role == "session" and variant == "session_metadata":
        try:
            sp = doc["metadata"]["session_config"]["session_permissions"]
        except (KeyError, TypeError):
            sp = None
        if isinstance(sp, dict):
            return [
                ("$.metadata.session_config.session_permissions", sp, SESSION_VOCAB_SET)
            ]
        return []
    if role in ("session", "agent") and variant in ("defaults", "agent_config"):
        if isinstance(doc, dict) and isinstance(doc.get("session_permissions"), dict):
            return [("$.session_permissions", doc["session_permissions"], SESSION_VOCAB_SET)]
        return []
    return []


def resolve_vault_root(flag_root: Optional[str] = None) -> Path:
    """Resolve the vault root: ``--vault-root`` > env > ``~/.thoughtmachine``.

    Exits with code 3 (stderr) when the resolved root is not a directory.
    """
    if flag_root:
        root = Path(flag_root).expanduser().resolve()
    elif os.environ.get("THOUGHTMACHINE_VAULT_ROOT"):
        root = Path(os.environ["THOUGHTMACHINE_VAULT_ROOT"]).expanduser().resolve()
    else:
        root = Path(DEFAULT_VAULT_ROOT).expanduser().resolve()
    if not root.is_dir():
        print(
            f"VAULT ROOT ERROR: {root} does not exist or is not a directory",
            file=sys.stderr,
        )
        print(
            "vault_repair (phase 1, read-only) requires an existing vault root; "
            "run the ThoughtMachine bootstrap first or pass --vault-root.",
            file=sys.stderr,
        )
        sys.exit(3)
    return root


def _classify_abort(message: str) -> str:
    if "not valid JSON" in message:
        return "invalid_json"
    if "expected JSON root type" in message or "has type" in message:
        return "type_mismatch"
    return "schema_error"


class _IssueSink:
    """Collect issues with de-duplication by (file, path, category, message)."""

    def __init__(self) -> None:
        self.issues: List[dict] = []
        self._seen = set()

    def add(self, *, file: Optional[str], path_in_file: str, category: str,
            classification: str, severity: str, message: str, fix: str,
            default_value: Any = None) -> None:
        key = (file, path_in_file, category, message, severity)
        if key in self._seen:
            return
        self._seen.add(key)
        issue: Dict[str, Any] = {
            "file": file,
            "path_in_file": path_in_file,
            "category": category,
            "classification": classification,
            "severity": severity,
            "message": message,
            "fix": fix,
        }
        if default_value is not None:
            issue["default_value"] = default_value
        self.issues.append(issue)


def _drift_issue_map(issue: str, field: str, message: str, action: str,
                     file_spec: dict) -> dict:
    """Map one drift-checker issue vocabulary word to an inspection issue."""
    if issue == "file missing":
        return {"category": "missing_file", "classification": "manual_review",
                "severity": "warning", "fix": action or "run vault bootstrap or create file"}
    if issue == "no files match required pattern":
        return {"category": "missing_file", "classification": "manual_review",
                "severity": "info", "fix": action or "create the workspace files or ignore if no workspaces exist"}
    if issue == "missing field (repair pending)":
        default_value = None
        fspec = (file_spec.get("fields") or {}).get(field)
        if isinstance(fspec, dict) and "default" in fspec:
            default_value = fspec["default"]
        out = {"category": "missing_field", "classification": "machine_apply",
               "severity": "info", "fix": action or "backfill from schema default"}
        if default_value is not None:
            out["default_value"] = default_value
        return out
    if issue == "missing required field":
        return {"category": "missing_field", "classification": "manual_review",
                "severity": "warning", "fix": action or "add the field or run vault bootstrap"}
    if issue == "type mismatch (repair pending)":
        return {"category": "type_mismatch", "classification": "manual_review",
                "severity": "warning", "fix": action or "run check(apply_repairs=True)"}
    if issue == "undeclared field":
        return {"category": "unknown_top_key", "classification": "machine_apply",
                "severity": "warning",
                "fix": action or "remove the field or declare it in the schema manifest"}
    if issue == "seeded file modified":
        return {"category": "seeded_drift", "classification": "manual_review",
                "severity": "warning", "fix": action or "restore from seed or accept drift"}
    if issue == "file is empty":
        return {"category": "content", "classification": "manual_review",
                "severity": "warning", "fix": action or "populate the file or run vault bootstrap"}
    return {"category": "other", "classification": "manual_review",
            "severity": "warning", "fix": "inspect and resolve"}


def _collect_drift_issues(drift_report: dict, manifest: dict, sink: _IssueSink,
                          extra_files: List[str]) -> None:
    manifest_files = manifest.get("files") or {}
    for rel in sorted(drift_report.get("files", {})):
        finfo = drift_report["files"][rel]
        status = finfo.get("status")
        drifts = finfo.get("drifts") or []
        file_spec = manifest_files.get(rel) or {}
        if status == "backfill_pending":
            # A missing vault file restorable from the manifest safe_default.
            default_value = file_spec.get("safe_default")
            message = "Missing vault file '%s' has a schema safe_default; re-create it (phase-1 tool is read-only)." % rel
            d0 = drifts[0] if drifts else {}
            if d0.get("hint"):
                message = d0["hint"]
            sink.add(file=rel, path_in_file="", category="missing_file",
                     classification="machine_apply", severity="info",
                     message=message, fix="backfill from schema safe_default",
                     default_value=default_value)
            continue
        for d in drifts:
            issue = d.get("issue") or "other"
            field = d.get("field") or "*"
            message = d.get("hint") or d.get("issue") or status
            action = d.get("action") or ""
            mapped = _drift_issue_map(issue, field, message, action, file_spec)
            sink.add(file=rel,
                     path_in_file=field if field != "*" else "",
                     category=mapped["category"],
                     classification=mapped["classification"],
                     severity=mapped["severity"],
                     message=message,
                     fix=mapped["fix"],
                     default_value=mapped.get("default_value"))
    for w in drift_report.get("warnings", []):
        if not isinstance(w, str) or not w.startswith("Unknown file: "):
            continue
        name = w[len("Unknown file: "):]
        if name not in extra_files:
            extra_files.append(name)
        sink.add(file=name, path_in_file="", category="extra_file",
                 classification="manual_review", severity="warning",
                 message=w, fix="Review and remove if not needed")


def _collect_permission_issues(root: Path, sink: _IssueSink) -> None:
    """Walk all ``*.json`` vault files and report permission-dict drift."""
    if not root.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            if not name.endswith(".json"):
                continue
            full = Path(dirpath) / name
            rel = str(full.relative_to(root))
            try:
                doc = json.loads(full.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                if not any(i.get("file") == rel and i.get("category") == "invalid_json"
                           for i in sink.issues):
                    sink.add(file=rel, path_in_file="", category="invalid_json",
                             classification="manual_review", severity="warning",
                             message="Vault file %s is not valid JSON: %s" % (rel, exc),
                             fix="fix or remove file")
                continue
            role, variant = classify(rel)
            locations = locate(role, variant, doc)
            scoped_ids = set()
            for label, perm_dict, vocab in locations:
                scoped_ids.add(id(perm_dict))
                ceiling = vocab == CEILING_VOCAB
                for k in sorted(perm_dict):
                    path = "%s.%s" % (label, k)
                    if ceiling:
                        if k in LEGACY_SET:
                            sink.add(file=rel, path_in_file=path,
                                     category="legacy_permission_key",
                                     classification="machine_apply",
                                     severity="warning",
                                     message='Legacy permission key "%s" in ceiling permission dict (role=ceiling) — cleanup would remove it' % k,
                                     fix="remove legacy key")
                        elif k == "docker":
                            sink.add(file=rel, path_in_file=path,
                                     category="legacy_permission_key",
                                     classification="machine_apply",
                                     severity="warning",
                                     message='Legacy alias "docker" in ceiling permission dict — cleanup would convert to "container"',
                                     fix="convert to 'container'")
                        elif k == "system":
                            sink.add(file=rel, path_in_file=path,
                                     category="unknown_nested_key",
                                     classification="manual_review",
                                     severity="warning",
                                     message='Permission key "system" in ceiling dict — valid ceiling resource, KEPT for human review',
                                     fix="review (kept)")
                        elif k not in CEILING_VOCAB:
                            sink.add(file=rel, path_in_file=path,
                                     category="unknown_nested_key",
                                     classification="manual_review",
                                     severity="warning",
                                     message='Unknown permission key "%s" in ceiling permission dict — KEPT for human review' % k,
                                     fix="review (kept)")
                    else:
                        if k in LEGACY_SET:
                            sink.add(file=rel, path_in_file=path,
                                     category="legacy_permission_key",
                                     classification="machine_apply",
                                     severity="warning",
                                     message='Legacy permission key "%s" in session/agent permission dict — cleanup would remove it' % k,
                                     fix="remove legacy key")
                        elif k not in SESSION_VOCAB_SET:
                            sink.add(file=rel, path_in_file=path,
                                     category="unknown_nested_key",
                                     classification="machine_apply",
                                     severity="warning",
                                     message='Unknown permission key "%s" in session/agent permission dict — cleanup would drop it' % k,
                                     fix="drop on cleanup")
            _deep_permission_scan(rel, doc, sink, scoped_ids)


def _deep_permission_scan(rel: str, doc: Any, sink: _IssueSink,
                          skip_ids: set) -> None:
    """Report permission-shaped nested dicts that are not scoped locations."""
    visited = set()

    def walk(value: Any, path: str) -> None:
        if isinstance(value, dict):
            if id(value) in visited or id(value) in skip_ids:
                return
            visited.add(id(value))
            keys = set(value)
            if keys & _PERM_INTEREST:
                for k in sorted(keys):
                    if k in LEGACY_SET:
                        sink.add(file=rel, path_in_file="%s.%s" % (path, k),
                                 category="legacy_permission_key",
                                 classification="machine_apply",
                                 severity="warning",
                                 message='Legacy permission key "%s" in permission-shaped object at %s::%s — cleanup would remove it' % (k, rel, path),
                                 fix="remove legacy key")
                    elif k in ("system", "docker"):
                        sink.add(file=rel, path_in_file="%s.%s" % (path, k),
                                 category="unknown_nested_key",
                                 classification="manual_review",
                                 severity="warning",
                                 message='Permission key "%s" in permission-shaped object at %s::%s — KEPT for human review' % (k, rel, path),
                                 fix="review (kept)")
                    elif k not in SESSION_VOCAB_SET:
                        sink.add(file=rel, path_in_file="%s.%s" % (path, k),
                                 category="unknown_nested_key",
                                 classification="manual_review",
                                 severity="warning",
                                 message='Unknown permission key "%s" in permission-shaped object at %s::%s — review' % (k, rel, path),
                                 fix="review")
            for k, v in value.items():
                walk(v, "%s.%s" % (path, k))
        elif isinstance(value, list):
            for idx, item in enumerate(value):
                walk(item, "%s[%d]" % (path, idx))

    walk(doc, "root")


def _collect_seeded_files(root: Path) -> List[dict]:
    """Compare seeded vault files against their ``resources/`` seeds."""
    entries = []
    resources = _resources_dir()
    for rel in sorted(_SEED_MAP):
        seed_name = _SEED_MAP[rel]
        vault_path = root / rel
        if not vault_path.is_file():
            entries.append({"file": rel, "status": "missing"})
            continue
        seed_path = resources / seed_name
        if not seed_path.is_file():
            entries.append({"file": rel, "status": "unchecked"})
            continue
        try:
            vault_bytes = vault_path.read_bytes()
            seed_bytes = seed_path.read_bytes()
        except OSError:
            entries.append({"file": rel, "status": "unchecked"})
            continue
        if seed_name.endswith(".json"):
            try:
                same = json.loads(vault_bytes) == json.loads(seed_bytes)
            except (json.JSONDecodeError, UnicodeDecodeError):
                same = False
        else:
            same = vault_bytes == seed_bytes
        entries.append({"file": rel, "status": "match" if same else "drift"})
    return entries


def run_inspection(vault_root: Any, manifest_path: Optional[Any] = None) -> dict:
    """Run the read-only vault integrity inspection. Never raises.

    Returns a ``run_gc``-style report::

        {
          "run": {"now", "vault_root", "tool_version", "dry_run"},
          "summary": {"total_issues", "by_category", "by_classification"},
          "issues": [ {id, file, path_in_file, category, classification,
                       severity, message, fix, [default_value]}, ...],
          "extra_files": [...],
          "seeded_files": [{"file", "status"}, ...],
        }
    """
    root = Path(vault_root)
    sink = _IssueSink()
    checker = VaultDriftChecker(root, manifest_path)
    try:
        manifest = json.loads(checker.manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        manifest = {}

    drift_report = None
    try:
        drift_report = checker.check(apply_repairs=False)
    except DriftAbortError as exc:
        drift_report = checker.report()
        message = str(exc)
        sink.add(file=None, path_in_file="",
                 category=_classify_abort(message),
                 classification="manual_review", severity="error",
                 message=message, fix="inspect the vault and fix the critical drift")

    extra_files: List[str] = []
    _collect_drift_issues(drift_report or {}, manifest, sink, extra_files)
    _collect_permission_issues(root, sink)

    issues = sink.issues
    issues.sort(key=lambda i: (i.get("file") or "", i.get("path_in_file") or "",
                               i.get("category") or "", i.get("message") or ""))
    for idx, issue in enumerate(issues, start=1):
        issue["id"] = "VR-%03d" % idx

    by_category: Dict[str, int] = {}
    by_classification: Dict[str, int] = {"machine_apply": 0, "manual_review": 0}
    for issue in issues:
        cat = issue.get("category") or "other"
        by_category[cat] = by_category.get(cat, 0) + 1
        cls = issue.get("classification") or "manual_review"
        by_classification[cls] = by_classification.get(cls, 0) + 1

    extra_files.sort()
    seeded_files = _collect_seeded_files(root)
    meta = {
        "now": _utcnow_iso(),
        "vault_root": str(root),
        "tool_version": TOOL_VERSION,
        "dry_run": True,
    }
    report = {
        "run": meta,
        "summary": {
            "total_issues": len(issues),
            "by_category": dict(sorted(by_category.items())),
            "by_classification": dict(sorted(by_classification.items())),
        },
        "issues": issues,
        "extra_files": extra_files,
        "seeded_files": seeded_files,
    }
    return report


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry point. Exit codes: 0 healthy, 1 findings, 2 usage, 3 root error."""
    parser = argparse.ArgumentParser(
        prog="thoughtmachine.vault_repair",
        description=(
            "Phase-1 read-only vault integrity scanner. Reports schema drift, "
            "permission-dict drift, seeded-file drift and unknown root files. "
            "Repair (--apply), quarantine and seed restoration are later "
            "phases; this tool never writes."
        ),
    )
    parser.add_argument("--vault-root", metavar="PATH", default=None,
                        help="Vault root to inspect (default: $THOUGHTMACHINE_VAULT_ROOT or ~/.thoughtmachine)")
    parser.add_argument("--report-json", metavar="PATH", default=None,
                        help="Write the full JSON report to this path")
    parser.add_argument("--dry-run", action="store_true",
                        help="Accepted for forward compatibility; phase 1 is always read-only")
    args = parser.parse_args(argv)

    root = resolve_vault_root(args.vault_root)
    report = run_inspection(root)
    if args.report_json:
        out = Path(args.report_json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    summary = report["summary"]
    print("vault_repair %s: inspected %s" % (__version__, root))
    print("  issues: %d  (machine_apply: %d, manual_review: %d)"
          % (summary["total_issues"],
             summary["by_classification"].get("machine_apply", 0),
             summary["by_classification"].get("manual_review", 0)))
    if args.report_json:
        print("  report written to %s" % args.report_json)
    if summary["total_issues"]:
        print("  findings present (read-only mode; no changes were made)")
    else:
        print("  vault is healthy (no findings)")
    return 0 if summary["total_issues"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
