"""
test_security_gate_disk.py — Disk mode of ``get_effective_permissions``.

Dual-mode contract (security/security_gate.py):

* **Legacy mode** (unchanged, byte-for-byte): explicit in-memory
  ``session`` + ``workspace`` + ``workspace_permissions``.
* **Disk mode** (new, keyword-only ``session_id`` + ``workspace_id``):
  engages ONLY when both ids are supplied AND ``workspace_permissions`` is
  None.  The grant profile is read from the vault permission store
  (``<vault>/workspaces/<ws>/sessions/<sid>/permissions.json``) and the
  workspace ceiling from ``<vault>/workspaces/<ws>/config.json``.  Disk
  mode fails CLOSED: missing/corrupt store state yields the all-banned
  10-key result shape (including ``host_bash: "banned"``), never default
  grants.

The ``hermetic_vault`` fixture (tests/conftest.py) monkeypatches
``thoughtmachine.vault.vault_root()`` to a temp vault, which the gate's
lazy ``import thoughtmachine.vault`` picks up (same module object).
"""

import json

from thoughtmachine.permission_store import session_grants_path, write_session_permissions
from thoughtmachine.security import SessionPermissions
from thoughtmachine.workspace_capabilities import WorkspaceCapabilities
from security.security_gate import get_effective_permissions

# Fully-permissive workspace capabilities (all-True defaults) — used so the
# merge below never downgrades beyond what the disk ceiling/grants dictate.
_FULL_CAPS = WorkspaceCapabilities()

# The fail-closed 10-key shape produced by a deny-all session + deny-all
# ceiling merged with the fully-permissive workspace caps.
ALL_BANNED = {
    "filesystem": "banned",
    "network": "banned",
    "container": False,
    "git": "banned",
    "git_read": "banned",
    "git_write": "banned",
    "system": "banned",
    "mcp": "banned",
    "execution": "banned",
    "host_bash": "banned",
}


def _write_config(vault, ws_id, permissions):
    """Write a workspace config.json with an explicit permission ceiling."""
    ws_dir = vault / "workspaces" / ws_id
    ws_dir.mkdir(parents=True, exist_ok=True)
    (ws_dir / "config.json").write_text(
        json.dumps({"purpose": "coding", "permissions": permissions})
    )


def _write_corrupt_config(vault, ws_id):
    ws_dir = vault / "workspaces" / ws_id
    ws_dir.mkdir(parents=True, exist_ok=True)
    (ws_dir / "config.json").write_text("{not json")


