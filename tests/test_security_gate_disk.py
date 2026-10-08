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
  six-key result shape (``container: False``, every string category
  ``banned``), never default grants.

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

# The fail-closed six-key shape produced by a deny-all session + deny-all
# ceiling merged with the fully-permissive workspace caps.
ALL_BANNED = {
    "container": False,
    "network": "banned",
    "filesystem": "banned",
    "git": "banned",
    "mcp": "banned",
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
    assert eff["container"] is False  # session default (no grant)
    assert eff["network"] == "banned"  # session default (no grant)
    assert eff["mcp"] == "banned"
    assert eff["host_bash"] == "banned"


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
    assert eff["network"] == "write"
    assert eff["container"] is True
    assert eff["mcp"] == "banned"
    assert eff["host_bash"] == "banned"


def test_disk_mode_missing_session_record_fails_closed(hermetic_vault):
    """(c) Missing session record (no sidecar, no legacy fallback) -> the
    all-banned six-key shape, no exception — even with a permissive ceiling."""
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
    """(d) Corrupt permission sidecar -> all-banned six-key shape, no exception."""
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
    all-banned six-key shape, no exception."""
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
    # Sanity on the shared shape: git capped to read by the ceiling.
    assert legacy["git"] == "read"
    assert legacy["filesystem"] == "write"
    assert legacy["network"] == "write"
    assert legacy["container"] is True


def test_disk_ids_win_over_explicit_workspace_permissions(hermetic_vault):
    """(g) Precedence rule (Edge 2 Change 4): when BOTH ids are supplied the
    disk read is authoritative — an explicit in-memory ``workspace_permissions``
    dict no longer wins; the stored grant profile + ceiling are used instead."""
    ws_id, sid = "ws-g", "sess-1"
    # Disk state that allows write (grant + ceiling both write).
    write_session_permissions(
        hermetic_vault, ws_id, sid,
        {"filesystem": "write", "git": "write"},
    )
    _write_config(
        hermetic_vault, ws_id,
        {"filesystem": "write", "git": "write"},
    )

    # A DIVERGENT explicit ceiling (read) supplied ALONGSIDE both ids: before
    # Edge 2 Change 4 this won; now the disk read governs -> write.
    eff = get_effective_permissions(
        SessionPermissions(filesystem="write", git="write"),
        _FULL_CAPS,
        {"filesystem": "read", "git": "read"},  # explicit ceiling — ignored
        session_id=sid,
        workspace_id=ws_id,
    )
    assert eff["filesystem"] == "write"  # disk wins, NOT the explicit "read"
    assert eff["git"] == "write"
    assert eff["container"] is False

    # Control: with NO ids the in-memory path still governs, so the explicit
    # read ceiling applies as before.
    legacy = get_effective_permissions(
        SessionPermissions(filesystem="write", git="write"),
        _FULL_CAPS,
        {"filesystem": "read", "git": "read"},
    )
    assert legacy["filesystem"] == "read"
    assert legacy["git"] == "read"


def test_disk_mode_schema_invalid_ceiling_fails_closed(hermetic_vault, monkeypatch):
    """A schema-invalid capped profile must fail CLOSED, not keep the grant.

    R2/S4: when applying the workspace ceiling yields a dict that is not a
    schema-valid ``SessionPermissions`` profile, the session collapses to the
    deny-all ``_DISK_FAIL_CLOSED_SESSION``.  The resulting effective profile
    is all-banned AND carries NO ceiling annotation (the annotation is
    suppressed for the fail-closed session).
    """
    ws_id, sid = "ws-s4", "sess-1"
    write_session_permissions(
        hermetic_vault, ws_id, sid,
        {"filesystem": "write", "git": "write"},
    )
    _write_config(hermetic_vault, ws_id, {"filesystem": "write", "git": "write"})

    # Force the ceiling pass to produce a schema-INVALID profile.
    monkeypatch.setattr(
        "security.security_gate.apply_workspace_ceiling",
        lambda workspace_permissions, raw: {
            "filesystem": "not-a-level",
            "git": 123,
            "network": object(),
            "mcp": ["nope"],
            "container": "maybe",
            "host_bash": None,
        },
    )

    eff = get_effective_permissions(
        SessionPermissions(),  # ignored in disk mode
        _FULL_CAPS,
        session_id=sid,
        workspace_id=ws_id,
    )

    assert eff == ALL_BANNED  # (a) deny-all session
    # (b) NO ceiling annotation attached: the fail-closed deny-all was caused
    # by an unusable ceiling, not a legitimate reduction, so the result must
    # NOT carry the _ceiling_annotations provenance attribute.
    assert getattr(eff, "_ceiling_annotations", None) in (None, {})


# ── B5: the fail-closed CEILING must deny ``mcp`` too ────────────────────────
def test_disk_fail_closed_ceiling_bans_mcp():
    """The paired fail-closed sentinels must cover the SAME six resources.

    ``_DISK_FAIL_CLOSED_CEILING`` must ban ``mcp`` exactly like
    ``_DISK_FAIL_CLOSED_SESSION`` does, so a fail-closed ceiling is genuinely
    deny-all: applied over a permissive ``mcp`` grant it must cap ``mcp`` to
    ``banned`` (an unreadable ceiling denies ``mcp`` rather than leaving it
    uncapped).  RED before ``mcp`` is added to the ceiling.
    """
    from security import security_gate as sg
    from security.security_gate import apply_workspace_ceiling

    # (1) Structural: the ceiling covers the same resources as the session.
    session_keys = set(sg._DISK_FAIL_CLOSED_SESSION.model_dump())
    assert set(sg._DISK_FAIL_CLOSED_CEILING) == session_keys
    assert sg._DISK_FAIL_CLOSED_CEILING["mcp"] == "banned"

    # (2) Behavioural: used as a ceiling over a permissive ``mcp`` grant, the
    # fail-closed ceiling caps ``mcp`` to ``banned`` (an absent key = fail-open,
    # the grant would survive).
    capped = apply_workspace_ceiling(sg._DISK_FAIL_CLOSED_CEILING, {"mcp": "full"})
    assert capped["mcp"] == "banned"


# ── B4: the module fail-policy has two directions ────────────────────────────
def test_fail_policy_unknown_io_is_fail_closed(hermetic_vault):
    """The *I/O* direction of the module fail-policy.

    Contrast the *value* direction, which is fail-OPEN.

    An unreadable/missing on-disk configuration (here: NO ``config.json``, so
    no ceiling is readable) must resolve to the most-restrictive sentinel --
    fail-CLOSED -- even though the session GRANT sidecar is perfectly
    readable.  The unreadable I/O is what forces the deny-all shape; a mere
    unknown ceiling *value* never would.
    """
    ws_id, sid = "ws-b4io", "sess-1"
    # A readable GRANT sidecar only -- the ceiling/config I/O is what fails.
    write_session_permissions(
        hermetic_vault, ws_id, sid,
        {"filesystem": "write", "git": "write", "network": "write"},
    )
    eff = get_effective_permissions(
        SessionPermissions(),  # ignored in disk mode
        _FULL_CAPS,
        session_id=sid,
        workspace_id=ws_id,
    )
    # Most-restrictive sentinel: the deny-all six-key shape, never a grant,
    # despite the permissive grant sidecar above.
    assert eff == ALL_BANNED
    # Distinct from the existing pins: the fail-closed result NAMES its I/O
    # cause, proving the deny-all came from the unreadable I/O path (not from a
    # legitimate ceiling reduction) -- this is the fail-CLOSED half of the
    # policy that fail-OPENS for an unknown ceiling *value*.
    assert getattr(eff, "_fail_closed_reason", "") != ""
