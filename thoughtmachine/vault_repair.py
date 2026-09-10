"""vault_repair.py -- ThoughtMachine vault integrity scanner and repairer.

Phase-1 scope: DIAGNOSIS.  This module inspects a ThoughtMachine vault
(``~/.thoughtmachine`` or ``$THOUGHTMACHINE_VAULT_ROOT``) and produces a
structured, never-raising report of every integrity finding:

* schema drift vs ``agent/config/schema_manifest.json`` (via
  :class:`~agent.config.vault_drift.VaultDriftChecker`),
* permission-dict drift (legacy / unknown permission keys, mirrored from
  ``scripts/vault_permission_cleanup_dryrun.py``),
* seeded-file drift vs the ``resources/`` seed files,
* files present in the vault root but undeclared by the manifest.

Phase-2 scope (this module is ADDITIVE on top of phase 1): ``--apply``
repair.  The default remains read-only; ``run_repair(apply=True)`` / the
``--apply`` CLI flag mutate the vault under strict guardrails (see
``working_docs/vault_repair_design.md``):

* every rewrite of an existing file is preceded by a timestamped sibling
  ``<name>.bak-<YYYYmmddHHMMSS>`` backup (never deleted),
* nothing is ever deleted: unknown files/keys are quarantined (moved under
  ``<vault_root>/.quarantine``) or recorded, never destroyed,
* permission tightening only: legacy ``git_read``/``git_write`` fields are
  folded into the canonical ``git`` grain at *at least* their legacy
  permissiveness before removal; ``docker`` is converted to ``container``
  only in ceiling dicts,
* ``git_allow_worktree_commits`` is an engine-read flag (the agent/config
  validators fold ``True`` to ``git: write`` at load) and is NEVER
  auto-removed -- it is downgraded to a manual-review finding,
* seeded files (incl. the checksystem allowlist) are restored only under
  ``--restore-seeds --yes``; allowlist tampering is quarantined, never
  auto-fixed,
* ``--dry-run`` is accepted as a no-op for forward compatibility.

The module never raises and is stdlib-only apart from the repo-local drift
checker import, so it can be invoked on the host as
``python3 -m thoughtmachine.vault_repair``.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import shutil
import stat
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

__version__ = "0.2.0"
TOOL_VERSION = "thoughtmachine.vault_repair 0.2.0"
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

# Engine-read legacy flag: agent/config/models.py (migrate_git_allow_worktree_commits)
# and agent/config/session_config.py (migrate_legacy_git_fields) fold True to
# git 'write' at load time.  NEVER auto-removed by this tool (removal could
# silently reduce grants); reported as manual_review everywhere it is seen.
_GAW_KEY = "git_allow_worktree_commits"

# Canonical git grain ranks (mirrors security/resource_catalog.py
# GRANT_LEVEL_RANKS: banned < ask < read < write == write_on_feature_branch).
_GIT_MERGE_RANK = {
    "banned": 0,
    "ask": 1,
    "read": 2,
    "write": 3,
    "write_on_feature_branch": 3,
    "full": 4,
}

# Backups created by this tool: '<name>.bak-<YYYYmmddHHMMSS>[-N]'.  They do
# NOT end in '.bak' (so agent.config.vault_drift's unknown-root-file scan
# would flag them); filtered everywhere the tool scans.
_BACKUP_SUFFIX_RE = re.compile(r"\.bak-\d{14}(?:-\d+)?$")

# Default quarantine location, relative to the vault root.
#
# NOTE: deliberately NOT 'logs/quarantine'.  The schema manifest declares a
# 'logs/*' PATTERN entry with root_type 'string', and VaultDriftChecker feeds
# every glob match -- directories included -- to the text-file check, which
# raises DriftAbortError on a directory.  A quarantine dir under logs/ would
# therefore abort every post-apply re-scan.  A hidden, root-level directory is
# invisible to the drift checker (patterns never match it; the unknown-file
# scan only inspects root FILES and skips dot-names) and to the permission
# deep-scan (which prunes it explicitly).
_DEFAULT_QUARANTINE_REL = ".quarantine"

# Key-name hints used to redact secret material from repair artifacts.
_SECRET_HINTS = ("api_key", "apikey", "secret", "token", "password", "credential")

# Risk categories assigned to every issue (report summary keys).  From most to
# least severe: credential/unsafe-permission exposure (security_critical),
# permission-dict drift (permission_integrity), ordinary config drift
# (config_drift), and benign extras such as config_audit.jsonl (cosmetic).
_RISK_SECURITY_CRITICAL = "security_critical"
_RISK_PERMISSION_INTEGRITY = "permission_integrity"
_RISK_CONFIG_DRIFT = "config_drift"
_RISK_COSMETIC = "cosmetic"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _utcnow_compact() -> str:
    """Compact UTC timestamp used in backup/quarantine names."""
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")


def _is_secret_key(key: str) -> bool:
    low = key.lower()
    return any(h in low for h in _SECRET_HINTS)


def _redact_value(value: Any, key: str = "") -> Any:
    """Recursively redact values whose key names look secret."""
    if isinstance(value, dict):
        return {k: ("<redacted>" if _is_secret_key(k) else _redact_value(v, k))
                for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_value(v, key) for v in value]
    if isinstance(value, str) and _is_secret_key(key):
        return "<redacted>"
    return value


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


def _has_secret_signal(*texts: str) -> bool:
    """True when any *texts* carries a credential-ish signal.

    Used by the risk categoriser and the unsafe-file-permissions scan to
    decide whether a path/name is credential material worth protecting.
    Matches lowercased ``_SECRET_HINTS`` substrings anywhere, plus segment
    checks on '/'-separated path parts (a ``credentials`` directory, ``.pem``
    / ``.env`` suffix files, ``id_rsa``, and api_key/secret/token parts).
    """
    for text in texts:
        if not text:
            continue
        low = text.lower()
        if any(h in low for h in _SECRET_HINTS):
            return True
        for seg in low.split("/"):
            if seg == "credentials" or seg == ".env" or seg == "id_rsa":
                return True
            if seg.endswith(".pem") or seg.endswith(".env"):
                return True
            if "api_key" in seg or "secret" in seg or "token" in seg:
                return True
    return False


def _is_allowlist_file(rel: Optional[str]) -> bool:
    """True for the engine's CheckSystem allowlist (system/..._allowlist.json)."""
    return bool(rel) and rel.startswith("system/") and rel.endswith("checksystem_allowlist.json")


def _is_permissions_file(rel: Optional[str]) -> bool:
    """True for a session permissions.json (any depth)."""
    return bool(rel) and os.path.basename(rel) == "permissions.json"


