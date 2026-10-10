"""
Resource catalog loader.

Loads ``agent/config/resource_catalog.json`` — the canonical list of
controllable resource grains (``git``, ``filesystem``, ``container``,
``network``, ``mcp``, ``host_bash``), their permission levels
(``banned|ask|read|write``), defaults, required workspace feature
switches, and risk ratings.

NOTE: the catalog FILE now carries the new resource-level array shape
(``git`` / ``filesystem`` / ``container`` / ``host_bash`` / ``tty`` /
``jtag``) and is the source of truth for the ``/api/resource-catalog``
endpoint.  This loader exposes the workspace-permission view (the
permission resources in ``_LEGACY_RESOURCES`` — the six canonical
workspace resources; the removed legacy grains ``git_read``/
``git_write``/``system``/``execution`` no longer appear) for the
permission machinery (workspace permission validation, purpose presets,
risk model) so their behavior stays unchanged.

Consumers: workspace permission endpoints (``web_ui/backend/workspace_routes.py``),
the workspace purpose presets (``agent/config/workspace_purpose.py``) and the
risk model (``agent/config/risk_model.py``).
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from security.resource_catalog import WORKSPACE_CEILING_VOCAB

_CATALOG_PATH = Path(__file__).resolve().parent / "resource_catalog.json"

#: Canonical display order of the DERIVED legacy workspace-permission
#: view (emitted in this order for a stable sequence): ``git``,
#: ``filesystem``, ``container``, ``network``, ``mcp``, ``host_bash``.
_LEGACY_RESOURCE_ORDER: Tuple[str, ...] = (
    "git",
    "filesystem",
    "container",
    "network",
    "mcp",
    "host_bash",
)

#: Display-only array-catalog resources EXCLUDED from the legacy
#: workspace-permission view (R6).  ``tty``/``jtag`` carry no session grant,
#: so they are not permission resources and must not appear here.
_DISPLAY_ONLY_RESOURCES: Tuple[str, ...] = ("tty", "jtag")

#: Per-resource ``default_permission`` literals for the legacy view (R1).
#: ``default_permission`` is intentionally NOT moved into the array-catalog
#: file (B); it stays a test-pinned literal here.
#: RECONCILED (R13 step 4): these literals are now OWNED by
#: ``thoughtmachine.security.SAFE_DEFAULTS`` (the single owner).  The former
#: ``network`` divergence (``"ask"``) is resolved fail-closed to ``"banned"``
#: so outbound access is never granted by default;
#: ``tests/test_safe_defaults_single_owner.py`` pins this dict EQUAL to
#: ``SAFE_DEFAULTS`` so the two can never drift again.
_RESIDUAL_DEFAULT_PERMISSIONS: Dict[str, Any] = {
    "git": "read",
    "filesystem": "read",
    "container": False,
    "network": "banned",
    "mcp": "banned",
    "host_bash": "banned",
}

#: Residual permission-only grains that have NO array-catalog (B)
#: counterpart (R2).  ``network``/``mcp`` are present in A
#: (``security.resource_catalog``) but ABSENT from B, so D1's invariant
#: "A's session vocabulary is a subset of B's catalog" is FALSE on the
#: current data; step 6 closes it.  They are therefore explicit residual
#: literals here and are deliberately NOT added to B (doing so would grow
#: the served array from 6 to 8 entries).  Neither carries an
#: ``execution_mode``.
_RESIDUAL_RESOURCES: Dict[str, Dict[str, Any]] = {
    "network": {
        "name": "Network",
        "description": "Outbound network access (HTTP requests, downloads, API calls).",
        "required_workspace_switch": None,
        "risk_level": "medium",
        "ui_category": "network",
    },
    "mcp": {
        "name": "MCP Integrations",
        "description": "External MCP server tool integrations.",
        "required_workspace_switch": None,
        "risk_level": "high",
        "ui_category": "integrations",
    },
}

#: Canonical per-resource permission vocabularies (workspace ceilings).
#: Import-backed alias of ``security.resource_catalog.WORKSPACE_CEILING_VOCAB``
#: (the single source of truth) under the name consumed by the permission
#: machinery here.  ``validate_workspace_permissions`` accepts exactly these
#: levels per resource: ``git`` retains ``write_on_feature_branch``
#: (branch-restricted write ceiling); ``git``/``filesystem`` do NOT accept
#: ``full`` (a legacy stored ``full`` ceiling is normalised to ``write``);
#: ``docker`` is handled as a legacy string alias of the boolean
#: ``container`` ceiling and is NOT a table key.  The removed legacy grains
#: ``git_read``/``git_write``/``system``/``execution`` have no entry here --
#: they are rejected by validation with a legacy-removed hint.
_PERMISSION_LEVELS_BY_RESOURCE: Dict[str, Tuple[str, ...]] = WORKSPACE_CEILING_VOCAB

#: Legacy string vocabulary accepted for the ``docker`` alias of the
#: boolean ``container`` ceiling. ``banned``/``read``/``ask`` deny the
#: container session grant (normalise to False); ``write``/``full`` allow
#: it (normalise to True).  The ``container`` key itself accepts NO string
#: levels: workspace permissions for it must be real booleans.
_CONTAINER_LEGACY_STRINGS: Tuple[str, ...] = ("banned", "read", "ask", "write", "full")

#: Legacy docker-alias strings that map to a granted (True) ceiling.
_CONTAINER_GRANT_STRINGS: Tuple[str, ...] = ("write", "full")


#: Legacy permission resources removed from the workspace-ceiling surface.
#: ``git_read`` / ``git_write`` folded onto the canonical ``git`` ceiling;
#: ``system`` / ``execution`` ceilings no longer exist (system inspection
#: is unconditionally available; execution is not user-configurable).
_REMOVED_LEGACY_RESOURCES: Tuple[str, ...] = (
    "git_read",
    "git_write",
    "system",
    "execution",
)

#: Valid per-resource ``execution_mode`` vocabulary (renamed from the retired
#: ``default_execution_context``).  ``"container"`` runs a resource's work
#: inside its resource container; ``"host"`` runs it on the host.
_EXECUTION_MODES: Tuple[str, ...] = ("container", "host")


def _validate_execution_mode(entries: Any) -> None:
    """Reject any array-catalog entry whose ``execution_mode`` is invalid.

    Raises ``ValueError`` naming the offending entry and value so a malformed
    catalog fails loudly at load time instead of silently defaulting.  Non-list
    inputs are ignored (the legacy dict shape carries no ``execution_mode``).
    """
    if not isinstance(entries, list):
        return
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        mode = entry.get("execution_mode")
        if mode not in _EXECUTION_MODES:
            raise ValueError(
                "resource catalog entry "
                f"{entry.get('name', '<unnamed>')!r} has invalid "
                f"execution_mode {mode!r}; expected one of {list(_EXECUTION_MODES)}"
            )


def _build_legacy_resources(
    catalog_entries: List[Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    """DERIVE the legacy workspace-permission view from B (+ residuals).

    For every array-catalog (B) entry that is not display-only
    (``tty``/``jtag``) the metadata is read straight from B:
    ``name`` <- B ``display_name``; ``description``, ``execution_mode``,
    ``risk_level``, ``ui_category`` and ``required_workspace_switch`` <- the
    matching B fields (R3/R4/E.1).  ``default_permission`` stays a residual
    literal (R1).  The permission-only grains ``network``/``mcp`` are added
    as explicit residuals (R2).  A FRESH dict is returned -- never a live
    reference to a module global.
    """
    by_key: Dict[str, Dict[str, Any]] = {}
    for entry in catalog_entries:
        key = entry.get("name")
        if key in _DISPLAY_ONLY_RESOURCES:
            continue
        derived: Dict[str, Any] = {
            "name": entry.get("display_name"),
            "description": entry.get("description"),
            "default_permission": _RESIDUAL_DEFAULT_PERMISSIONS.get(key, "banned"),
            "risk_level": entry.get("risk_level"),
            "ui_category": entry.get("ui_category"),
            "required_workspace_switch": entry.get("required_workspace_switch"),
        }
        mode = entry.get("execution_mode")
        if mode is not None:
            derived["execution_mode"] = mode
        by_key[key] = derived
    for key, residual in _RESIDUAL_RESOURCES.items():
        by_key[key] = {
            "name": residual["name"],
            "description": residual["description"],
            "default_permission": _RESIDUAL_DEFAULT_PERMISSIONS[key],
            "risk_level": residual["risk_level"],
            "ui_category": residual["ui_category"],
            "required_workspace_switch": residual["required_workspace_switch"],
        }
    ordered: Dict[str, Dict[str, Any]] = {
        key: by_key[key] for key in _LEGACY_RESOURCE_ORDER if key in by_key
    }
    for key, value in by_key.items():
        if key not in ordered:
            ordered[key] = value
    return ordered


def load_resource_catalog() -> Dict[str, Any]:
    """Load the resource catalog JSON (uncached, always re-read).

    Shim: if the file still carries the legacy dict shape it is returned
    as-is; if it carries the NEW array shape, the legacy dict view
    ``{"schema_version": 1, "permission_levels": ["banned", "ask", "read",
    "write"], "resources": <derived from B + residuals>}`` is DERIVED (see
    ``_build_legacy_resources``) so the permission machinery keeps its legacy
    tool-level semantics while B stays the single metadata store.

    Every array entry's ``execution_mode`` is validated against
    ``_EXECUTION_MODES`` (``container`` | ``host``); an invalid value raises
    ``ValueError`` rather than being silently defaulted.
    """
    with open(_CATALOG_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        return data
    _validate_execution_mode(data)
    # Wrapper literals (R5): the legacy historical shape contract.  Kept
    # EXACTLY as-is and deliberately NOT derived from A, whose ``git``
    # vocabulary is a 5-item list including ``write_on_feature_branch``.
    return {
        "schema_version": 1,
        "permission_levels": ["banned", "ask", "read", "write"],
        "resources": _build_legacy_resources(data),
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


def catalog_default_permissions() -> Dict[str, Any]:
    """Return ``{resource_name: default_permission}`` for every resource.

    Values are the per-resource defaults: strings for level-based resources
    and a boolean for the ``container`` ceiling.
    """
    defaults: Dict[str, Any] = {}
    for name, entry in get_resource_catalog().get("resources", {}).items():
        defaults[name] = entry.get("default_permission", "banned")
    return defaults


def validate_workspace_permissions(
    permissions: Dict[str, Any],
) -> Tuple[Dict[str, Any], List[str]]:
    """Validate and normalise a workspace-permission mapping.

    ``permissions`` maps a resource name to its requested ceiling.  Each
    resource has its own canonical vocabulary (see
    ``_PERMISSION_LEVELS_BY_RESOURCE``):

    - ``git`` / ``filesystem`` accept the ceiling levels ``banned|ask|read|
      write`` (``git`` additionally retains ``write_on_feature_branch`` as
      its branch-restricted-write ceiling; no ``full`` ceiling exists --
      legacy stored ``full`` ceilings are normalised to ``write``); the
      removed legacy grains ``git_read`` / ``git_write`` / ``system`` /
      ``execution`` are rejected with a legacy-removed hint (``git_read``/
      ``git_write`` point at ``git``; ``system``/``execution`` have no
      ceiling anymore);
    - ``network`` accepts ``banned|ask|write|outbound``;
    - ``mcp`` accepts ``banned|connect|full`` (its own session scale);
    - ``host_bash`` accepts ``banned|ask|allow``;
    - ``container`` is a boolean-only ceiling: real booleans pass through;
      any string level (including the legacy ``banned``/``read``/``ask``/
      ``write``/``full`` forms) is rejected;
    - ``docker`` is a legacy string alias of ``container``:
      ``banned``/``read``/``ask`` normalise to ``False`` and
      ``write``/``full`` to ``True``.

    Returns ``(normalized, errors)`` where *normalized* contains only
    validated entries and *errors* lists every rejected one.  Unknown
    resource names and invalid levels are reported, never silently dropped.
    """
    errors: List[str] = []
    normalized: Dict[str, Any] = {}
    if not isinstance(permissions, dict):
        return {}, ["permissions must be an object mapping resource name to level"]
    valid_levels = set(catalog_permission_levels())
    known = set(catalog_resource_names())
    for name, level in permissions.items():
        if name == "docker":
            if isinstance(level, str) and level.strip().lower() in _CONTAINER_LEGACY_STRINGS:
                normalized["container"] = level.strip().lower() in _CONTAINER_GRANT_STRINGS
                continue
            errors.append(
                f"invalid level '{level}' for resource 'docker' (legacy alias of "
                f"'container'; expected one of {sorted(_CONTAINER_LEGACY_STRINGS)})"
            )
            continue
        if name in _REMOVED_LEGACY_RESOURCES:
            # Removed ceiling grains: always rejected with a hint naming the
            # canonical replacement so stale configs/UI payloads get a
            # precise 422 instead of a generic 'unknown resource'.
            hint = "; use 'git'" if name in ("git_read", "git_write") else ""
            errors.append(
                f"legacy permission resource '{name}' is no longer supported"
                f"{hint}"
            )
            continue
        if name not in known:
            errors.append(f"unknown resource '{name}'")
            continue
        if name == "container":
            if isinstance(level, bool):
                normalized[name] = level
                continue
            # String ceilings are rejected (fail closed) rather than
            # normalised: ``container`` is boolean-only.  Legacy string
            # configs must use the ``docker`` alias (or the stored boolean
            # form) to be accepted.
            errors.append(
                f"invalid level '{level}' for resource 'container' (expected a boolean)"
            )
            continue
        if name == "host_bash":
            if level in ("banned", "ask", "allow"):
                normalized[name] = str(level)
                continue
            errors.append(
                f"invalid level '{level}' for resource 'host_bash' "
                f"(expected one of {sorted(('banned', 'ask', 'allow'))})"
            )
            continue
        # Generic per-resource vocabulary check.  Every catalog name is either
        # special-cased above or present in _PERMISSION_LEVELS_BY_RESOURCE, so
        # the fallback below is purely defensive.
        accepted = _PERMISSION_LEVELS_BY_RESOURCE.get(name)
        if accepted is None:
            accepted = tuple(sorted(valid_levels))
        if not isinstance(level, str) or level not in accepted:
            errors.append(
                f"invalid level '{level}' for resource '{name}' "
                f"(expected one of {sorted(accepted)})"
            )
            continue
        normalized[name] = str(level)
    return normalized, errors

