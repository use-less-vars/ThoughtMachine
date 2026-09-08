"""
RED regression test: the tool-execution gate trusts an in-memory mirror of
session_permissions while the on-disk session record is the authoritative
state (permissions-staleness scenario, Phase-1).

Scenario exercised here ("disk staleness"):
  1. A coding session is persisted whose on-disk session record grants
     ``filesystem=write`` (metadata.session_config.session_permissions).
  2. The ToolExecutor is configured with the same ``write`` mirror, so a
     FileEditor write probe is ALLOWED (sanity leg).
  3. The session is then demoted on disk to ``filesystem=read`` — a direct
     edit of the authoritative record (both the legacy JSON field and the
     permissions sidecar a phase-2 gate would read).  Crucially, the
     in-memory config mirror is NOT updated: no bridge push, no
     request_config_update.  That stale mirror is the bug.
  4. Because the gate currently reads the STALE in-memory mirror, a second
     FileEditor write probe after the disk demotion is STILL ALLOWED on
     dev @0761d02 → assertion fails → RED.

Expected after the gate refactor reads the disk source of truth:
  the second probe is DENIED with a result containing 'Permission denied'.

The dual write below mirrors both storage shapes (the primary session JSON
``metadata.session_config.session_permissions`` and a
``<sid>/permissions.json`` sidecar next to the session file) so whichever
layout the refactored gate reads, the disk says ``read`` and the test goes
green only when the gate stops trusting the stale mirror.
"""

import json
import sys
from pathlib import Path

# Add project root so that imports work (same pattern as sibling tests).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from session.models import Session
from session.store import FileSystemSessionStore
from agent.config.models import AgentConfig
from agent.core.tool_executor import ToolExecutor
from agent.core.state import AgentState
from tools.file_editor import FileEditor
from thoughtmachine.security import SessionPermissions


def test_disk_demotion_to_read_still_allows_write_via_stale_mirror(hermetic_vault, tmp_path):
    """A write stays allowed after the on-disk perms say read (stale mirror)."""
    vault_path = hermetic_vault  # == tmp_path/.thoughtmachine (fixture-built vault)

    # ------------------------------------------------------------------
    # 1) Persist a session whose DISK record grants filesystem=write.
    # ------------------------------------------------------------------
    store = FileSystemSessionStore(
        sessions_dir=str(vault_path / "sessions"),
        state_dir=str(vault_path / "state"),
    )
    ws_id = "ws-a"
    ws_dir = vault_path / "workspaces" / ws_id
    ws_dir.mkdir(parents=True, exist_ok=True)
    # Scenario ceiling: coding workspace with a write ceiling.
    (ws_dir / "config.json").write_text(
        json.dumps({"purpose": "coding", "permissions": {"filesystem": "write", "git": "write"}})
    )

    sess = Session(
        workspace_id=ws_id,
        metadata={
            "name": "staleness-probe",
            "session_config": {
                "name": "staleness-probe",
                "session_permissions": {"filesystem": "write", "git": "write"},
            },
        },
    )
    store.save_session(sess, workspace_id=ws_id)
    session_file = store._find_session_path(sess.session_id)
    assert session_file is not None, "session file not found on disk after save"
    raw0 = json.loads(session_file.read_text())
    perms0 = raw0["metadata"]["session_config"]["session_permissions"]
    assert perms0["filesystem"] == "write", f"expected write on disk, got {perms0}"

    # ------------------------------------------------------------------
    # 2) Executor with the same WRITE mirror (no workspace_path so no
    #    workspace-capability fail-closed interferes; caps stay empty).
    # ------------------------------------------------------------------
    config = AgentConfig(
        session_permissions=SessionPermissions(filesystem="write", git="write")
    )
    state = AgentState(config=config)
    executor = ToolExecutor(tool_classes=[FileEditor], config=config, state=state)

    # Sanity leg: with the mirror at write the probe is allowed AND the file
    # is really written (proves the executor would deny if perms said read).
    probe_before = tmp_path / "probe-before.txt"
    r1 = executor._execute_single_tool(
        FileEditor,
        {"operation": "write", "filename": str(probe_before), "content": "sanity"},
        "FileEditor",
        0,
        lambda: False,
        lambda: "",
        lambda: 0,
    )
    assert "Permission denied" not in r1.get("result", ""), f"sanity leg denied: {r1}"
    assert probe_before.exists(), "sanity write did not land on disk"

    # ------------------------------------------------------------------
    # 3) DEMOTE the session on disk to filesystem=read.  Direct-to-disk
    #    only — deliberately NOT via the bridge (no apply_config push, no
    #    request_config_update), so the executor's in-memory mirror stays
    #    stale at 'write'.  This is the staleness the bug leaves behind.
    # ------------------------------------------------------------------
    raw = json.loads(session_file.read_text())
    raw["metadata"]["session_config"]["session_permissions"]["filesystem"] = "read"
    session_file.write_text(json.dumps(raw, indent=2))

    # Sidecar mirror of the same demotion (the layout a refactored
    # permissions gate would read): <sid>/permissions.json next to the
    # session file.
    sidecar_dir = session_file.parent / sess.session_id
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    (sidecar_dir / "permissions.json").write_text(
        json.dumps({"filesystem": "read", "git": "write"})
    )

    # Re-read: both disk representations now say read (writes landed).
    raw_after = json.loads(session_file.read_text())
    perms_after = raw_after["metadata"]["session_config"]["session_permissions"]
    assert perms_after["filesystem"] == "read", f"disk demotion failed: {perms_after}"
    sidecar_after = json.loads((sidecar_dir / "permissions.json").read_text())
    assert sidecar_after["filesystem"] == "read", f"sidecar demotion failed: {sidecar_after}"

    # ------------------------------------------------------------------
    # 4) Second write probe WITH the session id, mirror untouched (write).
    #    Disk source of truth says read → a gate honoring disk state must
    #    DENY.  Dev's gate reads the stale in-memory mirror → still ALLOWS.
    # ------------------------------------------------------------------
    probe_after = tmp_path / "probe-after.txt"
    r2 = executor._execute_single_tool(
        FileEditor,
        {"operation": "write", "filename": str(probe_after), "content": "x"},
        "FileEditor",
        0,
        lambda: False,
        lambda: "",
        lambda: 0,
        session_id=sess.session_id,
    )
    assert "Permission denied" in r2.get("result", ""), (
        "STALE MIRROR / gate still ALLOW: disk says filesystem=read but the "
        f"write probe succeeded. Dev gate trusts the in-memory mirror: {r2}"
    )
