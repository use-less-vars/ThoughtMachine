"""
Tests for the workspace risk model (agent/config/risk_model.py).
"""

from __future__ import annotations

from agent.config.resource_catalog import catalog_resource_names
from agent.config.risk_model import compute_workspace_risk, risk_level_for_permissions
from agent.config.workspace_purpose import apply_purpose_preset


def test_risk_model_returns_expected_levels():
    """Risk scoring maps permission/feature combinations to low/medium/high."""
    all_banned = {name: "banned" for name in catalog_resource_names()}
    low = compute_workspace_risk(permissions=all_banned)
    assert low["level"] == "low"
    assert low["score"] == 0
    assert low["granted_count"] == 0

    general = compute_workspace_risk(permissions=apply_purpose_preset("general"))
    assert general["level"] == "medium"
    assert general["score"] >= 20

    risky = compute_workspace_risk(
        permissions={"host_bash": "write", "git_write": "write"},
        allow_host_resources=True,
    )
    assert risky["level"] == "high"
    assert risky["score"] >= 45

    # The single-level helper agrees with the full model.
    assert risk_level_for_permissions(
        permissions={"host_bash": "write", "git_write": "write"},
        allow_host_resources=True,
    ) == risky["level"]
    assert risk_level_for_permissions(permissions=all_banned) == "low"
