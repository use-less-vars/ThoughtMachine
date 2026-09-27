"""RED test: a disk-mode vault-read failure must NOT be silent.

``security.security_gate.get_effective_permissions`` in disk mode
(both ``session_id`` and ``workspace_id`` supplied, no explicit
``workspace_permissions``) reads the session grant profile and the
workspace ceiling from the vault permission store.  Any store error is
swallowed and fails CLOSED (deny-all session + deny-all ceiling).  That
swallow must emit a WARNING on the ``security.security_gate`` logger so
the failure is observable and not silently indistinguishable from an
honest all-banned grant set.

This pins the *observability* half of the disk-mode contract; the
fail-closed *outcome* is already covered in
``tests/test_security_gate_disk.py``.

Run:  pytest tests/test_vault_read_failure_warning.py -q
"""

import logging

import thoughtmachine.permission_store as permission_store
from security.security_gate import (
    SessionPermissions,
    WorkspaceCapabilities,
    get_effective_permissions,
)

_LOGGER_NAME = "security.security_gate"
_SESSION_ID = "sess-logfix"
_WORKSPACE_ID = "ws-logfix"


class _VaultReadBoom(RuntimeError):
    """Distinct exception type so the log assertion can pin the type name."""


def test_disk_vault_read_failure_logs_warning_and_fails_closed(
    hermetic_vault, monkeypatch, caplog
):
    """Vault-read failure: fail closed AND emit one WARNING naming the ids."""

    def _boom(*args, **kwargs):
        raise _VaultReadBoom("simulated vault read failure")

    monkeypatch.setattr(permission_store, "read_session_permissions", _boom)

    caplog.set_level(logging.WARNING, logger=_LOGGER_NAME)

    effective = get_effective_permissions(
        SessionPermissions(network="write", filesystem="write"),
        WorkspaceCapabilities.default(),
        session_id=_SESSION_ID,
        workspace_id=_WORKSPACE_ID,
    )

    # Outcome: fail closed — the deny-all session + deny-all ceiling flow
    # through the same merge, so the categories are all-banned (unchanged by
    # the fix).
    assert effective["network"] == "banned"
    assert effective["filesystem"] == "banned"

    # Observability: a WARNING was emitted naming the workspace/session and
    # the exception type.
    matching = [
        rec
        for rec in caplog.records
        if rec.name == _LOGGER_NAME and rec.levelno == logging.WARNING
    ]
    assert matching, "no WARNING emitted on the vault-read failure path"
    blob = " ".join(rec.getMessage() for rec in matching)
    assert _WORKSPACE_ID in blob
    assert _SESSION_ID in blob
    assert "_VaultReadBoom" in blob
