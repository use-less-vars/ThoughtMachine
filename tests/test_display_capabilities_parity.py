"""Display/gate capability parity -- the human-visible surfaces must never
out-claim the profile the gate actually enforces.

The enforcement path resolves workspace capabilities through
``security.security_gate.get_workspace_capabilities``, which FAILS CLOSED to
the deny-all sentinel (``filesystem_write`` / ``allow_docker`` /
``allow_network`` / ``git_available`` all ``False``) when
``workspaces/<id>/capabilities.json`` is missing or unparsable.

Three display surfaces used to bypass that resolver and fall back to
``WorkspaceCapabilities.default()`` -- the fully permissive profile
(``True`` on all four enforcing booleans) -- whenever the loader returned
``None``:

* ``GET /api/workspace/{ws_id}/summary``                      -> ``capabilities``
* ``GET /api/workspace/{ws_id}/effective_permissions``         -> ``effective_permissions``
* ``GET /api/sessions/{session_id}/permissions``               -> ``effective``

So a workspace with NO capabilities file displayed "filesystem write / docker
allowed / network allowed / git available" while the gate enforced "deny all"
on exactly those four axes: the UI out-claimed the enforcement layer in the
over-permissive direction.  These tests pin the two surfaces to the gate's own
answer, for a missing file AND for a corrupt file, and keep the opposite
direction covered: when a capabilities file IS saved, the display reports
exactly what it says (the fail-closed value is not hardcoded).

Fixture style follows ``tests/test_workspace_summary.py``: a hermetic vault
under ``tmp_path`` (``THOUGHTMACHINE_VAULT_ROOT``) so the real vault is never
touched.
"""

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

# Add project root so that imports work (same pattern as sibling tests).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from security.security_gate import get_workspace_capabilities  # noqa: E402
from thoughtmachine.workspace_capabilities import WorkspaceCapabilities  # noqa: E402

from web_ui.backend import session_routes, workspace_routes  # noqa: E402
from web_ui.backend.session_routes import (  # noqa: E402
    _compute_effective_session_permissions as _effective,
)

WS_ID = "ws-parity"
SESSION_ID = "sess-parity"

# The gate's fail-closed sentinel, spelled out: NOT the same object as
# ``WorkspaceCapabilities.default()`` -- the four enforcing booleans are its
# exact negation.
FAIL_CLOSED_CAPS = {
    "allowed_tools": [],
    "blocked_tools": [],
    "allowed_providers": [],
    "max_context_length": 0,
    "max_conversation_turns": 0,
    "allowed_file_extensions": [],
    "max_file_size_bytes": 0,
    "allow_network": False,
    "allow_docker": False,
    "filesystem_write": False,
    "git_available": False,
    "allowed_workspace_dirs": ["."],
    "extra": {},
}

# A saved capabilities file that GRANTS everything: the display must honour it
# (the fail-closed value above must not be hardcoded).
PERMISSIVE_CAPS_FILE = {
    "allow_network": True,
    "allow_docker": True,
    "filesystem_write": True,
    "git_available": True,
}


# ── helpers ─────────────────────────────────────────────────────────────────


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _vault(tmp_path, monkeypatch) -> Path:
    """Point the vault root at ``tmp_path/vault`` (hermetic)."""
    vault = tmp_path / "vault"
    monkeypatch.setenv("THOUGHTMACHINE_VAULT_ROOT", str(vault))
    return vault


def _ws_dir(vault: Path) -> Path:
    return Path(vault) / "workspaces" / WS_ID


def _write_capabilities(vault: Path, payload, *, corrupt: bool = False) -> Path:
    path = _ws_dir(vault) / "capabilities.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if corrupt:
        path.write_text("{ not json !!!", encoding="utf-8")
    else:
        path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _write_grants_sidecar(vault: Path, grants) -> Path:
    path = _ws_dir(vault) / "sessions" / SESSION_ID / "permissions.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(grants), encoding="utf-8")
    return path


