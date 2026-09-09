"""
Tests for the resource catalog (agent/config/resource_catalog.json + loader).

Canonical model:

- The on-disk catalog FILE is the new resource-level array shape (``git`` /
  ``filesystem`` / ``container`` / ``host_bash`` / ``tty`` / ``jtag``) used
  by the ``/api/resource-catalog`` endpoint.
- The LOADER exposes the workspace-permission view for the permission
  machinery: exactly the six canonical workspace resources ``git`` /
  ``filesystem`` / ``container`` / ``network`` / ``mcp`` / ``host_bash``
  (``git_read``/``git_write``/``system``/``execution`` were removed as
  permission resources; ``git_read``/``git_write`` survive only as git TOOL
  names listed in the git resource's ``tools`` array).
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

# Canonical workspace-permission resources (loader view), in display order.
_CANONICAL_RESOURCES = [
    "git",
    "filesystem",
    "container",
    "network",
    "mcp",
    "host_bash",
]

# On-disk array entries (the endpoint's resource list).
_ON_DISK_NAMES = {"git", "filesystem", "container", "host_bash", "tty", "jtag"}


def test_resource_catalog_contains_canonical_resources():
    """The catalog exposes exactly the six canonical permission resources and
    the git tools (git_read/git_write) are real tools, not resources."""
    from session.tool_presets import _ALL_TOOLS

    names = catalog_resource_names()
    assert names == _CANONICAL_RESOURCES, f"catalog resources: {names}"
    for name in _CANONICAL_RESOURCES:
        assert catalog_entry(name) is not None, f"catalog missing resource '{name}'"
    # Removed legacy permission grains are NOT catalog resources.
    for removed in ("git_read", "git_write", "system", "execution"):
        assert catalog_entry(removed) is None, (
            f"legacy grain '{removed}' must not be a catalog resource"
        )
    # git_read/git_write/host_bash remain real tool names.
    for tool in ("git_read", "git_write", "host_bash"):
        assert tool in _ALL_TOOLS, f"'{tool}' missing from session tool presets"


def test_resource_catalog_default_permissions():
    """Default permission map covers every resource with a valid level."""
    defaults = catalog_default_permissions()
    levels = catalog_permission_levels()
    assert list(defaults.keys()) == catalog_resource_names()
    for name, level in defaults.items():
        # Boolean ceilings (container) are normalised separately; string
        # levels must be part of the canonical vocabulary.
        if isinstance(level, bool):
            continue
        assert level in levels, f"resource '{name}' has invalid default level '{level}'"
    assert defaults["git"] == "read"
    assert defaults["filesystem"] == "read"
    assert defaults["container"] is False
    assert defaults["network"] == "ask"
    assert defaults["mcp"] == "banned"
    assert defaults["host_bash"] == "banned"
    assert levels == ["banned", "ask", "read", "write"]


def test_resource_catalog_json_matches_loader():
    """The on-disk file (array shape) and the loader's permission view agree.

    The raw file is the resource-level array (git / filesystem / container /
    host_bash / tty / jtag) for the /api/resource-catalog endpoint; the loader
    shims it into the canonical six-resource workspace-permission view so the
    permission machinery keeps working.
    """
    catalog_path = Path(__file__).resolve().parent.parent / "agent/config/resource_catalog.json"
    raw = json.loads(catalog_path.read_text(encoding="utf-8"))

    # Array shape: 6 entries, each with exactly the 8 canonical keys.
    assert isinstance(raw, list)
    assert len(raw) == 6
    assert {entry["name"] for entry in raw} == _ON_DISK_NAMES
    for entry in raw:
        assert set(entry.keys()) == {
            "name", "display_name", "description", "permission_grain_set",
            "default_execution_context", "container_image",
            "dockerfile_reference", "tools",
        }
    git_entry = next(e for e in raw if e["name"] == "git")
    assert git_entry["dockerfile_reference"] == "docker/resource/git_overlay.Dockerfile"
    assert git_entry["tools"] == ["git_read", "git_write"]
    host_bash_entry = next(e for e in raw if e["name"] == "host_bash")
    assert host_bash_entry["tools"] == ["host_bash"]

    # Loader view: exactly the six canonical permission resources.
    loaded = get_resource_catalog()
    assert loaded["schema_version"] == 1
    assert loaded["permission_levels"] == ["banned", "ask", "read", "write"]
    assert list(loaded["resources"].keys()) == _CANONICAL_RESOURCES
    canonical_defaults = {
        "git": "read",
        "filesystem": "read",
        "container": False,
        "network": "ask",
        "mcp": "banned",
        "host_bash": "banned",
    }
    for name, level in canonical_defaults.items():
        assert loaded["resources"][name]["default_permission"] == level
