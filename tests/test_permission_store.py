"""
Hermetic unit tests for the disk-pure session permission store
(thoughtmachine/permission_store.py).

The store is the storage half of the permission-simplification effort:
per-session grants live in a sidecar in canonical resource-catalog shape
(``security/resource_catalog.py``; legacy grains are migrated on write and
read)
``<vault_root>/workspaces/<ws_id>/sessions/<sid>/permissions.json``, with a
legacy fallback to ``metadata.session_config.session_permissions`` inside the
session record (located by content scan), and the workspace ceiling is read
from ``config.json['permissions']``.

The vault layout is built directly under ``tmp_path`` and every call passes
``vault_root=tmp_path`` explicitly, so the tests never touch the real vault.
"""

import json
import sys
from pathlib import Path

# Add project root so that imports work (same pattern as sibling tests).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402  (import order forced by the sys.path shim)

import thoughtmachine.permission_store as store_module  # noqa: E402
from thoughtmachine.permission_store import (  # noqa: E402
    PermissionStoreError,
    migrate_session_permissions,
    read_session_permissions,
    session_grants_path,
    workspace_ceiling,
    write_session_permissions,
)
from thoughtmachine.security import SessionPermissions  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers: build a hermetic vault layout under tmp_path
# ---------------------------------------------------------------------------


def _sessions_dir(vault_root, ws_id="ws-a") -> Path:
    d = Path(vault_root) / "workspaces" / ws_id / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_config(vault_root, ws_id="ws-a", permissions=None, *, corrupt=False):
    cfg = Path(vault_root) / "workspaces" / ws_id / "config.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    if corrupt:
        cfg.write_text("{ not json !!!", encoding="utf-8")
        return cfg
    payload = {"purpose": "coding"}
    if permissions is not None:
        payload["permissions"] = permissions
    cfg.write_text(json.dumps(payload), encoding="utf-8")
    return cfg


def _write_session_record(vault_root, ws_id, session_id, legacy=None, *, name="my-session"):
    """Friendly-named session JSON record (content-scan target), mirroring
    the shape FileSystemSessionStore.save_session produces."""
    record = _sessions_dir(vault_root, ws_id) / f"{name}_ab12cd.json"
    data = {"session_id": session_id, "name": name, "metadata": {}}
    if legacy is not None:
        data["metadata"]["session_config"] = {"session_permissions": dict(legacy)}
    record.write_text(json.dumps(data), encoding="utf-8")
    return record


# ---------------------------------------------------------------------------
# Read path: sidecar round-trips
# ---------------------------------------------------------------------------


def test_sidecar_read_round_trip_sparse_dict(tmp_path):
    """Raw sparse dict (D1 RED-test shape) is stored verbatim and read back."""
    vault = tmp_path / "vault"
    grants = {"filesystem": "read", "git": "write"}
    target = write_session_permissions(vault, "ws-a", "sess-1", grants)

    assert target == session_grants_path(vault, "ws-a", "sess-1")
    assert target.is_file()
    assert json.loads(target.read_text(encoding="utf-8")) == grants
    assert read_session_permissions(vault, "ws-a", "sess-1") == grants


def test_raw_dict_stored_normalized_to_canonical_shape(tmp_path):
    """Legacy optional grains are normalised on write: with ``git`` present,
    ``git_read`` is dropped and only canonical keys survive."""
    vault = tmp_path / "vault"
    grants = {
        "container": True,
        "filesystem": "write",
        "git": "read",
        "git_read": "read",
    }
    write_session_permissions(vault, "ws-a", "sess-1", grants)
    assert read_session_permissions(vault, "ws-a", "sess-1") == {
        "container": True,
        "filesystem": "write",
        "git": "read",
    }


def test_session_permissions_object_input_round_trip(tmp_path):
    """A SessionPermissions instance is stored in canonical shape: the
    non-catalog keys (system/execution) and None-valued legacy git grains
    are dropped, while the container bool and valid session-grant grains
    survive -- including the network/mcp defaults, now storable grants."""
    vault = tmp_path / "vault"
    sp = SessionPermissions(filesystem="read", git="write")
    target = write_session_permissions(vault, "ws-a", "sess-1", sp)

    expected = {
        "container": False,
        "filesystem": "read",
        "git": "write",
        "network": "banned",
        "mcp": "banned",
    }
    assert json.loads(target.read_text(encoding="utf-8")) == expected
    assert read_session_permissions(vault, "ws-a", "sess-1") == expected


