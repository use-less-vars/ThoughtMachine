"""
test_gate_catalog_migration.py — security_gate consuming resource_catalog.

Phase-2 migration contract (security/security_gate.py now consumes
``security/resource_catalog.py``):

* **Ceiling-vocabulary validation (fail-open):** ``apply_workspace_ceiling``
  recognises the canonical catalog resource keys (filesystem, git,
  container, network, mcp, host_bash) plus the legacy workspace alias
  ``docker`` (normalised onto ``container``).  A ceiling on an unknown
  resource or with an unknown level is logged at WARNING and ignored — the
  session value stands (forward-compatible workspace maps never break
  resolution).  The legacy ``git_read``/``git_write`` ceiling grains were
  removed in the permission-schema unification and are unknown resources
  now.
* **write_on_feature_branch is a first-class git level:** it ranks at
  write level in ``_min_permission``, ceiling comparisons AND
  ``_value_satisfies`` (Phase-3 helper edit: the outer ``git:write``
  category gate passes and the feature-branch-only restriction is enforced
  inside the git write tool at commit time), survives disk-mode coercion
  (catalog-valid), and satisfies ``git:read``.  The effective dict exposes
  the single ``git`` value — the ``git_read``/``git_write`` split exists
  only inside ``split_git_permission`` (used by the git tools), never as
  effective keys.
* **Worker footprints** capping git to ``read`` also cap a
  branch-write session git level down to ``read``.
* **Disk mode** (``hermetic_vault`` fixture) coerces stored grants through
  the catalog before constructing ``SessionPermissions``; a read ceiling
  caps a stored branch-write grant.

The ``hermetic_vault`` fixture (tests/conftest.py) monkeypatches
``thoughtmachine.vault.vault_root()`` to a temp vault, which the gate's
lazy ``import thoughtmachine.vault`` picks up (same module object).
"""

import json
import logging

import pytest

from thoughtmachine.permission_store import write_session_permissions
from thoughtmachine.security import SessionPermissions
from thoughtmachine.workspace_capabilities import WorkspaceCapabilities
from security.security_gate import (
    _min_permission,
    apply_workspace_ceiling,
    check_atomic_operation,
    check_required_categories,
    get_effective_permissions,
    split_git_permission,
)

# Fully-permissive workspace capabilities (all-True defaults) — used so the
# merge below never downgrades beyond what the ceiling/grants dictate.
_FULL_CAPS = WorkspaceCapabilities()


def _write_config(vault, ws_id, permissions):
    """Write a workspace config.json with an explicit permission ceiling."""
    ws_dir = vault / "workspaces" / ws_id
    ws_dir.mkdir(parents=True, exist_ok=True)
    (ws_dir / "config.json").write_text(
        json.dumps({"purpose": "coding", "permissions": permissions})
    )


# ── Ceiling-vocabulary validation (E4): fail-open warnings ────────────────


def test_ceiling_unknown_resource_warns_fail_open(caplog):
    """A ceiling key outside RESOURCE_CATALOG + legacy grains is logged and
    ignored (fail-open): the session value stands."""
    with caplog.at_level(logging.WARNING):
        result = apply_workspace_ceiling(
            {"mystery_resource": "banned"}, {"filesystem": "write"}
        )
    assert result == {"filesystem": "write"}
    assert any(
        "ignoring workspace ceiling for unknown resource" in r.message
        for r in caplog.records
    )


def test_ceiling_unknown_level_warns_fail_open(caplog):
    """An unknown ceiling level is logged and ignored (fail-open)."""
    with caplog.at_level(logging.WARNING):
        result = apply_workspace_ceiling(
            {"filesystem": "mega"}, {"filesystem": "write"}
        )
    assert result == {"filesystem": "write"}
    assert any(
        "ignoring workspace ceiling level" in r.message for r in caplog.records
    )


def test_catalog_ceiling_keys_recognised_no_warning(caplog):
    """Every canonical catalog key + the legacy ``docker`` alias is a
    recognised ceiling resource: applying them emits no unknown-resource
    warning.  (``git_read``/``git_write`` were removed in the
    permission-schema unification and are no longer recognised.)"""
    ceiling = {
        "git": "read",
        "filesystem": "read",
        "container": "banned",
        "network": "banned",
        "mcp": "banned",
        "host_bash": "banned",
        "docker": "banned",  # legacy alias normalised onto container
    }
    session = {
        "git": "write",
        "filesystem": "write",
        "container": True,
        "network": "write",
        "mcp": "connect",
        "host_bash": "allow",
    }
    with caplog.at_level(logging.WARNING):
        result = apply_workspace_ceiling(ceiling, session)
    assert not any(
        "ignoring workspace ceiling" in r.message for r in caplog.records
    )
    assert result["git"] == "read"
    assert result["filesystem"] == "read"
    assert result["container"] is False
    assert result["network"] == "banned"
    assert result["mcp"] == "banned"
    assert result["host_bash"] == "banned"
    assert set(result) == {"git", "filesystem", "container", "network", "mcp", "host_bash"}


