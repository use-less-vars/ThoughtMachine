"""Shared policy helper for host-execution resource gates.

The workspace-level ``allow_host_resources`` top-level key (workspaces/<id>/config.json)
gates the host_bash tool -- checked in-tool by
``tools.host_bash_tool.HostBashTool.execute`` before the permission-grain/approval
flow -- and acts as an operator ceiling for *other* host-execution surfaces (e.g.
host-side git fallback).  This module exposes the single reader for that policy so the
semantics stay in one place.

Reads are fail-closed: a missing workspace id, missing file, malformed JSON or a
non-dict body all report ``False``.  The gate is also strict: only the JSON
boolean ``true`` enables host resources -- every other present value (e.g. the
strings ``"true"``/``"yes"``/``"false"``, the numbers ``1``/``0``, ``null``)
reports ``False``.
"""

from __future__ import annotations

from typing import Optional


def load_workspace_config(workspace_id: str) -> dict:
    """Return the parsed ``<vault_root>/workspaces/<id>/config.json`` as a dict.

    Single source of truth for reading a workspace ``config.json``. Fail-closed
    on any lookup error (missing/empty workspace id, missing file, invalid JSON,
    non-dict body) by returning an empty dict -- this function never raises.
    """
    if not workspace_id:
        return {}
    import json
    from pathlib import Path

    from thoughtmachine.vault import vault_root

    cfg_path = Path(vault_root()) / "workspaces" / workspace_id / "config.json"
    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def workspace_allows_host_resources(workspace_id: Optional[str]) -> bool:
    """Return whether the workspace config allows host-resource execution.

    Reads the top-level ``allow_host_resources`` key of
    ``<vault_root>/workspaces/<workspace_id>/config.json``. Fail-closed on any
    lookup error (missing workspace id, missing file, invalid JSON, non-dict).

    Only the JSON boolean ``true`` enables host resources. Any other present
    value -- including the strings ``"true"``/``"yes"``/``"false"``, the
    numbers ``1``/``0`` and ``null`` -- reports ``False`` (an absent key too).
    """
    if not workspace_id:
        return False
    return load_workspace_config(workspace_id).get("allow_host_resources") is True
