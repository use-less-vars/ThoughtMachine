"""
Resource catalog loader.

Loads ``agent/config/resource_catalog.json`` — the canonical list of
controllable resource grains (``git_read``, ``git_write``, ``host_bash``,
``container``, ...), their permission levels (``banned|ask|read|write``),
defaults, required workspace feature switches, and risk ratings.

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


def load_resource_catalog() -> Dict[str, Any]:
    """Load the raw resource catalog JSON (uncached, always re-read)."""
    with open(_CATALOG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


@lru_cache(maxsize=1)
def get_resource_catalog() -> Dict[str, Any]:
    """Return the resource catalog (cached for the process lifetime)."""
    return load_resource_catalog()


def catalog_permission_levels() -> List[str]:
    """Return the valid permission levels, e.g. ``["banned", "ask", "read", "write"]``."""
    return list(get_resource_catalog().get("permission_levels", []))


def catalog_resource_names() -> List[str]:
    """Return the canonical resource names (stable insertion order)."""
    return list(get_resource_catalog().get("resources", {}).keys())


def catalog_entry(name: str) -> Optional[Dict[str, Any]]:
    """Return the catalog entry dict for *name*, or None if unknown."""
    return get_resource_catalog().get("resources", {}).get(name)


def catalog_default_permissions() -> Dict[str, str]:
    """Return ``{resource_name: default_permission}`` for every catalog resource."""
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
