"""
test_workspace_lifecycle.py — Tests for ``delete_workspace`` teardown
orchestration (thoughtmachine.workspace_lifecycle).

The suite is hermetic: the vault root is redirected into a tmp dir and every
external dependency (Docker client, infra cleanup fns, WorkspaceRegistry) is
replaced with fakes, so no real containers/volumes/registries are touched.
"""

from __future__ import annotations

import json
import pathlib
from unittest.mock import Mock

import pytest

import infra.container_manager as cm
import infra.resource_container_manager as rcm
import thoughtmachine.workspace_lifecycle as wl

WS_ID = "ws-lifecycle-test"
SID = "sess-000000-lifecycle"

EXPECTED_STEPS = [
    "user_containers",
    "resource_containers_and_image",
    "package_volume",
    "workspace_sessions",
    "workspace_vault_dir",
    "registry_unregister",
]


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def vault(tmp_path, monkeypatch):
    """Redirect the vault root into a tmp dir (hermetic)."""
    monkeypatch.setattr(pathlib.Path, "home", lambda: tmp_path)
    import thoughtmachine.vault

    vault_path = tmp_path / ".thoughtmachine"
    monkeypatch.setattr(thoughtmachine.vault, "vault_root", lambda: vault_path)
    # workspace_lifecycle holds a direct import binding; patch it too.
    monkeypatch.setattr(wl, "vault_root", lambda: vault_path)
    vault_path.mkdir(parents=True, exist_ok=True)
    return vault_path


def _make_workspace(vault, ws_id=WS_ID, with_session=True):
    """Seed a workspace vault dir (config, workers, notes, session file)."""
    ws_dir = vault / "workspaces" / ws_id
    sessions_dir = ws_dir / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    (ws_dir / "config.json").write_text(
        json.dumps({"name": ws_id}), encoding="utf-8"
    )
    (ws_dir / "workers.json").write_text(json.dumps([]), encoding="utf-8")
    (ws_dir / "container_notes.json").write_text(
        json.dumps({"n": 1}), encoding="utf-8"
    )
    if with_session:
        session_file = sessions_dir / f"{SID}.json"
        session_file.write_text(
            json.dumps(
                {
                    "session_id": SID,
                    "metadata": {"name": "lifecycle"},
                    "created_at": "2025-01-01T00:00:00Z",
                    "updated_at": "2025-01-01T00:00:00Z",
                    "user_history": [],
                }
            ),
            encoding="utf-8",
        )
    return ws_dir


# ── Fakes (docker + registry) ───────────────────────────────────────────────


class NotFound(Exception):
    """Duck-typed stand-in for docker.errors.NotFound (matched by name)."""


class _FakeVolume:
    def __init__(self, name):
        self.name = name
        self.remove_calls = []

    def remove(self, force=False):
        self.remove_calls.append({"force": force})


class _FakeVolumes:
    def __init__(self, present=(), events=None):
        self.present = set(present)
        self.events = events

    def get(self, name):
        if self.events is not None:
            self.events.append("package_volume")
        if name not in self.present:
            raise NotFound(name)
        return _FakeVolume(name)


class _FakeClient:
    def __init__(self, present=(), events=None):
        self.volumes = _FakeVolumes(present=present, events=events)


class _FakeEntry:
    def __init__(self, root_path=None):
        self.root_path = root_path


class _FakeRegistry:
    def __init__(self, entry, events=None):
        self.entry = entry
        self.events = events
        self.unregister_calls = []

    def get_workspace(self, workspace_id):
        return self.entry

    def unregister_workspace(self, workspace_id):
        if self.events is not None:
            self.events.append("registry_unregister")
        self.unregister_calls.append(workspace_id)
        return self.entry is not None