def _is_ceiling_config(rel: Optional[str]) -> bool:
    """True for a workspace ceiling config (workspaces/<ws>/config.json)."""
    if not rel:
        return False
    parts = rel.split("/")
    return len(parts) == 3 and parts[0] == "workspaces" and parts[-1] == "config.json"


def _collect_host_resources_type_issue(rel: str, doc: Any,
                                       sink: "_IssueSink") -> None:
    """Flag a ceiling config whose ``allow_host_resources`` is present but not a
    JSON boolean.

    The key is the operator gate for host-resource execution
    (``tools.host_resource_policy``) whose contract is strictly boolean: only a
    literal ``true`` enables host resources, so a string / number / ``null``
    silently changes the gate's meaning.  A *missing* key is the ordinary
    optional-field case and is never reported here.
    """
    if not isinstance(doc, dict):
        return
    if "allow_host_resources" not in doc:
        return
    value = doc["allow_host_resources"]
    if isinstance(value, bool):
        return
    sink.add(
        file=rel,
        path_in_file="allow_host_resources",
        category="host_resources_type",
        classification="manual_review",
        severity="warning",
        message=(
            "`allow_host_resources` in a workspace config is not a boolean "
            "(found %r); only a literal `true` enables host resources."
            % (value,)
        ),
        fix="normalize `allow_host_resources` to a boolean (`true` / `false`)",
    )


def _risk_category(file: Optional[str], path_in_file: str,
                   engine_category: str, message: str) -> str:
    """Bucket an issue into one of the four report risk categories.

    security_critical    -- credential exposure / unsafe permissions
                            (the engine surfaces these at severity 'error')
    permission_integrity -- drift in permission dicts proper (legacy keys,
                            unknown permission keys, structural damage to a
                            permissions.json / ceiling config)
    config_drift         -- schema drift of ordinary config (the default)
    cosmetic             -- benign extras (e.g. config_audit.jsonl)
    """
    rel = file or ""
    pif = path_in_file or ""
    msg = message or ""
    permission_scoped = (
        pif.startswith("$.permissions")
        or "session_permissions" in pif
        or pif in ("git_read", "git_write", "execution",
                   "git_allow_worktree_commits")
    )
    if engine_category == "host_resources_type":
        # A present-but-non-boolean operator gate is permission-dict damage on
        # a ceiling config -- distinct from the security_critical missing-field
        # / unsafe-permission findings, and never auto-applied.
        return _RISK_PERMISSION_INTEGRITY
    if engine_category == "unsafe_file_permissions":
        return _RISK_SECURITY_CRITICAL
    if _is_allowlist_file(rel):
        # Allowlist tampering is quarantined, never auto-fixed: the engine
        # forces severity 'error' for every issue on this file.
        return _RISK_SECURITY_CRITICAL
    if "allow_host_resources" in pif or "allow_host_resources" in msg:
        return _RISK_SECURITY_CRITICAL
    if _has_secret_signal(rel, pif, msg):
        return _RISK_SECURITY_CRITICAL
    if engine_category == "legacy_permission_key":
        return _RISK_PERMISSION_INTEGRITY
    if engine_category in ("unknown_top_key", "unknown_nested_key"):
        if permission_scoped or _is_permissions_file(rel) or "permission-shaped" in msg:
            return _RISK_PERMISSION_INTEGRITY
    if engine_category in ("type_mismatch", "content", "invalid_json",
                           "schema_error"):
        if _is_permissions_file(rel) or _is_ceiling_config(rel):
            return _RISK_PERMISSION_INTEGRITY
    if ("git_allow_worktree_commits" in pif or "git_allow_worktree_commits" in msg
            or "git_write" in pif or "git_read" in pif):
        return _RISK_PERMISSION_INTEGRITY
    if engine_category == "extra_file":
        # Root audit/journal artifacts the tool itself writes elsewhere are
        # expected noise; anything else extra is cosmetic.
        return _RISK_CONFIG_DRIFT if "config_audit" in rel else _RISK_COSMETIC
    if engine_category == "seeded_drift":
        return _RISK_CONFIG_DRIFT
    if _is_permissions_file(rel) or permission_scoped:
        return _RISK_PERMISSION_INTEGRITY
    return _RISK_CONFIG_DRIFT


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
        file_spec = _manifest_spec_for(rel, manifest_files) or {}
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
            if issue == "undeclared field" and field == _GAW_KEY:
                # git_allow_worktree_commits is an engine-read flag (folded to
                # git 'write' at load by agent/config validators); never offer
                # machine removal -- removal could silently reduce grants.
                sink.add(
                    file=rel, path_in_file=field, category="unknown_top_key",
                    classification="manual_review", severity="warning",
                    message=(
                        'Legacy engine-read flag "%s" in %s -- agent/config '
                        'validators (models.py migrate_git_allow_worktree_commits, '
                        'session_config.py migrate_legacy_git_fields) fold True to '
                        'git "write" at load; left for human review'
                        % (_GAW_KEY, rel)
                    ),
                    fix="review (engine-read legacy flag)",
                )
                continue
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
        if _BACKUP_SUFFIX_RE.search(name):
            # Backup siblings created by this tool ('<name>.bak-<ts>') would
            # otherwise resurface as unknown files on every post-apply scan.
            continue
        if name not in extra_files:
            extra_files.append(name)
        sink.add(file=name, path_in_file="", category="extra_file",
                 classification="manual_review", severity="warning",
                 message=w, fix="Review and remove if not needed")


def _pattern_segments_match(pattern: str, relpath: str) -> bool:
    """Match *relpath* against a manifest glob *pattern* segment-wise.

    Both sides are split on '/'; every segment must match via
    fnmatch.fnmatchcase, so a '*' never crosses a directory boundary (the
    same semantics VaultDriftChecker relies on when globbing pattern entries).
    """
    pat_segs = pattern.split("/")
    rel_segs = relpath.split("/")
    if len(pat_segs) != len(rel_segs):
        return False
    return all(fnmatch.fnmatchcase(rp, pp)
               for rp, pp in zip(rel_segs, pat_segs))


def _manifest_spec_for(rel: str, manifest_files: Dict[str, Any]) -> Optional[dict]:
    """Return the manifest spec governing *rel*, or None.

    An exact key match wins; otherwise the first pattern entry (sorted by
    key) whose glob matches the whole relative path segment-wise.  Pattern
    entries carry ``"pattern": true`` and use their manifest KEY as the glob.
    """
    if not manifest_files:
        return None
    if rel in manifest_files:
        spec = manifest_files[rel]
        return spec if isinstance(spec, dict) else None
    for key in sorted(manifest_files):
        spec = manifest_files[key]
        if not isinstance(spec, dict):
            continue
        if not spec.get("pattern"):
            continue
        if _pattern_segments_match(key, rel):
            return spec
    return None


