"""
R13 step 3 (RED-first) — ``agent/config/schema_manifest.json`` (D) must DERIVE
its ``system/resource_catalog.json`` ``safe_default`` seed from
``agent/config/resource_catalog.json`` (B), the single hand-authored metadata
store, rather than hand-copying the array.

Design doc ``design_permission_facts_ownership.md`` step 3: "Generate D's
``safe_default`` from B's serialiser. Replace the hand-copied array in
``schema_manifest.json`` ... Vault-repair / drift / backfill tests must keep
passing with byte-identical seed content."

Pre-regeneration D's seed is a hand-copied 6-object array whose entries carry
only the old 8 keys (no ``risk_level`` / ``ui_category`` /
``required_workspace_switch``) and stale ``permission_grain_set`` arrays, so
this assertion is expected to FAIL before the regeneration lands and PASS
after.

Ruling (b): no bespoke generator script — a pin test asserting D's seed equals
B's serialization, plus a one-time regeneration of D's ``safe_default`` block.
"""

from __future__ import annotations

import json
from pathlib import Path

import agent.config.resource_catalog as rc

_CONFIG_DIR = Path(rc.__file__).resolve().parent
_B_PATH = _CONFIG_DIR / "resource_catalog.json"
_D_PATH = _CONFIG_DIR / "schema_manifest.json"

_ENTRY_KEY = "system/resource_catalog.json"


def _load_b():
    """Read B (the single hand-authored metadata store) from disk."""
    return json.loads(_B_PATH.read_text(encoding="utf-8"))


def _load_d_entry():
    """Return D's ``system/resource_catalog.json`` entry dict."""
    manifest = json.loads(_D_PATH.read_text(encoding="utf-8"))
    return manifest["files"][_ENTRY_KEY]


def test_safe_default_is_b_serialization():
    """D's safe_default seed must equal B's serialization (derived projection)."""
    entry = _load_d_entry()
    assert entry["safe_default"] == _load_b()


def test_safe_default_carries_relocated_keys():
    """Every seed entry must carry B's relocated metadata keys."""
    seed = _load_d_entry()["safe_default"]
    relocated = ("risk_level", "ui_category", "required_workspace_switch")
    for obj in seed:
        for key in relocated:
            assert key in obj, f"seed entry {obj.get('name')!r} missing {key!r}"
