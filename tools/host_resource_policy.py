"""Shared policy helper for host-execution resource gates.

The workspace-level ``allow_host_resources`` top-level key (workspaces/<id>/config.json)
gates the host_bash tool -- checked in-tool by
``tools.host_bash_tool.HostBashTool.execute`` before the permission-grain/approval
flow -- and acts as an operator ceiling for *other* host-execution surfaces (e.g.
host-side git fallback).  This module exposes the single reader for that policy so the
semantics stay in one place.

Reads are fail-closed: a missing workspace id, missing file, malformed JSON or a
non-dict body all report ``False``.
"""

from __future__ import annotations

from typing import Optional


def workspace_allows_host_resources(workspace_id: Optional[str]) -> bool:
    """Return whether the workspace config allows host-resource execution.

    Reads the top-level ``allow_host_resources`` key of
    ``<vault_root>/workspaces/<workspace_id>/config.json``. Fail-closed on any
    lookup error (missing workspace id, missing file, invalid JSON, non-dict).
    """
    if not workspace_id:
        return False
    import json
    from pathlib import Path

    from thoughtmachine.vault import vault_root

    cfg_path = Path(vault_root()) / "workspaces" / workspace_id / "config.json"
    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    return bool(data.get("allow_host_resources", False))
