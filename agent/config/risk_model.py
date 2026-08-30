"""
Workspace risk model.

Computes a numeric risk score and a human-readable level for a workspace
permission map (see ``agent/config/resource_catalog.json`` for the resource
grains) combined with the workspace feature switches
(``allow_host_resources``).

Scoring rules (deterministic, additive):

- a granted (non-``banned``) catalog resource with ``risk_level == "high"``
  adds +25;
- a granted catalog resource with ``risk_level == "medium"`` adds +8;
- ``git_write`` granted at ``read`` or ``write`` adds +10 (repo mutation);
- ``allow_host_resources=True`` adds +20;
- every non-``banned`` permission adds +1.

Levels: score < 20 → ``low``; < 45 → ``medium``; otherwise ``high``.

Sanity: all-banned → 0 → low; the ``general`` purpose preset → ~31 →
medium; full grants + host resources → high.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from agent.config.resource_catalog import (
    catalog_default_permissions,
    catalog_entry,
)
from agent.config.workspace_purpose import apply_purpose_preset


def _resolve_permissions(
    permissions: Optional[Dict[str, str]],
    purpose: Optional[str],
) -> Dict[str, str]:
    """Fill an empty permission map from the purpose preset / catalog defaults."""
    if permissions:
        return dict(permissions)
    if purpose:
        return apply_purpose_preset(purpose)
    return catalog_default_permissions()


def compute_workspace_risk(
    permissions: Optional[Dict[str, str]] = None,
    allow_host_resources: bool = False,
    purpose: Optional[str] = None,
) -> Dict[str, Any]:
    """Compute ``{level, score, factors, granted_count}`` for a workspace.

    ``permissions`` is a ``{resource_name: level}`` map.  When it is empty
    or None, the *purpose* preset (or catalog defaults) supplies the map.
    """
    perms = _resolve_permissions(permissions, purpose)

    score = 0
    granted_count = 0
    factors: List[Dict[str, Any]] = []

    for name, level in perms.items():
        if level == "banned":
            continue
        granted_count += 1
        score += 1
        entry = catalog_entry(name)
        risk = (entry or {}).get("risk_level", "low")
        if risk == "high":
            score += 25
            factors.append(
                {
                    "resource": name,
                    "level": level,
                    "weight": 25,
                    "reason": f"high-risk resource '{name}' granted ({level})",
                }
            )
        elif risk == "medium":
            score += 8
            factors.append(
                {
                    "resource": name,
                    "level": level,
                    "weight": 8,
                    "reason": f"medium-risk resource '{name}' granted ({level})",
                }
            )
        if name == "git_write" and level in ("read", "write"):
            score += 10
            factors.append(
                {
                    "resource": name,
                    "level": level,
                    "weight": 10,
                    "reason": "git_write grants repository mutation",
                }
            )

    if allow_host_resources:
        score += 20
        factors.append(
            {
                "resource": "allow_host_resources",
                "level": "true",
                "weight": 20,
                "reason": "host resource access enabled",
            }
        )

    if score < 20:
        level = "low"
    elif score < 45:
        level = "medium"
    else:
        level = "high"

    return {
        "level": level,
        "score": score,
        "factors": factors,
        "granted_count": granted_count,
    }


def risk_level_for_permissions(
    permissions: Optional[Dict[str, str]] = None,
    allow_host_resources: bool = False,
    purpose: Optional[str] = None,
) -> str:
    """Return just the risk level string (``low`` | ``medium`` | ``high``)."""
    return compute_workspace_risk(
        permissions=permissions,
        allow_host_resources=allow_host_resources,
        purpose=purpose,
    )["level"]
