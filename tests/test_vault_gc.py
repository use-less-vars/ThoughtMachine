"""Tests for thoughtmachine.vault_gc.run_gc() — age-based vault garbage collection.

Covers: dry-run reporting, stale-workspace removal, orphan workspace dirs,
orphan sessions, resource + orphan user containers, orphan package volumes,
error isolation, registry failure, docker unavailability and env thresholds.
"""

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import thoughtmachine.vault  # noqa: F401  (patched via monkeypatch.setattr)
import thoughtmachine.vault_gc as gc

NOW = datetime(2025, 1, 1, tzinfo=timezone.utc)
OLD = NOW - timedelta(days=200)          # older than every default threshold
STALE = NOW - timedelta(days=40)         # older than 90d stale threshold
FRESH = NOW - timedelta(days=1)          # within the 7d active window
RECENT = NOW - timedelta(hours=1)        # within the 24h container cutoff


def _iso(dt):
    return dt.isoformat()


# ── Fixtures / helpers ─────────────────────────────────────────────────────

@pytest.fixture
def vault(tmp_path, monkeypatch):
    """A fresh vault root at tmp_path/.thoughtmachine with state/sessions/workspaces."""
    vault_path = tmp_path / ".thoughtmachine"
    for sub in ("state", "sessions", "workspaces"):
        (vault_path / sub).mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr("thoughtmachine.vault.vault_root", lambda: vault_path)
    monkeypatch.setattr(gc, "vault_root", lambda: vault_path)
    return vault_path


def _make_ws_dir(vault_path, ws_id, mtime=None):
    """Create a workspace directory (with config.json), optionally with mtime."""
    ws_dir = vault_path / "workspaces" / ws_id
    ws_dir.mkdir(parents=True)
    (ws_dir / "config.json").write_text('{"max_containers": 4}', encoding="utf-8")
    if mtime is not None:
        ts = mtime.timestamp()
        os.utime(ws_dir, (ts, ts))
    return ws_dir


def _write_session(dir_path, sid, updated_at):
    """Write a session file + its _meta_ companion; return the session file path."""
    dir_path.mkdir(parents=True, exist_ok=True)
    payload = {
        "session_id": sid,
        "metadata": {"name": sid},
        "created_at": _iso(updated_at),
        "updated_at": _iso(updated_at),
        "user_history": [],
    }
    session_path = dir_path / f"{sid}.json"
    session_path.write_text(json.dumps(payload), encoding="utf-8")
    (dir_path / f"_meta_{sid}.json").write_text(
        json.dumps({"session_id": sid}), encoding="utf-8"
    )
    return session_path


# ── Fakes ──────────────────────────────────────────────────────────────────

class FakeEntry:
    def __init__(self, id, last_opened="", updated_at="", created_at=""):
        self.id = id
        self.last_opened = last_opened
        self.updated_at = updated_at
        self.created_at = created_at


class FakeRegistry:
    def __init__(self, entries=None, error=None):
        self.entries = list(entries or [])
        self.error = error

    def list_workspaces(self):
        if self.error is not None:
            raise self.error
        return list(self.entries)


class FakeContainer:
    def __init__(self, cid, status="exited", created="2020-01-01T00:00:00Z",
                 labels=None, remove_error=None):
        self.id = cid
        self.name = cid
        self.status = status
        self.created = created
        self.attrs = {"Created": created}
        self.labels = dict(labels or {})
        self.remove_error = remove_error
        self.remove_calls = []

    def remove(self, force=False):
        self.remove_calls.append({"force": force})
        if self.remove_error is not None:
            raise self.remove_error


class FakeVolume:
    def __init__(self, name, created, remove_error=None):
        self.name = name
        self.CreatedAt = created
        self.attrs = {"Name": name, "CreatedAt": created}
        self.remove_error = remove_error
        self.remove_calls = []

    def remove(self, force=False):
        self.remove_calls.append({"force": force})
        if self.remove_error is not None:
            raise self.remove_error


