"""Edge-2 / Change-2: session permission helper now resolves in DISK mode.

The REST helper ``web_ui.backend.session_routes._compute_effective_session_permissions``
used to compute a *hybrid* effective profile: the session grants were taken from
the in-memory ``raw_perms`` argument while only the workspace ceiling was read
from disk, and an unreadable ceiling (missing / corrupt ``config.json``) was
swallowed as "no ceiling" (``{}``) -- a FAIL-OPEN grant.

After the migration the helper delegates to the security gate's **disk mode**
(``get_effective_permissions(SessionPermissions(), caps, session_id=..., workspace_id=...)``),
where BOTH the session grant profile and the workspace ceiling come from the
vault permission store and a store read failure fails CLOSED.

Differential (confirmed against the pre-migration helper):

* readable ``config.json`` (with or without a ``permissions`` key) -- hybrid and
  disk agree exactly (same grants, same ceiling);
* a legacy session record with NO sidecar is no longer a grant source: the
  sidecar-only store read fails, so the helper fails CLOSED to the all-banned
  profile (the retired metadata fallback is never consulted);
* the other divergence: a MISSING or CORRUPT ``config.json`` -- hybrid returned the
  stored grants (filesystem/git ``write`` pass through), disk fails CLOSED to the
  all-banned profile.

Those two unreadable-config cases are the RED tests below (they fail against the
pre-migration hybrid helper and pass once the helper uses disk mode).

All tests build a hermetic vault under ``tmp_path`` and point
``THOUGHTMACHINE_VAULT_ROOT`` at it, so the real vault is never touched.
"""

import json
import sys
from pathlib import Path

# Add project root so that imports work (same pattern as sibling tests).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from web_ui.backend import session_routes  # noqa: E402
from web_ui.backend.session_routes import (  # noqa: E402
    _compute_effective_session_permissions as _effective,
)

WS_ID = "ws-a"
SESSION_ID = "sess-1"

# Deny-all / fail-closed shape produced by the gate's disk-mode sentinel.
FAIL_CLOSED = {
    "filesystem": "banned",
    "network": "banned",
    "container": False,
    "git": "banned",
    "mcp": "banned",
    "host_bash": "banned",
}


# ---------------------------------------------------------------------------
# Helpers: build a hermetic vault layout under tmp_path
# ---------------------------------------------------------------------------


def _vault(tmp_path, monkeypatch) -> Path:
    vault = tmp_path / "vault"
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(vault))
    return vault


def _write_config(vault, permissions=None, *, corrupt=False) -> Path:
    cfg = Path(vault) / "workspaces" / WS_ID / "config.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    if corrupt:
        cfg.write_text("{ not json !!!", encoding="utf-8")
        return cfg
    payload = {"purpose": "coding"}
    if permissions is not None:
        payload["permissions"] = permissions
    cfg.write_text(json.dumps(payload), encoding="utf-8")
    return cfg


def _write_grants_sidecar(vault, grants) -> Path:
    sidecar = (
        Path(vault) / "workspaces" / WS_ID / "sessions" / SESSION_ID / "permissions.json"
    )
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(json.dumps(grants), encoding="utf-8")
    return sidecar


# ---------------------------------------------------------------------------
# Agreement with disk mode when the store is readable
# ---------------------------------------------------------------------------


def test_readable_ceiling_caps_session_grants(tmp_path, monkeypatch):
    """A readable ceiling caps grants that sit above it (filesystem/git write ->
    read), and the effect matches the gate's own disk-mode resolution."""
    vault = _vault(tmp_path, monkeypatch)
    _write_config(vault, permissions={"filesystem": "read", "git": "read"})
    _write_grants_sidecar(vault, {"filesystem": "write", "git": "write"})

    effective = dict(_effective(WS_ID, SESSION_ID))

    assert effective == {
        "filesystem": "read",
        "network": "banned",
        "container": False,
        "git": "read",
        "mcp": "banned",
        "host_bash": "banned",
    }

    # True differential: identical to the gate's disk-mode call.
    from security.security_gate import get_effective_permissions
    from thoughtmachine.security import SessionPermissions
    from thoughtmachine.workspace_capabilities import (
        WorkspaceCapabilities,
        load_workspace_capabilities,
    )

    caps = load_workspace_capabilities(WS_ID) or WorkspaceCapabilities.default()
    expected = get_effective_permissions(
        SessionPermissions(), caps, session_id=SESSION_ID, workspace_id=WS_ID
    )
    assert effective == dict(expected)


