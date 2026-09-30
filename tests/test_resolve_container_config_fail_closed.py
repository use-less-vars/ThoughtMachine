"""RED-first test: a resolve_container_config(``use_disk=True``) disk-read
failure must FAIL CLOSED.

``security.security_gate.resolve_container_config(..., use_disk=True)`` reads
the session grants + workspace ceiling from the vault permission store.  When
that store read raises, the resolver must (a) emit a WARNING on the
``security.security_gate`` logger naming the workspace/session ids and the
exception type, and (b) return the FAIL-CLOSED config -- the deny-all session
+ deny-all ceiling flow through the SAME merge below and yield
``network_mode="none"`` / ``workspace_mode="ro"`` with the all-banned 6-key
effective profile.  The caller-supplied in-memory session mirror must NOT be
consulted.

This is the resolve_container_config companion to
``tests/test_vault_read_failure_warning.py`` (which pins the fail-CLOSED half
of the disk-mode contract on ``get_effective_permissions``).

The store is forced to raise by monkeypatching the gate's own
``_read_disk_permission_sources`` seam: this isolates "store failure ->
WARNING + fail-closed config" from any filesystem-absence semantics.

Run:  pytest tests/test_resolve_container_config_fail_closed.py -q
"""

import logging

import security.security_gate as gate
from security.security_gate import (
    ContainerConfig,
    WorkspaceCapabilities,
    get_effective_permissions,
)

_LOGGER_NAME = "security.security_gate"
_WORKSPACE_ID = "ws-failclosed"
_SESSION_ID = "sess-failclosed"
_MIRROR = {"network": "write", "filesystem": "write"}

#: The deny-all 6-key effective profile a disk read failure must produce.
_ALL_BANNED = {
    "filesystem": "banned",
    "network": "banned",
    "container": False,
    "git": "banned",
    "mcp": "banned",
    "host_bash": "banned",
}


class _VaultReadBoom(RuntimeError):
    """Distinct exception type so the log assertion can pin the type name."""


def _boom(*args, **kwargs):
    raise _VaultReadBoom("simulated vault permission store failure")


def test_disk_store_failure_warns_and_fails_closed(monkeypatch, caplog):
    """Store read raises: WARNING names the ids AND the config fails closed."""

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

    # (b) The config is fail-closed -- the caller's mirror is IGNORED.
    assert isinstance(cfg, ContainerConfig)
    assert cfg.network_mode == "none"
    assert cfg.workspace_mode == "ro"
    assert dict(cfg.effective) == _ALL_BANNED
    expected = get_effective_permissions(
        gate._DISK_FAIL_CLOSED_SESSION,
        WorkspaceCapabilities.default(),
        gate._DISK_FAIL_CLOSED_CEILING,
    )
    assert dict(cfg.effective) == dict(expected)

    # (a) A WARNING was emitted naming the ids and the exception type.
    matching = [
        rec
        for rec in caplog.records
        if rec.name == _LOGGER_NAME and rec.levelno == logging.WARNING
    ]
    assert matching, "no WARNING emitted on the resolver fail-closed path"
    blob = " ".join(rec.getMessage() for rec in matching)
    assert "resolve_container_config" in blob
    assert _WORKSPACE_ID in blob
    assert _SESSION_ID in blob
    assert "_VaultReadBoom" in blob