class FakeContainers:
    """Minimal docker-py containers collection with presence-label filtering."""

    def __init__(self, containers=None, error=None):
        self.containers = list(containers or [])
        self.error = error
        self.list_calls = []

    def list(self, all=False, filters=None):
        self.list_calls.append({"all": all, "filters": filters})
        if self.error is not None:
            raise self.error
        label_filter = (filters or {}).get("label")
        if label_filter is None:
            return list(self.containers)
        if isinstance(label_filter, str):
            label_filter = [label_filter]

        def matches(container):
            labels = getattr(container, "labels", None) or {}
            for key in label_filter:
                if key not in labels:
                    return False
            return True

        return [c for c in self.containers if matches(c)]


class FakeVolumes:
    def __init__(self, volumes=None, error=None):
        self.volumes = list(volumes or [])
        self.error = error
        self.list_calls = []

    def list(self):
        if self.error is not None:
            raise self.error
        return list(self.volumes)


class FakeClient:
    def __init__(self, containers=None, volumes=None,
                 containers_error=None, volumes_error=None):
        self.containers = FakeContainers(containers, containers_error)
        self.volumes = FakeVolumes(volumes, volumes_error)


# ── Dry run ────────────────────────────────────────────────────────────────

class TestDryRun:
    def test_dry_run_reports_would_remove_and_mutates_nothing(self, vault, monkeypatch):
        calls = []
        monkeypatch.setattr(
            gc, "delete_workspace", lambda ws_id: calls.append(ws_id) or {"errors": []}
        )
        entry = FakeEntry(id="ws-1", last_opened=_iso(OLD))
        ghost_dir = _make_ws_dir(vault, "ghost-dir", mtime=OLD)
        session_path = _write_session(vault / "sessions", "sess-1", OLD)
        cont = FakeContainer(cid="c-1", labels={gc._RESOURCE_LABEL: "1"})
        vol = FakeVolume("tm-packages-gone-1", created=_iso(OLD))

        report = gc.run_gc(
            now=NOW,
            registry=FakeRegistry([entry]),
            docker_client=FakeClient(containers=[cont], volumes=[vol]),
            dry_run=True,
        )
        cats = report["categories"]
        assert cats[gc.CAT_STALE_WORKSPACES]["would_remove"] == ["ws-1"]
        assert cats[gc.CAT_ORPHAN_WORKSPACE_DIRS]["would_remove"] == [
            str(vault / "workspaces" / "ghost-dir")
        ]
        assert cats[gc.CAT_ORPHAN_SESSIONS]["would_remove"] == ["sess-1"]
        assert cats[gc.CAT_ORPHAN_RESOURCE_CONTAINERS]["would_remove"] == ["c-1"]
        assert cats[gc.CAT_ORPHAN_VOLUMES]["would_remove"] == ["tm-packages-gone-1"]
        # No side effects anywhere:
        assert calls == []
        assert cont.remove_calls == []
        assert vol.remove_calls == []
        assert session_path.exists()
        assert ghost_dir.is_dir()
        for name in cats:
            assert cats[name]["removed"] == []


# ── Stale workspaces ───────────────────────────────────────────────────────

