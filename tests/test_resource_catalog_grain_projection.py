"""Pin test: the SERVED resource catalog's ``permission_grain_set`` is derived
from ``security/resource_catalog.py::RESOURCE_CATALOG`` (R13 step 1).

D1 ruling: ``RESOURCE_CATALOG`` (A) is the single owner of the resource
vocabulary; each ``agent/config/resource_catalog.json`` (B) entry whose
``name`` matches an A key carries A's grain set, and a B resource absent from
A is display-only (its ``permission_grain_set`` is empty).  This relationship
is machine-checked here, exercised through the served endpoint
(``GET /api/resource-catalog``) so the assertion is on the SERVED value, not a
hand-read of the file.

RED-first: on the pre-R13 tree the git entry is missing
``write_on_feature_branch`` (and filesystem/host_bash carry stale values while
tty/jtag are non-empty), so this test fails before the grain fields are
projected from A.

NOTE (R13 STOP condition): ``container`` is intentionally EXCLUDED from the
equality projection below.  A declares container's vocabulary as boolean-only
(``[True, False]``, "no string vocabulary") whereas B's ``permission_grain_set``
is a string list; D1 does not specify the boolean->string mapping, so per the
brief's shape-mismatch rule the container grain is left unchanged and flagged
as an open STOP condition rather than invented.
"""

import os
import tempfile

import pytest
from starlette.testclient import TestClient

from security.resource_catalog import RESOURCE_CATALOG

#: Container is deliberately not projected (boolean-only in A; shape mismatch
#: unresolved by D1 -- see module docstring / R13 STOP condition).
_SHAPE_MISMATCH_KEYS = {"container"}


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


def test_served_grain_set_projection_from_canonical(client):
    """The SERVED grain sets are the canonical A grain sets.

    Core requirement: ``git``'s SERVED ``permission_grain_set`` equals
    ``RESOURCE_CATALOG['git']`` (it gains ``write_on_feature_branch``).
    Every other shared key matches A too, and keys absent from A (tty/jtag)
    are display-only with an empty grain set.
    """
    resp = client.get("/api/resource-catalog")
    assert resp.status_code == 200
    served = {entry["name"]: entry for entry in resp.json()}

    # Core RED-first assertion: git's SERVED grain set == A's git grain set.
    assert served["git"]["permission_grain_set"] == RESOURCE_CATALOG["git"]
    assert "write_on_feature_branch" in served["git"]["permission_grain_set"]

    # Full projection over the shared keys (container excluded: shape mismatch).
    for name, entry in served.items():
        if name in RESOURCE_CATALOG and name not in _SHAPE_MISMATCH_KEYS:
            assert entry["permission_grain_set"] == RESOURCE_CATALOG[name], name

    # Keys absent from A are display-only -> empty grain set.
    for name in ("tty", "jtag"):
        assert served[name]["permission_grain_set"] == [], name
