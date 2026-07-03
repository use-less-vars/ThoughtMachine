#!/usr/bin/env python3
"""
Bootstrap a workspace for the current project so the Worker tool can find it.

Creates ~/.thoughtmachine/workspaces/<id>/config.json pointing at the project,
then bootstraps default files including workers.json.
"""
import hashlib
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = os.path.abspath(os.path.dirname(__file__))


def main():
    thoughtmachine_dir = Path.home() / ".thoughtmachine"
    workspaces_dir = thoughtmachine_dir / "workspaces"
    workspaces_dir.mkdir(parents=True, exist_ok=True)

    # Compute workspace ID from project root (same algorithm as thoughtmachine.workspace_capabilities)
    ws_id = hashlib.sha256(PROJECT_ROOT.encode()).hexdigest()[:16]
    ws_dir = workspaces_dir / ws_id
    ws_dir.mkdir(parents=True, exist_ok=True)

    # ── Write config.json ────────────────────────────────────────────────
    config_path = ws_dir / "config.json"
    if not config_path.exists():
        config_path.write_text(
            json.dumps({"root": PROJECT_ROOT, "capabilities": {}}, indent=2),
            encoding="utf-8",
        )
        print(f"✅ Created {config_path}")
    else:
        print(f"⏩ Already exists: {config_path}")

    # ── Bootstrap default files (same as ensure_workspace_dirs) ──────────
    from thoughtmachine.workspace_capabilities import (
        WorkspaceCapabilities,
        ensure_workspace_dirs,
    )

    created = ensure_workspace_dirs(ws_id)
    for path in created:
        print(f"✅ Created {path}")

    # ── Write the echo worker into workers.json ──────────────────────────
    # ── Workers already written by ensure_workspace_dirs (echo + templates) ─
    workers_path = ws_dir / "workers.json"
    if workers_path.exists():
        print(f"Workers already bootstrapped at {workers_path}")
    else:
        echo_worker = {
            "name": "echo",
            "system_prompt": "You are Echo, a simple test worker. Respond concisely and directly to any query.",
            "required_categories": [],
            "worker_permissions": {},
            "description": "Simple test worker for verifying the delegation loop",
            "tools": ["FilePreviewTool"],
        }
        workers_path.write_text(json.dumps([echo_worker], indent=2), encoding="utf-8")
        print(f"Wrote echo worker definition to {workers_path}")


if __name__ == "__main__":
    sys.exit(main())