class TestStaleWorkspaces:
    def test_stale_workspace_removed(self, vault, monkeypatch):
        calls = []
        monkeypatch.setattr(
            gc, "delete_workspace", lambda ws_id: calls.append(ws_id) or {"errors": []}
        )
        entry = FakeEntry(id="ws-1", last_opened=_iso(OLD))
        report = gc.run_gc(now=NOW, registry=FakeRegistry([entry]),
                           docker_client=FakeClient())
        cat = report["categories"][gc.CAT_STALE_WORKSPACES]
        assert cat["removed"] == ["ws-1"]
        assert calls == ["ws-1"]

    def test_created_at_used_when_no_other_timestamp(self, vault, monkeypatch):
        calls = []
        monkeypatch.setattr(
            gc, "delete_workspace", lambda ws_id: calls.append(ws_id) or {"errors": []}
        )
        entry = FakeEntry(id="ws-1", created_at=_iso(OLD))
        report = gc.run_gc(now=NOW, registry=FakeRegistry([entry]),
                           docker_client=FakeClient())
        assert report["categories"][gc.CAT_STALE_WORKSPACES]["removed"] == ["ws-1"]

    def test_active_within_window_skipped(self, vault):
        entry = FakeEntry(id="ws-1", last_opened=_iso(FRESH))
        report = gc.run_gc(now=NOW, registry=FakeRegistry([entry]),
                           docker_client=FakeClient())
        cat = report["categories"][gc.CAT_STALE_WORKSPACES]
        assert cat["removed"] == []
        assert cat["skipped"] == [
            {"id": "ws-1", "reason": "active (within active window)"}
        ]

    def test_not_old_enough_skipped(self, vault):
        entry = FakeEntry(id="ws-1", last_opened=_iso(STALE))
        report = gc.run_gc(now=NOW, registry=FakeRegistry([entry]),
                           docker_client=FakeClient())
        cat = report["categories"][gc.CAT_STALE_WORKSPACES]
        assert cat["removed"] == []
        assert cat["skipped"] == [{"id": "ws-1", "reason": "not old enough"}]

    def test_no_timestamp_skipped(self, vault):
        entry = FakeEntry(id="ws-1")
        report = gc.run_gc(now=NOW, registry=FakeRegistry([entry]),
                           docker_client=FakeClient())
        cat = report["categories"][gc.CAT_STALE_WORKSPACES]
        assert cat["removed"] == []
        assert cat["skipped"] == [
            {"id": "ws-1", "reason": "no parseable last-activity timestamp"}
        ]

    def test_delete_workspace_errors_land_in_category_errors(self, vault, monkeypatch):
        monkeypatch.setattr(
            gc,
            "delete_workspace",
            lambda ws_id: {"errors": [{"step": "package_volume", "error": "boom"}]},
        )
        entry = FakeEntry(id="ws-1", last_opened=_iso(OLD))
        report = gc.run_gc(now=NOW, registry=FakeRegistry([entry]),
                           docker_client=FakeClient())
        cat = report["categories"][gc.CAT_STALE_WORKSPACES]
        assert cat["removed"] == []
        assert cat["errors"] == [
            {"id": "ws-1", "step": "package_volume", "error": "boom"}
        ]


# ── Orphan workspace dirs ──────────────────────────────────────────────────

class TestOrphanWorkspaceDirs:
    def test_old_unregistered_dir_removed(self, vault):
        ws_dir = _make_ws_dir(vault, "ghost-1", mtime=OLD)
        report = gc.run_gc(now=NOW, registry=FakeRegistry([]),
                           docker_client=FakeClient())
        cat = report["categories"][gc.CAT_ORPHAN_WORKSPACE_DIRS]
        assert cat["removed"] == [str(ws_dir)]
        assert not ws_dir.exists()

    def test_registered_dir_skipped(self, vault):
        ws_dir = _make_ws_dir(vault, "ws-1", mtime=OLD)
        report = gc.run_gc(now=NOW, registry=FakeRegistry([FakeEntry(id="ws-1")]),
                           docker_client=FakeClient())
        cat = report["categories"][gc.CAT_ORPHAN_WORKSPACE_DIRS]
        assert cat["removed"] == []
        assert cat["skipped"] == [
            {"id": str(ws_dir), "reason": "registered workspace"}
        ]
        assert ws_dir.is_dir()

    def test_fresh_unregistered_dir_skipped(self, vault):
        ws_dir = _make_ws_dir(vault, "ghost-1", mtime=FRESH)
        report = gc.run_gc(now=NOW, registry=FakeRegistry([]),
                           docker_client=FakeClient())
        cat = report["categories"][gc.CAT_ORPHAN_WORKSPACE_DIRS]
        assert cat["removed"] == []
        assert cat["skipped"] == [{"id": str(ws_dir), "reason": "not old enough"}]

    def test_symlink_dir_skipped_target_intact(self, vault):
        target = vault / "real-target"
        target.mkdir()
        keep = target / "keep.txt"
        keep.write_text("x", encoding="utf-8")
        link = vault / "workspaces" / "ghost-link"
        link.symlink_to(target, target_is_directory=True)
        ts = OLD.timestamp()
        os.utime(link, (ts, ts), follow_symlinks=False)

        report = gc.run_gc(now=NOW, registry=FakeRegistry([]),
                           docker_client=FakeClient())
        cat = report["categories"][gc.CAT_ORPHAN_WORKSPACE_DIRS]
        assert cat["removed"] == []
        assert cat["skipped"] == [{"id": str(link), "reason": "symlink (refusing)"}]
        assert target.is_dir()
        assert keep.exists()