def _patch_deps(
    monkeypatch,
    tmp_path,
    events=None,
    registry_entry="default",
    user_result="default",
    resource_result="default",
    volume_present=True,
):
    """Replace docker/infra/registry dependencies with fakes.

    Returns the fake registry. ``events`` (a caller-owned list) records each
    executed external call in order, when provided.
    """
    if user_result == "default":
        user_result = {"removed": 1}
    if resource_result == "default":
        resource_result = {"removed_containers": 1, "removed_image": True, "detail": ""}
    if registry_entry == "default":
        registry_entry = _FakeEntry(root_path=str(tmp_path / "projects" / WS_ID))

    registry = _FakeRegistry(entry=registry_entry, events=events)
    monkeypatch.setattr(wl, "WorkspaceRegistry", lambda: registry)

    def _user(workspace_id, docker_client):
        if events is not None:
            events.append("user_containers")
        if isinstance(user_result, Exception):
            raise user_result
        return user_result

    monkeypatch.setattr(cm, "cleanup_workspace", _user)

    def _resource(workspace_id):
        if events is not None:
            events.append("resource_containers_and_image")
        if isinstance(resource_result, Exception):
            raise resource_result
        return resource_result

    monkeypatch.setattr(rcm, "cleanup_workspace_resources", _resource)

    client = _FakeClient(
        present=[f"tm-packages-{WS_ID}"] if volume_present else [],
        events=events,
    )
    monkeypatch.setattr(wl, "_docker_client", lambda: client)
    return registry


# ── Tests ───────────────────────────────────────────────────────────────────


class TestDryRun:
    def test_dry_run_reports_all_steps_without_side_effects(self, vault, tmp_path, monkeypatch):
        """dry_run must run the read-only registry lookup and touch nothing."""
        _make_workspace(vault, with_session=True)
        registry = _FakeRegistry(
            entry=_FakeEntry(root_path=str(tmp_path / "projects" / WS_ID))
        )
        monkeypatch.setattr(wl, "WorkspaceRegistry", lambda: registry)
        user_mock = Mock()
        resource_mock = Mock()
        client_mock = Mock()
        monkeypatch.setattr(cm, "cleanup_workspace", user_mock)
        monkeypatch.setattr(rcm, "cleanup_workspace_resources", resource_mock)
        monkeypatch.setattr(wl, "_docker_client", lambda: client_mock)

        report = wl.delete_workspace(WS_ID, dry_run=True)

        assert report["workspace_id"] == WS_ID
        assert report["dry_run"] is True
        assert report["would_remove"] == EXPECTED_STEPS
        assert report["removed"] == []
        assert report["skipped"] == []
        assert report["errors"] == []
        assert report["registered"] is True
        assert report["root_path"] == str(tmp_path / "projects" / WS_ID)

        user_mock.assert_not_called()
        resource_mock.assert_not_called()
        client_mock.assert_not_called()
        assert registry.unregister_calls == []

        # Nothing on disk was touched.
        ws_dir = vault / "workspaces" / WS_ID
        assert ws_dir.exists()
        assert (ws_dir / "sessions" / f"{SID}.json").exists()