# ── host_bash ceiling semantics (banned < ask < allow) ─────────────────────


def test_host_bash_ceiling_allow_keeps_ask_grant():
    """An 'allow' host_bash ceiling never caps a lower session grant: an
    'ask' session grant stands (banned < ask < allow)."""
    assert apply_workspace_ceiling(
        {"host_bash": "allow"}, {"host_bash": "ask"}
    ) == {"host_bash": "ask"}


def test_host_bash_ceiling_ask_keeps_banned_grant():
    """An 'ask' host_bash ceiling leaves an already-banned session grant
    untouched (banned is below the ceiling)."""
    assert apply_workspace_ceiling(
        {"host_bash": "ask"}, {"host_bash": "banned"}
    ) == {"host_bash": "banned"}


def test_host_bash_ceiling_allow_without_grant_stays_banned():
    """A host_bash ceiling never fabricates a grant: with no session value
    the key stays absent after the ceiling pass, and the effective profile
    resolves to the pydantic default 'banned'."""
    assert apply_workspace_ceiling({"host_bash": "allow"}, {}) == {}
    eff = get_effective_permissions(
        SessionPermissions(), _FULL_CAPS, {"host_bash": "allow"}
    )
    assert eff["host_bash"] == "banned"


def test_host_bash_grant_allow_without_ceiling_passes():
    """With no host_bash ceiling, an 'allow' session grant stands both in
    the raw ceiling pass and in the effective profile."""
    assert apply_workspace_ceiling({}, {"host_bash": "allow"}) == {
        "host_bash": "allow"
    }
    eff = get_effective_permissions(
        SessionPermissions(host_bash="allow"), _FULL_CAPS
    )
    assert eff["host_bash"] == "allow"


def test_default_session_effective_contains_host_bash_banned():
    """A default session carries host_bash 'banned' in its effective
    profile (safe pydantic default: no accidental host-shell grant)."""
    eff = get_effective_permissions(SessionPermissions(), _FULL_CAPS)
    assert eff["host_bash"] == "banned"


def test_disk_mode_host_bash_grant_capped_by_ceiling(hermetic_vault):
    """Disk mode: a stored 'ask' host_bash grant survives an 'allow'
    ceiling and is capped to 'banned' by a 'banned' ceiling (control)."""
    ws_id, sid = "ws-a", "sess-1"
    write_session_permissions(hermetic_vault, ws_id, sid, {"host_bash": "ask"})
    _write_config(hermetic_vault, ws_id, {"host_bash": "allow"})
    eff = get_effective_permissions(
        SessionPermissions(),  # ignored in disk mode
        _FULL_CAPS,
        session_id=sid,
        workspace_id=ws_id,
    )
    assert eff["host_bash"] == "ask"

    _write_config(hermetic_vault, ws_id, {"host_bash": "banned"})
    eff2 = get_effective_permissions(
        SessionPermissions(),  # ignored in disk mode
        _FULL_CAPS,
        session_id=sid,
        workspace_id=ws_id,
    )
    assert eff2["host_bash"] == "banned"


# ── write_on_feature_branch vs ceilings (E3/E4 gap #2 fix) ───────────────


def test_ceiling_read_caps_branch_write_session():
    """gap #2 fix: a read/banned git ceiling caps a write_on_feature_branch
    session git level down to the ceiling."""
    assert apply_workspace_ceiling(
        {"git": "read"}, {"git": "write_on_feature_branch"}
    ) == {"git": "read"}
    assert apply_workspace_ceiling(
        {"git": "banned"}, {"git": "write_on_feature_branch"}
    ) == {"git": "banned"}


def test_ceiling_write_keeps_branch_write():
    """A write-level git ceiling leaves write_on_feature_branch standing
    (both rank at write level; equal rank keeps the session value)."""
    assert apply_workspace_ceiling(
        {"git": "write"}, {"git": "write_on_feature_branch"}
    ) == {"git": "write_on_feature_branch"}


# ── split_git_permission sub-grains (E5) ─────────────────────────────────


def test_split_git_permission_branch_write():
    """write_on_feature_branch splits into git_read 'read' + git_write
    'write_on_feature_branch' (case-insensitive); unknown levels still
    fail closed to (False, False)."""
    assert split_git_permission("write_on_feature_branch") == (
        "read",
        "write_on_feature_branch",
    )
    assert split_git_permission("WRITE_ON_FEATURE_BRANCH") == (
        "read",
        "write_on_feature_branch",
    )
    assert split_git_permission("write_on_feature_branch")[0] == "read"
    assert split_git_permission("unknown_level") == (False, False)


# ── Effective permissions for a branch-write session ─────────────────────


def _branch_write_eff():
    return get_effective_permissions(
        SessionPermissions(git="write_on_feature_branch"), _FULL_CAPS
    )


