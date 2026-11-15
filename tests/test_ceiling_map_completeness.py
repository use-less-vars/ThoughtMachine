"""Pin the COMPLETENESS invariant of ``_WORKSPACE_RESOURCE_MAP``.

Every recognised workspace-ceiling resource must have a session-side mapping.
A resource in ``_WORKSPACE_CEILING_KEYS`` with no entry in
``_WORKSPACE_RESOURCE_MAP`` is a ceiling that silently does nothing: the
operator configures a restriction, the gate skips it and the session value
stands uncapped (the historical ``ci`` hole -- recognised resource, no
mapping, no evidence).

The set is asserted against the REAL key set rather than a hand-written list,
so adding a catalog resource without mapping it fails here instead of
degrading into a no-op ceiling.
"""

from __future__ import annotations

from security import security_gate as sg
from security.resource_catalog import RESOURCE_CATALOG
from security.security_gate import apply_workspace_ceiling


def test_every_ceiling_resource_has_a_session_mapping():
    """No recognised ceiling resource may be missing from the map."""
    missing = sorted(sg._WORKSPACE_CEILING_KEYS - set(sg._WORKSPACE_RESOURCE_MAP))
    assert missing == [], (
        f"workspace-ceiling resources with no session-side mapping: {missing}; "
        "a configured ceiling for these would be a SILENT no-op"
    )


def test_every_catalog_resource_is_mapped():
    """The map covers every canonical catalog resource (the ceiling surface is
    the catalog plus the legacy ``docker`` alias)."""
    assert set(sg._WORKSPACE_RESOURCE_MAP) >= set(RESOURCE_CATALOG)


def test_every_mapping_target_is_a_real_session_key():
    """Each mapping target must be a session-side catalog key, so a ceiling
    can never be routed onto a grain ``SessionPermissions`` does not carry."""
    targets = set(sg._WORKSPACE_RESOURCE_MAP.values())
    assert targets <= set(RESOURCE_CATALOG), sorted(targets - set(RESOURCE_CATALOG))


def test_ci_ceiling_is_capped_not_skipped(caplog):
    """Behavioural partner of the structural test: a configured ``ci`` ceiling
    now CAPS the session grant (``banned`` denies an otherwise-granted read)
    and does not warn about a missing mapping."""
    assert apply_workspace_ceiling({"ci": "banned"}, {"ci": "read"}) == {"ci": "banned"}
    assert apply_workspace_ceiling({"ci": "read"}, {"ci": "read"}) == {"ci": "read"}
    assert not [
        r for r in caplog.records if "SKIPPED" in r.message
    ], [r.message for r in caplog.records]
