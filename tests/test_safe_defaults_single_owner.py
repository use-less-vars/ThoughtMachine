"""Pin test for R13 step 4 — SINGLE OWNER of permission safe defaults.

The permission safe defaults have exactly ONE owner:
``thoughtmachine.security.SAFE_DEFAULTS``.  These tests pin the three
consistency contracts that keep that ownership honest:

1. The dedicated ``GET /api/permission-defaults`` endpoint serves exactly
   ``SAFE_DEFAULTS`` across the Py/JS boundary (network defaults to
   ``banned`` — fail-closed).
2. ``SAFE_DEFAULTS`` is STRUCTURAL: its keys are the canonical resource
   keys and every value is a valid level of that resource's scale.
3. The residual default literals in ``agent.config.resource_catalog`` and
   the JS boot-fallback literal in ``web_ui/frontend/src/store/useStore.js``
   agree with ``SAFE_DEFAULTS`` (no drift between the Python owner and the
   JS fallback published during boot).

Hermetic: the endpoint reads only the in-process ``SAFE_DEFAULTS`` constant
(no vault / runtime state touched), so the fresh server module (temp HOME +
prefix purge) is the only machinery needed.
"""

import os
import re
import sys
import tempfile

import pytest
from starlette.testclient import TestClient

from security.resource_catalog import RESOURCE_CATALOG, canonical_resource_keys
from thoughtmachine.security import SAFE_DEFAULTS


# ==========================================================================
# 1. Structural SAFE_DEFAULTS (single owner in thoughtmachine/security.py)
# ==========================================================================
def test_safe_defaults_keys_are_canonical_resource_keys():
    assert set(SAFE_DEFAULTS) == set(canonical_resource_keys)
    assert set(SAFE_DEFAULTS) == set(RESOURCE_CATALOG)


def test_safe_defaults_values_are_valid_levels():
    for key, value in SAFE_DEFAULTS.items():
        assert value in RESOURCE_CATALOG[key], (key, value)


def test_safe_defaults_network_is_banned_fail_closed():
    assert SAFE_DEFAULTS["network"] == "banned"


# ==========================================================================
# 2. Residual default literals agree with the owner (no drift)
# ==========================================================================
def test_residual_default_permissions_match_safe_defaults():
    from agent.config.resource_catalog import _RESIDUAL_DEFAULT_PERMISSIONS

    assert _RESIDUAL_DEFAULT_PERMISSIONS == dict(SAFE_DEFAULTS)


# ==========================================================================
# 3. JS store must NOT be a second source of truth -- it reads the endpoint
# ==========================================================================
_STORE_JS = (
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "web_ui",
        "frontend",
        "src",
        "store",
        "useStore.js",
    )
)


def _read_store_js() -> str:
    with open(_STORE_JS, "r", encoding="utf-8") as fh:
        return fh.read()


def test_js_store_has_no_hand_copied_permission_defaults_literal():
    """useStore.js must NOT define a hand-copied PERMISSION_DEFAULTS map.

    The single owner is the backend (``SAFE_DEFAULTS`` served via
    GET /api/permission-defaults); the JS store mirrors the response in its
    ``permissionDefaults`` slice.  A hard-coded ``PERMISSION_DEFAULTS = {...}``
    value map here would be a drifting duplicate of the owner and is asserted
    absent.
    """
    text = _read_store_js()
    assert not re.search(r"PERMISSION_DEFAULTS\s*=\s*\{", text), (
        "useStore.js must not define a hand-copied PERMISSION_DEFAULTS literal"
    )


def test_js_store_reads_permission_defaults_from_endpoint():
    """useStore.js must hydrate its defaults from the backend endpoint.

    Pins the read path that replaces the retired literal: the store imports
    ``fetchPermissionDefaults`` (-> GET /api/permission-defaults) and mirrors
    the response in the ``permissionDefaults`` slice.
    """
    text = _read_store_js()
    assert "fetchPermissionDefaults" in text
    assert "permissionDefaults" in text


# ==========================================================================
# 4. Dedicated endpoint publishes SAFE_DEFAULTS across the Py/JS boundary
# ==========================================================================
@pytest.fixture(scope="module")
def server_module():
    """Fresh import of web_ui.backend.server (temp HOME + prefix purge)."""
    from tests.integration.harness import purged_sys_modules

    prefixes = (
        "web_ui.backend",
        "agent.config.provider_profile",
        "thoughtmachine.bootstrap",
        "session",
    )

    with tempfile.TemporaryDirectory() as tmp_home:
        old_home = os.environ.get("HOME")
        os.environ["HOME"] = tmp_home
        with purged_sys_modules(prefixes):
            try:
                import web_ui.backend.server as server_mod

                yield server_mod
            finally:
                if old_home is None:
                    os.environ.pop("HOME", None)
                else:
                    os.environ["HOME"] = old_home


@pytest.fixture(scope="module")
def client(server_module):
    with TestClient(server_module.app) as test_client:
        yield test_client


def test_permission_defaults_endpoint_serves_safe_defaults(client):
    resp = client.get("/api/permission-defaults")

    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, dict)
    assert body == dict(SAFE_DEFAULTS)
    assert body["network"] == "banned"