def _write_corrupt_sidecar(vault, ws_id, session_id):
    path = session_grants_path(vault, ws_id, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")


def test_disk_mode_grant_wider_than_ceiling_is_capped(hermetic_vault):
    """(a) A disk grant wider than the workspace ceiling shrinks via the
    monotone-∧ merge: filesystem write grant + read ceiling -> read."""
    ws_id, sid = "ws-a", "sess-1"
    write_session_permissions(
        hermetic_vault, ws_id, sid,
        {"filesystem": "write", "git": "write"},
    )
    _write_config(hermetic_vault, ws_id, {"filesystem": "read", "git": "read"})

    eff = get_effective_permissions(
        SessionPermissions(),  # ignored in disk mode
        _FULL_CAPS,
        session_id=sid,
        workspace_id=ws_id,
    )
    assert eff["filesystem"] == "read"  # capped by ceiling
    assert eff["git"] == "read"
    assert eff["git_read"] == "read"  # derived from capped git
    assert eff["git_write"] == "banned"
    assert eff["system"] == "read"  # grant-side default passes through
    assert eff["mcp"] == "banned"
    assert eff["execution"] == "banned"
    assert eff["container"] is False


def test_disk_mode_compatible_grants_pass_ceiling(hermetic_vault):
    """(b) Compatible disk state passes through: write grant + write ceiling
    -> write; explicit container True survives a write-level docker ceiling."""
    ws_id, sid = "ws-b", "sess-1"
    write_session_permissions(
        hermetic_vault, ws_id, sid,
        {
            "filesystem": "write",
            "git": "write",
            "network": "write",
            "container": True,
        },
    )
    _write_config(
        hermetic_vault, ws_id,
        {"filesystem": "write", "docker": "write", "git": "write", "network": "write"},
    )

    eff = get_effective_permissions(
        SessionPermissions(),
        _FULL_CAPS,
        session_id=sid,
        workspace_id=ws_id,
    )
    assert eff["filesystem"] == "write"
    assert eff["git"] == "write"
    assert eff["git_read"] == "write"
    assert eff["git_write"] == "write"
    assert eff["network"] == "write"
    assert eff["container"] is True


def test_disk_mode_missing_session_record_fails_closed(hermetic_vault):
    """(c) Missing session record (no sidecar, no legacy fallback) -> the
    all-banned 10-key shape, no exception — even with a permissive ceiling."""
    ws_id = "ws-c"
    _write_config(
        hermetic_vault, ws_id,
        {"filesystem": "write", "git": "write", "network": "write"},
    )
    eff = get_effective_permissions(
        SessionPermissions(),
        _FULL_CAPS,
        session_id="no-such-session",
        workspace_id=ws_id,
    )
    assert eff == ALL_BANNED


def test_disk_mode_corrupt_sidecar_fails_closed(hermetic_vault):
    """(d) Corrupt permission sidecar -> all-banned shape, no exception."""
    ws_id, sid = "ws-d", "sess-1"
    _write_corrupt_sidecar(hermetic_vault, ws_id, sid)
    _write_config(
        hermetic_vault, ws_id,
        {"filesystem": "write", "git": "write"},
    )
    eff = get_effective_permissions(
        SessionPermissions(),
        _FULL_CAPS,
        session_id=sid,
        workspace_id=ws_id,
    )
    assert eff == ALL_BANNED


def test_disk_mode_missing_or_corrupt_config_fails_closed(hermetic_vault):
    """(e) Missing or corrupt workspace config.json (no ceiling readable) ->
    all-banned shape, no exception."""
    ws_id, sid = "ws-e", "sess-1"
    # Grant is perfectly readable — the unreadable ceiling still fails closed.
    write_session_permissions(
        hermetic_vault, ws_id, sid,
        {"filesystem": "write", "git": "write"},
    )
    eff = get_effective_permissions(
        SessionPermissions(),
        _FULL_CAPS,
        session_id=sid,
        workspace_id=ws_id,
    )
    assert eff == ALL_BANNED

    # Corrupt config variant.
    ws2, sid2 = "ws-e2", "sess-1"
    write_session_permissions(
        hermetic_vault, ws2, sid2,
        {"filesystem": "write", "git": "write"},
    )
    _write_corrupt_config(hermetic_vault, ws2)
    eff2 = get_effective_permissions(
        SessionPermissions(),
        _FULL_CAPS,
        session_id=sid2,
        workspace_id=ws2,
    )
    assert eff2 == ALL_BANNED


def test_disk_mode_equals_legacy_explicit_args(hermetic_vault):
    """(f) For identical content, the legacy explicit-args call and the disk
    mode call produce the same result (disk grants == session arg, disk
    ceiling == workspace_permissions arg)."""
    grants = {
        "filesystem": "write",
        "git": "write",
        "network": "write",
        "container": True,
    }
    ceiling = {"filesystem": "write", "git": "read", "network": "write"}

    ws_id, sid = "ws-f", "sess-1"
    write_session_permissions(hermetic_vault, ws_id, sid, dict(grants))
    _write_config(hermetic_vault, ws_id, dict(ceiling))

    legacy = get_effective_permissions(
        SessionPermissions(**grants),
        _FULL_CAPS,
        dict(ceiling),
    )
    disk = get_effective_permissions(
        SessionPermissions(),  # ignored in disk mode
        _FULL_CAPS,
        session_id=sid,
        workspace_id=ws_id,
    )
    assert disk == legacy
    # Sanity on the shared shape: git capped to read, write grain denied.
    assert legacy["git"] == "read"
    assert legacy["git_write"] == "banned"
    assert legacy["filesystem"] == "write"


def test_explicit_workspace_permissions_win_over_disk_ids(hermetic_vault):
    """(g) Precedence rule: an explicit in-memory workspace_permissions dict
    always wins — disk state (grants + ceiling) is ignored entirely when it
    is supplied alongside the ids."""
    ws_id, sid = "ws-g", "sess-1"
    # Disk state that WOULD allow write if it were consulted.
    write_session_permissions(
        hermetic_vault, ws_id, sid,
        {"filesystem": "write", "git": "write"},
    )
    _write_config(
        hermetic_vault, ws_id,
        {"filesystem": "write", "git": "write"},
    )

    eff = get_effective_permissions(
        SessionPermissions(filesystem="write", git="write"),
        _FULL_CAPS,
        {"filesystem": "read", "git": "read"},  # explicit ceiling
        session_id=sid,
        workspace_id=ws_id,
    )
    assert eff["filesystem"] == "read"  # explicit ceiling applied
    assert eff["git"] == "read"
    assert eff["git_write"] == "banned"

    # Control: same ids, workspace_permissions=None -> disk mode says write.
    disk = get_effective_permissions(
        SessionPermissions(),
        _FULL_CAPS,
        session_id=sid,
        workspace_id=ws_id,
    )
    assert disk["filesystem"] == "write"
