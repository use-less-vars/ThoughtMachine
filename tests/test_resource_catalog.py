"""
Tests for the resource catalog (agent/config/resource_catalog.json + loader).
"""

from __future__ import annotations

import json
from pathlib import Path

from agent.config.resource_catalog import (
    catalog_default_permissions,
    catalog_entry,
    catalog_permission_levels,
    catalog_resource_names,
    get_resource_catalog,
)


def test_resource_catalog_contains_required_tools():
    """The catalog covers git_read/git_write/host_bash and all three are real tools."""
    from session.tool_presets import _ALL_TOOLS

    names = catalog_resource_names()
    for required in ("git_read", "git_write", "host_bash"):
        assert required in names, f"catalog missing resource '{required}'"
        assert catalog_entry(required) is not None
        assert required in _ALL_TOOLS, f"'{required}' missing from session tool presets"


def test_resource_catalog_default_permissions():
    """Default permission map covers every resource with a valid level."""
    defaults = catalog_default_permissions()
    levels = catalog_permission_levels()
    assert list(defaults.keys()) == catalog_resource_names()
    for name, level in defaults.items():
        assert level in levels, f"resource '{name}' has invalid default level '{level}'"
    assert defaults["git_read"] == "read"
    assert defaults["git_write"] == "ask"
    assert defaults["host_bash"] == "banned"
    assert levels == ["banned", "ask", "read", "write"]


def test_resource_catalog_json_matches_loader():
    """The on-disk JSON is the source of truth for the loader."""
    catalog_path = Path(__file__).resolve().parent.parent / "agent/config/resource_catalog.json"
    raw = json.loads(catalog_path.read_text(encoding="utf-8"))
    assert raw["schema_version"] == 1
    loaded = get_resource_catalog()
    assert loaded["schema_version"] == raw["schema_version"]
    assert loaded["permission_levels"] == raw["permission_levels"]
    assert loaded["resources"].keys() == raw["resources"].keys()
    for name, entry in raw["resources"].items():
        assert loaded["resources"][name] == entry