def test_effective_branch_write_session_grains():
    """A branch-write session yields git='write_on_feature_branch' as the
    single canonical git category value.  The effective dict exposes only
    the six canonical keys — the derived git_read/git_write split lives in
    ``split_git_permission`` (consumed by the git tools), never as
    effective keys."""
    eff = _branch_write_eff()
    assert eff["git"] == "write_on_feature_branch"
    assert split_git_permission(eff["git"]) == (
        "read",
        "write_on_feature_branch",
    )
    assert set(eff) == {"filesystem", "network", "container", "git", "mcp", "host_bash"}


def test_outer_gate_branch_write_allows_write_and_read():
    """Phase-3 helper edit: 'write_on_feature_branch' ranks at the write
    level in _value_satisfies — git:read AND git:write pass the outer gate
    (the tool-side commit gate enforces the branch restriction)."""
    eff = _branch_write_eff()
    assert check_atomic_operation("git:read", eff, "GitReadTool") is True
    assert check_atomic_operation("git:write", eff, "GitWriteTool") is True

    ok, msg = check_required_categories(
        ["git:read"], dict(eff), "GitReadTool", {}, "read git", event_bus=None
    )
    assert ok is True and msg == ""
    ok, msg = check_required_categories(
        ["git:write"], dict(eff), "GitWriteTool", {}, "write git", event_bus=None
    )
    assert ok is True and msg == ""


def test_worker_footprint_read_caps_branch_write():
    """E6: _min_permission ranks write_on_feature_branch at write level, so a
    worker footprint of git='read' caps a branch-write session git level down
    to read (git:read allowed, git:write denied after the footprint)."""
    assert _min_permission("write_on_feature_branch", "read") == "read"

    eff = _branch_write_eff()
    ok, _ = check_required_categories(
        ["git:read"], dict(eff), "GitReadTool", {}, "read git",
        event_bus=None, permission_footprint={"git": "read"},
    )
    assert ok is True
    denied, deny_msg = check_required_categories(
        ["git:write"], dict(eff), "GitWriteTool", {}, "write git",
        event_bus=None, permission_footprint={"git": "read"},
    )
    assert denied is False
    assert "git:write" in deny_msg


# ── Disk mode (E7): catalog coercion at the gate boundary ────────────────


def test_disk_mode_branch_write_grant_capped_by_ceiling(hermetic_vault):
    """A stored write_on_feature_branch grant is catalog-valid (survives
    coercion); a read ceiling caps the merged git level to read.  A write
    ceiling leaves the branch-write grant standing.  The effective dict
    always exposes the single canonical ``git`` value."""
    ws_id, sid = "ws-a", "sess-1"
    write_session_permissions(
        hermetic_vault, ws_id, sid, {"git": "write_on_feature_branch"}
    )
    _write_config(hermetic_vault, ws_id, {"git": "read"})

    eff = get_effective_permissions(
        SessionPermissions(),  # ignored in disk mode
        _FULL_CAPS,
        session_id=sid,
        workspace_id=ws_id,
    )
    assert eff["git"] == "read"
    assert set(eff) == {"filesystem", "network", "container", "git", "mcp", "host_bash"}

    # Control: a write-level ceiling leaves the branch-write grant standing.
    _write_config(hermetic_vault, ws_id, {"git": "write"})
    eff2 = get_effective_permissions(
        SessionPermissions(),  # ignored in disk mode
        _FULL_CAPS,
        session_id=sid,
        workspace_id=ws_id,
    )
    assert eff2["git"] == "write_on_feature_branch"
    assert split_git_permission(eff2["git"]) == (
        "read",
        "write_on_feature_branch",
    )


def test_disk_mode_non_catalog_grant_keys_dropped(
    hermetic_vault, monkeypatch, caplog
):
    """E7 belt-and-braces: even if the store ever returned a non-catalog
    grant key (system / stray), the gate's coerce drops it and the pydantic
    defaults stand — a non-catalog key never reaches SessionPermissions."""
    import thoughtmachine.permission_store

    ws_id, sid = "ws-a", "sess-1"
    _write_config(hermetic_vault, ws_id, {"git": "read"})

    monkeypatch.setattr(
        thoughtmachine.permission_store,
        "read_session_permissions",
        lambda vault_root, ws, s: {"git": "read", "system": "write", "stray": "full"},
    )

    with caplog.at_level(logging.WARNING):
        eff = get_effective_permissions(
            SessionPermissions(),  # ignored in disk mode
            _FULL_CAPS,
            session_id=sid,
            workspace_id=ws_id,
        )
    # The effective dict carries only the six canonical keys; the dropped
    # system/stray grants leave the pydantic defaults standing.
    assert eff == {
        "filesystem": "read",  # session default
        "network": "banned",  # session default
        "container": False,  # session default
        "git": "read",
        "mcp": "banned",  # session default
        "host_bash": "banned",  # session default
    }
    assert "system" not in eff
    assert "execution" not in eff
    assert any(
        "dropping unknown session permission key" in r.message
        for r in caplog.records
    )