def test_invalid_permissions_type_rejected(tmp_path):
    """Non-dict, non-SessionPermissions input fails closed before any write."""
    vault = tmp_path / "vault"
    with pytest.raises(PermissionStoreError):
        write_session_permissions(vault, "ws-a", "sess-1", "filesystem:write")


# ---------------------------------------------------------------------------
# Read path: legacy metadata fallback
# ---------------------------------------------------------------------------


def test_legacy_fallback_without_sidecar(tmp_path):
    """No sidecar -> legacy metadata.session_config.session_permissions."""
    vault = tmp_path / "vault"
    legacy = {"filesystem": "read", "git": "write"}
    _write_session_record(vault, "ws-a", "sess-1", legacy=legacy)

    assert not session_grants_path(vault, "ws-a", "sess-1").exists()
    assert read_session_permissions(vault, "ws-a", "sess-1") == legacy


def test_record_without_legacy_grants_returns_empty(tmp_path):
    """A matching session record with no recorded grants reads as {} (neutral:
    grants nothing; the gate applies defaults afterwards)."""
    vault = tmp_path / "vault"
    record = _write_session_record(vault, "ws-a", "sess-1", legacy=None)
    assert read_session_permissions(vault, "ws-a", "sess-1") == {}

    # Record without any metadata at all -> also {}.
    bare = _sessions_dir(vault, "ws-a") / "bare_000001.json"
    bare.write_text(json.dumps({"session_id": "sess-2", "name": "bare"}), encoding="utf-8")
    assert read_session_permissions(vault, "ws-a", "sess-2") == {}


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


def test_migrate_idempotent(tmp_path):
    """First call writes the sidecar from legacy metadata (True); the sidecar
    is then authoritative and the second call is a no-op (False)."""
    vault = tmp_path / "vault"
    legacy = {"filesystem": "read", "git": "write"}
    _write_session_record(vault, "ws-a", "sess-1", legacy=legacy)

    assert migrate_session_permissions(vault, "ws-a", "sess-1") is True
    sidecar = session_grants_path(vault, "ws-a", "sess-1")
    assert sidecar.is_file()
    assert json.loads(sidecar.read_text(encoding="utf-8")) == legacy
    assert migrate_session_permissions(vault, "ws-a", "sess-1") is False


def test_migrate_no_legacy_returns_false(tmp_path):
    """Record exists but carries no legacy grants -> False (nothing to
    migrate; reads fall back to {})."""
    vault = tmp_path / "vault"
    _write_session_record(vault, "ws-a", "sess-1", legacy=None)
    assert migrate_session_permissions(vault, "ws-a", "sess-1") is False
    assert not session_grants_path(vault, "ws-a", "sess-1").exists()


def test_migrate_missing_record_raises(tmp_path):
    """No sidecar and no matching record -> PermissionStoreError: never
    fabricate grants during migration."""
    vault = tmp_path / "vault"
    _sessions_dir(vault, "ws-a")  # empty sessions dir
    with pytest.raises(PermissionStoreError):
        migrate_session_permissions(vault, "ws-a", "sess-ghost")


# ---------------------------------------------------------------------------
# Canonical normalisation (legacy grain migration on write + read)
# ---------------------------------------------------------------------------


def test_git_read_alone_migrates_to_git_read(tmp_path):
    vault = tmp_path / "vault"
    write_session_permissions(vault, "ws-a", "sess-1", {"git_read": "read"})
    assert read_session_permissions(vault, "ws-a", "sess-1") == {"git": "read"}


def test_git_write_migrates_to_git_write(tmp_path):
    vault = tmp_path / "vault"
    write_session_permissions(vault, "ws-a", "sess-1", {"git_write": "write"})
    assert read_session_permissions(vault, "ws-a", "sess-1") == {"git": "write"}


def test_git_write_ask_migrates_to_git_ask(tmp_path):
    vault = tmp_path / "vault"
    write_session_permissions(vault, "ws-a", "sess-1", {"git_write": "ask"})
    assert read_session_permissions(vault, "ws-a", "sess-1") == {"git": "ask"}


