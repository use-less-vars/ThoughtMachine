"""
R13 step 2 (RED-first) — ``agent/config/resource_catalog.json`` (B) must be the
single hand-authored store for resource metadata; the loader's *legacy*
workspace-permission view must be DERIVED from B (plus residuals) rather than
being a static module literal (``_LEGACY_RESOURCES``).

Pre-fold the loader returns the frozen ``_LEGACY_RESOURCES`` dict, so:

* B's raw entries do NOT yet carry the relocated non-A/B metadata keys
  (``risk_level`` / ``ui_category`` / ``required_workspace_switch``);
* B's ``description`` strings are NOT reflected by the legacy view for the
  divergent resources (``git`` / ``container``);
* editing B does NOT change the legacy view.

These assert the POST-fold contract; they are expected to FAIL on the pre-fold
tree and PASS once step 2 lands.
"""

from __future__ import annotations

import json
from pathlib import Path

import agent.config.resource_catalog as rc

_B_PATH = Path(rc.__file__).resolve().parent / "resource_catalog.json"

# Resources present in BOTH B's hand-authored array and the legacy view.
_SHARED = ("git", "filesystem", "container", "host_bash")

# Per-entry keys carrying the relocated non-A/B metadata (E.1).
_RELOCATED_KEYS = ("risk_level", "ui_category", "required_workspace_switch")

# Permission-only residual grains that have NO B entry.
_RESIDUAL = ("network", "mcp")


def _read_b_hardcoded():
    """Read B from its canonical on-disk location."""
    return json.loads(_B_PATH.read_text(encoding="utf-8"))


def _legacy_view():
    """Uncached legacy view (X2: defeat the lru_cache)."""
    rc.get_resource_catalog.cache_clear()
    try:
        return rc.load_resource_catalog()
    finally:
        rc.get_resource_catalog.cache_clear()


# ---------------------------------------------------------------------------
# B is the single store: its raw entries carry the relocated metadata keys.
# ---------------------------------------------------------------------------


def test_b_raw_entries_carry_relocated_metadata_keys():
    for entry in _read_b_hardcoded():
        for key in _RELOCATED_KEYS:
            assert key in entry, f"B entry {entry['name']!r} missing {key!r}"


# ---------------------------------------------------------------------------
# Legacy view derives name / description / relocated metadata FROM B.
# ---------------------------------------------------------------------------


def test_legacy_name_derives_from_b_display_name():
    view = _legacy_view()["resources"]
    b = {e["name"]: e for e in _read_b_hardcoded()}
    for name in _SHARED:
        assert view[name]["name"] == b[name]["display_name"], (
            f"{name}: legacy name {view[name]['name']!r} != B display_name "
            f"{b[name]['display_name']!r}"
        )


def test_legacy_description_derives_from_b():
    view = _legacy_view()["resources"]
    b = {e["name"]: e for e in _read_b_hardcoded()}
    for name in _SHARED:
        assert view[name]["description"] == b[name]["description"], (
            f"{name}: legacy description {view[name]['description']!r} != "
            f"B description {b[name]['description']!r}"
        )


def test_legacy_relocated_fields_derive_from_b():
    view = _legacy_view()["resources"]
    b = {e["name"]: e for e in _read_b_hardcoded()}
    for name in _SHARED:
        for key in _RELOCATED_KEYS:
            assert view[name][key] == b[name][key], (
                f"{name}.{key}: legacy view {view[name][key]!r} != "
                f"B {b[name][key]!r}"
            )


def test_editing_b_metadata_changes_legacy_view(monkeypatch, tmp_path):
    """A mutated B must flow through: B is the source, not a static literal."""
    b = _read_b_hardcoded()
    for entry in b:
        if entry["name"] == "git":
            entry["display_name"] = "GIT_MUTANT"
            entry["description"] = "MUTATED DESC"
            entry["risk_level"] = "MUTATED_RISK"
            entry["ui_category"] = "MUTATED_UI"
            entry["required_workspace_switch"] = "MUTATED_SWITCH"
    mutated = tmp_path / "resource_catalog.json"
    mutated.write_text(json.dumps(b), encoding="utf-8")
    monkeypatch.setattr(rc, "_CATALOG_PATH", mutated)
    rc.get_resource_catalog.cache_clear()
    try:
        view = rc.load_resource_catalog()["resources"]
    finally:
        rc.get_resource_catalog.cache_clear()
    git = view["git"]
    assert git["name"] == "GIT_MUTANT"
    assert git["description"] == "MUTATED DESC"
    assert git["risk_level"] == "MUTATED_RISK"
    assert git["ui_category"] == "MUTATED_UI"
    assert git["required_workspace_switch"] == "MUTATED_SWITCH"


# ---------------------------------------------------------------------------
# tty/jtag excluded (R6); network/mcp stay explicit residuals (R2).
# ---------------------------------------------------------------------------


def test_tty_jtag_excluded_from_legacy_view():
    names = rc.catalog_resource_names()
    assert "tty" not in names
    assert "jtag" not in names


def test_network_mcp_remain_residual_grains():
    view = _legacy_view()["resources"]
    assert set(view) == {
        "git", "filesystem", "container", "network", "mcp", "host_bash",
    }
    assert view["network"]["default_permission"] == "ask"
    assert view["mcp"]["default_permission"] == "banned"
    for name in _RESIDUAL:
        assert name not in {e["name"] for e in _read_b_hardcoded()}, (
            f"residual grain {name!r} must NOT be added to B"
        )


# ---------------------------------------------------------------------------
# Wrapper literals (R5) and residual defaults (R1) stay pinned.
# ---------------------------------------------------------------------------


def test_wrapper_literals_unchanged():
    view = _legacy_view()
    assert view["schema_version"] == 1
    assert view["permission_levels"] == ["banned", "ask", "read", "write"]


def test_default_permission_residual_values_pinned():
    view = _legacy_view()["resources"]
    assert view["git"]["default_permission"] == "read"
    assert view["filesystem"]["default_permission"] == "read"
    assert view["container"]["default_permission"] is False
    assert view["network"]["default_permission"] == "ask"
    assert view["mcp"]["default_permission"] == "banned"
    assert view["host_bash"]["default_permission"] == "banned"
