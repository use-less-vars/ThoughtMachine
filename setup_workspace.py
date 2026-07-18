#!/usr/bin/env python3
"""One-shot setup script: creates a ThoughtMachine workspace for the current project.

Usage:
    python setup_workspace.py

This will:
1. Register the project root in the workspace registry (generating a human-readable ID).
2. Create the workspace directory at ~/.thoughtmachine/workspaces/<human_id>/.
3. Write workspace_identity.json and config.json inside it.
4. Bootstrap default workspace files via ensure_workspace_dirs().
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

# Add project root to sys.path so thoughtmachine modules are importable
_PROJECT_ROOT = os.path.abspath(os.path.dirname(__file__))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


def _get_deterministic_hash(root_path: str) -> str:
    """Return a deterministic 16-char hex identifier for a root path."""
    return hashlib.sha256(root_path.encode()).hexdigest()[:16]


def _write_identity_file(ws_dir: Path, root_path: str, human_id: str) -> None:
    """Write workspace_identity.json inside the workspace directory."""
    identity = {
        "deterministic_hash": _get_deterministic_hash(root_path),
        "root_path": os.path.abspath(root_path),
        "human_id": human_id,
    }
    ws_dir.mkdir(parents=True, exist_ok=True)
    (ws_dir / "workspace_identity.json").write_text(
        json.dumps(identity, indent=2), encoding="utf-8"
    )


def _write_config_json(ws_dir: Path, root_path: str) -> None:
    """Write config.json for backward compatibility with ensure_workspace_dirs and bridge.py."""
    ws_dir.mkdir(parents=True, exist_ok=True)
    (ws_dir / "config.json").write_text(
        json.dumps({"root": os.path.abspath(root_path)}, indent=2), encoding="utf-8"
    )


def main() -> None:
    from thoughtmachine.workspace_registry import WorkspaceRegistry
    from thoughtmachine.workspace_capabilities import ensure_workspace_dirs

    root_path = os.path.abspath(_PROJECT_ROOT)

    # 1. Register in the workspace registry (gets or creates human-readable ID)
    registry = WorkspaceRegistry.get_default()
    entry = registry.register_by_root(root_path, label=os.path.basename(root_path))
    human_id = entry.id
    print(f"Workspace ID: {human_id}")
    print(f"  Root path:  {root_path}")

    # 2. Create workspace directory
    ws_dir = Path.home() / ".thoughtmachine" / "workspaces" / human_id

    # 3. Write identity and config files
    _write_identity_file(ws_dir, root_path, human_id)
    _write_config_json(ws_dir, root_path)

    # 4. Bootstrap default workspace files
    ensure_workspace_dirs(human_id)
    print(f"  Directory:  {ws_dir}")
    print("Done.")


if __name__ == "__main__":
    main()
