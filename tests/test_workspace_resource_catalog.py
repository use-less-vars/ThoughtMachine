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
    """The on-disk file (new array shape) and the loader's legacy view agree.

    The raw file is the new resource-level array (git / filesystem / docker /
    host_bash / tty / jtag); the loader shims it into the legacy tool-level
    dict view so the permission machinery keeps working unchanged.
    """
    catalog_path = Path(__file__).resolve().parent.parent / "agent/config/resource_catalog.json"
    raw = json.loads(catalog_path.read_text(encoding="utf-8"))

    # New array shape: 6 resources, each with exactly the 8 canonical keys.
    assert isinstance(raw, list)
    assert len(raw) == 6
    assert {entry["name"] for entry in raw} == {
        "git", "filesystem", "docker", "host_bash", "tty", "jtag",
    }
    for entry in raw:
        assert set(entry.keys()) == {
            "name", "display_name", "description", "permission_grain_set",
            "default_execution_context", "container_image",
            "dockerfile_reference", "tools",
        }
    git_entry = next(e for e in raw if e["name"] == "git")
    assert git_entry["dockerfile_reference"] == "docker/resource/git_overlay.Dockerfile"
    assert git_entry["tools"] == ["git_read", "git_write"]

    # Loader still exposes the legacy dict view for list-shaped files.
    loaded = get_resource_catalog()
    assert loaded["schema_version"] == 1
    assert loaded["permission_levels"] == ["banned", "ask", "read", "write"]
    assert list(loaded["resources"].keys()) == [
        "git_read", "git_write", "host_bash", "container", "network",
        "filesystem", "system", "git", "execution", "mcp",
    ]
    legacy_defaults = {
        "git_read": "read", "git_write": "ask", "host_bash": "banned",
        "container": "ask", "network": "ask", "filesystem": "read",
        "system": "read", "git": "read", "execution": "banned",
        "mcp": "banned",
    }
    for name, level in legacy_defaults.items():
        assert loaded["resources"][name]["default_permission"] == level