# ── Orphan sessions ────────────────────────────────────────────────────────

class TestOrphanSessions:
    def test_old_session_removed_with_meta(self, vault):
        _write_session(vault / "sessions", "sess-1", OLD)
        report = gc.run_gc(now=NOW, registry=FakeRegistry([]),
                           docker_client=FakeClient())
        cat = report["categories"][gc.CAT_ORPHAN_SESSIONS]
        assert cat["removed"] == ["sess-1"]
        assert not (vault / "sessions" / "sess-1.json").exists()
        assert not (vault / "sessions" / "_meta_sess-1.json").exists()

    def test_open_session_skipped(self, vault):
        _write_session(vault / "sessions", "sess-2", OLD)
        (vault / "state" / "open_sessions.json").write_text(
            json.dumps(["sess-2"]), encoding="utf-8"
        )
        report = gc.run_gc(now=NOW, registry=FakeRegistry([]),
                           docker_client=FakeClient())
        cat = report["categories"][gc.CAT_ORPHAN_SESSIONS]
        assert cat["removed"] == []
        assert cat["skipped"] == [{"id": "sess-2", "reason": "session is open"}]
        assert (vault / "sessions" / "sess-2.json").exists()

    def test_current_session_skipped(self, vault):
        _write_session(vault / "sessions", "sess-3", OLD)
        (vault / "state" / ".current_session").write_text(
            "sess-3\n", encoding="utf-8"
        )
        report = gc.run_gc(now=NOW, registry=FakeRegistry([]),
                           docker_client=FakeClient())
        cat = report["categories"][gc.CAT_ORPHAN_SESSIONS]
        assert cat["removed"] == []
        assert cat["skipped"] == [{"id": "sess-3", "reason": "session is open"}]

    def test_fresh_session_skipped(self, vault):
        _write_session(vault / "sessions", "sess-4", FRESH)
        report = gc.run_gc(now=NOW, registry=FakeRegistry([]),
                           docker_client=FakeClient())
        cat = report["categories"][gc.CAT_ORPHAN_SESSIONS]
        assert cat["removed"] == []
        assert cat["skipped"] == [{"id": "sess-4", "reason": "not old enough"}]

    def test_workspace_scoped_session_removed(self, vault):
        ws_dir = _make_ws_dir(vault, "ws-9")  # fresh mtime -> dir sweep skips it
        _write_session(ws_dir / "sessions", "sess-9", OLD)
        report = gc.run_gc(now=NOW, registry=FakeRegistry([FakeEntry(id="ws-9")]),
                           docker_client=FakeClient())
        cat = report["categories"][gc.CAT_ORPHAN_SESSIONS]
        assert cat["removed"] == ["sess-9"]
        assert not (ws_dir / "sessions" / "sess-9.json").exists()


# ── Resource containers ────────────────────────────────────────────────────

class TestResourceContainers:
    def test_old_stopped_resource_container_removed(self, vault):
        cont = FakeContainer(cid="c-1", status="exited", created=_iso(OLD),
                             labels={gc._RESOURCE_LABEL: "1"})
        report = gc.run_gc(now=NOW, registry=FakeRegistry([]),
                           docker_client=FakeClient(containers=[cont]))
        cat = report["categories"][gc.CAT_ORPHAN_RESOURCE_CONTAINERS]
        assert cat["removed"] == ["c-1"]
        assert cont.remove_calls == [{"force": True}]

    def test_running_resource_container_skipped(self, vault):
        cont = FakeContainer(cid="c-1", status="running", created=_iso(OLD),
                             labels={gc._RESOURCE_LABEL: "1"})
        report = gc.run_gc(now=NOW, registry=FakeRegistry([]),
                           docker_client=FakeClient(containers=[cont]))
        cat = report["categories"][gc.CAT_ORPHAN_RESOURCE_CONTAINERS]
        assert cat["removed"] == []
        assert cat["skipped"] == [{"id": "c-1", "reason": "in use (status=running)"}]
        assert cont.remove_calls == []

    def test_created_status_old_removed(self, vault):
        cont = FakeContainer(cid="c-1", status="created", created=_iso(OLD),
                             labels={gc._RESOURCE_LABEL: "1"})
        report = gc.run_gc(now=NOW, registry=FakeRegistry([]),
                           docker_client=FakeClient(containers=[cont]))
        assert report["categories"][gc.CAT_ORPHAN_RESOURCE_CONTAINERS]["removed"] == ["c-1"]

    def test_recent_resource_container_skipped(self, vault):
        cont = FakeContainer(cid="c-1", status="exited", created=_iso(RECENT),
                             labels={gc._RESOURCE_LABEL: "1"})
        report = gc.run_gc(now=NOW, registry=FakeRegistry([]),
                           docker_client=FakeClient(containers=[cont]))
        cat = report["categories"][gc.CAT_ORPHAN_RESOURCE_CONTAINERS]
        assert cat["removed"] == []
        assert cat["skipped"] == [{"id": "c-1", "reason": "not old enough"}]


