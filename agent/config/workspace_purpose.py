"""
Workspace purpose presets.

A workspace purpose (``coding`` | ``research`` | ``general``) selects a
default permission map for the workspace resource catalog.  Custom
permission overrides (user-provided) are merged on top of the preset so an
explicit choice always wins.

The ``general`` preset mirrors the catalog defaults, so it stays in sync
with ``agent/config/resource_catalog.json`` automatically.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from agent.config.resource_catalog import catalog_default_permissions

#: Canonical workspace purposes (validated by POST /api/workspace).
WORKSPACE_PURPOSES = ["coding", "research", "general"]

#: Per-purpose preset definitions.  Each preset provides default_permissions
#: (only overrides of the catalog defaults need to be spelled out) and
#: risk_settings (feature switches that shape the workspace risk score).
PURPOSE_PRESETS: Dict[str, Dict[str, Any]] = {
    "coding": {
        "default_permissions": {
            "git_read": "read",
            "git_write": "ask",
            "host_bash": "banned",
            "container": "ask",
            "network": "ask",
            "filesystem": "write",
            "system": "read",
            "execution": "banned",
            "mcp": "banned",
        },
        "risk_settings": {
            "allow_host_resources": False,
        },
    },
    "research": {
        "default_permissions": {
            "git_read": "read",
            "git_write": "banned",
            "host_bash": "banned",
            "container": "banned",
            "network": "ask",
            "filesystem": "read",
            "system": "read",
            "execution": "banned",
            "mcp": "banned",
        },
        "risk_settings": {
            "allow_host_resources": False,
        },
    },
    "general": {
        "default_permissions": {},  # catalog defaults
        "risk_settings": {
            "allow_host_resources": False,
        },
    },
}


def get_purpose_preset(purpose: str) -> Dict[str, Any]:
    """Return the raw preset dict for *purpose* (empty dict if unknown)."""
    return dict(PURPOSE_PRESETS.get(purpose, {}))


def preset_default_permissions(purpose: str) -> Dict[str, str]:
    """Return the fully-resolved default permission map for *purpose*.

    ``general`` resolves to the catalog defaults; the other presets start
    from the catalog defaults and layer their overrides on top.
    """
    base = catalog_default_permissions()
    preset = PURPOSE_PRESETS.get(purpose, {})
    overrides = preset.get("default_permissions", {})
    merged = dict(base)
    merged.update(overrides)
    return merged


def apply_purpose_preset(
    purpose: str,
    custom_permissions: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    """Resolve a permission map for *purpose*, merging custom overrides.

    ``custom_permissions`` entries win over the preset defaults.  Unknown
    purposes fall back to the ``general`` preset.
    """
    if purpose not in PURPOSE_PRESETS:
        purpose = "general"
    merged = preset_default_permissions(purpose)
    if custom_permissions:
        merged.update(custom_permissions)
    return merged