class TestFullDelete:
    def test_delete_removes_everything_in_order(self, vault, tmp_path, monkeypatch):
        """A registered workspace is fully torn down, session bookkeeping too."""
        _make_workspace(vault, with_session=True)
        state_dir = vault / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "open_sessions.json").write_text(
            json.dumps([SID, "other-session"]), encoding="utf-8"
        )
        (state_dir / ".current_session").write_text(SID, encoding="utf-8")

        events = []
        registry = _patch_deps(monkeypatch, tmp_path, events=events)

        report = wl.delete_workspace(WS_ID)

        assert report["dry_run"] is False
        assert report["errors"] == []
        assert sorted(report["removed"]) == sorted(EXPECTED_STEPS)
        assert report["would_remove"] == []
        assert report["skipped"] == []
        assert report["registered"] is True

        # Vault workspace dir (incl. session files) is gone.
        assert not (vault / "workspaces" / WS_ID).exists()

        # Session bookkeeping: open-session entry dropped, marker cleared.
        assert json.loads(
            (state_dir / "open_sessions.json").read_text(encoding="utf-8")
        ) == ["other-session"]
        assert not (state_dir / ".current_session").exists()

        # External calls happened in step order.
        assert events == [
            "user_containers",
            "resource_containers_and_image",
            "package_volume",
            "registry_unregister",
        ]
        assert registry.unregister_calls == [WS_ID]

    def test_step_failure_is_isolated(self, vault, tmp_path, monkeypatch):
        """A failing step is recorded in errors; the remaining steps still run."""
        _make_workspace(vault, with_session=True)
        _patch_deps(
            monkeypatch, tmp_path, user_result=RuntimeError("boom")
        )

        report = wl.delete_workspace(WS_ID)

        assert report["errors"] == [{"step": "user_containers", "error": "boom"}]
        assert sorted(report["removed"]) == sorted(EXPECTED_STEPS[1:])
        assert "user_containers" not in report["removed"]
        assert "user_containers" not in report["skipped"]
        # Everything downstream still ran.
        assert not (vault / "workspaces" / WS_ID).exists()

    def test_registry_root_path_is_never_deleted(self, vault, tmp_path, monkeypatch):
        """The project root reported by the registry must stay untouched."""
        _make_workspace(vault, with_session=True)
        root = tmp_path / "projects" / WS_ID
        root.mkdir(parents=True, exist_ok=True)
        (root / "important.txt").write_text("keep me", encoding="utf-8")
        _patch_deps(monkeypatch, tmp_path)  # default entry root_path == root

        report = wl.delete_workspace(WS_ID)

        assert report["root_path"] == str(root)
        assert report["errors"] == []
        assert (root / "important.txt").read_text(encoding="utf-8") == "keep me"
        assert not (vault / "workspaces" / WS_ID).exists()

    def test_unregistered_workspace_skips_unregister(self, vault, tmp_path, monkeypatch):
        """With no registry entry, unregister is skipped and reported absent."""
        _make_workspace(vault, with_session=False)
        registry = _patch_deps(monkeypatch, tmp_path, registry_entry=None)

        report = wl.delete_workspace(WS_ID)

        assert report["registered"] is False
        assert report["root_path"] is None
        assert "registry_unregister" in report["skipped"]
        assert "registry_unregister" not in report["removed"]
        assert registry.unregister_calls == []
        assert not (vault / "workspaces" / WS_ID).exists()

    def test_missing_package_volume_is_skipped(self, vault, tmp_path, monkeypatch):
        """Absent tm-packages volume → package_volume skipped, not an error."""
        _make_workspace(vault, with_session=False)
        _patch_deps(monkeypatch, tmp_path, volume_present=False)

        report = wl.delete_workspace(WS_ID)

        assert report["errors"] == []
        assert "package_volume" in report["skipped"]
        assert "package_volume" not in report["removed"]
        assert not (vault / "workspaces" / WS_ID).exists()


class TestSessionBookkeeping:
    def test_foreign_current_session_marker_is_kept(self, vault, tmp_path, monkeypatch):
        """A .current_session pointing outside this workspace must survive."""
        _make_workspace(vault, with_session=False)
        state_dir = vault / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / ".current_session").write_text(
            "foreign-session", encoding="utf-8"
        )
        legacy_dir = vault / "sessions"
        legacy_dir.mkdir(parents=True, exist_ok=True)
        (legacy_dir / "foreign-session.json").write_text(
            json.dumps(
                {
                    "session_id": "foreign-session",
                    "metadata": {"name": "foreign"},
                    "created_at": "2025-01-01T00:00:00Z",
                    "updated_at": "2025-01-01T00:00:00Z",
                    "user_history": [],
                }
            ),
            encoding="utf-8",
        )
        _patch_deps(monkeypatch, tmp_path)

        report = wl.delete_workspace(WS_ID)

        assert report["errors"] == []
        assert (state_dir / ".current_session").read_text(
            encoding="utf-8"
        ) == "foreign-session"
        assert not (vault / "workspaces" / WS_ID).exists()


class TestSymlinkSafety:
    def test_symlinked_workspace_dir_is_refused(self, vault, tmp_path, monkeypatch):
        """A vault workspace dir that is a symlink must never be followed."""
        real_dir = tmp_path / "real-target"
        real_dir.mkdir(parents=True, exist_ok=True)
        (real_dir / "payload.txt").write_text("payload", encoding="utf-8")

        ws_parent = vault / "workspaces"
        ws_parent.mkdir(parents=True, exist_ok=True)
        link = ws_parent / WS_ID
        link.symlink_to(real_dir, target_is_directory=True)

        _patch_deps(monkeypatch, tmp_path)

        report = wl.delete_workspace(WS_ID)

        error_steps = {e["step"] for e in report["errors"]}
        assert "workspace_vault_dir" in error_steps
        assert "workspace_sessions" in error_steps
        # The real target and its payload are untouched.
        assert (real_dir / "payload.txt").read_text(encoding="utf-8") == "payload"
        # The symlink itself is still there (nothing was deleted).
        assert link.is_symlink()
