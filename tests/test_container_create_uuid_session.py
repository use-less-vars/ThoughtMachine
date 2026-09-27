"""RED-first test: a container create path for a UUID workspace_id / session_id
with NO vault sidecar must fall back to the caller-supplied in-memory session
mirror, NOT fail closed to ``"none"``.

Bug: ``bug/container-create-uuid-ids-fail-closed``.

The create path threads UUID ids into
``security.security_gate.resolve_container_config``.  Its disk read
path-joins the raw id into the vault path
(``thoughtmachine.permission_store.session_grants_path``), which raises
``TypeError`` for a ``uuid.UUID`` (``PosixPath / UUID`` is unsupported).  The
pre-fix resolver treated that error as an unreadable store and failed CLOSED,
overriding the caller's already-correct mirror and locking the container down
to ``network_mode="none"`` / read-only.

The disk read is now caller-gated (``use_disk=True`` opt-in): a store read
error is observable (a WARNING naming the ids) and the resolver FALLS BACK to
the caller's mirror.

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


def test_uuid_ids_without_sidecar_falls_back_to_mirror(
    permissive_caps, hermetic_vault
):
    # UUID ids that the vault store path cannot express as a path component;
    # NO sidecar is written for them, so the disk read raises TypeError.
    workspace_id = uuid.uuid4()
    session_id = uuid.uuid4()

    mode = _resolve_network_mode_via_gate(
        workspace_id,
        {"network": "write", "filesystem": "write"},
        session_id=session_id,
    )

    # The caller's mirror must win on a store-read failure.
    # Pre-fix: the resolver failed CLOSED over the mirror -> "none" (RED).
    # Post-fix: it falls back to the mirror -> "bridge" (GREEN).
    assert mode == "bridge"