# ── Orphan user containers (workspace_id label, unregistered workspace) ────

class TestUserContainers:
    def test_orphan_user_container_removed(self, vault):
        cont = FakeContainer(cid="uc-1", status="exited", created=_iso(OLD),
                             labels={gc._WORKSPACE_LABEL: "ghost-ws"})
        report = gc.run_gc(now=NOW, registry=FakeRegistry([]),
                           docker_client=FakeClient(containers=[cont]))
        cat = report["categories"][gc.CAT_ORPHAN_RESOURCE_CONTAINERS]
        assert cat["removed"] == ["uc-1"]
        assert cont.remove_calls == [{"force": True}]

    def test_user_container_of_registered_workspace_skipped(self, vault):
        cont = FakeContainer(cid="uc-1", status="exited", created=_iso(OLD),
                             labels={gc._WORKSPACE_LABEL: "ws-1"})
        report = gc.run_gc(now=NOW, registry=FakeRegistry([FakeEntry(id="ws-1")]),
                           docker_client=FakeClient(containers=[cont]))
        cat = report["categories"][gc.CAT_ORPHAN_RESOURCE_CONTAINERS]
        assert cat["removed"] == []
        assert cat["skipped"] == [
            {"id": "uc-1", "reason": "belongs to a registered workspace"}
        ]

    def test_resource_container_not_double_processed(self, vault):
        cont = FakeContainer(cid="rc-1", status="exited", created=_iso(OLD),
                             labels={gc._RESOURCE_LABEL: "1",
                                     gc._WORKSPACE_LABEL: "ghost-ws"})
        report = gc.run_gc(now=NOW, registry=FakeRegistry([]),
                           docker_client=FakeClient(containers=[cont]))
        cat = report["categories"][gc.CAT_ORPHAN_RESOURCE_CONTAINERS]
        assert cat["removed"] == ["rc-1"]
        assert cont.remove_calls == [{"force": True}]


# ── Orphan volumes ─────────────────────────────────────────────────────────

class TestOrphanVolumes:
    def test_unregistered_package_volume_removed(self, vault):
        vol = FakeVolume("tm-packages-gone-1", created=_iso(OLD))
        report = gc.run_gc(now=NOW, registry=FakeRegistry([]),
                           docker_client=FakeClient(volumes=[vol]))
        cat = report["categories"][gc.CAT_ORPHAN_VOLUMES]
        assert cat["removed"] == ["tm-packages-gone-1"]
        assert vol.remove_calls == [{"force": True}]

    def test_registered_workspace_volume_skipped(self, vault):
        vol = FakeVolume("tm-packages-ws-1", created=_iso(OLD))
        report = gc.run_gc(now=NOW, registry=FakeRegistry([FakeEntry(id="ws-1")]),
                           docker_client=FakeClient(volumes=[vol]))
        cat = report["categories"][gc.CAT_ORPHAN_VOLUMES]
        assert cat["removed"] == []
        assert cat["skipped"] == [
            {"id": "tm-packages-ws-1", "reason": "belongs to a registered workspace"}
        ]

    def test_non_package_volume_ignored(self, vault):
        vol = FakeVolume("my-app-data", created=_iso(OLD))
        report = gc.run_gc(now=NOW, registry=FakeRegistry([]),
                           docker_client=FakeClient(volumes=[vol]))
        cat = report["categories"][gc.CAT_ORPHAN_VOLUMES]
        assert cat["removed"] == []
        assert cat["would_remove"] == []
        assert cat["skipped"] == []
        assert vol.remove_calls == []

    def test_fresh_volume_skipped(self, vault):
        vol = FakeVolume("tm-packages-fresh-1", created=_iso(FRESH))
        report = gc.run_gc(now=NOW, registry=FakeRegistry([]),
                           docker_client=FakeClient(volumes=[vol]))
        cat = report["categories"][gc.CAT_ORPHAN_VOLUMES]
        assert cat["removed"] == []
        assert cat["skipped"] == [{"id": "tm-packages-fresh-1", "reason": "not old enough"}]