def test_git_write_unknown_value_fails_closed(tmp_path):
    """A git_write value outside the documented mapping (write/ask/read/
    banned) fails closed to git: banned."""
    vault = tmp_path / "vault"
    write_session_permissions(vault, "ws-a", "sess-1", {"git_write": "full"})
    assert read_session_permissions(vault, "ws-a", "sess-1") == {"git": "banned"}


def test_git_write_overwrites_git_read_when_git_absent(tmp_path):
    """Both grains present and git absent: git_write (write) wins over the
    git_read (read) step-1 result."""
    vault = tmp_path / "vault"
    write_session_permissions(
        vault, "ws-a", "sess-1",
        {"git_read": "read", "git_write": "write"},
    )
    assert read_session_permissions(vault, "ws-a", "sess-1") == {"git": "write"}


def test_present_git_untouched_and_grains_dropped(tmp_path):
    """git present -> git untouched; git_read/git_write never stored."""
    vault = tmp_path / "vault"
    write_session_permissions(
        vault, "ws-a", "sess-1",
        {"git": "read", "git_read": "write", "git_write": "write"},
    )
    assert read_session_permissions(vault, "ws-a", "sess-1") == {"git": "read"}


def test_write_stores_only_cleaned_canonical_payload(tmp_path):
    """Legacy grains, ceiling-only keys (host_bash) and non-catalog keys
    never appear in the stored sidecar JSON; network/mcp are storable
    session grants and survive."""
    vault = tmp_path / "vault"
    write_session_permissions(
        vault, "ws-a", "sess-1",
        {
            "git": "write",
            "git_read": "read",
            "git_write": "ask",
            "network": "write",
            "system": "read",
            "execution": "banned",
            "mcp": "connect",
            "host_bash": "ask",
            "git_allow_worktree_commits": True,
        },
    )
    stored = json.loads(
        session_grants_path(vault, "ws-a", "sess-1").read_text(encoding="utf-8")
    )
    assert stored == {"git": "write", "network": "write", "mcp": "connect"}
    assert read_session_permissions(vault, "ws-a", "sess-1") == {
        "git": "write", "network": "write", "mcp": "connect",
    }


def test_read_never_surfaces_legacy_junk(tmp_path):
    """The legacy fallback read path normalises too: record junk keys
    (legacy git grains, non-bool container) never reach the caller, while
    network -- now a valid session grant -- survives."""
    vault = tmp_path / "vault"
    legacy = {
        "git": "read",
        "git_read": "write",
        "git_write": "write",
        "network": "write",
        "container": "ask",
    }
    _write_session_record(vault, "ws-a", "sess-1", legacy=legacy)
    assert read_session_permissions(vault, "ws-a", "sess-1") == {
        "git": "read", "network": "write",
    }


def test_session_permissions_accepts_git_write_on_feature_branch(tmp_path):
    """SessionPermissions accepts the new git level and the store round-trips
    it verbatim: the object form stores it alongside the valid defaults, and
    a raw dict input stays tight to the git grant alone."""
    vault = tmp_path / "vault"
    sp = SessionPermissions(git="write_on_feature_branch")
    assert sp.git == "write_on_feature_branch"

    write_session_permissions(vault, "ws-a", "sess-1", sp)
    assert read_session_permissions(vault, "ws-a", "sess-1") == {
        "container": False,
        "filesystem": "read",
        "git": "write_on_feature_branch",
        "network": "banned",
        "mcp": "banned",
    }

    write_session_permissions(
        vault, "ws-a", "sess-2", {"git": "write_on_feature_branch"},
    )
    assert read_session_permissions(vault, "ws-a", "sess-2") == {
        "git": "write_on_feature_branch",
    }


# ---------------------------------------------------------------------------
# Write path: atomicity
# ---------------------------------------------------------------------------


