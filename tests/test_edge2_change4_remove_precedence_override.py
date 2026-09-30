"""
test_edge2_change4_remove_precedence_override.py — Edge 2, Change 4.

Change 4 removes the in-memory precedence override in
``security.security_gate.get_effective_permissions``: the disk-dispatch
condition used to be ``workspace_permissions is None and session_id is not
None and workspace_id is not None``; the leading ``workspace_permissions is
None and`` conjunct is deleted, so a disk read is now authoritative whenever
BOTH ids are supplied — even when a divergent in-memory
``workspace_permissions`` dict is passed alongside them.

Contract proven here (all by OUTPUT of ``get_effective_permissions``):

1. ids + non-None ``workspace_permissions`` (divergent) -> the DISK result
   wins (the removed override no longer applies).
2. no ids + explicit ``workspace_permissions`` -> the in-memory value is
   honoured exactly as before.
3. ids + ``workspace_permissions=None`` -> the disk result (pre-existing disk
   behaviour, unchanged).
4. The module-level fail-closed sentinels still exist on
   ``security.security_gate`` AND ``web_ui.backend.config_manager`` still
   imports them (the shared fail-closed shape must not drift).
5. RED-by-output direction proof: with both ids supplied the OUTPUT is
   invariant to the in-memory ``workspace_permissions`` argument — the exact
   property the removed override violated.

The ``hermetic_vault`` fixture (tests/conftest.py) monkeypatches
``thoughtmachine.vault.vault_root()`` to a temp vault, which the gate's lazy
``import thoughtmachine.vault`` picks up (same module object).
"""

import json
from pathlib import Path

from thoughtmachine.permission_store import write_session_permissions
from thoughtmachine.security import SessionPermissions
from thoughtmachine.workspace_capabilities import WorkspaceCapabilities
from security.security_gate import get_effective_permissions

# Fully-permissive workspace capabilities (all-True defaults) — used so the
# merge never downgrades beyond what the disk ceiling/grants dictate.
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

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _write_config(vault, ws_id, permissions):
    """Write a workspace config.json with an explicit permission ceiling."""
    ws_dir = vault / "workspaces" / ws_id
    ws_dir.mkdir(parents=True, exist_ok=True)
    (ws_dir / "config.json").write_text(
        json.dumps({"purpose": "coding", "permissions": permissions})
    )


def test_disk_ids_win_over_populated_workspace_permissions(hermetic_vault):
    """(1) With BOTH ids supplied the disk read is authoritative even when a
    non-None ``workspace_permissions`` is passed: the stored grant + ceiling
    (write) win, not the divergent in-memory dict (read)."""
    ws_id, sid = "ws-e2c4-a", "sess-1"
    write_session_permissions(
        hermetic_vault, ws_id, sid,
        {"filesystem": "write", "git": "write"},
    )
    _write_config(
        hermetic_vault, ws_id,
        {"filesystem": "write", "git": "write"},
    )

    eff = get_effective_permissions(
        SessionPermissions(),  # ignored in disk mode
        _FULL_CAPS,
        {"filesystem": "read", "git": "read"},  # divergent — must be ignored
        session_id=sid,
        workspace_id=ws_id,
    )
    assert eff["filesystem"] == "write"  # disk wins
    assert eff["git"] == "write"
    assert eff["container"] is False
    assert eff["network"] == "banned"
    assert eff["mcp"] == "banned"
    assert eff["host_bash"] == "banned"


def test_no_ids_honours_explicit_workspace_permissions(hermetic_vault):
    """(2) With NO ids the in-memory path is UNCHANGED: the supplied ceiling
    governs the supplied session profile."""
    eff = get_effective_permissions(
        SessionPermissions(filesystem="write", git="write"),
        _FULL_CAPS,
        {"filesystem": "read", "git": "read"},
    )
    assert eff["filesystem"] == "read"
    assert eff["git"] == "read"
    assert eff["container"] is False


def test_ids_with_none_workspace_permissions_use_disk(hermetic_vault):
    """(3) The pre-existing disk-mode input (both ids, workspace_permissions
    None) is unchanged: the disk grant + ceiling are used."""
    ws_id, sid = "ws-e2c4-c", "sess-1"
    write_session_permissions(
        hermetic_vault, ws_id, sid,
        {"filesystem": "write", "git": "write"},
    )
    _write_config(
        hermetic_vault, ws_id,
        {"filesystem": "write", "git": "write"},
    )
    eff = get_effective_permissions(
        SessionPermissions(),
        _FULL_CAPS,
        None,
        session_id=sid,
        workspace_id=ws_id,
    )
    assert eff["filesystem"] == "write"
    assert eff["git"] == "write"


def test_fail_closed_sentinels_intact_and_still_imported():
    """(4) The module-level fail-closed sentinels survive the change unchanged,
    and ``web_ui.backend.config_manager`` still imports both of them from the
    gate (the shared fail-closed shape must not drift)."""
    import security.security_gate as gate

    assert isinstance(gate._DISK_FAIL_CLOSED_SESSION, SessionPermissions)
    assert gate._DISK_FAIL_CLOSED_SESSION.model_dump() == ALL_BANNED
    assert gate._DISK_FAIL_CLOSED_CEILING == {
        "filesystem": "banned",
        "container": False,
        "host_bash": "banned",
        "git": "banned",
        "network": "banned",
    }

    # config_manager must still import both sentinels from security.security_gate
    # (read the source directly — no heavy web-app import needed).
    cm_src = (
        _REPO_ROOT / "web_ui" / "backend" / "config_manager.py"
    ).read_text(encoding="utf-8")
    assert "from security.security_gate import" in cm_src
    assert "_DISK_FAIL_CLOSED_CEILING" in cm_src
    assert "_DISK_FAIL_CLOSED_SESSION" in cm_src


def test_output_invariant_to_workspace_permissions_when_ids_supplied(hermetic_vault):
    """(5) RED-by-output direction proof: with both ids supplied the OUTPUT of
    ``get_effective_permissions`` is INVARIANT to the in-memory
    ``workspace_permissions`` argument.  Passing a divergent ceiling (read — the
    opposite of the disk write grant/ceiling) yields the SAME disk result as
    passing None.  Under the removed override the two would have differed
    (read vs write), which is exactly the regression Change 4 fixes."""
    ws_id, sid = "ws-e2c4-e", "sess-1"
    write_session_permissions(
        hermetic_vault, ws_id, sid,
        {"filesystem": "write", "git": "write"},
    )
    _write_config(
        hermetic_vault, ws_id,
        {"filesystem": "write", "git": "write"},
    )

    with_none = get_effective_permissions(
        SessionPermissions(), _FULL_CAPS, None,
        session_id=sid, workspace_id=ws_id,
    )
    with_override = get_effective_permissions(
        SessionPermissions(), _FULL_CAPS,
        {"filesystem": "read", "git": "read"},
        session_id=sid, workspace_id=ws_id,
    )

    assert with_none == with_override  # output no longer depends on the arg
    assert with_override["filesystem"] == "write"  # disk wins, not "read"
    assert with_none["filesystem"] == "write"
