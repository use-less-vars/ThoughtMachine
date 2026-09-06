"""
Hermetic unit tests for the canonical resource permission catalog
(security/resource_catalog.py).

The catalog is the single source of truth for the resource keys and levels a
session permission grant may carry.  ``coerce_resource_permissions`` is
applied by ``thoughtmachine/permission_store`` on every session-grant write
and read; these tests pin the catalog contents and the coercion rules
(unknown-key and invalid-value dropping, the container bool-only rule, and
warning logging).
"""

import logging
import sys
from pathlib import Path

# Add project root so that imports work (same pattern as sibling tests).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402  (import order forced by the sys.path shim)

from security.resource_catalog import (  # noqa: E402
    RESOURCE_CATALOG,
    canonical_resource_keys,
    coerce_resource_permissions,
)


# ---------------------------------------------------------------------------
# Catalog contents
# ---------------------------------------------------------------------------


def test_canonical_resource_keys_match_catalog():
    assert canonical_resource_keys == set(RESOURCE_CATALOG)
    assert canonical_resource_keys == {
        "git", "filesystem", "container", "network", "mcp", "host_bash",
    }


def test_catalog_expected_shape():
    assert RESOURCE_CATALOG["git"] == [
        "banned", "ask", "read", "write", "write_on_feature_branch",
    ]
    assert RESOURCE_CATALOG["filesystem"] == ["banned", "read", "write"]
    assert RESOURCE_CATALOG["container"] == [True, False]
    assert RESOURCE_CATALOG["network"] == ["banned", "ask", "write", "outbound"]
    assert RESOURCE_CATALOG["mcp"] == ["banned", "connect", "full"]
    assert RESOURCE_CATALOG["host_bash"] == ["banned", "ask", "allow"]


# ---------------------------------------------------------------------------
# Coercion: unknown keys / invalid values
# ---------------------------------------------------------------------------


def test_coerce_drops_unknown_key_and_logs_warning(caplog):
    # network/mcp are now valid session-grant keys; system is not in the catalog.
    raw = {"system": "read", "filesystem": "read"}
    with caplog.at_level(logging.WARNING, logger="security.resource_catalog"):
        clean = coerce_resource_permissions(raw)
    assert clean == {"filesystem": "read"}
    assert any("system" in record.message for record in caplog.records)


def test_coerce_logs_one_warning_per_unknown_key(caplog):
    raw = {"system": "read", "execution": "banned", "git": "read"}
    with caplog.at_level(logging.WARNING, logger="security.resource_catalog"):
        clean = coerce_resource_permissions(raw)
    assert clean == {"git": "read"}
    messages = [record.message for record in caplog.records]
    assert sum("system" in m for m in messages) == 1
    assert sum("execution" in m for m in messages) == 1


def test_coerce_drops_invalid_level_and_logs_warning(caplog):
    raw = {"filesystem": "full", "git": "read"}
    with caplog.at_level(logging.WARNING, logger="security.resource_catalog"):
        clean = coerce_resource_permissions(raw)
    assert clean == {"git": "read"}
    assert any("full" in record.message for record in caplog.records)


# ---------------------------------------------------------------------------
# Coercion: container boolean-only rule
# ---------------------------------------------------------------------------


def test_coerce_container_accepts_only_real_bools():
    assert coerce_resource_permissions({"container": True}) == {"container": True}
    assert coerce_resource_permissions({"container": False}) == {"container": False}
    # Non-bool ints are invalid (False == 0 / True == 1 membership pitfall).
    assert coerce_resource_permissions({"container": 1}) == {}
    assert coerce_resource_permissions({"container": 0}) == {}
    assert coerce_resource_permissions({"container": "ask"}) == {}


# ---------------------------------------------------------------------------
# Coercion: valid entries preserved
# ---------------------------------------------------------------------------


def test_coerce_preserves_valid_entries_in_order():
    raw = {
        "git": "write_on_feature_branch",
        "filesystem": "banned",
        "container": True,
        "network": "outbound",
        "mcp": "connect",
        "host_bash": "allow",
    }
    assert coerce_resource_permissions(raw) == raw


def test_coerce_empty_dict_returns_empty():
    assert coerce_resource_permissions({}) == {}
