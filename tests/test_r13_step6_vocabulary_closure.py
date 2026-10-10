"""R13 step 6 (RED-first) -- session-vocabulary closure of B.

Closes D1's invariant that was FALSE before step 6: A's session-resource
vocabulary (``security.resource_catalog.RESOURCE_CATALOG``) must be a SUBSET
of B's resources by name, where B is the array served by
``GET /api/resource-catalog``.  Before step 6 ``network``/``mcp`` lived in A
only (as loader residuals in ``agent/config/resource_catalog.py``), so the
subset assertion below FAILS; step 6 adds them to B so it PASSES.

The converse direction is also pinned: the only B entries absent from A are
the display-only resources (``tty``/``jtag``), which carry an empty
``permission_grain_set`` (no session grant).

Exercised through the SERVED endpoint so the assertion is on the served value,
not a hand-read of the file.
"""

import os
import tempfile

import pytest
from starlette.testclient import TestClient

from security.resource_catalog import RESOURCE_CATALOG

#: Display-only array-catalog resources: present in B but no A session key.
_DISPLAY_ONLY = {"tty", "jtag"}


@pytest.fixture(scope="module")
def client():
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

                with TestClient(server_mod.app) as test_client:
                    yield test_client
            finally:
                if old_home is None:
                    os.environ.pop("HOME", None)
                else:
                    os.environ["HOME"] = old_home


def test_a_session_vocabulary_is_subset_of_served_b_names(client):
    """Every A session-resource key must appear in the served B array."""
    resp = client.get("/api/resource-catalog")
    assert resp.status_code == 200
    served_names = {entry["name"] for entry in resp.json()}

    missing = set(RESOURCE_CATALOG) - served_names
    assert not missing, f"A resources absent from B: {sorted(missing)}"


def test_served_b_minus_a_is_display_only_with_empty_grains(client):
    """B entries absent from A are display-only, with an empty grain set."""
    resp = client.get("/api/resource-catalog")
    assert resp.status_code == 200
    entries = {entry["name"]: entry for entry in resp.json()}

    extra = set(entries) - set(RESOURCE_CATALOG)
    assert extra <= _DISPLAY_ONLY, f"unexpected B-only resources: {sorted(extra)}"
    for name in extra:
        assert entries[name]["permission_grain_set"] == [], name
