"""RED-first test: the resolve_container_config disk-read failure must be
OBSERVABLE (a WARNING) and must FALL BACK to the caller's in-memory session
mirror rather than failing closed.

``security.security_gate.resolve_container_config(..., use_disk=True)`` reads
the session grants + workspace ceiling from the vault permission store.  When
that store read raises, the resolver must (a) emit a WARNING on the
``security.security_gate`` logger naming the workspace/session ids and the
exception type, and (b) return a config derived from the CALLER-SUPPLIED
session mirror -- NOT the fail-closed substitute (``network_mode="none"`` /
read-only).

This is the resolve_container_config companion to
``tests/test_vault_read_failure_warning.py`` (which pins the fail-CLOSED half
of the disk-mode contract on ``get_effective_permissions``).

The store is forced to raise by monkeypatching the gate's own
``_read_disk_permission_sources`` seam: this isolates "store failure ->
WARNING + caller mirror" from any filesystem-absence semantics.

Run:  pytest tests/test_resolve_container_config_fallback_warning.py -q
"""

import logging

import security.security_gate as gate
from security.security_gate import (
    ContainerConfig,
    SessionPermissions,
    WorkspaceCapabilities,
    get_effective_permissions,
)

_LOGGER_NAME = "security.security_gate"
_WORKSPACE_ID = "ws-fbwarn"
_SESSION_ID = "sess-fbwarn"
_MIRROR = {"network": "write", "filesystem": "write"}


class _VaultReadBoom(RuntimeError):
    """Distinct exception type so the log assertion can pin the type name."""


def _boom(*args, **kwargs):
    raise _VaultReadBoom("simulated vault permission store failure")


def test_disk_store_failure_warns_and_falls_back_to_mirror(monkeypatch, caplog):
    """Store read raises: WARNING names the ids AND the caller mirror wins."""

    monkeypatch.setattr(gate, "_read_disk_permission_sources", _boom)

    caplog.set_level(logging.WARNING, logger=_LOGGER_NAME)

    cfg = gate.resolve_container_config(
        _MIRROR,
        WorkspaceCapabilities.default(),
        "persistent",
        session_id=_SESSION_ID,
        workspace_id=_WORKSPACE_ID,
        use_disk=True,
    )

    # (b) The config is derived from the caller's mirror -- NOT fail-closed.
    assert isinstance(cfg, ContainerConfig)
    expected = get_effective_permissions(
        SessionPermissions(network="write", filesystem="write"),
        WorkspaceCapabilities.default(),
    )
    assert cfg.network_mode == "bridge"
    assert cfg.workspace_mode == "rw"
    assert dict(cfg.effective) == expected

    # (a) A WARNING was emitted naming the ids and the exception type.
    matching = [
        rec
        for rec in caplog.records
        if rec.name == _LOGGER_NAME and rec.levelno == logging.WARNING
    ]
    assert matching, "no WARNING emitted on the resolver fallback path"
    blob = " ".join(rec.getMessage() for rec in matching)
    assert "resolve_container_config" in blob
    assert "falling back" in blob
    assert _WORKSPACE_ID in blob
    assert _SESSION_ID in blob
    assert "_VaultReadBoom" in blob
