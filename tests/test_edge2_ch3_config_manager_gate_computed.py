"""Edge2 / Change3 — ``ConfigManager.resolve_effective_permissions`` is now
ALWAYS gate-computed.

``resolve_effective_permissions`` used to project the in-memory
``session_config.session_permissions`` dict directly whenever the
``session_id`` / ``workspace_id`` identifiers were unavailable (fail-OPEN).
It now mirrors the canonical gate-resolved computation in every case:

* when a ``session_id`` AND a ``workspace_id`` are both available, the vault
  permission store is authoritative — the security gate re-reads the session
  grants and the workspace ceiling and merges them with the workspace
  capabilities (failing CLOSED to a deny-all profile if the store is
  unreadable);
* when *either* identifier is missing the vault store cannot be consulted and
  there is no in-memory projection to fall back to, so the profile ALSO fails
  CLOSED (the gate's deny-all session + deny-all ceiling).

The in-memory ``session_config.session_permissions`` dict is NEVER consulted —
not even its ``workspace_id`` attribute.

These tests pin the **shape contract** (exactly the six canonical category
keys) and the **behaviour** (no-ids / partial-ids fail closed; disk fail-closed;
disk + ceiling cap; disk-authoritative precedence over a divergent in-memory
config) across all modes.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from web_ui.backend import config_manager as cm

CANONICAL_KEYS = {
    "container",
    "filesystem",
    "git",
    "host_bash",
    "mcp",
    "network",
}

DENY_ALL = {
    "container": False,
    "filesystem": "banned",
    "git": "banned",
    "host_bash": "banned",
    "mcp": "banned",
    "network": "banned",
}


class _FakeSessionConfig:
    """Minimal duck-typed stand-in for ``SessionConfig``."""

    def __init__(self, session_permissions=None, workspace_id=None):
        self.session_permissions = session_permissions or {}
        self.workspace_id = workspace_id


def _resolve(sc, session_id=None, workspace_id=None):
    return cm.ConfigManager.resolve_effective_permissions(
        sc, session_id, workspace_id
    )


# ── Mode A: missing / partial identifiers -> FAIL CLOSED ─────────────────────


def test_no_ids_fails_closed_even_with_permissive_in_memory_config():
    sc = _FakeSessionConfig(
        {"filesystem": "write", "network": "write", "git": "full"}
    )
    out = _resolve(sc)
    assert set(out.keys()) == CANONICAL_KEYS
    # the permissive in-memory dict is IGNORED: fail closed to deny-all
    assert out == DENY_ALL


def test_no_ids_empty_in_memory_config_fails_closed():
    out = _resolve(_FakeSessionConfig({}))
    assert set(out.keys()) == CANONICAL_KEYS
    assert out == DENY_ALL


def test_only_session_id_fails_closed(monkeypatch, tmp_path):
    """Supplying just ONE identifier must NOT engage disk mode and must NOT
    fall back to the in-memory projection — it fails CLOSED."""
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(tmp_path))
    sc = _FakeSessionConfig({"filesystem": "write", "network": "write"})
    out = _resolve(sc, session_id="sess-1")  # no workspace_id
    assert set(out.keys()) == CANONICAL_KEYS
    assert out == DENY_ALL


def test_workspace_id_is_not_derived_from_session_config(monkeypatch, tmp_path):
    """The old code derived ``workspace_id`` from
    ``session_config.workspace_id`` when the argument was omitted.  That hidden
    fallback is gone: with the argument omitted the profile fails CLOSED even
    though the config carries a workspace_id."""
    from thoughtmachine import permission_store as ps
    from thoughtmachine.vault import vault_root

    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(tmp_path))
    # a real, permissive store entry that WOULD resolve if disk mode engaged
    ps.write_session_permissions(
        vault_root(), "ws-d", "sess-d", {"filesystem": "write"}
    )
    _write_workspace(tmp_path, "ws-d", {"filesystem": "banned"})
    sc = _FakeSessionConfig({"filesystem": "write"}, workspace_id="ws-d")
    # note: workspace_id argument omitted (None) -> NOT derived -> fail closed
    out = _resolve(sc, session_id="sess-d")
    assert set(out.keys()) == CANONICAL_KEYS
    assert out == DENY_ALL


# ── Mode B: disk mode, unreadable/absent store -> fail CLOSED ────────────────


def test_disk_mode_missing_store_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(tmp_path))
    sc = _FakeSessionConfig({"filesystem": "write"}, workspace_id="ws-missing")
    out = _resolve(sc, session_id="sess-missing", workspace_id="ws-missing")
    assert set(out.keys()) == CANONICAL_KEYS
    # every category is denied, despite the permissive in-memory config
    assert out == DENY_ALL


# ── Mode C: disk mode, real store -> grants loaded, ceiling applied ──────────


def _write_workspace(tmp_path, workspace_id, ceiling):
    wdir = tmp_path / "workspaces" / workspace_id
    wdir.mkdir(parents=True, exist_ok=True)
    (wdir / "config.json").write_text(json.dumps({"permissions": ceiling}))


def test_disk_mode_reads_store_and_applies_ceiling(monkeypatch, tmp_path):
    """Disk-authoritative precedence: the vault grant/ceiling wins over a
    DIVERGENT in-memory ``session_config.session_permissions``."""
    from thoughtmachine import permission_store as ps
    from thoughtmachine.vault import vault_root

    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(tmp_path))
    ps.write_session_permissions(
        vault_root(),
        "ws-c",
        "sess-c",
        {"filesystem": "write", "git": "write", "network": "outbound"},
    )
    _write_workspace(
        tmp_path,
        "ws-c",
        {
            "filesystem": "read",
            "container": False,
            "git": "read",
            "network": "banned",
            "host_bash": "banned",
        },
    )
    # deliberately permissive in-memory config: disk must win
    sc = _FakeSessionConfig(
        {"filesystem": "write", "git": "write", "network": "outbound"},
        workspace_id="ws-c",
    )
    out = _resolve(sc, session_id="sess-c", workspace_id="ws-c")
    assert set(out.keys()) == CANONICAL_KEYS
    # ceiling caps filesystem/git down to "read" and network to "banned"
    assert out == {
        "container": False,
        "filesystem": "read",
        "git": "read",
        "host_bash": "banned",
        "mcp": "banned",
        "network": "banned",
    }