def _patch_summary_deps(monkeypatch, vault: Path):
    """Patch the summary's non-capability dependencies onto *vault* / fakes
    (mirrors ``tests/test_workspace_summary.py::_patch_summary_deps``).

    Capabilities are deliberately NOT patched: they are the subject here.
    """
    entry = SimpleNamespace(id=WS_ID, label="L", root_path="/tmp/ws-root")
    fake_registry = SimpleNamespace(
        get_workspace=lambda wid: entry if wid == WS_ID else None
    )
    monkeypatch.setattr(
        workspace_routes.WorkspaceRegistry,
        "get_default",
        classmethod(lambda cls: fake_registry),
    )
    monkeypatch.setattr(workspace_routes, "ensure_workspace_dirs", lambda ws_id: None)

    import web_ui.backend.config_manager as config_manager

    monkeypatch.setattr(
        config_manager, "_load_workspace_permission_ceiling", lambda ws: {}
    )

    import session.session_registry as sreg

    fake_sessions = SimpleNamespace(get_all=lambda: {})
    monkeypatch.setattr(
        sreg.SessionRegistry, "get_default", classmethod(lambda cls: fake_sessions)
    )

    monkeypatch.setattr(workspace_routes, "_collect_active_workers", lambda ws_id: [])
    monkeypatch.setattr(workspace_routes, "_containers_for_workspace", lambda entry: [])
    monkeypatch.setattr(
        workspace_routes, "_containers_listed_for_workspace", lambda entry: 0
    )

    import session.tool_presets as tool_presets

    monkeypatch.setattr(tool_presets, "_ALL_TOOLS", [])

    catalog_path = Path(vault).parent / "resource_catalog.json"
    monkeypatch.setattr(workspace_routes, "_RESOURCE_CATALOG_PATH", catalog_path)


def _summary(monkeypatch, vault: Path, *, corrupt: bool = False,
             payload=None):
    _patch_summary_deps(monkeypatch, vault)
    _write_json(_ws_dir(vault) / "config.json", {"purpose": "general"})
    if corrupt:
        _write_capabilities(vault, None, corrupt=True)
    elif payload is not None:
        _write_capabilities(vault, payload)
    return asyncio.run(workspace_routes.get_workspace_overview(WS_ID))


# ── GET /api/workspace/{ws_id}/summary -> capabilities ──────────────────────


def test_summary_missing_capabilities_file_is_fail_closed(tmp_path, monkeypatch):
    """No capabilities.json: the summary reports the gate's deny-all profile,
    never ``WorkspaceCapabilities.default()``."""
    vault = _vault(tmp_path, monkeypatch)

    result = _summary(monkeypatch, vault)

    gate = get_workspace_capabilities(WS_ID)
    assert result["capabilities"] == gate.to_dict()
    assert result["capabilities"] == FAIL_CLOSED_CAPS
    assert result["capabilities"] != WorkspaceCapabilities.default().to_dict(), (
        "summary out-claims the gate: it reported the permissive default for a "
        "workspace whose capabilities file is absent"
    )
    for flag in ("filesystem_write", "allow_docker", "allow_network",
                 "git_available"):
        assert result["capabilities"][flag] is False, flag


def test_summary_corrupt_capabilities_file_is_fail_closed(tmp_path, monkeypatch):
    """An unparsable capabilities.json fails closed exactly like a missing one."""
    vault = _vault(tmp_path, monkeypatch)

    result = _summary(monkeypatch, vault, corrupt=True)

    assert result["capabilities"] == get_workspace_capabilities(WS_ID).to_dict()
    assert result["capabilities"] == FAIL_CLOSED_CAPS
    assert result["capabilities"] != WorkspaceCapabilities.default().to_dict()


def test_summary_reports_saved_capabilities_file(tmp_path, monkeypatch):
    """Opposite direction: a saved permissive file is reported as-is (the
    fail-closed profile above is a fallback, not a hardcoded answer)."""
    vault = _vault(tmp_path, monkeypatch)

    result = _summary(monkeypatch, vault, payload=PERMISSIVE_CAPS_FILE)

    assert result["capabilities"] == get_workspace_capabilities(WS_ID).to_dict()
    assert result["capabilities"] == WorkspaceCapabilities.default().to_dict()
    for flag in ("filesystem_write", "allow_docker", "allow_network",
                 "git_available"):
        assert result["capabilities"][flag] is True, flag


# ── GET /api/workspace/{ws_id}/effective_permissions ────────────────────────