def _declared_root_fields(spec: Optional[dict]) -> Optional[dict]:
    """Return the declared top-level ``fields`` of a root_type-dict spec."""
    if not isinstance(spec, dict):
        return None
    if spec.get("root_type") != "dict":
        return None
    fields = spec.get("fields")
    if not isinstance(fields, dict) or not fields:
        return None
    return fields


def _collect_permission_issues(root: Path, sink: _IssueSink,
                               manifest_files: Optional[Dict[str, Any]] = None) -> None:
    """Walk all ``*.json`` vault files and report permission-dict drift.

    *manifest_files* (``manifest['files']``) bounds the deep scan of
    declared-root dicts (see ``_deep_permission_scan``).
    """
    if not root.is_dir():
        return
    manifest_files = manifest_files or {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        # The drift checker never descends into hidden dirs (its globs and
        # the unknown-file scan ignore dot-names); mirror that here so this
        # tool's own '.quarantine' audit dir is never re-scanned.
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in sorted(filenames):
            if not name.endswith(".json"):
                continue
            if _BACKUP_SUFFIX_RE.search(name):
                # '<name>.bak-<ts>' siblings created by this tool's backups
                # must never be re-scanned (they would otherwise resurface as
                # unknown files / invalid JSON on every post-apply scan).
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
            if (role, variant) == ("ceiling", "config"):
                _collect_host_resources_type_issue(rel, doc, sink)
            locations = locate(role, variant, doc)
            scoped_ids = set()
            for label, perm_dict, vocab in locations:
                scoped_ids.add(id(perm_dict))
                ceiling = vocab == CEILING_VOCAB
                for k in sorted(perm_dict):
                    path = "%s.%s" % (label, k)
                    if ceiling:
                        if k == _GAW_KEY:
                            sink.add(file=rel, path_in_file=path,
                                     category="legacy_permission_key",
                                     classification="manual_review",
                                     severity="warning",
                                     message='Legacy engine-read flag "%s" in ceiling permission dict — agent/config validators fold True to git "write" at load; left for human review' % k,
                                     fix="review (engine-read legacy flag)")
                        elif k in LEGACY_SET:
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
                        if k == _GAW_KEY:
                            sink.add(file=rel, path_in_file=path,
                                     category="legacy_permission_key",
                                     classification="manual_review",
                                     severity="warning",
                                     message='Legacy engine-read flag "%s" in session/agent permission dict — agent/config validators fold True to git "write" at load; left for human review' % k,
                                     fix="review (engine-read legacy flag)")
                        elif k in LEGACY_SET:
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
            if (role, variant) == ("session", "session_metadata"):
                # Session descriptor files (workspaces/<ws>/sessions/*.json,
                # workspaces/<ws>/sessions/<sid>/session.json + _meta_*.json,
                # legacy sessions/*.json) carry exactly ONE permission
                # location -- the embedded
                # metadata.session_config.session_permissions dict handled by
                # the scoped scan above.  The remainder of the document is a
                # session transcript + SessionConfig snapshot, never a
                # permission map: an unbounded deep scan would treat a
                # permission-named key anywhere in metadata.session_config
                # (e.g. legacy git_write / git_read /
                # git_allow_worktree_commits that agent/config folds at load)
                # as a permission-shaped object and flag every config key
                # (base_url, max_turns, ...) as an unknown permission key.
                # Mirrors the dryrun source of truth: only scoped locations
                # are examined -- no fallback into session_config or the
                # whole document.
                continue
            spec = _manifest_spec_for(rel, manifest_files)
            _deep_permission_scan(rel, doc, sink, scoped_ids,
                                  declared_fields=_declared_root_fields(spec))


def _deep_permission_scan(rel: str, doc: Any, sink: _IssueSink,
                          skip_ids: set,
                          declared_fields: Optional[Dict[str, Any]] = None) -> None:
    """Report permission-shaped nested dicts that are not scoped locations.

    When *declared_fields* is given (the top-level ``fields`` of a
    root_type-dict manifest spec), the scan bounds the document root to the
    declared keys: undeclared top-level keys of a declared-root file are
    owned by the drift checker (machine removal of the whole subtree), so
    reporting stale nested issues inside them would double-report and leave
    issues pointing at already-removed nodes.
    """
    visited = set()

    def walk(value: Any, path: str) -> None:
        if isinstance(value, dict):
            if id(value) in visited or id(value) in skip_ids:
                return
            visited.add(id(value))
            keys = set(value)
            declared_root = bool(declared_fields) and path == "root"
            children = (sorted(keys & set(declared_fields)) if declared_root
                        else sorted(keys))
            if not declared_root and keys & _PERM_INTEREST:
                for k in children:
                    if k == _GAW_KEY:
                        sink.add(file=rel, path_in_file="%s.%s" % (path, k),
                                 category="legacy_permission_key",
                                 classification="manual_review",
                                 severity="warning",
                                 message='Legacy engine-read flag "%s" in permission-shaped object at %s::%s — agent/config validators fold True to git "write" at load; left for human review' % (k, rel, path),
                                 fix="review (engine-read legacy flag)")
                    elif k in LEGACY_SET:
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
            for k in children:
                walk(value[k], "%s.%s" % (path, k))
        elif isinstance(value, list):
            for idx, item in enumerate(value):
                walk(item, "%s[%d]" % (path, idx))

    walk(doc, "root")


def _collect_unsafe_permission_issues(root: Path, sink: _IssueSink) -> None:
    """Report world-readable credential files/dirs (read-only filesystem scan).

    A file is in scope when its parent directory is named ``credentials`` or
    the file name itself carries a secret signal (``_has_secret_signal``).
    Any in-scope regular file whose mode grants other-read (0o004) is
    reported as ``unsafe_file_permissions`` with classification
    ``manual_review`` -- the tool never rewrites modes; the suggested
    ``chmod 0o700`` is for the user.  Hidden/pruned directories are skipped
    (mirrors every other vault scan) and symlinks are never followed.
    """
    if not root.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        dirnames[:] = [d for d in dirnames
                       if not d.startswith(".")
                       and d not in (".quarantine", ".bak", ".git")]
        dir_path = Path(dirpath)
        for name in sorted(filenames):
            if not (dir_path.name == "credentials" or _has_secret_signal(name)):
                continue
            full = dir_path / name
            try:
                mode = stat.S_IMODE(os.stat(full).st_mode)
            except OSError:
                continue
            if mode & stat.S_IROTH:
                rel = str(full.relative_to(root))
                sink.add(file=rel, path_in_file="",
                         category="unsafe_file_permissions",
                         classification="manual_review", severity="error",
                         message=("world-readable credentials path: %s -- "
                                  "chmod 0o700 recommended" % rel),
                         fix="chmod 0o700 %s" % rel)


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


# ---------------------------------------------------------------------------
# Phase-2 repair engine.
#
# Guardrails (see working_docs/vault_repair_design.md): every rewrite of an
# existing file is preceded by a timestamped sibling '<name>.bak-<ts>' backup
# (never deleted, never re-scanned); removed nodes are quarantined as JSON
# artifacts under '<vault_root>/.quarantine' (never destroyed); permission
# changes only tighten -- legacy git_read/git_write/execution keys are folded
# into the canonical 'git' grain at >= their legacy permissiveness and 'docker'
# is converted to 'container' in ceiling dicts; git_allow_worktree_commits is
# an engine-read flag and is NEVER auto-removed; seeded files are restored
# only under --restore-seeds --yes, and allowlist tampering is quarantined,
# never auto-fixed.
# ---------------------------------------------------------------------------

# Broader = smaller number (mirrors security/resource_catalog.py
# GRANT_LEVEL_RANKS: write is broader than write_on_feature_branch ...).
_GIT_BREADTH = {
    "write": 0,
    "write_on_feature_branch": 1,
    "read": 2,
    "ask": 3,
    "banned": 4,
}

# Segment matcher: '<key>[<idx>][<idx>]...' (dict key, then list indices).
_SEG_RE = re.compile(r"([^\[]+)((\[\d+\])*)")


def _resolve_quarantine_dir(root: Path, quarantine_dir: Optional[Any] = None) -> Path:
    """Resolve (and create) the quarantine dir for removed-node artifacts."""
    q = Path(quarantine_dir).expanduser() if quarantine_dir \
        else (root / _DEFAULT_QUARANTINE_REL)
    if not q.is_absolute():
        q = root / q
    q.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(q, 0o700)
    except OSError:
        pass
    return q


def _unique_path(path: Path) -> Path:
    """Return *path*, or the first '<name>-N' sibling that does not exist."""
    if not path.exists():
        return path
    for i in range(1, 10000):
        candidate = path.with_name("%s-%d" % (path.name, i))
        if not candidate.exists():
            return candidate
    return path.with_name("%s-9999" % path.name)


def _canon_path(path_in_file: str) -> str:
    """Normalize a locate-style path label into a dot path from the doc root.

    'root' -> '', '$.permissions' -> 'permissions',
    '$.metadata.session_config.session_permissions' ->
    'metadata.session_config.session_permissions', 'root.a.b' -> 'a.b'.
    """
    p = path_in_file or ""
    if p.startswith("$."):
        p = p[2:]
    if p == "root":
        return ""
    if p.startswith("root."):
        p = p[len("root."):]
    return p


def _parent_path_of(canon: str) -> Tuple[str, str]:
    """Split *canon* into its parent dot path and final segment."""
    if "." in canon:
        head, _, last = canon.rpartition(".")
        return head, last
    return "", canon


def _backup_file(path: Path, made: Dict[str, str]) -> Optional[Path]:
    """Copy *path* to a timestamped sibling backup; record it in *made*."""
    if not path.is_file():
        return None
    ts = _utcnow_compact()
    target = _unique_path(path.with_name("%s.bak-%s" % (path.name, ts)))
    try:
        shutil.copy2(path, target)
    except OSError:
        return None
    made[str(path)] = str(target)
    return target


def _atomic_write_bytes(path: Path, data: bytes, new_mode: int = 0o600) -> None:
    """Atomically write *data* to *path* (tmp file + os.replace)."""
    prev_mode = None
    if path.exists():
        try:
            prev_mode = stat.S_IMODE(path.stat().st_mode)
        except OSError:
            pass
    tmp = path.with_name(".%s.tmp-%s" % (path.name, _utcnow_compact()))
    try:
        tmp.write_bytes(data)
        try:
            os.chmod(tmp, prev_mode if prev_mode is not None else new_mode)
        except OSError:
            pass
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _atomic_write_json_file(path: Path, obj: Any, new_mode: int = 0o600) -> None:
    """Atomically write *obj* as pretty JSON with a trailing newline."""
    _atomic_write_bytes(path, (json.dumps(obj, indent=2) + "\n").encode("utf-8"),
                        new_mode)


def _resolve_parent(doc: Any, dotted: str) -> Tuple[Any, str]:
    """Navigate *dotted* from *doc* to its parent dict and final key.

    Segments are split on '.'; an optional '[<idx>]' suffix on a segment
    addresses list elements (applied while descending intermediate segments).
    The final segment's indices are IGNORED: removal only ever targets dict
    keys, never list elements.  Raises KeyError/TypeError/IndexError/
    ValueError when the path does not exist or *doc* is not a dict at the
    top level; empty *dotted* raises ValueError.
    """
    if not dotted:
        raise ValueError("empty path")
    parts = dotted.split(".")
    parent = doc
    for i, part in enumerate(parts):
        m = _SEG_RE.match(part)
        if not m:
            raise KeyError("malformed segment %r" % part)
        key = m.group(1)
        if i == len(parts) - 1:
            return parent, key
        node = parent[key]
        for idx in (int(idx) for idx in re.findall(r"\[(\d+)\]", part)):
            node = node[idx]
        parent = node
    raise KeyError("unreachable path %r" % dotted)  # pragma: no cover


def _git_rank(value: Any) -> Optional[int]:
    """Canonical rank of a git grain string; None for non-strings/unknown."""
    if not isinstance(value, str):
        return None
    return _GIT_MERGE_RANK.get(value.strip().lower())


def _legacy_git_contributions(perm_dict: dict) -> List[Tuple[str, str, int]]:
    """Return (source_key, canonical_value, rank) for legacy git fields."""
    out: List[Tuple[str, str, int]] = []
    if perm_dict.get(_GAW_KEY) is True:
        out.append((_GAW_KEY, "write", 3))
    for key in ("git_write", "git_read"):
        if key not in perm_dict:
            continue
        val = perm_dict[key]
        if isinstance(val, bool):
            canon = "write" if val else "banned"
            out.append((key, canon, _GIT_MERGE_RANK[canon]))
            continue
        if not isinstance(val, str):
            continue
        norm = val.strip().lower()
        if norm in ("write", "full"):
            out.append((key, "write", 3))
        elif norm == "write_on_feature_branch":
            out.append((key, "write_on_feature_branch", 3))
        elif norm in ("read", "ask", "banned"):
            out.append((key, norm, _GIT_MERGE_RANK[norm]))
        # Unrecognised values have no safe fold target; skipped.
    return out


def _merged_git_value(perm_dict: dict) -> Optional[str]:
    """Fold legacy git fields into the canonical 'git' value.

    Picks the most permissive candidate (rank desc), ties broken by breadth
    asc and existing 'git' winning (prio 0 < 1).  Never downgrades an
    existing canonical value.
    """
    candidates: List[Tuple[int, int, int, str]] = []
    existing = perm_dict.get("git")
    if isinstance(existing, str):
        rank = _git_rank(existing)
        if rank is not None:
            candidates.append((rank, _GIT_BREADTH.get(existing, 9), 0, existing))
    for src, val, rank in _legacy_git_contributions(perm_dict):
        candidates.append((rank, _GIT_BREADTH.get(val, 9), 1, val))
    if not candidates:
        return None
    candidates.sort(key=lambda c: (-c[0], c[1], c[2]))
    return candidates[0][3]


def _quarantine_artifact_path(quarantine_dir: Path, rel: str, dotted: str,
                              category: str, removed: Any,
                              hint_key: str) -> Path:
    """Write one JSON artifact for a removed node; return its path.

    OSError propagates to the caller (treated as an abort of the rewrite).
    """
    payload = {
        "quarantined_from": rel,
        "path_in_file": dotted,
        "category": category,
        "removed_at": _utcnow_iso(),
        "value": _redact_value(removed, hint_key),
    }
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", "%s__%s" % (rel, dotted or "root"))
    name = _unique_path(quarantine_dir /
                        ("%s__%s.json" % (safe, _utcnow_compact())))
    _atomic_write_json_file(name, payload, new_mode=0o600)
    return name


def _read_json_doc(root: Path, rel: str) -> Tuple[Path, Any]:
    """Read and parse *rel* under *root*. OSError/JSONDecodeError propagate."""
    p = root / rel
    doc = json.loads(p.read_text(encoding="utf-8"))
    return p, doc


def _remove_node(doc: Any, canon: str) -> None:
    """Delete the dict key addressed by *canon* from *doc*."""
    parent, last = _resolve_parent(doc, canon)
    del parent[last]


def _get_node(doc: Any, canon: str) -> Any:
    """Return the value addressed by *canon* (descends list indices too)."""
    if not canon:
        raise KeyError("empty path")
    node = doc
    for part in canon.split("."):
        m = _SEG_RE.match(part)
        if not m:
            raise KeyError("malformed segment %r" % part)
        node = node[m.group(1)]
        for idx in (int(idx) for idx in re.findall(r"\[(\d+)\]", part)):
            node = node[idx]
    return node


def _fix_issue(root: Path, quarantine_dir: Path, issue: dict,
               made: Dict[str, str]) -> dict:
    """Perform exactly ONE machine fix for *issue*.  Raises on failure.

    The file is re-read per issue (fixes run against the pre-apply report and
    later fixes in the same run may have rewritten the same file).
    """
    rel = issue.get("file") or ""
    category = issue.get("category") or ""
    path_in_file = issue.get("path_in_file") or ""
    full = root / rel

    if category == "missing_file":
        if full.exists():
            raise ValueError("target file already exists: %s" % rel)
        default = issue.get("default_value")
        if default is None:
            raise ValueError("no schema safe_default for %s" % rel)
        full.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(default, str):
            _atomic_write_bytes(full, default.encode("utf-8"))
        else:
            _atomic_write_json_file(full, default)
        return {"status": "applied", "action": "created from schema safe_default"}

    _, doc = _read_json_doc(root, rel)
    if not isinstance(doc, dict):
        raise ValueError("document root is not a dict: %s" % rel)
    canon = _canon_path(path_in_file)

    def rewrite_with_backup() -> None:
        if full.is_file():
            bak = _backup_file(full, made)
            if bak is None:
                raise ValueError("backup failed; rewrite aborted")
        _atomic_write_json_file(full, doc)

    if category == "missing_field":
        default = issue.get("default_value")
        if default is None:
            raise ValueError("no schema default for %s::%s" % (rel, canon))
        parent, last = _resolve_parent(doc, canon)
        if last in parent:
            raise ValueError("field %r already present in %s" % (last, rel))
        parent[last] = default
        rewrite_with_backup()
        return {"status": "applied", "action": "backfilled %s from schema default" % last}

    if category in ("unknown_top_key", "unknown_nested_key"):
        removed = _get_node(doc, canon)
        hint_key = _parent_path_of(canon)[1] or canon or "value"
        _quarantine_artifact_path(quarantine_dir, rel, canon, category,
                                  removed, hint_key)
        if full.is_file():
            bak = _backup_file(full, made)
            if bak is None:
                raise ValueError("backup failed; rewrite aborted")
        _remove_node(doc, canon)
        _atomic_write_json_file(full, doc)
        return {"status": "applied",
                "action": "removed %s; original quarantined under %s"
                          % (canon or "<root>", _DEFAULT_QUARANTINE_REL)}

    if category == "legacy_permission_key":
        parent, last = _resolve_parent(doc, canon)
        if last == _GAW_KEY:
            raise ValueError("manual review only (engine-read flag; never auto-removed)")
        if last == "docker":
            if "container" in parent:
                raise ValueError("'container' already present; refusing to overwrite")
            docker_val = parent["docker"]
            parent["container"] = (docker_val if isinstance(docker_val, bool)
                                   else (docker_val == "write"))
            del parent["docker"]
            rewrite_with_backup()
            return {"status": "applied", "action": "converted docker to container"}
        if last in ("git_write", "git_read", "execution"):
            merged = _merged_git_value(parent)
            if merged is None:
                raise ValueError("no canonical git value to fold legacy key %s into" % last)
            parent["git"] = merged
            del parent[last]
            rewrite_with_backup()
            return {"status": "applied", "action": "folded %s into git=%s" % (last, merged)}
        raise ValueError("unsupported legacy key %s" % last)

    raise ValueError("unsupported category %s" % category)


def _apply_fixes(root: Path, quarantine_dir: Path, report: dict,
                 made: Dict[str, str]) -> List[dict]:
    """Apply every machine_apply issue in *report*.  Never raises."""
    performed: List[dict] = []
    for issue in report.get("issues", []):
        if issue.get("classification") != "machine_apply":
            continue
        if issue.get("risk_category") == _RISK_SECURITY_CRITICAL:
            # security-critical requires explicit repair_ids selection
            continue
        entry: Dict[str, Any] = {
            "id": issue.get("id"),
            "file": issue.get("file"),
            "path_in_file": issue.get("path_in_file") or "",
            "category": issue.get("category"),
        }
        try:
            res = _fix_issue(root, quarantine_dir, issue, made)
            entry["status"] = "applied"
            entry["action"] = res["action"]
        except Exception as exc:
            entry["status"] = "error"
            entry["error"] = str(exc)
        performed.append(entry)
    return performed


def _restore_seeded_seeds(root: Path, quarantine_dir: Path, report: dict,
                          made: Dict[str, str]) -> List[dict]:
    """Restore missing/drifted seeded files under --restore-seeds --yes.

    Allowlist drift is only quarantined (never auto-fixed); other drifted
    seeded files are restored from the seed after a sibling backup.  Missing
    seeded files are re-created from the seed directly.
    """
    performed: List[dict] = []
    resources = _resources_dir()
    for entry in sorted(report.get("seeded_files", []),
                        key=lambda e: str(e.get("file"))):
        rel = entry["file"]
        status = entry.get("status")
        if status not in ("missing", "drift"):
            continue
        seed_name = _SEED_MAP.get(rel)
        if not seed_name:
            continue
        seed = resources / seed_name
        vpath = root / rel
        if not seed.is_file():
            performed.append({"file": rel, "category": "seeded_drift",
                              "status": "error",
                              "error": "no seed resource %s" % seed_name})
            continue
        if status == "missing":
            vpath.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(seed, vpath)
            performed.append({"file": rel, "category": "seeded_drift",
                              "status": "applied", "action": "restored from seed"})
            continue
        # Drift.
        if "checksystem_allowlist" in rel:
            try:
                payload = json.loads(vpath.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                payload = vpath.read_text(encoding="utf-8")
            try:
                _quarantine_artifact_path(quarantine_dir, rel, "", "seeded_drift",
                                          payload, rel)
            except OSError:
                performed.append({"file": rel, "category": "seeded_drift",
                                  "status": "error",
                                  "error": "quarantine write failed"})
                continue
            performed.append({"file": rel, "category": "seeded_drift",
                              "status": "applied",
                              "action": "quarantined allowlist drift; not auto-fixed"})
            continue
        bak = _backup_file(vpath, made)
        if bak is None:
            performed.append({"file": rel, "category": "seeded_drift",
                              "status": "error",
                              "error": "backup failed; restore aborted"})
            continue
        shutil.copy2(seed, vpath)
        performed.append({"file": rel, "category": "seeded_drift",
                          "status": "applied", "action": "restored from seed"})
    return performed


# ---------------------------------------------------------------------------
# Repair selection (run_repair's repair_ids / categories kwargs).
#
# An explicit selection names a SUBSET of the pre-apply findings to repair.
# Selection overrides an issue's classification (manual_review issues are
# attempted) but only for engine-safe categories; git_allow_worktree_commits
# is never auto-removed and extra root files are quarantined by moving them
# (no rewrite / no backup / nothing deleted).
# ---------------------------------------------------------------------------

# Public category aliases -> internal issue categories.
_CATEGORY_ALIASES = {
    "missing_field": frozenset(("missing_field",)),
    "missing_file": frozenset(("missing_file",)),
    "unknown_key": frozenset(("unknown_top_key", "unknown_nested_key")),
    "legacy_permission": frozenset(("legacy_permission_key",)),
    "unknown_root_file": frozenset(("extra_file",)),
    "seeded_drift": frozenset(("seeded_drift",)),
}

# Internal issue categories with an engine-safe fix when explicitly
# selected.  The raw engine names are accepted alongside the aliases above.
_FIXABLE_SELECTABLE = frozenset(
    ("missing_file", "missing_field", "unknown_top_key",
     "unknown_nested_key", "legacy_permission_key", "extra_file",
     "seeded_drift")
)


def _expand_selection_category(category: Any) -> Optional[frozenset]:
    """Expand one public selection category into engine issue categories.

    Returns None when *category* is neither a known alias nor an engine
    issue category the engine can fix safely.
    """
    if not isinstance(category, str):
        return None
    key = category.strip()
    if not key:
        return None
    if key in _CATEGORY_ALIASES:
        return _CATEGORY_ALIASES[key]
    if key in _FIXABLE_SELECTABLE:
        return frozenset((key,))
    return None


def _parse_selection(repair_ids: Optional[Any],
                     categories: Optional[Any]) -> Optional[dict]:
    """Normalize the selection kwargs; None when nothing was selected.

    The result echoes the RAW requested ids/categories (sorted, de-duplicated
    strings) so the repair report can state exactly what was asked for.
    Unknown category names are only detectable against the pre-apply issue
    list and are resolved later by ``_resolve_selection``.
    """
    try:
        ids_raw = list(repair_ids) if repair_ids is not None else []
        cats_raw = list(categories) if categories is not None else []
    except TypeError:  # pragma: no cover - defensive
        ids_raw = []
        cats_raw = []
    ids = sorted({str(v).strip() for v in ids_raw
                  if isinstance(v, (str, int)) and str(v).strip()})
    cats = sorted({str(v).strip() for v in cats_raw
                   if isinstance(v, (str, int)) and str(v).strip()})
    if not ids and not cats:
        return None
    return {"ids": ids, "categories": cats}


def _resolve_selection(report: dict, parsed: dict) -> dict:
    """Resolve a parsed selection against the pre-apply report.

    Expands category aliases to engine categories, splits repair ids into
    known/unknown, detects unknown category names (these abort the whole
    selection: the caller performs no fix when ``errors`` is non-empty) and
    decides whether seed restoration was requested through the selection.
    """
    by_id = {i.get("id"): i for i in (report.get("issues") or [])
             if i.get("id")}
    cat_expanded: set = set()
    errors: List[dict] = []
    for cat in parsed["categories"]:
        expanded = _expand_selection_category(cat)
        if expanded is None:
            errors.append({
                "id": None, "file": "", "path_in_file": "",
                "category": cat, "status": "error",
                "error": "unknown category %s" % cat,
            })
        else:
            cat_expanded.update(expanded)
    known_ids = [rid for rid in parsed["ids"] if rid in by_id]
    unknown_ids = [rid for rid in parsed["ids"] if rid not in by_id]
    seed_restore = "seeded_drift" in cat_expanded
    if not seed_restore:
        for rid in known_ids:
            if by_id[rid].get("category") == "seeded_drift":
                seed_restore = True
                break
    return {
        "known_ids": known_ids,
        "unknown_ids": unknown_ids,
        "cat_expanded": cat_expanded,
        "seed_restore": seed_restore,
        "errors": errors,
    }


def _is_gaw_target(path_in_file: str) -> bool:
    """True when the issue addresses the engine-read GAW legacy flag."""
    p = path_in_file or ""
    return p == _GAW_KEY or p.endswith("." + _GAW_KEY)


def _quarantine_root_file(root: Path, quarantine_dir: Path, name: str) -> Path:
    """Move an unknown vault-root file into the quarantine dir.

    The destination is uniquified (``<name>``, ``<name>-N``); the move is a
    plain ``shutil.move`` -- no rewrite, no sibling backup, nothing deleted.
    Raises on failure.
    """
    if not name or name != Path(name).name:
        raise ValueError("unsafe file name for quarantine move: %r" % name)
    source = root / name
    if not source.is_file():
        raise ValueError("unknown root file is not present: %s" % name)
    dest = _unique_path(quarantine_dir / name)
    try:
        shutil.move(str(source), str(dest))
    except OSError as exc:
        raise ValueError("quarantine move failed for %s: %s" % (name, exc))
    return dest


def _repair_report(requested_apply: bool, restore_seeds: bool,
                   performed: List[dict], backups: List[dict],
                   selection: Optional[dict],
                   error: Optional[str] = None) -> dict:
    """Assemble the ``report['repair']`` dict.

    The no-selection shape is fixed (``requested_apply``/``restore_seeds``/
    ``performed``/``backups`` plus an optional ``error``); the
    ``requested_ids``/``requested_categories`` metadata keys are only added
    when a selection was given.
    """
    report: Dict[str, Any] = {
        "requested_apply": bool(requested_apply),
        "restore_seeds": bool(restore_seeds),
    }
    if error is not None:
        report["error"] = error
    if selection is not None:
        report["requested_ids"] = list(selection["ids"])
        report["requested_categories"] = list(selection["categories"])
    report["performed"] = performed
    report["backups"] = backups
    return report


def _apply_selected_fixes(root: Path, quarantine_dir: Path, report: dict,
                          made: Dict[str, str], sel: dict) -> List[dict]:
    """Apply exactly the issues selected by repair_ids/categories.

    Never raises.  An explicit selection overrides an issue's classification
    (manual_review issues are attempted), but only engine-safe categories:
    extra_file issues are quarantined by moving the unknown root file,
    ``git_allow_worktree_commits`` is never removed, and issues the engine
    has no safe fix for (content, type_mismatch, ...) yield an error entry.
    ``seeded_drift`` issues are left to ``_restore_seeded_seeds``.
    """
    performed: List[dict] = []
    issues = report.get("issues") or []
    by_id = {i.get("id"): i for i in issues if i.get("id")}
    # ordered items carry an ``explicit`` flag: issues named by repair_ids
    # are explicitly requested and always attempted, while issues that only
    # match a category expansion are gated on risk (security_critical issues
    # require an explicit repair_ids selection).
    ordered: List[Tuple[str, Any, bool]] = []
    seen = set()
    for rid in sel["known_ids"]:
        issue = by_id[rid]
        if issue.get("id") not in seen:
            seen.add(issue.get("id"))
            ordered.append(("issue", issue, True))
    for rid in sel["unknown_ids"]:
        ordered.append(("unknown_id", rid, False))
    for issue in issues:
        if (issue.get("category") or "") in sel["cat_expanded"] \
                and issue.get("id") not in seen:
            seen.add(issue.get("id"))
            ordered.append(("issue", issue, False))
    for kind, payload, explicit in ordered:
        if kind == "unknown_id":
            performed.append({
                "id": payload, "file": "", "path_in_file": "",
                "category": "", "status": "error",
                "error": "unknown repair id",
            })
            continue
        issue = payload
        category = issue.get("category") or ""
        entry: Dict[str, Any] = {
            "id": issue.get("id"),
            "file": issue.get("file"),
            "path_in_file": issue.get("path_in_file") or "",
            "category": category,
        }
        try:
            if not explicit and issue.get("risk_category") == \
                    _RISK_SECURITY_CRITICAL:
                raise ValueError(
                    "security_critical: requires explicit repair_ids selection")
            if category == "seeded_drift":
                # Restored (as a whole) by _restore_seeded_seeds.
                continue
            if category == "extra_file":
                _quarantine_root_file(root, quarantine_dir,
                                      issue.get("file") or "")
                entry["status"] = "applied"
                entry["action"] = "quarantined unknown root file"
            elif _is_gaw_target(entry["path_in_file"]):
                raise ValueError(
                    "manual review only (engine-read flag; never auto-removed)")
            elif category in _FIXABLE_SELECTABLE:
                res = _fix_issue(root, quarantine_dir, issue, made)
                entry["status"] = "applied"
                entry["action"] = res["action"]
            else:
                raise ValueError("no safe fix for category %s" % category)
        except Exception as exc:
            entry["status"] = "error"
            entry["error"] = str(exc)
        performed.append(entry)
    return performed


def run_repair(vault_root: Any, apply: bool = False,
               quarantine_dir: Optional[Any] = None,
               restore_seeds: bool = False, yes: bool = False,
               repair_ids: Optional[List[str]] = None,
               categories: Optional[List[str]] = None) -> dict:
    """Inspect the vault and optionally apply machine fixes / seed restores.

    Never raises.  ``apply=False`` without ``restore_seeds --yes`` (and with
    no *repair_ids*/*categories* selection) is a pure inspection (identical
    to ``run_inspection``).  With ``apply=True`` every machine_apply finding
    is fixed under the repair guardrails; seed restoration only happens when
    *restore_seeds* AND *yes* are True.

    *repair_ids* (pre-apply ``VR-###`` issue ids) and *categories* (aliases
    below) select a SUBSET of findings to repair.  A non-empty selection is
    an explicit mutation request on its own (it overrides an issue's
    classification -- manual_review findings are attempted -- but only for
    categories the engine can fix safely; ``git_allow_worktree_commits`` is
    never auto-removed).  When a selection is given it restricts the run:
    unselected issues are left untouched even if ``apply=True``.

    Category aliases:
      missing_field, missing_file, unknown_key (top+nested),
      legacy_permission (docker convert / legacy-key fold; never GAW),
      unknown_root_file (extra root files are moved to .quarantine),
      seeded_drift (seed restore, same guardrails as --restore-seeds --yes).
    Engine category names (unknown_top_key, extra_file, ...) are accepted
    directly as well; an unknown category aborts the selection with error
    entries and no other fix is attempted.
    """
    root = Path(vault_root)
    made: Dict[str, str] = {}
    selection = _parse_selection(repair_ids, categories)
    try:
        want_repair = bool(apply) or (restore_seeds and yes) \
            or selection is not None
        if not want_repair:
            return run_inspection(root)
        pre = run_inspection(root)
        performed: List[dict] = []
        seed_perf: List[dict] = []
        if selection is not None:
            sel = _resolve_selection(pre, selection)
            if sel["errors"]:
                performed = sel["errors"]
            else:
                quarantine = _resolve_quarantine_dir(root, quarantine_dir)
                performed = _apply_selected_fixes(root, quarantine, pre,
                                                  made, sel)
                if (restore_seeds and yes) or sel["seed_restore"]:
                    seed_perf = _restore_seeded_seeds(root, quarantine,
                                                      pre, made)
        else:
            quarantine = _resolve_quarantine_dir(root, quarantine_dir)
            performed = _apply_fixes(root, quarantine, pre, made) if apply \
                else []
            if restore_seeds and yes:
                seed_perf = _restore_seeded_seeds(root, quarantine, pre, made)
        post = run_inspection(root)
        post["run"]["dry_run"] = False
        backups = []
        for orig, bak in sorted(made.items()):
            backups.append({
                "backup": os.path.relpath(bak, str(root)),
                "backup_of": os.path.relpath(orig, str(root)),
            })
        post["repair"] = _repair_report(bool(apply),
                                        bool(restore_seeds and yes),
                                        performed + seed_perf, backups,
                                        selection)
        return post
    except Exception as exc:
        try:
            base = run_inspection(root)
        except Exception:
            base = {
                "run": {"vault_root": str(root), "dry_run": False,
                        "tool_version": TOOL_VERSION, "now": _utcnow_iso()},
                "summary": {"total_issues": 0, "by_category": {},
                            "by_classification": {"machine_apply": 0,
                                                   "manual_review": 0},
                            "security_critical": 0,
                            "permission_integrity": 0,
                            "config_drift": 0,
                            "cosmetic": 0},
                "issues": [],
                "extra_files": [],
                "seeded_files": [],
            }
        base["repair"] = _repair_report(bool(apply),
                                        bool(restore_seeds and yes),
                                        [], [], selection, error=str(exc))
        return base


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
    _collect_permission_issues(root, sink, manifest.get("files") or {})
    _collect_unsafe_permission_issues(root, sink)

    issues = sink.issues
    issues.sort(key=lambda i: (i.get("file") or "", i.get("path_in_file") or "",
                               i.get("category") or "", i.get("message") or ""))
    by_risk: Dict[str, int] = {
        _RISK_SECURITY_CRITICAL: 0,
        _RISK_PERMISSION_INTEGRITY: 0,
        _RISK_CONFIG_DRIFT: 0,
        _RISK_COSMETIC: 0,
    }
    for issue in issues:
        risk = _risk_category(issue.get("file"),
                              issue.get("path_in_file") or "",
                              issue.get("category") or "",
                              issue.get("message") or "")
        issue["risk_category"] = risk
        by_risk[risk] = by_risk.get(risk, 0) + 1
        if _is_allowlist_file(issue.get("file") or "") \
                and issue.get("severity") != "error":
            issue["severity"] = "error"
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
            "security_critical": by_risk[_RISK_SECURITY_CRITICAL],
            "permission_integrity": by_risk[_RISK_PERMISSION_INTEGRITY],
            "config_drift": by_risk[_RISK_CONFIG_DRIFT],
            "cosmetic": by_risk[_RISK_COSMETIC],
        },
        "issues": issues,
        "extra_files": extra_files,
        "seeded_files": seeded_files,
    }
    return report


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry point.

    Exit codes:
      0  healthy -- no findings
      1  findings present, but no fixes performed (read-only/dry-run, or
         --apply left findings unfixed)
      2  usage/argument error (argparse; e.g. unknown flag or missing value)
      3  vault root missing/unreadable, or repair errors occurred
      4  --apply performed one or more fixes

    Precedence: repair errors (3) override applied fixes (4); applied fixes
    (4) override remaining findings; 0 is returned only for a clean vault.
    """
    parser = argparse.ArgumentParser(
        prog="thoughtmachine.vault_repair",
        description=(
            "ThoughtMachine vault integrity scanner and repairer. Default "
            "mode is read-only diagnosis (schema drift, permission-dict "
            "drift, seeded-file drift, unknown root files). --apply mutates: "
            "machine fixes are performed under guardrails (timestamped "
            "sibling backups, .quarantine artifacts, permission tightening "
            "only). Seed restoration requires --restore-seeds --yes."
        ),
    )
    parser.add_argument("--vault-root", metavar="PATH", default=None,
                        help="Vault root to inspect (default: $THOUGHTMACHINE_VAULT_ROOT or ~/.thoughtmachine)")
    parser.add_argument("--report-json", metavar="PATH", default=None,
                        help="Write the full JSON report to this path")
    parser.add_argument("--dry-run", action="store_true",
                        help="Force read-only mode; --apply is ignored")
    parser.add_argument("--apply", action="store_true",
                        help="Mutate the vault: apply machine fixes (backups + quarantine artifacts) and re-scan")
    parser.add_argument("--quarantine-dir", metavar="PATH", default=None,
                        help="Directory for removed-node artifacts (default: <vault_root>/.quarantine)")
    parser.add_argument("--restore-seeds", action="store_true",
                        help="Restore missing/drifted seeded files (requires --yes)")
    parser.add_argument("--yes", action="store_true",
                        help="Acknowledge the --restore-seeds mutation")
    args = parser.parse_args(argv)

    root = resolve_vault_root(args.vault_root)
    dry_run = bool(args.dry_run)
    apply_mut = args.apply and not dry_run
    if apply_mut or args.restore_seeds:
        report = run_repair(root, apply=apply_mut,
                            quarantine_dir=args.quarantine_dir,
                            restore_seeds=args.restore_seeds, yes=args.yes)
    else:
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
    if summary["total_issues"]:
        print("  risk: security_critical=%d permission_integrity=%d "
              "config_drift=%d cosmetic=%d"
              % (summary["security_critical"],
                 summary["permission_integrity"],
                 summary["config_drift"],
                 summary["cosmetic"]))
    if args.report_json:
        print("  report written to %s" % args.report_json)
    if args.restore_seeds and not args.yes:
        print("  note: seed restoration requires --yes; seeds left untouched")
    repair = report.get("repair")
    if repair:
        performed = repair.get("performed") or []
        errs = [p for p in performed if p.get("status") == "error"]
        backups = repair.get("backups") or []
        print("  repair: %d performed (%d errors), %d backups"
              % (len(performed), len(errs), len(backups)))
        if repair.get("error"):
            print("  repair error: %s" % repair["error"])
    if summary["total_issues"]:
        if apply_mut:
            print("  findings remain after repair (manual_review or errors)")
        else:
            print("  findings present (read-only mode; no changes were made)")
    else:
        print("  vault is healthy (no findings)")
    if repair and (repair.get("error") or any(
            p.get("status") == "error" for p in (repair.get("performed") or []))):
        return 3
    if apply_mut:
        performed = repair.get("performed") or []
        if any(p.get("status") == "applied" for p in performed):
            # Exit 4: --apply performed at least one fix. (Usage errors exit 2
            # via argparse; exit 2 is never returned by a successful run.)
            return 4
        return 0 if summary["total_issues"] == 0 else 1
    return 0 if summary["total_issues"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
