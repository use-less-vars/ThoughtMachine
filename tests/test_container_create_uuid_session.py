"""RED-first test: a container create path for a UUID workspace_id / session_id
with NO vault sidecar must FAIL CLOSED to ``network_mode="none"``.

Bug: ``bug/container-create-uuid-ids-fail-closed``.

The create path threads UUID ids into
``security.security_gate.resolve_container_config`` with ``use_disk=True``.
Its disk read path-joins the raw id into the vault path
(``thoughtmachine.permission_store``), which now coerces the id to ``str``
(``_coerce_id``); with NO sidecar and no matching session record the store
raises ``PermissionStoreError`` (unknown session: deny, do not default).

The resolver treats any store read failure as fail-CLOSED: it substitutes the
deny-all session + deny-all ceiling, so the caller's in-memory mirror is NOT
used and the container is locked down to ``network_mode="none"`` / read-only
(``workspace_mode="ro"``).  A store read error stays observable (a WARNING
naming the ids).

Run:  pytest tests/test_container_create_uuid_session.py -q
"""

import os
import sys
import uuid

import pytest

# Make the repository root importable when running this file directly.
_SRC_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _SRC_ROOT not in sys.path:
    sys.path.insert(0, _SRC_ROOT)

from infra.container_registry import _resolve_network_mode_via_gate  # noqa: E402


@pytest.fixture
def permissive_caps(monkeypatch):
    """Permissive capabilities so the ONLY variable under test is the
    session-grant source (disk read failure vs caller mirror)."""
    from security.security_gate import WorkspaceCapabilities

    monkeypatch.setattr(
        "security.security_gate.get_workspace_capabilities",
        lambda workspace_id=None: WorkspaceCapabilities.default(),
    )


def test_uuid_ids_without_sidecar_fail_closed(permissive_caps, hermetic_vault):
    # UUID ids the vault store has no sidecar (nor matching session record)
    # for, so the disk read raises PermissionStoreError (unknown session).
    workspace_id = uuid.uuid4()
    session_id = uuid.uuid4()

    mode = _resolve_network_mode_via_gate(
        workspace_id,
        {"network": "write", "filesystem": "write"},
        session_id=session_id,
    )

    # A store-read failure must FAIL CLOSED -- the caller's mirror is ignored.
    # Pre-fix: the resolver fell back to the mirror -> "bridge" (RED).
    # Post-fix: it fails closed to the deny-all profile -> "none" (GREEN).
    assert mode == "none"