def test_atomic_write_replace_failure_leaves_original(tmp_path, monkeypatch):
    """If os.replace fails once, the caller gets PermissionStoreError, the
    previous permissions.json is intact, and no .tmp residue is left."""
    vault = tmp_path / "vault"
    write_session_permissions(vault, "ws-a", "sess-1", {"filesystem": "read"})
    target = session_grants_path(vault, "ws-a", "sess-1")

    real_replace = store_module.os.replace
    state = {"calls": 0}

    def flaky_replace(src, dst):
        state["calls"] += 1
        if state["calls"] == 1:
            raise OSError("injected replace failure")
        return real_replace(src, dst)

    monkeypatch.setattr(store_module.os, "replace", flaky_replace)

    with pytest.raises(PermissionStoreError):
        write_session_permissions(vault, "ws-a", "sess-1", {"filesystem": "write"})

    assert state["calls"] == 1
    assert json.loads(target.read_text(encoding="utf-8")) == {"filesystem": "read"}
    leftovers = [p.name for p in target.parent.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == [], f"temp files left behind: {leftovers}"


# ---------------------------------------------------------------------------
# Fail-closed read behaviour
# ---------------------------------------------------------------------------


def test_corrupt_sidecar_raises(tmp_path):
    """Corrupt sidecar JSON must raise, never silently return {}."""
    vault = tmp_path / "vault"
    sidecar = session_grants_path(vault, "ws-a", "sess-1")
    sidecar.parent.mkdir(parents=True)
    sidecar.write_text("{ not json !!!", encoding="utf-8")

    with pytest.raises(PermissionStoreError):
        read_session_permissions(vault, "ws-a", "sess-1")


def test_sidecar_non_object_shape_raises(tmp_path):
    """A sidecar that is valid JSON but not an object raises."""
    vault = tmp_path / "vault"
    sidecar = session_grants_path(vault, "ws-a", "sess-1")
    sidecar.parent.mkdir(parents=True)
    sidecar.write_text("[\"filesystem\", \"read\"]", encoding="utf-8")

    with pytest.raises(PermissionStoreError):
        read_session_permissions(vault, "ws-a", "sess-1")


def test_missing_sidecar_and_no_record_raises(tmp_path):
    """Unknown session (no sidecar, no matching record) -> raise: deny, do
    not default."""
    vault = tmp_path / "vault"
    # Empty sessions directory.
    _sessions_dir(vault, "ws-a")
    with pytest.raises(PermissionStoreError):
        read_session_permissions(vault, "ws-a", "sess-ghost")

    # Entirely missing sessions directory -> same fail-closed result.
    with pytest.raises(PermissionStoreError):
        read_session_permissions(vault, "ws-no-sessions", "sess-ghost")


def test_corrupt_session_record_raises(tmp_path):
    """The matching session record itself is corrupt -> raise (content scan
    hits it, strict read fails closed)."""
    vault = tmp_path / "vault"
    record = _write_session_record(vault, "ws-a", "sess-1", legacy=None)
    record.write_text("{ broken json", encoding="utf-8")

    with pytest.raises(PermissionStoreError):
        read_session_permissions(vault, "ws-a", "sess-1")


# ---------------------------------------------------------------------------
# Workspace ceiling
# ---------------------------------------------------------------------------


def test_workspace_ceiling_happy(tmp_path):
    vault = tmp_path / "vault"
    ceiling = {"filesystem": "write", "git": "write"}
    _write_config(vault, "ws-a", permissions=ceiling)
    assert workspace_ceiling(vault, "ws-a") == ceiling


def test_workspace_ceiling_missing_permissions_key_returns_empty(tmp_path):
    """config.json without a 'permissions' key -> {} (purpose-preset fallback
    is the caller's job, per blueprint section 8)."""
    vault = tmp_path / "vault"
    _write_config(vault, "ws-a", permissions=None)
    assert workspace_ceiling(vault, "ws-a") == {}


def test_workspace_ceiling_missing_config_raises(tmp_path):
    """Missing config.json raises: an unreadable ceiling must never be
    treated as 'no ceiling'."""
    vault = tmp_path / "vault"
    (Path(vault) / "workspaces" / "ws-a").mkdir(parents=True)
    with pytest.raises(PermissionStoreError):
        workspace_ceiling(vault, "ws-a")


def test_workspace_ceiling_corrupt_config_raises(tmp_path):
    vault = tmp_path / "vault"
    _write_config(vault, "ws-a", permissions=None, corrupt=True)
    with pytest.raises(PermissionStoreError):
        workspace_ceiling(vault, "ws-a")


def test_workspace_ceiling_non_object_permissions_raises(tmp_path):
    vault = tmp_path / "vault"
    cfg = _write_config(vault, "ws-a", permissions=None)
    cfg.write_text(json.dumps({"purpose": "coding", "permissions": ["filesystem"]}), encoding="utf-8")
    with pytest.raises(PermissionStoreError):
        workspace_ceiling(vault, "ws-a")
