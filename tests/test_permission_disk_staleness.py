"""
Disk-staleness regression test: the tool-execution gate must treat the on-disk
session permissions as the authoritative state, NOT an in-memory mirror of
``session_permissions`` (permissions-staleness scenario, Phase-1).

Scenario exercised here ("disk staleness"):
  1. A coding session is persisted whose on-disk session record grants
     ``filesystem=write`` (metadata.session_config.session_permissions), and
     whose canonical permission sidecar (the disk-mode gate's grant source,
     written via ``thoughtmachine.permission_store.write_session_permissions``)
     also grants ``filesystem=write``.
  2. The ToolExecutor is configured with the same ``write`` mirror, so a
     FileEditor write probe is ALLOWED (sanity leg).  Permissive
     ``capabilities.json`` is seeded for the workspace so the capability merge
     cannot downgrade write -> read.
  3. The session is then demoted on disk to ``filesystem=read``: a direct edit
     of the authoritative record AND a rewrite of the canonical sidecar.
     Crucially, the in-memory config mirror is NOT updated: no bridge push, no
     request_config_update.
  4. Because the gate reads the on-disk source of truth (not the stale mirror),
     a second FileEditor write probe after the disk demotion is DENIED with a
     result containing 'Permission denied'.
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
from thoughtmachine.permission_store import write_session_permissions


def test_disk_demotion_to_read_denies_write_despite_stale_mirror(hermetic_vault, tmp_path):
    """A write is DENIED after the on-disk perms are demoted to read, even
    though the in-memory mirror stays stale at write (disk is authoritative)."""
    vault_path = hermetic_vault  # == tmp_path/.thoughtmachine (fixture-built vault)
    store = FileSystemSessionStore(
        sessions_dir=str(vault_path / "sessions"),
        state_dir=str(vault_path / "state"),
    )
    ws_id = "ws-a"
    ws_dir = vault_path / "workspaces" / ws_id
    ws_dir.mkdir(parents=True, exist_ok=True)
    # Permissive capabilities so filesystem_write / git_available stay true
    # (a MISSING capabilities file fails the capability merge closed).
    (ws_dir / "capabilities.json").write_text(
        json.dumps({"filesystem_write": True, "git_available": True})
    )
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
    # Canonical sidecar (the disk-mode gate's grant source) starts at WRITE.
    sidecar_path = write_session_permissions(
        vault_path, ws_id, sess.session_id, {"filesystem": "write", "git": "write"}
    )
    config = AgentConfig(
        session_permissions=SessionPermissions(filesystem="write", git="write")
    )
    state = AgentState(config=config)
    executor = ToolExecutor(tool_classes=[FileEditor], config=config, state=state)
    # Sanity leg: disk says write -> the probe is allowed AND really written.
    probe_before = tmp_path / "probe-before.txt"
    r1 = executor._execute_single_tool(
        FileEditor,
        {"operation": "write", "filename": str(probe_before), "content": "sanity"},
        "FileEditor",
        0,
        lambda: False,
        lambda: "",
        lambda: 0,
        session_id=sess.session_id,
        workspace_id=ws_id,
    )
    assert "Permission denied" not in r1.get("result", ""), f"sanity leg denied: {r1}"
    assert probe_before.exists(), "sanity write did not land on disk"
    # Demote the session on disk to filesystem=read (record + canonical sidecar).
    raw = json.loads(session_file.read_text())
    raw["metadata"]["session_config"]["session_permissions"]["filesystem"] = "read"
    session_file.write_text(json.dumps(raw, indent=2))
    write_session_permissions(
        vault_path, ws_id, sess.session_id, {"filesystem": "read", "git": "write"}
    )
    raw_after = json.loads(session_file.read_text())
    perms_after = raw_after["metadata"]["session_config"]["session_permissions"]
    assert perms_after["filesystem"] == "read", f"disk demotion failed: {perms_after}"
    sidecar_after = json.loads(sidecar_path.read_text())
    assert sidecar_after["filesystem"] == "read", f"sidecar demotion failed: {sidecar_after}"
    # Second probe: disk says read, mirror still write -> gate must DENY.
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
        workspace_id=ws_id,
    )
    assert "Permission denied" in r2.get("result", ""), (
        "STALE MIRROR / gate still ALLOW: disk says filesystem=read but the "
        f"write probe succeeded: {r2}"
    )
