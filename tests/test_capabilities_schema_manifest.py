"""Phase 1 \u2014 the ``capabilities.json`` schema-manifest declaration.

Guards three contracts:

1. **Drift guard.**  The schema manifest's ``workspaces/*/capabilities.json``
   entry must declare *every* field of the ``WorkspaceCapabilities`` dataclass,
   so no capability can be silently unvalidated / unrepairable.
2. **Reject-with-notice.**  A ``capabilities.json`` carrying a field the
   manifest does not declare is flagged by :class:`VaultDriftChecker` (an
   ``undeclared field`` warning drift) \u2014 never silently ignored.
3. **Honest enforcement map.**  The capability flags the two tests below
   exercise really are enforced by the security gate (``filesystem_write``
   downgrades filesystem to read; ``allow_network`` caps the network grant),
   and the forward-compat-only fields are explicitly marked ``INERT`` in the
   manifest instead of pretending to be enforced.
"""

import json
from pathlib import Path

from agent.config.vault_drift import VaultDriftChecker
from thoughtmachine.security import SessionPermissions
from thoughtmachine.workspace_capabilities import WorkspaceCapabilities

_REPO_ROOT = Path(__file__).resolve().parents[1]
_MANIFEST_PATH = _REPO_ROOT / "agent" / "config" / "schema_manifest.json"
_CAPS_ENTRY = "workspaces/*/capabilities.json"

# Fields recorded for forward-compat only; the manifest must say so.
_INERT_FIELDS = ["allowed_tools", "blocked_tools", "allowed_providers", "extra"]


def _caps_manifest_entry() -> dict:
    """Return the real manifest entry for ``workspaces/*/capabilities.json``."""
    manifest = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
    return manifest["files"][_CAPS_ENTRY]


def _write_manifest(tmp_path: Path, entry: dict) -> Path:
    manifest = {
        "schema_version": 1,
        "vault_version": 1,
        "manifest_version": 1,
        "vault_version_file": "vault_version.json",
        "files": {_CAPS_ENTRY: entry},
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest_path


# \u2500\u2500 1. Drift guard: manifest mirrors the dataclass \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500


def test_manifest_declares_every_workspace_capability_field():
    """Every ``WorkspaceCapabilities`` field must be declared in the manifest."""
    declared = set(_caps_manifest_entry()["fields"].keys())
    dataclass_fields = set(WorkspaceCapabilities.__dataclass_fields__.keys())
    missing = dataclass_fields - declared
    assert not missing, (
        "capabilities.json manifest is missing fields declared on "
        f"WorkspaceCapabilities: {sorted(missing)}"
    )


def test_forward_compat_fields_are_explicitly_marked_inert():
    """Forward-compat-only capability fields are labelled INERT, not faked."""
    fields = _caps_manifest_entry()["fields"]
    for name in _INERT_FIELDS:
        description = fields[name]["description"]
        assert "INERT" in description, (
            f"capability field '{name}' must be explicitly marked INERT "
            "in its manifest description"
        )


# \u2500\u2500 2. Reject-with-notice: undeclared field is flagged \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500


def test_undeclared_capability_field_is_flagged(tmp_path):
    """An undeclared capability field raises a warning drift, never silent."""
    entry = _caps_manifest_entry()
    vault = tmp_path / "vault"
    ws_dir = vault / "workspaces" / "ws-a"
    ws_dir.mkdir(parents=True)
    (ws_dir / "capabilities.json").write_text(
        json.dumps(
            {
                "allow_network": False,
                "filesystem_write": True,
                "totally_unknown_capability": True,
            }
        ),
        encoding="utf-8",
    )

    checker = VaultDriftChecker(
        vault_root=vault,
        manifest_path=_write_manifest(tmp_path, entry),
    )
    report = checker.check()

    assert report["status"] == "warnings"
    rel = "workspaces/ws-a/capabilities.json"
    file_entry = report["files"][rel]
    assert file_entry["status"] == "warning"
    assert any(
        d["issue"] == "undeclared field" and d["field"] == "totally_unknown_capability"
        for d in file_entry["drifts"]
    ), file_entry["drifts"]
    assert any(
        "totally_unknown_capability" in warning and "not declared" in warning
        for warning in report["warnings"]
    ), report["warnings"]


# \u2500\u2500 3. Enforcement map is honest \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500


def test_filesystem_and_network_capabilities_are_enforced():
    """Capability flags the manifest labels ENFORCED really do cap grants."""
    from security.security_gate import _merge_with_capabilities

    session = SessionPermissions(
        filesystem="write", network="outbound", container=True, git="write",
    )
    workspace = WorkspaceCapabilities(
        filesystem_write=False,
        allow_network=False,
        allow_docker=False,
        git_available=False,
    )
    effective = _merge_with_capabilities(session, workspace)

    assert effective["filesystem"] == "read"
    assert effective["network"] is False
    assert effective["container"] is False
    assert effective["git"] is False


def test_permissive_capabilities_preserve_session_grants():
    """A permissive workspace must not weaken the session profile."""
    from security.security_gate import _merge_with_capabilities

    session = SessionPermissions(filesystem="write", network="outbound")
    workspace = WorkspaceCapabilities(filesystem_write=True, allow_network=True)
    effective = _merge_with_capabilities(session, workspace)

    assert effective["filesystem"] == "write"
    assert effective["network"] == "outbound"