# ── Error isolation / degraded modes ───────────────────────────────────────

class TestErrorIsolation:
    def test_container_remove_error_isolated(self, vault):
        bad = FakeContainer(cid="c-bad", status="exited", created=_iso(OLD),
                            labels={gc._RESOURCE_LABEL: "1"},
                            remove_error=RuntimeError("boom"))
        good = FakeContainer(cid="c-good", status="exited", created=_iso(OLD),
                             labels={gc._RESOURCE_LABEL: "1"})
        report = gc.run_gc(now=NOW, registry=FakeRegistry([]),
                           docker_client=FakeClient(containers=[bad, good]))
        cat = report["categories"][gc.CAT_ORPHAN_RESOURCE_CONTAINERS]
        assert [e["id"] for e in cat["errors"]] == ["c-bad"]
        assert cat["removed"] == ["c-good"]


class TestRegistryError:
    def test_registry_list_raises_skips_registry_dependent_categories(self, vault):
        reg = FakeRegistry(entries=[], error=RuntimeError("reg down"))
        ghost = _make_ws_dir(vault, "ghost-1", mtime=OLD)
        report = gc.run_gc(now=NOW, registry=reg, docker_client=FakeClient())
        # Top-level registry error recorded:
        assert any(e.get("category") == "registry" for e in report["errors"])
        # Orphan dirs skipped — deleting dirs without registry knowledge is unsafe:
        orphan_dirs = report["categories"][gc.CAT_ORPHAN_WORKSPACE_DIRS]
        assert orphan_dirs["errors"] == [{"id": None, "error": "registry unavailable; skipped"}]
        assert orphan_dirs["removed"] == []
        assert ghost.is_dir()
        # Orphan volumes skipped for the same reason:
        volumes = report["categories"][gc.CAT_ORPHAN_VOLUMES]
        assert volumes["errors"] == [{"id": None, "error": "registry unavailable; skipped"}]
        # User-container sweep skipped too:
        containers = report["categories"][gc.CAT_ORPHAN_RESOURCE_CONTAINERS]
        assert any("user-container sweep skipped" in e.get("error", "")
                   for e in containers["errors"])


class TestDockerUnavailable:
    def test_docker_unavailable_reports_errors(self, vault, monkeypatch):
        monkeypatch.setattr(gc, "_docker_client", lambda: None)
        report = gc.run_gc(now=NOW, registry=FakeRegistry([]), docker_client=None)
        container_errs = [e for e in report["errors"]
                          if e.get("category") == gc.CAT_ORPHAN_RESOURCE_CONTAINERS]
        volume_errs = [e for e in report["errors"]
                       if e.get("category") == gc.CAT_ORPHAN_VOLUMES]
        assert container_errs and "docker unavailable" in container_errs[0]["error"]
        assert volume_errs and "docker unavailable" in volume_errs[0]["error"]


# ── Env thresholds ─────────────────────────────────────────────────────────

class TestEnvThresholds:
    def test_env_thresholds_override_stale_window(self, vault, monkeypatch):
        calls = []
        monkeypatch.setattr(
            gc, "delete_workspace", lambda ws_id: calls.append(ws_id) or {"errors": []}
        )
        monkeypatch.setenv("TM_GC_STALE_WORKSPACE_DAYS", "30")
        monkeypatch.setenv("TM_GC_ACTIVE_WINDOW_DAYS", "0")
        entry = FakeEntry(id="ws-1", last_opened=_iso(STALE))  # 40 days old
        report = gc.run_gc(now=NOW, registry=FakeRegistry([entry]),
                           docker_client=FakeClient())
        cat = report["categories"][gc.CAT_STALE_WORKSPACES]
        assert cat["removed"] == ["ws-1"]
        assert calls == ["ws-1"]
