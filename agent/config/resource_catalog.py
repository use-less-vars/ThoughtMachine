"""
Resource catalog loader.

Loads ``agent/config/resource_catalog.json`` — the canonical list of
controllable resource grains (``git_read``, ``git_write``, ``host_bash``,
``container``, ...), their permission levels (``banned|ask|read|write``),
defaults, required workspace feature switches, and risk ratings.

NOTE: the catalog FILE now carries the new resource-level array shape
(``git`` / ``filesystem`` / ``docker`` / ``host_bash`` / ``tty`` / ``jtag``)
and is the source of truth for the ``/api/resource-catalog`` endpoint.  This
loader exposes a LEGACY tool-level view (the permission grains in
``_LEGACY_RESOURCES``) for the permission machinery (workspace permission
validation, purpose presets, risk model) so their behavior stays unchanged.

Consumers: workspace permission endpoints (``web_ui/backend/workspace_routes.py``),
the workspace purpose presets (``agent/config/workspace_purpose.py``) and the
risk model (``agent/config/risk_model.py``).
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_CATALOG_PATH = Path(__file__).resolve().parent / "resource_catalog.json"

#: Legacy tool-level resource grains exposed to the permission machinery.
#: The on-disk catalog file is the NEW resource-level array; this constant is
#: the authoritative legacy view so validation / presets / risk keep working.
_LEGACY_RESOURCES = {
    "git_read": {
        "name": "Git Read",
        "description": "Read-only git repository inspection (status, diff, log, branch).",
        "default_permission": "read",
        "required_workspace_switch": None,
        "risk_level": "low",
        "ui_category": "git",
    },
    "git_write": {
        "name": "Git Write",
        "description": "Git write operations (commit, init, clone, checkout, stage).",
        "default_permission": "ask",
        "required_workspace_switch": None,
        "risk_level": "medium",
        "ui_category": "git",
    },
    "host_bash": {
        "name": "Host Bash",
        "description": "Supervised shell command execution on the host machine.",
        "default_permission": "banned",
        "required_workspace_switch": "allow_host_resources",
        "risk_level": "high",
        "ui_category": "host",
    },
    "container": {
        "name": "Container",
        "description": "Docker container lifecycle and code execution in sandboxes.",
        "default_permission": "ask",
        "required_workspace_switch": "allow_docker",
        "risk_level": "medium",
        "ui_category": "sandbox",
    },
    "network": {
        "name": "Network",
        "description": "Outbound network access (HTTP requests, downloads, API calls).",
        "default_permission": "ask",
        "required_workspace_switch": None,
        "risk_level": "medium",
        "ui_category": "network",
    },
    "filesystem": {
        "name": "Filesystem",
        "description": "File read/write access to the workspace tree.",
        "default_permission": "read",
        "required_workspace_switch": None,
        "risk_level": "low",
        "ui_category": "filesystem",
    },
    "system": {
        "name": "System",
        "description": "Read-only system inspection (environment, processes, vault state).",
        "default_permission": "read",
        "required_workspace_switch": None,
        "risk_level": "low",
        "ui_category": "system",
    },
    "git": {
        "name": "Git",
        "description": "Legacy git permission grain (superseded by git_read / git_write).",
        "default_permission": "read",
        "required_workspace_switch": None,
        "risk_level": "low",
        "ui_category": "git",
    },
    "execution": {
        "name": "Execution",
        "description": "Arbitrary code execution outside sandboxes (high blast radius).",
        "default_permission": "banned",
        "required_workspace_switch": None,
        "risk_level": "high",
        "ui_category": "execution",
    },
    "mcp": {
        "name": "MCP Integrations",
        "description": "External MCP server tool integrations.",
        "default_permission": "banned",
        "required_workspace_switch": None,
        "risk_level": "high",
        "ui_category": "integrations",
    },
}


def load_resource_catalog() -> Dict[str, Any]:
    """Load the resource catalog JSON (uncached, always re-read).

    Shim: if the file still carries the legacy dict shape it is returned
    as-is; if it carries the NEW array shape, the legacy dict view
    ``{"schema_version": 1, "permission_levels": ["banned", "ask", "read",
    "write"], "resources": _LEGACY_RESOURCES}`` is returned so the permission
    machinery keeps its legacy tool-level semantics.
    """
    with open(_CATALOG_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        return data
    return {
        "schema_version": 1,
        "permission_levels": ["banned", "ask", "read", "write"],
        "resources": _LEGACY_RESOURCES,
    }


@lru_cache(maxsize=1)
def get_resource_catalog() -> Dict[str, Any]:
    """Return the resource catalog (cached for the process lifetime)."""
    return load_resource_catalog()


def catalog_permission_levels() -> List[str]:
    """Return the valid permission levels in canonical order."""
    return list(get_resource_catalog().get("permission_levels", []))


def catalog_resource_names() -> List[str]:
    """Return the canonical resource names (legacy tool-level grains)."""
    return list(get_resource_catalog().get("resources", {}).keys())


def catalog_entry(name: str) -> Optional[Dict[str, Any]]:
    """Return the catalog entry dict for *name*, or None if unknown."""
    return get_resource_catalog().get("resources", {}).get(name)


def catalog_default_permissions() -> Dict[str, str]:
    """Return ``{resource_name: default_permission}`` for every resource."""
    defaults: Dict[str, str] = {}
    for name, entry in get_resource_catalog().get("resources", {}).items():
        defaults[name] = entry.get("default_permission", "banned")
    return defaults


def validate_workspace_permissions(
    permissions: Dict[str, str],
) -> Tuple[Dict[str, str], List[str]]:
    """Validate a workspace permission map against the resource catalog.

    Returns ``(normalized, errors)``:

    - ``normalized`` — dict with unknown resource names and invalid levels
      dropped; every remaining value is coerced to ``str``.
    - ``errors`` — human-readable problems (empty when the input is clean).

    Callers should treat any non-empty ``errors`` as a hard rejection
    (HTTP 422) rather than silently persisting the partial map.
    """
    errors: List[str] = []
    normalized: Dict[str, str] = {}
    if not isinstance(permissions, dict):
        return {}, ["permissions must be an object mapping resource name to level"]

    valid_levels = set(catalog_permission_levels())
    known = set(catalog_resource_names())

    for name, level in permissions.items():
        if name not in known:
            errors.append(f"unknown resource '{name}'")
            continue
        if level not in valid_levels:
            errors.append(
                f"invalid level '{level}' for resource '{name}' "
                f"(expected one of {sorted(valid_levels)})"
            )
            continue
        normalized[name] = str(level)

    return normalized, errors