def test_config_without_permissions_key_passes_grants(tmp_path, monkeypatch):
    """A ``config.json`` with no ``permissions`` key is a legitimate empty
    ceiling (not an error): grants pass through unchanged."""
    vault = _vault(tmp_path, monkeypatch)
    _write_config(vault)  # no "permissions" key
    _write_grants_sidecar(vault, {"filesystem": "write", "git": "write"})

    assert dict(_effective(WS_ID, SESSION_ID)) == {
        "filesystem": "write",
        "network": "banned",
        "container": False,
        "git": "write",
        "mcp": "banned",
        "host_bash": "banned",
    }


def test_legacy_record_without_sidecar_fails_closed(tmp_path, monkeypatch):
    """A legacy session record that carries grants but has NO sidecar is no
    longer a grant source for reads: the sidecar-only store read fails, so the
    helper fails CLOSED to the all-banned profile -- even though a readable
    ceiling is present."""
    vault = _vault(tmp_path, monkeypatch)
    _write_config(vault, permissions={"filesystem": "read", "git": "read"})
    sessions = Path(vault) / "workspaces" / WS_ID / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / "my-session_ab12cd.json").write_text(
        json.dumps(
            {
                "session_id": SESSION_ID,
                "name": "my-session",
                "metadata": {
                    "session_config": {
                        "session_permissions": {
                            "filesystem": "write",
                            "git": "write",
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    assert dict(_effective(WS_ID, SESSION_ID)) == FAIL_CLOSED


# ---------------------------------------------------------------------------
# RED: an unreadable workspace config must fail CLOSED (not fall open)
# ---------------------------------------------------------------------------


def test_corrupt_workspace_config_fails_closed(tmp_path, monkeypatch):
    """A corrupt ``config.json`` makes the store read fail -> deny-all profile.

    Pre-migration the hybrid helper swallowed the ceiling error as ``{}`` and
    returned the stored ``filesystem``/``git`` ``write`` grants (FAIL-OPEN);
    post-migration the disk-mode store read fails CLOSED.
    """
    vault = _vault(tmp_path, monkeypatch)
    _write_config(vault, corrupt=True)
    _write_grants_sidecar(vault, {"filesystem": "write", "git": "write"})

    effective = dict(_effective(WS_ID, SESSION_ID))

    assert effective == FAIL_CLOSED


def test_missing_workspace_config_fails_closed(tmp_path, monkeypatch):
    """A missing ``config.json`` (no workspace dir config at all) likewise fails
    CLOSED rather than passing the stored grants through."""
    vault = _vault(tmp_path, monkeypatch)
    # No config.json written at all -- only the readable grants sidecar.
    _write_grants_sidecar(vault, {"filesystem": "write", "git": "write"})

    assert dict(_effective(WS_ID, SESSION_ID)) == FAIL_CLOSED


def test_fail_closed_result_carries_no_ceiling_provenance(tmp_path, monkeypatch):
    """The fail-closed sentinel is not attributed to a legitimate ceiling
    reduction, so the provenance annotation stays empty."""
    vault = _vault(tmp_path, monkeypatch)
    _write_config(vault, corrupt=True)
    _write_grants_sidecar(vault, {"filesystem": "write", "git": "write"})

    effective = _effective(WS_ID, SESSION_ID)
    provenance = session_routes._ceiling_provenance(effective)

    assert provenance["workspace_ceiling"] == {}
    assert provenance["contradictions"] == []