# ``git_available=False`` bans git and ``allow_network=False`` bans network.
# A capability deny on a LEVEL axis is spelled in that axis's own vocabulary
# ("banned"), never as a boolean False; ``container`` is a genuine boolean
# axis, so its deny stays False.  (With the permissive default both axes
# would read "read" / "banned" -- i.e. a WIDER profile than the gate
# enforces.)
EFFECTIVE_FAIL_CLOSED = {
    "filesystem": "read",
    "network": "banned",
    "container": False,
    "git": "banned",
    "mcp": "banned",
    "host_bash": "banned",
    "ci": "banned",
    "visual": "banned",
    "credential": "banned",
}


def test_effective_permissions_missing_capabilities_file_is_fail_closed(
        tmp_path, monkeypatch):
    """The effective-permissions display mirrors the gate's capability merge."""
    vault = _vault(tmp_path, monkeypatch)
    monkeypatch.setattr(workspace_routes, "ensure_workspace_dirs", lambda ws_id: None)
    _write_json(_ws_dir(vault) / "config.json", {"purpose": "general"})

    result = asyncio.run(
        workspace_routes.get_effective_permissions(WS_ID, session_id=None)
    )

    assert result["effective_permissions"] == EFFECTIVE_FAIL_CLOSED
    assert result["effective_permissions"]["git"] == "banned"
    assert result["effective_permissions"]["network"] == "banned"


def test_effective_permissions_reports_saved_capabilities_file(
        tmp_path, monkeypatch):
    """A saved permissive file widens the display again (no hardcoded deny)."""
    vault = _vault(tmp_path, monkeypatch)
    monkeypatch.setattr(workspace_routes, "ensure_workspace_dirs", lambda ws_id: None)
    _write_json(_ws_dir(vault) / "config.json", {"purpose": "general"})
    _write_capabilities(vault, PERMISSIVE_CAPS_FILE)

    result = asyncio.run(
        workspace_routes.get_effective_permissions(WS_ID, session_id=None)
    )

    assert result["effective_permissions"]["git"] == "read"
    assert result["effective_permissions"]["network"] == "banned"


# ── GET /api/sessions/{session_id}/permissions ──────────────────────────────

# Disk mode with a readable grants sidecar (filesystem/git = write) and a
# permissive ceiling: with the permissive capability default the helper
# reported "write"/"write"; the gate enforces read/banned because the workspace
# has no capabilities file.
SESSION_FAIL_CLOSED = {
    "filesystem": "read",
    "network": "banned",
    "container": False,
    "git": "banned",
    "mcp": "banned",
    "host_bash": "banned",
    "ci": "banned",
    "visual": "banned",
    "credential": "banned",
}


def _session_store(vault: Path):
    _write_json(
        _ws_dir(vault) / "config.json",
        {"purpose": "coding", "permissions": {"filesystem": "write", "git": "write"}},
    )
    _write_grants_sidecar(vault, {"filesystem": "write", "git": "write"})


def test_session_permissions_missing_capabilities_file_is_fail_closed(
        tmp_path, monkeypatch):
    """The session-permissions display cannot grant what the gate denies."""
    vault = _vault(tmp_path, monkeypatch)
    _session_store(vault)

    effective = dict(_effective(WS_ID, SESSION_ID))

    assert effective == SESSION_FAIL_CLOSED
    # The pre-fix over-claim, stated explicitly:
    assert effective["filesystem"] == "read"
    assert effective["git"] == "banned"


def test_session_permissions_reports_saved_capabilities_file(tmp_path, monkeypatch):
    """A saved permissive capabilities file restores the wider profile."""
    vault = _vault(tmp_path, monkeypatch)
    _session_store(vault)
    _write_capabilities(vault, PERMISSIVE_CAPS_FILE)

    effective = dict(_effective(WS_ID, SESSION_ID))

    assert effective["filesystem"] == "write"
    assert effective["git"] == "write"


# ── the two constants themselves ────────────────────────────────────────────


def test_default_is_not_the_fail_closed_profile():
    """Guard rail for the tests above: ``default()`` really is the antipode of
    the gate's fail-closed sentinel on the four enforcing booleans."""
    gate = get_workspace_capabilities("ws-never-exists")
    default = WorkspaceCapabilities.default()
    for flag in ("filesystem_write", "allow_docker", "allow_network",
                 "git_available"):
        assert getattr(gate, flag) is False, flag
        assert getattr(default, flag) is True, flag
